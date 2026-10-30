"""Shared plugin library: push-buttons on a HAT.

Reads a HAT's keys, turns presses into gestures, and acts on them: your
plugin's own functions, or the built-in actions that start or stop capture and
reboot or shut the device down, with the confirmation and safety rules every
plugin needs kept here once. The ``eink`` plugin's four keys are built on it.
Load it with ``kestrel_pi.load_library`` (see
docs/developer/plugin-libraries.md)::

    from kestrel_pi import Plugin, load_library

    keys = load_library(__file__, "hat_keys", api=0)

    class MyHat(Plugin):
        def on_loaded(self):
            pins = keys.parse_pins(self.context.options.get("pins"), 2, "5,6")
            self._keys = keys.Keys(
                "my-hat",
                [keys.Key("KEY1", pins[0], press=self._next_page),
                 keys.Key("KEY2", pins[1], hold="shutdown")],
                apply=self.context.apply, logger=self.log,
                options=lambda: self.context.options)
            note = self._keys.start()      # "" or why the keys are unavailable

        def on_unload(self):
            self._keys.stop()

        def on_ui_update(self, ui, state):
            self._keys.set_capturing(bool(state.capture_active))

Gestures, per key: **press** (released before ``hold_seconds``), **hold** (kept
down for ``hold_seconds``; fires while still down), **double** (two presses
within ``DOUBLE_SECONDS``; only a key that binds one waits for a second press,
so the others answer at once) and **arm and confirm**: a hold arms the action
and shows "Press KEY3 again to reboot", and a press of the same key within
``CONFIRM_SECONDS`` carries it out. Pressing any other key, or waiting, cancels
it.

Rules it keeps for every plugin, whatever the bindings say:

- The built-in actions (``capture``, ``reboot``, ``shutdown``) bind to a hold
  only; :class:`Keys` refuses them on a press or a double press.
- Rebooting, shutting down and starting capture always need the confirming
  press. Stopping capture is immediate (it is the safe direction).
- Every action goes through the plugin's ``context.apply``, the same checked
  path as the dashboard, so keys grant no new privilege.
- Starting capture sends ``{"target": "capture", "enabled": true}`` with **no**
  ``ack``: the root helper starts capture only if the operator has already
  given the authorized-use acknowledgment, and a key never gives it.
- Input callbacks only queue; a worker thread acts, and a failing action never
  stops it.

``LIB_API`` is ``(0, ...)``: **provisional**. One HAT uses it so far, and the
API may still change in a minor release once a second HAT with keys shows what
really varies; pin ``api=0`` and expect to follow changes until it reaches 1.
Names with a leading underscore are private.
"""

import functools
import queue
import threading
import time

LIB_API = (0, 1)

CONFIRM_SECONDS = 5      # time to press again after an action is armed
PROMPT_SECONDS = 6       # how long a hint or message stays up
PENDING_SECONDS = 60     # backstop for "Starting/Stopping capture..."
POWER_SECONDS = 30       # how long "Rebooting..." / "Shutting down..." stays up
DOUBLE_SECONDS = 0.4     # the gap a double press must fall within
BUILTINS = ("capture", "reboot", "shutdown")
_GESTURES = ("press", "hold", "double")
_LAST_KIND = {"press": "short", "hold": "long", "double": "double"}
_POWER_WORDS = {"reboot": "reboot", "shutdown": "shut down"}
_COUNT_WORDS = {1: "one", 2: "two", 3: "three", 4: "four", 5: "five", 6: "six",
                7: "seven", 8: "eight"}
_STOP = object()


def parse_pins(raw, count, default):
    """``count`` distinct BCM pin numbers (0-27) from a ``"5,6,13,19"`` option
    value (``default`` when it is empty). Raises ValueError with a message fit
    for the dashboard card."""
    words = _COUNT_WORDS.get(count, str(count))
    try:
        pins = [int(p) for p in str(raw or default).split(",") if p.strip()]
    except ValueError:
        raise ValueError("pins must be %s numbers, e.g. %s" % (words, default)) from None
    if len(pins) != count or len(set(pins)) != count or not all(0 <= p <= 27 for p in pins):
        raise ValueError("pins must be %s different BCM numbers (0-27)" % words)
    return pins


class Key:
    """One key: its ``name`` as the prompts say it ("KEY3"), its BCM ``pin``,
    and what each gesture does. A binding is a built-in action name
    (``BUILTINS``, on ``hold`` only) or a function of no arguments, run on the
    worker, that may return a short line to show. ``confirm`` (a prompt such as
    "Press KEY1 again to clear") makes a function bound to ``hold`` arm and
    wait for a confirming press, like the built-ins."""

    def __init__(self, name, pin, press=None, hold=None, double=None, confirm=None):
        self.name = str(name)
        self.pin = pin
        self.press = press
        self.hold = hold
        self.double = double
        self.confirm = confirm


