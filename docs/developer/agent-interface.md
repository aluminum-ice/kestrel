# Agent interface contract

`SPEC_VERSION: 2`

This is the stable seam between the reinforcement-learning algorithm and the
device. It is the promise about what stays put, so a trained model is portable
across devices and adapters and a tool that produces models or consumes the
agent's logs can build against it without reading the implementation.

It is versioned by `SPEC_VERSION` (in
`builder/extra/files/kestrel_pi/kestrel_pi/agent/spec.py`, recorded in
every model sidecar). A change that alters the observation or action **shapes**
bumps it; a model whose sidecar names a different `SPEC_VERSION` falls back to
the baseline rather than loading a wrong-shaped policy. `SPEC_VERSION` is
distinct from the capture `contract_version` (AngryOxide's launch surface, see
[Capture interface](capture-interface.md)) and from `HOOK_API_VERSION` (the
plugin hook API) — the three move independently.

This file is the normative reference. `spec.py` is pure stdlib + numpy (no gymnasium, no torch,
no hardware), so it is exactly what a trainer or a test imports to build against
the contract.

## Action space

- **`Discrete(32)`** — a single flattened index, `0 .. ACTION_N-1`, where
  `ACTION_N = len(POSTURES) * len(DWELLS) * len(TRANSMIT) = 4 × 4 × 2 = 32`.
  It is flattened (not `MultiDiscrete`) on purpose: DQN and QR-DQN accept only a
  plain `Discrete`, so a flat index is what keeps all four algorithms swappable
  from config with no code change.
- **The triple** an index decodes to is `(posture, dwell_seconds, transmit)`:
  - `POSTURES = ("sweep-band", "lock-subset", "pin-channel", "autohunt")`
  - `DWELLS = (2, 5, 10, 20)` seconds
  - `TRANSMIT = ("active", "passive")` — `passive` is AngryOxide `--notransmit`
- **Bijection** — `spec.decode(index) -> (posture, dwell, transmit)` and
  `spec.encode(posture, dwell, transmit) -> index` are inverse over
  `range(32)`; an out-of-range index raises `ValueError` (a bad action never
  silently maps to a posture). The layout is
  `index = (posture_i*4 + dwell_i)*2 + transmit_i`, so
  `posture = index // 8`, `dwell = (index % 8) // 2`, `transmit = index % 2`.
  (e.g. `0` = sweep-band @ 2s active; `31` = autohunt @ 20s passive.)
- **Postures are adapter-independent.** A posture is a *channel plan realized
  through AngryOxide v0.9.3 launch flags only* — there is no live or per-target
  control to expose. The concrete channel(s) come from the live channel ranking
  (`spec.rank_channels`), not from the action, so the action never changes with
  the dongle. `spec.posture_to_plan(posture, ranking, bands, lock_subset_n)`
  turns a posture into the launcher plan:
  - `sweep-band` → `{"kind": "band", "band": "<2|5|6|60|both>"}` (`-b`)
  - `lock-subset` → `{"kind": "channels", "channels": [top-N]}` (`-c`, N =
    `[agent.action] lock_subset_n`)
  - `pin-channel` → `{"kind": "channels", "channels": [top-1]}` (`-c`)
  - `autohunt` → `{"kind": "autohunt"}` (`--autohunt`)
  A posture with no channels to pin/lock (empty ranking) degrades to a band
  sweep, so a decision window is never a no-op.

## Observation space

- **`Box(395,)`**, `float32`, every feature normalized to `[0, 1]`.
  `OBS_SIZE = MAX_CH * len(PER_CH_FEATURES) + len(GLOBAL_FEATURES) =
  64 × 6 + 11 = 395`.
