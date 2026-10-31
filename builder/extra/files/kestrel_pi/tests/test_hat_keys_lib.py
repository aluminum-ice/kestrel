"""The hat_keys plugin library (``plugins/_lib_hat_keys.py``): gestures, the
built-in capture and power actions and their rules, driven with a fake input
source and a fake clock (no gpiozero or GPIO). Every apply call goes through a
fake that fails on an ``ack``: a key never gives the authorized-use
acknowledgment. eink-ws27's wiring is tested in test_eink_keys.py."""

import logging
import os
import time

import pytest

from kestrel_pi import load_library

PLUGIN_DIR = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", "..", "plugins"))
LIB = load_library(os.path.join(PLUGIN_DIR, "eink-ws27.py"), "hat_keys", api=0)
LOG = logging.getLogger("test.hat_keys")


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


class FakeSource:
    def __init__(self, fail=None):
        self.fail = fail
        self.closed = False
        self.on_down = self.on_up = None

    def start(self, on_down, on_up):
        if self.fail:
            raise RuntimeError(self.fail)
        self.on_down, self.on_up = on_down, on_up

    def close(self):
        self.closed = True


class Hat:
    """Four keys like the 2.7" HAT's: KEY1 a function on press, KEY2 capture,
    KEY3 reboot, KEY4 shut down (all on hold)."""

    def __init__(self, options=None, reply=None, capturing=None, keys=None, source=None):
        self.clock = Clock()
        self.applied = []
        self.pressed = []
        self.reply = reply
        opts = dict({"hold_seconds": 3, "allow_power": True, "allow_capture": True},
                    **(options or {}))
        self.keys = LIB.Keys("test", keys or [
            LIB.Key("KEY1", 5, press=lambda: self.pressed.append("KEY1")),
            LIB.Key("KEY2", 6, hold="capture"),
            LIB.Key("KEY3", 13, hold="reboot"),
            LIB.Key("KEY4", 19, hold="shutdown")],
            apply=self.apply, logger=LOG, options=lambda: opts,
            source=source or FakeSource(), clock=self.clock)
        if capturing is not None:
            self.keys.set_capturing(capturing)

    def apply(self, change):
        assert "ack" not in change, change          # a key never gives the acknowledgment
        self.applied.append(change)
        return self.reply if self.reply is not None else {"ok": True}

    def tap(self, index, gap=0.1):
        self.keys.process("down", index, self.clock.t)
        self.clock.t += gap
        self.keys.process("up", index, self.clock.t)

    def hold(self, index, seconds=3.0):
        self.keys.process("down", index, self.clock.t)
        self.clock.t += seconds
        self.keys.tick(self.clock.t)
        self.clock.t += 0.1
        self.keys.process("up", index, self.clock.t)

    def wait(self, seconds):
        self.clock.t += seconds
        self.keys.tick(self.clock.t)

    @property
    def prompt(self):
        return self.keys.state()["prompt"]


# -- the built-in actions (prompts as the 2.7" HAT has always shown them) -------

def test_a_short_press_on_a_power_key_is_a_hint():
    h = Hat()
    h.tap(3)
    assert h.applied == [] and h.prompt == "Hold KEY4 to shut down"
    h.tap(2)
    assert h.prompt == "Hold KEY3 to reboot"
    assert h.keys.last == "KEY3 (short)"


def test_shutdown_needs_a_hold_then_a_second_press():
    h = Hat()
    h.hold(3)
    assert h.applied == [] and h.prompt == "Press KEY4 again to shut down"
    assert h.keys.last == "KEY4 (long)"
    h.tap(3)
    assert h.applied == [{"target": "power", "action": "shutdown"}]
    assert h.prompt == "Shutting down..."
    assert h.keys.last == "KEY4 (short)"


def test_reboot_the_same_way():
    h = Hat()
    h.hold(2)
    h.tap(2)
    assert h.applied == [{"target": "power", "action": "reboot"}]
    assert h.prompt == "Rebooting..."
    h.wait(LIB.POWER_SECONDS + 1)
    assert h.prompt is None


def test_the_confirm_window_expires():
    h = Hat()
    h.hold(2)
    h.wait(LIB.CONFIRM_SECONDS + 1)
    assert h.prompt is None
    h.tap(2)                                    # too late: just the hint
    assert h.applied == [] and h.prompt == "Hold KEY3 to reboot"


