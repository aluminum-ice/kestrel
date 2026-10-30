"""ups-wsc (UPS HAT (C)) and ups-wsd (UPS HAT (D)): the two one-cell Waveshare
boards, on the ups and ina219 libraries (tested in test_ups_lib.py). Neither
has been tested on hardware; these follow Waveshare's demos and check what tells
the boards apart: the (D)'s controller at 0x2D, and the opposite current sign."""

import importlib.util
import logging
import os
import types

import pytest

from kestrel_pi.capture import CaptureInterface
from kestrel_pi.connectivity import ConnectivityState
from kestrel_pi.context import PluginContext, ServiceRegistry
from kestrel_pi.declarations import read_declarations
from kestrel_pi.loader import Loader
from kestrel_pi.ui import StubUI, UIState

PLUGIN_DIR = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", "..", "plugins"))


def _mod(name):
    spec = importlib.util.spec_from_file_location(name.replace("-", "_") + "_under_test",
                                                  os.path.join(PLUGIN_DIR, name + ".py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


C = _mod("ups-wsc")
D = _mod("ups-wsd")
INA = C.ina219
REAL_OPEN_CHIP = INA.open_chip


class FakeBus:
    """An I2C bus with an INA219 (a register dict) and, if ``controller``, a
    device that acks at 0x2D like the (D)'s controller."""

    def __init__(self, regs=None, controller=False):
        self.regs = dict(regs or {})
        self.controller = controller
        self.closed = False

    def read_i2c_block_data(self, addr, reg, count):
        value = self.regs.get(reg, 0) & 0xFFFF
        return [(value >> 8) & 0xFF, value & 0xFF]

    def write_i2c_block_data(self, addr, reg, data):
        self.regs[reg] = ((data[0] << 8) | data[1]) & 0xFFFF

    def read_byte(self, addr):
        if not (self.controller and addr == 0x2D):
            raise OSError("no ack")
        return 0

    def close(self):
        self.closed = True


def _volts(v):
    return int(round(v / 0.004)) << 3


def _plugin(mod, cls, name, monkeypatch, bus, applied=None):
    monkeypatch.setattr(INA, "open_chip", lambda b, a, c: REAL_OPEN_CHIP(
        b, a, c, smbus_module=types.SimpleNamespace(SMBus=lambda n: bus)))
    p = getattr(mod, cls)()
    p.options = {"i2c_bus": 1, "address": 0x43, "shutdown_percent": 5, "poll_seconds": 20}
    p.context = PluginContext(
        name=name, options=dict(p.options), capture=CaptureInterface("/nonexistent"),
        connectivity=types.SimpleNamespace(online=False), services=ServiceRegistry(),
        capabilities=("ui",),
        apply=(lambda change: applied.append(change) or {"ok": True}) if applied is not None
        else None)
    p._setup()
    return p


def _card(p, name):
    ui = StubUI()
    view = ui.for_plugin(name)
    p.on_ui_setup(view)
    p.on_ui_update(view, UIState())
    return {e["name"]: e for e in ui.widgets()[name]}


BOARDS = [(C, "UpsWsc", "ups-wsc", False), (D, "UpsWsd", "ups-wsd", True)]


@pytest.mark.parametrize("mod, cls, name, controller", BOARDS)
def test_declarations(mod, cls, name, controller):
    decl = read_declarations(os.path.join(PLUGIN_DIR, name + ".py"))[name]
    assert decl["capabilities"] == ("ui",) and decl["provides"] == ("battery",)
    assert decl["options"]["address"]["default"] == 0x43
    assert decl["options"]["shutdown_percent"]["default"] == 5
    assert "Not yet tested on hardware" in decl["description"]
    assert mod.PACK.cells == 1 and mod.CALIBRATION is INA.CAL_16V_5A


@pytest.mark.parametrize("mod, cls, name, controller", BOARDS)
def test_each_finds_its_own_board(mod, cls, name, controller, monkeypatch):
    bus = FakeBus(regs={0x02: _volts(3.9)}, controller=controller)
    p = _plugin(mod, cls, name, monkeypatch, bus)
    assert p.monitor.ensure_device() is True
    assert p.context.health_note == ""


def test_ups_wsc_refuses_a_ups_hat_d(monkeypatch):
    bus = FakeBus(regs={0x02: _volts(3.9)}, controller=True)
    p = _plugin(C, "UpsWsc", "ups-wsc", monkeypatch, bus)
    assert p.monitor.ensure_device() is False and bus.closed
    assert _card(p, "ups-wsc")["status"]["value"] == "this looks like a UPS HAT (D): use ups-wsd"
    assert "enable ups-wsd instead" in p.context.health_note


def test_ups_wsd_refuses_a_ups_hat_c(monkeypatch):
    bus = FakeBus(regs={0x02: _volts(3.9)}, controller=False)
    p = _plugin(D, "UpsWsd", "ups-wsd", monkeypatch, bus)
    assert p.monitor.ensure_device() is False and bus.closed
    assert _card(p, "ups-wsd")["status"]["value"] == "this looks like a UPS HAT (C): use ups-wsc"


@pytest.mark.parametrize("mod, cls, name, controller", BOARDS)
def test_no_board_no_reading(mod, cls, name, controller, monkeypatch):
    bus = FakeBus(regs={0x02: 0}, controller=controller)            # nothing plausible
    p = _plugin(mod, cls, name, monkeypatch, bus)
    assert p.monitor.ensure_device() is False
    assert p.provide_battery() is None
    assert "not detected on I2C bus 1 at 0x43" in p.context.health_note


def test_the_d_reads_its_current_the_other_way_round(monkeypatch):
    # The same raw register: +200 mA on the (C) is charging; on the (D), whose
    # shunt is wired the other way, it is the cell discharging.
    raw = {0x02: _volts(3.7), 0x04: 1312, 0x03: 300}
    c = _plugin(C, "UpsWsc", "ups-wsc", monkeypatch, FakeBus(regs=raw, controller=False))
    assert c.monitor.ensure_device()            # probe now: the next plugin re-patches the bus
    d = _plugin(D, "UpsWsd", "ups-wsd", monkeypatch, FakeBus(regs=raw, controller=True))
    assert d.monitor.ensure_device()
    rc, rd = c.monitor.read_battery(), d.monitor.read_battery()
    assert rc["status"] == "charging" and rc["current_ma"] > 0
    assert rd["status"] == "discharging" and rd["current_ma"] < 0
    assert round(rc["percent"]) == round(rd["percent"]) == 58          # (3.7 - 3) / 1.2


def test_a_low_d_on_battery_shuts_down_and_a_charging_c_does_not(monkeypatch):
    raw = {0x02: _volts(3.04), 0x04: 1312, 0x03: 300}                 # ~3% left
    applied_c, applied_d = [], []
    for mod, cls, name, controller, applied in (
            (C, "UpsWsc", "ups-wsc", False, applied_c), (D, "UpsWsd", "ups-wsd", True, applied_d)):
        p = _plugin(mod, cls, name, monkeypatch, FakeBus(regs=raw, controller=controller), applied)
        assert p.monitor.ensure_device()
        for _ in range(3):
            p.monitor.evaluate(p.monitor.read_battery())
    assert applied_c == []                                             # charging: never
    assert applied_d == [{"target": "power", "action": "shutdown"}]


@pytest.mark.parametrize("name", ["ups-wsc", "ups-wsd"])
def test_loads_and_waits_for_its_board_without_one(tmp_path, name):
    loader = Loader(config={"plugins": {name: {"enabled": True}}},
                    capture=CaptureInterface(str(tmp_path)), connectivity=ConnectivityState(),
                    services=ServiceRegistry(), search_paths=[PLUGIN_DIR],
                    logger=logging.getLogger("test.ups_wsc_wsd"))
    try:
        assert name in {r.name for r in loader.load_all()}
    finally:
        loader.unload_all(timeout=5)
