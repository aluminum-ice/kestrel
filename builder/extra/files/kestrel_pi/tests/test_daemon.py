"""The daemon's start-up UI population.

Enabling a plugin restarts the daemon, so the dashboard shows the newly loaded
plugin right after the restart. Without an immediate ``on_ui_update`` the card
would hold its ``on_ui_setup`` defaults until the first epoch tick (up to
``epoch_seconds`` later) and look like the plugin is not working. This checks
``start()`` dispatches ``on_ui_setup`` and then ``on_ui_update`` at start,
using a recording dispatcher so the assertion is deterministic (the real
dispatcher runs hooks off the loop)."""

import types

from kestrel_pi import daemon as daemon_mod


class _RecDispatcher:
    def __init__(self):
        self.calls = []

    def dispatch(self, record, hook, *args):
        self.calls.append((record.name, hook))

    def shutdown(self, wait=False):
        pass


def test_start_populates_ui_immediately(tmp_path, monkeypatch):
    cfg = tmp_path / "config.toml"
    cfg.write_text("[web]\nenabled = false\n")
    d = daemon_mod.Daemon(config_path=str(cfg), capture_store=str(tmp_path))

    rec = types.SimpleNamespace(name="memtemp")
    monkeypatch.setattr(d.loader, "load_all", lambda: [rec])
    monkeypatch.setattr(d.loader, "loaded_records", lambda: [rec])
    monkeypatch.setattr(d.capture_watcher, "prime", lambda: None)
    monkeypatch.setattr(d, "_spawn", lambda *a, **k: None)   # no background threads
    d.dispatcher = _RecDispatcher()

    d.start()

    hooks = [hook for (_name, hook) in d.dispatcher.calls]
    assert "on_ui_setup" in hooks and "on_ui_update" in hooks
    # Setup must run before the first update so the elements exist to be set.
    assert hooks.index("on_ui_setup") < hooks.index("on_ui_update")


def test_agent_and_capture_state_reflect_runtime_config_changes(tmp_path):
    """Enabling the agent (or setting its policy, or toggling capture) rewrites
    config.toml through the root apply helper without restarting the daemon.
    Status must re-read the file, not the config cached at start-up. Regression:
    it used to read ``self.config``, so after enabling with policy=learned the
    AI Agent card stayed "Off" and the policy radio reverted to baseline even
    though the agent service was running the new config."""
    cfg = tmp_path / "config.toml"
    cfg.write_text('[agent]\nenabled = false\npolicy = "baseline"\n'
                   '[capture]\nenabled = false\nwhitelist = []\n')
    d = daemon_mod.Daemon(config_path=str(cfg), capture_store=str(tmp_path))

    before = d._agent_state()
    assert before["enabled"] is False and before["policy"] == "baseline"

    cfg.write_text('[agent]\nenabled = true\npolicy = "learned"\n'
                   'algorithm = "DQN"\n'
                   '[capture]\nenabled = true\nwhitelist = ["Home"]\n')

    after = d._agent_state()
    assert after["enabled"] is True
    assert after["policy"] == "learned"
    assert after["algorithm"] == "DQN"
    assert d.capture_whitelist() == ["Home"]


def test_format_scan_channel_prefers_agent_override():
    # While the agent drives, its per-window plan wins over the config.
    scan = {"mode": "sweep", "bands": "auto"}
    assert daemon_mod.format_scan_channel({"kind": "autohunt"}, scan) == "hunt"
    assert daemon_mod.format_scan_channel(
        {"kind": "channels", "channels": [1, 6, 11]}, scan) == "1,6,11"
    assert daemon_mod.format_scan_channel(
        {"kind": "channels", "channels": [36]}, scan) == "36"
    # A long channel list collapses rather than overflow the panel.
    assert daemon_mod.format_scan_channel(
        {"kind": "channels", "channels": [1, 6, 11, 36, 40]}, scan) == "5ch"
    assert daemon_mod.format_scan_channel({"kind": "band", "band": "5"}, scan) == "5G"
    assert daemon_mod.format_scan_channel({"kind": "band", "band": "both"}, scan) == "sweep"