def test_another_key_cancels_a_pending_confirm():
    h = Hat()
    h.hold(2)
    assert h.prompt == "Press KEY3 again to reboot"
    h.keys.process("down", 3, h.clock.t)        # KEY4 goes down: KEY3's confirm is gone
    assert h.prompt is None
    h.keys.process("up", 3, h.clock.t + 0.1)
    h.clock.t += 0.2
    h.tap(2)                                    # KEY3 again, inside the old window
    assert h.applied == [] and h.prompt == "Hold KEY3 to reboot"


def test_a_function_key_also_cancels_a_pending_confirm():
    h = Hat()
    h.hold(3)
    h.tap(0)
    assert h.pressed == ["KEY1"] and h.prompt is None
    h.tap(3)
    assert h.applied == []


def test_holding_the_armed_key_again_re_arms_it():
    h = Hat()
    h.hold(3)
    h.clock.t += 4
    h.hold(3)                                   # a fresh 5 s window
    h.clock.t += 3
    h.tap(3)
    assert h.applied == [{"target": "power", "action": "shutdown"}]


def test_key2_stops_capture_at_once():
    h = Hat(capturing=True)
    h.tap(1)
    assert h.applied == [] and h.prompt == "Hold KEY2 to stop capture"
    h.hold(1)
    assert h.applied == [{"target": "capture", "enabled": False}]
    assert h.prompt == "Stopping capture..."


def test_key2_starts_capture_only_after_a_second_press_and_never_sends_the_ack():
    h = Hat(capturing=False)
    h.tap(1)
    assert h.prompt == "Hold KEY2 to start capture"
    h.hold(1)
    assert h.applied == [] and h.prompt == "Press KEY2 again to start capture"
    h.tap(1)
    assert h.applied == [{"target": "capture", "enabled": True}]
    assert "ack" not in h.applied[0]
    assert h.prompt == "Starting capture..."


def test_starting_without_a_prior_acknowledgment_points_to_the_dashboard():
    h = Hat(capturing=False, reply={"ok": False, "need_ack": True, "error": "confirm"})
    h.hold(1)
    h.tap(1)
    assert h.applied == [{"target": "capture", "enabled": True}]
    assert h.prompt == "Accept authorized use on the dashboard first"


def test_the_capture_prompt_clears_when_capture_gets_there():
    h = Hat(capturing=False)
    h.hold(1)
    h.tap(1)
    assert h.prompt == "Starting capture..."
    h.keys.set_capturing(False)                 # not there yet: still showing
    assert h.prompt == "Starting capture..."
    seq = h.keys.state()["seq"]
    h.keys.set_capturing(True)                  # running: gone at once
    assert h.prompt is None and h.keys.state()["seq"] != seq
    h.hold(1)
    assert h.prompt == "Stopping capture..."
    h.keys.set_capturing(False)
    assert h.prompt is None


def test_the_capture_prompt_has_a_time_limit():
    h = Hat(capturing=True)
    h.hold(1)
    h.wait(LIB.PENDING_SECONDS - 1)
    assert h.prompt == "Stopping capture..."
    h.wait(2)                                   # capture never got there
    assert h.prompt is None


def test_unknown_capture_state_does_nothing():
    h = Hat()                                   # no UI update yet
    h.hold(1)
    assert h.applied == [] and "not known" in h.prompt


def test_options_turn_the_built_ins_off():
    h = Hat({"allow_power": False, "allow_capture": False}, capturing=True)
    h.hold(3)
    assert h.prompt == "Reboot and shut down by button are off"
    h.tap(3)
    h.hold(1)
    assert h.applied == [] and h.prompt == "Capture by button is off"


def test_a_refused_action_is_reported():
    h = Hat(reply={"ok": False, "error": "busy"})
    h.hold(3)
    h.tap(3)
    assert h.prompt == "Couldn't do that: busy"


# -- gestures --------------------------------------------------------------------

def test_hold_fires_while_still_down_and_follows_hold_seconds():
    h = Hat({"hold_seconds": 5}, capturing=True)
    h.keys.process("down", 1, h.clock.t)
    h.clock.t += 3
    h.keys.tick(h.clock.t)
    assert h.applied == []                      # 3 s is not a hold at 5 s
    h.clock.t += 2
    h.keys.tick(h.clock.t)
    assert h.applied == [{"target": "capture", "enabled": False}]   # before release
    h.keys.process("up", 1, h.clock.t + 1)
    assert len(h.applied) == 1                  # the release is not a press too


def test_a_press_on_a_key_without_double_answers_at_once():
    h = Hat()
    h.tap(0)
    assert h.pressed == ["KEY1"]


