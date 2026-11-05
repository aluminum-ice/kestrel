"""Tests for the publisher's release handling (no Ghost, browser or tree):

    python3 -m pytest tools/ghost-guide
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import ghost_guide  # noqa: E402


def test_a_figure_waits_for_the_release_its_feature_ships_in():
    cfg = {"page": {}, "figure": [{"key": "old"},
                                  {"key": "new", "since": "1.0.14"}]}
    keys = lambda tag: [f["key"] for f in ghost_guide.for_release(cfg, tag)["figure"]]
    assert keys("1.0.13") == ["old"]
    assert keys("1.0.14") == ["old", "new"]
    assert keys("1.1.0") == ["old", "new"]
    assert [f["key"] for f in cfg["figure"]] == ["old", "new"]   # cfg untouched


def test_the_shipped_figures_all_parse_their_since():
    for fig in ghost_guide.load_config()["figure"]:
        if fig.get("since"):
            ghost_guide._version_key(fig["since"])
