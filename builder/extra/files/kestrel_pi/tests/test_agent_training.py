"""On-device training: the manual "train now" request path and its feasibility
gate (controller side). The heavy ML stack is not needed - train_from_logs is
stubbed - so these run with torch/SB3 absent."""

import json

from kestrel_pi.agent import EPISODES_DIR, TRAIN_REQUEST_NAME, STATUS_NAME
from kestrel_pi.agent import controller as ctrl_mod
from kestrel_pi.agent import train as train_mod


def _make(tmp_path, train="off"):
    cfg = tmp_path / "config.toml"
    cfg.write_text('[agent]\nenabled = true\nalgorithm = "DQN"\n'
                   'policy = "learned"\ntrain = "%s"\n'
                   '[capture]\nenabled = true\n' % train)
    agent_dir = tmp_path / "agent"
    (agent_dir / EPISODES_DIR).mkdir(parents=True)
    (tmp_path / "cap").mkdir()
    c = ctrl_mod.AgentController(
        config_path=str(cfg), agent_dir=str(agent_dir),
        capture_store=str(tmp_path / "cap"))
    return c, agent_dir


def test_train_from_logs_writes_the_per_algorithm_model(tmp_path, monkeypatch):
    """Training writes the algorithm's own model file (model-<slug>.zip) + its
    sidecar, so each algorithm keeps a separate persisted model. The ML stack is
    stubbed: build_replay_env and make_algorithm are replaced."""
    from kestrel_pi.agent import model_meta_name_for, model_name_for
    agent_dir = tmp_path / "agent"
    (agent_dir / EPISODES_DIR).mkdir(parents=True)
    (agent_dir / EPISODES_DIR / "e.jsonl").write_text(
        json.dumps({"action": 0, "reward": 1.0}) + "\n")

    class _Model:
        def learn(self, **_kw):
            pass

        def save(self, path):
            with open(path, "w", encoding="utf-8") as handle:
                handle.write("stub")

    monkeypatch.setattr(train_mod, "build_replay_env", lambda *a, **k: object())
    monkeypatch.setattr(train_mod.policies_mod, "make_algorithm",
                        lambda *a, **k: _Model())

    train_mod.train_from_logs(str(agent_dir), "DQN", timesteps=1)
    assert (agent_dir / model_name_for("DQN")).exists()
    assert (agent_dir / model_meta_name_for("DQN")).exists()
    # Training stamps the sidecar as locally trained (the host's own view).
    from kestrel_pi.agent import policies
    assert policies.read_meta(str(agent_dir), "DQN")["source"] == "local"
    # A different algorithm has no model from this run.
    assert not (agent_dir / model_name_for("PPO")).exists()


def test_status_reports_the_model_source_only_when_learned(tmp_path):
    from kestrel_pi.agent import policies
    c, agent_dir = _make(tmp_path)
    policies.write_meta(str(agent_dir), "DQN", source="remote")
    c._write_status(True, "learned", "")
    status = json.loads((agent_dir / STATUS_NAME).read_text())
    assert status["model_source"] == "remote"
    # On baseline (no learned model is driving), the source is omitted.
    c._write_status(True, "baseline", "")
    status = json.loads((agent_dir / STATUS_NAME).read_text())
    assert "model_source" not in status


def test_episodes_context_tag_is_written_and_pii_free(tmp_path):
    # Phase 4.5 fleet-alignment: the episodes dir is tagged with a coarse,
    # PII-free context (board + env spec) so a future pooled trainer can segment
    # by device class. It must not look like an episode log to the trainer.
    from kestrel_pi.agent import metrics as metrics_mod
    from kestrel_pi.agent import spec

    c, agent_dir = _make(tmp_path)
    ctx_path = agent_dir / EPISODES_DIR / ctrl_mod.CONTEXT_NAME
    assert ctx_path.exists()
    ctx = json.load(open(ctx_path))
    assert ctx["spec_version"] == spec.SPEC_VERSION
    assert set(ctx) == {"spec_version", "board", "written"}   # nothing identifying
    # The trainer and the data-presence check ignore it (only *.jsonl count).
    assert metrics_mod.load_rows(str(agent_dir / EPISODES_DIR)) == []
    assert c._has_training_data() is False


