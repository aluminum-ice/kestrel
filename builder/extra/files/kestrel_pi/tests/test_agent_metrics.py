"""Agent-vs-sweep metrics (kestrel_pi.agent.metrics).

All figures come from the episode log the controller already writes, attributed
per window by the recorded policy, so they reconcile with the reward and can be
recomputed off-device for the Gate 4 record. Pure stdlib.
"""

import json

from kestrel_pi.agent import metrics


def _row(policy, new_hashlines=0, new_networks=0, discoverable=0,
         window_seconds=30, ts=0.0):
    return {"policy": policy, "new_hashlines": new_hashlines,
            "new_networks": new_networks, "discoverable": discoverable,
            "window_seconds": window_seconds, "ts": ts}


def test_load_rows_reads_jsonl_oldest_first_and_skips_garbage(tmp_path):
    (tmp_path / "001.jsonl").write_text(
        json.dumps(_row("baseline", 1)) + "\n"
        "{ not json\n"
        + json.dumps(_row("learned", 2)) + "\n")
    (tmp_path / "002.jsonl").write_text(json.dumps(_row("baseline", 3)) + "\n")
    (tmp_path / "notes.txt").write_text("ignored\n")
    rows = metrics.load_rows(str(tmp_path))
    assert [r["new_hashlines"] for r in rows] == [1, 2, 3]


def test_load_rows_on_a_missing_dir_is_empty():
    assert metrics.load_rows("/no/such/dir") == []


def test_summarize_splits_by_policy_and_computes_rates():
    rows = [
        _row("baseline", new_hashlines=1, new_networks=1, discoverable=2, window_seconds=3600),
        _row("baseline", new_hashlines=0, new_networks=0, discoverable=2, window_seconds=3600),
        _row("learned", new_hashlines=4, new_networks=2, discoverable=4, window_seconds=3600),
    ]
    summary = metrics.summarize(rows)
    base = summary["baseline"]
    assert base["windows"] == 2
    assert base["handshakes"] == 1
    assert base["hit_rate"] == 0.5          # 1 of 2 windows produced a handshake
    assert base["yield_per_hour"] == 0.5    # 1 handshake over 2 hours
    assert base["opportunity_yield"] == 0.25  # 1 handshake / 4 discoverable AP-windows
    learned = summary["learned"]
    assert learned["windows"] == 1 and learned["handshakes"] == 4
    assert learned["yield_per_hour"] == 4.0   # 4 over 1 hour


def test_summarize_only_lists_policies_with_windows():
    summary = metrics.summarize([_row("baseline", 1)])
    assert "baseline" in summary and "learned" not in summary


def test_summarize_rolling_window_filters_by_timestamp():
    rows = [_row("learned", 1, ts=100), _row("learned", 9, ts=10_000)]
    # now=10_000, 1-minute window keeps only the second row.
    summary = metrics.summarize(rows, window_minutes=1, now=10_000)
    assert summary["window_minutes"] == 1
    assert summary["learned"]["handshakes"] == 9


def test_format_table_renders_both_policies_and_a_no_data_note():
    summary = metrics.summarize([
        _row("baseline", 1, window_seconds=3600),
        _row("learned", 2, window_seconds=3600),
    ])
    table = metrics.format_table(summary)
    assert "agent vs sweep" in table
    assert "baseline" in table and "learned" in table
    assert "handshakes / hour" in table
    assert "no decision windows" in metrics.format_table({"window_minutes": None})


def test_data_watermark_is_latest_ts_and_row_count():
    rows = [_row("learned", ts=100.0), _row("baseline", ts=250.5),
            _row("learned", ts=180.0)]
    wm = metrics.data_watermark(rows)
    assert wm == {"latest_ts": 250.5, "episodes": 3}
    assert metrics.data_watermark([]) == {"latest_ts": 0.0, "episodes": 0}


def test_meta_watermark_defaults_to_oldest_when_absent():
    assert metrics.meta_watermark({}) == (0.0, 0)
    assert metrics.meta_watermark(
        {"data_watermark": {"latest_ts": 42.0, "episodes": 7}}) == (42.0, 7)
    # Junk is tolerated, not fatal.
    assert metrics.meta_watermark({"data_watermark": {"latest_ts": "x"}}) == (0.0, 0)


