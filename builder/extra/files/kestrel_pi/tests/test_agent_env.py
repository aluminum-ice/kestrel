"""The capture env (kestrel_pi.agent.env.CaptureEnv) against a synthetic store.

No radio: a fake Control surface records the launch plans, a synthetic status
snapshot supplies the RF state, and the yield comes from writing lines into a
real capture store on disk between steps (the exact-line-deduped unique delta
the reward and Gate 4 count). gymnasium is importorskip'd so the suite runs
wherever it is installed.
"""

import os

import pytest

gym = pytest.importorskip("gymnasium")

from kestrel_pi.agent import spec                       # noqa: E402
from kestrel_pi.agent.env import CaptureEnv             # noqa: E402
from kestrel_pi.capture import CaptureInterface         # noqa: E402


def _mac(n):
    return "%012x" % n


def _hashline(ap_index, sta_index=1, essid="lab"):
    """A valid WPA*02 (eapol) hc22000 line whose AP MAC parses to a BSSID."""
    pmkid = "a" * 32
    return "WPA*02*%s*%s*%s*%s*00*00" % (
        pmkid, _mac(0x100 + ap_index), _mac(0x200 + sta_index),
        essid.encode().hex())


def _bssid(ap_index):
    raw = _mac(0x100 + ap_index)
    return ":".join(raw[i:i + 2] for i in range(0, 12, 2)).upper()


class FakeSurface:
    """Records every apply() so a test can assert the realized plans, and
    reports a fixed supported-channel set."""

    def __init__(self, channels=(1, 6, 11)):
        self._channels = list(channels)
        self.applied = []

    def supported_channels(self):
        return list(self._channels)

    def apply(self, plan, dwell, transmit):
        self.applied.append({"plan": plan, "dwell": dwell, "transmit": transmit})


@pytest.fixture
def store(tmp_path):
    d = tmp_path / "capture"
    d.mkdir()
    return d


def _write(store, lines, name="capture.hc22000"):
    path = os.path.join(str(store), name)
    with open(path, "a", encoding="utf-8") as fh:
        for line in lines:
            fh.write(line + "\n")


def _env(store, surface=None, status=None, config=None, logger=None):
    clock = {"t": 0.0}

    def tick():
        clock["t"] += 1.0
        return clock["t"]

    logs = []
    env = CaptureEnv(
        surface=surface or FakeSurface(),
        capture=CaptureInterface(str(store)),
        config=config or {"decision_seconds": 30, "capture_bands": "both",
                          "action": {"lock_subset_n": 2}},
        status_source=(lambda: status) if status is not None else None,
        wait=lambda _s: None,            # never actually sleep a window
        clock=tick,
        episode_logger=logs.append,
    )
    env._episode_rows = logs
    return env


def test_spaces_match_the_spec():
    env = _env_store()
    assert env.action_space.n == spec.ACTION_N
    assert env.observation_space.shape == (spec.OBS_SIZE,)


def _env_store():
    import tempfile
    d = tempfile.mkdtemp()
    return _env(d)


def test_reset_returns_a_well_shaped_observation(store):
    env = _env(store)
    obs, info = env.reset()
    assert obs.shape == (spec.OBS_SIZE,)
    assert env.observation_space.contains(obs)
    assert info == {}


def test_step_rewards_new_unique_hashlines_from_the_store_delta(store):
    snapshot = {"aps": [{"bssid": _bssid(1), "channel": 6, "signal": -40,
                         "clients": 1, "whitelisted": False}]}
    env = _env(store, status=snapshot)
    env.reset()
    # One new handshake lands this window.
    _write(store, [_hashline(1)])
    obs, reward, terminated, truncated, info = env.step(0)
    assert terminated is False and truncated is False   # continuing task
    assert info["new_hashlines"] == 1
    assert info["new_networks"] == 1
    # per_handshake 1.0 - time_penalty 0.01, APs present so no idle penalty.
    assert reward == pytest.approx(1 - 0.01)
    assert env.observation_space.contains(obs)


