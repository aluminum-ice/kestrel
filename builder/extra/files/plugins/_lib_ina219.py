"""Shared plugin library: the TI INA219 current/voltage monitor over I2C.

The battery monitor on Waveshare's UPS HAT, UPS HAT (B) and UPS HAT (C): the
reads and writes a UPS plugin needs, with the chip's calibration as a parameter
because each board's shunt and range differ. Load it with
``kestrel_pi.load_library`` (see docs/developer/plugin-libraries.md)::

    from kestrel_pi import load_library

    ina219 = load_library(__file__, "ina219", api=1)

    chip = ina219.open_chip(bus=1, address=0x42, calibration=ina219.CAL_16V_5A)
    if chip is not None and chip.present():
        volts, milliamps, watts = chip.bus_voltage(), chip.current_ma(), chip.power_w()

``smbus`` (``python3-smbus`` in the image) is imported only when a chip is
opened, so loading the library needs no I2C support. The calibrations are named
after the functions in Waveshare's ``INA219.py`` demos that set them.

``LIB_API`` follows docs/developer/plugin-libraries.md: a new calibration or
method bumps the minor; a changed signature bumps the major. Names with a
leading underscore are private.
"""

LIB_API = (1, 1)

# INA219 registers (see the TI datasheet and Waveshare's demo driver).
REG_CONFIG = 0x00
REG_BUSVOLTAGE = 0x02
REG_POWER = 0x03
REG_CURRENT = 0x04
REG_CALIBRATION = 0x05


class Calibration:
    """How the chip is set up for a board: the calibration register value, the
    current and power per bit, and the config register (bus range, shunt gain,
    ADC resolution and mode)."""

    def __init__(self, name, cal_value, current_lsb_ma, power_lsb_w, config):
        self.name = name
        self.cal_value = cal_value
        self.current_lsb_ma = current_lsb_ma
        self.power_lsb_w = power_lsb_w
        self.config = config

    def __repr__(self):
        return "Calibration(%r)" % self.name


def _config(bus_range, gain):
    """The config register: ``bus_range`` 0 = 16 V, 1 = 32 V; ``gain`` 1 = /2
    (80 mV), 3 = /8 (320 mV); 12-bit x 32-sample bus and shunt ADC; shunt and
    bus continuous."""
    return (bus_range << 13) | (gain << 11) | (0x0D << 7) | (0x0D << 3) | 0x07


# Waveshare's set_calibration_16V_5A (0.01 ohm shunt, 16 V / 5 A): the UPS HAT
# (C) demo's setting, and what ups-ws (formerly ups-hat) has always programmed.
CAL_16V_5A = Calibration("16V_5A", 26868, 0.1524, 0.003048, _config(0x00, 0x01))
# Waveshare's set_calibration_32V_2A (0.1 ohm shunt, 32 V / 2 A): the UPS HAT and
# UPS HAT (B) demos' setting.
CAL_32V_2A = Calibration("32V_2A", 4096, 0.1, 0.002, _config(0x01, 0x03))

# A bus voltage outside this range at probe time means it isn't a battery board.
MIN_PLAUSIBLE_V = 3.0
MAX_PLAUSIBLE_V = 20.0


class INA219:
    """One INA219 at ``address`` on an open smbus ``bus``."""

    def __init__(self, bus, address, calibration):
        self._bus = bus
        self._addr = address
        self.cal = calibration

    def _read(self, reg):
        data = self._bus.read_i2c_block_data(self._addr, reg, 2)
        return (data[0] << 8) | data[1]

    def _write(self, reg, value):
        self._bus.write_i2c_block_data(
            self._addr, reg, [(value >> 8) & 0xFF, value & 0xFF])

    def present(self, min_v=MIN_PLAUSIBLE_V, max_v=MAX_PLAUSIBLE_V):
        """True only if the calibration register round-trips the exact value
        written (proving a real INA219, not a bare ACK) and the bus voltage then
        reads in a plausible pack range."""
        self._write(REG_CALIBRATION, self.cal.cal_value)
        if self._read(REG_CALIBRATION) != self.cal.cal_value:
            return False
        self._write(REG_CONFIG, self.cal.config)
        return min_v <= self.bus_voltage() <= max_v

    def bus_voltage(self):
        """Volts on the load side. The calibration is written again first, as
        Waveshare's demo does, so a chip that reset keeps reading right."""
        self._write(REG_CALIBRATION, self.cal.cal_value)
        return (self._read(REG_BUSVOLTAGE) >> 3) * 0.004

    def current_ma(self):
        """Milliamps; negative while the battery discharges."""
        value = self._read(REG_CURRENT)
        if value > 32767:
            value -= 65536          # 16-bit two's complement (discharge < 0)
        return value * self.cal.current_lsb_ma

    def power_w(self):
        self._write(REG_CALIBRATION, self.cal.cal_value)
        value = self._read(REG_POWER)
        if value > 32767:
            value -= 65536
        return value * self.cal.power_lsb_w

    def answers(self, address):
        """True if a device at ``address`` on the same bus acknowledges a
        one-byte read (what ``i2cdetect -r`` does), else False. For telling
        boards apart by the other chips they carry; it writes nothing. *(1.1)*"""
        try:
            self._bus.read_byte(address)
        except (OSError, IOError):
            return False
        return True

    def close(self):
        try:
            self._bus.close()
        except (OSError, AttributeError):
            pass


def open_chip(bus, address, calibration, smbus_module=None):
    """An :class:`INA219` on I2C bus number ``bus``, or None when there is no
    I2C support (no ``python3-smbus``). Raises OSError if the bus can't be
    opened. ``smbus_module`` is for tests."""
    if smbus_module is None:
        try:
            import smbus as smbus_module
        except ImportError:
            return None
    return INA219(smbus_module.SMBus(bus), address, calibration)
