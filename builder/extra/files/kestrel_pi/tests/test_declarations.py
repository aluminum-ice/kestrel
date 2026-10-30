"""Static plugin declarations: one contract for root and the daemon.

Root (the ``kestrel plugins`` CLI under sudo, the ``plugins-ctl apply`` socket)
reads plugin declarations with ast and never imports a plugin or the pi-owned
venv. These check the two readers (``kestrel_pi.declarations`` and its mirror in
``plugins-ctl``) agree, that the loader refuses a plugin root can't read, and
that the CLI's read subcommands need neither kestrel_pi nor the plugin code."""

import importlib.machinery
import importlib.util
import logging
import os
import subprocess
import sys

import pytest

from kestrel_pi import declarations
from kestrel_pi.capture import CaptureInterface
from kestrel_pi.connectivity import ConnectivityState
from kestrel_pi.context import ServiceRegistry
from kestrel_pi.loader import Loader, discover_plugin_classes

FILES = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
PLUGINS_DIR = os.path.join(FILES, "plugins")
CTL_PATH = os.path.join(FILES, "plugins-ctl.py")
LOG = logging.getLogger("test.declarations")


def _load(name, path):
    loader = importlib.machinery.SourceFileLoader(name, path)
    spec = importlib.util.spec_from_loader(name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


ctl = _load("pwn_plugins_ctl_decl_test", CTL_PATH)

# A plugin whose module body leaves a marker when it is imported (executed).
SENTINEL = (
    "import pathlib\n"
    "pathlib.Path(__file__).with_suffix('.ran').write_text('ran')\n"
)

FIXTURES = {
    "plain": (
        "from kestrel_pi import Plugin\n"
        "class Plain(Plugin):\n"
        "    __description__ = 'a plain plugin'\n"
        "    __version__ = '1.2.3'\n"
        "    __target_api__ = 2\n"
        "    __capabilities__ = ('ui', 'network')\n"
        "    __options__ = {'key': {'type': 'str', 'required': True}, 'n': None}\n"
        "    __provides__ = ['svc']\n"
        "    __requires__ = ()\n"
        "    __optional_requires__ = ('other',)\n"
    ),
    "annotated": (
        "from kestrel_pi import plugins\n"
        "class Annotated(plugins.Plugin):\n"
        "    __plugin_name__ = 'renamed'\n"
        "    __capabilities__: tuple = ('ui',)\n"
        "    __options__: dict = {}\n"
    ),
    "bare": (
        "from kestrel_pi import Plugin\n"
        "class Bare(Plugin):\n"
        "    pass\n"
    ),
    "computed": (
        "from kestrel_pi import Plugin\n"
        "CAPS = ('network',)\n"
        "class Computed(Plugin):\n"
        "    __capabilities__ = CAPS\n"
    ),
    "badshape": (
        "from kestrel_pi import Plugin\n"
        "class BadShape(Plugin):\n"
        "    __options__ = {'key': 'str'}\n"
    ),
    "indirect": (
        "from kestrel_pi import Plugin\n"
        "class Base(Plugin):\n"
        "    pass\n"
        "class Child(Base):\n"
        "    __capabilities__ = ('network',)\n"
    ),
    "mutated": (
        "from kestrel_pi import Plugin\n"
        "class Mutated(Plugin):\n"
        "    __capabilities__ = ('ui',)\n"
        "Mutated.__capabilities__ = ('ui', 'network')\n"
    ),
    "decorated": (
        "def handshake(event):\n"
        "    pass\n"
    ),
    "choices": (
        "from kestrel_pi import Plugin\n"
        "class Choices(Plugin):\n"
        "    __options__ = {'variant': {'type': 'str', 'default': 'v4',\n"
        "                               'choices': ['v4', 'v3', 'v2']},\n"
        "                   'rotation': {'type': 'int', 'default': 0, 'choices': (0, 180)}}\n"
    ),
    "choices_default": (
        "from kestrel_pi import Plugin\n"
        "class ChoicesDefault(Plugin):\n"
        "    __options__ = {'variant': {'type': 'str', 'default': 'v9',\n"
        "                               'choices': ['v4', 'v3']}}\n"
    ),
    "choices_bool": (
        "from kestrel_pi import Plugin\n"
        "class ChoicesBool(Plugin):\n"
        "    __options__ = {'on': {'type': 'bool', 'choices': [True, False]}}\n"
    ),
    "choices_empty": (
        "from kestrel_pi import Plugin\n"
        "class ChoicesEmpty(Plugin):\n"
        "    __options__ = {'variant': {'type': 'str', 'choices': []}}\n"
    ),
    "choices_mixed": (
        "from kestrel_pi import Plugin\n"
        "class ChoicesMixed(Plugin):\n"
        "    __options__ = {'rotation': {'type': 'int', 'choices': [0, '90']}}\n"
    ),
    "choices_comma": (
        "from kestrel_pi import Plugin\n"
        "class ChoicesComma(Plugin):\n"
        "    __options__ = {'variant': {'type': 'str', 'choices': ['a,b', 'c']}}\n"
    ),
    "syntax": "class Broken(Plugin:\n",
}


def _read(reader, path):
    try:
        return ("ok", reader(path))
    except Exception as exc:  # noqa: BLE001 - compare the failure mode too
        return ("error", type(exc).__name__)


# A "_" file beside the plugins is a plugin's helper (kestrel_pi.load_helper),
# never a plugin: the loader and plugins-ctl both skip it.
SHIPPED_PLUGINS = sorted(f for f in os.listdir(PLUGINS_DIR)
                         if f.endswith(".py") and not f.startswith("_"))


@pytest.mark.parametrize("path", [os.path.join(PLUGINS_DIR, f) for f in SHIPPED_PLUGINS])
def test_shipped_plugins_read_statically_and_match_their_classes(path):
    declared = declarations.read_declarations(path)
    assert declared, path
    # The loader's check: the imported classes match what root reads.
    found = discover_plugin_classes(path)
    assert sorted(name for name, _cls in found) == sorted(declared)


def test_readers_do_not_drift(tmp_path):
    paths = [os.path.join(PLUGINS_DIR, f) for f in sorted(os.listdir(PLUGINS_DIR))
             if f.endswith(".py")]
    for name, body in FIXTURES.items():
        (tmp_path / (name + ".py")).write_text(body)
        paths.append(str(tmp_path / (name + ".py")))
    for path in paths:
        assert _read(declarations.read_declarations, path) == \
            _read(ctl.read_declarations, path), path
    assert ctl.DECLARED == declarations.DECLARED


def test_reader_defaults_match_the_plugin_base_class():
    from kestrel_pi import Plugin

    assert declarations.class_declarations(Plugin, "x") == dict(
        declarations.normalize({}, "x"), name="x")


def test_static_reads(tmp_path):
    for name, body in FIXTURES.items():
        (tmp_path / (name + ".py")).write_text(body)
    plain = declarations.read_declarations(str(tmp_path / "plain.py"))["plain"]
    assert plain["capabilities"] == ("ui", "network")
    assert plain["provides"] == ("svc",)
    assert plain["options"]["n"] is None
    assert plain["version"] == "1.2.3" and plain["target_api"] == 2
    assert list(declarations.read_declarations(str(tmp_path / "annotated.py"))) == ["renamed"]
    assert declarations.read_declarations(str(tmp_path / "bare.py"))["bare"]["target_api"] == 1
    # Only a direct Plugin subclass is a plugin statically.
    assert list(declarations.read_declarations(str(tmp_path / "indirect.py"))) == ["indirect"]
    assert declarations.read_declarations(str(tmp_path / "decorated.py")) == {}
    for bad in ("computed", "badshape", "syntax"):
        with pytest.raises(declarations.DeclarationError):
            declarations.read_declarations(str(tmp_path / (bad + ".py")))


def _loader(tmp_path, names):
    for name in names:
        (tmp_path / (name + ".py")).write_text(FIXTURES[name])
    config = {"plugins": {name: {"enabled": True, "key": "v"} for name in names}}
    return Loader(config=config, capture=CaptureInterface(str(tmp_path)),
                  connectivity=ConnectivityState(), services=ServiceRegistry(),
                  search_paths=[str(tmp_path)], logger=LOG)


@pytest.mark.parametrize("name", ["computed", "indirect", "mutated", "decorated",
                                  "badshape"])
def test_loader_refuses_what_root_cannot_read(tmp_path, name):
    loader = _loader(tmp_path, [name, "plain"])
    loaded = loader.load_all()
    assert [r.name for r in loaded] == ["plain"]
    record = loader.records[name]
    assert record.state == "failed"
    assert record.reason.startswith("unreadable declarations:")


def test_loader_does_not_import_a_statically_unreadable_plugin(tmp_path):
    (tmp_path / "computed.py").write_text(SENTINEL + FIXTURES["computed"])
    loader = _loader(tmp_path, [])
    loader.config = {"plugins": {"computed": {"enabled": True}}}
    loader.load_all()
    assert loader.records["computed"].state == "failed"
    assert not (tmp_path / "computed.ran").exists()


def _run_ctl(tmp_path, *args, plugin_paths=None):
    """Run plugins-ctl as the CLI does (system Python, isolated mode, from an
    unrelated cwd), with kestrel_pi made unimportable."""
    config = tmp_path / "config.toml"
    if not config.exists():
        config.write_text("[plugins.wpa-sec]\nenabled = true\n")
    code = ("import runpy, sys; sys.modules['kestrel_pi'] = None; "
            "sys.argv = sys.argv[1:]; runpy.run_path(sys.argv[0], run_name='__main__')")
    env = dict(os.environ, PWN_CONFIG=str(config),
               PWN_PLUGIN_PATHS=plugin_paths or PLUGINS_DIR)
    return subprocess.run([sys.executable, "-I", "-c", code, CTL_PATH, *args],
                          cwd=str(tmp_path), env=env, capture_output=True, text=True,
                          timeout=60, check=False)


def test_cli_reads_need_no_venv_and_run_no_plugin_code(tmp_path):
    listed = _run_ctl(tmp_path, "list")
    assert listed.returncode == 0, listed.stderr
    rows = {line.split("\t")[0]: line.split("\t")[1] for line in listed.stdout.splitlines()}
    shipped = {os.path.splitext(f)[0] for f in SHIPPED_PLUGINS}
    assert set(rows) == shipped
    assert rows["wpa-sec"] == "enabled" and rows["gps"] == "disabled"

    caps = _run_ctl(tmp_path, "caps", "wpa-sec")
    assert caps.returncode == 0 and "network" in caps.stdout.split()
    info = _run_ctl(tmp_path, "info", "gps")
    assert info.returncode == 0 and "capabilities:  location, capture-read, ui" in info.stdout
    schema = _run_ctl(tmp_path, "schema", "wpa-sec")
    assert schema.returncode == 0 and schema.stdout.strip()
    missing = _run_ctl(tmp_path, "missing", "wpa-sec")
    assert missing.returncode == 0
    assert _run_ctl(tmp_path, "caps", "nope").returncode == 1
    assert _run_ctl(tmp_path, "exists", "nope").returncode == 1


def test_cli_lists_an_unreadable_plugin_as_invalid_without_running_it(tmp_path):
    operator = tmp_path / "operator"
    operator.mkdir()
    (operator / "computed.py").write_text(SENTINEL + FIXTURES["computed"])
    (operator / "plain.py").write_text(SENTINEL + FIXTURES["plain"])
    paths = PLUGINS_DIR + ":" + str(operator)
    listed = _run_ctl(tmp_path, "list", plugin_paths=paths)
    rows = {line.split("\t")[0]: line.split("\t")[1] for line in listed.stdout.splitlines()}
    assert rows["computed"] == "invalid" and rows["plain"] == "disabled"
    caps = _run_ctl(tmp_path, "caps", "computed", plugin_paths=paths)
    assert caps.returncode == 1 and "aren't plain literals" in caps.stderr
    # Still disable-able: the CLI checks existence, not readability.
    assert _run_ctl(tmp_path, "exists", "computed", plugin_paths=paths).returncode == 0
    assert _run_ctl(tmp_path, "info", "plain", plugin_paths=paths).returncode == 0
    assert not list(operator.glob("*.ran"))


@pytest.mark.parametrize("name, ok", [
    ("choices", True), ("choices_default", False), ("choices_bool", False),
    ("choices_empty", False), ("choices_mixed", False), ("choices_comma", False),
])
def test_choices_are_checked_by_both_readers(tmp_path, name, ok):
    path = tmp_path / (name + ".py")
    path.write_text(FIXTURES[name])
    for reader, error in ((declarations.read_declarations, declarations.DeclarationError),
                          (ctl.read_declarations, ctl.DeclarationError)):
        if ok:
            opts = reader(str(path))[name]["options"]
            assert opts["variant"]["choices"] == ["v4", "v3", "v2"]
        else:
            with pytest.raises(error, match="choices"):
                reader(str(path))

