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
import urllib.parse

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


def test_the_image_build_installs_the_same_operator_scripts(tmp_path):
    # A flashed card and an in-place-updated one must carry the same scripts:
    # run the playbook task's shell loop against the repo's scripts folder and
    # compare what it installs (names and modes) with the updater's plan.
    import re
    import stat
    import subprocess
    playbook = open(os.path.join(REPO, "builder", "config.yml"), encoding="utf-8").read()
    task = playbook.split("- name: install kestrel operator scripts", 1)[1]
    task = task.split("\n  - name:", 1)[0]
    loop = task.split("shell: |", 1)[1]
    loop = "\n".join(line[6:] for line in loop.splitlines() if line.strip())
    scripts_src = os.path.join(REPO, "builder", "extra", "files", "scripts")
    loop = re.sub(r"\{\{.*?\}\}", "'%s'" % scripts_src, loop)
    dest = tmp_path / "scripts"
    dest.mkdir()
    loop = loop.replace("/usr/local/share/kestrel/scripts/", str(dest) + "/")
    subprocess.run(["sh", "-c", loop], check=True)
    built = {f.name: stat.S_IMODE(f.stat().st_mode) for f in dest.iterdir()}
    planned = {os.path.basename(d): m for _s, d, m in ua.file_plan(REPO)
               if d.startswith(ua.SHARE_SCRIPTS_DIR + "/")}
    assert built == planned
    assert "pull-health.sh" in built


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
    # Every file was written to a temp name and renamed into place.
    leftovers = [n for _r, _d, names in os.walk(prefix) for n in names
                 if n.endswith(".kestrel-new")]
    assert leftovers == []
    # The marker is gone and the install list recorded for the next update.
    run_dir = prefix + ua.RUN_DIR
    assert not os.path.exists(os.path.join(run_dir, ua.MARKER))
    assert cli in json.load(open(os.path.join(run_dir, ua.INSTALLED_LIST)))


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

    pkg = []
    monkeypatch.setattr(ua, "install_package", boom)
    monkeypatch.setattr(ua, "snapshot_package",
                        lambda snap, runner=None: pkg.append(("snapshot", snap)))
    monkeypatch.setattr(ua, "restore_package",
                        lambda snap, runner=None: pkg.append(("restore", snap)))
    ok, err = ua.apply_update(REPO, "0.5.0", state_file=state,
                              version_file=version, prefix=prefix,
                              do_package=True, do_services=False, do_backup=False)
    assert not ok
    assert "restored" in err
    with open(cli, encoding="utf-8") as handle:
        assert handle.read() == "OLD CLI\n"  # original came back
    # Files the update added are removed again, not left half-new.
    assert not os.path.exists(prefix + os.path.join(ua.LIB_DIR, "plugins", "updates.py"))
    # The previous package is put back too (pip may have installed the new one).
    assert [k for k, _ in pkg] == ["snapshot", "restore"]
    assert pkg[0][1] == pkg[1][1]
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
    monkeypatch.setattr(ua, "STAGING_PARENT", str(tmp_path))
    calls, runner = _recorder()
    ua.install_package(REPO, runner=runner)
    # Staged in a fresh root-created dir, removed afterwards.
    assert os.listdir(str(tmp_path)) == []
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
                do_package=False, do_services=True, do_backup=False,
                sleep=lambda _s: None)       # the post-restart health settle
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


def test_backup_timeout_reads_the_updates_option(tmp_path):
    # Default when the option is absent (e.g. only [plugins.backup] is set).
    assert ua.backup_timeout(config_file=_backup_config(tmp_path)) == \
        ua.BACKUP_TIMEOUT_DEFAULT
    cfg = tmp_path / "updates.toml"
    cfg.write_text("[plugins.updates]\nbackup_timeout_seconds = 42\n")
    assert ua.backup_timeout(config_file=str(cfg)) == 42
    cfg.write_text("[plugins.updates]\nbackup_timeout_seconds = 0\n")
    assert ua.backup_timeout(config_file=str(cfg)) is None     # 0 = no bound
    cfg.write_text('[plugins.updates]\nbackup_timeout_seconds = "nope"\n')
    assert ua.backup_timeout(config_file=str(cfg)) == ua.BACKUP_TIMEOUT_DEFAULT
    # A set value is held to 30-3600 s (0 still means "no bound").
    for raw, want in ((5, 30), (99999, 3600), (-1, ua.BACKUP_TIMEOUT_DEFAULT)):
        cfg.write_text("[plugins.updates]\nbackup_timeout_seconds = %d\n" % raw)
        assert ua.backup_timeout(config_file=str(cfg)) == want, raw
    assert ua.backup_timeout(config_file=str(tmp_path / "missing")) == \
        ua.BACKUP_TIMEOUT_DEFAULT


