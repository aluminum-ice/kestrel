#!/usr/bin/env python3
"""Apply an in-place *app-layer* update, as root.

This is the engine behind ``kestrel update apply``. The CLI clones the
target release, verifies its commit, then ``exec``s THIS script **from the
clone** (never the installed copy) and hands over. Running from the clone is
what makes it safe to overwrite every installed file - including this script,
the CLI and plugins-ctl - because nothing under the install tree is being read
any more.

What it does, in order (see docs/developer/updates-setup.md):

1. validate the cloned source byte-compiles / parses before touching anything,
2. quiesce the device: stop the learning agent (training), the capture service
   and the plugin daemon, remembering which were running,
3. if the backup plugin is set up, mirror the latest capture data and model to
   the remote first - and abort the update (restarting what was paused) if that
   backup fails, so the update only proceeds with a current snapshot safe,
4. back up the current application files,
5. lay the new files down and reinstall the ``kestrel_pi`` package (offline,
   from the local tree), check it imports, else restore the backup and abort,
6. stamp the new version,
7. restart whatever was running before (the daemon, then capture and the agent).

Only the download (done by the CLI before this engine starts) and the optional
backup touch the network, both before any installed file is changed and both
retried; the install itself is entirely local, so a flaky connection can only
cancel the update cleanly, never half-apply it.

It deliberately does NOT touch the operator's ``config.toml`` or
``/etc/default/kestrel`` (new config keys fall back to code defaults), nor
the base OS. Services are paused with a plain ``systemctl stop`` (not
``capture``/``agent disable``), so no operator setting changes and the device
comes back exactly as it was.

The install map mirrors builder/config.yml's install tasks and is kept in sync
with it; ``test_update_apply`` asserts every mapped source exists in the tree.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time

# Install destinations (absolute). A --prefix relocates them under a test root
# so the file operations can be exercised without a real system.
LIB_DIR = "/usr/local/lib/kestrel"
BIN = "/usr/local/bin/kestrel"
UNIT_DIR = "/etc/systemd/system"
JOURNALD_CONF = "/etc/systemd/journald.conf.d/kestrel.conf"
# Workstation-side operator scripts, shipped read-only (see
# builder/extra/files/scripts/README.md). Carried here so a later script change
# lands as an app-layer update; the first release that adds the folder touches
# config.yml and so is a reflash.
SHARE_SCRIPTS_DIR = "/usr/local/share/kestrel/scripts"
VERSION_FILE = "/etc/kestrel/version"
STATE_FILE = "/var/lib/kestrel/update/state.json"
BACKUP_ROOT = "/var/lib/kestrel/update"
VENV_PIP = "/home/pi/.venv/bin/pip"
VENV_PY = "/home/pi/.venv/bin/python"
DAEMON = "kestrel-daemon.service"
# The two other services that write device state; paused before the update and
# restarted afterwards (if they were running).
CAPTURE_SERVICE = "kestrel-capture.service"
AGENT_SERVICE = "kestrel-agent.service"
# Read to decide whether a pre-update backup can run (never imported).
CONFIG_FILE = "/etc/kestrel/config.toml"
BACKUP_KEY = "/home/pi/.ssh/id_ed25519"

# (source under builder/extra/files, destination basename under LIB_DIR, mode).
# Mirrors "install kestrel library scripts" + capture scripts + the
# plugins-ctl / agent-provision tasks in builder/config.yml. The .sh/.py suffix
# is dropped on install.
LIB_SCRIPTS = [
    ("common.sh", "common", 0o644),
    ("bt-agent.py", "bt-agent", 0o755),
    ("bt-onboard.sh", "bt-onboard", 0o755),
    ("bt-pan.sh", "bt-pan", 0o755),
    ("wifi-provision.sh", "wifi-provision", 0o755),
    ("config-get.py", "config-get", 0o755),
    ("config-set.py", "config-set", 0o755),
    ("capture-launch.sh", "capture-launch", 0o755),
    ("capture-collect.py", "capture-collect", 0o755),
    ("capture-whitelist-provision.sh", "capture-whitelist-provision", 0o755),
    ("plugins-ctl.py", "plugins-ctl", 0o755),
    ("agent-provision.sh", "agent-provision", 0o755),
    ("update-apply.py", "update-apply", 0o755),
]


def run(cmd, **kwargs):
    """Thin subprocess wrapper so tests can monkeypatch every side effect."""
    kwargs.setdefault("check", True)
    kwargs.setdefault("stdin", subprocess.DEVNULL)
    return subprocess.run(cmd, **kwargs)


def _dest(path, prefix):
    """An absolute install path, optionally relocated under a test prefix."""
    return prefix + path if prefix else path


def file_plan(src, prefix=""):
    """The (source, destination, mode) triples to install from clone ``src``
    (the repo root). Covers the lib scripts, the CLI, every reference plugin
    and every systemd unit, plus the journald drop-in. Operator-owned files
    (config.toml, /etc/default/kestrel) are deliberately excluded."""
    files = os.path.join(src, "builder", "extra", "files")
    plan = []
    for name, dest, mode in LIB_SCRIPTS:
        plan.append((os.path.join(files, name),
                     _dest(os.path.join(LIB_DIR, dest), prefix), mode))
    plan.append((os.path.join(files, "kestrel.sh"), _dest(BIN, prefix), 0o755))
    plugins_src = os.path.join(files, "plugins")
    for name in sorted(os.listdir(plugins_src)):
        if name.endswith(".py"):
            plan.append((os.path.join(plugins_src, name),
                         _dest(os.path.join(LIB_DIR, "plugins", name), prefix), 0o644))
    units_src = os.path.join(files, "systemd")
    for name in sorted(os.listdir(units_src)):
        if name.endswith((".service", ".socket", ".timer")):
            plan.append((os.path.join(units_src, name),
                         _dest(os.path.join(UNIT_DIR, name), prefix), 0o644))
    plan.append((os.path.join(files, "journald-kestrel.conf"),
                 _dest(JOURNALD_CONF, prefix), 0o644))
    scripts_src = os.path.join(files, "scripts")
    if os.path.isdir(scripts_src):
        for name in sorted(os.listdir(scripts_src)):
            path = os.path.join(scripts_src, name)
            if not os.path.isfile(path):
                continue
            mode = 0o755 if name.endswith(".sh") else 0o644
            plan.append((path,
                         _dest(os.path.join(SHARE_SCRIPTS_DIR, name), prefix), mode))
    return plan


def validate_source(src):
    """Byte-compile the package and plugins and syntax-check the shell scripts
    in the clone BEFORE anything is overwritten, so a corrupt download cannot
    half-install. Returns (ok, error)."""
    files = os.path.join(src, "builder", "extra", "files")
    pkg = os.path.join(files, "kestrel_pi")
    py_files = []
    for root, _dirs, names in os.walk(pkg):
        if "tests" in root.split(os.sep):
            continue
        py_files += [os.path.join(root, n) for n in names if n.endswith(".py")]
    plugins = os.path.join(files, "plugins")
    py_files += [os.path.join(plugins, n) for n in os.listdir(plugins)
                 if n.endswith(".py")]
    for name, _dest, _mode in LIB_SCRIPTS:
        if name.endswith(".py"):
            py_files.append(os.path.join(files, name))
    try:
        run([sys.executable, "-m", "py_compile"] + py_files, capture_output=True)
    except subprocess.CalledProcessError as exc:
        return False, "python byte-compile failed: %s" % (
            (exc.stderr or b"").decode("utf-8", "replace").strip()[:300])
    sh_files = [os.path.join(files, name) for name, _d, _m in LIB_SCRIPTS
                if name.endswith(".sh")]
    scripts = os.path.join(files, "scripts")
    if os.path.isdir(scripts):
        sh_files += [os.path.join(scripts, n) for n in os.listdir(scripts)
                     if n.endswith(".sh")]
    for path in sh_files:
        try:
            run(["bash", "-n", path], capture_output=True)
        except subprocess.CalledProcessError as exc:
            return False, "shell syntax check failed for %s: %s" % (
                os.path.basename(path),
                (exc.stderr or b"").decode("utf-8", "replace").strip()[:200])
    return True, ""


def backup_current(plan, backup_dir):
    """Copy each destination that currently exists into ``backup_dir`` (flat,
    indexed), returning a restore list of (backup_path, dest)."""
    os.makedirs(backup_dir, exist_ok=True)
    restore = []
    for index, (_src, dest, _mode) in enumerate(plan):
        if os.path.isfile(dest):
            saved = os.path.join(backup_dir, "%04d" % index)
            shutil.copy2(dest, saved)
            restore.append((saved, dest))
    manifest = os.path.join(backup_dir, "manifest.json")
    with open(manifest, "w", encoding="utf-8") as handle:
        json.dump(restore, handle)
    return restore


def apply_plan(plan):
    """Copy every source into place with its mode, creating parents."""
    for src, dest, mode in plan:
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        shutil.copyfile(src, dest)
        os.chmod(dest, mode)


def restore_backup(restore):
    for saved, dest in restore:
        try:
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            shutil.copy2(saved, dest)
        except OSError:
            pass


def install_package(src, runner=run):
    """Reinstall the kestrel_pi package into the pi venv. pip builds a
    local-directory install in-tree, so stage a pi-owned copy and install as
    pi (as the playbook does), then verify the new package imports."""
    files = os.path.join(src, "builder", "extra", "files", "kestrel_pi")
    staging = "/home/pi/.pwn-update-pkg"
    shutil.rmtree(staging, ignore_errors=True)
    shutil.copytree(files, staging)
    try:
        runner(["chown", "-R", "pi:pi", staging])
        # --no-index keeps the reinstall entirely local: an app-layer release
        # never adds a pip dependency (that is a reflash), so nothing need be
        # fetched, and a flaky connection cannot stall or half-do the install.
        runner(["runuser", "-u", "pi", "--", VENV_PIP, "install",
                "--no-cache-dir", "--no-build-isolation", "--no-index", staging])
        runner(["runuser", "-u", "pi", "--", VENV_PY, "-c", "import kestrel_pi"])
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def _service_active(service, runner=run):
    """True if the systemd unit is active now. Tolerant of a runner that does not
    return a CompletedProcess (the tests' recorder), treating that as inactive."""
    try:
        res = runner(["systemctl", "is-active", "--quiet", service], check=False)
    except subprocess.SubprocessError:
        return False
    return getattr(res, "returncode", 1) == 0


def quiesce_services(runner=run):
    """Before any file is replaced, stop the three services that write device
    state, so nothing changes under the update or races the pre-update backup.
    Returns which were running (to restart afterwards). Order: the learning
    agent first (its SIGTERM handler stops cleanly and its model saves are
    atomic), then capture, then the daemon."""
    prior = {
        "capture": _service_active(CAPTURE_SERVICE, runner=runner),
        "agent": _service_active(AGENT_SERVICE, runner=runner),
        "daemon": _service_active(DAEMON, runner=runner),
    }
    for key, service in (("agent", AGENT_SERVICE),
                         ("capture", CAPTURE_SERVICE),
                         ("daemon", DAEMON)):
        if prior[key]:
            runner(["systemctl", "stop", service], check=False)
    return prior


def restore_services(prior, runner=run):
    """Restart whatever was running before the update: ``daemon-reload`` so new
    units take effect, then the daemon (to load the new code), then capture and
    the agent - each only if it was active before, so a service the operator had
    stopped stays stopped. May raise if systemd is unreachable."""
    runner(["systemctl", "daemon-reload"], check=False)
    if prior.get("daemon"):
        runner(["systemctl", "restart", DAEMON], check=False)
    if prior.get("capture"):
        runner(["systemctl", "start", CAPTURE_SERVICE], check=False)
    if prior.get("agent"):
        runner(["systemctl", "start", AGENT_SERVICE], check=False)


def _restore_quietly(prior, runner=run):
    """restore_services on an abort path, where an error is already recorded and
    a failed restart must not mask it."""
    try:
        restore_services(prior, runner=runner)
    except subprocess.SubprocessError:
        pass


def backup_configured(config_file=CONFIG_FILE, key_file=BACKUP_KEY):
    """True when the backup plugin is enabled, has host/user/path set, and the
    device SSH key exists - i.e. a pre-update backup can actually run. Read
    straight from config.toml with the stdlib TOML parser; this never imports the
    plugin (the engine runs as root)."""
    try:
        import tomllib
        with open(config_file, "rb") as handle:
            data = tomllib.load(handle)
    except (OSError, ValueError, ImportError):
        return False
    backup = (data.get("plugins") or {}).get("backup") or {}
    if not backup.get("enabled"):
        return False
    if not (backup.get("host") and backup.get("user") and backup.get("path")):
        return False
    return os.path.isfile(backup.get("ssh_key") or key_file)


def run_backup(runner=run, cli=BIN):
    """Run one pre-update backup through the installed CLI (still the old one at
    this point - files are replaced only afterwards). The CLI's rsync already
    retries a dropped transfer. Returns (ok, detail)."""
    try:
        runner([cli, "backup", "now"], check=True)
        return True, ""
    except subprocess.SubprocessError as exc:
        return False, "backup before updating failed (%s)" % exc


def write_state(state_file, **fields):
    try:
        with open(state_file, "r", encoding="utf-8") as handle:
            state = json.load(handle)
    except (OSError, ValueError):
        state = {}
    state.update(fields)
    try:
        os.makedirs(os.path.dirname(state_file), exist_ok=True)
        tmp = state_file + ".tmp"
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(state, handle)
        os.replace(tmp, state_file)
    except OSError:
        pass
    return state


def apply_update(src, tag, state_file=STATE_FILE, version_file=VERSION_FILE,
                 prefix="", do_package=True, do_services=True, do_backup=True,
                 config_file=CONFIG_FILE, runner=run):
    """Orchestrate one in-place update from clone ``src`` for release ``tag``.
    Returns (ok, error). Pauses the device, takes a pre-update backup if one is
    set up, lays the new files down, then restarts whatever was running. On any
    failure the file layer is restored from the backup and the paused services
    are brought back, so a failed or interrupted update leaves the device on the
    previous version, running."""
    ok, err = validate_source(src)
    if not ok:
        write_state(state_file, status="error", detail=err, finished_at=time.time())
        return False, err

    write_state(state_file, status="running", detail="applying %s" % tag,
                started_at=time.time())

    prior = {"capture": False, "agent": False, "daemon": True}
    if do_services:
        write_state(state_file, status="running",
                    detail="pausing capture, training and the daemon")
        prior = quiesce_services(runner=runner)

    # A current snapshot before anything is overwritten, when the backup plugin
    # is set up. If it cannot finish, do not update: restart what was paused and
    # stop, so the device stays as it was with a safe copy not taken.
    if do_backup and backup_configured(config_file=config_file):
        write_state(state_file, status="running", detail="backing up before updating")
        bok, berr = run_backup(runner=runner)
        if not bok:
            write_state(state_file, status="error", detail=berr, finished_at=time.time())
            if do_services:
                _restore_quietly(prior, runner=runner)
            return False, berr

    plan = file_plan(src, prefix=prefix)
    backup_dir = _dest(os.path.join(BACKUP_ROOT,
                                    "backup-" + time.strftime("%Y%m%d-%H%M%S")),
                       prefix)
    restore = backup_current(plan, backup_dir)
    try:
        apply_plan(plan)
        if do_package:
            install_package(src, runner=runner)
    except (OSError, shutil.Error, subprocess.SubprocessError) as exc:
        restore_backup(restore)
        detail = "update failed (%s); restored previous files" % exc
        write_state(state_file, status="error", detail=detail,
                    finished_at=time.time())
        if do_services:
            _restore_quietly(prior, runner=runner)
        return False, detail

    # Point of no return passed: new files are down and the package imports.
    # Stamp the version so the restart loads it.
    stamp = _dest(version_file, prefix)
    try:
        os.makedirs(os.path.dirname(stamp), exist_ok=True)
        with open(stamp, "w", encoding="utf-8") as handle:
            handle.write(tag + "\n")
    except OSError:
        pass

    if do_services:
        try:
            restore_services(prior, runner=runner)
        except subprocess.SubprocessError as exc:
            write_state(state_file, status="error",
                        detail="installed %s but restart failed (%s)" % (tag, exc),
                        finished_at=time.time())
            return False, str(exc)
    write_state(state_file, status="ok", detail="updated to %s" % tag,
                installed=tag, update_available=False, offerable=False,
                backup_dir=backup_dir, finished_at=time.time())
    return True, ""


def main(argv):
    parser = argparse.ArgumentParser(description="apply an in-place app-layer update")
    parser.add_argument("--src", required=True, help="path to the cloned release")
    parser.add_argument("--tag", required=True, help="the release tag being applied")
    parser.add_argument("--repo", default="", help="owner/repo (recorded only)")
    parser.add_argument("--state", default=STATE_FILE)
    parser.add_argument("--prefix", default="", help="relocate dests (testing)")
    parser.add_argument("--cleanup", default="", help="remove this dir when done")
    parser.add_argument("--no-package", action="store_true")
    parser.add_argument("--no-services", action="store_true")
    parser.add_argument("--no-backup", action="store_true",
                        help="skip the pre-update backup even if one is set up")
    args = parser.parse_args(argv[1:])
    ok, err = apply_update(
        args.src, args.tag, state_file=args.state, prefix=args.prefix,
        do_package=not args.no_package, do_services=not args.no_services,
        do_backup=not args.no_backup)
    if not ok:
        sys.stderr.write("update apply failed: %s\n" % err)
    # Clean up the clone we were exec'd from (the CLI can't, having exec'd into
    # us). Safe even though we're running from it: the interpreter already read
    # this file into memory.
    if args.cleanup:
        shutil.rmtree(args.cleanup, ignore_errors=True)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
