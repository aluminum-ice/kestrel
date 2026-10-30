"""kestrel_pi.load_library: plugins share code through ``_lib_<name>.py`` files
that live with the plugins, never in core."""

import os
import sys

import pytest

from kestrel_pi import load_library
from kestrel_pi import plugins as plugin_api
from kestrel_pi.plugins import LIBRARIES_PACKAGE


def _lib(directory, name, body="", api="(1, 0)"):
    path = directory / ("_lib_%s.py" % name)
    path.write_text(("LIB_API = %s\n" % api if api is not None else "") + body)
    return path


@pytest.fixture
def dirs(tmp_path, monkeypatch):
    """A shipped plugins dir (``SHIPPED_PLUGINS_DIR`` points at it) with a
    plugin ``demo.py``, and an operator dir with a plugin ``mine.py``."""
    shipped = tmp_path / "shipped"
    operator = tmp_path / "operator"
    shipped.mkdir()
    operator.mkdir()
    (shipped / "demo.py").write_text("# a shipped plugin\n")
    (operator / "mine.py").write_text("# an operator plugin\n")
    monkeypatch.setattr(plugin_api, "SHIPPED_PLUGINS_DIR", str(shipped))
    yield shipped, operator
    for name in [n for n in sys.modules if n.startswith(LIBRARIES_PACKAGE + ".")]:
        del sys.modules[name]


def test_loads_a_library_beside_the_caller_once(dirs):
    shipped, _ = dirs
    _lib(shipped, "face", "LOADS = []\nLOADS.append(1)\n")
    first = load_library(str(shipped / "demo.py"), "face", api=1)
    assert first.LOADS == [1]
    assert first.__name__.startswith(LIBRARIES_PACKAGE + ".face_")
    assert load_library(str(shipped / "demo.py"), "face", api=1) is first   # not re-run


def test_two_plugins_share_one_module(dirs):
    shipped, _ = dirs
    (shipped / "other.py").write_text("# a second plugin\n")
    _lib(shipped, "face", "STATE = {}\n")
    a = load_library(str(shipped / "demo.py"), "face", api=1)
    b = load_library(str(shipped / "other.py"), "face", api=1)
    assert a is b


def test_an_operator_plugin_falls_back_to_the_shipped_library(dirs):
    shipped, operator = dirs
    _lib(shipped, "face", "WHERE = 'shipped'\n")
    assert load_library(str(operator / "mine.py"), "face", api=1).WHERE == "shipped"


def test_an_operator_plugin_uses_its_own_copy_first(dirs):
    shipped, operator = dirs
    _lib(shipped, "face", "WHERE = 'shipped'\n")
    _lib(operator, "face", "WHERE = 'operator'\n")
    assert load_library(str(operator / "mine.py"), "face", api=1).WHERE == "operator"
    # Both copies are loaded side by side, under different module names.
    assert load_library(str(shipped / "demo.py"), "face", api=1).WHERE == "shipped"


def test_a_shipped_plugin_never_sees_an_operator_library(dirs):
    shipped, operator = dirs
    _lib(operator, "face", "WHERE = 'operator'\n")
    with pytest.raises(ImportError, match="not found"):
        load_library(str(shipped / "demo.py"), "face", api=1)


def test_a_different_major_api_is_refused_before_the_library_runs(dirs, tmp_path):
    shipped, _ = dirs
    marker = tmp_path / "ran"
    _lib(shipped, "face", "open(%r, 'w').close()\n" % str(marker), api="(2, 3)")
    with pytest.raises(ImportError, match=r"demo\.py needs the face library API 1, found 2\.3"):
        load_library(str(shipped / "demo.py"), "face", api=1)
    assert not marker.exists()


def test_any_minor_api_is_accepted(dirs):
    shipped, _ = dirs
    _lib(shipped, "face", "OK = True\n", api="(1, 7)")
    assert load_library(str(shipped / "demo.py"), "face", api=1).OK


@pytest.mark.parametrize("api", [None, "1", "(1,)", "(1, 0, 0)", "('1', 0)", "(True, 0)",
                                 "(-1, 0)", "make_api()"])
def test_a_library_must_declare_its_api_as_a_literal(dirs, tmp_path, api):
    shipped, _ = dirs
    marker = tmp_path / "ran"
    _lib(shipped, "face", "open(%r, 'w').close()\n" % str(marker), api=api)
    with pytest.raises(ImportError, match="no LIB_API"):
        load_library(str(shipped / "demo.py"), "face", api=1)
    assert not marker.exists()