def test_consume_train_request_is_one_shot(tmp_path):
    c, agent_dir = _make(tmp_path)
    assert c._consume_train_request() is False          # no request
    (agent_dir / TRAIN_REQUEST_NAME).write_text("")
    assert c._consume_train_request() is True            # present -> consumed
    assert not (agent_dir / TRAIN_REQUEST_NAME).exists() # and removed
    assert c._consume_train_request() is False           # only once


def test_has_training_data(tmp_path):
    c, agent_dir = _make(tmp_path)
    assert c._has_training_data() is False
    (agent_dir / EPISODES_DIR / "episodes-20260101.jsonl").write_text("{}\n")
    assert c._has_training_data() is True


def test_train_feasibility_reasons(tmp_path, monkeypatch):
    c, agent_dir = _make(tmp_path)
    monkeypatch.setattr(c, "_resources_ok", lambda: True)
    monkeypatch.setattr(c, "_board_can_train", lambda: True)

    ok, reason = c._train_feasibility()
    assert ok is False and "episode data" in reason      # nothing to train on

    (agent_dir / EPISODES_DIR / "e.jsonl").write_text("{}\n")
    ok, reason = c._train_feasibility()
    assert ok is True and reason == ""

    monkeypatch.setattr(c, "_resources_ok", lambda: False)
    ok, reason = c._train_feasibility()
    assert ok is False and "memory" in reason

    monkeypatch.setattr(c, "_resources_ok", lambda: True)
    c._training = True
    ok, reason = c._train_feasibility()
    assert ok is False and "in progress" in reason


def test_run_training_manual_records_and_surfaces(tmp_path, monkeypatch):
    c, agent_dir = _make(tmp_path)
    monkeypatch.setattr(c, "_resources_ok", lambda: True)
    monkeypatch.setattr(c, "_board_can_train", lambda: True)

    seen = {}

    def fake_spawn():
        seen["training_during"] = c._training      # flag set while running
        return 0                                   # a model was written

    monkeypatch.setattr(c, "_spawn_training", fake_spawn)
    c._run_training("manual", force=True)

    assert seen["training_during"] is True
    assert c._training is False                    # cleared after
    assert c._last_trained is not None             # recorded on a clean exit
    status = json.load(open(agent_dir / STATUS_NAME))
    assert "can_train" in status and "train_blocked" in status


def test_training_command_carries_algorithm_and_timesteps(tmp_path):
    # The child runs this interpreter's trainer module with the configured
    # algorithm and timesteps, so the subprocess does exactly what the in-thread
    # call used to.
    import sys as _sys
    c, _agent_dir = _make(tmp_path)
    cmd = c._training_command()
    assert cmd[0] == _sys.executable
    assert cmd[1:3] == ["-m", "kestrel_pi.agent.train"]
    assert "--algorithm" in cmd and cmd[cmd.index("--algorithm") + 1] == "DQN"
    assert cmd[cmd.index("--timesteps") + 1] == str(c._train_timesteps)
    assert cmd[cmd.index("--config") + 1] == c.config_path


