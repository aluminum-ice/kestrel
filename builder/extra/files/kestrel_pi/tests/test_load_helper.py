"""kestrel_pi.load_helper: a plugin loads helper files that ship beside it."""

import logging
import os
import sys

import pytest

from kestrel_pi import load_helper
from kestrel_pi.plugins import HELPERS_PACKAGE


@pytest.fixture
def plugin_dir(tmp_path, monkeypatch):
    """A plugins directory with a plugin ``demo-hat.py`` (not imported here)."""
    (tmp_path / "demo-hat.py").write_text("# a plugin\n")
    yield tmp_path
    for name in [n for n in sys.modules if n.startswith(HELPERS_PACKAGE + ".demo_hat")]:
        del sys.modules[name]


def test_loads_a_helper_beside_the_plugin_once(plugin_dir):
    (plugin_dir / "_demo-hat_util.py").write_text("LOADS = []\nLOADS.append(1)\n")
    plugin = str(plugin_dir / "demo-hat.py")
    first = load_helper(plugin, "_demo-hat_util.py")
    assert first.LOADS == [1]
    assert first.__name__ == HELPERS_PACKAGE + ".demo_hat.util"
    assert load_helper(plugin, "_demo-hat_util.py") is first      # not re-run


def test_helpers_of_one_plugin_reach_each_other_with_relative_imports(plugin_dir):
    # How vendored code stays verbatim: ``from . import base`` finds the helper
    # loaded before it.
    (plugin_dir / "_demo-hat_base.py").write_text("VALUE = 42\n")
    (plugin_dir / "_demo-hat_driver.py").write_text("from . import base\nVALUE = base.VALUE\n")
    plugin = str(plugin_dir / "demo-hat.py")
    load_helper(plugin, "_demo-hat_base.py")
    assert load_helper(plugin, "_demo-hat_driver.py").VALUE == 42


@pytest.mark.parametrize("filename", [
    "_other_util.py",              # another plugin's helper
    "_demo-hat_.py",               # no name
    "util.py",                     # no prefix
    "_demo-hat_util.txt",          # not Python
    "../_demo-hat_util.py",        # not beside the plugin
    "sub/_demo-hat_util.py",
])
def test_only_the_plugins_own_helpers_beside_it(plugin_dir, filename):
    with pytest.raises(ValueError):
        load_helper(str(plugin_dir / "demo-hat.py"), filename)


def test_a_missing_helper_is_an_import_error(plugin_dir):
    with pytest.raises(ImportError):
        load_helper(str(plugin_dir / "demo-hat.py"), "_demo-hat_absent.py")


def test_a_helper_that_fails_to_import_is_not_cached(plugin_dir):
    helper = plugin_dir / "_demo-hat_flaky.py"
    helper.write_text("raise RuntimeError('no hardware')\n")
    plugin = str(plugin_dir / "demo-hat.py")
    with pytest.raises(RuntimeError):
        load_helper(plugin, "_demo-hat_flaky.py")
    helper.write_text("OK = True\n")
    assert load_helper(plugin, "_demo-hat_flaky.py").OK is True


def test_an_override_loads_its_own_copy(plugin_dir, tmp_path_factory):
    # An operator copy of a plugin elsewhere uses the helpers beside it, not the
    # shipped ones loaded earlier under the same name.
    (plugin_dir / "_demo-hat_util.py").write_text("WHERE = 'shipped'\n")
    other = tmp_path_factory.mktemp("etc-plugins")
    (other / "demo-hat.py").write_text("# an override\n")
    (other / "_demo-hat_util.py").write_text("WHERE = 'operator'\n")
    assert load_helper(str(plugin_dir / "demo-hat.py"), "_demo-hat_util.py").WHERE == "shipped"
    assert load_helper(str(other / "demo-hat.py"), "_demo-hat_util.py").WHERE == "operator"


def test_the_loader_never_loads_a_helper_as_a_plugin(plugin_dir):
    from kestrel_pi.capture import CaptureInterface
    from kestrel_pi.connectivity import ConnectivityState
    from kestrel_pi.context import ServiceRegistry
    from kestrel_pi.loader import Loader

    (plugin_dir / "_demo-hat_util.py").write_text("raise SystemExit('never imported')\n")
    loader = Loader(config={"plugins": {}}, capture=CaptureInterface(str(plugin_dir)),
                    connectivity=ConnectivityState(), services=ServiceRegistry(),
                    search_paths=[str(plugin_dir)],
                    logger=logging.getLogger("test.load_helper"))
    assert set(loader._scan()) == {"demo-hat"}
    assert os.path.exists(plugin_dir / "_demo-hat_util.py")
