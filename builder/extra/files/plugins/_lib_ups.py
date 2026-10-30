"""Shared plugin library: a UPS HAT's battery - readout, card and auto-shutdown.

Everything about a battery HAT that doesn't depend on its chip: the reading a
UPS plugin publishes as the ``battery`` service (which the e-ink face and the
alert plugins consume by name), charge percent from the pack voltage, charging
or discharging from the current, the time-to-shutdown estimate, the dashboard
card, the poll loop that waits for the board to appear, and the low-battery
auto-shutdown rules, kept here once for every UPS plugin. A plugin supplies its
options, a ``probe`` that finds its board and a ``read`` that measures it. Load
it with ``kestrel_pi.load_library`` (see docs/developer/plugin-libraries.md)::

    from kestrel_pi import Plugin, load_library

    ups = load_library(__file__, "ups", api=0)

    class MyUps(Plugin):
        __capabilities__ = ("ui",)
        __provides__ = ("battery",)
        __options__ = {"shutdown_percent": {"type": "int", "default": 5},
                       "poll_seconds": {"type": "int", "default": 20}}

        def on_loaded(self):
            self.monitor = ups.Monitor(self, "my-ups", self._probe, self._read,
                                       ups.Pack(cells=2), board="My UPS HAT")
            self.monitor.start()

        def on_unload(self):
            self.monitor.stop()

        def provide_battery(self):
            return self.monitor.reading()

        def on_ui_setup(self, ui):
            self.monitor.ui_setup(ui)

        def on_ui_update(self, ui, state):
            self.monitor.ui_update(ui, state)

        def _probe(self):            # (device, None) or (None, (card text, health note))
            ...

        def _read(self, device):     # (volts, milliamps or None, watts or None); OSError on failure
            ...

**The auto-shutdown rules**, the same for every UPS plugin: it fires only while
the plugin is enabled, only while the pack is discharging, only at or below the
plugin's ``shutdown_percent`` (0 turns it off), only after
``SHUTDOWN_DEBOUNCE`` low polls in a row (a voltage sag under load never trips
it), and only once. It then powers off at once, with no confirmation - a dying
battery can't wait for a prompt - by sending ``{"target": "power",
"action": "shutdown"}`` through the plugin's ``context.apply``: the same root
helper as the dashboard's power buttons, which allows power changes whenever a
plugin asks. A refused request is logged and retried on the next low poll.

``LIB_API`` is ``(0, ...)``: **provisional**. One family of boards uses it so
far, and the API may still change in a minor release while more UPS plugins
show what really varies; pin ``api=0``. Names with a leading underscore are
private.
"""

import threading
import time
from collections import deque

LIB_API = (0, 1)

# Current near zero is "resting" (on external power, pack full); beyond this it
# is charging (positive) or discharging (negative).
CHARGE_DEADBAND_MA = 30.0
# Consecutive low-and-discharging polls before the auto-shutdown trips.
SHUTDOWN_DEBOUNCE = 3
# Time-to-shutdown estimate: a rolling history of (monotonic time, percent)
# readings, and the trend over it projected to the shutdown threshold. The span
# and minimum drop keep a noisy voltage-derived percent from yielding a wild
# estimate; below them the card shows "estimating..." instead of a number.
HISTORY_LEN = 30            # ~10 min at the 20s default poll
ETA_MIN_SPAN = 120.0        # seconds of history before an estimate is offered
ETA_MIN_DROP = 0.5          # percent the pack must have fallen over that span

STATUS_LABEL = {
    "charging": "Charging",
    "discharging": "Discharging",
    "full": "Full",
    "idle": "Idle",
}


def safe_int(value, default):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def fmt_eta(seconds):
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


class Pack:
    """A Li-ion/Li-po pack of ``cells`` in series, read as linear from
    ``empty_v`` to ``full_v`` per cell (3.0-4.2 V, as Waveshare's demos do)."""

    def __init__(self, cells, empty_v=3.0, full_v=4.2):
        self.cells = cells
        self.empty_v = cells * empty_v
        self.span_v = cells * (full_v - empty_v)

    def percent(self, volts):
        """Charge percent (0-100) from the pack voltage, clamped at both ends."""
        pct = (volts - self.empty_v) / self.span_v * 100.0
        return max(0.0, min(100.0, pct))


