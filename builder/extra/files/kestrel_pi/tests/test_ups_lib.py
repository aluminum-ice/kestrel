"""The ups and ina219 plugin libraries (``plugins/_lib_ups.py``,
``plugins/_lib_ina219.py``): a UPS HAT's battery readout, card, time-to-shutdown
estimate and auto-shutdown rules, and the INA219 driver, tested once here with
no hardware (a fake I2C bus and a fake plugin). The ups-hat plugin's wiring is
tested in test_reference_plugins.py."""

import logging
import os
import types

import pytest

from kestrel_pi import load_library
from kestrel_pi.ui import StubUI, UIState

PLUGIN_DIR = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", "..", "plugins"))
UPS = load_library(os.path.join(PLUGIN_DIR, "ups-hat.py"), "ups", api=0)
INA = load_library(os.path.join(PLUGIN_DIR, "ups-hat.py"), "ina219", api=1)


class FakeBus:
    def __init__(self, regs=None, roundtrip=True):
        self.regs = dict(regs or {})
        self.roundtrip = roundtrip
        self.closed = False
        self.writes = []

    def read_i2c_block_data(self, addr, reg, count):
        value = self.regs.get(reg, 0) & 0xFFFF
        return [(value >> 8) & 0xFF, value & 0xFF]

    def write_i2c_block_data(self, addr, reg, data):
        value = ((data[0] << 8) | data[1]) & 0xFFFF
        self.writes.append((reg, value))
        if reg == INA.REG_CALIBRATION and not self.roundtrip:
            value ^= 0xFFFF
        self.regs[reg] = value

    def close(self):
        self.closed = True


def _plugin(applied=None, **options):
    opts = dict({"shutdown_percent": 5, "poll_seconds": 20}, **options)
    replies = []

    def apply(change):
        (applied if applied is not None else replies).append(change)
        return {"ok": True}

    ctx = types.SimpleNamespace(logger=logging.getLogger("test.ups"), health_note="x",
                                apply=apply)
    return types.SimpleNamespace(options=opts, context=ctx)


def _reading(percent=50.0, status="discharging", voltage=7.2, current=-250.0, power=1.8):
    return {"percent": percent, "status": status, "voltage": voltage,
            "current_ma": current, "power_w": power}


def _monitor(plugin=None, probe=None, read=None, pack=None):
    return UPS.Monitor(plugin or _plugin(), "test-ups", probe or (lambda: (None, ("none", "no board"))),
                       read or (lambda device: (7.2, -250.0, 1.8)),
                       pack or UPS.Pack(cells=2), board="Test HAT")


def _card(monitor, name="test-ups"):
    ui = StubUI()
    view = ui.for_plugin(name)
    monitor.ui_setup(view)
    monitor.ui_update(view, UIState())
    return {e["name"]: e for e in ui.widgets()[name]}


# -- ina219 ---------------------------------------------------------------------

def test_the_16v_5a_calibration_is_what_ups_hat_always_programmed():
    cal = INA.CAL_16V_5A
    assert (cal.cal_value, cal.current_lsb_ma, cal.power_lsb_w) == (26868, 0.1524, 0.003048)
    assert cal.config == (0x00 << 13) | (0x01 << 11) | (0x0D << 7) | (0x0D << 3) | 0x07


def test_the_32v_2a_calibration_matches_waveshares_ups_hat_demo():
    cal = INA.CAL_32V_2A
    assert (cal.cal_value, cal.current_lsb_ma, cal.power_lsb_w) == (4096, 0.1, 0.002)
    assert cal.config == (0x01 << 13) | (0x03 << 11) | (0x0D << 7) | (0x0D << 3) | 0x07


def test_presence_needs_a_register_roundtrip_and_a_plausible_voltage():
    good = INA.INA219(FakeBus(regs={INA.REG_BUSVOLTAGE: 1850 << 3}), 0x42, INA.CAL_16V_5A)
    assert good.present() is True                         # ~7.4 V
    impostor = INA.INA219(FakeBus(roundtrip=False), 0x42, INA.CAL_16V_5A)
    assert impostor.present() is False
    flat = INA.INA219(FakeBus(regs={INA.REG_BUSVOLTAGE: 0}), 0x42, INA.CAL_16V_5A)
    assert flat.present() is False
    one_cell = INA.INA219(FakeBus(regs={INA.REG_BUSVOLTAGE: 925 << 3}), 0x43, INA.CAL_16V_5A)
    assert one_cell.present() is True                     # ~3.7 V: a 1S board is plausible too


def test_readings_scale_by_the_calibration():
    regs = {INA.REG_BUSVOLTAGE: 1750 << 3, INA.REG_CURRENT: 65536 - 1312,
            INA.REG_POWER: 459}
    chip = INA.INA219(FakeBus(regs=regs), 0x42, INA.CAL_16V_5A)
    assert chip.bus_voltage() == pytest.approx(7.0)
    assert chip.current_ma() == pytest.approx(-1312 * 0.1524)
    assert chip.power_w() == pytest.approx(459 * 0.003048)
    other = INA.INA219(FakeBus(regs=regs), 0x42, INA.CAL_32V_2A)
    assert other.current_ma() == pytest.approx(-131.2)


