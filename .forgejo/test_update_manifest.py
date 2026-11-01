"""Tests for the Release workflow's update-manifest builder (update_manifest.py)
and the install line the release notes take from it. Run by `make test`."""

import importlib.util
import io
import json
import os
import subprocess

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))


def _load(name, filename):
    spec = importlib.util.spec_from_file_location(name, os.path.join(HERE, filename))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


um = _load("update_manifest_under_test", "update_manifest.py")
notes = _load("gen_release_notes_under_test", "gen-release-notes.py")


def _rel(tag, **extra):
    return dict({"tag_name": tag, "prerelease": "-beta." in tag, "draft": False}, **extra)


CYCLE = [_rel("1.0.9"), _rel("1.0.10-beta.1"), _rel("1.0.10"), _rel("1.0.11-beta.1"),
         _rel("1.0.11-beta.2"), _rel("1.0.11-beta.3")]


def _changed(build_changed_since):
    """A fake build_changed: True for the tags in ``build_changed_since``."""
    return lambda since, ref: since in build_changed_since


def test_versions_sort_by_semver_precedence():
    tags = ["1.0.10", "1.0.10-beta.2", "1.0.9", "1.0.10-beta.10", "1.0.11-beta.1"]
    assert sorted(tags, key=um.key) == [
        "1.0.9", "1.0.10-beta.2", "1.0.10-beta.10", "1.0.10", "1.0.11-beta.1"]
    assert um.key("1.0") is None and um.key("v1.0.0") is None


def test_previous_release():
    assert um.previous(CYCLE, "1.0.11", stable=True) == "1.0.10"
    assert um.previous(CYCLE, "1.0.11-beta.4", stable=False) == "1.0.11-beta.3"
    assert um.previous(CYCLE, "1.0.11-beta.1", stable=False) == "1.0.10"
    drafts = CYCLE + [_rel("1.0.10-beta.9", draft=True)]
    assert um.previous(drafts, "1.0.10", stable=False) == "1.0.10-beta.1"
    assert um.previous([], "1.0.0", stable=True) == ""


def test_cycle_betas_are_this_cycles_published_betas_newest_first():
    releases = CYCLE + [_rel("1.0.11-beta.4", draft=True)]
    assert um.cycle_betas(releases, "1.0.10", "1.0.11") == [
        "1.0.11-beta.3", "1.0.11-beta.2", "1.0.11-beta.1"]


@pytest.mark.parametrize("update_kind, kind, min_from", [
    ("reflash", "reflash", ""), ("app-layer", "app-layer", "")])
def test_the_maintainers_choice_wins(update_kind, kind, min_from):
    got = um.decide("1.0.11", False, update_kind, "HEAD", CYCLE, _changed({"1.0.10"}))
    assert (got[0], got[2]) == (kind, min_from)


def test_the_first_release_is_reflash():
    assert um.decide("1.0.0", False, "auto", "HEAD", [], _changed(set()))[0] == "reflash"


def test_no_build_change_since_the_previous_stable_is_app_layer():
    assert um.decide("1.0.11", False, "auto", "HEAD", CYCLE, _changed(set())) == (
        "app-layer", "", "")


def test_a_stable_release_is_app_layer_from_the_oldest_beta_with_no_build_change_since():
    # beta.1 changed the build (so 1.0.10 -> 1.0.11 is reflash); nothing in the
    # build changed after it, so devices on beta.1 or later update in place.
    got = um.decide("1.0.11", False, "auto", "HEAD", CYCLE, _changed({"1.0.10"}))
    assert got == ("app-layer", "", "1.0.11-beta.1")


def test_the_floor_stops_at_the_newest_beta_the_build_changed_after():
    # The build changed after beta.2 too, so only beta.3 is a safe start.
    changed = _changed({"1.0.10", "1.0.11-beta.1", "1.0.11-beta.2"})
    assert um.decide("1.0.11", False, "auto", "HEAD", CYCLE, changed)[2] == "1.0.11-beta.3"
    # Every beta is followed by a build change: no floor, reflash for everyone.
    changed = _changed({"1.0.10", "1.0.11-beta.1", "1.0.11-beta.2", "1.0.11-beta.3"})
    kind, reason, min_from = um.decide("1.0.11", False, "auto", "HEAD", CYCLE, changed)
    assert (kind, min_from) == ("reflash", "") and "1.0.10" in reason