def status_from_current(current_ma, percent, deadband_ma=CHARGE_DEADBAND_MA):
    """``charging``/``discharging`` beyond the deadband, else ``full`` (at
    99% or more) or ``idle``. A source with no current reading is ``idle``."""
    if current_ma is not None and current_ma > deadband_ma:
        return "charging"
    if current_ma is not None and current_ma < -deadband_ma:
        return "discharging"
    return "full" if percent >= 99.0 else "idle"


class Monitor:
    """One battery board: the poll worker, the latest reading, the card and the
    auto-shutdown.

    ``plugin`` is the plugin (its ``context`` and live ``options``, of which
    ``shutdown_percent`` and ``poll_seconds`` are read); ``name`` starts each log
    line; ``probe()`` returns ``(device, None)`` once the board is confirmed,
    else ``(None, (card text, health note))``; ``read(device)`` returns
    ``(volts, current_ma, power_w)`` (current and power may be None) and raises
    OSError when the board stops answering; ``pack`` is a :class:`Pack`;
    ``board`` names the board in messages."""

    def __init__(self, plugin, name, probe, read, pack, board="UPS HAT"):
        self.plugin = plugin
        self.name = name
        self._probe = probe
        self._read = read
        self.pack = pack
        self.board = board
        self.log = plugin.context.logger
        self._lock = threading.Lock()        # guards the readout state below
        self._stop = threading.Event()
        self.device = None
        self._reading = None                 # latest reading, or None when absent
        self._status_text = "checking"       # shown while absent / before first read
        self._low_count = 0
        self.shutting_down = False
        self._shutdown_pct = 0
        self.history = deque(maxlen=HISTORY_LEN)   # (monotonic_t, percent)
        self._eta_seconds = None                   # estimated time to threshold
        self._worker = None

    # -- options ----------------------------------------------------------------
    def poll_seconds(self):
        return max(5, safe_int(self.plugin.options.get("poll_seconds"), 20))

    def shutdown_percent(self):
        return max(0, min(100, safe_int(self.plugin.options.get("shutdown_percent"), 5)))

    # -- lifecycle --------------------------------------------------------------
    def start(self):
        self._worker = threading.Thread(target=self._run, name=self.name, daemon=True)
        self._worker.start()

    def stop(self):
        self._stop.set()
        device = self.device
        if device is not None:
            try:
                device.close()
            except Exception:  # noqa: BLE001 - best effort on the way out
                pass

    def _run(self):
        while not self._stop.is_set():
            if self.ensure_device():
                reading = self.read_battery()
                if reading is None:
                    # Lost contact mid-run: drop the handle and re-probe next pass.
                    self.device = None
                    self.set_absent(
                        "%s read error" % self.board,
                        "lost contact with the %s; re-probing" % self.board)
                else:
                    self.evaluate(reading)
            self._stop.wait(timeout=self.poll_seconds())

    # -- the board ----------------------------------------------------------------
    def ensure_device(self):
        """Confirm the board is present before any reading. Caches the device
        once confirmed; sets the absent state and keeps re-probing otherwise."""
        if self.device is not None:
            return True
        device, why = self._probe()
        if device is None:
            self.set_absent(*why)
            return False
        self.device = device
        self.plugin.context.health_note = ""
        return True

    def set_absent(self, status_text, health):
        with self._lock:
            self._reading = None
            self._status_text = status_text
        self.plugin.context.health_note = health

    def read_battery(self):
        """One reading as the ``battery`` service publishes it, or None if the
        board didn't answer."""
        device = self.device
        if device is None:
            return None
        try:
            voltage, current, power = self._read(device)
        except (OSError, IOError) as exc:
            self.log.debug("%s: read failed: %s", self.name, exc)
            return None
        percent = self.pack.percent(voltage)
        return {"percent": percent, "voltage": voltage, "current_ma": current,
                "power_w": power, "status": status_from_current(current, percent)}

    def reading(self):
        """A copy of the latest reading for the ``battery`` service, or None
        when no board is present or nothing has been read yet."""
        with self._lock:
            return dict(self._reading) if self._reading else None

    # -- the auto-shutdown ----------------------------------------------------------
    def evaluate(self, reading):
        """Cache one reading and trip the debounced auto-shutdown when the pack
        is low and discharging. Returns True if a shutdown was requested."""
        self.history.append((time.monotonic(), reading["percent"]))
        eta = self.estimate_eta(reading)
        with self._lock:
            self._reading = reading
            self._status_text = STATUS_LABEL.get(reading["status"])
            self._eta_seconds = eta
        threshold = self.shutdown_percent()
        low = (threshold > 0 and reading["status"] == "discharging"
               and reading["percent"] <= threshold)
        self._low_count = self._low_count + 1 if low else 0
        if self._low_count < SHUTDOWN_DEBOUNCE or self.shutting_down:
            return False
        with self._lock:
            self.shutting_down = True
            self._shutdown_pct = int(reading["percent"])
        self.log.warning(
            "%s: battery %.0f%% at or below the %d%% limit while on "
            "battery; shutting down", self.name, reading["percent"], threshold)
        return self._request_shutdown()

    def _request_shutdown(self):
        # The daemon runs as the pi user; the shutdown goes through the root
        # apply helper (the same one the dashboard's power buttons use).
        try:
            reply = self.plugin.context.apply({"target": "power", "action": "shutdown"})
        except Exception as exc:  # noqa: BLE001 - never let a shutdown error crash the worker
            self.log.error("%s: could not request shutdown: %r", self.name, exc)
            reply = {"ok": False}
        if not (reply or {}).get("ok"):
            self.log.error("%s: shutdown request refused: %s", self.name,
                           (reply or {}).get("error"))
            with self._lock:
                self.shutting_down = False   # the pack is still low; retry next poll
            return False
        return True

    def estimate_eta(self, reading):
        """Estimate seconds until the pack falls to the shutdown threshold, from
        the discharge trend over the reading history. Returns None when it is not
        meaningful (not discharging, no threshold, or too little/too flat a trend
        to be reliable)."""
        threshold = self.shutdown_percent()
        if threshold <= 0 or reading["status"] != "discharging":
            return None
        if reading["percent"] <= threshold:
            return 0.0
        history = list(self.history)
        if len(history) < 2:
            return None
        t_old, p_old = history[0]
        t_new, p_new = history[-1]
        span = t_new - t_old
        drop = p_old - p_new            # percent fallen over the window
        if span < ETA_MIN_SPAN or drop < ETA_MIN_DROP:
            return None                 # too little trend to project reliably
        rate = drop / span              # percent per second, positive
        return (reading["percent"] - threshold) / rate

    # -- the dashboard card ---------------------------------------------------------
    @staticmethod
    def shutdown_text(threshold):
        return ("%d%%" % threshold) if threshold > 0 else "off"

    def ui_setup(self, ui):
        threshold = self.shutdown_percent()
        # The charge gauge marks the shutdown threshold on the bar (when one is
        # set); the threshold is also shown as text below, so it never relies on
        # the marker alone.
        ui.add_element("percent", "-", label="Charge", kind="gauge",
                       mark=(threshold if threshold > 0 else None))
        ui.add_element("status", "checking", label="Status")
        ui.add_element("shutdown", self.shutdown_text(threshold),
                       label="Auto-shutdown")
        if threshold > 0:
            ui.add_element("eta", "-", label="Time to shutdown")
        ui.add_element("voltage", "-", label="Voltage")
        ui.add_element("current", "-", label="Current")
        ui.add_element("power", "-", label="Power")

    def ui_update(self, ui, state):
        with self._lock:
            reading = self._reading
            status_text = self._status_text
            shutting = self.shutting_down
            pct = self._shutdown_pct
            eta = self._eta_seconds
        threshold = self.shutdown_percent()
        ui.set("shutdown", self.shutdown_text(threshold))
        if reading is None:
            # No board confirmed: show the reason, leave the gauge/readouts blank.
            ui.set("percent", "-")
            ui.set("status", status_text)
            ui.set("voltage", "-")
            ui.set("current", "-")
            ui.set("power", "-")
            if threshold > 0:
                ui.set("eta", "-")
            return
        status = STATUS_LABEL.get(reading["status"], reading["status"])
        if shutting:
            status = "Shutting down (%d%%)" % pct
        ui.set("percent", "%d%%" % int(round(reading["percent"])))
        ui.set("status", status)
        ui.set("voltage", "%.2f V" % reading["voltage"])
        current, power = reading["current_ma"], reading["power_w"]
        ui.set("current", "-" if current is None else "%+.0f mA" % current)
        ui.set("power", "-" if power is None else "%.2f W" % power)
        if threshold > 0:
            if reading["status"] != "discharging":
                ui.set("eta", "—")              # only meaningful on battery
            elif eta is None:
                ui.set("eta", "estimating…")    # not enough trend yet
            else:
                ui.set("eta", fmt_eta(eta))
