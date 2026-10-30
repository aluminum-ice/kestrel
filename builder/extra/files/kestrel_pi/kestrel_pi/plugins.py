"""The plugin API surface: the ``Plugin`` base class and the hook catalogue.

This is modelled on legacy pwnagotchi's plugin API: a plugin is a module that
defines a ``Plugin`` subclass with no-op ``on_*`` methods it overrides, under
the familiar hook names. It is not drop-in compatible: several hooks take
different arguments (a typed event, and ``self.context`` in place of
pwnagotchi's ``agent`` object), so a ported plugin needs its hook signatures
and any ``agent`` calls updated. The legacy ``@plugins.on`` decorator style is
not supported, and declarations must be plain literals in the class body
(:mod:`.declarations`): root reads them without importing the plugin. The
declarations the loader reads are ``__target_api__``, ``__capabilities__``,
``__options__`` and ``__requires__``/``__provides__``.
"""

from __future__ import annotations

import ast
import hashlib
import importlib.util
import os
import re
import sys
import threading
import types

# The fixed capability set (improvement 3). A plugin declares a subset in
# ``__capabilities__``; anything that leaves the device ("network") is gated
# behind the authorized-use acknowledgment at enable time.
CAPABILITIES = ("capture-read", "network", "location", "ui", "web")

# Capabilities whose use transmits data off the device. Declaring one of these
# makes enabling the plugin require the authorized-use acknowledgment.
DEVICE_LEAVING_CAPABILITIES = ("network",)

# The hooks the daemon dispatches. ``on_loaded``/``on_ready``/``on_unload``/
# ``on_config_changed`` are lifecycle (fired directly by the loader); the rest
# are event hooks dispatched off the loop.
LIFECYCLE_HOOKS = ("on_loaded", "on_ready", "on_unload", "on_config_changed")
EVENT_HOOKS = (
    "on_handshake",
    "on_internet_available",
    "on_epoch",
    "on_webhook",
    "on_ui_setup",
    "on_ui_update",
)
ALL_HOOKS = LIFECYCLE_HOOKS + EVENT_HOOKS