def test_format_scan_channel_falls_back_to_config():
    assert daemon_mod.format_scan_channel(None, {"mode": "sweep", "bands": "auto"}) == "sweep"
    assert daemon_mod.format_scan_channel(None, {"mode": "sweep", "bands": "2"}) == "2.4G"
    assert daemon_mod.format_scan_channel(None, {"mode": "autohunt", "bands": "auto"}) == "hunt"
    assert daemon_mod.format_scan_channel(
        None, {"mode": "fixed", "bands": "1,6,11"}) == "1,6,11"
    assert daemon_mod.format_scan_channel(None, {}) == "sweep"


def test_scan_channel_is_none_when_capture_is_off(tmp_path):
    cfg = tmp_path / "config.toml"
    cfg.write_text('[capture]\nenabled = false\nwhitelist = []\n'
                   '[capture.scan]\nmode = "sweep"\nbands = "auto"\n')
    d = daemon_mod.Daemon(config_path=str(cfg), capture_store=str(tmp_path))
    # Not running -> no channel readout (the face never claims a scan it is not
    # doing); running -> the configured plan.
    assert d._scan_channel(None, capture_active=False) is None
    assert d._scan_channel(None, capture_active=True) == "sweep"


def _env_cfg(enabled=True, thresholds=None):
    env = {"enabled": enabled}
    if thresholds is not None:
        env["thresholds"] = thresholds
    return {"capture": {"environment": env}}


def test_environment_state_is_none_when_capture_off_or_disabled(tmp_path):
    cfg = tmp_path / "config.toml"
    cfg.write_text("[web]\nenabled = false\n")
    d = daemon_mod.Daemon(config_path=str(cfg), capture_store=str(tmp_path))
    import json as _json, time as _time
    (tmp_path / "capture-status.json").write_text(_json.dumps(
        {"updated_at": int(_time.time()), "window_s": 300, "radios_recent": 9,
         "data_ts": int(_time.time()), "scanning": True}))
    # capture off -> deaf -> no readout at all
    assert d._environment_state(_env_cfg(), capture_active=False) is None
    # capture on but the feature disabled -> no readout
    assert d._environment_state(_env_cfg(enabled=False), capture_active=True) is None


def test_environment_state_scanning_without_a_file(tmp_path):
    cfg = tmp_path / "config.toml"
    cfg.write_text("[web]\nenabled = false\n")
    d = daemon_mod.Daemon(config_path=str(cfg), capture_store=str(tmp_path))
    env = d._environment_state(_env_cfg(), capture_active=True)
    assert env == {"scanning": True, "available": False,
                   "caught_session": 0, "handshakes_session": 0}


def test_environment_state_reports_the_session_yield_while_scanning(tmp_path):
    # The density reading is unavailable (no status file), but handshakes have
    # been caught this session. The yield must still be reported, or the
    # "Caught N handshakes" row blanks to 0 whenever the card is "Scanning..."
    # even though the daemon counted them (regression: the yield used to live
    # only in the "available" branch).
    cfg = tmp_path / "config.toml"
    cfg.write_text("[web]\nenabled = false\n")
    d = daemon_mod.Daemon(config_path=str(cfg), capture_store=str(tmp_path))
    d._session_bssids = {"AA:BB:CC:DD:EE:01", "AA:BB:CC:DD:EE:02"}
    d._session_handshakes = 3
    env = d._environment_state(_env_cfg(), capture_active=True)
    assert env["available"] is False and env["scanning"] is True
    assert env["caught_session"] == 2 and env["handshakes_session"] == 3


def test_environment_state_classifies_and_pairs_the_session_yield(tmp_path):
    cfg = tmp_path / "config.toml"
    cfg.write_text("[web]\nenabled = false\n")
    d = daemon_mod.Daemon(config_path=str(cfg), capture_store=str(tmp_path))
    import json as _json, time as _time
    now = int(_time.time())
    (tmp_path / "capture-status.json").write_text(_json.dumps(
        {"updated_at": now, "window_s": 300, "radios_recent": 23,
         "data_ts": now, "scanning": True}))
    d._session_bssids = {"AA:BB:CC:DD:EE:01", "AA:BB:CC:DD:EE:02"}
    d._session_handshakes = 5
    env = d._environment_state(_env_cfg(), capture_active=True)
    assert env["available"] is True
    assert env["radios_recent"] == 23
    assert env["label"] == "Moderate" and env["level"] == 2
    assert env["bar_pct"] == round(23 * 100 / 60)       # vs the top threshold (60)
    assert env["data_age_s"] is not None and env["data_age_s"] < 5
    assert env["caught_session"] == 2 and env["handshakes_session"] == 5


