"""ups-ws: the Waveshare UPS HAT's plugin under the name that says which board
it drives, beside the deprecated ``ups-hat``. Until ``ups-hat`` is removed the
two are the same code: the ups-hat tests in test_reference_plugins.py test that
code, and the first test here fails if the two files drift apart anywhere but
the lines that must differ. The rest test what is particular to each name."""

import ast
import logging
import os

from kestrel_pi.capture import CaptureInterface
from kestrel_pi.connectivity import ConnectivityState
from kestrel_pi.context import ServiceRegistry
from kestrel_pi.declarations import read_declarations
from kestrel_pi.loader import Loader

PLUGIN_DIR = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", "..", "plugins"))
WS = os.path.join(PLUGIN_DIR, "ups-ws.py")
HAT = os.path.join(PLUGIN_DIR, "ups-hat.py")
SHORT_NOTICE = ("Deprecated: replaced by ups-ws; ups-hat will be removed in a future release "
                "that requires a reflash.")


def _normalized(path):
    """The plugin's code with the lines allowed to differ set aside: the module
    docstring, ``DEPRECATED``, the class name and ``__description__``."""
    tree = ast.parse(open(path, encoding="utf-8").read())
    body = tree.body[1:]
    for node in body:
        if isinstance(node, ast.Assign) and getattr(node.targets[0], "id", "") == "DEPRECATED":
            node.value = ast.Constant("")
        if isinstance(node, ast.ClassDef):
            node.name = "Plugin_"
            for item in node.body:
                target = getattr(item, "targets", [None])[0]
                if getattr(target, "id", "") == "__description__":
                    item.value = ast.Constant("")
    return ast.dump(ast.Module(body=body, type_ignores=[]))


def test_ups_hat_and_ups_ws_are_the_same_code():
    assert _normalized(HAT) == _normalized(WS), (
        "ups-hat.py and ups-ws.py differ: make the change in both files")


def test_declarations():
    ws = read_declarations(WS)["ups-ws"]
    hat = read_declarations(HAT)["ups-hat"]
    assert ws["options"] == hat["options"]
    assert ws["capabilities"] == hat["capabilities"] == ("ui",)
    assert ws["provides"] == hat["provides"] == ("battery",)
    assert not ws["description"].startswith("Deprecated")
    assert hat["description"].startswith(SHORT_NOTICE)


def _loader(tmp_path, *names):
    cfg = {"plugins": {name: {"enabled": True} for name in names}}
    return Loader(config=cfg, capture=CaptureInterface(str(tmp_path)),
                  connectivity=ConnectivityState(), services=ServiceRegistry(),
                  search_paths=[PLUGIN_DIR], logger=logging.getLogger("test.ups_ws"))


def test_ups_ws_loads_and_says_nothing_about_deprecation(tmp_path):
    loader = _loader(tmp_path, "ups-ws")
    try:
        assert "ups-ws" in {r.name for r in loader.load_all()}
        assert "deprecated" not in loader.records["ups-ws"].instance.context.health_note
    finally:
        loader.unload_all(timeout=5)


def test_ups_hat_says_it_is_deprecated(tmp_path):
    loader = _loader(tmp_path, "ups-hat")
    try:
        assert "ups-hat" in {r.name for r in loader.load_all()}
        note = loader.records["ups-hat"].instance.context.health_note
        assert note.startswith("deprecated: replaced by ups-ws")
    finally:
        loader.unload_all(timeout=5)


def test_with_both_enabled_ups_hat_gives_way(tmp_path):
    loader = _loader(tmp_path, "ups-hat", "ups-ws")
    try:
        loader.load_all()
        hat = loader.records["ups-hat"].instance.monitor
        ws = loader.records["ups-ws"].instance.monitor
        assert hat.active() is ws and ws.active() is ws
        assert hat.yields and not ws.yields
    finally:
        loader.unload_all(timeout=5)


def test_the_docs_mark_ups_hat_deprecated_wherever_they_list_it():
    root = os.path.join(PLUGIN_DIR, "..", "..", "..", "..")
    for rel in ("README.md", "docs/user-guide/managing-plugins.md",
                "docs/user-guide/user-guides.md", "docs/user-guide/config-reference.md",
                "docs/user-guide/ups-hat-setup.md",
                "builder/extra/files/kestrel_pi/kestrel_pi/userguide.py"):
        text = open(os.path.join(root, rel), encoding="utf-8").read()
        assert "ups-ws" in text, rel
        i = text.find("`ups-hat`")
        assert i >= 0, rel
        assert "deprecated" in text.lower() and "reflash" in text, rel
