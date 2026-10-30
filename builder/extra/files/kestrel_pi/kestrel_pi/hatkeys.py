"""The four keys on the Waveshare 2.7" e-Paper HAT, for the ``eink`` plugin.

The panel the ``eink`` plugin draws on has four push-buttons. They are part of
the same HAT, so the plugin owns them; the key handling lives here so the
plugin file stays about drawing:

=====  ===========================  ==========================================
Key    Short press                  Long press (``hold_seconds``, 3 s)
=====  ===========================  ==========================================
KEY1   info screen for 30 s         -
KEY2   (a hint)                     stop capture at once; start it after a
                                    second press
KEY3   (a hint)                     reboot, after a second press
KEY4   (a hint)                     shut down, after a second press
=====  ===========================  ==========================================

Starting capture, rebooting and shutting down ask "press KEY_ again to ..."; a
second press of the same key within ``CONFIRM_SECONDS`` confirms, so a press in
a pocket does nothing. Stopping capture is immediate.

Every action goes through the same root apply helper as the dashboard
(``context.apply``), which validates each request, so the keys grant no new
privilege. Starting capture sends no authorized-use acknowledgment: the helper
starts it only if the operator has already acknowledged (on the dashboard or
with ``kestrel capture enable``), exactly as the dashboard's capture switch, and
otherwise the panel asks for it on the dashboard.

"Starting capture..." and "Stopping capture..." clear as soon as capture
reaches that state (``set_capturing``), with a time limit as a backstop, so a
message never outlives what it describes.

Keys are read with gpiozero (``python3-gpiozero`` is in the image), imported
only when the keys are set up, so this module imports off a Pi. Callbacks only
queue events; a worker thread acts on them.
"""

import functools
import queue
import threading
import time

INFO_SECONDS = 30
CONFIRM_SECONDS = 5
PROMPT_SECONDS = 6
PENDING_SECONDS = 60        # backstop for "Starting/Stopping capture..."
DEFAULT_PINS = "5,6,13,19"  # BCM pins of KEY1-KEY4 on the Waveshare 2.7" HAT
POWER = {3: ("reboot", "reboot"), 4: ("shutdown", "shut down")}


def parse_pins(raw):
    """Four distinct BCM pins from ``"5,6,13,19"``; raises ValueError."""
    try:
        pins = [int(p) for p in str(raw or DEFAULT_PINS).split(",") if p.strip()]
    except ValueError:
        raise ValueError("pins must be four numbers, e.g. 5,6,13,19") from None
    if len(pins) != 4 or len(set(pins)) != 4 or not all(0 <= p <= 27 for p in pins):
        raise ValueError("pins must be four different BCM numbers (0-27)")
    return pins


