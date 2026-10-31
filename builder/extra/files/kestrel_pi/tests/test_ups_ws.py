"""ups-ws: the Waveshare UPS HAT's plugin. It replaced ``ups-hat`` (the same code
under a name that says which board it drives); ``ups-hat`` was removed in 1.0.11.
The battery logic is tested in test_ups_lib.py and the plugin's wiring in
test_reference_plugins.py; these test the name, and that ``ups-hat`` is gone."""

import logging
import os
import re
import tomllib

from kestrel_pi.capture import CaptureInterface
from kestrel_pi.connectivity import ConnectivityState
from kestrel_pi.context import ServiceRegistry
from kestrel_pi.declarations import read_declarations
from kestrel_pi.loader import Loader

PLUGIN_DIR = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", "..", "plugins"))
WS = os.path.join(PLUGIN_DIR, "ups-ws.py")
ROOT = os.path.normpath(os.path.join(PLUGIN_DIR, "..", "..", "..", ".."))


def test_declarations():
    ws = read_declarations(WS)["ups-ws"]
    assert set(ws["options"]) == {"i2c_bus", "address", "shutdown_percent", "poll_seconds"}
    assert ws["options"]["address"]["default"] == 0x42
    assert ws["capabilities"] == ("ui",)
    assert ws["provides"] == ("battery",)
    assert not ws["description"].startswith("Deprecated")


def _loader(tmp_path, *names):
    cfg = {"plugins": {name: {"enabled": True} for name in names}}
    return Loader(config=cfg, capture=CaptureInterface(str(tmp_path)),
                  connectivity=ConnectivityState(), services=ServiceRegistry(),
                  search_paths=[PLUGIN_DIR], logger=logging.getLogger("test.ups_ws"))


def test_ups_ws_loads_with_no_health_note(tmp_path):
    loader = _loader(tmp_path, "ups-ws")
    try:
        assert "ups-ws" in {r.name for r in loader.load_all()}
        assert loader.records["ups-ws"].instance.context.health_note == ""
    finally:
        loader.unload_all(timeout=5)


def test_ups_hat_is_gone():
    # ups-hat was removed in 1.0.11: no plugin, no shipped config table, and the
    # user-facing plugin lists don't offer it.
    assert not os.path.exists(os.path.join(PLUGIN_DIR, "ups-hat.py"))
    with open(os.path.join(ROOT, "builder", "extra", "files", "config.toml"), "rb") as fh:
        plugins = tomllib.load(fh)["plugins"]
    assert "ups-hat" not in plugins and "ups-ws" in plugins
    for rel in ("README.md", "docs/user-guide/managing-plugins.md",
                "docs/user-guide/user-guides.md", "docs/user-guide/config-reference.md",
                "builder/extra/files/kestrel_pi/kestrel_pi/userguide.py"):
        text = open(os.path.join(ROOT, rel), encoding="utf-8").read()
        assert "ups-ws" in text, rel
        assert not re.search(r"`ups-hat`(?! was| is)", text), rel
    assert not os.path.exists(os.path.join(ROOT, "docs", "user-guide", "ups-hat-setup.md"))
