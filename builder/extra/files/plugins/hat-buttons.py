"""hat-buttons - the four keys on the Waveshare 2.7" e-Paper HAT.

The HAT the ``eink`` plugin draws on has four push-buttons. This plugin gives
them jobs you want with no phone at hand:

=====  ===========================  ====================================
Key    Short press                  Long press (``hold_seconds``, 3 s)
=====  ===========================  ====================================
KEY1   info screen for 30 s         -
KEY2   (a hint)                     stop capture (never starts it)
KEY3   (a hint)                     reboot, after a second press
KEY4   (a hint)                     shut down, after a second press
=====  ===========================  ====================================

A long KEY3 or KEY4 press asks "press again to ..." on the panel; a second
press of the same key within 5 seconds confirms, so a press in a pocket can't
reboot or shut the device down. Capture can only be stopped here: turning it
on takes the authorized-use acknowledgment, which a button press can't give.

Actions go through the same root apply helper as the dashboard
(``context.apply``), which validates every request, so the buttons grant no
new privilege. The panel shows the info screen and prompts through the
``buttons`` service, which the ``eink`` plugin reads; neither plugin imports
the other.

The keys are read with gpiozero (``python3-gpiozero`` is in the image). The
pins default to BCM 5, 6, 13 and 19 and can be changed with ``pins``. With no
HAT, or no GPIO, the plugin stays loaded and says so in its health note.
"""

from __future__ import annotations

import functools
import queue
import threading
import time

from kestrel_pi import Plugin

INFO_SECONDS = 30
CONFIRM_SECONDS = 5
PROMPT_SECONDS = 6
ACTIONS = {3: ("reboot", "reboot"), 4: ("shutdown", "shut down")}


