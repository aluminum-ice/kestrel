# Remote training

**Status: implemented.** The `remote-train` reference plugin offloads the
learning agent's training to a more powerful desktop Linux machine, so a Pi 5
(or any board) doesn't have to do the heavy compute itself. It ships **disabled**
(`[plugins.remote-train]` in `config.toml`); see [Using it](#using-it) to turn it
on. This note is both the design rationale and the operator reference.

The host runs the device's **installed package copied verbatim**, not a built
wheel — see
[What the training host needs](#what-the-training-host-needs-just-the-package).

Read alongside [the learning-agent guide](../user-guide/agent-setup.md) and
[the agent interface](agent-interface.md); this reuses the backup plugin's
SSH plumbing pattern ([backup setup](../user-guide/backup-setup.md)).

## Why

On-device training works on a Pi 5 but is gated hard by RAM and heat, and never
runs at all on a small board (a Zero 2 W). A desktop has far more CPU/RAM (and
maybe a GPU), trains in a fraction of the time, and never competes with capture
for the device's resources. The manual round trip already exists — copy the
episode logs off, run `kestrel-agent-train` on the desktop, fly the model
back — and the guide documents it. This plugin **automates that loop** and makes
it survive the realities of a field device: a flaky link, several models to keep
current, and a training host that is not the backup host.

Because the host runs the exact same trainer the device would, it produces the
same **state-conditioned** model (see [Training](../user-guide/agent-setup.md#training-on-device-and-off)) —
the extra compute just lets it train on far more timesteps. Nothing in this
plugin changed for that: the trainer upgrade lives entirely in
`kestrel_pi.agent.train`, which both paths invoke.

## What already makes this cheap

Three pieces are already in place, so this plugin is mostly orchestration:

- **The data is already sufficient and already backed up.** Each episode row the
  agent logs carries the full observation vector, the action, and the reward —
  exactly what the trainer replays. The `backup` plugin already mirrors the whole
  agent directory (including `episodes/*.jsonl`), so nothing new has to be
  captured; remote training just needs those logs on the training host.
- **Per-algorithm models already land cleanly.** The agent keeps a separate
  `model-<algorithm>.zip` per algorithm and **adopts a promoted one live** (its
  decide loop watches both the active model file and the staging dir mtime), so a
  model pulled back from the desktop takes effect within a decision window once
  the agent promotes it, no restart.
- **The SSH pattern already exists.** The backup plugin copies as the device's
  own `pi` SSH key over key-based, passwordless SSH, with a one-time terminal
  `setup` that runs `ssh-copy-id`. This plugin follows the same model.

## What the training host needs (just the package)

**Not the whole repo.** The trainer only needs the `kestrel_pi` Python
package — specifically the `agent/` code (`train.py`, `policies.py`, `spec.py`,
`metrics.py`) — plus the ML stack from pip (`numpy`, `gymnasium`,
`stable-baselines3`, `sb3-contrib`, `torch`). None of the image build, Ansible,
shell tooling, or systemd units are involved in training.

**One hard constraint: version match.** A trained model records the env-spec
version it was built under (`spec_version` in its sidecar), and the device
**refuses a model whose spec doesn't match** its own, falling back to the
baseline. So the training host must run the *same* package version the device
runs, or the model it produces won't load back.

**How it ships: the installed package, copied verbatim — not a wheel.** The
running image has no build toolchain and no package *source* tree (the
`kestrel_pi` sources are deleted after the venv install), so there is nothing
to `pip wheel` from at runtime and the feature stays app-layer (no reflash).
Instead the plugin **rsyncs the device's already-installed
`kestrel_pi` package directory** (`os.path.dirname(kestrel_pi.__file__)`) to
the host: into the venv's `site-packages` at setup, and to `pkg/` before every
run, which the run imports first. This is *stronger* than a wheel for
the one thing that matters — the host then runs the device's **exact bytes**, so
`spec_version` parity is guaranteed by construction — and it needs no compiler or
package index for our code (only for the ML dependencies, and only the first time
they're installed). The update engine already carries that installed package onto
a device, so an app-layer update keeps the device and host in lockstep.

**Everything on the host is local and isolated.** Nothing the plugin installs
touches the host's system Python or anything outside its own directory. Both the
copied package **and** the ML stack (`torch`, `stable-baselines3`, `sb3-contrib`,
`gymnasium`, `numpy`) go into a **dedicated virtualenv the plugin owns**, under
`train_path` on the host (e.g. `<train_path>/venv`), and the trainer is invoked
as that venv's Python. So the training host can be a machine the operator already
uses for other work: we add one self-contained directory with its own venv and
never modify the system interpreter, its site-packages, or anything outside
`train_path`. Uninstalling remote training is deleting that directory.

## Preparing the training host

The host is any always-on Linux machine the device can reach over SSH. Because
the plugin builds *its own* virtualenv there, the host only needs a handful of
OS packages — the venv + pip modules for Python 3, `rsync` for the transfers,
and an SSH server — and a few GB of free disk for the PyTorch download. The
plugin and `remote-train setup` do everything else (key install, venv, package
copy, ML-stack install); nothing Kestrel-specific is installed by hand.

Debian / Ubuntu / Raspberry Pi OS:

```bash
sudo apt update
sudo apt install -y python3-venv python3-pip rsync openssh-server
```

Fedora / RHEL / Rocky (venv ships with `python3`):

```bash
sudo dnf install -y python3 python3-pip rsync openssh-server
```

Arch:

```bash
sudo pacman -S --needed python python-pip rsync openssh
sudo systemctl enable --now sshd
```

`python3-venv` is the package most often missing on a Debian/Ubuntu host that
already has Python: without it `python3 -m venv` fails with "ensurepip is not
available", the plugin cannot build its venv, and the ML stack never installs,
so training silently never starts. When the host stack will not come up,
`kestrel remote-train status` reports `host stack: FAILED - <reason>` and
the dashboard **Host stack** readout shows `failed`; the full reason is in
`<train_path>/jobs/prep.log` on the host.

A copy-paste check to confirm a host is ready before you point the device at it
(run it on the host, as the account the device will train under):

```bash
#!/usr/bin/env bash
# Verify a remote-training host has what the plugin needs.
set -eu
ok=1
for cmd in python3 rsync; do
  if command -v "$cmd" >/dev/null 2>&1; then
    echo "ok: $cmd -> $(command -v "$cmd")"
  else
    echo "MISSING: $cmd"; ok=0
  fi
done
if python3 -m venv --help >/dev/null 2>&1 && python3 -c 'import ensurepip' 2>/dev/null; then
  echo "ok: python3 venv + ensurepip"
else
  echo "MISSING: python3 venv/ensurepip (install python3-venv)"; ok=0
fi
if systemctl is-active --quiet ssh 2>/dev/null || systemctl is-active --quiet sshd 2>/dev/null; then
  echo "ok: ssh server running"
else
  echo "WARN: ssh server not detected (needed for the device to reach this host)"
fi
avail=$(df -Pm "$HOME" | awk 'NR==2 {print $4}')
echo "info: $(( avail / 1024 )) GB free in \$HOME (PyTorch needs a few GB)"
[ "$ok" -eq 1 ] && echo "HOST LOOKS READY" || { echo "HOST NOT READY — install the missing packages above"; exit 1; }
```

Once the packages are in place: on the device run `sudo kestrel
remote-train setup` (installs the key and kicks off the venv/ML-stack build on
the host), then `sudo kestrel remote-train status` until **host stack**
reads `ready`.

## Separate from backup

The training host is **not assumed to be the backup host**. The plugin gets its
own connection settings and its own one-time key install:

- `train_host`, `train_user`, `train_path`, `train_port` — where to train and
  where on that host the work lives.
- `remote-train setup` — the terminal-only step that installs the device's
  public key into the training host's `authorized_keys` (via `ssh-copy-id`), the
  same one-time password-once, store-nothing flow as `backup setup`. It can reuse
  the *same* device SSH key (no new key material), just authorized on a second
  host. It then creates the plugin's dedicated venv under `train_path`, copies the
  device's installed package into it, and kicks off the ML-stack install **into
  that venv, in the background on the host** — see the next paragraph.

**The host prep is flaky-link tolerant and verified before every run.** Installing
torch + SB3 is a large download, and the link may drop mid-install, so `setup`
doesn't block on it: it launches the install detached on the host (with its own
marker under `train_path`) and returns. A dropped link just means the install
keeps running on the desktop and finishes on its own. Then **every training run
first verifies the venv is complete** — a quick import check of the package and
the ML stack — and, if anything is missing (interrupted install, or the venv was
cleared), (re)installs before it trains rather than failing. So the operator never
has to babysit the install: run `setup`, and the first `now` that finds a ready
venv trains; one that doesn't finishes the install first.

Backup and remote training are fully independent: different host, different
config, set up separately. (If someone happens to use one machine for both, it
just means the same host answers to both configs — but the plugin still assumes
nothing about that.)

## Choosing which models to train

An `algorithms` option — a comma-separated list — selects which per-algorithm
models this device keeps trained. Set it with `--set algorithms=RecurrentPPO,DQN`
(or on the plugin's web settings page). Name RecurrentPPO + DQN (or all four) and
the plugin trains each one on the host and pulls each `model-<algorithm>.zip` back
into its own slot. This maps one-to-one onto the per-algorithm model layout, so
you can keep several trained algorithms current from one desktop.

## The job model: async, disconnect-tolerant, idempotent

This is the heart of the design, and it is built around the field reality that
the link **will** drop. Training is treated as a background job on the host, not
a blocking SSH call the device babysits.

1. **Launch detached.** The plugin opens a short SSH session only to *start* the
   run — in the background on the host (`setsid`/`nohup`, or a transient
   `systemd-run --user` unit) — and returns immediately. If the link drops a
   second later, the run keeps going on the desktop; nothing on the device is
   holding it open.
2. **State lives in markers on the host, not in a held connection.** Per
   algorithm, a run writes a small state under `train_path`:
   - a `running` lock (with a start timestamp and the package/spec version it's
     training against) while in progress;
   - on success, the finished `model-<algorithm>.zip` + its sidecar and a `done`
     marker (carrying the model's version/mtime).
3. **Reconcile every interval / on reconnect, *before* starting any new work:**
   - **Pull and stage; the agent decides.** If the host has a finished
     `model-<algorithm>.zip`, pull its sidecar first and record it
     (`.remote-staging/host-models.json`: the host's latest model per algorithm).
     A model already staged, one the agent has already judged
     (`.remote-staging/evaluated.json`) or one with a different env-spec isn't
     pulled again. A new one is pulled to a **staging path**
     (`.remote-staging/<slug>/` under the agent dir). The plugin never loads a
     model and never touches the active slot; the **agent process** runs the
     same contest for it as for a model trained on the device (see "One contest
     for every model" below, and
     [the promotion rules](agent-interface.md#candidates-and-promotion)).
   - **Don't double-launch.** If an algorithm is marked `running`, leave it —
     never start a second run for the same algorithm. A `running` lock older than
     `stale_lock_hours` (a config option, **default 6**) is treated as a crashed
     run and may be relaunched. It's a config knob precisely because a desktop run
     time isn't known up front — set it comfortably above a real run's length so a
     slow-but-live run is never relaunched under itself, and lower it once you've
     measured your host.
   - **Only train when there's enough new data — the trigger is data-driven, not
     a clock.** A fresh run for an algorithm starts only when **enough new episode
     rows have accrued since that model was last trained** — more than
     `remote_train_min_new_episodes` (a config option) — and nothing is in flight
     for it. The plugin still *checks* on its cadence (below), but the cadence only
     decides how often it looks; the **data** decides whether it launches. So a
     device sitting idle with no new experience never triggers a run, and a device
     out capturing hard triggers one as soon as it has enough to be worth the
     compute. "New since last trained" is measured against the data watermark
     of **the host's own last model** of that algorithm (from
     `host-models.json`), never the device's: the device training its own model
     doesn't reset the remote's clock. With no remote model yet, every row
     counts. The algorithm the agent runs is always trained, and first, even if
     the `algorithms` option leaves it out.
4. **Push the package and the logs up as part of a run.** Before launching, the
   plugin rsyncs the device's installed `kestrel_pi` package to `pkg/` on the
   host (the run imports it ahead of the venv's copy, `PYTHONPATH=pkg`), so the
   host always trains with the device's current code, even after an in-place
   update. It then rsyncs the agent's `episodes/` up to the host (incremental — only new rows cross the
   link), so the host trains on current experience. (If the backup host and
   training host are the same, this overlaps with backup, but it's cheap and
   keeps the two features independent.)

Net effect: a disconnect mid-training just means "pull it next time the link is
up"; a crashed run is detected by its stale lock; a model that's already current
is never retrained; and two runs for the same algorithm never stack. The device
only ever *reads* results and decides what to start — all the heavy work, and all
the waiting, happen on the host.

## One contest for every model

The point is to use the remote model *when it's genuinely better* — it trains
with far more timesteps than a resource-gated on-device run can afford — without
letting either side win by default. "Newer file mtime" is the wrong test, and so
is "trained on newer data": more compute on slightly older data can produce the
better policy, and fresher data doesn't guarantee one. So every new model,
whatever trained it, is a **candidate** that has to beat the active model in the
same **contest** (the full rules are in
[the agent interface](agent-interface.md#candidates-and-promotion)):

- **Held-out rows no model trains on.** About 20% of the logged rows, chosen by
  hour block (`metrics.is_holdout`), are kept out of every training run, on the
  device and on the host alike (the host runs the device's own trainer). They
  are what every candidate is scored on.
- **One yardstick.** The agent process (which already has torch for inference)
  scores the candidate, the active model and the scripted baseline on the newest
  of those rows, under one reward model fitted on the other rows, stepping
  through each hour block in order so a recurrent model sees real sequences.
- **One rule for both sources** (`metrics.contest_decision`): the candidate must
  beat the scripted baseline, beat the active model by a significant paired
  difference (`[agent] promotion_min_z`, default 1.0), and clear the margin
  (`[agent] promotion_margin`, default 10% of lift), and its data must not be
  more than `[agent] promotion_max_stale_episodes` rows behind (default 900).
  With no active model at all, a candidate that beats the baseline is adopted:
  that's how a board that can't train (a Zero 2 W) gets its first model.
- **On-device runs are candidates too.** They warm-start from the active model
  (a fine-tune, not a fresh model) and write to `.candidates/local/<slug>/`;
  they replace the active model only by winning. A remote model is no longer
  overwritten half an hour later by a fresh local run that never had to compete.

The plugin daemon stays torch-free throughout: it only pulls and stages. The
dashboard's agent card shows **Model contests** (how many on-device and remote
candidates won) and the **Last contest**, and
`builder/extra/files/scripts/pull-agent-diagnostics.sh` reports every contest
from the journal.

## Triggers and surface

- **CLI:** `kestrel remote-train setup` (one-time key + host prep),
  `remote-train now` (reconcile + launch due runs, non-blocking),
  `remote-train status` (what's running/done on the host, what's been staged for
  the agent to score).
- **Web:** the plugin contributes a **readout card to the dashboard** (train
  host, host-stack state, training status, last model pulled) like every other
  plugin, live whenever the plugin is enabled. **Which algorithms to train** is
  the `algorithms` field on the plugin's own settings page (`/manage/remote-train`,
  behind `[web] auth`). A **"Train" button** sits on the remote-train row of the
  plugins page (far right, neutral blue — matching the backup Restore button),
  shown only once the plugin is enabled, web auth is on, and the host +
  passwordless SSH are set up; one click requests a run now (it posts through the
  same root helper as the other controls, which shells to `remote-train now`).
- **Automatic:** the plugin reconciles on its normal cadence — on the agent's
  epoch tick and on reconnect — but, per the data-driven trigger above, that
  cadence only decides how often it *looks*; a run launches only when an algorithm
  has accrued more than `remote_train_min_new_episodes` new rows since its model's
  watermark. There is no fixed "train every N hours" timer. So a device near its
  desktop keeps its models fresh on its own, pacing itself to how much new
  experience it's actually gathering rather than to the clock.
- **Host unreachable:** when an SSH attempt can't connect or log in (ssh exits
  255: refused, timed out, key no longer authorized, host key changed), the
  card's Training line says "can't reach or log in to the host (last reached
  3h ago)", the Host stack line keeps its last known state, and the Plugins
  note names the host. No host prep, pull or launch is tried until the host
  answers again; a light poll that can't reach it keeps the run state as it
  was, so a run that finished meanwhile is still pulled afterwards.
- **Kept across restarts:** the last contact and the last model staged are
  saved in `<agent_dir>/.remote-staging/remote-status.json` (beside
  `host-models.json`, which stays one record per algorithm), so "Last model
  pulled" survives a daemon restart.

## The `remote-training` service

The plugin provides a `remote-training` service, so other plugins can read the
training host's state without reading this plugin's settings or files (see
[Inter-plugin services](plugin-development.md#inter-plugin-services)). The
value is a new dict on every call, built from what the worker last saw (it
never runs SSH):

| Key | Meaning |
| --- | --- |
| `configured` | `train_host`, `train_user` and `train_path` are set and the SSH key exists. |
| `host_state` | `"unknown"` (not tried yet), `"reachable"`, or `"unreachable"` (couldn't connect or log in), from the last SSH attempt. |
| `stack` | The host's training software: `"checking"`, `"installing"`, `"ready"` or `"failed"`. |
| `last_contact` | Epoch seconds of the last SSH session that ran, or `None`. |
| `last_model` | Epoch seconds when the last new model was staged for the agent, or `None`. |
| `running` | The algorithms with a live run on the host. |

It never carries the host name, user, path, port or key path. When
`remote-train` isn't enabled the service isn't registered, and
`services.get("remote-training")` returns `None`. Keys may be added later;
existing keys keep their meaning.

## Security posture

Same as the uploaders and backup: the plugin declares the `network` capability,
so enabling it takes the one-time authorized-use acknowledgment. It is SSH to a
host the operator controls, with the operator's own key; no new secret is stored
(the remote password is typed once into `ssh-copy-id` at setup and kept nowhere).
The host key is confirmed once, interactively, when `remote-train setup` runs
`ssh-copy-id`; every later connection requires that recorded key
(`StrictHostKeyChecking=yes`), so an unattended run never trusts a host it
hasn't seen. Pulled models are loaded as weights only (no pickled objects are
restored from the file), so a bad model can't run code. Nothing in
the model path runs as root on the device — pulled models land in the pi-owned
agent dir.

## What it does not do

- It never enables the agent or capture, and never edits operator config beyond
  its own `[plugins.remote-train]` table.
- It doesn't train on the device — if the host is unreachable, it simply does
  nothing that cycle and reports why; on-device training (where the board allows)
  is unchanged and independent.
- It doesn't delete anything on the host or the device (additive, like backup).
- It is not fleet-wide learning — it's one device offloading its *own* training
  to one host. Pooling many devices' logs into one shared model remains the
  future-work item the agent design note already calls out.

## Delivery / update type

The plugin is `plugins/remote-train.py` plus `remote-train` subcommands in the
shipped CLI (`kestrel.sh`); both are already carried by the in-place
app-layer updater (the plugins glob and the CLI), so **no new lib script and no
`LIB_SCRIPTS` change** were needed and it ships to an existing device as an
**app-layer** update. No base-image, pip, or kernel change is needed on the
*device* for the plugin itself; the ML stack it needs lives on the *host*,
installed by `remote-train setup`.

## Design rationale

Why the plugin works the way it does:

1. **Package delivery by rsync of the installed package.** The host venv gets the
   device's installed `kestrel_pi` package rsynced into it, not a built wheel —
   the running image has no build toolchain or package source. This gives strong
   spec parity (the host runs the device's exact bytes) and needs no package index
   for first-party code. See
   [What the training host needs](#what-the-training-host-needs-just-the-package).
2. **Isolation in a plugin-owned venv.** The copied package **and** the ML stack
   install into a **dedicated venv the plugin owns** under `train_path`, never the
   host's system Python. `setup` installs it **in the background** (flaky-link
   tolerant), and **every run verifies the venv** and (re)installs anything missing
   before training.
3. **Data-driven trigger, not time-based.** A run launches when an algorithm has
   accrued more than `remote_train_min_new_episodes` new rows since its model's
   watermark, checked on the normal epoch/reconnect cadence — no fixed "train every
   N hours" timer.
4. **Configurable stale lock.** `stale_lock_hours` (default **6**) is liberal
   because a desktop run time isn't known up front; lower it once measured.
5. **Training host separate from the backup host.** It has its own
   `train_host`/`train_user`/`train_path`/`train_port` and its own `setup`; it may
   reuse the device's `pi` SSH key, authorized on the second host.
6. **One contest for every model, judged on rows none trained on.** The plugin
   only pulls and stages (torch-free, in the daemon); the **agent** runs the same
   contest for a remote model as for one trained on the device: scored on
   held-out hours no model trains on, under one reward model, against the active
   model, behind a baseline floor, a significant paired difference, a 10% margin
   and a staleness guard. Either side wins when it's genuinely better, and
   neither wins by default. See
   [the promotion rules](agent-interface.md#candidates-and-promotion).

## Config surface

Under `[plugins.remote-train]` (ships disabled; all documented in `config.toml`):

- `train_host`, `train_user`, `train_path`, `train_port` — the host and where its
  work (venv, staging, markers, episode mirror) lives.
- `algorithms` — comma-separated, case-sensitive per-algorithm models to keep
  trained remotely: any of `RecurrentPPO`, `PPO`, `DQN`, `QR-DQN`. Defaults to
  all four; set to `""` to train nothing. A string, so it is settable with
  `plugins ... --set` and on the plugin's web settings page.
- `remote_train_min_new_episodes` — new-episode threshold that triggers a run.
  **Default 300** (a few hundred episodes since the model's watermark). Lower it
  to train more eagerly, raise it to batch more experience per run.
- `stale_lock_hours` — default `6`.
- `timesteps` — training timesteps per remote run (default 200000).
- `interval_seconds` — how often, at most, to reconcile (default 3600).
- `timeout` — per SSH/rsync operation, not the detached run (default 1800).
- `ssh_key` — reuse the backup/device key by default
  (`/home/pi/.ssh/id_ed25519`).
- `agent_dir` — the on-device agent runtime dir (episodes + per-algorithm
  models); default `/var/lib/kestrel/agent`.

## Using it

Remote training ships **off**. To turn it on, from a terminal on the device:

1. **Enable it with the training host set.** Enabling a `network` plugin takes
   the one-time authorized-use acknowledgment, the same as the uploaders:

   ```
   sudo kestrel plugins enable remote-train \
     --set train_host=desktop.lan --set train_user=me --set train_path=/srv/pwn-train \
     --set algorithms=RecurrentPPO,DQN
   ```

   (You can also set `algorithms` later on the plugin's settings page in the web
   UI, or with `kestrel plugins set remote-train --set algorithms=...`.)

2. **Install the device key on the host** — asks the host password once, stores
   nothing (it runs `ssh-copy-id`, reusing the device's own key):

   ```
   sudo kestrel remote-train setup
   ```

   This also creates the plugin's venv under `train_path`, copies the device's
   package into it, and starts the ML-stack install in the background on the host.

3. **That's it.** From here the plugin runs on its own: it checks on each epoch
   and on reconnect, and launches a run for an algorithm once enough new episodes
   have accrued (`remote_train_min_new_episodes`). You don't have to trigger
   anything.

Useful commands:

- `kestrel remote-train status` — the configured host, whether SSH reaches
  it, and the host's job markers (what's installing / running / done).
- `kestrel remote-train now` — reconcile immediately: pull and stage any new
  finished model (the agent then runs its contest), and launch due runs, without
  waiting for the interval.

The plugin's dashboard card shows the train host, the host-stack state, the live
training status and when a model was last pulled. A finished model is pulled back
and staged automatically; the agent judges it against the current model on
held-out hours no model trained on and promotes it only if it's genuinely better
(see above), picking it up live, no restart.

Uninstalling is `sudo kestrel plugins disable remote-train` on the device
and deleting `train_path` on the host.