def test_spawn_training_caps_threads_and_nices_the_child(tmp_path, monkeypatch):
    # The child is launched with its torch thread pool capped via the env and a
    # preexec that lowers its priority, and _train_proc is cleared afterwards.
    c, _agent_dir = _make(tmp_path)

    class _FakeProc:
        returncode = 0

        def communicate(self):
            return ("wrote model-dqn.zip\n", None)

        def poll(self):
            return 0

    captured = {}

    def fake_popen(cmd, **kwargs):
        captured["cmd"] = cmd
        captured["env"] = kwargs.get("env")
        captured["preexec_fn"] = kwargs.get("preexec_fn")
        captured["new_session"] = kwargs.get("start_new_session")
        return _FakeProc()

    # The controller applies the cap with env.setdefault, so it deliberately
    # keeps a thread count already set in the environment (an operator override).
    # Some CI runners pre-set OMP_NUM_THREADS; clear the lot so the test sees the
    # cap the controller computes rather than the runner's ambient value.
    for _var in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS",
                 "MKL_NUM_THREADS", "NUMEXPR_NUM_THREADS"):
        monkeypatch.delenv(_var, raising=False)

    monkeypatch.setattr(ctrl_mod.subprocess, "Popen", fake_popen)
    code = c._spawn_training()

    assert code == 0
    assert captured["env"]["OMP_NUM_THREADS"] == str(train_mod._training_thread_cap())
    assert captured["preexec_fn"] is not None      # child gets niced
    assert captured["new_session"] is True         # own process group
    assert c._train_proc is None                   # cleared after the run


def test_model_mtime_and_reload_trigger(tmp_path):
    """The decide loop reloads the policy when the model appears/changes. Lock
    the trigger: _model_mtime flips None -> float when the configured algorithm's
    per-algorithm model is written, and _load_configured_policy reports that
    mtime and drops the "no model" note. (The SB3 load itself, and the in-loop
    driver swap, run on-device.)"""
    from kestrel_pi.agent import model_name_for
    c, agent_dir = _make(tmp_path)        # policy = learned

    assert c._model_mtime() is None                       # no model yet
    _pol, name, detail, mtime = c._load_configured_policy()
    assert name == "baseline" and "no model" in detail    # falls back, says why
    assert mtime is None

    # The configured algorithm's own model file is the one the loop watches.
    model = agent_dir / model_name_for(c.agent_cfg.get("algorithm"))
    model.write_text("not-a-real-zip")                     # a model appears
    assert c._model_mtime() is not None                    # now tracked

    _pol, _name, detail, mtime = c._load_configured_policy()
    assert mtime == c._model_mtime() and mtime is not None  # reload sees it
    assert "no model" not in detail                         # note clears


def test_run_training_manual_skips_when_resources_low(tmp_path, monkeypatch):
    c, agent_dir = _make(tmp_path)
    monkeypatch.setattr(c, "_board_can_train", lambda: True)
    monkeypatch.setattr(c, "_resources_ok", lambda: False)

    called = []
    monkeypatch.setattr(c, "_spawn_training",
                        lambda *a, **k: called.append(1))
    c._run_training("manual", force=True)

    assert called == []                            # never trained
    assert c._training is False
    status = json.load(open(agent_dir / STATUS_NAME))
    assert "not started" in status["detail"]       # said why on the card


def test_small_board_blocks_training(tmp_path, monkeypatch):
    # A board below the memory floor (e.g. a Zero 2 W) must never train, even on
    # a forced manual run, and must say why on the card.
    c, agent_dir = _make(tmp_path)
    monkeypatch.setattr(c, "_board_can_train", lambda: False)
    monkeypatch.setattr(c, "_resources_ok", lambda: True)
    (agent_dir / EPISODES_DIR / "e.jsonl").write_text("{}\n")

    ok, reason = c._train_feasibility()
    assert ok is False and "too small" in reason

    called = []
    monkeypatch.setattr(c, "_spawn_training", lambda *a, **k: called.append(1))
    c._run_training("manual", force=True)
    assert called == []                            # never trained
    status = json.load(open(agent_dir / STATUS_NAME))
    assert "not supported on this board" in status["detail"]


def test_auto_train_trigger_counts_new_episodes(tmp_path, monkeypatch):
    # Periodic training fires only after train_min_new_episodes new episodes,
    # and the counter resets after a run. train=idle so the mode gate is open.
    c, agent_dir = _make(tmp_path, train="idle")
    monkeypatch.setattr(c, "_board_can_train", lambda: True)
    monkeypatch.setattr(c, "_resources_ok", lambda: True)
    c._train_min_new_episodes = 3
    c._train_while_capturing = True                # ignore the idle gate

    assert c._should_auto_train() is False         # 0 new episodes
    for _ in range(3):
        c._log_episode({"new_hashlines": 0})
    assert c._episodes_since_train == 3
    assert c._should_auto_train() is True          # threshold reached

    monkeypatch.setattr(c, "_spawn_training", lambda *a, **k: 0)
    c._run_training("auto")
    assert c._episodes_since_train == 0            # reset after the run
    assert c._should_auto_train() is False         # needs new episodes again


