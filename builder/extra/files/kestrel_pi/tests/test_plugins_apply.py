"""plugins-ctl apply: the root side of the web page's plugin controls.

It runs as root, so these check that it only ever writes the named plugin's
table, only declared options of the declared type, enforces web auth, the
required options and the network authorized-use ack, never echoes a secret,
and never imports (executes) a plugin file."""

import importlib.machinery
import importlib.util
import io
import json
import os
import shutil
import sys
import tomllib

import pytest

FILES = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
PLUGINS_DIR = os.path.join(FILES, "plugins")


def _load(name, path):
    loader = importlib.machinery.SourceFileLoader(name, path)
    spec = importlib.util.spec_from_loader(name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


ctl = _load("pwn_plugins_ctl_test", os.path.join(FILES, "plugins-ctl.py"))
cs = ctl.config_set_module()


@pytest.fixture
def env(tmp_path, monkeypatch):
    config = tmp_path / "config.toml"
    shutil.copy(os.path.join(FILES, "config.toml"), config)
    monkeypatch.setattr(ctl, "CONFIG", str(config))
    monkeypatch.setattr(ctl, "PLUGIN_PATHS", [PLUGINS_DIR])
    monkeypatch.setattr(ctl, "ACK_FILE", str(tmp_path / "capture-ack"))
    return tmp_path


def _auth_on(env):
    path = str(env / "config.toml")
    cs.set_many(path, "web", [("auth", '"basic"'), ("username", '"pi"'),
                              ("password", '"hunter2"')])


def _config(env):
    with open(env / "config.toml", "rb") as handle:
        return tomllib.load(handle)


def _plugins_enabled(env):
    return {name: t["enabled"] for name, t in _config(env)["plugins"].items()}


def test_declarations_read_statically(env):
    plugins = ctl.static_plugins()
    assert set(plugins) == {"gps", "memtemp", "session-stats", "wigle",
                            "wpa-sec", "backup", "updates",
                            "remote-train", "eink-ws213", "eink-ws27", "ups-ws", "ups-wsc", "ups-wsd", "ups-wse", "downloads", "map",
                            "ntfy", "telegram"}
    # downloads only serves the operator's own browser: no ack-gated network.
    assert "network" not in plugins["downloads"][0]["capabilities"]
    wpa, _src = plugins["wpa-sec"]
    assert "network" in wpa["capabilities"]
    assert wpa["options"]["api_key"] == {"type": "str", "required": True, "secret": True}


def test_plugin_files_are_parsed_not_imported(env, monkeypatch):
    marker = env / "imported"
    plugdir = env / "plugins"
    plugdir.mkdir()
    (plugdir / "trap.py").write_text(
        "open(%r, 'w').write('ran')\n"
        "from kestrel_pi.plugins import Plugin\n"
        "class Trap(Plugin):\n"
        "    __capabilities__ = ('network',)\n"
        "    __options__ = {'x': {'type': 'int', 'default': 1}}\n" % str(marker)
    )
    monkeypatch.setattr(ctl, "PLUGIN_PATHS", [str(plugdir)])
    plugins = ctl.static_plugins()
    assert plugins["trap"][0]["options"] == {"x": {"type": "int", "default": 1}}
    assert not marker.exists()


def test_computed_declarations_are_refused(env, monkeypatch):
    plugdir = env / "plugins"
    plugdir.mkdir()
    (plugdir / "computed.py").write_text(
        "from kestrel_pi.plugins import Plugin\n"
        "class Computed(Plugin):\n"
        "    __options__ = {k: {'type': 'int'} for k in ('a', 'b')}\n"
    )
    (plugdir / "odd.py").write_text(
        "class Odd(Plugin):\n"
        "    __options__ = {'a': 'int'}\n"
    )
    monkeypatch.setattr(ctl, "PLUGIN_PATHS", [str(plugdir)])
    _auth_on(env)
    for name in ("computed", "odd"):
        reply = ctl.apply_change({"plugin": name, "enabled": True})
        assert reply["ok"] is False and "aren't plain literals" in reply["error"]


def test_refused_while_web_auth_is_off(env):
    cs.set_value(str(env / "config.toml"), "web", "auth", '"none"')
    before = _config(env)
    reply = ctl.apply_change({"plugin": "memtemp", "enabled": True})
    assert reply["ok"] is False and "web auth" in reply["error"]
    assert _config(env) == before


def test_system_auth_is_control_ready():
    # The default auth mode (device login) enables the controls with no stored
    # credential; basic needs both fields; none is off.
    assert ctl.web_control_ready({"web": {"auth": "system"}}) is True
    assert ctl.web_control_ready({"web": {"auth": "basic", "username": "pi"}}) is False
    assert ctl.web_control_ready({"web": {"auth": "none"}}) is False


class _Spent:
    def __init__(self, hashed):
        self.sp_pwdp = hashed


def test_verify_system_password_matches_only_the_right_password():
    # A fake shadow lookup + a fake crypt (so the test needs no real account or
    # the removed-in-3.13 crypt module): crypt echoes salt$+password.
    shadow = {"pi": _Spent("$6$salt$HASH")}
    crypt_fn = lambda pw, h: "$6$salt$" + pw   # noqa: E731
    getspnam = shadow.__getitem__

    def verify(user, pw):
        return ctl.verify_system_password(user, pw, getspnam=getspnam,
                                          crypt_fn=lambda p, h: "$6$salt$HASH"
                                          if p == "right" else "$6$salt$NO")

    assert verify("pi", "right") is True
    assert verify("pi", "wrong") is False
    # Unknown user, and locked / password-less accounts never match.
    assert ctl.verify_system_password("nobody", "x", getspnam=getspnam,
                                      crypt_fn=crypt_fn) is False
    for bad in ("", "*", "!", "!locked"):
        locked = {"u": _Spent(bad)}
        assert ctl.verify_system_password("u", "x", getspnam=locked.__getitem__,
                                          crypt_fn=crypt_fn) is False
    assert ctl.verify_system_password("pi", "", getspnam=getspnam,
                                      crypt_fn=crypt_fn) is False


def test_cmd_checkauth_round_trip(monkeypatch, capsys):
    monkeypatch.setattr(ctl, "verify_system_password",
                        lambda u, p: u == "pi" and p == "secret")
    monkeypatch.setattr(sys, "stdin",
                        io.TextIOWrapper(io.BytesIO(
                            json.dumps({"username": "pi", "password": "secret"}).encode())))
    rc = ctl.cmd_checkauth()
    out, err = capsys.readouterr()
    assert rc == 0 and json.loads(out) == {"ok": True}
    assert "secret" not in err and "pi: ok" in err


def test_cmd_checkauth_denies_and_never_echoes_the_password(monkeypatch, capsys):
    monkeypatch.setattr(ctl, "verify_system_password", lambda u, p: False)
    monkeypatch.setattr(sys, "stdin",
                        io.TextIOWrapper(io.BytesIO(
                            json.dumps({"username": "pi", "password": "hunter2"}).encode())))
    rc = ctl.cmd_checkauth()
    out, err = capsys.readouterr()
    # A denial is an answer: exit 0, so no failed kestrel-web-auth@ unit lingers.
    assert rc == 0 and json.loads(out) == {"ok": False}
    assert "hunter2" not in out and "hunter2" not in err


def test_enable_needs_required_options(env):
    _auth_on(env)
    reply = ctl.apply_change({"plugin": "wpa-sec", "enabled": True})
    assert reply["ok"] is False and reply["missing"] == ["api_key"]
    assert _plugins_enabled(env)["wpa-sec"] is False


def test_network_plugin_needs_ack_and_nothing_written_until_given(env):
    _auth_on(env)
    change = {"plugin": "wpa-sec", "set": {"api_key": "0123456789"}, "enabled": True}
    reply = ctl.apply_change(change)
    assert reply["ok"] is False and reply["need_ack"] is True
    assert "api_key" not in _config(env)["plugins"]["wpa-sec"]
    assert not os.path.exists(ctl.ACK_FILE)

    reply = ctl.apply_change(dict(change, ack=True))
    assert reply == {"ok": True, "changed": ["api_key", "enabled"], "restart": True}
    table = _config(env)["plugins"]["wpa-sec"]
    # An all-digit key is still written as a string (its declared type).
    assert table["api_key"] == "0123456789" and table["enabled"] is True
    assert open(ctl.ACK_FILE).read().startswith("acknowledged ")
    # Only wpa-sec moved; capture, daemon and web are untouched.
    others = {k: v for k, v in _plugins_enabled(env).items() if k != "wpa-sec"}
    assert not any(others.values())
    data = _config(env)
    assert data["capture"]["enabled"] is False
    assert data["daemon"]["enabled"] is True and data["web"]["enabled"] is True


def test_stored_ack_is_reused(env):
    _auth_on(env)
    open(ctl.ACK_FILE, "w").write("acknowledged earlier\n")
    reply = ctl.apply_change({"plugin": "wpa-sec", "set": {"api_key": "k"},
                              "enabled": True})
    assert reply["ok"] is True


def test_empty_text_keeps_the_stored_secret(env):
    _auth_on(env)
    cs.set_value(str(env / "config.toml"), "plugins.wpa-sec", "api_key", '"keep-me"')
    open(ctl.ACK_FILE, "w").write("acknowledged earlier\n")
    reply = ctl.apply_change({"plugin": "wpa-sec", "set": {"api_key": ""},
                              "enabled": True})
    assert reply["ok"] is True and reply["changed"] == ["enabled"]
    assert _config(env)["plugins"]["wpa-sec"]["api_key"] == "keep-me"


@pytest.mark.parametrize("updates", [
    {"timeout": "30"},             # text for an int
    {"timeout": True},             # bool is not an int
    {"download_results": "yes"},   # text for a bool
    {"endpoint": 5},               # number for text
    {"endpoint": "a\nb"},          # control characters
    {"endpoint": "x" * 5000},      # too long
    {"nope": 1},                   # undeclared option
    {"enabled": True},             # framework key, not an option
])
def test_bad_values_refused(env, updates):
    _auth_on(env)
    before = _config(env)
    reply = ctl.apply_change({"plugin": "wpa-sec", "set": updates})
    assert reply["ok"] is False
    assert _config(env) == before


@pytest.mark.parametrize("change", [
    "not a dict", {"plugin": "../etc"}, {"plugin": "nosuch"},
    {"plugin": "memtemp", "enabled": "yes"}, {"plugin": "memtemp", "set": []},
])
def test_bad_requests_refused(env, change):
    _auth_on(env)
    assert ctl.apply_change(change)["ok"] is False


def test_unchanged_value_means_no_restart(env):
    _auth_on(env)
    reply = ctl.apply_change({"plugin": "memtemp", "enabled": False})
    assert reply == {"ok": True, "changed": [], "restart": False}


def test_toggle_plain_plugin_needs_no_ack(env):
    _auth_on(env)
    assert ctl.apply_change({"plugin": "memtemp", "enabled": True})["ok"] is True
    assert _plugins_enabled(env)["memtemp"] is True
    assert ctl.apply_change({"plugin": "memtemp", "enabled": False})["ok"] is True
    assert _plugins_enabled(env)["memtemp"] is False
    assert not os.path.exists(ctl.ACK_FILE)


def _run_apply(monkeypatch, capsys, payload):
    restarts = []
    monkeypatch.setattr(ctl, "restart_daemon", lambda: restarts.append(1))
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(payload)))
    rc = ctl.cmd_apply()
    out, err = capsys.readouterr()
    return rc, json.loads(out), err, restarts


