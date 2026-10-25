"""The Gymnasium environment wrapping the Phase 2 capture interface.

One ``step()`` is one decision window on the capture dongle: it decodes the
action into a scan posture, applies it through the Control surface (the
supervised launcher (re)starts AngryOxide with the posture's launch flags for
the window), waits ``decision_seconds``, then reads the window's yield from the
append-only store delta and the RF state from the live status, and returns
``(observation, reward, terminated, truncated, info)``. Everything
hardware-specific is behind the injected ``surface`` and ``status_source``, so
the algorithm is stock SB3 and this is what the unit tests drive against a
synthetic store (no radio).

The task is continuing - there is no terminal state on a device that just keeps
capturing - so ``terminated``/``truncated`` are always False; the controller
ends an episode by wall-clock, and the offline trainer replays fixed-length
chunks of the logged episodes.

gymnasium is imported at module load (not torch/SB3); it is a light pure-Python
dep already pulled in by stable-baselines3. The env unit tests ``importorskip``
it so they run wherever gymnasium is installed.
"""

from __future__ import annotations

import gymnasium as gym
import numpy as np
from gymnasium import spaces

from ..capture import parse_hashline
from . import spec
from .features import FeatureTracker, unique_hashlines


class CaptureEnv(gym.Env):
    """Gymnasium env over the capture interface. See the module docstring.

    Parameters
    ----------
    surface:
        The Control surface. Must provide ``supported_channels()`` -> list[int]
        and ``apply(plan, dwell, transmit)`` (plan is a dict from
        :func:`spec.posture_to_plan`). On a device this is the supervised
        launcher; a test passes a fake that records calls.
    capture:
        A :class:`~kestrel_pi.capture.CaptureInterface` over the store, for
        the unique-line yield delta.
    config:
        The merged ``[agent]`` config (with ``action``/``reward`` sub-tables and
        ``capture_bands`` = the operator's ``[capture.scan] bands`` for the
        sweep-band posture).
    status_source:
        Optional callable returning the live RF snapshot (see
        :mod:`.features`); None yields an all-zero RF observation.
    gps_source:
        Optional callable returning the latest GPS reading dict (from the
        cross-process seam the controller reads) or None; None (or a stale/
        absent fix) yields the GPS features zeroed with ``gps_valid = 0``.
    wait, clock:
        Injectable sleep/clock so tests don't actually wait a window.
    episode_logger:
        Optional callable ``(record_dict)`` appending one JSONL episode row.
    """

    metadata = {"render_modes": []}

    def __init__(self, surface, capture, config, status_source=None,
                 gps_source=None, wait=None, clock=None, episode_logger=None,
                 logger=None):
        super().__init__()
        self.surface = surface
        self.capture = capture
        self.config = config or {}
        self.status_source = status_source
        self.gps_source = gps_source
        self._wait = wait
        self._episode_logger = episode_logger
        self.log = logger

        supported = [int(c) for c in surface.supported_channels()]
        # Resolve the adapter's channels once, de-duplicated and sorted, capped
        # at MAX_CH so the observation shape is constant. A channel beyond the
        # cap is simply never offered to a posture.
        self.supported = sorted(dict.fromkeys(supported))[:spec.MAX_CH]

        low, high = spec.observation_bounds()
        self.observation_space = spaces.Box(low=low, high=high, dtype=np.float32)
        self.action_space = spaces.Discrete(spec.ACTION_N)

        self.tracker = FeatureTracker(clock=clock)
        self._weights = self.config.get("reward") or {}
        action_cfg = self.config.get("action") or {}
        self._lock_subset_n = int(action_cfg.get("lock_subset_n", 3) or 3)
        self._bands = str(self.config.get("capture_bands") or "both")
        self.decision_seconds = int(self.config.get("decision_seconds", 30) or 30)

        # Per-step scratch the controller may set (globals that aren't in the
        # snapshot): run-dir pressure 0..1 and the whitelist size.
        self.rundir_pressure = 0.0
        self.whitelist_size = len(self.config.get("whitelist") or [])

        self._ranking = list(self.supported)
        self._prev_lines = set()
        self._steps = 0
        self._last_obs = spec.empty_observation()

    # -- gym API ---------------------------------------------------------
    def reset(self, *, seed=None, options=None):
        super().reset(seed=seed)
        # Baseline the store so the first window's delta counts only new lines.
        self._prev_lines = unique_hashlines(self.capture)
        self._ranking = list(self.supported)
        self._steps = 0
        snapshot = self._snapshot()
        obs, info = self.tracker.build(
            snapshot, self.supported, {}, self.rundir_pressure,
            self.whitelist_size, gps=self._gps())
        self._ranking = info["channel_ranking"] or list(self.supported)
        self._last_obs = obs
        return obs, {}

    def step(self, action):
        posture, dwell, transmit = spec.decode(action)
        plan = spec.posture_to_plan(posture, self._ranking, self._bands,
                                    self._lock_subset_n)
        self.surface.apply(plan=plan, dwell=dwell, transmit=transmit)
        # Record which channels this window scans so their recency resets. A
        # band sweep / autohunt visits the whole supported set.
        if plan.get("kind") == "channels":
            self.tracker.note_visit(plan["channels"])
        else:
            self.tracker.note_visit(self.supported)

        self._sleep(self.decision_seconds)

        snapshot = self._snapshot()
        new_lines = self._new_unique_lines()
        yield_by_channel = self._attribute_yield(new_lines, snapshot)

        obs, info = self.tracker.build(
            snapshot, self.supported, yield_by_channel, self.rundir_pressure,
            self.whitelist_size, gps=self._gps())
        self._ranking = info["channel_ranking"] or list(self.supported)
        self.tracker.add_handshakes(len(new_lines))

        value = spec.reward(len(new_lines), info["had_aps"], self._weights)
        new_networks = self._distinct_bssids(new_lines)
        discoverable = int(info.get("discoverable", 0))
        self._steps += 1
        self._log_episode(action, posture, dwell, transmit, len(new_lines),
                          new_networks, discoverable, value, self._last_obs)
        self._last_obs = obs
        # Continuing task: never terminates or truncates from inside the env.
        return obs, value, False, False, {
            "posture": posture, "dwell": dwell, "transmit": transmit,
            "new_hashlines": len(new_lines), "new_networks": new_networks,
            "discoverable": discoverable, "channel_ranking": self._ranking,
        }

    # -- helpers ---------------------------------------------------------
    def _snapshot(self):
        if self.status_source is None:
            return None
        try:
            return self.status_source()
        except Exception:  # noqa: BLE001 - a status read must never kill the loop
            if self.log:
                self.log.exception("agent status source failed")
            return None

    def _gps(self):
        if self.gps_source is None:
            return None
        try:
            return self.gps_source()
        except Exception:  # noqa: BLE001 - a GPS read must never kill the loop
            if self.log:
                self.log.exception("agent gps source failed")
            return None

    def _new_unique_lines(self):
        current = unique_hashlines(self.capture)
        new = current - self._prev_lines
        self._prev_lines = current
        return new

    def _attribute_yield(self, new_lines, snapshot):
        """Map each new hashline to the channel its AP is currently on (from the
        snapshot). Unattributable lines still count in the reward (len), they
        just don't enrich a per-channel yield feature."""
        if not new_lines:
            return {}
        bssid_channel = {}
        for ap in (snapshot or {}).get("aps") or []:
            bssid = (ap.get("bssid") or "").upper()
            if bssid and ap.get("channel") is not None:
                bssid_channel[bssid] = ap["channel"]
        counts = {}
        for line in new_lines:
            event = parse_hashline(line)
            if event is None or not event.bssid:
                continue
            channel = bssid_channel.get(event.bssid.upper())
            if channel is not None:
                counts[channel] = counts.get(channel, 0) + 1
        return counts

    def _distinct_bssids(self, new_lines):
        """Distinct AP BSSIDs among this window's new hashlines (the per-window
        coverage signal the metrics module turns into networks/hour)."""
        bssids = set()
        for line in new_lines:
            event = parse_hashline(line)
            if event is not None and event.bssid:
                bssids.add(event.bssid.upper())
        return len(bssids)

    def _sleep(self, seconds):
        if self._wait is not None:
            self._wait(seconds)
        else:
            import time
            time.sleep(seconds)

    def _log_episode(self, action, posture, dwell, transmit, new_hashlines,
                     new_networks, discoverable, value, observation):
        if self._episode_logger is None:
            return
        try:
            self._episode_logger({
                "step": self._steps,
                "action": int(action),
                "posture": posture,
                "dwell": dwell,
                "transmit": transmit,
                "window_seconds": self.decision_seconds,
                "new_hashlines": int(new_hashlines),
                "new_networks": int(new_networks),
                "discoverable": int(discoverable),
                "reward": float(value),
                # The observation that led to this action, for offline training.
                "obs": [round(float(x), 5) for x in observation],
            })
        except Exception:  # noqa: BLE001 - logging must never break the loop
            if self.log:
                self.log.exception("episode log failed")
