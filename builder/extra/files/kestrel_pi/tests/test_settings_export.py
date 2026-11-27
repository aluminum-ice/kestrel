"""``kestrel settings export``: the settings changed from the shipped defaults.

``plugins-ctl export-settings`` compares the live config.toml with the shipped
one (installed as ``config.default.toml``) and writes only what differs, as a
settings file a new image can import. Secrets are left out unless asked for and
only their names are ever printed. The CLI writes the file as the person who
ran sudo, mode 0600, never over an existing file.
"""
import importlib.machinery
import importlib.util
import os
import shutil
import stat
import subprocess
import tomllib

import pytest

FILES = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
REPO = os.path.abspath(os.path.join(FILES, "..", "..", ".."))
PLUGINS_DIR = os.path.join(FILES, "plugins")
SHIPPED = os.path.join(FILES, "config.toml")


def _load(name, path):
    loader = importlib.machinery.SourceFileLoader(name, path)
    spec = importlib.util.spec_from_loader(name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


ctl = _load("pwn_plugins_ctl_export_test", os.path.join(FILES, "plugins-ctl.py"))
ua = _load("pwn_update_apply_export_test", os.path.join(FILES, "update-apply.py"))
cs = ctl.config_set_module()


@pytest.fixture
def env(tmp_path, monkeypatch):
    live = tmp_path / "config.toml"
    shutil.copy(SHIPPED, live)
    version = tmp_path / "version"
    version.write_text("1.0.14\n")
    monkeypatch.setattr(ctl, "CONFIG", str(live))
    monkeypatch.setattr(ctl, "CONFIG_DEFAULT", SHIPPED)
    monkeypatch.setattr(ctl, "VERSION_FILE", str(version))
    monkeypatch.setattr(ctl, "PLUGIN_PATHS", [PLUGINS_DIR])
    return tmp_path


def _live(env):
    return str(env / "config.toml")


def _parsed(text):
    return tomllib.loads(text)


def _body(text):
    data = _parsed(text)
    data.pop(ctl.SETTINGS_HEADER)
    return data


def test_an_unchanged_config_exports_only_the_header(env):
    text, summary = ctl.export_settings()
    data = _parsed(text)
    assert summary["count"] == 0
    assert data == {ctl.SETTINGS_HEADER: {
        "format": 1, "from_version": "1.0.14",
        "exported": data[ctl.SETTINGS_HEADER]["exported"],
        "secrets": False, "secrets_left_out": []}}


def test_only_the_changed_keys_are_written(env):
    live = _live(env)
    cs.set_value(live, "capture.scan", "mode", '"autohunt"')
    cs.set_value(live, "capture.scan", "dwell_seconds", "5")  # the default
    cs.set_string_array(live, "capture", "whitelist", ["My Network", "00:00:5E:00:53:01"])
    cs.set_value(live, "plugins.memtemp", "enabled", "true")
    # A plugin with no shipped table: everything in its table is a change.
    cs.set_value(live, "plugins.map", "tile_url", '"https://tiles.example.com/{z}/{x}/{y}.png"')
    text, summary = ctl.export_settings()
    assert _body(text) == {
        "capture": {"whitelist": ["My Network", "00:00:5E:00:53:01"],
                    "scan": {"mode": "autohunt"}},
        "plugins": {"memtemp": {"enabled": True},
                    "map": {"tile_url": "https://tiles.example.com/{z}/{x}/{y}.png"}},
    }
    assert summary["count"] == 4


def test_a_value_of_another_type_counts_as_changed():
    assert ctl._same(5, 5) and not ctl._same(True, 1) and not ctl._same(1.0, 1)
    assert ctl.changed_settings({"a": {"b": 1, "c": True}}, {"a": {"b": 1, "c": 1}}) \
        == {"a": {"c": True}}


def test_a_key_missing_from_the_live_config_is_not_written():
    assert ctl.changed_settings({"a": {}}, {"a": {"b": 1}}) == {}


def test_secrets_are_left_out_by_default_and_only_named(env, capsys):
    live = _live(env)
    cs.set_value(live, "plugins.wpa-sec", "enabled", "true")
    cs.set_value(live, "plugins.wpa-sec", "api_key", '"wpa-sec-placeholder-key"')
    cs.set_many(live, "web", [("auth", '"basic"'), ("username", '"pi"'),
                              ("password", '"hunter2"')])
    assert ctl.cmd_export_settings([]) == 0
    out, err = capsys.readouterr()
    for secret in ("wpa-sec-placeholder-key", "hunter2"):
        assert secret not in out and secret not in err
    data = _parsed(out)
    assert data[ctl.SETTINGS_HEADER]["secrets"] is False
    assert data[ctl.SETTINGS_HEADER]["secrets_left_out"] == [
        "web.password", "plugins.wpa-sec.api_key"]
    assert data["plugins"]["wpa-sec"] == {"enabled": True}
    assert data["web"] == {"auth": "basic", "username": "pi"}
    assert "wpa-sec api_key" in err and "web password" in err


def test_with_secrets_they_are_written(env, capsys):
    live = _live(env)
    cs.set_value(live, "plugins.wpa-sec", "api_key", '"wpa-sec-placeholder-key"')
    assert ctl.cmd_export_settings(["--with-secrets"]) == 0
    out, err = capsys.readouterr()
    data = _parsed(out)
    assert data[ctl.SETTINGS_HEADER]["secrets"] is True
    assert data[ctl.SETTINGS_HEADER]["secrets_left_out"] == []
    assert data["plugins"]["wpa-sec"]["api_key"] == "wpa-sec-placeholder-key"
    assert "plain text" in out
    # The summary still names them, never their values.
    assert "wpa-sec api_key" in err and "wpa-sec-placeholder-key" not in err


def test_every_declared_plugin_secret_is_treated_as_one(env):
    live = _live(env)
    secrets = []
    for name, (decl, _src) in ctl.static_plugins().items():
        for opt, spec in decl["options"].items():
            if (spec or {}).get("secret") and (spec or {}).get("type", "str") == "str":
                cs.set_value(live, "plugins." + name, opt, '"placeholder-secret"')
                secrets.append("plugins.%s.%s" % (name, opt))
    assert len(secrets) >= 8
    text, summary = ctl.export_settings()
    assert "placeholder-secret" not in text
    assert sorted(summary["secrets_left_out"]) == sorted(secrets)


def test_a_table_for_a_plugin_that_isnt_installed_is_left_out(env, capsys):
    cs.set_value(_live(env), "plugins.ups-hat", "enabled", "true")
    assert ctl.cmd_export_settings([]) == 0
    out, err = capsys.readouterr()
    assert "ups-hat" not in _parsed(out).get("plugins", {})
    assert "[plugins.ups-hat]" in err


def test_awkward_values_round_trip(env):
    with open(_live(env), "a", encoding="utf-8") as handle:
        handle.write('\n[plugins.memtemp.extra]\n'
                     'quote = "say \\"hi\\" \\\\ there\\ttab"\n'
                     'unicode = "Café ☃"\n'
                     'deleted = "a\\u007fb"\n'
                     'ratio = 0.25\n'
                     'big = 1e300\n'
                     'nested = [[1, 2], ["a"]]\n'
                     'inline = {a = 1, "b c" = "d"}\n'
                     'when = 2026-01-02T03:04:05Z\n')
    with open(_live(env), "rb") as handle:
        want = tomllib.load(handle)["plugins"]["memtemp"]["extra"]
    text, _summary = ctl.export_settings()
    assert _body(text)["plugins"]["memtemp"]["extra"] == want


def test_missing_defaults_fail_without_output(env, capsys, monkeypatch):
    monkeypatch.setattr(ctl, "CONFIG_DEFAULT", str(env / "absent.toml"))
    assert ctl.cmd_export_settings([]) == 1
    out, err = capsys.readouterr()
    assert out == "" and "shipped defaults" in err


def test_an_unreadable_config_fails_without_output(env, capsys):
    (env / "config.toml").write_text("this is [not toml\n")
    assert ctl.cmd_export_settings([]) == 1
    out, err = capsys.readouterr()
    assert out == "" and "the config" in err


def test_a_version_that_isnt_a_release_is_noted(env, capsys):
    (env / "version").write_text("master\n")
    assert ctl.cmd_export_settings([]) == 0
    out, err = capsys.readouterr()
    assert _parsed(out)[ctl.SETTINGS_HEADER]["from_version"] == "master"
    assert "isn't a release" in err


def test_an_unknown_option_is_refused(env, capsys):
    assert ctl.cmd_export_settings(["--secrets"]) == 2
    assert capsys.readouterr().out == ""


def test_the_build_and_the_updater_install_the_shipped_defaults():
    # The export compares against this file, so a flashed card and an
    # updated one must both have it, where plugins-ctl looks for it.
    assert ("config.toml", "config.default.toml", 0o644) in ua.LIB_SCRIPTS
    playbook = open(os.path.join(REPO, "builder", "config.yml"), encoding="utf-8").read()
    assert "- { src: config.toml, dest: config.default.toml, mode: '0644' }" in playbook
    if "PWN_CONFIG_DEFAULT" not in os.environ:
        assert ctl.CONFIG_DEFAULT == os.path.join(ua.LIB_DIR, "config.default.toml")


# ---- the CLI: kestrel settings export ------------------------------------
# Run kestrel.sh under bash with `id -u` answering 0 (need_root) and `runuser`
# running the command as the test user, both as exported functions so they win
# over the PATH that common.sh resets for root.
CLI_SHIMS = (
    'id() { if [ "$#" -eq 1 ] && [ "$1" = -u ]; then echo 0; else command id "$@"; fi; }\n'
    'runuser() { shift 3; "$@"; }\n'
    'export -f id runuser\n'
)


def _cli(env, *args, stdin=None):
    lib = env / "lib"
    lib.mkdir(exist_ok=True)
    if not (lib / "common").exists():
        os.symlink(os.path.join(FILES, "common.sh"), lib / "common")
    run_env = dict(os.environ,
                   PWN_LIB_DIR=str(lib),
                   PWN_PLUGINS_CTL=os.path.join(FILES, "plugins-ctl.py"),
                   PWN_CONFIG=_live(env),
                   PWN_CONFIG_DEFAULT=SHIPPED,
                   PWN_VERSION_FILE=str(env / "version"),
                   PWN_PLUGINS_DIR=PLUGINS_DIR,
                   PWN_PLUGINS_USER_DIR=str(env / "no-user-plugins"),
                   SUDO_USER=subprocess.run(["id", "-un"], capture_output=True,
                                            text=True).stdout.strip())
    script = CLI_SHIMS + 'exec bash "$0" "$@"\n'
    return subprocess.run(["bash", "-c", script, os.path.join(FILES, "kestrel.sh"),
                           "settings", "export", *args],
                          cwd=env, env=run_env, input=stdin or "",
                          capture_output=True, text=True)


def test_cli_writes_a_private_file(env):
    cs.set_value(_live(env), "capture.scan", "mode", '"autohunt"')
    res = _cli(env, "-o", "settings.toml")
    assert res.returncode == 0, res.stderr
    path = env / "settings.toml"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert _body(path.read_text()) == {"capture": {"scan": {"mode": "autohunt"}}}
    assert "doesn't carry" in res.stderr


def test_cli_never_writes_over_a_file(env):
    (env / "settings.toml").write_text("keep me\n")
    res = _cli(env, "-o", "settings.toml")
    assert res.returncode != 0 and "already exists" in res.stderr
    assert (env / "settings.toml").read_text() == "keep me\n"


def test_cli_asks_before_writing_secrets(env):
    cs.set_value(_live(env), "plugins.wpa-sec", "api_key", '"wpa-sec-placeholder-key"')
    res = _cli(env, "--with-secrets", "-o", "settings.toml")
    assert res.returncode != 0 and "--yes" in res.stderr
    assert not (env / "settings.toml").exists()
    res = _cli(env, "--with-secrets", "--yes", "-o", "settings.toml")
    assert res.returncode == 0, res.stderr
    data = _parsed((env / "settings.toml").read_text())
    assert data["plugins"]["wpa-sec"]["api_key"] == "wpa-sec-placeholder-key"
    assert "wpa-sec-placeholder-key" not in res.stderr + res.stdout


def test_cli_leaves_no_file_when_the_export_fails(env):
    (env / "config.toml").write_text("this is [not toml\n")
    res = _cli(env, "-o", "settings.toml")
    assert res.returncode != 0 and "nothing was written" in res.stderr
    assert not (env / "settings.toml").exists()


def test_cli_writes_to_stdout_without_a_file(env):
    res = _cli(env)
    assert res.returncode == 0, res.stderr
    assert _parsed(res.stdout)[ctl.SETTINGS_HEADER]["format"] == 1
