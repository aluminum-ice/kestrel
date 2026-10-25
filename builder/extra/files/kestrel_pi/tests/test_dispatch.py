import threading
import time

from kestrel_pi.dispatch import Dispatcher
from kestrel_pi.loader import PluginRecord
from kestrel_pi.plugins import Plugin


def _record(instance, name):
    rec = PluginRecord(name, "test")
    rec.instance = instance
    rec.state = "loaded"
    return rec


def test_isolation_one_raiser_does_not_stop_others():
    class Raiser(Plugin):
        def on_handshake(self, event):
            raise RuntimeError("boom")

    hits = []

    class Good(Plugin):
        def on_handshake(self, event):
            hits.append(event)

    bad = _record(Raiser(), "bad")
    good = _record(Good(), "good")
    disp = Dispatcher(max_workers=2, max_hook_seconds=5)
    disp.dispatch(bad, "on_handshake", "e")
    disp.dispatch(good, "on_handshake", "e")
    assert disp.wait_idle(5)
    disp.shutdown()

    assert hits == ["e"]                       # good ran despite the raiser
    assert "boom" in bad.last_error            # failure recorded, not raised


def test_non_blocking_peer_runs_while_one_blocks():
    gate = threading.Event()
    b_done = threading.Event()

    class Blocker(Plugin):
        def on_handshake(self, event):
            gate.wait(5)

    class Quick(Plugin):
        def on_handshake(self, event):
            b_done.set()

    disp = Dispatcher(max_workers=4, max_hook_seconds=5)
    disp.dispatch(_record(Blocker(), "a"), "on_handshake", "e")
    disp.dispatch(_record(Quick(), "b"), "on_handshake", "e")
    # Quick must finish even though Blocker is still parked.
    assert b_done.wait(3), "peer hook was blocked by a slow plugin"
    gate.set()
    assert disp.wait_idle(5)
    disp.shutdown()


def test_per_plugin_hooks_do_not_overlap():
    overlap = {"concurrent": 0, "max": 0}
    lock = threading.Lock()

    class Serial(Plugin):
        def on_epoch(self, epoch, info):
            with lock:
                overlap["concurrent"] += 1
                overlap["max"] = max(overlap["max"], overlap["concurrent"])
            time.sleep(0.05)
            with lock:
                overlap["concurrent"] -= 1

    rec = _record(Serial(), "serial")
    disp = Dispatcher(max_workers=4, max_hook_seconds=5)
    for i in range(5):
        disp.dispatch(rec, "on_epoch", i, {})
    assert disp.wait_idle(5)
    disp.shutdown()
    assert overlap["max"] == 1   # the same plugin never re-enters itself


def test_overrun_is_recorded():
    class Slow(Plugin):
        def on_handshake(self, event):
            time.sleep(0.4)

    rec = _record(Slow(), "slow")
    disp = Dispatcher(max_workers=2, max_hook_seconds=0.1)
    disp.dispatch(rec, "on_handshake", "e")
    assert disp.wait_idle(5)
    disp.shutdown()
    assert rec.overruns >= 1


def test_overrun_that_finishes_is_not_quarantined():
    # A hook that crosses the soft deadline but then returns is slow, not
    # stuck: it is recorded as an overrun, but the plugin stays in rotation.
    class Slow(Plugin):
        def on_handshake(self, event):
            time.sleep(0.3)

    rec = _record(Slow(), "slow")
    disp = Dispatcher(max_workers=2, max_hook_seconds=0.05, stuck_after_seconds=5)
    disp.dispatch(rec, "on_handshake", "e")
    assert disp.wait_idle(5)
    disp.shutdown()
    assert rec.overruns >= 1
    assert not rec.failed and rec.state == "loaded"


def test_stuck_hook_is_quarantined():
    # A hook still running at the stuck ceiling has stopped returning; the
    # plugin is taken out of rotation so it can't keep draining the pool.
    gate = threading.Event()

    class Stuck(Plugin):
        def on_handshake(self, event):
            gate.wait(5)

    rec = _record(Stuck(), "stuck")
    disp = Dispatcher(max_workers=2, max_hook_seconds=0.05,
                      stuck_after_seconds=0.2)
    disp.dispatch(rec, "on_handshake", "e")
    # The stuck ceiling fires while the hook is still parked.
    deadline = time.monotonic() + 5
    while not rec.failed and time.monotonic() < deadline:
        time.sleep(0.02)
    assert rec.failed and rec.state == "failed"
    assert "stuck" in rec.reason
    gate.set()                      # let the parked hook finish
    assert disp.wait_idle(5)
    disp.shutdown()


def test_quarantined_plugin_is_not_dispatched_again():
    hits = []
    gate = threading.Event()

    class Stuck(Plugin):
        def on_handshake(self, event):
            hits.append(event)
            gate.wait(5)

    rec = _record(Stuck(), "stuck")
    disp = Dispatcher(max_workers=2, max_hook_seconds=0.05,
                      stuck_after_seconds=0.2)
    disp.dispatch(rec, "on_handshake", "first")
    deadline = time.monotonic() + 5
    while not rec.failed and time.monotonic() < deadline:
        time.sleep(0.02)
    assert rec.failed
    # After quarantine a further dispatch is a no-op.
    disp.dispatch(rec, "on_handshake", "second")
    gate.set()
    assert disp.wait_idle(5)
    disp.shutdown()
    assert hits == ["first"]        # "second" never ran


def test_quarantine_can_be_disabled():
    # stuck_after_seconds=0 disables quarantine: a stuck hook holds its worker,
    # but the plugin is never taken out of rotation.
    gate = threading.Event()

    class Stuck(Plugin):
        def on_handshake(self, event):
            gate.wait(5)

    rec = _record(Stuck(), "stuck")
    disp = Dispatcher(max_workers=2, max_hook_seconds=0.05,
                      stuck_after_seconds=0)
    disp.dispatch(rec, "on_handshake", "e")
    time.sleep(0.3)                 # well past the soft deadline
    assert rec.overruns >= 1
    assert not rec.failed and rec.state == "loaded"
    gate.set()
    assert disp.wait_idle(5)
    disp.shutdown()


def test_base_noop_hooks_are_skipped():
    # A plugin that overrides nothing should enqueue no work.
    rec = _record(Plugin(), "noop")
    disp = Dispatcher(max_workers=2, max_hook_seconds=5)
    disp.dispatch(rec, "on_handshake", "e")
    assert disp.wait_idle(1)
    disp.shutdown()
    assert rec.last_hook == ""   # nothing ran