def test_train_while_capturing_false_requires_idle(tmp_path, monkeypatch):
    c, agent_dir = _make(tmp_path, train="idle")
    monkeypatch.setattr(c, "_board_can_train", lambda: True)
    monkeypatch.setattr(c, "_resources_ok", lambda: True)
    c._train_min_new_episodes = 1
    c._train_while_capturing = False               # only train when idle
    c._log_episode({"new_hashlines": 5})           # a catch -> not idle
    monkeypatch.setattr(c, "_is_idle", lambda: False)
    assert c._should_auto_train() is False         # held: capturing, not idle
    monkeypatch.setattr(c, "_is_idle", lambda: True)
    assert c._should_auto_train() is True          # idle now -> allowed


def test_train_while_capturing_auto_is_board_aware(tmp_path, monkeypatch):
    # "auto" (the default) follows the board; explicit values are honored.
    c, _ = _make(tmp_path, train="idle")
    monkeypatch.setattr(c, "_board_can_train", lambda: True)
    assert c._resolve_train_while_capturing("auto") is True
    monkeypatch.setattr(c, "_board_can_train", lambda: False)
    assert c._resolve_train_while_capturing("auto") is False
    assert c._resolve_train_while_capturing(True) is True
    assert c._resolve_train_while_capturing(False) is False
    assert c._resolve_train_while_capturing("false") is False


def test_training_config_is_clamped(tmp_path):
    # Config can tune the knobs, but unsafe values are clamped so a bad choice
    # can't cook or OOM the board.
    cfg = tmp_path / "config.toml"
    cfg.write_text('[agent]\nenabled = true\npolicy = "learned"\n'
                   'train_max_temp_c = 150\ntrain_min_free_mb = 10\n'
                   'train_timesteps = 1\n[capture]\nenabled = true\n')
    (tmp_path / "cap").mkdir()
    (tmp_path / "agent" / EPISODES_DIR).mkdir(parents=True)
    c = ctrl_mod.AgentController(config_path=str(cfg),
                                 agent_dir=str(tmp_path / "agent"),
                                 capture_store=str(tmp_path / "cap"))
    assert c._train_max_temp_c == 85.0                     # capped at 85
    assert c._train_min_free_bytes == 256 * 1024 * 1024    # floored at 256 MB
    assert c._train_timesteps == 100                       # floored at 100


# -- quality-aware promotion: held-out scoring (numpy, no torch) ----------

class _FixedPolicy:
    """A stub policy that always picks one action - enough to score, no SB3."""

    def __init__(self, action):
        self._action = action
        self.resets = 0

    def reset(self):
        self.resets += 1

    def predict(self, _obs):
        return self._action, None


def _obs_rows(n, actions_rewards):
    """``n`` observation-carrying episode rows cycling through
    ``actions_rewards`` ``(action, reward)`` pairs, with deterministic obs."""
    import numpy as np
    from kestrel_pi.agent import spec
    rng = np.random.default_rng(0)
    rows = []
    for i in range(n):
        action, reward = actions_rewards[i % len(actions_rewards)]
        rows.append({"obs": rng.standard_normal(spec.OBS_SIZE).tolist(),
                     "action": action, "reward": reward, "ts": float(i)})
    return rows


def test_score_candidates_ranks_the_better_policy_higher():
    # Reward is 1.0 for action 0, 0.0 for action 1. The policy that always picks
    # action 0 must score above the one that always picks action 1, on the shared
    # held-out slice under the shared reward model.
    rows = _obs_rows(40, [(0, 1.0), (1, 0.0)])
    policies = {"remote": _FixedPolicy(0), "local": _FixedPolicy(1),
                "baseline": _FixedPolicy(1)}
    scores = train_mod.score_candidates(rows, policies)
    assert scores is not None
    assert scores["remote"] > scores["local"]
    # Each policy's recurrent state was reset exactly once before its pass.
    assert policies["remote"].resets == 1