class Plugin:
    """Base class for kestrel plugins.

    Subclass it, set the declaration attributes, and override the ``on_*``
    hooks you care about. Every hook is a no-op by default, so a plugin only
    implements the events it needs. The daemon hands each instance a
    :class:`~kestrel_pi.context.PluginContext` as ``self.context`` before
    ``on_loaded`` is called; reach the rest of the system only through it.
    """

    # Metadata (legacy-compatible, informational).
    __author__ = "unknown"
    __version__ = "0.0.0"
    __license__ = "GPL3"
    __description__ = ""

    # Improvement 4: the hook-API version this plugin was written against.
    __target_api__ = 1
    # Improvement 3: declared capabilities, a subset of CAPABILITIES.
    __capabilities__: tuple = ()
    # Improvement 5: options schema {name: {"type", "default", "required"}}.
    __options__: dict = {}
    # Improvement 2: named services required from / provided to other plugins.
    # ``__requires__`` is a hard dependency: the loader disables-with-reason a
    # plugin whose required service no enabled plugin provides.
    # ``__optional_requires__`` is a soft dependency: it still orders a provider
    # that IS enabled ahead of this plugin, but a missing one never blocks the
    # load - the plugin is expected to cope at runtime (e.g. a degraded mode).
    __requires__: tuple = ()
    __optional_requires__: tuple = ()
    __provides__: tuple = ()
    # Up to two optional buttons the plugin places on its row on the Plugins
    # page (shown only for an enabled plugin, when web auth is on). Each entry
    # is a dict ``{"label": str, "path": str, "method": "get"|"post" (default
    # "get"), "order": int (optional), "confirm": bool (optional)}``. A button
    # routes to this plugin's own ``on_webhook(path, request)`` at
    # ``/plugins/<name>/<path>`` - a GET navigates to a page the plugin renders,
    # a POST performs an action (core adds the CSRF token). At most two are
    # shown, ordered by ``order`` then declaration order. Override
    # :meth:`webhook_actions` to vary the live set (e.g. hide a button until a
    # prerequisite is met).
    __actions__: tuple = ()

    def __init__(self):
        # The loader assigns these before on_loaded. Declared here so a plugin
        # (and the type checker) can rely on them existing.
        self.context = None
        self.options = {}
        self.name = self.__class__.__module__

    # -- lifecycle -------------------------------------------------------
    def on_loaded(self):
        """Called once after the plugin is instantiated and configured."""

    def on_ready(self):
        """Called once after every enabled plugin has loaded."""

    def on_unload(self):
        """Called when the plugin is disabled or the daemon stops.

        Release anything the plugin acquired itself here; resources taken
        through ``self.context`` (threads, web routes, store watchers) are torn
        down by the daemon automatically.
        """

    def on_config_changed(self, config):
        """Called when the plugin's ``[plugins.<name>]`` table changed live.

        ``config`` is the plugin's new validated options dict. Return ``True``
        if the change was applied in place; a falsy return tells the loader a
        daemon restart is needed to apply it.
        """
        return False

    # -- events ----------------------------------------------------------
    def on_handshake(self, event):
        """A new hashline landed in the capture store. ``event`` is a
        :class:`~kestrel_pi.capture.HandshakeEvent` (path/essid/bssid/type).
        """

    def on_internet_available(self):
        """A management path (BT PAN or Wi-Fi) just reached real upstream."""

    def on_epoch(self, epoch, info):
        """Timer tick. ``epoch`` is the monotonically increasing tick count and
        ``info`` a dict of daemon state."""

    def on_webhook(self, path, request):
        """An HTTP request for ``/plugins/<name>/<path>``. Return a string,
        a (body, status) / (body, status, headers) tuple, a Flask response, or
        a :class:`PluginPage` / :class:`PluginRedirect` marker (the daemon wraps
        a ``PluginPage`` in the shared page shell and turns a ``PluginRedirect``
        into a redirect back to the Plugins page with a one-line banner).
        """

    def webhook_actions(self):
        """The row buttons to show right now - by default the static
        ``__actions__``. Override to vary the live set, e.g. hide a button until
        a prerequisite (a configured host, a key) is in place.
        """
        return self.__actions__

    def on_ui_setup(self, ui):
        """Register display elements. ``ui`` is this plugin's namespaced view
        (:class:`~kestrel_pi.ui._PluginUI`)."""

    def on_ui_update(self, ui, state):
        """Update display elements. ``ui`` is this plugin's namespaced view and
        ``state`` is a read-only :class:`~kestrel_pi.ui.UIState` snapshot of
        global device state (online / epoch / capture / agent / battery /
        position) at this tick - hook API 2. Most plugins ignore ``state``; a
        display plugin such as the e-ink face uses it to drive its view."""


# -- plugin web results + row-button declarations ------------------------
# Small, flask-free markers a plugin's on_webhook can return. The web layer
# wraps a PluginPage in the shared page shell (so a plugin page matches the
# house style) and turns a PluginRedirect into a 303 back to the Plugins page
# carrying a one-line banner. A plugin never imports the web module.
class PluginPage:
    """A full page a plugin wants rendered in the shared page shell."""

    def __init__(self, title, body, status=200):
        self.title = title
        self.body = body
        self.status = status


class PluginRedirect:
    """A request to return to the Plugins page with a one-line banner."""

    def __init__(self, message=None, error=False):
        self.message = message
        self.error = bool(error)


