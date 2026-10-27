"""Config-selected SB3 algorithm map, and the always-present scripted baseline.

The ``[agent] algorithm`` key picks the stable-baselines3 / sb3-contrib class;
because the env exposes a plain ``Discrete`` action and a ``Box`` observation,
every entry below loads against the same env with no code change - the two-way
door the design keeps open. ``[agent] policy`` picks what actually drives
capture: ``baseline`` (the Phase 2 sweep, expressed in the env's own action
vocabulary) or ``learned`` (the trained ``model.zip``). If a learned model is
missing, corrupt, or trained under a mismatched algorithm/spec, loading falls
back to the baseline with a logged reason - the robust scripted sweep is always
the fallback and the scoring reference.

torch and SB3 are imported lazily (only when a learned model or a trainer is
actually built), so this module imports - and the enums below are usable - with
the heavy stack absent. That is what lets ``plugins-ctl`` mirror the enum for
validation and the CLI/daemon read ``algorithm``/``policy`` without torch.
"""

from __future__ import annotations

import json
import os

from . import (MODEL_META_NAME, MODEL_NAME, model_meta_name_for,
               model_name_for, spec)

# name -> (module, class, SB3 policy string). The enum the web dropdown and the
# root apply helper validate against; an unknown value is rejected before it is
# ever used to import a class.
ALGORITHMS = {
    "RecurrentPPO": ("sb3_contrib", "RecurrentPPO", "MlpLstmPolicy"),  # default
    "PPO": ("stable_baselines3", "PPO", "MlpPolicy"),
    "DQN": ("stable_baselines3", "DQN", "MlpPolicy"),
    "QR-DQN": ("sb3_contrib", "QRDQN", "MlpPolicy"),
}
POLICIES = ("baseline", "learned")
DEFAULT_ALGORITHM = "RecurrentPPO"
DEFAULT_POLICY = "baseline"


def valid_algorithm(name):
    return name in ALGORITHMS


def valid_policy(name):
    return name in POLICIES


def _nearest_dwell(seconds):
    """The dwell bucket closest to a configured ``dwell_seconds``."""
    try:
        want = float(seconds)
    except (TypeError, ValueError):
        want = 5.0
    return min(spec.DWELLS, key=lambda d: abs(d - want))


class BaselinePolicy:
    """The Phase 2 scripted sweep, expressed in the env's action vocabulary.

    Every step it emits the ``sweep-band`` posture over the operator's
    configured band at the configured dwell (active transmit) - i.e. AngryOxide
    sweeping its channels at a fixed dwell, exactly the Phase 2 behaviour, run
    through the identical ``step()`` control path so the agent-vs-baseline
    comparison is apples-to-apples. It is deterministic and needs no model, so
    it is always available as the fallback."""

    name = "baseline"

    def __init__(self, dwell_seconds=5, transmit="active"):
        transmit = transmit if transmit in spec.TRANSMIT else "active"
        self._action = spec.encode("sweep-band", _nearest_dwell(dwell_seconds),
                                   transmit)

    def predict(self, observation, **_kwargs):
        # Signature-compatible with SB3's predict (returns (action, state)).
        return self._action, None

    def reset(self):
        pass


class LearnedPolicy:
    """Wraps a loaded SB3 model for inference, carrying the recurrent state
    across steps so RecurrentPPO's LSTM keeps its context. ``predict`` returns
    a plain int action from the ``Discrete`` space."""

    name = "learned"

    def __init__(self, model, algorithm):
        self._model = model
        self.algorithm = algorithm
        self._state = None
        self._episode_start = True

    def predict(self, observation, **_kwargs):
        import numpy as np

        action, self._state = self._model.predict(
            observation, state=self._state,
            episode_start=np.array([self._episode_start]), deterministic=True)
        self._episode_start = False
        return int(np.asarray(action).reshape(-1)[0]), self._state

    def reset(self):
        self._state = None
        self._episode_start = True


def _algo_class(algorithm):
    """Import and return the SB3 class for ``algorithm`` (lazy; torch/SB3 only
    loaded here). Raises KeyError for an unknown name, ImportError if the stack
    is not installed."""
    module_name, class_name, _policy = ALGORITHMS[algorithm]
    import importlib

    module = importlib.import_module(module_name)
    return getattr(module, class_name)


