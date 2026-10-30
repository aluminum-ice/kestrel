"""ups-wse - battery readout and low-battery auto-shutdown for the Waveshare UPS HAT (E).

The UPS HAT (E) holds four 21700 cells in series, charges over USB-C (with fast
charging) and has its own controller at I2C address 0x2D that reports the
charge state, the battery voltage, current and percent, and each cell's voltage.
This plugin reads it, shows the battery card on the dashboard (with a row of
the four cell voltages) and - when the operator sets a minimum percent - cleanly
shuts the device down before the pack is exhausted. Everything about the battery
(the card, the time-to-shutdown estimate, the auto-shutdown rules, one UPS plugin
reading at a time) is the shared ``ups`` plugin library; this file holds the
board's options, its registers and the two things particular to it, below. The
registers are those of Waveshare's own (E) demo (``ups.py`` in
``UPS_HAT_E.zip``).

- **The low-cell rule.** With four cells in series, one weak cell can be in
  danger before the pack's percent looks low, so - as Waveshare's demo does - a
  cell under 3.15 V while discharging counts as low too. The library treats it
  like reaching ``shutdown_percent`` (the same three polls, once), and only
  while the auto-shutdown is on.
- **Power on again when charged** (option ``power_on_when_charged``, off by
  default). Just before it shuts down, the plugin can tell the board to power
  the Raspberry Pi back on once it has charged, by writing 0x55 to the
  controller's register 0x01, as the demo does. It is the only write this
  plugin makes to the board.

**Not yet tested on hardware.** No board was on hand when it was written; it
follows Waveshare's demo, and results are asked for through CONTRIBUTING.md
("Reporting hardware test results").

**It confirms the board before doing anything:** the controller at 0x2D must
answer with a plausible pack (9-18 V, a percent from 0 to 100, four cells
between 2.5 and 4.5 V), and there must be no INA219 at 0x43 - that is the UPS HAT
(D), whose controller also sits at 0x2D and which needs ``ups-wsd``.

It reads (and, with the option, writes) only the local I2C bus; nothing leaves
the device, so it declares just the ``ui`` capability and needs no
authorized-use acknowledgment. It ships disabled, like every reference plugin.
Operator guide: ``docs/user-guide/ups-wse-setup.md``.
"""

import os

from kestrel_pi import Plugin, load_library

# The battery readout, card and auto-shutdown (provisional API 0; 0.3 adds the
# board's own percent, the "low" reading and the before-shutdown hook).
ups = load_library(__file__, "ups", api=0)

# The plugin's name is its file name: it starts the log lines and names the
# poll thread.
NAME = os.path.splitext(os.path.basename(__file__))[0]
BOARD = "UPS HAT (E)"
# Four cells in series. The board reports its own percent; this is only the
# estimate the library would fall back to.
PACK = ups.Pack(cells=4)

# The controller's registers (Waveshare's UPS HAT (E) demo). Words are
# little-endian.
REG_POWER_ON = 0x01          # write 0x55: power the Pi on again once charged
POWER_ON_WHEN_CHARGED = 0x55
REG_STATE = 0x02             # 1 byte: the charge state bits below
STATE_FAST_CHARGING = 0x40
STATE_CHARGING = 0x80
STATE_DISCHARGING = 0x20
REG_BATTERY = 0x20           # 12 bytes: mV, mA (signed; < 0 discharging), percent, mAh,
#                              minutes to empty, minutes to full
REG_CELLS = 0x30             # 8 bytes: the four cells' mV
LOW_CELL_MV = 3150           # the demo's low-cell limit
# A plausible (E): the 4S pack, a percent, four cells.
PACK_MV = (9000, 18000)
CELL_MV = (2500, 4500)
# The UPS HAT (D)'s INA219: the (D) also has a controller at 0x2D, the (E) has no
# INA219, so this tells them apart.
D_INA219 = 0x43


def _word(data, index):
    return data[index] | (data[index + 1] << 8)


class _Board:
    """The (E)'s controller on an open smbus ``bus``."""

    def __init__(self, bus, address):
        self.bus = bus
        self.address = address

    def _block(self, reg, count):
        return self.bus.read_i2c_block_data(self.address, reg, count)

    def state(self):
        return self._block(REG_STATE, 1)[0]

    def battery(self):
        """``(millivolts, milliamps, percent)``; milliamps < 0 while discharging."""
        data = self._block(REG_BATTERY, 12)
        current = _word(data, 2)
        if current > 0x7FFF:
            current -= 0x10000          # 16-bit two's complement
        return _word(data, 0), current, _word(data, 4)

    def cells(self):
        data = self._block(REG_CELLS, 8)
        return [_word(data, i) for i in (0, 2, 4, 6)]

    def answers(self, address):
        try:
            self.bus.read_byte(address)
        except (OSError, IOError):
            return False
        return True

    def power_on_when_charged(self):
        self.bus.write_byte_data(self.address, REG_POWER_ON, POWER_ON_WHEN_CHARGED)

    def close(self):
        try:
            self.bus.close()
        except (OSError, AttributeError):
            pass


def _open_bus(number, smbus_module=None):
    """An smbus bus, or None with no I2C support (``python3-smbus``)."""
    if smbus_module is None:
        try:
            import smbus as smbus_module
        except ImportError:
            return None
    return smbus_module.SMBus(number)


