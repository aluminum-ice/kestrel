"""The plugin host: loads plugins, sources events from the Phase 1/2 surfaces,
dispatches the hooks off the loop, and serves the web status page.

``kestrel-daemon.service`` runs :func:`main`. The daemon is benign with no
plugins enabled - it just runs the event loop and serves a read-only status
page over the management link - so it ships enabled; nothing it does touches
captures or the network until a plugin is explicitly enabled.
"""

from __future__ import annotations

import argparse
import copy
import json
import logging
import os
import signal
import subprocess
import threading
import time

from . import HOOK_API_VERSION, __version__
from .agent import AGENT_DIR
from .agent import readout as agent_readout
from .capture import (
    DEFAULT_DENSITY_THRESHOLDS,
    DEFAULT_ENV_MAX_AGE_S,
    CaptureInterface,
    CaptureWatcher,
    classify_density,
)
from .config import (
    DAEMON_DEFAULTS, DEFAULT_CONFIG_PATH, WEB_DEFAULTS, load_config, section,
)
from .connectivity import ConnectivityMonitor, http_probe, nmcli_connectivity_online
from .context import ServiceRegistry
from .recovered import RecoveredStore
from .geo import GeoStore
from .dispatch import Dispatcher
from .loader import Loader
from .ui import StubUI, UIState

# Plugin search paths: shipped reference plugins first, operator plugins second
# (an operator plugin overrides a shipped one of the same name).
DEFAULT_PLUGIN_PATHS = (
    "/usr/local/lib/kestrel/plugins",
    "/etc/kestrel/plugins",
)

DEFAULT_CAPTURE_STORE = "/var/lib/kestrel/capture"
CAPTURE_SERVICE = "kestrel-capture.service"
AGENT_SERVICE = "kestrel-agent.service"
# ``systemctl is-active`` for the capture/agent services is shelled out on every
# status() render (and the dashboard auto-refreshes). A short cache collapses
# the repeat spawns so a slow systemd under load can't stretch each render into
# seconds of piled-up subprocesses; it is far below any refresh interval, so the
# card still reflects a service change on the next tick.
_SERVICE_STATE_TTL = 2.0
# After a capture toggle the dashboard re-renders straight away, but the cache
# above can still hold the pre-change state, and `capture enable` starts the
# service with --no-block, so it may not be up yet. The toggle re-probes for up
# to this long until systemd reports the new state. It runs only on the toggle,
# after the apply socket has replied, so it can't bring back the slow-apply hang
# that made the start non-blocking.
_SERVICE_SETTLE_TIMEOUT = 5.0
_SERVICE_SETTLE_POLL = 0.25
# Concurrent status() renders that miss the cache share one `systemctl
# is-active` per service instead of each forking its own; a follower waits this
# long for the leader (just over the probe's 5s subprocess timeout).
_SERVICE_PROBE_WAIT = 6.0
# How long stop() gives all the plugins' on_unload calls, together. A restart
# from a settings save waits on it, and it must stay well inside the unit's
# TimeoutStopSec (40s), or systemd SIGKILLs a daemon that is still blanking the
# e-ink panel and the restart drags out past the page's reconnect window. The
# e-ink unload (a worker join of up to 20s, then a panel clear) is the longest.
_UNLOAD_TIMEOUT = 25.0
# The onboarding writes the paired phone's MAC here (see common.sh
# PWN_BT_PHONE_FILE); the daemon (pi) can read it to tell whether a phone is
# paired, so the web shows a re-pair control only when none is.
BT_PHONE_FILE = "/var/lib/kestrel/bt-phone"
# The agent's per-window scan plan (written by agent/controller.py). Named here
# rather than imported so the benign daemon never pulls in the agent's ML deps.
SCAN_OVERRIDE_NAME = "scan-override.json"
# One-whole-band tokens ([capture.scan] bands / the agent band plan) -> a short
# label for the channel readout.
_BAND_LABEL = {"2": "2.4G", "5": "5G", "6": "6G", "60": "60G"}


