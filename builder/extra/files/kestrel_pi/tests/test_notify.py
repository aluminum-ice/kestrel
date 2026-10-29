"""notify plugin: digests, battery/capture/update alerts, the offline queue and
the ntfy/Telegram backends, with no real network (requests.post is faked) and
the state directory in tmp_path. Tokens and topics here are made up at test
time; names are placeholders."""

import importlib.util
import json
import os
import stat
import types

import pytest

requests = pytest.importorskip("requests")

from kestrel_pi.capture import CaptureInterface  # noqa: E402
from kestrel_pi.context import PluginContext, ServiceRegistry  # noqa: E402
from kestrel_pi.plugins import PluginPage, PluginRedirect  # noqa: E402
from kestrel_pi.ui import StubUI, UIState  # noqa: E402

PLUGIN_DIR = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", "..", "plugins"))


def _mod():
    spec = importlib.util.spec_from_file_location(
        "notify_under_test", os.path.join(PLUGIN_DIR, "notify.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


N = _mod()


class _Resp:
    def __init__(self, status=200):
        self.status_code = status
        self.headers = {}

    def iter_content(self, chunk_size=1):
        yield b'{"ok":true}'

    def close(self):
        pass


@pytest.fixture
def posts(monkeypatch):
    sent = []

    def fake(url, **kw):
        sent.append((url, kw))
        return _Resp()

    monkeypatch.setattr(requests, "post", fake)
    return sent


def _plugin(tmp_path, monkeypatch, options=None, online=True, authed=True, worker=False):
    monkeypatch.setattr(N, "_state_dir", lambda: str(tmp_path / "state"))
    if not worker:
        # No background worker: these tests drive every step by hand.
        monkeypatch.setattr(N.Notify, "_run", lambda self: None)
    p = N.Notify()
    p.options = dict({"backend": "ntfy", "ntfy_server": "https://ntfy.example.com",
                      "events": N.DEFAULT_EVENTS, "digest_minutes": 30,
                      "include_names": False, "battery_percent": 20,
                      "max_per_hour": 6, "quiet_hours": "", "timeout": 5},
                     **(options or {}))
    p.context = PluginContext(
        name="notify", options=p.options, capture=CaptureInterface(str(tmp_path)),
        connectivity=types.SimpleNamespace(online=online), services=ServiceRegistry(),
        capabilities=("network", "capture-read", "ui", "web"))
    p.context.web_authed = authed
    p.on_loaded()
    if not worker:
        p._worker.join(timeout=5)
    p._config_path = str(tmp_path / "config.toml")
    p._update_state = str(tmp_path / "update-state.json")
    return p


def _hs(essid, bssid="00:00:5E:00:53:01"):
    return types.SimpleNamespace(essid=essid, bssid=bssid, type="eapol", path="x")


# -- messages ---------------------------------------------------------------------

def test_digest_is_counts_only_by_default():
    msg = N.handshake_digest(12, {"My Network", "Cafe Guest", "Library WiFi"}, 30, False)
    assert msg["title"] == "Kestrel: 12 new handshakes"
    assert "12 new handshakes from 3 networks in the last 30 minutes." == msg["body"]
    assert "My Network" not in msg["body"]


def test_digest_names_only_when_asked_and_cleaned():
    names = {"My Network", "Cafe\x07 Guest", ""} | {"Net %d" % i for i in range(6)}
    body = N.handshake_digest(9, names, 30, True)["body"]
    assert "Networks: " in body and "(+4 more)" in body
    assert "\x07" not in body


def test_quiet_hours():
    assert N.in_quiet_hours("22-07", 23) and N.in_quiet_hours("22-07", 3)
    assert not N.in_quiet_hours("22-07", 12)
    assert N.in_quiet_hours("09-17", 10) and not N.in_quiet_hours("09-17", 18)
    assert not N.in_quiet_hours("", 3) and not N.in_quiet_hours("x", 3)


# -- events -----------------------------------------------------------------------

def test_handshakes_become_one_digest_without_addresses(tmp_path, monkeypatch, posts):
    p = _plugin(tmp_path, monkeypatch)
    for essid in ("My Network", "My Network", "Cafe Guest"):
        p.on_handshake(_hs(essid))
    p._flush_digest()                       # the window hasn't passed
    assert p._queue == []
    p._flush_digest(force=True)
    assert len(p._queue) == 1
    p._drain()
    url, kw = posts[0]
    assert url.startswith("https://ntfy.example.com/kestrel-")
    assert kw["data"] == b"3 new handshakes from 2 networks in the last 30 minutes."
    assert "00:00:5E" not in kw["data"].decode()
    assert kw["headers"]["Title"] == "Kestrel: 3 new handshakes"
    assert kw["allow_redirects"] is False and p._queue == []


def test_battery_low_once_and_power_loss(tmp_path, monkeypatch):
    p = _plugin(tmp_path, monkeypatch)
    p._check_battery({"percent": 80, "status": "charging"})
    p._check_battery({"percent": 79, "status": "discharging"})
    assert [m["title"] for m in p._queue] == ["Kestrel is on battery"]
    p._check_battery({"percent": 19, "status": "discharging"})
    p._check_battery({"percent": 18, "status": "discharging"})
    lows = [m for m in p._queue if m["title"] == "Kestrel battery low"]
    assert len(lows) == 1 and lows[0]["urgent"]
    p._check_battery({"percent": 40, "status": "charging"})
    p._check_battery({"percent": 19, "status": "discharging"})
    assert len([m for m in p._queue if m["title"] == "Kestrel battery low"]) == 2


def test_capture_stop_alerts_only_while_switched_on(tmp_path, monkeypatch):
    p = _plugin(tmp_path, monkeypatch)
    (tmp_path / "config.toml").write_text("[capture]\nenabled = true\n")
    p._check_capture(True)
    p._check_capture(False)                 # one blip: a relaunch, no alert
    p._check_capture(True)
    p._check_capture(False)
    p._check_capture(False)
    assert [m["title"] for m in p._queue] == ["Kestrel capture stopped"]
    p._check_capture(False)                 # still down: no repeat
    assert len(p._queue) == 1
    q = _plugin(tmp_path / "q", monkeypatch)
    (tmp_path / "q").mkdir(exist_ok=True)
    (tmp_path / "q" / "config.toml").write_text("[capture]\nenabled = false\n")
    q._config_path = str(tmp_path / "q" / "config.toml")
    for active in (True, False, False):
        q._check_capture(active)
    assert q._queue == []                   # switched off on purpose


def test_update_alert_once_per_release(tmp_path, monkeypatch):
    p = _plugin(tmp_path, monkeypatch)
    (tmp_path / "update-state.json").write_text(json.dumps(
        {"update_available": True, "latest": "1.2.3"}))
    p._check_update()
    p._update_checked = 0
    p._check_update()
    assert [m["body"] for m in p._queue] == [
        "Release 1.2.3 is available. Open the dashboard to update."]
    again = _plugin(tmp_path, monkeypatch)        # remembered across a restart
    again._check_update()
    assert again._queue[-1:] == p._queue[-1:] and len(again._queue) == 1


def test_online_alert_is_opt_in(tmp_path, monkeypatch):
    p = _plugin(tmp_path, monkeypatch)
    p.on_internet_available()
    assert p._queue == []
    q = _plugin(tmp_path / "q", monkeypatch, {"events": "online"})
    q.on_internet_available()
    q.on_internet_available()
    assert [m["title"] for m in q._queue] == ["Kestrel is online"]


# -- queue and sending ------------------------------------------------------------

def test_offline_alerts_wait_and_survive_a_restart(tmp_path, monkeypatch, posts):
    p = _plugin(tmp_path, monkeypatch, online=False)
    p._enqueue(N.message("test", "T", "B"))
    p._drain()
    assert posts == [] and len(p._queue) == 1
    later = _plugin(tmp_path, monkeypatch, online=True)
    assert len(later._queue) == 1
    later._drain()
    assert len(posts) == 1 and later._queue == []


def test_failures_keep_the_alert_and_back_off(tmp_path, monkeypatch):
    monkeypatch.setattr(requests, "post", lambda *a, **k: _Resp(500))
    p = _plugin(tmp_path, monkeypatch)
    p._enqueue(N.message("test", "T", "B"))
    p._drain()
    assert len(p._queue) == 1 and "HTTP 500" in p._last_error
    assert p._backoff_until > 0


def test_rate_limit_and_quiet_hours_but_urgent_gets_through(tmp_path, monkeypatch, posts):
    p = _plugin(tmp_path, monkeypatch, {"max_per_hour": 2, "quiet_hours": "0-23"})
    monkeypatch.setattr(N, "in_quiet_hours", lambda spec, hour: False)
    for i in range(4):
        p._enqueue(N.message("test", "T%d" % i, "B"))
    p._drain()
    assert len(posts) == 2 and len(p._queue) == 2
    monkeypatch.setattr(N, "in_quiet_hours", lambda spec, hour: True)
    p._sent_times = []
    p._enqueue(N.message("battery", "Kestrel battery low", "low", urgent=True))
    p._drain()
    assert posts[-1][1]["headers"]["Title"] == "Kestrel battery low"
    assert posts[-1][1]["headers"]["Priority"] == "high"
    assert len(p._queue) == 2                # the quiet-hours ones still wait


def test_ntfy_needs_https_and_sends_the_token(tmp_path, monkeypatch, posts):
    p = _plugin(tmp_path, monkeypatch, {"ntfy_server": "http://ntfy.example.com"})
    with pytest.raises(Exception):
        p.send(N.message("test", "T", "B"))
    assert posts == []
    q = _plugin(tmp_path / "q", monkeypatch, {"ntfy_token": "tk_made_up",
                                              "ntfy_topic": "my-topic"})
    q.send(N.message("test", "T", "B"))
    url, kw = posts[0]
    assert url == "https://ntfy.example.com/my-topic"
    assert kw["headers"]["Authorization"] == "Bearer tk_made_up"


def test_generated_topic_is_private_and_stable(tmp_path, monkeypatch):
    p = _plugin(tmp_path, monkeypatch)
    topic = p._topic()
    assert topic.startswith("kestrel-") and len(topic) > 24
    assert stat.S_IMODE(os.stat(tmp_path / "state" / "topic").st_mode) == 0o600
    assert p._topic() == topic


def test_telegram_errors_never_carry_the_token(tmp_path, monkeypatch):
    token = "123456:made-up-" + "x" * 20

    def boom(url, **kw):
        raise requests.ConnectionError("Max retries exceeded with url: %s" % url)

    monkeypatch.setattr(requests, "post", boom)
    p = _plugin(tmp_path, monkeypatch, {"backend": "telegram", "telegram_token": token,
                                        "telegram_chat": "42"})
    with pytest.raises(Exception) as err:
        p.send(N.message("test", "T", "B"))
    assert token not in str(err.value) and "ConnectionError" in str(err.value)
    p._enqueue(N.message("test", "T", "B"))
    p._drain()
    assert token not in p._last_error


def test_telegram_request_shape(tmp_path, monkeypatch, posts):
    p = _plugin(tmp_path, monkeypatch, {"backend": "telegram",
                                        "telegram_token": "123:made-up",
                                        "telegram_chat": "42"})
    p.send(N.message("test", "Kestrel test", "It works."))
    url, kw = posts[0]
    assert url == "https://api.telegram.org/bot123:made-up/sendMessage"
    assert kw["json"] == {"chat_id": "42", "text": "Kestrel test\nIt works.",
                          "disable_web_page_preview": True}


# -- pages and card -----------------------------------------------------------------

def test_send_test_button_reports_the_result(tmp_path, monkeypatch, posts):
    p = _plugin(tmp_path, monkeypatch)
    ok = p.on_webhook("test", types.SimpleNamespace(method="POST", args={}))
    assert isinstance(ok, PluginRedirect) and "sent" in ok.message
    monkeypatch.setattr(requests, "post", lambda *a, **k: _Resp(403))
    bad = p.on_webhook("test", types.SimpleNamespace(method="POST", args={}))
    assert bad.error and "HTTP 403" in bad.message


def test_status_page_shows_only_a_generated_topic_and_only_signed_in(tmp_path, monkeypatch):
    p = _plugin(tmp_path, monkeypatch)
    page = p.on_webhook("", types.SimpleNamespace(method="GET", args={}))
    assert isinstance(page, PluginPage) and p._topic() in page.body
    signed_out = _plugin(tmp_path, monkeypatch, authed=False)
    assert p._topic() not in signed_out.on_webhook(
        "", types.SimpleNamespace(method="GET", args={})).body
    own = _plugin(tmp_path / "o", monkeypatch, {"ntfy_topic": "secret-topic-made-up"})
    body = own.on_webhook("", types.SimpleNamespace(method="GET", args={})).body
    assert "secret-topic-made-up" not in body and "the one you set" in body


def test_dashboard_card(tmp_path, monkeypatch):
    p = _plugin(tmp_path, monkeypatch)
    ui = StubUI().for_plugin("notify")
    p.on_ui_setup(ui)
    p._enqueue(N.message("test", "T", "B"))
    p.on_ui_update(ui, UIState())
    assert ui.get("status") == "ready"
    assert ui.get("queued") == "1" and ui.get("sent") == "0"


def test_declarations_need_the_acknowledgment():
    from kestrel_pi.declarations import read_declarations

    decl = read_declarations(os.path.join(PLUGIN_DIR, "notify.py"))["notify"]
    caps = decl.get("capabilities") or decl.get("__capabilities__")
    assert "network" in caps
    secrets = [k for k, v in decl["options"].items() if v.get("secret")]
    assert set(secrets) == {"ntfy_topic", "ntfy_token", "telegram_token", "telegram_chat"}


def test_the_worker_sends_in_the_background_and_stops(tmp_path, monkeypatch, posts):
    import time

    p = _plugin(tmp_path, monkeypatch, worker=True)
    p._enqueue(N.message("test", "T", "B"))
    deadline = time.monotonic() + 5
    while not posts and time.monotonic() < deadline:
        time.sleep(0.05)
    assert len(posts) == 1
    p.on_unload()
    assert not p._worker.is_alive()
