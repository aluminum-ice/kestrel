"""The capture gate for the learning agent: it only *drives* while capture is
on, and now notices capture going off (or coming back) at runtime - not just at
start-up - so it stands down to idle instead of looping as a zombie that still
publishes "running" while driving a stopped capture. Pure-stdlib: the env and
policies are stubbed, so no torch/gymnasium is needed."""

import json
import sys
import threading
import time
import types

from kestrel_pi.agent import EPISODES_DIR, STATUS_NAME
from kestrel_pi.agent import controller as ctrl_mod
from kestrel_pi.agent.controller import OVERRIDE_NAME


def _make(tmp_path, capture=True):
    cfg = tmp_path / "config.toml"
    cfg.write_text('[agent]\nenabled = true\npolicy = "baseline"\n'
                   "decision_seconds = 1\n"
                   "[capture]\nenabled = %s\n" % ("true" if capture else "false"))
    agent_dir = tmp_path / "agent"
    (agent_dir / EPISODES_DIR).mkdir(parents=True)
    (tmp_path / "cap").mkdir()
    c = ctrl_mod.AgentController(
        config_path=str(cfg), agent_dir=str(agent_dir),
        capture_store=str(tmp_path / "cap"))
    return c, cfg, agent_dir


def _set_capture(cfg_path, on):
    cfg_path.write_text('[agent]\nenabled = true\npolicy = "baseline"\n'
                        "decision_seconds = 1\n"
                        "[capture]\nenabled = %s\n" % ("true" if on else "false"))


def test_capture_enabled_fresh_rereads_the_file(tmp_path):
    """capture_enabled() reads the start-up config (cached); the capture toggle
    rewrites config.toml with no agent restart, so the loop must re-read. The
    fresh check sees the change where the cached one does not."""
    c, cfg, _ = _make(tmp_path, capture=True)
    assert c.capture_enabled() is True
    assert c._capture_enabled_fresh() is True
    _set_capture(cfg, False)
    # The cached value is stale; the fresh re-read reflects the change.
    assert c.capture_enabled() is True
    assert c._capture_enabled_fresh() is False
    _set_capture(cfg, True)
    assert c._capture_enabled_fresh() is True


def test_capture_enabled_fresh_falls_back_on_a_bad_read(tmp_path, monkeypatch):
    """A transient bad config read must not trip a stand-down: the fresh check
    falls back to the start-up value rather than reading capture as off."""
    c, _cfg, _ = _make(tmp_path, capture=True)

    def _boom(*_a, **_k):
        raise OSError("transient")

    monkeypatch.setattr(ctrl_mod, "load_config", _boom)
    assert c._capture_enabled_fresh() is True   # falls back to cached True


def test_await_capture_returns_false_when_stopped(tmp_path):
    """With capture off, _await_capture blocks; once asked to stop it returns
    False promptly (it must not spin), and leaves an idle status behind."""
    c, _cfg, agent_dir = _make(tmp_path, capture=False)
    c._stop.set()   # pre-set: the wait returns at once
    assert c._await_capture() is False
    status = json.loads((agent_dir / STATUS_NAME).read_text())
    assert status["running"] is False
    assert status["detail"] == "capture disabled; agent idle"


def test_read_gps_fix_honors_freshness_and_the_kill_switch(tmp_path, monkeypatch):
    """Phase 4.8: the controller reads the gps plugin's latest-fix seam, but
    only a fresh fix with use_gps on; a stale, missing or unparseable file, or
    use_gps off, all read as 'no GPS' (None), never an error."""
    c, _cfg, _ = _make(tmp_path, capture=True)
    seam = tmp_path / "position.json"
    monkeypatch.setattr(ctrl_mod, "POSITION_FILE", str(seam))

    assert c._read_gps_fix() is None                      # no file yet

    def _write(age):
        seam.write_text(json.dumps({"ts": time.time() - age, "lat": 40.0,
                                     "lon": -73.0, "fix": 3, "sats": 9}))

    _write(0.0)
    assert c._read_gps_fix()["lat"] == 40.0               # fresh fix read through
    _write(c._gps_stale_seconds + 5)
    assert c._read_gps_fix() is None                      # stale -> no GPS
    _write(0.0)
    c._use_gps = False
    assert c._read_gps_fix() is None                      # kill-switch off
    c._use_gps = True
    seam.write_text("{ not json")
    assert c._read_gps_fix() is None                      # unparseable -> no GPS