def test_open_chip_without_smbus_is_none_and_a_bad_bus_raises(monkeypatch):
    import sys

    monkeypatch.setitem(sys.modules, "smbus", None)      # "import smbus" fails
    assert INA.open_chip(1, 0x42, INA.CAL_16V_5A) is None
    fake = types.SimpleNamespace(SMBus=lambda n: FakeBus())
    assert isinstance(INA.open_chip(1, 0x42, INA.CAL_16V_5A, smbus_module=fake), INA.INA219)

    def broken(n):
        raise OSError("no bus")

    with pytest.raises(OSError):
        INA.open_chip(1, 0x42, INA.CAL_16V_5A, smbus_module=types.SimpleNamespace(SMBus=broken))


# -- ups: pack, status, eta --------------------------------------------------------

def test_pack_percent_is_linear_and_clamped():
    two = UPS.Pack(cells=2)
    assert two.percent(6.0) == 0.0 and two.percent(8.4) == pytest.approx(100.0)
    assert two.percent(5.0) == 0.0 and two.percent(9.0) == 100.0
    assert round(two.percent(7.2)) == 50
    one = UPS.Pack(cells=1)
    assert one.percent(3.0) == 0.0 and round(one.percent(3.6)) == 50
    assert one.percent(4.2) == pytest.approx(100.0)


def test_status_from_current():
    assert UPS.status_from_current(300.0, 50) == "charging"
    assert UPS.status_from_current(-300.0, 50) == "discharging"
    assert UPS.status_from_current(10.0, 99.5) == "full"
    assert UPS.status_from_current(-10.0, 60) == "idle"
    assert UPS.status_from_current(None, 60) == "idle"


def test_fmt_eta():
    assert UPS.fmt_eta(30) == "~<1m"
    assert UPS.fmt_eta(45 * 60) == "~45m"
    assert UPS.fmt_eta(80 * 60) == "~1h 20m"
    assert UPS.fmt_eta(3 * 86400) == "~3d"


def test_eta_estimates_time_to_shutdown():
    m = _monitor()
    # 10% fall over 600s -> 1%/60s; from 50% to the 5% limit is 45% -> 2700s.
    m.history.extend([(0.0, 60.0), (600.0, 50.0)])
    assert m.estimate_eta(_reading(percent=50.0)) == pytest.approx(2700.0, abs=1.0)


def test_eta_none_when_flat_charging_or_without_a_threshold():
    m = _monitor()
    m.history.extend([(0.0, 50.0), (30.0, 49.9)])
    assert m.estimate_eta(_reading(percent=49.9)) is None          # too little trend
    m.history.clear()
    m.history.extend([(0.0, 60.0), (600.0, 50.0)])
    assert m.estimate_eta(_reading(percent=50.0, status="charging")) is None
    off = _monitor(_plugin(shutdown_percent=0))
    off.history.extend([(0.0, 60.0), (600.0, 50.0)])
    assert off.estimate_eta(_reading(percent=50.0)) is None


# -- ups: the auto-shutdown rules ------------------------------------------------------

def test_auto_shutdown_trips_after_three_low_polls_on_battery_and_only_once():
    applied = []
    m = _monitor(_plugin(applied))
    low = _reading(percent=4.0)
    assert m.evaluate(low) is False and applied == []
    assert m.evaluate(low) is False and applied == []
    assert m.evaluate(low) is True
    assert applied == [{"target": "power", "action": "shutdown"}]
    m.evaluate(low)
    assert len(applied) == 1
    assert "Shutting down (4%)" == _card(m)["status"]["value"]


def test_a_recovery_resets_the_count():
    applied = []
    m = _monitor(_plugin(applied))
    m.evaluate(_reading(percent=4.0))
    m.evaluate(_reading(percent=4.0))
    m.evaluate(_reading(percent=40.0))                    # a sag, not a flat pack
    m.evaluate(_reading(percent=4.0))
    assert applied == []


def test_no_shutdown_while_charging_or_when_off():
    applied = []
    m = _monitor(_plugin(applied))
    for _ in range(5):
        assert m.evaluate(_reading(percent=3.0, status="charging", current=300.0)) is False
    off = _monitor(_plugin(applied, shutdown_percent=0))
    for _ in range(5):
        assert off.evaluate(_reading(percent=1.0)) is False
    assert applied == []


def test_a_refused_shutdown_is_retried_next_poll():
    calls = []
    plugin = _plugin()
    plugin.context.apply = lambda change: calls.append(change) or {"ok": False, "error": "busy"}
    m = _monitor(plugin)
    low = _reading(percent=4.0)
    for _ in range(3):
        m.evaluate(low)
    assert len(calls) == 1 and m.shutting_down is False
    m.evaluate(low)
    assert len(calls) == 2


