# Writing a plugin

A practical, complete guide to writing a third-party plugin for kestrel.
It covers the plugin contract, the hooks, the typed context a plugin talks to
the system through, the capability/permission model, the non-blocking dispatch
your code runs under, inter-plugin services, options and config, the UI/web
surfaces, and how to install and test a plugin.

Everything here is the current behaviour of the framework
(`builder/extra/files/kestrel_pi/kestrel_pi/`). For the broader system see
[Architecture](architecture.md); for the capture data your plugin reads see the
[Capture interface contract](capture-interface.md); for web pages see [Web UI
conventions](web-ui-conventions.md).

> **Porting from legacy pwnagotchi.** The API is modelled on legacy pwnagotchi:
> a plugin is a module with a `Plugin` subclass that overrides `on_*` hooks with
> the familiar names. It is **not** drop-in compatible, though. Several hooks
> take different arguments, because kestrel passes a typed event and a typed
> `self.context` where pwnagotchi passes its `agent` object (for example
> `on_handshake(self, event)` instead of `on_handshake(self, agent, filename,
> access_point, client_station)`, and `on_ready(self)` instead of
> `on_ready(self, agent)`; see [The hooks](#the-hooks)). A ported plugin needs
> its hook signatures and any `agent` calls updated, plus the declarations the
> loader reads. The legacy `@plugins.on("handshake")` module-function style is
> **not** supported, and the class must subclass `Plugin` directly with its
> declarations as plain literals (see [Declarations the loader
> reads](#declarations-the-loader-reads)).

---

## The shape of a plugin

A plugin is a single `.py` file that defines one `Plugin` subclass and overrides
the hooks it needs. Every hook is a no-op by default, so you implement only the
events you care about. Here is a complete, working plugin (this is
`session-stats`, lightly annotated):

```python
from kestrel_pi import Plugin   # same class as kestrel_pi.plugins.Plugin


class SessionStats(Plugin):
    __author__ = "you"
    __version__ = "0.1.0"
    __license__ = "GPL3"
    __description__ = "Per-session handshake/AP counters with a web view."

    __target_api__ = 1                 # the hook-API version you wrote against
    __capabilities__ = ("ui", "web")   # what this plugin uses
    __options__ = {}                   # no configurable options

    def on_loaded(self):
        self.log = self.context.logger
        self._handshakes = 0
        self._aps = set()

    def on_handshake(self, event):
        self._handshakes += 1
        if event.bssid:
            self._aps.add(event.bssid)

    def on_ui_setup(self, ui):
        ui.add_element("handshakes", "0", label="Handshakes", kind="number")
        ui.add_element("aps", "0", label="Unique APs", kind="number")

    def on_ui_update(self, ui):
        ui.set("handshakes", str(self._handshakes))
        ui.set("aps", str(len(self._aps)))
```

The daemon finds the `Plugin` subclass **defined in your module** (imported base
classes are ignored), assigns `self.context` and `self.options` before
`on_loaded`, and from then on dispatches hooks to your instance.

**Your plugin's name is its filename**, not its class name: `session-stats.py`
is the plugin `session-stats`, configured under `[plugins.session-stats]`. Keep
one `Plugin` subclass per file.

---

## Keep plugins standalone

**A plugin carries its own feature, all of it.** The core (`kestrel_pi`) stays
lean: it holds what the daemon, the learning agent and the shipped scripts run,
plus a few general services any plugin may use. It never holds code that exists
for particular plugins, and it never imports a plugin. This keeps the always-on
daemon small, means a plugin can be read, copied and changed on its own, and
lets you drop one file (and its helpers) into `/etc/kestrel/plugins/` without
touching the image.

That holds **even when two plugins need the same code.** The code never moves
into `kestrel_pi`. Instead, either each plugin carries its own copy, or, once
at least two plugins really share it, it becomes a **shared plugin library**:
a `_lib_<name>.py` file that lives with the plugins and that a plugin loads by
name (see [Shared plugin libraries](#shared-plugin-libraries) and the
[library reference](plugin-libraries.md)). A library is still plugin code:
core never loads one. The shipped plugins show both:

- `ntfy` and `telegram` send the same alerts, so the alert code (the digest,
  the offline queue, rate limits, quiet hours) is the `alerts` library. Each
  plugin holds only its settings, the request that delivers a message and its
  status page.
- `eink-ws27` and `eink-ws213` draw the same face on two different panels, so the
  face (moods, voices, motion, the Kestrel mark, glyphs) is the `eink_face`
  library. Each plugin holds its own options, layout, panel driver and redraw,
  and `eink-ws27` its HAT's four keys (on the `hat_keys` library). A plugin for
  another e-ink HAT loads
  `eink_face` and supplies its own layout and panel.
- `ups-ws` keeps only its options and how it finds and reads its board: the
  battery card, the time-to-shutdown estimate and the low-battery auto-shutdown
  rules are the `ups` library, and the INA219 chip driver the `ina219` library,
  so a plugin for another UPS HAT supplies a `probe`, a `read` and its pack.

What core offers every plugin, and you may use freely:

| Service | What it is |
| --- | --- |
| `Plugin`, `self.context` | the plugin API (this guide) |
| `kestrel_pi.load_helper` | load your own helper files (below) |
| `kestrel_pi.load_library` | load a shared plugin library (below) |
| `kestrel_pi.httpsafe` | how a plugin sends a credential: `https://` only, no redirects, capped reads |
| `kestrel_pi.webtls.fingerprint()`, `kestrel_pi.connectivity.MANAGEMENT_INTERFACES` | read-only facts about the device |

A test (`tests/test_core_lean.py`) fails if a module in `kestrel_pi` is used
only by plugins; when it fires, move that code into the plugin that needs it,
or into a shared plugin library if several do. It also fails if core loads a
library. A new general service (one any plugin could use, such as a policy every
credential-sending plugin must follow) is the exception, added to that test's
list on purpose.

**Copied rules still apply in every copy.** When you copy code that enforces a
rule, the copy enforces it too, and review checks it. In particular, a plugin
that lets a button, a key or any other input start capture sends
`{"target": "capture", "enabled": true}` with **no** `ack`: the root helper
starts capture only if the operator has already given the authorized-use
acknowledgment, and no plugin ever gives it on the operator's behalf. For a
HAT's keys, the `hat_keys` library keeps that rule (and the confirmation for
power and capture) for you; see the
[plugin library reference](plugin-libraries.md#hat_keys-push-buttons-on-a-hat).

### Helper files

A plugin is one file, and the loader loads it without registering it as an
importable module, so it can't `import` a neighbour. When a plugin needs more
than one file - most often third-party code that should stay exactly as
upstream ships it, such as a display driver - put the rest in **helper files**
beside the plugin:

- Name each one `_<plugin>_<name>.py`, in the same directory as the plugin. The
  `eink-ws27` plugin ships `_eink-ws27_epdconfig.py`, `_eink-ws27_epd2in7.py`
  and `_eink-ws27_epd2in7_V2.py` (Waveshare's driver, see
  [Vendored code](vendored-code.md)).
- The loader and `kestrel plugins` skip files whose name starts with `_`, so a
  helper is never taken for a plugin. The image build and app-layer updates
  install every `plugins/*.py`, helpers included.
- Load a helper with `kestrel_pi.load_helper` (available from the release that
  added it; older releases have no `load_helper`):

  ```python
  from kestrel_pi import Plugin, load_helper

  class MyDisplay(Plugin):
      def _driver(self):
          # Inside the method that needs it, so importing the plugin touches
          # no hardware. Load a helper's dependencies first.
          load_helper(__file__, "_my-display_epdconfig.py")
          return load_helper(__file__, "_my-display_epd2in13.py")
  ```

  Each helper is registered as `kestrel_plugin_helpers.<plugin>.<name>`, so the
  helpers of one plugin can import each other with relative imports
  (`from . import epdconfig`) and vendored code needs no edits. A helper is
  loaded once; later calls return the same module.
- **A helper belongs to one plugin.** `load_helper` loads only files beside the
  plugin that carry its own prefix, so plugins never share helpers. An operator
  copy of a plugin in `/etc/kestrel/plugins/` brings copies of its helpers.
  Code several plugins share goes in a library instead (next section).

### Shared plugin libraries

When at least two plugins need the same code, put it once in a **plugin
library** instead of a copy in each. A library is a file
`_lib_<name>.py` (`<name>` in lower-case letters, digits and `_`) in the
plugins directory, beside the plugins that use it. Like a helper, the loader
and `kestrel plugins` skip it, and the build and app-layer updates install
it. Unlike a helper, any plugin may load it:

```python
from kestrel_pi import Plugin, load_library

face = load_library(__file__, "eink_face", api=1)   # at module top level is fine

class MyDisplay(Plugin):
    ...
        mood = face.decide_mood(inputs)
```

- **Where it is found:** beside the calling plugin first, then in the shipped
  plugins directory (`/usr/local/lib/kestrel/plugins/`). A shipped plugin
  always gets the shipped library; your plugin in `/etc/kestrel/plugins/` uses
  the shipped library unless you put your own copy beside it.
- **Versions:** every library declares `LIB_API = (major, minor)` as a plain
  literal. `api=` is the major version you wrote against; a library with a
  different major is refused before any of its code runs, so your plugin fails
  to load with a clear message instead of breaking later. A minor bump only
  adds, so any minor is accepted. A library at major `0` is provisional and may
  still change in a minor release.
- **What a library is not:** it isn't core and grants nothing. It runs inside
  the plugin that loaded it, as that plugin, with its capabilities. Your
  plugin's declarations (`__options__`, `__capabilities__` and the rest) stay
  plain literals in your own class; a library can't supply them. Compose a
  library's classes and functions; don't subclass a library class as your
  plugin.
- **Writing one:** make a library only for code at least two plugins really
  share, keep its hardware imports lazy (inside functions) like a plugin's,
  and give it a module docstring that explains its use. A library may load
  another library with `load_library(__file__, ...)`, but not helpers. No
  plugin may be named `lib`: that name is reserved so `_lib_*.py` files can't
  clash with a plugin's helpers.

The shipped libraries, their API versions and how to use each are listed in
the [plugin library reference](plugin-libraries.md).

---

## The hook-API contract

The daemon publishes a single integer, `HOOK_API_VERSION` (currently **2**, in
`kestrel_pi/__init__.py`; it moved from 1 to 2 in Phase 5, when `on_ui_update`
gained its read-only `UIState` snapshot argument). A plugin declares the version it was written
against with `__target_api__`, and the loader compares them:

| Your `__target_api__` | Result |
| --- | --- |
| equal to the daemon's | loads normally |
| **older** than the daemon's | loads, with a warning (the daemon keeps hook signatures back-compatible within a major version) |
| **newer** than the daemon's | **refused** — disabled with a reason (the daemon can't honour a contract it doesn't implement) |
| not declared | loads, assuming the current version, with a warning |

`HOOK_API_VERSION` is bumped only on a **breaking change to a dispatched hook's
signature**. Additive changes (a new optional keyword on the UI element API, a
new capability name) don't bump it. Pin `__target_api__` to the version you
tested against.

---

## Declarations the loader reads

Set these as class attributes. All are optional except that you'll usually set
at least `__capabilities__`.

**Write them as plain literals in the class body of a class that subclasses
`Plugin` directly** (strings, numbers, tuples, lists, dicts, `True`/`False`/
`None`; no names, comprehensions or calls, and no reassigning them after the
class). Root reads them without importing your plugin: the `kestrel plugins`
CLI and the web page's settings helper run as root and parse the file with
`ast`, so nothing a plugin (or the `pi`-writable venv) contains ever runs as
root. The loader enforces the same rule, so a plugin root can't read fails to
load with an `unreadable declarations` reason and shows as `invalid` in
`kestrel plugins list`. This covers `__plugin_name__`, `__description__`,
`__version__`, `__target_api__`, `__capabilities__`, `__options__`,
`__provides__`, `__requires__` and `__optional_requires__`.

| Attribute | Type | Purpose |
| --- | --- | --- |
| `__target_api__` | `int` | The hook-API version you wrote against (see above). |
| `__capabilities__` | `tuple[str]` | What the plugin uses; a subset of the capability set (see [Permissions](#the-capabilitypermission-model)). |
| `__options__` | `dict` | Options schema `{name: {"type", "default", "required"}}` (see [Options](#options-and-configuration)). |
| `__provides__` | `tuple[str]` | Named services this plugin offers other plugins (see [Services](#inter-plugin-services)). |
| `__requires__` | `tuple[str]` | **Hard** service dependencies: the plugin is disabled-with-reason if no enabled plugin provides one. |
| `__optional_requires__` | `tuple[str]` | **Soft** dependencies: they order a provider ahead of you if present, but a missing one never blocks the load (you degrade at runtime). |
| `__actions__` | `tuple[dict]` | Up to two row buttons on the Plugins page (see [Row buttons](#row-buttons-__actions__)). |
| `__author__`, `__version__`, `__license__`, `__description__` | `str` | Informational metadata, surfaced on the status page. |

---

## The context: how a plugin reaches the system

A plugin never imports a global singleton. Everything it needs is on
`self.context` (a `PluginContext`), assigned before `on_loaded`:

| `self.context.…` | What it is |
| --- | --- |
| `logger` | A per-plugin `logging.Logger`. Also the conventional `self.log = self.context.logger`. |
| `options` | Your validated options dict (also `self.options`). |
| `capture` | A read-only [`CaptureInterface`](#reading-captures) over the capture store. |
| `connectivity` | The live [`ConnectivityState`](#connectivity) (`.online`, `.paths`, `.active_paths()`). |
| `services` | The [service registry](#inter-plugin-services) (`get`/`has`/`names`). |
| `require_capability(cap)` | The honest capability self-check (see below). |
| `health_note` | A short operator-facing string you can set to explain a degraded/idle mode; shown on the status page. Empty means nothing to report. |
| `apply(change)` | Send one change to the root apply helper (the same channel the web layer uses) and get its reply dict; for a [row button](#row-buttons-__actions__) that performs a privileged action. |
| `page(title, body, status=200)` / `redirect(message=None, error=False)` | Build the `on_webhook` result markers (see [Web pages](#web-pages-on_webhook)). |
| `esc(value)` | HTML-escape a value for a page body you render yourself. |
| `form_csrf` | The CSRF token for a POST form your own page renders; set by the web layer per request. |
| `web_authed` | `True` when the web login is on and the request is signed in; a page that shows sensitive data refuses to render it when this is `False`. |

Because the context is a plain object, a test constructs a fake one and drives
your plugin with no daemon and no hardware (see [Testing](#testing)).

### Reading captures

`self.context.capture` is a `CaptureInterface` onto
`/var/lib/kestrel/capture/`:

- `store_dir` — the store path.
- `hashline_files()` — every `*.hc22000` file (sorted).
- `read_events(path)` — parse a file into `HandshakeEvent`s.
- `captured_networks()` — unique APs across the whole store
  (`{"essid", "bssids", "handshakes"}`); this reads every file, so paginate the
  result rather than calling it on a hot path.
- `sidecar_path(capture_path, suffix)` — the path for a sidecar next to a
  capture (e.g. a `.gps.json`).

A `HandshakeEvent` has `path`, `essid` (`""` if hidden/unknown), `bssid`
(`AA:BB:CC:DD:EE:FF` or `""`), `type` (`"pmkid"`, `"eapol"`, `"unknown"`).

### Connectivity

`self.context.connectivity.online` is `True` when **either** management path
(Bluetooth PAN `bnep0` or Wi-Fi `wlan0`) has real upstream reachability — a
probe, not just link-up, so a tethered phone with no data reads as offline. The
capture dongle is never counted. `.active_paths()` lists the reachable ones.

---

## The hooks

Override only what you need. Hooks split into **lifecycle** (called directly by
the loader, in dependency order) and **events** (dispatched off the loop — see
[Non-blocking dispatch](#non-blocking-dispatch-and-threading)).

### Lifecycle

| Hook | When | Notes |
| --- | --- | --- |
| `on_loaded(self)` | once, after instantiation and configuration | Do setup here; `self.context`/`self.options` are set. A raise here quarantines the plugin (`failed`). |
| `on_ready(self)` | once, after **all** enabled plugins have loaded | Use it when you need another plugin's service to already be registered. |
| `on_unload(self)` | on disable or daemon stop | Release anything **you** acquired (threads, sockets). Services you registered are withdrawn for you. On daemon stop all plugins' `on_unload` calls run concurrently and share a 25 s budget; one still running then is abandoned (logged) so it can't stall a restart, so keep it short and bound any join or device wait. |
| `on_config_changed(self, config)` | your `[plugins.<name>]` table changed live | `config` is your new validated options. Return `True` if you applied it in place; a falsy return tells the loader a daemon restart is needed to apply it. |

### Events

| Hook | Signature | Fired when |
| --- | --- | --- |
| `on_handshake` | `(self, event)` | A new, de-duplicated hashline landed in the store (the store file grew). `event` is a `HandshakeEvent`. It does **not** fire for an AP merely re-seen producing an *identical* hashline — exact-line dedup means no store growth — so per-handshake side effects (e.g. the `gps` plugin's location sidecar, keyed by `event.bssid`) happen only on genuinely new material. The fix a later handshake tags is current at that moment, so a new handshake can geocode an AP an earlier fix-less one missed, overwriting any prior fix for that BSSID. |
| `on_internet_available` | `(self)` | A management path just reached real upstream (the offline→online edge). |
| `on_epoch` | `(self, epoch, info)` | Each timer tick. `epoch` is a monotonically increasing count; `info` is `{"epoch", "online", "paths"}`. |
| `on_webhook` | `(self, path, request)` | An HTTP request to `/plugins/<name>/<path>` (see [Web pages](#web-pages-on_webhook)). |
| `on_ui_setup` | `(self, ui)` | Register your readout elements (see [UI readout](#the-ui-readout-model)). |
| `on_ui_update` | `(self, ui)` | Refresh your readout values. Called immediately after start and every epoch. |

---

## Non-blocking dispatch and threading

This is the most important thing to understand as a plugin author. Legacy
pwnagotchi also runs hooks on background threads (the original starts a new
thread for every hook call; the community fork keeps one queue per plugin), but
nothing bounds those threads, notices a slow hook or sets aside a stuck plugin,
so a hung plugin's work silently piles up. kestrel runs event hooks on a
bounded worker pool off the main loop, with these guarantees:

- **Non-blocking.** Dispatch returns immediately; a slow or hung hook never holds
  up capture, connectivity, or other plugins.
- **Per-plugin serialization.** Your plugin's hooks never overlap each other — a
  single plugin runs one hook at a time — so your instance state isn't re-entered
  concurrently by the framework. Different plugins' hooks do run concurrently.
- **Isolation.** Every hook runs under try/except keyed by your plugin. An
  exception is logged and swallowed, recorded as your `last_error`, and the
  daemon stays up. It does **not** disable your plugin (only a raising *lifecycle*
  hook does that).
- **Soft deadline.** A hook running past `max_hook_seconds` (a daemon setting,
  default 30) is logged as an overrun. Python can't kill the thread, so your
  hook keeps running to completion, but the pool is bounded — don't rely on
  being force-stopped. A hook that overruns but **finishes** is only slow and is
  left alone; a hook still running past `hook_stuck_seconds` (default 300) has
  stopped returning, so the plugin is quarantined (marked `failed`, no longer
  dispatched) to stop a stuck hook tying up a worker for good. Set
  `hook_stuck_seconds = 0` to disable quarantine. The real fix for slow work is
  to do it on your own thread and return from the hook at once (see below).

What this means for you:

- **Don't block a hook forever.** A hook that may run long (a big upload, a
  remote sync) should hand the work to **your own worker thread** and return
  quickly. The `backup` plugin is the model: `on_loaded` starts a daemon thread,
  and `on_internet_available`/`on_epoch` just signal it.
- **Guard shared state you touch from your own threads.** The framework won't run
  two of your hooks at once, but if you start your own threads, you're
  responsible for locking state they share with your hooks (use a
  `threading.Lock`).
- **Keep per-hook work bounded** so you stay under `max_hook_seconds` and don't
  starve the pool.

The pool size is the daemon's `hook_workers`, the overrun deadline is
`max_hook_seconds`, and `hook_stuck_seconds` is how long a hook may run before it
is treated as stuck and its plugin quarantined (all under `[daemon]` in
config.toml).

---

## The capability/permission model

A plugin declares what it uses in `__capabilities__`, a subset of:

| Capability | Meaning |
| --- | --- |
| `capture-read` | Reads the capture store. |
| `network` | **Transmits data off the device.** |
| `location` | Uses or provides device location. |
| `ui` | Contributes a dashboard readout. |
| `web` | Serves its own web page via `on_webhook`. |

Two things to understand:

1. **It is an honest model, not a sandbox.** A Pi can't afford real isolation, so
   the declarations are auditable rather than enforced by a cage. Before a gated
   action, call `self.context.require_capability("network")`: if you declared it,
   it returns `True`; if you didn't, it returns `False`, logs a warning, and
   flags the violation on the status page — **but your code still runs**. Declare
   honestly and check before the action the capability names.

2. **`network` has a real consequence at enable time.** Because `network` means
   data leaves the device, enabling a plugin that declares it requires the
   operator to give the same **authorized-use acknowledgment** as capture. The
   other capabilities are informational (surfaced on the status page); `network`
   is the one that gates the enable.

   A plugin that sends a **credential** (an API key, a token, a login) uses
   `kestrel_pi.httpsafe`, as `wpa-sec` and `wigle` do: `https_endpoint()` refuses
   anything but `https://`, requests go with `allow_redirects=False` and
   `stream=True` (`requests` re-sends a `cookies=` value to wherever a redirect
   points), then `check_response()` refuses a redirect and `read_capped()` /
   `read_json()` read the body with a size cap, because a broken or hostile server
   could otherwise exhaust the daemon's memory, which every plugin shares.

```python
def _upload(self, data):
    if not self.context.require_capability("network"):
        return   # declared "network"? then this is a no-op guard; otherwise it's flagged
    ...  # do the upload
```

---

## Inter-plugin services

Plugins share data through a tiny named-service registry instead of importing
each other.

- **Provide** a service: declare it in `__provides__` and implement a
  `provide_<service>` method returning the current value. (Hyphens in a service
  name map to underscores in the method: a service `foo-bar` → `provide_foo_bar`.)
- **Require** it: declare it in `__requires__` (hard) or `__optional_requires__`
  (soft) and fetch the current value with `self.context.services.get("name")`.

Getters are called **lazily** each time, so the value is always current, and a
getter that raises yields `None` to the caller rather than propagating. The
loader orders providers to start before their consumers, and disables a plugin
whose **hard** requirement nothing enabled provides (a missing soft requirement
just means no ordering — you handle its absence at runtime).

The `gps` plugin provides `position`:

```python
class GPS(Plugin):
    __capabilities__ = ("location", "capture-read", "ui")
    __provides__ = ("position",)

    def provide_position(self):
        return self._last_fix   # or None when there's no fix
```

and `wigle` consumes it optionally, degrading when it's absent:

```python
class Wigle(Plugin):
    __optional_requires__ = ("position",)

    def _record(self):
        fix = self.context.services.get("position")
        if fix is None:
            ...  # no GPS: idle or geolocate-only mode
```

The services that ship with Kestrel:

| Service | Provided by | What it returns |
| --- | --- | --- |
| `position` | `gps` | The current fix (with signal readings), or `None` without one. |
| `battery` | the UPS plugins (`ups-ws`, `ups-wsc`, `ups-wsd`, `ups-wse`) | The battery reading: `percent`, `status` (charging, discharging, ...), `voltage`, `current_ma`, `power_w`. |
| `agent` | the daemon | The learning agent's published status, or `None` when it has published nothing. |
| `recovered` | the daemon | The recovered-networks store (known passphrases). |
| `geo` | the daemon | The network-location store. |
| `remote-training` | `remote-train` | The training host's state, without the host's details ([remote training](remote-training.md#the-remote-training-service)). |
| `census` | `census` | The fleet's totals: `online`, `active_today`, `active_7d_avg`, `versions`, `local_win_rate_7d`, `remote_win_rate_7d` (each `None` until the census is read). |

---

## Options and configuration

Declare an options schema in `__options__`; the loader validates the operator's
`[plugins.<name>]` table against it before your plugin loads:

```python
__options__ = {
    "endpoint":  {"type": "str",  "default": "https://example.org"},
    "api_key":   {"type": "str",  "required": True},
    "interval":  {"type": "int",  "default": 3600},
    "verbose":   {"type": "bool", "default": False},
}
```

- `type` is one of `str`, `int`, `float`, `bool`, `list`, `dict`. A declared
  option of the wrong type, or a `required` option with no value and no default,
  disables the plugin **with a reason** (it never crashes the daemon). `bool` is
  kept distinct from `int`.
- Defaults are applied; an undeclared key in the table is ignored with a warning.
- The validated values arrive as `self.options` (and `self.context.options`).
- `"secret": True` marks a value the CLI and the web page never show back
  (a password field, masked prompts).
- `"root_only": True` marks an option that **root acts on**, such as where
  the `updates` plugin's in-place update comes from (`host`, `repo`, `token`).
  It can be set only from the terminal (`sudo kestrel plugins enable <name>
  --set <option>=...`): the web page shows it read-only, and the root apply
  helper refuses it, because anything that can reach that socket runs as `pi`.
  Mark any option whose value a root process trusts.
- `"choices": [...]` makes a `str` or `int` option one of a fixed set:

  ```python
  "variant": {"type": "str", "default": "v4", "choices": ["v4", "v3", "v2"],
              "help": "The panel revision printed on the sticker on its back."},
  ```

  The Plugins page shows a dropdown, `kestrel plugins enable` lists the choices
  and asks again on anything else, and both the root helper and `--set` refuse
  a value outside them before writing `config.toml`; a stored value outside them
  disables the plugin with a reason, like a wrong type. Matching is exact
  (`"V4"` is not `"v4"`). The list is a plain literal of at least one value of
  the option's type (a `str` choice non-empty, with no comma or tab), and the
  `default` must be one of them; anything else and the plugin is refused at
  load. **Add `choices` only to a new option, or at a release that needs a
  reflash anyway:** an existing option's stored values may fall outside the
  list, and the plugin would stop on devices that update in place.

In config.toml, your options live under `[plugins.<name>]` alongside the
framework's own `enabled` flag. **Config is edited one key at a time** through the
config tool / dashboard (which re-parse and refuse a multi-key change) — never by
a file-wide `sed`, because several tables share key names like `enabled`.

**Secrets** (API keys, tokens) are collected at enable time and masked; they are
stored in config and never echoed back. Declare them as normal `str` options —
the enable flow and the web form handle the masking.

To apply an option change without a restart, implement `on_config_changed` and
return `True`.

---

## The UI readout model

A plugin contributes to the dashboard in one of two ways — **as data** (a readout
card) or by **owning a page** (`on_webhook`). It never injects markup into the
dashboard shell; the core renders and escapes your values.

For a readout card, register elements in `on_ui_setup` and update them in
`on_ui_update`. Each element has a `label`, a `value`, and a `kind` render hint:

```python
def on_ui_setup(self, ui):
    ui.add_element("charge", "0", label="Charge", kind="percent", mark=5)
    ui.add_element("state",  "-", label="State")        # kind defaults to "text"

def on_ui_update(self, ui):
    ui.set("charge", str(self._percent))
    ui.set("state", self._state)
```

- `kind` is one of `text`, `number`, `percent`, `gauge`. It's a hint: a renderer
  that doesn't understand one falls back to text (the web dashboard draws a bar
  for `percent`/`gauge`; the e-ink view shows plain text). The two views share
  this one state model but render independently.
- `mark` (0–100) on a `percent`/`gauge` element places a reference marker on the
  bar (e.g. a battery gauge marking its shutdown threshold).
- `ui.add_element(name, value)` and `ui.set(name, value)` are the minimal legacy
  calls; `label`/`kind`/`mark` are optional additions. A `set` before an `add`
  still shows (it creates a `text` element).

Your elements are namespaced to your plugin, so two plugins can both have a
`status` element without colliding.

---

## Web pages (`on_webhook`)

Declare the `web` capability and implement `on_webhook(self, path, request)` to
serve pages under `/plugins/<name>/` on the management link. `path` is the
subpath after `/plugins/<name>/`; `request` is the Flask request (GET or POST).

Return any of:

- a `str` (served as the body),
- a `(body, status)` or `(body, status, headers)` tuple,
- a Flask `Response`,
- a `self.context.page(title, body, status=200)` **marker** — the daemon wraps
  `body` in the shared page shell (house CSS, the Documentation footer), so a
  plugin page matches the rest of the UI without importing the web module. The
  shell also opens every such page with the standard links back,
  **"← Plugins · Dashboard"** (in that order and wording, keeping a token
  login's token), so start `body` with your `<h1>` and don't write those links
  yourself, or they show twice. Use `context.page()` for every page a person
  sees, error and "not found" pages included, so none is a dead end,
- a `self.context.redirect(message=None, error=False)` **marker** — the daemon
  sends the browser back to the Plugins page with a one-line banner (`message`,
  shown red when `error`); use it after a POST action,
- `None` → an empty `204`.

```python
def on_webhook(self, path, request):
    if path == "stats.json":
        return (json.dumps(self._snapshot()), 200,
                {"Content-Type": "application/json"})
    return self.context.page("My plugin - kestrel",
                             "<h1>my plugin</h1>")
```

A raising `on_webhook` is caught and turned into a 500 — it never takes the
daemon down. Any HTML you serve must follow the [Web UI
conventions](web-ui-conventions.md): server-rendered, dependency-free, mobile
first, dark-mode aware, CSRF-protected on writes, and never echo a secret. The
server already binds to the management interfaces only. Escape with
`self.context.esc(...)`; for a page that shows sensitive data, render it only
when `self.context.web_authed` is true (otherwise show a login notice).

**Don't block in `on_webhook`.** It runs on the request thread, so a page that
does slow or open-ended work (a network call, an SSH/rsync, a long subprocess)
holds that request open. Render from already-collected state, and send any
privileged or slow action through `self.context.apply` — the built-in targets
that take a while are deferred and return at once (see the reference plugins).
As a backstop, the handler runs under a soft deadline (`[web]
webhook_timeout_seconds`, default 30s, `0` disables): past it the browser gets a
timeout page instead of hanging, and your call is left to finish in the
background. The deadline frees the *request*, not the worker — Python can't kill
a stuck thread — so a page that hangs every time still leaks one thread per hit;
treat the timeout as a safety net, not a substitute for returning promptly.

---

## Row buttons (`__actions__`)

A plugin can place **up to two** buttons on its own row of the Plugins page.
Declare them in `__actions__` (a tuple of dicts); each routes to your
`on_webhook` at `/plugins/<name>/<path>`:

```python
class MyPlugin(Plugin):
    __capabilities__ = ("web",)
    __actions__ = (
        {"label": "Show",  "path": "view",   "method": "get"},
        {"label": "Run",   "path": "run",    "method": "post", "order": 2},
    )
```

Each action is `{"label", "path", "method"?, "order"?, "confirm"?}`:

| Key | Meaning |
| --- | --- |
| `label` | The button text (required; HTML-escaped by core). |
| `path` | The `on_webhook` subpath it routes to (required). |
| `method` | `"get"` (default) navigates to a page you render; `"post"` performs an action. |
| `order` | Optional int; buttons sort by `order`, then declaration order. |
| `confirm` | Optional bool (reserved for a client-side confirm hint). |

Core shows the buttons only for an **enabled** plugin with the **web login on**,
caps the list at two, and handles the plumbing: a **GET** button is plain
navigation (your page gates its own sensitive data on `self.context.web_authed`);
a **POST** button is a privileged state change, so core validates the CSRF token
and refuses it when the login is off *before* calling you — you never check
either yourself. A page you render that needs its own POST form (a confirm step)
embeds `self.context.form_csrf` as a hidden `csrf` field.

To vary the live set — hide a button until a prerequisite is met — override
`webhook_actions(self)` (it returns `self.__actions__` by default):

```python
def webhook_actions(self):
    if not self._configured():
        return ()          # nothing until the host/key are set up
    return self.__actions__
```

A POST action typically ends by sending a change through `self.context.apply`
and returning a `redirect`:

```python
def on_webhook(self, path, request):
    if path == "run":
        reply = self.context.apply({"target": "my-thing", "action": "go"})
        if reply.get("ok"):
            return self.context.redirect("Started.")
        return self.context.redirect(reply.get("error") or "not applied",
                                     error=True)
    return self.context.redirect("No such action.", error=True)
```

`self.context.apply` reaches the same root helper the web layer uses; it
validates every change against a fixed target allow-list, so exposing it to a
plugin grants no new privilege. A re-checkable prerequisite (e.g. an SSH key
only the daemon can see) is re-asserted in `on_webhook` too, not just in
`webhook_actions`, so a stale or crafted POST is refused.

---

## Lifecycle, failure, and the status page

The loader scans the search paths, imports each module, and for every plugin
enabled in config: checks `__target_api__`, validates options, resolves
`requires`/`provides` and orders startup (providers first), instantiates with the
context, then calls `on_loaded` (in order) and `on_ready` (once all are loaded).

Anything that fails a check becomes a record with a state and a human reason,
surfaced on the status page — never a daemon crash:

- **`loaded`** — running.
- **`disabled`** — not enabled, a bad/missing required option, a missing hard
  service, or an incompatible target API. The `reason` says which.
- **`failed`** — declarations root can't read statically (see above), an
  import error, or a raising `on_loaded`/`on_ready`. The plugin is quarantined;
  the rest run on.

An exception in an *event* hook is logged and recorded as `last_error` but does
not change your state. Overruns and the last hook/time are tracked per plugin.

---

## Installing and enabling your plugin

Plugins are discovered from two directories, **shipped first, operator second**,
and an operator plugin **overrides** a shipped one of the same name:

- `/usr/local/lib/kestrel/plugins/` — the shipped reference plugins.
- `/etc/kestrel/plugins/` — **your plugins go here.** A file here named the
  same as a shipped plugin takes precedence, so you can override a reference
  plugin without touching the image.

Drop `my-plugin.py` in the operator directory and enable it from the dashboard
**Plugins** page (with the login on) or the terminal:

```bash
sudo kestrel plugins enable my-plugin                # walks the options; secrets hidden
printf '%s\n' "$KEY" | sudo kestrel plugins enable my-plugin --set-stdin api_key   # scripted
sudo kestrel plugins info my-plugin     # capabilities, options, deps, health
```

A plugin that declares `network` prompts for the authorized-use acknowledgment on
first enable. Files whose name starts with `_` are skipped: they are plugins'
[helper files](#helper-files), so copy a plugin's `_<plugin>_*.py` helpers along
with it.

---

## Testing

The context is a plain object precisely so you can unit-test a plugin with no
daemon and no hardware: construct a fake context (or a minimal stand-in exposing
`logger`, `options`, `capture`, `connectivity`, `services`, `require_capability`),
assign it to an instance, and call the hooks directly. The framework's own tests
live in `builder/extra/files/kestrel_pi/tests/` and run with:

```bash
make test        # the venv's pytest (needs flask/requests)
```

A plugin that uses a library needs no special setup in a test: loaded from a
checkout, `load_library` finds the library beside the plugin file. Patch a
library's globals on the library module (the one `load_library` returns), not
on the plugin module. See [Testing](testing.md).

`make lint` byte-compiles the package and plugins; keep the target Python at
**3.11** (no 3.12+ syntax).

---

## Checklist

- One `Plugin` subclass per file, subclassing `Plugin` directly; the
  **filename** is the plugin name.
- Keep the plugin standalone: its feature lives in its own file, its own
  `_<plugin>_*.py` helpers (loaded with `load_helper`) and any shared plugin
  libraries it loads with `load_library`. Never move plugin code into
  `kestrel_pi`; use a library (or a copy) instead.
- Using a library? Pin its major version with `api=` and check the
  [library reference](plugin-libraries.md) for what it guarantees.
- Write every declaration as a plain literal in the class body.
- Set `__target_api__` to the version you tested against.
- Declare `__capabilities__` honestly; call `require_capability` before a gated
  action; expect the `network` ack at enable time.
- Declare `__options__` with types and defaults; read `self.options`.
- Never block an event hook — offload long work to your own thread and lock
  shared state.
- Contribute to the dashboard as data (`on_ui_*`) or own a page (`on_webhook`);
  never inject markup; follow the web conventions for any page. Return pages
  with `context.page()` and start them with an `<h1>`: the shell adds the
  "← Plugins · Dashboard" links above it.
- For a row button, declare `__actions__` (at most two), route it to
  `on_webhook`, and gate a POST action's prerequisite in `webhook_actions` *and*
  re-check it in `on_webhook`; send privileged changes through
  `self.context.apply` and return a `page`/`redirect` marker.
- Release your own resources in `on_unload`.