def test_duplicate_lines_are_not_counted_again(store):
    env = _env(store, status={"aps": []})
    env.reset()
    _write(store, [_hashline(1)])
    assert env.step(0)[4]["new_hashlines"] == 1
    # The same exact line again (even in a second file) is not new.
    _write(store, [_hashline(1)], name="per-ssid.hc22000")
    assert env.step(0)[4]["new_hashlines"] == 0


def test_idle_penalty_applies_only_with_no_discoverable_aps(store):
    env = _env(store, status={"aps": []})     # dead air
    env.reset()
    _, reward, _, _, info = env.step(0)
    assert info["discoverable"] == 0
    assert reward == pytest.approx(-0.01 - 0.05)


def test_whitelisted_aps_are_not_counted_as_discoverable(store):
    snapshot = {"aps": [
        {"bssid": _bssid(1), "channel": 6, "signal": -50, "whitelisted": True},
        {"bssid": _bssid(2), "channel": 6, "signal": -50, "whitelisted": True},
    ]}
    env = _env(store, status=snapshot)
    env.reset()
    _, reward, _, _, info = env.step(0)
    # Only whitelisted APs seen -> no discoverable AP -> idle penalty applies,
    # i.e. the agent is never rewarded for dwelling on protected networks.
    assert info["discoverable"] == 0
    assert reward == pytest.approx(-0.01 - 0.05)


def test_action_decodes_to_the_realized_launch_plan(store):
    surface = FakeSurface(channels=(1, 6, 11))
    snapshot = {"aps": [{"bssid": _bssid(1), "channel": 11, "signal": -30,
                         "clients": 3, "whitelisted": False}]}
    env = _env(store, surface=surface, status=snapshot)
    env.reset()
    # pin-channel @ 10s active: posture index 2 (sweep,lock,pin,autohunt).
    action = spec.encode("pin-channel", 10, "active")
    env.step(action)
    last = surface.applied[-1]
    assert last["dwell"] == 10 and last["transmit"] == "active"
    # Channel 11 is the only one with signal/clients, so it ranks top and is
    # the pinned channel.
    assert last["plan"] == {"kind": "channels", "channels": [11]}


def test_sweep_band_uses_the_operator_configured_band(store):
    surface = FakeSurface()
    env = _env(store, surface=surface, status={"aps": []},
               config={"decision_seconds": 5, "capture_bands": "5",
                       "action": {"lock_subset_n": 2}})
    env.reset()
    env.step(spec.encode("sweep-band", 5, "active"))
    assert surface.applied[-1]["plan"] == {"kind": "band", "band": "5"}


def test_episode_log_records_the_pre_step_observation_and_action(store):
    env = _env(store, status={"aps": []})
    env.reset()
    _write(store, [_hashline(1)])
    env.step(7)
    row = env._episode_rows[-1]
    assert row["action"] == 7
    assert row["new_hashlines"] == 1
    assert row["window_seconds"] == 30
    assert "obs" in row and len(row["obs"]) == spec.OBS_SIZE
    posture, dwell, transmit = spec.decode(7)
    assert row["posture"] == posture and row["dwell"] == dwell
    assert row["transmit"] == transmit


def test_yield_is_attributed_to_the_channel_of_the_cracked_ap(store):
    # AP 1 on channel 6; its handshake must enrich channel 6's yield so the next
    # step's ranking favours it.
    snapshot = {"aps": [{"bssid": _bssid(1), "channel": 6, "signal": -40,
                         "clients": 0, "whitelisted": False}]}
    env = _env(store, status=snapshot)
    env.reset()
    _write(store, [_hashline(1)])
    _, _, _, _, info = env.step(0)
    assert info["channel_ranking"][0] == 6


def test_supported_channels_are_deduped_sorted_and_capped():
    import tempfile
    surface = FakeSurface(channels=[11, 6, 6, 1] + list(range(100, 200)))
    env = _env(tempfile.mkdtemp(), surface=surface)
    assert env.supported[:3] == [1, 6, 11]
    assert len(env.supported) <= spec.MAX_CH
