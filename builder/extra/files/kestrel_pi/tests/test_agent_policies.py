"""Policy selection + model loading (kestrel_pi.agent.policies).

The enum is validated with the heavy stack absent (plugins-ctl and the CLI
mirror it torch-free); the baseline is always available and deterministic; a
learned policy falls back to the baseline on every failure mode rather than
crashing the service. The "each algorithm instantiates against the env spaces"
two-way-door check is importorskip'd on the ML stack.
"""

import os

import pytest

from kestrel_pi.agent import policies, spec


def test_enum_membership_is_usable_without_torch():
    assert set(policies.ALGORITHMS) == {"RecurrentPPO", "PPO", "DQN", "QR-DQN"}
    assert policies.DEFAULT_ALGORITHM == "RecurrentPPO"
    assert policies.DEFAULT_POLICY == "baseline"
    assert policies.valid_algorithm("RecurrentPPO")
    assert not policies.valid_algorithm("DreamerV3")
    assert policies.valid_policy("learned") and not policies.valid_policy("psychic")


def test_baseline_policy_emits_the_configured_sweep_every_step():
    pol = policies.BaselinePolicy(dwell_seconds=10, transmit="active")
    action, state = pol.predict(spec.empty_observation())
    posture, dwell, transmit = spec.decode(action)
    assert posture == "sweep-band" and dwell == 10 and transmit == "active"
    assert state is None
    # Deterministic: same action again.
    assert pol.predict(spec.empty_observation())[0] == action


def test_baseline_snaps_an_odd_dwell_to_the_nearest_bucket():
    pol = policies.BaselinePolicy(dwell_seconds=7)   # nearest bucket is 5
    _, dwell, _ = spec.decode(pol.predict(spec.empty_observation())[0])
    assert dwell == 5
    # A bad transmit value falls back to active.
    pol = policies.BaselinePolicy(dwell_seconds=5, transmit="bogus")
    assert spec.decode(pol.predict(spec.empty_observation())[0])[2] == "active"


def test_load_policy_baseline_needs_no_model(tmp_path):
    pol, name, detail = policies.load_policy("baseline", "RecurrentPPO", str(tmp_path))
    assert name == "baseline" and detail == "" and pol.name == "baseline"


def test_load_policy_learned_falls_back_when_the_model_is_missing(tmp_path):
    pol, name, detail = policies.load_policy("learned", "PPO", str(tmp_path))
    assert name == "baseline" and pol.name == "baseline"
    assert "no model" in detail


def test_load_policy_learned_falls_back_on_an_unknown_algorithm(tmp_path):
    (tmp_path / policies.MODEL_NAME).write_bytes(b"not really a model")
    pol, name, detail = policies.load_policy("learned", "Bogus", str(tmp_path))
    assert name == "baseline" and "unknown algorithm" in detail


def test_load_policy_learned_falls_back_on_an_algorithm_mismatch(tmp_path):
    # A per-algorithm model file whose own sidecar names a different algorithm
    # (a mislabeled/inconsistent import) falls back with the mismatch reason.
    (tmp_path / policies.model_name_for("PPO")).write_bytes(b"x")
    (tmp_path / policies.model_meta_name_for("PPO")).write_text(
        '{"algorithm": "DQN", "spec_version": %d}' % spec.SPEC_VERSION)
    pol, name, detail = policies.load_policy("learned", "PPO", str(tmp_path))
    assert name == "baseline" and "trained as DQN" in detail


def test_load_policy_learned_falls_back_on_a_spec_mismatch(tmp_path):
    (tmp_path / policies.model_name_for("PPO")).write_bytes(b"x")
    (tmp_path / policies.model_meta_name_for("PPO")).write_text(
        '{"algorithm": "PPO", "spec_version": %d}' % (spec.SPEC_VERSION + 1))
    pol, name, detail = policies.load_policy("learned", "PPO", str(tmp_path))
    assert name == "baseline" and "env-spec" in detail


def test_meta_round_trip(tmp_path):
    policies.write_meta(str(tmp_path), "QR-DQN")
    meta = policies.read_meta(str(tmp_path), "QR-DQN")
    assert meta == {"algorithm": "QR-DQN", "spec_version": spec.SPEC_VERSION}
    # The sidecar is per-algorithm: another algorithm has its own (absent here).
    assert policies.read_meta(str(tmp_path), "PPO") == {}
    # A corrupt sidecar reads as {} rather than raising.
    (tmp_path / policies.model_meta_name_for("QR-DQN")).write_text("{not json")
    assert policies.read_meta(str(tmp_path), "QR-DQN") == {}