def test_cmd_apply_round_trip_never_echoes_the_secret(env, monkeypatch, capsys):
    _auth_on(env)
    open(ctl.ACK_FILE, "w").write("acknowledged earlier\n")
    secret = "s3cr3t-api-key"
    payload = json.dumps({"plugin": "wpa-sec", "set": {"api_key": secret},
                          "enabled": True}).encode()
    rc, reply, err, restarts = _run_apply(monkeypatch, capsys, payload)
    assert rc == 0 and reply["ok"] is True and restarts == [1]
    assert secret not in json.dumps(reply) and secret not in err
    assert "api_key(secret)" in err   # audit line names the option only


def test_end_to_end_over_a_socket(env):
    """The daemon's client (web.apply_change) against the helper run the way
    the socket unit runs it (Accept=yes: the connection is its stdin/stdout,
    the system Python in isolated mode), with a fake systemd-run on PATH."""
    import socket
    import subprocess
    import threading

    from kestrel_pi import web

    _auth_on(env)
    bindir = env / "bin"
    bindir.mkdir()
    marker = env / "restarted"
    fake = bindir / "systemd-run"
    fake.write_text("#!/bin/sh\necho \"$@\" > %s\n" % marker)
    fake.chmod(0o755)
    sock_path = str(env / "apply.sock")
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(sock_path)
    server.listen(1)
    child_env = dict(os.environ, PWN_CONFIG=str(env / "config.toml"),
                     PWN_PLUGIN_PATHS=PLUGINS_DIR,
                     PWN_CAPTURE_ACK_FILE=str(env / "capture-ack"),
                     PATH=str(bindir) + os.pathsep + os.environ.get("PATH", ""))
    result = {}

    def serve():
        conn, _ = server.accept()
        with conn:
            proc = subprocess.run(
                [sys.executable, "-I", os.path.join(FILES, "plugins-ctl.py"), "apply"],
                stdin=conn, stdout=conn, stderr=subprocess.PIPE, env=child_env,
                timeout=30, check=False)
        result["stderr"] = proc.stderr.decode()

    thread = threading.Thread(target=serve)
    thread.start()
    try:
        reply = web.apply_change({"plugin": "memtemp", "enabled": True},
                                 socket_path=sock_path, timeout=30)
    finally:
        thread.join(30)
        server.close()
    assert reply == {"ok": True, "changed": ["enabled"], "restart": True}
    assert _plugins_enabled(env)["memtemp"] is True
    assert "restart kestrel-daemon.service" in marker.read_text()
    assert "memtemp: changed enabled" in result["stderr"]


