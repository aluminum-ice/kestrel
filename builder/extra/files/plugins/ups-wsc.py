"""ups-wsc - battery readout and low-battery auto-shutdown for the Waveshare UPS HAT (C).

The UPS HAT (C) is Waveshare's UPS for the Raspberry Pi Zero, on pogo pins: one
803040 Li-po cell (1000 mAh, 3.7 V nominal, 4.2 V full) and a TI INA219
current/voltage monitor at I2C address 0x43. This plugin reads it, shows the
battery card on the dashboard and - when the operator sets a minimum percent -
cleanly shuts the device down before the cell is exhausted. Everything about
the battery (the card, the time-to-shutdown estimate, the auto-shutdown rules,
one UPS plugin reading at a time) is the shared ``ups`` plugin library, and the
chip is the ``ina219`` library; this file holds the board's options, how it
finds the chip and its pack. The values are those of Waveshare's own (C) demo
(``INA219.py`` in ``UPS_HAT_C.7z``: address 0x43, ``set_calibration_16V_5A``,
``(V - 3) / 1.2``).

**Not yet tested on hardware.** No board was on hand when it was written; it
follows Waveshare's demo, and results are asked for through CONTRIBUTING.md
("Reporting hardware test results").

**It confirms the board before doing anything:** the INA219 must round-trip its
calibration register and read a plausible voltage, and there must be **no**
controller at 0x2D - that is the UPS HAT (D), which reports current the other
way round and needs ``ups-wsd``. The card says which it found.

It reads only the local I2C bus (nothing leaves the device), so it declares just
the ``ui`` capability and needs no authorized-use acknowledgment. It ships
disabled, like every reference plugin. Operator guide:
``docs/user-guide/ups-wsc-setup.md``.
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
BOARD = "UPS HAT (C)"
# One 803040 Li-po cell (1000 mAh), read as linear from 3.0 V empty to 4.2 V full
# (Waveshare's demo: percent = (V - 3) / 1.2).
PACK = ups.Pack(cells=1)
# Waveshare's UPS HAT (C) demo sets the INA219 up with set_calibration_16V_5A.
CALIBRATION = ina219.CAL_16V_5A
# The (D) carries a controller at 0x2D besides its INA219; the (C) has none. It
# is how the two one-cell boards, which otherwise look the same on the bus, are
# told apart: they report current with opposite signs, so the wrong plugin
# would read charging as discharging and its auto-shutdown would never fire.
D_CONTROLLER = 0x2D


class UpsWsc(Plugin):
    __author__ = "kestrel"
    __version__ = "0.1.0"
    __license__ = "GPL3"
    __description__ = ("Waveshare UPS HAT (C) (Pi Zero) battery readout and low-battery "
                       "auto-shutdown. Not yet tested on hardware.")

    __target_api__ = 2
    __capabilities__ = ("ui",)
    # Expose the latest battery reading as a service so a UI plugin (the e-ink
    # face) can show battery / low-power mood without touching I2C itself.
    __provides__ = ("battery",)
    __options__ = {
        "i2c_bus": {"type": "int", "default": 1},
        # The board's INA219 address (0x43 = 67).
        "address": {"type": "int", "default": 0x43},
        # Shut down when the cell falls to this percent while on battery.
        # 0 disables the auto-shutdown (monitor only).
        "shutdown_percent": {"type": "int", "default": 5},
        "poll_seconds": {"type": "int", "default": 20},
    }

    # -- lifecycle -------------------------------------------------------
    def on_loaded(self):
        self._setup()
        self.monitor.start()

    def _setup(self):
        """Initialise state without starting the poll thread (the tests use this
        to exercise the readout and shutdown logic with no hardware)."""
        self.log = self.context.logger
        self._monitor = ups.Monitor(self, NAME, self._probe, self._read, PACK, board=BOARD)

    @property
    def monitor(self):
        """The battery monitor (ups library), made on first use, so the card can
        be built before on_loaded."""
        if getattr(self, "_monitor", None) is None:
            self._setup()
        return self._monitor

    def on_unload(self):
        self.monitor.stop()

    def provide_battery(self):
        """The latest battery reading for the ``battery`` service (see the
        ``ups`` library), or None when the board isn't confirmed."""
        return self.monitor.reading()

    # -- dashboard readout ----------------------------------------------
    def on_ui_setup(self, ui):
        self.monitor.ui_setup(ui)

    def on_ui_update(self, ui, state):
        self.monitor.ui_update(ui, state)

    # -- the HAT ---------------------------------------------------------
    def _probe(self):
        """``(chip, None)`` once the INA219 answers as this board, else ``(None,
        (card text, health note))``."""
        bus, address = self._i2c_bus(), self._address()
        try:
            chip = ina219.open_chip(bus, address, CALIBRATION)
            if chip is None:
                return None, ("no UPS HAT (C): I2C support missing",
                              "python3-smbus is not available, so the UPS HAT (C) "
                              "cannot be read")
            if not chip.present():
                chip.close()
                return None, ("no UPS HAT (C) detected",
                              "Waveshare UPS HAT (C) not detected on I2C bus %d at 0x%02x; "
                              "check it is seated and I2C is enabled" % (bus, address))
            if chip.answers(D_CONTROLLER):
                chip.close()
                return None, ("this looks like a UPS HAT (D): use ups-wsd",
                              "a controller answers at 0x%02x, which the UPS HAT (D) has "
                              "and the (C) doesn't; the (D) reports current the other way "
                              "round, so enable ups-wsd instead" % D_CONTROLLER)
        except (OSError, IOError) as exc:
            return None, ("no UPS HAT (C) detected",
                          "cannot open I2C bus %d (%s); check I2C is enabled" % (bus, exc))
        return chip, None

    @staticmethod
    def _read(chip):
        """Volts, milliamps (negative while the cell discharges) and watts."""
        return chip.bus_voltage(), chip.current_ma(), chip.power_w()

    # -- option getters --------------------------------------------------
    def _i2c_bus(self):
        return ups.safe_int(self.options.get("i2c_bus"), 1)

    def _address(self):
        return ups.safe_int(self.options.get("address"), 0x43)
