"""The display interface for ``on_ui_setup`` / ``on_ui_update``.

Phase 5 swaps a real e-ink renderer behind this same minimal interface (named
elements with a value), so display plugins written now keep working. The web
dashboard (Phase 3.5) renders the elements a plugin registers as a per-plugin
readout card, and the Phase 5 e-ink plugin will render the same elements as
terse text: one state model, two independent views. Keeping the surface tiny is
deliberate - it is provisional until the Phase 5 renderer meets it, and a small
surface is cheap to change.

Each plugin gets its own namespaced view of the UI via :meth:`StubUI.for_plugin`,
so elements are grouped by the plugin that registered them (no collisions
between two plugins that both add a ``status`` element) and the dashboard can
draw a card per plugin. An element carries a ``label``, a ``value`` and a
``kind`` render hint (``text`` by default; ``number``/``percent``/``gauge`` let
a renderer draw a bar from a numeric value while the e-ink stays plain text).

The element API is **source-compatible** with the Phase 3 stub: the old
``add_element(name, value)`` and ``set(name, value)`` calls still work (they
make a ``text`` element whose label is the name), so ported and reference
plugins need no change. ``label`` and ``kind`` are optional keyword additions.

Phase 5 locks the hook signatures now that a physical renderer (the e-ink face)
exists: ``on_ui_update`` gains a second argument, a read-only :class:`UIState`
snapshot of global device state, so a display plugin can drive its view from
one consolidated model. That signature change bumps ``HOOK_API_VERSION`` to 2.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass

# Render hints a renderer MAY use. Anything else is treated as "text". These are
# hints, not a contract: a renderer that doesn't understand one falls back to
# showing the value as text.
ELEMENT_KINDS = ("text", "number", "percent", "gauge")


def _normalize_kind(kind):
    return kind if kind in ELEMENT_KINDS else "text"


@dataclass(frozen=True)
class UIState:
    """Read-only snapshot of global device state at an ``on_ui_update`` tick.

    Passed as the second argument to ``on_ui_update`` (hook API 2). Fields are
    plain data the daemon assembles once per tick: ``agent``, ``battery`` and
    ``position`` are the service-provider dicts (or ``None`` when the agent has
    published nothing, no UPS HAT is present, or there is no GPS fix). It is
    frozen; a plugin reads it and never mutates the shared dicts. Most plugins
    ignore it - a display plugin such as the e-ink face uses it to pick a mood.
    """

    online: bool = False
    epoch: int = 0
    capture_active: bool = False
    agent: dict | None = None
    battery: dict | None = None
    position: dict | None = None
    # Per-management-interface link state (``{"bnep0": bool, "wlan0": bool}``),
    # so a display can show BT PAN and Wi-Fi separately. ``None`` when unknown.
    paths: dict | None = None
    # The three capture counts the UI speaks in plain English ("N handshakes
    # from M access points across K networks"), all from the daemon's
    # capture_counts() so the face agrees with the dashboard. ``capture_total``
    # is the lifetime handshake (hashline) count; ``capture_session`` is those
    # captured since this daemon started. ``None`` when not provided.
    capture_total: int | None = None
    capture_session: int | None = None
    # Seconds the daemon has been running (for an "uptime" readout). ``None``
    # when not provided.
    uptime: float | None = None
    # Distinct captured access points, i.e. distinct **radios (BSSIDs)**: a radio
    # seen under more than one name counts once. ``aps_total`` is lifetime in the
    # store; ``aps_session`` is distinct BSSIDs since this daemon started (used by
    # the capture card, not the face). ``None`` when absent.
    aps_total: int | None = None
    aps_session: int | None = None
    # Distinct captured networks (unique ESSIDs / names) lifetime in the store.
    # ``None`` when absent.
    networks_total: int | None = None
    # A short label of the channel plan capture is pinned to this window (e.g.
    # ``"1,6,11"``, ``"2.4G"``, ``"sweep"``, ``"hunt"``), or ``None`` when
    # capture is not running / unknown. Honest: it is the plan, not a live tune.
    channel: str | None = None
    # The in-range RF readout for this window: ``None`` when capture is off (the
    # radio is deaf). Otherwise a dict with ``available`` (False until the
    # collector has a reading), and when available ``radios_recent``,
    # ``window_s``, ``level`` (0-4), ``label`` and ``data_age_s`` (how old the
    # underlying capture data is). A density indicator of distinct radios over
    # the last ``window_s`` of capture data, not an instant count.
    env: dict | None = None


class _PluginUI:
    """A single plugin's namespaced view of the shared UI, handed to its
    ``on_ui_setup`` / ``on_ui_update``. Writes land under the plugin's name."""

    def __init__(self, store, owner):
        self._store = store
        self._owner = owner

    def add_element(self, name, value="", label=None, kind="text", mark=None):
        """Register a display element (called from ``on_ui_setup``). ``label``
        defaults to ``name``; ``kind`` is a render hint (see ELEMENT_KINDS).
        ``mark`` (0-100), on a ``percent``/``gauge`` element, places a reference
        marker on the bar - e.g. a battery gauge marking its shutdown threshold.
        A renderer that doesn't understand it ignores it."""
        self._store._add(self._owner, name, value, label, kind, mark)

    def set(self, name, value):
        """Update an element's value (called from ``on_ui_update``). If the
        element was never added, it is created as a ``text`` element so a
        set-before-add still shows."""
        self._store._set(self._owner, name, value)

    def get(self, name, default=None):
        return self._store._get(self._owner, name, default)


