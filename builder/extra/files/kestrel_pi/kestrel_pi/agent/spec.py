"""The fixed env contract: observation shape, action vocabulary, reward.

This module is the *stable* seam between the RL algorithm and the device, and
the reason a trained model is portable across devices and adapters: the
observation is a fixed-size vector (zero-padded to a constant ``MAX_CH``) and
the action is a single flattened ``Discrete`` over adapter-independent postures,
so the spaces never change with the dongle. A ``model.zip`` is bound to this
spec via ``SPEC_VERSION`` (recorded in the model sidecar); a change that alters
the shapes bumps it, and a model trained under a different spec falls back to
the baseline rather than loading a wrong-shaped policy.

It is pure stdlib + numpy on purpose: no gymnasium, no torch, no hardware, so
it is what the env unit-tests exercise and what the daemon/metrics can import
with the heavy ML stack absent. The *decisions* (which flags a posture maps to,
how channels are ranked) live here; :mod:`.env` only wires them to the capture
interface, and :mod:`.features` only fills the observation from live state.
"""

from __future__ import annotations

import numpy as np

# Bumped only when the observation/action shapes change (a model trained under
# an older spec then falls back to the baseline). Not the capture
# contract_version (that is AngryOxide's launch surface) and not
# HOOK_API_VERSION (the plugin hook API).
#   v2 (Phase 4.8): the global block gained a GPS motion block (gps_valid,
#   speed, turn_rate) and a GPS fix-quality block (sat_count, fix_accuracy,
#   signal_strength); OBS_SIZE 389 -> 395. v1 models fall back to the baseline.
SPEC_VERSION = 2

# -- action vocabulary ------------------------------------------------------
# A scan posture is a structured channel plan realized purely through
# AngryOxide v0.9.3 launch flags (see docs/developer/agent-interface.md, "Action
# space"): there is no live or per-target control to
# expose. The concrete channel(s) a posture uses come from the live channel
# ranking, NOT from the action, so the action stays adapter-independent.
POSTURES = ("sweep-band", "lock-subset", "pin-channel", "autohunt")
# Dwell buckets (seconds) the agent may choose; each maps to AngryOxide --dwell.
DWELLS = (2, 5, 10, 20)
# Transmit mode: active, or passive (AngryOxide --notransmit) for the window.
TRANSMIT = ("active", "passive")

# The flattened Discrete size. Flattened (not MultiDiscrete) so DQN and QR-DQN -
# which accept only a plain Discrete space - load against the env unchanged;
# that is what keeps all four algorithms swappable from config with no code
# change. decode() is the single bijection between the index and the triple.
ACTION_N = len(POSTURES) * len(DWELLS) * len(TRANSMIT)


