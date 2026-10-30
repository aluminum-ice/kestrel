"""A demo Kestrel device for the guide's screenshots.

Runs the release's own daemon and web app (``kestrel_pi`` and the bundled
plugins, exported from the release tag) on loopback with made-up data: a
capture store of fictional networks on documentation MAC addresses
(00:00:5E:00:53:xx, RFC 7042), a learning agent mid-session, and plugin
readouts. Only the device touch points are redirected (systemctl, /etc and
/var/lib files, interface addresses, the root auth socket) and the plugins'
``on_loaded`` hooks are skipped, since those talk to hardware. Labels, layout
and every rendered string come from the release's code, so a screenshot shows
what that release really draws. When the release changes something this
module reaches into, it fails loudly instead of rendering a misleading page.
"""

import copy
import json
import logging
import os
import sys
import threading
import time

# Fictional networks: (essid, [(bssid suffix, handshakes, minutes since last)]).
NETWORKS = [
    ("Airport Free", [(0x11, 2, 1500), (0x12, 2, 1560), (0x13, 1, 1620)]),
    ("Bus Depot", [(0x21, 1, 2900)]),
    ("Cafe Guest", [(0x31, 3, 12), (0x32, 1, 95)]),
    ("Corner Bookshop", [(0x41, 2, 300)]),
    ("Harbor Hotel", [(0x51, 2, 40), (0x52, 1, 44)]),
    ("Library WiFi", [(0x61, 2, 180)]),
    ("Office Guest", [(0x71, 3, 1560), (0x72, 1, 1580)]),
    ("Riverside Diner", [(0x81, 3, 6)]),
]
SESSION_BSSIDS = (0x31, 0x51, 0x52, 0x81, 0x32)
SESSION_HANDSHAKES = 8
WHITELIST = ["My Network", "My Network 5G", "00:00:5E:00:53:F0"]
UPTIME_S = 2 * 3600 + 14 * 60
EPOCH = 268

ENABLED_PLUGINS = ("gps", "map", "memtemp", "session-stats", "ups-hat", "updates")
# The 2.7" HAT's e-ink plugin: eink-ws27 in the releases that ship it (eink is
# then deprecated), eink before that. This renders the latest stable release.
EINK_NAMES = ("eink-ws27", "eink")

POSITION = {"lat": 12.34567, "lon": -45.67890, "alt": 18.0, "fix": 3, "sats": 11,
            "cn0": 41.0, "strong": 9}
BATTERY = {"percent": 82.0, "voltage": 3.92, "current_ma": -480.0, "power_w": 1.88,
           "status": "discharging"}

# Invented map positions in one city centre, so the map shot has streets under
# it; none is a capture. Recorded (GPS) fixes by network, as bssid suffix and
# lat/lon (Cafe Guest's two radios share one fix, to show the one-spot list),
# then estimated (WiGLE-style) ones, then the networks whose passphrase is
# "recovered" (green on the map).
RECORDED_FIXES = {
    "Cafe Guest": [(0x31, 38.71395, -9.13940), (0x32, 38.71395, -9.13940)],
    "Riverside Diner": [(0x81, 38.70610, -9.14420)],
    "Harbor Hotel": [(0x51, 38.70790, -9.13610), (0x52, 38.70815, -9.13560)],
    "Corner Bookshop": [(0x41, 38.71060, -9.14230)],
    "Airport Free": [(0x11, 38.71650, -9.13150), (0x12, 38.71690, -9.13090)],
}
ESTIMATED_FIXES = [(0x61, 38.71180, -9.13790), (0x21, 38.71840, -9.13520),
                   (0x71, 38.71520, -9.14620), (0x72, 38.71560, -9.14580)]
RECOVERED = [(0x31, "Cafe Guest"), (0x41, "Corner Bookshop"), (0x51, "Harbor Hotel"),
             (0x61, "Library WiFi"), (0x81, "Riverside Diner")]


def mac(suffix):
    return "00:00:5E:00:53:%02X" % suffix


def agent_status(now):
    return {
        "running": True, "policy": "learned", "algorithm": "RecurrentPPO",
        "model_source": "local", "promotion_tally": {"local": 3, "remote": 1},
        "training": False, "last_trained": now - 3 * 3600 - 300,
        "last_train_secs": 754, "posture": "lock-subset", "dwell": 5,
        "transmit": "active", "last_reward": 0.42,
        "session_yield": SESSION_HANDSHAKES, "decisions": 134,
        "metrics": {"baseline": {"yield_per_hour": 3.1, "hit_rate": 0.21},
                    "learned": {"yield_per_hour": 4.4, "hit_rate": 0.29}},
    }


