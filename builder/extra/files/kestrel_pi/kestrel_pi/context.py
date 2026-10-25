"""The typed context handed to every plugin (improvement 2).

A plugin never reaches into a global singleton; it talks to the rest of the
system only through ``self.context``:

    self.context.logger          - a per-plugin logging.Logger
    self.context.options         - its validated options (also self.options)
    self.context.capture         - the capture-store view (CaptureInterface)
    self.context.connectivity    - the live ConnectivityState
    self.context.services        - the provides/requires service registry
    self.context.require_capability(cap) - the honest capability check
    self.context.apply(change)   - send a change to the root apply helper
    self.context.page(title, body) / .redirect(msg) - on_webhook result markers
    self.context.esc(value)      - HTML-escape a value for a plugin page
    self.context.form_csrf       - the CSRF token for a plugin's own POST form

Because the context is a plain object, a test injects a fake one and exercises
a plugin with no daemon and no hardware.
"""

from __future__ import annotations

import logging

from .plugins import CAPABILITIES, PluginPage, PluginRedirect


def esc(value):
    """HTML-escape a value for inclusion in a plugin-rendered page."""
    text = "" if value is None else str(value)
    return (
        text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        .replace('"', "&quot;")
    )


class ServiceRegistry:
    """Named services plugins provide to and require from each other.

    A provider registers a zero-argument getter under a name (the ``gps``
    plugin ``provides`` ``position``); a consumer calls ``get(name)`` to fetch
    the current value (``wigle`` ``requires`` ``position``). Getters are called
    lazily so the value is always current, and a getter that raises yields
    ``None`` rather than propagating into the caller.
    """

    def __init__(self, logger=None):
        self._getters = {}   # name -> (getter, owner)
        self._log = logger

    def provide(self, name, getter, owner=None):
        self._getters[name] = (getter, owner)

    def has(self, name):
        return name in self._getters

    def names(self):
        return sorted(self._getters)

    def get(self, name, default=None):
        entry = self._getters.get(name)
        if entry is None:
            return default
        getter, _owner = entry
        try:
            value = getter()
        except Exception:  # noqa: BLE001 - a broken provider must not break the caller
            if self._log:
                self._log.exception("service %r getter failed", name)
            return default
        return default if value is None else value

    def withdraw_owner(self, owner):
        """Remove every service a plugin provided (called on unload)."""
        for name in [n for n, (_g, o) in self._getters.items() if o == owner]:
            del self._getters[name]


class PluginContext:
    """Per-plugin handle onto the daemon's shared surfaces."""

    def __init__(self, name, options, capture, connectivity, services,
                 capabilities=(), logger=None, apply=None):
        self.name = name
        self.options = options
        self.capture = capture
        self.connectivity = connectivity
        self.services = services
        self.capabilities = tuple(capabilities)
        self.logger = logger or logging.getLogger("kestrel_pi.plugin.%s" % name)
        # Send a change to the root apply helper (same channel the web layer
        # uses). A plugin's on_webhook calls this for a privileged action; the
        # root helper validates every change against its fixed target list.
        self._apply = apply
        # HTML escape, for a plugin that renders its own page body.
        self.esc = esc
        # Set by the web layer per-request before on_webhook, so a plugin that
        # renders its own POST form can carry the page's CSRF token.
        self.form_csrf = ""
        # True when the web login is on and the request is signed in; a plugin
        # page that shows sensitive data refuses to render it when this is False.
        self.web_authed = False
        # Capabilities a plugin used without declaring; surfaced on the status
        # page (the honest, non-sandbox capability model, improvement 3).
        self.capability_violations = set()
        # A short, operator-facing note a plugin can set to explain a degraded
        # or idle mode (e.g. wigle running without gps). Surfaced on the status
        # page via the plugin's health record; empty means nothing to report.
        self.health_note = ""

    def require_capability(self, cap):
        """Assert the plugin declared ``cap`` before a gated action.

        This is an honest, auditable check, not a sandbox: an undeclared
        capability is logged and flagged on the status page, but the code still
        runs (a Pi can't afford real isolation). Plugins call this before the
        action the capability names (e.g. ``network`` before an upload).
        """
        if cap not in CAPABILITIES:
            self.logger.warning("unknown capability requested: %r", cap)
            return False
        if cap not in self.capabilities:
            if cap not in self.capability_violations:
                self.logger.warning(
                    "used undeclared capability %r; add it to __capabilities__",
                    cap,
                )
            self.capability_violations.add(cap)
            return False
        return True

    def apply(self, change):
        """Send one change to the root apply helper and return its reply dict.

        The same channel the web layer uses; the root helper validates every
        change against its fixed target list, so this grants no new privilege.
        """
        if self._apply is None:
            return {"ok": False, "error": "settings channel unavailable"}
        return self._apply(change)

    def page(self, title, body, status=200):
        """Build a :class:`~kestrel_pi.plugins.PluginPage` for on_webhook to
        return; the daemon renders ``body`` inside the shared page shell."""
        return PluginPage(title, body, status)

    def redirect(self, message=None, error=False):
        """Build a :class:`~kestrel_pi.plugins.PluginRedirect` for on_webhook
        to return; the daemon sends the browser back to the Plugins page with a
        one-line banner."""
        return PluginRedirect(message, error)