def test_cmd_apply_bad_input(env, monkeypatch, capsys):
    _auth_on(env)
    rc, reply, _err, restarts = _run_apply(monkeypatch, capsys, b"{not json")
    # A refusal is an answer: exit 0, so no failed kestrel-plugins-apply@ unit.
    assert rc == 0 and reply["ok"] is False and restarts == []
    big = b'{"plugin": "' + b"a" * (ctl.MAX_REQUEST + 10) + b'"}'
    rc, reply, _err, _r = _run_apply(monkeypatch, capsys, big)
    assert reply == {"ok": False, "error": "request too large"}


# -- capture enable/disable from the web (Phase 3.5) -------------------------
def _stub_cli(monkeypatch, rc=0, out=""):
    calls = []
    monkeypatch.setattr(ctl, "_run_capture_cli",
                        lambda args: (calls.append(list(args)), (rc, out))[1])
    return calls


def test_capture_enable_needs_the_ack_before_running_the_cli(env, monkeypatch):
    _auth_on(env)
    calls = _stub_cli(monkeypatch)
    reply = ctl.apply_change({"target": "capture", "enabled": True})
    assert reply["ok"] is False and reply.get("need_ack") is True
    assert calls == []                 # the CLI is not run until authorized


def test_capture_enable_with_ack_runs_the_cli_with_yes(env, monkeypatch):
    _auth_on(env)
    calls = _stub_cli(monkeypatch, out="capture enabled on wlan1")
    reply = ctl.apply_change({"target": "capture", "enabled": True, "ack": True})
    assert reply == {"ok": True, "capture": True, "restart": False}
    assert calls == [["capture", "enable", "--yes"]]


def test_capture_enable_skips_the_ack_when_already_acknowledged(env, monkeypatch):
    _auth_on(env)
    (env / "capture-ack").write_text("acknowledged\n")   # the shared ACK_FILE
    calls = _stub_cli(monkeypatch)
    reply = ctl.apply_change({"target": "capture", "enabled": True})
    assert reply["ok"] is True
    assert calls == [["capture", "enable", "--yes"]]


def test_capture_enable_surfaces_a_missing_adapter(env, monkeypatch):
    _auth_on(env)
    _stub_cli(monkeypatch, rc=1,
              out="kestrel: no monitor-capable capture adapter found")
    reply = ctl.apply_change({"target": "capture", "enabled": True, "ack": True})
    assert reply["ok"] is False
    assert "no monitor-capable capture adapter" in reply["error"]


def test_capture_disable_runs_the_cli(env, monkeypatch):
    _auth_on(env)
    calls = _stub_cli(monkeypatch, out="capture disabled")
    reply = ctl.apply_change({"target": "capture", "enabled": False})
    assert reply == {"ok": True, "capture": False, "restart": False}
    assert calls == [["capture", "disable"]]


def test_capture_refused_while_web_auth_is_off(env, monkeypatch):
    cs.set_value(str(env / "config.toml"), "web", "auth", '"none"')
    calls = _stub_cli(monkeypatch)
    reply = ctl.apply_change({"target": "capture", "enabled": True, "ack": True})
    assert reply["ok"] is False and "web auth" in reply["error"]
    assert calls == []


# -- reboot / shutdown from the web (Phase 3.5) -----------------------------
def test_power_reboot_returns_the_power_flag(env):
    _auth_on(env)
    reply = ctl.apply_change({"target": "power", "action": "reboot"})
    assert reply == {"ok": True, "power": "reboot"}


def test_power_unknown_action_is_refused(env):
    _auth_on(env)
    reply = ctl.apply_change({"target": "power", "action": "halt"})
    assert reply["ok"] is False and "unknown power action" in reply["error"]


def test_power_works_regardless_of_web_auth(env):
    # Power is device-safety automation (a UPS plugin's low-battery shutdown), so
    # unlike capture/plugin controls it is NOT gated on web auth at the socket;
    # the web buttons are gated at the route instead.
    cs.set_value(str(env / "config.toml"), "web", "auth", '"none"')
    reply = ctl.apply_change({"target": "power", "action": "shutdown"})
    assert reply == {"ok": True, "power": "shutdown"}


def test_run_power_defers_the_command_past_the_reply(monkeypatch):
    calls = []
    monkeypatch.setattr(ctl.subprocess, "run", lambda *a, **k: calls.append(a[0]))
    ctl.run_power("shutdown")
    assert calls == [["systemd-run", "--quiet", "--collect", "--on-active=2",
                      "systemctl", "poweroff"]]
    calls.clear()
    ctl.run_power("bogus")        # unknown action runs nothing
    assert calls == []


def test_update_apply_returns_the_update_flag(env):
    _auth_on(env)
    reply = ctl.apply_change({"target": "update", "action": "apply", "tag": "0.7.0"})
    assert reply == {"ok": True, "update": "apply", "update_tag": "0.7.0",
                     "update_no_backup": False}
    beta = ctl.apply_change({"target": "update", "action": "apply",
                             "tag": "0.7.0-beta.2"})
    assert beta["ok"] is True and beta["update_tag"] == "0.7.0-beta.2"


def test_update_apply_threads_no_backup_flag(env):
    _auth_on(env)
    reply = ctl.apply_change({"target": "update", "action": "apply",
                              "tag": "0.7.0", "no_backup": True})
    assert reply["ok"] is True and reply["update_no_backup"] is True
    # Coerced to a plain bool, never echoed back attacker-shaped.
    truthy = ctl.apply_change({"target": "update", "action": "apply",
                               "tag": "0.7.0", "no_backup": "yes"})
    assert truthy["update_no_backup"] is True


def test_run_update_appends_no_backup_only_when_asked(monkeypatch):
    seen = []
    monkeypatch.setattr(ctl.subprocess, "run",
                        lambda cmd, **kw: seen.append(list(cmd)))
    ctl.run_update("0.7.0", no_backup=True)
    ctl.run_update("0.7.0", no_backup=False)
    with_nb, without_nb = seen
    # Both go through the fixed-name transient unit, the shipped CLI, and pin
    # the confirmed tag with --expect; only the asked-for one skips the backup.
    for cmd in (with_nb, without_nb):
        assert "--unit=kestrel-update" in cmd
        assert cmd[-6:-1] == [ctl.PWN_TOOL, "update", "apply", "--yes", "--expect"] \
            or ("update" in cmd and "--expect" in cmd and "0.7.0" in cmd)
    assert with_nb[-1] == "--no-backup"
    assert "--no-backup" not in without_nb


