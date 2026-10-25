"""backup - mirror the on-device capture data to a remote server over SSH.

Backs up everything worth keeping from the capture store to a remote host, so a
lost or reflashed SD card doesn't take the data with it: the hashline files, the
GPS sidecars and the wigle geo-cache (whether a fix was captured on-device or
looked up), the wpa-sec cracked-results potfile, and any archived captures - all
of which live under the capture store, so the backup is a copy of that store
(minus the transient ``.run/`` raw-packet dir, which is out of the capture
contract and re-created each session).

It uses **rsync over SSH** by default: an incremental delta copy, so only new or
changed bytes cross the link and an interrupted transfer resumes
(``--partial``). ``tool = "scp"`` forces a plain recursive copy instead (no
delta - every file every run), kept only because scp is the lowest common
denominator; rsync is the recommendation.

**Passwordless copying.** The device authenticates with the ``pi`` account's SSH
key. No key or password is ever carried in the repo or in config. The one-time
install of the device's *public* key on the remote - the only step that needs
the remote password - is done from the terminal with ``kestrel backup
setup``, which runs ``ssh-copy-id`` so the password is typed into ssh and never
stored. This plugin only ever runs key-based, non-interactive ssh
(``BatchMode=yes``), so it never blocks waiting for a password.

Because it transmits captured material off-device it is **opt-in**: it declares
the ``network`` capability (so enabling it takes the authorized-use
acknowledgment), ships disabled, and stores no secret. It backs up once at load,
on every reconnect (``on_internet_available``), and at most every
``interval_seconds`` otherwise; the transfer runs on its own worker thread so a
large sync never blocks the daemon's hook pool.
"""

from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import threading
import time
from urllib.parse import urlencode

from kestrel_pi import Plugin

_DEFAULT_KEY = "/home/pi/.ssh/id_ed25519"
# Retry a dropped transfer a few times: a flaky BT-PAN/Wi-Fi tether drops rsync
# mid-stream, and --partial keeps the progress so a retry resumes it.
_RSYNC_TRIES = 3
_RSYNC_BACKOFF = 5