def _parse_channel_list(spec):
    """The integer channels in a comma-separated spec (e.g. ``"1,6,11"``)."""
    out = []
    for tok in str(spec).split(","):
        tok = tok.strip()
        if tok.isdigit():
            out.append(int(tok))
    return out


def _channels_label(channels):
    """A terse label for a channel list: the channels joined, or ``"<n>ch"`` when
    there are too many to fit, or ``"sweep"`` when the list is empty."""
    chans = [int(c) for c in channels if str(c).strip().isdigit()]
    if not chans:
        return "sweep"
    if len(chans) > 4:
        return "%dch" % len(chans)
    return ",".join(str(c) for c in chans)


def _band_label(band):
    b = str(band or "")
    if b in ("both", "auto", ""):
        return "sweep"
    return _BAND_LABEL.get(b, b)


def format_scan_channel(override, scan_cfg):
    """A short, honest label of the channel plan capture is pinned to this
    window - the agent's per-window ``override`` when it is driving, else the
    configured ``[capture.scan]``. Pure and testable. Returns e.g. ``"1,6,11"``,
    ``"6"``, ``"2.4G"``, ``"sweep"`` or ``"hunt"``; a long channel list collapses
    to ``"<n>ch"``. It is the configured plan, never a live instantaneous tune
    (AngryOxide hops within the plan), so the face never claims a channel lock
    it cannot read."""
    if override:
        kind = override.get("kind")
        if kind == "autohunt":
            return "hunt"
        if kind == "channels":
            return _channels_label(override.get("channels") or [])
        if kind == "band":
            return _band_label(override.get("band"))
    scan_cfg = scan_cfg or {}
    mode = str(scan_cfg.get("mode", "sweep"))
    bands = str(scan_cfg.get("bands", "auto"))
    if mode == "autohunt":
        return "hunt"
    if mode == "fixed":
        return _channels_label(_parse_channel_list(bands))
    # sweep (the default): a whole band or the adapter's full channel set.
    if bands in _BAND_LABEL:
        return _band_label(bands)
    if bands in ("auto", "both", ""):
        return "sweep"
    return _channels_label(_parse_channel_list(bands))


