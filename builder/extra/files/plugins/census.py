"""census - an anonymous, serverless count of active Kestrels.

Each hour an enabled device writes one small report to the public BitTorrent
DHT (BEP 44 mutable items), and reads everyone's reports back, so every device
can show how many Kestrels are in use, which releases they run, how their
learning agents are set up, and how often a model trained on a desktop beats
one trained on the device. There is no server: the reports live in 64 public
"slots" per UTC day whose keys anyone can derive from the date (the format is
``kestrel-census/1``, documented in docs/developer/census-protocol.md).

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

The numbers are approximate and unverified: anyone can write to a public
location, and only devices with this plugin enabled are counted.

Opt-in: it sends data off the device, so it declares the ``network`` capability
and enabling it takes the authorized-use acknowledgment. It ships disabled,
with no ``config.toml`` table.

The DHT client and the census format live in this plugin's helper files,
``_census_dht.py`` and ``_census_proto.py``.
"""

from __future__ import annotations

import os
import re
import threading

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
        "poll_hours": {
            "type": "int", "default": 4,
            "help": "How often to fetch the whole census (1-24 hours).",
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
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._routers, bad = parse_bootstrap(self.options.get("bootstrap"))
        self._bad_bootstrap = bad
        self._set_health()

    def on_unload(self):
        self._stop.set()
        self._wake.set()

    # -- the census service ----------------------------------------------
    def provide_census(self):
        """The fleet totals for other plugins, or None before the first read."""
        return None

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

    # -- the page ----------------------------------------------------------
    def on_webhook(self, path, request):
        if path not in ("", "index"):
            return self.context.page("Census", "<p>There's nothing here.</p>", status=404)
        return self.context.page("Census", "<p>The census hasn't been read yet.</p>")

    # -- helpers -----------------------------------------------------------
    def _poll_hours(self):
        try:
            return min(24, max(1, int(self.options.get("poll_hours", 4))))
        except (TypeError, ValueError):
            return 4

    def _daily_bytes(self):
        """The day's data budget in bytes; 0 means no limit."""
        try:
            return max(0, int(self.options.get("daily_mb", 5))) * 1024 * 1024
        except (TypeError, ValueError):
            return 5 * 1024 * 1024

    def _state_dir(self):
        import pwd
        home = pwd.getpwuid(os.getuid()).pw_dir
        return os.path.join(home, ".local", "state", "kestrel-census")

    def _set_health(self):
        if not self._routers:
            self.context.health_note = (
                "no usable bootstrap router: set bootstrap to host:port entries")
        elif self._bad_bootstrap:
            self.context.health_note = (
                "ignoring bootstrap entries that aren't host:port: %s"
                % ", ".join(self._bad_bootstrap))
        else:
            self.context.health_note = ""