class GpioSource:
    """Keys on GPIO pins, read with gpiozero (imported only in :meth:`start`,
    so loading the library touches no GPIO): active low with the internal
    pull-up and a 50 ms debounce, as on Waveshare's HATs. A HAT whose keys are
    wired otherwise (active high, an I2C expander) passes its own source to
    :class:`Keys`: any object with ``start(on_down, on_up)``, which calls
    ``on_down(index)``/``on_up(index)`` for the key at that position, and
    ``close()``."""

    def __init__(self, pins, pull_up=True, bounce_time=0.05):
        self.pins = list(pins)
        self.pull_up = pull_up
        self.bounce_time = bounce_time
        self._buttons = []

    def start(self, on_down, on_up):
        try:
            for index, pin in enumerate(self.pins):
                button = self._make_button(pin)
                button.when_pressed = functools.partial(on_down, index)
                button.when_released = functools.partial(on_up, index)
                self._buttons.append(button)
        except BaseException:
            self.close()
            raise

    def _make_button(self, pin):
        from gpiozero import Button

        return Button(pin, pull_up=self.pull_up, bounce_time=self.bounce_time)

    def close(self):
        for button in self._buttons:
            try:
                button.close()
            except Exception:  # noqa: BLE001
                pass
        self._buttons = []


