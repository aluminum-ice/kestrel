"""Make the package importable when running pytest from a checkout (no install
needed): put the project root - the directory holding ``kestrel_pi/`` - on
sys.path."""

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def _private_web_state(tmp_path, monkeypatch):
    """Keep the dashboard's certificate and session key (kestrel_pi.webtls) in a
    per-test directory, never the real ~/.local/state of whoever runs the tests."""
    from kestrel_pi import webtls

    monkeypatch.setattr(webtls, "state_dir", lambda: str(tmp_path / "kestrel-web"))


@pytest.fixture(autouse=True)
def _no_leftover_panel_claims():
    """The eink_face library keeps which e-ink plugin drives the panel in a
    module-level list (one panel at a time). A test that loads an e-ink plugin
    without unloading it would leave its claim behind, so clear them after each
    test."""
    yield
    for name, mod in list(sys.modules.items()):
        if name.startswith("kestrel_plugin_libs.eink_face_"):
            with mod._claims_lock:
                mod._claims[:] = []