@pytest.mark.parametrize("name", ["", "../face", "Face", "a-b", "face.py", "sub/face", None])
def test_library_names(dirs, name):
    shipped, _ = dirs
    with pytest.raises(ValueError):
        load_library(str(shipped / "demo.py"), name, api=1)


@pytest.mark.parametrize("api", [-1, "1", 1.0, True, None])
def test_api_is_a_whole_number(dirs, api):
    shipped, _ = dirs
    _lib(shipped, "face")
    with pytest.raises(ValueError):
        load_library(str(shipped / "demo.py"), "face", api=api)


def test_a_missing_library_is_an_import_error(dirs):
    shipped, _ = dirs
    with pytest.raises(ImportError, match="_lib_absent.py not found"):
        load_library(str(shipped / "demo.py"), "absent", api=1)


def test_a_library_that_fails_to_import_is_not_cached(dirs):
    shipped, _ = dirs
    path = _lib(shipped, "flaky", "raise RuntimeError('no hardware')\n")
    with pytest.raises(RuntimeError):
        load_library(str(shipped / "demo.py"), "flaky", api=1)
    path.write_text("LIB_API = (1, 0)\nOK = True\n")
    assert load_library(str(shipped / "demo.py"), "flaky", api=1).OK is True


def test_a_library_loads_another_library(dirs):
    shipped, _ = dirs
    _lib(shipped, "base", "VALUE = 42\n")
    _lib(shipped, "face", "from kestrel_pi import load_library\n"
                          "base = load_library(__file__, 'base', api=1)\n"
                          "VALUE = base.VALUE\n")
    assert load_library(str(shipped / "demo.py"), "face", api=1).VALUE == 42


def test_a_dataclass_in_a_library_works(dirs):
    # Unlike a plugin module, a library is in sys.modules, so dataclass's
    # lookups by module name (and future annotations) work.
    shipped, _ = dirs
    (shipped / "_lib_face.py").write_text("from __future__ import annotations\n"
                                          "import dataclasses\n"
                                          "LIB_API = (1, 0)\n"
                                          "@dataclasses.dataclass\n"
                                          "class Inputs:\n"
                                          "    mood: str = 'calm'\n")
    assert load_library(str(shipped / "demo.py"), "face", api=1).Inputs().mood == "calm"


def test_a_library_cannot_load_private_helpers(dirs):
    from kestrel_pi import load_helper

    shipped, _ = dirs
    _lib(shipped, "face")
    (shipped / "__lib_face_x.py").write_text("X = 1\n")
    with pytest.raises(ValueError, match="helpers belong to a plugin"):
        load_helper(str(shipped / "_lib_face.py"), "__lib_face_x.py")


def test_the_daemon_searches_the_shipped_dir_first():
    from kestrel_pi import daemon

    assert daemon.DEFAULT_PLUGIN_PATHS[0] == plugin_api.SHIPPED_PLUGINS_DIR


def test_the_loader_never_loads_a_library_as_a_plugin(dirs):
    import logging

    from kestrel_pi.capture import CaptureInterface
    from kestrel_pi.connectivity import ConnectivityState
    from kestrel_pi.context import ServiceRegistry
    from kestrel_pi.loader import Loader

    shipped, _ = dirs
    _lib(shipped, "face", "raise SystemExit('never imported')\n")
    loader = Loader(config={"plugins": {}}, capture=CaptureInterface(str(shipped)),
                    connectivity=ConnectivityState(), services=ServiceRegistry(),
                    search_paths=[str(shipped)],
                    logger=logging.getLogger("test.load_library"))
    assert set(loader._scan()) == {"demo"}
    assert os.path.exists(shipped / "_lib_face.py")


def test_the_library_reference_lists_every_shipped_library():
    # docs/developer/plugin-libraries.md "Shipped libraries" names each
    # _lib_*.py file and its LIB_API, so a plugin developer finds them all.
    import re

    files = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", ".."))
    plugins_dir = os.path.join(files, "plugins")
    shipped = {}
    for fname in os.listdir(plugins_dir):
        if fname.startswith("_lib_") and fname.endswith(".py"):
            major, minor = plugin_api._lib_api(os.path.join(plugins_dir, fname))
            shipped[fname] = "%d.%d" % (major, minor)
    doc = os.path.join(files, "..", "..", "..", "docs", "developer", "plugin-libraries.md")
    text = open(doc, encoding="utf-8").read().split("## Shipped libraries", 1)[1]
    listed = dict(re.findall(r"^\| `[a-z0-9_]+` \| `(_lib_[a-z0-9_]+\.py)` \| ([0-9]+\.[0-9]+) \|",
                             text, re.M))
    assert listed == shipped
