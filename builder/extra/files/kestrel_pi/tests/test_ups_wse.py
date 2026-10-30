"""ups-wse (UPS HAT (E)): four 21700 cells in series and a controller at 0x2D that
reports the battery itself. Not tested on hardware; these follow Waveshare's
demo with a fake controller: finding the board (and not mistaking a (D) for
it), its readings, the low-cell rule and the optional power-on-when-charged
write before an auto-shutdown."""

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
PLUGIN = os.path.join(PLUGIN_DIR, "ups-wse.py")


def _mod():
    spec = importlib.util.spec_from_file_location("ups_wse_under_test", PLUGIN)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


E = _mod()


def _words(*values):
    out = []
    for v in values:
        v &= 0xFFFF
        out += [v & 0xFF, v >> 8]
    return out


class FakeE:
    """The (E)'s controller at 0x2D on a fake bus; ``ina219`` puts a (D)'s INA219
    at 0x43 too."""

    def __init__(self, state=0x20, mv=15200, ma=-300, pct=80, cells=(3800, 3800, 3800, 3800),
                 ina219=False, present=True):
        self.state, self.mv, self.ma, self.pct = state, mv, ma, pct
        self.cells = list(cells)
        self.ina219 = ina219
        self.present = present
        self.writes = []
        self.closed = False
        self.log = []

    def read_i2c_block_data(self, addr, reg, count):
        if addr != 0x2D or not self.present:
            raise OSError("no ack")
        data = {0x02: [self.state], 0x20: _words(self.mv, self.ma, self.pct, 4000, 120, 0),
                0x30: _words(*self.cells)}[reg]
        return data[:count]

    def read_byte(self, addr):
        if not (self.ina219 and addr == 0x43):
            raise OSError("no ack")
        return 0

    def write_byte_data(self, addr, reg, value):
        self.writes.append((addr, reg, value))
        self.log.append("write")

    def close(self):
        self.closed = True


def _plugin(monkeypatch, board, applied=None, **options):
    monkeypatch.setattr(E, "_open_bus", lambda number, smbus_module=None: board)
    p = E.UpsWse()
    p.options = dict({"i2c_bus": 1, "address": 0x2D, "shutdown_percent": 5,
                      "poll_seconds": 20, "power_on_when_charged": False}, **options)

    def apply(change):
        board.log.append("apply")
        applied.append(change)
        return {"ok": True}

    p.context = PluginContext(
        name="ups-wse", options=dict(p.options), capture=CaptureInterface("/nonexistent"),
        connectivity=types.SimpleNamespace(online=False), services=ServiceRegistry(),
        capabilities=("ui",), apply=apply if applied is not None else None)
    p._setup()
    return p


def _card(p):
    ui = StubUI()
    view = ui.for_plugin("ups-wse")
    p.on_ui_setup(view)
    p.on_ui_update(view, UIState())
    return {e["name"]: e for e in ui.widgets()["ups-wse"]}


def test_declarations():
    decl = read_declarations(PLUGIN)["ups-wse"]
    assert decl["capabilities"] == ("ui",) and decl["provides"] == ("battery",)
    opts = decl["options"]
    assert opts["address"]["default"] == 0x2D and opts["shutdown_percent"]["default"] == 5
    assert opts["power_on_when_charged"]["default"] is False         # off unless asked
    assert "Not yet tested on hardware" in decl["description"]


def test_finds_the_board_and_reads_its_own_percent(monkeypatch):
    p = _plugin(monkeypatch, FakeE(pct=80, mv=15200, ma=-300))
    assert p.monitor.ensure_device() is True and p.context.health_note == ""
    r = p.monitor.read_battery()
    assert r["percent"] == 80.0 and r["status"] == "discharging"
    assert r["voltage"] == pytest.approx(15.2) and r["current_ma"] == -300.0
    assert r["power_w"] == pytest.approx(4.56) and "low" not in r
    p.monitor.evaluate(r)
    card = _card(p)
    assert card["percent"]["value"] == "80%" and card["status"]["value"] == "Discharging"
    assert card["cells"]["value"] == "3.80 / 3.80 / 3.80 / 3.80 V"