class UpsWse(Plugin):
    __author__ = "kestrel"
    __version__ = "0.1.0"
    __license__ = "GPL3"
    __description__ = ("Waveshare UPS HAT (E) battery readout and low-battery "
                       "auto-shutdown. Not yet tested on hardware.")

    __target_api__ = 2
    __capabilities__ = ("ui",)
    # Expose the latest battery reading as a service so a UI plugin (the e-ink
    # face) can show battery / low-power mood without touching I2C itself.
    __provides__ = ("battery",)
    __options__ = {
        "i2c_bus": {"type": "int", "default": 1},
        # The controller's address (0x2D = 45).
        "address": {"type": "int", "default": 0x2D},
        # Shut down when the pack falls to this percent - or a cell under
        # 3.15 V - while on battery. 0 disables the auto-shutdown (monitor only).
        "shutdown_percent": {"type": "int", "default": 5},
        "poll_seconds": {"type": "int", "default": 20},
        "power_on_when_charged": {
            "type": "bool", "default": False,
            "help": "Before an auto-shutdown, tell the HAT to power the Raspberry Pi "
                    "on again once it has charged."},
    }

    # -- lifecycle -------------------------------------------------------
    def on_loaded(self):
        self._setup()
        self.monitor.start()

    def _setup(self):
        """Initialise state without starting the poll thread (the tests use this
        to exercise the readout and shutdown logic with no hardware)."""
        self.log = self.context.logger
        self._cells = None
        self._monitor = ups.Monitor(self, NAME, self._probe, self._read, PACK, board=BOARD,
                                    before_shutdown=self._before_shutdown)

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
        ui.add_element("cells", "-", label="Cells")

    def on_ui_update(self, ui, state):
        self.monitor.ui_update(ui, state)
        cells = self._cells if self.monitor.reading() is not None else None
        ui.set("cells", " / ".join("%.2f" % (mv / 1000.0) for mv in cells) + " V"
               if cells else "-")

    # -- the HAT ---------------------------------------------------------
    def _probe(self):
        """``(board, None)`` once the controller answers as an (E), else
        ``(None, (card text, health note))``."""
        number, address = self._i2c_bus(), self._address()
        try:
            bus = _open_bus(number)
            if bus is None:
                return None, ("no %s: I2C support missing" % BOARD,
                              "python3-smbus is not available, so the %s cannot be read"
                              % BOARD)
            board = _Board(bus, address)
            try:
                millivolts, _current, percent = board.battery()
                cells = board.cells()
                board.state()
            except (OSError, IOError):
                board.close()
                return None, ("no %s detected" % BOARD,
                              "Waveshare %s not detected on I2C bus %d at 0x%02x; check "
                              "it is seated and I2C is enabled" % (BOARD, number, address))
            plausible = (PACK_MV[0] <= millivolts <= PACK_MV[1] and 0 <= percent <= 100
                         and all(CELL_MV[0] <= mv <= CELL_MV[1] for mv in cells))
            if not plausible:
                board.close()
                return None, ("no %s detected" % BOARD,
                              "a device answers at 0x%02x on I2C bus %d, but not as a %s "
                              "(no plausible 4-cell pack)" % (address, number, BOARD))
            if board.answers(D_INA219):
                board.close()
                return None, ("this looks like a UPS HAT (D): use ups-wsd",
                              "an INA219 answers at 0x%02x, which the UPS HAT (D) has and "
                              "the (E) doesn't; enable ups-wsd instead" % D_INA219)
        except (OSError, IOError) as exc:
            return None, ("no %s detected" % BOARD,
                          "cannot open I2C bus %d (%s); check I2C is enabled" % (number, exc))
        return board, None

    def _read(self, board):
        """The board's own reading, and the low-cell rule (Waveshare's demo): a
        cell under LOW_CELL_MV makes the reading ``low``, which the library
        treats like reaching shutdown_percent while discharging."""
        state = board.state()
        millivolts, current, percent = board.battery()
        cells = board.cells()
        self._cells = cells
        if state & (STATE_FAST_CHARGING | STATE_CHARGING):
            status = "charging"
        elif state & STATE_DISCHARGING:
            status = "discharging"
        else:
            status = "full" if percent >= 99 else "idle"
        low = None
        weak = [(i + 1, mv) for i, mv in enumerate(cells) if mv < LOW_CELL_MV]
        if weak:
            index, mv = min(weak, key=lambda c: c[1])
            low = "cell %d at %.2f V, under %.2f V" % (index, mv / 1000.0, LOW_CELL_MV / 1000.0)
        return {"voltage": millivolts / 1000.0, "current_ma": float(current),
                "power_w": abs(millivolts * current) / 1e6, "percent": float(percent),
                "status": status, "low": low}

    def _before_shutdown(self):
        """With ``power_on_when_charged``, tell the board to power the Pi on
        again once it has charged (the demo's write of 0x55 to register 0x01)."""
        if not self.options.get("power_on_when_charged", False):
            return
        board = self.monitor.device
        if board is None:
            return
        board.power_on_when_charged()
        self.log.info("%s: the HAT will power the device on again once charged", NAME)

    # -- option getters --------------------------------------------------
    def _i2c_bus(self):
        return ups.safe_int(self.options.get("i2c_bus"), 1)

    def _address(self):
        return ups.safe_int(self.options.get("address"), 0x2D)
