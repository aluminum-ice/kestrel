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

import json
import os


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


def remote_model_wins(remote_meta, local_meta):
    """Whether a pulled remote model is worth *staging* for evaluation, comparing
    their data watermarks (not file mtimes): the remote clears the bar only when
    it was trained on data at least as recent as the local model's. A tie favors
    the remote; an older remote watermark (a stale remote, while the device
    trained locally in the meantime) does not. A model with no watermark compares
    as the oldest data.

    Quality-aware promotion (docs/developer/agent-interface.md) uses this as
    the ``remote-train`` plugin's cheap, torch-free **pre-filter** - whether to
    bother pulling at all. It is no longer the promotion decider; the agent
    re-scores both models on a held-out slice (:func:`promotion_decision`)."""
    remote_ts, _ = meta_watermark(remote_meta)
    local_ts, _ = meta_watermark(local_meta)
    return remote_ts >= local_ts


# The smallest held-out-score lift (over baseline) that counts as a real
# difference rather than reward-model noise, both for clearing the baseline floor
# and for the absolute-improvement guard that stops two near-equal models
# flip-flopping each pull. Reward magnitudes here are O(0.01-1), so 1e-3 is
# comfortably below a genuine improvement while rejecting float dust.
PROMOTION_ABS_FLOOR = 1e-3


def promotion_decision(scores, has_local, new_since_remote=0, margin=0.10,
                       max_stale_episodes=0, abs_floor=PROMOTION_ABS_FLOOR):
    """Decide whether to promote a staged remote model, given held-out
    ``scores`` (``{"remote", "local", "baseline"}`` -> mean greedy r̂ from
    :func:`train.score_candidates`). Pure (no torch/numpy), so it is unit-tested.

    Returns ``(promote: bool, reason: str)``. The env-spec gate runs earlier (the
    caller disqualifies a spec mismatch before scoring). The rule:

    - **No incumbent** (``has_local`` false): adopt the remote - the only way a
      board that cannot train locally (a Zero 2 W) ever gets a model. Judged
      before scoring, so it holds even on a log too thin to score.
    - **Staleness guard:** if the device has logged more than
      ``max_stale_episodes`` new episodes since the remote's data watermark
      (``new_since_remote``), keep the incumbent - don't adopt a model trained on
      data the device has since moved far past on the strength of a held-out win.
      ``0`` disables the guard.
    - **Baseline floor:** the remote must beat the scripted baseline's held-out
      score by at least ``abs_floor``; otherwise keep the incumbent.
    - **Margin:** promote only when the remote's lift over baseline is at least
      ``(1 + margin)`` x the local's lift *and* exceeds it by at least
      ``abs_floor`` (the absolute guard stops churn when both lifts are ~0).
      Measuring lift over baseline keeps the test robust to reward scale/offset.
    """
    if not has_local:
        return True, "no local model yet; adopting the remote (bootstrap)"
    remote = scores.get("remote") if scores else None
    if remote is None:
        return False, "no remote score"
    if (max_stale_episodes and new_since_remote is not None
            and new_since_remote > max_stale_episodes):
        return False, ("remote data too stale: %d new episodes since it trained"
                       % new_since_remote)
    baseline = scores.get("baseline")
    local = scores.get("local")
    if baseline is None or local is None:
        return False, "incomplete scores; keeping the local model"
    remote_lift = remote - baseline
    local_lift = local - baseline
    if remote_lift <= abs_floor:
        return False, "remote does not beat the scripted baseline"
    if (remote_lift >= (1.0 + margin) * local_lift
            and remote_lift - local_lift >= abs_floor):
        return True, ("remote wins: lift %.4f vs local %.4f over baseline "
                      "(>= %.0f%% margin)" % (remote_lift, local_lift,
                                              margin * 100))
    return False, ("local model is as good or better: lift %.4f vs remote %.4f"
                   % (local_lift, remote_lift))


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