def test_meta_carries_the_data_watermark_when_given(tmp_path):
    wm = {"latest_ts": 1234.5, "episodes": 42}
    policies.write_meta(str(tmp_path), "DQN", watermark=wm)
    meta = policies.read_meta(str(tmp_path), "DQN")
    assert meta["algorithm"] == "DQN"
    assert meta["data_watermark"] == wm
    # Omitted watermark leaves the field out (source-compatible with old readers).
    policies.write_meta(str(tmp_path), "PPO")
    assert "data_watermark" not in policies.read_meta(str(tmp_path), "PPO")


def test_meta_carries_the_source_when_given(tmp_path):
    policies.write_meta(str(tmp_path), "DQN", source="local")
    assert policies.read_meta(str(tmp_path), "DQN")["source"] == "local"
    # Omitted source leaves the field out (source-compatible with old readers).
    policies.write_meta(str(tmp_path), "PPO")
    assert "source" not in policies.read_meta(str(tmp_path), "PPO")


def test_meta_carries_the_training_time_when_given(tmp_path):
    # So the device can show how long a (remote) training run took.
    policies.write_meta(str(tmp_path), "DQN", train_seconds=405.0)
    assert policies.read_meta(str(tmp_path), "DQN")["train_seconds"] == 405.0
    # Omitted leaves the field out (source-compatible with old readers).
    policies.write_meta(str(tmp_path), "PPO")
    assert "train_seconds" not in policies.read_meta(str(tmp_path), "PPO")


def test_per_algorithm_models_are_kept_separate(tmp_path):
    # Each algorithm has its own model file; a switch to one with a model loads
    # it, a switch to one without falls back to baseline (which triggers the
    # auto-train elsewhere). model_path_for/has_model_for report this.
    d = str(tmp_path)
    (tmp_path / policies.model_name_for("DQN")).write_bytes(b"x")
    policies.write_meta(d, "DQN")

    assert policies.has_model_for(d, "DQN") is True
    assert policies.has_model_for(d, "PPO") is False
    assert policies.model_path_for(d, "DQN").endswith(policies.model_name_for("DQN"))
    # No model for PPO yet -> the target path where training will write it.
    assert policies.model_path_for(d, "PPO").endswith(policies.model_name_for("PPO"))

    _pol, name, detail = policies.load_policy("learned", "PPO", d)
    assert name == "baseline" and "no model for PPO" in detail


def test_legacy_single_model_still_loads_for_its_algorithm(tmp_path):
    # A device that trained/imported a model before per-algorithm files existed
    # has a plain model.zip + model.meta.json; it still loads for the algorithm
    # its sidecar names (migration), but not for a different one.
    d = str(tmp_path)
    (tmp_path / policies.MODEL_NAME).write_bytes(b"x")
    (tmp_path / policies.MODEL_META_NAME).write_text(
        '{"algorithm": "RecurrentPPO", "spec_version": %d}' % spec.SPEC_VERSION)

    assert policies.has_model_for(d, "RecurrentPPO") is True
    assert policies.has_model_for(d, "DQN") is False
    assert policies.model_path_for(d, "RecurrentPPO").endswith(policies.MODEL_NAME)


def test_model_name_for_slugs_the_algorithm():
    from kestrel_pi.agent import model_name_for, model_slug
    assert model_slug("RecurrentPPO") == "recurrentppo"
    assert model_slug("QR-DQN") == "qr-dqn"
    assert model_slug("") == "default"
    assert model_name_for("DQN") == "model-dqn.zip"
    assert model_name_for("QR-DQN") == "model-qr-dqn.zip"


def test_make_algorithm_rejects_an_unknown_name():
    with pytest.raises(ValueError):
        policies.make_algorithm("Nope", env=None)


@pytest.mark.parametrize("algorithm", ["RecurrentPPO", "PPO", "DQN", "QR-DQN"])
def test_every_algorithm_instantiates_against_the_env_spaces(algorithm, tmp_path):
    # The two-way door: a Discrete action + Box observation means every listed
    # algorithm loads against the same env with no code change. Needs the ML
    # stack; skipped where it (or its import) is unavailable.
    pytest.importorskip("stable_baselines3")
    if policies.ALGORITHMS[algorithm][0] == "sb3_contrib":
        pytest.importorskip("sb3_contrib")
    gym = pytest.importorskip("gymnasium")
    from kestrel_pi.agent.env import CaptureEnv
    from kestrel_pi.capture import CaptureInterface

    class _Surface:
        def supported_channels(self):
            return [1, 6, 11]

        def apply(self, **_kw):
            pass

    store = tmp_path / "s"
    store.mkdir()
    env = CaptureEnv(_Surface(), CaptureInterface(str(store)),
                     {"decision_seconds": 1}, wait=lambda _s: None)
    model = policies.make_algorithm(algorithm, env)
    assert model is not None
