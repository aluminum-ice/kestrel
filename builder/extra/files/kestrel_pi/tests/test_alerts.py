"""The alerts plugin library (``_lib_alerts.py``) and the ntfy and telegram
plugins that use it: digests, battery/capture/update alerts, the offline queue
and each service's request, with no real network (requests.post is faked) and
the state directory in tmp_path. The library's pure functions are tested once;
behaviour through a plugin is tested with both, since each wires its own hooks
and request to it. Tokens and topics here are made up at test time; names are
placeholders."""

import importlib.util
import json
import os
import stat
import types

import pytest

requests = pytest.importorskip("requests")

from kestrel_pi import load_library  # noqa: E402
from kestrel_pi.capture import CaptureInterface  # noqa: E402
from kestrel_pi.context import PluginContext, ServiceRegistry  # noqa: E402
from kestrel_pi.plugins import PluginPage, PluginRedirect  # noqa: E402
from kestrel_pi.ui import StubUI, UIState  # noqa: E402

PLUGIN_DIR = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", "..", "plugins"))


def _mod(name):
    spec = importlib.util.spec_from_file_location(
        name + "_under_test", os.path.join(PLUGIN_DIR, name + ".py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


NTFY = _mod("ntfy")
TELEGRAM = _mod("telegram")
# The library module both plugins share (load_library returns the same module
# for every caller of the same file). Patch library globals here, not on a plugin.
LIB = load_library(os.path.join(PLUGIN_DIR, "ntfy.py"), "alerts", api=1)
SHARED = {"events": "handshakes,battery,update,capture", "digest_minutes": 30, "include_names": False,
          "battery_percent": 20, "max_per_hour": 6, "quiet_hours": "", "timeout": 5}
SERVICES = {
    "ntfy": (NTFY, NTFY.Ntfy, {"server": "https://ntfy.example.com"}),
    "telegram": (TELEGRAM, TELEGRAM.Telegram, {"bot_token": "123:made-up", "chat_id": "42"}),
}


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


def _plugin(tmp_path, monkeypatch, service="ntfy", options=None, online=True, authed=True,
            worker=False):
    _, cls, defaults = SERVICES[service]
    monkeypatch.setattr(LIB, "state_dir", lambda name: str(tmp_path / "state"))
    if not worker:
        # No background worker: these tests drive every step by hand.
        monkeypatch.setattr(LIB.Alerts, "start", lambda self: None)
    p = cls()
    p.options = {**SHARED, **defaults, **(options or {})}
    p.context = PluginContext(
        name=service, options=p.options, capture=CaptureInterface(str(tmp_path)),
        connectivity=types.SimpleNamespace(online=online), services=ServiceRegistry(),
        capabilities=("network", "capture-read", "ui", "web"))
    p.context.web_authed = authed
    p.on_loaded()
    p.alerts.config_path = str(tmp_path / "config.toml")
    p.alerts.update_state = str(tmp_path / "update-state.json")
    return p


def _hs(essid, bssid="00:00:5E:00:53:01"):
    return types.SimpleNamespace(essid=essid, bssid=bssid, type="eapol", path="x")


both = pytest.mark.parametrize("service", sorted(SERVICES))


# -- messages ---------------------------------------------------------------------

def test_digest_is_counts_only_by_default():
    A = LIB
    msg = A.handshake_digest(12, {"My Network", "Cafe Guest", "Library WiFi"}, 30, False)
    assert msg["title"] == "Kestrel: 12 new handshakes"
    assert "12 new handshakes from 3 networks in the last 30 minutes." == msg["body"]
    assert "My Network" not in msg["body"]


def test_digest_names_only_when_asked_and_cleaned():
    A = LIB
    names = {"My Network", "Cafe\x07 Guest", ""} | {"Net %d" % i for i in range(6)}
    body = A.handshake_digest(9, names, 30, True)["body"]
    assert "Networks: " in body and "(+4 more)" in body
    assert "\x07" not in body


def test_quiet_hours():
    A = LIB
    assert A.in_quiet_hours("22-07", 23) and A.in_quiet_hours("22-07", 3)
    assert not A.in_quiet_hours("22-07", 12)
    assert A.in_quiet_hours("09-17", 10) and not A.in_quiet_hours("09-17", 18)
    assert not A.in_quiet_hours("", 3) and not A.in_quiet_hours("x", 3)


# -- events (shared) --------------------------------------------------------------

def test_handshakes_become_one_digest_without_addresses(tmp_path, monkeypatch, posts):
    p = _plugin(tmp_path, monkeypatch)
    for essid in ("My Network", "My Network", "Cafe Guest"):
        p.on_handshake(_hs(essid))
    p.alerts.flush_digest()                  # the window hasn't passed
    assert p.alerts.queue == []
    p.alerts.flush_digest(force=True)
    assert len(p.alerts.queue) == 1
    p.alerts.drain()
    url, kw = posts[0]
    assert url.startswith("https://ntfy.example.com/kestrel-")
    assert kw["data"] == b"3 new handshakes from 2 networks in the last 30 minutes."
    assert "00:00:5E" not in kw["data"].decode()
    assert kw["headers"]["Title"] == "Kestrel: 3 new handshakes"
    assert kw["allow_redirects"] is False and p.alerts.queue == []


@both
def test_battery_low_once_and_power_loss(tmp_path, monkeypatch, service):
    a = _plugin(tmp_path, monkeypatch, service).alerts
    a.check_battery({"percent": 80, "status": "charging"})
    a.check_battery({"percent": 79, "status": "discharging"})
    assert [m["title"] for m in a.queue] == ["Kestrel is on battery"]
    a.check_battery({"percent": 19, "status": "discharging"})
    a.check_battery({"percent": 18, "status": "discharging"})
    lows = [m for m in a.queue if m["title"] == "Kestrel battery low"]
    assert len(lows) == 1 and lows[0]["urgent"]
    a.check_battery({"percent": 40, "status": "charging"})
    a.check_battery({"percent": 19, "status": "discharging"})
    assert len([m for m in a.queue if m["title"] == "Kestrel battery low"]) == 2


@both
def test_capture_stop_alerts_only_while_switched_on(tmp_path, monkeypatch, service):
    a = _plugin(tmp_path, monkeypatch, service).alerts
    (tmp_path / "config.toml").write_text("[capture]\nenabled = true\n")
    a.check_capture(True)
    a.check_capture(False)                   # one blip: a relaunch, no alert
    a.check_capture(True)
    a.check_capture(False)
    a.check_capture(False)
    assert [m["title"] for m in a.queue] == ["Kestrel capture stopped"]
    a.check_capture(False)                   # still down: no repeat
    assert len(a.queue) == 1
    (tmp_path / "q").mkdir()
    q = _plugin(tmp_path / "q", monkeypatch, service).alerts
    (tmp_path / "q" / "config.toml").write_text("[capture]\nenabled = false\n")
    for active in (True, False, False):
        q.check_capture(active)
    assert q.queue == []                     # switched off on purpose


@both
def test_update_alert_once_per_release(tmp_path, monkeypatch, service):
    a = _plugin(tmp_path, monkeypatch, service).alerts
    (tmp_path / "update-state.json").write_text(json.dumps(
        {"update_available": True, "latest": "1.2.3"}))
    a.check_update()
    a.update_checked = 0
    a.check_update()
    assert [m["body"] for m in a.queue] == [
        "Release 1.2.3 is available. Open the dashboard to update."]
    again = _plugin(tmp_path, monkeypatch, service).alerts      # remembered across a restart
    again.check_update()
    assert again.queue[-1:] == a.queue[-1:] and len(again.queue) == 1


@both
def test_online_alert_is_opt_in(tmp_path, monkeypatch, service):
    p = _plugin(tmp_path, monkeypatch, service)
    p.on_internet_available()
    assert p.alerts.queue == []
    q = _plugin(tmp_path / "q", monkeypatch, service, options={"events": "online"})
    q.on_internet_available()
    q.on_internet_available()
    assert [m["title"] for m in q.alerts.queue] == ["Kestrel is online"]


# -- queue and sending (shared) ---------------------------------------------------

@both
def test_offline_alerts_wait_and_survive_a_restart(tmp_path, monkeypatch, posts, service):
    A = LIB
    p = _plugin(tmp_path, monkeypatch, service, online=False)
    p.alerts.enqueue(A.message("test", "T", "B"))
    p.alerts.drain()
    assert posts == [] and len(p.alerts.queue) == 1
    later = _plugin(tmp_path, monkeypatch, service, online=True)
    assert len(later.alerts.queue) == 1
    later.alerts.drain()
    assert len(posts) == 1 and later.alerts.queue == []


@both
def test_failures_keep_the_alert_and_back_off(tmp_path, monkeypatch, service):
    A = LIB
    monkeypatch.setattr(requests, "post", lambda *a, **k: _Resp(500))
    a = _plugin(tmp_path, monkeypatch, service).alerts
    a.enqueue(A.message("test", "T", "B"))
    a.drain()
    assert len(a.queue) == 1 and ("%s answered HTTP 500" % service) in a.last_error
    assert a.backoff_until > 0


def test_rate_limit_and_quiet_hours_but_urgent_gets_through(tmp_path, monkeypatch, posts):
    A = LIB
    a = _plugin(tmp_path, monkeypatch, options={"max_per_hour": 2,
                                                "quiet_hours": "0-23"}).alerts
    monkeypatch.setattr(A, "in_quiet_hours", lambda spec, hour: False)
    for i in range(4):
        a.enqueue(A.message("test", "T%d" % i, "B"))
    a.drain()
    assert len(posts) == 2 and len(a.queue) == 2
    monkeypatch.setattr(A, "in_quiet_hours", lambda spec, hour: True)
    a._sent_times = []
    a.enqueue(A.message("battery", "Kestrel battery low", "low", urgent=True))
    a.drain()
    assert posts[-1][1]["headers"]["Title"] == "Kestrel battery low"
    assert posts[-1][1]["headers"]["Priority"] == "high"
    assert len(a.queue) == 2                 # the quiet-hours ones still wait


@both
def test_the_worker_sends_in_the_background_and_stops(tmp_path, monkeypatch, posts, service):
    A = LIB
    import time

    p = _plugin(tmp_path, monkeypatch, service, worker=True)
    p.alerts.enqueue(A.message("test", "T", "B"))
    deadline = time.monotonic() + 5
    while not posts and time.monotonic() < deadline:
        time.sleep(0.05)
    assert len(posts) == 1
    p.on_unload()
    assert not p.alerts.worker.is_alive()


# -- ntfy -------------------------------------------------------------------------

def test_ntfy_needs_https_and_sends_the_token(tmp_path, monkeypatch, posts):
    A = LIB
    p = _plugin(tmp_path, monkeypatch, options={"server": "http://ntfy.example.com"})
    with pytest.raises(Exception):
        p.alerts.send(A.message("test", "T", "B"))
    assert posts == []
    q = _plugin(tmp_path / "q", monkeypatch, options={"token": "tk_made_up",
                                                      "topic": "my-topic"})
    q.alerts.send(A.message("test", "T", "B"))
    url, kw = posts[0]
    assert url == "https://ntfy.example.com/my-topic"
    assert kw["headers"]["Authorization"] == "Bearer tk_made_up"


def test_generated_topic_is_private_and_stable(tmp_path, monkeypatch):
    p = _plugin(tmp_path, monkeypatch)
    topic = p._topic()
    assert topic.startswith("kestrel-") and len(topic) > 24
    assert stat.S_IMODE(os.stat(tmp_path / "state" / "topic").st_mode) == 0o600
    assert p._topic() == topic


def test_ntfy_status_page_shows_only_a_generated_topic_and_only_signed_in(tmp_path, monkeypatch):
    p = _plugin(tmp_path, monkeypatch)
    page = p.on_webhook("index", types.SimpleNamespace(method="GET", args={}))
    assert isinstance(page, PluginPage) and p._topic() in page.body
    signed_out = _plugin(tmp_path, monkeypatch, authed=False)
    assert p._topic() not in signed_out.on_webhook(
        "index", types.SimpleNamespace(method="GET", args={})).body
    own = _plugin(tmp_path / "o", monkeypatch, options={"topic": "secret-topic-made-up"})
    body = own.on_webhook("index", types.SimpleNamespace(method="GET", args={})).body
    assert "secret-topic-made-up" not in body and "the one you set" in body


# -- telegram ---------------------------------------------------------------------

def test_telegram_request_shape(tmp_path, monkeypatch, posts):
    A = LIB
    p = _plugin(tmp_path, monkeypatch, "telegram")
    p.alerts.send(A.message("test", "Kestrel test", "It works."))
    url, kw = posts[0]
    assert url == "https://api.telegram.org/bot123:made-up/sendMessage"
    assert kw["json"] == {"chat_id": "42", "text": "Kestrel test\nIt works.",
                          "disable_web_page_preview": True}


def test_telegram_needs_both_settings(tmp_path, monkeypatch, posts):
    A = LIB
    p = _plugin(tmp_path, monkeypatch, "telegram", options={"chat_id": ""})
    with pytest.raises(Exception) as err:
        p.alerts.send(A.message("test", "T", "B"))
    assert "chat_id" in str(err.value) and posts == []


def test_telegram_errors_never_carry_the_token(tmp_path, monkeypatch):
    A = LIB
    token = "123456:made-up-" + "x" * 20

    def boom(url, **kw):
        raise requests.ConnectionError("Max retries exceeded with url: %s" % url)

    monkeypatch.setattr(requests, "post", boom)
    p = _plugin(tmp_path, monkeypatch, "telegram", options={"bot_token": token})
    with pytest.raises(Exception) as err:
        p.alerts.send(A.message("test", "T", "B"))
    assert token not in str(err.value) and "ConnectionError" in str(err.value)
    p.alerts.enqueue(A.message("test", "T", "B"))
    p.alerts.drain()
    assert token not in p.alerts.last_error


def test_telegram_status_page_never_shows_the_settings(tmp_path, monkeypatch):
    p = _plugin(tmp_path, monkeypatch, "telegram")
    body = p.on_webhook("index", types.SimpleNamespace(method="GET", args={})).body
    assert "123:made-up" not in body and "42" not in body.replace("&", "")
    assert "Bot token: set" in body and "Chat: set" in body


# -- pages, card and declarations ---------------------------------------------------

@both
def test_send_test_button_reports_the_result(tmp_path, monkeypatch, posts, service):
    p = _plugin(tmp_path, monkeypatch, service)
    ok = p.on_webhook("test", types.SimpleNamespace(method="POST", args={}))
    assert isinstance(ok, PluginRedirect) and "sent" in ok.message
    monkeypatch.setattr(requests, "post", lambda *a, **k: _Resp(403))
    bad = p.on_webhook("test", types.SimpleNamespace(method="POST", args={}))
    assert bad.error and "HTTP 403" in bad.message


@both
def test_status_page_links_back_with_the_token(tmp_path, monkeypatch, service):
    p = _plugin(tmp_path, monkeypatch, service)
    body = p.on_webhook("index", types.SimpleNamespace(method="GET", args={"token": "abc"})).body
    assert '<a href="/plugins?token=abc">&larr; Plugins</a>' in body
    assert '<a href="/?token=abc">Dashboard</a>' in body


@both
def test_dashboard_card(tmp_path, monkeypatch, service):
    A = LIB
    p = _plugin(tmp_path, monkeypatch, service)
    ui = StubUI().for_plugin(service)
    p.on_ui_setup(ui)
    p.alerts.enqueue(A.message("test", "T", "B"))
    p.on_ui_update(ui, UIState())
    assert ui.get("status") == "ready"
    assert ui.get("queued") == "1" and ui.get("sent") == "0"


@pytest.mark.parametrize("service,secrets", [("ntfy", {"topic", "token"}),
                                             ("telegram", {"bot_token", "chat_id"})])
def test_declarations_need_the_acknowledgment_and_carry_only_their_service(service, secrets):
    from kestrel_pi.declarations import read_declarations

    decl = read_declarations(os.path.join(PLUGIN_DIR, service + ".py"))[service]
    caps = decl.get("capabilities") or decl.get("__capabilities__")
    assert "network" in caps
    assert {k for k, v in decl["options"].items() if v.get("secret")} == secrets
    other = "telegram" if service == "ntfy" else "ntfy"
    assert not any(other in k for k in decl["options"])


@pytest.mark.parametrize("service", sorted(SERVICES))
def test_each_plugin_uses_the_shared_alerts_library(service):
    # The alert code lives once, in the plugin library beside the plugins -
    # never in core and never as a copy in a plugin.
    mod = SERVICES[service][0]
    assert mod.alerts is LIB
    src = open(os.path.join(PLUGIN_DIR, service + ".py"), encoding="utf-8").read()
    assert 'load_library(__file__, "alerts", api=1)' in src
    assert "class Alerts" not in src and "def handshake_digest" not in src
    assert not os.path.exists(os.path.join(PLUGIN_DIR, "..", "kestrel_pi", "kestrel_pi",
                                           "alerts.py"))


@both
def test_state_written_before_the_library_still_loads(tmp_path, monkeypatch, service):
    # An updated device keeps its queued alerts and what it has already
    # announced: the files and their format are those the per-plugin copies wrote.
    state = tmp_path / "state"
    state.mkdir()
    queued = {"kind": "battery", "title": "Kestrel battery low", "body": "The battery is at 9%.",
              "urgent": True, "ts": 1700000000}
    (state / "queue.json").write_text(json.dumps([queued]))
    (state / "seen.json").write_text(json.dumps({"update": "1.2.3"}))
    a = _plugin(tmp_path, monkeypatch, service).alerts
    assert a.queue == [queued]
    (tmp_path / "update-state.json").write_text(json.dumps(
        {"update_available": True, "latest": "1.2.3"}))
    a.check_update()
    assert a.queue == [queued]                       # 1.2.3 was already announced


def test_the_documented_example_plugin_works(tmp_path, monkeypatch, posts):
    # docs/developer/plugin-libraries.md shows a minimal plugin on the alerts
    # library; it must load through the real loader and send.
    import logging
    import re

    from kestrel_pi.connectivity import ConnectivityState
    from kestrel_pi.loader import Loader

    doc = os.path.join(PLUGIN_DIR, "..", "..", "..", "..", "docs", "developer",
                       "plugin-libraries.md")
    text = open(doc, encoding="utf-8").read().split("## `alerts`", 1)[1]
    code = re.search(r"### A minimal plugin\n\n```python\n(.*?)```", text, re.S).group(1)
    plugins = tmp_path / "plugins"
    plugins.mkdir()
    (plugins / "webhook.py").write_text(code)
    # As for an operator's plugin: no copy beside it, so it gets the shipped
    # library - the same module as LIB, so the patches below apply.
    from kestrel_pi import plugins as plugin_api
    monkeypatch.setattr(plugin_api, "SHIPPED_PLUGINS_DIR", PLUGIN_DIR)
    monkeypatch.setattr(LIB, "state_dir", lambda name: str(tmp_path / "state"))
    monkeypatch.setattr(LIB.Alerts, "start", lambda self: None)
    loader = Loader(config={"plugins": {"webhook": {"enabled": True,
                                                    "url": "https://hooks.example.com/x"}}},
                    capture=CaptureInterface(str(tmp_path)), connectivity=ConnectivityState(),
                    services=ServiceRegistry(), search_paths=[str(plugins)],
                    logger=logging.getLogger("test.alerts.doc"))
    loader.load_all()
    record = loader.records["webhook"]
    assert record.state == "loaded", getattr(record, "reason", "")
    assert record.instance.alerts.dir == str(tmp_path / "state")
    reply = record.instance.on_webhook("test", types.SimpleNamespace(method="POST", args={}))
    assert isinstance(reply, PluginRedirect) and not reply.error, reply.message
    assert posts[0][0] == "https://hooks.example.com/x"
    assert posts[0][1]["json"]["title"] == "Kestrel test"
