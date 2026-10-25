"""Build the observation vector and read the store yield.

Both the observation (per-channel RF + mobility/dynamics + globals) and the
reward's yield come from the two stable sources the capture contract tells
consumers to prefer: the append-only store's unique-line delta (the Output
surface) and AngryOxide's best-effort live status (the Status surface). Nothing
here parses AngryOxide's TUI, and nothing is hardware-specific - a test feeds a
synthetic store and a synthetic status snapshot, exactly as Gate-free unit tests
require. Pure stdlib + numpy (no gymnasium/torch).

A *snapshot* is the normalized live RF state the controller assembles from
whatever the Status surface offers (and a test provides directly):

    {"aps": [{"bssid": str, "channel": int, "signal": dbm,
              "clients": int, "whitelisted": bool}, ...]}

The channel shape is not frozen by the contract, so a missing or empty snapshot
just yields an all-zero RF observation; the store delta still drives the reward.
"""

from __future__ import annotations

import math

from . import spec

# Below this per-window displacement (metres) the heading is left unchanged:
# a near-stationary fix wanders by GPS noise, so re-estimating a bearing from it
# would manufacture spurious turn_rate. Speed is still derived from the (tiny)
# displacement, so a parked device reads speed ~0 and turn_rate 0.
_GPS_MIN_MOVE_M = 1.0
_EARTH_RADIUS_M = 6371000.0


def _haversine_m(lat1, lon1, lat2, lon2):
    """Great-circle distance in metres between two lat/lon points (degrees)."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlmb = math.radians(lon2 - lon1)
    a = (math.sin(dphi / 2.0) ** 2
         + math.cos(p1) * math.cos(p2) * math.sin(dlmb / 2.0) ** 2)
    return 2.0 * _EARTH_RADIUS_M * math.asin(min(1.0, math.sqrt(a)))


def _initial_bearing(lat1, lon1, lat2, lon2):
    """Initial great-circle bearing in degrees (0..360) from point 1 to 2."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dlmb = math.radians(lon2 - lon1)
    y = math.sin(dlmb) * math.cos(p2)
    x = (math.cos(p1) * math.sin(p2)
         - math.sin(p1) * math.cos(p2) * math.cos(dlmb))
    return (math.degrees(math.atan2(y, x)) + 360.0) % 360.0


def _bearing_delta(a, b):
    """Absolute smallest angle (degrees, 0..180) between two bearings."""
    d = abs(a - b) % 360.0
    return min(d, 360.0 - d)


def unique_hashlines(capture):
    """The set of unique non-blank hashlines across the whole store.

    The store is exact-line-deduped per file by the Phase 2 collector, but a
    line can appear under two filenames (per-ssid vs bssid-named), so this
    de-dupes across files too - the "new unique" the reward and Gate 4 count."""
    lines = set()
    for path in capture.hashline_files():
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as handle:
                for line in handle:
                    line = line.strip()
                    if line:
                        lines.add(line)
        except OSError:
            continue
    return lines


