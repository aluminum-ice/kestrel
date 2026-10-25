"""ups-hat - battery readout and low-battery auto-shutdown for the Waveshare UPS HAT.

The Waveshare UPS HAT (and UPS HAT (B)/(C)) carries a TI INA219 current/voltage
monitor on the I2C bus that measures the 2-cell Li-ion pack. This plugin reads
it and shows a battery card on the dashboard (charge percent, charge/discharge
state, voltage, current and power), and - when the operator sets a minimum
percent - cleanly shuts the device down before the pack is exhausted, protecting
the SD card from an abrupt power loss.

**It confirms the HAT is actually present before doing anything.** On each poll
it probes the INA219 at the HAT's I2C address and round-trips the (read/write)
calibration register: a device that returns the exact value written - not merely
an I2C ACK - and then reports a bus voltage in a plausible pack range is taken as
the HAT. With no HAT (or no I2C/smbus) the card reads "no UPS HAT detected", the
plugin stays idle, and the auto-shutdown never fires.

**Auto-shutdown** is governed by ``shutdown_percent`` (0 disables it; default 5).
It runs only while this plugin is enabled, only when the pack is discharging (not
while charging or on external power), and only after the low reading persists for
a few consecutive polls, so a brief voltage sag under load never trips it. When
it does trip it powers off **immediately, with no confirmation** - a dying
battery can't wait for a prompt. The daemon runs as the ``pi`` user and can't
power the device off itself, so the shutdown goes through the same root helper
socket as the web power buttons (a ``{"target": "power", "action": "shutdown"}``
change); that path is allowed whenever this plugin asks, independent of the web
UI's auth setting.

It reads only the local I2C bus (nothing leaves the device), so it declares just
the ``ui`` capability and needs no authorized-use acknowledgment. It ships
disabled, like every reference plugin.
"""

from __future__ import annotations

import threading
import time
from collections import deque

from kestrel_pi import Plugin

try:
    import smbus
except ImportError:  # no python3-smbus (e.g. the build/test host): degrade cleanly
    smbus = None

# INA219 registers (see the TI datasheet / the Waveshare vendor driver).
_REG_CONFIG = 0x00
_REG_BUSVOLTAGE = 0x02
_REG_POWER = 0x03
_REG_CURRENT = 0x04
_REG_CALIBRATION = 0x05

# Waveshare UPS HAT calibration: 0.01 ohm shunt, 16V / 5A range. The LSBs and
# the calibration value are the vendor driver's for that range.
_CAL_VALUE = 26868
_CURRENT_LSB = 0.1524    # mA per bit
_POWER_LSB = 0.003048    # W per bit
# config register: 16V bus range, /2 gain (80mV), 12-bit 32-sample bus and shunt
# ADC, shunt-and-bus continuous.
_CONFIG_VALUE = (0x00 << 13) | (0x01 << 11) | (0x0D << 7) | (0x0D << 3) | 0x07

# The 2S Li-ion pack reads ~6.0V empty to ~8.4V full; percent is linear across
# that 2.4V span (the vendor driver's formula).
_EMPTY_V = 6.0
_FULL_SPAN_V = 2.4
# A bus voltage outside this range at probe time means it isn't the UPS HAT.
_MIN_PLAUSIBLE_V = 3.0
_MAX_PLAUSIBLE_V = 20.0

# Current near zero is "resting" (on external power, pack full); beyond this it
# is charging (positive) or discharging (negative).
_CHARGE_DEADBAND_MA = 30.0
# Consecutive low-and-discharging polls before the auto-shutdown trips.
_SHUTDOWN_DEBOUNCE = 3

# Time-to-shutdown estimate: a rolling history of (monotonic time, percent)
# readings, and the trend over it projected to the shutdown threshold. The span
# and minimum drop keep a noisy voltage-derived percent from yielding a wild
# estimate; below them the card shows "estimating..." instead of a number.
_HISTORY_LEN = 30            # ~10 min at the 20s default poll
_ETA_MIN_SPAN = 120.0        # seconds of history before an estimate is offered
_ETA_MIN_DROP = 0.5          # percent the pack must have fallen over that span

_STATUS_LABEL = {
    "charging": "Charging",
    "discharging": "Discharging",
    "full": "Full",
    "idle": "Idle",
}