# -- ups: the board, the card and the battery service -----------------------------------

def test_absent_board_shows_why_and_publishes_nothing():
    m = _monitor(probe=lambda: (None, ("no Test HAT detected", "check it is seated")))
    assert m.ensure_device() is False
    assert m.plugin.context.health_note == "check it is seated"
    card = _card(m)
    assert card["status"]["value"] == "no Test HAT detected"
    assert card["percent"]["value"] == "-" and m.reading() is None


def test_found_board_reads_and_fills_the_card():
    m = _monitor(probe=lambda: ("dev", None), read=lambda d: (7.0, -200.0, 1.4))
    assert m.ensure_device() is True and m.plugin.context.health_note == ""
    reading = m.read_battery()
    assert reading["status"] == "discharging" and round(reading["percent"]) == 42
    m.evaluate(reading)
    assert m.reading() == reading and m.reading() is not reading     # a copy
    card = _card(m)
    assert card["percent"]["value"] == "42%" and card["percent"]["mark"] == 5
    assert card["status"]["value"] == "Discharging"
    assert card["voltage"]["value"] == "7.00 V" and card["current"]["value"] == "-200 mA"
    assert card["power"]["value"] == "1.40 W" and card["shutdown"]["value"] == "5%"
    assert card["eta"]["value"] == "estimating…"


def test_a_board_without_current_or_power_shows_dashes():
    m = _monitor(probe=lambda: ("dev", None), read=lambda d: (7.9, None, None))
    m.ensure_device()
    m.evaluate(m.read_battery())
    card = _card(m)
    assert card["current"]["value"] == "-" and card["power"]["value"] == "-"
    assert card["status"]["value"] == "Idle"


def test_a_read_error_is_none():
    def boom(device):
        raise OSError("bus gone")

    m = _monitor(probe=lambda: ("dev", None), read=boom)
    m.ensure_device()
    assert m.read_battery() is None


def test_no_threshold_card_says_off_and_has_no_eta():
    card = _card(_monitor(_plugin(shutdown_percent=0)))
    assert card["shutdown"]["value"] == "off"
    assert "mark" not in card["percent"] and "eta" not in card


def test_the_library_is_provisional():
    assert UPS.LIB_API[0] == 0 and INA.LIB_API == (1, 0)


def test_the_documented_example_plugin_works(tmp_path, monkeypatch):
    # docs/developer/plugin-libraries.md shows a minimal UPS plugin on the ups
    # and ina219 libraries; it must load through the real loader and read a board.
    import re

    from kestrel_pi import loader as loader_mod
    from kestrel_pi import plugins as plugin_api
    from kestrel_pi.capture import CaptureInterface
    from kestrel_pi.connectivity import ConnectivityState
    from kestrel_pi.context import ServiceRegistry
    from kestrel_pi.loader import Loader

    doc = os.path.join(PLUGIN_DIR, "..", "..", "..", "..", "docs", "developer",
                       "plugin-libraries.md")
    text = open(doc, encoding="utf-8").read().split("## `ups`", 1)[1]
    code = re.search(r"### A minimal plugin\n.*?```python\n(.*?)```", text, re.S).group(1)
    plugins = tmp_path / "plugins"
    plugins.mkdir()
    (plugins / "myups.py").write_text(code)
    monkeypatch.setattr(plugin_api, "SHIPPED_PLUGINS_DIR", PLUGIN_DIR)   # the shipped libraries
    bus = FakeBus(regs={INA.REG_BUSVOLTAGE: 1750 << 3, INA.REG_CURRENT: 65536 - 2000,
                        INA.REG_POWER: 700})
    real = INA.open_chip
    monkeypatch.setattr(INA, "open_chip", lambda b, a, c: real(
        b, a, c, smbus_module=types.SimpleNamespace(SMBus=lambda n: bus)))
    monkeypatch.setattr(UPS.Monitor, "start", lambda self: None)    # no poll thread
    applied = []
    monkeypatch.setattr(loader_mod, "apply_change",
                        lambda change, *a, **k: applied.append(change) or {"ok": True})
    services = ServiceRegistry()
    loader = Loader(config={"plugins": {"myups": {"enabled": True}}},
                    capture=CaptureInterface(str(tmp_path)), connectivity=ConnectivityState(),
                    services=services, search_paths=[str(plugins)],
                    logger=logging.getLogger("test.ups.doc"))
    loader.load_all()
    record = loader.records["myups"]
    assert record.state == "loaded", getattr(record, "reason", "")
    monitor = record.instance.monitor
    assert monitor.ensure_device() is True
    monitor.evaluate(monitor.read_battery())
    battery = services.get("battery")
    assert battery["status"] == "discharging" and round(battery["percent"]) == 42
    assert INA.CAL_32V_2A is monitor.device.cal
    record.instance.on_unload()
    assert bus.closed
