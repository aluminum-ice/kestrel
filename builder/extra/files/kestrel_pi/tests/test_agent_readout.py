"""The agent dashboard readout: the training indicator in particular."""

from kestrel_pi.agent import readout


def _labels(elements):
    return {e["label"]: e["value"] for e in elements}


def test_training_in_progress_shows_a_live_indicator():
    els = readout.widget_elements({
        "running": True, "policy": "learned", "algorithm": "PPO",
        "training": True, "last_trained": 1000.0, "last_train_secs": 42.0,
    })
    labels = _labels(els)
    assert labels.get("Training") == "in progress…"
    # While training, the "last trained" rows are superseded by the live row.
    assert "Last trained" not in labels


def test_last_trained_shows_relative_time_and_duration():
    now = 10_000.0
    els = readout.widget_elements({
        "running": True, "policy": "baseline", "algorithm": "RecurrentPPO",
        "training": False,
        "last_trained": now - 7 * 60,       # 7 minutes ago
        "last_train_secs": 125.0,           # 2m 05s
    })
    labels = _labels(els)
    assert "Training" not in labels
    # _ago uses wall-clock now; just assert the shape is a relative string.
    assert labels["Last trained"].endswith("ago") or labels["Last trained"] == "just now"
    assert labels["Last train time"] == "2m 05s"


def test_model_source_shows_local_or_remote_only_when_learned():
    local = _labels(readout.widget_elements({
        "running": True, "policy": "learned", "algorithm": "PPO",
        "model_source": "local"}))
    assert local["Model source"] == "On-device"
    remote = _labels(readout.widget_elements({
        "running": True, "policy": "learned", "algorithm": "PPO",
        "model_source": "remote"}))
    assert remote["Model source"] == "Remote host"
    # Not shown on baseline (no learned model drives) or when the source is unknown.
    base = _labels(readout.widget_elements({
        "running": True, "policy": "baseline", "algorithm": "PPO",
        "model_source": "remote"}))
    assert "Model source" not in base
    unknown = _labels(readout.widget_elements({
        "running": True, "policy": "learned", "algorithm": "PPO"}))
    assert "Model source" not in unknown


def test_promotion_tally_shows_local_and_remote_counts():
    els = readout.widget_elements({
        "running": True, "policy": "learned", "algorithm": "RecurrentPPO",
        "promotion_tally": {"local": 3, "remote": 1}})
    assert _labels(els)["Promotions"] == "3 on-device / 1 remote"


def test_promotion_tally_shown_once_either_side_has_happened():
    # A device that has only trained locally still sees its count.
    local_only = _labels(readout.widget_elements({
        "running": True, "policy": "baseline", "algorithm": "PPO",
        "promotion_tally": {"local": 2, "remote": 0}}))
    assert local_only["Promotions"] == "2 on-device / 0 remote"
    # Nothing to show before either side has adopted a model.
    none_yet = _labels(readout.widget_elements({
        "running": True, "policy": "baseline", "algorithm": "PPO",
        "promotion_tally": {"local": 0, "remote": 0}}))
    assert "Promotions" not in none_yet
    missing = _labels(readout.widget_elements({
        "running": True, "policy": "baseline", "algorithm": "PPO"}))
    assert "Promotions" not in missing


def test_no_training_record_shows_neither_row():
    els = readout.widget_elements({
        "running": True, "policy": "baseline", "algorithm": "RecurrentPPO",
    })
    labels = _labels(els)
    assert "Training" not in labels and "Last trained" not in labels


def test_ago_and_dur_formatting():
    now = 1_000_000.0
    assert readout._ago(now - 5, now) == "just now"
    assert readout._ago(now - 90, now) == "1m ago"
    assert readout._ago(now - 2 * 3600, now) == "2h ago"
    assert readout._ago(now - 3 * 86400, now) == "3d ago"
    assert readout._dur(34) == "34s"
    assert readout._dur(125) == "2m 05s"
