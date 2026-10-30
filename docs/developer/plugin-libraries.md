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
