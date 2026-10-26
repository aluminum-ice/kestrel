#!/usr/bin/env python3
"""Apply an in-place *app-layer* update, as root.

This is the engine behind ``kestrel update apply``. It has three modes:

``--verify``
    Run by the CLI from the **installed** copy, as root, before anything is
    fetched. It decides what may be installed from root-owned inputs only - the
    release server, repo and token in ``config.toml`` and the stamped version -
    by asking the server itself for the release, its update manifest and the
    releases in between. The ``updates`` plugin's shared state is written by the
    pi daemon, so it is only ever a hint here, never trusted. Prints a JSON
    verdict (the tag and the commit to check out).

apply (the default)
    The CLI clones the verified release, checks its commit, then ``exec``s THIS
    script **from the clone** (never the installed copy) and hands over. Running
    from the clone is what makes it safe to overwrite every installed file -
    including this script, the CLI and plugins-ctl - because nothing under the
    install tree is being read any more.

``--recover``
    Run at boot by ``kestrel-update-recover.service`` when an update was
    interrupted (power loss, crash): it restores the pre-update files and
    package from the backup the interrupted run recorded.

What apply does, in order (see docs/developer/updates-setup.md):

1. take the update lock and validate the cloned source byte-compiles / parses
   before touching anything,
2. quiesce the device: stop the learning agent (training), the capture service
   and the plugin daemon, remembering which were running,
3. if the backup plugin is set up, mirror the latest capture data and model to
   the remote first - and abort the update (restarting what was paused) if that
   backup fails, so the update only proceeds with a current snapshot safe,
4. back up the current application files and snapshot the installed package,
   then record an in-progress marker naming that backup,
5. lay the new files down (each written to a temp name and renamed into place)
   and reinstall the ``kestrel_pi`` package (offline, from the local tree),
   check it imports, else restore the backup and abort,
6. stamp the new version,
7. restart whatever was running before (the daemon, then capture and the
   agent), and if the daemon does not stay up, roll everything back.

Only the download (done by the CLI before this engine starts) and the optional
backup touch the network, both before any installed file is changed and both
retried; the install itself is entirely local, so a flaky connection can only
cancel the update cleanly, never half-apply it.

Everything this engine writes as root lives in root-owned paths
(``RUN_DIR``); it never writes into the pi-owned update state directory,
where a symlink planted by the daemon could redirect a root write.

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
import fcntl
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request

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
# Root-owned home of everything the engine writes: the run status (status.json,
# world-readable so the daemon and CLI can show it), the update lock, the
# in-progress marker, the list of installed files and the pre-update backups.
# Its parent /var/lib/kestrel is root-owned, so pi cannot plant anything here.
RUN_DIR = "/var/lib/kestrel/update-run"
RUN_STATE = "status.json"
MARKER = "in-progress.json"
INSTALLED_LIST = "installed.json"
LOCK = "lock"
# The pi-owned state file older CLIs passed as --state. Root must never write
# there, so it is ignored in favour of RUN_DIR/status.json.
LEGACY_STATE_FILE = "/var/lib/kestrel/update/state.json"
# How many pre-update backups to keep in RUN_DIR.
KEEP_BACKUPS = 2
# Where the package snapshot is kept. It is a copy of pi's own venv files, so
# pi writes and reads it (as pi); root never touches it directly.
PKG_SNAPSHOT_ROOT = "/home/pi/.cache/kestrel-update"
# Parent of the root-created, private staging dir the package is built from.
STAGING_PARENT = "/var/tmp"
VENV_PIP = "/home/pi/.venv/bin/pip"
VENV_PY = "/home/pi/.venv/bin/python"
PACKAGE_DIST = "kestrel-pi"
DAEMON = "kestrel-daemon.service"
# The two other services that write device state; paused before the update and
# restarted afterwards (if they were running).
CAPTURE_SERVICE = "kestrel-capture.service"
AGENT_SERVICE = "kestrel-agent.service"
# Boot-time recovery of an interrupted update; enabled by the engine too, so a
# device that got it through an app-layer update has it on.
RECOVER_SERVICE = "kestrel-update-recover.service"
# After the restart the daemon must come up within HEALTH_TIMEOUT seconds and
# then stay up (no restarts) for HEALTH_SETTLE seconds, or the update is rolled
# back. Its unit restarts on failure after 10s, so a crash on start shows.
HEALTH_TIMEOUT = 90
HEALTH_SETTLE = 20
# Read to decide whether a pre-update backup can run (never imported), and by
# --verify for the release server, repo, token and channel.
CONFIG_FILE = "/etc/kestrel/config.toml"
BACKUP_KEY = "/home/pi/.ssh/id_ed25519"
# The [plugins.updates] defaults, as in the plugin's __options__.
DEFAULT_REPO = "aluminum-ice/kestrel"
DEFAULT_HOST = "https://git.3ntropy.io"
MANIFEST_ASSET = "update-manifest.json"
RELEASES_PAGE = 50
RELEASES_MAX_PAGES = 2
# Response size caps for --verify (a release list carries the release notes).
MAX_LIST_BYTES = 4 * 1024 * 1024
MAX_MANIFEST_BYTES = 256 * 1024

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


def removal_plan(plan, prefix="", run_dir=None):
    """Installed files the new release no longer ships, to delete (after
    backing them up) so a plugin or script dropped upstream - say, for a
    security reason - stops loading on updated devices. Two sources: every
    ``*.py`` in the shipped plugins dir (it holds only reference plugins;
    operator plugins live in /etc/kestrel/plugins), and the files the last
    successful update recorded installing. Only paths under the lib dir, the
    CLI and the shared scripts dir are ever removed; systemd units are not
    (removing one needs it disabled first)."""
    run_dir = run_dir or _dest(RUN_DIR, prefix)
    keep = {dest for _src, dest, _mode in plan}
    roots = [_dest(LIB_DIR, prefix) + os.sep, _dest(SHARE_SCRIPTS_DIR, prefix) + os.sep]
    cli = _dest(BIN, prefix)
    candidates = set()
    plugins_dir = _dest(os.path.join(LIB_DIR, "plugins"), prefix)
    if os.path.isdir(plugins_dir):
        for name in os.listdir(plugins_dir):
            if name.endswith(".py"):
                candidates.add(os.path.join(plugins_dir, name))
    try:
        with open(os.path.join(run_dir, INSTALLED_LIST), encoding="utf-8") as handle:
            recorded = json.load(handle)
    except (OSError, ValueError):
        recorded = []
    if isinstance(recorded, list):
        candidates.update(p for p in recorded if isinstance(p, str))
    out = []
    for path in sorted(candidates):
        if path in keep or os.path.islink(path) or not os.path.isfile(path):
            continue
        if path == cli or any(path.startswith(root) for root in roots):
            out.append(path)
    return out


def record_installed(plan, run_dir, prefix=""):
    """Remember what this update installed (bar the units and the journald
    drop-in, which removal_plan never touches), for the next update."""
    skip = {_dest(UNIT_DIR, prefix), os.path.dirname(_dest(JOURNALD_CONF, prefix))}
    files = [dest for _src, dest, _mode in plan if os.path.dirname(dest) not in skip]
    _write_json_atomic(os.path.join(run_dir, INSTALLED_LIST), files)


def backup_current(plan, backup_dir, removals=()):
    """Copy each destination that currently exists - and each file about to be
    removed - into ``backup_dir`` (flat, indexed, root-only), and note which
    destinations are new, so a rollback can delete them. Returns the restore
    record ``{"saved": [[backup_path, dest], ...], "added": [dest, ...]}``,
    also written to ``backup_dir/manifest.json`` for --recover."""
    os.makedirs(backup_dir, mode=0o700, exist_ok=True)
    saved, added = [], []
    dests = [dest for _src, dest, _mode in plan] + list(removals)
    for index, dest in enumerate(dests):
        if os.path.isfile(dest):
            copy = os.path.join(backup_dir, "%04d" % index)
            shutil.copy2(dest, copy)
            saved.append([copy, dest])
        elif not os.path.lexists(dest):
            added.append(dest)
    restore = {"saved": saved, "added": added}
    _write_json_atomic(os.path.join(backup_dir, "manifest.json"), restore)
    return restore


def _install_file(src, dest, mode):
    """Copy ``src`` to ``dest`` crash-safely: write a temp file beside it, sync
    it, then rename it into place, so a power cut leaves either the old or the
    new file, never a truncated one."""
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    tmp = dest + ".kestrel-new"
    if os.path.lexists(tmp):
        os.remove(tmp)
    shutil.copyfile(src, tmp)
    os.chmod(tmp, mode)
    with open(tmp, "rb") as handle:
        os.fsync(handle.fileno())
    os.replace(tmp, dest)


def _fsync_dirs(paths):
    for directory in sorted({os.path.dirname(p) for p in paths}):
        try:
            fd = os.open(directory, os.O_RDONLY)
        except OSError:
            continue
        try:
            os.fsync(fd)
        except OSError:
            pass
        finally:
            os.close(fd)


def apply_plan(plan, removals=()):
    """Install every source with its mode (each atomically), then delete the
    files the release dropped."""
    for src, dest, mode in plan:
        _install_file(src, dest, mode)
    for path in removals:
        os.remove(path)
    _fsync_dirs([dest for _src, dest, _mode in plan] + list(removals))


def restore_backup(restore):
    """Put the backed-up files back and delete the ones the update added.
    Best-effort per file, so one failure does not stop the rest."""
    if isinstance(restore, list):          # a pre-"added" manifest
        restore = {"saved": restore, "added": []}
    for saved, dest in restore.get("saved", []):
        try:
            _install_file(saved, dest, os.stat(saved).st_mode & 0o7777)
        except OSError:
            pass
    for dest in restore.get("added", []):
        try:
            os.remove(dest)
        except OSError:
            pass
    _fsync_dirs([d for _s, d in restore.get("saved", [])] + restore.get("added", []))


def prune_backups(run_dir, keep=KEEP_BACKUPS):
    """Drop all but the newest ``keep`` pre-update backups."""
    try:
        names = sorted(n for n in os.listdir(run_dir) if n.startswith("backup-"))
    except OSError:
        return
    for name in names[:-keep] if keep else names:
        shutil.rmtree(os.path.join(run_dir, name), ignore_errors=True)


# Run as pi with the venv Python: copy every file of the installed package
# (from its RECORD, so the dist-info and console scripts too) into a snapshot
# dir, with an index of where each came from. Older snapshots are removed.
SNAPSHOT_PY = r"""
import importlib.metadata as md, json, os, shutil, sys
out, dist_name = sys.argv[1], sys.argv[2]
parent = os.path.dirname(out)
os.makedirs(parent, exist_ok=True)
for name in os.listdir(parent):
    if name.startswith("pkg-"):
        shutil.rmtree(os.path.join(parent, name), ignore_errors=True)