def read_meta(model_dir, algorithm=None):
    """The model sidecar dict (algorithm + spec it was trained under), or {}.

    With ``algorithm`` it reads that algorithm's per-algorithm sidecar
    (``model-<slug>.meta.json``); without it, the legacy single sidecar
    (``model.meta.json``) kept only for migration."""
    name = model_meta_name_for(algorithm) if algorithm else MODEL_META_NAME
    path = os.path.join(model_dir, name)
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def write_meta(model_dir, algorithm, watermark=None, provenance=None,
               train_seconds=None, source=None):
    """Write the per-algorithm model sidecar beside a freshly trained model.

    ``watermark`` (a ``{"latest_ts", "episodes"}`` dict from
    :func:`metrics.data_watermark`) records the newest episode data the model
    was trained on. ``provenance`` (from :func:`train.select_reward_model`)
    records which reward model trained it (state-conditioned vs the Phase 4 mean
    model) and the held-out scores, so a fleet can attribute and roll back a
    model. ``train_seconds`` is how long the training run took, so a remote run's
    cost is visible on the device. ``source`` is where this model was trained
    from the reader's point of view: training stamps ``"local"`` (trained on this
    host), and a device that imports a pulled model restamps it ``"remote"``, so
    the dashboard can tell an on-device model from one trained on the remote host.
    All are written whether the model was trained on-device or on a remote host;
    readers that predate any field ignore it (source-compatible, so the hook API
    is unchanged)."""
    meta = {"algorithm": algorithm, "spec_version": spec.SPEC_VERSION}
    if watermark:
        meta["data_watermark"] = watermark
    if provenance:
        meta["provenance"] = provenance
    if train_seconds is not None:
        meta["train_seconds"] = train_seconds
    if source:
        meta["source"] = source
    path = os.path.join(model_dir, model_meta_name_for(algorithm))
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(meta, handle)
    os.replace(tmp, path)


def _resolve_model(model_dir, algorithm):
    """Resolve the model file to load for ``algorithm`` and its sidecar.

    Returns ``(model_path, meta)``: the per-algorithm model and its sidecar if
    present; else a legacy ``model.zip`` whose sidecar matches ``algorithm`` (or
    carries no algorithm) - the migration path for a model trained/imported
    before per-algorithm files existed; else ``(None, {})`` when there is no
    usable model for this algorithm."""
    per_algo = os.path.join(model_dir, model_name_for(algorithm))
    if os.path.exists(per_algo):
        return per_algo, read_meta(model_dir, algorithm)
    legacy = os.path.join(model_dir, MODEL_NAME)
    if os.path.exists(legacy):
        meta = read_meta(model_dir)
        if meta.get("algorithm") in (None, algorithm):
            return legacy, meta
    return None, {}


def model_path_for(model_dir, algorithm):
    """The model file :func:`load_policy` would use for ``algorithm`` (the
    per-algorithm model, or a matching legacy ``model.zip``), or the
    per-algorithm target path where a new model will be written when none
    exists yet. The decide loop watches this path to adopt a model trained in
    after start."""
    path, _meta = _resolve_model(model_dir, algorithm)
    return path or os.path.join(model_dir, model_name_for(algorithm))


def has_model_for(model_dir, algorithm):
    """Whether a usable persisted model already exists for ``algorithm`` (so a
    switch to it loads that model rather than starting a training run)."""
    path, _meta = _resolve_model(model_dir, algorithm)
    return path is not None


def load_policy(policy_name, algorithm, model_dir, dwell_seconds=5,
                transmit="active", logger=None):
    """Resolve the policy that drives capture.

    Returns ``(policy, effective_name, detail)``. ``baseline`` always yields the
    scripted sweep. ``learned`` loads ``model.zip`` under ``algorithm`` (checked
    against the sidecar's algorithm and spec version); any problem - missing
    model, mismatched algorithm/spec, corrupt file, or the ML stack absent -
    falls back to the baseline with a human reason in ``detail`` rather than
    failing the service."""
    baseline = BaselinePolicy(dwell_seconds=dwell_seconds, transmit=transmit)
    if policy_name != "learned":
        return baseline, "baseline", ""
    if not valid_algorithm(algorithm):
        return baseline, "baseline", "unknown algorithm %r; using baseline" % (algorithm,)
    model_path, meta = _resolve_model(model_dir, algorithm)
    if not model_path:
        return (baseline, "baseline",
                "no model for %s yet; using baseline (train or import one)"
                % algorithm)
    meta_algo = meta.get("algorithm")
    if meta_algo and meta_algo != algorithm:
        return (baseline, "baseline",
                "model was trained as %s but [agent] algorithm is %s; using "
                "baseline" % (meta_algo, algorithm))
    meta_spec = meta.get("spec_version")
    if meta_spec is not None and meta_spec != spec.SPEC_VERSION:
        return (baseline, "baseline",
                "model env-spec v%s != current v%s; using baseline"
                % (meta_spec, spec.SPEC_VERSION))
    try:
        model = _load_weights(algorithm, model_path)
    except KeyError:
        return baseline, "baseline", "unknown algorithm %r; using baseline" % (algorithm,)
    except ImportError as exc:
        return baseline, "baseline", "ML stack unavailable (%s); using baseline" % exc
    except Exception as exc:  # noqa: BLE001 - a corrupt/incompatible model
        if logger:
            logger.warning("loading model failed: %s", exc)
        return baseline, "baseline", "model failed to load (%s); using baseline" % exc
    return LearnedPolicy(model, algorithm), "learned", ""


