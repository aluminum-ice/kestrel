"""remote-train - train the learning agent on a remote desktop host over SSH.

On-device training is gated hard by RAM/heat and doesn't run at all on a small
board. This plugin offloads it to a more powerful Linux host: it pushes the
agent's episode logs to the host, runs the *same* trainer there
(``python -m kestrel_pi.agent.train``), and pulls the finished per-algorithm
model back. The device keeps doing inference the whole time.

Design (see docs/developer/remote-training.md):

- **Isolated on the host.** Everything lives under ``train_path``: the plugin's
  own virtualenv (``venv``), the uploaded episodes and models (``agent/``), and
  job markers (``jobs/``). Nothing touches the host's system Python. The venv
  gets the device's *exact* ``kestrel_pi`` package (synced from the device's
  own install, so the env-spec matches and a model loads back) plus the ML stack
  (torch/SB3/sb3-contrib/gymnasium/numpy) from pip. The install runs detached and
  flaky-link-tolerant; every run verifies the venv first and repairs it if
  needed.

- **Async, disconnect-tolerant, idempotent job model.** A run is launched
  detached on the host (``setsid``), so a dropped link never kills it. Per
  algorithm the host carries a ``jobs/<slug>.lock`` (running) and the finished
  ``agent/model-<slug>.zip`` + sidecar. Each reconcile pulls a finished model
  first, never double-launches a running algorithm (a lock older than
  ``stale_lock_hours`` is a crashed run and may be relaunched), and starts a
  fresh run only when enough new episodes have accrued since the host's last
  model of that algorithm. The algorithm the agent runs is always trained, and
  first. The device's package is synced up before every launch and the run
  imports it ahead of the venv's copy, so the host trains with the device's
  current code (which keeps the held-out rows out and warm-starts from the
  host's previous model).

- **Pull-and-stage; the agent decides.** This plugin runs inside the always-on
  daemon, which must stay torch-free, so it never loads or scores a model. It
  pulls a new finished model into the agent's staging dir
  (``<agent_dir>/.remote-staging/<slug>/``) when its env-spec matches and the
  agent hasn't judged it before (``evaluated.json``), and leaves it there. The
  **agent process** (which already has torch for inference) runs the same
  contest for it as for a model trained on the device - scored on held-out rows
  no model trained on, one rule for both - see docs/developer/agent-interface.md.

Opt-in: it ships captured-derived experience off-device, so it declares the
``network`` capability (enabling it takes the authorized-use ack) and copies as
the device's own key over passwordless SSH, storing no secret. The training host
is independent of the backup host.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import threading
import time

from kestrel_pi import Plugin
from kestrel_pi.agent import (EPISODES_DIR, EVALUATED_NAME, HOST_MODELS_NAME,
                                 REMOTE_STAGING_DIR, model_meta_name_for,
                                 model_name_for, model_slug)
from kestrel_pi.agent import metrics as metrics_mod
from kestrel_pi.agent import policies as policies_mod
from kestrel_pi.config import DEFAULT_CONFIG_PATH, load_config

_DEFAULT_KEY = "/home/pi/.ssh/id_ed25519"
# The ML stack installed into the host venv. torch last (the big download).
_ML_STACK = ("numpy", "gymnasium", "stable-baselines3", "sb3-contrib", "torch")
# Markers under <train_path>, relative to it.
_STACK_READY = ".stack-ready"
_STACK_INSTALLING = ".stack-installing"
_STACK_ERROR = ".stack-error"
# Retry a dropped transfer a few times (--partial resumes it) before failing;
# a flaky BT-PAN/Wi-Fi tether drops rsync mid-stream.
_RSYNC_TRIES = 3
_RSYNC_BACKOFF = 5



def _remove(path):
    try:
        os.remove(path)
    except OSError:
        pass

class RemoteTrain(Plugin):
    __author__ = "kestrel"
    __version__ = "0.1.0"
    __license__ = "GPL3"
    __description__ = "Train the learning agent on a remote desktop host (opt-in)."

    __target_api__ = 2
    __capabilities__ = ("capture-read", "network", "ui")
    # One row button on the Plugins page: "Train" requests a reconcile/launch
    # now (on_webhook, a POST). Shown only once the plugin is configured and
    # passwordless SSH is set up - see webhook_actions.
    __actions__ = ({"label": "Train", "path": "train", "method": "post"},)
    __options__ = {
        # Where to train. Supplied by the operator; the plugin stays idle until
        # host/user/path are set. Separate from the backup host.
        "train_host": {"type": "str", "default": ""},
        "train_user": {"type": "str", "default": ""},
        "train_path": {"type": "str", "default": ""},
        "train_port": {"type": "int", "default": 22},
        # Reuse the device key by default (authorized on the host by `setup`).
        # Literal default (not _DEFAULT_KEY): plugins-ctl reads __options__ with
        # ast.literal_eval and never imports plugin code.
        "ssh_key": {"type": "str", "default": "/home/pi/.ssh/id_ed25519"},
        # Which per-algorithm models to keep trained remotely: a comma-separated
        # subset of the agent's algorithms. Defaults to all four; empty trains
        # nothing. A string (not a TOML array) so it is settable via
        # `plugins ... --set` and the web settings page like every scalar option.
        "algorithms": {
            "type": "str", "default": "RecurrentPPO,PPO,DQN,QR-DQN",
            "help": "Comma-separated, case-sensitive. Any of: "
                    "RecurrentPPO, PPO, DQN, QR-DQN. Empty trains nothing.",
        },
        # Data-driven trigger: launch a run for an algorithm only once this many
        # new episodes have accrued since its model's watermark.
        "remote_train_min_new_episodes": {"type": "int", "default": 300},
        # A running lock older than this many hours is treated as a crashed run.
        "stale_lock_hours": {"type": "int", "default": 6},
        # Training timesteps per remote run (a desktop can afford far more than
        # the device's opportunistic fine-tune).
        "timesteps": {"type": "int", "default": 200000},
        # How often (at most) to do a full reconcile (ensure the host stack and
        # launch new runs when enough data has accrued) when nothing triggers it
        # sooner.
        "interval_seconds": {"type": "int", "default": 3600},
        # How often to do a light status poll between full reconciles: one cheap
        # SSH to refresh the card's run state (and pull a just-finished model) so
        # it isn't stale for a whole interval. Kept short; the heavy work still
        # runs on interval_seconds.
        "status_poll_seconds": {"type": "int", "default": 120},
        # Per SSH/rsync operation (not the training run, which is detached).
        "timeout": {"type": "int", "default": 1800},
        # The on-device agent runtime dir (episodes + per-algorithm models).
        "agent_dir": {"type": "str", "default": "/var/lib/kestrel/agent"},
    }

    # -- lifecycle -------------------------------------------------------
    def on_loaded(self):
        self.log = self.context.logger
        self._lock = threading.Lock()         # guards the readout state
        self._run_lock = threading.Lock()     # one reconcile at a time
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._train_now = threading.Event()   # a manual "Train now" request
        self._last_run = 0.0                  # monotonic throttle marker
        self._status = "idle"                 # one-line readout summary
        self._last_pull = None                # (algo, epoch, secs) last promote
        self._last_pull_secs = None           # train time of the last pulled model
        self._stack_err = ""                  # last host-prep failure reason
        self._stack_state = "checking"        # cached host-stack state for the card
        self._running_prev = set()            # slugs seen running at the last poll
        self._config_path = DEFAULT_CONFIG_PATH
        self._logged = set()                  # one-time notes already logged
        self._set_health()
        self._worker = threading.Thread(
            target=self._run, name="remote-train-worker", daemon=True)
        self._worker.start()

    def on_unload(self):
        self._stop.set()
        self._wake.set()

    # -- the "Train" row button -----------------------------------------
    def webhook_actions(self):
        """Offer the Train button only once a manual run could actually do
        something: the training host is configured and passwordless SSH is set
        up (``remote-train setup`` has run). Hidden until then, so it is never
        offered before it can work."""
        if self._missing_config() or not self._key_ready():
            return ()
        return self.__actions__

    def on_webhook(self, path, request):
        """The ``/plugins/remote-train/train`` action (the "Train" row button):
        request a reconcile/launch now, bypassing the data-driven threshold. It
        drops the same trigger the CLI's ``remote-train now`` does, via the root
        apply helper (no confirm - requesting training is additive), then returns
        to the Plugins page with a banner. The run's state shows on the
        remote-train dashboard card."""
        if path != "train":
            return self.context.redirect("No such action.", error=True)
        # Re-check the prerequisite here (the button could be stale): the key
        # check must run in the daemon, which can see /home, not in the
        # ProtectHome'd root helper.
        if self._missing_config() or not self._key_ready():
            return self.context.redirect(
                "passwordless SSH is not set up - run "
                "'sudo kestrel remote-train setup' on the device first",
                error=True)
        reply = self.context.apply({"target": "remote-train",
                                     "action": "train-now"})
        if reply.get("ok"):
            return self.context.redirect("Remote training requested.")
        return self.context.redirect(reply.get("error") or "not applied",
                                     error=True)

    def on_internet_available(self):
        self._wake.set()

    def on_epoch(self, epoch, info):
        self._set_health()
        # `kestrel remote-train now` drops a trigger file (same idea as the
        # agent's train-request); pick it up and wake the worker to reconcile at
        # once rather than waiting for the interval.
        trigger = os.path.join(self._agent_dir(), ".remote-train-now")
        if os.path.exists(trigger):
            try:
                os.remove(trigger)
            except OSError:
                pass
            # A manual request (Train button / `remote-train now`) forces a run
            # past the new-episode threshold; a plain wake (interval, reconnect)
            # does not. Flag it so _reconcile can tell them apart.
            self._train_now.set()
            self._wake.set()

    # -- worker ----------------------------------------------------------
    def _run(self):
        # First reconcile at load (forced), then wake on a trigger or the short
        # status-poll timeout. A full reconcile still runs at most every
        # interval_seconds; the shorter wake just keeps the card's run state
        # current (and pulls a just-finished model) in between.
        self._maybe_reconcile(force=True)
        while not self._stop.is_set():
            woke = self._wake.wait(timeout=self._poll())
            self._wake.clear()
            if self._stop.is_set():
                return
            self._maybe_reconcile(force=woke)

    def _maybe_reconcile(self, force=False):
        if self._missing_config() or not self._key_ready():
            if self._train_now.is_set():
                self._train_now.clear()
                self._set_status(
                    "can't train: set a host and run 'remote-train setup'")
            self._set_health()
            return
        if not self.context.connectivity.online:
            if self._train_now.is_set():
                self._train_now.clear()
                self._set_status("can't train now: the device is offline")
            return
        algorithms = self._algorithms()
        if not algorithms:
            self._train_now.clear()
            self._set_status("idle: no algorithms selected")
            return
        if not self._run_lock.acquire(blocking=False):
            return
        try:
            now = time.monotonic()
            due = (force or not self._last_run
                   or now - self._last_run >= self._interval())
            if due:
                self._last_run = now
                self._reconcile(algorithms)
            else:
                # Light poll between full reconciles: keep the card honest.
                self._refresh(algorithms)
        finally:
            self._run_lock.release()

    def _reconcile(self, algorithms):
        """One pass: make sure the host stack is present, then per algorithm pull
        a fresher model and launch a new run when there is enough new data.

        A manual request (the Train button / ``remote-train now``) forces a run
        even below the new-episode threshold, as long as there is some episode
        data and the algorithm is not already running."""
        manual = self._train_now.is_set()
        self._train_now.clear()
        if not self.context.require_capability("network"):
            self._set_status("error: network capability not declared")
            return
        if not self._stack_ready():
            # A previous prep may have failed (commonly: the host is missing
            # python3-venv) - surface the reason instead of a silent "not
            # prepared yet", then (re)launch: the prep clears .stack-error on
            # start and self-heals once the host is fixed.
            err = self._stack_error()
            self._ensure_stack()
            self._stack_err = err
            if err:
                self._stack_state = "failed"
                self._set_status("host prep failed: %s" % err)
            else:
                self._stack_state = "installing"
                self._set_status("preparing the host (installing the ML stack)")
            self._set_health()
            return
        # Stack ready: clear any stale host-prep failure note.
        self._stack_state = "ready"
        self._stack_err = ""
        self._set_health()
        # A manual run trains on whatever experience exists; only block it when
        # there are no episodes at all (nothing to learn from).
        has_data = bool(self._episode_rows()) if manual else True
        running = []
        launched = []
        attempted = []
        for algorithm in algorithms:
            slug = model_slug(algorithm)
            # 1) Pull a finished model and stage it for the agent to evaluate.
            staged = self._pull_and_stage(algorithm, slug)
            if staged:
                with self._lock:
                    self._last_pull = (algorithm, time.time(),
                                       self._last_pull_secs)
            # 2) Don't double-launch; relaunch only past the stale-lock window.
            if self._algo_running(slug):
                running.append(algorithm)
                continue
            # 3) Launch when enough new experience has accrued, or always on a
            #    manual request - as long as there is some data to train on.
            new_rows = self._new_episodes(algorithm)
            if manual or new_rows >= self._min_new_episodes():
                if manual and not has_data:
                    continue
                attempted.append(algorithm)
                if self._launch_training(algorithm, slug):
                    self.log.info("remote-train: launched %s on the host (%s)",
                                  algorithm, "manual request" if manual else
                                  "%d new rows since the last remote model" % new_rows)
                    running.append(algorithm)
                    launched.append(algorithm)
        self._running_prev = {model_slug(a) for a in running}
        self._summarize(running, launched, manual=manual,
                        has_data=has_data, attempted=attempted)

    def _refresh(self, algorithms):
        """A light poll between full reconciles (one cheap SSH, no stack prep or
        launch): refresh the card's run state so a finished run isn't shown as
        still training for a whole interval, and pull a model for a run that just
        finished (so its result + training time land promptly)."""
        if not self.context.require_capability("network"):
            return
        if self._stack_state != "ready":
            return                      # a full reconcile owns stack prep
        live = self._job_state()        # slugs with a live run lock, one SSH
        just_done = self._running_prev - live
        self._running_prev = set(live)
        for algorithm in algorithms:
            slug = model_slug(algorithm)
            if slug in just_done and self._pull_and_stage(algorithm, slug):
                with self._lock:
                    self._last_pull = (algorithm, time.time(),
                                       self._last_pull_secs)
        self._summarize([a for a in algorithms if model_slug(a) in live], [])

    def _job_state(self):
        """One SSH: the set of algorithm slugs with a live (non-stale) run lock
        on the host - cheap enough to poll between full reconciles."""
        ok, out = self._remote(
            "cd %s 2>/dev/null || exit 0; for f in jobs/*.lock; do "
            "[ -e \"$f\" ] || continue; "
            "echo \"$(basename \"$f\" .lock) $(cat \"$f\" 2>/dev/null)\"; done"
            % self._q_path(), timeout=60)
        live = set()
        if not ok:
            return live
        cutoff = self._stale_lock_hours() * 3600.0
        now = time.time()
        for line in out.splitlines():
            parts = line.split()
            if len(parts) < 2:
                continue
            try:
                started = float(parts[1])
            except ValueError:
                continue
            if started and (now - started) < cutoff:
                live.add(parts[0])
        return live

    # -- device-side decisions (pure, unit-tested) -----------------------
    def _episode_rows(self):
        return metrics_mod.load_rows(
            os.path.join(self._agent_dir(), EPISODES_DIR))

    def _staging_root(self):
        return os.path.join(self._agent_dir(), REMOTE_STAGING_DIR)

    def _read_json(self, path, default):
        """The JSON in ``path`` when it has ``default``'s type (a dict or a
        list), else ``default``."""
        try:
            with open(path, "r", encoding="utf-8") as handle:
                data = json.load(handle)
        except (OSError, ValueError):
            return default
        return data if isinstance(data, type(default)) else default

    def _host_models(self):
        """The host's latest model per algorithm slug, as last seen on a pull:
        ``{slug: {"fingerprint", "latest_ts", "train_seconds", "seen_at"}}``."""
        return self._read_json(os.path.join(self._staging_root(), HOST_MODELS_NAME), {})

    def _record_host_model(self, slug, meta):
        record = self._host_models()
        latest, _ = metrics_mod.meta_watermark(meta)
        record[slug] = {"fingerprint": metrics_mod.model_fingerprint(meta),
                        "latest_ts": latest,
                        "train_seconds": meta.get("train_seconds"),
                        "seen_at": time.time()}
        path = os.path.join(self._staging_root(), HOST_MODELS_NAME)
        try:
            os.makedirs(self._staging_root(), exist_ok=True)
            with open(path + ".tmp", "w", encoding="utf-8") as handle:
                json.dump(record, handle)
            os.replace(path + ".tmp", path)
        except OSError as exc:
            self.log.warning("remote-train: could not record the host model: %s", exc)

    def _judged(self):
        """Fingerprints of remote models the agent has already judged."""
        return set(f for f in self._read_json(
            os.path.join(self._staging_root(), EVALUATED_NAME), []) if isinstance(f, str))

    def _new_episodes(self, algorithm):
        """New episode rows since the host's last model of ``algorithm`` (its
        data watermark) - the launch trigger's measure. With no remote model yet,
        every row counts. The device's own training doesn't reset it."""
        latest = (self._host_models().get(model_slug(algorithm)) or {}).get("latest_ts", 0)
        return metrics_mod.new_rows_since(self._episode_rows(), latest)

    def _should_stage(self, remote_meta):
        """Whether a pulled remote model should be staged for the agent's
        contest: its env-spec matches this device and the agent hasn't already
        judged it. Its data age doesn't matter here - the contest scores every
        candidate on held-out rows and applies one rule to all of them."""
        try:
            from kestrel_pi.agent import spec
            remote_spec = remote_meta.get("spec_version")
            if remote_spec is not None and remote_spec != spec.SPEC_VERSION:
                return False
        except Exception:  # noqa: BLE001 - spec import must not break a pull
            pass
        return metrics_mod.model_fingerprint(remote_meta) not in self._judged()

    def _note_once(self, key, message, *args):
        if key not in self._logged:
            self._logged.add(key)
            self.log.info(message, *args)

    # -- staging (the agent owns promotion) ------------------------------
    def _pull_and_stage(self, algorithm, slug):
        """Pull the host's finished model for ``algorithm`` into the agent's
        staging dir and leave it for the agent's contest. This plugin never loads
        a model (the daemon stays torch-free). The sidecar comes first: an
        unchanged model (already staged) or one the agent already judged is not
        pulled again, so the staged file's mtime - the agent's trigger - only
        changes for a new model. Returns True when a new candidate was staged."""
        staging = os.path.join(self._staging_root(), slug)
        model_name = model_name_for(algorithm)
        meta_name = model_meta_name_for(algorithm)
        remote_agent = self._remote_agent_dir()
        try:
            os.makedirs(staging, exist_ok=True)
        except OSError as exc:
            self.log.warning("remote-train: cannot stage: %s", exc)
            return False
        incoming_meta = os.path.join(staging, ".incoming-" + meta_name)
        self._rsync_down("%s/%s" % (remote_agent, meta_name), incoming_meta)
        remote_meta = self._read_json(incoming_meta, {})
        _remove(incoming_meta)
        if not remote_meta:
            return False             # no finished model on the host yet
        self._record_host_model(slug, remote_meta)
        fingerprint = metrics_mod.model_fingerprint(remote_meta)
        staged_meta = self._read_json(os.path.join(staging, meta_name), {})
        if staged_meta and metrics_mod.model_fingerprint(staged_meta) == fingerprint:
            return False             # already staged, waiting for the contest
        if not self._should_stage(remote_meta):
            self._note_once(("skip", fingerprint),
                            "remote-train: not staging the host's %s model (%s)",
                            algorithm, "already judged" if fingerprint in self._judged()
                            else "env-spec mismatch")
            return False
        incoming_model = os.path.join(staging, ".incoming-" + model_name)
        self._rsync_down("%s/%s" % (remote_agent, model_name), incoming_model)
        if not os.path.isfile(incoming_model):
            return False
        try:
            # Model first, then sidecar, so the agent never sees a model without
            # its matching meta.
            os.replace(incoming_model, os.path.join(staging, model_name))
            with open(os.path.join(staging, meta_name) + ".tmp", "w",
                      encoding="utf-8") as handle:
                json.dump(remote_meta, handle)
            os.replace(os.path.join(staging, meta_name) + ".tmp",
                       os.path.join(staging, meta_name))
        except OSError as exc:
            self.log.warning("remote-train: cannot stage: %s", exc)
            _remove(incoming_model)
            return False
        # Record the host's training time (from the sidecar) for the card's "Last
        # model pulled". The agent stamps source="remote" if it actually promotes.
        secs = remote_meta.get("train_seconds")
        self._last_pull_secs = secs if isinstance(secs, (int, float)) else None
        latest, _ = metrics_mod.meta_watermark(remote_meta)
        self.log.info("remote-train: staged a %s model from the host for the "
                      "agent's contest (data to %s, trained %s s)", algorithm,
                      time.strftime("%Y-%m-%d %H:%MZ", time.gmtime(latest))
                      if latest else "unknown", secs)
        return True

    # -- host operations (SSH/rsync; isolated so tests can stub them) -----
    def _ssh_opts(self):
        # Keepalives so a quiet/flaky tether is detected within ~a minute and an
        # idle control link isn't dropped by the phone's power-save.
        return ["-i", self._key(), "-p", str(self._port()),
                "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes",
                "-o", "ConnectTimeout=20",
                "-o", "ServerAliveInterval=15", "-o", "ServerAliveCountMax=4"]

    def _ssh_e(self):
        return ("ssh -i %s -p %d -o BatchMode=yes "
                "-o StrictHostKeyChecking=yes -o ConnectTimeout=20 "
                "-o ServerAliveInterval=15 -o ServerAliveCountMax=4"
                % (shlex.quote(self._key()), self._port()))

    def _remote(self, script, timeout=None):
        """Run ``script`` on the host via ``ssh ... sh -s`` (script on stdin).
        Returns ``(ok, output)``. Used for quick, synchronous host commands."""
        cmd = (["ssh"] + self._ssh_opts()
               + ["%s@%s" % (self._user(), self._host()), "sh", "-s"])
        try:
            proc = subprocess.run(cmd, input=script, capture_output=True,
                                  text=True, timeout=timeout or self._timeout(),
                                  check=False)
        except (OSError, subprocess.SubprocessError) as exc:
            return (False, str(exc))
        out = (proc.stdout or "") + (proc.stderr or "")
        return (proc.returncode == 0, out.strip())

    def _rsync_up(self, local_dir, remote_dir):
        cmd = ["rsync", "-az", "--partial", "--mkpath",
               "-e", self._ssh_e(), local_dir.rstrip("/") + "/",
               "%s@%s:%s" % (self._user(), self._host(), remote_dir)]
        return self._run_rsync(cmd)

    def _rsync_down(self, remote_path, local_path):
        cmd = ["rsync", "-az", "--partial",
               "-e", self._ssh_e(),
               "%s@%s:%s" % (self._user(), self._host(), remote_path),
               local_path]
        return self._run_rsync(cmd)

    def _run_rsync(self, cmd):
        # A flaky tether drops a transfer mid-stream; --partial keeps the
        # progress, so retry a few times before reporting failure.
        last_err = ""
        for attempt in range(_RSYNC_TRIES):
            try:
                proc = subprocess.run(cmd, capture_output=True, text=True,
                                      timeout=self._timeout(), check=False)
            except FileNotFoundError:
                return (False, "rsync not installed")
            except (OSError, subprocess.SubprocessError) as exc:
                last_err = str(exc)
            else:
                if proc.returncode == 0:
                    return (True, (proc.stderr or proc.stdout or "").strip())
                last_err = (proc.stderr or proc.stdout or "").strip()
            if attempt + 1 < _RSYNC_TRIES:
                time.sleep(_RSYNC_BACKOFF * (attempt + 1))
        return (False, last_err)

    # -- host stack (package + ML deps in an isolated venv) --------------
    def _stack_ready(self):
        ok, _out = self._remote(
            "test -x %s/bin/python && test -f %s/%s"
            % (self._q_venv(), self._q_path(), _STACK_READY),
            timeout=60)
        return ok

    def _stack_error(self):
        """The host prep's recorded failure reason (``.stack-error``), or "".
        The prep script writes it when the venv or the ML-stack install fails,
        so a silent "not prepared yet" becomes a clear error on the device."""
        ok, out = self._remote(
            "head -c 200 %s/%s 2>/dev/null" % (self._q_path(), _STACK_ERROR),
            timeout=60)
        return out.strip() if ok else ""

    def _ensure_stack(self):
        """Create the venv, sync the device's exact package into it, and install
        the ML stack - detached on the host, so a flaky link doesn't interrupt a
        big download. Idempotent: a `.stack-installing` lock prevents stacking
        installs; `.stack-ready` is written only on success."""
        # Push the device's installed package to a staging dir on the host, so
        # the host trains the exact same version (env-spec parity).
        pkg_dir = self._local_package_dir()
        if pkg_dir:
            self._rsync_up(pkg_dir,
                           "%s/pkg/kestrel_pi" % self._remote_path())
        script = self._stack_install_script()
        ok, out = self._remote(script, timeout=120)
        if not ok:
            self.log.warning("remote-train: could not start host prep: %s", out)

    def _stack_install_script(self):
        """A detached host-prep script: build the venv, drop the device's package
        into its site-packages, and pip-install the ML stack, writing
        ``.stack-ready`` only on success. Backgrounded with setsid so the SSH
        call returns at once."""
        path = self._q_path()
        pip_pkgs = " ".join(shlex.quote(p) for p in _ML_STACK)
        # The inner worker script; run detached. Single-quoted heredoc, so no
        # shell expansion inside - every value is interpolated by Python here.
        worker = (
            "set -u\n"
            # Runs with CWD already at <path> (the launcher cd'd here before
            # backgrounding this), so no cd - a relative one would fail.
            "rm -f %(error)s\n"
            # Rebuild from a clean venv: a prior attempt may have left a broken
            # half-made one (e.g. an interrupted or failed create).
            "rm -rf venv\n"
            "if ! python3 -m venv venv && ! python -m venv venv; then\n"
            "  echo 'could not create a Python venv on the host - install "
            "python3-venv (e.g. sudo apt install python3-venv)' > %(error)s\n"
            "  rm -f %(installing)s\n"
            "  exit 0\n"
            "fi\n"
            "SITE=$(venv/bin/python -c "
            "'import sysconfig;print(sysconfig.get_paths()[\"purelib\"])')\n"
            "rm -rf \"$SITE/kestrel_pi\"\n"
            "cp -r pkg/kestrel_pi \"$SITE/kestrel_pi\"\n"
            "if venv/bin/pip install --upgrade pip >/dev/null 2>&1 && "
            "venv/bin/pip install %(pip)s; then\n"
            "  touch %(ready)s\n"
            "  rm -f %(error)s\n"
            "else\n"
            "  echo 'the ML stack pip install failed on the host "
            "(see jobs/prep.log)' > %(error)s\n"
            "fi\n"
            "rm -f %(installing)s\n"
        ) % {"pip": pip_pkgs, "ready": _STACK_READY,
             "installing": _STACK_INSTALLING, "error": _STACK_ERROR}
        # The outer launcher: make dirs, don't double-install, background worker.
        return (
            "set -eu\n"
            "cd %(path)s\n"
            "mkdir -p jobs agent/episodes pkg\n"
            "if [ -f %(installing)s ]; then echo already-installing; exit 0; fi\n"
            "date +%%s > %(installing)s\n"
            "cat > prep.sh <<'PREP'\n"
            "%(worker)s"
            "PREP\n"
            "setsid sh prep.sh >jobs/prep.log 2>&1 </dev/null &\n"
            "echo prep-launched\n"
        ) % {"path": path, "installing": _STACK_INSTALLING, "worker": worker}

    # -- host training jobs ----------------------------------------------
    def _algo_running(self, slug):
        """True if a run for ``slug`` is in flight and its lock is not stale."""
        ok, out = self._remote(
            "f=%s/jobs/%s.lock; [ -f \"$f\" ] && cat \"$f\" || echo ''"
            % (self._q_path(), slug), timeout=60)
        if not ok:
            return False
        try:
            started = float((out or "0").strip() or 0)
        except ValueError:
            return False
        if not started:
            return False
        age_h = (time.time() - started) / 3600.0
        return age_h < self._stale_lock_hours()

    def _launch_training(self, algorithm, slug):
        """Upload the latest episodes and the device's package, then launch a
        detached training run for ``algorithm`` on the host. The package goes up
        before every launch, so the host always runs the device's current
        trainer, even after an in-place update. Returns True if the launch was
        accepted."""
        pkg_dir = self._local_package_dir()
        if pkg_dir:
            ok, out = self._rsync_up(pkg_dir, "%s/pkg/kestrel_pi" % self._remote_path())
            if not ok:
                self.log.warning("remote-train: package upload failed: %s", out)
                return False
        up_ok, out = self._rsync_up(
            os.path.join(self._agent_dir(), EPISODES_DIR),
            "%s/agent/%s" % (self._remote_path(), EPISODES_DIR))
        if not up_ok:
            self.log.warning("remote-train: episode upload failed: %s", out)
            return False
        ok, out = self._remote(self._train_launch_script(algorithm, slug),
                               timeout=120)
        if not ok:
            self.log.warning("remote-train: launch failed for %s: %s",
                             algorithm, out)
        return ok

    def _train_launch_script(self, algorithm, slug):
        """A detached training launcher for one algorithm. Writes the lock
        (start timestamp), runs the SAME trainer the device uses - the package
        just synced to ``pkg/``, ahead of the venv's copy on ``PYTHONPATH`` -
        records a result and clears the lock on exit."""
        path = self._q_path()
        worker = (
            "set -u\n"
            # Runs with CWD already at <path> (the launcher cd'd here before
            # backgrounding this), so no cd - a relative one would fail.
            "date +%%s > jobs/%(slug)s.lock\n"
            "if PYTHONPATH=pkg venv/bin/python -m kestrel_pi.agent.train "
            "--agent-dir agent --algorithm %(algo)s --timesteps %(ts)d; then\n"
            "  echo ok > jobs/%(slug)s.result\n"
            "else\n"
            "  echo error > jobs/%(slug)s.result\n"
            "fi\n"
            "rm -f jobs/%(slug)s.lock\n"
        ) % {"slug": slug, "algo": shlex.quote(algorithm),
             "ts": self._timesteps()}
        return (
            "set -eu\n"
            "cd %(path)s\n"
            "mkdir -p jobs agent/episodes\n"
            "if [ -f jobs/%(slug)s.lock ]; then echo already-running; exit 0; fi\n"
            "cat > jobs/run-%(slug)s.sh <<'RUN'\n"
            "%(worker)s"
            "RUN\n"
            "setsid sh jobs/run-%(slug)s.sh >jobs/%(slug)s.log 2>&1 </dev/null &\n"
            "echo launched\n"
        ) % {"path": path, "slug": slug, "worker": worker}

    # -- readout ---------------------------------------------------------
    def on_ui_setup(self, ui):
        ui.add_element("target", "-", label="Train host")
        ui.add_element("stack", "-", label="Host stack")
        ui.add_element("training", "idle", label="Training")
        ui.add_element("pulled", "never", label="Last model pulled")

    def on_ui_update(self, ui, state):
        with self._lock:
            status = self._status
            last_pull = self._last_pull
        ui.set("target", self._target() or "-")
        ui.set("stack", self._stack_label())
        ui.set("training", status)
        if last_pull:
            text = "%s %s" % (last_pull[0], _ago(last_pull[1]))
            secs = last_pull[2] if len(last_pull) > 2 else None
            if secs:
                text += " (trained in %s)" % _duration(secs)
            ui.set("pulled", text)
        else:
            ui.set("pulled", "never")

    def _stack_label(self):
        # The host-stack state the worker cached on its last reconcile (ready /
        # installing / failed / checking). Cached so the UI tick never blocks on
        # an SSH probe; the live run detail is on the Training line.
        if self._missing_config() or not self._key_ready():
            return "-"
        return self._stack_state

    def _summarize(self, running, launched, manual=False,
                   has_data=True, attempted=None):
        if launched:
            self._set_status("training %s" % ", ".join(launched))
        elif running:
            self._set_status("running %s" % ", ".join(running))
        elif manual and not has_data:
            # A manual Train with no episodes yet: say so instead of "idle",
            # which looked like the button did nothing.
            self._set_status("nothing to train on yet: no capture episodes recorded")
        elif manual and attempted:
            # Manual, there was data, but every launch was refused (host busy /
            # a launch error) - point at the log rather than claim success.
            self._set_status("couldn't start a run - see the log")
        elif manual:
            self._set_status("models are already current")
        else:
            self._set_status("idle (models current)")

    # -- helpers ---------------------------------------------------------
    def _configured_algorithm(self):
        """The algorithm the agent runs (``[agent] algorithm``), read live so a
        switch takes effect without a restart; None when unreadable."""
        try:
            agent = (load_config(self._config_path) or {}).get("agent") or {}
        except Exception:  # noqa: BLE001 - an unreadable config trains the list only
            return None
        name = agent.get("algorithm") or policies_mod.DEFAULT_ALGORITHM
        return name if policies_mod.valid_algorithm(name) else None

    def _algorithms(self):
        """The algorithms to train on the host: the one the agent runs first
        (always, even if ``algorithms`` leaves it out), then the rest of the
        ``algorithms`` option, deduplicated."""
        out = []
        configured = self._configured_algorithm()
        if configured:
            out.append(configured)
        raw = self.options.get("algorithms") or ""
        # Normally a comma/space-separated string; tolerate a list defensively
        # (the config validator already enforces the declared str type).
        items = re.split(r"[,\s]+", raw.strip()) if isinstance(raw, str) else list(raw)
        for item in items:
            name = str(item).strip()
            if policies_mod.valid_algorithm(name) and name not in out:
                out.append(name)
        return out

    def _missing_config(self):
        return [k for k in ("train_host", "train_user", "train_path")
                if not str(self.options.get(k) or "").strip()]

    def _key_ready(self):
        return os.path.exists(self._key())

    def _key(self):
        return self.options.get("ssh_key") or _DEFAULT_KEY

    def _host(self):
        return self.options.get("train_host") or ""

    def _user(self):
        return self.options.get("train_user") or ""

    def _remote_path(self):
        return (self.options.get("train_path") or "").rstrip("/") or "."

    def _remote_agent_dir(self):
        return self._remote_path() + "/agent"

    def _q_path(self):
        return shlex.quote(self._remote_path())

    def _q_venv(self):
        return shlex.quote(self._remote_path() + "/venv")

    def _agent_dir(self):
        return self.options.get("agent_dir") or "/var/lib/kestrel/agent"

    def _local_package_dir(self):
        """The device's installed ``kestrel_pi`` package directory, synced to
        the host so it trains the exact same version."""
        try:
            import kestrel_pi
            return os.path.dirname(os.path.abspath(kestrel_pi.__file__))
        except Exception:  # noqa: BLE001
            return ""

    def _target(self):
        if self._missing_config():
            return ""
        return "%s@%s:%s" % (self._user(), self._host(),
                             self.options.get("train_path"))

    def _port(self):
        try:
            return int(self.options.get("train_port", 22))
        except (TypeError, ValueError):
            return 22

    def _min_new_episodes(self):
        try:
            return max(1, int(self.options.get("remote_train_min_new_episodes", 300)))
        except (TypeError, ValueError):
            return 300

    def _stale_lock_hours(self):
        try:
            return max(1, int(self.options.get("stale_lock_hours", 6)))
        except (TypeError, ValueError):
            return 6

    def _timesteps(self):
        try:
            return max(100, int(self.options.get("timesteps", 200000)))
        except (TypeError, ValueError):
            return 200000

    def _interval(self):
        try:
            return max(60, int(self.options.get("interval_seconds", 3600)))
        except (TypeError, ValueError):
            return 3600

    def _poll(self):
        try:
            return max(30, int(self.options.get("status_poll_seconds", 120)))
        except (TypeError, ValueError):
            return 120

    def _timeout(self):
        try:
            return max(30, int(self.options.get("timeout", 1800)))
        except (TypeError, ValueError):
            return 1800

    def _set_status(self, status):
        with self._lock:
            self._status = status

    def _set_health(self):
        missing = self._missing_config()
        if missing:
            self.context.health_note = (
                "not configured: set %s (plugins enable remote-train --set ...)"
                % ", ".join(missing))
        elif not self._key_ready():
            self.context.health_note = (
                "passwordless SSH not set up - run 'sudo kestrel "
                "remote-train setup' to install the device key on %s"
                % self._host())
        elif not self._algorithms():
            self.context.health_note = (
                "no algorithms selected to train (set algorithms=...)")
        elif self._stack_err:
            self.context.health_note = (
                "host training stack not ready: %s" % self._stack_err)
        else:
            self.context.health_note = ""


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


def _duration(seconds):
    """A short human duration, e.g. "45s", "6m 45s", "1h 3m"."""
    try:
        total = int(round(float(seconds)))
    except (TypeError, ValueError):
        return ""
    if total < 0:
        total = 0
    if total < 60:
        return "%ds" % total
    if total < 3600:
        return "%dm %ds" % (total // 60, total % 60)
    return "%dh %dm" % (total // 3600, (total % 3600) // 60)