def test_run_backup_times_out_fast(tmp_path):
    def runner(cmd, **kw):
        raise ua.subprocess.TimeoutExpired(cmd, kw.get("timeout"))
    ok, detail = ua.run_backup(runner=runner, timeout=5)
    assert not ok
    assert "timed out after 5s" in detail and "nothing changed" in detail


def test_run_backup_passes_the_timeout_to_the_runner(tmp_path):
    seen = {}
    def runner(cmd, **kw):
        seen["timeout"] = kw.get("timeout")
        return _Result(0)
    ok, _ = ua.run_backup(runner=runner, timeout=90)
    assert ok and seen["timeout"] == 90


def test_apply_update_aborts_and_restores_when_the_backup_times_out(tmp_path):
    cfg = _backup_config(tmp_path)
    calls = []
    def runner(cmd, **kw):
        calls.append(list(cmd))
        if cmd[:3] == [ua.BIN, "backup", "now"]:
            raise ua.subprocess.TimeoutExpired(cmd, kw.get("timeout"))
        if cmd[:2] == ["systemctl", "is-active"]:
            return _Result(0 if cmd[-1] in (ua.CAPTURE_SERVICE, ua.AGENT_SERVICE,
                                            ua.DAEMON) else 1)
        return _Result(0)
    kwargs = _apply_kwargs(tmp_path, do_backup=True, config_file=cfg)
    ok, err = ua.apply_update(REPO, "0.5.0", runner=runner, **kwargs)
    assert not ok and "timed out" in err
    # Nothing was installed, and the paused services are brought back.
    assert not os.path.exists(kwargs["prefix"] + ua.BIN)
    assert ["systemctl", "start", ua.CAPTURE_SERVICE] in calls
    assert json.load(open(kwargs["state_file"], encoding="utf-8"))["status"] == "error"


def test_backup_configured_requires_enabled_remote_and_key(tmp_path):
    assert ua.backup_configured(config_file=_backup_config(tmp_path)) is True
    assert ua.backup_configured(
        config_file=_backup_config(tmp_path, enabled=False)) is False
    assert ua.backup_configured(
        config_file=_backup_config(tmp_path, host="")) is False
    assert ua.backup_configured(
        config_file=_backup_config(tmp_path, with_key=False)) is False
    assert ua.backup_configured(config_file=str(tmp_path / "nope.toml")) is False


# ---- removals, health check, recovery and the lock ------------------------

def _seed(prefix, rel, text):
    path = prefix + rel
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(text)
    return path


def _plain(tmp_path, **extra):
    base = dict(state_file=str(tmp_path / "state.json"),
                version_file="/etc/kestrel/version",
                prefix=str(tmp_path / "root"),
                do_package=False, do_services=False, do_backup=False)
    base.update(extra)
    return base


def test_a_plugin_dropped_upstream_is_removed(tmp_path):
    kwargs = _plain(tmp_path)
    stale = _seed(kwargs["prefix"], os.path.join(ua.LIB_DIR, "plugins", "retired.py"),
                  "OLD\n")
    plan = ua.file_plan(REPO, prefix=kwargs["prefix"])
    assert stale in ua.removal_plan(plan, prefix=kwargs["prefix"])
    ok, err = ua.apply_update(REPO, "0.5.0", **kwargs)
    assert ok, err
    assert not os.path.exists(stale)


def test_a_dropped_file_comes_back_on_rollback(tmp_path, monkeypatch):
    kwargs = _plain(tmp_path, do_package=True)
    stale = _seed(kwargs["prefix"], os.path.join(ua.LIB_DIR, "plugins", "retired.py"),
                  "OLD\n")
    monkeypatch.setattr(ua, "snapshot_package", lambda snap, runner=None: None)
    monkeypatch.setattr(ua, "restore_package", lambda snap, runner=None: None)

    def boom(_src, runner=None):
        raise OSError("pip exploded")

    monkeypatch.setattr(ua, "install_package", boom)
    ok, _err = ua.apply_update(REPO, "0.5.0", **kwargs)
    assert not ok
    assert open(stale, encoding="utf-8").read() == "OLD\n"


