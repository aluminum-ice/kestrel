"""hat-buttons plugin: the e-Paper HAT's four keys, with fake buttons (no
gpiozero or GPIO needed) and a fake apply helper that records each change."""

import importlib.util
import os
import time
import types

from kestrel_pi.capture import CaptureInterface
from kestrel_pi.context import PluginContext, ServiceRegistry
from kestrel_pi.ui import StubUI, UIState

PLUGIN_DIR = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", "..", "plugins"))


def _mod():
    spec = importlib.util.spec_from_file_location(
        "hat_buttons_under_test", os.path.join(PLUGIN_DIR, "hat-buttons.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


HB = _mod()


class FakeButton:
    def __init__(self, pin):
        self.pin = pin
        self.closed = False
        self.when_pressed = self.when_held = self.when_released = None

    def tap(self):
        self.when_pressed()
        self.when_released()

    def hold(self):
        self.when_pressed()
        self.when_held()
        self.when_released()

    def close(self):
        self.closed = True


def _plugin(tmp_path, monkeypatch, options=None, make=None, reply=None):
    made = []

    def fake_make(self, pin):
        if make:
            return make(pin)
        button = FakeButton(pin)
        made.append(button)
        return button

    monkeypatch.setattr(HB.HatButtons, "_make_button", fake_make)
    p = HB.HatButtons()
    p.options = dict({"pins": "5,6,13,19", "hold_seconds": 3, "allow_power": True,
                      "allow_capture_stop": True}, **(options or {}))
    applied = []

    def fake_apply(change):
        applied.append(change)
        return reply if reply is not None else {"ok": True}

    p.context = PluginContext(
        name="hat-buttons", options=p.options, capture=CaptureInterface(str(tmp_path)),
        connectivity=types.SimpleNamespace(online=False), services=ServiceRegistry(),
        capabilities=("ui",), apply=fake_apply)
    p.on_loaded()
    return p, made, applied


def test_buttons_are_made_on_the_hat_pins(tmp_path, monkeypatch):
    p, made, _ = _plugin(tmp_path, monkeypatch)
    assert [b.pin for b in made] == [5, 6, 13, 19]
    assert p.context.health_note == ""
    p.on_unload()
    assert all(b.closed for b in made)


def test_no_hat_or_bad_pins_degrade_with_a_note(tmp_path, monkeypatch):
    def broken(pin):
        raise RuntimeError("no GPIO")

    p, _, _ = _plugin(tmp_path, monkeypatch, make=broken)
    assert "buttons unavailable" in p.context.health_note
    q, made, _ = _plugin(tmp_path, monkeypatch, {"pins": "5,6,13"})
    assert "four" in q.context.health_note and made == []
    p.on_unload()
    q.on_unload()


def test_key1_toggles_the_info_screen(tmp_path, monkeypatch):
    p, _, _ = _plugin(tmp_path, monkeypatch)
    seq = p.provide_buttons()["seq"]
    p.handle("short", 1)
    state = p.provide_buttons()
    assert state["screen"] == "info" and state["seq"] != seq
    p.handle("short", 1)
    assert p.provide_buttons()["screen"] is None
    p.on_unload()


def test_shutdown_needs_a_long_press_then_a_second_press(tmp_path, monkeypatch):
    p, _, applied = _plugin(tmp_path, monkeypatch)
    p.handle("short", 4)
    assert applied == [] and "Hold KEY4" in p.provide_buttons()["prompt"]
    now = time.monotonic()
    p.handle("long", 4, now=now)
    assert applied == [] and p.provide_buttons()["prompt"] == "Press KEY4 again to shut down"
    p.handle("short", 4, now=now + 2)
    assert applied == [{"target": "power", "action": "shutdown"}]
    assert p.provide_buttons()["prompt"] == "Shutting down..."
    p.on_unload()


def test_the_confirm_window_expires_and_is_per_key(tmp_path, monkeypatch):
    p, _, applied = _plugin(tmp_path, monkeypatch)
    now = time.monotonic()
    p.handle("long", 3, now=now)
    p.handle("short", 4, now=now + 1)          # another key doesn't confirm
    p.handle("short", 3, now=now + HB.CONFIRM_SECONDS + 1)   # too late
    assert applied == []
    p.handle("long", 3, now=now + 10)
    p.handle("short", 3, now=now + 11)
    assert applied == [{"target": "power", "action": "reboot"}]
    p.on_unload()


def test_capture_can_only_be_stopped(tmp_path, monkeypatch):
    p, _, applied = _plugin(tmp_path, monkeypatch)
    ui = StubUI().for_plugin("hat-buttons")
    p.on_ui_setup(ui)
    p.on_ui_update(ui, UIState(capture_active=True))
    p.handle("long", 2)
    assert applied == [{"target": "capture", "enabled": False}]
    p.on_ui_update(ui, UIState(capture_active=False))
    p.handle("long", 2)
    assert len(applied) == 1 and p.provide_buttons()["prompt"] == "Capture is already off"
    p.handle("short", 2)
    assert "dashboard" in p.provide_buttons()["prompt"]
    assert not any(c.get("enabled") is True for c in applied)
    p.on_unload()


def test_options_turn_actions_off(tmp_path, monkeypatch):
    p, _, applied = _plugin(tmp_path, monkeypatch, {"allow_power": False,
                                                    "allow_capture_stop": False})
    now = time.monotonic()
    p.handle("long", 4, now=now)
    p.handle("short", 4, now=now + 1)
    p.handle("long", 2, now=now + 2)
    assert applied == []
    assert "off" in p.provide_buttons()["prompt"]
    p.on_unload()


def test_a_refused_action_is_reported_on_the_panel(tmp_path, monkeypatch):
    p, _, _ = _plugin(tmp_path, monkeypatch, reply={"ok": False, "error": "busy"})
    now = time.monotonic()
    p.handle("long", 4, now=now)
    p.handle("short", 4, now=now + 1)
    assert p.provide_buttons()["prompt"] == "Couldn't do that: busy"
    p.on_unload()


def test_real_callbacks_reach_the_worker(tmp_path, monkeypatch):
    p, made, applied = _plugin(tmp_path, monkeypatch)
    made[0].tap()                               # KEY1 short
    made[3].hold()                              # KEY4 long
    made[3].tap()                               # KEY4 confirm
    deadline = time.monotonic() + 5
    while not applied and time.monotonic() < deadline:
        time.sleep(0.02)
    assert applied == [{"target": "power", "action": "shutdown"}]
    ui = StubUI().for_plugin("hat-buttons")
    p.on_ui_setup(ui)
    p.on_ui_update(ui, UIState())
    assert ui.get("last") == "KEY4 (short)"
    p.on_unload()
    assert not p._worker.is_alive()


def test_declarations_are_local_only():
    from kestrel_pi.declarations import read_declarations

    decl = read_declarations(os.path.join(PLUGIN_DIR, "hat-buttons.py"))["hat-buttons"]
    caps = decl.get("capabilities") or decl.get("__capabilities__")
    assert "network" not in caps
    assert "buttons" in (decl.get("provides") or decl.get("__provides__"))