def test_double_press_only_where_bound():
    calls = []
    h = Hat(keys=[LIB.Key("A", 5, press=lambda: calls.append("press"),
                          double=lambda: calls.append("double")),
                  LIB.Key("B", 6, press=lambda: calls.append("b"))])
    h.tap(0)
    assert calls == []                          # waiting to see if a second press follows
    h.wait(LIB.DOUBLE_SECONDS + 0.1)
    assert calls == ["press"]
    h.tap(0)
    h.clock.t += 0.1
    h.tap(0)
    assert calls == ["press", "double"]
    h.wait(1)
    assert calls == ["press", "double"]         # no stray single press after a double
    assert h.keys.last == "A (double)"
    h.tap(1)
    assert calls[-1] == "b"                     # B has no double: instant


def test_a_function_can_arm_and_confirm():
    done = []
    h = Hat(keys=[LIB.Key("KEY1", 5, hold=lambda: done.append(1) or "Cleared",
                          confirm="Press KEY1 again to clear")])
    h.hold(0)
    assert done == [] and h.prompt == "Press KEY1 again to clear"
    h.tap(0)
    assert done == [1] and h.prompt == "Cleared"


def test_a_function_key_without_confirm_runs_on_hold():
    done = []
    h = Hat(keys=[LIB.Key("KEY1", 5, hold=lambda: done.append(1))])
    h.hold(0)
    assert done == [1]


@pytest.mark.parametrize("key", [
    LIB.Key("K", 5, press="shutdown"),          # a built-in on a press
    LIB.Key("K", 5, double="capture"),          # ...or a double press
    LIB.Key("K", 5, hold="format-disk"),        # not a built-in
    LIB.Key("K", 5, hold="reboot", confirm="Press K again"),   # built-ins confirm already
])
def test_unsafe_or_unknown_bindings_are_refused(key):
    with pytest.raises(ValueError):
        LIB.Keys("test", [key], apply=lambda c: {"ok": True}, logger=LOG)


def test_names_and_pins_must_differ():
    with pytest.raises(ValueError):
        LIB.Keys("test", [LIB.Key("A", 5), LIB.Key("A", 6)], apply=None, logger=LOG)
    with pytest.raises(ValueError):
        LIB.Keys("test", [LIB.Key("A", 5), LIB.Key("B", 5)], apply=None, logger=LOG)


# -- the worker and the input source ---------------------------------------------

def test_an_unavailable_source_is_reported_and_the_worker_still_stops():
    source = FakeSource(fail="no GPIO")
    h = Hat(source=source)
    note = h.keys.start()
    assert note == "keys unavailable: no GPIO" and h.keys.last == note
    h.keys.stop()
    assert source.closed and not h.keys._worker.is_alive()


def test_the_worker_turns_input_into_gestures():
    source = FakeSource()
    h = Hat(source=source)
    assert h.keys.start() == ""
    source.on_down(3)
    h.clock.t += 3                              # the worker ticks within a second
    deadline = time.monotonic() + 5
    while h.prompt != "Press KEY4 again to shut down" and time.monotonic() < deadline:
        time.sleep(0.02)
    assert h.prompt == "Press KEY4 again to shut down"
    source.on_up(3)
    source.on_down(3)
    source.on_up(3)
    deadline = time.monotonic() + 5
    while not h.applied and time.monotonic() < deadline:
        time.sleep(0.02)
    assert h.applied == [{"target": "power", "action": "shutdown"}]
    h.keys.stop()
    assert source.closed and not h.keys._worker.is_alive()


def test_a_failing_action_never_stops_the_worker():
    source = FakeSource()
    done = []
    h = Hat(source=source, keys=[LIB.Key("A", 5, press=lambda: 1 / 0),
                                 LIB.Key("B", 6, press=lambda: done.append(1))])
    h.keys.start()
    source.on_down(0)
    source.on_up(0)
    source.on_down(1)
    source.on_up(1)
    deadline = time.monotonic() + 5
    while not done and time.monotonic() < deadline:
        time.sleep(0.02)
    assert done == [1]
    h.keys.stop()


def test_gpio_source_wires_each_pin_to_its_index(monkeypatch):
    made = []

    class Button:
        def __init__(self, pin):
            self.pin, self.closed = pin, False
            self.when_pressed = self.when_released = None

        def close(self):
            self.closed = True

    monkeypatch.setattr(LIB.GpioSource, "_make_button",
                        lambda self, pin: made.append(Button(pin)) or made[-1])
    events = []
    source = LIB.GpioSource([5, 6])
    source.start(lambda i: events.append(("down", i)), lambda i: events.append(("up", i)))
    made[1].when_pressed()
    made[1].when_released()
    assert [b.pin for b in made] == [5, 6] and events == [("down", 1), ("up", 1)]
    source.close()
    assert all(b.closed for b in made)


