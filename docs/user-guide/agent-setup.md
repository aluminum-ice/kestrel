# The learning capture agent (`agent`)

A guide to the reinforcement-learning capture agent: what it does, how to turn it
on, how to read whether it is actually beating the scripted sweep, and how to
train and move a model between devices.

The agent ships **off**. With it off, capture runs exactly the built-in scripted
channel sweep. Turning it on is opt-in and takes the same authorized-use
acknowledgment as capture itself.

## What it does

Instead of sweeping channels on a fixed schedule, the agent picks a **scan
posture** for each short window and lets the capture launcher realize it:

- **sweep-band** — sweep a whole band (the Phase 2 behaviour).
- **lock-subset** — lock onto the top few channels that have been productive.
- **pin-channel** — camp on the single best channel.
- **autohunt** — hand channel choice to AngryOxide's own hunt mode.

Each posture is combined with a **dwell** (how long to sit, in seconds) and a
**transmit** choice (active, or passive = listen-only). That is the whole action
space: every choice maps only to an AngryOxide launch flag, so the agent never
does anything the sweep could not, it just decides *when* to do each thing based
on what it is seeing (AP and client density, signal drift, how long since it
last visited a channel, how many handshakes each channel has been yielding).

It optimizes for the one number that matters at the gate: **new unique
handshakes per hour**, with only a light nudge away from dwelling on dead air.

### Using GPS (optional)

If the [`gps`](gps-setup.md) plugin is enabled and has a fix, the agent also
sees **motion** (whether the device is moving or parked, turning or holding a
heading) and a coarse **open-sky vs enclosed** signal (from the fix quality —
satellite count, accuracy and signal strength), so it can scan differently while
walking than while parked indoors. Speed and heading change are derived on the
device from successive fixes, so even a basic USB receiver works. GPS is
optional and local: with no receiver, no lock or a stale fix the agent simply
runs without it, and it is read on-device with no network and no extra
acknowledgment. To keep GPS out of the agent's input without disabling the `gps`
plugin, set `[agent] use_gps = false`.

> **Upgrading note.** This image carries the GPS-aware observation
> (`SPEC_VERSION 2`). A learned model trained on an older image is tied to the
> old observation shape, so after updating, each device falls back to the safe
> scripted `baseline` until it retrains or imports a new model — exactly the
> same fallback as a missing or mismatched model, with no crash.

### What it does not do

- It never attacks whitelisted networks (they are excluded from every signal it
  learns from).
- It never starts or stops AngryOxide itself, and never edits `config.toml`. It
  writes a scan plan, whenever it picks a different one, that the existing
  capture service reads; capture stays in charge of the radio.
- It does nothing at all unless **both** capture and the agent are enabled.

## Requirements

- Capture must be set up and enabled first — see
  [Setting up the capture service](capture-setup.md). The agent is a smarter
  driver for capture, not a replacement.
