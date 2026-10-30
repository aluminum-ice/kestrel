"""The eink plugin's four HAT keys: how the plugin wires the hat_keys plugin
library (KEY1 info screen, KEY2 capture, KEY3 reboot, KEY4 shut down), with fake
buttons (no gpiozero or GPIO) and a fake apply helper that records each change.
The library's own gestures and rules are tested in test_hat_keys_lib.py."""

import importlib.util
import logging
import os
import time
import types

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


E = _eink()
KEYS = E.keys            # the hat_keys library the plugin loads


class FakeButton:
    def __init__(self, pin):
        self.pin = pin
        self.closed = False
        self.when_pressed = self.when_released = None

    def close(self):
        self.closed = True


def _plugin(tmp_path, monkeypatch, options=None, applied=None, reply=None):
    made = []

    def fake_make(self, pin):
        button = FakeButton(pin)
        made.append(button)
        return button

    monkeypatch.setattr(KEYS.GpioSource, "_make_button", fake_make)
    monkeypatch.setattr(E.EinkFace, "_run", lambda self: None)       # no render loop
    applied = [] if applied is None else applied

    def apply(change):
        assert "ack" not in change                # a key never gives the acknowledgment
        applied.append(change)
        return reply if reply is not None else {"ok": True}

    p = E.EinkFace()
    p.options = dict(options or {})
    p.context = PluginContext(
        name="eink", options=dict(options or {}), capture=CaptureInterface(str(tmp_path)),
        connectivity=types.SimpleNamespace(online=False, paths={}), services=ServiceRegistry(),
        capabilities=("ui", "web"), apply=apply, logger=logging.getLogger("test.eink.keys"))
    p.on_loaded()
    return p, made, applied


def _hold(p, index):
    now = time.monotonic()
    p._keys.process("down", index, now)
    p._keys.tick(now + 3)
    p._keys.process("up", index, now + 3.1)


def _tap(p, index):
    now = time.monotonic()
    p._keys.process("down", index, now)
    p._keys.process("up", index, now + 0.1)


def test_the_keys_are_on_the_hat_pins(tmp_path, monkeypatch):
    p, made, _ = _plugin(tmp_path, monkeypatch)
    assert [b.pin for b in made] == [5, 6, 13, 19]
    assert [k.name for k in p._keys.keys] == ["KEY1", "KEY2", "KEY3", "KEY4"]
    assert [k.hold for k in p._keys.keys[1:]] == ["capture", "reboot", "shutdown"]
    p.on_unload()
    assert all(b.closed for b in made)


def test_keys_off_or_bad_pins(tmp_path, monkeypatch):
    off, made, _ = _plugin(tmp_path, monkeypatch, {"buttons": False})
    assert off._keys is None and off._buttons_state() == {} and made == []
    off.on_unload()
    bad, made, _ = _plugin(tmp_path, monkeypatch, {"pins": "5,6,13"})
    assert bad._keys is None and made == [] and "four" in bad._keys_note
    ui = StubUI().for_plugin("eink")
    bad.on_ui_setup(ui)
    assert ui.get("keys") == bad._keys_note            # the reason, on the card
    bad.on_unload()


def test_key2_stops_capture_and_the_prompt_clears_when_it_has(tmp_path, monkeypatch):
    p, _, applied = _plugin(tmp_path, monkeypatch)
    ui = StubUI().for_plugin("eink")
    p.on_ui_setup(ui)
    p.on_ui_update(ui, UIState(capture_active=True))
    _hold(p, 1)
    assert applied == [{"target": "capture", "enabled": False}]
    assert p._buttons_state()["prompt"] == "Stopping capture..."
    p.on_ui_update(ui, UIState(capture_active=False))
    assert p._buttons_state()["prompt"] is None
    assert ui.get("keys") == "KEY2 (long)"
    p.on_unload()


def test_key4_shuts_down_after_a_second_press(tmp_path, monkeypatch):
    p, _, applied = _plugin(tmp_path, monkeypatch)
    _tap(p, 3)
    assert applied == [] and p._buttons_state()["prompt"] == "Hold KEY4 to shut down"
    _hold(p, 3)
    assert applied == [] and p._buttons_state()["prompt"] == "Press KEY4 again to shut down"
    _tap(p, 3)
    assert applied == [{"target": "power", "action": "shutdown"}]
    assert p._buttons_state()["prompt"] == "Shutting down..."
    p.on_unload()


def test_key1_toggles_the_info_screen_and_clears_a_prompt(tmp_path, monkeypatch):
    p, _, applied = _plugin(tmp_path, monkeypatch)
    _hold(p, 2)                                         # "Press KEY3 again to reboot"
    seq = p._buttons_state()["seq"]
    _tap(p, 0)                                          # KEY1: cancels it, info screen up
    state = p._buttons_state()
    assert state["screen"] == "info" and state["prompt"] is None and state["seq"] != seq
    _tap(p, 2)                                          # no longer armed: just a hint
    assert applied == []
    _tap(p, 0)
    assert p._buttons_state()["screen"] is None
    p.on_unload()


def test_eink_declares_the_key_options_and_no_buttons_service():
    from kestrel_pi.declarations import read_declarations

    decl = read_declarations(os.path.join(PLUGIN_DIR, "eink.py"))["eink"]
    opts = decl["options"]
    assert opts["buttons"]["default"] is True and opts["pins"]["default"] == "5,6,13,19"
    assert opts["allow_capture"]["default"] is True and "allow_capture_stop" not in opts
    assert "network" not in (decl.get("capabilities") or ())
    assert "buttons" not in (decl.get("optional_requires") or ())
    assert not os.path.exists(os.path.join(PLUGIN_DIR, "hat-buttons.py"))


def test_eink_uses_the_shared_library_and_keeps_no_copy():
    src = open(os.path.join(PLUGIN_DIR, "eink.py"), encoding="utf-8").read()
    assert 'load_library(__file__, "hat_keys", api=0)' in src
    assert "class HatKeys" not in src and "def parse_pins" not in src