def test_a_beta_never_gets_a_floor():
    got = um.decide("1.0.11-beta.4", True, "auto", "HEAD", CYCLE, _changed({"1.0.11-beta.3"}))
    assert got[0] == "reflash" and got[2] == ""


def test_the_manifest_carries_min_update_from_only_when_set():
    data = um.manifest("1.0.11", "app-layer", "a" * 40, "", "1.0.11-beta.1")
    assert data["min_update_from"] == "1.0.11-beta.1" and data["requires_reflash_reason"] == ""
    assert "min_update_from" not in um.manifest("1.0.11", "app-layer", "a" * 40, "", "")


def _git(repo, *args):
    subprocess.run(["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@example.com",
                    *args], check=True, capture_output=True)


def test_build_changed_reads_the_real_diff(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    (repo / "builder").mkdir(parents=True)
    (repo / "builder" / "config.yml").write_text("a: 1\n")
    (repo / "README.md").write_text("x\n")
    _git(repo, "init", "-q")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "one")
    _git(repo, "tag", "1.0.0")
    (repo / "README.md").write_text("y\n")
    _git(repo, "commit", "-qam", "docs")
    _git(repo, "tag", "1.0.1")
    (repo / "builder" / "config.yml").write_text("a: 2\n")
    _git(repo, "commit", "-qam", "build")
    monkeypatch.chdir(repo)
    assert um.build_changed("1.0.0", "1.0.1") is False
    assert um.build_changed("1.0.1", "HEAD") is True
    assert um.build_changed("no-such-tag", "HEAD") is True      # unknown counts as a change


def test_only_the_release_workflow_counts_as_a_build_input(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    (repo / ".forgejo" / "workflows").mkdir(parents=True)
    for name in ("main.yml", "lint.yml"):
        (repo / ".forgejo" / "workflows" / name).write_text("on: push\n")
    _git(repo, "init", "-q")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "one")
    _git(repo, "tag", "1.0.0")
    (repo / ".forgejo" / "workflows" / "lint.yml").write_text("on: [push]\n")
    (repo / ".forgejo" / "workflows" / "sync-github.yml").write_text("on: release\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "ci")
    _git(repo, "tag", "1.0.1")
    (repo / ".forgejo" / "workflows" / "main.yml").write_text("on: workflow_dispatch\n")
    _git(repo, "commit", "-qam", "release workflow")
    monkeypatch.chdir(repo)
    assert um.build_changed("1.0.0", "1.0.1") is False       # lint and a new sync workflow
    assert um.build_changed("1.0.1", "HEAD") is True         # the workflow that builds the image
    assert not um.BUILD_INPUTS.match(".forgejo/workflows/main.yml.bak")


def test_fetch_releases_reads_every_page(monkeypatch):
    pages = {1: [_rel("1.0.%d" % i) for i in range(um.PAGE)], 2: [_rel("0.9.0")]}
    seen = []

    def urlopen(req, timeout):
        seen.append((req.full_url, req.get_header("Authorization")))
        page = int(req.full_url.rsplit("page=", 1)[1].split("&")[0])
        return io.BytesIO(json.dumps(pages.get(page, [])).encode())

    monkeypatch.setattr(um.urllib.request, "urlopen", urlopen)
    got = um.fetch_releases("https://forge.example/api/v1/repos/o/r", "t0k")
    assert len(got) == um.PAGE + 1 and len(seen) == 2
    assert seen[0][1] == "token t0k"


def test_fetch_releases_gives_up_to_an_empty_list(monkeypatch):
    def urlopen(req, timeout):
        raise OSError("down")

    monkeypatch.setattr(um.urllib.request, "urlopen", urlopen)
    assert um.fetch_releases("https://forge.example/api", "") == []


def test_the_release_notes_name_the_floor():
    text = notes.render("1.0.11", "app-layer", "abc1234", "", ["Fix a thing"],
                        "1.0.11-beta.1")
    assert "app-layer on 1.0.11-beta.1 or later" in text and "older devices flash" in text
    plain = notes.render("1.0.11", "app-layer", "abc1234", "", ["Fix a thing"])
    assert "apply in place via the updates plugin._" in plain and "or later" not in plain
