"""The agent-vs-sweep comparison, from the per-window episode log.

"Does the learning agent beat the scripted sweep?" has to be answerable with
numbers, computed honestly on one radio. Every metric here is derived from the
same episode log the controller already writes (which records the policy that
drove each window), so nothing new is instrumented and the figures reconcile
with the reward signal. The controller writes a rolling summary into
``status.json`` for the dashboard card; ``kestrel agent metrics`` prints
the full table; and because they come from the logs, the exact same numbers can
be recomputed off-device after a session for the Gate 4 record.

Pure stdlib. Fair-comparison method: because the device has one capture radio
and a mobile RF world drifts, the honest A/B is **interleaved blocks within one
session** (``interleave_compare``), attributed per window by the ``policy`` the
log records; two separate runs are never compared head-to-head.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import statistics


def load_rows(episodes_dir):
    """Every episode row across the JSONL files in ``episodes_dir``, oldest
    first. A malformed line is skipped, not fatal."""
    rows = []
    try:
        names = sorted(n for n in os.listdir(episodes_dir) if n.endswith(".jsonl"))
    except OSError:
        return rows
    for name in names:
        path = os.path.join(episodes_dir, name)
        try:
            with open(path, "r", encoding="utf-8") as handle:
                for line in handle:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        row = json.loads(line)
                    except ValueError:
                        continue
                    if isinstance(row, dict):
                        rows.append(row)
        except OSError:
            continue
    return rows


def _row_ts(row):
    try:
        return float(row.get("ts", 0) or 0)
    except (TypeError, ValueError):
        return 0.0


def data_watermark(rows):
    """The high-water mark of the episode data in ``rows``: the newest episode
    timestamp and the row count. Stamped into a trained model's sidecar so the
    model carries "the newest experience I learned from", which is what lets a
    pulled remote model be compared against the on-device one by *what data it
    learned from*, not by which file is newer. Pure stdlib.

    Returns ``{"latest_ts": float, "episodes": int}`` (0/0 for no rows)."""
    latest = 0.0
    for row in rows:
        ts = _row_ts(row)
        if ts > latest:
            latest = ts
    return {"latest_ts": latest, "episodes": len(rows)}


def meta_watermark(meta):
    """``(latest_ts, episodes)`` from a model sidecar's ``data_watermark`` block,
    defaulting to ``(0.0, 0)`` for a model trained before watermarks existed (so
    it compares as the oldest possible data)."""
    wm = (meta or {}).get("data_watermark") or {}
    try:
        latest = float(wm.get("latest_ts", 0) or 0)
    except (TypeError, ValueError):
        latest = 0.0
    try:
        episodes = int(wm.get("episodes", 0) or 0)
    except (TypeError, ValueError):
        episodes = 0
    return latest, episodes


def new_rows_since(rows, latest_ts):
    """How many episode rows are newer than ``latest_ts`` (a model's watermark).
    This is the "new experience since this model was trained" the remote-train
    data-driven trigger measures against its threshold."""
    try:
        cutoff = float(latest_ts or 0)
    except (TypeError, ValueError):
        cutoff = 0.0
    return sum(1 for r in rows if _row_ts(r) > cutoff)


# -- the held-out set no model trains on ---------------------------------
# A row is held out when its hour block hashes into one bucket of five (about
# 20% of rows). Deterministic, so the device and a remote training host agree
# without talking; stable as old episode files rotate out; and whole hours, so a
# recurrent model is scored on real sequences. Training (``train.train_from_logs``)
# never sees a held-out row, and promotion scores every candidate on them.
HOLDOUT_SCHEME = "block-hash-v1"
HOLDOUT_BLOCK_SECONDS = 3600
HOLDOUT_BUCKETS = 5


def holdout_block(ts):
    """The hour block a timestamp falls in."""
    return int(float(ts) // HOLDOUT_BLOCK_SECONDS)


def is_holdout(row):
    """Whether ``row`` belongs to the held-out set. A row with no usable ``ts``
    is never held out (it goes to training)."""
    try:
        block = holdout_block(row.get("ts"))
    except (TypeError, ValueError):
        return False
    digest = hashlib.sha256(b"kestrel-holdout-v1:%d" % block).hexdigest()
    return int(digest, 16) % HOLDOUT_BUCKETS == 0


def split_holdout(rows):
    """``(train_rows, holdout_rows)``, each in the original order."""
    train, held = [], []
    for row in rows:
        (held if is_holdout(row) else train).append(row)
    return train, held


def model_fingerprint(meta):
    """A short identity for a trained model from its sidecar (algorithm, data
    watermark and training time), so "the same model" is recognised across
    pulls without hashing the model file. "" for a missing sidecar."""
    if not isinstance(meta, dict) or not meta:
        return ""
    latest, episodes = meta_watermark(meta)
    return "%s|%.3f|%d|%s" % (meta.get("algorithm") or "", latest, episodes,
                              meta.get("train_seconds"))


# The smallest held-out difference that counts as real rather than reward-model
# noise: for clearing the baseline and for the challenger's mean improvement
# over the incumbent. Reward magnitudes here are O(0.01-1), so 1e-3 is well
# below a genuine improvement while rejecting float dust.
PROMOTION_ABS_FLOOR = 1e-3


def contest_decision(diffs, challenger_lift, incumbent_lift, margin=0.10,
                     min_z=1.0, abs_floor=PROMOTION_ABS_FLOOR,
                     has_incumbent=True, new_since=None, max_stale_episodes=0):
    """Whether a candidate model (the challenger) should replace the active one
    (the incumbent). The same rule for every candidate, whatever trained it.
    Pure stdlib, so it is unit-tested directly.

    ``diffs`` are the per-row differences ``r̂(o, challenger) - r̂(o, incumbent)``
    over the held-out evaluation rows; the lifts are each model's mean reward
    over the scripted baseline's on those rows. Returns ``(promote, reason)``:

    - **Stale:** more than ``max_stale_episodes`` rows logged since the
      challenger's data (``new_since``) rejects it. ``0`` turns the guard off.
    - **Baseline:** the challenger must beat the scripted baseline by
      ``abs_floor``.
    - **No incumbent:** then it is adopted (bootstrap).
    - **Paired test:** the mean difference must be at least ``abs_floor`` and
      significant, ``mean / standard error >= min_z``.
    - **Margin:** its lift must be at least ``(1 + margin)`` times the
      incumbent's, so two near-equal models don't swap on every run.
    """
    if (max_stale_episodes and new_since is not None
            and new_since > max_stale_episodes):
        return False, ("challenger data too stale: %d rows logged since it trained"
                       % new_since)
    if challenger_lift < abs_floor:
        return False, ("does not beat the scripted baseline (lift %+.4f)"
                       % challenger_lift)
    if not has_incumbent:
        return True, "no active model; adopting (bootstrap)"
    diffs = [float(d) for d in diffs or ()]
    if not diffs:
        return False, "no held-out rows to compare on"
    mean = statistics.fmean(diffs)
    if mean < abs_floor:
        return False, "not better than the active model (mean diff %+.4f)" % mean
    stdev = statistics.pstdev(diffs) if len(diffs) > 1 else 0.0
    z = (mean / (stdev / math.sqrt(len(diffs)))) if stdev > 0 else float("inf")
    if z < min_z:
        return False, "not significant (mean diff %+.4f, z %.2f)" % (mean, z)
    if incumbent_lift > 0 and challenger_lift < (1.0 + margin) * incumbent_lift:
        return False, ("lift %+.4f is within the %.0f%% margin of the active "
                       "model's %+.4f" % (challenger_lift, margin * 100,
                                          incumbent_lift))
    return True, ("challenger wins: mean diff %+.4f, z %.2f, lift %+.4f vs %+.4f"
                  % (mean, z, challenger_lift, incumbent_lift))


def _policy_summary(rows):
    windows = len(rows)
    if not windows:
        return None
    handshakes = sum(int(r.get("new_hashlines", 0) or 0) for r in rows)
    networks = sum(int(r.get("new_networks", 0) or 0) for r in rows)
    discoverable = sum(int(r.get("discoverable", 0) or 0) for r in rows)
    hits = sum(1 for r in rows if int(r.get("new_hashlines", 0) or 0) >= 1)
    seconds = sum(int(r.get("window_seconds", 0) or 0) for r in rows)
    hours = seconds / 3600.0 if seconds else 0.0
    return {
        "windows": windows,
        "handshakes": handshakes,
        "networks": networks,
        "hit_rate": hits / windows,
        "yield_per_hour": (handshakes / hours) if hours else 0.0,
        "coverage_per_hour": (networks / hours) if hours else 0.0,
        # Opportunity-normalized: handshakes per discoverable (non-whitelisted)
        # AP-window, so a dense site doesn't flatter a policy and a sparse one
        # doesn't punish it.
        "opportunity_yield": (handshakes / discoverable) if discoverable else 0.0,
    }


def summarize(rows, window_minutes=None, now=None):
    """Per-policy metrics over the rolling ``window_minutes`` (None = all rows).

    Returns ``{"window_minutes": N, "baseline": {...}, "learned": {...}}`` with
    only the policies that have windows in range."""
    if window_minutes:
        if now is None:
            import time
            now = time.time()
        cutoff = now - window_minutes * 60
        rows = [r for r in rows if float(r.get("ts", 0) or 0) >= cutoff]
    out = {"window_minutes": window_minutes}
    for policy in ("baseline", "learned"):
        block = _policy_summary([r for r in rows if r.get("policy") == policy])
        if block is not None:
            out[policy] = block
    return out


def format_table(summary):
    """A plain-text agent-vs-sweep table for ``kestrel agent metrics``."""
    labels = [
        ("windows", "decision windows", "%d"),
        ("handshakes", "new handshakes", "%d"),
        ("yield_per_hour", "handshakes / hour", "%.2f"),
        ("coverage_per_hour", "new networks / hour", "%.2f"),
        ("hit_rate", "window hit rate", "%.0f%%"),
        ("opportunity_yield", "handshakes / AP-window", "%.3f"),
    ]
    policies = [p for p in ("baseline", "learned") if p in summary]
    lines = []
    window = summary.get("window_minutes")
    lines.append("agent vs sweep%s:" %
                 (" (last %d min)" % window if window else " (all logged)"))
    if not policies:
        lines.append("  no decision windows recorded yet")
        return "\n".join(lines)
    header = "  %-24s" % "metric" + "".join("%12s" % p for p in policies)
    lines.append(header)
    for key, label, fmt in labels:
        cells = []
        for policy in policies:
            value = summary[policy].get(key)
            if value is None:
                cells.append("%12s" % "-")
            elif key == "hit_rate":
                cells.append("%12s" % (fmt % (value * 100.0)))
            else:
                cells.append("%12s" % (fmt % value))
        lines.append("  %-24s" % label + "".join(cells))
    return "\n".join(lines)