def normalize_actions(actions):
    """Validate and cap a plugin's declared row buttons.

    Drops malformed entries (an action needs a non-empty string ``label`` and
    ``path``), defaults ``method`` to ``"get"`` (only ``get``/``post``), sorts
    by ``order`` (entries without one sort after those with one, stable by
    declaration order), and returns at most the first two as clean dicts
    ``{"label", "path", "method", "order", "confirm"}``.
    """
    cleaned = []
    for index, action in enumerate(list(actions or [])):
        if not isinstance(action, dict):
            continue
        label = action.get("label")
        path = action.get("path")
        if not isinstance(label, str) or not label:
            continue
        if not isinstance(path, str) or not path:
            continue
        method = str(action.get("method", "get")).lower()
        if method not in ("get", "post"):
            method = "get"
        order = action.get("order")
        order = order if isinstance(order, int) and not isinstance(order, bool) else None
        cleaned.append({
            "label": label,
            "path": path,
            "method": method,
            "order": order,
            "confirm": bool(action.get("confirm", False)),
            "_seq": index,
        })
    cleaned.sort(key=lambda a: (a["order"] is None, a["order"] or 0, a["_seq"]))
    return [{k: a[k] for k in ("label", "path", "method", "order", "confirm")}
            for a in cleaned[:2]]


# -- helper files a plugin ships beside itself ------------------------------
# A plugin is one file, and the loader loads it without registering it as an
# importable module, so it can't ``import`` a neighbour. A plugin that needs
# more than one file (vendored third-party code kept verbatim, say) ships the
# rest as helper files in its own directory, named ``_<plugin>_<name>.py``: the
# loader and plugins-ctl skip ``_`` files, and the build and app-layer updates
# install every ``plugins/*.py``. Helpers belong to one plugin - a plugin can't
# load another plugin's helpers - so every plugin stays standalone.
HELPERS_PACKAGE = "kestrel_plugin_helpers"
_helpers_lock = threading.RLock()    # re-entrant: a library may load another while loading


def load_helper(plugin_file, filename):
    """Import a helper file that ships beside a plugin and return its module.

    Call it as ``load_helper(__file__, "_<plugin>_<name>.py")`` from the plugin,
    where ``<plugin>`` is the plugin's file name without ``.py``. The helper
    must sit in the same directory as the plugin file (an operator plugin in
    /etc/kestrel/plugins brings its own helpers) and carry the plugin's prefix.

    Each helper is registered as ``kestrel_plugin_helpers.<plugin>.<name>``, so
    helpers of one plugin can import each other with relative imports
    (``from . import epdconfig``) once the imported one has been loaded: load
    a helper's dependencies first. A helper is loaded once; later calls return
    the same module. Import a helper lazily (inside the method that needs it)
    if importing it touches hardware."""
    directory, plugin = os.path.split(os.path.abspath(plugin_file))
    stem = os.path.splitext(plugin)[0]
    if stem.startswith("_"):
        raise ValueError("helpers belong to a plugin, not to %s" % plugin)
    prefix = "_%s_" % stem
    if (os.path.basename(filename) != filename or not filename.startswith(prefix)
            or not filename.endswith(".py") or len(filename) <= len(prefix) + 3):
        raise ValueError("a helper of plugin %r is a file %s<name>.py beside it, not %r"
                         % (stem, prefix, filename))
    path = os.path.join(directory, filename)
    if not os.path.isfile(path):
        raise ImportError("helper %s not found beside %s" % (filename, plugin))
    package = "%s.%s" % (HELPERS_PACKAGE, stem.replace("-", "_"))
    name = "%s.%s" % (package, filename[len(prefix):-3])
    with _helpers_lock:
        module = sys.modules.get(name)
        if module is not None and getattr(module, "__file__", None) == path:
            return module
        for pkg in (HELPERS_PACKAGE, package):
            if pkg not in sys.modules:
                parent = types.ModuleType(pkg)
                parent.__path__ = []    # nothing is found by path: helpers are loaded explicitly
                sys.modules[pkg] = parent
        spec = importlib.util.spec_from_file_location(name, path)
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        try:
            spec.loader.exec_module(module)
        except BaseException:
            sys.modules.pop(name, None)
            raise
        return module