os.makedirs(out)
root = os.path.realpath(sys.prefix) + os.sep
index = []
try:
    dist = md.distribution(dist_name)
    files = list(dist.files or [])
except md.PackageNotFoundError:
    dist, files = None, []
for number, entry in enumerate(files):
    path = os.path.realpath(str(dist.locate_file(entry)))
    if not path.startswith(root) or not os.path.isfile(path):
        continue
    saved = os.path.join(out, "%05d" % number)
    shutil.copy2(path, saved)
    index.append([saved, path])
with open(os.path.join(out, "index.json"), "w") as handle:
    json.dump(index, handle)
"""

# Run as pi with the venv Python: uninstall whatever package is there now,
# then put the snapshot's files back (only ever inside the venv).
RESTORE_PY = r"""
import json, os, shutil, subprocess, sys
snap, dist_name = sys.argv[1], sys.argv[2]
with open(os.path.join(snap, "index.json")) as handle:
    index = json.load(handle)
subprocess.run([os.path.join(sys.prefix, "bin", "pip"), "uninstall", "-y", "-q",
                dist_name], check=False)
root = os.path.realpath(sys.prefix) + os.sep
for saved, path in index:
    if not os.path.realpath(path).startswith(root):
        continue
    os.makedirs(os.path.dirname(path), exist_ok=True)
    shutil.copy2(saved, path)