- **Layout** — `MAX_CH = 64` per-channel slots first, then 11 global features:
  - **Per-channel block** (6 floats, fixed order): `aps`, `clients`, `yield`,
    `recency`, `sig_mean`, `sig_var`. Channel slot `s` occupies
    `obs[s*6 : s*6+6]`.
  - **Global features** at offset `384` (11 floats, fixed order): `churn`,
    `drift`, `session`, `rundir`, `whitelist`, then the Phase 4.8 GPS blocks —
    the **motion** block `gps_valid`, `speed`, `turn_rate` and the **fix-quality**
    block `sat_count`, `fix_accuracy`, `signal_strength`. The GPS features are
    appended after the original five (their offsets are unchanged); see
    [GPS observation (Phase 4.8)](#gps-observation-phase-48).
- **Channel → slot** is the adapter's resolved supported-channel set (sorted,
  de-duplicated, resolved once at env start). The tail is **zero-padded** to the
  constant `MAX_CH`, and a channel the adapter does not support (or beyond
  `MAX_CH`) is never populated and never selected. This fixed, zero-padded shape
  is what makes a model trained on one adapter load on another.
- **Normalization** uses documented soft caps in `spec.py` (`NORM_APS`,
  `NORM_YIELD`, `NORM_RECENCY_S = 300`, signal mapped over
  `[SIG_MIN_DBM, SIG_MAX_DBM] = [-100, -30]` dBm, the GPS `NORM_*` below, …) —
  fixed constants, not learned.
- **`spec.pack_observation(per_channel, globals_, channel_index)`** assembles
  the vector; `spec.empty_observation()` and `spec.observation_bounds()` give a
  zeroed vector and the `Box` bounds.

### Why each signal is in the observation

The non-GPS features split into three roles (the GPS block below adds motion
and open-sky reads). Each is normalized to `[0, 1]` by a documented soft cap so
no single raw count dominates the policy input.

- **Per-channel productivity** — `aps`, `clients`, `yield`, `sig_mean`,
  `sig_var`. The observed payoff and population of each channel, so the policy
  can favour channels that produce handshakes and avoid dead air. `yield` (new
  unique handshakes attributed to the channel last window) is the most direct
  productivity signal; `aps`/`clients` say whether there is anything there to
  capture at all; `sig_mean`/`sig_var` separate a channel with a few strong,
  stable APs from a weak or noisy one (which affects how long a dwell is worth).