class Backup(Plugin):
    __author__ = "kestrel"
    __version__ = "0.1.0"
    __license__ = "GPL3"
    __description__ = "Back up the capture data to a remote server over SSH (opt-in)."

    __target_api__ = 2
    __capabilities__ = ("capture-read", "network", "ui")
    # Two row buttons on the Plugins page: "Backup" (a POST, start a backup now)
    # and "Restore" (a GET to a confirm page the plugin renders). Both shown only
    # once passwordless SSH is set up - see webhook_actions.
    __actions__ = (
        {"label": "Backup", "path": "backup-now", "method": "post", "order": 1},
        {"label": "Restore", "path": "restore", "method": "get", "order": 2},
    )
    __options__ = {
        # host/user/path are supplied by the operator at enable time, never
        # baked in. They are not secret (so they are plain, editable fields),
        # but the plugin stays idle until all three are set.
        "host": {"type": "str", "default": ""},
        "user": {"type": "str", "default": ""},
        "path": {"type": "str", "default": ""},
        "port": {"type": "int", "default": 22},
        "tool": {"type": "str", "default": "rsync"},   # rsync | scp
        # Literal default (not _DEFAULT_KEY): the root plugins-ctl apply helper
        # reads __options__ with ast.literal_eval and never imports plugin code,
        # so a Name reference here would not be resolvable.
        "ssh_key": {"type": "str", "default": "/home/pi/.ssh/id_ed25519"},
        "interval_seconds": {"type": "int", "default": 3600},
        "timeout": {"type": "int", "default": 1800},
        # The Phase 4 learning-agent runtime dir, mirrored to <remote>/agent/ so
        # a trained model and its episode logs survive a reflash and a fresh
        # device can inherit the training. The ephemeral status.json / scan
        # override are excluded. Empty disables the agent mirror.
        "agent_dir": {"type": "str", "default": "/var/lib/kestrel/agent"},
    }

    def on_loaded(self):
        self.log = self.context.logger
        self._lock = threading.Lock()        # guards the readout state below
        self._run_lock = threading.Lock()    # at most one transfer at a time
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._state_path = os.path.join(
            self.context.capture.store_dir, ".backup-state.json"
        )
        # Where the CLI `backup restore` records its progress (the web Restore
        # button runs that CLI in the background). In the store's transient
        # .run/ dir, which both backup and restore exclude from the sync.
        self._restore_state_path = os.path.join(
            self.context.capture.store_dir, ".run", "restore-state.json"
        )
        state = self._load_state()
        # Readout state: last success time, last status string, and the files/
        # bytes moved by the last run. Persisted so a reboot keeps the history.
        self._last_ok = state.get("last_ok")
        self._last_status = state.get("last_status") or "idle"
        self._last_files = int(state.get("last_files") or 0)
        self._last_bytes = int(state.get("last_bytes") or 0)
        self._last_run = 0.0                 # monotonic throttle marker
        self._set_health()
        self._worker = threading.Thread(
            target=self._run, name="backup-worker", daemon=True)
        self._worker.start()

    def on_unload(self):
        self._stop.set()
        self._wake.set()

    # -- row buttons (Backup / Restore) ---------------------------------
    def webhook_actions(self):
        """Offer Backup and Restore only once passwordless SSH is set up
        (``backup setup`` has installed the key): both need the key-based remote,
        so neither is offered before it can work. Hidden until then."""
        if self._missing_config() or not self._key_ready():
            return ()
        return self.__actions__

    def on_webhook(self, path, request):
        """The backup row's two actions, both rendered/handled by the plugin:

        ``backup-now`` (POST): start a manual backup now (no confirm - a backup
        only copies to the remote, it never overwrites local data), via the root
        apply helper, then back to the Plugins page with a banner.

        ``restore`` (GET): a confirm page (restoring overwrites local data, so a
        stray tap never triggers it). Its form POSTs back to ``restore`` with
        ``confirm=yes``; core validates the CSRF token before dispatching, so a
        confirmed POST runs the restore and shows a "restoring" page."""
        if path == "backup-now":
            return self._do_backup_now()
        if path == "restore":
            if request.method == "POST" and request.form.get("confirm") == "yes":
                return self._do_restore(request)
            return self._restore_confirm_page(request)
        return self.context.redirect("No such action.", error=True)

    def _do_backup_now(self):
        # The key check must run here, in the daemon (which can see /home), not
        # in the ProtectHome'd root helper (see _key_ready).
        if self._missing_config() or not self._key_ready():
            return self.context.redirect(
                "passwordless SSH is not set up - run "
                "'sudo kestrel backup setup' on the device first",
                error=True)
        reply = self.context.apply({"target": "backup", "action": "now"})
        if reply.get("ok"):
            return self.context.redirect("Backup started.")
        return self.context.redirect(reply.get("error") or "not applied",
                                     error=True)

    def _do_restore(self, request):
        if self._missing_config() or not self._key_ready():
            return self.context.redirect(
                "passwordless SSH is not set up - run "
                "'sudo kestrel backup setup' on the device first",
                error=True)
        reply = self.context.apply({"target": "backup-restore",
                                    "action": "restore"})
        if not reply.get("ok"):
            return self.context.redirect(reply.get("error") or "not applied",
                                         error=True)
        # The root helper runs the (possibly slow) restore in the background.
        body = (
            "<h1>Restoring…</h1>"
            "<p>Pulling the capture data and the agent model back from the "
            "backup in the background. Larger stores take a while; the "
            "dashboard's backup card and the journal show progress. Settings "
            "are not restored, so re-enable capture and plugins and re-enter "
            "any keys once it is done.</p>"
            + '<p><a href="/plugins' + self._qs(request) + '">'
            "Back to Plugins</a></p>")
        return self.context.page("kestrel - restore", body)

    def _restore_confirm_page(self, request):
        esc = self.context.esc
        token = request.args.get("token") or ""
        action = "/plugins/backup/restore" + self._qs(request)
        csrf = ('<input type="hidden" name="csrf" value="'
                + esc(self.context.form_csrf) + '">')
        token_field = ('<input type="hidden" name="token" value="' + esc(token)
                       + '">' if token else "")
        body = (
            "<h1>Restore from the backup?</h1>"
            + '<div class="ack"><p>This pulls the capture store and the learning '
            "agent's model and logs back from the remote backup, overwriting any "
            "local files of the same name (nothing on the remote is changed). It "
            "is meant for recovering onto a fresh image. Settings are not part of "
            "the backup, so you will still re-enable capture and plugins and "
            "re-enter any keys afterwards. A large first restore runs in the "
            "background and can take a while.</p>"
            + '<form method="post" action="' + esc(action) + '">'
            + csrf + token_field
            + '<input type="hidden" name="confirm" value="yes">'
            + '<button type="submit">Restore now</button></form></div>'
            + '<p><a href="/plugins' + self._qs(request) + '">Cancel</a></p>')
        return self.context.page("kestrel - restore", body)

    @staticmethod
    def _qs(request):
        """A ``?token=…`` query string carrying the current token, or ""; keeps
        token-auth navigation signed across the plugin's own links."""
        token = request.args.get("token") or ""
        return ("?" + urlencode({"token": token})) if token else ""

    # -- triggers --------------------------------------------------------
    def on_internet_available(self):
        # A reconnect: back up the latest data now (the worker treats an event
        # wake as a forced run, bypassing the interval throttle).
        self._wake.set()

    def on_epoch(self, epoch, info):
        # Keep the /plugins config-page hint current between backups: a cheap
        # recompute so "not configured" / "needs key setup" clears within an
        # epoch once the remote is set and `backup setup` has installed the key.
        self._set_health()
        # Pick up a backup run outside the plugin - the CLI `kestrel
        # backup now` writes the same state file - so the dashboard card reflects
        # a manual backup within an epoch, without a daemon restart.
        self._adopt_external_state()

    # -- dashboard readout ----------------------------------------------
    def on_ui_setup(self, ui):
        ui.add_element("last", "never", label="Last backup")
        ui.add_element("status", "idle", label="Status")
        ui.add_element("files", "0", label="Files", kind="number")
        ui.add_element("size", "0 B", label="Size")
        ui.add_element("target", "-", label="Target")
        ui.add_element("restore", "idle", label="Restore")

    def on_ui_update(self, ui, state):
        # The main dashboard card shows only the run state (idle/running/ok/
        # error). Backup is optional, so the "set host/user/path" and "run
        # backup setup" hints live on the /plugins config page (the plugin's
        # health note), not on the main UI.
        with self._lock:
            last_ok, status = self._last_ok, self._last_status
            files, nbytes = self._last_files, self._last_bytes
        ui.set("last", _ago(last_ok))
        ui.set("status", status)
        ui.set("files", str(files))
        ui.set("size", _human_bytes(nbytes))
        ui.set("target", self._target() or "-")
        ui.set("restore", self._restore_status_text())

    # -- worker ----------------------------------------------------------
    def _run(self):
        # One attempt at load (forced), then wake on a trigger or on the
        # interval timeout. An event wake is a forced run (reconnect); a timeout
        # wake is the periodic run, which the throttle lets through.
        self._maybe_backup(force=True)
        while not self._stop.is_set():
            woke = self._wake.wait(timeout=self._interval())
            self._wake.clear()
            if self._stop.is_set():
                return
            self._maybe_backup(force=woke)

    def _maybe_backup(self, force=False):
        missing = self._missing_config()
        if missing or not self._key_ready():
            self._set_health()
            return
        if not self.context.connectivity.online:
            return
        store = self.context.capture.store_dir
        if not os.path.isdir(store):
            self._set_status("idle: nothing to back up yet")
            return
        now = time.monotonic()
        if not force and self._last_run and now - self._last_run < self._interval():
            return
        if not self._run_lock.acquire(blocking=False):
            return  # a transfer is already running
        try:
            self._last_run = now
            self._set_status("running")
            ok, files, nbytes, detail = self._transfer(store)
            if ok:
                # Only advance the "last backup" time when data actually moved.
                # A successful rsync that copied nothing (nothing changed since
                # the last run) reports 0 files transferred; counting that as a
                # fresh backup pegged the card to "just now" every time a flaky
                # link re-triggered a sync. scp reports local tree totals, so
                # its count is >0 whenever there is anything to copy.
                with self._lock:
                    if files:
                        self._last_ok = time.time()
                    self._last_status = "ok" if files else "ok (no changes)"
                    self._last_files = files
                    self._last_bytes = nbytes
                self.log.info("backup ok: %d file(s), %d byte(s) to %s",
                              files, nbytes, self._target())
            else:
                self._set_status("error: " + (detail or "failed")[:80])
                self.log.warning("backup failed: %s", detail)
            self._save_state()
        finally:
            self._run_lock.release()

    # -- transfer --------------------------------------------------------
    def _transfer(self, store):
        """Run one backup. Returns ``(ok, files, bytes, detail)``.

        Mirrors the capture store to ``<remote>/`` and, when present, the agent
        runtime dir to ``<remote>/agent/`` (the model, its sidecar and the
        episode logs - enough for a fresh device to inherit the training),
        excluding the ephemeral status/override files."""
        if not self.context.require_capability("network"):
            return (False, 0, 0, "network capability not declared")
        tool = (self.options.get("tool") or "rsync").lower()
        use_scp = tool == "scp" or not shutil.which("rsync")
        total_files = total_bytes = 0
        for src, remote, excludes in self._sources(store):
            run = self._scp_dir if use_scp else self._rsync
            ok, files, nbytes, detail = run(src, remote, excludes)
            if not ok:
                return (False, total_files, total_bytes, detail)
            total_files += files
            total_bytes += nbytes
        return (True, total_files, total_bytes, "")

    def _sources(self, store):
        """``(local_dir, remote_path, excludes)`` for each mirrored root: the
        capture store (minus the transient run dir) and the agent dir (minus the
        ephemeral status/override), when the agent dir exists."""
        base = self._remote_path()
        sources = [(store, base, ["/.run/", "/.run"])]
        agent_dir = (self.options.get("agent_dir") or "").strip()
        if agent_dir and os.path.isdir(agent_dir):
            remote = base.rstrip("/") + "/agent"
            sources.append((agent_dir, remote,
                            ["/status.json", "/scan-override.json",
                             "/*.tmp"]))
        return sources

    def _rsync(self, src, remote_path, excludes=None):
        target = "%s@%s:%s" % (self.options.get("user"), self.options.get("host"),
                               remote_path)
        # Keep these flags in step with `kestrel backup now` in the CLI.
        cmd = ["rsync", "-az", "--partial", "--mkpath", "--stats"]
        for pattern in excludes or ():
            cmd += ["--exclude", pattern]
        cmd += ["-e", self._ssh_cmd(), src.rstrip("/") + "/", target]
        # A flaky tether drops a transfer mid-stream; --partial keeps the
        # progress, so retry a few times before reporting failure.
        last_err = ""
        for attempt in range(_RSYNC_TRIES):
            try:
                proc = subprocess.run(cmd, capture_output=True, text=True,
                                      timeout=self._timeout(), check=False)
            except FileNotFoundError:
                return (False, 0, 0, "rsync not installed")
            except (OSError, subprocess.SubprocessError) as exc:
                last_err = str(exc)
            else:
                if proc.returncode == 0:
                    files, nbytes = _parse_rsync_stats(proc.stdout)
                    return (True, files, nbytes, "")
                last_err = (_best_error(proc.stderr or proc.stdout)
                            or ("rsync exit %d" % proc.returncode))
            if attempt + 1 < _RSYNC_TRIES:
                time.sleep(_RSYNC_BACKOFF * (attempt + 1))
        return (False, 0, 0, last_err)

    def _scp_dir(self, src, remote, excludes=None):
        """Fallback copy with scp (no delta; copies everything each run) of one
        directory into ``remote``, skipping names matched by ``excludes``."""
        if not shutil.which("scp"):
            return (False, 0, 0, "scp not installed")
        key = self.options.get("ssh_key") or _DEFAULT_KEY
        port = str(self._port())
        user, host = self.options.get("user"), self.options.get("host")
        try:
            names = sorted(os.listdir(src))
        except OSError as exc:
            return (False, 0, 0, str(exc))
        items = [os.path.join(src, n) for n in names
                 if not _excluded(n, excludes)]
        if not items:
            return (True, 0, 0, "")
        sshopts = ["-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=accept-new",
                   "-o", "ConnectTimeout=20",
                   "-o", "ServerAliveInterval=15", "-o", "ServerAliveCountMax=4"]
        try:
            # scp has no --mkpath, so create the destination directory first.
            subprocess.run(["ssh", "-i", key, "-p", port] + sshopts
                           + ["%s@%s" % (user, host), "mkdir", "-p", remote],
                           capture_output=True, text=True, timeout=60, check=False)
            cmd = (["scp", "-r", "-C", "-i", key, "-P", port] + sshopts + items
                   + ["%s@%s:%s" % (user, host, remote)])
            proc = subprocess.run(cmd, capture_output=True, text=True,
                                  timeout=self._timeout(), check=False)
        except (OSError, subprocess.SubprocessError) as exc:
            return (False, 0, 0, str(exc))
        if proc.returncode != 0:
            return (False, 0, 0, _best_error(proc.stderr or proc.stdout)
                    or ("scp exit %d" % proc.returncode))
        # scp gives no transfer stats; report what we pushed (local totals).
        files = nbytes = 0
        for item in items:
            f, b = _tree_totals(item)
            files += f
            nbytes += b
        return (True, files, nbytes, "")

    def _ssh_cmd(self):
        key = self.options.get("ssh_key") or _DEFAULT_KEY
        # Keepalives so a flaky tether that goes quiet is detected (and torn
        # down) within ~a minute instead of hanging the transfer, and an idle
        # link is kept from being dropped by the phone's power-save.
        return ("ssh -i %s -p %d -o BatchMode=yes "
                "-o StrictHostKeyChecking=accept-new -o ConnectTimeout=20 "
                "-o ServerAliveInterval=15 -o ServerAliveCountMax=4"
                % (shlex.quote(key), self._port()))

    # -- helpers ---------------------------------------------------------
    def _missing_config(self):
        return [k for k in ("host", "user", "path")
                if not (self.options.get(k) or "").strip()]

    def _key_ready(self):
        return os.path.exists(self.options.get("ssh_key") or _DEFAULT_KEY)

    def _remote_path(self):
        return (self.options.get("path") or "").rstrip("/") or "."

    def _target(self):
        if self._missing_config():
            return ""
        return "%s@%s:%s" % (self.options.get("user"), self.options.get("host"),
                             self.options.get("path"))

    def _port(self):
        try:
            return int(self.options.get("port", 22))
        except (TypeError, ValueError):
            return 22

    def _interval(self):
        try:
            return max(60, int(self.options.get("interval_seconds", 3600)))
        except (TypeError, ValueError):
            return 3600

    def _timeout(self):
        try:
            return max(30, int(self.options.get("timeout", 1800)))
        except (TypeError, ValueError):
            return 1800

    def _set_status(self, status):
        with self._lock:
            self._last_status = status

    def _set_health(self):
        missing = self._missing_config()
        if missing:
            self.context.health_note = (
                "not configured: set %s (plugins enable backup --set ...)"
                % ", ".join(missing))
        elif not self._key_ready():
            self.context.health_note = (
                "passwordless SSH not set up - run 'kestrel backup setup' "
                "to install the device key on %s" % self.options.get("host"))
        else:
            self.context.health_note = ""

    def _restore_status_text(self):
        """Human-readable state of the most recent restore, read live from the
        marker the CLI ``backup restore`` writes (the web Restore button runs
        that CLI in the background, so this is the only progress the dashboard
        can show). ``idle`` when no restore has run on this image."""
        try:
            with open(self._restore_state_path, "r", encoding="utf-8") as handle:
                state = json.load(handle)
        except (OSError, ValueError):
            return "idle"
        if not isinstance(state, dict):
            return "idle"
        status = state.get("status")
        if status == "running":
            return "running"
        if status == "ok":
            ago = _ago(state.get("finished"))
            files, nbytes = state.get("files"), state.get("bytes")
            if isinstance(files, (int, float)) and isinstance(nbytes, (int, float)):
                return "ok (%s, %d file%s, %s)" % (
                    ago, int(files), "" if int(files) == 1 else "s",
                    _human_bytes(nbytes))
            return "ok (%s)" % ago
        if status == "error":
            detail = str(state.get("detail") or "").strip()
            base = ("error: " + detail) if detail else "error"
            return "%s (%s)" % (base[:80], _ago(state.get("finished")))
        return "idle"

    def _load_state(self):
        try:
            with open(self._state_path, "r", encoding="utf-8") as handle:
                data = json.load(handle)
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def _adopt_external_state(self):
        """Adopt a newer last-backup time written by something other than this
        plugin's own worker (the CLI `backup now` writes the same state file in
        the same shape). Only a strictly newer ``last_ok`` is taken, so the
        plugin's own, more recent runs are never overwritten by a stale file.
        Runs on the daemon loop, so no lock contention with the worker's own
        save beyond the short critical section below."""
        state = self._load_state()
        ext_ok = state.get("last_ok")
        if not isinstance(ext_ok, (int, float)):
            return
        with self._lock:
            if self._last_ok is not None and ext_ok <= self._last_ok:
                return
            self._last_ok = ext_ok
            self._last_status = state.get("last_status") or self._last_status
            self._last_files = int(state.get("last_files") or 0)
            self._last_bytes = int(state.get("last_bytes") or 0)

    def _save_state(self):
        tmp = self._state_path + ".tmp"
        try:
            with self._lock:
                data = {"last_ok": self._last_ok, "last_status": self._last_status,
                        "last_files": self._last_files, "last_bytes": self._last_bytes}
            with open(tmp, "w", encoding="utf-8") as handle:
                json.dump(data, handle)
            os.replace(tmp, self._state_path)
        except OSError as exc:
            self.log.warning("could not save backup state: %s", exc)


