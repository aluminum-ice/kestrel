# Plugin libraries

Reference for the **shared plugin libraries**: code that several plugins use,
kept once in a `_lib_<name>.py` file beside the plugins instead of a copy in
each. Read [Writing a plugin](plugin-development.md) first; this page assumes
the plugin API and the rule that
[plugins are standalone](plugin-development.md#keep-plugins-standalone).

## What a library is

- **Plugin code, not core.** A library lives in the plugins directory
  (`builder/extra/files/plugins/` in the repository,
  `/usr/local/lib/kestrel/plugins/` on the device), never in `kestrel_pi`.
  Core never loads one; a plugin loads it when it asks for it, and
  `tests/test_core_lean.py` fails if core does.
- **No privileges of its own.** A library runs inside the plugin that loaded
  it, in the daemon, as `pi`, with that plugin's capabilities. Where it acts on
  the device (a capture or power change, say), it does so through the plugin's
  `context`, which checks everything as for the plugin itself.
- **Shipped like a plugin.** The image build and app-layer updates install
  every `plugins/*.py`, libraries included. The loader and `kestrel plugins`
  skip files whose name starts with `_`, so a library is never taken for a
  plugin, and no plugin may be named `lib`.

## Using a library

```python
from kestrel_pi import Plugin, load_library

alerts = load_library(__file__, "alerts", api=1)


class MyService(Plugin):
    __capabilities__ = ("network", "ui", "web")
    __options__ = {...}                       # your own literals, as always

    def on_loaded(self):
        self.alerts = alerts.Alerts(self, "my-service", self._deliver,
                                    alerts.state_dir("kestrel-my-service"))
```

- **`load_library(caller_file, name, api)`** returns the library module. Pass
  your own `__file__`, the library's name (`"alerts"` for `_lib_alerts.py`)
  and the major API version you wrote against. Calling it at module top level
  is fine: a library keeps hardware imports inside its functions, as a plugin
  does.
- **Where it is found:** beside the calling file first, then in the shipped
  plugins directory. So a shipped plugin always gets the shipped library, and
  an operator plugin in `/etc/kestrel/plugins/` uses the shipped one unless it
  brings its own copy (to stay on a version it has tested, say).
- **One module per file:** every plugin that loads the same library file gets
  the same module, loaded once. A shipped copy and an operator copy are two
  modules and don't affect each other.
- **Errors:** a missing library, an API mismatch or an import error raises
  `ImportError` (or the library's own exception), so your plugin fails to load
  and `kestrel plugins info` and the daemon log say why. Nothing is half-loaded.
- **Composition:** use a library's functions and classes from your plugin. Your
  plugin class still subclasses `Plugin` directly and declares everything
  itself; root reads those declarations without running any code, so a library
  can't supply them.

## Versions

Every library declares, as a plain literal near the top:

```python
LIB_API = (1, 0)   # (major, minor)
```

- **Minor** goes up when the library only adds (a new function, a new
  optional argument). Any minor is accepted.
- **Major** goes up when a change could break a plugin written for the
  previous one (a removed or renamed function, a changed signature or
  result). `load_library(..., api=N)` refuses a library whose major isn't `N`,
  and it reads `LIB_API` **before running any of the library's code**, so an
  incompatible library never runs. A major bump is called out in the release
  notes.
- **Major `0` is provisional:** the library may still change in a minor
  release while it waits for more users to show what it should look like. Pin
  it like any other and expect to follow its changes.

The shipped plugins and the libraries they use change together, in the same
release, so on a device they always agree. The version protects plugins
written outside this repository.

## Writing a library

- **Only for code at least two plugins really share.** One plugin's code stays
  in that plugin (and its `_<plugin>_*.py` helpers). A library is never a way
  to put code in core or to give a plugin something its capabilities don't.
- **Name** it `_lib_<name>.py`, `<name>` in lower-case letters, digits and
  `_`, and declare `LIB_API`.
- **Document it in the file:** a module docstring that says what it is for,
  what its version promises, and shows a short example of loading and using
  it. Name the public functions and classes without a leading underscore;
  anything with one is private and may change at any time.
- **Keep it importable off a Pi:** PIL, GPIO and drivers imported inside the
  functions that need them, never at the top.
- **Keep the rules it enforces in the library**, so every plugin gets them
  (for example, an input library never sends the authorized-use
  acknowledgment).
- A library may load another library with `load_library(__file__, ...)`. It
  can't load helpers: helpers belong to one plugin.
- **Test it once**, against the library module, and test each plugin for
  what the plugin adds (see [Testing](testing.md)).
- Add it to the table below, with its version, in the same change.

## Shipped libraries

The table lists every `_lib_*.py` file in `builder/extra/files/plugins/` and
its `LIB_API`; a test (`tests/test_load_library.py`) keeps the two in step.

| Library | File | API | Status | Used by |
| --- | --- | --- | --- | --- |
| `alerts` | `_lib_alerts.py` | 1.0 | stable | `ntfy`, `telegram` |
| `eink_face` | `_lib_eink_face.py` | 1.0 | stable | `eink`, `eink-ws213` |
| `hat_keys` | `_lib_hat_keys.py` | 0.1 | provisional | `eink` |

Each library has its own section below: what it is for, a minimal plugin using
it, its public functions and classes, the rules it enforces and what stays in
the plugin.

## `alerts`: alerts to a messaging service

`load_library(__file__, "alerts", api=1)`. Sends short alerts from the device
to one messaging service: a digest of new handshakes, a low battery or a
switch to battery power, a new release, capture stopping while it is switched
on, and (opt-in) the device coming online. It handles what to send, when, and
what happens when the device is offline; your plugin supplies the one request
that delivers a message. `ntfy` and `telegram` are built on it, and are the
best examples.

### A minimal plugin

```python
from kestrel_pi import Plugin, httpsafe, load_library

alerts = load_library(__file__, "alerts", api=1)


class Webhook(Plugin):
    __version__ = "0.1.0"
    __target_api__ = 2
    # network: it sends off the device, so enabling it takes the authorized-use
    # acknowledgment. capture-read: it counts new handshakes. ui, web: card, page.
    __capabilities__ = ("network", "capture-read", "ui", "web")
    __optional_requires__ = ("battery",)
    __actions__ = (
        {"label": "Status", "path": "index", "method": "get", "order": 1},
        {"label": "Send test", "path": "test", "method": "post", "order": 2},
    )
    __options__ = {
        "url": {"type": "str", "default": "", "secret": True},
        # the options the library reads (copy them as they are in ntfy.py):
        "events": {"type": "str", "default": "handshakes,battery,update,capture"},
        "digest_minutes": {"type": "int", "default": 30},
        "include_names": {"type": "bool", "default": False},
        "battery_percent": {"type": "int", "default": 20},
        "max_per_hour": {"type": "int", "default": 6},
        "quiet_hours": {"type": "str", "default": ""},
        "timeout": {"type": "int", "default": 15},
    }

    def on_loaded(self):
        self.alerts = alerts.Alerts(self, "webhook", self._deliver,
                                    alerts.state_dir("kestrel-webhook"))
        self.alerts.start()

    def on_unload(self):
        self.alerts.stop()

    def on_handshake(self, event):
        self.alerts.on_handshake(event)

    def on_internet_available(self):
        self.alerts.on_internet_available()

    def on_ui_setup(self, ui):
        self.alerts.on_ui_setup(ui)

    def on_ui_update(self, ui, state):
        self.alerts.on_ui_update(ui, state)

    def _deliver(self, requests, msg, timeout):
        # https_endpoint refuses an empty or non-https URL with UnsafeResponse.
        url = httpsafe.https_endpoint(self.options.get("url"), "")
        return requests.post(url, json={"title": msg["title"], "text": msg["body"]},
                             timeout=timeout, allow_redirects=False, stream=True)

    def on_webhook(self, path, request):
        if path == "test" and getattr(request, "method", "GET") == "POST":
            return self.alerts.send_test()
        rows = ["<li>URL: %s</li>" % ("set" if self.options.get("url") else "<b>not set</b>")]
        return self.context.page("Webhook alerts", self.alerts.status_page(
            "Webhook alerts", rows,
            token=(getattr(request, "args", {}) or {}).get("token") or ""))
```

### The contract

- **`Alerts(plugin, service, deliver, directory)`**: `service` names it in logs
  and errors; `directory` is where it keeps its state (use
  `state_dir("kestrel-<plugin>")`, which is `~/.local/state/kestrel-<plugin>/` of
  the daemon's account). Start it in `on_loaded`, stop it in `on_unload`, and
  forward `on_handshake`, `on_internet_available`, `on_ui_setup` and
  `on_ui_update` to it.
- **`deliver(requests, msg, timeout)`**: your plugin's one request. `msg` is
  `{"kind", "title", "body", "urgent", "ts"}`. Make the request with
  `allow_redirects=False` and `stream=True` and return the response; the
  library checks it through `httpsafe` and treats HTTP 400 and up as a failure.
  Raise `httpsafe.UnsafeResponse` if your settings are incomplete. Use
  `httpsafe.https_endpoint` for a configurable URL. If your service answers 200
  with an error in the body, check it and raise.
- **Options** are read live from `plugin.options`, which your plugin declares
  itself: `events` (any of `handshakes`, `battery`, `update`, `capture`,
  `online`), `digest_minutes`, `include_names`, `battery_percent`,
  `max_per_hour`, `quiet_hours` (local hours such as `22-07`) and `timeout`.
- **Pages:** `send_test()` returns the redirect for a Send test button;
  `status_page(title, setup_rows, token)` returns the status page body, with
  your own already-escaped `<li>` rows first.
- **Functions:** `state_dir(name)`, `message(kind, title, body, urgent=False)`,
  `handshake_digest(count, names, minutes, include_names)`,
  `in_quiet_hours(spec, hour)`, `clean_name(name)`, `read_json(path, default)`,
  `write_json(path, data)`; constants `EVENTS`, `DEFAULT_EVENTS`, `QUEUE_MAX`.

### Rules it keeps for you

- Messages are built from typed fields only (counts, a percentage, a release
  tag and, only when `include_names` is on, cleaned network names), so a
  hashline, an address, a location or a recovered password never ends up in
  one.
- Every send needs the plugin's `network` capability.
- A transport error is reduced to its type before it is logged or shown,
  because its text carries the URL and a credential may ride in it (Telegram's
  bot token is in the path).
- Quiet hours and the hourly limit hold ordinary alerts back; urgent ones (a
  low battery, capture stopping) get through. Up to `QUEUE_MAX` alerts wait in
  `queue.json` while the device is offline or the service fails, with backoff
  between attempts.

### What stays in your plugin

Its declarations (including the alert options above), its own settings, the
request in `_deliver`, its status-page setup rows, and anything particular to
the service (`ntfy` generates a private topic; `telegram` needs a bot token and
a chat). Declare `network`: the alerts leave the device, so enabling the
plugin takes the authorized-use acknowledgment. If your service is public by
default (a social network, say), only ever post privately, and keep
`include_names` off by default.

## `eink_face`: the Kestrel face for e-ink panels

`load_library(__file__, "eink_face", api=1)`. The animated "tamagotchi" face
the e-ink plugins draw, without anything that depends on a particular panel:
the faces and their voices, the mood ladder that picks one from live device
and learning-agent state, the motion estimate from GPS, the Kestrel mark, the
small glyphs of the status rows, text helpers and the address lookup. Your
plugin decides the layout (what goes where on its panel), brings its panel up
and redraws it. `eink` (2.7" HAT, 264x176, with keys) and `eink-ws213` (2.13"
HAT, 250x122, partial redraws) are built on it, and are the best examples.

### A minimal plugin

A face with no panel at all: it draws a frame on request and serves it as a
PNG from a **View** button on the Plugins page. A real plugin adds its panel
driver, a render thread that redraws on a timer and only when the frame
changed, and more rows (see `eink-ws213.py`).

```python
from kestrel_pi import Plugin, load_library

face = load_library(__file__, "eink_face", api=1)
W, H = 212, 104                          # your panel's size, landscape


class TinyFace(Plugin):
    __version__ = "0.1.0"
    __target_api__ = 2
    __capabilities__ = ("ui", "web")
    __optional_requires__ = ("agent", "battery", "position")
    __actions__ = ({"label": "View", "path": "display", "method": "get", "order": 1},)
    __options__ = {"faces": {"type": "dict"}, "voices": {"type": "dict"}}

    def on_loaded(self):
        self._state = None
        self._moods = face.MoodTracker()
        self._motion = face.MobilityTracker()

    def on_ui_update(self, ui, state):
        self._state = state                  # the latest device snapshot

    def on_handshake(self, event):
        state = self._state
        self._moods.handshake(state.epoch if state is not None else None)

    def frame(self):
        from PIL import Image, ImageDraw     # inside the method: no PIL at import

        state, opts = self._state, self.context.options
        fix = self.context.services.get("position") or (state.position if state else None)
        motion = self._motion.update(fix)
        mood = self._moods.decide(state, opts, motion)
        img = Image.new("1", (W, H), 255)
        draw = ImageDraw.Draw(img)
        draw.text((4, 4), face.face_glyph(mood, opts.get("faces")),
                  font=face.font(20), fill=0)
        small = face.font(10)
        voice = face.voice_for(mood, 0, opts.get("voices"))
        for i, line in enumerate(face.wrap_text(draw, voice, small, W - 8)[:2]):
            draw.text((4, 34 + 12 * i), line, font=small, fill=0)
        linked = {iface for iface, _ip in face.addresses()}
        face.draw_bt(draw, 4, H - 16, 8, 12, "bnep0" in linked)
        face.draw_wifi(draw, 18, H - 14, 14, 10, "wlan0" in linked)
        face.draw_kestrel(draw, W - 26, H - 26, 24, motion in face.MOVING)
        return img

    def on_webhook(self, path, request):
        return (face.png_bytes(self.frame()), 200,
                {"Content-Type": "image/png", "Cache-Control": "no-store"})
```

### What it provides

- **Moods.** `MoodTracker()` holds what changes from frame to frame (the
  agent's smoothed reward trend, a just-promoted model, the few seconds after a
  handshake, idle epochs, the look-left/right toggle).
  `decide(state, options, mobility, fault=False)` returns the mood for this
  frame, a key into `FACES`, from the plugin's last `UIState` and its live
  options (`battery_low_percent`, `bored_epochs`, `sad_epochs`); call
  `handshake(epoch)` from `on_handshake`. Underneath, `MoodInputs` and
  `decide_mood(inputs)` are the pure priority ladder (fault, power, milestone,
  handshake, agent posture, idle, base), usable on their own.
- **Faces and voices.** `FACES` and `VOICES` (mood to glyph, mood to phrases);
  `face_glyph(mood, overrides)` and `voice_for(mood, rot, overrides)` apply
  your plugin's `faces`/`voices` options (dict options an operator sets under
  `[plugins.<name>.faces]`); `voice_phrases(mood, overrides)` gives the whole
  list. Advance `rot` when the mood changes, not every frame, so the words vary
  without redrawing a still face.
- **Motion.** `MobilityTracker()`: `update(fix)` folds in a `position` service
  fix (or None) and returns `parked`, `walking`, `driving` or `unknown`
  (`.state` keeps it). Speed is timed by when each fix was taken
  (`fix_seconds(fix)`: its `ts`, else the `gps` plugin's `fix_time`), so
  reading the same fix again isn't a stop. Read the position service each
  frame, falling back to `state.position`, which the daemon refreshes only once
  an epoch. `MOVING`, `MOBILITY_LABEL`, `PARK_MS`/`WALK_MS`/`DRIVE_MS`,
  `classify_speed(speed)` and `haversine_m(a, b)` are there too.
- **The Kestrel mark.** `draw_kestrel(draw, x, y, size, moving)`: hovering when
  parked, stooping when moving. `KESTREL_HOVER`/`KESTREL_STOOP`,
  `flatten_svg_path` and `scaled_polygon` are the parts.
- **Glyphs**, each `(draw, x, y, w, h, ...)` in 1-bit: `draw_bt` and
  `draw_wifi` (slashed when the link is down), `draw_sat` (solid when locked)
  and `draw_sigbars` with `signal_level(cn0, locked)`, `draw_battery`,
  `draw_chip` (the agent), `draw_antenna` (capture), `draw_key` (handshakes),
  `draw_density`, and `draw_motion`/`draw_person`/`draw_car`.
- **Text.** `font(size)` (DejaVu, falling back to PIL's default), `text_w`,
  `text_right`, `wrap_text(draw, text, font, max_w)`, `fmt_uptime(seconds)`
  (`H:MM`, so a ticking clock doesn't redraw the panel every frame),
  `agent_label(agent)` (`off`/`baseline`/`learned`) and `as_float`.
- **Device facts.** `addresses()` lists the management links that have an
  IPv4 address (Bluetooth first), via `iface_ipv4(iface)`. Make your plugin's
  own `_addresses()` method call it, so tests and screenshots can substitute
  placeholder addresses. `png_bytes(image)` for a web mirror.

### Rules it keeps for you

- **The face never lies:** every mood maps to a real device or agent state.
  `smart` shows only while a *learned* policy is driving, never under the
  scripted baseline; with capture off the face sleeps, and a real event (a
  fault, a low battery, a handshake) still shows through.
- **No hardware or PIL at load:** PIL is imported inside the functions that
  draw or measure, so loading the library, and deciding moods, works on any
  host.

### What stays in your plugin

Its declarations (the face options, `faces` and `voices`, are declared by your
plugin like any other), its layout (fonts, sizes and where each row goes: the
two shipped plugins lay the same face out differently), its panel driver
(Waveshare's code ships as the plugin's own `_<plugin>_*.py` helpers), how and
when it redraws, any keys, the web mirror and the dashboard card, and the
start-up HTTPS fingerprint lines (`webtls.fingerprint()`), whose format
depends on the space you have. Keep importing your driver and PIL inside
methods. A panel plugin needs no `network` capability, so it needs no
authorized-use acknowledgment; if it drives keys that start capture, the rule
in [Keep plugins standalone](plugin-development.md#keep-plugins-standalone)
applies: a key never sends the acknowledgment.

## `hat_keys`: push-buttons on a HAT

`load_library(__file__, "hat_keys", api=0)`. **Provisional (API 0):** one HAT
uses it so far (the 2.7" e-Paper HAT's four keys in `eink`), so its API may
still change in a minor release when a second HAT with keys shows what really
varies. Pin `api=0` and expect to follow its changes until it reaches 1.

It reads a HAT's keys, turns presses into gestures and acts on them: your
plugin's own functions, or built-in actions that start or stop capture and
reboot or shut down, with the confirmation and safety rules kept for you. You
say how many keys there are, on which pins, and what each gesture does.

### A minimal plugin

A two-key HAT: a short press on KEY1 runs your function, a long press on KEY2
shuts down after a second press. It shows its prompts on its dashboard card; a
plugin with a display draws them on the panel instead (see `eink.py`).

```python
from kestrel_pi import Plugin, load_library

keys = load_library(__file__, "hat_keys", api=0)


class TwoKeys(Plugin):
    __version__ = "0.1.0"
    __target_api__ = 2
    __capabilities__ = ("ui",)
    __options__ = {
        "pins": {"type": "str", "default": "5,6",
                 "help": "BCM GPIO pins of KEY1 and KEY2."},
        "hold_seconds": {"type": "int", "default": 3},
        "allow_power": {"type": "bool", "default": True},
    }

    def on_loaded(self):
        self.presses = 0
        self._keys = None
        try:
            pins = keys.parse_pins(self.context.options.get("pins"), 2, "5,6")
        except ValueError as exc:
            self.context.health_note = "keys off: %s" % exc
            return
        self._keys = keys.Keys(
            "twokeys",
            [keys.Key("KEY1", pins[0], press=self._count),
             keys.Key("KEY2", pins[1], hold="shutdown")],
            apply=self.context.apply, logger=self.context.logger,
            options=lambda: self.context.options)
        note = self._keys.start()            # "" or why the keys are unavailable
        if note:
            self.context.health_note = note

    def _count(self):
        self.presses += 1
        return "Pressed %d times" % self.presses     # shown as the prompt

    def on_unload(self):
        if self._keys is not None:
            self._keys.stop()

    def on_ui_setup(self, ui):
        ui.add_element("prompt", "", label="Keys")

    def on_ui_update(self, ui, state):
        if self._keys is not None:
            self._keys.set_capturing(bool(state.capture_active))
            ui.set("prompt", self._keys.state()["prompt"] or self._keys.last)
```

### Gestures

- **press:** released before `hold_seconds` (your plugin's option, default 3).
  It fires at once, unless the key also binds `double`.
- **hold:** kept down for `hold_seconds`; it fires while the key is still down,
  and the release does nothing more.
- **double:** two presses within `DOUBLE_SECONDS` (0.4 s). Only a key that binds
  one waits to see if a second press follows, so every other key answers at
  once.
- **arm and confirm:** a hold arms the action and shows "Press KEY3 again to
  reboot"; a press of the same key within `CONFIRM_SECONDS` (5) carries it
  out. Pressing any other key, or waiting, cancels it. The built-in reboot,
  shutdown and capture start always work this way; your own function bound to
  `hold` does too when its `Key` has `confirm="<prompt>"`.
- A key that binds only a built-in `hold` answers a short press with a hint
  ("Hold KEY4 to shut down", "Hold KEY2 to start capture").

### The contract

- **`Key(name, pin, press=None, hold=None, double=None, confirm=None)`:** `name`
  is how the prompts say it ("KEY3"). A binding is a function of no arguments
  (run on the worker thread; a returned string is shown as the prompt) or, on
  `hold` only, a built-in: `"capture"` (stop at once while capturing; else arm
  a start), `"reboot"` or `"shutdown"` (arm).
- **`Keys(plugin_name, keys, apply, logger, options=None, source=None)`:**
  `apply` is `self.context.apply`; `options` returns your live options, of
  which it reads `hold_seconds`, `allow_capture` and `allow_power` (declare
  the ones you use). `start()` returns `""` or why the keys are unavailable
  (the worker runs either way); `stop()` in `on_unload`.
- **What you read and set:** `state()` is `{"seq", "prompt"}`: `prompt` is the
  line to show now (or None) and `seq` changes on every press and message, so a
  display can redraw at once. `last` is the last gesture ("KEY2 (long)").
  `set_capturing(active)` from `on_ui_update`, so the capture key knows which
  way to go and "Starting capture..." clears when capture has started.
  `say(text, seconds)` and `clear_prompt()` for your own messages.
- **Input:** by default a `GpioSource` on the keys' pins (gpiozero, imported
  only when the keys start; active low with the internal pull-up and a 50 ms
  debounce, as on Waveshare's HATs). For keys wired otherwise (active high, an
  I2C expander), pass `source=` any object with `start(on_down, on_up)`, which
  calls `on_down(index)`/`on_up(index)` for the key at that position in
  `keys`, and `close()`. The library times holds and double presses itself, so
  every source behaves the same.
- **Pins:** `parse_pins(raw, count, default)` checks a `pins` option: `count`
  different BCM numbers from 0 to 27, with a message fit for the dashboard.

### Rules it keeps for you

- The built-in actions bind to a hold only; `Keys` raises `ValueError` if one
  is bound to a press or a double press, so a brush against a key can't reboot
  the device.
- Rebooting, shutting down and starting capture always need the confirming
  press; stopping capture is immediate, since it is the safe direction.
  `allow_power` and `allow_capture` switch them off.
- Every action goes through your plugin's `context.apply`, the same checked path
  as the dashboard, so keys grant no new privilege.
- **A key never gives the authorized-use acknowledgment.** Starting capture
  sends `{"target": "capture", "enabled": true}` with no `ack`; the root helper
  starts capture only if the operator has already accepted it, and otherwise
  the prompt says to accept it on the dashboard first.
- Input callbacks only queue; a worker thread acts, and an action that fails is
  logged without stopping the keys.

### What stays in your plugin

Its declarations (including the key options above), which key does what, any
screen of its own (`eink`'s info screen on KEY1 is a plugin function), and
where the prompt is shown. A plugin that only reads keys needs no `network`
capability; the actions go through the same checks as the dashboard.