- A board with enough RAM to run inference. Inference itself is light; on-device
  *training* is gated by RAM (see [Training](#training-on-device-and-off)).
- No extra hardware and no network. The ML runtime (stable-baselines3,
  sb3-contrib, gymnasium, torch) is already in the image.

## Turn it on

```bash
sudo kestrel capture enable      # if capture isn't on yet
sudo kestrel agent enable
```

`agent enable` takes the authorized-use acknowledgment the first time (as
capture does; `--yes` confirms it non-interactively). By default it runs the
**baseline** policy — the scripted sweep expressed in the agent's own action
vocabulary — so nothing about capture's behaviour changes yet, but every window
is now logged for comparison and training. That is the honest starting point:
the agent earns the switch to a learned policy by the numbers.

Check what it is doing:

```bash
kestrel agent status      # state, policy, algorithm, current posture
kestrel agent metrics     # the agent-vs-sweep table (see below)
```

Turn it back off at any time — capture immediately returns to the configured
sweep:

```bash
sudo kestrel agent disable
```

You can also drive all of this from the **web dashboard** (with web auth on):
the dashboard's **AI Agent** card opens the agent page, which has an
enable/disable button, a policy choice (baseline or learned), an algorithm
picker, an **On-device training** On/Off toggle, a **Train now** button and the
live posture and agent-vs-sweep rates.

## A typical first run

Start to finish, the usual path from a working capture setup to a learned
policy that you've confirmed is worth using:

1. **Enable capture, then the agent.** `sudo kestrel capture enable`, then
   `sudo kestrel agent enable`. The agent starts on the **baseline** sweep
   and begins logging every window — capture behaves exactly as before for now.
2. **Let it gather experience.** Capture as you normally would for a while. Each
   decision window is logged, and the agent needs a body of those logs before a
   model is worth training. On a Pi 5 (or any ≥~4 GB board) a fine-tune starts on
   its own once enough new windows have accrued; on a smaller board, train on a
   desktop instead (see [Training](#training-on-device-and-off)).
3. **Switch to the learned policy.** `sudo kestrel agent set --policy learned`
   (or the web page). If a model for the current algorithm exists it loads within
   about one decision window; if none exists yet, switching to that algorithm
   starts a training run first and asks you to accept it. Until a model is ready
   the agent safely keeps driving the baseline and the status line says so.
4. **Confirm it's actually winning.** Turn on interleaved comparison and read
   `kestrel agent metrics` (see [Reading the comparison](#reading-the-comparison)).
   If learned beats baseline on handshakes/hour, keep it; if not, you've lost
   nothing — the baseline sweep is always the floor it falls back to.

Every step here can be done from the web dashboard's **AI Agent** page instead
of the terminal.

## Algorithm and policy

Two config keys decide how the agent behaves:

- **`algorithm`** — which reinforcement-learning algorithm a *learned* model
  uses: `RecurrentPPO` (default), `PPO`, `DQN`, or `QR-DQN`. RecurrentPPO's
  memory helps it tell "the device is moving" from "parked".
- **`policy`** — what actually drives capture: `baseline` (the sweep) or
  `learned` (a trained model). `learned` with no valid model on the device
  safely falls back to `baseline`.

```bash
sudo kestrel agent set --algorithm DQN
sudo kestrel agent set --policy learned
```

The CLI and the web dropdowns write the same two keys and can never drift. An
unknown value is rejected before anything is written.

### Each algorithm keeps its own model

A model is bound to the algorithm it was trained under, so the device keeps a
**separate model file per algorithm** (`model-<algorithm>.zip`, e.g.
`model-dqn.zip`). Switching algorithm and back reloads that algorithm's saved
model — you don't lose a trained DQN model by trying RecurrentPPO for a while.

Switching to an algorithm that has **no** model yet starts a **background
training run** to build one (from the logged episodes, when the board and
resources allow — see [Training](#training-on-device-and-off)). Because that
run uses the device, it is an explicit choice:

- On the **web** `/agent` page, picking such an algorithm shows a short confirm
  page ("Train a model for … ?") before anything changes.
- On the **CLI**, `agent set`/`agent algorithm`/`agent enable` print a one-line
  warning and start the run (running the command is the acceptance).

Switching to an algorithm that already has a model just loads it — no training,
no confirm. A model trained or imported before this change (a single
`model.zip`) still loads for the algorithm its sidecar names.

## Reading the comparison

Because a device has one radio and the RF world around it drifts, the only
honest A/B test is to **interleave** the two policies within one session and
compare them window-for-window. Turn that on:

```toml
[agent]
interleave_compare = true    # alternate baseline/learned blocks
compare_block_minutes = 10   # length of each block
```

Then `kestrel agent metrics` prints handshakes/hour, new networks/hour,
window hit-rate and handshakes-per-AP-window for each policy, computed from the
same episode log either policy wrote. The dashboard card shows the headline
rates live. The exact same numbers can be recomputed off-device from the logs
for the Gate 4 record.

## Training (on-device and off)

The agent learns from **episode logs** — one JSONL row per decision window,
written under the agent directory. Training replays those logs; it is a short
fine-tune, not a from-scratch run, and the trained `model.zip` is only picked up
if `policy = learned`.

**What the trainer learns.** Training fits a model of how much reward each scan
posture earns *given what the RF environment looks like* — a state-conditioned
reward model — so the learned policy can choose a different posture in a busy
place than in a quiet one, instead of only the posture that pays off best on
average. If a device's log is too thin or too noisy for that to hold up on a
held-out slice of the log, the trainer automatically falls back to the simpler
average-reward model, and (as always) a learned policy that does not match or
beat the scripted sweep on measured handshakes-per-hour is never adopted. The
on-device fine-tune and the desktop / `remote-train` runs use the exact same
trainer, so both produce the same kind of model; the model's sidecar records
which reward model trained it, for later comparison.

### When training runs, and why the defaults are what they are

On-device training is gated in layers, outermost first. Each layer exists to
keep training from ever hurting the thing you actually care about — capture.

- **The board gate comes first and overrides everything.** A board below the
  RAM floor (~4 GB — so a Pi Zero 2 W's 512 MB, a Pi 4 2GB, etc.) **never**
  trains on-device, no matter what any knob below says. Training a policy needs
  far more memory than running one, and on a small board it would either be
  killed by the OOM reaper or starve capture of CPU, so it is simply refused and
  the UI says why. Inference still runs fine on these boards — only *training*
  is blocked. Train on a desktop instead (see below) and fly the model over.
- **`train`** — `auto` (the default), `idle`, or `off`:
  - **`auto`** resolves once at agent start from the board's RAM: training **on**
    for a board that clears the floor (e.g. a Pi 5, a Pi 4 4GB/8GB), **off** for
    one below it. This is why a Pi 5 starts learning on its own out of the box
    while a Zero 2 W never does — the default adapts to the hardware instead of
    making you pick.
  - **`idle`** forces training on (still subject to the board gate — it can't
    switch training on where the board can't do it).
  - **`off`** forces it off. The **Train now** button still works regardless, so
    `off` means "don't train on your own", not "never train".
  - The agent page's **On-device training** toggle writes this key: **On** sets
    `auto` (board-aware), **Off** sets `off`. Turning it off is the simple way to
    stop the device out-training a model you train remotely.

### What runs during capture — the first-boot default

- **`train_while_capturing`** — `auto` (the default), `true`, or `false`:
  - **`auto` is board-aware, and this is what's decided at first boot:** on a
    board that can train (clears the RAM floor), it defaults to **true** —
    periodic fine-tunes run in the background *while capture continues*; on a
    board that can't train, it resolves to **false** (and training is blocked
    anyway). So a fresh Pi 5 image trains while capturing without you touching
    anything; a fresh Zero 2 W image does not.
  - Set it **`true`**/**`false`** to override that. **`false`** trains only while
    the device is genuinely idle (not actively catching handshakes) — the old,
    more conservative behaviour, useful if you'd rather never spend cycles on
    training mid-session.
  - Either way the training thread is **niced down**, so capture and the
    decision loop always win the scheduler. In practice a Pi 5 training while
    capturing draws noticeably more power but does not drop captures.
- **`train_min_new_episodes`** (default `60`) — a periodic fine-tune fires after
  this many *new* decision windows have been logged since the last run (the
  counter resets after each run), not on a fixed clock. This ties training to
  how much fresh experience there is to learn from: a busy session trains
  sooner, a quiet one later. Lower it to train more eagerly, raise it to train
  less often.
- **`train_timesteps`** (default `3000`) — gradient steps per fine-tune. Higher
  fits harder but costs more time and power per run.

### Safety clamps

Two knobs cap when a run may start; both are **hard-bounded** so a careless
value can't cook or OOM the board:

- **`train_max_temp_c`** (default `75`) — skip training at/above this CPU temp.
  **Hard-capped at 85 °C**: a larger value is clamped down, so you can never set
  a temperature that would let training run the SoC into thermal trouble.
- **`train_min_free_mb`** (default `512`) — skip training below this much free
  RAM. **Hard floor 256 MB**: a smaller positive value is raised to 256; `0` or
  blank falls back to the default. You can't set it low enough to let a run start
  with too little headroom.

A periodic run therefore only starts when: the board can train, training is on
for this board, enough new episodes have accrued, the idle requirement (if you
set `train_while_capturing = false`) is met, and there's thermal/memory
headroom. The **Train now** button bypasses the new-episode and idle gates but
still respects the board gate and the safety clamps, and tells you on the card
exactly which one blocked it.

While a run is in flight the agent card (on the AI Agent page, and the
dashboard's AI Agent summary) shows **Training… in progress**; once it finishes
the card shows **Last trained** (how long ago) and **Last train time** (how long
that run took). Both survive a reboot, so you can tell at a glance whether the
device has been learning on its own.

**Train now (dashboard).** On the AI Agent page, while the agent is enabled, a
**Train now** button starts a fine-tune on demand without waiting for the device
to go idle. It's available regardless of the `train` setting (so it works even
with `train = off`), but the agent is the final gate: it's greyed out, with the
reason shown, when a run is already in progress, there's no episode data yet,
the board is too small to train on, or it's low on memory or too hot. The run
happens in the background on the
device and won't interrupt capture; the card shows **Training… in progress**
while it runs. The same thing from the terminal:

```bash
kestrel agent train-now   # non-blocking: asks the running agent to train
```

You can also train by hand, synchronously, on the device or any machine with the
logs:

```bash
kestrel agent train --algorithm RecurrentPPO
```

That produces a `model.zip` and its sidecar (which records the algorithm and the
env-spec version the model is bound to).

### Training on a desktop Linux machine

The trainer is the same code that runs on the device, packaged as a standalone
command, so it runs on an ordinary desktop Linux machine — which trains far
faster than a Pi and keeps the load off the device.

> **Automate this with the `remote-train` plugin.** The manual round trip below
> is worth understanding, but the [`remote-train` plugin](remote-training.md)
> does the whole loop for you: it pushes the episode logs to a desktop host, runs
> this same trainer there in an isolated venv, and pulls the finished model back
> (adopting it only when it is genuinely fresher). Enable it and you never do the
> steps below by hand. Use the manual round trip for a one-off, or when you'd
> rather not give the device a key to the training host.

The manual round trip:

**1. Get the episode logs off the device.** The agent writes one JSONL row per
decision window to `/var/lib/kestrel/agent/episodes/`. Either copy the
agent directory over SSH:

```bash
scp -r pi@<device>:/var/lib/kestrel/agent ./agent
```

or, if you've enabled the [backup plugin](backup-setup.md), it already mirrors
the agent directory (model + episode logs) to your remote host — train from that
copy.

**2. Install the trainer on the desktop.** From a checkout of the project
repository:

```bash
pip install ./builder/extra/files/kestrel_pi
pip install stable-baselines3 sb3-contrib gymnasium torch
```

That gives you the `kestrel-agent-train` command. (A virtualenv is
recommended; there are no Pi-specific dependencies.)

**3. Train**, pointing at the copied agent directory:

```bash
kestrel-agent-train --agent-dir ./agent --algorithm RecurrentPPO --timesteps 20000
```

It replays the logs and writes `model.zip` and `model.meta.json` into that
directory. `--algorithm` accepts `RecurrentPPO`, `PPO`, `DQN` or `QR-DQN`;
`--timesteps` trades training time for fit.

**4. Fly the model back to the device**, either way from
[Moving a model between devices](#moving-a-model-between-devices) below: drop
`model.zip` + `model.meta.json` onto the boot partition (renamed
`kestrel-model.zip` / `kestrel-model.meta.json`), or
`sudo kestrel agent import ./agent/model.zip --algorithm RecurrentPPO`
over SSH. The model is device-independent, so one trained from one device's logs
runs on any board.

> **One caveat on the current trainer:** it learns from the reward each action
> earned in your logs — a deliberately simple first-version model — so training
> reflects the postures that paid off in the sessions you captured, not yet the
> finer "do X *when the radio environment looks like this*" choices. A richer,
> context-aware trainer that learns those is a planned follow-up; today's trainer
> and the scripted sweep it falls back to stay the dependable baseline.

### Moving a model between devices

Three ways to get a trained model onto a device:

1. **`agent import`** — install a model file you already have:

   ```bash
   sudo kestrel agent import ./model.zip --algorithm RecurrentPPO
   ```

   This copies it into the agent directory under that algorithm's own model
   name, points `[agent]` at it and sets `policy = learned`. Importing models for
   different algorithms keeps each one — they don't overwrite each other.

2. **The boot partition** — drop `kestrel-model.zip` (plus its
   `kestrel-model.meta.json` sidecar, and optionally a
   `kestrel-model-episodes.tar.gz`) onto the FAT boot partition from a card
   reader. On the next boot the device imports it, points `[agent]` at it, and
   removes the staged files. This is the "train on a laptop, fly it to the field
   unit" workflow. You can stage **more than one model** — one per algorithm,
   e.g. `kestrel-model-dqn.zip` + `kestrel-model-dqn.meta.json`
   alongside the plain pair — and all of them are imported, each to its own
   per-algorithm file; add a one-line `kestrel-model-active` file to pick
   which one `[agent]` is set to. See
   [Preparing the card before first boot](boot-partition-provisioning.md).

3. **The backup plugin** — if the [backup plugin](backup-setup.md) is enabled,
   the agent directory (model + episode logs, excluding the live status files) is
   mirrored off-device with the rest of the capture store, so a reflash doesn't
   lose the trained model.

The model is always portable: the observation and action shapes are fixed and
device-independent, and a model trained under a different env-spec version is
ignored in favour of the sweep rather than loaded wrong.

> **Fleet-wide learning** — pooling episode logs from many devices to train one
> shared model and pushing it back out — is deliberately left as future work.
> Today each device trains on its own logs; moving a model between them is manual
> (import / boot partition / backup), which is enough to share a model across a
> small fleet by hand.

## Troubleshooting

- **Policy is set to `learned` but the agent is running `baseline`.** That is the
  safe fallback, and `kestrel agent status` (and the card's note) say why.
  The usual reasons: there is **no model for the current algorithm yet** (train or
  import one, or wait for an auto-train run to finish); the model on disk was
  **trained under a different algorithm or an older env-spec version** (train or
  import one for the current algorithm); or the model file is unreadable. Once a
  model for the current algorithm is present it is adopted within about one
  decision window — no restart needed.
- **Switching algorithm "did nothing".** If that algorithm has **no** model yet,
  the switch starts a background training run (you are warned and must accept it
  first on the web page; the CLI prints a one-line warning). It needs logged
  episodes and a capable board, and the card shows **Training…** while it runs. If
  the algorithm already had a model, it is simply loaded — switching back and
  forth between trained algorithms is instant and never retrains.
- **Nothing trains on its own.** A board below the ~4 GB RAM floor never trains
  on-device (the card says so) — train on a desktop and fly the model over.
  Otherwise check `train` isn't `off`, and note a periodic run only fires once
  enough **new** episodes have accrued (`train_min_new_episodes`). The **Train
  now** button forces a run and names whatever is blocking it (no data, too hot,
  low memory, board too small).
- **The agent does nothing at all.** It runs only when **both** capture and the
  agent are enabled. `kestrel agent status` and the capture status show
  which is off.

## Configuration reference

All keys live under `[agent]` (and its `[agent.action]` / `[agent.reward]`
sub-tables) in `/etc/kestrel/config.toml`. Prefer the CLI or the web UI
over hand-editing; unknown keys are warned about and ignored.

| Key | Default | Meaning |
| --- | --- | --- |
| `enabled` | `false` | Opt-in; off = capture uses the scripted sweep. |
| `algorithm` | `RecurrentPPO` | `RecurrentPPO` \| `PPO` \| `DQN` \| `QR-DQN`. |
| `policy` | `baseline` | `baseline` (sweep) \| `learned` (`model.zip`). |
| `decision_seconds` | `30` | Length of one decision window. |
| `use_gps` | `true` | Feed the `gps` plugin's fix (motion + open-sky/enclosed quality) into the observation. Inert without the plugin and a fix; set `false` to force GPS out. |
| `train` | `auto` | `auto` \| `off` \| `idle`. `auto` = on for a ≥~4 GB board, off below; board gate overrides. |
| `train_while_capturing` | `auto` | `auto` \| `true` \| `false`. `auto` = true if the board can train (the first-boot default), else false; `false` = only when idle. |
| `train_min_new_episodes` | `60` | New decision windows since the last run before a periodic fine-tune fires (≥ 1). |
| `train_timesteps` | `3000` | Gradient steps per fine-tune (floored at 100). |
| `train_max_temp_c` | `75` | Skip training at/above this CPU temp (hard-capped at 85). |
| `train_min_free_mb` | `512` | Skip training below this free RAM (hard floor 256 MB). |
| `log_episodes` | `true` | Append per-window JSONL for training/metrics. |
| `interleave_compare` | `false` | Alternate baseline/learned for a fair A/B. |
| `compare_block_minutes` | `10` | Block length when interleaving. |
| `metrics_window_minutes` | `60` | Rolling window for the live metrics. |
| `log_max_mb` | `64` | Cap on episode logs (oldest rotate out). |
| `[agent.action] lock_subset_n` | `3` | Top-N channels for lock-subset. |
| `[agent.action] dwell_buckets` | `[2, 5, 10, 20]` | Dwell seconds the agent may choose. |
| `[agent.reward] per_handshake` | `1.0` | Reward per new unique handshake. |
| `[agent.reward] time_penalty` | `0.01` | Small per-window cost. |
| `[agent.reward] idle_penalty` | `0.05` | Extra cost for a window with no discoverable AP. |

## Commands

| Command | What it does |
| --- | --- |
| `kestrel agent status` | State, policy, algorithm and current posture. |
| `sudo kestrel agent enable [--algorithm A] [--policy P]` | Enable the agent (needs capture on). |
| `sudo kestrel agent disable` | Disable it; capture returns to the sweep. |
| `sudo kestrel agent set [--algorithm A] [--policy P]` | Change the algorithm and/or policy. Switching to an algorithm with no model yet starts a background training run to build one. |
| `sudo kestrel agent algorithm A` | Shorthand for `set --algorithm A`. |
| `sudo kestrel agent policy P` | Shorthand for `set --policy P`. |
| `sudo kestrel agent import FILE --algorithm A` | Install a trained model (kept per-algorithm) and switch to it. |
| `kestrel agent train [--algorithm A] [--timesteps N]` | Train synchronously from the logged episodes. |
| `kestrel agent train-now` | Ask the running agent to fine-tune in the background (non-blocking). |
| `kestrel agent metrics` | Show the agent-vs-sweep comparison. |

See the full [command reference](cli-reference.md).