"""


def snapshot_package(snapshot, runner=run):
    """Snapshot the installed ``kestrel_pi`` package (as pi) so a rollback can
    put the previous version back; restoring only the files would leave the new
    package in the venv."""
    runner(["runuser", "-u", "pi", "--", VENV_PY, "-I", "-c", SNAPSHOT_PY,
            snapshot, PACKAGE_DIST])


def restore_package(snapshot, runner=run):
    """Reinstate the snapshotted package (as pi) and check it imports."""
    runner(["runuser", "-u", "pi", "--", VENV_PY, "-I", "-c", RESTORE_PY,
            snapshot, PACKAGE_DIST])
    runner(["runuser", "-u", "pi", "--", VENV_PY, "-c", "import kestrel_pi"])


def discard_snapshot(snapshot, runner=run):
    runner(["runuser", "-u", "pi", "--", "rm", "-rf", "--", snapshot], check=False)


def install_package(src, runner=run):
    """Reinstall the kestrel_pi package into the pi venv. pip builds a
    local-directory install in-tree, so stage a pi-owned copy and install as
    pi (as the playbook does), then verify the new package imports. The copy is
    made in a fresh root-created directory (random name, not listable by pi)
    and only chowned to pi once complete, so root never writes into a path pi
    controls."""
    files = os.path.join(src, "builder", "extra", "files", "kestrel_pi")
    parent = tempfile.mkdtemp(prefix="kestrel-pkg-", dir=STAGING_PARENT)
    try:
        os.chmod(parent, 0o711)
        staging = os.path.join(parent, "pkg")
        shutil.copytree(files, staging)
        runner(["chown", "-R", "pi:pi", staging])
        # --no-index keeps the reinstall entirely local: an app-layer release
        # never adds a pip dependency (that is a reflash), so nothing need be
        # fetched, and a flaky connection cannot stall or half-do the install.
        runner(["runuser", "-u", "pi", "--", VENV_PIP, "install",
                "--no-cache-dir", "--no-build-isolation", "--no-index", staging])
        runner(["runuser", "-u", "pi", "--", VENV_PY, "-c", "import kestrel_pi"])
    finally:
        shutil.rmtree(parent, ignore_errors=True)


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


# Default seconds to allow the pre-update backup before giving up on it, so a
# backup that can't reach the remote (a flaky field uplink) cannot hold the
# paused daemon -- and the dashboard -- down for many minutes. Overridable via
# [plugins.updates] backup_timeout_seconds; 0 there means wait indefinitely.
BACKUP_TIMEOUT_DEFAULT = 180


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


def backup_timeout(config_file=CONFIG_FILE):
    """Seconds to allow the pre-update backup, from [plugins.updates]
    backup_timeout_seconds. Returns an int, or ``None`` for "no bound" (the
    operator set 0). A missing or bad value falls back to
    ``BACKUP_TIMEOUT_DEFAULT``. Read straight from config.toml with the stdlib
    TOML parser; never imports the plugin (root)."""
    try:
        import tomllib
        with open(config_file, "rb") as handle:
            data = tomllib.load(handle)
    except (OSError, ValueError, ImportError):
        return BACKUP_TIMEOUT_DEFAULT
    raw = ((data.get("plugins") or {}).get("updates") or {}).get(
        "backup_timeout_seconds", BACKUP_TIMEOUT_DEFAULT)
    try:
        val = int(raw)
    except (TypeError, ValueError):
        return BACKUP_TIMEOUT_DEFAULT
    if val == 0:
        return None            # explicit "wait as long as it takes"
    return val if val > 0 else BACKUP_TIMEOUT_DEFAULT


def run_backup(runner=run, cli=BIN, timeout=None):
    """Run one pre-update backup through the installed CLI (still the old one at
    this point - files are replaced only afterwards). The CLI's rsync already
    retries a dropped transfer, so ``timeout`` (seconds, ``None`` = no bound)
    caps the whole attempt: past it the backup is abandoned and the update is
    cancelled, rather than holding the paused daemon down for many minutes on a
    backup that cannot finish (e.g. the remote host is unreachable). Returns
    (ok, detail). The detail names the recovery so the dashboard can show it."""
    advice = ("; check the backup host is reachable, or update without a fresh "
              "backup")
    try:
        runner([cli, "backup", "now"], check=True, timeout=timeout)
        return True, ""
    except subprocess.TimeoutExpired:
        return False, ("backup before updating timed out after %ss; nothing "
                       "changed%s" % (timeout, advice))
    except subprocess.SubprocessError as exc:
        return False, "backup before updating failed (%s); nothing changed%s" % (
            exc, advice)


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


def _write_json_atomic(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(data, handle)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)


def _read_json(path):
    try:
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return None


def ensure_run_dir(run_dir):
    """Create the root-owned run dir if needed; refuse a symlink in its place."""
    if os.path.islink(run_dir):
        raise OSError("%s is a symlink; refusing to use it" % run_dir)
    os.makedirs(run_dir, mode=0o755, exist_ok=True)


def acquire_lock(run_dir):
    """The update lock (an flock on RUN_DIR/lock), or None if another update or
    recovery holds it. Released when the returned file is closed."""
    handle = open(os.path.join(run_dir, LOCK), "a", encoding="utf-8")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        return None
    return handle


def _read_text(path):
    try:
        with open(path, encoding="utf-8") as handle:
            return handle.read()
    except OSError:
        return None


def _write_version(stamp, text):
    """Atomically stamp ``text`` (or remove the stamp for None)."""
    if text is None:
        try:
            os.remove(stamp)
        except OSError:
            pass
        return
    os.makedirs(os.path.dirname(stamp), exist_ok=True)
    tmp = stamp + ".tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        handle.write(text)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, stamp)


def _systemctl_show(service, prop, runner=run):
    try:
        res = runner(["systemctl", "show", "-p", prop, "--value", service],
                     check=False, capture_output=True, text=True)
    except subprocess.SubprocessError:
        return ""
    return (getattr(res, "stdout", "") or "").strip()


def daemon_healthy(runner=run, sleep=time.sleep, timeout=HEALTH_TIMEOUT,
                   settle=HEALTH_SETTLE):
    """True once the restarted daemon is active and then stays active, with no
    restart, for ``settle`` seconds. A daemon that crashes on start is caught
    here (its unit restarts it on failure) and the update is rolled back."""
    for _ in range(max(1, timeout // 2)):
        if _service_active(DAEMON, runner=runner):
            break
        sleep(2)
    else:
        return False
    restarts = _systemctl_show(DAEMON, "NRestarts", runner=runner)
    sleep(settle)
    return (_service_active(DAEMON, runner=runner)
            and _systemctl_show(DAEMON, "NRestarts", runner=runner) == restarts)


def enable_recovery(runner=run):
    """Enable the boot-time recovery unit (the playbook does too); a device that
    got the unit through an app-layer update has it on from then on."""
    runner(["systemctl", "enable", "--quiet", RECOVER_SERVICE], check=False)


def _rollback(restore, snapshot, stamp, old_version, runner, do_package):
    """Undo a partly or fully applied update: files, package and version."""
    restore_backup(restore)
    if do_package and snapshot:
        try:
            restore_package(snapshot, runner=runner)
        except (OSError, subprocess.SubprocessError):
            pass
    try:
        _write_version(stamp, old_version)
    except OSError:
        pass


def apply_update(src, tag, state_file=None, version_file=VERSION_FILE,
                 prefix="", do_package=True, do_services=True, do_backup=True,
                 config_file=CONFIG_FILE, runner=run, sleep=time.sleep):
    """Orchestrate one in-place update from clone ``src`` for release ``tag``.
    Returns (ok, error). Pauses the device, takes a pre-update backup if one is
    set up, lays the new files down, then restarts whatever was running. On any
    failure - including a daemon that will not stay up afterwards - the files,
    the package and the version stamp are restored and the paused services are
    brought back, so a failed or interrupted update leaves the device on the
    previous version, running. Only one update runs at a time."""
    run_dir = _dest(RUN_DIR, prefix)
    state_file = state_file or os.path.join(run_dir, RUN_STATE)
    ensure_run_dir(run_dir)
    lock = acquire_lock(run_dir)
    if lock is None:
        err = "another update is already running"
        return False, err
    try:
        if os.path.exists(os.path.join(run_dir, MARKER)):
            err = ("an earlier update was interrupted and has not been rolled "
                   "back yet; reboot (or run update-apply --recover) first")
            write_state(state_file, status="error", detail=err, finished_at=time.time())
            return False, err
        return _apply_locked(src, tag, state_file, version_file, prefix,
                             do_package, do_services, do_backup, config_file,
                             runner, sleep, run_dir)
    finally:
        lock.close()


def _apply_locked(src, tag, state_file, version_file, prefix, do_package,
                  do_services, do_backup, config_file, runner, sleep, run_dir):
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
        bok, berr = run_backup(runner=runner, timeout=backup_timeout(config_file))
        if not bok:
            write_state(state_file, status="error", detail=berr, finished_at=time.time())
            if do_services:
                _restore_quietly(prior, runner=runner)
            return False, berr

    stamp_id = time.strftime("%Y%m%d-%H%M%S")
    plan = file_plan(src, prefix=prefix)
    removals = removal_plan(plan, prefix=prefix, run_dir=run_dir)
    backup_dir = os.path.join(run_dir, "backup-" + stamp_id)
    stamp = _dest(version_file, prefix)
    old_version = _read_text(stamp)
    snapshot = None
    try:
        restore = backup_current(plan, backup_dir, removals)
        if do_package:
            snapshot = os.path.join(PKG_SNAPSHOT_ROOT, "pkg-" + stamp_id)
            snapshot_package(snapshot, runner=runner)
    except (OSError, shutil.Error, subprocess.SubprocessError) as exc:
        detail = "could not back up the current version (%s); nothing changed" % exc
        write_state(state_file, status="error", detail=detail, finished_at=time.time())
        if do_services:
            _restore_quietly(prior, runner=runner)
        return False, detail
    # From here a power cut is recoverable: the boot-time recovery unit finds
    # this marker and restores the backup it names.
    _write_json_atomic(os.path.join(run_dir, MARKER), {
        "tag": tag, "backup_dir": backup_dir, "snapshot": snapshot,
        "old_version": old_version, "started_at": time.time()})

    def fail(detail):
        _rollback(restore, snapshot, stamp, old_version, runner, do_package)
        write_state(state_file, status="error", detail=detail, finished_at=time.time())
        if do_services:
            _restore_quietly(prior, runner=runner)
        _clear_marker(run_dir)
        return False, detail

    try:
        apply_plan(plan, removals)
        if do_package:
            install_package(src, runner=runner)
    except (OSError, shutil.Error, subprocess.SubprocessError) as exc:
        return fail("update failed (%s); restored previous files" % exc)

    # The new files are down and the package imports. Stamp the version so the
    # restart loads it.
    try:
        _write_version(stamp, tag + "\n")
    except OSError:
        pass

    if do_services:
        enable_recovery(runner=runner)
        try:
            restore_services(prior, runner=runner)
        except subprocess.SubprocessError as exc:
            # systemd itself is unreachable, so a rollback could not restart
            # anything either; keep the new version and report it.
            write_state(state_file, status="error",
                        detail="installed %s but restart failed (%s)" % (tag, exc),
                        finished_at=time.time())
            _clear_marker(run_dir)
            return False, str(exc)
        if prior.get("daemon") and not daemon_healthy(runner=runner, sleep=sleep):
            # Stop what the new version started before putting the old one back.
            quiesce_services(runner=runner)
            return fail("the plugin daemon did not stay up on %s; rolled back to "
                        "the previous version" % tag)

    try:
        record_installed(plan, run_dir, prefix=prefix)
    except OSError:
        pass
    _clear_marker(run_dir)
    if snapshot:
        discard_snapshot(snapshot, runner=runner)
    prune_backups(run_dir)
    write_state(state_file, status="ok", detail="updated to %s" % tag,
                installed=tag, backup_dir=backup_dir, finished_at=time.time())
    return True, ""


def _clear_marker(run_dir):
    try:
        os.remove(os.path.join(run_dir, MARKER))
    except OSError:
        pass


def recover(prefix="", version_file=VERSION_FILE, runner=run, do_package=True):
    """Roll back an update that was interrupted mid-way (power loss, crash),
    from the backup its in-progress marker names. Run at boot, before the
    daemon, capture and agent start. Returns (ok, detail)."""
    run_dir = _dest(RUN_DIR, prefix)
    marker_path = os.path.join(run_dir, MARKER)
    marker = _read_json(marker_path)
    if marker is None:
        return True, "nothing to recover"
    ensure_run_dir(run_dir)
    lock = acquire_lock(run_dir)
    if lock is None:
        return False, "an update is running"
    try:
        state_file = os.path.join(run_dir, RUN_STATE)
        backup_dir = marker.get("backup_dir") or ""
        # Only ever restore from a backup inside the root-owned run dir.
        if os.path.dirname(os.path.realpath(backup_dir)) != os.path.realpath(run_dir):
            _clear_marker(run_dir)
            return False, "the interrupted update's backup is not in %s" % run_dir
        restore = _read_json(os.path.join(backup_dir, "manifest.json")) or {}
        snapshot = marker.get("snapshot")
        if snapshot and not os.path.realpath(snapshot).startswith(
                os.path.realpath(PKG_SNAPSHOT_ROOT) + os.sep):
            snapshot = None
        _rollback(restore, snapshot, _dest(version_file, prefix),
                  marker.get("old_version"), runner, do_package)
        runner(["systemctl", "daemon-reload"], check=False)
        _clear_marker(run_dir)
        detail = ("the update to %s was interrupted and has been rolled back"
                  % (marker.get("tag") or "a new version"))
        write_state(state_file, status="error", detail=detail, finished_at=time.time())
        return True, detail
    finally:
        lock.close()


# -- --verify: decide what may be installed, from root-owned inputs only ------
_TAG_RE = re.compile(r"[0-9A-Za-z][0-9A-Za-z._+-]{0,63}")
_REPO_RE = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")
_COMMIT_RE = re.compile(r"[0-9a-f]{40}")


class Refused(Exception):
    pass


def version_key(text):
    """SemVer-ordered key for ``X.Y.Z`` / ``X.Y.Z-beta.N`` (``None`` for any
    other form). Must match plugins/updates.py's ``version_key``; a parity
    test checks the two agree."""
    if not text:
        return None
    body = text.strip().lstrip("vV").split("+", 1)[0]
    core, sep, suffix = body.partition("-")
    if not core or not core.replace(".", "").isdigit():
        return None
    nums = []
    for part in core.split("."):
        if part.isdigit():
            nums.append(int(part))
        else:
            break
    if not nums:
        return None
    nums = tuple(nums)
    if not sep:
        return (nums, (1,))
    label, dot, number = suffix.partition(".")
    if label == "beta" and dot and number.isdigit():
        return (nums, (0, int(number)))
    return None


def _text(value):
    return value.strip() if isinstance(value, str) else ""


def _is_prerelease(rel):
    key = version_key(_text(rel.get("tag_name")))
    return bool(rel.get("prerelease")) or (key is not None and key[1][0] == 0)


def read_updates_config(config_file=CONFIG_FILE):
    """The [plugins.updates] repo/host/token/beta, from root-owned config.toml."""
    try:
        import tomllib
        with open(config_file, "rb") as handle:
            data = tomllib.load(handle)
    except (OSError, ValueError, ImportError):
        data = {}
    table = (data.get("plugins") or {}).get("updates") or {}
    return {
        "repo": _text(table.get("repo")) or DEFAULT_REPO,
        "host": (_text(table.get("host")) or DEFAULT_HOST).rstrip("/"),
        "token": _text(table.get("token")),
        "beta": table.get("beta") is True,
        "timeout": table.get("timeout") if isinstance(table.get("timeout"), int) else 30,
    }


def http_fetch(url, token="", timeout=30, limit=MAX_LIST_BYTES, attempts=3):
    """GET ``url`` and parse JSON, with retries for transient errors and a size
    cap. Returns (status, body); body is None for an HTTP error. The token is
    only ever added by the caller for URLs on the configured host."""
    headers = {"Accept": "application/json", "User-Agent": "kestrel-update"}
    if token:
        headers["Authorization"] = "token %s" % token
    delay = 2
    for attempt in range(attempts):
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read(limit + 1)
                if len(raw) > limit:
                    raise Refused("the server's reply for %s is too large" % url)
                return resp.status, json.loads(raw.decode("utf-8"))
        except urllib.error.HTTPError as exc:
            if exc.code < 500 or attempt == attempts - 1:
                return exc.code, None
        except (urllib.error.URLError, OSError, ValueError) as exc:
            if attempt == attempts - 1:
                raise Refused("could not reach the release server (%s)" % exc)
        time.sleep(delay)
        delay *= 2
    return 599, None


def verify_release(tag, explicit=False, force=False, expect="",
                   config_file=CONFIG_FILE, version_file=VERSION_FILE,
                   fetch=http_fetch):
    """Decide whether release ``tag`` may be installed in place, asking the
    configured server directly. Returns a dict with ``ok`` and either ``error``
    or the ``tag``/``commit``/``host``/``repo`` to fetch (plus ``warnings``
    for checks ``force`` overrode). ``explicit`` is an operator-named tag
    (``--ref``), which may be a beta with beta updates off; ``force``
    overrides the safety checks (reflash, gap, downgrade, dev build), never
    the release's existence, a draft or a mismatch with ``expect``."""
    warnings = []

    def refuse(msg):
        if not force:
            raise Refused(msg)
        warnings.append(msg)

    try:
        cfg = read_updates_config(config_file)
        host, repo, token = cfg["host"], cfg["repo"], cfg["token"]
        if not host.startswith("https://"):
            raise Refused("[plugins.updates] host must be an https:// URL")
        if not _REPO_RE.fullmatch(repo):
            raise Refused("[plugins.updates] repo must be owner/name")
        if not tag or not _TAG_RE.fullmatch(tag):
            raise Refused("not a valid release tag: %r" % tag)
        if expect and tag != expect:
            raise Refused("the release to install changed since it was confirmed "
                          "(confirmed %s, now %s); review the update again"
                          % (expect, tag))
        api = "%s/api/v1/repos/%s" % (host, repo)
        timeout = cfg["timeout"]

        def get(url, limit=MAX_LIST_BYTES):
            on_host = url.startswith(host + "/")
            return fetch(url, token if on_host else "", timeout, limit)

        status, rel = get("%s/releases/tags/%s" % (api, urllib.parse.quote(tag, safe="")))
        if status in (401, 403):
            raise Refused("the release server refused the request (HTTP %s); check "
                          "the [plugins.updates] token" % status)
        if status == 404 or not isinstance(rel, dict):
            if status == 404 and force:
                warnings.append("%s is not a published release; its commit can't "
                                "be pinned" % tag)
                return {"ok": True, "tag": tag, "commit": "", "host": host,
                        "repo": repo, "warnings": warnings}
            raise Refused("%s is not a published release of %s" % (tag, repo))
        if rel.get("draft"):
            raise Refused("%s is a draft release" % tag)

        installed = (_read_text(version_file) or "").strip()
        tkey, ikey = version_key(tag), version_key(installed)
        if tkey is None:
            refuse("%s is not an X.Y.Z or X.Y.Z-beta.N release" % tag)
        if _is_prerelease(rel) and not cfg["beta"] and not explicit:
            refuse("%s is a beta release and beta updates are off" % tag)
        if ikey is None:
            refuse("the installed version (%s) is a dev build" % (installed or "unknown"))
        elif tkey is not None and tkey <= ikey:
            refuse("%s is not newer than the installed %s" % (tag, installed))

        manifest = _manifest_for(rel, get)
        commit = ""
        if manifest is None:
            refuse("%s has no update manifest, so it needs a reflash" % tag)
        else:
            commit = _text(manifest.get("commit"))
            if commit and not _COMMIT_RE.fullmatch(commit):
                raise Refused("%s's manifest names an invalid commit" % tag)
            if not commit:
                refuse("%s's manifest does not pin a commit" % tag)
            if _text(manifest.get("kind")).lower() != "app-layer":
                refuse(_text(manifest.get("requires_reflash_reason"))
                       or "%s needs a reflash" % tag)
            min_from = manifest.get("min_update_from")
            if min_from:
                mkey = version_key(str(min_from))
                if mkey is None:
                    refuse("%s's manifest has an unreadable min_update_from" % tag)
                elif ikey is not None and ikey < mkey:
                    refuse("the installed %s predates the oldest version %s can "
                           "update in place (%s)" % (installed, tag, min_from))

        if tkey is not None and ikey is not None and tkey > ikey:
            reason = _gap_reason(api, get, ikey, tkey, installed, tag)
            if reason:
                refuse(reason)
        return {"ok": True, "tag": tag, "commit": commit, "host": host,
                "repo": repo, "warnings": warnings}
    except Refused as exc:
        return {"ok": False, "error": str(exc)}