def test_update_bad_action_is_refused(env):
    _auth_on(env)
    assert ctl.apply_change({"target": "update", "action": "check"})["ok"] is False
    assert ctl.apply_change({"target": "update"})["ok"] is False


@pytest.mark.parametrize("tag", [None, "", "master", "--force", "0.7.0; reboot",
                                 "0.7.0-rc.1", 7])
def test_update_apply_needs_the_confirmed_release_tag(env, tag):
    # The confirmed tag is passed on to the CLI as an argument, so only a
    # release-shaped tag is accepted.
    _auth_on(env)
    change = {"target": "update", "action": "apply"}
    if tag is not None:
        change["tag"] = tag
    assert ctl.apply_change(change)["ok"] is False


def test_update_refused_while_web_auth_is_off(env):
    # Unlike power (device-safety automation), an in-place update is a web-only
    # control, so it stays gated on web auth at the socket like capture/plugins.
    cs.set_value(str(env / "config.toml"), "web", "auth", '"none"')
    reply = ctl.apply_change({"target": "update", "action": "apply"})
    assert reply["ok"] is False and "web auth" in reply["error"]


def test_run_update_defers_the_cli_past_the_reply(monkeypatch):
    calls = []
    monkeypatch.setattr(ctl.subprocess, "run", lambda *a, **k: calls.append(a[0]))
    ctl.run_update("0.7.0")
    # A fixed unit name: a second request while one is pending is refused by
    # systemd instead of running alongside. --expect pins the confirmed tag.
    assert calls == [["systemd-run", "--quiet", "--collect", "--on-active=2",
                      "--unit=kestrel-update",
                      ctl.PWN_TOOL, "update", "apply", "--yes", "--expect", "0.7.0"]]


def _backup_configured(env):
    cfg = str(env / "config.toml")
    cs.set_value(cfg, "plugins.backup", "enabled", "true")
    cs.set_value(cfg, "plugins.backup", "host", '"nas.local"')
    cs.set_value(cfg, "plugins.backup", "user", '"pi"')
    cs.set_value(cfg, "plugins.backup", "path", '"pwn-backups/dev01"')
    # Passwordless SSH is set up (the device key exists): point ssh_key at a
    # real file so the restore pre-flight passes.
    key = env / "id_ed25519"
    key.write_text("KEY")
    cs.set_value(cfg, "plugins.backup", "ssh_key", '"%s"' % key)


def test_backup_restore_returns_the_flag_when_enabled_and_configured(env):
    _auth_on(env)
    _backup_configured(env)
    reply = ctl.apply_change({"target": "backup-restore", "action": "restore"})
    assert reply == {"ok": True, "backup_restore": True, "restart": False}


def test_backup_restore_does_not_key_check_in_the_sandboxed_helper(env):
    # The root apply helper runs under a hardened unit with ProtectHome=yes, so
    # /home is empty to it and a key-existence check there ALWAYS false-fails
    # (os.path.exists("/home/pi/.ssh/...") is False even when the key is there).
    # The passwordless-SSH check therefore lives in the daemon route, not here:
    # the helper must accept a seemingly-missing key and defer the restore,
    # which records its own error if the key really is absent.
    _auth_on(env)
    cfg = str(env / "config.toml")
    cs.set_value(cfg, "plugins.backup", "enabled", "true")
    cs.set_value(cfg, "plugins.backup", "host", '"nas.local"')
    cs.set_value(cfg, "plugins.backup", "user", '"pi"')
    cs.set_value(cfg, "plugins.backup", "path", '"pwn-backups/dev01"')
    cs.set_value(cfg, "plugins.backup", "ssh_key", '"%s"' % (env / "missing-key"))
    reply = ctl.apply_change({"target": "backup-restore", "action": "restore"})
    assert reply == {"ok": True, "backup_restore": True, "restart": False}


def test_backup_restore_refused_when_plugin_disabled(env):
    _auth_on(env)
    reply = ctl.apply_change({"target": "backup-restore", "action": "restore"})
    assert reply["ok"] is False and "enable the backup plugin" in reply["error"]


def test_backup_restore_refused_when_remote_not_configured(env):
    _auth_on(env)
    cs.set_value(str(env / "config.toml"), "plugins.backup", "enabled", "true")
    reply = ctl.apply_change({"target": "backup-restore", "action": "restore"})
    assert reply["ok"] is False and "set the backup remote" in reply["error"]


def test_backup_restore_bad_action_is_refused(env):
    _auth_on(env)
    _backup_configured(env)
    assert ctl.apply_change({"target": "backup-restore"})["ok"] is False


def test_backup_restore_refused_while_web_auth_is_off(env):
    _backup_configured(env)
    cs.set_value(str(env / "config.toml"), "web", "auth", '"none"')
    reply = ctl.apply_change({"target": "backup-restore", "action": "restore"})
    assert reply["ok"] is False and "web auth" in reply["error"]


def test_run_backup_restore_defers_the_cli_past_the_reply(monkeypatch):
    calls = []
    monkeypatch.setattr(ctl.subprocess, "run", lambda *a, **k: calls.append(a[0]))
    ctl.run_backup_restore()
    assert calls == [["systemd-run", "--quiet", "--collect", "--on-active=2",
                      ctl.PWN_TOOL, "backup", "restore", "--yes"]]


def test_backup_now_returns_the_flag_when_enabled_and_configured(env):
    _auth_on(env)
    _backup_configured(env)
    reply = ctl.apply_change({"target": "backup", "action": "now"})
    assert reply == {"ok": True, "backup_now": True, "restart": False}


def test_backup_now_does_not_key_check_in_the_sandboxed_helper(env):
    # Same reason as restore: the ProtectHome'd helper cannot see /home, so the
    # key check lives in the daemon, not here; a seemingly-missing key still defers
    # (the deferred `backup now` records its own error if the key is really gone).
    _auth_on(env)
    cfg = str(env / "config.toml")
    cs.set_value(cfg, "plugins.backup", "enabled", "true")
    cs.set_value(cfg, "plugins.backup", "host", '"nas.local"')
    cs.set_value(cfg, "plugins.backup", "user", '"pi"')
    cs.set_value(cfg, "plugins.backup", "path", '"pwn-backups/dev01"')
    cs.set_value(cfg, "plugins.backup", "ssh_key", '"%s"' % (env / "missing-key"))
    reply = ctl.apply_change({"target": "backup", "action": "now"})
    assert reply == {"ok": True, "backup_now": True, "restart": False}