def test_environment_state_drops_back_to_unknown_when_data_is_stale(tmp_path):
    cfg = tmp_path / "config.toml"
    cfg.write_text("[web]\nenabled = false\n")
    d = daemon_mod.Daemon(config_path=str(cfg), capture_store=str(tmp_path))
    import json as _json, time as _time
    # Collector is alive (recent updated_at) but the newest capture DATA is far
    # older than max_age_s: show "unknown", never a stale count as if current.
    now = int(_time.time())
    (tmp_path / "capture-status.json").write_text(_json.dumps(
        {"updated_at": now, "window_s": 300, "radios_recent": 23,
         "data_ts": now - 5000, "scanning": True}))
    env = d._environment_state(_env_cfg(), capture_active=True)
    assert env == {"scanning": True, "available": False,
                   "caught_session": 0, "handshakes_session": 0}


def test_service_state_is_cached_within_ttl(tmp_path, monkeypatch):
    # `systemctl is-active` is shelled out per status() render; a short cache
    # collapses the repeat spawns (a slow systemd under load must not stretch
    # each render into seconds of piled-up subprocesses).
    cfg = tmp_path / "config.toml"
    cfg.write_text("[web]\nenabled = false\n")
    d = daemon_mod.Daemon(config_path=str(cfg), capture_store=str(tmp_path))

    calls = []

    class _R:
        returncode = 0

    monkeypatch.setattr(daemon_mod.subprocess, "run",
                        lambda argv, **k: (calls.append(argv), _R())[1])

    # Repeated reads of one service inside the TTL spawn systemctl once.
    assert d._service_state("x.service") == "running"
    assert d._service_state("x.service") == "running"
    assert len(calls) == 1
    # A different service is tracked separately.
    assert d._service_state("y.service") == "running"
    assert len(calls) == 2


def test_service_state_cache_can_be_turned_off(tmp_path, monkeypatch):
    cfg = tmp_path / "config.toml"
    cfg.write_text("[web]\nenabled = false\n")
    d = daemon_mod.Daemon(config_path=str(cfg), capture_store=str(tmp_path))

    calls = []

    class _R:
        returncode = 0

    monkeypatch.setattr(daemon_mod.subprocess, "run",
                        lambda argv, **k: (calls.append(argv), _R())[1])
    # A non-positive TTL never serves a hit: every read probes again.
    monkeypatch.setattr(daemon_mod, "_SERVICE_STATE_TTL", 0)
    d._service_state("x.service")
    d._service_state("x.service")
    assert len(calls) == 2


def _probe_daemon(tmp_path, monkeypatch, returncodes):
    """A daemon whose `systemctl is-active` returns each code in turn (the last
    one repeats), with sleeps skipped. Returns (daemon, calls)."""
    cfg = tmp_path / "config.toml"
    cfg.write_text("[web]\nenabled = false\n")
    d = daemon_mod.Daemon(config_path=str(cfg), capture_store=str(tmp_path))
    calls = []

    def fake_run(argv, **k):
        calls.append(argv)

        class _R:
            returncode = returncodes[min(len(calls), len(returncodes)) - 1]
        return _R()

    monkeypatch.setattr(daemon_mod.subprocess, "run", fake_run)
    monkeypatch.setattr(daemon_mod.time, "sleep", lambda s: None)
    return d, calls


def test_wait_capture_state_drops_the_stale_cached_state(tmp_path, monkeypatch):
    # The toggle's own status() read cached "stopped" just before the enable;
    # the wait must re-probe rather than return that cached value.
    d, calls = _probe_daemon(tmp_path, monkeypatch, [3, 0])
    assert d._capture_state() == "stopped"
    assert d.wait_capture_state(True) == "running"
    assert len(calls) == 2
    # The fresh state is what the re-render then reads from the cache.
    assert d._capture_state() == "running"
    assert len(calls) == 2


def test_wait_capture_state_polls_until_the_service_is_up(tmp_path, monkeypatch):
    # `capture enable` starts the service --no-block, so it may still be
    # coming up when the apply returns.
    d, calls = _probe_daemon(tmp_path, monkeypatch, [3, 3, 3, 0])
    assert d.wait_capture_state(True) == "running"
    assert len(calls) == 4


