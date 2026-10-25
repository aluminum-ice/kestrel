"""The fixed env contract (kestrel_pi.agent.spec).

This is the stable seam a trained model is bound to, so these pin the pieces a
model's portability depends on: the action bijection over the whole Discrete
space, the observation shape and bounds, the posture->flag mapping (channels
come from the live ranking, not the action, so the action stays
adapter-independent), and the reward. Pure stdlib + numpy, no radio.
"""

import numpy as np
import pytest

from kestrel_pi.agent import spec


def test_action_count_is_postures_x_dwells_x_transmit():
    assert spec.ACTION_N == len(spec.POSTURES) * len(spec.DWELLS) * len(spec.TRANSMIT)
    assert spec.ACTION_N == 32


def test_decode_encode_is_a_bijection_over_the_whole_space():
    seen = set()
    for action in range(spec.ACTION_N):
        posture, dwell, transmit = spec.decode(action)
        assert posture in spec.POSTURES
        assert dwell in spec.DWELLS
        assert transmit in spec.TRANSMIT
        assert spec.encode(posture, dwell, transmit) == action
        seen.add((posture, dwell, transmit))
    # Every triple is reached exactly once.
    assert len(seen) == spec.ACTION_N


@pytest.mark.parametrize("action", [-1, spec.ACTION_N, spec.ACTION_N + 5])
def test_decode_rejects_out_of_range(action):
    with pytest.raises(ValueError):
        spec.decode(action)


@pytest.mark.parametrize("triple", [
    ("no-such-posture", 5, "active"),
    ("sweep-band", 7, "active"),      # dwell not a bucket
    ("sweep-band", 5, "stealth"),     # unknown transmit
])
def test_encode_rejects_values_outside_the_vocabulary(triple):
    with pytest.raises(ValueError):
        spec.encode(*triple)


def test_spec_version_is_2_with_the_gps_observation():
    # Phase 4.8 bumped the observation shape (added the GPS blocks), so a v1
    # model must read as incompatible and fall back to the baseline.
    assert spec.SPEC_VERSION == 2


def test_global_block_has_the_gps_motion_and_quality_features():
    # Appended after the original five, in a fixed order, so OBS_SIZE grew by 6.
    assert spec.GLOBAL_FEATURES[:5] == ("churn", "drift", "session", "rundir",
                                        "whitelist")
    for key in ("gps_valid", "speed", "turn_rate",
                "sat_count", "fix_accuracy", "signal_strength"):
        assert key in spec.GLOBAL_FEATURES
    assert spec.OBS_SIZE == spec.MAX_CH * 6 + 11 == 395


def test_observation_size_and_bounds():
    assert spec.OBS_SIZE == spec.MAX_CH * len(spec.PER_CH_FEATURES) + len(spec.GLOBAL_FEATURES)
    low, high = spec.observation_bounds()
    assert low.shape == (spec.OBS_SIZE,) and high.shape == (spec.OBS_SIZE,)
    assert np.all(low == 0.0) and np.all(high == 1.0)
    empty = spec.empty_observation()
    assert empty.shape == (spec.OBS_SIZE,) and empty.dtype == np.float32
    assert np.all(empty == 0.0)


def test_norm_signal_maps_dbm_into_0_1_and_clips():
    assert spec.norm_signal(spec.SIG_MIN_DBM) == 0.0
    assert spec.norm_signal(spec.SIG_MAX_DBM) == 1.0
    assert spec.norm_signal(-200) == 0.0      # below range clips low
    assert spec.norm_signal(0) == 1.0         # above range clips high
    mid = spec.norm_signal((spec.SIG_MIN_DBM + spec.SIG_MAX_DBM) / 2.0)
    assert 0.49 < mid < 0.51