def _write_store(store, now):
    os.makedirs(store, exist_ok=True)
    seen = {}
    for essid, aps in NETWORKS:
        lines = []
        newest = 0
        for suffix, count, minutes in aps:
            bssid = mac(suffix)
            for i in range(count):
                lines.append("WPA*02*%032x*%s*%012x*%s***" % (
                    suffix * 100 + i, bssid.replace(":", "").lower(),
                    0x00005E005300 + 0xA0 + i, essid.encode().hex()))
            stamp = int(now - minutes * 60)
            seen[bssid] = {"last": stamp}
            newest = max(newest, stamp)
        path = os.path.join(store, essid + ".hc22000")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("\n".join(lines) + "\n")
        os.utime(path, (newest, newest))
    with open(os.path.join(store, "capture-seen.json"), "w", encoding="utf-8") as handle:
        json.dump({"aps": seen}, handle)
    with open(os.path.join(store, "capture-status.json"), "w", encoding="utf-8") as handle:
        json.dump({"scanning": True, "updated_at": now, "data_ts": now - 30,
                   "radios_recent": 23, "window_s": 300}, handle)

    # Locations: the gps plugin's per-capture sidecars and a wigle-style cache.
    stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now - 3600))
    for essid, fixes in RECORDED_FIXES.items():
        with open(os.path.join(store, essid + ".hc22000.gps.json"), "w",
                  encoding="utf-8") as handle:
            json.dump({mac(s): {"lat": lat, "lon": lon, "alt": 12.0, "fix_time": stamp,
                                "source": "gpsd"} for s, lat, lon in fixes}, handle)
    os.makedirs(os.path.join(store, "geo"), exist_ok=True)
    with open(os.path.join(store, "geo", "wigle-cache.json"), "w", encoding="utf-8") as handle:
        json.dump({mac(s): {"bssid": mac(s), "lat": lat, "lon": lon,
                            "source": "wigle-search", "query_time": stamp}
                   for s, lat, lon in ESTIMATED_FIXES}, handle)
    # Recovered passphrases, in the wpa-sec potfile layout (made up).
    os.makedirs(os.path.join(store, "cracked"), exist_ok=True)
    with open(os.path.join(store, "cracked", "demo.potfile"), "w", encoding="utf-8") as handle:
        for suffix, essid in RECOVERED:
            handle.write("%s:00005e0053f9:%s:demo-passphrase-%d\n"
                         % (mac(suffix).replace(":", "").lower(), essid, suffix))


def _merge(base, extra):
    for key, value in extra.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            _merge(base[key], value)
        else:
            base[key] = value
    return base


