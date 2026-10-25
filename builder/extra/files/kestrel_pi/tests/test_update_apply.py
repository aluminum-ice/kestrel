"""The in-place app-layer update engine (builder/extra/files/update-apply.py).

Runs as root on the device from a fresh clone. These exercise the pure file
operations against a --prefix test root (no real system touched): the install
plan mirrors the playbook, the source is validated before anything is written,
a mid-apply failure rolls the file layer back, and a clean run stamps the
version and records an "ok" state. The package reinstall and the service
restart are thin subprocess wrappers, checked with a recording runner."""

import importlib.machinery
import importlib.util
import json
import os
import shutil
import stat

import pytest

FILES = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
REPO = os.path.abspath(os.path.join(FILES, "..", "..", ".."))


def _load(name, path):
    loader = importlib.machinery.SourceFileLoader(name, path)
    spec = importlib.util.spec_from_loader(name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


ua = _load("pwn_update_apply_test", os.path.join(FILES, "update-apply.py"))


def _fake_clone(tmp_path):
    """A clone root whose builder/extra/files is a copy of the real tree, so it
    can be corrupted without touching the repo."""
    clone = tmp_path / "clone"
    dest = clone / "builder" / "extra" / "files"
    shutil.copytree(FILES, str(dest),
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    return str(clone)


def _recorder():
    calls = []

    def runner(cmd, **kwargs):
        calls.append(list(cmd))
        return None

    return calls, runner


class _Result:
    def __init__(self, returncode=0):
        self.returncode = returncode


def _service_recorder(active=("capture", "agent", "daemon"), fail_cmds=()):
    """A recording runner that answers ``systemctl is-active`` from ``active``
    (the service keys treated as running) so quiesce/restore can be exercised,
    and raises for any command whose joined form contains one of ``fail_cmds``
    (e.g. ``"backup now"``)."""
    calls = []
    key_of = {ua.CAPTURE_SERVICE: "capture", ua.AGENT_SERVICE: "agent",
              ua.DAEMON: "daemon"}

    def runner(cmd, **kwargs):
        calls.append(list(cmd))
        joined = " ".join(cmd)
        for bad in fail_cmds:
            if bad in joined:
                raise ua.subprocess.CalledProcessError(1, cmd)
        if cmd[:2] == ["systemctl", "is-active"]:
            return _Result(0 if key_of.get(cmd[-1]) in active else 1)
        return _Result(0)

    return calls, runner


def _backup_config(tmp_path, enabled=True, host="h", user="u", path="/d",
                   with_key=True):
    """Write a config.toml for backup_configured / the pre-update backup path,
    with the device key present unless ``with_key`` is False."""
    key = tmp_path / "id_ed25519"
    if with_key:
        key.write_text("KEY")
    elif key.exists():
        key.unlink()
    lines = ["[plugins.backup]", "enabled = %s" % ("true" if enabled else "false")]
    if host:
        lines.append('host = "%s"' % host)
    if user:
        lines.append('user = "%s"' % user)
    if path:
        lines.append('path = "%s"' % path)
    lines.append('ssh_key = "%s"' % key)
    cfg = tmp_path / "config.toml"
    cfg.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return str(cfg)


# ---- the install plan ----------------------------------------------------

def test_file_plan_sources_all_exist():
    plan = ua.file_plan(REPO)
    assert plan, "plan is empty"
    for src, _dest, _mode in plan:
        assert os.path.isfile(src), "missing plan source: %s" % src


def test_file_plan_covers_cli_plugins_units_journald():
    dests = [d for _s, d, _m in ua.file_plan(REPO)]
    assert ua.BIN in dests  # the CLI itself
    assert any(d.endswith("/plugins/updates.py") for d in dests)
    assert any(d.endswith("kestrel-daemon.service") for d in dests)
    assert any(d.endswith(".socket") for d in dests)
    assert ua.JOURNALD_CONF in dests


def test_file_plan_carries_operator_scripts():
    plan = ua.file_plan(REPO)
    scripts = [(d, m) for _s, d, m in plan if d.startswith(ua.SHARE_SCRIPTS_DIR + "/")]
    assert scripts, "no scripts in the install plan"
    dests = [d for d, _m in scripts]
    assert any(d.endswith("/pull-agent-diagnostics.sh") for d in dests)
    # the .sh helper is executable; the folder README is not.
    modes = dict(scripts)
    assert modes[ua.SHARE_SCRIPTS_DIR + "/pull-agent-diagnostics.sh"] == 0o755
    assert modes.get(ua.SHARE_SCRIPTS_DIR + "/README.md") == 0o644


def test_file_plan_excludes_operator_config():
    dests = [d for _s, d, _m in ua.file_plan(REPO)]
    assert not any(d.endswith("/config.toml") for d in dests)
    assert not any(d.endswith("/kestrel") and "default" in d for d in dests)


def test_prefix_relocates_every_destination(tmp_path):
    prefix = str(tmp_path)
    for _src, dest, _mode in ua.file_plan(REPO, prefix=prefix):
        assert dest.startswith(prefix)


# ---- source validation ----------------------------------------------------

def test_validate_source_accepts_the_real_tree():
    ok, err = ua.validate_source(REPO)
    assert ok, err


def test_validate_source_rejects_broken_python(tmp_path):
    clone = _fake_clone(tmp_path)
    bad = os.path.join(clone, "builder", "extra", "files", "plugins", "memtemp.py")
    with open(bad, "a", encoding="utf-8") as handle:
        handle.write("\ndef broken(:\n")
    ok, err = ua.validate_source(clone)
    assert not ok
    assert "byte-compile" in err


def test_validate_source_rejects_broken_shell(tmp_path):
    clone = _fake_clone(tmp_path)
    bad = os.path.join(clone, "builder", "extra", "files", "bt-pan.sh")
    with open(bad, "a", encoding="utf-8") as handle:
        handle.write("\nif then fi done )\n")
    ok, err = ua.validate_source(clone)
    assert not ok
    assert "syntax check failed" in err


# ---- the orchestrated apply ------------------------------------------------

def test_apply_update_lays_files_down_and_records_ok(tmp_path, monkeypatch):
    prefix = str(tmp_path / "root")
    state = str(tmp_path / "state.json")
    version = "/etc/kestrel/version"
    calls, runner = _recorder()
    ok, err = ua.apply_update(REPO, "0.5.0", state_file=state,
                              version_file=version, prefix=prefix,
                              do_package=False, do_services=False,
                              do_backup=False, runner=runner)
    assert ok, err
    cli = prefix + ua.BIN
    assert os.path.isfile(cli)
    assert stat.S_IMODE(os.stat(cli).st_mode) == 0o755
    plugin = prefix + os.path.join(ua.LIB_DIR, "plugins", "updates.py")
    assert stat.S_IMODE(os.stat(plugin).st_mode) == 0o644
    with open(prefix + version, encoding="utf-8") as handle:
        assert handle.read() == "0.5.0\n"
    saved = json.load(open(state, encoding="utf-8"))
    assert saved["status"] == "ok"
    assert saved["installed"] == "0.5.0"
    assert saved["offerable"] is False
    assert saved["update_available"] is False


def test_apply_update_validate_failure_writes_nothing(tmp_path):
    clone = _fake_clone(tmp_path)
    bad = os.path.join(clone, "builder", "extra", "files", "plugins", "gps.py")
    with open(bad, "a", encoding="utf-8") as handle:
        handle.write("\n(((\n")
    prefix = str(tmp_path / "root")
    state = str(tmp_path / "state.json")
    ok, err = ua.apply_update(clone, "0.5.0", state_file=state,
                              version_file="/etc/kestrel/version",
                              prefix=prefix, do_package=False, do_services=False)
    assert not ok and err
    assert not os.path.exists(prefix + ua.BIN)
    assert json.load(open(state, encoding="utf-8"))["status"] == "error"


def test_apply_update_rolls_back_on_package_failure(tmp_path, monkeypatch):
    prefix = str(tmp_path / "root")
    state = str(tmp_path / "state.json")
    version = str(tmp_path / "version")
    # Pre-seed the installed CLI with old content so the backup has something to
    # restore when the (stubbed) package install blows up after files are laid.
    cli = prefix + ua.BIN
    os.makedirs(os.path.dirname(cli))
    with open(cli, "w", encoding="utf-8") as handle:
        handle.write("OLD CLI\n")

    def boom(_src, runner=None):
        raise OSError("pip exploded")

    monkeypatch.setattr(ua, "install_package", boom)
    ok, err = ua.apply_update(REPO, "0.5.0", state_file=state,
                              version_file=version, prefix=prefix,
                              do_package=True, do_services=False, do_backup=False)
    assert not ok
    assert "restored" in err
    with open(cli, encoding="utf-8") as handle:
        assert handle.read() == "OLD CLI\n"  # original came back
    assert not os.path.exists(version)  # never got to the stamp
    assert json.load(open(state, encoding="utf-8"))["status"] == "error"


def test_apply_update_restart_failure_is_reported_after_version_stamp(tmp_path):
    prefix = str(tmp_path / "root")
    state = str(tmp_path / "state.json")
    version = "/etc/kestrel/version"

    def runner(cmd, **kwargs):
        if cmd[:2] == ["systemctl", "daemon-reload"] or "restart" in cmd:
            raise ua.subprocess.SubprocessError("no systemd here")
        return None

    ok, err = ua.apply_update(REPO, "0.5.0", state_file=state,
                              version_file=version, prefix=prefix,
                              do_package=False, do_services=True,
                              do_backup=False, runner=runner)
    assert not ok
    # Version is stamped before the restart, so it persists even when restart dies.
    assert open(prefix + version, encoding="utf-8").read() == "0.5.0\n"
    assert json.load(open(state, encoding="utf-8"))["status"] == "error"


# ---- the package reinstall wrapper ----------------------------------------

def test_install_package_installs_and_imports_as_pi(tmp_path, monkeypatch):
    monkeypatch.setattr(ua.shutil, "copytree", lambda *a, **k: None)
    monkeypatch.setattr(ua.shutil, "rmtree", lambda *a, **k: None)
    calls, runner = _recorder()
    ua.install_package(REPO, runner=runner)
    joined = [" ".join(c) for c in calls]
    assert any("chown -R pi:pi" in j for j in joined)
    assert any(ua.VENV_PIP in j and "install" in j for j in joined)
    assert any("--no-index" in j for j in joined)  # the reinstall stays offline
    assert any(ua.VENV_PY in j and "import kestrel_pi" in j for j in joined)


# ---- quiescing, restoring, and the pre-update backup ----------------------

def _apply_kwargs(tmp_path, **extra):
    base = dict(state_file=str(tmp_path / "state.json"),
                version_file="/etc/kestrel/version",
                prefix=str(tmp_path / "root"),
                do_package=False, do_services=True, do_backup=False)
    base.update(extra)
    return base


def test_apply_update_quiesces_before_and_restores_after(tmp_path):
    calls, runner = _service_recorder(active=("capture", "agent", "daemon"))
    ok, err = ua.apply_update(REPO, "0.5.0", runner=runner, **_apply_kwargs(tmp_path))
    assert ok, err
    # All three are stopped, agent -> capture -> daemon.
    stop_agent = calls.index(["systemctl", "stop", ua.AGENT_SERVICE])
    stop_capture = calls.index(["systemctl", "stop", ua.CAPTURE_SERVICE])
    stop_daemon = calls.index(["systemctl", "stop", ua.DAEMON])
    assert stop_agent < stop_capture < stop_daemon
    # The restore comes after every stop: daemon-reload, then restart/starts.
    reload_at = calls.index(["systemctl", "daemon-reload"])
    assert reload_at > stop_daemon
    assert ["systemctl", "restart", ua.DAEMON] in calls
    assert ["systemctl", "start", ua.CAPTURE_SERVICE] in calls
    assert ["systemctl", "start", ua.AGENT_SERVICE] in calls


def test_apply_update_restores_only_services_that_were_running(tmp_path):
    calls, runner = _service_recorder(active=("daemon",))  # capture/agent were off
    ok, err = ua.apply_update(REPO, "0.5.0", runner=runner, **_apply_kwargs(tmp_path))
    assert ok, err
    assert ["systemctl", "stop", ua.DAEMON] in calls
    assert ["systemctl", "stop", ua.CAPTURE_SERVICE] not in calls
    assert ["systemctl", "stop", ua.AGENT_SERVICE] not in calls
    assert ["systemctl", "restart", ua.DAEMON] in calls
    assert ["systemctl", "start", ua.CAPTURE_SERVICE] not in calls
    assert ["systemctl", "start", ua.AGENT_SERVICE] not in calls


def test_apply_update_backs_up_before_touching_files_when_configured(tmp_path):
    cfg = _backup_config(tmp_path)
    calls, runner = _service_recorder(active=("daemon",))
    ok, err = ua.apply_update(REPO, "0.5.0", runner=runner,
                              **_apply_kwargs(tmp_path, do_backup=True, config_file=cfg))
    assert ok, err
    # The backup runs after the daemon is paused and before any file is laid
    # down (i.e. before the restore's daemon-reload).
    backup_at = calls.index([ua.BIN, "backup", "now"])
    stop_daemon = calls.index(["systemctl", "stop", ua.DAEMON])
    reload_at = calls.index(["systemctl", "daemon-reload"])
    assert stop_daemon < backup_at < reload_at


def test_apply_update_aborts_and_restores_when_the_backup_fails(tmp_path):
    cfg = _backup_config(tmp_path)
    calls, runner = _service_recorder(active=("capture", "agent", "daemon"),
                                      fail_cmds=("backup now",))
    kwargs = _apply_kwargs(tmp_path, do_backup=True, config_file=cfg)
    ok, err = ua.apply_update(REPO, "0.5.0", runner=runner, **kwargs)
    assert not ok
    assert "backup" in err
    # Nothing was installed - the CLI was never written under the prefix.
    assert not os.path.exists(kwargs["prefix"] + ua.BIN)
    # The paused services are brought back even on this abort path.
    assert ["systemctl", "start", ua.CAPTURE_SERVICE] in calls
    assert ["systemctl", "start", ua.AGENT_SERVICE] in calls
    assert json.load(open(kwargs["state_file"], encoding="utf-8"))["status"] == "error"


def test_apply_update_skips_backup_when_not_set_up(tmp_path):
    cfg = _backup_config(tmp_path, enabled=False)
    calls, runner = _service_recorder(active=("daemon",))
    ok, err = ua.apply_update(REPO, "0.5.0", runner=runner,
                              **_apply_kwargs(tmp_path, do_backup=True, config_file=cfg))
    assert ok, err
    assert not any(c[:1] == [ua.BIN] for c in calls)


def test_backup_configured_requires_enabled_remote_and_key(tmp_path):
    assert ua.backup_configured(config_file=_backup_config(tmp_path)) is True
    assert ua.backup_configured(
        config_file=_backup_config(tmp_path, enabled=False)) is False
    assert ua.backup_configured(
        config_file=_backup_config(tmp_path, host="")) is False
    assert ua.backup_configured(
        config_file=_backup_config(tmp_path, with_key=False)) is False
    assert ua.backup_configured(config_file=str(tmp_path / "nope.toml")) is False