class StubUI:
    """Holds the UI element values, grouped by the plugin that registered them.

    The daemon keeps one ``StubUI`` and hands each plugin its own
    :class:`_PluginUI` view (:meth:`for_plugin`). Rendering is done elsewhere
    (the web dashboard now, the e-ink plugin in Phase 5); this just stores the
    model and logs changes."""

    def __init__(self, logger=None):
        # owner -> {name: {"label", "value", "kind"}}, plus an insertion order
        # per owner so a card lists elements in the order the plugin added them.
        self._elements = {}
        self._order = {}
        self._lock = threading.Lock()
        self._log = logger

    def for_plugin(self, owner):
        """A namespaced view for one plugin, passed as ``ui`` to its hooks."""
        return _PluginUI(self, owner)

    def _add(self, owner, name, value="", label=None, kind="text", mark=None):
        kind = _normalize_kind(kind)
        with self._lock:
            slot = self._elements.setdefault(owner, {})
            order = self._order.setdefault(owner, [])
            if name not in slot:
                order.append(name)
            element = {
                "label": name if label is None else label,
                "value": value,
                "kind": kind,
            }
            if mark is not None:
                element["mark"] = mark
            slot[name] = element
        if self._log:
            self._log.debug("ui: %s add %s=%r (%s)", owner, name, value, kind)

    def _set(self, owner, name, value):
        with self._lock:
            slot = self._elements.get(owner)
            if slot is None or name not in slot:
                # set before add: create a text element so it still shows.
                slot = self._elements.setdefault(owner, {})
                self._order.setdefault(owner, []).append(name)
                slot[name] = {"label": name, "value": value, "kind": "text"}
            else:
                slot[name]["value"] = value
        if self._log:
            self._log.debug("ui: %s set %s=%r", owner, name, value)

    def _get(self, owner, name, default=None):
        with self._lock:
            slot = self._elements.get(owner) or {}
            element = slot.get(name)
            return element["value"] if element else default

    def elements(self):
        """Flat ``{name: value}`` across all plugins, kept for the status JSON
        and back-compat. Last writer wins on a name collision (as the Phase 3
        single-namespace stub did)."""
        with self._lock:
            flat = {}
            for owner, names in self._order.items():
                for name in names:
                    flat[name] = self._elements[owner][name]["value"]
            return flat

    def widgets(self):
        """Grouped, typed elements for the dashboard: ``{plugin: [element, ...]}``
        in insertion order, each element a dict with ``name``/``label``/``value``
        /``kind``. Only plugins that registered at least one element appear."""
        with self._lock:
            out = {}
            for owner, names in self._order.items():
                out[owner] = [
                    dict(self._elements[owner][name], name=name) for name in names
                ]
            return out