def test_clear_override_removes_a_stale_plan(tmp_path):
    """Standing down drops the scan override so a later capture relaunch falls
    back to the configured sweep instead of a frozen agent plan."""
    c, _cfg, agent_dir = _make(tmp_path, capture=True)
    override = agent_dir / OVERRIDE_NAME
    override.write_text("{}")
    c._clear_override()
    assert not override.exists()
    c._clear_override()   # absent is fine (best-effort)


class _FakePolicy:
    def predict(self, _obs):
        return 0, None

    def reset(self):
        pass


class _FakeEnv:
    """A capture env that does no real capture: step() returns at once and, on
    its first call, turns capture off in config.toml to drive the runtime
    stand-down path."""

    def __init__(self, cfg_path, **_kw):
        self._cfg_path = cfg_path
        self.supported = [1, 6, 11]
        self.decision_seconds = 1
        self.steps = 0

    def reset(self, **_kw):
        return [0.0], {}

    def step(self, _action):
        self.steps += 1
        if self.steps == 1:
            _set_capture(self._cfg_path, False)
        return [0.0], 0.0, False, False, {
            "posture": "sweep-band", "dwell": 5, "transmit": "active"}


def test_run_stands_down_to_idle_when_capture_is_turned_off_at_runtime(
        tmp_path, monkeypatch):
    """The decide loop re-checks capture each window: when capture is turned off
    while the agent runs, it drops its override, publishes an idle status and
    stops driving - instead of the old behaviour of looping forever while still
    reporting "running"."""
    c, cfg, agent_dir = _make(tmp_path, capture=True)
    (agent_dir / OVERRIDE_NAME).write_text("{}")
    # run() does `from .env import CaptureEnv` lazily; the real env module imports
    # gymnasium at top, so inject a stub module to keep this test ML-free.
    fake_env_mod = types.ModuleType("kestrel_pi.agent.env")
    fake_env_mod.CaptureEnv = lambda **kw: _FakeEnv(cfg, **kw)
    monkeypatch.setitem(sys.modules, "kestrel_pi.agent.env", fake_env_mod)
    monkeypatch.setattr(ctrl_mod.policies_mod, "load_policy",
                        lambda *a, **k: (_FakePolicy(), "baseline", ""))
    monkeypatch.setattr(ctrl_mod.policies_mod, "BaselinePolicy",
                        lambda **k: _FakePolicy())
    monkeypatch.setattr(c, "_train_loop", lambda: None)

    # Stop the moment the idle status is written, so the stand-down's wait for
    # capture returns at once (no real sleep) and run() exits cleanly.
    real_write = c._write_status

    def _write(running, policy, detail, *a, **k):
        real_write(running, policy, detail, *a, **k)
        if detail == "capture disabled; agent idle":
            c._stop.set()

    monkeypatch.setattr(c, "_write_status", _write)

    assert c.run() == 0
    status = json.loads((agent_dir / STATUS_NAME).read_text())
    # Last write is the clean "stopped"; the idle stand-down ran on the way.
    assert status["running"] is False
    # The stale override was dropped so capture falls back to the sweep.
    assert not (agent_dir / OVERRIDE_NAME).exists()


def test_run_idles_at_startup_when_capture_is_off_then_stops(
        tmp_path, monkeypatch):
    """With capture off at start-up the agent idles (publishing an idle status)
    rather than exiting, so enabling capture brings it back on its own; a stop
    still ends it with exit 0."""
    c, _cfg, agent_dir = _make(tmp_path, capture=False)
    monkeypatch.setattr(c, "_train_loop", lambda: None)

    done = []

    def _run():
        done.append(c.run())

    t = threading.Thread(target=_run)
    t.start()
    # Give it a moment to publish the idle status, then ask it to stop.
    deadline = time.time() + 5
    while time.time() < deadline:
        if (agent_dir / STATUS_NAME).exists():
            break
        time.sleep(0.05)
    c.stop()
    t.join(timeout=5)
    assert done == [0]
    status = json.loads((agent_dir / STATUS_NAME).read_text())
    assert status["running"] is False
    assert status["detail"] == "capture disabled; agent idle"
