"""Exploration and the allowed action set (agent/explore.py and the controller
hooks): a learned policy's windows sometimes run another allowed action, those
windows are marked in the episode log and kept out of the policy's own figures,
and no action outside [agent.action] ever runs. No torch."""

import json
import random

from kestrel_pi.agent import EPISODES_DIR, STATUS_NAME, spec
from kestrel_pi.agent import controller as ctrl_mod
from kestrel_pi.agent import explore
from kestrel_pi.agent import metrics


def _make(tmp_path, extra="", policy="learned"):
    cfg = tmp_path / "config.toml"
    cfg.write_text('[agent]\nenabled = true\nalgorithm = "DQN"\n'
                   'policy = "%s"\ntrain = "off"\n%s'
                   '[capture]\nenabled = true\n' % (policy, extra))
    agent_dir = tmp_path / "agent"
    (agent_dir / EPISODES_DIR).mkdir(parents=True)
    (tmp_path / "cap").mkdir()
    c = ctrl_mod.AgentController(
        config_path=str(cfg), agent_dir=str(agent_dir),
        capture_store=str(tmp_path / "cap"))
    return c, agent_dir


class _AlwaysExplore(random.Random):
    """An RNG whose rate draw always explores and whose pick takes the first
    candidate."""

    def random(self):
        return 0.0


# -- the allowed action set ---------------------------------------------

def test_the_shipped_lists_allow_every_action():
    allowed = explore.allowed_actions(
        {"postures": list(spec.POSTURES), "dwell_buckets": list(spec.DWELLS),
         "transmit_modes": list(spec.TRANSMIT)}, fallback=2)
    assert allowed == list(range(spec.ACTION_N))
    # Missing lists allow their whole vocabulary too.
    assert explore.allowed_actions({}, fallback=2) == list(range(spec.ACTION_N))


def test_passive_only_allows_only_passive_actions():
    allowed = explore.allowed_actions({"transmit_modes": ["passive"]}, fallback=2)
    assert len(allowed) == spec.ACTION_N // 2
    assert all(spec.decode(a)[2] == "passive" for a in allowed)


def test_unknown_values_are_dropped_and_nothing_valid_falls_back():
    allowed = explore.allowed_actions(
        {"postures": ["autohunt", "teleport"], "dwell_buckets": [5, "x", 7],
         "transmit_modes": ["passive"]}, fallback=2)
    assert [spec.decode(a) for a in allowed] == [("autohunt", 5, "passive")]
    assert explore.allowed_actions({"postures": ["teleport"]}, fallback=2) == [2]


def test_nearest_allowed_keeps_posture_and_transmit_then_dwell():
    passive = explore.allowed_actions({"transmit_modes": ["passive"]}, fallback=2)
    active = spec.encode("lock-subset", 10, "active")
    assert spec.decode(explore.nearest_allowed(active, passive)) == (
        "lock-subset", 10, "passive")
    only_sweep = explore.allowed_actions(
        {"postures": ["sweep-band"], "dwell_buckets": [2, 20]}, fallback=2)
    assert spec.decode(explore.nearest_allowed(
        spec.encode("autohunt", 5, "active"), only_sweep)) == ("sweep-band", 2, "active")
    assert explore.nearest_allowed(7, list(range(spec.ACTION_N))) == 7


# -- the explorer -------------------------------------------------------

def test_the_rate_holds_and_zero_never_explores():
    allowed = list(range(spec.ACTION_N))
    explorer = explore.Explorer(0.05, 1, allowed, rng=random.Random(1))
    hits = sum(1 for _ in range(20000) if explorer.choose(3) is not None)
    assert 800 < hits < 1200                      # about 5%
    off = explore.Explorer(0, 1, allowed, rng=random.Random(1))
    assert all(off.choose(3) is None for _ in range(1000))


def test_settings_are_clamped():
    explorer = explore.Explorer(0.9, 9, [1, 2])
    assert explorer.rate == explore.MAX_RATE and explorer.windows == explore.MAX_WINDOWS
    explorer = explore.Explorer("x", 0, [1, 2])
    assert explorer.rate == 0.0 and explorer.windows == 1
    assert not explore.Explorer(0.05, 1, [4]).enabled      # nothing else to try


def test_it_never_picks_the_policy_action_or_one_outside_the_set():
    allowed = explore.allowed_actions({"transmit_modes": ["passive"]}, fallback=2)
    explorer = explore.Explorer(0.25, 1, allowed, rng=random.Random(7))
    policy_action = allowed[0]
    for _ in range(4000):
        picked = explorer.choose(policy_action)
        if picked is not None:
            assert picked[0] in allowed and picked[0] != policy_action