class Daemon:
    def __init__(self, config_path=DEFAULT_CONFIG_PATH, plugin_paths=None,
                 capture_store=DEFAULT_CAPTURE_STORE, logger=None):
        self.log = logger or logging.getLogger("kestrel_pi.daemon")
        self.config_path = config_path
        self.config = load_config(config_path, self.log)
        self.daemon_cfg = section(self.config, "daemon", DAEMON_DEFAULTS)
        self.web_cfg = section(self.config, "web", WEB_DEFAULTS)

        self.capture = CaptureInterface(capture_store)
        # Known passphrases for captured networks, parsed from any *.potfile an
        # operator or plugin drops in <store>/cracked/. Owned here, read by the
        # web layer to annotate /networks; its own mtime cache keeps it cheap.
        self.recovered = RecoveredStore(capture_store)
        # Known locations for captured radios, read from the GPS sidecars and the
        # external geo caches any producer drops in the store. Owned here, read
        # by the web layer to annotate /networks; its own mtime cache keeps it
        # cheap and it imports no plugin, exactly like ``recovered`` above.
        self.geo = GeoStore(capture_store)
        # Short-TTL cache of `systemctl is-active` results (see _service_state),
        # keyed by service name. Guarded because status() runs on web request
        # threads (the server is threaded).
        self._svc_state_cache = {}
        self._svc_state_lock = threading.Lock()
        self._svc_inflight = {}       # service -> Event, the probe in progress
        # config.toml parsed once per on-disk version (see _fresh_config).
        self._cfg_cache = None        # (stat key, parsed dict)
        self._cfg_cache_lock = threading.Lock()
        self.services = ServiceRegistry(self.log)
        # Expose the learning agent's published readout as a service so a UI
        # plugin (the e-ink face) can react to agent state without importing the
        # agent package or its torch deps. read_status() is stdlib-only and
        # returns None when the agent has published nothing; a plugin consumes it
        # via ``__optional_requires__ = ("agent",)`` and degrades when it is None.
        self.services.provide(
            "agent", lambda: agent_readout.read_status(), owner="daemon")
        # The recovered-networks store as a service, so a plugin can read known
        # passphrases without importing the daemon; the registry getter swallows
        # a read failure to None, so a broken potfile never breaks a consumer.
        self.services.provide("recovered", lambda: self.recovered, owner="daemon")
        # The network-location store as a service, same posture as ``recovered``:
        # a plugin can read known locations without importing the daemon, and the
        # registry getter swallows a read failure to None.
        self.services.provide("geo", lambda: self.geo, owner="daemon")
        self.ui = StubUI(self.log.getChild("ui"))

        self.dispatcher = Dispatcher(
            max_workers=self.daemon_cfg["hook_workers"],
            max_hook_seconds=self.daemon_cfg["max_hook_seconds"],
            logger=self.log,
            stuck_after_seconds=self.daemon_cfg.get("hook_stuck_seconds", 300),
        )

        probe = None
        if self.web_cfg.get("probe_url"):
            url = self.web_cfg["probe_url"]
            probe = lambda: http_probe(url, 10)  # noqa: E731
        self.connectivity_monitor = ConnectivityMonitor(
            on_online=self._on_internet_available,
            probe=probe or nmcli_connectivity_online,
            logger=self.log,
            offline_confirmations=int(self.daemon_cfg.get("offline_confirmations", 2)),
        )
        self.connectivity = self.connectivity_monitor.state

        self.capture_watcher = CaptureWatcher(
            capture_store, self._on_handshake, self.log.getChild("capture")
        )
        # Capture counters for the UI. The lifetime totals (handshakes, access
        # points, networks) come straight from capture_counts() so the e-ink
        # face can never disagree with the dashboard navcard, which reads the
        # same call. Only the per-session deltas are tracked here: the
        # handshakes and distinct BSSIDs seen since this daemon started, for the
        # capture status card's "caught this session" line (and the face's
        # session count beside the handshake total).
        self._session_handshakes = 0
        self._session_bssids = set()

        self.loader = Loader(
            config=self.config,
            capture=self.capture,
            connectivity=self.connectivity,
            services=self.services,
            search_paths=plugin_paths or DEFAULT_PLUGIN_PATHS,
            logger=self.log.getChild("loader"),
        )

        self.epoch = 0
        # Wall-clock start, in status(): the web page uses a change in it to
        # tell that a restarted daemon is back after a plugin settings change.
        self.started = time.time()
        self._stop = threading.Event()
        self._threads = []
        self._web = None

    # -- event sources ---------------------------------------------------
    def _on_handshake(self, event):
        self._session_handshakes += 1
        bssid = getattr(event, "bssid", "") or ""
        if bssid:
            self._session_bssids.add(bssid)
        for record in self.loader.loaded_records():
            self.dispatcher.dispatch(record, "on_handshake", event)

    def _on_internet_available(self):
        for record in self.loader.loaded_records():
            self.dispatcher.dispatch(record, "on_internet_available")

    def _epoch_info(self):
        return {
            "epoch": self.epoch,
            "online": self.connectivity.online,
            "paths": self.connectivity.active_paths(),
        }

    def _read_scan_override(self):
        """The agent's per-window scan plan dict, or None when the agent has
        written none (or it is unreadable). Stdlib-only; no agent import."""
        try:
            with open(os.path.join(AGENT_DIR, SCAN_OVERRIDE_NAME),
                      "r", encoding="utf-8") as handle:
                data = json.load(handle)
        except (OSError, ValueError):
            return None
        return data if isinstance(data, dict) else None

    def _scan_channel(self, agent, capture_active):
        """A short label of the channel plan capture is pinned to this window, or
        None when capture is not running. The agent's override wins while it is
        driving; otherwise it is the configured ``[capture.scan]``."""
        if not capture_active:
            return None
        override = (self._read_scan_override()
                    if (agent and agent.get("running")) else None)
        scan_cfg = (self.config.get("capture") or {}).get("scan") or {}
        return format_scan_channel(override, scan_cfg)

    def _environment_state(self, cfg, capture_active):
        """The in-range RF readout for the status/UI, or ``None`` when capture is
        off (the radio is deaf, so "unknown" - never a misleading 0).

        While capturing, returns ``{"available": False}`` until the collector has
        a usable reading, then the distinct-radios count with a density
        ``level``/``label`` from the configured thresholds and the data's
        ``data_age_s`` freshness. AngryOxide flushes its kismet DB in bursts, so
        the reading can lag real time; a reading older than ``max_age_s`` is
        dropped back to "unknown" rather than shown as current. Pure read of the
        collector's small status file plus config; no device access here."""
        if not capture_active:
            return None
        env_cfg = (cfg.get("capture") or {}).get("environment") or {}
        if not bool(env_cfg.get("enabled", True)):
            return None
        raw = self.capture.environment()
        # The session yield is real capture data, independent of the density
        # reading, so it is reported in every capture-active state - including
        # while the density read is lagging or unavailable ("Scanning..."). It
        # used to live only in the "available" branch, which blanked the
        # "Caught N handshakes" row to 0 whenever the density flipped to unknown,
        # even though handshakes were being caught (the daemon counters here are
        # the same ones session-stats reports).
        yield_counts = {
            "caught_session": len(self._session_bssids),
            "handshakes_session": self._session_handshakes,
        }
        if raw is None or "radios_recent" not in raw:
            return {"scanning": True, "available": False, **yield_counts}
        data_ts = raw.get("data_ts")
        data_age = None
        if data_ts is not None:
            try:
                data_age = max(0, int(time.time() - float(data_ts)))
            except (TypeError, ValueError):
                data_age = None
        try:
            max_age = int(env_cfg.get("max_age_s", DEFAULT_ENV_MAX_AGE_S))
        except (TypeError, ValueError):
            max_age = DEFAULT_ENV_MAX_AGE_S
        if data_age is not None and max_age > 0 and data_age > max_age:
            # the newest capture data is too old to call a current reading
            return {"scanning": True, "available": False, **yield_counts}
        thresholds = env_cfg.get("thresholds") or DEFAULT_DENSITY_THRESHOLDS
        radios = int(raw.get("radios_recent") or 0)
        level, label = classify_density(radios, thresholds)
        top = max(1, int(thresholds[-1]) if thresholds else DEFAULT_DENSITY_THRESHOLDS[-1])
        return {
            "scanning": True,
            "available": True,
            "radios_recent": radios,
            "window_s": int(raw.get("window_s") or 0),
            "level": level,
            "label": label,
            "bar_pct": min(100, round(radios * 100 / top)),
            "data_age_s": data_age,
            # the session yield, shown as a plain count alongside (not divided
            # by) the density - the two are independent measurements.
            **yield_counts,
        }

    def _ui_state(self):
        """A read-only snapshot of global device state, passed to on_ui_update.
        Assembled once per tick; the service-provider dicts (agent/battery/
        position) are None when absent, and a UI plugin degrades accordingly."""
        agent = self.services.get("agent")
        capture_active = (self._capture_state() == "running")
        counts = self.capture.capture_counts()
        return UIState(
            online=self.connectivity.online,
            epoch=self.epoch,
            capture_active=capture_active,
            agent=agent,
            battery=self.services.get("battery"),
            position=self.services.get("position"),
            paths=dict(self.connectivity.paths),
            capture_total=counts["handshakes"],
            capture_session=self._session_handshakes,
            uptime=time.time() - self.started,
            aps_total=counts["access_points"],
            aps_session=len(self._session_bssids),
            networks_total=counts["networks"],
            channel=self._scan_channel(agent, capture_active),
            env=self._environment_state(self.config, capture_active),
        )

    def _tick_epoch(self):
        self.epoch += 1
        info = self._epoch_info()
        state = self._ui_state()
        for record in self.loader.loaded_records():
            self.dispatcher.dispatch(record, "on_epoch", self.epoch, info)
            self.dispatcher.dispatch(record, "on_ui_update",
                                     self.ui.for_plugin(record.name), state)

    # -- lifecycle -------------------------------------------------------
    def start(self):
        self.log.info("kestrel daemon %s (hook API %d) starting",
                      __version__, HOOK_API_VERSION)
        loaded = self.loader.load_all()
        self.log.info("loaded %d plugin(s): %s", len(loaded),
                      ", ".join(r.name for r in loaded) or "none")

        # Fire on_ui_setup once against each plugin's namespaced UI view, then an
        # immediate on_ui_update so the dashboard shows real values right away.
        # Enabling a plugin restarts the daemon, so without this the newly shown
        # plugin would display its setup defaults until the first epoch tick (up
        # to epoch_seconds later) and look like it is not working.
        for record in loaded:
            self.dispatcher.dispatch(record, "on_ui_setup",
                                     self.ui.for_plugin(record.name))
        state = self._ui_state()
        for record in loaded:
            self.dispatcher.dispatch(record, "on_ui_update",
                                     self.ui.for_plugin(record.name), state)

        # Don't re-announce hashlines already in the store on startup.
        self.capture_watcher.prime()

        if self.web_cfg.get("enabled", True):
            self._start_web()

        self._spawn(self._capture_loop, "pwn-capture")
        self._spawn(self._connectivity_loop, "pwn-connectivity")
        self._spawn(self._epoch_loop, "pwn-epoch")

    def _start_web(self):
        try:
            from .web import WebServer, create_app, resolve_bind_hosts
        except Exception as exc:  # noqa: BLE001 - flask missing shouldn't kill the daemon
            self.log.warning("web subsystem unavailable: %s", exc)
            return
        hosts = resolve_bind_hosts(self.web_cfg.get("bind", "management"))
        self._web = WebServer(create_app(self), hosts, self.web_cfg.get("port", 8080),
                              self.log.getChild("web"))
        if not self._web.start():
            self.log.warning("web subsystem bound no interfaces")

    def _spawn(self, target, name):
        thread = threading.Thread(target=target, name=name, daemon=True)
        thread.start()
        self._threads.append(thread)

    def _capture_loop(self):
        interval = max(1, int(self.daemon_cfg["capture_poll_seconds"]))
        while not self._stop.is_set():
            try:
                self.capture_watcher.poll_once()
            except Exception:  # noqa: BLE001
                self.log.exception("capture poll failed")
            self._stop.wait(interval)

    def _connectivity_loop(self):
        interval = max(1, int(self.daemon_cfg["connectivity_poll_seconds"]))
        while not self._stop.is_set():
            try:
                self.connectivity_monitor.poll_once()
            except Exception:  # noqa: BLE001
                self.log.exception("connectivity poll failed")
            # Follow management interfaces that get (or lose) their address after
            # startup. The common case is the boot race: the daemon comes up
            # before wlan0 has a DHCP lease, so the web page first binds
            # loopback-only; once wlan0 has an IP this rebinds it off-box.
            self._reconcile_web_bind()
            self._stop.wait(interval)

    def _reconcile_web_bind(self):
        if self._web is None:
            return
        try:
            from .web import resolve_bind_hosts

            self._web.reconcile(
                resolve_bind_hosts(self.web_cfg.get("bind", "management"))
            )
        except Exception:  # noqa: BLE001 - a rebind failure must not kill the loop
            self.log.exception("web rebind failed")

    def _epoch_loop(self):
        interval = max(1, int(self.daemon_cfg["epoch_seconds"]))
        while not self._stop.is_set():
            if self._stop.wait(interval):
                break
            try:
                self._tick_epoch()
            except Exception:  # noqa: BLE001
                self.log.exception("epoch tick failed")

    def stop(self):
        self.log.info("daemon stopping")
        self._stop.set()
        if self._web:
            self._web.stop()
        self.loader.unload_all(_UNLOAD_TIMEOUT)
        self.dispatcher.shutdown(wait=False)

    def run_forever(self):
        self.start()
        try:
            while not self._stop.is_set():
                self._stop.wait(3600)
        finally:
            self.stop()

    # -- status ----------------------------------------------------------
    def _capture_state(self):
        """Live capture-service state for the status page. The daemon runs as
        pi; ``systemctl is-active`` needs no privilege. Returns ``running``,
        ``stopped`` or ``unknown`` (systemctl unavailable)."""
        return self._service_state(CAPTURE_SERVICE)

    def wait_capture_state(self, running, timeout=None):
        """Called after a capture toggle so the page re-renders with the new
        state: see _wait_service_state."""
        return self._wait_service_state(CAPTURE_SERVICE, running, timeout)

    def _wait_service_state(self, service, running, timeout=None):
        """Drop the cached state (it may predate the change), then re-probe
        until the service is running (or stopped) as wanted, or the bounded
        timeout passes. Returns the last state read, which the cache then holds.
        Gives up at once on ``unknown`` (no systemctl), which won't change."""
        want = "running" if running else "stopped"
        if timeout is None:
            timeout = _SERVICE_SETTLE_TIMEOUT
        deadline = time.monotonic() + timeout
        while True:
            with self._svc_state_lock:
                self._svc_state_cache.pop(service, None)
            state = self._service_state(service)
            if state in (want, "unknown") or time.monotonic() >= deadline:
                return state
            time.sleep(_SERVICE_SETTLE_POLL)

    def _service_state(self, service):
        now = time.monotonic()
        with self._svc_state_lock:
            cached = self._svc_state_cache.get(service)
            if cached and cached[1] > now:
                return cached[0]
            flight = self._svc_inflight.get(service)
            leader = flight is None
            if leader:
                flight = self._svc_inflight[service] = threading.Event()
        if not leader:
            # Another request is already probing this service: use its answer
            # (whatever it cached, even if that has since aged past the TTL).
            flight.wait(_SERVICE_PROBE_WAIT)
            with self._svc_state_lock:
                cached = self._svc_state_cache.get(service)
            return cached[0] if cached else "unknown"
        try:
            try:
                rc = subprocess.run(
                    ["systemctl", "is-active", "--quiet", service],
                    timeout=5, check=False,
                ).returncode
            except (OSError, subprocess.SubprocessError):
                # Don't cache an error: retry on the next read rather than pin
                # "unknown" for the whole TTL.
                return "unknown"
            state = "running" if rc == 0 else "stopped"
            with self._svc_state_lock:
                self._svc_state_cache[service] = (state, now + _SERVICE_STATE_TTL)
            return state
        finally:
            with self._svc_state_lock:
                self._svc_inflight.pop(service, None)
            flight.set()

    def _bt_paired(self):
        """Whether a phone is paired for the Bluetooth PAN link, read from the
        state file the onboarding writes. The daemon runs as pi and can read it;
        the web shows the re-pair control only when this is False (so it never
        offers to drop a link the viewer might be on)."""
        try:
            with open(BT_PHONE_FILE, "r", encoding="utf-8") as handle:
                return bool(handle.readline().strip())
        except OSError:
            return False

    def _fresh_config(self):
        """Re-read config.toml so status reflects changes made at runtime through
        the root apply helper (web/CLI agent enable, policy/algorithm, capture
        on/off, whitelist edits) without a daemon restart. ``self.config`` is
        loaded once at start-up and never mutated, so reading config intent from
        it would show stale state after any such change. Falls back to the
        start-up config on any read or parse error, so a bad read never breaks
        status()."""
        try:
            try:
                st = os.stat(self.config_path)
                key = (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns,
                       st.st_ctime_ns)
            except OSError:
                key = None
            with self._cfg_cache_lock:
                cached = self._cfg_cache
            if key is not None and cached is not None and cached[0] == key:
                parsed = cached[1]
            else:
                # Every page render used to re-parse the file. It changes only
                # through an atomic replace (new inode) or an in-place edit (new
                # mtime), so a parse is reused until the file's stat changes.
                parsed = load_config(self.config_path, self.log)
                if key is not None:
                    with self._cfg_cache_lock:
                        self._cfg_cache = (key, parsed)
            # A copy, so no caller can alter the cached parse for the next one.
            return copy.deepcopy(parsed)
        except Exception:  # noqa: BLE001 - never let a bad read break status()
            return self.config

    def capture_whitelist(self, cfg=None):
        """The [capture] whitelist (ESSIDs/MACs never actively attacked), re-read
        from config.toml so the web page reflects edits made through the apply
        helper without a daemon restart. Returns a list of strings."""
        cfg = cfg if cfg is not None else self._fresh_config()
        wl = (cfg.get("capture") or {}).get("whitelist") or []
        return [str(x) for x in wl if isinstance(x, str)]

    def _agent_state(self, cfg=None):
        """The learning agent's config intent plus its live service state and
        the status it published. The config intent (enabled/algorithm/policy) is
        re-read fresh from config.toml, not taken from the start-up config, so
        the page reflects a web/CLI enable or a policy change without a daemon
        restart. The agent runs in its own process; the daemon only reads its
        ``status.json`` (stdlib, no torch) and renders a card - it never imports
        the agent's env or policies."""
        agent_cfg = (cfg if cfg is not None else self._fresh_config()).get("agent") or {}
        return {
            "enabled": bool(agent_cfg.get("enabled", False)),
            "algorithm": agent_cfg.get("algorithm") or "RecurrentPPO",
            "policy": agent_cfg.get("policy") or "baseline",
            # Configured on-device training mode (auto|idle|off), re-read fresh
            # so the /agent page's On/Off radio reflects a web/CLI change without
            # a daemon restart. "off" reads as Off; anything else reads as On.
            "train": agent_cfg.get("train") or "auto",
            "service": self._service_state(AGENT_SERVICE),
            "status": agent_readout.read_status(),
        }

    def status(self):
        cfg = self._fresh_config()
        agent = self._agent_state(cfg)
        # One capture-service read, reused for both the capture card and the
        # environment readout below (the short cache would collapse a second
        # call anyway, but reading it once keeps the intent clear).
        capture_state = self._capture_state()
        # The agent runs in its own process, so its readout isn't a plugin UI
        # view; the daemon merges its published status into the same grouped
        # widget model so the "Learning agent" card draws exactly like a plugin
        # readout (and the Phase 5 e-ink plugin renders it with no extra code).
        widgets = self.ui.widgets()
        agent_elements = agent_readout.widget_elements(agent.get("status"))
        if agent_elements:
            widgets[agent_readout.CARD_NAME] = agent_elements
        # How many captured access points have a known passphrase, for the
        # dashboard's "recovered N of M" figure (N of M captured APs). Join each
        # captured BSSID to the recovered store; both sides are mtime-cached so
        # this stays cheap on a repeat render. A store-read failure omits the
        # key (the navcard then shows no recovered figure) rather than breaking
        # status(); the key is never a secret, so it is always safe to include.
        recovered_aps = None
        located_aps = None
        try:
            aps = self.capture.captured_access_points()
            recovered_aps = sum(
                1 for ap in aps
                if ap.get("bssid") and self.recovered.lookup_bssid(ap["bssid"]))
            # Captured APs with a known location (recorded or estimated), the
            # "located N of M" figure, joined the same cached way. A location is
            # not a secret, so this is always safe to include.
            located_aps = sum(
                1 for ap in aps
                if ap.get("bssid") and self.geo.lookup_bssid(ap["bssid"]))
        except Exception:  # noqa: BLE001 - a store read must never break status()
            pass
        return {
            "version": __version__,
            "hook_api": HOOK_API_VERSION,
            "started": self.started,
            "epoch": self.epoch,
            "connectivity": {
                "online": self.connectivity.online,
                "paths": self.connectivity.active_paths(),
            },
            "capture": {
                "store": self.capture.store_dir,
                "hashline_files": len(self.capture.hashline_files()),
                # Distinct captured networks (by ESSID) and access points (by
                # ESSID+BSSID) -- the two /networks views' row counts, shown
                # together on the dashboard navcard. Derived from one cached
                # store parse.
                **self.capture.capture_counts(),
                # recovered APs of the captured APs above; omitted on a store-read
                # failure so the navcard simply shows no recovered figure.
                **({"recovered_aps": recovered_aps} if recovered_aps is not None else {}),
                # located APs of the captured APs above; omitted on a store-read
                # failure, same as recovered_aps.
                **({"located_aps": located_aps} if located_aps is not None else {}),
                "state": capture_state,
                # config intent (``[capture] enabled``), separate from the live
                # service state above; the web toggle reads this to know which
                # way to point.
                "enabled": bool((cfg.get("capture") or {}).get("enabled", False)),
                "whitelist": len(self.capture_whitelist(cfg)),
            },
            "environment": self._environment_state(
                cfg, capture_state == "running"),
            "agent": agent,
            "bluetooth": {"paired": self._bt_paired()},
            "services": self.services.names(),
            "ui": self.ui.elements(),
            "widgets": widgets,
            "plugins": [r.health() for r in sorted(
                self.loader.records.values(), key=lambda r: r.name)],
        }