class HatButtons(Plugin):
    __plugin_name__ = "hat-buttons"
    __author__ = "kestrel"
    __version__ = "0.1.0"
    __license__ = "GPL3"
    __description__ = "Use the e-Paper HAT's four keys: info screen, stop capture, reboot, shut down."

    __target_api__ = 2
    # Local hardware only; a small dashboard card shows the last key press.
    __capabilities__ = ("ui",)
    __provides__ = ("buttons",)
    __options__ = {
        "pins": {"type": "str", "default": "5,6,13,19",
                 "help": "BCM GPIO pins of KEY1-KEY4, comma-separated. The defaults "
                         "suit the Waveshare 2.7\" e-Paper HAT."},
        "hold_seconds": {"type": "int", "default": 3,
                         "help": "How long a press counts as a long press."},
        "allow_power": {"type": "bool", "default": True,
                        "help": "Let a long KEY3/KEY4 press (then a second press) "
                                "reboot or shut down."},
        "allow_capture_stop": {"type": "bool", "default": True,
                               "help": "Let a long KEY2 press stop capture."},
    }

    # -- lifecycle --------------------------------------------------------------
    def on_loaded(self):
        self.log = self.context.logger
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._events = queue.Queue()
        self._seq = 0
        self._screen_until = 0.0
        self._prompt = None
        self._prompt_until = 0.0
        self._armed = None            # (key, action, until)
        self._capturing = None
        self._last = "none yet"
        self._buttons = []
        self._held = {}
        self._worker = threading.Thread(target=self._run, name="hat-buttons", daemon=True)
        self._worker.start()
        try:
            pins = self._pins()
            for key, pin in enumerate(pins, start=1):
                button = self._make_button(pin)
                button.when_pressed = functools.partial(self._on_pressed, key)
                button.when_held = functools.partial(self._on_held, key)
                button.when_released = functools.partial(self._on_released, key)
                self._buttons.append(button)
            self.context.health_note = ""
        except Exception as exc:  # noqa: BLE001 - no HAT or no GPIO: stay loaded, say why
            self._close_buttons()
            self.context.health_note = "buttons unavailable: %s" % exc
            self.log.info("hat-buttons: keys unavailable (%s)", exc)

    def on_unload(self):
        self._stop.set()
        self._events.put(None)
        self._close_buttons()
        self._worker.join(timeout=5)

    def _close_buttons(self):
        for button in self._buttons:
            try:
                button.close()
            except Exception:  # noqa: BLE001
                pass
        self._buttons = []

    def _pins(self):
        raw = str(self.options.get("pins") or "5,6,13,19")
        try:
            pins = [int(p) for p in raw.split(",") if p.strip()]
        except ValueError:
            raise ValueError("pins must be four numbers, e.g. 5,6,13,19") from None
        if len(pins) != 4 or len(set(pins)) != 4 or not all(0 <= p <= 27 for p in pins):
            raise ValueError("pins must be four different BCM numbers (0-27)")
        return pins

    def _hold_seconds(self):
        try:
            return max(1, int(self.options.get("hold_seconds", 3)))
        except (TypeError, ValueError):
            return 3

    def _make_button(self, pin):
        """A gpiozero Button on ``pin`` (active low, internal pull-up). Imported
        here, never at module load, so the plugin imports off a Pi."""
        from gpiozero import Button

        return Button(pin, pull_up=True, bounce_time=0.05, hold_time=self._hold_seconds())

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

    # -- the worker -------------------------------------------------------------
    def _run(self):
        while not self._stop.is_set():
            event = self._events.get()
            if event is None:
                return
            try:
                self.handle(event[0], event[1])
            except Exception:  # noqa: BLE001 - a bad press never stops the keys
                self.log.exception("hat-buttons: handling %s failed", event)

    def _say(self, text, seconds=PROMPT_SECONDS):
        with self._lock:
            self._prompt = text
            self._prompt_until = time.monotonic() + seconds
            self._seq += 1

    def handle(self, kind, key, now=None):
        """Act on one press: ``kind`` is "short" or "long", ``key`` 1-4."""
        now = time.monotonic() if now is None else now
        with self._lock:
            self._last = "KEY%d (%s)" % (key, kind)
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
            if kind == "short":
                self._say("Hold KEY2 to stop capture" if self._capturing
                          else "Start capture from the dashboard")
            elif not self.options.get("allow_capture_stop", True):
                self._say("Stopping capture by button is off")
            elif self._capturing is False:
                self._say("Capture is already off")
            else:
                self._do("capture-stop")
        elif key in ACTIONS:
            action, words = ACTIONS[key]
            if kind == "short":
                self._say("Hold KEY%d to %s" % (key, words))
            elif not self.options.get("allow_power", True):
                self._say("Reboot and shut down by button are off")
            else:
                with self._lock:
                    self._armed = (key, action, now + CONFIRM_SECONDS)
                self._say("Press KEY%d again to %s" % (key, words), CONFIRM_SECONDS)
        with self._lock:
            self._seq += 1

    def _do(self, action):
        if action == "capture-stop":
            change, doing = {"target": "capture", "enabled": False}, "Stopping capture"
        else:
            change = {"target": "power", "action": action}
            doing = "Rebooting" if action == "reboot" else "Shutting down"
        reply = self.context.apply(change) or {}
        if reply.get("ok"):
            self.log.info("hat-buttons: %s", doing.lower())
            self._say(doing + "...", 30)
        else:
            self.log.warning("hat-buttons: %s failed: %s", doing.lower(), reply.get("error"))
            self._say("Couldn't do that: %s" % (reply.get("error") or "no reply"))

    # -- what the panel and dashboard read ----------------------------------------
    def provide_buttons(self):
        """``{"seq", "screen", "prompt"}``: ``screen`` is "info" while the info
        screen is up, ``prompt`` a short line to show (or None). ``seq`` changes
        on every press, so the panel can redraw at once."""
        now = time.monotonic()
        with self._lock:
            return {"seq": self._seq,
                    "screen": "info" if now < self._screen_until else None,
                    "prompt": self._prompt if now < self._prompt_until else None}

    def on_ui_setup(self, ui):
        ui.add_element("last", "none yet", label="Last key press")

    def on_ui_update(self, ui, state):
        self._capturing = bool(getattr(state, "capture_active", False))
        with self._lock:
            ui.set("last", self._last)