def test_untried_actions_are_favoured():
    allowed = [0, 1, 2]
    explorer = explore.Explorer(0.25, 1, allowed, rng=random.Random(3),
                                load_counts=lambda: {1: 400, 2: 400})
    picks = [p[0] for p in (explorer.choose(5) for _ in range(20000)) if p]
    assert picks.count(0) > 5 * picks.count(1)
    # The probability recorded is the rate times the pick's share of the weight.
    picked = next(p for p in (explorer.choose(5) for _ in range(500)) if p and p[0] == 0)
    weight = [1.0, 1 / 401 ** 0.5, 1 / 401 ** 0.5]
    assert abs(picked[1] - 0.25 * weight[0] / sum(weight)) < 1e-6


def test_an_exploring_action_holds_its_windows():
    explorer = explore.Explorer(0.05, 2, [0, 1, 2], rng=_AlwaysExplore())
    first = explorer.choose(0)
    assert first is not None
    assert explorer.choose(0) == first            # held for a second window
    explorer.reset()
    assert explorer.choose(0) is not None          # a reset drops the hold


def test_counts_load_lazily_and_follow_new_rows():
    loads = []
    explorer = explore.Explorer(0.05, 1, [0, 1], load_counts=lambda: loads.append(1) or {0: 2})
    explorer.note(1)                               # before the load: ignored
    assert loads == []
    assert explorer.tried() == 1 and loads == [1]
    explorer.note(1)
    assert explorer.tried() == 2 and loads == [1]


# -- the controller -----------------------------------------------------

def test_the_controller_explores_only_the_learned_policy(tmp_path):
    c, _agent_dir = _make(tmp_path)
    c._explorer._rng = _AlwaysExplore()
    c._current_policy = "baseline"
    assert c._choose_action(2) == 2 and c._explore is None
    c._current_policy = "learned"
    action = c._choose_action(5)
    assert action != 5 and c._explore is not None and c._explore[0] == action


def test_explore_rate_zero_turns_it_off(tmp_path):
    c, _agent_dir = _make(tmp_path, extra="explore_rate = 0\n")
    c._explorer._rng = _AlwaysExplore()
    c._current_policy = "learned"
    assert c._choose_action(5) == 5 and c._explore is None


def test_a_model_never_runs_a_disallowed_action(tmp_path):
    c, _agent_dir = _make(
        tmp_path, extra='explore_rate = 0\n[agent.action]\ntransmit_modes = ["passive"]\n')
    c._current_policy = "learned"
    for action in range(spec.ACTION_N):
        assert spec.decode(c._choose_action(action))[2] == "passive"
    # The baseline's action is held to the set as well.
    c._current_policy = "baseline"
    baseline = spec.encode("sweep-band", 5, "active")
    assert spec.decode(c._choose_action(baseline)) == ("sweep-band", 5, "passive")


def test_exploring_windows_are_marked_in_the_log(tmp_path):
    c, agent_dir = _make(tmp_path)
    c._current_policy = "learned"
    c._explore = (9, 0.004)
    c._log_episode({"action": 9, "reward": 0.1})
    c._explore = None
    c._log_episode({"action": 3, "reward": 0.2})
    rows = metrics.load_rows(str(agent_dir / EPISODES_DIR))
    assert rows[0]["explore"] is True and rows[0]["explore_p"] == 0.004
    assert rows[0]["policy"] == "learned"
    assert "explore" not in rows[1]


def test_logged_action_counts_scan_the_episode_files(tmp_path):
    c, agent_dir = _make(tmp_path)
    rows = [{"step": 1, "action": 4, "reward": 0.0},
            {"step": 2, "action": 4, "reward": 0.0},
            {"step": 3, "action": 12, "reward": 0.0, "obs": [0.0] * 3}]
    (agent_dir / EPISODES_DIR / "episodes-1.jsonl").write_text(
        "".join(json.dumps(r) + "\n" for r in rows) + "not json\n")
    assert c._logged_action_counts() == {4: 2, 12: 1}


def test_the_status_shows_exploration(tmp_path):
    c, agent_dir = _make(tmp_path)
    c._explore = (9, 0.004)
    c._log_episode({"action": 9, "reward": 0.1})
    c._write_status(True, "learned", "trying x")
    status = json.loads((agent_dir / STATUS_NAME).read_text())
    assert status["explore"] == {"rate": 0.05, "windows": 2, "allowed": spec.ACTION_N,
                                 "tried": 1, "last_hour": 1, "now": True}


# -- the metrics --------------------------------------------------------

def test_exploring_windows_stay_out_of_the_learned_figures():
    rows = [{"policy": "learned", "new_hashlines": 2, "window_seconds": 30},
            {"policy": "learned", "explore": True, "new_hashlines": 0,
             "window_seconds": 30},
            {"policy": "baseline", "new_hashlines": 1, "window_seconds": 30}]
    summary = metrics.summarize(rows)
    assert summary["learned"]["windows"] == 1 and summary["learned"]["handshakes"] == 2
    assert summary["exploring"]["windows"] == 1
    assert summary["baseline"]["windows"] == 1
    assert "exploring" in metrics.format_table(summary)


def test_agreement():
    assert metrics.agreement([1, 2, 3, 4], [1, 2, 0, 4]) == 0.75
    assert metrics.agreement([], []) is None
    assert metrics.agreement(None, [1]) is None
