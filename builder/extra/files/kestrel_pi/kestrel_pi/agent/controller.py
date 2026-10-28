"""The agent process: the decide loop, the status file and the episode logs.

``kestrel-agent.service`` runs :func:`main`. The agent is opt-in and
guarded by capture: it refuses to start (logs the reason, exits 0 - like the
capture launcher) unless ``[capture] enabled`` is true, so it can never drive a
radio the operator has not turned on, and it adds no new transmission - it only
changes *which channels and dwells* the already-opted-in capture uses.

It never starts AngryOxide itself and never rewrites ``config.toml``. Each
decision window it writes its chosen scan plan to a runtime *override file*
(``<agent dir>/scan-override.json``); the Phase 2 capture launcher, which
already supervises AngryOxide, picks the override up and relaunches AngryOxide
pinned to that plan for the window. Disabling the agent leaves the override
ignored (the launcher gates on ``[agent] enabled``), so capture returns to the
configured sweep with nothing written back into ``[capture.scan]``.

The heavy ML stack (torch + stable-baselines3, via :mod:`.env`/:mod:`.policies`)
is imported only here, in this dedicated process, so a torch allocation can OOM
without taking down the benign plugin daemon or the web page.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import subprocess
import sys
import threading
import time
from collections import deque

from ..capture import CaptureInterface
from ..config import DEFAULT_CONFIG_PATH, load_config, section
from . import (AGENT_DIR, EPISODES_DIR, LAST_TRAIN_NAME, MODEL_NAME,
               PROMOTION_TALLY_NAME, REMOTE_STAGING_DIR, STATUS_NAME,
               TRAIN_REQUEST_NAME, model_meta_name_for, model_name_for,
               model_slug)
from . import metrics as metrics_mod
from . import policies as policies_mod
from . import spec

CAPTURE_SERVICE = "kestrel-capture.service"
DEFAULT_CAPTURE_STORE = "/var/lib/kestrel/capture"
OVERRIDE_NAME = "scan-override.json"
CONTEXT_NAME = "context.json"   # coarse device context beside the episode logs
WIFI_DRIVER = "brcmfmac"   # the built-in chip; never the capture dongle
# The cross-process GPS seam (Phase 4.8): the gps plugin (in the daemon) writes
# the latest fix here atomically; the agent reads it best-effort each window.
# tmpfs: live-only state, not meant to survive a reboot. The agent never imports
# the plugin. Documented in docs/developer/agent-interface.md.
POSITION_FILE = "/run/kestrel/position.json"


def _read_board_model(path="/proc/device-tree/model"):
    """A coarse board identifier (e.g. ``Raspberry Pi 5 Model B``) for the
    episode-log context tag, or ``None`` off a Pi. The device-tree model string
    is NUL-terminated. PII-free: a board type, no serial or location."""
    try:
        with open(path, "rb") as handle:
            model = handle.read().decode("utf-8", "replace").replace("\x00", "").strip()
        return model or None
    except OSError:
        return None

AGENT_DEFAULTS = {
    "enabled": False,
    "algorithm": policies_mod.DEFAULT_ALGORITHM,
    "policy": policies_mod.DEFAULT_POLICY,
    "model": os.path.join(AGENT_DIR, MODEL_NAME),
    "decision_seconds": 30,
    "log_episodes": True,
    # Phase 4.8: feed the gps plugin's fix into the observation (motion + fix
    # quality). A kill-switch only: inert without the gps plugin and a fix (no
    # receiver => gps_valid = 0), so the default is safe. Set false to force GPS
    # out of the observation without disabling the gps plugin.
    "use_gps": True,
    "train": "auto",
    # On-device training tunables (periodic fine-tune). The board gate below
    # still blocks training entirely on a board too small for it (a Zero 2 W),
    # whatever these say.
    "train_while_capturing": "auto",  # auto = on if the board can train, else off; or true/false

    "train_min_new_episodes": 60,    # train after this many new episodes since the last run
    "train_timesteps": 3000,         # steps per fine-tune (a short run, not a full train)
    "train_max_temp_c": 75,          # skip training at/above this CPU temp (hard-capped at 85)
    "train_min_free_mb": 512,        # skip training below this free RAM (hard floor 256)
    "interleave_compare": False,
    "compare_block_minutes": 10,
    "metrics_window_minutes": 60,
    "log_max_mb": 64,
    # Quality-aware promotion (docs/developer/agent-interface.md): how much
    # better (lift over baseline) a pulled remote model must score on a held-out
    # slice to replace the incumbent, and a soft staleness guard. Code defaults
    # (not shipped in config.toml), so a release carrying them stays app-layer.
    "promotion_margin": 0.10,            # 10% lift over baseline
    "promotion_max_stale_episodes": 900,  # skip a remote this far behind; 0=off
}
ACTION_DEFAULTS = {
    "lock_subset_n": 3,
    "dwell_buckets": list(spec.DWELLS),
    "transmit_modes": list(spec.TRANSMIT),
}
REWARD_DEFAULTS = {"per_handshake": 1.0, "time_penalty": 0.01, "idle_penalty": 0.05}

# The MemTotal at/above which `train = auto` turns idle training on (a Pi 5 4GB+
# reports a little under 4 GiB after firmware-reserved memory, so trip a touch
# below 4 GiB). The Zero 2 W's 512 MB and a 2 GB Pi 5 fall below it.
TRAIN_AUTO_MIN_BYTES = int(3.8 * 1024 * 1024 * 1024)


# -- config helpers ---------------------------------------------------------
def agent_config(config):
    """The merged ``[agent]`` table with its sub-tables and the capture band the
    sweep-band posture uses, resolved from ``[capture.scan] bands``."""
    merged = section(config, "agent", AGENT_DEFAULTS)
    # section() reads top-level tables; [agent.action]/[agent.reward] are nested,
    # so pull them straight from the parsed config.
    agent_raw = config.get("agent") or {}
    merged["action"] = dict(ACTION_DEFAULTS, **(agent_raw.get("action") or {}))
    merged["reward"] = dict(REWARD_DEFAULTS, **(agent_raw.get("reward") or {}))
    scan = (config.get("capture") or {}).get("scan") or {}
    band = str(scan.get("bands", "auto"))
    merged["capture_bands"] = _band_for_sweep(band)
    merged["whitelist"] = (config.get("capture") or {}).get("whitelist") or []
    merged["dwell_seconds"] = scan.get("dwell_seconds", 5)
    return merged


def _band_for_sweep(bands):
    """Map ``[capture.scan] bands`` to the sweep-band posture's band token."""
    if bands in ("2", "5", "6", "60"):
        return bands
    return "both"