def _safe_int(value, default):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _fmt_eta(seconds):
    """A short time-to-shutdown string: "~<1m", "~45m", "~1h 20m", or "~2d" for
    very long projections."""
    seconds = max(0, int(seconds))
    if seconds < 60:
        return "~<1m"
    minutes = seconds // 60
    if minutes < 60:
        return "~%dm" % minutes
    hours, rem = divmod(minutes, 60)
    if hours < 48:
        return "~%dh %02dm" % (hours, rem)
    return "~%dd" % (hours // 24)


def _percent_from_voltage(volts):
    """Pack charge percent (0-100) from the bus voltage, linear over the 2S
    Li-ion span, clamped at both ends."""
    pct = (volts - _EMPTY_V) / _FULL_SPAN_V * 100.0
    return max(0.0, min(100.0, pct))


class _INA219:
    """The handful of INA219 reads/writes this plugin needs, over an smbus bus."""

    def __init__(self, bus, addr):
        self._bus = bus
        self._addr = addr

    def _read(self, reg):
        data = self._bus.read_i2c_block_data(self._addr, reg, 2)
        return (data[0] << 8) | data[1]

    def _write(self, reg, value):
        self._bus.write_i2c_block_data(
            self._addr, reg, [(value >> 8) & 0xFF, value & 0xFF])

    def present(self):
        """True only if the calibration register round-trips the exact value we
        write (proving a real INA219, not a bare ACK) and the bus voltage then
        reads in a plausible pack range."""
        self._write(_REG_CALIBRATION, _CAL_VALUE)
        if self._read(_REG_CALIBRATION) != _CAL_VALUE:
            return False
        self._write(_REG_CONFIG, _CONFIG_VALUE)
        return _MIN_PLAUSIBLE_V <= self.bus_voltage() <= _MAX_PLAUSIBLE_V

    def bus_voltage(self):
        self._write(_REG_CALIBRATION, _CAL_VALUE)
        return (self._read(_REG_BUSVOLTAGE) >> 3) * 0.004

    def current_ma(self):
        value = self._read(_REG_CURRENT)
        if value > 32767:
            value -= 65536          # 16-bit two's complement (discharge < 0)
        return value * _CURRENT_LSB

    def power_w(self):
        self._write(_REG_CALIBRATION, _CAL_VALUE)
        value = self._read(_REG_POWER)
        if value > 32767:
            value -= 65536
        return value * _POWER_LSB

    def close(self):
        try:
            self._bus.close()
        except (OSError, AttributeError):
            pass


class UpsHat(Plugin):
    __author__ = "kestrel"
    __version__ = "0.1.0"
    __license__ = "GPL3"
    __description__ = "Waveshare UPS HAT battery readout and low-battery auto-shutdown."

    __target_api__ = 2
    __capabilities__ = ("ui",)
    # Expose the latest battery reading as a service so a UI plugin (the e-ink
    # face) can show battery / low-power mood without touching I2C itself.
    __provides__ = ("battery",)
    __options__ = {
        "i2c_bus": {"type": "int", "default": 1},
        # The UPS HAT's INA219 address (0x42 = 66). Exposed for the odd variant.
        "address": {"type": "int", "default": 0x42},
        # Shut down when the pack falls to this percent while on battery.
        # 0 disables the auto-shutdown (monitor only).
        "shutdown_percent": {"type": "int", "default": 5},
        "poll_seconds": {"type": "int", "default": 20},
    }

    # -- lifecycle -------------------------------------------------------
    def on_loaded(self):
        self._setup()
        self._worker = threading.Thread(
            target=self._run, name="ups-hat", daemon=True)
        self._worker.start()

    def _setup(self):
        """Initialise state without starting the poll thread (the tests use this
        to exercise the readout and shutdown logic with no hardware)."""
        self.log = self.context.logger
        self._lock = threading.Lock()        # guards the readout state below
        self._stop = threading.Event()
        self._ina = None
        self._reading = None                 # latest reading, or None when absent
        self._status_text = "checking"       # shown while absent / before first read
        self._low_count = 0
        self._shutting_down = False
        self._shutdown_pct = 0
        self._history = deque(maxlen=_HISTORY_LEN)   # (monotonic_t, percent)
        self._eta_seconds = None                     # estimated time to threshold

    def on_unload(self):
        self._stop.set()
        ina = self._ina
        if ina is not None:
            ina.close()

    # -- service provider ------------------------------------------------
    def provide_battery(self):
        """The latest battery reading for the ``battery`` service. Returns a
        copy of the reading dict (``percent``/``voltage``/``current_ma``/
        ``power_w``/``status``) or None when no UPS HAT is present or nothing
        has been read yet. Wired to the ``battery`` service by the loader via
        ``__provides__``; consumed by the e-ink face as an optional requirement."""
        with self._lock:
            return dict(self._reading) if self._reading else None

    # -- dashboard readout ----------------------------------------------
    def on_ui_setup(self, ui):
        threshold = self._shutdown_percent()
        # The charge gauge marks the shutdown threshold on the bar (when one is
        # set); the threshold is also shown as text below, so it never relies on
        # the marker alone.
        ui.add_element("percent", "-", label="Charge", kind="gauge",
                       mark=(threshold if threshold > 0 else None))
        ui.add_element("status", "checking", label="Status")
        ui.add_element("shutdown", self._shutdown_text(threshold),
                       label="Auto-shutdown")
        if threshold > 0:
            ui.add_element("eta", "-", label="Time to shutdown")
        ui.add_element("voltage", "-", label="Voltage")
        ui.add_element("current", "-", label="Current")
        ui.add_element("power", "-", label="Power")

    def on_ui_update(self, ui, state):
        with self._lock:
            reading = self._reading
            status_text = self._status_text
            shutting = self._shutting_down
            pct = self._shutdown_pct
            eta = self._eta_seconds
        threshold = self._shutdown_percent()
        ui.set("shutdown", self._shutdown_text(threshold))
        if reading is None:
            # No HAT confirmed: show the reason, leave the gauge/readouts blank.
            ui.set("percent", "-")
            ui.set("status", status_text)
            ui.set("voltage", "-")
            ui.set("current", "-")
            ui.set("power", "-")
            if threshold > 0:
                ui.set("eta", "-")
            return
        status = _STATUS_LABEL.get(reading["status"], reading["status"])
        if shutting:
            status = "Shutting down (%d%%)" % pct
        ui.set("percent", "%d%%" % int(round(reading["percent"])))
        ui.set("status", status)
        ui.set("voltage", "%.2f V" % reading["voltage"])
        ui.set("current", "%+.0f mA" % reading["current_ma"])
        ui.set("power", "%.2f W" % reading["power_w"])
        if threshold > 0:
            if reading["status"] != "discharging":
                ui.set("eta", "—")              # only meaningful on battery
            elif eta is None:
                ui.set("eta", "estimating…")    # not enough trend yet
            else:
                ui.set("eta", _fmt_eta(eta))

    # -- worker ----------------------------------------------------------
    def _run(self):
        while not self._stop.is_set():
            if self._ensure_device():
                reading = self._read_battery()
                if reading is None:
                    # Lost contact mid-run: drop the handle and re-probe next pass.
                    self._ina = None
                    self._set_absent(
                        "UPS HAT read error",
                        "lost contact with the UPS HAT; re-probing")
                else:
                    self._evaluate(reading)
            self._stop.wait(timeout=self._poll_seconds())

    def _ensure_device(self):
        """Confirm the HAT is present (probing the INA219) before any reading.
        Caches the device handle once confirmed; sets the absent state and keeps
        re-probing otherwise."""
        if self._ina is not None:
            return True
        if smbus is None:
            self._set_absent(
                "no UPS HAT: I2C support missing",
                "python3-smbus is not available, so the UPS HAT cannot be read")
            return False
        bus = None
        try:
            bus = smbus.SMBus(self._i2c_bus())
            ina = _INA219(bus, self._address())
            if not ina.present():
                if bus is not None:
                    try:
                        bus.close()
                    except (OSError, AttributeError):
                        pass
                self._set_absent(
                    "no UPS HAT detected",
                    "Waveshare UPS HAT not detected on I2C bus %d at 0x%02x; "
                    "check it is seated and I2C is enabled"
                    % (self._i2c_bus(), self._address()))
                return False
        except (OSError, IOError) as exc:
            self._set_absent(
                "no UPS HAT detected",
                "cannot open I2C bus %d (%s); check I2C is enabled"
                % (self._i2c_bus(), exc))
            return False
        self._ina = ina
        self.context.health_note = ""
        return True

    def _read_battery(self):
        ina = self._ina
        if ina is None:
            return None
        try:
            voltage = ina.bus_voltage()
            current = ina.current_ma()
            power = ina.power_w()
        except (OSError, IOError) as exc:
            self.log.debug("ups-hat: read failed: %s", exc)
            return None
        percent = _percent_from_voltage(voltage)
        if current > _CHARGE_DEADBAND_MA:
            status = "charging"
        elif current < -_CHARGE_DEADBAND_MA:
            status = "discharging"
        else:
            status = "full" if percent >= 99.0 else "idle"
        return {"percent": percent, "voltage": voltage, "current_ma": current,
                "power_w": power, "status": status}

    def _evaluate(self, reading):
        """Cache one reading and trip the debounced auto-shutdown when the pack
        is low and discharging. Returns True if a shutdown was requested."""
        self._history.append((time.monotonic(), reading["percent"]))
        eta = self._estimate_eta(reading)
        with self._lock:
            self._reading = reading
            self._status_text = _STATUS_LABEL.get(reading["status"])
            self._eta_seconds = eta
        threshold = self._shutdown_percent()
        low = (threshold > 0 and reading["status"] == "discharging"
               and reading["percent"] <= threshold)
        self._low_count = self._low_count + 1 if low else 0
        if self._low_count < _SHUTDOWN_DEBOUNCE or self._shutting_down:
            return False
        with self._lock:
            self._shutting_down = True
            self._shutdown_pct = int(reading["percent"])
        self.log.warning(
            "ups-hat: battery %.0f%% at or below the %d%% limit while on "
            "battery; shutting down", reading["percent"], threshold)
        return self._request_shutdown()

    def _estimate_eta(self, reading):
        """Estimate seconds until the pack falls to the shutdown threshold, from
        the discharge trend over the reading history. Returns None when it is not
        meaningful (not discharging, no threshold, or too little/too flat a trend
        to be reliable)."""
        threshold = self._shutdown_percent()
        if threshold <= 0 or reading["status"] != "discharging":
            return None
        if reading["percent"] <= threshold:
            return 0.0
        history = list(self._history)
        if len(history) < 2:
            return None
        t_old, p_old = history[0]
        t_new, p_new = history[-1]
        span = t_new - t_old
        drop = p_old - p_new            # percent fallen over the window
        if span < _ETA_MIN_SPAN or drop < _ETA_MIN_DROP:
            return None                 # too little trend to project reliably
        rate = drop / span              # percent per second, positive
        return (reading["percent"] - threshold) / rate

    def _shutdown_text(self, threshold):
        return ("%d%%" % threshold) if threshold > 0 else "off"

    def _request_shutdown(self):
        # The daemon is the pi user; the shutdown goes through the root apply
        # helper socket (the same one the web power buttons use). web.apply_change
        # is only the socket client here - it needs no running web server.
        from kestrel_pi import web
        try:
            reply = web.apply_change({"target": "power", "action": "shutdown"})
        except Exception as exc:  # noqa: BLE001 - never let a shutdown error crash the worker
            self.log.error("ups-hat: could not request shutdown: %r", exc)
            reply = {"ok": False}
        if not reply.get("ok"):
            self.log.error("ups-hat: shutdown request refused: %s",
                           reply.get("error"))
            with self._lock:
                self._shutting_down = False   # the pack is still low; retry next poll
            return False
        return True

    def _set_absent(self, status_text, health):
        with self._lock:
            self._reading = None
            self._status_text = status_text
        self.context.health_note = health

    # -- option getters --------------------------------------------------
    def _i2c_bus(self):
        return _safe_int(self.options.get("i2c_bus"), 1)

    def _address(self):
        return _safe_int(self.options.get("address"), 0x42)

    def _poll_seconds(self):
        return max(5, _safe_int(self.options.get("poll_seconds"), 20))

    def _shutdown_percent(self):
        return max(0, min(100, _safe_int(self.options.get("shutdown_percent"), 5)))