def load_learned(algorithm, model_path, logger=None):
    """Load the SB3 model at ``model_path`` as a :class:`LearnedPolicy` for
    ``algorithm``, or return ``(None, reason)``.

    Unlike :func:`load_policy` (which resolves the *active* model slot and falls
    back to the baseline), this loads an explicit file - used by quality-aware
    promotion to score a staged remote model, and to re-load the incumbent, from
    known paths. It is inference-only; the caller handles the baseline reference
    separately. torch/SB3 load lazily inside, so a load failure (corrupt file,
    missing stack, unknown algorithm) returns a human reason rather than
    raising."""
    if not valid_algorithm(algorithm):
        return None, "unknown algorithm %r" % (algorithm,)
    try:
        model = _load_weights(algorithm, model_path)
    except ImportError as exc:
        return None, "ML stack unavailable (%s)" % exc
    except Exception as exc:  # noqa: BLE001 - a corrupt/incompatible model
        if logger:
            logger.warning("loading model %s failed: %s", model_path, exc)
        return None, "model failed to load (%s)" % exc
    return LearnedPolicy(model, algorithm), ""


# Construction overrides for an inference-only model. They only shrink the
# training buffers SB3 allocates up front (a default DQN replay buffer holds a
# million observations); the networks, and so the weights that fit them, are
# the same as training builds with make_algorithm's defaults.
_INFERENCE_KWARGS = {
    "RecurrentPPO": {"n_steps": 2, "batch_size": 2},
    "PPO": {"n_steps": 2, "batch_size": 2},
    "DQN": {"buffer_size": 1, "learning_starts": 0},
    "QR-DQN": {"buffer_size": 1, "learning_starts": 0},
}


def _spaces_env():
    """A gymnasium env with only the fixed spec spaces: enough for SB3 to build
    a model's networks for inference. It is never reset or stepped."""
    import gymnasium as gym
    import numpy as np
    from gymnasium import spaces

    class _SpacesEnv(gym.Env):
        def __init__(self):
            low, high = spec.observation_bounds()
            self.observation_space = spaces.Box(low=low, high=high, dtype=np.float32)
            self.action_space = spaces.Discrete(spec.ACTION_N)

        def reset(self, *, seed=None, options=None):
            raise NotImplementedError("inference-only env")

        def step(self, action):
            raise NotImplementedError("inference-only env")

    return _SpacesEnv()


def _load_weights(algorithm, model_path):
    """The model at ``model_path``, loaded as weights only.

    SB3's ``cls.load`` also restores the Python objects saved alongside the
    weights by unpickling them, which can run code from the file - and model
    files arrive from a remote training host and the boot partition. This
    builds a fresh model for the fixed spec spaces instead and copies in only
    the tensors (``set_parameters`` skips the pickled data; torch loads the
    tensors with ``weights_only``). A file whose networks don't match raises,
    and the caller falls back to the baseline."""
    model = make_algorithm(algorithm, _spaces_env(), device="cpu",
                           **_INFERENCE_KWARGS[algorithm])
    model.set_parameters(model_path, exact_match=True, device="cpu")
    return model


def make_algorithm(algorithm, env, logger=None, **kwargs):
    """Instantiate a fresh SB3 model of ``algorithm`` bound to ``env`` (for
    training, and for the two-way-door compatibility test). Raises ValueError
    for an unknown algorithm; ImportError propagates when the stack is absent."""
    if not valid_algorithm(algorithm):
        raise ValueError("unknown algorithm: %r" % (algorithm,))
    cls = _algo_class(algorithm)
    _module, _class, policy_name = ALGORITHMS[algorithm]
    return cls(policy_name, env, **kwargs)