def test_recorded_files_are_pruned_but_only_inside_the_install_roots(tmp_path):
    prefix = str(tmp_path / "root")
    run_dir = prefix + ua.RUN_DIR
    os.makedirs(run_dir)
    old_script = _seed(prefix, os.path.join(ua.LIB_DIR, "retired-script"), "x")
    outside = _seed(prefix, "/etc/passwd", "root:x:0:0")
    with open(os.path.join(run_dir, ua.INSTALLED_LIST), "w", encoding="utf-8") as handle:
        json.dump([old_script, outside], handle)
    removals = ua.removal_plan(ua.file_plan(REPO, prefix=prefix), prefix=prefix)
    assert old_script in removals
    assert outside not in removals


class _Shown(_Result):
    def __init__(self, stdout):
        super().__init__(0)
        self.stdout = stdout


def test_a_daemon_that_does_not_stay_up_rolls_the_update_back(tmp_path):
    kwargs = _plain(tmp_path, do_services=True)
    prefix = kwargs["prefix"]
    cli = _seed(prefix, ua.BIN, "OLD CLI\n")
    stamp = _seed(prefix, "/etc/kestrel/version", "0.4.0\n")
    calls = []

    def runner(cmd, **kw):
        calls.append(list(cmd))
        if cmd[:2] == ["systemctl", "is-active"]:
            if cmd[-1] != ua.DAEMON:
                return _Result(1)
            # Running before the update; after the restart it keeps failing.
            restarted = ["systemctl", "restart", ua.DAEMON] in calls
            return _Result(3 if restarted else 0)
        return _Result(0)

    ok, err = ua.apply_update(REPO, "0.5.0", runner=runner,
                              sleep=lambda _s: None, **kwargs)
    assert not ok and "rolled back" in err
    assert open(cli, encoding="utf-8").read() == "OLD CLI\n"
    assert open(stamp, encoding="utf-8").read() == "0.4.0\n"
    assert json.load(open(kwargs["state_file"]))["status"] == "error"
    assert not os.path.exists(os.path.join(prefix + ua.RUN_DIR, ua.MARKER))
    # The previous version is started again after the rollback.
    assert calls.count(["systemctl", "restart", ua.DAEMON]) == 2


def test_a_daemon_restarting_during_the_settle_is_unhealthy():
    count = {"n": 0}

    def runner(cmd, **kw):
        if cmd[:2] == ["systemctl", "is-active"]:
            return _Result(0)
        if cmd[:2] == ["systemctl", "show"]:
            count["n"] += 1
            return _Shown(str(count["n"]))
        return _Result(0)

    assert ua.daemon_healthy(runner=runner, sleep=lambda _s: None) is False


def test_a_steady_daemon_is_healthy():
    def runner(cmd, **kw):
        if cmd[:2] == ["systemctl", "show"]:
            return _Shown("0")
        return _Result(0)

    assert ua.daemon_healthy(runner=runner, sleep=lambda _s: None) is True


def test_the_recovery_unit_is_enabled(tmp_path):
    calls, runner = _service_recorder(active=("daemon",))
    ok, err = ua.apply_update(REPO, "0.5.0", runner=runner, **_apply_kwargs(tmp_path))
    assert ok, err
    assert ["systemctl", "enable", "--quiet", ua.RECOVER_SERVICE] in calls


def test_recover_rolls_back_an_interrupted_update(tmp_path, monkeypatch):
    kwargs = _plain(tmp_path)
    prefix = kwargs["prefix"]
    cli = _seed(prefix, ua.BIN, "OLD CLI\n")
    stamp = _seed(prefix, "/etc/kestrel/version", "0.4.0\n")
    real_apply = ua.apply_plan

    def cut(plan, removals=()):
        real_apply(plan, removals)
        raise KeyboardInterrupt      # the power goes: nothing after this runs

    monkeypatch.setattr(ua, "apply_plan", cut)
    with pytest.raises(KeyboardInterrupt):
        ua.apply_update(REPO, "0.5.0", **kwargs)
    marker = os.path.join(prefix + ua.RUN_DIR, ua.MARKER)
    assert os.path.exists(marker)
    assert open(cli, encoding="utf-8").read() != "OLD CLI\n"   # half-updated

    calls, runner = _recorder()
    ok, detail = ua.recover(prefix=prefix, runner=runner, do_package=False)
    assert ok and "rolled back" in detail
    assert open(cli, encoding="utf-8").read() == "OLD CLI\n"
    assert open(stamp, encoding="utf-8").read() == "0.4.0\n"
    assert not os.path.exists(prefix + os.path.join(ua.LIB_DIR, "plugins", "updates.py"))
    assert not os.path.exists(marker)
    assert ["systemctl", "daemon-reload"] in calls
    assert ua.recover(prefix=prefix, runner=runner) == (True, "nothing to recover")


