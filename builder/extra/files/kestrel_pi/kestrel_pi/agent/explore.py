"""Exploration and the allowed action set.

A learned policy acts greedily, so on its own it only ever runs the action it
already prefers and the episode logs never learn anything about the others.
:class:`Explorer` makes a small share of the learned policy's decision windows
run a different action instead, favouring the actions tried least, so the
reward model gets data for every action the operator allows. Those windows are
logged with ``explore: true`` and the probability their action had, and the
live metrics keep them apart from the policy's own figures.

The allowed action set comes from ``[agent.action]`` (``postures``,
``dwell_buckets``, ``transmit_modes``). Exploration picks only from it, and the
controller maps every other action it runs (the learned policy's, the
baseline's) to the nearest allowed one with :func:`nearest_allowed`.

Stdlib + :mod:`.spec` only: no torch, so the controller and its tests use it
without the ML stack.
"""

from __future__ import annotations

import math
import random

from . import spec

# Bounds for the operator's settings; values outside are clamped.
MAX_RATE = 0.25
MAX_WINDOWS = 4


def _listed(value, vocabulary, cast):
    """The entries of a configured list that are in ``vocabulary``, in the
    vocabulary's order. A missing or non-list value allows the whole
    vocabulary; a list with nothing valid allows nothing."""
    if not isinstance(value, (list, tuple)):
        return list(vocabulary)
    wanted = set()
    for item in value:
        try:
            wanted.add(cast(item))
        except (TypeError, ValueError):
            continue
    return [v for v in vocabulary if v in wanted]


def allowed_actions(action_cfg, fallback):
    """The sorted ``Discrete`` indices ``[agent.action]`` allows: every
    posture x dwell x transmit combination whose parts are all listed. When
    the lists allow nothing, only ``fallback`` (the baseline's action)."""
    action_cfg = action_cfg if isinstance(action_cfg, dict) else {}
    postures = _listed(action_cfg.get("postures"), spec.POSTURES, str)
    dwells = _listed(action_cfg.get("dwell_buckets"), spec.DWELLS, int)
    modes = _listed(action_cfg.get("transmit_modes"), spec.TRANSMIT, str)
    actions = sorted(spec.encode(p, d, t)
                     for p in postures for d in dwells for t in modes)
    return actions or [int(fallback)]


def nearest_allowed(action, allowed):
    """``action`` if it is allowed, else the allowed action closest to it: the
    same posture and transmit mode where possible, then the nearest dwell
    (ties to the lower index)."""
    action = int(action)
    if action in allowed:
        return action
    posture, dwell, transmit = spec.decode(action)

    def distance(candidate):
        p, d, t = spec.decode(candidate)
        return (p != posture, t != transmit, abs(d - dwell), candidate)

    return min(allowed, key=distance)


def clamp_rate(value):
    try:
        rate = float(value)
    except (TypeError, ValueError):
        return 0.0
    if math.isnan(rate):
        return 0.0
    return min(MAX_RATE, max(0.0, rate))


def clamp_windows(value):
    try:
        windows = int(value)
    except (TypeError, ValueError):
        return 1
    return min(MAX_WINDOWS, max(1, windows))


class Explorer:
    """Decides, window by window, whether the learned policy's action runs or
    an exploring one does.

    ``rate`` is the chance a window starts an exploration; ``windows`` is how
    many consecutive windows an exploring action then holds (a relaunch starts
    the scan part-way into its first window, so a second window gives a
    cleaner reading). ``counts`` maps an action to how often it has been
    logged; it is read lazily through ``load_counts`` the first time it is
    needed and kept current with :meth:`note`."""

    def __init__(self, rate, windows, allowed, load_counts=None, rng=None):
        self.rate = clamp_rate(rate)
        self.windows = clamp_windows(windows)
        self.allowed = list(allowed)
        self._load_counts = load_counts
        self._counts = None
        self._rng = rng or random.Random()
        self._held = None           # (action, probability) being held
        self._left = 0              # windows left to hold it

    @property
    def enabled(self):
        return self.rate > 0 and len(self.allowed) > 1

    def counts(self):
        if self._counts is None:
            loaded = {}
            if self._load_counts is not None:
                try:
                    loaded = dict(self._load_counts() or {})
                except Exception:  # noqa: BLE001 - counts are a preference only
                    loaded = {}
            self._counts = {int(a): int(n) for a, n in loaded.items()}
        return self._counts

    def note(self, action):
        """Count one logged window of ``action`` (any policy)."""
        if self._counts is None:
            return              # the lazy load will include it
        action = int(action)
        self._counts[action] = self._counts.get(action, 0) + 1

    def tried(self):
        """How many allowed actions have been logged at least once."""
        counts = self.counts()
        return sum(1 for a in self.allowed if counts.get(a, 0) > 0)

    def reset(self):
        """Drop a held exploration (the policy changed or capture restarted)."""
        self._held = None
        self._left = 0

    def choose(self, policy_action):
        """``(action, probability)`` to run instead of ``policy_action`` this
        window, or None to run the policy's own action. ``probability`` is the
        chance this action was drawn when the exploration started."""
        if self._held is not None and self._left > 0:
            self._left -= 1
            held = self._held
            if self._left == 0:
                self._held = None
            return held
        self._held = None
        if not self.enabled or self._rng.random() >= self.rate:
            return None
        policy_action = int(policy_action)
        candidates = [a for a in self.allowed if a != policy_action]
        if not candidates:
            return None
        counts = self.counts()
        weights = [1.0 / math.sqrt(1.0 + counts.get(a, 0)) for a in candidates]
        total = sum(weights)
        pick = self._rng.random() * total
        index = len(candidates) - 1
        for i, weight in enumerate(weights):
            pick -= weight
            if pick < 0:
                index = i
                break
        chosen = (candidates[index], round(self.rate * weights[index] / total, 6))
        if self.windows > 1:
            self._held = chosen
            self._left = self.windows - 1
        return chosen