def decode(action):
    """Decode a ``Discrete`` index into ``(posture, dwell_seconds, transmit)``.

    Bijective with :func:`encode` over ``range(ACTION_N)``; raises ValueError
    for an out-of-range index so a bad action never silently maps to a posture.
    """
    action = int(action)
    if not 0 <= action < ACTION_N:
        raise ValueError("action %d out of range [0, %d)" % (action, ACTION_N))
    per_posture = len(DWELLS) * len(TRANSMIT)
    posture = POSTURES[action // per_posture]
    rem = action % per_posture
    dwell = DWELLS[rem // len(TRANSMIT)]
    transmit = TRANSMIT[rem % len(TRANSMIT)]
    return posture, dwell, transmit


def encode(posture, dwell, transmit):
    """The ``Discrete`` index for a ``(posture, dwell, transmit)`` triple.

    Inverse of :func:`decode`; raises ValueError for a value outside the
    vocabulary."""
    if posture not in POSTURES:
        raise ValueError("unknown posture: %r" % (posture,))
    if dwell not in DWELLS:
        raise ValueError("unknown dwell: %r" % (dwell,))
    if transmit not in TRANSMIT:
        raise ValueError("unknown transmit mode: %r" % (transmit,))
    return ((POSTURES.index(posture) * len(DWELLS) + DWELLS.index(dwell))
            * len(TRANSMIT) + TRANSMIT.index(transmit))


# -- observation shape ------------------------------------------------------
# The channel axis is zero-padded to a constant so the observation shape is the
# same on every adapter (a dongle with fewer channels leaves the tail masked to
# zero; channels the adapter does not support are never selected). 64 covers
# 2.4 + 5 GHz with headroom; 6 GHz-heavy adapters would bump this (a spec
# change).
MAX_CH = 64
# Per-channel features, in this fixed order (each normalized to 0..1):
#   aps      - recent APs seen on the channel (vs a soft cap)
#   clients  - recent clients seen on the channel
#   yield    - new unique handshakes attributed to the channel last window
#   recency  - time since the channel was last visited (per-channel revisit
#              interval; the mobility/dynamics signal that lets a recurrent
#              policy tell "moving" from "parked")
#   sig_mean - mean signal strength on the channel
#   sig_var  - signal-strength variance on the channel
PER_CH_FEATURES = ("aps", "clients", "yield", "recency", "sig_mean", "sig_var")
# Global features, in this fixed order (each normalized to 0..1):
#   churn    - BSSID churn rate (new vs returning APs per window): mobility
#   drift    - how fast signal strengths are drifting: mobility
#   session  - session handshake total (vs a soft cap)
#   rundir   - run-dir pressure (fraction of the configured run-dir cap)
#   whitelist- whitelist size (vs a soft cap)
# GPS motion block (Phase 4.8; derived in the agent from successive fixes, so it
# works with a GGA-only serial receiver). All zero, with gps_valid = 0, when
# there is no receiver, no lock, or a stale fix:
#   gps_valid  - 1 when a fresh, locked fix is available this window, else 0
#   speed      - derived ground speed (vs NORM_SPEED_MS)
#   turn_rate  - absolute heading change per second (vs NORM_TURN_RATE)
# GPS fix-quality block (Phase 4.8): an open-sky vs enclosed (indoor/outdoor)
# proxy, useful even when parked. All zero with gps_valid = 0:
#   sat_count       - satellites used in the fix (vs NORM_SATS)
#   fix_accuracy    - inverse HDOP: higher reads as more open sky
#   signal_strength - mean carrier-to-noise (C/N0) of the satellites used
# Appended (not inserted) after the original five so the per-channel and the
# original global slots keep their offsets; the SPEC_VERSION bump already
# retires v1 models, so the layout change is only for readability.
GLOBAL_FEATURES = ("churn", "drift", "session", "rundir", "whitelist",
                   "gps_valid", "speed", "turn_rate",
                   "sat_count", "fix_accuracy", "signal_strength")

OBS_SIZE = MAX_CH * len(PER_CH_FEATURES) + len(GLOBAL_FEATURES)

# Soft caps used to normalize raw counts into 0..1 (documented, not learned).
NORM_APS = 32.0
NORM_CLIENTS = 32.0
NORM_YIELD = 8.0
NORM_RECENCY_S = 300.0     # 5 min: a channel unvisited this long reads as 1.0
NORM_SESSION = 500.0
NORM_WHITELIST = 32.0
# Signal strengths are dBm, roughly [-100, -30]; map to 0..1.
SIG_MIN_DBM = -100.0
SIG_MAX_DBM = -30.0

# GPS normalization (Phase 4.8), documented not learned. Motion:
NORM_SPEED_MS = 15.0       # ~54 km/h reads as 1.0 (covers walking and driving)
NORM_TURN_RATE = 45.0      # deg/s; a brisk corner (~90 deg in 2 s) reads near 1.0
# Fix quality (the open-sky vs enclosed proxy):
NORM_SATS = 12.0           # satellites-used count that reads as a full 1.0
NORM_HDOP_MAX = 20.0       # HDOP at/above this reads as 0 accuracy (1 - hdop/max)
NORM_CNO_MIN = 10.0        # mean C/N0 (dB-Hz) floor -> 0.0
NORM_CNO_MAX = 50.0        # mean C/N0 (dB-Hz) ceiling -> 1.0


def _clip01(value):
    return float(min(1.0, max(0.0, value)))


def norm_signal(dbm):
    """A dBm reading normalized to 0..1 over ``[SIG_MIN_DBM, SIG_MAX_DBM]``."""
    span = SIG_MAX_DBM - SIG_MIN_DBM
    return _clip01((float(dbm) - SIG_MIN_DBM) / span) if span else 0.0


def empty_observation():
    """A zeroed observation vector of the fixed shape."""
    return np.zeros(OBS_SIZE, dtype=np.float32)


def observation_bounds():
    """``(low, high)`` arrays for the ``Box`` space - all features are 0..1."""
    low = np.zeros(OBS_SIZE, dtype=np.float32)
    high = np.ones(OBS_SIZE, dtype=np.float32)
    return low, high


def pack_observation(per_channel, globals_, channel_index):
    """Assemble the flat observation vector.

    ``per_channel`` maps a channel number to a dict of the ``PER_CH_FEATURES``
    (already normalized 0..1; missing keys read as 0). ``globals_`` maps the
    ``GLOBAL_FEATURES`` likewise. ``channel_index`` maps a channel number to its
    slot ``0..MAX_CH-1`` (the adapter's supported set, resolved once). Channels
    not in the index, or beyond ``MAX_CH``, are dropped (masked to zero)."""
    obs = empty_observation()
    width = len(PER_CH_FEATURES)
    for channel, feats in (per_channel or {}).items():
        slot = (channel_index or {}).get(channel)
        if slot is None or slot >= MAX_CH:
            continue
        base = slot * width
        for i, key in enumerate(PER_CH_FEATURES):
            obs[base + i] = _clip01(feats.get(key, 0.0))
    gbase = MAX_CH * width
    for i, key in enumerate(GLOBAL_FEATURES):
        obs[gbase + i] = _clip01((globals_ or {}).get(key, 0.0))
    return obs


# -- channel ranking --------------------------------------------------------
# Feeds the lock-subset (top-N) and pin-channel (top-1) postures. Ranks the
# adapter's supported channels by observed productivity: yield first, then
# recent AP/client presence, then how long since it was last visited (so a
# stale-but-unseen channel is explored). Deterministic from the observation, so
# the same obs always produces the same plan (the decode stays reproducible).
def rank_channels(per_channel, supported):
    """Return ``supported`` channels ordered best-first for lock/pin."""
    def score(channel):
        feats = (per_channel or {}).get(channel, {})
        return (
            feats.get("yield", 0.0) * 4.0
            + feats.get("aps", 0.0) * 2.0
            + feats.get("clients", 0.0)
            + feats.get("recency", 0.0) * 0.25
        )
    return sorted(supported, key=lambda ch: (-score(ch), ch))


def posture_to_plan(posture, ranking, bands, lock_subset_n):
    """Turn a decoded posture into an AngryOxide launch-flag plan.

    Returns a dict the controller's launcher turns into flags:
      ``{"kind": "band",   "band": "<2|5|both>"}``        -> -b
      ``{"kind": "channels", "channels": [ch, ...]}``     -> -c
      ``{"kind": "autohunt"}``                            -> --autohunt
    ``ranking`` is the best-first channel list from :func:`rank_channels`;
    ``bands`` is the operator's configured band for sweep-band (``both`` sweeps
    every supported channel). A posture with no channels to pin/lock (empty
    ranking) degrades to a band sweep so the window is never a no-op."""
    if posture == "autohunt":
        return {"kind": "autohunt"}
    if posture == "sweep-band":
        return {"kind": "band", "band": bands or "both"}
    if posture == "pin-channel":
        if ranking:
            return {"kind": "channels", "channels": [ranking[0]]}
        return {"kind": "band", "band": bands or "both"}
    if posture == "lock-subset":
        n = max(1, int(lock_subset_n or 1))
        if ranking:
            return {"kind": "channels", "channels": list(ranking[:n])}
        return {"kind": "band", "band": bands or "both"}
    raise ValueError("unknown posture: %r" % (posture,))


# -- reward -----------------------------------------------------------------
def reward(new_unique_hashlines, had_discoverable_aps, weights):
    """The per-window reward: new unique hashlines (the Gate 4 metric) with
    light, documented shaping.

    ``weights`` is the ``[agent.reward]`` table (per_handshake / time_penalty /
    idle_penalty). The idle penalty applies only when the window saw no
    discoverable APs on the chosen channel(s) - dwelling on dead air - never as
    a blanket cost, so the signal stays "almost pure yield"."""
    per = float((weights or {}).get("per_handshake", 1.0))
    time_penalty = float((weights or {}).get("time_penalty", 0.01))
    idle_penalty = float((weights or {}).get("idle_penalty", 0.05))
    value = per * max(0, int(new_unique_hashlines)) - time_penalty
    if not had_discoverable_aps:
        value -= idle_penalty
    return float(value)