- **Per-channel recency** — `recency` (time since the channel was last visited).
  Paired with the global `churn`, this is the mobility cue that lets a recurrent
  policy tell *moving* (a channel's value decays fast, so revisiting matters)
  from *parked* (a channel that was dead tends to stay dead). It also stops a
  locked subset from starving the rest of the band indefinitely.
- **Global context** — `churn` (BSSID turnover, new vs returning APs per window)
  and `drift` (how fast signal strengths are moving) are the RF-only mobility
  proxies, both of which rise when the device is in motion. `session`
  (handshakes captured so far), `rundir` (fraction of the run-dir cap used) and
  `whitelist` (how much of what is in range is off-limits) ground the policy in
  the device's real state — there is little point hammering a band that is
  almost entirely whitelisted, or a channel plan that would fill a nearly-full
  run-dir.

The GPS block below is appended to these; a model learns to lean on whichever
signals its own data makes predictive, and GPS is zeroed out (so ignored) when
no fix is available.

## GPS observation (Phase 4.8)

The six GPS globals let the policy condition its scan posture on **motion**
(moving vs parked, turning vs holding a heading) and on an **open-sky vs
enclosed** proxy (the fix quality), both of which the RF-inferred signals
(`churn`/`drift`) cannot give reliably today. GPS is optional: with no receiver,
no lock or a stale fix, all six read `0.0` with `gps_valid = 0`, so a model
learns to ignore the block when that bit is off.

| Feature | Meaning | Normalization |
| --- | --- | --- |
| `gps_valid` | a fresh, locked (≥ 2D) fix is available this window | `0`/`1` |
| `speed` | ground speed derived in the agent from successive fixes | `min(1, m_s / NORM_SPEED_MS)` (`15.0`) |
| `turn_rate` | absolute heading change per second between the last two fixes | `min(1, deg_s / NORM_TURN_RATE)` (`45.0`) |
| `sat_count` | satellites used in the fix | `min(1, sats / NORM_SATS)` (`12`) |
| `fix_accuracy` | inverse HDOP (higher ⇒ more open sky) | `clip01(1 - hdop / NORM_HDOP_MAX)` (`20`) |
| `signal_strength` | mean C/N0 (dB-Hz) of the tracked satellites | `clip01((cn0 - NORM_CNO_MIN) / (NORM_CNO_MAX - NORM_CNO_MIN))` (`10..50`) |

- **`speed`/`turn_rate` are derived in the agent** (`features.FeatureTracker`)
  from the displacement and bearing change between successive fixes over the
  window delta — **not** taken from a receiver sentence — so the signal works
  with a GGA-only serial receiver that reports no speed. A near-stationary fix
  (< 1 m of movement) leaves the heading unchanged so GPS noise never
  manufactures a turn.
- **The cross-process seam** is a file, not an import (the agent never imports
  plugin code). The `gps` plugin (inside the daemon) writes the latest fix
  atomically to **`/run/kestrel/position.json`** (tmpfs; the daemon unit's
  `RuntimeDirectory=kestrel` owns it) whenever the fix updates:

  ```json
  {"ts": <epoch>, "lat": <deg>, "lon": <deg>, "alt": <m|null>,
   "fix": <0|1|2|3>, "sats": <n|null>, "hdop": <float|null>, "cn0": <dB-Hz|null>}
  ```

  The agent reads it best-effort once per decision window. A missing,
  unparseable or **stale** file (`now - ts` beyond `max(15 s, 2 ×
  decision_seconds)`) is treated as no GPS — never an error, never a stall.
- **Kill-switch:** `[agent] use_gps` (default `true`) forces the block out of
  the observation without disabling the `gps` plugin. Reading the device's own
  position locally needs no network capability and no authorized-use ack.

## Reward

`spec.reward(new_unique_hashlines, had_discoverable_aps, weights)` —

```
value = per_handshake * max(0, new_unique_hashlines) - time_penalty
if not had_discoverable_aps:      # dwelled on dead air
    value -= idle_penalty
```

`weights` is the `[agent.reward]` table (defaults `per_handshake = 1.0`,
`time_penalty = 0.01`, `idle_penalty = 0.05`). The idle penalty applies only
when the window saw no discoverable (non-whitelisted) APs on the chosen
channel(s), never as a blanket cost, so the signal stays "almost pure new-unique
yield" — the Gate 4 metric.

## Control (the launch-time seam)

The agent drives capture through one file and nothing else. Each decision window
the controller writes an **override** to `<agent dir>/scan-override.json`
(atomically, via a `.tmp` + `os.replace`):

```json
{"kind": "band", "band": "both", "dwell": 5, "transmit": "active"}
```

- `kind` / `band` / `channels` come from `posture_to_plan` (above); `dwell` is
  the decoded dwell seconds; `transmit` is `active` | `passive`.
- The Phase 2 capture launcher reads the override and relaunches AngryOxide
  pinned to that plan. Control is **launch-time**, matching the capture
  contract: a change takes effect on the next relaunch, not mid-run.
- The agent rewrites the override **only when the plan, dwell or transmit mode
  changes**, and the launcher relaunches only when the file's **content**
  changes (a SHA-256 fingerprint, not the timestamp), because every relaunch
  restarts the scan. A relaunch for a new plan keeps the previous run's raw
  files, so the collector's in-range reading still has recent packets while
  the new run's kismet DB fills; a capture start or a size-cap roll clears them.
- The override is the `pi` agent's file and the launcher is root, so the
  launcher opens it without following a symlink or blocking on a FIFO, and
  reads only a small regular file.
- The launcher honours the override **only** while `[agent] enabled` is true.
  The agent never starts AngryOxide itself and never rewrites `config.toml`, so
  disabling the agent makes the launcher ignore the override and capture returns
  to the configured `[capture.scan]` sweep with nothing written back.

## Model artifacts

All of the agent's persistent artifacts live under
`AGENT_DIR = /var/lib/kestrel/agent` (owned by `pi`). The boot-partition
importer and the backup plugin reference these names.

- **Per-algorithm model + sidecar** — `model-<slug>.zip` and
  `model-<slug>.meta.json`, where `<slug>` is the algorithm name lowercased with
  every non-`[a-z0-9-]` character turned into `-` (`RecurrentPPO` →
  `recurrentppo`, `QR-DQN` → `qr-dqn`; `agent.model_slug()` is the one
  definition, mirrored by `agent-provision.sh` and the web/CLI). Each algorithm
  keeps its own file, so switching `[agent] algorithm` and back **reloads** the
  saved model instead of retraining.
- **Sidecar schema** (`model-<slug>.meta.json`):

  | key | type | meaning |
  | --- | --- | --- |
  | `algorithm` | string | the algorithm this model is for (decides the file it lands in) |
  | `spec_version` | int | the `SPEC_VERSION` it was trained under |
  | `data_watermark` | `{latest_ts, episodes}` | newest episode data it learned from (optional) |
  | `provenance` | object | which reward model trained it + held-out scores (optional) |
  | `train_seconds` | number | how long the training run took (optional) |
  | `source` | `"local"` \| `"remote"` | trained on this device, or pulled from the remote host (optional) |

  Every optional key is additive: a reader that predates one ignores it, so
  adding a key does **not** bump `SPEC_VERSION`.
- **Algorithms** (`policies.ALGORITHMS`): `RecurrentPPO` (default,
  `sb3_contrib`, `MlpLstmPolicy`), `PPO` (`stable_baselines3`, `MlpPolicy`),
  `DQN` (`stable_baselines3`, `MlpPolicy`), `QR-DQN` (`sb3_contrib` `QRDQN`,
  `MlpPolicy`). `[agent] policy` is `baseline` | `learned`.
- **Load gate.** `policy = learned` loads the resolved model for `[agent]
  algorithm` only when the sidecar's `algorithm` matches and its `spec_version`
  equals the running `SPEC_VERSION`. A missing model, an algorithm mismatch, a
  spec mismatch, a corrupt file, or the ML stack being absent all fall back to
  the **baseline scripted sweep** with a logged reason. The baseline is always
  available and is the scoring reference.
- **Legacy migration** — a single `model.zip` / `model.meta.json` (from before
  per-algorithm files) still loads for the algorithm its sidecar names; it is
  the only fallback path and is otherwise superseded by the per-algorithm files.

## Remote-model staging and promotion

A model pulled from a remote training host is **staged**, not dropped straight
into the active slot, so the agent can judge it on quality before adopting it
(see [Remote training](remote-training.md)).

- **Staging dir** — `AGENT_DIR/.remote-staging/<slug>/` holds a pulled
  `model-<slug>.zip` + `model-<slug>.meta.json` for the configured algorithm
  (`agent.REMOTE_STAGING_DIR`). The `remote-train` plugin (daemon, **torch-free**)
  writes here after a cheap pre-filter (spec match + data-watermark freshness via
  `metrics.remote_model_wins`); it never loads a model and never writes the active
  slot. A candidate the pre-filter rejects is cleared from staging.
- **Promotion ownership is the agent process** (which has torch for inference).
  Its decide loop watches the staged model's mtime like it watches the active
  model's. When a staged model appears, the agent re-scores the staged remote and
  the incumbent on one fixed held-out slice of the current logs under one reward
  model (`train.score_candidates`) and applies `metrics.promotion_decision`
  (spec gate, no-local bootstrap, baseline floor, `[agent] promotion_margin`
  lift-over-baseline margin, `[agent] promotion_max_stale_episodes` staleness
  guard). On a win it atomically `os.replace`s the staged files into the active
  slot and restamps `source="remote"`; otherwise the incumbent stays and the
  staged files are left for diagnostics. The swap is serialized against the
  agent's own training (`_training` guard) so a half-written model is never
  scored or promoted. The daemon and plugins stay torch-free throughout.
- **Lifetime adoption tally.** Each adoption increments a persistent
  `promotion-tally.json` (`{"local": N, "remote": M}`) in the agent dir: a
  completed on-device training run (which overwrites the active model with a
  freshly trained local one) bumps `local`; a staged remote model promoted into
  the active slot bumps `remote`. The agent publishes it in `status.json`
  (`promotion_tally`) and the daemon renders it on the `/agent` card as a
  "Promotions" line (`N on-device / M remote`), so the field can see which side
  has been winning over the device's life.
- **Validating it in the field.** `builder/extra/files/scripts/pull-agent-diagnostics.sh`
  (shipped in the image under `/usr/local/share/kestrel/scripts/`, run from
  your workstation) pulls a PII-free snapshot off a device — the model + staged sidecars,
  `status.json`, the agent-dir listings, and the `promotion check … | scores={…}`
  / `staged …` / `promoted …` journal lines — so a promotion decision can be
  checked off-device.

## Episode logs

The controller appends one JSONL row per decision window to
`<agent dir>/episodes/episodes-YYYYMMDD.jsonl` (UTC-dated; the directory is
size-bounded by `[agent] log_max_mb`, oldest files roll out, the newest is never
deleted). This is the sole training input and the sole source of the
agent-vs-sweep metrics.

Row schema:

| key | type | meaning |
| --- | --- | --- |
| `ts` | float | epoch seconds |
| `policy` | `"baseline"` \| `"learned"` | which policy drove this window |
| `step` | int | step index within the run |
| `action` | int | the `Discrete(32)` index |
| `posture` | string | decoded posture |
| `dwell` | int | decoded dwell seconds |
| `transmit` | `"active"` \| `"passive"` | decoded transmit mode |
| `window_seconds` | int | wall-clock length of the window |
| `new_hashlines` | int | new unique hashlines captured in the window |
| `new_networks` | int | distinct AP BSSIDs among those hashlines |
| `discoverable` | int | discoverable (non-whitelisted) AP-windows |
| `reward` | float | the window's reward |
| `obs` | list[float] | the 395-wide observation that led to `action` (rounded to 5 dp) |

Beside the logs, `episodes/context.json` tags the directory with a coarse,
PII-free device context for a future fleet trainer: `{"spec_version", "board",
"written"}`, where `board` is a device-tree model string (e.g. `Raspberry Pi 5
Model B`) or `null`. It holds no captures, SSIDs, client identifiers or
locations.

## Status

The controller writes `<agent dir>/status.json` for the daemon's "Learning
agent" dashboard card. `agent/readout.py` is a **stdlib-only** reader (no torch,
no gymnasium, not even the env spec), so the always-on daemon renders the card
without the heavy ML stack and never imports the agent's env or policies.

