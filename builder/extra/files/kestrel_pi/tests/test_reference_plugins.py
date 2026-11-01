"""Load the shipped reference plugins (the e-ink faces have their own test files)
through the real loader, proving they import, declare valid metadata, and
satisfy provides/requires - the framework half of Gate 3's "all plugins load
and report as loaded" check (the hardware half is on-device)."""

import importlib.util
import json
import logging
import os
import subprocess
import threading
import time
import types

from kestrel_pi.capture import CaptureInterface
from kestrel_pi.connectivity import ConnectivityState
from kestrel_pi.context import PluginContext, ServiceRegistry
from kestrel_pi.loader import Loader
from kestrel_pi.plugins import CAPABILITIES
from kestrel_pi.ui import StubUI, UIState


class _Streamable:
    """What the uploader plugins read from a response (kestrel_pi.httpsafe):
    headers and a streamed body, built here from a fake's json()."""

    headers = {}

    def iter_content(self, chunk_size=1):
        data = json.dumps(self.json()).encode()
        for start in range(0, len(data), chunk_size):
            yield data[start:start + chunk_size]

    def close(self):
        pass


LOG = logging.getLogger("test.reference")
PLUGIN_DIR = os.path.normpath(
    os.path.join(os.path.dirname(__file__), "..", "..", "plugins")
)

FULL_CONFIG = {
    "plugins": {
        "memtemp": {"enabled": True},
        "session-stats": {"enabled": True},
        "gps": {"enabled": True, "source": "gpsd"},
        "wpa-sec": {"enabled": True, "api_key": "testkey"},
        "wigle": {"enabled": True, "api_name": "n", "api_token": "t"},
        "backup": {"enabled": True, "host": "h", "user": "u", "path": "/remote"},
        "ups-ws": {"enabled": True},
        "updates": {"enabled": True},
        "remote-train": {"enabled": True, "train_host": "h", "train_user": "u",
                         "train_path": "/remote"},
        "downloads": {"enabled": True},
        "map": {"enabled": True},
        "ntfy": {"enabled": True},
        "telegram": {"enabled": True},
        "census": {"enabled": True},
    }
}


def _loader(config, store):
    return Loader(
        config=config,
        capture=CaptureInterface(str(store)),
        connectivity=ConnectivityState(),
        services=ServiceRegistry(),
        search_paths=[PLUGIN_DIR],
        logger=LOG,
    )


def test_plugin_dir_exists():
    assert os.path.isdir(PLUGIN_DIR), PLUGIN_DIR


def test_all_reference_plugins_load(tmp_path):
    loader = _loader(FULL_CONFIG, tmp_path)
    loaded = loader.load_all()
    try:
        names = {r.name for r in loaded}
        assert names == {"memtemp", "session-stats", "gps", "wpa-sec", "wigle",
                         "backup", "ups-ws", "updates", "remote-train",
                         "downloads", "map", "ntfy", "telegram", "census"}
        # gps provides position; wigle optionally requires it, so when gps is
        # enabled the loader still orders it ahead.
        order = [r.name for r in loaded]
        assert order.index("gps") < order.index("wigle")
        # census optionally requires remote-train's service: ordered after it.
        assert order.index("remote-train") < order.index("census")
        assert loader.services.has("position")
        # every declared capability is from the fixed set
        for rec in loaded:
            assert set(rec.capabilities) <= set(CAPABILITIES)
    finally:
        for rec in loaded:
            loader.unload(rec)   # stop the gps reader thread


def test_wigle_loads_without_gps_idle(tmp_path):
    # gps is optional now: wigle loads without it, but reports it is idle.
    config = {"plugins": {
        "wigle": {"enabled": True, "api_name": "n", "api_token": "t"},
    }}
    loader = _loader(config, tmp_path)
    loaded = loader.load_all()
    try:
        rec = loader.records["wigle"]
        assert rec.state == "loaded"
        assert not loader.services.has("position")
        assert "idle" in rec.health()["note"]
        assert "position" in rec.health()["optional_requires"]
    finally:
        for rec in loaded:
            loader.unload(rec)


def test_wigle_loads_without_gps_geolocate(tmp_path):
    # With geolocate on and no gps, wigle loads in geolocate-only mode.
    config = {"plugins": {
        "wigle": {"enabled": True, "api_name": "n", "api_token": "t",
                  "geolocate": True},
    }}
    loader = _loader(config, tmp_path)
    loaded = loader.load_all()
    try:
        rec = loader.records["wigle"]
        assert rec.state == "loaded"
        assert "geolocate-only" in rec.health()["note"]
    finally:
        for rec in loaded:
            loader.unload(rec)


def test_wpa_sec_requires_api_key(tmp_path):
    config = {"plugins": {"wpa-sec": {"enabled": True}}}
    loader = _loader(config, tmp_path)
    loader.load_all()
    rec = loader.records["wpa-sec"]
    assert rec.state == "disabled"
    assert "api_key" in rec.reason


