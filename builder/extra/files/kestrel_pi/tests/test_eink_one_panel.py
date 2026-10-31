"""One e-ink plugin drives the e-Paper panel at a time (eink_face 1.1): the HATs
share the SPI bus and GPIO pins (and the 2.7" HAT's keys). The first plugin to
claim the panel that doesn't yield drives it (a deprecated plugin yields to any
other), and the rest stand down: no panel probe, no keys, the web mirror only,
and their card and health note name the plugin that has the panel."""

import gc
import importlib.util
import logging
import os
import types

from kestrel_pi import load_library
from kestrel_pi.capture import CaptureInterface
from kestrel_pi.context import PluginContext, ServiceRegistry
from kestrel_pi.ui import StubUI, UIState

PLUGIN_DIR = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", "..", "plugins"))
FACE = load_library(os.path.join(PLUGIN_DIR, "eink-ws27.py"), "eink_face", api=1)


def _mod(name):
    spec = importlib.util.spec_from_file_location(
        name.replace("-", "_") + "_one_panel", os.path.join(PLUGIN_DIR, name + ".py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


MODS = {n: _mod(n) for n in ("eink-ws27", "eink-ws213")}
CLASSES = {"eink-ws27": "EinkWs27", "eink-ws213": "EinkWs213"}


class _Owner:
    pass


def test_library_api_and_the_owner_rule():
    assert FACE.LIB_API == (1, 1)
    a, b, c = _Owner(), _Owner(), _Owner()
    assert FACE.panel_owner() is None
    FACE.claim_panel(a, "old-face", yields=True)
    assert FACE.panel_owner() == "old-face"             # alone, even a yielding claim drives it
    FACE.claim_panel(b, "eink-ws213")
    FACE.claim_panel(c, "eink-ws27")
    assert FACE.panel_owner() == "eink-ws213"           # the first that doesn't yield
    FACE.release_panel(b)
    assert FACE.panel_owner() == "eink-ws27"
    FACE.claim_panel(c, "eink-ws27")                    # claiming again doesn't duplicate
    assert [n for _r, n, _y in FACE._claims] == ["old-face", "eink-ws27"]
    del c
    gc.collect()
    assert FACE.panel_owner() == "old-face"             # a plugin that is gone holds nothing


def _plugin(name, monkeypatch, started, probed, options=None):
    mod = MODS[name]
    cls = getattr(mod, CLASSES[name])
    monkeypatch.setattr(cls, "_run", lambda self: None)              # no render loop
    if hasattr(cls, "_start_keys"):
        monkeypatch.setattr(cls, "_start_keys", lambda self: started.append(name))
    probe = "_try_panel"
    monkeypatch.setattr(cls, probe, lambda self, *a: probed.append(name) or (None, "no panel"))
    p = cls()
    p.options = dict(options or {})
    p.context = PluginContext(
        name=name, options=dict(options or {}), capture=CaptureInterface("/nonexistent"),
        connectivity=types.SimpleNamespace(online=False, paths={}), services=ServiceRegistry(),
        capabilities=("ui", "web"), logger=logging.getLogger("test.one_panel." + name))
    return p


def _card(p, name):
    ui = StubUI()
    view = ui.for_plugin(name)
    p.on_ui_setup(view)
    p.on_ui_update(view, UIState())
    return {e["name"]: e["value"] for e in ui.widgets()[name]}


def _load(plugins):
    for p in plugins:
        p.on_loaded()
    for p in plugins:
        p.on_ready()


def test_the_first_plugin_drives_the_panel_and_the_other_stands_down(monkeypatch):
    started, probed = [], []
    ws213 = _plugin("eink-ws213", monkeypatch, started, probed)
    ws27 = _plugin("eink-ws27", monkeypatch, started, probed)
    _load([ws213, ws27])
    assert ws213._ensure_panel() is None and ws27._ensure_panel() is None
    assert set(probed) == {"eink-ws213"}                # only the owner touches the pins
    assert started == []                                # nor does the 2.7" plugin start its keys
    assert _card(ws27, "eink-ws27")["panel"] == "used by eink-ws213"
    assert ws27.context.health_note == ("standing down: eink-ws213 drives the e-Paper "
                                        "panel (one e-ink plugin at a time)")
    assert "eink-ws213 drives the panel" in ws27._mirror_page().body
    assert _card(ws213, "eink-ws213")["panel"] == "not detected"
    for p in (ws213, ws27):
        p.on_unload()
    assert FACE.panel_owner() is None


def test_a_yielding_claim_gives_way_to_eink_ws27(monkeypatch):
    # A plugin that yields (as a deprecated plugin does) gives the panel to any
    # other, even when it claimed first.
    started, probed = [], []
    other = _Owner()
    FACE.claim_panel(other, "old-face", yields=True)
    new = _plugin("eink-ws27", monkeypatch, started, probed)
    _load([new])
    new._ensure_panel()
    assert set(probed) == {"eink-ws27"} and started == ["eink-ws27"]
    assert new._standing_down is None
    FACE.release_panel(other)
    new.on_unload()


def test_alone_a_plugin_drives_the_panel_and_starts_its_keys(monkeypatch):
    started, probed = [], []
    only = _plugin("eink-ws27", monkeypatch, started, probed)
    _load([only])
    only._ensure_panel()
    assert set(probed) == {"eink-ws27"} and started == ["eink-ws27"] and only._standing_down is None
    only.on_unload()