def _excluded(name, excludes):
    """True if ``name`` (a directory entry) matches any rsync-style exclude
    pattern, for the scp fallback (rsync applies the patterns itself). A leading
    slash and a trailing slash are stripped, so ``/.run/`` and ``/*.tmp`` match
    the entry ``.run`` and any ``*.tmp``."""
    import fnmatch

    for pattern in excludes or ():
        pat = pattern.strip("/")
        if pat and (name == pat or fnmatch.fnmatch(name, pat)):
            return True
    return False


def _parse_rsync_stats(out):
    """Pull files-transferred and bytes-transferred from ``rsync --stats``."""
    files = nbytes = 0
    for line in (out or "").splitlines():
        low = line.lower()
        if "regular files transferred" in low:
            files = _first_int(line)
        elif "total transferred file size" in low:
            nbytes = _first_int(line)
    return files, nbytes


def _first_int(line):
    digits = "".join(ch for ch in line.split(":", 1)[-1] if ch.isdigit())
    return int(digits) if digits else 0


def _best_error(text):
    """The most useful line of rsync/scp stderr. rsync prints the specific cause
    (``rsync: [receiver] mkdir "..." failed: Permission denied (13)``) and then a
    generic summary (``rsync error: error in file IO (code 11) at main.c(787)``);
    the summary is last, so return the specific ``rsync:`` line when present and
    fall back to the last line otherwise."""
    lines = [ln.strip() for ln in (text or "").splitlines() if ln.strip()]
    for line in lines:
        low = line.lower()
        if low.startswith("rsync:") and not low.startswith("rsync error:"):
            return line
    return lines[-1] if lines else ""


def _tree_totals(path):
    """(file count, total bytes) under ``path`` (a file counts as itself)."""
    if os.path.isfile(path):
        try:
            return (1, os.path.getsize(path))
        except OSError:
            return (1, 0)
    files = nbytes = 0
    for root, _dirs, names in os.walk(path):
        for name in names:
            try:
                nbytes += os.path.getsize(os.path.join(root, name))
                files += 1
            except OSError:
                continue
    return files, nbytes


def _human_bytes(n):
    try:
        n = float(n)
    except (TypeError, ValueError):
        return "0 B"
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return ("%d %s" % (int(n), unit)) if unit == "B" else ("%.1f %s" % (n, unit))
        n /= 1024.0
    return "%d B" % int(n)


def _ago(epoch):
    if not epoch:
        return "never"
    delta = int(time.time() - float(epoch))
    if delta < 0:
        delta = 0
    if delta < 60:
        return "just now"
    if delta < 3600:
        return "%dm ago" % (delta // 60)
    if delta < 86400:
        return "%dh ago" % (delta // 3600)
    return "%dd ago" % (delta // 86400)