def resolve_train_mode(train, meminfo_path="/proc/meminfo"):
    """Resolve ``[agent] train``. ``off``/``idle`` are explicit and always win;
    ``auto`` reads ``MemTotal`` and returns ``idle`` on a >= ~4 GB board, ``off``
    on smaller ones (the Zero 2 W / a 2 GB Pi 5)."""
    mode = str(train or "auto").lower()
    if mode in ("off", "idle"):
        return mode
    total = _mem_total_bytes(meminfo_path)
    return "idle" if total is not None and total >= TRAIN_AUTO_MIN_BYTES else "off"


def _mem_total_bytes(meminfo_path="/proc/meminfo"):
    return _meminfo_field("MemTotal:", meminfo_path)


def _mem_available_bytes(meminfo_path="/proc/meminfo"):
    return _meminfo_field("MemAvailable:", meminfo_path)


def _meminfo_field(field, meminfo_path="/proc/meminfo"):
    try:
        with open(meminfo_path, "r", encoding="utf-8") as handle:
            for line in handle:
                if line.startswith(field):
                    return int(line.split()[1]) * 1024
    except (OSError, ValueError, IndexError):
        return None
    return None


def _cpu_temp_c(path="/sys/class/thermal/thermal_zone0/temp"):
    """CPU temperature in degrees C, or None if unreadable."""
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return int(handle.read().strip()) / 1000.0
    except (OSError, ValueError):
        return None


# -- the launcher surface (hardware control, device-only) -------------------
class LauncherSurface:
    """The Control surface the env applies actions through. It resolves the
    capture adapter's channels and writes the per-window scan override the
    capture launcher reads; it never starts AngryOxide itself."""

    def __init__(self, agent_dir=AGENT_DIR, interface="auto", logger=None):
        self.agent_dir = agent_dir
        self.interface = interface
        self.log = logger
        self._override = os.path.join(agent_dir, OVERRIDE_NAME)

    def supported_channels(self):
        iface = resolve_capture_iface(self.interface)
        if not iface:
            return []
        return supported_channels(iface)

    def apply(self, plan, dwell, transmit):
        override = {"dwell": int(dwell), "transmit": transmit}
        override.update(plan)
        # Normalised through JSON so it compares equal to what was written.
        override = json.loads(json.dumps(override))
        # The launcher relaunches AngryOxide whenever this file's content
        # changes, and a relaunch restarts the scan, so a window that keeps the
        # same plan leaves the file alone.
        if self._written() == override:
            return
        tmp = self._override + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as handle:
                json.dump(override, handle)
            os.replace(tmp, self._override)
        except OSError as exc:
            if self.log:
                self.log.warning("could not write scan override: %s", exc)


    def _written(self):
        """The override currently on disk, or None if absent or unreadable."""
        try:
            with open(self._override, encoding="utf-8") as handle:
                return json.load(handle)
        except (OSError, ValueError):
            return None


def resolve_capture_iface(want="auto"):
    """The capture interface: an explicit name, or the first monitor-capable
    wlan whose driver is not the built-in chip. Mirrors the shell
    ``capture_resolve_iface``; returns None if none is found."""
    if want and want != "auto":
        return want
    try:
        names = sorted(n for n in os.listdir("/sys/class/net") if n.startswith("wlan"))
    except OSError:
        return None
    for dev in names:
        if _iface_driver(dev) == WIFI_DRIVER:
            continue
        if _monitor_capable(dev):
            return dev
    return None


def _iface_driver(dev):
    link = "/sys/class/net/%s/device/driver" % dev
    try:
        return os.path.basename(os.readlink(link))
    except OSError:
        return ""


def _phy_for(dev):
    try:
        with open("/sys/class/net/%s/phy80211/name" % dev, "r",
                  encoding="utf-8") as handle:
            return handle.read().strip()
    except OSError:
        return ""


def _iw_phy_info(phy):
    try:
        return subprocess.run(["iw", "phy", phy, "info"], capture_output=True,
                              text=True, timeout=10, check=False).stdout
    except (OSError, subprocess.SubprocessError):
        return ""


def _monitor_capable(dev):
    phy = _phy_for(dev)
    return bool(phy) and "* monitor" in _iw_phy_info(phy)


def supported_channels(dev):
    """The non-disabled channels the adapter supports across its bands, as a
    sorted int list (mirrors the shell ``capture_auto_channels``)."""
    import re

    phy = _phy_for(dev)
    if not phy:
        return []
    channels = []
    for line in _iw_phy_info(phy).splitlines():
        if "disabled" in line:
            continue
        match = re.search(r"\[(\d+)\]", line)
        if match:
            channels.append(int(match.group(1)))
    return sorted(dict.fromkeys(channels))


