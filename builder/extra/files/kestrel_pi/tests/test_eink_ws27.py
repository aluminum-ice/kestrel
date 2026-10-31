"""eink-ws27: the Waveshare 2.7" HAT's face plugin. It replaced ``eink`` (the same
face under a name that says which display it drives); ``eink`` was removed in
1.0.11. test_eink.py and test_eink_keys.py test the face, the panel and the keys;
these test what is particular to the name, and that ``eink`` is gone."""

import importlib.util
import logging
import os
import re
import tomllib
import types

from kestrel_pi.capture import CaptureInterface
from kestrel_pi.connectivity import ConnectivityState
from kestrel_pi.context import PluginContext, ServiceRegistry
from kestrel_pi.declarations import read_declarations
from kestrel_pi.loader import Loader
from kestrel_pi.ui import UIState

PLUGIN_DIR = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", "..", "plugins"))
WS27 = os.path.join(PLUGIN_DIR, "eink-ws27.py")
ROOT = os.path.normpath(os.path.join(PLUGIN_DIR, "..", "..", "..", ".."))


def _mod(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


W = _mod(WS27, "eink_ws27_under_test")


def test_declarations():
    ws27 = read_declarations(WS27)["eink-ws27"]
    assert ws27["options"]["variant"]["choices"] == ["auto", "v2", "v1"]
    assert ws27["options"]["variant"]["default"] == "auto"
    assert ws27["options"]["rotation"]["choices"] == [0, 90, 180, 270]
    assert ws27["capabilities"] == ("ui", "web")
    assert not ws27["description"].startswith("Deprecated")


def _loader(tmp_path, name, options=None):
    cfg = {"plugins": {name: dict({"enabled": True}, **(options or {}))}}
    return Loader(config=cfg, capture=CaptureInterface(str(tmp_path)),
                  connectivity=ConnectivityState(), services=ServiceRegistry(),
                  search_paths=[PLUGIN_DIR], logger=logging.getLogger("test.eink_ws27"))


def test_eink_ws27_loads_and_degrades_without_a_hat(tmp_path):
    loader = _loader(tmp_path, "eink-ws27", {"buttons": False})
    loaded = {r.name for r in loader.load_all()}
    try:
        assert "eink-ws27" in loaded
        plugin = loader.records["eink-ws27"].instance
        assert plugin.context.health_note == ""
    finally:
        loader.unload_all(timeout=5)


def test_eink_ws27_refuses_a_value_outside_its_choices(tmp_path):
    for options in ({"variant": "V2"}, {"rotation": 45}):
        loader = _loader(tmp_path, "eink-ws27", dict(options, buttons=False))
        loader.load_all()
        record = loader.records["eink-ws27"]
        assert record.state != "loaded" and next(iter(options)) in record.reason, options
        loader.unload_all(timeout=5)


def _plugin(mod, cls, name, tmp_path, options=None):
    p = getattr(mod, cls)()
    p.options = dict(options or {})
    p.context = PluginContext(name=name, options=dict(options or {}),
                              capture=CaptureInterface(str(tmp_path)),
                              connectivity=types.SimpleNamespace(online=False),
                              services=ServiceRegistry(), capabilities=("ui", "web"))
    p._setup()
    return p


def test_it_loads_its_own_driver_helpers(tmp_path, monkeypatch):
    mod, cls, name = W, "EinkWs27", "eink-ws27"
    loaded = []

    def fake_load(plugin_file, filename):
        loaded.append(filename)
        if not filename.endswith("epdconfig.py"):
            raise ImportError("no SPI here")
        return types.SimpleNamespace()

    monkeypatch.setattr(mod, "load_helper", fake_load)
    p = _plugin(mod, cls, name, tmp_path)
    assert p._ensure_panel() is None
    prefix = "_%s_" % name
    assert loaded == [prefix + "epdconfig.py", prefix + "epd2in7_V2.py",
                      prefix + "epdconfig.py", prefix + "epd2in7.py"]     # auto: V2, then V1
    assert p.context.health_note == "no e-Paper HAT detected"


def test_the_mirror_etag_and_keys_carry_the_plugin_name(tmp_path, monkeypatch):
    p = _plugin(W, "EinkWs27", "eink-ws27", tmp_path)
    p._addresses = lambda: []
    p._state = UIState(online=True)
    _body, status, headers = p.on_webhook("display.png", None)
    assert status == 200 and headers["ETag"].startswith('"eink-ws27-')

    class Button:
        def __init__(self, pin):
            self.when_pressed = self.when_released = None

        def close(self):
            pass

    monkeypatch.setattr(W.keys.GpioSource, "_make_button", lambda self, pin: Button(pin))
    p._start_keys()
    try:
        assert p._keys.plugin_name == "eink-ws27"
    finally:
        p._keys.stop()


def test_eink_is_gone():
    # eink was removed in 1.0.11: no plugin, no driver helpers, no shipped config
    # table, and the user-facing plugin lists don't offer it.
    names = os.listdir(PLUGIN_DIR)
    assert "eink.py" not in names
    assert not [n for n in names if n.startswith("_eink_")]
    with open(os.path.join(ROOT, "builder", "extra", "files", "config.toml"), "rb") as fh:
        plugins = tomllib.load(fh)["plugins"]
    assert "eink" not in plugins and "eink-ws27" in plugins
    for rel in ("README.md", "docs/user-guide/managing-plugins.md",
                "docs/user-guide/user-guides.md", "docs/user-guide/config-reference.md",
                "builder/extra/files/kestrel_pi/kestrel_pi/userguide.py"):
        text = open(os.path.join(ROOT, rel), encoding="utf-8").read()
        assert "eink-ws27" in text, rel
        assert not re.search(r"`eink`(?! was| is)", text), rel
    assert not os.path.exists(os.path.join(ROOT, "docs", "user-guide", "eink-setup.md"))