class HatKeys:
    """The key state and worker. ``options`` returns the plugin's live options,
    ``apply`` is ``context.apply``."""

    def __init__(self, options, apply, logger):
        self._options = options
        self._apply = apply
        self.log = logger
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._events = queue.Queue()
        self._seq = 0
        self._screen_until = 0.0
        self._prompt = None
        self._prompt_until = 0.0
        self._armed = None            # (key, action, until)
        self._pending = None          # capture state a "...ing capture" prompt waits for
        self._capturing = None
        self.last = "none yet"
        self._buttons = []
        self._held = {}
        self._worker = threading.Thread(target=self._run, name="eink-keys", daemon=True)

    # -- lifecycle --------------------------------------------------------------
    def start(self):
        """Set up the keys and the worker. Returns "" or why the keys are
        unavailable (no HAT, no GPIO); the worker runs either way."""
        self._worker.start()
        try:
            hold = self._hold_seconds()
            for key, pin in enumerate(parse_pins(self._opt("pins")), start=1):
                button = self._make_button(pin, hold)
                button.when_pressed = functools.partial(self._on_pressed, key)
                button.when_held = functools.partial(self._on_held, key)
                button.when_released = functools.partial(self._on_released, key)
                self._buttons.append(button)
        except Exception as exc:  # noqa: BLE001 - no HAT or no GPIO: say why
            self._close_buttons()
            self.last = "keys unavailable: %s" % exc
            self.log.info("eink: HAT keys unavailable (%s)", exc)
            return self.last
        return ""

    def stop(self):
        self._stop.set()
        self._events.put(None)
        self._close_buttons()
        if self._worker.is_alive():
            self._worker.join(timeout=5)

    def _close_buttons(self):
        for button in self._buttons:
            try:
                button.close()
            except Exception:  # noqa: BLE001
                pass
        self._buttons = []

    def _opt(self, key, default=None):
        return (self._options() or {}).get(key, default)

    def _hold_seconds(self):
        try:
            return max(1, int(self._opt("hold_seconds", 3)))
        except (TypeError, ValueError):
            return 3

    def _make_button(self, pin, hold):
        """A gpiozero Button on ``pin`` (active low, internal pull-up)."""
        from gpiozero import Button

        return Button(pin, pull_up=True, bounce_time=0.05, hold_time=hold)

    # -- key callbacks (gpiozero's thread: record and return) ------------------
    def _on_pressed(self, key):
        self._held[key] = False

    def _on_held(self, key):
        self._held[key] = True
        self._events.put(("long", key))

    def _on_released(self, key):
        if not self._held.get(key):
            self._events.put(("short", key))
        self._held[key] = False

    def _run(self):
        while not self._stop.is_set():
            event = self._events.get()
            if event is None:
                return
            try:
                self.handle(event[0], event[1])
            except Exception:  # noqa: BLE001 - a bad press never stops the keys
                self.log.exception("eink: handling key %s failed", event)

    # -- what a press does ---------------------------------------------------------
    def _say(self, text, seconds=PROMPT_SECONDS):
        with self._lock:
            self._prompt = text
            self._prompt_until = time.monotonic() + seconds
            self._seq += 1

    def handle(self, kind, key, now=None):
        """Act on one press: ``kind`` is "short" or "long", ``key`` 1-4."""
        now = time.monotonic() if now is None else now
        with self._lock:
            self.last = "KEY%d (%s)" % (key, kind)
            armed = self._armed
        if armed and armed[0] == key and now < armed[2] and kind == "short":
            with self._lock:
                self._armed = None
            self._do(armed[1])
            return
        if key == 1 and kind == "short":
            with self._lock:
                self._screen_until = 0.0 if self._screen_until > now else now + INFO_SECONDS
                self._prompt_until = 0.0
                self._seq += 1
        elif key == 2:
            self._capture_key(kind, now)
        elif key in POWER:
            action, words = POWER[key]
            if kind == "short":
                self._say("Hold KEY%d to %s" % (key, words))
            elif not self._opt("allow_power", True):
                self._say("Reboot and shut down by button are off")
            else:
                self._arm(key, action, "Press KEY%d again to %s" % (key, words), now)
        with self._lock:
            self._seq += 1

    def _capture_key(self, kind, now):
        capturing = self._capturing
        if kind == "short":
            self._say("Hold KEY2 to stop capture" if capturing
                      else "Hold KEY2 to start capture")
        elif not self._opt("allow_capture", True):
            self._say("Capture by button is off")
        elif capturing is None:
            self._say("Capture state not known yet; try again")
        elif capturing:
            self._do("capture-stop")              # stopping is the safe direction
        else:
            # Starting transmits, so it takes a second press, like power.
            self._arm(2, "capture-start", "Press KEY2 again to start capture", now)

    def _arm(self, key, action, prompt, now):
        with self._lock:
            self._armed = (key, action, now + CONFIRM_SECONDS)
        self._say(prompt, CONFIRM_SECONDS)

    def _do(self, action):
        if action in ("capture-start", "capture-stop"):
            want = action == "capture-start"
            # No "ack" field: the helper starts capture only if the operator has
            # already acknowledged authorized use. A key never gives it.
            change = {"target": "capture", "enabled": want}
            doing = "Starting capture" if want else "Stopping capture"
        else:
            want = None
            change = {"target": "power", "action": action}
            doing = "Rebooting" if action == "reboot" else "Shutting down"
        reply = self._apply(change) or {}
        if reply.get("ok"):
            self.log.info("eink: %s by HAT key", doing.lower())
            with self._lock:
                self._pending = want
            self._say(doing + "...", PENDING_SECONDS if want is not None else 30)
        elif reply.get("need_ack"):
            self._say("Accept authorized use on the dashboard first")
        else:
            self.log.warning("eink: %s by HAT key failed: %s", doing.lower(),
                             reply.get("error"))
            self._say("Couldn't do that: %s" % (reply.get("error") or "no reply"))

    # -- what the panel and dashboard read ----------------------------------------
    def set_capturing(self, active):
        """The live capture state, from each UI update. Clears a "Starting/
        Stopping capture..." prompt as soon as capture gets there."""
        with self._lock:
            self._capturing = bool(active)
            if self._pending is not None and self._capturing == self._pending:
                self._pending = None
                self._prompt_until = 0.0
                self._seq += 1

    def state(self):
        """``{"seq", "screen", "prompt"}``: ``screen`` is "info" while the info
        screen is up, ``prompt`` a short line to show (or None). ``seq`` changes
        on every press, so the panel can redraw at once."""
        now = time.monotonic()
        with self._lock:
            return {"seq": self._seq,
                    "screen": "info" if now < self._screen_until else None,
                    "prompt": self._prompt if now < self._prompt_until else None}