def test_recover_only_restores_from_the_run_dir(tmp_path):
    prefix = str(tmp_path / "root")
    run_dir = prefix + ua.RUN_DIR
    os.makedirs(run_dir)
    with open(os.path.join(run_dir, ua.MARKER), "w", encoding="utf-8") as handle:
        json.dump({"backup_dir": str(tmp_path / "elsewhere")}, handle)
    _calls, runner = _recorder()
    ok, _detail = ua.recover(prefix=prefix, runner=runner, do_package=False)
    assert not ok


def test_an_unrecovered_interrupted_update_blocks_a_new_one(tmp_path):
    kwargs = _plain(tmp_path)
    run_dir = kwargs["prefix"] + ua.RUN_DIR
    os.makedirs(run_dir)
    open(os.path.join(run_dir, ua.MARKER), "w").close()
    ok, err = ua.apply_update(REPO, "0.5.0", **kwargs)
    assert not ok and "interrupted" in err
    assert not os.path.exists(kwargs["prefix"] + ua.BIN)


def test_only_one_update_runs_at_a_time(tmp_path):
    kwargs = _plain(tmp_path)
    run_dir = kwargs["prefix"] + ua.RUN_DIR
    ua.ensure_run_dir(run_dir)
    held = ua.acquire_lock(run_dir)
    try:
        ok, err = ua.apply_update(REPO, "0.5.0", **kwargs)
        assert not ok and "already running" in err
    finally:
        held.close()
    assert ua.apply_update(REPO, "0.5.0", **kwargs)[0] is True


def test_old_backups_are_pruned(tmp_path):
    run_dir = str(tmp_path)
    for stamp in ("20260101-000000", "20260102-000000", "20260103-000000"):
        os.makedirs(os.path.join(run_dir, "backup-" + stamp))
    ua.prune_backups(run_dir, keep=2)
    assert sorted(os.listdir(run_dir)) == ["backup-20260102-000000",
                                           "backup-20260103-000000"]


def test_main_never_writes_the_legacy_pi_owned_state_file(monkeypatch):
    # Older CLIs pass the pi-owned state file; the engine writes its own.
    seen = {}

    def fake_apply(src, tag, **kwargs):
        seen.update(kwargs)
        return True, ""

    monkeypatch.setattr(ua, "apply_update", fake_apply)
    assert ua.main(["x", "--src", "/s", "--tag", "0.5.0",
                    "--state", ua.LEGACY_STATE_FILE]) == 0
    assert seen["state_file"] is None


# ---- the package snapshot (run as pi) --------------------------------------

def test_package_snapshot_and_restore_run_as_pi():
    calls, runner = _recorder()
    ua.snapshot_package("/s", runner=runner)
    ua.restore_package("/s", runner=runner)
    ua.discard_snapshot("/s", runner=runner)
    assert calls and all(c[:4] == ["runuser", "-u", "pi", "--"] for c in calls)


def test_package_snapshot_script_copies_a_distribution(tmp_path):
    import subprocess
    import sys

    snap = str(tmp_path / "snaps" / "pkg-1")
    subprocess.run([sys.executable, "-I", "-c", ua.SNAPSHOT_PY, snap, "pytest"],
                   check=True)
    index = json.load(open(os.path.join(snap, "index.json"), encoding="utf-8"))
    root = os.path.realpath(sys.prefix) + os.sep
    assert index
    assert all(os.path.isfile(saved) and path.startswith(root) for saved, path in index)


def test_package_restore_script_compiles():
    compile(ua.RESTORE_PY, "restore", "exec")


# ---- --verify: what may be installed, from root-owned inputs only ----------

SHA = "a" * 40
FORGE = "https://forge.test"


def _updates_config(tmp_path, beta=False, token="", host=FORGE, repo="o/r"):
    cfg = tmp_path / "config.toml"
    cfg.write_text('[plugins.updates]\nrepo = "%s"\nhost = "%s"\ntoken = "%s"\n'
                   "beta = %s\n" % (repo, host, token, "true" if beta else "false"),
                   encoding="utf-8")
    return str(cfg)