def test_wait_capture_state_waits_for_disable_too(tmp_path, monkeypatch):
    d, calls = _probe_daemon(tmp_path, monkeypatch, [0, 0, 3])
    assert d._capture_state() == "running"
    assert d.wait_capture_state(False) == "stopped"


def test_wait_capture_state_is_bounded(tmp_path, monkeypatch):
    # A service that never comes up (e.g. the launcher exited) must not hang
    # the request: past the timeout the last state read is returned.
    d, calls = _probe_daemon(tmp_path, monkeypatch, [3])
    clock = iter(range(0, 1000))
    monkeypatch.setattr(daemon_mod.time, "monotonic", lambda: next(clock))
    assert d.wait_capture_state(True, timeout=5) == "stopped"
    assert 1 < len(calls) < 10


def test_wait_capture_state_gives_up_at_once_without_systemctl(tmp_path, monkeypatch):
    cfg = tmp_path / "config.toml"
    cfg.write_text("[web]\nenabled = false\n")
    d = daemon_mod.Daemon(config_path=str(cfg), capture_store=str(tmp_path))
    calls = []

    def no_systemctl(argv, **k):
        calls.append(argv)
        raise FileNotFoundError("systemctl")

    monkeypatch.setattr(daemon_mod.subprocess, "run", no_systemctl)
    assert d.wait_capture_state(True) == "unknown"
    assert len(calls) == 1


def test_status_probes_each_service_at_most_once(tmp_path, monkeypatch):
    # The capture service used to be read twice per status() (card + environment);
    # it is now read once, so a render makes one probe per distinct service.
    cfg = tmp_path / "config.toml"
    cfg.write_text("[web]\nenabled = false\n"
                   "[capture]\nenabled = false\nwhitelist = []\n")
    d = daemon_mod.Daemon(config_path=str(cfg), capture_store=str(tmp_path))

    calls = []

    class _R:
        returncode = 1   # stopped

    monkeypatch.setattr(daemon_mod.subprocess, "run",
                        lambda argv, **k: (calls.append(argv), _R())[1])

    d.status()
    services = [argv[-1] for argv in calls]
    assert services                                   # it did probe
    assert len(services) == len(set(services))        # no service probed twice
    assert daemon_mod.CAPTURE_SERVICE in services