@pytest.mark.parametrize("state, status", [(0x80, "charging"), (0x40, "charging"),
                                           (0xC0, "charging"), (0x20, "discharging"),
                                           (0x00, "idle")])
def test_charge_state_bits(monkeypatch, state, status):
    p = _plugin(monkeypatch, FakeE(state=state, pct=60))
    p.monitor.ensure_device()
    assert p.monitor.read_battery()["status"] == status


def test_idle_and_full_is_full(monkeypatch):
    p = _plugin(monkeypatch, FakeE(state=0x00, pct=100, ma=0))
    p.monitor.ensure_device()
    assert p.monitor.read_battery()["status"] == "full"


def test_refuses_a_ups_hat_d(monkeypatch):
    board = FakeE(ina219=True)
    p = _plugin(monkeypatch, board)
    assert p.monitor.ensure_device() is False and board.closed
    assert _card(p)["status"]["value"] == "this looks like a UPS HAT (D): use ups-wsd"


@pytest.mark.parametrize("fake", [FakeE(mv=7400), FakeE(pct=180),
                                  FakeE(cells=(3800, 3800, 0, 3800))])
def test_refuses_an_implausible_device(monkeypatch, fake):
    p = _plugin(monkeypatch, fake)
    assert p.monitor.ensure_device() is False and fake.closed
    assert "not as a UPS HAT (E)" in p.context.health_note


def test_no_board_no_reading(monkeypatch):
    p = _plugin(monkeypatch, FakeE(present=False))
    assert p.monitor.ensure_device() is False and p.provide_battery() is None
    assert "not detected on I2C bus 1 at 0x2d" in p.context.health_note
    q = _plugin(monkeypatch, None)
    assert q.monitor.ensure_device() is False and "smbus" in q.context.health_note


def test_a_low_cell_on_battery_shuts_down_without_the_power_on_write(monkeypatch, caplog):
    applied = []
    board = FakeE(pct=60, cells=(3800, 3790, 3100, 3810))     # 60%: above the limit
    p = _plugin(monkeypatch, board, applied)
    p.monitor.ensure_device()
    with caplog.at_level(logging.WARNING):
        for _ in range(3):
            p.monitor.evaluate(p.monitor.read_battery())
    assert applied == [{"target": "power", "action": "shutdown"}]
    assert board.writes == []                                  # the option is off
    assert "cell 3 at 3.10 V, under 3.15 V while on battery" in caplog.text


def test_a_low_cell_while_charging_or_with_the_auto_shutdown_off_does_nothing(monkeypatch):
    for kwargs, opts in (({"state": 0x80}, {}), ({}, {"shutdown_percent": 0})):
        applied = []
        p = _plugin(monkeypatch, FakeE(pct=60, cells=(3800, 3100, 3800, 3800), **kwargs),
                    applied, **opts)
        p.monitor.ensure_device()
        for _ in range(4):
            p.monitor.evaluate(p.monitor.read_battery())
        assert applied == []


def test_power_on_when_charged_is_written_just_before_the_shutdown(monkeypatch):
    applied = []
    board = FakeE(pct=3)
    p = _plugin(monkeypatch, board, applied, power_on_when_charged=True)
    p.monitor.ensure_device()
    for _ in range(3):
        p.monitor.evaluate(p.monitor.read_battery())
    assert board.writes == [(0x2D, 0x01, 0x55)]
    assert board.log == ["write", "apply"]


def test_current_is_signed(monkeypatch):
    p = _plugin(monkeypatch, FakeE(ma=-200))
    p.monitor.ensure_device()
    assert p.monitor.read_battery()["current_ma"] == -200.0


def test_loads_and_waits_without_a_board(tmp_path):
    loader = Loader(config={"plugins": {"ups-wse": {"enabled": True}}},
                    capture=CaptureInterface(str(tmp_path)), connectivity=ConnectivityState(),
                    services=ServiceRegistry(), search_paths=[PLUGIN_DIR],
                    logger=logging.getLogger("test.ups_wse"))
    try:
        assert "ups-wse" in {r.name for r in loader.load_all()}
    finally:
        loader.unload_all(timeout=5)