def test_backup_now_refused_when_plugin_disabled(env):
    _auth_on(env)
    reply = ctl.apply_change({"target": "backup", "action": "now"})
    assert reply["ok"] is False and "enable the backup plugin" in reply["error"]


def test_backup_now_refused_when_remote_not_configured(env):
    _auth_on(env)
    cs.set_value(str(env / "config.toml"), "plugins.backup", "enabled", "true")
    reply = ctl.apply_change({"target": "backup", "action": "now"})
    assert reply["ok"] is False and "set the backup remote" in reply["error"]


def test_backup_now_bad_action_is_refused(env):
    _auth_on(env)
    _backup_configured(env)
    assert ctl.apply_change({"target": "backup"})["ok"] is False


def test_backup_now_refused_while_web_auth_is_off(env):
    _backup_configured(env)
    cs.set_value(str(env / "config.toml"), "web", "auth", '"none"')
    reply = ctl.apply_change({"target": "backup", "action": "now"})
    assert reply["ok"] is False and "web auth" in reply["error"]


def test_run_backup_now_defers_the_cli_past_the_reply(monkeypatch):
    calls = []
    monkeypatch.setattr(ctl.subprocess, "run", lambda *a, **k: calls.append(a[0]))
    ctl.run_backup_now()
    assert calls == [["systemd-run", "--quiet", "--collect", "--on-active=2",
                      ctl.PWN_TOOL, "backup", "now"]]


def _remote_train_configured(env):
    cfg = str(env / "config.toml")
    cs.set_value(cfg, "plugins.remote-train", "enabled", "true")
    cs.set_value(cfg, "plugins.remote-train", "train_host", '"desktop.lan"')
    cs.set_value(cfg, "plugins.remote-train", "train_user", '"me"')
    cs.set_value(cfg, "plugins.remote-train", "train_path", '"/srv/pwn-train"')
    # Passwordless SSH set up: point ssh_key at a real file so pre-flight passes.
    key = env / "id_ed25519"
    key.write_text("KEY")
    cs.set_value(cfg, "plugins.remote-train", "ssh_key", '"%s"' % key)


def test_remote_train_now_shells_the_cli_when_ready(env, monkeypatch):
    _auth_on(env)
    _remote_train_configured(env)
    calls = _stub_cli(monkeypatch, out="reconcile queued")
    reply = ctl.apply_change({"target": "remote-train", "action": "train-now"})
    assert reply == {"ok": True, "remote_train": True, "restart": False}
    assert calls == [["remote-train", "now"]]


def test_remote_train_now_does_not_key_check_in_the_sandboxed_helper(env, monkeypatch):
    # The root apply helper runs under a hardened unit with ProtectHome=yes, so
    # /home is empty to it and a key-existence check there ALWAYS false-fails.
    # The passwordless-SSH check therefore lives in the daemon route, not here:
    # the helper must NOT refuse on a seemingly-missing key - it fires the CLI,
    # which runs the plugin's own real check.
    _auth_on(env)
    cfg = str(env / "config.toml")
    cs.set_value(cfg, "plugins.remote-train", "enabled", "true")
    cs.set_value(cfg, "plugins.remote-train", "train_host", '"desktop.lan"')
    cs.set_value(cfg, "plugins.remote-train", "train_user", '"me"')
    cs.set_value(cfg, "plugins.remote-train", "train_path", '"/srv/pwn-train"')
    cs.set_value(cfg, "plugins.remote-train", "ssh_key", '"%s"' % (env / "missing-key"))
    calls = _stub_cli(monkeypatch)
    reply = ctl.apply_change({"target": "remote-train", "action": "train-now"})
    assert reply == {"ok": True, "remote_train": True, "restart": False}
    assert calls == [["remote-train", "now"]]   # the CLI is still invoked


def test_remote_train_now_refused_when_plugin_disabled(env):
    _auth_on(env)
    reply = ctl.apply_change({"target": "remote-train", "action": "train-now"})
    assert reply["ok"] is False and "enable the remote-train plugin" in reply["error"]


def test_remote_train_now_refused_when_host_not_configured(env):
    _auth_on(env)
    cs.set_value(str(env / "config.toml"), "plugins.remote-train", "enabled", "true")
    reply = ctl.apply_change({"target": "remote-train", "action": "train-now"})
    assert reply["ok"] is False and "set the training host" in reply["error"]


def test_remote_train_now_bad_action_is_refused(env):
    _auth_on(env)
    _remote_train_configured(env)
    assert ctl.apply_change({"target": "remote-train"})["ok"] is False


def test_remote_train_now_refused_while_web_auth_is_off(env):
    _remote_train_configured(env)
    cs.set_value(str(env / "config.toml"), "web", "auth", '"none"')
    reply = ctl.apply_change({"target": "remote-train", "action": "train-now"})
    assert reply["ok"] is False and "web auth" in reply["error"]


def test_remote_train_now_surfaces_cli_failure(env, monkeypatch):
    _auth_on(env)
    _remote_train_configured(env)
    _stub_cli(monkeypatch, rc=1,
              out="ssh: connect to host desktop.lan port 22: No route to host")
    reply = ctl.apply_change({"target": "remote-train", "action": "train-now"})
    assert reply["ok"] is False and "No route to host" in reply["error"]


# -- update check from the web (force an immediate release check) ------------
def _updates_enabled(env):
    cs.set_value(str(env / "config.toml"), "plugins.updates", "enabled", "true")


def test_update_check_shells_the_cli_when_enabled(env, monkeypatch):
    _auth_on(env)
    _updates_enabled(env)
    calls = _stub_cli(monkeypatch, out="asked the updates plugin to check")
    reply = ctl.apply_change({"target": "update-check", "action": "check-now"})
    assert reply == {"ok": True, "update_check": True, "restart": False}
    assert calls == [["update", "check"]]


def test_update_check_refused_when_plugin_disabled(env, monkeypatch):
    _auth_on(env)
    calls = _stub_cli(monkeypatch)
    reply = ctl.apply_change({"target": "update-check", "action": "check-now"})
    assert reply["ok"] is False and "enable the updates plugin" in reply["error"]
    assert calls == []                 # the CLI is never run


def test_update_check_bad_action_is_refused(env, monkeypatch):
    _auth_on(env)
    _updates_enabled(env)
    calls = _stub_cli(monkeypatch)
    assert ctl.apply_change({"target": "update-check"})["ok"] is False
    assert ctl.apply_change({"target": "update-check", "action": "apply"})["ok"] is False
    assert calls == []


def test_update_check_refused_while_web_auth_is_off(env, monkeypatch):
    _updates_enabled(env)
    cs.set_value(str(env / "config.toml"), "web", "auth", '"none"')
    calls = _stub_cli(monkeypatch)
    reply = ctl.apply_change({"target": "update-check", "action": "check-now"})
    assert reply["ok"] is False and "web auth" in reply["error"]
    assert calls == []