def test_gpio_source_closes_what_it_made_when_a_pin_fails(monkeypatch):
    made = []

    class Button:
        def __init__(self, pin):
            if pin == 6:
                raise RuntimeError("pin busy")
            self.closed = False
            made.append(self)

        def close(self):
            self.closed = True

    monkeypatch.setattr(LIB.GpioSource, "_make_button", lambda self, pin: Button(pin))
    with pytest.raises(RuntimeError):
        LIB.GpioSource([5, 6]).start(lambda i: None, lambda i: None)
    assert made and all(b.closed for b in made)


def test_parse_pins():
    assert LIB.parse_pins("5,6,13,19", 4, "1,2,3,4") == [5, 6, 13, 19]
    assert LIB.parse_pins("", 2, "5,6") == [5, 6]
    for raw, words in (("5,6,13", "four"), ("5,5,6,7", "four"), ("5,6,13,40", "four"),
                       ("a,b", "two")):
        with pytest.raises(ValueError, match=words):
            LIB.parse_pins(raw, 4 if words == "four" else 2, "1,2")


def test_the_library_is_provisional_and_loads_without_gpio():
    assert LIB.LIB_API[0] == 0
    src = open(os.path.join(PLUGIN_DIR, "_lib_hat_keys.py"), encoding="utf-8").read()
    head = src.split("\nLIB_API", 1)[0].split('"""', 2)[2]
    assert "gpiozero" not in head


def test_the_documented_example_plugin_works(tmp_path, monkeypatch):
    # docs/developer/plugin-libraries.md shows a minimal two-key plugin on
    # hat_keys; it must load through the real loader and act on its keys.
    import re

    from kestrel_pi import loader as loader_mod
    from kestrel_pi import plugins as plugin_api
    from kestrel_pi.capture import CaptureInterface
    from kestrel_pi.connectivity import ConnectivityState
    from kestrel_pi.context import ServiceRegistry
    from kestrel_pi.loader import Loader
    from kestrel_pi.ui import StubUI, UIState

    doc = os.path.join(PLUGIN_DIR, "..", "..", "..", "..", "docs", "developer",
                       "plugin-libraries.md")
    text = open(doc, encoding="utf-8").read().split("## `hat_keys`", 1)[1]
    code = re.search(r"### A minimal plugin\n.*?```python\n(.*?)```", text, re.S).group(1)
    plugins = tmp_path / "plugins"
    plugins.mkdir()
    (plugins / "twokeys.py").write_text(code)
    monkeypatch.setattr(plugin_api, "SHIPPED_PLUGINS_DIR", PLUGIN_DIR)   # the shipped library
    made = []

    class Button:
        def __init__(self, pin):
            self.pin, self.when_pressed, self.when_released = pin, None, None
            made.append(self)

        def close(self):
            pass

    monkeypatch.setattr(LIB.GpioSource, "_make_button", lambda self, pin: Button(pin))
    applied = []
    # The plugin binds context.apply when it loads; the loader builds that from
    # apply_change (the root helper's client), so stand in for it there.
    monkeypatch.setattr(loader_mod, "apply_change",
                        lambda change, *a, **k: applied.append(change) or {"ok": True})
    loader = Loader(config={"plugins": {"twokeys": {"enabled": True}}},
                    capture=CaptureInterface(str(tmp_path)), connectivity=ConnectivityState(),
                    services=ServiceRegistry(), search_paths=[str(plugins)],
                    logger=LOG)
    loader.load_all()
    record = loader.records["twokeys"]
    assert record.state == "loaded", getattr(record, "reason", "")
    plugin = record.instance
    assert [b.pin for b in made] == [5, 6]
    hat = plugin._keys
    now = time.monotonic()
    hat.process("down", 0, now)
    hat.process("up", 0, now + 0.1)
    assert hat.state()["prompt"] == "Pressed 1 times"
    hat.process("down", 1, now + 1)
    hat.tick(now + 4.1)
    hat.process("up", 1, now + 4.2)
    hat.process("down", 1, now + 5)
    hat.process("up", 1, now + 5.1)
    assert applied == [{"target": "power", "action": "shutdown"}]
    ui = StubUI().for_plugin("twokeys")
    plugin.on_ui_setup(ui)
    plugin.on_ui_update(ui, UIState(capture_active=False))
    assert ui.get("prompt") == "Shutting down..."
    plugin.on_unload()
