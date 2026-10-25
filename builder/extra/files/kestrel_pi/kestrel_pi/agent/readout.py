"""Read the agent's ``status.json`` for the daemon's dashboard card.

The agent runs in its own process; the daemon just reads the small status file
it writes and renders a "Learning agent" card through the same shared UI element
model (``{label, value, kind}``) it uses for every plugin readout, so the card
draws like the others and the Phase 5 e-ink plugin renders it with no extra
agent code. This module is **stdlib-only** - no torch, no gymnasium, not even
the env spec - so the benign always-on daemon imports it with the heavy ML stack
absent. The daemon never imports the agent's env or policies.
"""

from __future__ import annotations

import json
import os
import time

from . import AGENT_DIR, STATUS_NAME

# The display name of the dashboard card (its widget-group key, rendered as the
# card title and used as the drag-reorder id).
CARD_NAME = "Learning agent"


def status_path(agent_dir=AGENT_DIR):
    return os.path.join(agent_dir, STATUS_NAME)


def read_status(agent_dir=AGENT_DIR):
    """The agent's status dict, or None if it has not written one (agent never
    started, or disabled). A malformed file reads as None rather than raising."""
    try:
        with open(status_path(agent_dir), "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _num(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _ago(ts, now=None):
    """A short relative time ("just now", "7m ago", "3h ago", "2d ago")."""
    now = time.time() if now is None else now
    delta = max(0.0, now - ts)
    if delta < 60:
        return "just now"
    if delta < 3600:
        return "%dm ago" % int(delta // 60)
    if delta < 86400:
        return "%dh ago" % int(delta // 3600)
    return "%dd ago" % int(delta // 86400)


def _dur(secs):
    """A short duration ("34s", "2m 05s")."""
    if secs < 60:
        return "%ds" % int(round(secs))
    minutes = int(secs // 60)
    return "%dm %02ds" % (minutes, int(round(secs - minutes * 60)))


def widget_elements(status):
    """The ``[{name,label,value,kind}, ...]`` for the Learning agent card, from
    a status dict (see :mod:`.controller`). Returns an empty list for a missing
    status so the card simply doesn't show when the agent has never run."""
    if not status:
        return []
    elements = [
        {"name": "state", "label": "State",
         "value": "running" if status.get("running") else "stopped", "kind": "text"},
        {"name": "policy", "label": "Policy",
         "value": status.get("policy") or "baseline", "kind": "text"},
        {"name": "algorithm", "label": "Algorithm",
         "value": status.get("algorithm") or "-", "kind": "text"},
    ]
    # Whether the learned model in use was trained on-device or pulled from the
    # remote training host (only when a learned model is actually driving).
    source = status.get("model_source")
    if status.get("policy") == "learned" and source:
        label = {"local": "On-device", "remote": "Remote host"}.get(source, source)
        elements.append({"name": "model_source", "label": "Model source",
                         "value": label, "kind": "text"})
    # Lifetime tally of which side won: how many times an on-device trained model
    # vs a promoted remote model became the active model. Shown once either side
    # has happened, so a device that only trains locally still sees its count.
    tally = status.get("promotion_tally") or {}
    local_n = int(_num(tally.get("local")) or 0)
    remote_n = int(_num(tally.get("remote")) or 0)
    if local_n or remote_n:
        elements.append({"name": "promotion_tally", "label": "Promotions",
                         "value": "%d on-device / %d remote" % (local_n, remote_n),
                         "kind": "text"})
    # On-device idle training: show it live while a run is in flight, otherwise
    # when it last ran and how long that run took (both persist across restarts).
    if status.get("training"):
        elements.append({"name": "training", "label": "Training",
                         "value": "in progress…", "kind": "text"})
    else:
        last_trained = _num(status.get("last_trained"))
        if last_trained is not None:
            elements.append({"name": "last_trained", "label": "Last trained",
                             "value": _ago(last_trained), "kind": "text"})
            secs = _num(status.get("last_train_secs"))
            if secs is not None:
                elements.append({"name": "last_train_secs",
                                 "label": "Last train time",
                                 "value": _dur(secs), "kind": "text"})
    posture = status.get("posture")
    if posture:
        dwell = status.get("dwell")
        transmit = status.get("transmit")
        shown = posture
        if dwell is not None:
            shown += " @ %ss" % dwell
        if transmit:
            shown += " / " + transmit
        elements.append({"name": "posture", "label": "Posture",
                         "value": shown, "kind": "text"})
    last_reward = _num(status.get("last_reward"))
    if last_reward is not None:
        elements.append({"name": "last_reward", "label": "Last reward",
                         "value": "%.2f" % last_reward, "kind": "number"})
    elements.append({"name": "session_yield", "label": "Session handshakes",
                     "value": str(int(status.get("session_yield") or 0)),
                     "kind": "number"})
    elements.append({"name": "decisions", "label": "Decisions",
                     "value": str(int(status.get("decisions") or 0)),
                     "kind": "number"})

    # Agent-vs-sweep comparison. Yield rate (handshakes/hour) is the headline
    # number, shown plainly per policy; the window hit rate (0..1, the fraction
    # of decision windows that produced a handshake) is a natural percent, so it
    # draws a core bar. Only policies with recorded windows appear.
    metrics = status.get("metrics") or {}
    for policy in ("baseline", "learned"):
        block = metrics.get(policy) or {}
        rate = _num(block.get("yield_per_hour"))
        if rate is None:
            continue
        elements.append({"name": "yield_" + policy,
                         "label": "%s h/hr" % policy,
                         "value": "%g" % round(rate, 2), "kind": "number"})
        hit = _num(block.get("hit_rate"))
        if hit is not None:
            elements.append({"name": "hit_" + policy,
                             "label": "%s win rate" % policy,
                             "value": "%.0f%%" % (max(0.0, min(1.0, hit)) * 100.0),
                             "kind": "percent"})
    detail = status.get("detail")
    if detail:
        elements.append({"name": "detail", "label": "Note",
                         "value": detail, "kind": "text"})
    return elements