def test_score_candidates_returns_none_on_a_thin_log():
    # Fewer than min_rows observation-carrying windows: too thin to score, so the
    # caller keeps the incumbent rather than promoting on noise.
    rows = _obs_rows(10, [(0, 1.0), (1, 0.0)])
    assert train_mod.score_candidates(rows, {"remote": _FixedPolicy(0)}) is None


# -- quality-aware promotion: controller decision (no torch) --------------

def _stage(agent_dir, meta):
    """Write a staged remote model + sidecar under .remote-staging/dqn/."""
    from kestrel_pi.agent import (REMOTE_STAGING_DIR, model_meta_name_for,
                                     model_name_for)
    staging = agent_dir / REMOTE_STAGING_DIR / "dqn"
    staging.mkdir(parents=True, exist_ok=True)
    (staging / model_name_for("DQN")).write_text("STAGED-MODEL")
    with open(staging / model_meta_name_for("DQN"), "w", encoding="utf-8") as fh:
        json.dump(meta, fh)


def test_promote_staged_bootstraps_when_no_local_model(tmp_path):
    from kestrel_pi.agent import (model_meta_name_for, model_name_for,
                                     policies, spec)
    c, agent_dir = _make(tmp_path)
    _stage(agent_dir, {"algorithm": "DQN", "spec_version": spec.SPEC_VERSION,
                       "data_watermark": {"latest_ts": 10.0, "episodes": 5}})
    assert c._maybe_promote_staged() is True
    # The staged model moved into the active slot and is stamped remote.
    assert (agent_dir / model_name_for("DQN")).read_text() == "STAGED-MODEL"
    assert policies.read_meta(str(agent_dir), "DQN")["source"] == "remote"


def test_promote_staged_rejects_a_spec_mismatch(tmp_path):
    from kestrel_pi.agent import model_name_for, spec
    c, agent_dir = _make(tmp_path)
    _stage(agent_dir, {"algorithm": "DQN", "spec_version": spec.SPEC_VERSION + 1,
                       "data_watermark": {"latest_ts": 10.0, "episodes": 5}})
    assert c._maybe_promote_staged() is False
    assert not (agent_dir / model_name_for("DQN")).exists()   # not adopted


def test_promote_staged_keeps_local_when_scoring_says_no(tmp_path, monkeypatch):
    from kestrel_pi.agent import model_name_for, policies, spec
    c, agent_dir = _make(tmp_path)
    # An incumbent exists.
    (agent_dir / model_name_for("DQN")).write_text("LOCAL-MODEL")
    policies.write_meta(str(agent_dir), "DQN", source="local",
                        watermark={"latest_ts": 50.0, "episodes": 40})
    _stage(agent_dir, {"algorithm": "DQN", "spec_version": spec.SPEC_VERSION,
                       "data_watermark": {"latest_ts": 60.0, "episodes": 45}})
    # Avoid torch: stub the SB3 load and the held-out scoring.
    monkeypatch.setattr(policies, "load_learned",
                        lambda algo, path, logger=None: (object(), ""))
    monkeypatch.setattr(train_mod, "score_candidates",
                        lambda *a, **k: {"remote": 0.1, "local": 0.5,
                                         "baseline": 0.0})
    assert c._maybe_promote_staged() is False
    assert (agent_dir / model_name_for("DQN")).read_text() == "LOCAL-MODEL"
    assert policies.read_meta(str(agent_dir), "DQN")["source"] == "local"


