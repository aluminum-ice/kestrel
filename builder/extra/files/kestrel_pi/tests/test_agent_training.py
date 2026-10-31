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


def _hourly_rows(hours, per_hour=6, actions_rewards=((0, 1.0), (1, 0.0))):
    """Observation-carrying rows spread over ``hours`` hours (``per_hour`` each),
    so about a fifth fall in held-out hour blocks."""
    import numpy as np
    from kestrel_pi.agent import spec
    rng = np.random.default_rng(0)
    rows = []
    for i in range(hours * per_hour):
        action, reward = actions_rewards[i % len(actions_rewards)]
        rows.append({"obs": rng.standard_normal(spec.OBS_SIZE).tolist(),
                     "action": action, "reward": reward,
                     "ts": i * 3600.0 / per_hour})
    return rows


def test_evaluate_scores_only_held_out_rows_and_ranks_the_better_policy():
    from kestrel_pi.agent import metrics
    rows = _hourly_rows(200)
    policies = {"good": _FixedPolicy(0), "bad": _FixedPolicy(1)}
    res = train_mod.evaluate(rows, policies)
    held = [r for r in rows if metrics.is_holdout(r)]
    assert res["rows"] == len(held)
    assert res["mean"]["good"] > res["mean"]["bad"]
    assert len(res["per_row"]["good"]) == res["rows"]
    # The recurrent state is reset at every held-out hour block, in time order.
    blocks = {metrics.holdout_block(r["ts"]) for r in held}
    assert policies["good"].resets == len(blocks)


