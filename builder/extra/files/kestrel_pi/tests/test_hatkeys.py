"""The e-Paper HAT's four keys (HatKeys, in the eink plugin), with fake buttons
(no gpiozero or GPIO needed) and a fake apply helper that records each change."""

import importlib.util
import logging
import os
import time
import types

import pytest

from kestrel_pi.capture import CaptureInterface
from kestrel_pi.context import PluginContext, ServiceRegistry
from kestrel_pi.ui import StubUI, UIState

PLUGIN_DIR = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", "..", "plugins"))


def _eink():
    spec = importlib.util.spec_from_file_location(
        "eink_keys_under_test", os.path.join(PLUGIN_DIR, "eink.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# The keys live in the plugin, so the key tests and the plugin tests share one
# load of it (a monkeypatch on HK.HatKeys must reach the plugin's own class).
HK = _eink()


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


def _keys(monkeypatch, options=None, make=None, reply=None, capturing=None):
    made, applied = [], []

    def fake_make(self, pin, hold):
        if make:
            return make(pin)
        button = FakeButton(pin)
        made.append(button)
        return button

    monkeypatch.setattr(HK.HatKeys, "_make_button", fake_make)
    opts = dict({"pins": "5,6,13,19", "hold_seconds": 3, "allow_power": True,
                 "allow_capture": True}, **(options or {}))

    def fake_apply(change):
        applied.append(change)
        return reply if reply is not None else {"ok": True}

    k = HK.HatKeys(lambda: opts, fake_apply, logging.getLogger("test.hatkeys"))
    note = k.start()
    if capturing is not None:
        k.set_capturing(capturing)
    return k, made, applied, note


def test_keys_are_made_on_the_hat_pins(monkeypatch):
    k, made, _, note = _keys(monkeypatch)
    assert [b.pin for b in made] == [5, 6, 13, 19] and note == ""
    k.stop()
    assert all(b.closed for b in made)


def test_no_hat_or_bad_pins_degrade_with_a_note(monkeypatch):
    def broken(pin):
        raise RuntimeError("no GPIO")

    k, _, _, note = _keys(monkeypatch, make=broken)
    assert "keys unavailable" in note and k.last == note
    j, made, _, note = _keys(monkeypatch, {"pins": "5,6,13"})
    assert "four" in note and made == []
    k.stop()
    j.stop()


def test_key1_toggles_the_info_screen(monkeypatch):
    k, _, _, _ = _keys(monkeypatch)
    seq = k.state()["seq"]
    k.handle("short", 1)
    assert k.state()["screen"] == "info" and k.state()["seq"] != seq
    k.handle("short", 1)
    assert k.state()["screen"] is None
    k.stop()


def test_shutdown_needs_a_long_press_then_a_second_press(monkeypatch):
    k, _, applied, _ = _keys(monkeypatch)
    k.handle("short", 4)
    assert applied == [] and "Hold KEY4" in k.state()["prompt"]
    now = time.monotonic()
    k.handle("long", 4, now=now)
    assert applied == [] and k.state()["prompt"] == "Press KEY4 again to shut down"
    k.handle("short", 4, now=now + 2)
    assert applied == [{"target": "power", "action": "shutdown"}]
    assert k.state()["prompt"] == "Shutting down..."
    k.stop()


def test_the_confirm_window_expires_and_is_per_key(monkeypatch):
    k, _, applied, _ = _keys(monkeypatch)
    now = time.monotonic()
    k.handle("long", 3, now=now)
    k.handle("short", 4, now=now + 1)          # another key doesn't confirm
    k.handle("short", 3, now=now + HK.CONFIRM_SECONDS + 1)   # too late
    assert applied == []
    k.handle("long", 3, now=now + 10)
    k.handle("short", 3, now=now + 11)
    assert applied == [{"target": "power", "action": "reboot"}]
    k.stop()


def test_key2_stops_capture_at_once(monkeypatch):
    k, _, applied, _ = _keys(monkeypatch, capturing=True)
    k.handle("short", 2)
    assert applied == [] and k.state()["prompt"] == "Hold KEY2 to stop capture"
    k.handle("long", 2)
    assert applied == [{"target": "capture", "enabled": False}]
    assert k.state()["prompt"] == "Stopping capture..."
    k.stop()


def test_key2_starts_capture_only_after_a_second_press_and_never_sends_the_ack(monkeypatch):
    k, _, applied, _ = _keys(monkeypatch, capturing=False)
    k.handle("short", 2)
    assert k.state()["prompt"] == "Hold KEY2 to start capture"
    now = time.monotonic()
    k.handle("long", 2, now=now)
    assert applied == [] and k.state()["prompt"] == "Press KEY2 again to start capture"
    k.handle("short", 2, now=now + 1)
    assert applied == [{"target": "capture", "enabled": True}]   # no "ack" field
    assert "ack" not in applied[0]
    assert k.state()["prompt"] == "Starting capture..."
    k.stop()


def test_starting_without_a_prior_acknowledgment_points_to_the_dashboard(monkeypatch):
    k, _, applied, _ = _keys(monkeypatch, capturing=False,
                             reply={"ok": False, "need_ack": True, "error": "confirm"})
    now = time.monotonic()
    k.handle("long", 2, now=now)
    k.handle("short", 2, now=now + 1)
    assert applied == [{"target": "capture", "enabled": True}]
    assert k.state()["prompt"] == "Accept authorized use on the dashboard first"
    k.stop()


def test_the_capture_prompt_clears_when_capture_gets_there(monkeypatch):
    k, _, _, _ = _keys(monkeypatch, capturing=False)
    now = time.monotonic()
    k.handle("long", 2, now=now)
    k.handle("short", 2, now=now + 1)
    assert k.state()["prompt"] == "Starting capture..."
    k.set_capturing(False)                     # not there yet: still showing
    assert k.state()["prompt"] == "Starting capture..."
    seq = k.state()["seq"]
    k.set_capturing(True)                      # capture is running: gone at once
    assert k.state()["prompt"] is None and k.state()["seq"] != seq
    k.handle("long", 2)
    assert k.state()["prompt"] == "Stopping capture..."
    k.set_capturing(False)
    assert k.state()["prompt"] is None
    k.stop()


def test_the_capture_prompt_has_a_time_limit(monkeypatch):
    k, _, _, _ = _keys(monkeypatch, capturing=True)
    monkeypatch.setattr(HK, "PENDING_SECONDS", 0.05)
    k.handle("long", 2)
    time.sleep(0.1)                            # capture never got there
    assert k.state()["prompt"] is None
    k.stop()


def test_unknown_capture_state_does_nothing(monkeypatch):
    k, _, applied, _ = _keys(monkeypatch)       # no UI update yet
    k.handle("long", 2)
    assert applied == [] and "not known" in k.state()["prompt"]
    k.stop()


def test_options_turn_actions_off(monkeypatch):
    k, _, applied, _ = _keys(monkeypatch, {"allow_power": False, "allow_capture": False},
                             capturing=True)
    now = time.monotonic()
    k.handle("long", 4, now=now)
    k.handle("short", 4, now=now + 1)
    k.handle("long", 2, now=now + 2)
    assert applied == [] and k.state()["prompt"] == "Capture by button is off"
    k.stop()


def test_a_refused_action_is_reported_on_the_panel(monkeypatch):
    k, _, _, _ = _keys(monkeypatch, reply={"ok": False, "error": "busy"})
    now = time.monotonic()
    k.handle("long", 4, now=now)
    k.handle("short", 4, now=now + 1)
    assert k.state()["prompt"] == "Couldn't do that: busy"
    k.stop()


def test_real_callbacks_reach_the_worker(monkeypatch):
    k, made, applied, _ = _keys(monkeypatch)
    made[0].tap()                               # KEY1 short
    made[3].hold()                              # KEY4 long
    made[3].tap()                               # KEY4 confirm
    deadline = time.monotonic() + 5
    while not applied and time.monotonic() < deadline:
        time.sleep(0.02)
    assert applied == [{"target": "power", "action": "shutdown"}]
    assert k.last == "KEY4 (short)"
    k.stop()
    assert not k._worker.is_alive()


# -- inside the eink plugin -------------------------------------------------------

def test_the_eink_plugin_owns_the_keys(tmp_path, monkeypatch):
    E = HK
    monkeypatch.setattr(HK.HatKeys, "_make_button", lambda self, pin, hold: FakeButton(pin))
    monkeypatch.setattr(E.EinkFace, "_run", lambda self: None)       # no render loop
    applied = []
    p = E.EinkFace()
    p.options = {}
    p.context = PluginContext(
        name="eink", options={}, capture=CaptureInterface(str(tmp_path)),
        connectivity=types.SimpleNamespace(online=False, paths={}), services=ServiceRegistry(),
        capabilities=("ui", "web"), apply=lambda c: applied.append(c) or {"ok": True},
        logger=logging.getLogger("test.eink.keys"))
    p.on_loaded()
    ui = StubUI().for_plugin("eink")
    p.on_ui_setup(ui)
    p.on_ui_update(ui, UIState(capture_active=True))
    p._keys.handle("long", 2)
    assert applied == [{"target": "capture", "enabled": False}]
    assert p._buttons_state()["prompt"] == "Stopping capture..."
    p.on_ui_update(ui, UIState(capture_active=False))
    assert p._buttons_state()["prompt"] is None
    assert ui.get("keys") == "KEY2 (long)"
    p.on_unload()
    q = E.EinkFace()
    q.options = {}
    q.context = PluginContext(
        name="eink", options={"buttons": False}, capture=CaptureInterface(str(tmp_path)),
        connectivity=types.SimpleNamespace(online=False, paths={}), services=ServiceRegistry(),
        capabilities=("ui", "web"), logger=logging.getLogger("test.eink.keys"))
    q.on_loaded()
    assert q._keys is None and q._buttons_state() == {}
    q.on_unload()


def test_eink_declares_the_key_options_and_no_buttons_service():
    from kestrel_pi.declarations import read_declarations

    decl = read_declarations(os.path.join(PLUGIN_DIR, "eink.py"))["eink"]
    opts = decl["options"]
    assert opts["buttons"]["default"] is True and opts["pins"]["default"] == "5,6,13,19"
    assert opts["allow_capture"]["default"] is True and "allow_capture_stop" not in opts
    assert "network" not in (decl.get("capabilities") or ())
    assert "buttons" not in (decl.get("optional_requires") or ())
    assert not os.path.exists(os.path.join(PLUGIN_DIR, "hat-buttons.py"))
