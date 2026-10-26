"""Plugin discovery, validation, ordering, and lifecycle.

The loader scans the two search paths (shipped reference plugins, then operator
plugins which override a shipped plugin of the same name), reads each module's
declarations statically, imports it, checks its ``Plugin`` subclasses match
what was read (:mod:`.declarations`: root reads the same declarations without
importing, so a plugin it can't read is refused here too), and for each plugin
whose ``[plugins.<name>] enabled`` is true:

1. checks ``__target_api__`` against the daemon's :data:`HOOK_API_VERSION`
   (improvement 4) - compatible loads, older warns, incompatible is refused;
2. validates its options table against ``__options__`` (improvement 5);
3. resolves ``__requires__`` against what other enabled plugins ``__provides__``
   (improvement 2) - a missing hard requirement disables-with-reason, while a
   missing ``__optional_requires__`` only forgoes the ordering - and orders
   startup so providers come up first.

Survivors are instantiated with their :class:`PluginContext`, then
``on_loaded`` (in dependency order) and ``on_ready`` (once all are loaded) are
called. Everything that fails a check becomes a ``disabled``/``failed`` record
with a reason, surfaced on the status page, never a daemon crash.
"""

from __future__ import annotations

import importlib.util
import logging
import os
import threading
import time

from . import HOOK_API_VERSION
from .applyclient import apply_change
from .context import PluginContext
from .declarations import DeclarationError, check_classes, read_declarations
from .options import OptionsError, validate_options
from .plugins import CAPABILITIES, Plugin, normalize_actions


class PluginRecord:
    """Health and identity of one plugin through its whole lifecycle."""

    def __init__(self, name, source):
        self.name = name
        self.source = source            # file the plugin was loaded from
        self.cls = None
        self.instance = None
        self.options = {}
        self.capabilities = ()
        self.provides = ()
        self.requires = ()
        self.optional_requires = ()
        self.target_api = None
        # state: "loaded" | "failed" | "disabled"
        self.state = "disabled"
        self.reason = ""
        self.warnings = []
        self.last_error = ""
        self.last_hook = ""
        self.last_hook_time = 0.0
        self.last_hook_seconds = 0.0
        self.overruns = 0

    @property
    def failed(self):
        return self.state != "loaded"

    @property
    def context(self):
        return getattr(self.instance, "context", None)

    def health(self):
        ctx = self.context
        violations = sorted(getattr(ctx, "capability_violations", ()) or ())
        return {
            "name": self.name,
            "state": self.state,
            "reason": self.reason,
            "note": getattr(ctx, "health_note", "") or "",
            "source": self.source,
            "actions": self._actions(),
            "capabilities": list(self.capabilities),
            "provides": list(self.provides),
            "requires": list(self.requires),
            "optional_requires": list(self.optional_requires),
            "target_api": self.target_api,
            "warnings": list(self.warnings),
            "last_error": self.last_error,
            "last_hook": self.last_hook,
            "last_hook_time": self.last_hook_time,
            "overruns": self.overruns,
            "capability_violations": violations,
        }

    def _actions(self):
        """The plugin's declared row buttons (only while loaded). A plugin that
        raises must not break the status page, so failures degrade to none."""
        if self.state != "loaded" or self.instance is None:
            return []
        try:
            return normalize_actions(self.instance.webhook_actions())
        except Exception:  # noqa: BLE001 - a plugin must not break status
            log = logging.getLogger(__name__)
            log.exception("plugin %s webhook_actions raised", self.name)
            return []


