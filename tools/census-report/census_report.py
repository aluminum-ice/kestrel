#!/usr/bin/env python3
"""Read the Kestrel census from the public DHT and print it as plain tables.

The ``census`` plugin's devices each write a small, anonymous report to the
BitTorrent DHT every hour (the ``kestrel-census/1`` format, in
docs/developer/census-protocol.md). This script reads the same public slots a
device reads, from a computer with a checkout of this repository, and prints
how many Kestrels reported, which releases they run, how their learning agents
are set up, and how their training contests went. Nothing reports to it, and
it writes nothing to the DHT.

It loads the plugin's own helper files (``_census_dht.py`` and
``_census_proto.py`` under ``builder/extra/files/plugins/``) by path, so the
script and the devices can't disagree about the format. It needs Python 3 and
``cryptography``.

    python3 tools/census-report/census_report.py [--day YYYY-MM-DD] [--csv DIR]

The numbers are approximate and unverified: anyone can write to a public slot,
and only devices with the census plugin on are counted.
"""

import argparse
import csv
import importlib.util
import os
import random
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
PLUGINS = os.path.normpath(os.path.join(HERE, "..", "..", "builder", "extra", "files", "plugins"))
DEFAULT_BOOTSTRAP = "dht.transmissionbt.com:6881,dht.libtorrent.org:25401"
DEFAULT_STATE = os.path.join("~", ".cache", "kestrel-census-report")
FRESH_NODES = 8


def load_helpers(plugins=PLUGINS):
    """``(dht, proto)``: the census plugin's helper files, loaded by path."""
    mods = []
    for name in ("_census_dht.py", "_census_proto.py"):
        spec = importlib.util.spec_from_file_location(
            "census_report_" + name[len("_census_"):-3], os.path.join(plugins, name))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        mods.append(mod)
    return mods[0], mods[1]


def parse_routers(raw):
    routers = []
    for item in (raw or "").split(","):
        host, sep, port = item.strip().rpartition(":")
        if sep and host and port.isdigit() and 0 < int(port) < 65536:
            routers.append((host, int(port)))
    return routers


def join(dht, client, pool, routers, now, resolve=None, backoff=(0, 10, 30), wait=None):
    """True once enough of the pool answers: the saved pool first, then the
    bootstrap routers."""
    def fresh(within):
        return sum(1 for e in pool.nodes.values() if e[3] and now() - e[3] <= within)
    rng = random.SystemRandom()
    if fresh(86400) >= FRESH_NODES:
        dht.refresh(client, pool, rng)
    if fresh(3600) < FRESH_NODES:
        if dht.bootstrap(client, pool, routers, resolve=resolve or dht.resolve,
                         wait=wait, backoff=backoff):
            dht.refresh(client, pool, rng)
    return fresh(3600) >= FRESH_NODES


def collect(dht, proto, client, pool, day, slots, bits):
    """``({token: Entry}, rejected)`` across a day's slots."""
    entries, rejected = {}, 0
    for index in range(slots):
        found, bad = proto.read_slot(dht, client, pool, None, day, index, bits=bits)
        rejected += bad
        for token, entry in found.items():
            held = entries.get(token)
            if held is None or entry.hour > held.hour:
                entries[token] = entry
    return entries, rejected


def rows_of(entries):
    return [[e.hour, e.version, e.agent] + list(e.contests) for e in entries.values()]


def _won(won, lost):
    return "%d of %d" % (won, won + lost)


def _table(header, rows):
    """Plain text columns: the first left-aligned, the rest right-aligned."""
    cells = [header] + [[str(c) for c in row] for row in rows]
    widths = [max(len(row[i]) for row in cells) for i in range(len(header))]
    out = []
    for row in cells:
        out.append("  ".join([row[0].ljust(widths[0])]
                             + [c.rjust(w) for c, w in zip(row[1:], widths[1:])]).rstrip())
    return "\n".join(out)


def sections(proto, tally):
    """``[(title, header, rows)]``: the report's tables."""
    total = tally.active

    def share(count, of=total):
        return "%d%%" % round(100 * count / of) if of else "-"
    learned = sum(tally.algorithms.values())
    return [
        ("Release", ["Release", "Devices", "Share"],
         [[name, n, share(n)] for name, n in tally.version_rows()]),
        ("Agent", ["Agent", "Devices", "Share"],
         [[proto.AGENT_STATES[code], tally.states[code], share(tally.states[code])]
          for code in sorted(tally.states, reverse=True)]),
        ("Learned algorithm", ["Learned algorithm", "Devices", "Share"],
         [[proto.ALGORITHM_NAMES.get(code, "other"), tally.algorithms[code],
           share(tally.algorithms[code], learned)] for code in sorted(tally.algorithms)]),
        ("Remote training", ["Remote training", "Devices", "Share"],
         [[proto.REMOTE_STATES.get(code, "other"), tally.remotes[code],
           share(tally.remotes[code])] for code in sorted(tally.remotes)]),
        ("Training contests", ["Training contests today", "On-device won", "Remote won"],
         [["all devices", _won(*tally.contests[:2]), _won(*tally.contests[2:])],
          ["remote training working", _won(*tally.working[:2]), _won(*tally.working[2:])]]),
    ]