def _rel(tag, manifest_host=FORGE, **extra):
    rel = {"tag_name": tag, "prerelease": "-beta." in tag,
           "assets": [{"name": "update-manifest.json",
                       "browser_download_url": "%s/m/%s.json" % (manifest_host, tag)}]}
    rel.update(extra)
    return rel


def _forge(releases, manifests, calls=None, list_status=200):
    by_tag = {r["tag_name"]: r for r in releases}

    def fetch(url, token, timeout, limit):
        if calls is not None:
            calls.append((url, token))
        if "/releases/tags/" in url:
            tag = urllib.parse.unquote(url.rsplit("/", 1)[1])
            return (200, by_tag[tag]) if tag in by_tag else (404, None)
        if "/releases?" in url:
            return (list_status, releases if list_status < 400 else None)
        if "/m/" in url:
            tag = url.rsplit("/", 1)[1][:-len(".json")]
            return (200, manifests[tag]) if tag in manifests else (404, None)
        return 404, None
    return fetch


def _app(commit=SHA):
    return {"kind": "app-layer", "commit": commit}


def _verify(tmp_path, tag, releases, manifests, installed="0.6.5", calls=None,
            cfg=None, list_status=200, **kwargs):
    version = tmp_path / "version"
    version.write_text(installed + "\n", encoding="utf-8")
    return ua.verify_release(
        tag, config_file=_updates_config(tmp_path, **(cfg or {})),
        version_file=str(version),
        fetch=_forge(releases, manifests, calls, list_status), **kwargs)


def test_verify_accepts_a_newer_app_layer_release(tmp_path):
    out = _verify(tmp_path, "0.6.6", [_rel("0.6.6"), _rel("0.6.5")],
                  {"0.6.6": _app()})
    assert out["ok"] is True, out
    # The host and repo come from config.toml, never from the pi-owned state.
    assert (out["tag"], out["commit"], out["host"], out["repo"]) == (
        "0.6.6", SHA, FORGE, "o/r")


def test_verify_honours_a_stable_release_floored_at_a_beta(tmp_path):
    # 1.0.11-beta.1 needed a reflash, beta.2 didn't, and 1.0.11 is app-layer
    # from beta.1 on (min_update_from): fine from either beta (the betas in
    # between don't count for a stable target), refused from 1.0.10.
    releases = [_rel("1.0.11"), _rel("1.0.11-beta.2"), _rel("1.0.11-beta.1"),
                _rel("1.0.10")]
    manifests = {"1.0.11": dict(_app(), min_update_from="1.0.11-beta.1"),
                 "1.0.11-beta.2": _app(),
                 "1.0.11-beta.1": {"kind": "reflash", "commit": SHA}}
    for installed in ("1.0.11-beta.1", "1.0.11-beta.2"):
        out = _verify(tmp_path, "1.0.11", releases, manifests, installed=installed)
        assert out["ok"] is True, (installed, out)
    out = _verify(tmp_path, "1.0.11", releases, manifests, installed="1.0.10")
    assert out["ok"] is False and "1.0.11-beta.1" in out["error"]


def test_verify_sends_the_token_only_to_the_configured_host(tmp_path):
    calls = []
    out = _verify(tmp_path, "0.6.6",
                  [_rel("0.6.6", manifest_host="https://cdn.test"), _rel("0.6.5")],
                  {"0.6.6": _app()}, calls=calls, cfg={"token": "TKN"})
    assert out["ok"] is True, out
    on_host = [t for u, t in calls if u.startswith(FORGE + "/")]
    off_host = [t for u, t in calls if not u.startswith(FORGE + "/")]
    assert on_host and all(t == "TKN" for t in on_host)
    assert off_host and all(t == "" for t in off_host)


def test_verify_refuses_a_non_https_host(tmp_path):
    out = _verify(tmp_path, "0.6.6", [_rel("0.6.6")], {"0.6.6": _app()},
                  cfg={"host": "http://forge.test"})
    assert out["ok"] is False and "https" in out["error"]


def test_verify_refuses_a_downgrade_unless_forced(tmp_path):
    rels = [_rel("0.6.5"), _rel("0.6.4")]
    out = _verify(tmp_path, "0.6.4", rels, {"0.6.4": _app()})
    assert out["ok"] is False and "not newer" in out["error"]
    forced = _verify(tmp_path, "0.6.4", rels, {"0.6.4": _app()}, force=True)
    assert forced["ok"] is True and forced["warnings"]