def discover_plugin_classes(path):
    """Import ``path`` and return its plugin classes.

    A module defines one or more classes that subclass ``Plugin`` directly, with
    their declarations as plain literals. Those are read statically first and
    checked against the imported classes; a file that fails either raises
    :class:`DeclarationError`. Returns a list of ``(name, cls)``; ``name`` is the
    class's ``__plugin_name__``, falling back to the module file name."""
    declared = read_declarations(path)
    if not declared:
        raise DeclarationError("%s: no class subclassing Plugin directly" % path)
    mod_name = "kestrel_pi_plugin_" + os.path.splitext(os.path.basename(path))[0].replace("-", "_")
    spec = importlib.util.spec_from_file_location(mod_name, path)
    if spec is None or spec.loader is None:
        raise ImportError("cannot load %s" % path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    classes = []
    for attr in vars(module).values():
        if isinstance(attr, type) and issubclass(attr, Plugin) and attr is not Plugin:
            # Only classes actually defined in this module, not imported bases.
            if attr.__module__ == module.__name__:
                classes.append(attr)

    file_default = os.path.splitext(os.path.basename(path))[0]
    found = [((getattr(c, "__plugin_name__", None) or file_default), c) for c in classes]
    check_classes(path, declared, found)
    return found


class Loader:
    def __init__(self, config, capture, connectivity, services,
                 search_paths, logger):
        self.config = config
        self.capture = capture
        self.connectivity = connectivity
        self.services = services
        self.search_paths = list(search_paths)
        self.log = logger
        self.records = {}   # name -> PluginRecord

    # -- discovery -------------------------------------------------------
    def _scan(self):
        """Return {name: source_path}; later paths override earlier ones."""
        found = {}
        for directory in self.search_paths:
            if not os.path.isdir(directory):
                continue
            for fname in sorted(os.listdir(directory)):
                if not fname.endswith(".py") or fname.startswith("_"):
                    continue
                found[os.path.splitext(fname)[0]] = os.path.join(directory, fname)
        return found

    # -- load ------------------------------------------------------------
    def load_all(self):
        """Discover, validate and instantiate every enabled plugin. Returns the
        list of successfully loaded records in dependency order."""
        plugins_cfg = self.config.get("plugins", {}) or {}
        sources = self._scan()

        # Build a record per discovered file, importing and reading declarations.
        candidates = {}
        for file_name, source in sources.items():
            try:
                classes = discover_plugin_classes(source)
            except DeclarationError as exc:
                record = PluginRecord(file_name, source)
                record.state = "failed"
                record.reason = "unreadable declarations: %s" % exc
                self.records[file_name] = record
                self.log.error("plugin %s refused: %s", file_name, exc)
                continue
            except Exception as exc:  # noqa: BLE001 - a bad module is quarantined
                record = PluginRecord(file_name, source)
                record.state = "failed"
                record.reason = "import error: %s" % exc
                self.records[file_name] = record
                self.log.error("plugin %s failed to import: %s", file_name, exc)
                continue
            for name, cls in classes:
                record = PluginRecord(name, source)
                record.cls = cls
                record.capabilities = tuple(getattr(cls, "__capabilities__", ()) or ())
                record.provides = tuple(getattr(cls, "__provides__", ()) or ())
                record.requires = tuple(getattr(cls, "__requires__", ()) or ())
                record.optional_requires = tuple(
                    getattr(cls, "__optional_requires__", ()) or ()
                )
                record.target_api = getattr(cls, "__target_api__", None)
                candidates[name] = record
                self.records[name] = record

        # Which plugins are enabled in config.
        enabled = {
            name: rec for name, rec in candidates.items()
            if (plugins_cfg.get(name, {}) or {}).get("enabled", False)
        }
        for name, rec in candidates.items():
            if name not in enabled and rec.state != "failed":
                rec.state = "disabled"
                rec.reason = "not enabled in config"

        # Per-plugin checks: target API, options, capability sanity.
        checked = {}
        for name, rec in enabled.items():
            table = plugins_cfg.get(name, {}) or {}
            if not self._check_target_api(rec):
                continue
            try:
                values, warnings = validate_options(
                    getattr(rec.cls, "__options__", {}), table
                )
            except OptionsError as exc:
                self._disable(rec, "bad options: %s" % exc)
                continue
            rec.options = values
            rec.warnings = warnings
            for warn in warnings:
                self.log.warning("plugin %s: %s", name, warn)
            bad = [c for c in rec.capabilities if c not in CAPABILITIES]
            if bad:
                self.log.warning(
                    "plugin %s declares unknown capabilities %s", name, bad
                )
            checked[name] = rec

        # Resolve requires/provides and order startup (providers first).
        order = self._resolve_and_order(checked)

        # Instantiate and run on_loaded in order.
        loaded = []
        for rec in order:
            if self._instantiate(rec):
                loaded.append(rec)

        # on_ready once everything that is going to load has loaded.
        for rec in loaded:
            self._safe_lifecycle(rec, "on_ready")
        return loaded

    # -- checks ----------------------------------------------------------
    def _check_target_api(self, rec):
        api = rec.target_api
        if api is None:
            rec.warnings.append("no __target_api__ declared; assuming %d" % HOOK_API_VERSION)
            rec.target_api = HOOK_API_VERSION
            return True
        if not isinstance(api, int) or api > HOOK_API_VERSION:
            self._disable(
                rec,
                "targets hook API %r, daemon provides %d" % (api, HOOK_API_VERSION),
            )
            return False
        if api < HOOK_API_VERSION:
            self.log.warning(
                "plugin %s targets older hook API %d (daemon %d); loading anyway",
                rec.name, api, HOOK_API_VERSION,
            )
        return True

    def _resolve_and_order(self, checked):
        provided = {}
        for name, rec in checked.items():
            for svc in rec.provides:
                provided.setdefault(svc, name)

        ok = {}
        for name, rec in checked.items():
            missing = [svc for svc in rec.requires if svc not in provided]
            if missing:
                self._disable(rec, "missing required service(s): %s" % ", ".join(missing))
            else:
                ok[name] = rec

        # Topological order: a plugin comes after the providers of the services
        # it needs. Both hard and optional requirements steer the order (so an
        # enabled provider always starts first), but only hard ones gate the
        # load - a missing optional requirement is simply not ordered against.
        order = []
        placed = set()

        def place(name, stack):
            if name in placed or name not in ok:
                return
            if name in stack:
                self.log.warning("dependency cycle involving %s; breaking it", name)
                return
            stack.add(name)
            for svc in tuple(ok[name].requires) + tuple(ok[name].optional_requires):
                provider = provided.get(svc)
                if provider in ok:
                    place(provider, stack)
            stack.discard(name)
            if name not in placed:
                placed.add(name)
                order.append(ok[name])

        for name in sorted(ok):
            place(name, set())
        return order

    # -- instantiation + lifecycle --------------------------------------
    def _instantiate(self, rec):
        try:
            instance = rec.cls()
            ctx = PluginContext(
                name=rec.name,
                options=rec.options,
                capture=self.capture,
                connectivity=self.connectivity,
                services=self.services,
                capabilities=rec.capabilities,
                logger=self.log.getChild(rec.name),
                apply=apply_change,
            )
            instance.context = ctx
            instance.options = rec.options
            instance.name = rec.name
            rec.instance = instance
            # Register the services this plugin provides. The convention is a
            # ``provide_<service>`` method returning the current value.
            for svc in rec.provides:
                getter = getattr(instance, "provide_" + svc.replace("-", "_"), None)
                if getter is None:
                    self.log.warning(
                        "plugin %s provides %r but has no provide_%s method",
                        rec.name, svc, svc,
                    )
                    continue
                self.services.provide(svc, getter, owner=rec.name)
            rec.state = "loaded"
            rec.reason = ""
        except Exception as exc:  # noqa: BLE001
            self._fail(rec, "instantiation error: %s" % exc)
            return False
        return self._safe_lifecycle(rec, "on_loaded")

    def _safe_lifecycle(self, rec, hook):
        method = getattr(rec.instance, hook, None)
        if method is None:
            return True
        try:
            method()
            return True
        except Exception as exc:  # noqa: BLE001 - a raising lifecycle hook quarantines
            self._fail(rec, "%s raised: %s" % (hook, exc))
            self.services.withdraw_owner(rec.name)
            return False

    def _disable(self, rec, reason):
        rec.state = "disabled"
        rec.reason = reason
        self.log.warning("plugin %s disabled: %s", rec.name, reason)

    def _fail(self, rec, reason):
        rec.state = "failed"
        rec.reason = reason
        rec.last_error = reason
        self.log.error("plugin %s failed: %s", rec.name, reason)

    # -- teardown --------------------------------------------------------
    def unload(self, rec):
        """Unload one plugin: call on_unload, withdraw its services, forget it."""
        self._call_on_unload(rec)
        self._forget(rec)

    def unload_all(self, timeout):
        """Unload every loaded plugin, giving all their ``on_unload`` calls one
        shared ``timeout`` (seconds) between them. They run concurrently, each
        on its own daemon thread, so one slow plugin (the e-ink panel blanking,
        a driver busy-wait on a wedged BUSY pin) can't hold up the rest or the
        daemon's stop past systemd's stop timeout. Python can't kill a thread,
        so an overrun is logged and left behind; the process is exiting anyway.
        Returns the names of the plugins whose on_unload did not finish."""
        records = self.loaded_records()
        workers = []
        for rec in records:
            worker = threading.Thread(target=self._call_on_unload, args=(rec,),
                                      name="pwn-unload-%s" % rec.name, daemon=True)
            worker.start()
            workers.append((rec, worker))
        deadline = time.monotonic() + max(0.0, float(timeout))
        overran = []
        for rec, worker in workers:
            worker.join(max(0.0, deadline - time.monotonic()))
            if worker.is_alive():
                overran.append(rec.name)
                self.log.warning("plugin %s on_unload still running after %.0fs; "
                                 "not waiting for it", rec.name, timeout)
        # Registry bookkeeping stays on this thread: ServiceRegistry is unlocked.
        for rec in records:
            self._forget(rec)
        return overran

    def _call_on_unload(self, rec):
        if rec.instance is not None:
            try:
                rec.instance.on_unload()
            except Exception:  # noqa: BLE001
                self.log.exception("plugin %s raised in on_unload", rec.name)

    def _forget(self, rec):
        self.services.withdraw_owner(rec.name)
        rec.state = "disabled"
        rec.reason = "unloaded"

    def loaded_records(self):
        return [r for r in self.records.values() if r.state == "loaded"]