def test_pack_observation_places_channels_in_their_slots_and_masks_the_rest():
    supported = [1, 6, 11]
    index = {ch: i for i, ch in enumerate(supported)}
    per_channel = {6: {"aps": 0.5, "yield": 1.0}}
    globals_ = {"churn": 0.25, "whitelist": 0.5}
    obs = spec.pack_observation(per_channel, globals_, index)
    width = len(spec.PER_CH_FEATURES)
    base = index[6] * width
    assert obs[base + spec.PER_CH_FEATURES.index("aps")] == pytest.approx(0.5)
    assert obs[base + spec.PER_CH_FEATURES.index("yield")] == pytest.approx(1.0)
    # Channel 1 (slot 0) contributed nothing -> all zero.
    assert np.all(obs[0:width] == 0.0)
    gbase = spec.MAX_CH * width
    assert obs[gbase + spec.GLOBAL_FEATURES.index("churn")] == pytest.approx(0.25)
    assert obs[gbase + spec.GLOBAL_FEATURES.index("whitelist")] == pytest.approx(0.5)


def test_pack_observation_drops_channels_beyond_max_ch():
    # A channel whose slot is past MAX_CH is masked out, not written out of range.
    index = {999: spec.MAX_CH}        # one past the last valid slot
    obs = spec.pack_observation({999: {"aps": 1.0}}, {}, index)
    assert obs.shape == (spec.OBS_SIZE,)
    assert np.all(obs == 0.0)


def test_pack_observation_clips_values_into_0_1():
    index = {1: 0}
    obs = spec.pack_observation({1: {"aps": 5.0}}, {"churn": -3.0}, index)
    assert obs[spec.PER_CH_FEATURES.index("aps")] == 1.0
    assert obs[spec.MAX_CH * len(spec.PER_CH_FEATURES)
               + spec.GLOBAL_FEATURES.index("churn")] == 0.0


def test_rank_channels_orders_by_yield_then_presence_deterministically():
    feats = {
        1: {"yield": 0.0, "aps": 0.1},
        6: {"yield": 0.5, "aps": 0.1},      # best: has yield
        11: {"yield": 0.0, "aps": 0.9},     # second: most APs
    }
    ranking = spec.rank_channels(feats, [1, 6, 11])
    assert ranking == [6, 11, 1]
    # Deterministic / stable tie-break by channel number when scores tie.
    assert spec.rank_channels({}, [11, 1, 6]) == [1, 6, 11]


def test_posture_to_plan_maps_each_posture_to_its_flags():
    ranking = [6, 11, 1]
    assert spec.posture_to_plan("autohunt", ranking, "both", 3) == {"kind": "autohunt"}
    assert spec.posture_to_plan("sweep-band", ranking, "5", 3) == {"kind": "band", "band": "5"}
    assert spec.posture_to_plan("pin-channel", ranking, "both", 3) == {
        "kind": "channels", "channels": [6]}
    assert spec.posture_to_plan("lock-subset", ranking, "both", 2) == {
        "kind": "channels", "channels": [6, 11]}


def test_posture_to_plan_degrades_to_a_band_sweep_with_no_channels():
    # An empty ranking must never produce a no-op window.
    for posture in ("pin-channel", "lock-subset"):
        plan = spec.posture_to_plan(posture, [], "both", 3)
        assert plan == {"kind": "band", "band": "both"}


def test_reward_counts_new_unique_hashlines_with_light_shaping():
    weights = {"per_handshake": 1.0, "time_penalty": 0.01, "idle_penalty": 0.05}
    # Two handshakes, APs present: 2 - time penalty.
    assert spec.reward(2, True, weights) == pytest.approx(2 - 0.01)
    # No handshakes but APs present: just the time penalty.
    assert spec.reward(0, True, weights) == pytest.approx(-0.01)
    # No handshakes and dead air: time + idle penalty.
    assert spec.reward(0, False, weights) == pytest.approx(-0.01 - 0.05)
    # Negative counts never add reward.
    assert spec.reward(-5, True, weights) == pytest.approx(-0.01)
