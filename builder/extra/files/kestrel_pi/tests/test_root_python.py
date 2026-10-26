"""Root never runs the pi-owned venv's Python, nor Python without -I.

The CLI and the lib scripts run as root (sudo, systemd). The venv is pi-owned,
so a .pth file pi drops into its site-packages would run as root; and a bare
``python3 -``/``-c`` puts the cwd first on sys.path, so a module pi leaves in
the directory sudo was run from would be imported as root. These scan the shell
sources: every ``python3`` call passes ``-I``, and ``$PWN_VENV_PYTHON`` only
ever runs through ``run_as_pi``, also with ``-I`` (as pi it is no escalation,
but a stray ``json.py`` in the cwd would still shadow the stdlib)."""

import os
import re

import pytest

FILES = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))


def _shell_sources():
    found = []
    for directory in (FILES, os.path.join(FILES, "scripts")):
        for fname in sorted(os.listdir(directory)):
            if fname.endswith(".sh"):
                found.append(os.path.join(directory, fname))
    return found


def _code_lines(path):
    with open(path, encoding="utf-8") as handle:
        for number, line in enumerate(handle, 1):
            if line.lstrip().startswith("#"):
                continue
            yield number, line


@pytest.mark.parametrize("path", _shell_sources(), ids=os.path.basename)
def test_python3_always_isolated(path):
    bad = [(n, line.strip()) for n, line in _code_lines(path)
           if re.search(r"\bpython3(?![-.\w])(?!\s+-I\b)", line)]
    assert not bad, bad


@pytest.mark.parametrize("path", _shell_sources(), ids=os.path.basename)
def test_venv_python_only_as_pi(path):
    bad = [(n, line.strip()) for n, line in _code_lines(path)
           if "PWN_VENV_PYTHON" in line
           and not re.search(r"\brun_as_pi\s+\"\$PWN_VENV_PYTHON\"\s+-I\b", line)
           and not re.match(r"\s*PWN_VENV_PYTHON=", line)]
    assert not bad, bad