def test_new_rows_since_counts_only_newer_episodes():
    rows = [_row("learned", ts=10.0), _row("learned", ts=20.0),
            _row("learned", ts=30.0)]
    assert metrics.new_rows_since(rows, 15.0) == 2
    assert metrics.new_rows_since(rows, 0) == 3
    assert metrics.new_rows_since(rows, 30.0) == 0


def test_remote_model_wins_only_when_not_stale():
    fresh_remote = {"data_watermark": {"latest_ts": 200.0, "episodes": 50}}
    stale_remote = {"data_watermark": {"latest_ts": 90.0, "episodes": 30}}
    local = {"data_watermark": {"latest_ts": 100.0, "episodes": 40}}
    assert metrics.remote_model_wins(fresh_remote, local) is True      # newer data
    assert metrics.remote_model_wins(stale_remote, local) is False     # stale remote
    assert metrics.remote_model_wins(local, local) is True             # tie -> remote
    # A remote with a watermark beats a local that has none (oldest).
    assert metrics.remote_model_wins(stale_remote, {}) is True


def test_promotion_decision_bootstraps_when_no_local_model():
    # No incumbent: adopt the remote even with no scores (the only way a board
    # that can't train locally ever gets a model). Judged before scoring.
    promote, reason = metrics.promotion_decision({}, has_local=False)
    assert promote is True
    assert "bootstrap" in reason


def test_promotion_decision_needs_a_remote_score():
    promote, reason = metrics.promotion_decision(
        {"local": 1.0, "baseline": 0.0}, has_local=True)
    assert promote is False
    assert "no remote score" in reason


def test_promotion_decision_promotes_a_clear_remote_win():
    # Remote lift 0.8, local lift 0.4 over baseline: 0.8 >= 1.1 * 0.4 and the
    # absolute gap clears the floor -> promote.
    scores = {"remote": 0.8, "local": 0.4, "baseline": 0.0}
    promote, reason = metrics.promotion_decision(scores, has_local=True)
    assert promote is True
    assert "remote wins" in reason


def test_promotion_decision_keeps_local_when_margin_not_met():
    # Remote only marginally above local (0.42 vs 0.40): under the 10% margin.
    scores = {"remote": 0.42, "local": 0.40, "baseline": 0.0}
    promote, reason = metrics.promotion_decision(scores, has_local=True)
    assert promote is False
    assert "as good or better" in reason


def test_promotion_decision_rejects_a_remote_below_baseline():
    scores = {"remote": -0.1, "local": 0.5, "baseline": 0.0}
    promote, reason = metrics.promotion_decision(scores, has_local=True)
    assert promote is False
    assert "baseline" in reason


def test_promotion_decision_staleness_guard_keeps_local():
    # A strong remote win is still rejected when the device has logged far more
    # new episodes than the staleness bound since the remote trained.
    scores = {"remote": 0.9, "local": 0.1, "baseline": 0.0}
    promote, reason = metrics.promotion_decision(
        scores, has_local=True, new_since_remote=1000, max_stale_episodes=900)
    assert promote is False
    assert "stale" in reason
    # Within the bound, the same scores promote.
    promote, _ = metrics.promotion_decision(
        scores, has_local=True, new_since_remote=100, max_stale_episodes=900)
    assert promote is True
    # A 0 bound disables the guard.
    promote, _ = metrics.promotion_decision(
        scores, has_local=True, new_since_remote=10_000, max_stale_episodes=0)
    assert promote is True


def test_promotion_decision_does_not_churn_on_near_zero_lifts():
    # Both models barely beat the baseline: the ratio test can pass but the
    # absolute-improvement guard keeps the incumbent so pulls don't flip-flop.
    scores = {"remote": 0.0006, "local": 0.0004, "baseline": 0.0}
    promote, _ = metrics.promotion_decision(scores, has_local=True)
    assert promote is False