# -- the agent controller ---------------------------------------------------
class AgentController:
    def __init__(self, config_path=DEFAULT_CONFIG_PATH, agent_dir=AGENT_DIR,
                 capture_store=DEFAULT_CAPTURE_STORE, surface=None,
                 status_source=None, logger=None):
        self.log = logger or logging.getLogger("kestrel_pi.agent")
        self.config_path = config_path
        self.agent_dir = agent_dir
        self.config = load_config(config_path, self.log)
        self.agent_cfg = agent_config(self.config)
        self.capture = CaptureInterface(capture_store)
        self.capture_store = capture_store
        self.surface = surface or LauncherSurface(
            agent_dir, self.config.get("capture", {}).get("interface", "auto"),
            self.log)
        self.status_source = status_source or self._default_status_source
        self._stop = threading.Event()
        self._recent = deque(maxlen=5000)   # recent episode rows for live metrics
        self._current_policy = "baseline"
        self._train_mode = resolve_train_mode(self.agent_cfg.get("train"))
        # On-device training tunables, read from config with safety clamps so a
        # bad value can't cook the board or OOM it. The board-capability gate
        # (_board_can_train) still blocks training outright on too-small boards.
        self._train_while_capturing = self._resolve_train_while_capturing(
            self.agent_cfg.get("train_while_capturing", "auto"))
        self._train_min_new_episodes = max(
            1, int(self.agent_cfg.get("train_min_new_episodes", 60) or 60))
        self._train_timesteps = max(
            100, int(self.agent_cfg.get("train_timesteps", 3000) or 3000))
        self._train_max_temp_c = min(
            85.0, float(self.agent_cfg.get("train_max_temp_c", 75) or 75))
        self._train_min_free_bytes = max(
            256, int(self.agent_cfg.get("train_min_free_mb", 512) or 512)) * 1024 * 1024
        # Quality-aware promotion knobs (clamped): the held-out lift margin a
        # staged remote model must clear, and the soft staleness guard.
        self._promotion_margin = max(
            0.0, float(self.agent_cfg.get("promotion_margin", 0.10) or 0.0))
        self._promotion_max_stale = max(
            0, int(self.agent_cfg.get("promotion_max_stale_episodes", 900) or 0))
        # New episodes logged since the last training run drives the periodic
        # trigger (replaces a fixed time interval).
        self._episodes_since_train = 0
        # On-device idle-training indicator for the dashboard: whether a run is in
        # flight, and when the last one finished / how long it took. Seeded from a
        # small file so "last trained" survives an agent or daemon restart.
        self._training = False
        self._last_trained = None
        self._last_train_secs = None
        # The running training child process (kestrel-agent-train), so stop()
        # can terminate it; None when no fine-tune is in flight.
        self._train_proc = None
        # A lifetime tally of model adoptions by source (a locally trained model
        # vs a promoted remote model becoming the active model), seeded from a
        # small file so it survives a restart.
        self._promotion_tally = {"local": 0, "remote": 0}

        os.makedirs(self.agent_dir, exist_ok=True)
        os.makedirs(os.path.join(self.agent_dir, EPISODES_DIR), exist_ok=True)
        self._write_episodes_context()
        self._status_path = os.path.join(self.agent_dir, STATUS_NAME)
        self._last_train_path = os.path.join(self.agent_dir, LAST_TRAIN_NAME)
        self._tally_path = os.path.join(self.agent_dir, PROMOTION_TALLY_NAME)
        self._read_promotion_tally()
        self._train_request_path = os.path.join(self.agent_dir, TRAIN_REQUEST_NAME)
        # The per-algorithm model file for the configured algorithm - the path
        # training writes and the decide loop watches. load_policy resolves the
        # file to actually load (this one, or a legacy model.zip for migration).
        self._model_path = os.path.join(
            self.agent_dir, model_name_for(self.agent_cfg.get("algorithm")))
        # The remote-train plugin stages a pulled model for the configured
        # algorithm here; the decide loop watches it (like the active model) and
        # scores it for promotion when it changes. Set once for the configured
        # algorithm, which is the only one this agent process drives.
        _slug = model_slug(self.agent_cfg.get("algorithm"))
        self._staging_dir = os.path.join(self.agent_dir, REMOTE_STAGING_DIR, _slug)
        self._staged_model_path = os.path.join(
            self._staging_dir, model_name_for(self.agent_cfg.get("algorithm")))
        self._staged_meta_path = os.path.join(
            self._staging_dir, model_meta_name_for(self.agent_cfg.get("algorithm")))
        # None so a model already staged at start-up is evaluated once; thereafter
        # only a changed mtime (a freshly pulled model) triggers a re-score.
        self._staged_mtime = None
        self._dwell_seconds = self.agent_cfg.get("dwell_seconds", 5)
        # GPS observation seam (Phase 4.8): whether to read the fix at all, and
        # how old a fix may be before it reads as "no GPS" (2x a decision window,
        # with a 15 s floor so a short window still tolerates a ~1 Hz receiver).
        self._use_gps = bool(self.agent_cfg.get("use_gps", True))
        self._gps_stale_seconds = max(
            15.0, 2.0 * int(self.agent_cfg.get("decision_seconds", 30) or 30))
        # How often the idle wait re-checks [capture] enabled while the agent is
        # enabled but capture is off (capped so capture coming back is noticed
        # within a few seconds, but no busier than a decision window).
        self._idle_poll_seconds = max(
            2, min(15, int(self.agent_cfg.get("decision_seconds", 30) or 30)))
        self._read_last_trained()
        self._episode_file = os.path.join(
            self.agent_dir, EPISODES_DIR,
            time.strftime("episodes-%Y%m%d.jsonl", time.gmtime()))

    # -- capture gate ----------------------------------------------------
    def capture_enabled(self):
        return bool((self.config.get("capture") or {}).get("enabled", False))

    def _capture_enabled_fresh(self):
        """Whether ``[capture] enabled`` is true *right now*, re-read from
        config.toml. :meth:`capture_enabled` reads the config loaded once at
        start-up; the capture on/off toggle rewrites config.toml with no agent
        restart, so the decide loop re-reads here to notice capture going off -
        or coming back - at runtime. Falls back to the start-up value if the
        file can't be read, so a transient bad read never trips a stand-down."""
        try:
            cfg = load_config(self.config_path, self.log)
        except Exception:  # noqa: BLE001 - a bad read must not stop the agent
            return self.capture_enabled()
        return bool((cfg.get("capture") or {}).get("enabled", False))

    def _await_capture(self):
        """Block until ``[capture] enabled`` is true, or until asked to stop.
        Returns True once capture is on, False if stopped first. While waiting it
        publishes an idle status so the dashboard shows the agent as enabled but
        idle (capture off) instead of a bare stopped service - the agent is
        capture-gated, so it idles rather than exits when capture is off."""
        if self._capture_enabled_fresh():
            return True
        self.log.info("[capture] enabled = false; the agent is idle until "
                      "capture is enabled")
        self._write_status(False, self._current_policy,
                           "capture disabled; agent idle")
        while not self._stop.wait(self._idle_poll_seconds):
            if self._capture_enabled_fresh():
                return True
        return False

    def _clear_override(self):
        """Remove the scan-override file so that, while the agent is idle, a
        capture (re)launch falls back to the configured sweep instead of a stale
        agent plan - the launcher's ``agent_active()`` treats a missing override
        as 'not driving'. Best-effort; a missing file is fine."""
        try:
            os.remove(os.path.join(self.agent_dir, OVERRIDE_NAME))
        except OSError:
            pass

    # -- status / logging ------------------------------------------------
    def _write_status(self, running, policy_name, detail, last_info=None,
                      last_reward=None):
        can_train, train_blocked = self._train_feasibility()
        status = {
            "running": running,
            "policy": policy_name,
            "algorithm": self.agent_cfg.get("algorithm"),
            "model": os.path.basename(self._model_path),
            "train": self._train_mode,
            "training": self._training,
            # Whether a manual "train now" from the dashboard can start now, and
            # a short reason when not, so the web Train button can gate honestly.
            "can_train": can_train,
            "train_blocked": train_blocked,
            "last_trained": self._last_trained,
            "last_train_secs": self._last_train_secs,
            # Lifetime adoptions by source: {"local": N, "remote": M}.
            "promotion_tally": dict(self._promotion_tally),
            "session_yield": self._session_yield(),
            "decisions": len(self._recent),
            "detail": detail or "",
            "updated": time.time(),
            "metrics": metrics_mod.summarize(
                list(self._recent),
                window_minutes=int(self.agent_cfg.get("metrics_window_minutes", 60) or 0)
                or None),
        }
        if policy_name == "learned":
            # Where the model in use came from (its sidecar's source): "local" if
            # this device trained it, "remote" if it was pulled from the remote
            # training host. Omitted for a model trained before this was stamped.
            source = policies_mod.read_meta(
                self.agent_dir, self.agent_cfg.get("algorithm")).get("source")
            if source:
                status["model_source"] = source
        if last_info:
            status["posture"] = last_info.get("posture")
            status["dwell"] = last_info.get("dwell")
            status["transmit"] = last_info.get("transmit")
        if last_reward is not None:
            status["last_reward"] = last_reward
        tmp = self._status_path + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as handle:
                json.dump(status, handle)
            os.replace(tmp, self._status_path)
        except OSError as exc:
            self.log.warning("could not write status: %s", exc)

    def _session_yield(self):
        return sum(int(r.get("new_hashlines", 0) or 0) for r in self._recent)

    # -- last-training record (persisted so it survives a restart) -------
    def _read_last_trained(self):
        """Seed the last-training time/duration from its file, if present. A
        missing or malformed file just leaves them unset (best-effort)."""
        try:
            with open(self._last_train_path, "r", encoding="utf-8") as handle:
                data = json.load(handle)
            self._last_trained = float(data["finished"])
            secs = data.get("seconds")
            self._last_train_secs = float(secs) if secs is not None else None
        except (OSError, ValueError, KeyError, TypeError):
            pass

    def _record_trained(self, finished, seconds):
        """Remember the last completed idle-training run and persist it."""
        self._last_trained = finished
        self._last_train_secs = seconds
        tmp = self._last_train_path + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as handle:
                json.dump({"finished": finished, "seconds": seconds}, handle)
            os.replace(tmp, self._last_train_path)
        except OSError as exc:
            self.log.warning("could not write last-training record: %s", exc)

    # -- promotion tally (local vs remote adoptions, persisted) ----------
    def _read_promotion_tally(self):
        """Seed the lifetime local/remote adoption tally from its file. A
        missing or malformed file just leaves the counts at zero (best-effort)."""
        try:
            with open(self._tally_path, "r", encoding="utf-8") as handle:
                data = json.load(handle)
        except (OSError, ValueError):
            return
        if isinstance(data, dict):
            for key in self._promotion_tally:
                try:
                    self._promotion_tally[key] = max(0, int(data.get(key, 0) or 0))
                except (TypeError, ValueError):
                    pass

    def _bump_promotion_tally(self, source):
        """Record one model adoption by ``source`` ("local" or "remote") and
        persist the running tally. Best-effort - a write failure just logs."""
        if source not in self._promotion_tally:
            return
        self._promotion_tally[source] += 1
        tmp = self._tally_path + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as handle:
                json.dump(self._promotion_tally, handle)
            os.replace(tmp, self._tally_path)
        except OSError as exc:
            self.log.warning("could not write promotion tally: %s", exc)

    def _write_episodes_context(self):
        """Tag the episodes dir with a coarse, PII-free context (board + env
        spec) so a future fleet trainer that pools logs from several devices can
        segment or weight by device class. It holds no captures, SSIDs, client
        identifiers or locations - only the board model and the observation spec
        version, the same whitelist-safe data the reward already uses.
        Best-effort, rewritten each start so the board/spec stay current."""
        path = os.path.join(self.agent_dir, EPISODES_DIR, CONTEXT_NAME)
        context = {
            "spec_version": spec.SPEC_VERSION,
            "board": _read_board_model(),
            "written": time.time(),
        }
        try:
            with open(path, "w", encoding="utf-8") as handle:
                json.dump(context, handle)
        except OSError as exc:
            self.log.warning("could not write episodes context: %s", exc)

    def _log_episode(self, record):
        record = dict(record, ts=time.time(), policy=self._current_policy)
        self._recent.append(record)
        self._episodes_since_train += 1
        self._rotate_episodes()
        try:
            with open(self._episode_file, "a", encoding="utf-8") as handle:
                handle.write(json.dumps(record) + "\n")
        except OSError as exc:
            self.log.warning("could not append episode log: %s", exc)

    def _rotate_episodes(self):
        """Keep the episodes dir under ``log_max_mb`` (oldest files roll out) so
        the agent dir the backup plugin mirrors stays bounded."""
        cap_mb = int(self.agent_cfg.get("log_max_mb", 64) or 0)
        if cap_mb <= 0:
            return
        directory = os.path.join(self.agent_dir, EPISODES_DIR)
        try:
            files = sorted(os.path.join(directory, n)
                           for n in os.listdir(directory) if n.endswith(".jsonl"))
        except OSError:
            return
        total = 0
        sizes = []
        for path in files:
            try:
                size = os.path.getsize(path)
            except OSError:
                size = 0
            sizes.append((path, size))
            total += size
        cap = cap_mb * 1024 * 1024
        i = 0
        while total > cap and i < len(sizes) - 1:  # never delete the newest
            path, size = sizes[i]
            if path == self._episode_file:
                break
            try:
                os.remove(path)
                total -= size
            except OSError:
                pass
            i += 1

    # -- status source (best-effort RF; see module docstring) -----------
    def _default_status_source(self):
        """AngryOxide v0.9.3 exposes no stable live-status API, so RF
        observations are best-effort and may be empty; the store-delta yield
        still drives the reward. A future contract_version bump could add a
        live status channel here. Returns None (empty RF obs) for now."""
        return None

    # -- GPS seam (Phase 4.8) --------------------------------------------
    def _read_gps_fix(self):
        """Read the gps plugin's latest-fix seam file (:data:`POSITION_FILE`),
        honoring staleness. Returns the reading dict (``fix``/``lat``/``lon``/
        ``sats``/``hdop``/``cn0``) or None when GPS is switched off (``use_gps``),
        the file is missing or unparseable, or the fix is stale. Best-effort: any
        error reads as "no GPS", never a stall - the env zeroes the GPS features
        and leaves ``gps_valid`` 0."""
        if not self._use_gps:
            return None
        try:
            with open(POSITION_FILE, "r", encoding="utf-8") as handle:
                data = json.load(handle)
        except (OSError, ValueError):
            return None
        if not isinstance(data, dict):
            return None
        try:
            age = time.time() - float(data.get("ts"))
        except (TypeError, ValueError):
            return None
        if age < 0 or age > self._gps_stale_seconds:
            return None
        return data

    # -- policy (re)loading ----------------------------------------------
    def _model_mtime(self):
        """The configured algorithm's model-file mtime, or None when there is no
        model yet. The decide loop watches this so a model trained or dropped in
        after the agent started is adopted without an agent restart."""
        try:
            return os.stat(self._model_path).st_mtime
        except OSError:
            return None

    def _load_configured_policy(self):
        """Load the configured policy and return ``(policy, name, detail,
        model_mtime)``. ``model_mtime`` is the ``model.zip`` mtime at load time,
        so the decide loop can tell when a newer model appears and reload."""
        policy, name, detail = policies_mod.load_policy(
            self.agent_cfg.get("policy"), self.agent_cfg.get("algorithm"),
            self.agent_dir, dwell_seconds=self._dwell_seconds, logger=self.log)
        return policy, name, detail, self._model_mtime()

    # -- quality-aware promotion (agent owns it) -------------------------
    def _staged_model_mtime(self):
        """The mtime of the remote-train plugin's staged model for the configured
        algorithm, or None when nothing is staged. The decide loop watches this so
        a pulled remote model is scored and (if it wins) promoted with no restart.
        """
        try:
            return os.stat(self._staged_model_path).st_mtime
        except OSError:
            return None

    def _maybe_promote_staged(self):
        """Score a freshly staged remote model against the active one on a common
        held-out slice and promote the winner (docs/developer/
        agent-interface.md). Returns True if the active model was replaced, so the caller reloads
        the policy. Best-effort: any error leaves the active model untouched. The
        heavy scoring lives here, in the agent process, so the always-on daemon
        and its plugins stay torch-free."""
        try:
            with open(self._staged_meta_path, "r", encoding="utf-8") as handle:
                staged_meta = json.load(handle)
            if not isinstance(staged_meta, dict):
                return False
        except (OSError, ValueError):
            return False
        algorithm = self.agent_cfg.get("algorithm")
        staged_spec = staged_meta.get("spec_version")
        if staged_spec is not None and staged_spec != spec.SPEC_VERSION:
            self.log.info("staged %s model is env-spec v%s != v%s; not promoting",
                          algorithm, staged_spec, spec.SPEC_VERSION)
            return False
        if not policies_mod.has_model_for(self.agent_dir, algorithm):
            return self._adopt_staged(staged_meta, "no local model yet (bootstrap)")
        try:
            from . import train as train_mod

            remote_policy, why = policies_mod.load_learned(
                algorithm, self._staged_model_path, logger=self.log)
            if remote_policy is None:
                self.log.info("staged %s model did not load (%s); not promoting",
                              algorithm, why)
                return False
            local_policy, why = policies_mod.load_learned(
                algorithm,
                policies_mod.model_path_for(self.agent_dir, algorithm),
                logger=self.log)
            if local_policy is None:
                # The incumbent is unusable, so a working remote is strictly
                # better - adopt it rather than keep falling back to the baseline.
                return self._adopt_staged(
                    staged_meta, "incumbent model unusable (%s)" % why)
            baseline = policies_mod.BaselinePolicy(dwell_seconds=self._dwell_seconds)
            rows = metrics_mod.load_rows(os.path.join(self.agent_dir, EPISODES_DIR))
            scores = train_mod.score_candidates(
                rows, {"local": local_policy, "remote": remote_policy,
                       "baseline": baseline})
        except Exception:  # noqa: BLE001 - scoring must never crash the agent
            self.log.exception("scoring the staged %s model failed; not promoting",
                               algorithm)
            return False
        if scores is None:
            self.log.info("too few logged windows to score the staged %s model; "
                          "keeping the local model", algorithm)
            return False
        new_since = metrics_mod.new_rows_since(
            rows, metrics_mod.meta_watermark(staged_meta)[0])
        promote, reason = metrics_mod.promotion_decision(
            scores, has_local=True, new_since_remote=new_since,
            margin=self._promotion_margin,
            max_stale_episodes=self._promotion_max_stale)
        self.log.info("promotion check (%s): %s | scores=%s", algorithm, reason,
                      {k: round(v, 4) for k, v in scores.items()})
        if promote:
            return self._adopt_staged(staged_meta, reason)
        return False

    def _adopt_staged(self, staged_meta, reason):
        """Move the staged model + sidecar into the active per-algorithm slot,
        restamping the sidecar ``source`` as ``remote`` (so the dashboard shows a
        pulled model). Atomic (``os.replace``) and refused while a training run is
        writing the model. Returns True on success."""
        if self._training:
            return False
        algorithm = self.agent_cfg.get("algorithm")
        active_model = os.path.join(self.agent_dir, model_name_for(algorithm))
        active_meta = os.path.join(self.agent_dir, model_meta_name_for(algorithm))
        staged_meta = dict(staged_meta, source="remote")
        try:
            with open(self._staged_meta_path, "w", encoding="utf-8") as handle:
                json.dump(staged_meta, handle)
            # Model first, then sidecar, so a reader never sees a model without
            # its matching meta.
            os.replace(self._staged_model_path, active_model)
            os.replace(self._staged_meta_path, active_meta)
        except OSError as exc:
            self.log.warning("promoting the staged %s model failed: %s",
                             algorithm, exc)
            return False
        self.log.info("promoted a remote %s model into the active slot: %s",
                      algorithm, reason)
        self._bump_promotion_tally("remote")
        return True

    # -- the decide loop -------------------------------------------------
    def run(self):
        # Capture-gated: idle (not exit) until capture is on, so enabling capture
        # brings the agent back on its own - at boot and at runtime alike.
        if not self._await_capture():
            return 0   # asked to stop while idle

        from .env import CaptureEnv

        env = CaptureEnv(
            surface=self.surface, capture=self.capture, config=self.agent_cfg,
            status_source=self.status_source, gps_source=self._read_gps_fix,
            episode_logger=self._log_episode, logger=self.log)
        if not env.supported:
            self.log.warning("no capture adapter channels resolved; the agent "
                             "will still run but drives nothing until a dongle "
                             "is present")

        learned, learned_name, detail, model_mtime = self._load_configured_policy()
        baseline = policies_mod.BaselinePolicy(dwell_seconds=self._dwell_seconds)
        if detail:
            self.log.info("policy: %s", detail)

        interleave = bool(self.agent_cfg.get("interleave_compare"))
        block_seconds = max(60, int(self.agent_cfg.get("compare_block_minutes", 10)
                                    or 10) * 60)

        self.log.info("agent starting: algorithm=%s policy=%s train=%s "
                      "decision_seconds=%s channels=%d",
                      self.agent_cfg.get("algorithm"), learned_name,
                      self._train_mode, env.decision_seconds, len(env.supported))

        # The on-device training thread. It always runs so a manual "train now"
        # from the dashboard is picked up promptly; it additionally fine-tunes
        # opportunistically while idle only when train = idle (or auto on a
        # >= 4 GB board). A low-priority background thread; it never touches the
        # decide loop and backs off under memory/thermal pressure.
        threading.Thread(target=self._train_loop, name="agent-train",
                         daemon=True).start()

        obs, _info = env.reset()
        block_started = time.monotonic()
        # Which policy drives now. Without interleave: the configured one
        # (learned, with baseline fallback). With interleave: alternate blocks.
        driver, self._current_policy = (
            (baseline, "baseline") if interleave else (learned, learned_name))
        self._write_status(True, self._current_policy, detail)

        while not self._stop.is_set():
            # Stand down to idle if capture was turned off at runtime. Re-read
            # config (the capture toggle rewrites config.toml with no agent
            # restart); only the start-up gate checked capture before, so a
            # running agent otherwise kept looping and publishing "running" while
            # driving a stopped capture. Drop our override so the launcher falls
            # back to the configured sweep, wait for capture (or a stop), then
            # resume with a fresh episode.
            if not self._capture_enabled_fresh():
                self.log.info("[capture] turned off at runtime; the agent is "
                              "standing down to idle until capture returns")
                self._clear_override()
                if not self._await_capture():
                    break
                self.log.info("[capture] back on; the agent is resuming")
                obs, _info = env.reset()
                driver.reset()
                self._write_status(True, self._current_policy, detail)
                continue

            # Quality-aware promotion: when the remote-train plugin stages a
            # pulled model, score it against the active one on a held-out slice
            # and adopt the winner. Only on an mtime change (a freshly staged
            # model) and never mid-training. A successful promote rewrites the
            # active model, so the mtime watch below reloads the policy this tick.
            if not self._training:
                staged_mtime = self._staged_model_mtime()
                if staged_mtime is not None and staged_mtime != self._staged_mtime:
                    self._staged_mtime = staged_mtime
                    if self._maybe_promote_staged():
                        self._staged_mtime = None   # staged files moved to active

            # Adopt a model trained or dropped in after start, with no agent
            # restart: when model.zip's mtime changes (and we're not writing it
            # ourselves mid-run), reload the configured policy and refresh the
            # status note. The mtime is recorded even on a failed load, so a bad
            # model is not retried every tick - only when it changes again.
            if not self._training:
                current_mtime = self._model_mtime()
                if current_mtime != model_mtime:
                    model_mtime = current_mtime
                    learned, learned_name, detail, _ = self._load_configured_policy()
                    self.log.info("policy reloaded: %s", detail or learned_name)
                    if not interleave or self._current_policy != "baseline":
                        driver, self._current_policy = learned, learned_name
            if interleave and time.monotonic() - block_started >= block_seconds:
                if self._current_policy == "baseline":
                    driver, self._current_policy = learned, learned_name
                else:
                    driver, self._current_policy = baseline, "baseline"
                driver.reset()
                block_started = time.monotonic()
                self.log.info("interleave: now driving with %s", self._current_policy)

            try:
                action, _state = driver.predict(obs)
            except Exception:  # noqa: BLE001 - a predict failure falls back a step
                self.log.exception("policy predict failed; using baseline action")
                action, _state = baseline.predict(obs)
            obs, reward, _term, _trunc, info = env.step(action)
            self._write_status(True, self._current_policy, detail, info, reward)

        self.log.info("agent stopping")
        self._write_status(False, self._current_policy, "stopped")
        return 0

    # -- on-device training ---------------------------------------------
    TRAIN_POLL_SECONDS = 15        # how often the loop checks for work
    TRAIN_NICE = 10                # scheduling niceness for the training child

    def _train_loop(self):
        """Handle on-device training. It honors a manual "train now" request
        from the dashboard promptly, and - when training is enabled for this
        board (``train = idle``, which ``auto`` resolves to on a big board) -
        fine-tunes periodically once enough new episodes have accrued. By
        default it trains during capture too (``train_while_capturing``); set
        that false to train only while the device is idle. The actual fine-tune
        runs in a separate, niced child process (see _run_training), so this
        loop only orchestrates it; the board gate blocks training outright on a
        board too small for it."""
        while not self._stop.wait(self.TRAIN_POLL_SECONDS):
            if self._consume_train_request():
                self._run_training("manual", force=True)
                continue
            if self._should_auto_train():
                self._run_training("auto")

    def _should_auto_train(self):
        """Whether a periodic (non-manual) fine-tune should start now: training
        enabled for this board, enough new episodes since the last run, not
        gated out by the idle requirement (when train_while_capturing is off),
        and memory/thermal headroom."""
        if self._train_mode != "idle":
            return False
        if not self._board_can_train():
            return False
        if self._episodes_since_train < self._train_min_new_episodes:
            return False
        if not self._train_while_capturing and not self._is_idle():
            return False
        return self._resources_ok()

    def _child_preexec(self):
        """Run in the training child after fork, before exec: drop its whole
        process (every torch compute thread) to a low priority so capture and
        the decide loop win the scheduler. A per-thread renice in the parent
        could not reach torch's worker threads; nicing the child process does.
        Best-effort - skipped where os.nice is unavailable."""
        try:
            os.nice(self.TRAIN_NICE)
        except (OSError, AttributeError):  # not Linux / unsupported
            pass

    def _training_command(self):
        """The argv for one fine-tune. Runs this interpreter's
        ``-m kestrel_pi.agent.train`` entry point (the same trainer remote-train
        uses), so it needs nothing on PATH and shares the venv's torch/SB3."""
        cmd = [sys.executable, "-m", "kestrel_pi.agent.train",
               "--agent-dir", self.agent_dir,
               "--timesteps", str(self._train_timesteps),
               "--config", self.config_path]
        algorithm = self.agent_cfg.get("algorithm")
        if algorithm:
            cmd += ["--algorithm", algorithm]
        return cmd

    def _spawn_training(self):
        """Run one fine-tune as a short-lived, niced child process and wait for
        it, returning its exit code (0 = a model was written). Isolating the
        run in its own process is what keeps a torch OOM or crash from taking
        down this agent's inference loop - the child dies, the decide loop goes
        on. The child's torch thread pool is capped via the environment so it
        can't spread across every core. stop() terminates it through
        ``self._train_proc``."""
        cmd = self._training_command()
        env = dict(os.environ)
        from .train import _training_thread_cap
        cap = str(_training_thread_cap())
        for var in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS",
                    "MKL_NUM_THREADS", "NUMEXPR_NUM_THREADS"):
            env.setdefault(var, cap)
        try:
            proc = subprocess.Popen(
                cmd, env=env, stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT, text=True,
                start_new_session=True, preexec_fn=self._child_preexec)
        except OSError:
            self.log.exception("could not launch the training subprocess")
            return 1
        self._train_proc = proc
        try:
            out, _err = proc.communicate()
        finally:
            self._train_proc = None
        for line in (out or "").splitlines():
            if line.strip():
                self.log.info("train: %s", line.rstrip())
        return proc.returncode

    def _resolve_train_while_capturing(self, value):
        """Resolve the ``train_while_capturing`` config. ``auto`` (the default)
        is board-aware: on when this board can train, off otherwise. An explicit
        true/false (bool, or the strings) is honoured as given."""
        if isinstance(value, str):
            token = value.strip().lower()
            if token == "auto":
                return self._board_can_train()
            return token in ("true", "1", "yes", "on")
        return bool(value)

    def _board_can_train(self):
        """Whether this board is big enough to train on at all. Blocks on-device
        training on a board below the auto threshold (e.g. a Zero 2 W), whatever
        the config says, so training can never be turned on where it would OOM
        or starve capture. ``None`` MemTotal (can't read it) is treated as OK."""
        total = _mem_total_bytes()
        return total is None or total >= TRAIN_AUTO_MIN_BYTES

    def _run_training(self, reason, force=False):
        """Run one short fine-tune from the logged episodes in a child process,
        surfacing it on the dashboard (the ``training`` flag and the last-trained
        record). A manual run (``force``) bypasses the new-episode and idle gates
        but still refuses on a too-small board or under memory/thermal pressure,
        reporting why. The heavy ML work runs in a separate, niced
        kestrel-agent-train process (see _spawn_training), so a torch OOM or
        crash there can't take down this agent's inference loop; a non-zero exit
        just leaves the model as it was."""
        if not self._board_can_train():
            if force:
                self._write_status(
                    True, self._current_policy,
                    "training not supported on this board (needs ~4 GB RAM)")
            return
        if not self._resources_ok():
            if force:
                self._write_status(
                    True, self._current_policy,
                    "training not started: low memory or the CPU is too hot")
            return
        started = time.time()
        self._training = True
        self._episodes_since_train = 0   # reset the new-episode trigger
        # Surface "training" at once rather than waiting for the next decision
        # tick, so the dashboard shows it promptly.
        self._write_status(True, self._current_policy, "training (%s)" % reason)
        try:
            code = self._spawn_training()
            if code == 0:
                self._record_trained(time.time(), time.time() - started)
                # A completed run overwrites the active model with a freshly
                # trained local one, so it counts as a local adoption.
                self._bump_promotion_tally("local")
                self.log.info("%s training finished (%.0fs)",
                              reason, self._last_train_secs or 0.0)
            else:
                # The child reports the reason (e.g. no episode logs) on its own
                # log; a non-zero exit means no new model was written.
                self.log.info("%s training did not complete (exit %s)",
                              reason, code)
        except Exception:  # noqa: BLE001 - never let training kill the agent
            self.log.exception("%s training failed", reason)
        finally:
            self._training = False
            self._write_status(True, self._current_policy, "")

    def _consume_train_request(self):
        """True if a manual "train now" request file was present (removing it so
        the run happens once). A missing file just returns False."""
        try:
            os.remove(self._train_request_path)
            return True
        except OSError:
            return False

    def _has_training_data(self):
        """Whether there is any logged episode data to train from - this
        session's rows, or a persisted episode log from an earlier run."""
        if self._recent:
            return True
        episodes = os.path.join(self.agent_dir, EPISODES_DIR)
        try:
            return any(name.endswith(".jsonl") for name in os.listdir(episodes))
        except OSError:
            return False

    def _train_feasibility(self):
        """Whether a manual training run can start now, and a short reason when
        not, for the dashboard's Train button. (Periodic idle training has its
        own idle gate; this only governs the manual button.)"""
        if self._training:
            return False, "a training run is already in progress"
        if not self._board_can_train():
            return False, "this board is too small for on-device training (needs ~4 GB RAM)"
        if not self._has_training_data():
            return False, "no episode data yet - let capture run a while first"
        if not self._resources_ok():
            return False, "not enough free memory or the CPU is too hot right now"
        return True, ""

    def _is_idle(self):
        """Idle = the recent windows caught nothing, so a fine-tune won't steal
        cycles from a productive capture."""
        recent = list(self._recent)[-10:]
        if len(recent) < 5:
            return False
        return sum(int(r.get("new_hashlines", 0) or 0) for r in recent) == 0

    def _resources_ok(self):
        free = _mem_available_bytes()
        if free is not None and free < self._train_min_free_bytes:
            return False
        temp = _cpu_temp_c()
        if temp is not None and temp >= self._train_max_temp_c:
            return False
        return True

    def stop(self):
        self._stop.set()
        # Tear down an in-flight training child so the agent exits promptly
        # instead of blocking on a long fine-tune. Best-effort: it may finish
        # or clear itself between the read and the signal.
        proc = self._train_proc
        if proc is not None and proc.poll() is None:
            try:
                proc.terminate()
            except (OSError, ProcessLookupError):
                pass


def main(argv=None):
    parser = argparse.ArgumentParser(prog="kestrel-agent")
    parser.add_argument("--config", default=DEFAULT_CONFIG_PATH)
    parser.add_argument("--agent-dir", default=AGENT_DIR)
    parser.add_argument("--capture-store", default=DEFAULT_CAPTURE_STORE)
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=getattr(logging, str(args.log_level).upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    log = logging.getLogger("kestrel_pi.agent")

    controller = AgentController(
        config_path=args.config, agent_dir=args.agent_dir,
        capture_store=args.capture_store, logger=log)

    def _handle(signum, _frame):
        log.info("signal %s received", signum)
        controller.stop()

    signal.signal(signal.SIGTERM, _handle)
    signal.signal(signal.SIGINT, _handle)
    return controller.run()


if __name__ == "__main__":
    raise SystemExit(main())