def render(proto, day, tally, read_at, today, rejected=0):
    lines = ["Kestrel census, %s (UTC), read %s - approximate and unverified"
             % (day, time.strftime("%H:%MZ", time.gmtime(read_at)))]
    if not tally.active:
        lines.append("No reports found.")
        return "\n".join(lines) + "\n"
    online = "online now %d · " % tally.online if today else ""
    lines.append("Devices: %sreported %s %d" % (online, "today" if today else "that day",
                                                 tally.active))
    for title, header, rows in sections(proto, tally):
        if rows:
            lines += ["", _table(header, rows)]
    notes = []
    if tally.capped:
        notes.append("%d report(s) claimed more contests than a day can hold; their "
                     "contests aren't counted." % tally.capped)
    if rejected:
        notes.append("%d malformed or unproven entries were skipped." % rejected)
    if notes:
        lines += [""] + notes
    return "\n".join(lines) + "\n"


def write_csv(proto, directory, day, tally):
    """``DIR/census-DAY.csv``: one row per count, rewritten on each run."""
    path = os.path.join(directory, "census-%s.csv" % day)
    os.makedirs(directory, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as handle:
        out = csv.writer(handle)
        out.writerow(["day", "table", "item", "value"])
        out.writerow([day, "devices", "reported", tally.active])
        out.writerow([day, "devices", "online", tally.online])
        for title, _header, rows in sections(proto, tally):
            for row in rows:
                if title == "Training contests":
                    out.writerow([day, title, row[0] + ", on-device won", row[1]])
                    out.writerow([day, title, row[0] + ", remote won", row[2]])
                else:
                    out.writerow([day, title, row[0], row[1]])
    return path


def main(argv=None, open_transport=None, resolve=None, now=time.time, accept=None,
         bits=None, client_opts=None, wait=None, out=sys.stdout):
    """Run the report. The keyword arguments are for the tests (a fake DHT)."""
    parser = argparse.ArgumentParser(description="Print the Kestrel census for a UTC day.")
    parser.add_argument("--day", help="the UTC day, YYYY-MM-DD (default: today)")
    parser.add_argument("--state", default=DEFAULT_STATE,
                        help="where to keep the DHT node list (default: %(default)s)")
    parser.add_argument("--csv", metavar="DIR", help="also write DIR/census-DAY.csv")
    parser.add_argument("--bootstrap", default=DEFAULT_BOOTSTRAP,
                        help="comma-separated host:port DHT routers (default: %(default)s)")
    parser.add_argument("--slots", type=int, default=None, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)

    dht, proto = load_helpers()
    started = now()
    today = time.strftime("%Y-%m-%d", time.gmtime(started))
    day = args.day or today
    try:
        time.strptime(day, "%Y-%m-%d")
    except ValueError:
        parser.error("--day must be YYYY-MM-DD")
    slots = min(proto.SLOTS, max(1, args.slots or proto.SLOTS))
    bits = bits or proto.POW_BITS

    state = os.path.expanduser(args.state)
    os.makedirs(state, mode=0o700, exist_ok=True)
    pool_path = os.path.join(state, "pool.json")
    pool = dht.NodePool(accept=accept or dht.public_ipv4, clock=now)
    pool.load(pool_path)
    transport = (open_transport or dht.UdpTransport)(None)
    client = dht.KrpcClient(transport, **(client_opts or {}))
    try:
        if not join(dht, client, pool, parse_routers(args.bootstrap), now, resolve=resolve,
                    wait=wait):
            print("Can't join the DHT: no bootstrap router or saved node answered.",
                  file=sys.stderr)
            return 2
        entries, rejected = collect(dht, proto, client, pool, day, slots, bits)
    finally:
        pool.save(pool_path)
        transport.close()
    hour = time.gmtime(started).tm_hour
    tally = proto.Tally(rows_of(entries), hour=hour if day == today else None)
    out.write(render(proto, day, tally, started, day == today, rejected))
    if day == today and hour < 2:
        # A device's token is new each day, so two days can't be added up.
        out.write("Early in the UTC day: run with --day %s for yesterday's full count.\n"
                  % time.strftime("%Y-%m-%d", time.gmtime(started - 86400)))
    if args.csv:
        out.write("Wrote %s\n" % write_csv(proto, args.csv, day, tally))
    return 0


if __name__ == "__main__":
    sys.exit(main())