class Demo:
    """The demo device. ``files_dir`` is a release's ``builder/extra/files``
    tree; ``workdir`` a scratch directory for the fake store and state."""

    def __init__(self, files_dir, workdir, tag, previous_tag, release_url):
        self.eink_name = next(n for n in EINK_NAMES
                              if os.path.exists(os.path.join(files_dir, "plugins", n + ".py")))
        self.enabled = (self.eink_name,) + ENABLED_PLUGINS
        sys.path.insert(0, os.path.join(files_dir, "kestrel_pi"))
        try:
            import tomllib

            import kestrel_pi
            from kestrel_pi import daemon as daemon_mod
            from kestrel_pi import ui as ui_mod
            from kestrel_pi import web
            from kestrel_pi.agent import readout
        except ImportError as exc:
            raise RuntimeError("can't import the release's kestrel_pi: %s" % exc) from exc
        if not os.path.realpath(kestrel_pi.__file__).startswith(os.path.realpath(files_dir)):
            raise RuntimeError("kestrel_pi was already imported from %s; run the "
                               "tool in a fresh process" % kestrel_pi.__file__)
        self.web = web
        self.ui_mod = ui_mod
        self.now = time.time()
        self.tag = tag
        self.update_state = {}

        with open(os.path.join(files_dir, "config.toml"), "rb") as handle:
            shipped = tomllib.load(handle)
        overrides = {
            "web": {"auth": "system"},
            "capture": {"enabled": True, "whitelist": list(WHITELIST)},
            "agent": {"enabled": True, "policy": "learned",
                      "algorithm": "RecurrentPPO", "train": "auto"},
            "plugins": {name: {"enabled": True} for name in self.enabled},
        }
        config = _merge(copy.deepcopy(shipped), overrides)

        store = os.path.join(workdir, "capture")
        _write_store(store, self.now)
        ack = os.path.join(workdir, "capture-ack")
        with open(ack, "w", encoding="utf-8") as handle:
            handle.write("acknowledged\n")

        status = agent_status(self.now)
        daemon_mod.load_config = lambda path, log=None: copy.deepcopy(config)
        readout.read_status = lambda agent_dir=None: copy.deepcopy(status)
        web._check_auth = lambda web_cfg, request: True
        web.management_addresses = lambda interfaces=None: [("bnep0", "192.168.44.1"),
                                                            ("wlan0", None)]
        web.ACK_FILE = ack
        web._agent_has_model = lambda algorithm, agent_dir=None: True
        web._read_update_state = lambda path=None: copy.deepcopy(self.update_state)
        web._read_update_run_state = lambda path=None: {}
        # Since 1.0.9 the dashboard has a sign-in page. The demo accepts any
        # password for the documented device account (the page would otherwise
        # name this desktop's login), and keeps the session key and certificate
        # state in the scratch directory instead of this desktop's home.
        web.system_login_user = lambda web_cfg: "pi"
        web._verify_system_login = lambda username, password: True
        try:
            from kestrel_pi import webtls
        except ImportError:
            pass
        else:
            webtls.state_dir = lambda: os.path.join(workdir, "web-state")

        d = daemon_mod.Daemon(config_path=os.path.join(workdir, "config.toml"),
                              plugin_paths=(os.path.join(files_dir, "plugins"),),
                              capture_store=store)
        d._service_state = lambda service: "running"
        d._bt_paired = lambda: True
        d.connectivity.online = True
        d.connectivity.paths = {"bnep0": True, "wlan0": False}
        d.epoch = EPOCH
        d.started = self.now - UPTIME_S
        d._session_handshakes = SESSION_HANDSHAKES
        d._session_bssids = {mac(s) for s in SESSION_BSSIDS}
        self.daemon = d
        self.agent = status

        # Load the enabled plugins without their on_loaded/on_ready hooks (they
        # start hardware readers), then build their dashboard cards with the
        # plugins' own on_ui_setup and fill them with demo readings.
        d.loader._safe_lifecycle = lambda rec, hook: True
        d.loader.load_all()
        missing = [n for n in self.enabled
                   if getattr(d.loader.records.get(n), "state", None) != "loaded"]
        if missing:
            raise RuntimeError("demo plugins did not load: %s" % ", ".join(
                "%s (%s)" % (n, getattr(d.loader.records.get(n), "reason", "absent"))
                for n in missing))
        # A bundled plugin that fails to import here (a missing host package,
        # say) would show as an error on the Plugins page, which no device shows.
        failed = ["%s (%s)" % (r.name, r.reason) for r in d.loader.records.values()
                  if r.state == "failed"]
        if failed:
            raise RuntimeError("bundled plugins failed under the demo (check the tool's "
                               "requirements): %s" % "; ".join(failed))
        # map's on_loaded only sets up where it reads [web] show_locations; point
        # that at the demo's (absent) config so the default (shown) applies.
        if "map" in d.loader.records:
            demo_map = d.loader.records["map"].instance
            demo_map.on_loaded()
            demo_map._config_path = os.path.join(workdir, "config.toml")
        self._previous_tag = previous_tag
        self._release_url = release_url
        self._fill_widgets()

    # -- plugin readouts -------------------------------------------------------
    def _instance(self, name):
        return self.daemon.loader.records[name].instance

    def _set(self, name, values):
        ui = self.daemon.ui.for_plugin(name)
        absent = object()
        for key, value in values.items():
            if ui.get(key, absent) is absent:
                raise RuntimeError("plugin %s has no %r readout in this release"
                                   % (name, key))
            ui.set(key, value)

    def _fill_widgets(self):
        memtemp = self._instance("memtemp")
        memtemp._has_fan = False
        memtemp._fan_pct = False
        eink = self._instance(self.eink_name)
        eink._setup()
        # The panel's address row would otherwise read this desktop's real
        # interfaces; show the same placeholder link the dashboard shots use.
        # (Harmless on releases from before the row existed.)
        eink._addresses = lambda: [("bnep0", "192.168.44.1")]
        # Nor the first-minutes HTTPS fingerprint, which would replace the voice.
        eink._fingerprint_lines = lambda: []
        for name in self.enabled:
            try:
                self._instance(name).on_ui_setup(self.daemon.ui.for_plugin(name))
            except Exception as exc:
                raise RuntimeError("plugin %s on_ui_setup failed under the demo: %s"
                                   % (name, exc)) from exc

        gps = self._instance("gps")
        number, read = gps._signal_text(POSITION["fix"], POSITION["cn0"], POSITION["strong"])
        self._set("gps", {"status": "3D fix", "sats": str(POSITION["sats"]),
                          "signal": number, "signal_read": read,
                          "lat": "%.5f" % POSITION["lat"], "lon": "%.5f" % POSITION["lon"],
                          "alt": "%.1f m" % POSITION["alt"]})
        self._set("memtemp", {"mem": "41%", "cpu": "0.35", "temp": "48.2C"})
        uptime = type(self._instance("session-stats")).on_ui_update.__globals__["_format_uptime"]
        self._set("session-stats", {"handshakes": str(SESSION_HANDSHAKES),
                                    "aps": str(len(SESSION_BSSIDS)),
                                    "epochs": str(EPOCH), "uptime": uptime(UPTIME_S)})
        ups = self._instance("ups-hat")
        labels = type(ups).on_ui_update.__globals__["_STATUS_LABEL"]
        values = {"percent": "%d%%" % BATTERY["percent"],
                  "status": labels[BATTERY["status"]],
                  "voltage": "%.2f V" % BATTERY["voltage"],
                  "current": "%+.0f mA" % BATTERY["current_ma"],
                  "power": "%.2f W" % BATTERY["power_w"]}
        if self.daemon.ui.for_plugin("ups-hat").get("eta", None) is not None:
            values["eta"] = "3h 10m"
        self._set("ups-hat", values)
        self.set_update_available(False)

        # A few stationary fixes first, so the face's motion tracker settles on
        # "parked" the way it would on a device that has been sitting still.
        for age in (90, 60, 30):
            eink._mobility_state(dict(POSITION, ts=self.now - age))
        state = self.ui_state()
        mood = eink._current_mood(state)
        voice = eink._voice_for(mood, 0)
        eink._state, eink._mood, eink._voice = state, mood, voice
        eink._absent = False
        self._eink_frame = (state, mood, voice)
        # The motion label as the plugin's card shows it. Releases from the one
        # that moved the face into the eink_face plugin library keep the
        # estimate in eink._motion and the labels in the library; earlier ones
        # keep both in the plugin (this renders the latest stable release).
        plugin_globals = type(eink).on_ui_update.__globals__
        if hasattr(eink, "_motion"):
            labels, motion = plugin_globals["face"].MOBILITY_LABEL, eink._motion.state
        else:
            labels, motion = plugin_globals["_MOBILITY_LABEL"], eink._mobility
        self._set(self.eink_name, {"face": eink._face_glyph(mood), "mood": mood, "voice": voice,
                           "mobility": labels.get(motion, "gps?"),
                           "panel": "detected"})

    def ui_state(self):
        d = self.daemon
        cfg = d._fresh_config()
        # Lifetime totals come straight from the capture store (the same
        # capture_counts() the daemon and dashboard read), so the demo's e-ink
        # face and dashboard navcard can't disagree. The session counts are
        # tracked separately and shown alongside.
        counts = d.capture.capture_counts()
        return self.ui_mod.UIState(
            online=True, epoch=EPOCH, capture_active=True, agent=self.agent,
            battery=dict(BATTERY), position=dict(POSITION, ts=self.now),
            paths=dict(d.connectivity.paths),
            capture_total=counts["handshakes"],
            capture_session=SESSION_HANDSHAKES, uptime=UPTIME_S,
            aps_total=counts["access_points"],
            aps_session=len(d._session_bssids),
            networks_total=counts["networks"],
            channel=d._scan_channel(self.agent, True),
            env=d._environment_state(cfg, True))

    def set_update_available(self, available):
        """Switch the updates plugin's shared state between "up to date" on
        this release and "an in-place upgrade to this release is available"."""
        installed = self._previous_tag if available else self.tag
        self.update_state = {
            "checked_at": self.now - 1800, "installed": installed, "latest": self.tag,
            "update_available": bool(available), "offerable": bool(available),
            "notes_url": self._release_url,
        }
        updates = self._instance("updates")
        self._set("updates", {"installed": installed, "latest": self.tag,
                              "status": updates._status_text(self.update_state)})

    def eink_image(self):
        """The e-ink panel frame for the demo state, from the plugin's own
        compositor (a 1-bit PIL image at panel resolution)."""
        state, mood, voice = self._eink_frame
        return self._instance(self.eink_name)._compose_image(state, mood, voice)

    # -- serving -----------------------------------------------------------------
    def serve(self):
        """Start the web app on a loopback port; returns ``(base_url, stop)``."""
        from werkzeug.serving import make_server

        logging.getLogger("werkzeug").setLevel(logging.WARNING)
        app = self.web.create_app(self.daemon)
        server = make_server("127.0.0.1", 0, app, threaded=True)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()

        def stop():
            server.shutdown()
            thread.join(5)

        return "http://127.0.0.1:%d" % server.server_port, stop