# -- shared plugin libraries ------------------------------------------------
# Code that several plugins really share (the e-ink face, the alert queue)
# lives once, as a library file with the plugins: ``_lib_<name>.py``. It is
# still plugin code, not core: core never loads a library itself, and a library
# runs inside the plugin that asked for it, with that plugin's capabilities.
# ``lib`` is reserved as a plugin name, so no plugin's helpers can clash.
LIBRARIES_PACKAGE = "kestrel_plugin_libs"
SHIPPED_PLUGINS_DIR = "/usr/local/lib/kestrel/plugins"
RESERVED_PLUGIN_NAMES = ("lib",)
_LIB_NAME = re.compile(r"[a-z0-9_]+\Z")


def _lib_api(path):
    """The ``LIB_API = (major, minor)`` literal of a library, read without
    running it; raises ImportError if it is missing or malformed."""
    with open(path, encoding="utf-8") as handle:
        tree = ast.parse(handle.read(), path)
    for node in tree.body:
        if (isinstance(node, ast.Assign) and len(node.targets) == 1
                and isinstance(node.targets[0], ast.Name)
                and node.targets[0].id == "LIB_API"):
            try:
                value = ast.literal_eval(node.value)
            except ValueError:
                break
            if (isinstance(value, tuple) and len(value) == 2
                    and all(type(v) is int and v >= 0 for v in value)):
                return value
            break
    raise ImportError("%s has no LIB_API = (major, minor)" % os.path.basename(path))


def load_library(caller_file, name, api):
    """Import the shared plugin library ``_lib_<name>.py`` and return it.

    Call it as ``load_library(__file__, "<name>", api=N)`` from a plugin (or from
    another library). The library is looked for beside the caller first, then
    in the shipped plugins directory: a shipped plugin always gets the shipped
    library, and an operator plugin in /etc/kestrel/plugins uses its own copy
    if it brings one, the shipped one otherwise.

    ``api`` is the library's major API version the caller was written for. The
    library's ``LIB_API = (major, minor)`` is read before any of its code runs,
    and a different major raises ImportError, so an incompatible library never
    runs. Minor versions only add, so any minor is accepted.

    A library is loaded once per file; later calls return the same module. It
    is registered in ``sys.modules`` (so a ``@dataclass`` in it works) under a
    name derived from its path, so a shipped and an operator copy can both be
    loaded. A library that fails to import is not cached."""
    if not isinstance(name, str) or not _LIB_NAME.match(name):
        raise ValueError("a library name is lower-case letters, digits and _, not %r" % (name,))
    if type(api) is not int or api < 0:
        raise ValueError("api is the library's major version, a whole number")
    filename = "_lib_%s.py" % name
    caller_dir = os.path.dirname(os.path.abspath(caller_file))
    candidates = [os.path.join(caller_dir, filename),
                  os.path.join(SHIPPED_PLUGINS_DIR, filename)]
    path = next((p for p in candidates if os.path.isfile(p)), None)
    if path is None:
        raise ImportError("plugin library %s not found beside %s or in %s"
                          % (filename, os.path.basename(caller_file), SHIPPED_PLUGINS_DIR))
    path = os.path.realpath(path)
    found = _lib_api(path)
    if found[0] != api:
        raise ImportError("%s needs the %s library API %d, found %d.%d"
                          % (os.path.basename(caller_file), name, api, found[0], found[1]))
    tag = hashlib.sha256(path.encode("utf-8")).hexdigest()[:8]
    module_name = "%s.%s_%s" % (LIBRARIES_PACKAGE, name, tag)
    with _helpers_lock:
        module = sys.modules.get(module_name)
        if module is not None:
            return module
        if LIBRARIES_PACKAGE not in sys.modules:
            parent = types.ModuleType(LIBRARIES_PACKAGE)
            parent.__path__ = []    # nothing is found by path: libraries are loaded explicitly
            sys.modules[LIBRARIES_PACKAGE] = parent
        spec = importlib.util.spec_from_file_location(module_name, path)
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        try:
            spec.loader.exec_module(module)
        except BaseException:
            sys.modules.pop(module_name, None)
            raise
        return module