def test_update_check_surfaces_cli_failure(env, monkeypatch):
    _auth_on(env)
    _updates_enabled(env)
    _stub_cli(monkeypatch, rc=1,
              out="kestrel: the updates plugin is not enabled")
    reply = ctl.apply_change({"target": "update-check", "action": "check-now"})
    assert reply["ok"] is False and "not enabled" in reply["error"]


def test_cmd_apply_runs_the_power_command_after_replying(env, monkeypatch, capsys):
    _auth_on(env)
    ran = []
    monkeypatch.setattr(ctl, "run_power", lambda action: ran.append(action))
    payload = json.dumps({"target": "power", "action": "reboot"}).encode()
    rc, reply, _err, restarts = _run_apply(monkeypatch, capsys, payload)
    assert rc == 0 and reply == {"ok": True, "power": "reboot"}
    assert ran == ["reboot"] and restarts == []


# -- learning agent enable/disable/set from the web (Phase 4) ----------------
# The agent algorithm/policy values select a Python class to import, so the
# socket enum-validates them before shelling to the trusted CLI; an unknown
# value is rejected and the CLI is never run.
def test_agent_enum_matches_the_policies_module():
    # The root helper's allow-list must not drift from the real enum.
    from kestrel_pi.agent import policies
    assert set(ctl.AGENT_ALGORITHMS) == set(policies.ALGORITHMS)
    assert set(ctl.AGENT_POLICIES) == set(policies.POLICIES)


def test_agent_set_runs_the_cli_with_validated_values(env, monkeypatch):
    _auth_on(env)
    calls = _stub_cli(monkeypatch, out="ok")
    reply = ctl.apply_change({"target": "agent", "algorithm": "DQN",
                              "policy": "learned"})
    assert reply["ok"] is True
    assert set(reply["changed"]) == {"algorithm", "policy"}
    assert calls == [["agent", "set", "--algorithm", "DQN", "--policy", "learned"]]


def test_agent_enable_runs_enable_with_the_values(env, monkeypatch):
    _auth_on(env)
    calls = _stub_cli(monkeypatch, out="agent enabled")
    reply = ctl.apply_change({"target": "agent", "enabled": True,
                              "algorithm": "PPO"})
    assert reply["ok"] is True and "enabled" in reply["changed"]
    assert calls == [["agent", "enable", "--yes", "--algorithm", "PPO"]]


def test_agent_disable_runs_disable(env, monkeypatch):
    _auth_on(env)
    calls = _stub_cli(monkeypatch, out="agent disabled")
    reply = ctl.apply_change({"target": "agent", "enabled": False})
    assert reply["ok"] is True and reply["changed"] == ["enabled"]
    assert calls == [["agent", "disable"]]


@pytest.mark.parametrize("change", [
    {"target": "agent", "algorithm": "DreamerV3"},     # not in the enum
    {"target": "agent", "policy": "psychic"},          # not in the enum
    {"target": "agent", "enabled": "yes"},             # not a bool
])
def test_agent_bad_values_are_refused_and_never_run_the_cli(env, monkeypatch, change):
    _auth_on(env)
    calls = _stub_cli(monkeypatch)
    reply = ctl.apply_change(change)
    assert reply["ok"] is False
    assert calls == []


def test_agent_surfaces_a_cli_failure(env, monkeypatch):
    _auth_on(env)
    _stub_cli(monkeypatch, rc=1,
              out="kestrel: capture must be enabled before the agent")
    reply = ctl.apply_change({"target": "agent", "enabled": True})
    assert reply["ok"] is False
    assert "capture must be enabled" in reply["error"]


def test_agent_silent_cli_failure_points_at_the_journal(env, monkeypatch):
    # A CLI that exits non-zero with no output (e.g. a `set -e` abort before it
    # could log) must not collapse to a bare generic string: the web page needs
    # an actionable hint, including the exit code and where to look.
    _auth_on(env)
    _stub_cli(monkeypatch, rc=3, out="")
    reply = ctl.apply_change({"target": "agent", "enabled": True})
    assert reply["ok"] is False
    assert "agent" in reply["error"] and "3" in reply["error"]
    assert "journalctl" in reply["error"]


def test_agent_train_now_requests_via_cli(env, monkeypatch):
    # A "train now" is its own action: it shells `agent train-now` (which drops
    # the request file) and never rewrites algorithm/policy or restarts.
    _auth_on(env)
    calls = _stub_cli(monkeypatch, out="requested a training run")
    reply = ctl.apply_change({"target": "agent", "action": "train"})
    assert reply["ok"] is True and reply["changed"] == ["train"]
    assert reply.get("restart") is False
    assert calls == [["agent", "train-now"]]


def test_agent_unknown_action_is_refused(env, monkeypatch):
    _auth_on(env)
    calls = _stub_cli(monkeypatch)
    reply = ctl.apply_change({"target": "agent", "action": "bogus"})
    assert reply["ok"] is False
    assert calls == []


def test_agent_train_now_surfaces_cli_failure(env, monkeypatch):
    _auth_on(env)
    _stub_cli(monkeypatch, rc=1, out="kestrel: must be pi or root")
    reply = ctl.apply_change({"target": "agent", "action": "train"})
    assert reply["ok"] is False
    assert "pi or root" in reply["error"]


def test_agent_refused_while_web_auth_is_off(env, monkeypatch):
    cs.set_value(str(env / "config.toml"), "web", "auth", '"none"')
    calls = _stub_cli(monkeypatch)
    reply = ctl.apply_change({"target": "agent", "algorithm": "PPO"})
    assert reply["ok"] is False and "web auth" in reply["error"]
    assert calls == []


# -- capture whitelist (add / remove) ------------------------------------
def test_whitelist_add_runs_the_cli(env, monkeypatch):
    _auth_on(env)
    calls = _stub_cli(monkeypatch, out="whitelisted")
    reply = ctl.apply_change({"target": "capture-whitelist",
                              "action": "add", "entry": "Home WiFi"})
    assert reply["ok"] is True and reply["whitelist"] == "add"
    assert calls == [["capture", "whitelist", "add", "Home WiFi"]]


def test_whitelist_remove_runs_the_cli(env, monkeypatch):
    _auth_on(env)
    calls = _stub_cli(monkeypatch, out="removed")
    reply = ctl.apply_change({"target": "capture-whitelist",
                              "action": "remove", "entry": "AA:BB:CC:DD:EE:FF"})
    assert reply["ok"] is True
    assert calls == [["capture", "whitelist", "remove", "AA:BB:CC:DD:EE:FF"]]


def test_whitelist_trims_the_entry(env, monkeypatch):
    _auth_on(env)
    calls = _stub_cli(monkeypatch, out="ok")
    ctl.apply_change({"target": "capture-whitelist",
                      "action": "add", "entry": "  Home  "})
    assert calls == [["capture", "whitelist", "add", "Home"]]