def test_verify_keeps_betas_off_the_stable_channel(tmp_path):
    rels = [_rel("0.7.0-beta.1"), _rel("0.6.5")]
    manifests = {"0.7.0-beta.1": _app()}
    out = _verify(tmp_path, "0.7.0-beta.1", rels, manifests)
    assert out["ok"] is False and "beta" in out["error"]
    # An operator-named tag (--ref), or the beta option on, may be a beta.
    assert _verify(tmp_path, "0.7.0-beta.1", rels, manifests, explicit=True)["ok"]
    assert _verify(tmp_path, "0.7.0-beta.1", rels, manifests, cfg={"beta": True})["ok"]


def test_verify_refuses_a_reflash_release(tmp_path):
    out = _verify(tmp_path, "0.6.6", [_rel("0.6.6"), _rel("0.6.5")],
                  {"0.6.6": {"kind": "reflash", "commit": SHA,
                             "requires_reflash_reason": "apt change"}})
    assert out["ok"] is False and "apt change" in out["error"]


def test_verify_refuses_a_skipped_reflash_in_between(tmp_path):
    rels = [_rel("0.6.7"), _rel("0.6.6"), _rel("0.6.5")]
    out = _verify(tmp_path, "0.6.7", rels,
                  {"0.6.7": _app(), "0.6.6": {"kind": "reflash"}})
    assert out["ok"] is False and "0.6.6" in out["error"]


def test_verify_refuses_when_the_release_list_cannot_be_read(tmp_path):
    out = _verify(tmp_path, "0.6.7", [_rel("0.6.7"), _rel("0.6.5")],
                  {"0.6.7": _app()}, list_status=500)
    assert out["ok"] is False and "release list" in out["error"]


def test_verify_refuses_a_changed_confirmation(tmp_path):
    out = _verify(tmp_path, "0.6.6", [_rel("0.6.6")], {"0.6.6": _app()},
                  expect="0.6.7", force=True)
    assert out["ok"] is False and "changed since" in out["error"]


def test_verify_never_installs_a_draft_or_a_bad_commit(tmp_path):
    draft = _verify(tmp_path, "0.6.6", [_rel("0.6.6", draft=True)],
                    {"0.6.6": _app()}, force=True)
    assert draft["ok"] is False and "draft" in draft["error"]
    bad = _verify(tmp_path, "0.6.6", [_rel("0.6.6"), _rel("0.6.5")],
                  {"0.6.6": _app(commit="not-a-sha")})
    assert bad["ok"] is False and "commit" in bad["error"]


def test_verify_an_unpublished_ref_needs_force(tmp_path):
    out = _verify(tmp_path, "main", [_rel("0.6.5")], {}, explicit=True)
    assert out["ok"] is False and "not a published release" in out["error"]
    forced = _verify(tmp_path, "main", [_rel("0.6.5")], {}, explicit=True, force=True)
    assert forced["ok"] is True and forced["commit"] == ""


def test_verify_refuses_a_dev_build_unless_forced(tmp_path):
    rels = [_rel("0.6.6")]
    out = _verify(tmp_path, "0.6.6", rels, {"0.6.6": _app()}, installed="master")
    assert out["ok"] is False and "dev build" in out["error"]
    assert _verify(tmp_path, "0.6.6", rels, {"0.6.6": _app()}, installed="master",
                   force=True)["ok"] is True


def test_version_key_matches_the_plugin():
    plugin = _load("pwn_updates_plugin_parity",
                   os.path.join(FILES, "plugins", "updates.py"))
    for text in ("0.6.9", "0.7.0-beta.1", "0.7.0-beta.10", "0.7.0", "v1.2.3",
                 "1.2.3+b5", "0.7.0-rc.1", "0.7.0-beta", "master", "", None, "0.7",
                 "0.7a.0"):
        assert ua.version_key(text) == plugin.version_key(text), text


def test_read_updates_config_holds_the_request_timeout_in_range(tmp_path):
    cfg = tmp_path / "config.toml"
    for raw, want in (("0", 5), ("1", 5), ("45", 45), ("100000", 120),
                      ('"x"', 30), ("true", 30)):
        cfg.write_text("[plugins.updates]\ntimeout = %s\n" % raw)
        assert ua.read_updates_config(str(cfg))["timeout"] == want, raw
    cfg.write_text("")
    assert ua.read_updates_config(str(cfg))["timeout"] == 30
