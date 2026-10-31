"""The `updates` plugin's read-only alert half: version parsing/compare, the
state it computes from a release + its manifest, and the dashboard card. The
upgrade *apply* path is a root CLI (kestrel update apply) covered
elsewhere; here nothing is installed and the network is faked."""

import importlib.util
import json
import os
import types

from kestrel_pi.capture import CaptureInterface
from kestrel_pi.context import PluginContext, ServiceRegistry
from kestrel_pi.ui import StubUI, UIState

PLUGIN_DIR = os.path.normpath(
    os.path.join(os.path.dirname(__file__), "..", "..", "plugins")
)


def _load():
    path = os.path.join(PLUGIN_DIR, "updates.py")
    spec = importlib.util.spec_from_file_location("updates_under_test", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _plugin(tmp_path, mod, installed="0.4.1", online=False, repo="o/r"):
    """An Updates instance with the version stamp and state file pointed at
    tmp_path, so nothing touches the real /etc or /var paths."""
    mod.VERSION_FILE = str(tmp_path / "version")
    mod.STATE_DIR = str(tmp_path / "state")
    mod.STATE_FILE = str(tmp_path / "state" / "state.json")
    if installed is not None:
        (tmp_path / "version").write_text(installed + "\n")
    plugin = mod.Updates()
    options = {"repo": repo, "check_interval_seconds": 21600, "timeout": 30}
    plugin.options = options
    plugin.context = PluginContext(
        name="updates", options=options,
        capture=CaptureInterface(str(tmp_path)),
        connectivity=types.SimpleNamespace(online=online),
        services=ServiceRegistry(), capabilities=("network", "ui"),
    )
    plugin.on_loaded()
    # Stop the background worker so tests drive the check synchronously (via
    # _tick / _maybe_check / _force_check); the hooks only wake the worker.
    plugin._stop.set()
    plugin._wake.set()
    plugin._worker.join(timeout=2)
    return plugin


def _card(plugin):
    ui = StubUI()
    view = ui.for_plugin("updates")
    plugin.on_ui_setup(view)
    plugin.on_ui_update(view, UIState())
    return {e["name"]: e for e in ui.widgets()["updates"]}


class _Resp:
    """A streamed ``requests`` response: the plugin reads the body through
    iter_content (capped) and closes it."""

    def __init__(self, code, body):
        self.status_code = code
        self._body = body
        self.closed = False

    def iter_content(self, chunk_size=1):
        data = json.dumps(self._body).encode("utf-8")
        for i in range(0, len(data), chunk_size):
            yield data[i:i + chunk_size]

    def close(self):
        self.closed = True


def _release(tag, manifest=None):
    assets = []
    if manifest is not None:
        assets = [{"name": "update-manifest.json",
                   "browser_download_url": "https://example.test/m.json"}]
    return {"tag_name": tag, "html_url": "https://example.test/r/" + tag,
            "assets": assets}


# -- pure helpers ------------------------------------------------------------
def test_parse_version():
    mod = _load()
    assert mod.parse_version("0.4.3") == (0, 4, 3)
    assert mod.parse_version("v0.4.3") == (0, 4, 3)
    assert mod.parse_version("0.4.3-rc1") == (0, 4, 3)
    assert mod.parse_version("0.4") == (0, 4)
    assert mod.parse_version("master") is None
    assert mod.parse_version("") is None
    assert mod.parse_version(None) is None


def test_is_newer():
    mod = _load()
    assert mod.is_newer("0.4.2", "0.4.1") is True
    assert mod.is_newer("0.4.1", "0.4.1") is False
    assert mod.is_newer("0.4.0", "0.4.1") is False
    assert mod.is_newer("1.0.0", "0.9.9") is True
    # a dev / unparseable installed version is never "older" (we don't nag it)
    assert mod.is_newer("0.4.2", "master") is False


def test_read_installed_version(tmp_path):
    mod = _load()
    path = tmp_path / "version"
    path.write_text("0.4.1\n")
    assert mod.read_installed_version(str(path)) == "0.4.1"
    assert mod.read_installed_version(str(tmp_path / "missing")) == ""


# -- state computation -------------------------------------------------------
def test_up_to_date(tmp_path):
    mod = _load()
    plugin = _plugin(tmp_path, mod, installed="0.4.2", online=False)
    state = plugin._compute_state("0.4.2", _release("0.4.2"), None)
    assert state["update_available"] is False
    assert state["offerable"] is False


def test_app_layer_update_is_offerable(tmp_path):
    mod = _load()
    plugin = _plugin(tmp_path, mod, installed="0.4.1", online=False)
    manifest = {"kind": "app-layer", "commit": "abc123"}
    state = plugin._compute_state("0.4.1", _release("0.4.2", manifest), manifest)
    assert state["update_available"] is True
    assert state["offerable"] is True
    assert state["kind"] == "app-layer"
    assert state["commit"] == "abc123"


def test_reflash_update_is_not_offerable(tmp_path):
    mod = _load()
    plugin = _plugin(tmp_path, mod, installed="0.4.1", online=False)
    manifest = {"kind": "reflash",
                "requires_reflash_reason": "new kernel + apt packages"}
    state = plugin._compute_state("0.4.1", _release("0.5.0", manifest), manifest)
    assert state["update_available"] is True
    assert state["offerable"] is False
    assert state["kind"] == "reflash"
    assert "kernel" in state["reason"]


def test_no_manifest_falls_back_to_reflash(tmp_path):
    mod = _load()
    plugin = _plugin(tmp_path, mod, installed="0.4.1", online=False)
    state = plugin._compute_state("0.4.1", _release("0.4.2"), None)
    assert state["update_available"] is True
    assert state["offerable"] is False
    assert "manifest" in state["reason"]


def test_min_update_from_forces_reflash(tmp_path):
    # An app-layer release that cannot be applied on top of a version this old
    # is downgraded to "needs reflash" rather than offered.
    mod = _load()
    plugin = _plugin(tmp_path, mod, installed="0.3.0", online=False)
    manifest = {"kind": "app-layer", "min_update_from": "0.4.0"}
    state = plugin._compute_state("0.3.0", _release("0.4.2", manifest), manifest)
    assert state["update_available"] is True
    assert state["offerable"] is False
    assert state["kind"] == "reflash"


def test_a_stable_release_with_a_beta_floor_is_offered_only_from_that_beta_on(tmp_path):
    # A stable release that needs a reflash from the previous stable one but not
    # from the betas of its cycle carries min_update_from = the oldest of those
    # betas: offered on it and later betas, reflash for the previous stable.
    mod = _load()
    manifest = {"kind": "app-layer", "min_update_from": "1.0.11-beta.1"}
    for installed in ("1.0.11-beta.1", "1.0.11-beta.2"):
        plugin = _plugin(tmp_path, mod, installed=installed, online=False)
        state = plugin._compute_state(installed, _release("1.0.11", manifest), manifest)
        assert state["offerable"] is True, (installed, state)
    plugin = _plugin(tmp_path, mod, installed="1.0.10", online=False)
    state = plugin._compute_state("1.0.10", _release("1.0.11", manifest), manifest)
    assert state["offerable"] is False and state["kind"] == "reflash"
    assert "1.0.11-beta.1" in state["reason"]


def test_dev_build_is_never_offered(tmp_path):
    mod = _load()
    plugin = _plugin(tmp_path, mod, installed="master", online=False)
    manifest = {"kind": "app-layer"}
    state = plugin._compute_state("master", _release("0.4.2", manifest), manifest)
    assert state["installed_is_dev"] is True
    assert state["update_available"] is False
    assert state["offerable"] is False


# -- the check writes shared state + drives the card -------------------------
def test_check_writes_state_and_card(tmp_path, monkeypatch):
    import requests

    mod = _load()
    plugin = _plugin(tmp_path, mod, installed="0.4.1", online=False)
    manifest = {"kind": "app-layer", "commit": "deadbeef"}

    def fake_get(url, **kwargs):
        if "/releases?" in url:
            return _Resp(200, [_release("0.4.2", manifest)])
        return _Resp(200, manifest)        # the manifest asset

    monkeypatch.setattr(requests, "get", fake_get)
    plugin._last_check = None
    plugin._check()

    state = json.load(open(mod.STATE_FILE))
    assert state["latest"] == "0.4.2"
    assert state["offerable"] is True
    assert state["installed"] == "0.4.1"

    card = _card(plugin)
    assert card["installed"]["value"] == "0.4.1"
    assert card["latest"]["value"] == "0.4.2"
    assert card["status"]["value"] == "update available"
    # the /plugins health note points the operator at the upgrade
    assert "0.4.2" in plugin.context.health_note


def test_check_up_to_date_card(tmp_path, monkeypatch):
    import requests

    mod = _load()
    plugin = _plugin(tmp_path, mod, installed="0.4.2", online=False)
    monkeypatch.setattr(requests, "get",
                        lambda url, **k: _Resp(200, [_release("0.4.2")]))
    plugin._last_check = None
    plugin._check()
    assert _card(plugin)["status"]["value"] == "up to date"
    assert plugin.context.health_note == ""


def test_check_handles_no_release(tmp_path, monkeypatch):
    import requests

    mod = _load()
    plugin = _plugin(tmp_path, mod, installed="0.4.1", online=False)
    monkeypatch.setattr(requests, "get", lambda url, **k: _Resp(404, {}))
    plugin._last_check = None
    plugin._check()
    state = json.load(open(mod.STATE_FILE))
    assert state["update_available"] is False


def test_check_is_throttled(tmp_path, monkeypatch):
    import requests

    mod = _load()
    plugin = _plugin(tmp_path, mod, installed="0.4.1", online=False)
    calls = []
    monkeypatch.setattr(requests, "get",
                        lambda url, **k: calls.append(1) or _Resp(200, [_release("0.4.1")]))
    plugin._maybe_check()            # first check runs
    plugin._maybe_check()            # within the interval -> skipped
    assert calls == [1]


# -- forced "check now" (the CLI `update check` / web button) ----------------
def _trigger_path(mod):
    return os.path.join(mod.STATE_DIR, mod.CHECK_TRIGGER_NAME)


def test_consume_check_request_is_one_shot(tmp_path):
    mod = _load()
    plugin = _plugin(tmp_path, mod, installed="0.4.1", online=False)
    assert plugin._consume_check_request() is False          # no trigger
    os.makedirs(mod.STATE_DIR, exist_ok=True)
    open(_trigger_path(mod), "w").close()
    assert plugin._consume_check_request() is True           # present -> consumed
    assert not os.path.exists(_trigger_path(mod))            # and removed
    assert plugin._consume_check_request() is False          # only once


def test_force_check_bypasses_the_throttle(tmp_path, monkeypatch):
    import requests

    mod = _load()
    plugin = _plugin(tmp_path, mod, installed="0.4.1", online=False)
    calls = []
    monkeypatch.setattr(requests, "get",
                        lambda url, **k: calls.append(1) or _Resp(200, [_release("0.4.1")]))
    plugin._maybe_check()            # first check runs, arms the throttle
    plugin._maybe_check()            # within the interval -> skipped
    assert calls == [1]
    plugin._force_check()            # forced: runs despite the closed throttle
    assert calls == [1, 1]


def test_worker_tick_forces_a_check_when_the_trigger_is_present(tmp_path, monkeypatch):
    # The worker pass (what a hook wakes) consumes a pending "check now" trigger
    # and forces a check past the interval throttle; with no trigger the throttle
    # still applies.
    import time
    import requests

    mod = _load()
    plugin = _plugin(tmp_path, mod, installed="0.4.1", online=False)
    plugin.context.connectivity.online = True                # now "online"
    calls = []
    monkeypatch.setattr(requests, "get",
                        lambda url, **k: calls.append(1) or _Resp(200, [_release("0.4.1")]))
    plugin._last_check = time.monotonic()                    # throttle closed
    plugin._tick()                                           # no trigger -> skipped
    assert calls == []
    os.makedirs(mod.STATE_DIR, exist_ok=True)
    open(_trigger_path(mod), "w").close()
    plugin._tick()                                           # trigger -> forced check
    assert calls == [1]
    assert not os.path.exists(_trigger_path(mod))            # consumed


def test_offline_keeps_the_trigger_until_online(tmp_path):
    mod = _load()
    plugin = _plugin(tmp_path, mod, installed="0.4.1", online=False)
    os.makedirs(mod.STATE_DIR, exist_ok=True)
    open(_trigger_path(mod), "w").close()
    plugin._tick()                                           # offline -> returns early
    assert os.path.exists(_trigger_path(mod))                # kept for when online


def test_hooks_only_nudge_the_worker(tmp_path):
    # The release check (several GitHub requests) runs on the worker, never
    # inline in a hook: a reconnect / epoch just wakes it, so a slow check can't
    # hold a dispatch worker and be quarantined by the stuck ceiling.
    mod = _load()
    plugin = _plugin(tmp_path, mod, installed="0.4.1", online=False)
    hit = {"tick": 0}
    plugin._tick = lambda: hit.__setitem__("tick", hit["tick"] + 1)

    plugin._wake.clear()
    plugin.on_epoch(1, {})
    assert plugin._wake.is_set()
    plugin._wake.clear()
    plugin.on_internet_available()
    assert plugin._wake.is_set()
    assert hit["tick"] == 0                                  # no check inline


# -- Forgejo API, private-repo token auth -----------------------------------
HOST = "https://git.3ntropy.io"


def _release_on_host(tag, host=HOST):
    """A release whose manifest is served from the Forgejo host, as Forgejo
    reports it (assets only carry a browser_download_url)."""
    return {
        "tag_name": tag,
        "html_url": "%s/o/r/releases/tag/%s" % (host, tag),
        "assets": [{
            "name": "update-manifest.json",
            "browser_download_url":
                "%s/o/r/releases/download/%s/update-manifest.json" % (host, tag),
        }],
    }


def _recording_get(calls, release, manifest, releases=None):
    def fake_get(url, headers=None, **kwargs):
        calls.append((url, dict(headers or {})))
        if "/releases?" in url:
            return _Resp(200, releases if releases is not None else [release])
        return _Resp(200, manifest)
    return fake_get


def test_check_uses_the_forgejo_api_on_the_default_host(tmp_path, monkeypatch):
    import requests

    mod = _load()
    plugin = _plugin(tmp_path, mod, installed="0.4.1", online=False)
    calls = []
    monkeypatch.setattr(requests, "get", _recording_get(
        calls, _release_on_host("0.4.2"), {"kind": "app-layer", "commit": "c"}))
    plugin._last_check = None
    plugin._check()

    # Both channels read the release list (not /releases/latest, which goes
    # by date) and pick the highest version.
    assert calls[0][0] == HOST + "/api/v1/repos/o/r/releases?limit=50&page=1"
    state = json.load(open(mod.STATE_FILE))
    assert state["host"] == HOST
    assert state["offerable"] is True


def test_token_authenticates_release_and_manifest(tmp_path, monkeypatch):
    # With a token set, every request to the host carries it, including the
    # manifest download from releases/download/ (Forgejo accepts the API token
    # there, so there is no separate asset API URL as on GitHub).
    import requests

    mod = _load()
    plugin = _plugin(tmp_path, mod, installed="0.4.1", online=False)
    plugin.options["token"] = "TKN"
    calls = []
    monkeypatch.setattr(requests, "get", _recording_get(
        calls, _release_on_host("0.4.2"), {"kind": "app-layer", "commit": "cafef00d"}))
    plugin._last_check = None
    plugin._check()

    assert calls and all(c[1].get("Authorization") == "token TKN" for c in calls)
    assert any("/releases/download/0.4.2/" in c[0] for c in calls)
    state = json.load(open(mod.STATE_FILE))
    assert state["offerable"] is True
    assert state["commit"] == "cafef00d"


def test_no_token_is_anonymous(tmp_path, monkeypatch):
    import requests

    mod = _load()
    plugin = _plugin(tmp_path, mod, installed="0.4.1", online=False)
    calls = []
    monkeypatch.setattr(requests, "get", _recording_get(
        calls, _release_on_host("0.4.2"), {"kind": "app-layer", "commit": "abc"}))
    plugin._last_check = None
    plugin._check()

    assert calls and all("Authorization" not in c[1] for c in calls)
    assert json.load(open(mod.STATE_FILE))["offerable"] is True


def test_token_is_never_sent_off_host(tmp_path, monkeypatch):
    # A manifest URL on another server must not receive the token.
    import requests

    mod = _load()
    plugin = _plugin(tmp_path, mod, installed="0.4.1", online=False)
    plugin.options["token"] = "TKN"
    calls = []
    monkeypatch.setattr(requests, "get", _recording_get(
        calls, _release_on_host("0.4.2", host="https://elsewhere.test"),
        {"kind": "app-layer", "commit": "abc"}))
    plugin._last_check = None
    plugin._check()

    off_host = [c for c in calls if c[0].startswith("https://elsewhere.test/")]
    assert off_host and all("Authorization" not in c[1] for c in off_host)


def test_a_custom_host_is_used_and_normalised(tmp_path, monkeypatch):
    import requests

    mod = _load()
    plugin = _plugin(tmp_path, mod, installed="0.4.2", online=False)
    plugin.options["host"] = "https://forgejo.example.org/"
    calls = []
    monkeypatch.setattr(requests, "get", _recording_get(
        calls, _release_on_host("0.4.2", host="https://forgejo.example.org"), None))
    plugin._last_check = None
    plugin._check()
    assert calls[0][0] == ("https://forgejo.example.org/api/v1/repos/o/r/"
                           "releases?limit=50&page=1")


def test_a_non_https_host_is_never_contacted(tmp_path, monkeypatch):
    # The token would go out in clear text, so an http:// host is refused.
    import requests

    mod = _load()
    plugin = _plugin(tmp_path, mod, installed="0.4.1", online=False)
    plugin.options["host"] = "http://forgejo.example.org"
    plugin.options["token"] = "TKN"
    calls = []
    monkeypatch.setattr(requests, "get", lambda url, **k: calls.append(url))
    plugin._last_check = None
    plugin._check()
    assert calls == []


def test_release_list_reads_a_second_page(tmp_path, monkeypatch):
    # Forgejo pages at 50: a full first page means there may be more.
    import requests

    mod = _load()
    plugin = _plugin(tmp_path, mod)
    pages = {1: [{"tag_name": "0.%d.0" % i} for i in range(50)],
             2: [{"tag_name": "1.0.0"}]}
    seen = []

    def fake_get(url, **kwargs):
        page = int(url.rsplit("page=", 1)[1])
        seen.append(page)
        return _Resp(200, pages.get(page, []))

    monkeypatch.setattr(requests, "get", fake_get)
    releases, complete = plugin._fetch_releases("o/r", 30)
    assert seen == [1, 2]
    assert len(releases) == 51 and complete is True


def test_release_list_stops_at_the_page_cap(tmp_path, monkeypatch):
    import requests

    mod = _load()
    plugin = _plugin(tmp_path, mod)
    seen = []

    def fake_get(url, **kwargs):
        seen.append(url)
        return _Resp(200, [{"tag_name": "0.0.%d" % i} for i in range(50)])

    monkeypatch.setattr(requests, "get", fake_get)
    releases, complete = plugin._fetch_releases("o/r", 30)
    assert len(releases) == 100 and complete is False     # capped, maybe more
    assert len(seen) == mod.RELEASES_MAX_PAGES


# -- merged release notes (the dashboard "What's new" link) ------------------
def test_check_captures_release_notes_in_range(tmp_path, monkeypatch):
    # When an update is available the plugin also collects the notes for every
    # release strictly newer than the installed one, up to the latest - newest
    # first, excluding the installed version, anything past the latest, and drafts.
    import requests

    mod = _load()
    plugin = _plugin(tmp_path, mod, installed="0.4.1", online=False)
    manifest = {"kind": "app-layer", "commit": "deadbeef"}
    # The intermediate 0.4.2 carries an app-layer manifest so the reflash-gap
    # scan finds a clean chain and the update stays offerable (the point of this
    # test is the notes range, not the gap - the gap behaviour is tested below).
    gap_asset = [{"name": "update-manifest.json",
                  "browser_download_url": "https://example.test/m.json"}]
    releases = [
        {"tag_name": "0.4.2", "published_at": "2026-01-02T00:00:00Z",
         "body": "**Fixes**\n\n- fix a thing", "assets": gap_asset},
        {"tag_name": "0.4.3", "published_at": "2026-01-03T00:00:00Z",
         "body": "**New**\n\n- add a feature", "assets": gap_asset},
        {"tag_name": "0.4.1", "published_at": "2026-01-01T00:00:00Z",
         "body": "already installed"},           # == installed -> excluded
        {"tag_name": "0.5.0", "published_at": "2026-02-01T00:00:00Z",
         "body": "future draft", "draft": True},  # draft / past latest -> excluded
    ]

    def fake_get(url, **kwargs):
        if "/releases?" in url:
            return _Resp(200, releases)
        return _Resp(200, manifest)

    monkeypatch.setattr(requests, "get", fake_get)
    plugin._last_check = None
    plugin._check()

    state = json.load(open(mod.STATE_FILE))
    notes = state["notes"]
    assert [n["version"] for n in notes] == ["0.4.3", "0.4.2"]   # newest first
    assert notes[0]["date"] == "2026-01-03"
    assert "add a feature" in notes[0]["body"]
    assert state["offerable"] is True


def test_a_failed_release_list_offers_nothing_and_retries_soon(tmp_path, monkeypatch):
    # The list is what the reflash-gap check reads, so a list that can't be read
    # must never be taken as "nothing in between" (that offered an in-place
    # update over a skipped reflash). Nothing is stored, and the check is
    # retried within RETRY_SECONDS instead of a whole interval later.
    import time
    import requests

    mod = _load()
    plugin = _plugin(tmp_path, mod, installed="0.4.1", online=False)
    full = [_rel_manifest_url("0.4.%d" % (i + 2)) for i in range(50)]

    def fake_get(url, **kwargs):
        if "page=1" in url:
            return _Resp(200, full)           # a full page: there may be more
        if "page=2" in url:
            return _Resp(500, {})             # ...and the rest can't be read
        return _Resp(200, {"kind": "app-layer"})

    monkeypatch.setattr(requests, "get", fake_get)
    plugin._maybe_check()
    assert not os.path.exists(mod.STATE_FILE)
    since = time.monotonic() - plugin._last_check
    assert plugin._interval() - since <= mod.RETRY_SECONDS + 1


def test_a_list_too_short_to_reach_installed_needs_reflash(tmp_path, monkeypatch):
    # The newest 100 releases don't reach back to the installed version, so the
    # releases in between can't all be checked: not offered in place.
    mod = _load()
    plugin = _plugin(tmp_path, mod, installed="0.1.0", online=False)
    monkeypatch.setattr(plugin, "_fetch_manifest",
                        lambda rel, t, tok="": {"kind": "app-layer"})
    releases = [_rel_manifest_url("0.5.%d" % i) for i in range(100)]
    reason = plugin._gap_requires_reflash(releases, "0.1.0", "0.5.99", 30, "",
                                          complete=False)
    assert "too far behind" in reason
    assert plugin._gap_requires_reflash(releases, "0.1.0", "0.5.99", 30, "",
                                        complete=True) == ""


def test_a_malformed_reply_does_not_end_the_worker(tmp_path, monkeypatch):
    # Non-string fields and a manifest that is a list must not raise out of the
    # check; the bad fields are ignored.
    import requests

    mod = _load()
    plugin = _plugin(tmp_path, mod, installed="0.4.1", online=False)
    plugin.context.connectivity.online = True     # after load: no startup check
    bad = {"tag_name": "0.4.2", "body": 7, "published_at": None, "html_url": 3,
           "assets": [{"name": "update-manifest.json",
                       "browser_download_url": "https://example.test/m.json"}]}

    def fake_get(url, **kwargs):
        if "/releases?" in url:
            return _Resp(200, [bad, "junk", {"tag_name": 5}])
        return _Resp(200, ["not", "a", "manifest"])

    monkeypatch.setattr(requests, "get", fake_get)
    plugin._tick()
    state = json.load(open(mod.STATE_FILE))
    assert state["latest"] == "0.4.2"
    assert state["offerable"] is False                  # no usable manifest
    assert state["notes_url"] == ""                     # not an https URL


def test_tick_survives_an_unexpected_error(tmp_path, monkeypatch):
    # An exception out of a check used to end the worker thread, and with it
    # every later check, until the daemon restarted.
    mod = _load()
    plugin = _plugin(tmp_path, mod, installed="0.4.1", online=False)
    plugin.context.connectivity.online = True

    def boom():
        raise KeyError("bug")

    monkeypatch.setattr(plugin, "_check", boom)
    plugin._tick()                                      # must not raise
    assert plugin._run_lock.acquire(blocking=False)     # and released the lock


def test_an_oversized_reply_is_refused(tmp_path, monkeypatch):
    import requests

    mod = _load()
    plugin = _plugin(tmp_path, mod, installed="0.4.1", online=False)
    monkeypatch.setattr(mod, "MAX_RESPONSE_BYTES", 100)
    big = [_release("0.4.2")] * 20
    monkeypatch.setattr(requests, "get", lambda url, **k: _Resp(200, big))
    assert plugin._check() is False
    assert not os.path.exists(mod.STATE_FILE)


def test_a_non_https_release_url_is_not_stored(tmp_path):
    mod = _load()
    plugin = _plugin(tmp_path, mod, installed="0.4.1", online=False)
    rel = dict(_release("0.4.2"), html_url="javascript:alert(1)")
    state = plugin._compute_state("0.4.1", rel, {"kind": "app-layer"})
    assert state["notes_url"] == ""


def test_a_malformed_min_update_from_needs_reflash(tmp_path):
    mod = _load()
    plugin = _plugin(tmp_path, mod, installed="0.4.1", online=False)
    state = plugin._compute_state("0.4.1", _release("0.4.2"),
                                  {"kind": "app-layer", "min_update_from": "soon"})
    assert state["offerable"] is False and "min_update_from" in state["reason"]


def test_an_update_since_the_check_withdraws_the_offer(tmp_path):
    # The stored offer was computed for 0.6.5; the device is now on 0.6.6.
    mod = _load()
    os.makedirs(str(tmp_path / "state"))
    stored = {"installed": "0.6.5", "latest": "0.6.6", "update_available": True,
              "offerable": True, "channel": "stable", "checked_at": 1.0}
    (tmp_path / "state" / "state.json").write_text(json.dumps(stored))
    _plugin(tmp_path, mod, installed="0.6.6")
    state = json.load(open(mod.STATE_FILE))
    assert state["offerable"] is False and state["installed"] == "0.6.6"


# -- reflash gap: an app-layer latest can't skip a reflash in between ---------
def _rel_manifest_url(tag):
    """A release whose manifest asset url encodes its tag, so a fake GET can
    serve a per-release manifest."""
    return {"tag_name": tag, "html_url": "https://example.test/r/" + tag,
            "assets": [{"name": "update-manifest.json",
                        "browser_download_url": "https://example.test/m/%s.json" % tag}]}


def test_gap_reflash_downgrades_app_layer_to_reflash(tmp_path):
    # Direct _compute_state check: a gap_reason flips an app-layer release to
    # "needs reflash", over the app-layer offer.
    mod = _load()
    plugin = _plugin(tmp_path, mod, installed="0.6.3", online=False)
    manifest = {"kind": "app-layer", "commit": "abc"}
    state = plugin._compute_state(
        "0.6.3", _release("0.6.5", manifest), manifest,
        gap_reason="release 0.6.4 in between needs a reflash")
    assert state["update_available"] is True
    assert state["offerable"] is False
    assert state["kind"] == "reflash"
    assert "0.6.4" in state["reason"]


def test_gap_requires_reflash_flags_a_reflash_in_between(tmp_path, monkeypatch):
    mod = _load()
    plugin = _plugin(tmp_path, mod, installed="0.6.3", online=False)
    manifests = {"0.6.5": {"kind": "app-layer"}, "0.6.4": {"kind": "reflash"}}
    monkeypatch.setattr(plugin, "_fetch_manifest",
                        lambda rel, t, tok="": manifests.get(rel["tag_name"]))
    releases = [_rel_manifest_url(t) for t in ("0.6.5", "0.6.4", "0.6.3")]
    reason = plugin._gap_requires_reflash(releases, "0.6.3", "0.6.5", 30, "")
    assert "0.6.4" in reason and "reflash" in reason


def test_gap_clean_when_every_in_between_is_app_layer(tmp_path, monkeypatch):
    mod = _load()
    plugin = _plugin(tmp_path, mod, installed="0.6.3", online=False)
    monkeypatch.setattr(plugin, "_fetch_manifest",
                        lambda rel, t, tok="": {"kind": "app-layer"})
    releases = [_rel_manifest_url(t) for t in ("0.6.5", "0.6.4", "0.6.3")]
    assert plugin._gap_requires_reflash(releases, "0.6.3", "0.6.5", 30, "") == ""


def test_gap_missing_manifest_in_between_is_reflash(tmp_path, monkeypatch):
    mod = _load()
    plugin = _plugin(tmp_path, mod, installed="0.6.3", online=False)
    # 0.6.4 has no readable manifest -> treated as reflash (the safe direction).
    monkeypatch.setattr(plugin, "_fetch_manifest",
                        lambda rel, t, tok="": None if rel["tag_name"] == "0.6.4"
                        else {"kind": "app-layer"})
    releases = [_rel_manifest_url(t) for t in ("0.6.5", "0.6.4", "0.6.3")]
    reason = plugin._gap_requires_reflash(releases, "0.6.3", "0.6.5", 30, "")
    assert "0.6.4" in reason and "manifest" in reason


def test_gap_adjacent_release_offers_in_place(tmp_path, monkeypatch):
    # No release strictly between installed and latest -> nothing to skip.
    mod = _load()
    plugin = _plugin(tmp_path, mod, installed="0.6.4", online=False)
    monkeypatch.setattr(plugin, "_fetch_manifest",
                        lambda rel, t, tok="": {"kind": "reflash"})
    releases = [_rel_manifest_url(t) for t in ("0.6.5", "0.6.4")]
    assert plugin._gap_requires_reflash(releases, "0.6.4", "0.6.5", 30, "") == ""


def test_check_blocks_in_place_when_a_reflash_is_in_the_gap(tmp_path, monkeypatch):
    # End to end: device on 0.6.3, latest 0.6.5 (app-layer), but 0.6.4 in between
    # is reflash-only -> the plugin must withhold the in-place offer.
    import requests

    mod = _load()
    plugin = _plugin(tmp_path, mod, installed="0.6.3", online=False)
    manifests = {"0.6.5": {"kind": "app-layer", "commit": "x"},
                 "0.6.4": {"kind": "reflash",
                           "requires_reflash_reason": "apt + boot change"},
                 "0.6.3": {"kind": "app-layer"}}

    def fake_get(url, **kwargs):
        if "/releases?" in url:
            return _Resp(200, [_rel_manifest_url(t)
                               for t in ("0.6.5", "0.6.4", "0.6.3")])
        if "/m/" in url:
            tag = url.rsplit("/", 1)[1][:-len(".json")]
            return _Resp(200, manifests[tag])
        return _Resp(404, {})

    monkeypatch.setattr(requests, "get", fake_get)
    plugin._last_check = None
    plugin._check()

    state = json.load(open(mod.STATE_FILE))
    assert state["update_available"] is True
    assert state["offerable"] is False          # NOT offered in place
    assert state["kind"] == "reflash"
    assert "0.6.4" in state["reason"]
    assert _card(plugin)["status"]["value"] == "update available — needs reflash"


def test_check_offers_in_place_when_the_gap_is_all_app_layer(tmp_path, monkeypatch):
    # Same shape, but 0.6.4 is app-layer too -> the whole chain is in-place, so
    # 0.6.5 is offered.
    import requests

    mod = _load()
    plugin = _plugin(tmp_path, mod, installed="0.6.3", online=False)
    manifests = {t: {"kind": "app-layer"} for t in ("0.6.5", "0.6.4", "0.6.3")}

    def fake_get(url, **kwargs):
        if "/releases?" in url:
            return _Resp(200, [_rel_manifest_url(t)
                               for t in ("0.6.5", "0.6.4", "0.6.3")])
        if "/m/" in url:
            tag = url.rsplit("/", 1)[1][:-len(".json")]
            return _Resp(200, manifests[tag])
        return _Resp(404, {})

    monkeypatch.setattr(requests, "get", fake_get)
    plugin._last_check = None
    plugin._check()

    state = json.load(open(mod.STATE_FILE))
    assert state["offerable"] is True
    assert state["kind"] == "app-layer"


# -- beta channel: X.Y.Z-beta.N pre-releases, opt-in via the beta option ------
def test_version_key_orders_betas_before_their_release():
    mod = _load()
    order = ["0.6.9", "0.7.0-beta.1", "0.7.0-beta.2", "0.7.0-beta.10",
             "0.7.0", "v0.7.1-beta.1"]
    keys = [mod.version_key(v) for v in order]
    assert keys == sorted(keys)
    assert mod.version_key("0.7.0+build5") == mod.version_key("0.7.0")
    # Only -beta.N is a recognised pre-release; anything else is never offered.
    for bad in ("0.7.0-rc.1", "0.7.0-beta", "0.7.0-beta.x", "0.7.0-dirty",
                "master", "", None, "0.7a.0"):
        assert mod.version_key(bad) is None


def test_is_newer_and_is_prerelease_with_betas():
    mod = _load()
    assert mod.is_newer("0.7.0-beta.2", "0.7.0-beta.1") is True
    assert mod.is_newer("0.7.0", "0.7.0-beta.3") is True
    assert mod.is_newer("0.7.0-beta.1", "0.7.0") is False
    assert mod.is_newer("0.7.0-beta.1", "0.6.9") is True
    assert mod.is_newer("0.7.0-rc.1", "0.6.9") is False
    assert mod.is_prerelease("0.7.0-beta.1") is True
    assert mod.is_prerelease("0.7.0") is False
    assert mod.release_is_prerelease({"tag_name": "0.7.0", "prerelease": True})
    assert mod.release_is_prerelease({"tag_name": "0.7.0-beta.1"})
    assert not mod.release_is_prerelease({"tag_name": "0.7.0"})


def _channel_get(releases, manifests):
    """A fake GET serving a release list and per-tag manifests (via
    _rel_manifest_url)."""
    calls = []

    def fake_get(url, **kwargs):
        calls.append(url)
        if "/releases?" in url:
            return _Resp(200, releases)
        if "/m/" in url:
            tag = url.rsplit("/", 1)[1][:-len(".json")]
            return _Resp(200, manifests[tag]) if tag in manifests else _Resp(404, {})
        return _Resp(404, {})
    return fake_get, calls


def _beta_rel(tag, **extra):
    """A release as Forgejo lists it, flagged a pre-release when it is a beta."""
    rel = _rel_manifest_url(tag)
    rel["prerelease"] = "-beta." in tag
    rel.update(extra)
    return rel


def test_stable_channel_never_offers_a_beta(tmp_path, monkeypatch):
    # A beta flagged as a pre-release, or one only tagged -beta.N, is skipped.
    import requests

    mod = _load()
    plugin = _plugin(tmp_path, mod, installed="0.6.9")
    unflagged = _beta_rel("0.7.1-beta.1", prerelease=False)
    fake, calls = _channel_get([_beta_rel("0.7.0-beta.1"), unflagged,
                                _beta_rel("0.6.9")], {})
    monkeypatch.setattr(requests, "get", fake)
    plugin._check()

    state = json.load(open(mod.STATE_FILE))
    assert state["update_available"] is False
    assert state["channel"] == "stable"
    assert "/releases?limit=50&page=1" in calls[0]


def test_beta_channel_offers_the_highest_version(tmp_path, monkeypatch):
    # Picked by version, not list order: a stable hotfix created after the beta
    # is listed first but is the lower version.
    import requests

    mod = _load()
    plugin = _plugin(tmp_path, mod, installed="0.6.5")
    plugin.options["beta"] = True
    releases = [_beta_rel("0.6.6"), _beta_rel("0.7.0-beta.1"),
                _beta_rel("0.8.0", draft=True), _beta_rel("0.6.5")]
    manifests = {t: {"kind": "app-layer", "commit": t}
                 for t in ("0.6.6", "0.7.0-beta.1")}
    fake, calls = _channel_get(releases, manifests)
    monkeypatch.setattr(requests, "get", fake)
    plugin._check()

    assert "/releases?limit=50&page=1" in calls[0]
    state = json.load(open(mod.STATE_FILE))
    assert state["latest"] == "0.7.0-beta.1"
    assert state["prerelease"] is True and state["channel"] == "beta"
    assert state["offerable"] is True
    assert [n["version"] for n in state["notes"]] == ["0.7.0-beta.1", "0.6.6"]


def test_beta_device_moves_on_to_the_final_release(tmp_path, monkeypatch):
    import requests

    mod = _load()
    plugin = _plugin(tmp_path, mod, installed="0.7.0-beta.2")
    plugin.options["beta"] = True
    releases = [_beta_rel("0.7.0"), _beta_rel("0.7.0-beta.2"),
                _beta_rel("0.7.0-beta.1"), _beta_rel("0.6.5")]
    fake, _calls = _channel_get(releases, {"0.7.0": {"kind": "app-layer"}})
    monkeypatch.setattr(requests, "get", fake)
    plugin._check()

    state = json.load(open(mod.STATE_FILE))
    assert state["latest"] == "0.7.0" and state["prerelease"] is False
    assert state["offerable"] is True
    # A stable target's notes leave out the betas (its own notes cover them).
    assert [n["version"] for n in state["notes"]] == ["0.7.0"]


def test_beta_target_gap_includes_betas(tmp_path, monkeypatch):
    # A stable device that turns beta on: an earlier reflash beta in the gap
    # blocks an in-place jump to a later app-layer beta.
    import requests

    mod = _load()
    plugin = _plugin(tmp_path, mod, installed="0.6.5")
    plugin.options["beta"] = True
    releases = [_beta_rel("0.7.0-beta.2"), _beta_rel("0.7.0-beta.1"),
                _beta_rel("0.6.5")]
    manifests = {"0.7.0-beta.2": {"kind": "app-layer"},
                 "0.7.0-beta.1": {"kind": "reflash"}}
    fake, _calls = _channel_get(releases, manifests)
    monkeypatch.setattr(requests, "get", fake)
    plugin._check()

    state = json.load(open(mod.STATE_FILE))
    assert state["update_available"] is True
    assert state["offerable"] is False
    assert "0.7.0-beta.1" in state["reason"]


def test_stable_target_gap_skips_betas(tmp_path, monkeypatch):
    # A stable target's manifest is built against the previous stable release,
    # so a reflash beta in between is already accounted for and doesn't block.
    import requests

    mod = _load()
    plugin = _plugin(tmp_path, mod, installed="0.6.5")
    releases = [_beta_rel("0.7.0"), _beta_rel("0.7.0-beta.1"), _beta_rel("0.6.5")]
    manifests = {"0.7.0": {"kind": "app-layer"},
                 "0.7.0-beta.1": {"kind": "reflash"}}
    fake, _calls = _channel_get(releases, manifests)
    monkeypatch.setattr(requests, "get", fake)
    plugin._check()

    state = json.load(open(mod.STATE_FILE))
    assert state["offerable"] is True
    assert [n["version"] for n in state["notes"]] == ["0.7.0"]


def test_beta_channel_with_no_release(tmp_path, monkeypatch):
    import requests

    mod = _load()
    plugin = _plugin(tmp_path, mod, installed="0.6.5")
    plugin.options["beta"] = True
    fake, _calls = _channel_get([], {})
    monkeypatch.setattr(requests, "get", fake)
    plugin._check()

    state = json.load(open(mod.STATE_FILE))
    assert state["update_available"] is False
    assert state["checked_at"]


def test_turning_beta_off_withdraws_a_stored_beta_offer(tmp_path):
    # The web Upgrade button and `update apply` act on the state file, so a beta
    # offer found earlier must not survive the toggle being switched off.
    mod = _load()
    os.makedirs(str(tmp_path / "state"))
    stored = {"installed": "0.6.5", "latest": "0.7.0-beta.1",
              "update_available": True, "offerable": True, "commit": "abc",
              "channel": "beta", "checked_at": 1.0, "detail": "kept"}
    (tmp_path / "state" / "state.json").write_text(json.dumps(stored))
    plugin = _plugin(tmp_path, mod, installed="0.6.5")

    state = json.load(open(mod.STATE_FILE))
    assert state["offerable"] is False and state["update_available"] is False
    assert state["latest"] == "" and state["channel"] == "stable"
    assert state["detail"] == "kept"
    assert _card(plugin)["status"]["value"] == "checking…"


def test_a_same_channel_offer_survives_a_reload(tmp_path):
    mod = _load()
    os.makedirs(str(tmp_path / "state"))
    stored = {"installed": "0.6.5", "latest": "0.6.6",
              "update_available": True, "offerable": True, "checked_at": 1.0}
    (tmp_path / "state" / "state.json").write_text(json.dumps(stored))
    _plugin(tmp_path, mod, installed="0.6.5")
    assert json.load(open(mod.STATE_FILE))["offerable"] is True
