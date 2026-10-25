"""The GPS observation block (kestrel_pi.agent.features, Phase 4.8).

The FeatureTracker derives the motion block (gps_valid, speed, turn_rate) from
successive fixes and normalizes the fix-quality block (sat_count, fix_accuracy,
signal_strength) straight from a reading, and zeroes the whole block with
gps_valid = 0 when there is no usable fix. Pure stdlib + numpy (no gymnasium /
torch), so this runs in CI with the heavy stack absent.
"""

import pytest

from kestrel_pi.agent import spec
from kestrel_pi.agent.features import FeatureTracker


def _tracker(times):
    """A tracker whose clock returns ``times`` in order (one per build call)."""
    it = iter(times)
    return FeatureTracker(clock=lambda: next(it))


def _gps_globals(tracker, gps, supported=(1, 6, 11)):
    """Build once with no RF snapshot and return the GPS globals from the obs."""
    obs, _info = tracker.build(None, list(supported), {}, 0.0, 0, gps=gps)
    gbase = spec.MAX_CH * len(spec.PER_CH_FEATURES)
    return {key: float(obs[gbase + spec.GLOBAL_FEATURES.index(key)])
            for key in ("gps_valid", "speed", "turn_rate",
                        "sat_count", "fix_accuracy", "signal_strength")}


def test_no_reading_zeroes_the_gps_block():
    g = _gps_globals(_tracker([100.0]), None)
    assert g == {"gps_valid": 0.0, "speed": 0.0, "turn_rate": 0.0,
                 "sat_count": 0.0, "fix_accuracy": 0.0, "signal_strength": 0.0}


def test_unlocked_fix_reads_as_no_gps():
    # A reading with fix mode < 2 (no lock) is no GPS, even with coordinates.
    g = _gps_globals(_tracker([100.0]),
                     {"fix": 1, "lat": 40.0, "lon": -73.0, "sats": 3})
    assert g["gps_valid"] == 0.0 and g["sat_count"] == 0.0


def test_fix_quality_normalizes_sats_hdop_and_cn0():
    g = _gps_globals(_tracker([100.0]),
                     {"fix": 3, "lat": 40.0, "lon": -73.0,
                      "sats": 6, "hdop": 1.0, "cn0": 40.0})
    assert g["gps_valid"] == 1.0
    assert g["sat_count"] == pytest.approx(6 / spec.NORM_SATS)        # 0.5
    assert g["fix_accuracy"] == pytest.approx(1 - 1 / spec.NORM_HDOP_MAX)  # 0.95
    assert g["signal_strength"] == pytest.approx(
        (40.0 - spec.NORM_CNO_MIN) / (spec.NORM_CNO_MAX - spec.NORM_CNO_MIN))  # 0.75


def test_missing_quality_fields_read_as_zero_but_fix_stays_valid():
    # A GGA-only receiver may give sats but no HDOP/C/N0; those read 0, the fix
    # is still valid, and the policy sees a low-quality fix (not "no GPS").
    g = _gps_globals(_tracker([100.0]),
                     {"fix": 2, "lat": 40.0, "lon": -73.0, "sats": 4})
    assert g["gps_valid"] == 1.0
    assert g["sat_count"] == pytest.approx(4 / spec.NORM_SATS)
    assert g["fix_accuracy"] == 0.0 and g["signal_strength"] == 0.0


def test_first_fix_has_no_derived_speed_or_turn():
    g = _gps_globals(_tracker([100.0]),
                     {"fix": 3, "lat": 40.0, "lon": -73.0, "sats": 9})
    assert g["gps_valid"] == 1.0 and g["speed"] == 0.0 and g["turn_rate"] == 0.0


def test_speed_is_derived_from_successive_fixes():
    # ~0.000353 deg lon at 40 N is ~30 m; over a 10 s window -> ~3 m/s -> 0.2.
    tr = _tracker([100.0, 110.0])
    _gps_globals(tr, {"fix": 3, "lat": 40.0, "lon": -73.0, "sats": 9})
    g = _gps_globals(tr, {"fix": 3, "lat": 40.0, "lon": -72.999647, "sats": 9})
    assert 0.15 < g["speed"] < 0.25
    assert g["gps_valid"] == 1.0


def test_a_parked_device_reads_near_zero_speed():
    tr = _tracker([100.0, 110.0])
    fix = {"fix": 3, "lat": 40.0, "lon": -73.0, "sats": 9}
    _gps_globals(tr, fix)
    g = _gps_globals(tr, dict(fix))          # same position next window
    assert g["speed"] == pytest.approx(0.0, abs=1e-6)
    assert g["turn_rate"] == 0.0


def test_turn_rate_is_derived_from_a_heading_change():
    # Leg 1 heads east, leg 2 heads north: ~90 deg of heading change over 10 s
    # -> ~9 deg/s -> ~0.2 once NORM_TURN_RATE-normalized.
    tr = _tracker([100.0, 110.0, 120.0])
    _gps_globals(tr, {"fix": 3, "lat": 40.0, "lon": -73.0, "sats": 9})
    g2 = _gps_globals(tr, {"fix": 3, "lat": 40.0, "lon": -72.999647, "sats": 9})
    assert g2["turn_rate"] == 0.0            # first leg: no prior heading yet
    g3 = _gps_globals(tr, {"fix": 3, "lat": 40.000270,
                           "lon": -72.999647, "sats": 9})   # now heads north
    assert g3["turn_rate"] > 0.0


def test_speed_resets_after_a_gps_gap():
    # A window with no fix clears the previous fix, so the next lock does not
    # derive a huge speed across the gap - it starts fresh at speed 0.
    tr = _tracker([100.0, 110.0, 160.0])
    _gps_globals(tr, {"fix": 3, "lat": 40.0, "lon": -73.0, "sats": 9})
    _gps_globals(tr, None)                              # GPS lost this window
    g = _gps_globals(tr, {"fix": 3, "lat": 41.0, "lon": -74.0, "sats": 9})
    assert g["speed"] == 0.0 and g["gps_valid"] == 1.0
