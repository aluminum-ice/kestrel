"""eink-ws27: the 2.7" HAT's plugin under the name that says which display it
drives, beside the deprecated ``eink``. Until ``eink`` is removed the two are
the same code: test_eink.py and test_eink_keys.py test that code (through
``eink``), and the first test here fails if the two files drift apart anywhere
but the lines that must differ. The rest test what is particular to each name."""

import ast
import hashlib
import importlib.util
import logging
import os
import types

import pytest

from kestrel_pi.capture import CaptureInterface
from kestrel_pi.connectivity import ConnectivityState
from kestrel_pi.context import PluginContext, ServiceRegistry
from kestrel_pi.declarations import read_declarations
from kestrel_pi.loader import Loader
from kestrel_pi.ui import UIState

PLUGIN_DIR = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", "..", "plugins"))
WS27 = os.path.join(PLUGIN_DIR, "eink-ws27.py")
EINK = os.path.join(PLUGIN_DIR, "eink.py")
SHORT_NOTICE = "Deprecated: replaced by eink-ws27; eink will be removed in a future release that requires a reflash."


def _mod(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


W = _mod(WS27, "eink_ws27_under_test")
E = _mod(EINK, "eink_deprecated_under_test")


def _normalized(path):
    """The plugin's code with the lines allowed to differ set aside: the module
    docstring, ``DEPRECATED``, the class name, ``__description__`` and the
    ``variant``/``rotation`` option entries. Comments don't count."""
    tree = ast.parse(open(path, encoding="utf-8").read())
    body = tree.body[1:]                                    # the docstring
    for node in body:
        if isinstance(node, ast.Assign) and getattr(node.targets[0], "id", "") == "DEPRECATED":
            node.value = ast.Constant("")
        if isinstance(node, ast.ClassDef):
            node.name = "Plugin_"
            for item in node.body:
                target = getattr(item, "targets", [None])[0]
                name = getattr(target, "id", "")
                if name == "__description__":
                    item.value = ast.Constant("")
                if name == "__options__":
                    keep = [(k, v) for k, v in zip(item.value.keys, item.value.values)
                            if getattr(k, "value", None) not in ("variant", "rotation")]
                    item.value.keys = [k for k, _ in keep]
                    item.value.values = [v for _, v in keep]
    return ast.dump(ast.Module(body=body, type_ignores=[]))


def test_eink_and_eink_ws27_are_the_same_code():
    assert _normalized(EINK) == _normalized(WS27), (
        "eink.py and eink-ws27.py differ: make the change in both files")


@pytest.mark.parametrize("name", ["epdconfig", "epd2in7", "epd2in7_V2"])
def test_the_driver_helpers_are_the_same_files(name):
    def digest(prefix):
        with open(os.path.join(PLUGIN_DIR, "_%s_%s.py" % (prefix, name)), "rb") as handle:
            return hashlib.sha256(handle.read()).hexdigest()

    assert digest("eink-ws27") == digest("eink")


def test_declarations():
    ws27 = read_declarations(WS27)["eink-ws27"]
    eink = read_declarations(EINK)["eink"]
    assert ws27["options"]["variant"]["choices"] == ["auto", "v2", "v1"]
    assert ws27["options"]["rotation"]["choices"] == [0, 90, 180, 270]
    assert ws27["options"]["variant"]["default"] == eink["options"]["variant"]["default"] == "auto"
    assert "choices" not in eink["options"]["variant"]     # stored values on devices stay valid
    assert set(ws27["options"]) == set(eink["options"])
    assert ws27["capabilities"] == eink["capabilities"] == ("ui", "web")
    assert "network" not in ws27["capabilities"]
    assert not ws27["description"].startswith("Deprecated")


def test_eink_says_it_is_deprecated_and_slated_for_removal():
    eink = read_declarations(EINK)["eink"]
    assert eink["description"].startswith(SHORT_NOTICE)
    assert "eink-ws27" in E.DEPRECATED and "reflash" in E.DEPRECATED
    assert W.DEPRECATED == ""


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


def test_eink_still_accepts_its_old_values(tmp_path):
    # eink has no choices, so a device updated in place keeps working with what
    # it stored (an upper-case variant, say).
    loader = _loader(tmp_path, "eink", {"variant": "V2", "buttons": False})
    loaded = {r.name for r in loader.load_all()}
    try:
        assert "eink" in loaded
        assert loader.records["eink"].instance.context.health_note.startswith("deprecated:")
    finally:
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


@pytest.mark.parametrize("mod, cls, name, note", [
    (W, "EinkWs27", "eink-ws27", "no e-Paper HAT detected"),
    (E, "EinkFace", "eink", None)])
def test_each_name_loads_its_own_driver_helpers(tmp_path, monkeypatch, mod, cls, name, note):
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
    if note:
        assert p.context.health_note == note
    else:
        assert p.context.health_note == E.DEPRECATED + "; no e-Paper HAT detected"


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


def test_the_docs_mark_eink_deprecated_wherever_they_list_it():
    # Wherever the user-facing docs list the plugins, eink is marked deprecated
    # and slated for removal in a release that requires a reflash.
    root = os.path.join(PLUGIN_DIR, "..", "..", "..", "..")
    for rel in ("README.md", "docs/user-guide/managing-plugins.md",
                "docs/user-guide/user-guides.md", "docs/user-guide/config-reference.md",
                "docs/user-guide/eink-setup.md",
                "builder/extra/files/kestrel_pi/kestrel_pi/userguide.py"):
        text = open(os.path.join(root, rel), encoding="utf-8").read()
        assert "eink-ws27" in text, rel
        assert "deprecated" in text.lower() and "reflash" in text, rel