def test_evaluate_respects_cap_min_ts_and_min_rows():
    from kestrel_pi.agent import metrics
    rows = _hourly_rows(400)
    held_ts = sorted(r["ts"] for r in rows if metrics.is_holdout(r))
    # The cap keeps the newest held-out rows.
    capped = train_mod.evaluate(rows, {"p": _FixedPolicy(0)}, cap=70)
    assert capped["rows"] == 70 and capped["newest_ts"] == held_ts[-1]
    assert capped["oldest_ts"] == held_ts[-70]
    # min_ts keeps only held-out rows newer than it.
    cut = held_ts[len(held_ts) // 3]
    later = train_mod.evaluate(rows, {"p": _FixedPolicy(0)}, min_ts=cut)
    assert later["oldest_ts"] > cut
    # Too thin to decide on.
    assert train_mod.evaluate(_hourly_rows(10), {"p": _FixedPolicy(0)}) is None


def _stub_training(monkeypatch, seen, set_params=None):
    class _Model:
        device = "cpu"

        def learn(self, **_kw):
            pass

        def save(self, path):
            with open(path, "w", encoding="utf-8") as handle:
                handle.write("TRAINED")

        def set_parameters(self, path, **_kw):
            if set_params is not None:
                set_params.append(path)

    def fake_env(rows, **_kw):
        seen["env_rows"] = rows
        return object()

    def fake_select(rows):
        seen["reward_rows"] = rows
        return (lambda o, a: 0.0), {"reward_model": "state-independent"}

    monkeypatch.setattr(train_mod, "build_replay_env", fake_env)
    monkeypatch.setattr(train_mod, "select_reward_model", fake_select)
    monkeypatch.setattr(train_mod.policies_mod, "make_algorithm",
                        lambda *a, **k: _Model())


def _write_rows(agent_dir, rows):
    with open(agent_dir / EPISODES_DIR / "e.jsonl", "w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row) + "\n")


def test_training_never_sees_a_held_out_row_and_writes_where_told(tmp_path, monkeypatch):
    from kestrel_pi.agent import metrics, model_name_for, policies
    agent_dir = tmp_path / "agent"
    (agent_dir / EPISODES_DIR).mkdir(parents=True)
    rows = [{"action": 0, "reward": 1.0, "ts": h * 1800.0} for h in range(400)]
    _write_rows(agent_dir, rows)
    seen = {}
    _stub_training(monkeypatch, seen)
    out = tmp_path / "candidate"
    train_mod.train_from_logs(str(agent_dir), "DQN", timesteps=7, out_dir=str(out))
    assert not any(metrics.is_holdout(r) for r in seen["env_rows"])
    assert not any(metrics.is_holdout(r) for r in seen["reward_rows"])
    assert len(seen["env_rows"]) < len(rows)
    # Written to the candidate folder, never the active slot.
    assert (out / model_name_for("DQN")).exists()
    assert not (agent_dir / model_name_for("DQN")).exists()
    meta = policies.read_meta(str(out), "DQN")
    assert meta["holdout"] == metrics.HOLDOUT_SCHEME
    assert meta["timesteps"] == 7 and meta["warm_start"] is None
    assert meta["held_out_rows"] == len(rows) - meta["train_rows"]


def test_training_warm_starts_from_the_active_model(tmp_path, monkeypatch):
    from kestrel_pi.agent import model_name_for, policies, spec
    agent_dir = tmp_path / "agent"
    (agent_dir / EPISODES_DIR).mkdir(parents=True)
    _write_rows(agent_dir, [{"action": 0, "reward": 1.0, "ts": h * 1800.0}
                            for h in range(100)])
    (agent_dir / model_name_for("DQN")).write_text("ACTIVE")
    policies.write_meta(str(agent_dir), "DQN", source="remote")
    seen, loaded = {}, []
    _stub_training(monkeypatch, seen, set_params=loaded)
    train_mod.train_from_logs(str(agent_dir), "DQN", timesteps=1,
                              out_dir=str(tmp_path / "cand"))
    assert loaded == [str(agent_dir / model_name_for("DQN"))]
    assert policies.read_meta(str(tmp_path / "cand"), "DQN")["warm_start"] == "remote"
    # A model from another env-spec isn't used as a starting point.
    loaded.clear()
    meta_path = agent_dir / "model-dqn.meta.json"
    meta = json.loads(meta_path.read_text())
    meta["spec_version"] = spec.SPEC_VERSION + 1
    meta_path.write_text(json.dumps(meta))
    train_mod.train_from_logs(str(agent_dir), "DQN", timesteps=1,
                              out_dir=str(tmp_path / "cand2"))
    assert loaded == []
    # --no-warm-start trains fresh.
    meta["spec_version"] = spec.SPEC_VERSION
    meta_path.write_text(json.dumps(meta))
    train_mod.train_from_logs(str(agent_dir), "DQN", timesteps=1,
                              out_dir=str(tmp_path / "cand3"), warm_start=False)
    assert loaded == []


def test_training_command_writes_a_local_candidate(tmp_path):
    from kestrel_pi.agent import CANDIDATES_DIR
    c, agent_dir = _make(tmp_path)
    cmd = c._training_command()
    assert cmd[cmd.index("--out-dir") + 1] == str(agent_dir / CANDIDATES_DIR / "local" / "dqn")


# -- contests: every candidate faces the same gate (no torch) -------------

def _cand(directory, meta, content):
    from kestrel_pi.agent import model_meta_name_for, model_name_for
    directory.mkdir(parents=True, exist_ok=True)
    (directory / model_name_for("DQN")).write_text(content)
    (directory / model_meta_name_for("DQN")).write_text(json.dumps(meta))


def _meta(latest=10.0, **extra):
    from kestrel_pi.agent import metrics, spec
    meta = {"algorithm": "DQN", "spec_version": spec.SPEC_VERSION,
            "holdout": metrics.HOLDOUT_SCHEME,
            "data_watermark": {"latest_ts": latest, "episodes": 5}}
    meta.update(extra)
    return meta


def _local(agent_dir, content="LOCAL-CAND", **meta):
    from kestrel_pi.agent import CANDIDATES_DIR
    _cand(agent_dir / CANDIDATES_DIR / "local" / "dqn", _meta(**meta), content)


def _remote(agent_dir, content="REMOTE-CAND", **meta):
    from kestrel_pi.agent import REMOTE_STAGING_DIR
    _cand(agent_dir / REMOTE_STAGING_DIR / "dqn", _meta(**meta), content)


def _active(agent_dir, content="ACTIVE", source="local", **meta):
    from kestrel_pi.agent import model_meta_name_for, model_name_for
    (agent_dir / model_name_for("DQN")).write_text(content)
    (agent_dir / model_meta_name_for("DQN")).write_text(
        json.dumps(_meta(source=source, **meta)))


def _contest_stubs(monkeypatch, means, calls=None, broken=()):
    """Stub the model loads and the held-out scoring: ``means`` gives each
    policy name ("baseline", "incumbent", "local", "remote") its per-row
    reward; None for ``means`` means too few held-out rows."""
    from kestrel_pi.agent import policies

    def fake_load(algorithm, path, logger=None):
        if any(b in str(path) for b in broken):
            return None, "corrupt"
        return _FixedPolicy(0), ""

    def fake_evaluate(rows, pols, cap=2000, min_rows=64, min_ts=None):
        if calls is not None:
            calls.append({"names": sorted(pols), "min_ts": min_ts})
        if means is None:
            return None
        per_row = {n: [means[n] + d for d in (0.01, -0.01) * 40] for n in pols}
        return {"rows": 80, "per_row": per_row,
                "mean": {n: means[n] for n in pols},
                "reward_model": "state-conditioned"}

    monkeypatch.setattr(policies, "load_learned", fake_load)
    monkeypatch.setattr(train_mod, "evaluate", fake_evaluate)


def _active_content(agent_dir):
    from kestrel_pi.agent import model_name_for
    return (agent_dir / model_name_for("DQN")).read_text()


def test_a_local_candidate_that_wins_is_promoted(tmp_path, monkeypatch):
    from kestrel_pi.agent import policies
    c, agent_dir = _make(tmp_path)
    _active(agent_dir)
    _local(agent_dir)
    _contest_stubs(monkeypatch, {"baseline": 0.0, "incumbent": 0.2, "local": 0.5})
    assert c._run_contest("local") == "promoted"
    assert _active_content(agent_dir) == "LOCAL-CAND"
    meta = policies.read_meta(str(agent_dir), "DQN")
    assert meta["source"] == "local" and meta["contest"]["trigger"] == "local"
    assert c._promotion_tally["local"] == {"won": 1, "lost": 0}


def test_a_local_candidate_that_loses_is_discarded(tmp_path, monkeypatch):
    from kestrel_pi.agent import CANDIDATES_DIR
    c, agent_dir = _make(tmp_path)
    _active(agent_dir)
    _local(agent_dir)
    _contest_stubs(monkeypatch, {"baseline": 0.0, "incumbent": 0.5, "local": 0.2})
    assert c._run_contest("local") == "kept"
    assert _active_content(agent_dir) == "ACTIVE"
    assert not list((agent_dir / CANDIDATES_DIR / "local" / "dqn").iterdir())
    assert c._promotion_tally["local"] == {"won": 0, "lost": 1}
    assert c._last_decision["result"] == "kept"


def test_a_remote_candidate_that_wins_is_promoted_and_stamped(tmp_path, monkeypatch):
    from kestrel_pi.agent import policies
    c, agent_dir = _make(tmp_path)
    _active(agent_dir)
    _remote(agent_dir)
    _contest_stubs(monkeypatch, {"baseline": 0.0, "incumbent": 0.2, "remote": 0.5})
    assert c._run_contest("remote") == "promoted"
    assert _active_content(agent_dir) == "REMOTE-CAND"
    assert policies.read_meta(str(agent_dir), "DQN")["source"] == "remote"
    assert c._promotion_tally["remote"] == {"won": 1, "lost": 0}


def test_a_losing_remote_is_recorded_and_never_judged_again(tmp_path, monkeypatch):
    from kestrel_pi.agent import EVALUATED_NAME, REMOTE_STAGING_DIR, metrics
    c, agent_dir = _make(tmp_path)
    _active(agent_dir)
    _remote(agent_dir)
    calls = []
    _contest_stubs(monkeypatch, {"baseline": 0.0, "incumbent": 0.5, "remote": 0.1},
                   calls=calls)
    assert c._run_contest("remote") == "kept"
    judged = json.loads((agent_dir / REMOTE_STAGING_DIR / EVALUATED_NAME).read_text())
    assert judged == [metrics.model_fingerprint(_meta())]
    # The same model staged again is dropped without another evaluation.
    _remote(agent_dir)
    assert c._run_contest("remote") == "none"
    assert len(calls) == 1


def test_the_rule_is_the_same_for_both_sources(tmp_path, monkeypatch):
    # Identical numbers decide identically whether the candidate was trained on
    # the device or on a remote host.
    outcomes = []
    for source in ("local", "remote"):
        (tmp_path / source).mkdir()
        c, agent_dir = _make(tmp_path / source)
        _active(agent_dir)
        (_local if source == "local" else _remote)(agent_dir)
        for means in ({"baseline": 0.0, "incumbent": 0.2, source: 0.5},
                      {"baseline": 0.0, "incumbent": 0.2, source: 0.21}):
            _contest_stubs(monkeypatch, means)
            (_local if source == "local" else _remote)(agent_dir, latest=means[source])
            outcomes.append((source, c._run_contest(source)))
    assert [o for _s, o in outcomes[:2]] == [o for _s, o in outcomes[2:]]


def test_a_spec_mismatch_is_discarded_unscored(tmp_path, monkeypatch):
    from kestrel_pi.agent import spec
    c, agent_dir = _make(tmp_path)
    _active(agent_dir)
    _remote(agent_dir, spec_version=spec.SPEC_VERSION + 1)
    calls = []
    _contest_stubs(monkeypatch, {"baseline": 0.0, "incumbent": 0.2, "remote": 0.9},
                   calls=calls)
    assert c._run_contest("remote") == "none"
    assert calls == [] and _active_content(agent_dir) == "ACTIVE"


def test_bootstrap_adopts_without_held_out_rows(tmp_path, monkeypatch):
    c, agent_dir = _make(tmp_path)
    _remote(agent_dir)                      # no active model at all
    _contest_stubs(monkeypatch, None)
    assert c._run_contest("remote") == "promoted"
    assert _active_content(agent_dir) == "REMOTE-CAND"


def test_bootstrap_still_needs_to_beat_the_baseline(tmp_path, monkeypatch):
    c, agent_dir = _make(tmp_path)
    _local(agent_dir)
    _contest_stubs(monkeypatch, {"baseline": 0.3, "local": 0.1})
    assert c._run_contest("local") == "kept"


def test_too_few_rows_defers_and_a_legacy_model_is_scored_on_newer_rows(tmp_path, monkeypatch):
    from kestrel_pi.agent import CANDIDATES_DIR, model_meta_name_for
    c, agent_dir = _make(tmp_path)
    _active(agent_dir, latest=500.0)
    # The active model predates held-out rows: no "holdout" in its sidecar.
    meta_path = agent_dir / model_meta_name_for("DQN")
    meta = json.loads(meta_path.read_text())
    del meta["holdout"]
    meta_path.write_text(json.dumps(meta))
    _local(agent_dir)
    calls = []
    _contest_stubs(monkeypatch, None, calls=calls)
    assert c._run_contest("local") == "deferred"
    assert calls[0]["min_ts"] == 500.0
    assert c._contest_deferred is True
    # The candidate waits for the retry.
    assert list((agent_dir / CANDIDATES_DIR / "local" / "dqn").iterdir())
    assert c._last_decision["result"] == "deferred"


def test_both_candidates_at_once_the_better_wins(tmp_path, monkeypatch):
    c, agent_dir = _make(tmp_path)
    _active(agent_dir)
    _local(agent_dir)
    _remote(agent_dir)
    _contest_stubs(monkeypatch, {"baseline": 0.0, "incumbent": 0.2,
                                 "local": 0.4, "remote": 0.6})
    assert c._run_contest("remote") == "promoted"
    assert _active_content(agent_dir) == "REMOTE-CAND"
    assert c._promotion_tally == {"local": {"won": 0, "lost": 1},
                                  "remote": {"won": 1, "lost": 0}}


def test_an_unusable_active_model_gives_way(tmp_path, monkeypatch):
    c, agent_dir = _make(tmp_path)
    _active(agent_dir, content="BROKEN")
    _local(agent_dir)
    _contest_stubs(monkeypatch, {"baseline": 0.0}, broken=("model-dqn.zip",))

    from kestrel_pi.agent import policies
    real = policies.load_learned

    def load(algorithm, path, logger=None):
        if str(path) == str(agent_dir / "model-dqn.zip"):
            return None, "corrupt"
        return _FixedPolicy(0), ""
    monkeypatch.setattr(policies, "load_learned", load)
    assert c._run_contest("local") == "promoted"
    assert _active_content(agent_dir) == "LOCAL-CAND"
    assert real is not None


def test_a_finished_training_run_starts_a_contest(tmp_path, monkeypatch):
    c, _agent_dir = _make(tmp_path)
    monkeypatch.setattr(c, "_resources_ok", lambda: True)
    monkeypatch.setattr(c, "_board_can_train", lambda: True)
    monkeypatch.setattr(c, "_spawn_training", lambda *a, **k: 0)
    contests = []
    monkeypatch.setattr(c, "_run_contest", lambda trigger: contests.append(trigger))
    c._run_training("manual", force=True)
    assert contests == ["local"]
    # A failed run (non-zero exit) starts none.
    monkeypatch.setattr(c, "_spawn_training", lambda *a, **k: 1)
    c._run_training("manual", force=True)
    assert contests == ["local"]


def test_the_tally_migrates_persists_and_reaches_the_status(tmp_path, monkeypatch):
    from kestrel_pi.agent import PROMOTION_TALLY_NAME
    c, agent_dir = _make(tmp_path)
    (agent_dir / PROMOTION_TALLY_NAME).write_text(json.dumps({"local": 34, "remote": 0}))
    c._read_promotion_tally()
    assert c._promotion_tally == {"local": {"won": 0, "lost": 0},
                                  "remote": {"won": 0, "lost": 0},
                                  "before_fair_promotion": {"local": 34, "remote": 0}}
    c._bump_promotion_tally("remote", won=True)
    c2 = ctrl_mod.AgentController(
        config_path=str(tmp_path / "config.toml"),
        agent_dir=str(agent_dir), capture_store=str(tmp_path / "cap"))
    assert c2._promotion_tally["remote"] == {"won": 1, "lost": 0}
    assert c2._promotion_tally["before_fair_promotion"]["local"] == 34
    c2._last_decision = {"result": "kept"}
    c2._write_status(True, "learned", "")
    status = json.load(open(agent_dir / STATUS_NAME))
    assert status["promotion_tally"]["remote"]["won"] == 1
    assert status["last_decision"] == {"result": "kept"}


def test_logging_an_episode_counts_toward_the_contest_retry(tmp_path):
    c, _agent_dir = _make(tmp_path)
    before = c._episodes_since_contest
    c._log_episode({"action": 0, "reward": 0.0})
    assert c._episodes_since_contest == before + 1
