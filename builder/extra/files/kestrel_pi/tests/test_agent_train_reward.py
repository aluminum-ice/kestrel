"""Phase 4.5 state-conditioned reward model (kestrel_pi.agent.train).

The reward fit and the validation gate are pure numpy, so they run with the
heavy stack (torch/SB3) absent; only the replay-env tests need gymnasium and
skip if it is not installed."""

import pytest

from kestrel_pi.agent import spec
from kestrel_pi.agent import train as train_mod


def _obs(signal=0.0):
    """An observation vector carrying a single feature (index 0) as the signal;
    everything else zero. Right length so the fit accepts it."""
    vec = [0.0] * spec.OBS_SIZE
    vec[0] = float(signal)
    return vec


def _rows(pairs, action=0):
    """Rows ``{obs, action, reward}`` from ``(signal, reward)`` pairs."""
    return [{"obs": _obs(s), "action": action, "reward": r} for s, r in pairs]


def test_fit_reward_model_conditions_on_the_observation():
    # action 0 pays off exactly when obs[0] is high; with enough samples the
    # per-action ridge learns that, so r̂ depends on the observation.
    rows = _rows([(1.0, 1.0), (0.0, 0.0)] * 40)       # 80 rows, obs predicts reward
    r_hat = train_mod.fit_reward_model(rows, min_samples=8)
    assert r_hat(_obs(1.0), 0) > r_hat(_obs(0.0), 0) + 0.3


def test_fit_reward_model_thin_action_falls_back_to_its_mean():
    # Too few observation-carrying rows for an action -> that action's logged
    # mean, ignoring the observation (the Phase 4 state-independent behavior).
    rows = _rows([(1.0, 1.0), (0.0, 0.0), (1.0, 1.0)], action=0)   # 3 < min_samples
    r_hat = train_mod.fit_reward_model(rows, min_samples=8)
    assert r_hat(_obs(1.0), 0) == r_hat(_obs(0.0), 0)             # obs ignored
    assert abs(r_hat(_obs(1.0), 0) - (2.0 / 3.0)) < 1e-6          # = the mean


def test_fit_reward_model_unseen_action_uses_the_global_mean():
    rows = _rows([(1.0, 1.0)] * 10, action=0)         # only action 0 is ever seen
    r_hat = train_mod.fit_reward_model(rows, min_samples=8)
    assert r_hat(_obs(0.0), 5) == 1.0                 # action 5 -> global mean


def test_fit_reward_model_clips_to_the_logged_reward_range():
    # An observation outside the logged range must not let the linear fit push
    # the reward past what the logs ever showed.
    rows = _rows([(1.0, 1.0), (0.0, 0.0)] * 40)
    r_hat = train_mod.fit_reward_model(rows, min_samples=8)
    assert r_hat(_obs(50.0), 0) <= 1.0 + 1e-9
    assert r_hat(_obs(-50.0), 0) >= 0.0 - 1e-9


def test_select_reward_model_adopts_state_conditioned_when_it_generalizes():
    rows = _rows([(1.0, 1.0), (0.0, 0.0)] * 40)       # obs genuinely predicts reward
    _fn, prov = train_mod.select_reward_model(rows)
    assert prov["reward_model"] == "state-conditioned"
    assert prov["val_mse_state_conditioned"] <= prov["val_mse_mean"]


def test_select_reward_model_stays_state_independent_without_signal():
    # Reward is constant per action and the observation carries no signal, so the
    # state-conditioned fit cannot beat the mean on held-out windows -> fall back.
    rows = _rows([(float(i % 2), 0.7) for i in range(80)])
    _fn, prov = train_mod.select_reward_model(rows)
    assert prov["reward_model"] == "state-independent"


def test_select_reward_model_too_few_rows_is_state_independent():
    rows = _rows([(1.0, 1.0), (0.0, 0.0)])            # 2 rows < min_rows
    _fn, prov = train_mod.select_reward_model(rows)
    assert prov["reward_model"] == "state-independent"
    assert "too few" in prov.get("reason", "")


def test_select_reward_model_handles_rows_without_observations():
    # A Phase 4 log with no observation column still trains (state-independent),
    # so old logs keep working.
    rows = [{"action": 0, "reward": 1.0}, {"action": 1, "reward": 0.0}] * 40
    fn, prov = train_mod.select_reward_model(rows)
    assert prov["reward_model"] == "state-independent"
    assert fn(None, 0) == 1.0 and fn(None, 1) == 0.0


def test_build_replay_env_rewards_the_current_observation():
    pytest.importorskip("gymnasium")
    rows = _rows([(0.5, 0.0)] * 5)                    # one obs value -> deterministic
    env = train_mod.build_replay_env(
        rows, episode_length=4, reward_fn=lambda obs, action: float(obs[0]) + action)
    env.reset()
    _obs2, reward, _term, _trunc, _info = env.step(3)
    assert reward == pytest.approx(0.5 + 3)           # couples obs AND action


def test_build_replay_env_defaults_to_the_mean_model():
    pytest.importorskip("gymnasium")
    rows = _rows([(0.0, 2.0)] * 5, action=1)          # action 1 mean = 2.0
    env = train_mod.build_replay_env(rows, episode_length=2)  # no reward_fn
    env.reset()
    _obs2, reward, _term, _trunc, _info = env.step(1)
    assert reward == pytest.approx(2.0)


def test_training_thread_cap_leaves_two_cores(monkeypatch):
    # Reserve two cores for the daemon/capture/inference; never below one.
    monkeypatch.setattr(train_mod.os, "cpu_count", lambda: 4)
    assert train_mod._training_thread_cap() == 2
    monkeypatch.setattr(train_mod.os, "cpu_count", lambda: 8)
    assert train_mod._training_thread_cap() == 6
    monkeypatch.setattr(train_mod.os, "cpu_count", lambda: 1)
    assert train_mod._training_thread_cap() == 1
    monkeypatch.setattr(train_mod.os, "cpu_count", lambda: None)
    assert train_mod._training_thread_cap() == 2  # falls back to 4 cores


def test_limit_training_threads_sets_env_without_torch(monkeypatch):
    # With torch absent the env-var belt still lands and the call never raises.
    monkeypatch.setattr(train_mod.os, "cpu_count", lambda: 4)
    for var in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS",
                "MKL_NUM_THREADS", "NUMEXPR_NUM_THREADS"):
        monkeypatch.delenv(var, raising=False)
    cap = train_mod.limit_training_threads()
    assert cap == 2
    assert train_mod.os.environ["OMP_NUM_THREADS"] == "2"
    assert train_mod.os.environ["MKL_NUM_THREADS"] == "2"


def test_limit_training_threads_respects_operator_env(monkeypatch):
    # setdefault: an operator's own OMP_NUM_THREADS is left untouched.
    monkeypatch.setattr(train_mod.os, "cpu_count", lambda: 4)
    monkeypatch.setenv("OMP_NUM_THREADS", "1")
    train_mod.limit_training_threads()
    assert train_mod.os.environ["OMP_NUM_THREADS"] == "1"
