"""ups-hat - battery readout and low-battery auto-shutdown for the Waveshare UPS HAT
- deprecated, replaced by ``ups-ws``.

**Deprecated.** ``ups-ws.py`` is this plugin under a name that says which board
it drives: the same code and options. ``ups-hat`` stays for devices that still
have it enabled, gives way to ``ups-ws`` when both are enabled, and is removed
in a future release that requires a reflash (docs/developer/deferred.md).
Change the plugin in both files: ``tests/test_ups_ws.py`` fails when they
differ anywhere but the lines that must (this docstring, ``DEPRECATED``, the
class name and the description).

The Waveshare UPS HAT (and UPS HAT (B), the same design) carries a TI INA219
current/voltage monitor at I2C address 0x42 that measures the 2-cell Li-ion
pack (2 x 18650 in series). The UPS HAT (C) is a different board (one cell, at
0x43) and is not supported here. This plugin reads
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

The battery logic - the readout, the card, the time-to-shutdown estimate and
the auto-shutdown rules - is the shared ``ups`` plugin library
(``_lib_ups.py``), and the chip driver is the ``ina219`` library
(``_lib_ina219.py``); this file holds the HAT's options, how it finds the chip
and its pack.

It reads only the local I2C bus (nothing leaves the device), so it declares just
the ``ui`` capability and needs no authorized-use acknowledgment. It ships
disabled, like every reference plugin.
"""

import os

from kestrel_pi import Plugin, load_library

# The battery readout, card and auto-shutdown (provisional API 0), and the
# INA219 chip driver.
ups = load_library(__file__, "ups", api=0)
ina219 = load_library(__file__, "ina219", api=1)

# The plugin's name is its file name: it starts the log lines and names the
# poll thread.
NAME = os.path.splitext(os.path.basename(__file__))[0]
# A plugin kept only for devices that still have it enabled names its
# replacement; the notice shows on the Plugins page and in the log, and the
# plugin gives way to any other UPS plugin that is enabled.
DEPRECATED = ("deprecated: replaced by ups-ws; enable ups-ws and disable ups-hat "
              "(ups-hat will be removed in a future release that requires a reflash)")
BOARD = "UPS HAT"
# The 2S Li-ion pack reads ~6.0V empty to ~8.4V full; percent is linear across
# that span (Waveshare's demo formula).
PACK = ups.Pack(cells=2)
# What the chip is set up as. Waveshare's UPS HAT demo uses 32V_2A; this plugin
# has always used 16V_5A, kept until a measurement on the board says otherwise.
CALIBRATION = ina219.CAL_16V_5A


class UpsHat(Plugin):
    __author__ = "kestrel"
    __version__ = "0.1.0"
    __license__ = "GPL3"
    __description__ = ("Deprecated: replaced by ups-ws; ups-hat will be removed in a future "
                       "release that requires a reflash. Waveshare UPS HAT battery readout "
                       "and low-battery auto-shutdown.")

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
        if DEPRECATED:
            self.log.warning("%s: %s", NAME, DEPRECATED)
            self.context.health_note = DEPRECATED
        self.monitor.start()

    def _setup(self):
        """Initialise state without starting the poll thread (the tests use this
        to exercise the readout and shutdown logic with no hardware)."""
        self.log = self.context.logger
        self._monitor = ups.Monitor(self, NAME, self._probe, self._read, PACK, board=BOARD,
                                    yields=bool(DEPRECATED), notice=DEPRECATED)

    @property
    def monitor(self):
        """The battery monitor (ups library), made on first use, so the card can
        be built before on_loaded (the user-guide screenshot fixture does)."""
        if getattr(self, "_monitor", None) is None:
            self._setup()
        return self._monitor

    def on_unload(self):
        self.monitor.stop()

    def provide_battery(self):
        """The latest battery reading for the ``battery`` service: a copy of the
        reading dict (``percent``/``voltage``/``current_ma``/``power_w``/
        ``status``) or None when no UPS HAT is present or nothing has been read
        yet. Wired to the ``battery`` service by the loader via ``__provides__``;
        consumed by the e-ink face and the alerts as an optional requirement."""
        return self.monitor.reading()

    # -- dashboard readout ----------------------------------------------
    def on_ui_setup(self, ui):
        self.monitor.ui_setup(ui)

    def on_ui_update(self, ui, state):
        self.monitor.ui_update(ui, state)

    # -- the HAT ---------------------------------------------------------
    def _probe(self):
        """``(chip, None)`` once the INA219 answers as the HAT, else ``(None,
        (card text, health note))``."""
        bus, address = self._i2c_bus(), self._address()
        try:
            chip = ina219.open_chip(bus, address, CALIBRATION)
            if chip is None:
                return None, ("no UPS HAT: I2C support missing",
                              "python3-smbus is not available, so the UPS HAT "
                              "cannot be read")
            if not chip.present():
                chip.close()
                return None, ("no UPS HAT detected",
                              "Waveshare UPS HAT not detected on I2C bus %d at 0x%02x; "
                              "check it is seated and I2C is enabled" % (bus, address))
        except (OSError, IOError) as exc:
            return None, ("no UPS HAT detected",
                          "cannot open I2C bus %d (%s); check I2C is enabled" % (bus, exc))
        return chip, None

    @staticmethod
    def _read(chip):
        return chip.bus_voltage(), chip.current_ma(), chip.power_w()

    # -- option getters --------------------------------------------------
    def _i2c_bus(self):
        return ups.safe_int(self.options.get("i2c_bus"), 1)

    def _address(self):
        return ups.safe_int(self.options.get("address"), 0x42)