Always present: `running` (bool), `policy`, `algorithm`, `model` (the basename
of the per-algorithm model file), `train` (`off` | `idle`), `training` (bool),
`can_train` (bool) and `train_blocked` (reason when it is not),
`last_trained` / `last_train_secs`, `session_yield`, `decisions`, `detail`,
`updated`, and `metrics`. Present when applicable: `model_source` (`local` |
`remote`, learned only), and the last window's `posture` / `dwell` / `transmit`
/ `last_reward`.

`metrics` is `{"window_minutes", "baseline": {...}, "learned": {...}}` with only
the policies that have windows in range; each per-policy block carries
`windows`, `handshakes`, `networks`, `hit_rate`, `yield_per_hour`,
`coverage_per_hour` and `opportunity_yield` (handshakes per discoverable
AP-window). `kestrel agent metrics` prints the same figures as a table,
recomputed from the logs, so the dashboard and the CLI reconcile.

## Stability

- **Frozen in v2** (`SPEC_VERSION = 2`): the `Discrete(32)` action vocabulary
  and its `decode`/`encode`, the 395-wide observation layout and feature order
  (the GPS blocks appended in Phase 4.8), the reward shape, the `algorithm` /
  `spec_version` sidecar keys, the `scan-override.json` fields, the
  `position.json` seam schema, and the episode-row and `status.json` field
  meanings above. A model whose sidecar `spec_version` does not match falls back
  to the baseline instead of loading — so every v1 model falls back under v2
  until it is retrained or a v2 model is imported.
- **Not frozen in v1**: live RF observations. AngryOxide v0.9.3 exposes no
  stable live-status API, so the default status source returns `None` and the
  per-channel/global RF features are best-effort and may be all-zero; the
  append-only **store-delta yield still drives the reward** regardless. A future
  `contract_version` bump on the capture side could add a live status channel.
- **Additive, not versioned**: the optional sidecar and status fields
  (`provenance`, `data_watermark`, `source`, `model_source`) are
  source-compatible — adding one changes neither `SPEC_VERSION` nor
  `HOOK_API_VERSION`.

See also: [Capture interface](capture-interface.md),
[Remote training](remote-training.md), [Architecture](architecture.md).
