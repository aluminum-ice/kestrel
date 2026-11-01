"""census - an anonymous, serverless count of active Kestrels.

Each hour an enabled device writes one small report to the public BitTorrent
DHT (BEP 44 mutable items), and reads everyone's reports back, so every device
can show how many Kestrels are in use, which releases they run, how their
learning agents are set up, and how often a model trained on a desktop beats
one trained on the device. There is no server: the reports live in 64 public
"slots" per UTC day, keyed by the date (the format is ``kestrel-census/1``,
documented in docs/developer/census-protocol.md).

What leaves the device, each hour, in a 20-byte report:

- a random token, new every UTC day, so a device counts once a day and its
  days aren't linked;
- the release version (``/etc/kestrel/version``);
- the learning agent's state (off, scripted sweep or learned) and algorithm;
- whether remote training is set up and working (a code from 0 to 4, from the
  ``remote-train`` plugin's ``remote-training`` service: never the training
  host's name or address);
- how many model contests the on-device and remote candidates won and lost
  since UTC midnight;
- a proof-of-work nonce.

No capture data, network names, locations or handshake counts are ever part of
it. The DHT nodes that store a slot (and the ones a lookup passes through) see
the internet address of each device that reads or writes it, as any internet
service would; with ``tether_only`` on (the default) every census packet goes
out over the phone's Bluetooth tether, so that address is the mobile carrier's
shared one. The plugin sends no client version and answers no DHT queries
(BEP 43 read-only mode).

The numbers are approximate and unverified, and only devices with this plugin
enabled are counted.

Opt-in: it sends data off the device, so it declares the ``network`` capability
and enabling it takes the authorized-use acknowledgment. It ships disabled,
with no ``config.toml`` table.

The DHT client and the census format live in this plugin's helper files,
``_census_dht.py`` and ``_census_proto.py``.
"""

from __future__ import annotations

import json
import os
import random
import re
import subprocess
import threading
import time

from kestrel_pi import Plugin, device_version, load_helper

dht = load_helper(__file__, "_census_dht.py")
proto = load_helper(__file__, "_census_proto.py")

_DAY = 86400
# The agent rewrites its status every decision (30 s by default) and when a
# training run starts; a status older than this says it stopped without
# saying so (a crash, or the service killed).
_AGENT_STALE_SECONDS = 6 * 3600
# Remote training counts as failing when the host hasn't been reached for a
# day, and as working while it delivered a model in the last week: a desktop
# that's off overnight, or only on now and then, still counts as working.
_REMOTE_CONTACT_SECONDS = _DAY
_REMOTE_MODEL_SECONDS = 7 * _DAY

_TETHER = "bnep0"
_CLOCK_SYNCED = "/run/systemd/timesync/synchronized"
_PAUSE_SECONDS = 60.0         # how often the gates are checked while paused
_MAX_SLEEP = 300.0
_JOIN_RETRY = 600.0           # after a failed join
_FRESH_NODES = 8              # nodes that answered recently, to skip a bootstrap
# The first write after the census starts, or comes back from a pause, waits
# this long (seconds), so a device doesn't report the moment it comes online.
_FIRST_WRITE = (300.0, 1800.0)
_FIRST_READ = (60.0, 600.0)
_CHECK_AFTER = (20.0, 60.0)   # a write's check follows it by this long
_UPKEEP = (600.0, 1200.0)     # node-pool upkeep, every 15 minutes or so
_HISTORY_DAYS = 30

_HOST_RE = re.compile(r"^(?=.{1,253}$)[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?$")


def parse_bootstrap(raw):
    """``(routers, bad)`` from a comma-separated ``host:port`` list: each good
    entry as ``(host, port)``, and the entries that aren't one."""
    routers, bad = [], []
    for item in str(raw or "").split(","):
        item = item.strip()
        if not item:
            continue
        host, sep, port = item.rpartition(":")
        if (sep and _HOST_RE.match(host) and port.isdigit()
                and 1 <= int(port) <= 65535):
            if (host, int(port)) not in routers:
                routers.append((host, int(port)))
        else:
            bad.append(item[:64])
    return routers, bad


def _num(value):
    if isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _utc(now):
    """``(day, hour, minute of the day)`` in UTC."""
    t = time.gmtime(now)
    return time.strftime("%Y-%m-%d", t), t.tm_hour, t.tm_hour * 60 + t.tm_min


