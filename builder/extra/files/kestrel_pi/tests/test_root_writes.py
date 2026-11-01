"""Root never writes into a pi-owned directory.

The CLI often runs as root (sudo, or the root apply helper behind a socket any
pi process can reach). A file or link pi planted in its own directory could
redirect a root write, so trigger files are created *as pi* by ``pi_touch``,
never through a symlink, and nothing is chowned by path. These tests run the
``pi_touch`` snippet itself and keep kestrel.sh from growing a new root write
into a pi tree."""

import os
import re
import subprocess
import sys

FILES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..")
KESTREL_SH = os.path.join(FILES, "kestrel.sh")


def _source():
    with open(KESTREL_SH, encoding="utf-8") as handle:
        return handle.read()


def _pi_touch_snippet():
    """The Python that pi_touch runs (as pi), taken from kestrel.sh."""
    body = re.search(r"^pi_touch\(\) \{\n(.*?)\n\}", _source(), re.S | re.M).group(1)
    return re.search(r"-c '(.*?)' \"\$1\"", body, re.S).group(1)


def _pi_touch(path):
    return subprocess.run([sys.executable, "-I", "-c", _pi_touch_snippet(), str(path)],
                          capture_output=True, text=True)


def test_pi_touch_creates_a_missing_trigger(tmp_path):
    target = tmp_path / "train-request"
    assert _pi_touch(target).returncode == 0
    assert target.is_file() and target.read_text() == ""


def test_pi_touch_leaves_an_existing_trigger(tmp_path):
    target = tmp_path / "train-request"
    target.write_text("queued")
    assert _pi_touch(target).returncode == 0
    assert target.read_text() == "queued"


def test_pi_touch_refuses_a_planted_symlink(tmp_path):
    victim = tmp_path / "victim"
    victim.write_text("precious")
    link = tmp_path / "train-request"
    link.symlink_to(victim)
    assert _pi_touch(link).returncode != 0
    assert victim.read_text() == "precious"


def test_triggers_go_through_pi_touch():
    src = _source()
    for func in ("agent_request_train", "remote_train_touch_now"):
        body = re.search(r"^%s\(\) \{\n(.*?)\n\}" % func, src, re.S | re.M).group(1)
        assert "pi_touch" in body, func


# A root write into a pi-owned tree: truncating or creating a file there with a
# redirect, chowning a path there without -h, or install -o pi into it.
_PI_TREES = r'"\$(?:PWN_AGENT_DIR|dir|PWN_CAPTURE_DIR|store)/'
_FORBIDDEN = [
    re.compile(r":\s*>\s*" + _PI_TREES),
    re.compile(r"\bchown\s+(?!-[A-Za-z]*h)(?:-[A-Za-z]+\s+)*pi:pi\s+" + _PI_TREES),
    # (install -d -o pi only creates a missing directory, under a root-owned
    # parent, which is how pi_dir does it.)
    re.compile(r"\binstall\s+(?!-d\b)(?:-\S+\s+)*-o\s+pi\b"),
]


def test_the_patterns_catch_the_old_trigger_code():
    old = ['    : > "$PWN_AGENT_DIR/train-request"',
           '    chown pi:pi "$dir/.remote-train-now" 2>/dev/null || true',
           '    install -o pi -g pi -m 0644 "$model" "$PWN_AGENT_DIR/$model_file"']
    for line in old:
        assert any(p.search(line) for p in _FORBIDDEN), line
    assert not any(p.search('        install -d -o pi -g pi -m 0755 "$1"') for p in _FORBIDDEN)


def test_kestrel_sh_has_no_root_write_into_a_pi_tree():
    offenders = []
    for number, line in enumerate(_source().splitlines(), 1):
        code = line.split("#", 1)[0]
        if any(p.search(code) for p in _FORBIDDEN):
            offenders.append("%d: %s" % (number, line.strip()))
    assert offenders == []


def test_remote_train_agent_dir_is_kept_under_var_lib_kestrel():
    body = re.search(r"^rt_agent_dir\(\) \{\n(.*?)\n\}", _source(), re.S | re.M).group(1)
    assert "/var/lib/kestrel/?*" in body and "*..*" in body