def test_promote_staged_adopts_a_clear_remote_win(tmp_path, monkeypatch):
    from kestrel_pi.agent import model_name_for, policies, spec
    c, agent_dir = _make(tmp_path)
    (agent_dir / model_name_for("DQN")).write_text("LOCAL-MODEL")
    policies.write_meta(str(agent_dir), "DQN", source="local",
                        watermark={"latest_ts": 50.0, "episodes": 40})
    _stage(agent_dir, {"algorithm": "DQN", "spec_version": spec.SPEC_VERSION,
                       "data_watermark": {"latest_ts": 60.0, "episodes": 45}})
    monkeypatch.setattr(policies, "load_learned",
                        lambda algo, path, logger=None: (object(), ""))
    monkeypatch.setattr(train_mod, "score_candidates",
                        lambda *a, **k: {"remote": 0.9, "local": 0.1,
                                         "baseline": 0.0})
    assert c._maybe_promote_staged() is True
    assert (agent_dir / model_name_for("DQN")).read_text() == "STAGED-MODEL"
    assert policies.read_meta(str(agent_dir), "DQN")["source"] == "remote"


def test_promote_staged_refuses_while_training(tmp_path):
    from kestrel_pi.agent import model_name_for, spec
    c, agent_dir = _make(tmp_path)
    c._training = True                                   # a run is writing a model
    _stage(agent_dir, {"algorithm": "DQN", "spec_version": spec.SPEC_VERSION,
                       "data_watermark": {"latest_ts": 10.0, "episodes": 5}})
    # Even the bootstrap adopt is refused mid-train (the atomic guard).
    assert c._maybe_promote_staged() is False
    assert not (agent_dir / model_name_for("DQN")).exists()


# -- local-vs-remote promotion tally --------------------------------------

def test_training_bumps_the_local_tally_and_persists(tmp_path, monkeypatch):
    from kestrel_pi.agent import PROMOTION_TALLY_NAME
    c, agent_dir = _make(tmp_path)
    monkeypatch.setattr(c, "_resources_ok", lambda: True)
    monkeypatch.setattr(c, "_board_can_train", lambda: True)
    monkeypatch.setattr(c, "_spawn_training", lambda *a, **k: 0)
    c._run_training("manual", force=True)
    assert c._promotion_tally == {"local": 1, "remote": 0}
    # Persisted and reloaded by a fresh controller (survives a restart).
    saved = json.load(open(agent_dir / PROMOTION_TALLY_NAME))
    assert saved["local"] == 1
    c2 = ctrl_mod.AgentController(
        config_path=str(tmp_path / "config.toml"),
        agent_dir=str(agent_dir), capture_store=str(tmp_path / "cap"))
    assert c2._promotion_tally == {"local": 1, "remote": 0}


def test_failed_training_does_not_bump_the_tally(tmp_path, monkeypatch):
    c, _agent_dir = _make(tmp_path)
    monkeypatch.setattr(c, "_resources_ok", lambda: True)
    monkeypatch.setattr(c, "_board_can_train", lambda: True)
    # A non-zero exit (e.g. the child found no episode logs) is a failed run.
    monkeypatch.setattr(c, "_spawn_training", lambda *a, **k: 1)
    c._run_training("manual", force=True)
    assert c._promotion_tally == {"local": 0, "remote": 0}
    assert c._last_trained is None                 # nothing recorded on failure


def test_remote_adoption_bumps_the_remote_tally(tmp_path):
    from kestrel_pi.agent import PROMOTION_TALLY_NAME, spec
    c, agent_dir = _make(tmp_path)
    _stage(agent_dir, {"algorithm": "DQN", "spec_version": spec.SPEC_VERSION,
                       "data_watermark": {"latest_ts": 10.0, "episodes": 5}})
    assert c._maybe_promote_staged() is True           # bootstrap adopt
    assert c._promotion_tally == {"local": 0, "remote": 1}
    assert json.load(open(agent_dir / PROMOTION_TALLY_NAME))["remote"] == 1


def test_status_carries_the_promotion_tally(tmp_path):
    c, agent_dir = _make(tmp_path)
    c._promotion_tally = {"local": 2, "remote": 1}
    c._write_status(True, "learned", "")
    status = json.load(open(agent_dir / STATUS_NAME))
    assert status["promotion_tally"] == {"local": 2, "remote": 1}