class FeatureTracker:
    """Carries the small amount of cross-window state the observation needs:
    per-channel last-visit times (recency), the previous snapshot's BSSID
    signals (churn/drift), and the running session handshake total. The env
    holds one; a recurrent policy still gets the raw features each step and
    learns the longer-range history itself."""

    def __init__(self, clock=None):
        self._clock = clock
        self._last_visit = {}        # channel -> timestamp last visited
        self._prev_signals = {}      # bssid -> signal dbm (previous snapshot)
        self.session_handshakes = 0
        # Previous *valid* GPS fix, for deriving speed/turn across windows:
        # {"lat", "lon", "bearing" (or None), "t"}. Cleared whenever a window
        # has no valid fix, so the next fix after a gap starts fresh (no
        # spurious speed across the gap).
        self._prev_fix = None

    def _now(self):
        if self._clock is not None:
            return self._clock()
        import time
        return time.monotonic()

    def note_visit(self, channels):
        """Record that these channels were just scanned (resets their recency)."""
        now = self._now()
        for channel in channels or ():
            self._last_visit[channel] = now

    def add_handshakes(self, count):
        self.session_handshakes += max(0, int(count))

    def build(self, snapshot, supported, yield_by_channel, rundir_pressure,
              whitelist_size, gps=None):
        """Return ``(observation, info)``.

        ``snapshot`` is the normalized live RF state (or None). ``supported`` is
        the adapter's channel list. ``yield_by_channel`` maps a channel to the
        new unique hashlines attributed to it this window. ``gps`` is the latest
        GPS reading the controller read from the cross-process seam (or None);
        see :meth:`_gps_features`. ``info`` carries ``channel_ranking`` (for the
        next step's lock/pin postures) and ``had_aps`` (any discoverable,
        non-whitelisted AP seen), which the reward's idle penalty uses."""
        channel_index = {ch: i for i, ch in enumerate(supported)}
        now = self._now()
        aps = (snapshot or {}).get("aps") or []

        per_channel = {}
        signals_now = {}
        discoverable = 0
        for ap in aps:
            channel = ap.get("channel")
            if channel is None:
                continue
            slot = per_channel.setdefault(
                channel, {"_aps": 0, "_clients": 0, "_sig": []})
            slot["_aps"] += 1
            slot["_clients"] += int(ap.get("clients", 0) or 0)
            sig = ap.get("signal")
            if sig is not None:
                slot["_sig"].append(float(sig))
                bssid = ap.get("bssid")
                if bssid:
                    signals_now[bssid] = float(sig)
            if not ap.get("whitelisted"):
                discoverable += 1

        feats = {}
        import statistics
        for channel, slot in per_channel.items():
            sigs = slot["_sig"]
            sig_mean = spec.norm_signal(statistics.fmean(sigs)) if sigs else 0.0
            sig_var = 0.0
            if len(sigs) > 1:
                # Variance of the normalized signals, itself in 0..1 territory.
                normed = [spec.norm_signal(s) for s in sigs]
                sig_var = min(1.0, statistics.pvariance(normed) * 4.0)
            last = self._last_visit.get(channel)
            recency = 1.0 if last is None else min(
                1.0, max(0.0, (now - last) / spec.NORM_RECENCY_S))
            feats[channel] = {
                "aps": slot["_aps"] / spec.NORM_APS,
                "clients": slot["_clients"] / spec.NORM_CLIENTS,
                "yield": (yield_by_channel or {}).get(channel, 0) / spec.NORM_YIELD,
                "recency": recency,
                "sig_mean": sig_mean,
                "sig_var": sig_var,
            }

        churn, drift = self._mobility(signals_now)
        self._prev_signals = signals_now

        globals_ = {
            "churn": churn,
            "drift": drift,
            "session": self.session_handshakes / spec.NORM_SESSION,
            "rundir": rundir_pressure,
            "whitelist": whitelist_size / spec.NORM_WHITELIST,
        }
        globals_.update(self._gps_features(gps, now))
        observation = spec.pack_observation(feats, globals_, channel_index)
        ranking = spec.rank_channels(feats, supported)
        return observation, {"channel_ranking": ranking,
                             "had_aps": discoverable > 0,
                             "discoverable": discoverable}

    def _mobility(self, signals_now):
        """``(churn, drift)`` vs the previous snapshot: churn is the fraction of
        BSSIDs that are new, drift is the mean absolute signal change of BSSIDs
        seen in both (normalized). Both read 0 on the first snapshot."""
        prev = self._prev_signals
        if not signals_now:
            return 0.0, 0.0
        new = sum(1 for b in signals_now if b not in prev)
        churn = new / len(signals_now)
        common = [b for b in signals_now if b in prev]
        if common:
            span = spec.SIG_MAX_DBM - spec.SIG_MIN_DBM
            drift = sum(abs(signals_now[b] - prev[b]) for b in common) / len(common)
            drift = min(1.0, drift / span) if span else 0.0
        else:
            drift = 0.0
        return min(1.0, churn), drift

    def _gps_features(self, gps, now):
        """The GPS motion + fix-quality globals for this window, all 0..1.

        ``gps`` is the latest reading (``{"fix", "lat", "lon", "sats", "hdop",
        "cn0", ...}``) or None. A missing, unlocked (``fix`` < 2) or
        coordinate-less reading is *no GPS*: every feature zero, ``gps_valid``
        0, and the previous fix is cleared so the next lock starts fresh.
        ``speed``/``turn_rate`` are derived here from successive fixes over the
        window delta (``now`` is this window's clock reading), so the feature
        works with a GGA-only serial receiver that reports no speed. The
        fix-quality block is a direct normalization of the reported satellite
        count, HDOP and mean C/N0 - the open-sky vs enclosed proxy."""
        zero = {"gps_valid": 0.0, "speed": 0.0, "turn_rate": 0.0,
                "sat_count": 0.0, "fix_accuracy": 0.0, "signal_strength": 0.0}
        mode = (gps or {}).get("fix")
        lat = (gps or {}).get("lat")
        lon = (gps or {}).get("lon")
        if not gps or lat is None or lon is None or (mode or 0) < 2:
            self._prev_fix = None
            return zero

        speed = turn = 0.0
        bearing = None
        prev = self._prev_fix
        if prev is not None:
            dt = now - prev["t"]
            if dt > 0:
                dist = _haversine_m(prev["lat"], prev["lon"], lat, lon)
                speed = dist / dt
                if dist >= _GPS_MIN_MOVE_M:
                    bearing = _initial_bearing(prev["lat"], prev["lon"], lat, lon)
                    if prev["bearing"] is not None:
                        turn = _bearing_delta(bearing, prev["bearing"]) / dt
                else:
                    bearing = prev["bearing"]   # too little movement to re-estimate
        self._prev_fix = {"lat": lat, "lon": lon, "bearing": bearing, "t": now}

        sats = (gps or {}).get("sats")
        hdop = (gps or {}).get("hdop")
        cno = (gps or {}).get("cn0")
        cno_span = spec.NORM_CNO_MAX - spec.NORM_CNO_MIN
        return {
            "gps_valid": 1.0,
            "speed": min(1.0, speed / spec.NORM_SPEED_MS),
            "turn_rate": min(1.0, turn / spec.NORM_TURN_RATE),
            "sat_count": min(1.0, (float(sats) if sats is not None else 0.0)
                             / spec.NORM_SATS),
            "fix_accuracy": (max(0.0, 1.0 - float(hdop) / spec.NORM_HDOP_MAX)
                             if hdop is not None else 0.0),
            "signal_strength": (((float(cno) - spec.NORM_CNO_MIN) / cno_span)
                                if cno is not None and cno_span else 0.0),
        }