def test_concurrent_service_probes_share_one_systemctl(tmp_path, monkeypatch):
    # Several renders missing the cache at once must not each fork systemctl.
    import threading
    import time as _time

    cfg = tmp_path / "config.toml"
    cfg.write_text("[web]\nenabled = false\n")
    d = daemon_mod.Daemon(config_path=str(cfg), capture_store=str(tmp_path))
    calls = []

    def slow_run(argv, **k):
        calls.append(argv)
        _time.sleep(0.3)
        return types.SimpleNamespace(returncode=0)

    monkeypatch.setattr(daemon_mod.subprocess, "run", slow_run)
    results = []
    threads = [threading.Thread(target=lambda: results.append(
        d._service_state("x.service"))) for _ in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(5)
    assert results == ["running"] * 5
    assert len(calls) == 1


def test_fresh_config_reparses_only_when_the_file_changes(tmp_path, monkeypatch):
    import os

    cfg = tmp_path / "config.toml"
    cfg.write_text("[capture]\nenabled = false\n")
    d = daemon_mod.Daemon(config_path=str(cfg), capture_store=str(tmp_path))
    parses = []
    real = daemon_mod.load_config
    monkeypatch.setattr(daemon_mod, "load_config",
                        lambda path, log=None: parses.append(path) or real(path, log))
    first = d._fresh_config()
    first["capture"]["enabled"] = "mutated"        # a caller can't corrupt the cache
    assert d._fresh_config()["capture"]["enabled"] is False
    assert len(parses) == 1
    # An atomic replace (how config-set writes) is seen on the next read.
    tmp = tmp_path / "new.toml"
    tmp.write_text("[capture]\nenabled = true\n")
    os.replace(tmp, cfg)
    assert d._fresh_config()["capture"]["enabled"] is True
    assert len(parses) == 2


def test_main_ignores_sighup(tmp_path, monkeypatch):
    """A terminal hangup must never kill the always-on daemon: main() sets
    SIGHUP to SIG_IGN (the backstop for a plugin that opens a tty device)."""
    import signal

    cfg = tmp_path / "config.toml"
    cfg.write_text("[web]\nenabled = false\n")
    # Don't actually run the loop; we only care that the handlers are installed.
    monkeypatch.setattr(daemon_mod.Daemon, "run_forever", lambda self: None)
    prev = signal.getsignal(signal.SIGHUP)
    try:
        rc = daemon_mod.main(["--config", str(cfg),
                              "--capture-store", str(tmp_path)])
        assert rc in (None, 0)
        assert signal.getsignal(signal.SIGHUP) is signal.SIG_IGN
    finally:
        signal.signal(signal.SIGHUP, prev)


# A radio (BSSID) advertising two names, plus a second radio, so distinct
# radios (2) and the old ESSID+BSSID pair count (3) diverge.
_WPA = "WPA*02*0123456789abcdef0123456789abcdef*%s*112233445566*%s*0*0*00"
_R1_HOME = _WPA % ("aabbccddeeff", "486f6d65")       # aa:..:ff "Home"
_R1_GUEST = _WPA % ("aabbccddeeff", "4775657374")    # aa:..:ff "Guest"
_R2_CAFE = _WPA % ("111111111111", "43616665")       # 11:..:11 "Cafe"


def test_ui_state_counts_match_capture_counts(tmp_path):
    cfg = tmp_path / "config.toml"
    cfg.write_text("[web]\nenabled = false\n")
    (tmp_path / "store.hc22000").write_text(
        "\n".join([_R1_HOME, _R1_GUEST, _R2_CAFE]) + "\n")
    d = daemon_mod.Daemon(config_path=str(cfg), capture_store=str(tmp_path))
    state = d._ui_state()
    # Plain English: 3 handshakes from 2 access points across 3 networks.
    assert state.capture_total == 3
    assert state.aps_total == 2          # distinct radios, not ESSID+BSSID pairs
    assert state.networks_total == 3     # Home, Guest, Cafe
    # The face reads exactly what the dashboard navcard reads.
    assert d.capture.capture_counts() == {
        "handshakes": 3, "access_points": 2, "networks": 3}


def test_status_reports_cracked_aps_joined_to_captured(tmp_path, monkeypatch):
    # Two captured radios; a cracked potfile that knows one of them.
    cfg = tmp_path / "config.toml"
    cfg.write_text("[web]\nenabled = false\n"
                   "[capture]\nenabled = false\nwhitelist = []\n")
    (tmp_path / "store.hc22000").write_text("\n".join([_R1_HOME, _R2_CAFE]) + "\n")
    cracked_dir = tmp_path / "cracked"
    cracked_dir.mkdir()
    (cracked_dir / "wpa-sec.potfile").write_text(
        "aabbccddeeff:112233445566:Home:correct-horse\n")   # knows radio 1 only
    d = daemon_mod.Daemon(config_path=str(cfg), capture_store=str(tmp_path))
    monkeypatch.setattr(d, "_capture_state", lambda: "stopped")
    monkeypatch.setattr(d, "_bt_paired", lambda: False)

    cap = d.status()["capture"]
    assert cap["access_points"] == 2     # M: both captured radios
    assert cap["cracked_aps"] == 1       # N: one of them has a known passphrase


def test_status_omits_cracked_aps_when_the_store_read_fails(tmp_path, monkeypatch):
    # A cracked-store read failure must not break status(); the key is omitted
    # so the navcard simply shows no cracked figure.
    cfg = tmp_path / "config.toml"
    cfg.write_text("[web]\nenabled = false\n"
                   "[capture]\nenabled = false\nwhitelist = []\n")
    (tmp_path / "store.hc22000").write_text(_R1_HOME + "\n")
    d = daemon_mod.Daemon(config_path=str(cfg), capture_store=str(tmp_path))
    monkeypatch.setattr(d, "_capture_state", lambda: "stopped")
    monkeypatch.setattr(d, "_bt_paired", lambda: False)

    def _boom(_bssid):
        raise RuntimeError("potfile unreadable")

    monkeypatch.setattr(d.cracked, "lookup_bssid", _boom)
    cap = d.status()["capture"]
    assert "cracked_aps" not in cap
    assert cap["access_points"] == 1     # the rest of the capture card is intact