class Keys:
    """The keys of one HAT: gestures, actions, prompts and a worker thread.

    ``plugin_name`` starts each log line; ``keys`` is a list of :class:`Key`;
    ``apply`` is the plugin's ``context.apply``; ``options`` returns the
    plugin's live options, of which it reads ``hold_seconds`` (default 3),
    ``allow_capture`` and ``allow_power`` (default on). ``source`` defaults to
    a :class:`GpioSource` on the keys' pins; ``clock`` is for tests."""

    def __init__(self, plugin_name, keys, apply, logger, options=None, source=None,
                 clock=time.monotonic):
        self.keys = list(keys)
        names = [k.name for k in self.keys]
        if len(set(names)) != len(names):
            raise ValueError("key names must differ")
        for key in self.keys:
            for gesture in ("press", "double"):
                if getattr(key, gesture) in BUILTINS:
                    raise ValueError("%s: %r binds to a hold only, not a %s"
                                     % (key.name, getattr(key, gesture), gesture))
            for gesture in _GESTURES:
                binding = getattr(key, gesture)
                if binding is not None and binding not in BUILTINS and not callable(binding):
                    raise ValueError("%s: %s must be a function or one of %s"
                                     % (key.name, gesture, ", ".join(BUILTINS)))
            if key.confirm and not callable(key.hold):
                raise ValueError("%s: confirm needs a function bound to hold" % key.name)
        if source is None:
            pins = [k.pin for k in self.keys]
            if len(set(pins)) != len(pins):
                raise ValueError("each key needs its own pin")
            source = GpioSource(pins)
        self.plugin_name = plugin_name
        self._apply = apply
        self.log = logger
        self._options = options or (lambda: {})
        self._source = source
        self._clock = clock
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._events = queue.Queue()
        self._seq = 0
        self._prompt = None
        self._prompt_until = 0.0
        self._armed = None            # (key index, action, until)
        self._pending = None          # capture state a "...ing capture" prompt waits for
        self._capturing = None
        self._down = {}               # key index -> when it went down
        self._held = set()            # keys down long enough to have fired hold
        self._first_press = {}        # key index -> a press waiting for a second one
        self.last = "none yet"
        self._worker = threading.Thread(target=self._run, name=plugin_name + "-keys",
                                        daemon=True)

    # -- lifecycle --------------------------------------------------------------
    def start(self):
        """Start the worker and the input source. Returns "" or why the keys are
        unavailable (no HAT, no GPIO); the worker runs either way."""
        self._worker.start()
        try:
            self._source.start(self._on_down, self._on_up)
        except Exception as exc:  # noqa: BLE001 - no HAT or no GPIO: say why
            self.last = "keys unavailable: %s" % exc
            self.log.info("%s: HAT keys unavailable (%s)", self.plugin_name, exc)
            return self.last
        return ""

    def stop(self):
        self._stop.set()
        self._events.put(_STOP)
        try:
            self._source.close()
        except Exception:  # noqa: BLE001
            pass
        if self._worker.is_alive():
            self._worker.join(timeout=5)

    # -- input (the source's thread: record and return) --------------------------
    def _on_down(self, index):
        self._events.put(("down", index, self._clock()))

    def _on_up(self, index):
        self._events.put(("up", index, self._clock()))

    def _run(self):
        while not self._stop.is_set():
            wait = self._next_deadline(self._clock())
            try:
                event = self._events.get(timeout=wait)
            except queue.Empty:
                event = None
            if event is _STOP:
                return
            try:
                if event is not None:
                    self.process(*event)
                self.tick(self._clock())
            except Exception:  # noqa: BLE001 - a bad press never stops the keys
                self.log.exception("%s: handling a HAT key failed", self.plugin_name)

    def _next_deadline(self, now):
        """Seconds until the next hold or double-press deadline (at most 1)."""
        hold = self._hold_seconds()
        times = [t + hold for i, t in self._down.items() if i not in self._held]
        times += [t + DOUBLE_SECONDS for t in self._first_press.values()]
        if not times:
            return 1.0
        return max(0.0, min(1.0, min(times) - now))

    # -- gestures (the worker; tests call these directly) ---------------------------
    def process(self, kind, index, now):
        """One input event: ``kind`` is "down" or "up" for the key at ``index``."""
        if not 0 <= index < len(self.keys):
            return
        if kind == "down":
            with self._lock:
                armed = self._armed
                if armed and armed[0] != index:
                    self._armed = None          # another key cancels a pending confirm
                    if now < armed[2]:          # ...and its "Press ... again" prompt
                        self._prompt_until = 0.0
                        self._seq += 1
            self._down[index] = now
            self._held.discard(index)
        elif kind == "up":
            if self._down.pop(index, None) is None:
                return
            if index in self._held:
                self._held.discard(index)
                return
            self._released(index, now)
        self.tick(now)

    def tick(self, now):
        """Fire the holds and the single presses whose time has come."""
        hold = self._hold_seconds()
        for index, since in list(self._down.items()):
            if index not in self._held and now - since >= hold:
                self._held.add(index)
                self._gesture("hold", index, now)
        for index, since in list(self._first_press.items()):
            if now - since >= DOUBLE_SECONDS:
                del self._first_press[index]
                self._gesture("press", index, now)

    def _released(self, index, now):
        key = self.keys[index]
        with self._lock:
            armed = self._armed
        if armed and armed[0] == index and now < armed[2]:
            with self._lock:
                self._armed = None
                self.last = "%s (short)" % key.name
            self._run_action(key, armed[1])
            with self._lock:
                self._seq += 1
            return
        if key.double is None:
            self._gesture("press", index, now)
        elif index in self._first_press:
            del self._first_press[index]
            self._gesture("double", index, now)
        else:
            self._first_press[index] = now      # wait to see if a second press follows

    def _gesture(self, gesture, index, now):
        key = self.keys[index]
        with self._lock:
            self.last = "%s (%s)" % (key.name, _LAST_KIND[gesture])
        binding = getattr(key, gesture)
        if binding is None:
            if gesture == "press" and key.hold in BUILTINS:
                self._hint(key)
        elif binding in BUILTINS:
            self._builtin(index, binding, now)
        elif gesture == "hold" and key.confirm:
            self._arm(index, binding, key.confirm, now)
        else:
            self._run_action(key, binding)
        with self._lock:
            self._seq += 1

    # -- what a gesture does --------------------------------------------------------
    def _hint(self, key):
        if key.hold == "capture":
            self.say("Hold %s to %s capture" % (key.name, "stop" if self._capturing else "start"))
        else:
            self.say("Hold %s to %s" % (key.name, _POWER_WORDS[key.hold]))

    def _builtin(self, index, action, now):
        name = self.keys[index].name
        if action == "capture":
            capturing = self._capturing
            if not self._opt("allow_capture", True):
                self.say("Capture by button is off")
            elif capturing is None:
                self.say("Capture state not known yet; try again")
            elif capturing:
                self._do("capture-stop")              # stopping is the safe direction
            else:
                # Starting transmits, so it takes a second press, like power.
                self._arm(index, "capture-start", "Press %s again to start capture" % name, now)
        elif not self._opt("allow_power", True):
            self.say("Reboot and shut down by button are off")
        else:
            self._arm(index, action, "Press %s again to %s" % (name, _POWER_WORDS[action]), now)

    def _arm(self, index, action, prompt, now):
        with self._lock:
            self._armed = (index, action, now + CONFIRM_SECONDS)
        self.say(prompt, CONFIRM_SECONDS)

    def _run_action(self, key, action):
        if callable(action):
            text = action()
            if text:
                self.say(str(text))
        else:
            self._do(action)

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
            self.log.info("%s: %s by HAT key", self.plugin_name, doing.lower())
            with self._lock:
                self._pending = want
            self.say(doing + "...", PENDING_SECONDS if want is not None else POWER_SECONDS)
        elif reply.get("need_ack"):
            self.say("Accept authorized use on the dashboard first")
        else:
            self.log.warning("%s: %s by HAT key failed: %s", self.plugin_name,
                             doing.lower(), reply.get("error"))
            self.say("Couldn't do that: %s" % (reply.get("error") or "no reply"))

    # -- options ----------------------------------------------------------------------
    def _opt(self, key, default=None):
        return (self._options() or {}).get(key, default)

    def _hold_seconds(self):
        try:
            return max(1, int(self._opt("hold_seconds", 3)))
        except (TypeError, ValueError):
            return 3

    # -- what the plugin reads and sets ------------------------------------------------
    def say(self, text, seconds=PROMPT_SECONDS):
        """Show ``text`` in the prompt for ``seconds``."""
        with self._lock:
            self._prompt = text
            self._prompt_until = self._clock() + seconds
            self._seq += 1

    def clear_prompt(self):
        with self._lock:
            self._prompt_until = 0.0
            self._seq += 1

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
        """``{"seq", "prompt"}``: ``prompt`` is a short line to show, or None.
        ``seq`` changes on every press and message, so a display can redraw at
        once."""
        now = self._clock()
        with self._lock:
            return {"seq": self._seq,
                    "prompt": self._prompt if now < self._prompt_until else None}