def _load_wpa_sec():
    path = os.path.join(PLUGIN_DIR, "wpa-sec.py")
    spec = importlib.util.spec_from_file_location("wpa_sec_under_test", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _quiesce_worker(plugin):
    """Stop a plugin's background worker so a test drives its network passes
    synchronously (via _tick / _upload / _flush) without the thread racing it.
    The hooks still set _wake (a no-op against the stopped worker), which the
    nudge tests check directly."""
    plugin._stop.set()
    plugin._wake.set()
    plugin._worker.join(timeout=2)


def _make_wpa_sec(tmp_path, online=True):
    plugin = _load_wpa_sec().WpaSec()
    options = {
        "api_key": "testkey",
        "endpoint": "https://example.test",
        "download_results": False,
        "results_interval_seconds": 3600,
        "timeout": 30,
    }
    plugin.options = options
    plugin.context = PluginContext(
        name="wpa-sec",
        options=options,
        capture=CaptureInterface(str(tmp_path)),
        connectivity=types.SimpleNamespace(online=online),
        services=ServiceRegistry(),
        capabilities=("capture-read", "network"),
    )
    plugin.on_loaded()
    _quiesce_worker(plugin)
    return plugin


def test_wpa_sec_uploads_queue_as_a_capture(tmp_path):
    # The queue is repackaged into a .cap and uploaded; the raw hashlines are
    # then marked uploaded and persisted so a reboot does not resend them.
    plugin = _make_wpa_sec(tmp_path, online=True)
    sent = {}

    def fake_upload(cap):
        sent["cap"] = cap
        return True

    plugin._convert_to_cap = lambda lines: b"CAP:" + str(len(lines)).encode()
    plugin._upload = fake_upload
    plugin._enqueue("WPA*02*aa*bb*cc*6464*0001*02")
    plugin._enqueue("WPA*01*dd*ee*ff*6565*0002*00")
    plugin._flush()

    assert sent["cap"] == b"CAP:2"
    assert plugin._queue == []
    assert len(plugin._uploaded) == 2
    with open(plugin._state_path) as handle:
        assert len(json.load(handle)) == 2


def test_wpa_sec_convert_invokes_hcxhash2cap(tmp_path, monkeypatch):
    # _convert_to_cap writes the batch to a temp hashfile and drives hcxhash2cap
    # with the 22000 input flag, returning the .cap bytes it produced.
    plugin = _make_wpa_sec(tmp_path, online=False)
    seen = {}

    def fake_run(argv, **kwargs):
        seen["argv"] = argv
        out = argv[argv.index("-c") + 1]
        with open(out, "wb") as handle:
            handle.write(b"\xd4\xc3\xb2\xa1capbytes")
        return types.SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    cap = plugin._convert_to_cap(["WPA*02*aa*bb*cc*6464*0001*02"])

    assert cap.startswith(b"\xd4\xc3\xb2\xa1")
    assert seen["argv"][0] == "hcxhash2cap"
    assert any(a.startswith("--pmkid-eapol=") for a in seen["argv"])
    assert "-c" in seen["argv"]


def test_wpa_sec_hooks_only_nudge_the_worker(tmp_path):
    # The slow convert+upload runs on the worker, never inline in a hook: a
    # handshake queues the hashline and wakes the worker, and a reconnect / epoch
    # just wake it. None of them does network work in the hook itself (which is
    # what the dispatcher's stuck ceiling would otherwise quarantine).
    plugin = _make_wpa_sec(tmp_path, online=True)
    hit = {"flush": 0}
    plugin._flush = lambda: hit.__setitem__("flush", hit["flush"] + 1)

    plugin._wake.clear()
    plugin.on_handshake(types.SimpleNamespace(raw="WPA*02*aa*bb*cc*6464*0001*02"))
    assert plugin._wake.is_set()                       # worker nudged
    assert "WPA*02*aa*bb*cc*6464*0001*02" in plugin._queue   # still enqueues
    assert hit["flush"] == 0                            # but does not upload inline

    plugin._wake.clear()
    plugin.on_internet_available()
    assert plugin._wake.is_set()
    plugin._wake.clear()
    plugin.on_epoch(1, {})
    assert plugin._wake.is_set()
    assert hit["flush"] == 0


def _load_wigle():
    path = os.path.join(PLUGIN_DIR, "wigle.py")
    spec = importlib.util.spec_from_file_location("wigle_under_test", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _make_wigle(tmp_path, position=None, online=True, geolocate=False):
    plugin = _load_wigle().Wigle()
    options = {
        "api_name": "n",
        "api_token": "t",
        "endpoint": "https://wigle.test",
        "geolocate": geolocate,
        "geolocate_max_lookups": 5,
        "timeout": 30,
    }
    plugin.options = options
    services = ServiceRegistry()
    if position is not None:
        services.provide("position", lambda: position, owner="gps")
    plugin.context = PluginContext(
        name="wigle",
        options=options,
        capture=CaptureInterface(str(tmp_path)),
        connectivity=types.SimpleNamespace(online=online),
        services=services,
        capabilities=("capture-read", "location", "network"),
    )
    plugin.on_loaded()
    _quiesce_worker(plugin)
    return plugin


def test_wigle_firstseen_is_wigle_timestamp_format(tmp_path):
    # FirstSeen must be 'YYYY-MM-DD HH:MM:SS' (UTC), never ISO-8601 with T/Z,
    # or WiGLE drops the row. online=False so recording does not try to upload.
    import re

    fix = {"lat": 40.0, "lon": -73.0, "alt": 10.0,
           "fix_time": "2024-01-15T14:30:00Z"}
    plugin = _make_wigle(tmp_path, position=fix, online=False)
    plugin.on_handshake(types.SimpleNamespace(bssid="AA:BB:CC:DD:EE:FF",
                                              essid="net"))

    assert len(plugin._observations) == 1
    ts = plugin._observations[0]["time"]
    assert re.match(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$", ts), ts
    assert "T" not in ts and "Z" not in ts

    csv_text = _load_wigle()._to_wigle_csv(plugin._observations)
    assert csv_text.startswith("WigleWifi-1.4,")
    assert "AA:BB:CC:DD:EE:FF" in csv_text   # colon-form MAC
    assert ts in csv_text


def test_wigle_upload_honors_response_success(tmp_path, monkeypatch):
    # A 200 with success=false is a rejection: the observation stays queued.
    import requests

    fix = {"lat": 40.0, "lon": -73.0, "alt": None}
    plugin = _make_wigle(tmp_path, position=fix, online=False)
    plugin.on_handshake(types.SimpleNamespace(bssid="AA:BB:CC:DD:EE:F0",
                                              essid="a"))
    assert len(plugin._observations) == 1

    class _Resp(_Streamable):
        def __init__(self, code, body):
            self.status_code = code
            self._body = body

        def json(self):
            return self._body

    monkeypatch.setattr(
        requests, "post",
        lambda *a, **k: _Resp(200, {"success": False, "message": "bad token"}))
    plugin._upload()
    assert len(plugin._observations) == 1   # rejected -> still queued

    monkeypatch.setattr(
        requests, "post",
        lambda *a, **k: _Resp(200, {"success": True,
                                    "results": [{"transid": "x"}]}))
    plugin._upload()
    assert plugin._observations == []       # accepted -> dequeued


def test_wigle_upload_survives_results_as_a_dict(tmp_path, monkeypatch):
    # WiGLE returns the upload ``results`` as a single object on some responses,
    # not a list. The old code did results[0] and died with KeyError: 0, killing
    # the worker thread (so wigle silently stopped uploading). A dict results
    # must dequeue cleanly and never raise.
    import requests

    fix = {"lat": 40.0, "lon": -73.0, "alt": None}
    plugin = _make_wigle(tmp_path, position=fix, online=False)
    plugin.on_handshake(types.SimpleNamespace(bssid="AA:BB:CC:DD:EE:F1", essid="a"))

    class _Resp(_Streamable):
        status_code = 200

        def json(self):
            return {"success": True, "results": {"transId": "20261027-00001"}}

    monkeypatch.setattr(requests, "post", lambda *a, **k: _Resp())
    plugin._upload()                         # must not raise
    assert plugin._observations == []        # accepted -> dequeued


def test_wigle_first_record_tolerates_shapes():
    from kestrel_pi.loader import Loader  # noqa: F401 - ensure package importable
    import importlib.util, os
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))),
                        "plugins", "wigle.py")
    spec = importlib.util.spec_from_file_location("wigle_under_test", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    f = mod._first_record
    assert f([{"transid": "a"}, {"transid": "b"}]) == {"transid": "a"}
    assert f({"transId": "c"}) == {"transId": "c"}
    assert f([]) is None
    assert f(None) is None
    assert f("nope") is None
    assert f([1, 2, {"x": 1}]) == {"x": 1}   # first dict item


def test_wigle_401_latches_and_stops_retrying(tmp_path, monkeypatch):
    # A 401 is bad credentials, not a transient error: latch so the retry stops
    # hammering WiGLE and name the fix on the card. The observation stays queued
    # and the latch clears on a reload (a fresh instance).
    import requests

    fix = {"lat": 40.0, "lon": -73.0, "alt": None}
    plugin = _make_wigle(tmp_path, position=fix, online=False)   # queue, no upload
    plugin.on_handshake(types.SimpleNamespace(bssid="AA:BB:CC:DD:EE:91", essid="x"))
    assert len(plugin._observations) == 1

    class _Resp(_Streamable):
        status_code = 401

        def json(self):
            return {}

    posts = []
    monkeypatch.setattr(requests, "post", lambda *a, **k: posts.append(1) or _Resp())
    plugin.context.connectivity.online = True
    plugin._upload()                                 # first attempt: 401
    assert posts == [1]
    assert plugin._auth_failed is True
    assert "401" in plugin._last_status and "api_name" in plugin._last_status
    assert len(plugin._observations) == 1            # stays queued

    # Neither a forced worker pass nor a direct call retries once latched.
    plugin._tick(force=True)                         # worker pass: latched, no post
    plugin._upload()                                 # direct call: latched, no post
    assert posts == [1]                              # no further requests to WiGLE

    reloaded = _make_wigle(tmp_path, position=fix, online=False)
    assert reloaded._auth_failed is False            # a reload clears the latch


def test_wigle_429_backs_off_and_keeps_queue(tmp_path, monkeypatch):
    # A 429 is WiGLE's rate limit, not a failure: show it as rate-limited (not
    # an error), keep the observation queued, and back off so further attempts
    # (handshake / on_epoch) don't hammer WiGLE until the window resets. A
    # success clears the backoff.
    import requests

    fix = {"lat": 40.0, "lon": -73.0, "alt": None}
    plugin = _make_wigle(tmp_path, position=fix, online=False)
    plugin.on_handshake(types.SimpleNamespace(bssid="AA:BB:CC:DD:EE:29", essid="x"))
    assert len(plugin._observations) == 1
    plugin.context.connectivity.online = True

    class _Resp(_Streamable):
        def __init__(self, code, body=None, headers=None):
            self.status_code = code
            self._body = body or {}
            self.headers = headers or {}

        def json(self):
            return self._body

    posts = []
    monkeypatch.setattr(
        requests, "post",
        lambda *a, **k: posts.append(1) or _Resp(429, headers={"Retry-After": "120"}))
    plugin._upload()                                 # first attempt: 429
    assert posts == [1]
    assert "rate-limited" in plugin._last_status
    assert "error" not in plugin._last_status        # not surfaced as an error
    assert len(plugin._observations) == 1            # stays queued
    assert plugin._rate_limited_until > 0

    # During the backoff window, no further request is made (not even via a
    # forced worker pass that ignores the normal retry throttle).
    plugin._tick(force=True)                         # worker pass: still backing off
    plugin._upload()                                 # direct call: still backing off
    assert posts == [1]                              # still just the one attempt

    # Once the window passes, an attempt is made again and a success clears it.
    plugin._rate_limited_until = 0.0
    monkeypatch.setattr(
        requests, "post",
        lambda *a, **k: posts.append(1) or _Resp(200, {"success": True}))
    plugin._upload()
    assert posts == [1, 1]
    assert plugin._observations == []
    assert plugin._last_status == "ok"
    assert plugin._rate_limited_until == 0.0


def test_wigle_retry_after_seconds_parsing():
    w = _load_wigle()
    assert w._retry_after_seconds("90") == 90
    assert w._retry_after_seconds(" 0 ") == 0
    assert w._retry_after_seconds(None) is None
    assert w._retry_after_seconds("") is None
    assert w._retry_after_seconds("not-a-date") is None
    # An HTTP-date in the past clamps to 0, never negative.
    assert w._retry_after_seconds("Wed, 21 Oct 2015 07:28:00 GMT") == 0


def test_wigle_persists_queue_across_reload_and_clears_on_upload(tmp_path, monkeypatch):
    # The queue and dedup set survive a reboot (a fresh instance over the same
    # store), the persisted dedup blocks re-queuing, and a successful upload
    # clears the persisted queue while keeping the dedup set.
    import json as _json
    import requests

    fix = {"lat": 40.0, "lon": -73.0, "alt": None}
    p1 = _make_wigle(tmp_path, position=fix, online=False)
    p1.on_handshake(types.SimpleNamespace(bssid="AA:BB:CC:DD:EE:01", essid="x"))
    assert len(p1._observations) == 1

    p2 = _make_wigle(tmp_path, position=fix, online=False)
    assert len(p2._observations) == 1                 # queue reloaded
    assert "AA:BB:CC:DD:EE:01" in p2._seen            # dedup reloaded
    p2.on_handshake(types.SimpleNamespace(bssid="AA:BB:CC:DD:EE:01", essid="x"))
    assert len(p2._observations) == 1                 # not re-queued

    class _Resp(_Streamable):
        status_code = 200

        def json(self):
            return {"success": True}

    monkeypatch.setattr(requests, "post", lambda *a, **k: _Resp())
    p2._upload()
    assert p2._observations == []
    state = _json.load(open(p2._state_path))
    assert state["queue"] == []
    assert "AA:BB:CC:DD:EE:01" in state["seen"]       # dedup kept after upload


def test_wigle_worker_retries_a_queued_upload(tmp_path, monkeypatch):
    # A handshake / reconnect force an upload, but the worker's periodic pass
    # also retries so a lone queued observation doesn't sit forever once the link
    # is up. Driving the worker's _tick (what a timeout wake does) exercises that.
    import requests

    fix = {"lat": 40.0, "lon": -73.0, "alt": None}
    plugin = _make_wigle(tmp_path, position=fix, online=False)   # queues, no upload
    plugin.on_handshake(types.SimpleNamespace(bssid="AA:BB:CC:DD:EE:A3", essid="x"))
    assert len(plugin._observations) == 1

    class _Resp(_Streamable):
        status_code = 200

        def json(self):
            return {"success": True}

    posts = []
    monkeypatch.setattr(requests, "post", lambda *a, **k: posts.append(1) or _Resp())
    plugin.context.connectivity.online = True                    # link is up now
    plugin._tick()                                               # periodic worker pass
    assert posts == [1] and plugin._observations == []           # retried + cleared


def test_wigle_worker_throttles_retries(tmp_path, monkeypatch):
    # A periodic (non-forced) worker pass respects the retry throttle: a just-
    # attempted upload is not retried again until retry_seconds has elapsed.
    import requests

    fix = {"lat": 40.0, "lon": -73.0, "alt": None}
    plugin = _make_wigle(tmp_path, position=fix, online=False)
    plugin.on_handshake(types.SimpleNamespace(bssid="AA:BB:CC:DD:EE:A4", essid="x"))

    class _Resp(_Streamable):
        status_code = 200

        def json(self):
            return {"success": True}

    posts = []
    monkeypatch.setattr(requests, "post", lambda *a, **k: posts.append(1) or _Resp())
    plugin.context.connectivity.online = True
    plugin._last_attempt = time.monotonic()                      # just attempted
    plugin._tick()                                               # throttled, no retry
    assert posts == []


def test_wigle_hooks_only_nudge_the_worker(tmp_path):
    # The network upload runs on the worker, never inline in a hook. A handshake
    # records the observation and wakes the worker forcing the next pass; a
    # reconnect forces it too; a plain epoch only nudges (no force), so the retry
    # throttle still applies. No hook uploads inline.
    fix = {"lat": 40.0, "lon": -73.0, "alt": None}
    plugin = _make_wigle(tmp_path, position=fix, online=True)
    hit = {"upload": 0}
    plugin._upload = lambda: hit.__setitem__("upload", hit["upload"] + 1)

    plugin._wake.clear()
    plugin._force_next = False
    plugin.on_handshake(types.SimpleNamespace(bssid="AA:BB:CC:DD:EE:A6", essid="x"))
    assert plugin._wake.is_set() and plugin._force_next is True
    assert len(plugin._observations) == 1              # recorded in the hook
    assert hit["upload"] == 0                           # but not uploaded inline

    plugin._wake.clear()
    plugin._force_next = False
    plugin.on_epoch(1, {})
    assert plugin._wake.is_set() and plugin._force_next is False   # nudge, not forced

    plugin._wake.clear()
    plugin.on_internet_available()
    assert plugin._wake.is_set() and plugin._force_next is True
    assert hit["upload"] == 0


def test_wigle_card_shows_upload_status_and_offline(tmp_path, monkeypatch):
    import requests

    fix = {"lat": 40.0, "lon": -73.0, "alt": None}
    # A rejection must be visible on the card (not a silent, growing queue).
    plugin = _make_wigle(tmp_path, position=fix, online=True)

    class _Rejected(_Streamable):
        status_code = 200

        def json(self):
            return {"success": False, "message": "too many queries today"}

    monkeypatch.setattr(requests, "post", lambda *a, **k: _Rejected())
    plugin.on_handshake(types.SimpleNamespace(bssid="AA:BB:CC:DD:EE:A5", essid="x"))
    plugin._upload()                                      # drive the upload (worker path)
    card = _card(plugin, "wigle")
    assert card["queued"]["value"] == "1"                 # stayed queued
    assert "rejected" in card["status"]["value"]          # and the card says why

    # Same queue, but now offline: the card reads as waiting, not an error.
    plugin.context.connectivity.online = False
    assert "offline" in _card(plugin, "wigle")["status"]["value"]


# -- dashboard readouts (Phase 3.5 on_ui_* widgets) --------------------------
def _load_module(filename, modname):
    path = os.path.join(PLUGIN_DIR, filename)
    spec = importlib.util.spec_from_file_location(modname, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _ctx(tmp_path, caps, options=None, online=False, name="p"):
    return PluginContext(
        name=name, options=options or {},
        capture=CaptureInterface(str(tmp_path)),
        connectivity=types.SimpleNamespace(online=online),
        services=ServiceRegistry(), capabilities=caps,
    )


def _card(plugin, owner, state=None):
    """Drive a plugin's on_ui_setup/on_ui_update against a fresh StubUI view and
    return its widget as ``{name: element}``. Hook API 2 passes a UIState
    snapshot to on_ui_update; an empty one is fine for the reference plugins."""
    ui = StubUI()
    view = ui.for_plugin(owner)
    plugin.on_ui_setup(view)
    plugin.on_ui_update(view, state if state is not None else UIState())
    return {element["name"]: element for element in ui.widgets()[owner]}


def test_session_stats_declares_ui_and_shows_a_readout(tmp_path):
    mod = _load_module("session-stats.py", "session_stats_under_test")
    plugin = mod.SessionStats()
    plugin.options = {}
    plugin.context = _ctx(tmp_path, ("ui", "web"), name="session-stats")
    assert "ui" in plugin.__capabilities__
    plugin.on_loaded()
    plugin.on_handshake(types.SimpleNamespace(bssid="AA:BB:CC:DD:EE:01"))
    plugin.on_handshake(types.SimpleNamespace(bssid="AA:BB:CC:DD:EE:01"))  # same AP
    plugin.on_handshake(types.SimpleNamespace(bssid="AA:BB:CC:DD:EE:02"))
    plugin.on_epoch(7, {})

    card = _card(plugin, "session-stats")
    assert card["handshakes"]["value"] == "3"
    assert card["handshakes"]["kind"] == "number"
    assert card["aps"]["value"] == "2" and card["aps"]["label"] == "Unique APs"
    assert card["epochs"]["value"] == "7"
    assert card["uptime"]["value"]            # a formatted uptime string


def test_session_stats_uptime_is_immune_to_a_wall_clock_jump(tmp_path, monkeypatch):
    """Uptime counts from a monotonic clock, so a stale-clock boot that NTP later
    jumps forward (RTC-less Pi) cannot inflate it. A wall-clock delta would have
    swallowed the powered-off gap and looked like it never reset across reboots."""
    mod = _load_module("session-stats.py", "session_stats_uptime")
    plugin = mod.SessionStats()
    plugin.options = {}
    plugin.context = _ctx(tmp_path, ("ui", "web"), name="session-stats")
    plugin.on_loaded()
    # Pretend this daemon launched 1h 01m 05s ago on the monotonic clock.
    plugin._started_monotonic = mod.time.monotonic() - 3665
    # Slam the wall clock to the epoch: if uptime consulted time.time() at all it
    # would go wildly wrong, but the monotonic basis ignores it.
    monkeypatch.setattr(mod.time, "time", lambda: 0.0)
    snap = plugin._snapshot()
    assert 3660 <= snap["uptime_seconds"] <= 3700
    assert _card(plugin, "session-stats")["uptime"]["value"] == "1h 01m"


def test_gps_readout_no_fix_then_3d_fix(tmp_path):
    import threading

    mod = _load_module("gps.py", "gps_under_test")
    plugin = mod.GPS()
    plugin.options = {"source": "gpsd"}
    plugin.context = _ctx(tmp_path, ("location", "capture-read", "ui"), name="gps")
    assert "ui" in plugin.__capabilities__
    # Initialise state without starting the reader thread.
    plugin._fix, plugin._mode, plugin._sats = None, 0, None
    plugin._fix_lock = threading.Lock()

    card = _card(plugin, "gps")
    assert card["status"]["value"] == "no fix"
    assert card["sats"]["value"] == "?"
    assert card["lat"]["value"] == "-" and card["alt"]["value"] == "-"

    plugin._mode, plugin._sats = 3, 9
    plugin._fix = {"lat": 40.1234567, "lon": -73.7654321, "alt": 12.3}
    card = _card(plugin, "gps")
    assert card["status"]["value"] == "3D fix"
    assert card["sats"]["value"] == "9"
    assert card["lat"]["value"] == "40.12346"
    assert card["lon"]["value"] == "-73.76543"
    assert card["alt"]["value"] == "12.3 m"


def test_gps_gpsd_tpv_tracks_mode_sats_and_fix(tmp_path):
    import threading

    mod = _load_module("gps.py", "gps_tpv_under_test")
    plugin = mod.GPS()
    plugin.options = {"source": "gpsd"}
    plugin._fix, plugin._mode, plugin._sats = None, 0, None
    plugin._fix_lock = threading.Lock()
    plugin._handle_gpsd_line(json.dumps({
        "class": "TPV", "mode": 3, "lat": 40.0, "lon": -73.0,
        "alt": 5.0, "uSat": 7}).encode())
    assert plugin._mode == 3 and plugin._sats == 7
    assert plugin.provide_position()["lat"] == 40.0


def test_gps_nmea_gga_tracks_sats_and_fix(tmp_path):
    import threading

    mod = _load_module("gps.py", "gps_nmea_under_test")
    plugin = mod.GPS()
    plugin.options = {"source": "serial"}
    plugin._fix, plugin._mode, plugin._sats = None, 0, None
    plugin._fix_lock = threading.Lock()
    plugin._handle_nmea(
        "$GPGGA,123519,4807.038,N,01131.000,E,1,08,0.9,545.4,M,46.9,M,,*47")
    assert plugin._sats == 8
    assert plugin._mode == 3           # has altitude -> treated as 3D
    assert plugin.provide_position() is not None
    assert plugin._hdop == 0.9         # Phase 4.8: HDOP from GGA field 8


def _bare_gps(mod, source="serial"):
    """A GPS instance with just the state the parsers touch (no reader thread),
    including the Phase 4.8 HDOP/C/N0 fields and the GSV accumulator."""
    import threading

    plugin = mod.GPS()
    plugin.options = {"source": source}
    plugin._fix, plugin._mode, plugin._sats = None, 0, None
    plugin._hdop, plugin._cn0, plugin._gsv = None, None, {}
    plugin._cn0_max, plugin._heard, plugin._strong = None, None, None
    plugin._last_rx = None
    plugin._fix_lock = threading.Lock()
    return plugin


def test_gps_nmea_gsv_yields_a_sane_mean_cn0(tmp_path):
    # Phase 4.8: GSV carries per-satellite C/N0 (the fix-quality signal GGA
    # lacks). A two-sentence group of four SNRs each -> the mean over all eight.
    mod = _load_module("gps.py", "gps_gsv_under_test")
    plugin = _bare_gps(mod)
    plugin._handle_nmea("$GPGSV,2,1,08,01,40,083,42,02,17,308,40,"
                        "03,07,344,38,04,22,228,40*7C")
    assert plugin._cn0 is None          # group not complete yet (2 of 2 missing)
    plugin._handle_nmea("$GPGSV,2,2,08,05,56,187,44,06,25,254,38,"
                        "07,00,000,,08,12,199,36*70")
    # Mean of 42,40,38,40,44,38,36 (the 7 tracked; the empty SNR is skipped).
    assert abs(plugin._cn0 - (42 + 40 + 38 + 40 + 44 + 38 + 36) / 7.0) < 1e-9


def test_gps_gpsd_sky_sets_cn0_and_hdop(tmp_path):
    # Phase 4.8: gpsd's SKY report carries per-satellite SNR (ss) and HDOP.
    mod = _load_module("gps.py", "gps_sky_under_test")
    plugin = _bare_gps(mod, source="gpsd")
    plugin._handle_gpsd_line(json.dumps({
        "class": "SKY", "hdop": 0.8, "uSat": 3,
        "satellites": [
            {"PRN": 1, "ss": 44, "used": True},
            {"PRN": 2, "ss": 40, "used": True},
            {"PRN": 3, "ss": 36, "used": True},
            {"PRN": 4, "ss": 20, "used": False},   # not used -> excluded
        ]}).encode())
    assert plugin._hdop == 0.8
    assert plugin._sats == 3
    assert abs(plugin._cn0 - (44 + 40 + 36) / 3.0) < 1e-9
    # The card's signal stats use every satellite heard (including the unused
    # one at 20), so the peak and the strong-count guide pre-fix acquisition.
    assert plugin._cn0_max == 44
    assert plugin._heard == 4                 # 44, 40, 36, 20 all have an SNR
    assert plugin._strong == 3                # three at >= 30 dB-Hz


def test_gps_gsv_tracks_peak_and_strong_satellite_count(tmp_path):
    # The GSV group carries per-satellite C/N0; the card needs the strongest
    # reading and how many are usable (>= 30 dB-Hz), not just the mean.
    mod = _load_module("gps.py", "gps_gsv_signal_under_test")
    plugin = _bare_gps(mod)
    plugin._handle_nmea("$GPGSV,2,1,08,01,40,083,42,02,17,308,40,"
                        "03,07,344,38,04,22,228,40*7C")
    plugin._handle_nmea("$GPGSV,2,2,08,05,56,187,44,06,25,254,38,"
                        "07,00,000,,08,12,199,36*70")
    # Tracked SNRs 42,40,38,40,44,38,36: peak 44, all seven at >= 30.
    assert plugin._cn0_max == 44
    assert plugin._heard == 7
    assert plugin._strong == 7


def test_gps_signal_readout_interprets_the_cn0(tmp_path):
    # The card turns the strongest-satellite C/N0 into a number plus a plain
    # read, so the operator knows whether to wait or find clearer sky.
    mod = _load_module("gps.py", "gps_signal_card_under_test")

    def read(mode, cn0_max, strong):
        plugin = _bare_gps(mod)
        plugin._mode, plugin._cn0_max, plugin._strong = mode, cn0_max, strong
        card = _card(plugin, "gps")
        return card["signal"]["value"], card["signal_read"]["value"]

    # Nothing heard: no number, and a clear antenna/sky prompt.
    number, text = read(0, None, None)
    assert number == "-" and "no signal" in text
    # An all-weak sky never locks.
    number, text = read(0, 22.0, 0)
    assert number == "22 dB-Hz" and "very weak" in text
    number, text = read(0, 27.0, 0)
    assert "weak" in text and "very weak" not in text
    # A moderate peak is acquiring.
    assert "moderate" in read(0, 32.0, 2)[1]
    # A strong peak with few usable satellites is still acquiring...
    assert "acquiring" in read(0, 38.0, 2)[1]
    # ...but a strong peak with four usable satellites is about to lock.
    assert "lock shortly" in read(0, 38.0, 5)[1]
    # With a fix, it reads as good.
    number, text = read(3, 40.0, 6)
    assert number == "40 dB-Hz" and "good" in text and "locked" in text


def test_gps_card_goes_stale_when_the_receiver_falls_silent(tmp_path):
    # A receiver that stops streaming (unplugged / powered off / stalled link)
    # must not leave a frozen fix and signal on the card; once nothing has
    # arrived for SIGNAL_STALE_S the card shows the dead-receiver state.
    mod = _load_module("gps.py", "gps_stale_under_test")
    plugin = _bare_gps(mod)
    plugin._mode, plugin._sats = 3, 9
    plugin._fix = {"lat": 40.0, "lon": -73.0, "alt": 12.3}
    plugin._cn0_max, plugin._strong = 40.0, 6
    plugin._last_rx = time.monotonic() - (mod.SIGNAL_STALE_S + 30)

    card = _card(plugin, "gps")
    assert card["status"]["value"] == "no signal"
    assert card["signal"]["value"] == "-"
    assert "not responding" in card["signal_read"]["value"]
    assert card["sats"]["value"] == "?"
    assert card["lat"]["value"] == "-" and card["alt"]["value"] == "-"


def test_gps_card_stays_live_while_data_flows(tmp_path):
    # The same fix, but a fresh last-received time, renders normally - staleness
    # must not blank a receiver that is still streaming.
    mod = _load_module("gps.py", "gps_live_under_test")
    plugin = _bare_gps(mod)
    plugin._mode, plugin._sats = 3, 9
    plugin._fix = {"lat": 40.0, "lon": -73.0, "alt": 12.3}
    plugin._cn0_max, plugin._strong = 40.0, 6
    plugin._last_rx = time.monotonic()

    card = _card(plugin, "gps")
    assert card["status"]["value"] == "3D fix"
    assert card["signal"]["value"] == "40 dB-Hz"
    assert card["lat"]["value"] == "40.00000"


def test_gps_any_sentence_marks_the_receiver_alive(tmp_path):
    # Liveness is "data is arriving", not "we got a fix": even a sentence the
    # plugin does not otherwise use (here RMC) refreshes the last-received time,
    # so a receiver emitting only non-GGA/GSV sentences is not flagged silent.
    mod = _load_module("gps.py", "gps_rx_under_test")
    plugin = _bare_gps(mod)
    assert plugin._last_rx is None
    plugin._handle_nmea("$GPRMC,003543.00,V,,,,,,,211026,,,N*7A")
    assert plugin._last_rx is not None


def test_gps_publishes_latest_fix_seam(tmp_path, monkeypatch):
    # Phase 4.8: a new fix writes the agent seam file atomically with the full
    # schema (ts/lat/lon/alt/fix/sats/hdop/cn0).
    mod = _load_module("gps.py", "gps_seam_under_test")
    seam = tmp_path / "run" / "position.json"
    monkeypatch.setattr(mod, "POSITION_FILE", str(seam))
    plugin = _bare_gps(mod)
    plugin._set_sats(8)
    plugin._set_hdop(1.2)
    plugin._set_cn0(39.5)
    plugin._set_mode(3)
    plugin._set_fix(48.1173, 11.5167, 545.4)    # publishes on the fix update
    with open(seam, encoding="utf-8") as handle:
        payload = json.load(handle)
    assert payload["lat"] == 48.1173 and payload["lon"] == 11.5167
    assert payload["alt"] == 545.4 and payload["fix"] == 3
    assert payload["sats"] == 8 and payload["hdop"] == 1.2 and payload["cn0"] == 39.5
    assert isinstance(payload["ts"], (int, float))


def test_gps_seam_write_survives_a_read_only_run(tmp_path, monkeypatch):
    # The seam write is best-effort: an unwritable path must not disturb the fix
    # (tagging and the UI keep working; the agent just sees no GPS).
    mod = _load_module("gps.py", "gps_seam_ro_under_test")
    blocker = tmp_path / "blocker"           # a regular file where a dir is needed
    blocker.write_text("x")
    monkeypatch.setattr(mod, "POSITION_FILE", str(blocker / "position.json"))
    plugin = _bare_gps(mod)
    plugin._set_mode(2)
    plugin._set_fix(40.0, -73.0)                # must not raise
    assert plugin.provide_position()["lat"] == 40.0


def _gps_reader_recorder(source, reachable, present):
    """A GPS instance wired so _read_once records which reader it would run,
    with gpsd reachability and serial presence stubbed. No thread is started."""
    mod = _load_module("gps.py", "gps_auto_under_test")
    plugin = mod.GPS()
    plugin.options = {"source": source}
    calls = []
    plugin._gpsd_reachable = lambda: reachable
    plugin._serial_present = lambda: present
    plugin._read_gpsd = lambda: calls.append("gpsd")
    plugin._read_serial = lambda: calls.append("serial")
    plugin._read_once(source)
    return calls


def test_gps_auto_prefers_gpsd_when_reachable():
    # gpsd up and a serial device present: gpsd wins.
    assert _gps_reader_recorder("auto", reachable=True, present=True) == ["gpsd"]


def test_gps_auto_falls_back_to_serial_when_gpsd_down():
    # No gpsd, but a receiver is plugged in: read it directly, no config needed.
    assert _gps_reader_recorder("auto", reachable=False, present=True) == ["serial"]


def test_gps_auto_idle_when_nothing_present():
    # Neither available: do nothing this pass (the loop retries after a wait).
    assert _gps_reader_recorder("auto", reachable=False, present=False) == []


def test_gps_explicit_source_pins_one_transport():
    # Explicit gpsd never touches serial even with a device present...
    assert _gps_reader_recorder("gpsd", reachable=True, present=True) == ["gpsd"]
    # ...and explicit serial never probes gpsd.
    assert _gps_reader_recorder("serial", reachable=True, present=True) == ["serial"]


def test_gps_serial_reader_recovers_from_a_stalled_link(monkeypatch):
    """The serial reader must not wedge forever when the USB link goes silent
    (the "no fix until I reboot outdoors" bug): a stalled read returns so _run
    can re-open, and the re-open then reads the fix that appears outdoors. A
    blocking readline() with no timeout never did, which is why a reboot was the
    only cure."""
    import threading

    mod = _load_module("gps.py", "gps_serial_recover_under_test")
    plugin = mod.GPS()
    plugin.options = {"source": "serial", "device": "/dev/ttyACM0"}
    plugin._fix, plugin._mode, plugin._sats = None, 0, None
    plugin._fix_lock = threading.Lock()
    plugin._stop = threading.Event()
    # Node resolution is exercised in its own test; here the device is present.
    plugin._resolve_serial_device = lambda: "/dev/ttyACM0"

    NOFIX = b"$GPGGA,,,,,0,00,,,,,,,,*00\r\n"
    FIX = (b"$GPGGA,123519,4807.038,N,01131.000,E,1,08,0.9,"
           b"545.4,M,46.9,M,,*47\r\n")
    # Two opens, keyed by the dummy fd each returns. The first streams a no-fix
    # line then stalls (select reports nothing ready); the second (the re-open)
    # delivers the fix then EOF.
    steps = {1: [("data", NOFIX), ("stall", b"")],
             2: [("data", FIX), ("eof", b"")]}
    cursor = {1: 0, 2: 0}
    opens = []

    class _FakePort:
        def __init__(self, fd):
            self._fd = fd

        def fileno(self):
            return self._fd

        def __enter__(self):
            return self

        def __exit__(self, *_a):
            return False

    import builtins
    _real_open = builtins.open

    def fake_open(device, *_a, **_k):
        # Only the serial node is faked; other opens (e.g. the latest-fix seam
        # the GGA fix now publishes) fall through to the real open.
        if device != "/dev/ttyACM0":
            return _real_open(device, *_a, **_k)
        opens.append(device)
        return _FakePort(len(opens))        # fd 1 on the first open, 2 on the next

    def fake_select(rlist, _w, _x, _timeout):
        fd = rlist[0]
        kind, _ = steps[fd][cursor[fd]]
        return ([], [], []) if kind == "stall" else ([fd], [], [])

    def fake_read(fd, _n):
        kind, data = steps[fd][cursor[fd]]
        cursor[fd] += 1
        return data                         # b"" signals EOF

    monkeypatch.setattr(mod, "open", fake_open, raising=False)
    monkeypatch.setattr(mod.select, "select", fake_select)
    monkeypatch.setattr(mod.os, "read", fake_read)

    # First pass: the no-fix line is read, then the stall returns the reader
    # (no hang, no fabricated fix).
    plugin._read_serial()
    assert opens == ["/dev/ttyACM0"]
    assert plugin.provide_position() is None

    # _run would loop and re-open; the second open delivers the fix.
    plugin._read_serial()
    assert opens == ["/dev/ttyACM0", "/dev/ttyACM0"]    # it re-opened the device
    assert plugin.provide_position() is not None
    assert plugin._mode >= 2

    # A set stop event is honoured promptly (the blocking read never let it be).
    plugin._stop.set()
    opens.clear()
    plugin._read_serial()
    assert opens == ["/dev/ttyACM0"]                    # opened, then exited at once


def test_gps_serial_redetects_a_moved_device_node(monkeypatch):
    """A receiver that re-enumerates to a different node (ttyACM0 -> ttyACM1)
    is found without a reboot, for explicit source=serial too: the configured
    node is preferred, else another node in the same family, else nothing (so
    an unrelated UART is never grabbed by mistake)."""
    mod = _load_module("gps.py", "gps_serial_redetect_under_test")
    plugin = mod.GPS()
    plugin.options = {"source": "serial", "device": "/dev/ttyACM0"}

    family = ["/dev/ttyACM0", "/dev/ttyACM1"]
    monkeypatch.setattr(mod.glob, "glob",
                        lambda pat: list(family) if "ttyACM" in pat else [])
    present = {"/dev/ttyACM1"}               # ttyACM0 gone; it came back as ACM1
    monkeypatch.setattr(mod.os.path, "exists", lambda p: p in present)

    # Configured node absent -> redetect the moved one in the same family.
    assert plugin._resolve_serial_device() == "/dev/ttyACM1"
    assert plugin._serial_present() is True

    # Configured node present -> it wins over the scanned alternatives.
    present.add("/dev/ttyACM0")
    assert plugin._resolve_serial_device() == "/dev/ttyACM0"

    # Nothing in the family -> None, not a stray node from another family.
    present.clear()
    monkeypatch.setattr(mod.glob, "glob", lambda _pat: [])
    assert plugin._resolve_serial_device() is None
    assert plugin._serial_present() is False


def test_wpa_sec_readout_counts_uploads_queue_and_cracked(tmp_path):
    plugin = _make_wpa_sec(tmp_path, online=False)
    assert "ui" in plugin.__capabilities__
    plugin._convert_to_cap = lambda lines: b"CAP"
    plugin._upload = lambda cap: True
    plugin._enqueue("WPA*02*aa*bb*cc*6464*0001*02")
    plugin._enqueue("WPA*01*dd*ee*ff*6565*0002*00")
    plugin._flush()                               # uploads both, clears the queue
    with plugin._lock:
        plugin._cracked = 4                       # as if a results fetch returned 4

    card = _card(plugin, "wpa-sec")
    assert card["uploaded"]["value"] == "2" and card["uploaded"]["kind"] == "number"
    assert card["queued"]["value"] == "0"
    assert card["cracked"]["value"] == "4"


def test_wigle_readout_counts_uploads_queue_and_networks(tmp_path, monkeypatch):
    import requests

    assert "ui" in _load_wigle().Wigle.__capabilities__
    fix = {"lat": 40.0, "lon": -73.0, "alt": None}
    plugin = _make_wigle(tmp_path, position=fix, online=False)   # gps mode

    # Two distinct networks observed with a fix: queued, not yet uploaded.
    plugin.on_handshake(types.SimpleNamespace(bssid="AA:BB:CC:DD:EE:01", essid="a"))
    plugin.on_handshake(types.SimpleNamespace(bssid="AA:BB:CC:DD:EE:02", essid="b"))

    card = _card(plugin, "wigle")
    assert card["queued"]["value"] == "2" and card["queued"]["kind"] == "number"
    assert card["networks"]["value"] == "2"
    assert card["uploaded"]["value"] == "0"
    assert card["mode"]["value"] == "gps" and card["mode"]["kind"] == "text"

    class _Resp(_Streamable):
        status_code = 200

        def json(self):
            return {"success": True}

    monkeypatch.setattr(requests, "post", lambda *a, **k: _Resp())
    plugin._upload()                              # accepted -> dequeued, counted

    card = _card(plugin, "wigle")
    assert card["uploaded"]["value"] == "2"
    assert card["queued"]["value"] == "0"
    assert card["networks"]["value"] == "2"       # dedup set kept after upload


def test_wigle_readout_mode_reflects_operating_state(tmp_path):
    # The Mode readout mirrors the documented load-time states.
    gps = _make_wigle(tmp_path, position={"lat": 1.0, "lon": 2.0, "alt": None},
                      online=False)
    assert _card(gps, "wigle")["mode"]["value"] == "gps"
    # gps + geolocate both on: real fixes upload, geolocate still fills in display
    # for fix-less observations, so the card names both.
    both = _make_wigle(tmp_path, position={"lat": 1.0, "lon": 2.0, "alt": None},
                       online=False, geolocate=True)
    assert _card(both, "wigle")["mode"]["value"] == "gps + geolocate"
    geo = _make_wigle(tmp_path, position=None, online=False, geolocate=True)
    assert _card(geo, "wigle")["mode"]["value"] == "geolocate"
    idle = _make_wigle(tmp_path, position=None, online=False, geolocate=False)
    assert _card(idle, "wigle")["mode"]["value"] == "idle"


def _make_memtemp(tmp_path, pct, rpm, monkeypatch):
    mod = _load_module("memtemp.py", "memtemp_under_test")
    monkeypatch.setattr(mod, "_fan_pwm_percent", lambda: pct)
    monkeypatch.setattr(mod, "_fan_rpm", lambda: rpm)
    plugin = mod.MemTemp()
    plugin.options = {}
    plugin.context = _ctx(tmp_path, ("ui",), name="memtemp")
    plugin.on_loaded()
    return plugin


def test_memtemp_shows_fan_when_a_pwm_fan_is_present(tmp_path, monkeypatch):
    plugin = _make_memtemp(tmp_path, pct=60, rpm=2400, monkeypatch=monkeypatch)
    card = _card(plugin, "memtemp")
    # mem/cpu/temp always present; fan appears only because one was detected.
    assert "mem" in card and "temp" in card
    assert card["fan"]["label"] == "Fan"
    assert card["fan"]["kind"] == "percent"        # percent leads so a bar draws
    assert card["fan"]["value"] == "60% · 2400 rpm"


def test_memtemp_fan_rpm_only_is_text(tmp_path, monkeypatch):
    plugin = _make_memtemp(tmp_path, pct=None, rpm=1800, monkeypatch=monkeypatch)
    card = _card(plugin, "memtemp")
    assert card["fan"]["kind"] == "text"           # no percent -> no bar
    assert card["fan"]["value"] == "1800 rpm"


def test_memtemp_hides_fan_when_none_present(tmp_path, monkeypatch):
    plugin = _make_memtemp(tmp_path, pct=None, rpm=None, monkeypatch=monkeypatch)
    card = _card(plugin, "memtemp")
    assert "mem" in card                           # the normal readout still renders
    assert "fan" not in card                       # but no empty fan element


# -- backup plugin -----------------------------------------------------------
def _make_backup(tmp_path, online=True, **opts):
    mod = _load_module("backup.py", "backup_under_test")
    plugin = mod.Backup()
    options = {"host": "h", "user": "u", "path": "/remote/dir", "port": 22,
               "tool": "rsync", "ssh_key": str(tmp_path / "id"),
               "interval_seconds": 3600, "timeout": 1800}
    options.update(opts)
    plugin.options = options
    plugin.context = PluginContext(
        name="backup", options=options,
        capture=CaptureInterface(str(tmp_path)),
        connectivity=types.SimpleNamespace(online=online),
        services=ServiceRegistry(),
        capabilities=("capture-read", "network", "ui"),
    )
    return plugin, mod


def test_backup_rsync_command_mirrors_store_over_ssh(tmp_path, monkeypatch):
    plugin, mod = _make_backup(tmp_path, port=2222)
    assert "ui" in plugin.__capabilities__ and "network" in plugin.__capabilities__
    captured = {}

    class _Proc:
        returncode = 0
        stdout = ("Number of regular files transferred: 2\n"
                  "Total transferred file size: 4,096 bytes\n")
        stderr = ""

    def fake_run(cmd, **kw):
        captured["cmd"] = cmd
        return _Proc()

    monkeypatch.setattr(mod.subprocess, "run", fake_run)
    ok, files, nbytes, detail = plugin._rsync(str(tmp_path), "/remote/dir",
                                              ["/.run/", "/.run"])
    assert ok and files == 2 and nbytes == 4096 and detail == ""
    cmd = captured["cmd"]
    assert cmd[0] == "rsync"
    assert "--partial" in cmd and "--mkpath" in cmd
    assert "/.run/" in cmd                       # transient raw-packet dir excluded
    transport = cmd[cmd.index("-e") + 1]
    assert "-p 2222" in transport and "BatchMode=yes" in transport
    assert str(tmp_path) + "/" in cmd            # source is the store, trailing slash
    assert cmd[-1] == "u@h:/remote/dir"          # dest is user@host:path


def test_backup_rsync_failure_reports_detail(tmp_path, monkeypatch):
    plugin, mod = _make_backup(tmp_path)

    class _Proc:
        returncode = 255
        stdout = ""
        stderr = "ssh: connect to host h port 22: No route to host\nrsync error: ...\n"

    monkeypatch.setattr(mod.time, "sleep", lambda *_a: None)   # don't wait out retries
    monkeypatch.setattr(mod.subprocess, "run", lambda cmd, **kw: _Proc())
    ok, files, nbytes, detail = plugin._rsync(str(tmp_path), "/remote/dir")
    assert not ok and files == 0 and "rsync error" in detail


def test_backup_rsync_failure_surfaces_the_specific_cause(tmp_path, monkeypatch):
    # rsync prints the real cause (permissions, path, disk) before the generic
    # "code 11" summary; the card must show the cause, not the summary.
    plugin, mod = _make_backup(tmp_path)

    class _Proc:
        returncode = 11
        stdout = ""
        stderr = ('rsync: [receiver] mkdir "/backups/pwn" failed: '
                  'Permission denied (13)\n'
                  'rsync error: error in file IO (code 11) at '
                  'main.c(787) [Receiver=3.2.3]\n')

    monkeypatch.setattr(mod.time, "sleep", lambda *_a: None)   # don't wait out retries
    monkeypatch.setattr(mod.subprocess, "run", lambda cmd, **kw: _Proc())
    ok, _files, _nbytes, detail = plugin._rsync(str(tmp_path), "/remote/dir")
    assert not ok
    assert "Permission denied" in detail        # the actionable cause
    assert "code 11" not in detail               # not the generic summary line


def test_backup_rsync_retries_a_dropped_transfer(tmp_path, monkeypatch):
    # A flaky tether drops a transfer mid-stream; --partial resumes, so _rsync
    # retries before reporting failure.
    plugin, mod = _make_backup(tmp_path)
    monkeypatch.setattr(mod.time, "sleep", lambda *_a: None)
    calls = {"n": 0}

    def flaky_then_ok(cmd, **_kw):
        calls["n"] += 1
        if calls["n"] < 2:
            return types.SimpleNamespace(
                returncode=12, stdout="",
                stderr="rsync: connection unexpectedly closed (0 bytes) [sender]")
        return types.SimpleNamespace(
            returncode=0, stderr="",
            stdout=("Number of regular files transferred: 3\n"
                    "Total transferred file size: 2,560 bytes\n"))
    monkeypatch.setattr(mod.subprocess, "run", flaky_then_ok)
    ok, files, nbytes, detail = plugin._rsync(str(tmp_path), "/remote/dir")
    assert ok and files == 3 and nbytes == 2560 and detail == ""
    assert calls["n"] == 2                       # retried once, then succeeded


def test_backup_scp_fallback_copies_store(tmp_path, monkeypatch):
    (tmp_path / "a.hc22000").write_text("WPA*02*x\n")
    (tmp_path / ".run").mkdir()                  # must be skipped
    (tmp_path / ".run" / "raw.pcapng").write_text("junk")
    plugin, mod = _make_backup(tmp_path, tool="scp")
    calls = []

    class _Proc:
        returncode = 0
        stdout = ""
        stderr = ""

    def fake_run(cmd, **kw):
        calls.append(cmd)
        return _Proc()

    monkeypatch.setattr(mod.shutil, "which", lambda name: "/usr/bin/" + name)
    monkeypatch.setattr(mod.subprocess, "run", fake_run)
    ok, files, nbytes, detail = plugin._transfer(str(tmp_path))
    assert ok and detail == ""
    scp = [c for c in calls if c and c[0] == "scp"][0]
    assert any(str(tmp_path / "a.hc22000") == arg for arg in scp)
    assert not any(".run" in arg for arg in scp)   # transient dir not pushed
    assert scp[-1] == "u@h:/remote/dir"
    assert "-p" in scp   # preserve on-device mtimes/modes (parity with rsync -a)


def test_backup_mirrors_the_agent_dir_to_a_subpath(tmp_path, monkeypatch):
    # With agent_dir set and present, the transfer also mirrors it to
    # <remote>/agent/, excluding the ephemeral status/override files, so a
    # trained model + episode logs survive a reflash.
    store = tmp_path / "store"
    store.mkdir()
    agent = tmp_path / "agent"
    (agent / "episodes").mkdir(parents=True)
    (agent / "model.zip").write_text("weights")
    (agent / "status.json").write_text("{}")          # must be excluded
    plugin, mod = _make_backup(store, agent_dir=str(agent))
    runs = []

    class _Proc:
        returncode = 0
        stdout = ("Number of regular files transferred: 1\n"
                  "Total transferred file size: 7 bytes\n")
        stderr = ""

    monkeypatch.setattr(mod.shutil, "which", lambda name: "/usr/bin/" + name)
    monkeypatch.setattr(mod.subprocess, "run",
                        lambda cmd, **kw: (runs.append(cmd), _Proc())[1])
    ok, files, _nbytes, detail = plugin._transfer(str(store))
    assert ok and detail == ""
    rsyncs = [c for c in runs if c and c[0] == "rsync"]
    assert len(rsyncs) == 2                               # store + agent
    store_cmd, agent_cmd = rsyncs
    assert store_cmd[-1] == "u@h:/remote/dir"
    assert agent_cmd[-1] == "u@h:/remote/dir/agent"
    assert str(agent) + "/" == agent_cmd[-2]
    assert "/status.json" in agent_cmd                   # ephemeral file excluded
    assert "/scan-override.json" in agent_cmd


def test_backup_without_an_agent_dir_mirrors_only_the_store(tmp_path, monkeypatch):
    store = tmp_path / "store"
    store.mkdir()
    # agent_dir points nowhere -> only the store source is mirrored.
    plugin, mod = _make_backup(store, agent_dir=str(tmp_path / "missing"))

    class _Proc:
        returncode = 0
        stdout = "Number of regular files transferred: 0\n"
        stderr = ""

    runs = []
    monkeypatch.setattr(mod.shutil, "which", lambda name: "/usr/bin/" + name)
    monkeypatch.setattr(mod.subprocess, "run",
                        lambda cmd, **kw: (runs.append(cmd), _Proc())[1])
    ok, _files, _nbytes, _detail = plugin._transfer(str(store))
    assert ok
    assert len([c for c in runs if c and c[0] == "rsync"]) == 1


def test_backup_readout_shows_target_and_last_run(tmp_path):
    plugin, _ = _make_backup(tmp_path, online=False)
    plugin.on_loaded()                 # worker no-ops: offline
    try:
        card = _card(plugin, "backup")
        assert card["last"]["value"] == "never"
        assert card["status"]["value"] in ("idle", "running")
        assert card["files"]["kind"] == "number"
        assert card["target"]["value"] == "u@h:/remote/dir"
        with plugin._lock:
            plugin._last_ok = time.time()
            plugin._last_status = "ok"
            plugin._last_files = 5
            plugin._last_bytes = 2048
        card = _card(plugin, "backup")
        assert card["status"]["value"] == "ok"
        assert card["files"]["value"] == "5"
        assert card["size"]["value"] == "2.0 KB"
        assert card["last"]["value"] == "just now"
    finally:
        plugin.on_unload()


def test_backup_card_shows_restore_progress(tmp_path):
    # The CLI `backup restore` - which the web Restore button runs in the
    # background - writes a progress marker in the store's transient .run/ dir;
    # the card surfaces it as idle -> running -> ok / error so the dashboard
    # shows a restore's progress.
    plugin, _ = _make_backup(tmp_path, online=False)
    plugin.on_loaded()
    try:
        run_dir = tmp_path / ".run"
        run_dir.mkdir(exist_ok=True)
        marker = run_dir / "restore-state.json"

        # No marker yet: nothing has been restored on this image.
        assert _card(plugin, "backup")["restore"]["value"] == "idle"

        marker.write_text(json.dumps({"status": "running", "started": time.time()}))
        assert _card(plugin, "backup")["restore"]["value"] == "running"

        # Back-compat: an ok marker without the counts (older CLI, or a marker
        # written before the sums were recorded) still shows the plain time.
        marker.write_text(json.dumps({"status": "ok", "finished": time.time()}))
        assert _card(plugin, "backup")["restore"]["value"] == "ok (just now)"

        # A successful restore that recorded how much it pulled shows the file
        # count and size alongside the time, in the same single card row.
        marker.write_text(json.dumps(
            {"status": "ok", "finished": time.time(), "files": 3, "bytes": 2560}))
        assert _card(plugin, "backup")["restore"]["value"] == \
            "ok (just now, 3 files, 2.5 KB)"

        # One file is singular.
        marker.write_text(json.dumps(
            {"status": "ok", "finished": time.time(), "files": 1, "bytes": 1024}))
        assert _card(plugin, "backup")["restore"]["value"] == \
            "ok (just now, 1 file, 1.0 KB)"

        marker.write_text(json.dumps(
            {"status": "error", "finished": time.time(),
             "detail": "capture restore failed"}))
        assert _card(plugin, "backup")["restore"]["value"].startswith(
            "error: capture restore failed")
    finally:
        plugin.on_unload()


def test_backup_last_run_time_only_advances_when_data_moves(tmp_path, monkeypatch):
    # A flaky link re-triggers backups; a successful rsync that copied nothing
    # (0 files transferred) must NOT reset "last backup" to "just now".
    plugin, _ = _make_backup(tmp_path, online=False)
    plugin.on_loaded()                          # worker starts but no-ops (offline)
    try:
        plugin._stop.set()
        plugin._wake.set()                      # retire the worker thread
        plugin._worker.join(timeout=2)
        (tmp_path / "id").write_text("key")     # as if `backup setup` installed the key
        plugin.context.connectivity.online = True
        results = iter([(True, 3, 4096, ""), (True, 0, 0, "")])
        monkeypatch.setattr(plugin, "_transfer", lambda store: next(results))

        plugin._maybe_backup(force=True)        # a real transfer: 3 files moved
        first_ok = plugin._last_ok
        assert first_ok and plugin._last_status == "ok" and plugin._last_files == 3

        plugin._last_run = 0.0                  # clear the monotonic throttle
        plugin._maybe_backup(force=True)        # a no-op sync: nothing changed
        assert plugin._last_ok == first_ok       # "last backup" stayed put
        assert plugin._last_status == "ok (no changes)"
        assert plugin._last_files == 0           # the card reports the real (empty) run
    finally:
        plugin.on_unload()


def test_backup_setup_hint_is_on_config_page_not_the_card(tmp_path):
    # Backup is optional, so the "set a target" / "run backup setup" hints live
    # in the plugin's health note (shown on the /plugins config page), NOT on the
    # main dashboard card (which stays a plain run state).
    plugin, _ = _make_backup(tmp_path, online=False, host="", user="", path="")
    plugin.on_loaded()
    try:
        assert "not configured" in plugin.context.health_note    # on /plugins
        assert plugin._target() == ""
        assert _card(plugin, "backup")["status"]["value"] == "idle"   # not on the card
    finally:
        plugin.on_unload()
    plugin, _ = _make_backup(tmp_path, online=False)   # configured, no key yet
    plugin.on_loaded()
    try:
        assert not plugin._key_ready()
        assert "backup setup" in plugin.context.health_note      # on /plugins
        assert _card(plugin, "backup")["status"]["value"] == "idle"   # not on the card
    finally:
        plugin.on_unload()


def test_backup_health_note_clears_once_ready(tmp_path):
    # on_epoch recomputes the config-page hint, so it clears after setup without
    # a daemon restart.
    plugin, _ = _make_backup(tmp_path, online=False)
    plugin.on_loaded()
    try:
        assert plugin.context.health_note                 # "needs key setup" at first
        (tmp_path / "id").write_text("key")               # as if backup setup ran
        plugin.on_epoch(1, {})
        assert plugin.context.health_note == ""
    finally:
        plugin.on_unload()


# -- ups-ws plugin (Waveshare UPS HAT battery) -------------------------------
# The battery logic is the ups library and the chip the ina219 library, both
# tested in test_ups_lib.py; these test the plugin's wiring.
class _FakeBus:
    """A stand-in smbus bus backed by a register dict, for exercising the INA219
    reads without hardware. ``roundtrip=False`` makes the calibration register
    fail its write/read check (an impostor device)."""

    def __init__(self, regs=None, roundtrip=True):
        self.regs = dict(regs or {})
        self.roundtrip = roundtrip
        self.closed = False

    def read_i2c_block_data(self, addr, reg, count):
        value = self.regs.get(reg, 0) & 0xFFFF
        return [(value >> 8) & 0xFF, value & 0xFF]

    def write_i2c_block_data(self, addr, reg, data):
        value = ((data[0] << 8) | data[1]) & 0xFFFF
        if reg == 0x05 and not self.roundtrip:
            value ^= 0xFFFF            # corrupt so the round-trip check fails
        self.regs[reg] = value

    def close(self):
        self.closed = True


_REAL_OPEN_CHIP = _load_module("ups-ws.py", "ups_ws_for_open_chip").ina219.open_chip


def _make_ups(tmp_path, applied=None, **opts):
    mod = _load_module("ups-ws.py", "ups_ws_under_test")
    plugin = mod.UpsWs()
    options = {"i2c_bus": 1, "address": 0x42, "shutdown_percent": 5,
               "poll_seconds": 20}
    options.update(opts)
    plugin.options = options
    plugin.context = _ctx(tmp_path, ("ui",), options=options, name="ups-ws")
    if applied is not None:
        plugin.context._apply = lambda change: applied.append(change) or {"ok": True}
    plugin._setup()                    # init state without starting the poll thread
    return plugin, mod


def _fake_smbus(regs=None, roundtrip=True, fail=None):
    """A stand-in ``smbus`` module whose SMBus() hands out a _FakeBus (and
    records it), or raises OSError(``fail``)."""
    buses = []

    def SMBus(number):
        if fail:
            raise OSError(fail)
        bus = _FakeBus(regs=regs, roundtrip=roundtrip)
        buses.append(bus)
        return bus

    return types.SimpleNamespace(SMBus=SMBus), buses


def _probe_with(monkeypatch, plugin, mod, **fake):
    """Probe through the real ina219.open_chip over a fake smbus module. The
    library module is shared, so use the function as first loaded, not one an
    earlier patch left in its place."""
    smbus_mod, buses = _fake_smbus(**fake)
    real = _REAL_OPEN_CHIP
    monkeypatch.setattr(mod.ina219, "open_chip",
                        lambda bus, address, cal: real(bus, address, cal, smbus_module=smbus_mod))
    return plugin.monitor.ensure_device(), buses


def test_ups_ws_declares_only_ui_and_provides_battery(tmp_path):
    plugin, mod = _make_ups(tmp_path)
    assert plugin.__capabilities__ == ("ui",)      # local read, no ack needed
    assert plugin.__provides__ == ("battery",)
    assert mod.PACK.cells == 2 and mod.CALIBRATION is mod.ina219.CAL_16V_5A
    assert mod.NAME == "ups-ws"


def test_ups_ws_finds_the_hat_by_a_register_roundtrip(tmp_path, monkeypatch):
    plugin, mod = _make_ups(tmp_path)
    # ~7.0V: raw bus-voltage register = (7.0/0.004) << 3.
    ok, buses = _probe_with(monkeypatch, plugin, mod, regs={0x02: (1750 << 3)})
    assert ok is True and plugin.context.health_note == ""
    other, _ = _make_ups(tmp_path)
    ok, buses = _probe_with(monkeypatch, other, mod, roundtrip=False)
    assert ok is False and buses[0].closed       # an impostor: closed again
    assert "not detected on I2C bus 1 at 0x42" in other.context.health_note


def test_ups_ws_card_absent_without_hardware(tmp_path, monkeypatch):
    # On the build/test host there is no smbus, so the HAT can't be confirmed:
    # the card reads "no UPS HAT" and the /plugins health note explains it.
    plugin, mod = _make_ups(tmp_path)
    monkeypatch.setattr(mod.ina219, "open_chip", lambda bus, address, cal: None)
    assert plugin.monitor.ensure_device() is False
    card = _card(plugin, "ups-ws")
    assert card["percent"]["value"] == "-"
    assert "UPS HAT" in card["status"]["value"]
    assert "smbus" in plugin.context.health_note   # the /plugins page says why
    bad, _ = _make_ups(tmp_path)
    ok, _ = _probe_with(monkeypatch, bad, mod, fail="no such bus")
    assert ok is False and "cannot open I2C bus 1" in bad.context.health_note


def test_ups_ws_card_shows_battery_readout(tmp_path, monkeypatch):
    plugin, mod = _make_ups(tmp_path)
    # ~7.0V, discharging ~ -200mA, ~1.4W.
    regs = {0x02: (1750 << 3), 0x04: (65536 - 1312), 0x03: 459}
    assert _probe_with(monkeypatch, plugin, mod, regs=regs)[0]
    reading = plugin.monitor.read_battery()
    assert reading["status"] == "discharging"
    assert round(reading["percent"]) == 42
    plugin.monitor.evaluate(reading)
    assert plugin.provide_battery() == reading
    card = _card(plugin, "ups-ws")
    assert card["percent"]["kind"] == "gauge"       # draws a gauge bar
    assert card["percent"]["value"] == "42%"
    assert card["status"]["value"] == "Discharging"
    assert card["voltage"]["value"] == "7.00 V"
    assert card["current"]["value"].endswith("mA") and card["current"]["value"].startswith("-")
    assert card["shutdown"]["value"] == "5%" and card["percent"]["mark"] == 5


def test_ups_ws_auto_shutdown_goes_through_the_plugins_context(tmp_path):
    applied = []
    plugin, _ = _make_ups(tmp_path, applied=applied, shutdown_percent=5)
    low = {"percent": 4.0, "status": "discharging", "voltage": 6.1,
           "current_ma": -250.0, "power_w": 1.5}
    for _ in range(3):
        plugin.monitor.evaluate(low)
    assert applied == [{"target": "power", "action": "shutdown"}]
    assert "Shutting down" in _card(plugin, "ups-ws")["status"]["value"]
    src = open(os.path.join(PLUGIN_DIR, "ups-ws.py"), encoding="utf-8").read()
    assert "kestrel_pi import web" not in src and "from kestrel_pi.web" not in src


def test_ups_ws_uses_the_shared_libraries_and_keeps_no_copy():
    src = open(os.path.join(PLUGIN_DIR, "ups-ws.py"), encoding="utf-8").read()
    assert 'load_library(__file__, "ups", api=0)' in src
    assert 'load_library(__file__, "ina219", api=1)' in src
    for name in ("class _INA219", "def _evaluate", "def _estimate_eta", "_SHUTDOWN_DEBOUNCE"):
        assert name not in src, name
    assert "UPS HAT (B)/(C)" not in src        # the (C) is a different board


# -- backup: the dashboard card adopts a manual (CLI) backup -----------------
def _write_backup_state(tmp_path, **fields):
    with open(os.path.join(str(tmp_path), ".backup-state.json"), "w",
              encoding="utf-8") as handle:
        json.dump(fields, handle)


def test_backup_card_adopts_a_newer_external_backup(tmp_path):
    # The CLI `backup now` writes the shared state file; on_epoch adopts it so
    # the dashboard card reflects a manual backup without a daemon restart.
    plugin, _ = _make_backup(tmp_path, online=False)
    plugin.on_loaded()                           # worker no-ops: offline
    try:
        assert plugin._last_ok is None
        _write_backup_state(tmp_path, last_ok=5000.0, last_status="ok",
                            last_files=7, last_bytes=512000)
        plugin.on_epoch(1, {})
        assert plugin._last_ok == 5000.0
        assert plugin._last_status == "ok"
        assert plugin._last_files == 7 and plugin._last_bytes == 512000
    finally:
        plugin.on_unload()


def test_backup_card_ignores_an_older_external_state(tmp_path):
    # A stale file must never roll the card's time backwards over the plugin's
    # own more recent run.
    plugin, _ = _make_backup(tmp_path, online=False)
    plugin.on_loaded()
    try:
        with plugin._lock:
            plugin._last_ok = 10000.0
            plugin._last_status = "ok"
        _write_backup_state(tmp_path, last_ok=5000.0,
                            last_status="ok (no changes)",
                            last_files=0, last_bytes=0)
        plugin.on_epoch(1, {})
        assert plugin._last_ok == 10000.0        # unchanged
        assert plugin._last_status == "ok"
    finally:
        plugin.on_unload()


def test_backup_card_survives_a_missing_or_junk_state_file(tmp_path):
    plugin, _ = _make_backup(tmp_path, online=False)
    plugin.on_loaded()
    try:
        plugin.on_epoch(1, {})                   # no state file yet
        assert plugin._last_ok is None
        with open(os.path.join(str(tmp_path), ".backup-state.json"), "w",
                  encoding="utf-8") as handle:
            handle.write("{not json")
        plugin.on_epoch(2, {})                   # junk file
        assert plugin._last_ok is None
    finally:
        plugin.on_unload()


# -- remote-train plugin -----------------------------------------------------
def _make_remote_train(tmp_path, online=True, **opts):
    """A RemoteTrain instance wired to a tmp agent dir, WITHOUT starting its
    worker thread (so tests drive its methods directly, no real SSH)."""
    mod = _load_module("remote-train.py", "remote_train_under_test")
    agent_dir = tmp_path / "agent"
    (agent_dir / "episodes").mkdir(parents=True, exist_ok=True)
    key = tmp_path / "id"
    key.write_text("KEY")
    options = {"train_host": "desk", "train_user": "me", "train_path": "/w",
               "train_port": 22, "ssh_key": str(key), "algorithms": "DQN",
               "remote_train_min_new_episodes": 5, "stale_lock_hours": 6,
               "timesteps": 123456, "interval_seconds": 3600, "timeout": 1800,
               "agent_dir": str(agent_dir)}
    options.update({k: v for k, v in opts.items() if not k.startswith("_")})
    plugin = mod.RemoteTrain()
    plugin.options = options
    plugin.context = PluginContext(
        name="remote-train", options=options,
        capture=CaptureInterface(str(tmp_path)),
        connectivity=types.SimpleNamespace(online=online),
        services=ServiceRegistry(),
        capabilities=("capture-read", "network", "ui"))
    plugin.log = plugin.context.logger
    plugin._init_state()
    # The agent runs DQN (read live from config.toml for the algorithm order).
    cfg = tmp_path / "config.toml"
    cfg.write_text('[agent]\nalgorithm = "%s"\n' % opts.pop("_agent_algorithm", "DQN"))
    plugin._config_path = str(cfg)
    return plugin, mod, str(agent_dir)


def _write_episodes(agent_dir, timestamps):
    import json as _json
    path = os.path.join(agent_dir, "episodes", "001.jsonl")
    with open(path, "w", encoding="utf-8") as handle:
        for ts in timestamps:
            handle.write(_json.dumps({"policy": "learned", "action": 0,
                                      "reward": 0.0, "ts": ts}) + "\n")


def test_remote_train_algorithms_filters_and_dedups(tmp_path):
    # The normal case: a comma-separated string (settable via --set / the web).
    plugin, _mod, _ad = _make_remote_train(
        tmp_path, algorithms="DQN, bogus, DQN, PPO")
    assert plugin._algorithms() == ["DQN", "PPO"]
    # A list value is handled defensively (the config validator enforces str).
    plugin, _mod, _ad = _make_remote_train(
        tmp_path, algorithms=["DQN", "bogus", "DQN", "PPO"])
    assert plugin._algorithms() == ["DQN", "PPO"]
    # Empty still trains the algorithm the agent runs.
    plugin, _mod, _ad = _make_remote_train(tmp_path, algorithms="")
    assert plugin._algorithms() == ["DQN"]


def test_remote_train_trains_the_agents_algorithm_first(tmp_path):
    # The algorithm the agent runs comes first, and is added when the option
    # leaves it out, so the host always trains the model the device can use.
    plugin, _mod, _ad = _make_remote_train(tmp_path, algorithms="DQN, QR-DQN",
                                           _agent_algorithm="PPO")
    assert plugin._algorithms() == ["PPO", "DQN", "QR-DQN"]
    plugin, _mod, _ad = _make_remote_train(tmp_path, algorithms="DQN, PPO",
                                           _agent_algorithm="PPO")
    assert plugin._algorithms() == ["PPO", "DQN"]


def test_remote_train_algorithms_default_is_all_four(tmp_path):
    # The shipped default trains every algorithm, so enabling the plugin with no
    # explicit selection keeps all four models current. Every name in the default
    # must be a real algorithm (survive the valid_algorithm filter).
    _plugin, mod, _ad = _make_remote_train(tmp_path)
    default = mod.RemoteTrain.__options__["algorithms"]["default"]
    assert default == "RecurrentPPO,PPO,DQN,QR-DQN"
    plugin, _mod, _ad2 = _make_remote_train(tmp_path, algorithms=default,
                                            _agent_algorithm="RecurrentPPO")
    assert plugin._algorithms() == ["RecurrentPPO", "PPO", "DQN", "QR-DQN"]


def test_remote_train_new_episodes_counts_since_the_hosts_last_model(tmp_path):
    # The trigger counts rows since the host's last model, not the device's own:
    # on-device training no longer resets the remote's clock.
    from kestrel_pi.agent import policies
    plugin, _mod, agent_dir = _make_remote_train(tmp_path)
    _write_episodes(agent_dir, [50, 100, 150, 200])
    assert plugin._new_episodes("DQN") == 4           # no remote model yet: all
    policies.write_meta(agent_dir, "DQN",              # the device's own model
                        watermark={"latest_ts": 175.0, "episodes": 3})
    plugin._record_host_model("dqn", {"algorithm": "DQN",
                                      "data_watermark": {"latest_ts": 100.0}})
    assert plugin._new_episodes("DQN") == 2


def test_remote_train_should_stage_rules(tmp_path):
    import json as _json
    from kestrel_pi.agent import EVALUATED_NAME, REMOTE_STAGING_DIR, metrics, spec
    plugin, _mod, agent_dir = _make_remote_train(tmp_path)
    older = {"algorithm": "DQN", "spec_version": spec.SPEC_VERSION,
             "data_watermark": {"latest_ts": 90.0, "episodes": 60}}
    mism = dict(older, spec_version=spec.SPEC_VERSION + 1)
    assert plugin._should_stage(older) is True               # data age doesn't matter
    assert plugin._should_stage(mism) is False               # spec mismatch
    # A model the agent already judged isn't staged again.
    judged = os.path.join(agent_dir, REMOTE_STAGING_DIR, EVALUATED_NAME)
    os.makedirs(os.path.dirname(judged), exist_ok=True)
    with open(judged, "w", encoding="utf-8") as handle:
        _json.dump([metrics.model_fingerprint(older)], handle)
    assert plugin._should_stage(older) is False


def _fake_host(meta, model=b"REMOTE-MODEL", pulls=None):
    import json as _json

    def fake_down(remote_path, local_path):
        if pulls is not None:
            pulls.append(os.path.basename(remote_path))
        if os.path.basename(local_path).endswith(".meta.json"):
            with open(local_path, "w", encoding="utf-8") as handle:
                _json.dump(meta, handle)
        else:
            with open(local_path, "wb") as handle:
                handle.write(model)
        return (True, "")
    return fake_down


def test_remote_train_pull_and_stage_leaves_a_model_for_the_agent(tmp_path):
    import json as _json
    from kestrel_pi.agent import (HOST_MODELS_NAME, REMOTE_STAGING_DIR,
                                     model_meta_name_for, model_name_for, policies,
                                     spec)
    plugin, _mod, agent_dir = _make_remote_train(tmp_path)
    # The device's own model has NEWER data: the remote is still staged now.
    policies.write_meta(agent_dir, "DQN",
                        watermark={"latest_ts": 500.0, "episodes": 80})
    meta = {"algorithm": "DQN", "spec_version": spec.SPEC_VERSION,
            "data_watermark": {"latest_ts": 300.0, "episodes": 99},
            "train_seconds": 405.0}
    plugin._rsync_down = _fake_host(meta)
    assert plugin._pull_and_stage("DQN", "dqn") is True
    # The model + sidecar land in the staging dir, NOT the active slot - the
    # agent's contest decides.
    staging = os.path.join(agent_dir, REMOTE_STAGING_DIR, "dqn")
    with open(os.path.join(staging, model_name_for("DQN")), "rb") as handle:
        assert handle.read() == b"REMOTE-MODEL"
    assert not os.path.exists(os.path.join(agent_dir, model_name_for("DQN")))
    assert sorted(n for n in os.listdir(staging) if n.startswith(".")) == []
    # The plugin does NOT restamp source; the agent does that on promotion.
    with open(os.path.join(staging, model_meta_name_for("DQN"))) as handle:
        assert "source" not in _json.load(handle)
    # The host's training time is captured for the card's "Last model pulled",
    # and the host's model is recorded for the launch trigger.
    assert plugin._last_pull_secs == 405.0
    with open(os.path.join(agent_dir, REMOTE_STAGING_DIR, HOST_MODELS_NAME)) as handle:
        assert _json.load(handle)["dqn"]["latest_ts"] == 300.0


def test_remote_train_does_not_pull_an_unchanged_or_judged_model_again(tmp_path):
    import json as _json
    from kestrel_pi.agent import (EVALUATED_NAME, REMOTE_STAGING_DIR, metrics,
                                     model_name_for, spec)
    plugin, _mod, agent_dir = _make_remote_train(tmp_path)
    meta = {"algorithm": "DQN", "spec_version": spec.SPEC_VERSION,
            "data_watermark": {"latest_ts": 300.0, "episodes": 99},
            "train_seconds": 405.0}
    pulls = []
    plugin._rsync_down = _fake_host(meta, pulls=pulls)
    assert plugin._pull_and_stage("DQN", "dqn") is True
    staged = os.path.join(agent_dir, REMOTE_STAGING_DIR, "dqn", model_name_for("DQN"))
    mtime = os.stat(staged).st_mtime_ns
    # Already staged: only the sidecar is fetched, and the model is untouched.
    pulls.clear()
    assert plugin._pull_and_stage("DQN", "dqn") is False
    assert pulls == ["model-dqn.meta.json"]
    assert os.stat(staged).st_mtime_ns == mtime
    # Judged by the agent (and cleared): not staged again.
    for name in os.listdir(os.path.dirname(staged)):
        os.remove(os.path.join(os.path.dirname(staged), name))
    with open(os.path.join(agent_dir, REMOTE_STAGING_DIR, EVALUATED_NAME), "w") as fh:
        _json.dump([metrics.model_fingerprint(meta)], fh)
    pulls.clear()
    assert plugin._pull_and_stage("DQN", "dqn") is False
    assert pulls == ["model-dqn.meta.json"] and not os.path.exists(staged)


def test_remote_train_reconcile_launches_only_with_enough_new_data(tmp_path):
    plugin, _mod, _ad = _make_remote_train(tmp_path,
                                           remote_train_min_new_episodes=5)
    plugin._stack_ready = lambda: True
    plugin._pull_and_stage = lambda algo, slug: False
    plugin._algo_running = lambda slug: False
    launched = []
    plugin._launch_training = lambda algo, slug: (launched.append(algo) or True)

    plugin._new_episodes = lambda algo: 4      # below threshold
    plugin._reconcile(["DQN"])
    assert launched == []

    plugin._new_episodes = lambda algo: 9      # at/over threshold
    plugin._reconcile(["DQN"])
    assert launched == ["DQN"]


def test_remote_train_manual_request_forces_a_run_below_the_threshold(tmp_path):
    # The Train button / `remote-train now` sets _train_now; a manual run must
    # launch even with fewer than remote_train_min_new_episodes new rows, as
    # long as there is some episode data - otherwise the button looked dead.
    plugin, _mod, agent_dir = _make_remote_train(
        tmp_path, remote_train_min_new_episodes=5)
    plugin._stack_ready = lambda: True
    plugin._pull_and_promote = lambda algo, slug: False
    plugin._algo_running = lambda slug: False
    plugin._new_episodes = lambda algo: 1          # well below the threshold
    launched = []
    plugin._launch_training = lambda algo, slug: (launched.append(algo) or True)
    _write_episodes(agent_dir, [10, 20, 30])       # there is data to train on

    plugin._train_now.set()
    plugin._reconcile(["DQN"])
    assert launched == ["DQN"]                      # forced past the threshold
    assert not plugin._train_now.is_set()           # the flag is consumed
    assert "training" in plugin._status


def test_remote_train_manual_request_with_no_data_reports_instead_of_launching(
        tmp_path):
    # A manual run with no recorded episodes can't train on anything; it must
    # say so on the card rather than launch an empty run or sit on "idle".
    plugin, _mod, _ad = _make_remote_train(tmp_path)
    plugin._stack_ready = lambda: True
    plugin._pull_and_promote = lambda algo, slug: False
    plugin._algo_running = lambda slug: False
    plugin._new_episodes = lambda algo: 0
    launched = []
    plugin._launch_training = lambda algo, slug: (launched.append(algo) or True)

    plugin._train_now.set()
    plugin._reconcile(["DQN"])                      # no episodes written
    assert launched == []
    assert not plugin._train_now.is_set()
    assert "nothing to train on" in plugin._status


def test_remote_train_manual_request_while_offline_reports_the_reason(tmp_path):
    # A manual Train while the device is offline can't reach the host; the card
    # must say so (and consume the request) instead of silently doing nothing.
    plugin, _mod, _ad = _make_remote_train(tmp_path, online=False)
    plugin._train_now.set()
    plugin._maybe_reconcile()
    assert not plugin._train_now.is_set()
    assert "offline" in plugin._status


def test_remote_train_reconcile_does_not_double_launch_a_running_algo(tmp_path):
    plugin, _mod, _ad = _make_remote_train(tmp_path)
    plugin._stack_ready = lambda: True
    plugin._pull_and_stage = lambda algo, slug: False
    plugin._algo_running = lambda slug: True        # already running
    plugin._new_episodes = lambda algo: 10_000
    launched = []
    plugin._launch_training = lambda algo, slug: (launched.append(algo) or True)
    plugin._reconcile(["DQN"])
    assert launched == []


def test_remote_train_reconcile_prepares_the_host_when_stack_missing(tmp_path):
    plugin, _mod, _ad = _make_remote_train(tmp_path)
    plugin._stack_ready = lambda: False
    plugin._stack_error = lambda: ""          # no prior failure recorded
    prepared = []
    plugin._ensure_stack = lambda: prepared.append(True)
    plugin._reconcile(["DQN"])
    assert prepared == [True]
    assert "installing" in plugin._status


def test_remote_train_reconcile_surfaces_a_host_prep_failure(tmp_path):
    # When the host prep recorded a failure (.stack-error), the plugin flags it
    # as a clear error on the card and the /plugins note instead of a silent
    # "not prepared yet", and still re-attempts the prep.
    plugin, _mod, _ad = _make_remote_train(tmp_path)
    plugin._stack_ready = lambda: False
    plugin._stack_error = lambda: "could not create a Python venv on the host"
    prepared = []
    plugin._ensure_stack = lambda: prepared.append(True)
    plugin._reconcile(["DQN"])
    assert prepared == [True]                 # still retries
    assert "host prep failed" in plugin._status
    assert "could not create a Python venv" in plugin._status
    assert "could not create a Python venv" in plugin.context.health_note


def _fake_ssh(monkeypatch, mod, returncode=None, raises=None):
    """Make the plugin's ssh calls end with ``returncode`` (or raise)."""
    def run(cmd, **_kw):
        if raises is not None:
            raise raises
        return subprocess.CompletedProcess(cmd, returncode, "", "")
    monkeypatch.setattr(mod.subprocess, "run", run)


def test_remote_train_ssh_failure_marks_the_host_unreachable(tmp_path, monkeypatch):
    # ssh exits 255 when it can't connect or log in.
    plugin, mod, _ad = _make_remote_train(tmp_path)
    _fake_ssh(monkeypatch, mod, returncode=255)
    assert plugin._remote_rc("true")[0] == 255
    assert plugin._host_state == "unreachable"
    assert plugin._last_contact is None


def test_remote_train_a_failing_command_still_counts_as_contact(tmp_path, monkeypatch):
    # Any other status is the remote command's own: the host was reached.
    plugin, mod, _ad = _make_remote_train(tmp_path)
    _fake_ssh(monkeypatch, mod, returncode=1)
    assert plugin._remote("false") == (False, "")
    assert plugin._host_state == "reachable"
    assert plugin._last_contact is not None


def test_remote_train_ssh_timeout_marks_the_host_unreachable(tmp_path, monkeypatch):
    plugin, mod, _ad = _make_remote_train(tmp_path)
    _fake_ssh(monkeypatch, mod, raises=subprocess.TimeoutExpired(["ssh"], 60))
    assert plugin._remote_rc("true")[0] is None
    assert plugin._host_state == "unreachable"


def test_remote_train_stack_ready_is_three_valued(tmp_path, monkeypatch):
    plugin, mod, _ad = _make_remote_train(tmp_path)
    for returncode, expected in ((0, True), (1, False), (255, None)):
        _fake_ssh(monkeypatch, mod, returncode=returncode)
        assert plugin._stack_ready() is expected
    _fake_ssh(monkeypatch, mod, raises=OSError("no ssh"))
    assert plugin._stack_ready() is None


def test_remote_train_reconcile_skips_host_prep_when_unreachable(tmp_path):
    # A host that's off reads as unreachable, not "installing": no prep, no
    # pull, no launch, and the last known stack state stays on the card.
    plugin, _mod, _ad = _make_remote_train(tmp_path)
    plugin._stack_state = "ready"
    plugin._host_state = "unreachable"
    plugin._stack_ready = lambda: None
    calls = []
    plugin._stack_error = lambda: calls.append("error") or ""
    plugin._ensure_stack = lambda: calls.append("prep")
    plugin._pull_and_stage = lambda algo, slug: calls.append("pull") or False
    plugin._launch_training = lambda algo, slug: calls.append("launch") or True
    plugin._reconcile(["DQN"])
    assert calls == []
    assert plugin._status == "can't reach or log in to the host (never reached)"
    assert plugin._stack_state == "ready"
    assert "can't reach or log in to desk" in plugin.context.health_note


def test_remote_train_manual_request_while_unreachable_reports_it(tmp_path):
    plugin, _mod, _ad = _make_remote_train(tmp_path)
    plugin._stack_ready = lambda: None
    plugin._train_now.set()
    plugin._reconcile(["DQN"])
    assert not plugin._train_now.is_set()
    assert plugin._status.startswith("can't reach or log in to the host")


def test_remote_train_refresh_keeps_run_state_when_unreachable(tmp_path):
    # A light poll that can't reach the host must not take every running
    # algorithm for finished (and try to pull each one).
    plugin, _mod, _ad = _make_remote_train(tmp_path)
    plugin._stack_state = "ready"
    plugin._running_prev = {"dqn"}
    plugin._job_state = lambda: None
    pulls = []
    plugin._pull_and_stage = lambda algo, slug: pulls.append(algo) or False
    plugin._refresh(["DQN"])
    assert pulls == []
    assert plugin._running_prev == {"dqn"}
    assert plugin._status.startswith("can't reach or log in to the host")


def test_remote_train_note_clears_when_the_host_answers_again(tmp_path):
    plugin, _mod, _ad = _make_remote_train(tmp_path)
    plugin._note_contact(False)
    assert "can't reach or log in to desk" in plugin.context.health_note
    plugin._note_contact(True)
    assert plugin.context.health_note == ""


def test_remote_train_unreachable_status_shows_the_last_contact(tmp_path):
    plugin, _mod, _ad = _make_remote_train(tmp_path)
    plugin._last_contact = time.time() - 3 * 3600 - 60
    plugin._report_unreachable()
    assert plugin._status == "can't reach or log in to the host (last reached 3h ago)"


def test_remote_train_staged_model_survives_a_restart(tmp_path):
    # The card's "Last model pulled" and the last contact come back after a
    # daemon restart instead of reading "never".
    from kestrel_pi.agent import spec
    plugin, _mod, _ad = _make_remote_train(tmp_path)
    meta = {"algorithm": "DQN", "spec_version": spec.SPEC_VERSION,
            "data_watermark": {"latest_ts": 300.0, "episodes": 99},
            "train_seconds": 405.0}
    plugin._rsync_down = _fake_host(meta)
    plugin._note_contact(True)                  # a pass reaches the host first
    assert plugin._pull_and_stage("DQN", "dqn") is True
    plugin._record_pull("DQN")
    again, _mod, _ad = _make_remote_train(tmp_path)
    assert again._last_pull[0] == "DQN" and again._last_pull[2] == 405.0
    assert abs(again._last_pull[1] - time.time()) < 60
    assert again._last_contact == plugin._last_contact
    assert (_card(again, "remote-train")["pulled"]["value"]
            == "DQN just now (trained in 6m 45s)")


def test_remote_train_contact_is_saved_at_most_every_ten_minutes(tmp_path, monkeypatch):
    plugin, mod, _ad = _make_remote_train(tmp_path)
    clock = [1000.0]
    monkeypatch.setattr(mod, "time", types.SimpleNamespace(
        time=time.time, monotonic=lambda: clock[0], gmtime=time.gmtime,
        strftime=time.strftime, sleep=time.sleep))
    saves = []
    save = plugin._save_status
    plugin._save_status = lambda: saves.append(1) or save()
    plugin._note_contact(True)                  # the first contact is saved
    plugin._note_contact(True)
    clock[0] += 599
    plugin._note_contact(True)
    assert len(saves) == 1
    clock[0] += 2
    plugin._note_contact(True)
    assert len(saves) == 2
    plugin._note_contact(False)                 # a failed attempt saves nothing
    clock[0] += 3600
    plugin._note_contact(False)
    assert len(saves) == 2


def test_remote_train_ignores_a_malformed_status_file(tmp_path):
    from kestrel_pi.agent import REMOTE_STAGING_DIR
    staging = tmp_path / "agent" / REMOTE_STAGING_DIR
    staging.mkdir(parents=True)
    for text in ("not json", "[]", '{"last_contact": true}',
                 '{"last_contact": "x", "last_model": -5, "last_model_algorithm": 7}',
                 '{"last_model": 1e400, "last_model_algorithm": "DQN"}'):
        (staging / "remote-status.json").write_text(text)
        plugin, _mod, _ad = _make_remote_train(tmp_path)
        assert plugin._last_contact is None, text
        assert plugin._last_pull is None, text


def test_remote_train_status_file_leaves_host_models_alone(tmp_path):
    # host-models.json is read as {slug: record}; the status lives elsewhere.
    from kestrel_pi.agent import spec
    plugin, _mod, _ad = _make_remote_train(tmp_path)
    meta = {"algorithm": "DQN", "spec_version": spec.SPEC_VERSION,
            "data_watermark": {"latest_ts": 300.0, "episodes": 99}}
    plugin._rsync_down = _fake_host(meta)
    plugin._pull_and_stage("DQN", "dqn")
    plugin._record_pull("DQN")
    plugin._note_contact(True)
    assert set(plugin._host_models()) == {"dqn"}


def test_remote_train_status_save_failure_is_logged_once(tmp_path, caplog):
    from kestrel_pi.agent import REMOTE_STAGING_DIR
    plugin, _mod, agent_dir = _make_remote_train(tmp_path)
    # A file where the staging dir should be: every save fails.
    with open(os.path.join(agent_dir, REMOTE_STAGING_DIR), "w") as handle:
        handle.write("")
    with caplog.at_level(logging.INFO):
        plugin._save_status()
        plugin._save_status()
    assert sum("could not save" in r.getMessage() for r in caplog.records) == 1


def test_remote_train_declares_the_remote_training_service(tmp_path):
    from kestrel_pi import declarations
    _plugin, mod, _ad = _make_remote_train(tmp_path)
    assert mod.RemoteTrain.__provides__ == ("remote-training",)
    # Root and plugins-ctl read it statically, never importing the plugin.
    decl = declarations.read_declarations(os.path.join(PLUGIN_DIR, "remote-train.py"))
    assert tuple(decl["remote-train"]["provides"]) == ("remote-training",)


def test_remote_train_service_before_any_contact(tmp_path):
    plugin, _mod, _ad = _make_remote_train(tmp_path)
    assert plugin.provide_remote_training() == {
        "configured": True, "host_state": "unknown", "stack": "checking",
        "last_contact": None, "last_model": None, "running": []}


def test_remote_train_service_after_a_working_pass(tmp_path):
    plugin, _mod, _ad = _make_remote_train(tmp_path)
    plugin._note_contact(True)
    plugin._stack_state = "ready"
    plugin._record_pull("DQN")
    plugin._summarize(["DQN"], ["DQN"])
    service = plugin.provide_remote_training()
    assert service["host_state"] == "reachable" and service["stack"] == "ready"
    assert abs(service["last_contact"] - time.time()) < 60
    assert abs(service["last_model"] - time.time()) < 60
    assert service["running"] == ["DQN"]
    service["running"].append("PPO")            # a copy: callers can't change it
    assert plugin.provide_remote_training()["running"] == ["DQN"]


def test_remote_train_service_when_unreachable(tmp_path):
    plugin, _mod, _ad = _make_remote_train(tmp_path)
    plugin._note_contact(True)
    reached = plugin._last_contact
    plugin._note_contact(False)
    service = plugin.provide_remote_training()
    assert service["host_state"] == "unreachable"
    assert service["last_contact"] == reached


def test_remote_train_service_not_configured(tmp_path):
    plugin, _mod, _ad = _make_remote_train(tmp_path, train_host="")
    assert plugin.provide_remote_training()["configured"] is False
    plugin, _mod, _ad = _make_remote_train(tmp_path)
    os.remove(plugin.options["ssh_key"])
    assert plugin.provide_remote_training()["configured"] is False


def test_remote_train_service_carries_no_option_values(tmp_path):
    key = tmp_path / "key-mark"
    key.write_text("KEY")
    plugin, _mod, _ad = _make_remote_train(
        tmp_path, train_host="host-mark", train_user="user-mark",
        train_path="/path-mark", train_port=2222, ssh_key=str(key))
    plugin._note_contact(True)
    plugin._record_pull("DQN")
    text = json.dumps(plugin.provide_remote_training())
    assert "mark" not in text and "2222" not in text


def test_remote_train_service_through_the_registry(tmp_path):
    plugin, _mod, _ad = _make_remote_train(tmp_path)
    registry = ServiceRegistry()
    assert registry.get("remote-training") is None
    registry.provide("remote-training", plugin.provide_remote_training,
                     owner="remote-train")
    assert registry.get("remote-training")["configured"] is True


def test_remote_train_launch_script_runs_the_trainer_detached(tmp_path):
    plugin, _mod, _ad = _make_remote_train(tmp_path, timesteps=123456)
    script = plugin._train_launch_script("DQN", "dqn")
    assert "setsid" in script                                  # detached
    assert "kestrel_pi.agent.train" in script               # the same trainer
    assert "--algorithm DQN" in script
    assert "--timesteps 123456" in script
    assert "jobs/dqn.lock" in script                           # per-algo lock
    # The run imports the package just synced to pkg/, ahead of the venv's
    # copy, and the worker doesn't cd (the launcher already did).
    assert "PYTHONPATH=pkg venv/bin/python -m kestrel_pi.agent.train" in script
    worker = script.split("<<'RUN'")[1].split("RUN\n")[0]
    assert "\ncd " not in worker


def test_remote_train_launch_syncs_the_package_before_the_run(tmp_path):
    plugin, _mod, _ad = _make_remote_train(tmp_path)
    calls = []
    plugin._local_package_dir = lambda: "/opt/pkg/kestrel_pi"
    plugin._rsync_up = lambda local, remote: (calls.append(("up", remote)) or (True, ""))
    plugin._remote = lambda script, timeout=None: (calls.append(("run", "")) or (True, ""))
    assert plugin._launch_training("DQN", "dqn") is True
    assert calls[0] == ("up", "/w/pkg/kestrel_pi")
    assert calls[1][1].endswith("/agent/episodes") and calls[2][0] == "run"
    # A failed package sync launches nothing.
    calls.clear()
    plugin._rsync_up = lambda local, remote: (calls.append(("up", remote)) or (False, "x"))
    assert plugin._launch_training("DQN", "dqn") is False
    assert [c[0] for c in calls] == ["up"]


def test_remote_train_prep_script_records_a_failure_reason(tmp_path):
    # The host-prep script writes .stack-error (with a reason) when the venv or
    # the ML-stack install fails, and .stack-ready only on success, so the
    # device can surface a clear error instead of a silent "not prepared yet".
    plugin, _mod, _ad = _make_remote_train(tmp_path)
    script = plugin._stack_install_script()
    assert ".stack-error" in script
    assert "python3-venv" in script                            # the common cause
    assert ".stack-ready" in script
    # The prep clears a stale .stack-error on start and a broken venv.
    assert "rm -rf venv" in script


def test_remote_train_run_rsync_retries_a_dropped_transfer(tmp_path, monkeypatch):
    # A flaky tether drops a transfer; --partial resumes, so _run_rsync retries
    # before reporting failure (and does not retry when rsync is absent).
    plugin, mod, _ad = _make_remote_train(tmp_path)
    monkeypatch.setattr(mod.time, "sleep", lambda *_a: None)
    calls = {"n": 0}

    def flaky_then_ok(cmd, **_kw):
        calls["n"] += 1
        rc = 0 if calls["n"] >= 2 else 12      # rsync 12 = stream/connection error
        return types.SimpleNamespace(returncode=rc, stdout="done", stderr="")
    monkeypatch.setattr(mod.subprocess, "run", flaky_then_ok)
    ok, _out = plugin._run_rsync(["rsync", "-az", "a", "b"])
    assert ok is True and calls["n"] == 2       # retried once, then succeeded

    def always_fnf(cmd, **_kw):
        raise FileNotFoundError()
    calls["n"] = 0
    monkeypatch.setattr(mod.subprocess, "run", always_fnf)
    ok, msg = plugin._run_rsync(["rsync", "-az", "a", "b"])
    assert ok is False and "not installed" in msg and calls["n"] == 0


def test_remote_train_algo_running_respects_the_stale_lock(tmp_path):
    plugin, _mod, _ad = _make_remote_train(tmp_path, stale_lock_hours=6)
    now = time.time()
    plugin._remote = lambda script, timeout=None: (True, str(now - 600))  # 10m
    assert plugin._algo_running("dqn") is True
    plugin._remote = lambda script, timeout=None: (True, str(now - 7 * 3600))  # 7h
    assert plugin._algo_running("dqn") is False                # stale -> relaunch ok
    plugin._remote = lambda script, timeout=None: (True, "")   # no lock
    assert plugin._algo_running("dqn") is False


def test_remote_train_health_note_states(tmp_path):
    # Missing config.
    plugin, _mod, _ad = _make_remote_train(tmp_path, train_host="")
    plugin._set_health()
    assert "not configured" in plugin.context.health_note
    # Configured + key + algorithms -> no note.
    plugin, _mod, _ad = _make_remote_train(tmp_path)
    plugin._set_health()
    assert plugin.context.health_note == ""
    # Nothing to train: no algorithms selected and the agent's own algorithm
    # isn't a valid one (otherwise that one is always trained).
    plugin, _mod, _ad = _make_remote_train(tmp_path, algorithms=[],
                                           _agent_algorithm="bogus")
    plugin._set_health()
    assert "no algorithm to train" in plugin.context.health_note
    # A recorded host-prep failure surfaces on the /plugins note and survives
    # the per-epoch _set_health() refresh (it reads the cached reason).
    plugin, _mod, _ad = _make_remote_train(tmp_path)
    plugin._stack_err = "could not create a Python venv on the host"
    plugin._set_health()
    assert "host training stack not ready" in plugin.context.health_note
    assert "could not create a Python venv" in plugin.context.health_note


def test_remote_train_card_shows_target_and_training_state(tmp_path):
    plugin, _mod, _ad = _make_remote_train(tmp_path)
    plugin._status = "training DQN"
    plugin._stack_state = "ready"
    card = _card(plugin, "remote-train")
    assert card["target"]["value"] == "me@desk:/w"
    assert card["training"]["value"] == "training DQN"
    assert card["pulled"]["value"] == "never"
    # Host stack shows the real cached state, not a cryptic "see Training".
    assert card["stack"]["value"] == "ready"
    plugin._stack_state = "failed"
    assert _card(plugin, "remote-train")["stack"]["value"] == "failed"


def test_remote_train_card_shows_pulled_model_training_time(tmp_path):
    # Once a remote model is pulled, the card shows how long the host took to
    # train it (from the model sidecar), e.g. 405s -> "6m 45s".
    plugin, _mod, _ad = _make_remote_train(tmp_path)
    plugin._last_pull = ("DQN", time.time(), 405.0)
    pulled = _card(plugin, "remote-train")["pulled"]["value"]
    assert pulled.startswith("DQN ")
    assert "(trained in 6m 45s)" in pulled
    # A pull without a recorded duration (older model) just shows the time.
    plugin._last_pull = ("DQN", time.time(), None)
    assert "trained in" not in _card(plugin, "remote-train")["pulled"]["value"]


def test_remote_train_refresh_updates_run_state_without_a_full_reconcile(tmp_path):
    # The light poll turns a run that has finished on the host from "running" to
    # idle on the card, and pulls the just-finished model - no stack prep/launch.
    plugin, _mod, _ad = _make_remote_train(tmp_path, algorithms="DQN,PPO")
    plugin._stack_state = "ready"
    plugin._running_prev = {"dqn", "ppo"}       # both were running last poll
    plugin._job_state = lambda: {"ppo"}          # dqn has since finished
    pulled = []
    plugin._pull_and_stage = lambda algo, slug: pulled.append(algo) or True
    plugin._refresh(["DQN", "PPO"])
    assert pulled == ["DQN"]                      # only the finished one is pulled
    assert plugin._running_prev == {"ppo"}
    assert plugin._status == "running PPO"        # card no longer claims DQN


def test_remote_train_maybe_reconcile_polls_between_full_reconciles(tmp_path):
    # A non-due wake does the light refresh, not a full reconcile.
    plugin, _mod, _ad = _make_remote_train(tmp_path)
    plugin._last_run = time.monotonic()           # a full reconcile just ran
    did = {"reconcile": 0, "refresh": 0}
    plugin._reconcile = lambda algos: did.__setitem__("reconcile", did["reconcile"] + 1)
    plugin._refresh = lambda algos: did.__setitem__("refresh", did["refresh"] + 1)
    plugin._maybe_reconcile(force=False)
    assert did == {"reconcile": 0, "refresh": 1}
    # A forced wake (trigger/reconnect) still does the full reconcile.
    plugin._maybe_reconcile(force=True)
    assert did == {"reconcile": 1, "refresh": 1}


def test_remote_train_now_trigger_wakes_the_worker(tmp_path):
    # `kestrel remote-train now` drops a trigger file; on_epoch consumes it
    # and wakes the reconcile worker at once (same idea as the agent's
    # train-request), instead of waiting for the interval.
    plugin, _mod, agent_dir = _make_remote_train(tmp_path)
    trigger = os.path.join(agent_dir, ".remote-train-now")
    # No trigger file -> on_epoch leaves the worker asleep.
    plugin._wake.clear()
    plugin.on_epoch(1, {})
    assert not plugin._wake.is_set()
    # Trigger present -> on_epoch removes it and sets the wake event.
    with open(trigger, "w", encoding="utf-8") as handle:
        handle.write("")
    plugin.on_epoch(2, {})
    assert plugin._wake.is_set()
    assert not os.path.exists(trigger)


# -- row buttons: declaration, gating, on_webhook (Phase 4.8) ----------------
# Each shipped plugin that used to own a bespoke web route now declares up to
# two row buttons (__actions__/webhook_actions) and renders/handles them in its
# own on_webhook; the generic core mechanism is tested in test_web.py.
def _req(method="GET", args=None, form=None):
    """A minimal stand-in for the Flask request on_webhook receives."""
    return types.SimpleNamespace(method=method, args=args or {}, form=form or {})


# -- wpa-sec: the "Show" cracked-networks page -------------------------------
_POTFILE = (
    "001122334455:66778899aabb:HomeNet:s3cret-pass\n"
    "001122334466:66778899aacc:Cafe\\:Wifi:latte123\n"   # ESSID with a colon
    "001122334455:66778899aabb:HomeNet:s3cret-pass\n"    # exact duplicate
    "00aabbccddee:112233445566::openpass\n"               # hidden (empty) ESSID
    "shortline\n"                                          # too few fields -> skip
    "\n"
)


def _wpa_sec_for_webhook(tmp_path, web_authed):
    plugin = _make_wpa_sec(tmp_path, online=False)
    plugin.context.web_authed = web_authed
    return plugin


def _write_potfile(plugin, text):
    path = plugin._potfile_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(text)


def test_wpa_sec_declares_a_single_show_action(tmp_path):
    plugin = _make_wpa_sec(tmp_path, online=False)
    assert plugin.__actions__ == (
        {"label": "Show", "path": "cracked", "method": "get"},)
    assert plugin.webhook_actions() == plugin.__actions__


def test_wpa_sec_parse_potfile_dedups_and_skips(tmp_path):
    mod = _load_wpa_sec()
    recs = mod.WpaSec._parse_cracked_potfile(_POTFILE)
    assert len(recs) == 3                                  # dup + malformed dropped
    assert recs[0] == {"bssid": "001122334455", "essid": "HomeNet",
                       "psk": "s3cret-pass"}
    assert recs[1]["essid"] == "Cafe\\:Wifi" and recs[1]["psk"] == "latte123"
    assert recs[2]["essid"] == "" and recs[2]["psk"] == "openpass"


def test_wpa_sec_cracked_page_requires_login(tmp_path):
    # Auth off: never render passphrases, show a notice instead.
    plugin = _wpa_sec_for_webhook(tmp_path, web_authed=False)
    _write_potfile(plugin, "001122334455:66778899aabb:HomeNet:SECRET\n")
    page = plugin.on_webhook("cracked", _req())
    assert page.title.startswith("recovered networks")
    assert "SECRET" not in page.body
    assert "signed in" in page.body or "web login" in page.body


def test_wpa_sec_cracked_page_lists_and_escapes(tmp_path):
    plugin = _wpa_sec_for_webhook(tmp_path, web_authed=True)
    _write_potfile(plugin, "001122334455:66778899aabb:Home<&>Net:p@ss<x>\n")
    page = plugin.on_webhook("cracked", _req())
    assert "Recovered networks" in page.body
    assert "Home&lt;&amp;&gt;Net" in page.body and "Home<&>Net" not in page.body
    assert "p@ss&lt;x&gt;" in page.body                   # passphrase shown, escaped
    assert "1 recovered network(s)" in page.body


def test_wpa_sec_cracked_page_search_filters_by_ssid(tmp_path):
    plugin = _wpa_sec_for_webhook(tmp_path, web_authed=True)
    _write_potfile(plugin,
                   "aa:bb:Alpha:1\n"
                   "cc:dd:Beta:2\n")
    page = plugin.on_webhook("cracked", _req(args={"q": "alph"}))
    assert "Alpha" in page.body and "Beta" not in page.body
    assert "1 of 2 recovered networks match" in page.body


def test_wpa_sec_unknown_path_is_404(tmp_path):
    plugin = _wpa_sec_for_webhook(tmp_path, web_authed=True)
    page = plugin.on_webhook("nope", _req())
    assert page.status == 404


# -- updates: the "Check" action ---------------------------------------------
def _updates_for_webhook(tmp_path, reply):
    mod = _load_module("updates.py", "updates_webhook_under_test")
    plugin = mod.Updates()
    options = {"repo": "o/r", "check_interval_seconds": 21600, "timeout": 30}
    plugin.options = options
    sent = []

    def fake_apply(change):
        sent.append(change)
        return reply

    plugin.context = PluginContext(
        name="updates", options=options,
        capture=CaptureInterface(str(tmp_path)),
        connectivity=types.SimpleNamespace(online=False),
        services=ServiceRegistry(), capabilities=("network", "ui"),
        apply=fake_apply)
    # on_webhook nudges the worker on success; this minimal helper skips
    # on_loaded, so give it the event the hook touches.
    plugin._wake = threading.Event()
    return plugin, sent


def test_updates_declares_a_check_action(tmp_path):
    mod = _load_module("updates.py", "updates_actions_under_test")
    assert mod.Updates.__actions__ == (
        {"label": "Check", "path": "check", "method": "post"},)


def test_updates_check_goes_through_apply(tmp_path):
    plugin, sent = _updates_for_webhook(tmp_path, {"ok": True})
    result = plugin.on_webhook("check", _req(method="POST"))
    assert sent == [{"target": "update-check", "action": "check-now"}]
    assert result.message == "Update check requested." and not result.error


def test_updates_check_surfaces_a_helper_error(tmp_path):
    plugin, sent = _updates_for_webhook(
        tmp_path, {"ok": False, "error": "enable the updates plugin first"})
    result = plugin.on_webhook("check", _req(method="POST"))
    assert result.error and "enable the updates plugin first" in result.message


# -- remote-train: the "Train" action ----------------------------------------
def test_remote_train_declares_a_train_action(tmp_path):
    plugin, _mod, _agent = _make_remote_train(tmp_path)
    assert plugin.__actions__ == (
        {"label": "Train", "path": "train", "method": "post"},)


def test_remote_train_action_hidden_until_ready(tmp_path):
    # Configured + key present -> shown; missing host -> hidden.
    plugin, _mod, _agent = _make_remote_train(tmp_path)
    assert plugin.webhook_actions() == plugin.__actions__
    plugin.options["train_host"] = ""
    assert plugin.webhook_actions() == ()


def test_remote_train_train_goes_through_apply(tmp_path):
    plugin, _mod, _agent = _make_remote_train(tmp_path)
    sent = []
    plugin.context._apply = lambda change: (sent.append(change) or {"ok": True})
    result = plugin.on_webhook("train", _req(method="POST"))
    assert sent == [{"target": "remote-train", "action": "train-now"}]
    assert result.message == "Remote training requested." and not result.error


def test_remote_train_refused_without_passwordless_ssh(tmp_path):
    plugin, _mod, _agent = _make_remote_train(tmp_path, ssh_key=str(tmp_path / "no"))
    sent = []
    plugin.context._apply = lambda change: (sent.append(change) or {"ok": True})
    result = plugin.on_webhook("train", _req(method="POST"))
    assert result.error and "passwordless SSH is not set up" in result.message
    assert sent == []                                      # helper never invoked


# -- backup: the "Backup" and "Restore" actions -------------------------------
def test_backup_declares_backup_then_restore(tmp_path):
    plugin, _mod = _make_backup(tmp_path, ssh_key=str(tmp_path / "id"))
    (tmp_path / "id").write_text("K")
    labels = [a["label"] for a in plugin.webhook_actions()]
    assert labels == ["Backup", "Restore"]                 # order 1, then 2


def test_backup_actions_hidden_until_ready(tmp_path):
    # No key file -> both hidden; key present -> both shown.
    plugin, _mod = _make_backup(tmp_path, ssh_key=str(tmp_path / "id"))
    assert plugin.webhook_actions() == ()
    (tmp_path / "id").write_text("K")
    assert len(plugin.webhook_actions()) == 2


def test_backup_now_goes_through_apply(tmp_path):
    plugin, _mod = _make_backup(tmp_path, ssh_key=str(tmp_path / "id"))
    (tmp_path / "id").write_text("K")
    sent = []
    plugin.context._apply = lambda change: (sent.append(change) or {"ok": True})
    result = plugin.on_webhook("backup-now", _req(method="POST"))
    assert sent == [{"target": "backup", "action": "now"}]
    assert result.message == "Backup started." and not result.error


def test_backup_restore_asks_to_confirm_first(tmp_path):
    plugin, _mod = _make_backup(tmp_path, ssh_key=str(tmp_path / "id"))
    (tmp_path / "id").write_text("K")
    sent = []
    plugin.context._apply = lambda change: (sent.append(change) or {"ok": True})
    plugin.context.form_csrf = "tok"
    page = plugin.on_webhook("restore", _req(method="GET"))
    # A GET lands on the confirm page; nothing is applied yet.
    assert sent == []
    assert "Restore from the backup?" in page.body
    assert 'name="confirm" value="yes"' in page.body
    assert 'name="csrf" value="tok"' in page.body


def test_backup_restore_confirm_goes_through_apply(tmp_path):
    plugin, _mod = _make_backup(tmp_path, ssh_key=str(tmp_path / "id"))
    (tmp_path / "id").write_text("K")
    sent = []
    plugin.context._apply = lambda change: (sent.append(change) or {"ok": True})
    result = plugin.on_webhook("restore",
                               _req(method="POST", form={"confirm": "yes"}))
    assert sent == [{"target": "backup-restore", "action": "restore"}]
    assert "Restoring" in result.body                      # the "restoring" page


def test_backup_refused_without_passwordless_ssh(tmp_path):
    plugin, _mod = _make_backup(tmp_path, ssh_key=str(tmp_path / "no-key"))
    sent = []
    plugin.context._apply = lambda change: (sent.append(change) or {"ok": True})
    result = plugin.on_webhook("backup-now", _req(method="POST"))
    assert result.error and "passwordless SSH is not set up" in result.message
    assert sent == []


def test_ssh_never_trusts_an_unseen_host_key():
    # The host key is confirmed interactively once, at `backup setup` /
    # `remote-train setup` (ssh-copy-id asks). Every other connection must
    # require that recorded key, never record a new one on its own.
    files = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
    for relpath in ("plugins/backup.py", "plugins/remote-train.py", "kestrel.sh"):
        with open(os.path.join(files, relpath), encoding="utf-8") as handle:
            text = handle.read()
        assert "StrictHostKeyChecking=accept-new" not in text, relpath
        assert "StrictHostKeyChecking=no" not in text, relpath
        assert "StrictHostKeyChecking=yes" in text, relpath


# -- uploaders: https only, no redirects, capped reads, private potfile ------

class _Http(_Streamable):
    def __init__(self, status=200, payload=None, raw=None, headers=None):
        self.status_code = status
        self._payload = payload if payload is not None else {}
        self._raw = raw
        self.headers = headers or {}

    def json(self):
        return self._payload

    def iter_content(self, chunk_size=1):
        if self._raw is None:
            yield from super().iter_content(chunk_size)
            return
        for start in range(0, len(self._raw), chunk_size):
            yield self._raw[start:start + chunk_size]


def _record_calls(monkeypatch, method, response):
    import requests

    calls = []

    def fake(*args, **kwargs):
        calls.append(kwargs)
        return response

    monkeypatch.setattr(requests, method, fake)
    return calls


def test_wpa_sec_refuses_a_plain_http_endpoint(tmp_path, monkeypatch):
    plugin = _make_wpa_sec(tmp_path)
    plugin.options["endpoint"] = "http://wpa-sec.example"
    calls = _record_calls(monkeypatch, "post", _Http())
    assert plugin._upload(b"cap") is False
    assert calls == []        # the key never left the device


def test_wpa_sec_sends_the_key_without_following_redirects(tmp_path, monkeypatch):
    plugin = _make_wpa_sec(tmp_path)
    calls = _record_calls(monkeypatch, "post", _Http(
        302, headers={"Location": "https://elsewhere.example/"}))
    assert plugin._upload(b"cap") is False
    assert calls and calls[0]["allow_redirects"] is False and calls[0]["stream"] is True


def test_wpa_sec_download_writes_a_private_potfile(tmp_path, monkeypatch):
    plugin = _make_wpa_sec(tmp_path)
    _record_calls(monkeypatch, "get", _Http(
        raw=b"001122334455:66778899aabb:Home:placeholder-pass\n"))
    plugin._download_results()
    potfile = plugin._potfile_path()
    assert open(potfile).read().startswith("001122334455:")
    assert os.stat(potfile).st_mode & 0o777 == 0o600
    assert os.stat(os.path.dirname(potfile)).st_mode & 0o777 == 0o700


def test_wpa_sec_download_tightens_an_existing_potfile(tmp_path, monkeypatch):
    plugin = _make_wpa_sec(tmp_path)
    potfile = plugin._potfile_path()
    os.makedirs(os.path.dirname(potfile), mode=0o755)
    with open(potfile + ".tmp", "w") as handle:      # a leftover, world-readable
        handle.write("old\n")
    os.chmod(potfile + ".tmp", 0o644)
    _record_calls(monkeypatch, "get", _Http(raw=b"a:b:c:d\n"))
    plugin._download_results()
    assert os.stat(potfile).st_mode & 0o777 == 0o600
    assert os.stat(os.path.dirname(potfile)).st_mode & 0o777 == 0o700


def test_wpa_sec_download_refuses_an_oversized_body(tmp_path, monkeypatch):
    plugin = _make_wpa_sec(tmp_path)
    _record_calls(monkeypatch, "get", _Http(raw=b"", headers={"Content-Length": str(10 ** 9)}))
    plugin._download_results()
    assert not os.path.exists(plugin._potfile_path())


def test_wigle_refuses_a_plain_http_endpoint(tmp_path, monkeypatch):
    plugin = _make_wigle(tmp_path, position={"lat": 12.3, "lon": -45.6, "alt": None})
    plugin.options["endpoint"] = "http://wigle.example"
    plugin.on_handshake(types.SimpleNamespace(bssid="AA:BB:CC:DD:EE:A6", essid="x"))
    calls = _record_calls(monkeypatch, "post", _Http(payload={"success": True}))
    plugin._upload()
    assert calls == [] and len(plugin._observations) == 1
    assert "https://" in plugin._last_status


def test_wigle_does_not_follow_a_redirect_with_credentials(tmp_path, monkeypatch):
    plugin = _make_wigle(tmp_path, position={"lat": 12.3, "lon": -45.6, "alt": None})
    plugin.on_handshake(types.SimpleNamespace(bssid="AA:BB:CC:DD:EE:A6", essid="x"))
    calls = _record_calls(monkeypatch, "post", _Http(
        307, headers={"Location": "https://elsewhere.example/upload"}))
    plugin._upload()
    assert calls[0]["allow_redirects"] is False
    assert len(plugin._observations) == 1 and "redirect" in plugin._last_status


def test_every_shipped_row_button_survives_normalization():
    """A declared __actions__ button that normalize_actions would drop (an empty
    path, a missing label) silently never shows on the Plugins page. Every
    shipped plugin's buttons must come through intact."""
    import ast

    from kestrel_pi.plugins import normalize_actions

    for fname in sorted(os.listdir(PLUGIN_DIR)):
        if not fname.endswith(".py"):
            continue
        tree = ast.parse(open(os.path.join(PLUGIN_DIR, fname), encoding="utf-8").read())
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign) and any(
                    isinstance(t, ast.Name) and t.id == "__actions__" for t in node.targets):
                declared = ast.literal_eval(node.value)
                kept = normalize_actions(declared)
                assert len(kept) == len(declared), "%s: a row button is dropped" % fname
                assert all(a["path"] for a in kept), fname


# -- every plugin page opens with the standard way back ----------------------

_NAV = '<p><a href="/plugins">&larr; Plugins</a> &middot; <a href="/">Dashboard</a></p>'
_NAV_TOKEN = ('<p><a href="/plugins?token=abc">&larr; Plugins</a> &middot; '
              '<a href="/?token=abc">Dashboard</a></p>')


def test_backup_restore_pages_link_back_to_plugins_and_the_dashboard(tmp_path):
    plugin, _mod = _make_backup(tmp_path, ssh_key=str(tmp_path / "id"))
    (tmp_path / "id").write_text("K")
    plugin.context._apply = lambda change: {"ok": True}
    plugin.context.form_csrf = "tok"
    confirm = plugin.on_webhook("restore", _req(method="GET")).body
    assert confirm.index(_NAV) < confirm.index("<h1>")
    started = plugin.on_webhook("restore", _req(method="POST", form={"confirm": "yes"},
                                               args={"token": "abc"})).body
    assert started.index(_NAV_TOKEN) < started.index("<h1>")


def test_wpa_sec_cracked_page_links_back_to_plugins_and_the_dashboard(tmp_path):
    plugin = _wpa_sec_for_webhook(tmp_path, web_authed=False)
    body = plugin.on_webhook("cracked", _req()).body
    assert body.index(_NAV) < body.index("<h1>")
    assert _NAV_TOKEN in plugin.on_webhook("cracked", _req(args={"token": "abc"})).body


def test_session_stats_page_uses_the_dashboard_shell_and_links_back(tmp_path):
    mod = _load_module("session-stats.py", "session_stats_page_under_test")
    plugin = mod.SessionStats()
    plugin.options = {}
    plugin.context = _ctx(tmp_path, ("ui", "web"), name="session-stats")
    plugin.on_loaded()
    plugin.on_handshake(types.SimpleNamespace(bssid="AA:BB:CC:DD:EE:01"))
    plugin.on_epoch(4, {})
    page = plugin.on_webhook("", _req())
    assert page.title == "Session stats"
    assert page.body.index(_NAV) < page.body.index("<h1>Session stats</h1>")
    assert "Handshakes: <b>1</b>" in page.body and "Epochs: <b>4</b>" in page.body
    assert _NAV_TOKEN in plugin.on_webhook("", _req(args={"token": "abc"})).body
    body, status, headers = plugin.on_webhook("stats.json", _req())
    assert status == 200 and json.loads(body)["handshakes"] == 1