def _next_hour(now):
    return (int(now) // 3600 + 1) * 3600.0


_synced_cache = [0.0, False]


def _clock_synced(now):
    """True once systemd-timesyncd has set the clock (the census keys its slots
    by the UTC date, so a wrong clock writes to the wrong day). Cached for a
    minute."""
    if now - _synced_cache[0] < 60:
        return _synced_cache[1]
    synced = os.path.exists(_CLOCK_SYNCED)
    if not synced:
        try:
            out = subprocess.run(["timedatectl", "show", "-p", "NTPSynchronized", "--value"],
                                 capture_output=True, text=True, timeout=5, check=False)
            synced = out.stdout.strip() == "yes"
        except (OSError, subprocess.SubprocessError):
            synced = False
    _synced_cache[:] = [now, synced]
    return synced


def _int_in(value, low, high):
    return isinstance(value, int) and not isinstance(value, bool) and low <= value <= high


def _hex(value, size):
    if not isinstance(value, str) or len(value) != size * 2:
        return False
    try:
        bytes.fromhex(value)
    except ValueError:
        return False
    return True


def _is_day(value):
    if not isinstance(value, str) or len(value) != 10:
        return False
    try:
        time.strptime(value, "%Y-%m-%d")
    except ValueError:
        return False
    return True


def _valid_day(data, bits=proto.POW_BITS):
    """The saved day state, or None when it's missing or malformed (a new day
    starts then; a broken file is never fatal)."""
    if not isinstance(data, dict) or not _is_day(data.get("day")):
        return None
    if (not _hex(data.get("token"), 8) or not _int_in(data.get("slot"), 0, proto.SLOTS - 1)
            or not _int_in(data.get("alt"), 0, proto.ALTERNATES)
            or not _int_in(data.get("bytes"), 0, 1 << 40)):
        return None
    day = {"day": data["day"], "token": data["token"], "slot": data["slot"],
           "alt": data["alt"], "bytes": data["bytes"], "nonce": None, "remote": None,
           "baseline": None, "cache": {}, "wrote": None}
    if _hex(data.get("nonce"), 4) and proto.pow_ok(
            day["day"], bytes.fromhex(day["token"]), bytes.fromhex(data["nonce"]), bits):
        day["nonce"] = data["nonce"]
    if data.get("remote") in proto.REMOTE_STATES:
        day["remote"] = data["remote"]
    base = data.get("baseline")
    if isinstance(base, list) and len(base) == 4 and all(_int_in(v, 0, 1 << 31) for v in base):
        day["baseline"] = base
    if isinstance(data.get("cache"), dict):
        day["cache"] = dht.SlotCache(data["cache"]).to_json()
    if _int_in(data.get("wrote"), 0, 23):
        day["wrote"] = data["wrote"]
    return day


def _valid_row(row):
    return (isinstance(row, list) and len(row) == 7 and _int_in(row[0], 0, 23)
            and _int_in(row[1], 0, 0xFFFF) and _int_in(row[2], 0, 0xFF)
            and all(_int_in(c, 0, 255) for c in row[3:]))


def _valid_seen(data):
    """``{day: {"entries": {token hex: row}, "read_at", "online", "rejected"}}``
    from seen.json, keeping only well-formed parts."""
    days = data.get("days") if isinstance(data, dict) else None
    out = {}
    for day, seen in (days.items() if isinstance(days, dict) else ()):
        if (not _is_day(day) or not isinstance(seen, dict)
                or not isinstance(seen.get("entries"), dict)):
            continue
        entries = {t: r for t, r in seen["entries"].items() if _hex(t, 8) and _valid_row(r)}
        read_at = seen.get("read_at")
        out[day] = {"entries": entries,
                    "read_at": float(read_at) if _num(read_at) is not None else None,
                    "online": seen["online"] if _int_in(seen.get("online"), 0, 1 << 20) else 0,
                    "rejected": seen["rejected"] if _int_in(seen.get("rejected"), 0, 1 << 30) else 0}
    return out


def _valid_last(data):
    """``(last_write, last_read)`` from status.json, each None when missing or
    malformed: the last push (``at``, ``ok``, ``held``) and the last read
    (``at``, ``complete``), kept so the page shows them after a restart."""
    data = data if isinstance(data, dict) else {}
    write, read = data.get("write"), data.get("read")
    last_write = last_read = None
    if isinstance(write, dict) and _num(write.get("at")) is not None:
        held = write.get("held")
        held = (tuple(held) if isinstance(held, list) and len(held) == 2
                and all(_int_in(v, 0, 1 << 10) for v in held) else None)
        last_write = {"at": float(write["at"]), "ok": write.get("ok") is True, "held": held}
    if isinstance(read, dict) and _num(read.get("at")) is not None:
        last_read = {"at": float(read["at"]), "complete": read.get("complete") is not False}
    return last_write, last_read


def _valid_counts(value):
    return isinstance(value, list) and len(value) == 4 and all(_int_in(v, 0, 1 << 31) for v in value)


def _valid_history(data):
    """``{day: row}`` from history.json, keeping only well-formed rows."""
    days = data.get("days") if isinstance(data, dict) else None
    out = {}
    for day, row in (days.items() if isinstance(days, dict) else ()):
        if (_is_day(day) and isinstance(row, dict)
                and all(_int_in(row.get(k), 0, 1 << 20) for k in ("active", "online", "capped"))
                and _valid_counts(row.get("contests")) and _valid_counts(row.get("working"))):
            out[day] = {k: row[k] for k in ("active", "online", "capped", "contests", "working")}
    return out


def _sum_counts(rows, key):
    return [sum(row[key][i] for row in rows) for i in range(4)]


def contest_line(week):
    """The card's contest line, from the week's ``[local won, local lost,
    remote won, remote lost]``."""
    judged = week[2] + week[3]
    if not judged:
        return "no training contests this week"
    return "desktop won %d of %d training contests this week" % (week[2], judged)


def _won(won, lost):
    judged = won + lost
    if not judged:
        return "\u2014"
    return "%d of %d (%d%%)" % (won, judged, round(100 * won / judged))


def _when(ts, now):
    """``2026-11-01 18:23 UTC (9 min ago)``: the full UTC date and time, since
    the last push or read may be from an earlier day."""
    stamp = time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(ts))
    ago = max(0, int(now - ts))
    if ago < 60:
        rel = "just now"
    elif ago < 3600:
        rel = "%d min ago" % (ago // 60)
    elif ago < 2 * _DAY:
        rel = "%dh %02dm ago" % (ago // 3600, ago % 3600 // 60)
    else:
        rel = "%d days ago" % (ago // _DAY)
    return "%s (%s)" % (stamp, rel)


def _size(count):
    if count >= 1024 * 1024:
        return "%.1f MB" % (count / (1024 * 1024))
    return "%d KB" % round(count / 1024)


def _card(title, lines, esc):
    """A card of short lines. The page uses these rather than tables: the
    shared stylesheet turns every table into a stack of cards at phone width,
    which makes short counts very long."""
    return ('<div class="card pad"><h2>%s</h2>' % esc(title)
            + "".join("<p>%s</p>" % esc(line) for line in lines) + "</div>")


def _shares(rows, total):
    """``label: N (S%)`` lines, each with its share of ``total``."""
    return ["%s: %d (%d%%)" % (label, count, round(100 * count / total) if total else 0)
            for label, count in rows]


# -- what this device reports ----------------------------------------------------

def agent_fields(status, now):
    """``(state, algorithm)`` codes from the ``agent`` service (the agent's
    ``status.json``, or None). The algorithm is the configured one, whatever
    the state."""
    if not isinstance(status, dict):
        return proto.AGENT_NONE, 0
    algorithm = proto.ALGORITHMS.get(status.get("algorithm"), 0)
    updated = _num(status.get("updated"))
    if (not status.get("running")
            or (updated is not None and now - updated > _AGENT_STALE_SECONDS)):
        return proto.AGENT_STOPPED, algorithm
    if status.get("policy") == "learned":
        return proto.AGENT_LEARNED, algorithm
    return proto.AGENT_SWEEP, algorithm


def remote_state(service, now):
    """The remote-training code (0-4) from the ``remote-training`` service."""
    if not isinstance(service, dict):
        return proto.REMOTE_OFF
    if not service.get("configured"):
        return proto.REMOTE_UNCONFIGURED
    contact = _num(service.get("last_contact"))
    if (service.get("stack") == "failed" or contact is None
            or now - contact > _REMOTE_CONTACT_SECONDS):
        return proto.REMOTE_FAILING
    model = _num(service.get("last_model"))
    if model is None or now - model > _REMOTE_MODEL_SECONDS:
        return proto.REMOTE_IDLE
    return proto.REMOTE_WORKING


def contest_counts(status):
    """The agent's lifetime contest results, ``[local won, local lost, remote
    won, remote lost]``, or None without a status. The adoption counts from
    before contests (``before_fair_promotion``) aren't counted."""
    if not isinstance(status, dict):
        return None
    tally = status.get("promotion_tally")
    tally = tally if isinstance(tally, dict) else {}
    counts = []
    for source in ("local", "remote"):
        side = tally.get(source)
        side = side if isinstance(side, dict) else {}
        for key in ("won", "lost"):
            value = _num(side.get(key))
            counts.append(max(0, int(value)) if value is not None else 0)
    return counts


def contests_today(counts, baseline):
    """``(today, baseline)``: the contests since the day's ``baseline`` (the
    lifetime counts at UTC midnight), each saturating at 255. With no baseline
    yet, or a tally that went down (the agent's state was reset), the current
    counts become the baseline."""
    if counts is None:
        return [0, 0, 0, 0], baseline
    if (not isinstance(baseline, list) or len(baseline) != 4
            or any(now < then for now, then in zip(counts, baseline))):
        baseline = list(counts)
    return [min(255, now - then) for now, then in zip(counts, baseline)], baseline


class Census(Plugin):
    __author__ = "kestrel"
    __version__ = "0.1.0"
    __license__ = "GPL3"
    __description__ = ("Anonymous, serverless count of active Kestrels: "
                       "releases, learning setups and model contests.")

    __target_api__ = 2
    __capabilities__ = ("network", "ui", "web")
    __provides__ = ("census",)
    # The agent's state and contest tally, and remote training's state, for
    # this device's report. Both optional: without them the report says so.
    __optional_requires__ = ("agent", "remote-training")
    # One row button on the Plugins page: "Census" shows the census page.
    __actions__ = ({"label": "Census", "path": "index", "method": "get"},)
    __options__ = {
        "report": {
            "type": "bool", "default": True,
            "help": "Add this device to the census each hour.",
        },
        "read": {
            "type": "bool", "default": True,
            "help": "Fetch the census and show the totals. Off, the plugin "
                    "only reports, and uses much less data.",
        },
        # An entry stays in its slot 60-120 minutes after its device's last
        # write, so reading every 90 minutes catches nearly every device.
        "poll_minutes": {
            "type": "int", "default": 90,
            "help": "How often to fetch the whole census, in minutes (60-1440).",
        },
        "tether_only": {
            "type": "bool", "default": True,
            "help": "Send and fetch census data only over your phone's "
                    "Bluetooth tether. The computers that store the census can "
                    "see the internet address of every device that reports or "
                    "reads. Over the tether that's your mobile carrier's shared "
                    "address, which says little about you. Over home or office "
                    "Wi-Fi it's that network's own address, which can point to "
                    "where you are. While this is on, the census pauses whenever "
                    "the tether is down.",
        },
        "daily_mb": {
            "type": "int", "default": 5,
            "help": "Data budget per UTC day, in MB (0 = no limit). Over it, "
                    "the plugin stops fetching; over half as much again, it "
                    "stops reporting too.",
        },
        # The public DHT bootstrap routers, used only on a device's first join
        # (or when its saved node pool has gone stale). Two projects run them;
        # one answer is enough.
        "bootstrap": {
            "type": "str", "default": "dht.transmissionbt.com:6881,dht.libtorrent.org:25401",
            "help": "Comma-separated host:port DHT routers, used only for the "
                    "first join or when the saved node list has gone stale.",
        },
    }

    # -- lifecycle -------------------------------------------------------
    def on_loaded(self):
        self.log = self.context.logger
        self._dht = dht
        self._proto = proto
        self._routers, bad = parse_bootstrap(self.options.get("bootstrap"))
        self._bad_bootstrap = bad
        self._init_state()
        self._set_health()

    def _init_state(self):
        """Every piece of runtime state, set in one place. The tests replace
        the seams (clock, transport, resolver, address filter, proof-of-work
        difficulty) and drive ``_tick`` without the worker thread."""
        self._lock = threading.Lock()         # guards what the page and card read
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._worker = None
        # Seams.
        self._now = time.time
        self._rng = random.SystemRandom()
        self._open_transport = dht.UdpTransport
        self._resolve = dht.resolve
        self._accept = dht.public_ipv4
        self._bits = proto.POW_BITS
        self._backoff = dht.BOOTSTRAP_BACKOFF
        self._client_opts = {}
        self._clock_synced = _clock_synced
        # The link: the socket, the KRPC client and the node pool.
        self._client = None
        self._pool = None
        self._joined = False
        self._join_after = 0.0
        self._counted = 0                     # client bytes already in the day's total
        # Today's state (day.json), the entries seen (seen.json), the schedule.
        self._day = None
        self._seen = None
        self._next_write = None
        self._next_read = None
        self._next_upkeep = 0.0
        self._check_at = None
        self._check_retry = False
        self._last_counts = None              # the agent's last contest tally reading
        self._history = {}                    # history.json: {day: row}, 30 days
        self._summary = None                  # what the page, card and service read
        # What the page shows.
        self._paused = "starting"
        self._link_error = ""
        self._last_write = None
        self._last_read = None

    def on_ready(self):
        self._worker = threading.Thread(target=self._run, name="census-worker", daemon=True)
        self._worker.start()

    def on_unload(self):
        self._stop.set()
        self._wake.set()

    def on_internet_available(self):
        self._wake.set()

    # -- the census service ----------------------------------------------
    def provide_census(self):
        """The ``census`` service: the fleet's totals for other plugins (the
        e-ink face may show them). A new dict on every call; each value is None
        until the census has been read. Keys may be added later; existing keys
        keep their meaning.

        - ``online``: Kestrels that reported this hour or the last.
        - ``active_today``: Kestrels that reported today (UTC).
        - ``active_7d_avg``: the average of that over the last seven days.
        - ``versions``: ``{release: devices}`` today.
        - ``local_win_rate_7d``, ``remote_win_rate_7d``: the share of contests
          on-device and remote candidates won over the last seven days.
        """
        with self._lock:
            summary = self._summary
        read = summary is not None and summary["read_at"] is not None
        tally = summary["tally"] if read else None
        week = summary["week"] if summary else [0, 0, 0, 0]
        return {"online": summary["online"] if read else None,
                "active_today": tally.active if read else None,
                "active_7d_avg": summary["active_avg"] if summary else None,
                "versions": dict(tally.version_rows()) if read else None,
                "local_win_rate_7d": proto.win_rate(week[0], week[1]),
                "remote_win_rate_7d": proto.win_rate(week[2], week[3])}

    # -- worker ----------------------------------------------------------
    def _run(self):
        try:
            while not self._stop.is_set():
                try:
                    delay = self._tick()
                except Exception:  # noqa: BLE001 - the worker must keep going
                    self.log.exception("census cycle failed")
                    self._drop_link()
                    delay = _PAUSE_SECONDS
                self._wake.wait(timeout=min(max(delay, 1.0), _MAX_SLEEP))
                self._wake.clear()
        finally:
            self._drop_link()

    def _tick(self):
        """One pass: check the gates, roll the day over, join the DHT, then do
        whatever is due. Returns the seconds until something next is."""
        now = self._now()
        self._roll_day(now)
        reason = self._gate(now)
        if reason:
            if reason != self._paused:
                self.log.info("census paused: %s", reason)
            self._paused = reason
            self._drop_link()
            self._next_write = self._next_read = None
            self._set_health()
            return _PAUSE_SECONDS
        if self._paused:
            self._paused = ""
            self._set_health()
        try:
            if not self._link(now):
                return max(_PAUSE_SECONDS, self._join_after - now)
            self._due(now)
        except dht.LinkDown:
            # The tether went away mid-cycle (a phone hotspot can sleep).
            # What's done stays done; the rest happens once it's back.
            self.log.info("census: the link went down mid-cycle")
            self._paused = "the network link went down"
            self._drop_link()
            self._next_write = self._next_read = None
            self._set_health()
            return _PAUSE_SECONDS
        finally:
            self._account()
            self._save_day()
        return self._until_due(self._now())

    def _gate(self, now):
        """Why the census can't run now, or "" when it can."""
        report, read = self.options.get("report", True), self.options.get("read", True)
        if not report and not read:
            return "reporting and reading are both off"
        if not self._clock_synced(now):
            return "waiting for the clock to be set"
        if not getattr(self.context.connectivity, "online", False):
            return "offline"
        if self._tether_only() and _TETHER not in self.context.connectivity.active_paths():
            return "waiting for the phone's Bluetooth tether"
        budget = self._daily_bytes()
        if budget and self._day_bytes(now) >= budget * 1.5:
            return "over today's data budget"
        return ""

    def _until_due(self, now):
        times = [t for t in (self._next_write, self._check_at, self._next_read,
                             self._next_upkeep) if t is not None]
        return max(1.0, min(times) - now) if times else _PAUSE_SECONDS

    def _due(self, now):
        report, read = self.options.get("report", True), self.options.get("read", True)
        budget = self._daily_bytes()
        under = not budget or self._day["bytes"] < budget
        if report:
            self._plan_write(now)
            if self._check_at is not None and now >= self._check_at:
                self._check(now)
            if now >= self._next_write:
                self._write(now)
        if read:
            self._plan_read(now)
            if now >= self._next_read and under and not self._stop.is_set():
                self._read(now)
            elif now >= self._next_read:
                self._next_read = now + _PAUSE_SECONDS * 15
        if now >= self._next_upkeep and not self._stop.is_set():
            if under:
                self._upkeep(now)
            self._next_upkeep = now + self._rng.uniform(*_UPKEEP)

    # -- the schedule ------------------------------------------------------
    def _plan_write(self, now):
        """Once an hour at a random minute; the first write after the census
        starts (or comes back from a pause) waits 5-30 minutes, and an hour
        already written waits for the next."""
        if self._next_write is not None:
            return
        _day, hour, _minute = _utc(now)
        at = now + self._rng.uniform(*_FIRST_WRITE)
        if self._day.get("wrote") == hour:
            at = max(at, _next_hour(now) + self._rng.uniform(0, 3540))
        self._next_write = at

    def _plan_read(self, now):
        """A few minutes after the census starts, then every ``poll_minutes``.
        A device that reports but hasn't written yet today reads only after its
        first write has been checked, so its own entry is in what it reads."""
        if self._next_read is not None:
            return
        seen = self._seen_day(_utc(now)[0])
        read_at = seen.get("read_at") or 0.0
        at = max(now + self._rng.uniform(*_FIRST_READ),
                 read_at + self._poll_minutes() * 60)
        if (self.options.get("report", True) and self._day.get("wrote") is None
                and self._next_write is not None):
            at = max(at, self._next_write + _CHECK_AFTER[1]
                     + self._rng.uniform(*_FIRST_READ))
        self._next_read = self._apart(at)

    def _apart(self, at):
        """Keep a read out of the minute of the next write."""
        if self._next_write is not None and abs(at - self._next_write) < 60:
            at = self._next_write + 120
        return at

    # -- joining the DHT -----------------------------------------------------
    def _link(self, now):
        """True once the socket is open and the node pool fresh enough to use,
        joining (or rejoining) the DHT when it isn't."""
        if self._client is None:
            try:
                transport = self._open_transport(_TETHER if self._tether_only() else None)
            except dht.TetherBindError as exc:
                self._link_error = str(exc.strerror)
                self._join_after = now + _JOIN_RETRY
                self._set_health()
                return False
            self._client = dht.KrpcClient(transport, **self._client_opts)
            self._counted = 0
            self._joined = False
        if self._pool is None:
            self._pool = dht.NodePool(accept=self._accept, clock=self._now)
            self._pool.load(self._path("pool.json"))
        if self._joined:
            return True
        if now < self._join_after:
            return False
        if self._fresh(now, _DAY) >= _FRESH_NODES:
            dht.refresh(self._client, self._pool, self._rng)
        if self._fresh(now, 3600) < _FRESH_NODES:
            if self._bootstrap():
                dht.refresh(self._client, self._pool, self._rng)
        if self._fresh(now, 3600) < _FRESH_NODES:
            self._link_error = "can't join the DHT: no bootstrap router or node answered"
            self._join_after = now + _JOIN_RETRY
            self._set_health()
            return False
        self._joined = True
        self._link_error = ""
        self._next_upkeep = now + self._rng.uniform(*_UPKEEP)
        self._save_pool()
        self._set_health()
        return True

    def _fresh(self, now, within):
        """How many pool nodes answered in the last ``within`` seconds."""
        return sum(1 for e in self._pool.nodes.values() if e[3] and now - e[3] <= within)

    def _bootstrap(self):
        """Join through the routers: first at the addresses saved from the last
        time their names resolved (no DNS lookup), then by name. Names are
        resolved only here, after the gates, so with ``tether_only`` the lookup
        happens while the tether is up."""
        wait = self._stop.wait
        saved = self._load_routing()
        if saved and dht.bootstrap(self._client, self._pool, saved,
                                   resolve=lambda host, port: (host, port),
                                   wait=wait, backoff=(0,)):
            return True
        if not self._routers:
            return False
        resolved = []

        def resolve(host, port):
            addr = self._resolve(host, port)
            if not addr or not self._accept(addr[0]):
                return None
            resolved.append([addr[0], addr[1]])
            return addr
        if dht.bootstrap(self._client, self._pool, self._routers, resolve=resolve,
                         wait=wait, backoff=self._backoff):
            unique = [list(a) for a in dict.fromkeys(tuple(a) for a in resolved)]
            self._save_json("routing.json", {"routers": unique})
            return True
        return False

    def _load_routing(self):
        data = self._load_json("routing.json")
        rows = data.get("routers") if isinstance(data, dict) else None
        return [(r[0], r[1]) for r in rows if isinstance(r, list) and len(r) == 2
                and isinstance(r[0], str) and self._accept(r[0])
                and isinstance(r[1], int) and 0 < r[1] < 65536] if isinstance(rows, list) else []

    def _drop_link(self):
        self._joined = False
        if self._client is None:
            return
        self._account()
        try:
            self._client.transport.close()
        except OSError:
            pass
        self._client = None
        self._save_pool()

    def _save_pool(self):
        if self._pool is not None and self._pool.nodes:
            try:
                self._pool.save(self._path("pool.json"))
            except OSError as exc:
                self.log.warning("census: can't save the node list: %s", exc)

    def _upkeep(self, now):
        """Keep the node pool fresh, and note the agent's contest tally so the
        next day's baseline can start from it."""
        dht.refresh(self._client, self._pool, self._rng)
        self._last_counts = contest_counts(self.context.services.get("agent"))
        self._save_pool()

    # -- writing -----------------------------------------------------------
    def _write(self, now):
        day = self._day
        self._next_write = _next_hour(now) + self._rng.uniform(0, 3540)
        if not day.get("nonce"):
            nonce = proto.find_pow(day["day"], bytes.fromhex(day["token"]), self._rng,
                                   self._bits, should_stop=self._stop.is_set)
            if nonce is None:
                return
            day["nonce"] = nonce.hex()
            self._save_day()
        self._publish(now)
        self._check_at = now + self._rng.uniform(*_CHECK_AFTER)
        self._check_retry = True

    def _publish(self, now):
        day = self._day
        _d, hour, minute = _utc(now)
        version, agent, contests = self._facts(now, day)
        self._last_counts = contest_counts(self.context.services.get("agent"))
        entry = proto.Entry(bytes.fromhex(day["token"]), hour, version, agent, contests,
                            bytes.fromhex(day["nonce"]))
        cache = dht.SlotCache(day.get("cache"))
        try:
            done = proto.publish(dht, self._client, self._pool, cache, day["day"],
                                 day["slot"], entry, minute, self._rng, bits=self._bits,
                                 start_alt=day.get("alt", 0))
        finally:
            day["cache"] = cache.to_json()
        with self._lock:
            self._last_write = {"at": now, "ok": done.alt is not None,
                                "confirms": done.confirms, "count": done.count,
                                "held": None}
        self._save_last()
        if done.alt is None:
            self.log.info("census: couldn't write slot %d (%s)", day["slot"],
                          ", ".join(why for _alt, why in done.skipped) or "no nodes")
            return
        day["alt"] = done.alt
        day["wrote"] = hour

    def _check(self, now):
        """A write's check, 20-60 seconds on: is the entry where it was put?
        If no copy has it (another writer's put landed over it), write once more."""
        self._check_at = None
        day = self._day
        if day.get("wrote") is None or not day.get("nonce"):
            return
        cache = dht.SlotCache(day.get("cache"))
        try:
            holders, mine = proto.holds_entry(dht, self._client, self._pool, cache, day["day"],
                                              day["slot"], day.get("alt", 0),
                                              bytes.fromhex(day["token"]), bits=self._bits)
        finally:
            day["cache"] = cache.to_json()
        with self._lock:
            if self._last_write:
                self._last_write["held"] = (mine, holders)
        self._save_last()
        if mine == 0 and self._check_retry:
            self._check_retry = False
            self.log.info("census: entry missing after the write; writing it again")
            self._publish(now)
            self._check_at = now + self._rng.uniform(*_CHECK_AFTER)

    # -- reading -----------------------------------------------------------
    def _read(self, now):
        """Read every slot of today's census (and of yesterday's in the first
        two hours of a UTC day, to finish its count). What's gathered is kept
        even if the read stops part-way."""
        today, hour, _minute = _utc(now)
        days = [today] + ([_utc(now - _DAY)[0]] if hour < 2 else [])
        budget = self._daily_bytes()
        stopped = False
        try:
            for day in days:
                seen = self._seen_day(day)
                cache = dht.SlotCache(self._day.get("cache") if day == today else None)
                for index in range(proto.SLOTS):
                    self._account()
                    if self._stop.is_set() or (budget and self._day["bytes"] >= budget):
                        stopped = True
                        break
                    entries, rejected = proto.read_slot(dht, self._client, self._pool, cache,
                                                        day, index, bits=self._bits)
                    self._add_seen(seen, entries, rejected)
                if day == today:
                    self._day["cache"] = cache.to_json()
                    seen["online"] = sum(1 for row in seen["entries"].values()
                                         if row[0] in (hour, hour - 1))
                seen["read_at"] = now
                if stopped:
                    break
        finally:
            self._save_seen()
            for day in days:
                self._record(day)
            self._summarize(now)
        with self._lock:
            self._last_read = {"at": now, "complete": not stopped}
        self._save_last()
        self._next_read = self._apart(now + self._poll_minutes() * 60
                                      * self._rng.uniform(0.9, 1.1))

    @staticmethod
    def _add_seen(seen, entries, rejected):
        rows = seen["entries"]
        for token, entry in entries.items():
            row = [entry.hour, entry.version, entry.agent] + list(entry.contests)
            held = rows.get(token.hex())
            if held is None or row[0] > held[0] or (row[0] == held[0] and row > held):
                rows[token.hex()] = row
        seen["rejected"] = seen.get("rejected", 0) + rejected

    # -- state ---------------------------------------------------------------
    def _roll_day(self, now):
        """Load today's state, or start a new day: a new token and slot (the
        proof-of-work waits for the day's first write), the contest baseline
        from the last tally reading if the census was running at midnight."""
        today = _utc(now)[0]
        loaded = self._day is None
        if loaded:
            self._day = _valid_day(self._load_json("day.json"), self._bits)
            self._seen = _valid_seen(self._load_json("seen.json"))
            self._history = _valid_history(self._load_json("history.json"))
            last_write, last_read = _valid_last(self._load_json("status.json"))
            with self._lock:
                self._last_write = self._last_write or last_write
                self._last_read = self._last_read or last_read
        if self._day is not None and self._day["day"] == today:
            if loaded:
                self._summarize(now)
            return
        carried = self._day is not None and self._day["day"] == _utc(now - _DAY)[0]
        self._day = {"day": today, "token": os.urandom(8).hex(),
                     "slot": self._rng.randrange(proto.SLOTS), "alt": 0, "nonce": None,
                     "remote": None,
                     "baseline": list(self._last_counts) if carried and self._last_counts else None,
                     "cache": {}, "bytes": 0, "wrote": None}
        self._counted = (self._client.sent_bytes + self._client.received_bytes
                         if self._client is not None else 0)
        keep = {today, _utc(now - _DAY)[0]}
        self._seen = {d: v for d, v in self._seen.items() if d in keep}
        self._next_write = self._next_read = self._check_at = None
        self._save_day()
        self._save_seen()
        self._summarize(now)

    # -- totals ----------------------------------------------------------------
    def _record(self, day):
        """Put a day's totals (from the entries seen so far) in the history."""
        seen = self._seen.get(day)
        if not seen or seen.get("read_at") is None:
            return
        tally = proto.Tally(seen["entries"].values())
        old = self._history.get(day) or {}
        self._history[day] = {"active": tally.active,
                              "online": max(old.get("online", 0), seen["online"]),
                              "capped": tally.capped, "contests": tally.contests,
                              "working": tally.working}
        for stale in sorted(self._history)[:-_HISTORY_DAYS]:
            del self._history[stale]
        self._save_json("history.json", {"days": self._history})

    def _summarize(self, now):
        """Build what the page, card and service show from today's entries and
        the history, and swap it in whole, so a reader never sees it half-built."""
        today = _utc(now)[0]
        seen = self._seen.get(today) or {"entries": {}, "read_at": None, "online": 0}
        week_days = {_utc(now - i * _DAY)[0] for i in range(7)}
        week = [row for day, row in self._history.items() if day in week_days]
        month = list(self._history.values())
        summary = {
            "day": today, "read_at": seen["read_at"], "online": seen["online"],
            "tally": proto.Tally(seen["entries"].values()),
            "active_avg": round(sum(r["active"] for r in week) / len(week), 1) if week else None,
            "active_peak": max(r["active"] for r in week) if week else None,
            "week": _sum_counts(week, "contests"), "week_working": _sum_counts(week, "working"),
            "month": _sum_counts(month, "contests"),
            "month_working": _sum_counts(month, "working"),
            "history": sorted(self._history.items(), reverse=True),
        }
        with self._lock:
            self._summary = summary

    def _seen_day(self, day):
        return self._seen.setdefault(day, {"entries": {}, "read_at": None,
                                           "online": 0, "rejected": 0})

    def _day_bytes(self, now):
        if self._day is None or self._day["day"] != _utc(now)[0]:
            return 0
        return self._day["bytes"]

    def _account(self):
        """Add the client's traffic since the last call to the day's total."""
        if self._client is None or self._day is None:
            return
        total = self._client.sent_bytes + self._client.received_bytes
        self._day["bytes"] += max(0, total - self._counted)
        self._counted = total

    def _save_day(self):
        if self._day is not None:
            self._save_json("day.json", self._day)

    def _save_last(self):
        """Keep the last push and read (status.json), so the page still shows
        them after a restart or across UTC midnight."""
        with self._lock:
            write, read = self._last_write, self._last_read
            data = {"write": {"at": write["at"], "ok": write["ok"],
                              "held": list(write["held"]) if write.get("held") else None}
                    if write else None,
                    "read": dict(read) if read else None}
        self._save_json("status.json", data)

    def _save_seen(self):
        if self._seen is not None:
            self._save_json("seen.json", {"days": self._seen})

    def _path(self, name):
        return os.path.join(self._state_dir(), name)

    def _load_json(self, name):
        try:
            with open(self._path(name), "r", encoding="utf-8") as handle:
                return json.load(handle)
        except (OSError, ValueError):
            return None

    def _save_json(self, name, data):
        """Write a state file atomically, readable by its owner only."""
        try:
            os.makedirs(self._state_dir(), mode=0o700, exist_ok=True)
            path = self._path(name)
            tmp = path + ".tmp"
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(data, handle)
            os.replace(tmp, path)
        except OSError as exc:
            self.log.warning("census: can't save %s: %s", name, exc)

    # -- this device's entry ------------------------------------------------
    def _facts(self, now, day):
        """``(version, agent byte, contests)`` for this device's entry. The
        remote-training state is worked out at the day's first write and kept
        all day (in ``day``, the day's state), so a device's reports don't
        flicker; the contest baseline is kept there too."""
        services = self.context.services
        status = services.get("agent")
        state, algorithm = agent_fields(status, now)
        if day.get("remote") not in proto.REMOTE_STATES:
            day["remote"] = remote_state(services.get("remote-training"), now)
        today, day["baseline"] = contests_today(contest_counts(status), day.get("baseline"))
        return (proto.encode_version(device_version()),
                proto.encode_agent(state, algorithm, day["remote"]), today)

    # -- the card ------------------------------------------------------------
    def on_ui_setup(self, ui):
        ui.add_element("online", "-", label="Kestrels online", kind="number")
        ui.add_element("active", "-", label="Active today", kind="number")
        ui.add_element("contests", "-", label="Training contests")

    def on_ui_update(self, ui, state):
        with self._lock:
            summary = self._summary
        if not self.options.get("read", True) or summary is None or summary["read_at"] is None:
            for name in ("online", "active", "contests"):
                ui.set(name, "-")
            return
        ui.set("online", summary["online"])
        ui.set("active", summary["tally"].active)
        ui.set("contests", contest_line(summary["week"]))

    # -- the page ----------------------------------------------------------
    def on_webhook(self, path, request):
        if path not in ("", "index"):
            return self.context.page("Census", "<p>There's nothing here.</p>", status=404)
        with self._lock:
            summary = self._summary
            last_write = dict(self._last_write) if self._last_write else None
            last_read = dict(self._last_read) if self._last_read else None
            day = dict(self._day) if self._day else {}
        esc = self.context.esc
        now = self._now()
        body = ('<p class="hint">Approximate and unverified. Counts only Kestrels with the '
                "census plugin on.</p>")
        body += ('<div class="card pad"><h2>Status</h2>'
                 + "".join("<p>" + esc(line) + "</p>"
                           for line in self._status_lines(last_write, last_read, day, now))
                 + "</div>")
        if not self.options.get("read", True):
            body += "<p>Reading is off, so this device doesn't fetch the census.</p>"
        elif summary is None or summary["read_at"] is None:
            body += "<p>The census hasn't been read yet today.</p>"
        else:
            body += self._totals_html(summary, esc)
        if summary is not None and summary["history"]:
            body += self._history_html(summary, esc)
        body += self._own_report_html(day, now, esc)
        return self.context.page("Census", body)

    def _status_lines(self, last_write, last_read, day, now):
        lines = []
        if self._paused == "starting":
            lines.append("Starting.")
        elif self._paused:
            lines.append("Paused: %s." % self._paused)
        elif self._link_error:
            lines.append(self._link_error[:1].upper() + self._link_error[1:] + ".")
        else:
            lines.append("Running.")
        # Whether the census can reach the DHT now, from what the worker last
        # found (the page never probes the network itself).
        if self._tether_only():
            tether = _TETHER in (self.context.connectivity.active_paths() or ())
            lines.append("Phone's Bluetooth tether: %s." % ("up" if tether else "down"))
        else:
            lines.append("Connection: any (tether_only is off).")
        if self._paused == "starting":
            reach = "not yet, starting"
        elif self._paused:
            reach = "no, " + self._paused
        elif self._link_error:
            reach = "no, " + self._link_error
        elif not self._joined:
            reach = "not yet, joining the DHT"
        else:
            reach = "yes"
        lines.append("Can read and write the census now: %s." % reach)
        if not self.options.get("report", True):
            lines.append("Reporting is off, so this device isn't counted.")
        elif not last_write:
            lines.append("Last pushed (this device's report): not yet.")
        elif last_write["ok"]:
            held = last_write.get("held")
            lines.append("Last pushed (this device's report): %s%s." % (
                _when(last_write["at"], now),
                "; checked: %d of %d storing nodes hold it" % held if held else ""))
        else:
            lines.append("Last push, %s, couldn't be stored; it's tried again next hour."
                         % _when(last_write["at"], now))
        if not self.options.get("read", True):
            pass                        # said below the card
        elif not last_read:
            lines.append("Last retrieved (the whole census): not yet.")
        else:
            lines.append("Last retrieved (the whole census): %s%s." % (
                _when(last_read["at"], now),
                "" if last_read["complete"] else ", stopped part-way"))
        budget = self._daily_bytes()
        used = day.get("bytes", 0) if day.get("day") == _utc(now)[0] else 0
        lines.append("Data used today: %s%s." % (
            _size(used), " of a %s budget" % _size(budget) if budget else " (no limit set)"))
        return lines

    def _totals_html(self, summary, esc):
        tally = summary["tally"]
        active = tally.active
        week = ""
        if summary["active_avg"] is not None:
            week = " &middot; last 7 days: %s a day on average, %d at most" % (
                esc(summary["active_avg"]), summary["active_peak"])
        html = ('<div class="card pad"><h2>Today (UTC)</h2><p>Kestrels online: %d &middot; '
                "active today: %d%s.</p><p>%s.</p></div>"
                % (summary["online"], active, week, esc(contest_line(summary["week"]).capitalize())))
        if not active:
            return html
        html += _card("Releases", _shares(tally.version_rows(), active), esc)
        lines = _shares([(proto.AGENT_STATES[code], tally.states[code])
                         for code in sorted(tally.states, reverse=True)], active)
        if tally.algorithms:
            lines += ["Learned with " + line for line in _shares(
                [(proto.ALGORITHM_NAMES.get(code, "other"), tally.algorithms[code])
                 for code in sorted(tally.algorithms)], sum(tally.algorithms.values()))]
        html += _card("Learning agent", lines, esc)
        html += _card("Remote training", _shares(
            [(proto.REMOTE_STATES.get(code, "other"), tally.remotes[code])
             for code in sorted(tally.remotes)], active), esc)
        lines = []
        for heading, key in (("All devices", ""), ("Devices with remote training working",
                                                    "_working")):
            lines.append(heading + ":")
            for span, counts in (("today", tally.working if key else tally.contests),
                                 ("7 days", summary["week" + key]),
                                 ("30 days", summary["month" + key])):
                lines.append("%s: on-device won %s, remote won %s" % (
                    span.capitalize(), _won(counts[0], counts[1]), _won(counts[2], counts[3])))
        if tally.capped:
            lines.append("%d report(s) claimed more contests than a day can hold; their "
                         "contests aren't counted." % tally.capped)
        html += _card("Training contests", lines, esc)
        return html

    @staticmethod
    def _history_html(summary, esc):
        lines = []
        for day, row in summary["history"]:
            c = row["contests"]
            lines.append("%s: %d active, %d online at most; on-device won %s, remote won %s"
                         % (day, row["active"], row["online"], _won(c[0], c[1]),
                            _won(c[2], c[3])))
        return _card("Last 30 days (UTC)", lines, esc)

    def _own_report_html(self, day, now, esc):
        """What this device puts in the census today, in words: exactly the
        fields that leave it, read the same way the worker reads them."""
        preview = {k: day.get(k) for k in ("remote", "baseline")}
        version, agent, contests = self._facts(now, preview)
        state, algorithm, remote = proto.decode_agent(agent)
        setup = proto.AGENT_STATES[state]
        if algorithm:
            setup += " (%s)" % proto.ALGORITHM_NAMES[algorithm]
        lines = ["Release: " + proto.decode_version(version),
                 "Learning agent: " + setup,
                 "Remote training: " + proto.REMOTE_STATES[remote],
                 "Contests today: on-device won %d, lost %d; remote won %d, lost %d"
                 % tuple(contests),
                 "Sent over: " + ("the phone's Bluetooth tether only" if self._tether_only()
                                  else "any connection"),
                 "Also sent: a random number that changes every UTC day, the hour of the "
                 "report, and a proof-of-work. Nothing from your captures, no network "
                 "names, no location."]
        html = _card("What this device reports today", lines, esc)
        if not self.options.get("report", True):
            html += "<p>Reporting is off, so none of this is sent.</p>"
        return html

    # -- helpers -----------------------------------------------------------
    def _poll_minutes(self):
        try:
            return min(1440, max(60, int(self.options.get("poll_minutes", 90))))
        except (TypeError, ValueError):
            return 90

    def _daily_bytes(self):
        """The day's data budget in bytes; 0 means no limit."""
        try:
            return max(0, int(self.options.get("daily_mb", 5))) * 1024 * 1024
        except (TypeError, ValueError):
            return 5 * 1024 * 1024

    def _tether_only(self):
        return bool(self.options.get("tether_only", True))

    def _state_dir(self):
        import pwd
        home = pwd.getpwuid(os.getuid()).pw_dir
        return os.path.join(home, ".local", "state", "kestrel-census")

    def _set_health(self):
        if self._paused and self._paused != "starting":
            self.context.health_note = "paused: " + self._paused
        elif self._link_error:
            self.context.health_note = self._link_error
        elif not self._routers:
            self.context.health_note = (
                "no usable bootstrap router: set bootstrap to host:port entries")
        elif self._bad_bootstrap:
            self.context.health_note = (
                "ignoring bootstrap entries that aren't host:port: %s"
                % ", ".join(self._bad_bootstrap))
        else:
            self.context.health_note = ""