@pytest.mark.parametrize("change", [
    {"target": "capture-whitelist", "action": "add", "entry": ""},
    {"target": "capture-whitelist", "action": "add", "entry": "   "},
    {"target": "capture-whitelist", "action": "nope", "entry": "x"},
    {"target": "capture-whitelist", "action": "add", "entry": "x" * 65},
    {"target": "capture-whitelist", "action": "add", "entry": "bad\tchar"},
    {"target": "capture-whitelist", "action": "add", "entry": 123},
])
def test_whitelist_bad_requests_never_run_the_cli(env, monkeypatch, change):
    _auth_on(env)
    calls = _stub_cli(monkeypatch)
    reply = ctl.apply_change(change)
    assert reply["ok"] is False
    assert calls == []


def test_whitelist_refused_while_web_auth_is_off(env, monkeypatch):
    cs.set_value(str(env / "config.toml"), "web", "auth", '"none"')
    calls = _stub_cli(monkeypatch)
    reply = ctl.apply_change({"target": "capture-whitelist",
                              "action": "add", "entry": "Home"})
    assert reply["ok"] is False and "web auth" in reply["error"]
    assert calls == []


# -- restart Bluetooth pairing from the web (Phase 1 re-pair) ----------------
def test_bt_pair_runs_the_cli(env, monkeypatch):
    _auth_on(env)
    calls = _stub_cli(monkeypatch, out="opening the onboarding window")
    reply = ctl.apply_change({"target": "bt-pair"})
    assert reply == {"ok": True, "bt_pair": True, "restart": False}
    assert calls == [["network", "bt-pair"]]


def test_bt_pair_surfaces_a_cli_failure(env, monkeypatch):
    _auth_on(env)
    _stub_cli(monkeypatch, rc=1, out="this command must be run as root")
    reply = ctl.apply_change({"target": "bt-pair"})
    assert reply["ok"] is False and "must be run as root" in reply["error"]


def test_bt_pair_refused_while_web_auth_is_off(env, monkeypatch):
    cs.set_value(str(env / "config.toml"), "web", "auth", '"none"')
    calls = _stub_cli(monkeypatch)
    reply = ctl.apply_change({"target": "bt-pair"})
    assert reply["ok"] is False and "web auth" in reply["error"]
    assert calls == []


# -- config-set: the web Settings page's one-key config.toml edits -----------
def _set_config(table, key, value):
    return {"target": "config-set", "table": table, "key": key, "value": value}


def test_config_set_writes_one_allowlisted_key(env):
    _auth_on(env)
    before = _config(env)["capture"]["output"].get("mode")
    reply = ctl.apply_change(_set_config("capture.scan", "dwell_seconds", 10))
    assert reply["ok"] and reply["changed"] == ["capture.scan.dwell_seconds"]
    assert reply["restart"] is False
    cfg = _config(env)
    assert cfg["capture"]["scan"]["dwell_seconds"] == 10
    # only the one key moved (the single-key config-set guard still applies)
    assert cfg["capture"]["output"].get("mode") == before


def test_config_set_needs_web_auth(env):
    # The shipped config defaults to auth="system" (ready); turn it off.
    cs.set_value(str(env / "config.toml"), "web", "auth", '"none"')
    reply = ctl.apply_change(_set_config("capture.scan", "dwell_seconds", 10))
    assert reply["ok"] is False and "web auth" in reply["error"]
    assert _config(env)["capture"]["scan"].get("dwell_seconds") != 10


def test_config_set_rejects_keys_not_on_the_allowlist(env):
    _auth_on(env)
    for table, key in (("capture", "enabled"), ("web", "port"),
                       ("daemon", "enabled"), ("nope", "nope")):
        reply = ctl.apply_change(_set_config(table, key, 1))
        assert reply["ok"] is False and "can't be edited" in reply["error"]


def test_config_set_validates_type_and_range(env):
    _auth_on(env)
    for table, key, value in (
        ("capture.scan", "dwell_seconds", 0),        # below min
        ("capture.scan", "dwell_seconds", "x"),      # not an int
        ("capture.scan", "mode", "bogus"),           # not an enum member
        ("capture.angryoxide", "headless", 1),       # int, not bool
        ("agent", "train_max_temp_c", 200),          # above max
        ("web", "probe_url", "http://x\x01y"),       # control character
    ):
        reply = ctl.apply_change(_set_config(table, key, value))
        assert reply["ok"] is False, (table, key, value)


def test_config_set_accepts_each_scalar_type(env):
    _auth_on(env)
    assert ctl.apply_change(_set_config("capture.scan", "mode", "autohunt"))["ok"]
    assert ctl.apply_change(_set_config("capture.angryoxide", "headless", False))["ok"]
    assert ctl.apply_change(_set_config("agent.reward", "idle_penalty", 0.2))["ok"]
    assert ctl.apply_change(_set_config("capture", "interface", "wlan1"))["ok"]
    cfg = _config(env)
    assert cfg["capture"]["scan"]["mode"] == "autohunt"
    assert cfg["capture"]["angryoxide"]["headless"] is False
    assert cfg["agent"]["reward"]["idle_penalty"] == 0.2
    assert cfg["capture"]["interface"] == "wlan1"


def test_config_set_daemon_change_asks_for_a_restart(env):
    _auth_on(env)
    reply = ctl.apply_change(_set_config("daemon", "epoch_seconds", 90))
    assert reply["ok"] and reply["restart"] is True
    assert "dashboard" in reply["restart_hint"].lower()
    assert _config(env)["daemon"]["epoch_seconds"] == 90


def test_config_set_capture_change_does_not_restart(env):
    _auth_on(env)
    reply = ctl.apply_change(_set_config("capture.housekeeping", "max_run_mb", 256))
    assert reply["ok"] and reply["restart"] is False
    assert "capture" in reply["restart_hint"].lower()
    assert _config(env)["capture"]["housekeeping"]["max_run_mb"] == 256


def test_config_settable_capture_keys_match_what_capture_reads():
    # The page once wrote [capture] max_run_mb while the launcher reads
    # [capture.housekeeping] max_run_mb, so the setting silently did nothing.
    # Any key the capture scripts read by a name the page also offers must be
    # offered under the same table.
    import re
    read = set()
    for script in ("capture-launch.sh", "kestrel.sh"):
        with open(os.path.join(FILES, script), encoding="utf-8") as handle:
            read |= set(re.findall(r"\bcfg (capture\.[a-z_.]+)", handle.read()))
    settable = {k for k in ctl.CONFIG_SETTABLE if k.startswith("capture.")}
    for key in sorted(read):
        leaf = key.rsplit(".", 1)[1]
        if leaf == "enabled":   # every table has one; they are different keys
            continue
        offered = {k for k in settable if k.rsplit(".", 1)[1] == leaf}
        assert not offered or key in offered, (key, offered)