def _manifest_for(rel, get):
    """The release's update manifest (a dict), or None. Only fetched from the
    configured host."""
    for asset in rel.get("assets") or []:
        if isinstance(asset, dict) and asset.get("name") == MANIFEST_ASSET:
            url = _text(asset.get("browser_download_url"))
            if not url:
                return None
            status, body = get(url, MAX_MANIFEST_BYTES)
            return body if status < 400 and isinstance(body, dict) else None
    return None


def _gap_reason(api, get, ikey, tkey, installed, tag):
    """A reason when a release strictly between installed and the target needs a
    reflash (or has no manifest), or when the release list can't be read whole
    enough to tell; else "". Betas count only for a beta target (a stable
    release's manifest is built against the previous stable one)."""
    releases, complete = [], False
    for page in range(1, RELEASES_MAX_PAGES + 1):
        status, batch = get("%s/releases?limit=%d&page=%d" % (api, RELEASES_PAGE, page))
        if status >= 400 or not isinstance(batch, list):
            return ("could not read the release list to check the releases "
                    "between %s and %s" % (installed, tag))
        releases.extend(r for r in batch if isinstance(r, dict))
        if len(batch) < RELEASES_PAGE:
            complete = True
            break
    with_betas = tkey[1][0] == 0
    keys = []
    gap = []
    for rel in releases:
        if rel.get("draft"):
            continue
        key = version_key(_text(rel.get("tag_name")))
        if key is None:
            continue
        keys.append(key)
        if not with_betas and _is_prerelease(rel):
            continue
        if ikey < key < tkey:
            gap.append((key, rel))
    if not complete and (not keys or min(keys) > ikey):
        return ("the installed %s is too far behind to check every release up "
                "to %s; reflash" % (installed, tag))
    for _key, rel in sorted(gap, key=lambda g: g[0], reverse=True):
        manifest = _manifest_for(rel, get)
        name = _text(rel.get("tag_name"))
        if manifest is None:
            return ("release %s in between has no update manifest, so it needs "
                    "a reflash and can't be skipped" % name)
        if _text(manifest.get("kind")).lower() != "app-layer":
            return ("release %s in between needs a reflash, so it can't be "
                    "skipped by an in-place update" % name)
    return ""


