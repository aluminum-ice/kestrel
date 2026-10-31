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


# -- the held-out set ----------------------------------------------------

def test_holdout_is_by_hour_block_deterministic_and_about_a_fifth():
    hours = range(10000)
    held = [h for h in hours if metrics.is_holdout({"ts": h * 3600.0})]
    assert 0.18 <= len(held) / len(hours) <= 0.22
    # Every row in one hour shares the verdict, and it never changes.
    for h in held[:50]:
        assert all(metrics.is_holdout({"ts": h * 3600.0 + s}) for s in (0, 1, 1799, 3599))
    assert held == [h for h in hours if metrics.is_holdout({"ts": h * 3600.0})]


def test_a_row_without_a_timestamp_is_never_held_out():
    assert metrics.is_holdout({}) is False
    assert metrics.is_holdout({"ts": "soon"}) is False


def test_split_holdout_keeps_order_and_covers_every_row():
    rows = [{"ts": i * 1800.0, "i": i} for i in range(400)]
    train, held = metrics.split_holdout(rows)
    assert len(train) + len(held) == 400 and held and train
    assert [r["i"] for r in train] == sorted(r["i"] for r in train)
    assert all(metrics.is_holdout(r) for r in held)
    assert not any(metrics.is_holdout(r) for r in train)


def test_model_fingerprint_identifies_a_model_by_its_sidecar():
    meta = {"algorithm": "PPO", "train_seconds": 12.5,
            "data_watermark": {"latest_ts": 100.0, "episodes": 40}}
    assert metrics.model_fingerprint(meta) == metrics.model_fingerprint(dict(meta))
    assert metrics.model_fingerprint(meta) != metrics.model_fingerprint(
        dict(meta, train_seconds=13.0))
    assert metrics.model_fingerprint({}) == ""


# -- the contest rule ----------------------------------------------------

def _decide(diffs, ch=0.5, inc=0.2, **kw):
    return metrics.contest_decision(diffs, ch, inc, **kw)


def test_contest_promotes_a_clear_significant_win():
    promote, reason = _decide([0.3, 0.25, 0.35, 0.3] * 10)
    assert promote is True and "challenger wins" in reason


def test_contest_keeps_the_incumbent_when_not_better():
    promote, reason = _decide([0.0, -0.1, 0.05, -0.02] * 10, ch=0.21)
    assert promote is False and "not better" in reason


def test_contest_keeps_the_incumbent_when_not_significant():
    # A positive mean that is mostly noise.
    promote, reason = _decide([1.0, -0.9, 1.0, -0.9, 0.9, -1.0], ch=0.5)
    assert promote is False and "not significant" in reason


def test_contest_keeps_the_incumbent_within_the_margin():
    # Significant but tiny: lift 0.21 vs 0.20 is inside the 10% margin.
    promote, reason = _decide([0.01] * 40, ch=0.21, inc=0.20)
    assert promote is False and "margin" in reason
    # A margin of 0 lets it through.
    assert _decide([0.01] * 40, ch=0.21, inc=0.20, margin=0.0)[0] is True


def test_contest_rejects_a_challenger_below_the_baseline():
    promote, reason = _decide([0.5] * 10, ch=-0.1, inc=-0.4)
    assert promote is False and "baseline" in reason


def test_contest_bootstraps_without_an_incumbent_but_not_below_the_baseline():
    assert _decide([], ch=0.3, inc=0.0, has_incumbent=False) == (
        True, "no active model; adopting (bootstrap)")
    assert _decide([], ch=0.0, inc=0.0, has_incumbent=False)[0] is False


def test_contest_staleness_guard():
    win = [0.3] * 20
    assert _decide(win, new_since=1000, max_stale_episodes=900)[0] is False
    assert _decide(win, new_since=100, max_stale_episodes=900)[0] is True
    assert _decide(win, new_since=10_000, max_stale_episodes=0)[0] is True


def test_contest_rule_has_no_notion_of_source():
    # The rule takes no "local"/"remote" argument at all: the same numbers give
    # the same outcome whoever trained either model.
    import inspect
    params = inspect.signature(metrics.contest_decision).parameters
    assert not any("local" in p or "remote" in p for p in params)


