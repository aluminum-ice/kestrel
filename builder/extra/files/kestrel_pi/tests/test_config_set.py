"""config-set: the table-scoped config.toml writer shared by the capture tool,
plugins-ctl and the web control.

Regression cover for `capture enable`/`disable`, which used a file-wide
`sed s/^enabled = .*/` and so also switched [daemon], [web] and every
[plugins.*] table (enabling every reference plugin, or turning the daemon
and web page off)."""

import importlib.machinery
import importlib.util
import io
import os
import shutil
import stat
import subprocess
import sys
import tomllib

import pytest

FILES = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
CONFIG_SET = os.path.join(FILES, "config-set.py")
PLUGINS_CTL = os.path.join(FILES, "plugins-ctl.py")
SHIPPED_CONFIG = os.path.join(FILES, "config.toml")


def _load(name, path):
    loader = importlib.machinery.SourceFileLoader(name, path)
    spec = importlib.util.spec_from_loader(name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


cs = _load("pwn_config_set_test", CONFIG_SET)


@pytest.fixture
def config(tmp_path):
    path = tmp_path / "config.toml"
    shutil.copy(SHIPPED_CONFIG, path)
    os.chmod(path, 0o640)
    return str(path)


def _read(path):
    with open(path, "rb") as handle:
        return tomllib.load(handle)


def _enabled_flags(data):
    flags = {
        "capture": data["capture"]["enabled"],
        "daemon": data["daemon"]["enabled"],
        "web": data["web"]["enabled"],
    }
    for name, table in data["plugins"].items():
        flags["plugins." + name] = table["enabled"]
    return flags


def _run_cli(config, *args, stdin=None):
    env = dict(os.environ, PWN_CONFIG=config)
    return subprocess.run(
        [sys.executable, CONFIG_SET, *args], env=env, input=stdin,
        capture_output=True, text=True, check=False,
    )


def test_capture_enable_touches_only_capture(config):
    before = _enabled_flags(_read(config))
    assert _run_cli(config, "capture", "enabled", "true").returncode == 0
    after = _enabled_flags(_read(config))
    assert after["capture"] is True
    # Nothing else moved: plugins stay off, daemon and web stay on.
    assert {k: v for k, v in after.items() if k != "capture"} == \
        {k: v for k, v in before.items() if k != "capture"}
    assert after["daemon"] is True and after["web"] is True
    assert not any(v for k, v in after.items() if k.startswith("plugins."))


def test_capture_disable_keeps_daemon_and_web_on(config):
    _run_cli(config, "capture", "enabled", "true")
    assert _run_cli(config, "capture", "enabled", "false").returncode == 0
    flags = _enabled_flags(_read(config))
    assert flags["capture"] is False
    assert flags["daemon"] is True and flags["web"] is True


def test_whole_file_otherwise_unchanged(config):
    before = _read(config)
    _run_cli(config, "capture", "interface", '"wlan1"')
    after = _read(config)
    before["capture"]["interface"] = "wlan1"
    assert after == before


def test_trailing_comment_kept(config):
    cs.set_value(config, "plugins.memtemp", "enabled", "true")
    with open(config, encoding="utf-8") as handle:
        text = handle.read()
    assert "enabled = true             # display plugin" in text


def test_missing_key_and_missing_table_are_added(config):
    cs.set_value(config, "plugins.wpa-sec", "api_key", cs.toml_string("abc"))
    cs.set_value(config, "plugins.newthing", "enabled", "true")
    data = _read(config)
    assert data["plugins"]["wpa-sec"]["api_key"] == "abc"
    assert data["plugins"]["newthing"]["enabled"] is True


def test_mode_preserved(config):
    cs.set_value(config, "capture", "enabled", "true")
    assert stat.S_IMODE(os.stat(config).st_mode) == 0o640


@pytest.mark.parametrize("table,key,literal", [
    ("capture", "enabled", "yes"),              # not TOML
    ("capture", "enabled", "true\nx = 1"),      # injection attempt
    ("capture", "bad key", "true"),
    ("capture]\n[x", "enabled", "true"),
    ("capture", "scan", "true"),                # would clash with [capture.scan]
])
def test_bad_edits_are_refused_and_nothing_written(config, table, key, literal):
    with open(config, encoding="utf-8") as handle:
        original = handle.read()
    with pytest.raises(cs.ConfigSetError):
        cs.set_value(config, table, key, literal)
    with open(config, encoding="utf-8") as handle:
        assert handle.read() == original


def test_string_from_stdin_round_trips(config):
    secret = 'p@ss "word" \\ ünïcode 🔑'
    result = _run_cli(config, "--string", "web", "password", stdin=secret)
    assert result.returncode == 0, result.stderr
    assert _read(config)["web"]["password"] == secret
    assert secret not in result.stdout + result.stderr


def test_control_characters_refused():
    with pytest.raises(cs.ConfigSetError):
        cs.toml_string("line1\nline2")


def test_plugins_ctl_set_uses_table_scoped_writer(config):
    env = dict(os.environ, PWN_CONFIG=config)
    result = subprocess.run(
        [sys.executable, PLUGINS_CTL, "set", "wpa-sec", "enabled", "true"],
        env=env, capture_output=True, text=True, check=False,
    )
    assert result.returncode == 0, result.stderr
    flags = _enabled_flags(_read(config))
    assert flags["plugins.wpa-sec"] is True
    assert flags["plugins.wigle"] is False and flags["capture"] is False


# -- array mode (--add-str / --del-str), used by the capture whitelist --------
def test_add_str_appends_and_is_idempotent(config):
    assert _run_cli(config, "--add-str", "capture", "whitelist",
                    stdin="Home WiFi").returncode == 0
    assert cs.current_string_array(config, "capture", "whitelist") == ["Home WiFi"]
    # Adding the same entry again is a no-op, not a duplicate.
    assert _run_cli(config, "--add-str", "capture", "whitelist",
                    stdin="Home WiFi").returncode == 0
    assert cs.current_string_array(config, "capture", "whitelist") == ["Home WiFi"]


def test_add_str_handles_quotes_and_backslashes(config):
    tricky = 'he said "hi"\\x'
    assert _run_cli(config, "--add-str", "capture", "whitelist",
                    stdin=tricky).returncode == 0
    assert cs.current_string_array(config, "capture", "whitelist") == [tricky]


def test_del_str_removes_only_that_entry(config):
    _run_cli(config, "--add-str", "capture", "whitelist", stdin="AA:BB:CC:DD:EE:FF")
    _run_cli(config, "--add-str", "capture", "whitelist", stdin="Home")
    assert _run_cli(config, "--del-str", "capture", "whitelist",
                    stdin="Home").returncode == 0
    assert cs.current_string_array(config, "capture", "whitelist") == ["AA:BB:CC:DD:EE:FF"]


def test_array_edit_touches_only_the_whitelist(config):
    before = _enabled_flags(_read(config))
    _run_cli(config, "--add-str", "capture", "whitelist", stdin="Home")
    assert _enabled_flags(_read(config)) == before


def test_add_str_refuses_empty_and_control_chars(config):
    assert _run_cli(config, "--add-str", "capture", "whitelist", stdin="").returncode == 1
    assert _run_cli(config, "--add-str", "capture", "whitelist",
                    stdin="bad\tval").returncode == 1
    assert cs.current_string_array(config, "capture", "whitelist") == []


# -- write lock (concurrent writers must not lose each other's change) -------
def test_concurrent_add_str_loses_nothing(config):
    # Parallel writers (two tabs, web + CLI) each read-modify-write the file;
    # the lock serializes them so every entry lands.
    env = dict(os.environ, PWN_CONFIG=config)
    procs = [subprocess.Popen(
        [sys.executable, CONFIG_SET, "--add-str", "capture", "whitelist"],
        env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, text=True) for _ in range(12)]
    for i, proc in enumerate(procs):
        proc.stdin.write("Net-%d" % i)
        proc.stdin.close()
    assert all(proc.wait(30) == 0 for proc in procs)
    whitelist = _read(config)["capture"]["whitelist"]
    assert {"Net-%d" % i for i in range(12)} <= set(whitelist)


def test_a_held_lock_makes_a_write_refuse_not_hang(config, monkeypatch):
    import fcntl

    monkeypatch.setattr(cs, "_LOCK_WAIT", 0.2)
    lock_path = os.path.join(os.path.dirname(config), ".config.toml.lock")
    fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        with pytest.raises(cs.ConfigSetError, match="busy"):
            cs.set_value(config, "capture", "enabled", "true")
    finally:
        os.close(fd)
    cs.set_value(config, "capture", "enabled", "true")       # free again
    assert _read(config)["capture"]["enabled"] is True


def test_config_lock_is_reentrant(config):
    with cs.config_lock(config):
        cs.set_value(config, "capture", "enabled", "true")   # nested: no deadlock
    assert _read(config)["capture"]["enabled"] is True