def main(argv=None):
    parser = argparse.ArgumentParser(prog="kestrel-daemon")
    parser.add_argument("--config", default=DEFAULT_CONFIG_PATH)
    parser.add_argument("--plugin-dir", action="append", dest="plugin_dirs")
    parser.add_argument("--capture-store", default=DEFAULT_CAPTURE_STORE)
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=getattr(logging, str(args.log_level).upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    log = logging.getLogger("kestrel_pi")

    config = load_config(args.config, log)
    daemon_cfg = section(config, "daemon", DAEMON_DEFAULTS)
    if not daemon_cfg.get("enabled", True):
        log.info("[daemon] enabled = false; nothing to do")
        return 0

    daemon = Daemon(
        config_path=args.config,
        plugin_paths=tuple(args.plugin_dirs) if args.plugin_dirs else None,
        capture_store=args.capture_store,
        logger=log,
    )

    def _handle_signal(signum, _frame):
        log.info("signal %s received", signum)
        daemon._stop.set()

    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)
    # Never die on a terminal hangup. The always-on daemon has no controlling
    # terminal by design, but a plugin that opens a serial/tty device (e.g. gps
    # on a USB receiver) could accidentally acquire one, and then unplugging the
    # device would SIGHUP the whole daemon. Plugins must open tty devices with
    # O_NOCTTY; ignoring SIGHUP here is the backstop so a stray open can never
    # take the daemon down.
    signal.signal(signal.SIGHUP, signal.SIG_IGN)

    daemon.run_forever()
    # Exit now, without the interpreter's shutdown joins: concurrent.futures
    # joins every hook-pool worker at exit (even after shutdown(wait=False)), so
    # one hook stuck on a dead network call would keep the process alive until
    # systemd's stop timeout SIGKILLs it, and every restart would take that long.
    # stop() has already unloaded the plugins; flush the logs and go.
    logging.shutdown()
    os._exit(0)


if __name__ == "__main__":
    raise SystemExit(main())