def main(argv):
    parser = argparse.ArgumentParser(description="apply an in-place app-layer update")
    parser.add_argument("--verify", action="store_true",
                        help="check --tag against the release server; print JSON")
    parser.add_argument("--recover", action="store_true",
                        help="roll back an interrupted update (run at boot)")
    parser.add_argument("--explicit", action="store_true",
                        help="--verify: the operator named this tag (--ref)")
    parser.add_argument("--force", action="store_true",
                        help="--verify: override the safety checks")
    parser.add_argument("--expect", default="",
                        help="--verify: refuse unless the tag is this one")
    parser.add_argument("--src", help="path to the cloned release")
    parser.add_argument("--tag", help="the release tag being applied")
    parser.add_argument("--repo", default="", help="owner/repo (recorded only)")
    parser.add_argument("--state", default="",
                        help="run-status file (default: %s/%s)" % (RUN_DIR, RUN_STATE))
    parser.add_argument("--prefix", default="", help="relocate dests (testing)")
    parser.add_argument("--cleanup", default="", help="remove this dir when done")
    parser.add_argument("--no-package", action="store_true")
    parser.add_argument("--no-services", action="store_true")
    parser.add_argument("--no-backup", action="store_true",
                        help="skip the pre-update backup even if one is set up")
    args = parser.parse_args(argv[1:])
    if args.verify:
        verdict = verify_release(args.tag or "", explicit=args.explicit,
                                 force=args.force, expect=args.expect)
        sys.stdout.write(json.dumps(verdict) + "\n")
        return 0 if verdict.get("ok") else 1
    if args.recover:
        ok, detail = recover(prefix=args.prefix, do_package=not args.no_package)
        sys.stderr.write("update recover: %s\n" % detail)
        return 0 if ok else 1
    if not args.src or not args.tag:
        parser.error("--src and --tag are required to apply an update")
    # Older CLIs pass the pi-owned state file; root never writes there.
    state = args.state if args.state and args.state != LEGACY_STATE_FILE else None
    ok, err = apply_update(
        args.src, args.tag, state_file=state, prefix=args.prefix,
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