# -- what root acts on stays root's to set -----------------------------------

@pytest.mark.parametrize("opt, value", [
    ("host", "https://forge.example.org"),
    ("repo", "someone/fork"),
    ("token", "not-a-real-token"),
])
def test_root_only_update_options_refused_from_the_socket(env, opt, value):
    # Root's `update apply` installs code from where these point, so a pi
    # process that can reach the socket must not be able to change them.
    _auth_on(env)
    before = dict(_config(env)["plugins"]["updates"])
    reply = ctl.apply_change({"plugin": "updates", "set": {opt: value}})
    assert reply["ok"] is False and "terminal" in reply["error"]
    assert _config(env)["plugins"]["updates"] == before


def test_root_only_does_not_block_the_plugins_other_options(env):
    _auth_on(env)
    reply = ctl.apply_change({"plugin": "updates", "set": {"beta": True}})
    assert reply["ok"] is True and reply["changed"] == ["beta"]
    assert _config(env)["plugins"]["updates"]["beta"] is True


def test_root_only_options_are_still_settable_from_the_cli(env, capsys):
    # `sudo kestrel plugins ... --set` goes through cmd_set, not the socket.
    assert ctl.cmd_set("updates", "host", "https://forge.example.org") == 0
    assert _config(env)["plugins"]["updates"]["host"] == "https://forge.example.org"


@pytest.mark.parametrize("value", [
    "/", "/etc", "/var/lib/kestrel", "/var/lib/kestrel/capture",
    "/var/lib/kestrel/capture/../../..", "/var/lib/kestrel/capture/./a",
    "/var/lib/kestrel/captureX", "var/lib/kestrel/capture/archive", "",
])
def test_archive_dir_must_be_inside_the_capture_store(env, value):
    _auth_on(env)
    reply = ctl.apply_change(_set_config("capture.housekeeping", "archive_dir", value))
    assert reply["ok"] is False and "inside" in reply["error"], value
    assert _config(env)["capture"]["housekeeping"]["archive_dir"] == \
        "/var/lib/kestrel/capture/archive"


def test_archive_dir_inside_the_store_is_accepted(env):
    _auth_on(env)
    for value in ("/var/lib/kestrel/capture/archive", "/var/lib/kestrel/capture/old/sets"):
        assert ctl.apply_change(_set_config("capture.housekeeping", "archive_dir", value))["ok"]


@pytest.mark.parametrize("value", [
    "../../etc/passwd", "/etc/passwd", "sub/all.hc22000", "..", ".hidden", "",
])
def test_single_file_must_be_a_plain_file_name(env, value):
    _auth_on(env)
    reply = ctl.apply_change(_set_config("capture.output", "single_file", value))
    assert reply["ok"] is False and "file name" in reply["error"], value


def test_single_file_plain_name_is_accepted(env):
    _auth_on(env)
    assert ctl.apply_change(_set_config("capture.output", "single_file", "all-2.hc22000"))["ok"]
    assert _config(env)["capture"]["output"]["single_file"] == "all-2.hc22000"


def test_set_stdin_reads_the_value_from_stdin(env, monkeypatch):
    # The CLI hands secrets over on stdin, so they never appear in this
    # process's arguments (readable by every local user).
    import io

    monkeypatch.setattr(ctl.sys, "stdin", io.StringIO("placeholder-key\n"))
    assert ctl.main(["plugins-ctl", "set-stdin", "wpa-sec", "api_key"]) == 0
    assert _config(env)["plugins"]["wpa-sec"]["api_key"] == "placeholder-key"


def test_set_stdin_keeps_the_value_verbatim(env, monkeypatch):
    import io

    monkeypatch.setattr(ctl.sys, "stdin", io.StringIO('a "quoted" value\r\n'))
    assert ctl.cmd_set_stdin("wpa-sec", "api_key") == 0
    assert _config(env)["plugins"]["wpa-sec"]["api_key"] == 'a "quoted" value'


# -- options with choices ------------------------------------------------------

PICKER = (
    "from kestrel_pi import Plugin\n"
    "class Picker(Plugin):\n"
    "    __capabilities__ = ('ui',)\n"
    "    __options__ = {\n"
    "        'variant': {'type': 'str', 'default': 'v4', 'choices': ['v4', 'v3', 'v2']},\n"
    "        'rotation': {'type': 'int', 'default': 0, 'choices': [0, 180]},\n"
    "        'label': {'type': 'str', 'default': ''},\n"
    "    }\n"
)


@pytest.fixture
def picker(env, monkeypatch):
    extra = env / "operator-plugins"
    extra.mkdir()
    (extra / "picker.py").write_text(PICKER)
    monkeypatch.setattr(ctl, "PLUGIN_PATHS", [PLUGINS_DIR, str(extra)])
    _auth_on(env)
    return env


def test_the_socket_writes_only_one_of_the_choices(picker):
    reply = ctl.apply_change({"plugin": "picker", "set": {"variant": "v2", "rotation": 180}})
    assert reply["ok"] is True
    table = _config(picker)["plugins"]["picker"]
    assert table["variant"] == "v2" and table["rotation"] == 180
    for bad in ({"variant": "V2"}, {"variant": "v9"}, {"rotation": 90}, {"rotation": "180"}):
        reply = ctl.apply_change({"plugin": "picker", "set": bad})
        assert reply["ok"] is False, bad
    assert _config(picker)["plugins"]["picker"] == table             # nothing written
    reply = ctl.apply_change({"plugin": "picker", "set": {"variant": "v9"}})
    assert reply["error"] == "variant must be one of: v4, v3, v2"


def test_the_terminal_writes_only_one_of_the_choices(picker, capsys):
    assert ctl.cmd_set("picker", "variant", "v3") == 0
    assert ctl.cmd_set("picker", "rotation", "180") == 0
    assert ctl.cmd_set("picker", "variant", "v9") == 2
    assert "variant must be one of: v4, v3, v2" in capsys.readouterr().err
    assert ctl.cmd_set("picker", "rotation", "90") == 2
    assert ctl.cmd_set("picker", "label", "anything") == 0          # no choices: as before
    table = _config(picker)["plugins"]["picker"]
    assert table["variant"] == "v3" and table["rotation"] == 180


def test_schema_and_info_list_the_choices(picker, capsys):
    assert ctl.cmd_schema("picker") == 0
    rows = {line.split("\t")[0]: line.split("\t") for line in
            capsys.readouterr().out.splitlines()}
    assert rows["variant"] == ["variant", "str", "0", "0", "0", "choices=v4,v3,v2", "v4"]
    assert rows["rotation"][5:] == ["choices=0,180", "0"]
    assert rows["label"][5:] == ["choices=", ""]
    assert ctl.cmd_info("picker") == 0
    assert "one of: v4/v3/v2" in capsys.readouterr().out

