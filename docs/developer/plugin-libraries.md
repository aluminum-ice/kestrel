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

None ship yet. Each library gets its own section here when it is added: what it
is for, a minimal plugin using it, its public functions and classes, the rules
it enforces and what stays in the plugin.
