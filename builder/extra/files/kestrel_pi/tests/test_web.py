"""Status-page rendering and the management-interface bind/rebind.

These exercise the two pieces that have no Flask/werkzeug dependency: the pure
``_render_status`` HTML builder (whose inline CSS once 500'd the page by going
through ``str.format``) and ``WebServer`` with an injected server factory (so
the bind/rebind logic is tested without opening real sockets)."""

import logging
import threading
import time
import types

import pytest

from kestrel_pi import web

LOG = logging.getLogger("test-web")


def _status(plugins):
    return {
        "version": "0.3.0",
        "epoch": 2,
        "connectivity": {"online": False, "paths": ["wlan0"]},
        "capture": {"store": "/var/lib/kestrel/capture", "hashline_files": 47},
        "plugins": plugins,
    }


# -- _render_environment: the "what's in range" card ---------------------
def test_environment_unknown_when_capture_off():
    html = web._render_environment(None)
    assert "Environment" in html
    assert "Unknown" in html and "enable capture to scan" in html
    # no misleading "0" and no density bar in this state
    assert 'class="bar"' not in html


def test_environment_scanning_before_first_reading():
    html = web._render_environment({"available": False})
    assert "Scanning the area" in html
    # A real ellipsis, not an HTML entity: _srow escapes its meta, so a literal
    # "&hellip;" would double-escape to "&amp;hellip;" and show as raw text.
    assert "…" in html
    assert "hellip" not in html
    assert 'class="bar"' not in html


def test_environment_available_shows_density_count_bar_and_yield():
    env = {"available": True, "label": "Moderate", "radios_recent": 23,
           "window_s": 300, "level": 2, "bar_pct": 38, "data_age_s": 12,
           "caught_session": 5, "handshakes_session": 8}
    html = web._render_environment(env)
    assert "Moderate" in html and "23 radios nearby" in html
    # fresh data (age <= 90s) reads "in the last <window>", not "as of N ago"
    assert "in the last 5 min" in html and "as of" not in html
    assert 'class="bar"' in html and "width:38%" in html
    # the session yield is a plain count, never "caught X of ~Y", and spelled
    # out so the two numbers are unambiguous
    assert "Caught 5 APs and 8 handshakes" in html
    assert "of ~" not in html


def test_environment_stale_data_reads_as_of_n_ago():
    env = {"available": True, "label": "Quiet", "radios_recent": 2,
           "window_s": 300, "level": 0, "bar_pct": 3, "data_age_s": 300,
           "caught_session": 0, "handshakes_session": 0}
    html = web._render_environment(env)
    assert "2 radios nearby" in html
    assert "as of 5 min ago" in html


def test_environment_singular_radio_and_handshake():
    env = {"available": True, "label": "Light", "radios_recent": 1,
           "window_s": 300, "level": 1, "bar_pct": 3, "data_age_s": 5,
           "caught_session": 1, "handshakes_session": 1}
    html = web._render_environment(env)
    assert "1 radio nearby" in html          # not "radios"
    # the yield pluralizes both nouns independently
    assert "Caught 1 AP and 1 handshake" in html
    assert "1 APs" not in html and "1 handshakes" not in html


def test_environment_card_is_on_the_dashboard():
    status = _status([])
    status["environment"] = {"available": True, "label": "Moderate",
                             "radios_recent": 12, "window_s": 300,
                             "level": 2, "bar_pct": 20, "data_age_s": 8,
                             "caught_session": 2, "handshakes_session": 2}
    html = web._render_status(status)
    assert "12 radios nearby" in html


# -- _render_status: the dashboard ---------------------------------------
def test_status_page_renders_with_css_and_no_plugins():
    html = web._render_status(_status([]))
    assert "<!doctype html>" in html
    # The inline CSS carries literal braces; it must survive rendering. This is
    # exactly the case that used to raise KeyError: 'font-family' and 500 the page.
    assert "font-family:system-ui" in html and "sans-serif" in html
    # The dashboard shows the status card and a link to the plugins page; the
    # plugin table itself lives on /plugins now, not here.
    assert 'class="card"' in html and 'class="srow"' in html
    assert 'class="navcard"' in html and 'href="/plugins"' in html
    assert "no plugins loaded" not in html
    # Mobile: viewport, a dark palette, and the responsive @media block present.
    assert "width=device-width" in html
    assert "prefers-color-scheme:dark" in html
    assert "@media(max-width:40em)" in html


def test_dashboard_labels_the_plugin_host_not_daemon():
    # The version pill used to read a cryptic "daemon"; it now says what it is.
    html = web._render_status(_status([]))
    assert "plugin host" in html
    assert "<b>daemon</b>" not in html


def test_dashboard_shows_internet_and_capture_rows():
    html = web._render_status(dict(
        _status([]),
        connectivity={"online": True, "paths": ["bnep0", "wlan0"]},
        capture={"store": "/x", "hashline_files": 3, "state": "running"},
    ))
    assert "Internet" in html and "Online" in html and "bnep0, wlan0" in html
    assert "Capture" in html and "Running" in html and "3 capture file(s)" in html


def test_dashboard_shows_management_addresses():
    # The address disclosure lists each management interface's IP (or "not
    # connected"); it never lists the capture dongle (not passed here).
    html = web._render_status(
        _status([]), addresses=[("bnep0", "172.26.0.1"), ("wlan0", None)])
    assert 'class="addrs"' in html and "Network addresses" in html
    assert "bnep0" in html and "172.26.0.1" in html
    assert "wlan0" in html and "not connected" in html


def test_dashboard_omits_address_disclosure_when_not_provided():
    assert 'class="addrs"' not in web._render_status(_status([]))


def test_management_addresses_maps_each_interface(monkeypatch):
    ips = {"bnep0": "172.26.0.1", "wlan0": None}
    monkeypatch.setattr(web, "iface_ipv4", lambda iface: ips.get(iface))
    assert web.management_addresses(interfaces=("bnep0", "wlan0")) == [
        ("bnep0", "172.26.0.1"), ("wlan0", None)]


def test_option_field_renders_help_and_default_hints():
    # An option spec with a "help" string shows a hint about valid entries, and
    # the default is still shown; both guide the operator on the settings page.
    spec = {"type": "str", "default": "RecurrentPPO,PPO,DQN,QR-DQN",
            "help": "Comma-separated, case-sensitive. Any of: "
                    "RecurrentPPO, PPO, DQN, QR-DQN. Empty trains nothing."}
    html = web._option_field("algorithms", spec, {}, set(), False)
    assert "Comma-separated, case-sensitive" in html
    assert "default: RecurrentPPO,PPO,DQN,QR-DQN" in html


def test_dashboard_nav_card_counts_enabled_plugins():
    control = {"ready": True, "csrf": "t", "qs": "",
               "enabled": {"gps": True, "wigle": False}, "acked": True}
    html = web._render_status(_status([
        {"name": "gps", "state": "loaded", "target_api": 1, "capabilities": [],
         "capability_violations": [], "reason": "", "note": "", "last_error": ""},
        {"name": "wigle", "state": "disabled", "target_api": 1, "capabilities": [],
         "capability_violations": [], "reason": "", "note": "", "last_error": ""},
    ]), control)
    assert "1 of 2 enabled" in html


def _ctl():
    return {"ready": True, "csrf": "t", "qs": "", "enabled": {}, "acked": True}


def test_bt_pair_control_shows_when_no_phone_is_paired():
    # No bluetooth key (or paired False) => unpaired => the discreet re-pair
    # control shows and posts to /bt-pair.
    html = web._render_status(_status([]), _ctl())
    assert 'class="btpair"' in html and 'action="/bt-pair"' in html
    assert "pair over Bluetooth" in html


def test_bt_pair_control_hidden_when_a_phone_is_paired():
    status = dict(_status([]), bluetooth={"paired": True})
    html = web._render_status(status, _ctl())
    assert 'class="btpair"' not in html and "/bt-pair" not in html


def test_bt_pair_control_hidden_when_controls_are_off():
    # Read-only dashboard (no control / auth off): no actionable re-pair button.
    assert 'class="btpair"' not in web._render_status(_status([]))
    off = {"ready": False, "csrf": "t", "qs": "", "enabled": {}}
    assert 'class="btpair"' not in web._render_status(_status([]), off)


# -- the AI Agent nav card and /agent page -------------------------------
def _with_agent(agent=None, widgets=None):
    s = _status([])
    s["agent"] = agent or {"enabled": False, "algorithm": "RecurrentPPO",
                           "policy": "baseline", "service": "inactive",
                           "status": None}
    if widgets is not None:
        s["widgets"] = widgets
    return s


def test_dashboard_has_ai_agent_nav_card_not_an_inline_form():
    html = web._render_status(_with_agent(), _ctl())
    # A compact nav card linking to /agent, like the Plugins card - not the old
    # inline control form on the dashboard.
    assert 'href="/agent"' in html and "AI Agent" in html
    assert 'class="agentform"' not in html
    # Disabled agent reads "Off".
    assert "Off" in html


def test_ai_agent_nav_card_summarizes_enabled_state():
    s = _with_agent({"enabled": True, "algorithm": "DQN", "policy": "learned",
                     "service": "active", "status": None})
    html = web._render_status(s, _ctl())
    assert "AI Agent" in html and "On" in html and "learned" in html and "DQN" in html


def test_ai_agent_nav_card_shows_training():
    s = _with_agent({"enabled": True, "algorithm": "PPO", "policy": "learned",
                     "service": "active", "status": {"training": True}})
    html = web._render_status(s, _ctl())
    assert "training" in html.lower()


def test_dashboard_grid_omits_the_agent_readout_card():
    # The agent's own readout card is in status["widgets"] but must not appear in
    # the dashboard widget grid (it lives on /agent now).
    s = _with_agent(widgets={web.AGENT_CARD_NAME: [
        {"name": "state", "label": "State", "value": "running", "kind": "text"}]})
    html = web._render_status(s, _ctl())
    assert 'data-plugin="' + web.AGENT_CARD_NAME + '"' not in html


def test_agent_page_renders_policy_radios_and_hides_algorithm_under_baseline():
    html = web._render_agent(_with_agent(), _ctl())
    assert "<h1>AI agent</h1>" in html
    # Policy is a radio group (two choices), not a dropdown.
    assert 'type="radio"' in html and 'name="policy"' in html
    assert 'value="baseline"' in html and 'value="learned"' in html
    # Algorithm field present but hidden by default under baseline; a tiny script
    # reveals it for "learned".
    assert 'id="agent-algo"' in html and " hidden>" in html
    assert 'name="algorithm"' in html
    assert "addEventListener('change'" in html


def test_agent_page_shows_algorithm_when_policy_is_learned():
    s = _with_agent({"enabled": True, "algorithm": "PPO", "policy": "learned",
                     "service": "active", "status": None})
    html = web._render_agent(s, _ctl())
    # The algorithm wrapper is rendered without the hidden attribute when the
    # current policy is learned (baseline renders it with ` hidden`).
    assert 'class="agent-algo" id="agent-algo">' in html
    assert 'class="agent-algo" id="agent-algo" hidden>' not in html


def test_agent_page_is_read_only_without_web_auth():
    off = {"ready": False, "csrf": "t", "qs": "", "enabled": {}}
    html = web._render_agent(_with_agent(), off)
    assert 'class="agentform"' not in html
    assert "read-only" in html.lower() or "log in" in html.lower()


def test_agent_page_shows_the_readout_card():
    s = _with_agent(widgets={web.AGENT_CARD_NAME: [
        {"name": "state", "label": "State", "value": "running", "kind": "text"}]})
    html = web._render_agent(s, _ctl())
    assert web.AGENT_CARD_NAME in html and "running" in html


def test_agent_page_auto_refreshes_while_the_agent_is_starting():
    # Just enabled: enabled in config but the agent process hasn't published a
    # readout yet. The page shows a "starting" note and refreshes itself so it
    # flips to the dashboard once the readout lands - no manual navigation.
    s = _with_agent({"enabled": True, "algorithm": "DQN", "policy": "learned",
                     "service": "activating", "status": None})
    html = web._render_agent(s, _ctl())
    assert "starting up" in html
    assert "location.reload()" in html


def test_agent_page_with_a_readout_does_not_auto_refresh():
    # Once the readout is present the page is static (it carries the control
    # form), so it must not reload under the user.
    s = _with_agent({"enabled": True, "algorithm": "DQN", "policy": "learned",
                     "service": "active", "status": {"state": "running"}},
                    widgets={web.AGENT_CARD_NAME: [
                        {"name": "state", "label": "State", "value": "running",
                         "kind": "text"}]})
    html = web._render_agent(s, _ctl())
    assert "location.reload()" not in html


def test_agent_page_disabled_without_a_readout_does_not_auto_refresh():
    # Disabled and never run: the "enable it below" prompt, no auto-refresh.
    html = web._render_agent(_with_agent(), _ctl())
    assert "has not run yet" in html
    assert "location.reload()" not in html


def test_agent_page_shows_a_clickable_train_button_when_ready():
    s = _with_agent({"enabled": True, "algorithm": "DQN", "policy": "learned",
                     "service": "active",
                     "status": {"training": False, "can_train": True,
                                "train_blocked": ""}})
    html = web._render_agent(s, _ctl())
    assert 'value="train"' in html and "Train now" in html
    assert "disabled>Train now" not in html        # enabled, clickable


def test_agent_page_train_button_disabled_with_reason_when_not_ready():
    s = _with_agent({"enabled": True, "algorithm": "DQN", "policy": "learned",
                     "service": "active",
                     "status": {"training": False, "can_train": False,
                                "train_blocked": "no episode data yet"}})
    html = web._render_agent(s, _ctl())
    assert "disabled>Train now" in html
    assert "no episode data yet" in html


def test_agent_page_train_button_shows_in_progress_while_training():
    s = _with_agent({"enabled": True, "algorithm": "DQN", "policy": "learned",
                     "service": "active",
                     "status": {"training": True, "can_train": False,
                                "train_blocked": "a training run is already in progress"}})
    html = web._render_agent(s, _ctl())
    assert 'value="train"' in html and "Training" in html and "disabled" in html


def test_agent_page_has_no_train_button_when_the_agent_is_off():
    # The agent process only runs while enabled, so there's nothing to pick up a
    # train request; don't offer the button.
    s = _with_agent({"enabled": False, "algorithm": "DQN", "policy": "baseline",
                     "service": "inactive", "status": {"can_train": True}})
    html = web._render_agent(s, _ctl())
    assert 'value="train"' not in html


def test_agent_model_slug_matches_the_package():
    # The /agent page derives the per-algorithm model filename itself (it does
    # not import the agent package); keep that slug in step with the package.
    from kestrel_pi.agent import model_slug
    for algo in ("RecurrentPPO", "PPO", "DQN", "QR-DQN"):
        assert web._agent_model_slug(algo) == model_slug(algo)


def test_agent_has_model_sees_per_algorithm_and_legacy(tmp_path):
    d = str(tmp_path)
    assert web._agent_has_model("DQN", d) is False
    (tmp_path / ("model-" + web._agent_model_slug("DQN") + ".zip")).write_bytes(b"x")
    assert web._agent_has_model("DQN", d) is True
    assert web._agent_has_model("PPO", d) is False
    # A legacy single model counts for the algorithm its sidecar names.
    (tmp_path / "model.zip").write_bytes(b"x")
    (tmp_path / "model.meta.json").write_text('{"algorithm": "PPO"}')
    assert web._agent_has_model("PPO", d) is True
    assert web._agent_has_model("QR-DQN", d) is False


def test_agent_train_confirm_page_warns_and_carries_the_choice():
    html = web._render_agent_train_confirm(_ctl(), "DQN", "learned", "enable")
    assert "Train a model for DQN" in html
    assert 'name="train_ack" value="yes"' in html
    assert 'name="algorithm" value="DQN"' in html
    assert 'name="policy" value="learned"' in html
    assert 'name="action" value="enable"' in html


def test_post_agent_switch_to_untrained_algorithm_confirms_first(monkeypatch, tmp_path):
    # Switching to an algorithm with no trained model shows a confirm page
    # (training will begin) and applies nothing until the user accepts.
    monkeypatch.setattr(web, "_agent_has_model", lambda algo, *a, **k: False)
    client, sent = _client(monkeypatch, tmp_path, BASIC)
    token = _csrf(client.get("/manage/wpa-sec",
                             headers=_basic()).get_data(as_text=True))
    resp = client.post("/agent",
                       data={"csrf": token, "action": "save",
                             "algorithm": "DQN", "policy": "learned"},
                       headers=_basic())
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "Train a model for DQN" in body
    assert 'name="train_ack" value="yes"' in body
    assert sent == []                                  # nothing applied yet

    resp2 = client.post("/agent",
                        data={"csrf": token, "action": "save", "algorithm": "DQN",
                              "policy": "learned", "train_ack": "yes"},
                        headers=_basic())
    assert resp2.status_code == 200
    assert sent == [{"target": "agent", "algorithm": "DQN", "policy": "learned"}]


def test_post_agent_switch_to_trained_algorithm_skips_confirm(monkeypatch, tmp_path):
    # Switching to an algorithm that already has a model loads it: no confirm,
    # no training.
    monkeypatch.setattr(web, "_agent_has_model", lambda algo, *a, **k: True)
    client, sent = _client(monkeypatch, tmp_path, BASIC)
    token = _csrf(client.get("/manage/wpa-sec",
                             headers=_basic()).get_data(as_text=True))
    resp = client.post("/agent",
                       data={"csrf": token, "action": "save",
                             "algorithm": "PPO", "policy": "learned"},
                       headers=_basic())
    assert resp.status_code == 200
    assert sent == [{"target": "agent", "algorithm": "PPO", "policy": "learned"}]


def test_post_agent_train_requests_only_the_train_action(monkeypatch, tmp_path):
    # A "Train now" click posts the same form (which also carries algorithm and
    # policy), but it must apply only the train action - never rewrite the
    # policy/algorithm the user is looking at.
    client, sent = _client(monkeypatch, tmp_path, BASIC,
                           replies=[{"ok": True, "changed": ["train"],
                                     "restart": False}])
    token = _csrf(client.get("/manage/wpa-sec",
                             headers=_basic()).get_data(as_text=True))
    resp = client.post("/agent",
                       data={"csrf": token, "action": "train",
                             "algorithm": "DQN", "policy": "learned"},
                       headers=_basic())
    assert resp.status_code == 200
    assert sent == [{"target": "agent", "action": "train"}]


def test_agent_page_renders_on_device_training_radio_default_on():
    html = web._render_agent(_with_agent(), _ctl())
    # A dedicated On/Off control for on-device training, its own Save action.
    assert 'name="train_mode"' in html
    assert 'value="on"' in html and 'value="off"' in html
    assert 'name="action" value="train_mode"' in html
    # No configured train => auto => the On radio is checked by default.
    assert 'value="on" checked' in html
    assert 'value="off" checked' not in html


def test_agent_page_training_radio_reflects_off():
    s = _with_agent({"enabled": True, "algorithm": "RecurrentPPO",
                     "policy": "baseline", "train": "off", "service": "active",
                     "status": None})
    html = web._render_agent(s, _ctl())
    assert 'value="off" checked' in html
    assert 'value="on" checked' not in html


def test_post_agent_training_off_writes_only_the_train_key(monkeypatch, tmp_path):
    # The On-device training radio writes [agent] train via the validated
    # config-set apply; "off" => never train. It touches nothing else.
    client, sent = _client(monkeypatch, tmp_path, BASIC,
                           replies=[{"ok": True, "changed": ["train"],
                                     "restart": True,
                                     "restart_hint": "the agent is restarting."}])
    token = _csrf(client.get("/manage/wpa-sec",
                             headers=_basic()).get_data(as_text=True))
    resp = client.post("/agent",
                       data={"csrf": token, "action": "train_mode",
                             "train_mode": "off"},
                       headers=_basic())
    assert resp.status_code == 200
    assert sent == [{"target": "config-set", "table": "agent",
                     "key": "train", "value": "off"}]


def test_post_agent_training_on_maps_to_auto(monkeypatch, tmp_path):
    # "On" maps to the board-aware default ("auto"), not a forced "idle".
    client, sent = _client(monkeypatch, tmp_path, BASIC,
                           replies=[{"ok": True, "changed": ["train"],
                                     "restart": True}])
    token = _csrf(client.get("/manage/wpa-sec",
                             headers=_basic()).get_data(as_text=True))
    resp = client.post("/agent",
                       data={"csrf": token, "action": "train_mode",
                             "train_mode": "on"},
                       headers=_basic())
    assert resp.status_code == 200
    assert sent == [{"target": "config-set", "table": "agent",
                     "key": "train", "value": "auto"}]


# -- the Whitelist nav card and /whitelist page --------------------------
def test_dashboard_has_whitelist_nav_below_captured_networks():
    s = _status([])
    s["capture"] = dict(s["capture"], whitelist=3)
    html = web._render_status(s, _ctl())
    assert 'href="/whitelist"' in html and "Whitelist" in html
    assert "3 protected AP(s)" in html
    # It sits directly below the Captured networks card.
    assert html.index('href="/networks"') < html.index('href="/whitelist"')


def test_whitelist_nav_reads_none_yet_when_empty():
    html = web._render_status(_status([]), _ctl())   # no whitelist key -> 0
    assert "Whitelist" in html and "none yet" in html


def test_whitelist_page_lists_entries_with_add_and_search():
    html = web._render_whitelist(["HomeWiFi", "AA:BB:CC:DD:EE:FF"], _ctl())
    assert "<h1>Whitelist</h1>" in html
    assert "HomeWiFi" in html and "AA:BB:CC:DD:EE:FF" in html
    # type column distinguishes an ESSID from a BSSID (MAC).
    assert "ESSID" in html and "BSSID" in html
    # an add form (POST, CSRF), a search box, and per-row remove buttons.
    assert 'class="wladd"' in html and 'name="entry"' in html
    assert 'action="/whitelist"' in html and 'name="csrf"' in html
    assert 'class="search"' in html
    assert "Remove" in html


def test_whitelist_page_is_read_only_without_auth():
    off = {"ready": False, "csrf": "t", "qs": "", "enabled": {}}
    html = web._render_whitelist(["HomeWiFi"], off)
    assert 'class="wladd"' not in html and "Remove" not in html
    assert "read-only" in html.lower() or "log in" in html.lower()


def test_whitelist_page_search_filters_entries():
    html = web._render_whitelist(["HomeWiFi", "CoffeeShop"], _ctl(), query="coffee")
    assert "CoffeeShop" in html and "HomeWiFi" not in html


# -- _render_plugins: the plugins page -----------------------------------
def test_plugins_page_renders_loaded_and_disabled_plugins():
    html = web._render_plugins(_status([
        {"name": "gps", "state": "loaded", "target_api": 1,
         "capabilities": ["location", "capture-read"], "capability_violations": [],
         "reason": "", "note": "", "last_error": ""},
        {"name": "wigle", "state": "disabled", "target_api": 1,
         "capabilities": ["network"], "capability_violations": [],
         "reason": "bad options: required option 'api_name' is not set",
         "note": "", "last_error": ""},
    ]))
    assert "gps" in html and "loaded" in html
    assert "wigle" in html and "api_name" in html
    # It links back to the dashboard.
    assert 'href="/"' in html


def test_plugins_page_is_mobile_friendly():
    # The table cells carry data-labels for the card reflow, and the header is in
    # a thead that the mobile CSS hides.
    html = web._render_plugins(_status([
        {"name": "gps", "state": "loaded", "target_api": 1,
         "capabilities": ["location"], "capability_violations": [],
         "reason": "", "note": "", "last_error": ""},
    ]))
    assert "<thead>" in html and "<tbody>" in html
    for label in ("api", "capabilities", "note"):
        assert ('data-label="%s"' % label) in html


def test_plugins_page_surfaces_health_note_and_violations():
    html = web._render_plugins(_status([
        {"name": "wigle", "state": "loaded", "target_api": 1,
         "capabilities": ["network"], "capability_violations": ["gps"],
         "reason": "", "note": "idle: no gps and geolocate=false", "last_error": ""},
    ]))
    assert "idle: no gps" in html
    assert "undeclared: gps" in html


def test_plugins_page_escapes_html():
    html = web._render_plugins(_status([
        {"name": "x", "state": "loaded", "target_api": 1, "capabilities": [],
         "capability_violations": [], "reason": "<script>", "note": "",
         "last_error": ""},
    ]))
    assert "<script>" not in html
    assert "&lt;script&gt;" in html


# -- resolve_bind_hosts --------------------------------------------------
def test_resolve_bind_hosts_management(monkeypatch):
    ips = {"bnep0": None, "wlan0": "192.168.1.50"}
    monkeypatch.setattr(web, "iface_ipv4", lambda iface: ips.get(iface))
    hosts = web.resolve_bind_hosts("management", interfaces=("bnep0", "wlan0"))
    assert hosts == ["127.0.0.1", "192.168.1.50"]   # loopback + the addressed iface


def test_resolve_bind_hosts_loopback_and_explicit(monkeypatch):
    monkeypatch.setattr(web, "iface_ipv4", lambda iface: "10.0.0.1")
    assert web.resolve_bind_hosts("loopback") == ["127.0.0.1"]
    assert web.resolve_bind_hosts("10.1.2.3") == ["127.0.0.1", "10.1.2.3"]


# -- WebServer reconcile (injected factory, no real sockets) -------------
class _FakeServer:
    def __init__(self, host, port, app):
        self.host = host
        self.port = port
        self._stop = threading.Event()
        self.shut = False

    def serve_forever(self):
        self._stop.wait()

    def shutdown(self):
        self.shut = True
        self._stop.set()


def _factory(created):
    def make(host, port, app):
        srv = _FakeServer(host, port, app)
        created.append(srv)
        return srv
    return make


def test_webserver_reconcile_binds_new_and_releases_gone():
    created = []
    ws = web.WebServer(app=object(), hosts=["127.0.0.1"], port=8080,
                       logger=LOG, server_factory=_factory(created))
    assert ws.start() is True
    assert ws.bound_hosts() == ["127.0.0.1"]

    # wlan0 gets its address after startup: its IP is now bound too.
    ws.reconcile(["127.0.0.1", "192.168.1.50"])
    assert ws.bound_hosts() == ["127.0.0.1", "192.168.1.50"]

    # Reconciling an unchanged set makes no new servers.
    before = len(created)
    ws.reconcile(["127.0.0.1", "192.168.1.50"])
    assert len(created) == before

    # wlan0 drops: one missed reconcile keeps it (a single failed address
    # lookup must not drop the page); the second in a row releases it.
    ws.reconcile(["127.0.0.1"])
    assert ws.bound_hosts() == ["127.0.0.1", "192.168.1.50"]
    ws.reconcile(["127.0.0.1"])
    assert ws.bound_hosts() == ["127.0.0.1"]
    assert any(s.host == "192.168.1.50" and s.shut for s in created)

    ws.stop()
    assert ws.bound_hosts() == []
    assert all(s.shut for s in created)


def test_controls_off_shows_notice_not_buttons():
    control = {"ready": False, "csrf": "t", "qs": "", "enabled": {"gps": True},
               "acked": False}
    plug = [{"name": "gps", "state": "loaded", "target_api": 1, "capabilities": [],
             "capability_violations": [], "reason": "", "note": "", "last_error": ""}]
    # The dashboard shows the read-only notice and no control buttons.
    dash = web._render_status(_status(plug), control)
    assert "Plugin controls are off" in dash
    assert "<button" not in dash
    # The plugins page shows the same notice, a settings link, and no buttons.
    plugins = web._render_plugins(_status(plug), control)
    assert "Plugin controls are off" in plugins
    assert "<button" not in plugins
    assert 'href="/manage/gps"' in plugins


def test_enabled_plugin_shows_a_red_disable_button():
    on = {"ready": True, "csrf": "t", "qs": "", "enabled": {"gps": True},
          "acked": True}
    html = web._render_plugins(_status([
        {"name": "gps", "state": "loaded", "target_api": 1, "capabilities": [],
         "capability_violations": [], "reason": "", "note": "", "last_error": ""},
    ]), on)
    assert '<button type="submit" class="danger">Disable</button>' in html
    assert "button.danger{" in html   # the red styling is defined

    off = {"ready": True, "csrf": "t", "qs": "", "enabled": {"gps": False},
           "acked": True}
    html = web._render_plugins(_status([
        {"name": "gps", "state": "disabled", "target_api": 1, "capabilities": [],
         "capability_violations": [], "reason": "", "note": "", "last_error": ""},
    ]), off)
    assert '<button type="submit">Enable</button>' in html   # default (green)
    assert 'class="danger"' not in html


# -- plugin controls through the Flask app (skipped without flask) ------------
class _FakeWpaSec:
    __description__ = "Upload hashlines to wpa-sec (opt-in)."
    __options__ = {
        "api_key": {"type": "str", "required": True, "secret": True},
        "endpoint": {"type": "str", "default": "https://wpa-sec.stanev.org"},
        "download_results": {"type": "bool", "default": False},
        "timeout": {"type": "int", "default": 30},
    }


class _FakeLoader:
    def __init__(self, records):
        self.records = records


class _FakeCapture:
    def __init__(self, networks):
        self._networks = networks

    def captured_networks(self):
        return self._networks


class _FakeDaemon:
    def __init__(self, web_cfg, plugin_table, networks=None):
        import logging
        from kestrel_pi.loader import PluginRecord

        self.log = logging.getLogger("test.daemon")
        rec = PluginRecord("wpa-sec", "/x/wpa-sec.py")
        rec.cls = _FakeWpaSec
        rec.capabilities = ("capture-read", "network")
        rec.state, rec.reason = "disabled", "not enabled in config"
        self.loader = _FakeLoader({"wpa-sec": rec})
        self.config = {"web": web_cfg, "plugins": {"wpa-sec": plugin_table}}
        self.capture = _FakeCapture(networks or [])

    def _fresh_config(self):
        return self.config

    def status(self):
        return dict(_status([r.health() for r in self.loader.records.values()]),
                    started=1.0)


BASIC = {"auth": "basic", "username": "pi", "password": "hunter2"}


def _basic(user="pi", password="hunter2"):
    import base64

    raw = base64.b64encode(("%s:%s" % (user, password)).encode()).decode()
    return {"Authorization": "Basic " + raw}


def _client(monkeypatch, tmp_path, web_cfg, table=None, replies=None, networks=None):
    pytest.importorskip("flask")
    sent = []

    def fake_apply(change):
        sent.append(change)
        return (replies or [{"ok": True, "changed": ["x"], "restart": True}]).pop(0)

    monkeypatch.setattr(web, "apply_change", fake_apply)
    monkeypatch.setattr(web, "ACK_FILE", str(tmp_path / "no-ack"))
    # Don't shell out to `ip` during tests; the address disclosure is covered
    # on its own in test_dashboard_shows_management_addresses.
    monkeypatch.setattr(web, "iface_ipv4", lambda iface: None)
    daemon = _FakeDaemon(web_cfg, table if table is not None else {"enabled": False},
                         networks=networks)
    return web.create_app(daemon).test_client(), sent


def _csrf(body):
    import re

    return re.search(r'name="csrf" value="([^"]+)"', body).group(1)


def test_controls_refused_while_auth_is_off(monkeypatch, tmp_path):
    client, sent = _client(monkeypatch, tmp_path, {"auth": "none"})
    assert "Plugin controls are off" in client.get("/").get_data(as_text=True)
    assert client.post("/manage/wpa-sec", data={"action": "enable"}).status_code == 403
    assert sent == []


def test_basic_auth_with_an_empty_password_fails_closed(monkeypatch, tmp_path):
    client, _ = _client(monkeypatch, tmp_path,
                        {"auth": "basic", "username": "pi", "password": ""})
    assert client.get("/", headers=_basic("pi", "")).status_code == 401


def test_unknown_auth_mode_fails_closed(monkeypatch, tmp_path):
    client, _ = _client(monkeypatch, tmp_path, {"auth": "bsaic"})
    assert client.get("/").status_code == 401


def test_manage_page_never_shows_the_secret(monkeypatch, tmp_path):
    client, _ = _client(monkeypatch, tmp_path, BASIC,
                        table={"enabled": False, "api_key": "SECRETVALUE"})
    assert client.get("/manage/wpa-sec").status_code == 401
    body = client.get("/manage/wpa-sec", headers=_basic()).get_data(as_text=True)
    assert "SECRETVALUE" not in body
    assert 'type="password"' in body and "set (leave empty to keep it)" in body
    assert "I am authorized" in body    # network plugin: ack offered


def test_post_without_csrf_is_refused(monkeypatch, tmp_path):
    client, sent = _client(monkeypatch, tmp_path, BASIC)
    resp = client.post("/manage/wpa-sec", data={"action": "enable"}, headers=_basic())
    assert resp.status_code == 400 and sent == []


def test_form_sends_only_what_changed(monkeypatch, tmp_path):
    client, sent = _client(monkeypatch, tmp_path, BASIC)
    token = _csrf(client.get("/manage/wpa-sec", headers=_basic()).get_data(as_text=True))
    resp = client.post("/manage/wpa-sec", headers=_basic(), data={
        "csrf": token,
        "enabled-field": "1", "enabled": "on",
        "opt-api_key": "k3y",
        "opt-endpoint": "https://wpa-sec.stanev.org",   # unchanged (the default)
        "bool-download_results": "1",                   # unchecked = default off
        "opt-timeout": "10",
        "ack": "yes",
    })
    assert resp.status_code == 200
    assert "Restarting the plugin host" in resp.get_data(as_text=True)
    assert sent == [{"plugin": "wpa-sec", "ack": True, "enabled": True,
                     "set": {"api_key": "k3y", "timeout": 10}}]


def test_status_toggle_and_refusal_shows_missing(monkeypatch, tmp_path):
    client, sent = _client(monkeypatch, tmp_path, BASIC, replies=[
        {"ok": False, "missing": ["api_key"], "error": "set api_key before enabling wpa-sec"},
    ])
    # The per-plugin enable/disable control lives on the plugins page now.
    page = client.get("/plugins", headers=_basic()).get_data(as_text=True)
    assert ">Enable</button>" in page
    resp = client.post("/manage/wpa-sec", headers=_basic(),
                       data={"csrf": _csrf(page), "action": "enable"})
    assert sent == [{"plugin": "wpa-sec", "enabled": True}]
    body = resp.get_data(as_text=True)
    assert resp.status_code == 400 and "(required)" in body and "set api_key" in body
    # The page comes back with Enabled still ticked, ready to fill in and save.
    assert 'name="enabled" checked' in body


def test_refused_save_keeps_entries_but_not_the_secret(monkeypatch, tmp_path):
    client, _ = _client(monkeypatch, tmp_path, BASIC, replies=[
        {"ok": False, "need_ack": True, "error": "confirm authorized use"},
    ])
    token = _csrf(client.get("/manage/wpa-sec", headers=_basic()).get_data(as_text=True))
    resp = client.post("/manage/wpa-sec", headers=_basic(), data={
        "csrf": token, "enabled-field": "1", "enabled": "on",
        "opt-api_key": "TYPEDSECRET", "opt-timeout": "12",
    })
    body = resp.get_data(as_text=True)
    assert resp.status_code == 400 and "confirm authorized use" in body
    assert 'value="12"' in body and 'name="enabled" checked' in body
    assert "TYPEDSECRET" not in body


# -- advanced Settings page ---------------------------------------------------
def test_settings_page_renders_editable_and_readonly(monkeypatch, tmp_path):
    client, _ = _client(monkeypatch, tmp_path, BASIC)
    assert client.get("/settings").status_code == 401   # auth required
    body = client.get("/settings", headers=_basic()).get_data(as_text=True)
    assert "Advanced settings" in body
    # an editable scalar rendered as its own one-key Apply/Reset form
    assert 'name="table" value="capture.scan"' in body
    assert 'name="key" value="dwell_seconds"' in body
    assert ">Apply</button>" in body and ">Reset</button>" in body
    # anchored sections
    assert 'id="capture"' in body and 'id="agent"' in body and 'id="daemon"' in body
    # a dedicated-control key is read-only (no input), and links to its control
    assert "capture.enabled" in body and 'href="/whitelist"' in body
    # the off-limits network file: a read-only note that redirects to the CLI
    assert "/etc/default/kestrel" in body and "network wifi --band" in body
    # plugin options live on the per-plugin page, linked from here
    assert 'href="/manage/wpa-sec"' in body


def test_settings_never_shows_a_secret_value(monkeypatch, tmp_path):
    client, _ = _client(monkeypatch, tmp_path, dict(BASIC, token="SECRETTOKEN"))
    body = client.get("/settings", headers=_basic()).get_data(as_text=True)
    assert "SECRETTOKEN" not in body
    assert "web.token" in body and "configured" in body


def test_settings_is_readonly_when_auth_off(monkeypatch, tmp_path):
    client, sent = _client(monkeypatch, tmp_path, {"auth": "none"})
    body = client.get("/settings").get_data(as_text=True)
    assert "Advanced settings" in body and ">Apply</button>" not in body
    assert client.post("/settings", data={
        "table": "capture.scan", "key": "dwell_seconds", "value": "10"}).status_code == 403
    assert sent == []


def test_settings_submit_sends_typed_config_set(monkeypatch, tmp_path):
    client, sent = _client(monkeypatch, tmp_path, BASIC, replies=[
        {"ok": True, "changed": ["capture.scan.dwell_seconds"], "restart": False,
         "restart_hint": "Saved. Capture will use it the next time it (re)starts."}])
    token = _csrf(client.get("/settings", headers=_basic()).get_data(as_text=True))
    resp = client.post("/settings", headers=_basic(), data={
        "csrf": token, "table": "capture.scan", "key": "dwell_seconds", "value": "10"})
    assert resp.status_code == 200
    # the form string is coerced to the declared type before it is sent on
    assert sent == [{"target": "config-set", "table": "capture.scan",
                     "key": "dwell_seconds", "value": 10}]


def test_settings_reset_sends_the_shipped_default(monkeypatch, tmp_path):
    client, sent = _client(monkeypatch, tmp_path, BASIC, replies=[
        {"ok": True, "changed": ["capture.scan.dwell_seconds"], "restart": False}])
    token = _csrf(client.get("/settings", headers=_basic()).get_data(as_text=True))
    client.post("/settings", headers=_basic(), data={
        "csrf": token, "table": "capture.scan", "key": "dwell_seconds",
        "value": "999", "reset": "1"})
    assert sent == [{"target": "config-set", "table": "capture.scan",
                     "key": "dwell_seconds", "value": 5}]


def test_settings_rejects_a_non_editable_key(monkeypatch, tmp_path):
    client, sent = _client(monkeypatch, tmp_path, BASIC)
    token = _csrf(client.get("/settings", headers=_basic()).get_data(as_text=True))
    resp = client.post("/settings", headers=_basic(), data={
        "csrf": token, "table": "web", "key": "port", "value": "9"})
    assert resp.status_code == 400 and sent == []


def test_settings_daemon_change_shows_restart_poll(monkeypatch, tmp_path):
    client, sent = _client(monkeypatch, tmp_path, BASIC, replies=[
        {"ok": True, "changed": ["daemon.epoch_seconds"], "restart": True,
         "restart_hint": "The dashboard will restart to apply this."}])
    token = _csrf(client.get("/settings", headers=_basic()).get_data(as_text=True))
    resp = client.post("/settings", headers=_basic(), data={
        "csrf": token, "table": "daemon", "key": "epoch_seconds", "value": "90"})
    assert "Restarting the dashboard" in resp.get_data(as_text=True)
    assert sent == [{"target": "config-set", "table": "daemon",
                     "key": "epoch_seconds", "value": 90}]


def test_settings_reports_a_bad_number(monkeypatch, tmp_path):
    client, sent = _client(monkeypatch, tmp_path, BASIC)
    token = _csrf(client.get("/settings", headers=_basic()).get_data(as_text=True))
    resp = client.post("/settings", headers=_basic(), data={
        "csrf": token, "table": "capture.scan", "key": "dwell_seconds", "value": "abc"})
    assert resp.status_code == 400 and sent == []
    assert "must be a whole number" in resp.get_data(as_text=True)


def test_settings_post_without_csrf_is_refused(monkeypatch, tmp_path):
    client, sent = _client(monkeypatch, tmp_path, BASIC)
    resp = client.post("/settings", headers=_basic(), data={
        "table": "capture.scan", "key": "dwell_seconds", "value": "10"})
    assert resp.status_code == 400 and sent == []


def test_dashboard_has_a_discreet_settings_link():
    html = web._render_status(_status([]), control={
        "ready": True, "csrf": "t", "qs": "", "enabled": {}})
    assert 'class="advlink"' in html and 'href="/settings"' in html
    assert "Advanced settings" in html


def test_pages_refuse_framing_and_caching(monkeypatch, tmp_path):
    client, _ = _client(monkeypatch, tmp_path, BASIC)
    resp = client.get("/manage/wpa-sec", headers=_basic())
    assert resp.headers["X-Frame-Options"] == "DENY"
    assert resp.headers["Referrer-Policy"] == "no-referrer"
    assert resp.headers["Cache-Control"] == "no-store"


def test_applied_page_script_cannot_be_broken_out_of():
    html = web._render_applied("x", {"changed": ["a"]}, {"qs": "?token=%3C%2Fscript%3E"},
                               "</script><b>")
    assert "</script><b>" not in html


def test_token_auth_keeps_the_token_on_links(monkeypatch, tmp_path):
    client, _ = _client(monkeypatch, tmp_path, {"auth": "token", "token": "T0k"})
    assert client.get("/").status_code == 401
    # The dashboard keeps the token on the plugins link and the capture form.
    body = client.get("/?token=T0k").get_data(as_text=True)
    assert 'href="/plugins?token=T0k"' in body
    assert 'action="/capture?token=T0k"' in body
    # The plugins page keeps it on each plugin's control form and settings link.
    plugins = client.get("/plugins?token=T0k").get_data(as_text=True)
    assert 'href="/manage/wpa-sec?token=T0k"' in plugins
    assert 'action="/manage/wpa-sec?token=T0k"' in plugins


def _plug(name, state, **extra):
    base = {"name": name, "state": state, "target_api": 1, "capabilities": [],
            "capability_violations": [], "reason": "", "note": "", "last_error": ""}
    base.update(extra)
    return base


def test_plugin_name_cell_is_shaded_and_state_column_is_gone():
    html = web._render_plugins(_status([
        _plug("gps", "loaded"),
        _plug("wigle", "disabled"),
        _plug("boom", "failed", reason="bad options: missing api_key"),
    ]))
    assert "<th>state</th>" not in html
    assert 'class="plug s-on" title="loaded"' in html
    assert 'class="plug s-off" title="disabled"' in html
    assert 'class="plug s-err" title="failed"' in html
    # The failure reason is still shown (in the note column).
    assert "missing api_key" in html
    # Legend is present.
    assert 'class="legend"' in html


def test_system_auth_verifies_via_the_root_helper_and_caches(monkeypatch, tmp_path):
    calls = []

    def fake_auth(username, password, socket_path=None, timeout=10):
        calls.append((username, password))
        return {"ok": username == "pi" and password == "letmein"}

    monkeypatch.setattr(web, "_auth_check", fake_auth)
    monkeypatch.setattr(web, "_auth_cache", {})
    client, _ = _client(monkeypatch, tmp_path, {"auth": "system", "username": "pi"})

    # No credentials: 401 with a Basic prompt so the browser asks.
    resp = client.get("/")
    assert resp.status_code == 401
    assert resp.headers.get("WWW-Authenticate", "").startswith("Basic")
    # Wrong password is refused (helper consulted).
    assert client.get("/", headers=_basic("pi", "nope")).status_code == 401
    # Right password is let in, and a repeat is served from the cache (the
    # helper is consulted once for the pair, not on every request).
    assert client.get("/", headers=_basic("pi", "letmein")).status_code == 200
    assert client.get("/", headers=_basic("pi", "letmein")).status_code == 200
    assert sum(1 for c in calls if c == ("pi", "letmein")) == 1
    # A username other than the device account never reaches the helper.
    calls.clear()
    assert client.get("/", headers=_basic("root", "letmein")).status_code == 401
    assert calls == []


def test_system_login_user_defaults_to_the_running_account(monkeypatch):
    assert web.system_login_user({"username": "alice"}) == "alice"
    name = web.system_login_user({})
    assert isinstance(name, str) and name


def test_webserver_miss_count_resets_when_the_host_comes_back():
    created = []
    ws = web.WebServer(app=object(), hosts=["127.0.0.1", "192.168.1.50"],
                       port=8080, logger=LOG, server_factory=_factory(created))
    ws.start()
    ws.reconcile(["127.0.0.1"])                    # one miss
    ws.reconcile(["127.0.0.1", "192.168.1.50"])    # back: the count resets
    ws.reconcile(["127.0.0.1"])                    # one miss again, still bound
    assert ws.bound_hosts() == ["127.0.0.1", "192.168.1.50"]
    assert len(created) == 2                       # never rebound in between
    ws.stop()
    assert ws.bound_hosts() == []                  # stop() releases at once


def test_iface_ipv4_caches_lookups_but_not_failures(monkeypatch):
    monkeypatch.setattr(web, "_addr_cache", {})
    results = [(None, False), ("10.0.0.5", True)]
    calls = []

    def probe(iface):
        calls.append(iface)
        return results.pop(0) if results else ("10.0.0.9", True)

    monkeypatch.setattr(web, "_probe_ipv4", probe)
    assert web.iface_ipv4("wlan0") is None          # ip failed: not cached
    assert web.iface_ipv4("wlan0") == "10.0.0.5"    # retried at once
    assert web.iface_ipv4("wlan0") == "10.0.0.5"    # served from the cache
    assert len(calls) == 2


def test_reconnect_script_aborts_a_stuck_probe():
    script = web._reconnect_script("/", "/status.json", 1000.0)
    assert "AbortController" in script and "limit=5000," in script


def test_webserver_skips_unbindable_host():
    def make(host, port, app):
        if host == "192.168.1.50":
            raise OSError("address not available")
        return _FakeServer(host, port, app)

    ws = web.WebServer(app=object(), hosts=["127.0.0.1", "192.168.1.50"],
                       port=8080, logger=LOG, server_factory=make)
    assert ws.start() is True
    # The unbindable host is simply absent; a later reconcile can retry it.
    assert ws.bound_hosts() == ["127.0.0.1"]


# -- plugin widgets on the dashboard (Phase 3.5) -----------------------------
def test_parse_ratio_reads_the_leading_number():
    assert web._parse_ratio("42.0%") == 42
    assert web._parse_ratio("55C") == 55
    assert web._parse_ratio("999") == 100     # clamped to 0-100
    assert web._parse_ratio("-5") == 0
    assert web._parse_ratio("-") is None
    assert web._parse_ratio("n/a") is None


def test_status_page_renders_plugin_widgets_with_a_bar():
    status = dict(_status([]), widgets={"memtemp": [
        {"name": "mem", "label": "Memory", "value": "42.0%", "kind": "percent"},
        {"name": "temp", "label": "Temperature", "value": "55C", "kind": "text"},
    ]})
    html = web._render_status(status)
    # No "widgets" heading: each card is titled with its plugin name already.
    assert "<h2>widgets</h2>" not in html
    assert 'class="widgets"' in html
    assert "<h3>memtemp</h3>" in html
    assert "Memory" in html and "42.0%" in html
    assert "Temperature" in html and "55C" in html
    assert "width:42%" in html                 # percent element draws a bar
    assert html.count('class="bar"') == 1      # the text element draws none


def test_dashboard_marks_a_quarantined_plugin_card_as_stopped():
    # A quarantined/failed plugin gets a subtle "stopped" badge on its dashboard
    # readout card (linking to the Plugins page, where the reason shows in full),
    # so the operator can tell on the main UI that the plugin isn't running.
    reason = "on_internet_available is stuck (ran past 5m) and was quarantined"
    status = dict(
        _status([
            {"name": "wpa-sec", "state": "failed", "reason": reason},
            {"name": "memtemp", "state": "loaded", "reason": ""},
        ]),
        widgets={
            "wpa-sec": [{"name": "queued", "label": "Queued", "value": "3",
                         "kind": "number"}],
            "memtemp": [{"name": "mem", "label": "Memory", "value": "42.0%",
                         "kind": "percent"}],
        })
    html = web._render_status(status)
    # Exactly one card is flagged and badged (the failed plugin, not the healthy
    # one), the badge links to /plugins, and the reason rides along in its title.
    assert html.count('data-state="stopped"') == 1
    assert html.count('class="wbadge"') == 1
    assert ">stopped</a>" in html
    assert '/plugins' in html and "ran past 5m" in html


def test_dashboard_healthy_plugins_have_no_stopped_badge():
    status = dict(
        _status([{"name": "memtemp", "state": "loaded", "reason": ""}]),
        widgets={"memtemp": [{"name": "mem", "label": "Memory", "value": "42%",
                              "kind": "percent"}]})
    html = web._render_status(status)
    # The .wbadge rule is always in the stylesheet; what must be absent is a
    # badge element and the stopped flag on the card.
    assert 'class="wbadge"' not in html and 'data-state="stopped"' not in html


def test_gauge_element_draws_a_threshold_marker():
    status = dict(_status([]), widgets={"ups-hat": [
        {"name": "charge", "label": "Charge", "value": "42%", "kind": "gauge",
         "mark": 5}]})
    html = web._render_status(status)
    assert 'class="bar"' in html and 'class="mark"' in html
    assert "left:5%" in html and "width:42%" in html


def test_gauge_without_a_mark_has_no_marker():
    status = dict(_status([]), widgets={"p": [
        {"name": "x", "label": "X", "value": "42%", "kind": "percent"}]})
    html = web._render_status(status)
    assert 'class="bar"' in html and 'class="mark"' not in html


def test_status_page_has_no_widgets_section_when_empty():
    assert 'class="widgets"' not in web._render_status(_status([]))
    assert 'class="widgets"' not in web._render_status(dict(_status([]), widgets={}))


def test_widget_values_and_labels_are_escaped():
    status = dict(_status([]), widgets={"evil": [
        {"name": "x", "label": "<b>", "value": "<script>", "kind": "text"}]})
    html = web._render_status(status)
    # The dashboard's own reorder/refresh script is the only <script> tag; the
    # evil widget value and label must be escaped, never injected as markup.
    assert html.count("<script>") == 1
    assert "&lt;script&gt;" in html
    assert "&lt;b&gt;" in html


# -- capture control on the dashboard (Phase 3.5) ----------------------------
def _capture_client(monkeypatch, tmp_path, web_cfg, cap_state="stopped",
                    replies=None, acked=False):
    pytest.importorskip("flask")
    sent = []

    def fake_apply(change):
        sent.append(change)
        return (replies or [{"ok": True, "capture": True, "restart": False}]).pop(0)

    monkeypatch.setattr(web, "apply_change", fake_apply)
    ack_path = tmp_path / "ack"
    if acked:
        ack_path.write_text("ack\n")
    monkeypatch.setattr(web, "ACK_FILE", str(ack_path))
    monkeypatch.setattr(web, "iface_ipv4", lambda iface: None)
    daemon = _FakeDaemon(web_cfg, {"enabled": False})
    base_status = daemon.status
    # Like the real daemon, status() keeps reporting the old capture state
    # until the toggle waits for the service to settle.
    state = {"cap": cap_state}
    daemon.waits = []

    def status_with_capture():
        data = base_status()
        data["capture"] = dict(data.get("capture", {}), state=state["cap"],
                               enabled=(state["cap"] == "running"))
        return data

    def wait_capture_state(running):
        daemon.waits.append(running)
        state["cap"] = "running" if running else "stopped"
        return state["cap"]

    daemon.status = status_with_capture
    daemon.wait_capture_state = wait_capture_state
    client = web.create_app(daemon).test_client()
    client.daemon = daemon
    return client, sent


def test_capture_toggle_absent_without_auth(monkeypatch, tmp_path):
    client, _ = _capture_client(monkeypatch, tmp_path, {"auth": "none"})
    assert "capture</button>" not in client.get("/").get_data(as_text=True)


def test_capture_shows_enable_when_stopped(monkeypatch, tmp_path):
    client, _ = _capture_client(monkeypatch, tmp_path, BASIC, cap_state="stopped")
    page = client.get("/", headers=_basic()).get_data(as_text=True)
    assert ">Enable capture</button>" in page


def test_capture_shows_disable_when_running(monkeypatch, tmp_path):
    client, _ = _capture_client(monkeypatch, tmp_path, BASIC, cap_state="running")
    page = client.get("/", headers=_basic()).get_data(as_text=True)
    assert ">Disable capture</button>" in page


def test_capture_enable_prompts_for_the_ack(monkeypatch, tmp_path):
    client, sent = _capture_client(monkeypatch, tmp_path, BASIC, replies=[
        {"ok": False, "need_ack": True, "error": "confirm authorized use to enable it"},
    ])
    page = client.get("/", headers=_basic()).get_data(as_text=True)
    resp = client.post("/capture", headers=_basic(),
                       data={"csrf": _csrf(page), "action": "enable"})
    body = resp.get_data(as_text=True)
    assert sent == [{"target": "capture", "enabled": True}]
    assert "deauthentication frames" in body        # the capture ack text
    assert "I am authorized" in body                # the confirm checkbox


def test_capture_enable_with_ack_succeeds(monkeypatch, tmp_path):
    client, sent = _capture_client(monkeypatch, tmp_path, BASIC,
                                   replies=[{"ok": True, "capture": True, "restart": False}])
    page = client.get("/", headers=_basic()).get_data(as_text=True)
    resp = client.post("/capture", headers=_basic(),
                       data={"csrf": _csrf(page), "action": "enable", "ack": "yes"})
    # Success shows the dashboard with no banner: the capture row reflects the
    # state, so a "Capture enabled" message beside it is just noise.
    assert resp.status_code == 200
    assert "Capture enabled." not in resp.get_data(as_text=True)
    assert sent == [{"target": "capture", "enabled": True, "ack": True}]


def test_capture_enable_flips_the_toggle_on_the_first_tap(monkeypatch, tmp_path):
    # The page served after the POST must already show the new state; it used
    # to re-render from the pre-change cached state, so it took a second tap.
    client, _ = _capture_client(monkeypatch, tmp_path, BASIC, cap_state="stopped",
                                replies=[{"ok": True, "capture": True, "restart": False}])
    page = client.get("/", headers=_basic()).get_data(as_text=True)
    body = client.post("/capture", headers=_basic(),
                       data={"csrf": _csrf(page), "action": "enable", "ack": "yes"}
                       ).get_data(as_text=True)
    assert client.daemon.waits == [True]
    assert ">Disable capture</button>" in body


def test_capture_disable_flips_the_toggle_on_the_first_tap(monkeypatch, tmp_path):
    client, _ = _capture_client(monkeypatch, tmp_path, BASIC, cap_state="running",
                                replies=[{"ok": True, "capture": False, "restart": False}])
    page = client.get("/", headers=_basic()).get_data(as_text=True)
    body = client.post("/capture", headers=_basic(),
                       data={"csrf": _csrf(page), "action": "disable"}
                       ).get_data(as_text=True)
    assert client.daemon.waits == [False]
    assert ">Enable capture</button>" in body


def test_capture_refused_change_does_not_wait(monkeypatch, tmp_path):
    client, _ = _capture_client(monkeypatch, tmp_path, BASIC, replies=[
        {"ok": False, "error": "no monitor-capable capture adapter found"}])
    page = client.get("/", headers=_basic()).get_data(as_text=True)
    resp = client.post("/capture", headers=_basic(),
                       data={"csrf": _csrf(page), "action": "enable", "ack": "yes"})
    assert resp.status_code == 400
    assert client.daemon.waits == []


def test_capture_disable_posts_the_change(monkeypatch, tmp_path):
    client, sent = _capture_client(monkeypatch, tmp_path, BASIC, cap_state="running",
                                   replies=[{"ok": True, "capture": False, "restart": False}])
    page = client.get("/", headers=_basic()).get_data(as_text=True)
    resp = client.post("/capture", headers=_basic(),
                       data={"csrf": _csrf(page), "action": "disable"})
    assert resp.status_code == 200
    assert "Capture disabled." not in resp.get_data(as_text=True)
    assert sent == [{"target": "capture", "enabled": False}]


def test_capture_post_without_csrf_is_refused(monkeypatch, tmp_path):
    client, sent = _capture_client(monkeypatch, tmp_path, BASIC)
    resp = client.post("/capture", headers=_basic(), data={"action": "enable"})
    assert resp.status_code == 400 and sent == []


# -- device power buttons on the dashboard (Phase 3.5) -----------------------
def test_power_buttons_absent_without_auth(monkeypatch, tmp_path):
    client, _ = _capture_client(monkeypatch, tmp_path, {"auth": "none"})
    page = client.get("/").get_data(as_text=True)
    assert "Device power" not in page
    assert ">Reboot</button>" not in page and ">Shut down</button>" not in page


def test_power_buttons_shown_with_auth(monkeypatch, tmp_path):
    client, _ = _capture_client(monkeypatch, tmp_path, BASIC)
    page = client.get("/", headers=_basic()).get_data(as_text=True)
    assert "Device power" in page
    assert ">Reboot</button>" in page and ">Shut down</button>" in page


def test_power_reboot_asks_to_confirm_before_applying(monkeypatch, tmp_path):
    client, sent = _capture_client(monkeypatch, tmp_path, BASIC)
    page = client.get("/", headers=_basic()).get_data(as_text=True)
    resp = client.post("/power", headers=_basic(),
                       data={"csrf": _csrf(page), "action": "reboot"})
    body = resp.get_data(as_text=True)
    # The confirm page, not the apply: a stray tap never reboots.
    assert sent == []
    assert "Reboot?" in body and 'name="confirm" value="yes"' in body
    assert "come back on its own" in body


def test_power_shutdown_confirm_applies_through_the_root_helper(monkeypatch, tmp_path):
    client, sent = _capture_client(monkeypatch, tmp_path, BASIC,
                                   replies=[{"ok": True, "power": "shutdown"}])
    page = client.get("/", headers=_basic()).get_data(as_text=True)
    resp = client.post("/power", headers=_basic(),
                       data={"csrf": _csrf(page), "action": "shutdown",
                             "confirm": "yes"})
    assert resp.status_code == 200
    assert sent == [{"target": "power", "action": "shutdown"}]
    assert "Shutting down" in resp.get_data(as_text=True)


def test_power_post_without_csrf_is_refused(monkeypatch, tmp_path):
    client, sent = _capture_client(monkeypatch, tmp_path, BASIC)
    resp = client.post("/power", headers=_basic(),
                       data={"action": "reboot", "confirm": "yes"})
    assert resp.status_code == 400 and sent == []


def test_power_unknown_action_changes_nothing(monkeypatch, tmp_path):
    client, sent = _capture_client(monkeypatch, tmp_path, BASIC)
    page = client.get("/", headers=_basic()).get_data(as_text=True)
    resp = client.post("/power", headers=_basic(),
                       data={"csrf": _csrf(page), "action": "halt", "confirm": "yes"})
    assert sent == []
    assert "Nothing changed." in resp.get_data(as_text=True)


# -- re-pair phone from the web (no phone paired) ----------------------------
def test_bt_pair_button_hidden_without_auth(monkeypatch, tmp_path):
    client, _ = _capture_client(monkeypatch, tmp_path, {"auth": "none"})
    page = client.get("/").get_data(as_text=True)
    assert "/bt-pair" not in page


def test_bt_pair_asks_to_confirm_before_applying(monkeypatch, tmp_path):
    client, sent = _capture_client(monkeypatch, tmp_path, BASIC)
    page = client.get("/", headers=_basic()).get_data(as_text=True)
    assert 'action="/bt-pair"' in page            # the discreet button is shown
    resp = client.post("/bt-pair", headers=_basic(), data={"csrf": _csrf(page)})
    body = resp.get_data(as_text=True)
    # The confirm page, not the apply: a stray tap never reopens onboarding.
    assert sent == []
    assert "Pair a phone over Bluetooth?" in body
    assert 'name="confirm" value="yes"' in body


def test_bt_pair_confirm_applies_through_the_root_helper(monkeypatch, tmp_path):
    client, sent = _capture_client(monkeypatch, tmp_path, BASIC,
                                   replies=[{"ok": True, "bt_pair": True,
                                             "restart": False}])
    page = client.get("/", headers=_basic()).get_data(as_text=True)
    resp = client.post("/bt-pair", headers=_basic(),
                       data={"csrf": _csrf(page), "confirm": "yes"})
    assert resp.status_code == 200
    assert sent == [{"target": "bt-pair"}]
    assert "Pairing window open" in resp.get_data(as_text=True)


def test_bt_pair_post_without_csrf_is_refused(monkeypatch, tmp_path):
    client, sent = _capture_client(monkeypatch, tmp_path, BASIC)
    resp = client.post("/bt-pair", headers=_basic(), data={"confirm": "yes"})
    assert resp.status_code == 400 and sent == []


# -- auto-refresh and the dashboard script (Phase 3.5, round 3) --------------
def test_refresh_seconds_reads_config_and_fails_safe():
    assert web.refresh_seconds({"refresh_seconds": 30}) == 30
    assert web.refresh_seconds({"refresh_seconds": "15"}) == 15
    assert web.refresh_seconds({"refresh_seconds": 0}) == 0         # off
    assert web.refresh_seconds({"refresh_seconds": -5}) == 0        # off
    assert web.refresh_seconds({"refresh_seconds": "nope"}) == 0    # bad -> off
    assert web.refresh_seconds({}) == 0                             # missing -> off
    assert web.refresh_seconds(None) == 0


def test_plain_dashboard_carries_no_script():
    # No widgets and no refresh: nothing to enhance, so no <script> at all.
    assert "<script>" not in web._render_status(_status([]))
    assert "<script>" not in web._render_status(_status([]), refresh=0)


def test_dashboard_script_emits_refresh_interval_in_ms():
    html = web._render_status(_status([]), refresh=30)
    assert "<script>" in html
    assert "var REFRESH=30;" in html
    # The reload is paused while a drag is in progress or the tab is hidden.
    assert "document.hidden" in html and "REFRESH*1000" in html


def test_dashboard_script_reorders_widgets_by_data_plugin():
    status = dict(_status([]), widgets={
        "memtemp": [{"name": "mem", "label": "Memory", "value": "40%", "kind": "percent"}],
        "gps": [{"name": "status", "label": "GPS", "value": "3D fix", "kind": "text"}],
    })
    html = web._render_status(status, refresh=0)
    # Each card is tagged with its plugin so the saved order can be reapplied,
    # and the order is persisted per-browser in localStorage.
    assert 'data-plugin="memtemp"' in html and 'data-plugin="gps"' in html
    assert "<script>" in html                 # reorder works even with refresh off
    assert "pwn-widgets-order" in html
    assert "localStorage" in html


def test_dashboard_reorder_captures_pointer_on_the_grid_not_the_grip():
    # iOS Safari drops pointer capture when the captured element is moved in the
    # DOM; the reorder moves the dragged card, so capture/listen on the stable
    # grid (not the grip) or touch drag freezes on iPhone.
    status = dict(_status([]), widgets={
        "memtemp": [{"name": "mem", "label": "Memory", "value": "40%", "kind": "percent"}],
    })
    html = web._render_status(status, refresh=0)
    assert "grid.setPointerCapture" in html
    assert "grid.addEventListener('pointermove'" in html
    assert "g.setPointerCapture" not in html   # must not capture on the moved grip


def test_dashboard_links_to_the_networks_page():
    html = web._render_status(_status([]))
    assert 'href="/networks"' in html
    assert "Captured networks" in html


# -- captured-networks page (Phase 3.5, round 3) -----------------------------
def _nets(*essids):
    return [{"essid": e, "bssids": 1, "handshakes": 1} for e in essids]


def test_networks_page_lists_and_counts(monkeypatch, tmp_path):
    client, _ = _client(monkeypatch, tmp_path, {"auth": "none"},
                        networks=_nets("HomeWiFi", "CoffeeShop", ""))
    body = client.get("/networks").get_data(as_text=True)
    assert "Captured networks" in body
    assert "HomeWiFi" in body and "CoffeeShop" in body
    assert "(hidden)" in body                 # the empty ESSID renders as hidden
    assert "3 network(s) captured" in body


def test_networks_page_search_filters_by_essid(monkeypatch, tmp_path):
    client, _ = _client(monkeypatch, tmp_path, {"auth": "none"},
                        networks=_nets("HomeWiFi", "CoffeeShop", "HomeOffice"))
    body = client.get("/networks?q=home").get_data(as_text=True)
    assert "HomeWiFi" in body and "HomeOffice" in body
    assert "CoffeeShop" not in body
    assert "2 of 3 networks match" in body


def test_networks_page_paginates_and_keeps_query(monkeypatch, tmp_path):
    many = _nets(*["net-%03d" % i for i in range(250)])
    client, _ = _client(monkeypatch, tmp_path, {"auth": "none"}, networks=many)
    first = client.get("/networks").get_data(as_text=True)
    assert "net-000" in first and "net-099" in first
    assert "net-100" not in first             # 100 per page
    assert "page 1 of 3" in first
    second = client.get("/networks?page=2").get_data(as_text=True)
    assert "net-100" in second and "net-000" not in second
    # An out-of-range page clamps rather than erroring.
    assert client.get("/networks?page=99").status_code == 200


def test_networks_search_form_carries_the_token(monkeypatch, tmp_path):
    client, _ = _client(monkeypatch, tmp_path,
                        {"auth": "token", "token": "s3cr3t"},
                        networks=_nets("HomeWiFi"))
    body = client.get("/networks?token=s3cr3t").get_data(as_text=True)
    # The search form and pager must keep the token so the authed page can
    # submit a search / page without dropping auth.
    assert 'name="token" value="s3cr3t"' in body


# -- release-update card + /update route (updates plugin) --------------------
def _update_client(monkeypatch, tmp_path, web_cfg=None, state=None,
                   enabled=True, replies=None):
    pytest.importorskip("flask")
    from kestrel_pi.loader import PluginRecord
    web_cfg = web_cfg if web_cfg is not None else BASIC
    sent = []

    def fake_apply(change):
        sent.append(change)
        return (replies or [{"ok": True, "update": "apply"}]).pop(0)

    monkeypatch.setattr(web, "apply_change", fake_apply)
    monkeypatch.setattr(web, "_read_update_state", lambda path=None: dict(state or {}))
    monkeypatch.setattr(web, "iface_ipv4", lambda iface: None)
    daemon = _FakeDaemon(web_cfg, {"enabled": False})
    rec = PluginRecord("updates", "/x/updates.py")
    rec.capabilities = ("network", "ui")
    rec.state, rec.reason = ("loaded", "") if enabled else ("disabled", "off")
    daemon.loader.records["updates"] = rec
    daemon.config["plugins"]["updates"] = {"enabled": enabled}
    return web.create_app(daemon).test_client(), sent


_OFFERABLE = {"update_available": True, "offerable": True, "latest": "0.5.0"}
_REFLASH = {"update_available": True, "offerable": False, "latest": "0.6.0",
            "reason": "the base image changed"}


def test_update_card_absent_without_auth(monkeypatch, tmp_path):
    client, _ = _update_client(monkeypatch, tmp_path, web_cfg={"auth": "none"},
                               state=_OFFERABLE)
    assert "Update available" not in client.get("/").get_data(as_text=True)


def test_update_card_absent_when_plugin_disabled(monkeypatch, tmp_path):
    client, _ = _update_client(monkeypatch, tmp_path, state=_OFFERABLE, enabled=False)
    assert "Update available" not in client.get("/", headers=_basic()).get_data(as_text=True)


def test_update_card_absent_when_up_to_date(monkeypatch, tmp_path):
    client, _ = _update_client(monkeypatch, tmp_path,
                               state={"update_available": False, "installed": "0.5.0"})
    assert "Update available" not in client.get("/", headers=_basic()).get_data(as_text=True)


def test_update_card_offers_the_upgrade_button(monkeypatch, tmp_path):
    client, _ = _update_client(monkeypatch, tmp_path, state=_OFFERABLE)
    page = client.get("/", headers=_basic()).get_data(as_text=True)
    assert "Update available" in page
    assert ">Upgrade to 0.5.0</button>" in page
    assert 'action="/update' in page


def test_update_card_shows_needs_reflash_without_a_button(monkeypatch, tmp_path):
    client, _ = _update_client(monkeypatch, tmp_path, state=_REFLASH)
    page = client.get("/", headers=_basic()).get_data(as_text=True)
    assert "needs a reflash" in page
    assert "the base image changed" in page
    assert "Upgrade to" not in page           # no in-place button


def test_update_apply_asks_to_confirm_first(monkeypatch, tmp_path):
    client, sent = _update_client(monkeypatch, tmp_path, state=_OFFERABLE)
    page = client.get("/", headers=_basic()).get_data(as_text=True)
    resp = client.post("/update", headers=_basic(),
                       data={"csrf": _csrf(page), "action": "apply"})
    body = resp.get_data(as_text=True)
    # The confirm page, not the apply: a stray tap never upgrades.
    assert sent == []
    assert "Upgrade to 0.5.0?" in body and 'name="confirm" value="yes"' in body
    # It spells out the sequence; the backup step is absent (backup not set up).
    assert "stop capture" in body
    assert "pausing training" in body
    assert "stop the plugin daemon" in body
    assert "back up the latest capture data" not in body


def test_update_confirm_names_the_backup_step_when_backup_is_set_up():
    pytest.importorskip("flask")
    control = {"csrf": "tok", "qs": "", "ready": True,
               "enabled": {"updates": True, "backup": True},
               "backup_ready": True}
    page = web._render_update_confirm(control, {"latest": "0.5.0"})
    assert "back up the latest capture data and the learning model" in page
    assert "cancel the update" in page
    # The network sentence names the backup when a pre-update backup runs.
    assert "the download and the backup use the network" in page


def test_update_confirm_omits_the_backup_step_when_not_set_up():
    pytest.importorskip("flask")
    control = {"csrf": "tok", "qs": "", "ready": True,
               "enabled": {"updates": True, "backup": True},
               "restore_ready": False}  # the device key is not installed yet
    page = web._render_update_confirm(control, {"latest": "0.5.0"})
    assert "back up the latest capture data" not in page
    # ...and the network sentence drops the backup mention too.
    assert "Only the download uses the network" in page
    assert "the backup use the network" not in page


def test_update_confirm_warns_on_a_single_link_and_points_at_recovery(monkeypatch):
    pytest.importorskip("flask")
    monkeypatch.setattr(web, "management_addresses",
                        lambda *a, **k: [("bnep0", "172.20.10.2"), ("wlan0", None)])
    control = {"csrf": "tok", "qs": "", "ready": True,
               "enabled": {"updates": True}, "restore_ready": False}
    page = web._render_update_confirm(control, {"latest": "0.5.0"})
    # Only bnep0 has an address -> the sharp single-link warning, naming the iface.
    assert "reachable on only one connection right now (bnep0)" in page
    # ...and it always points at the boot-folder Wi-Fi recovery.
    assert "kestrel-wifi.txt" in page


def test_update_confirm_softer_note_when_two_links_are_up(monkeypatch):
    pytest.importorskip("flask")
    monkeypatch.setattr(web, "management_addresses",
                        lambda *a, **k: [("bnep0", "172.20.10.2"),
                                         ("wlan0", "192.168.1.90")])
    control = {"csrf": "tok", "qs": "", "ready": True,
               "enabled": {"updates": True}, "restore_ready": False}
    page = web._render_update_confirm(control, {"latest": "0.5.0"})
    # Two links up -> the softer Bluetooth-PAN heads-up, not the single-link one.
    assert "reachable on only one connection right now" not in page
    assert "if your only link is Bluetooth PAN" in page
    assert "kestrel-wifi.txt" in page


def test_wbr_breaks_hosts_and_paths_but_not_decimals():
    pytest.importorskip("flask")
    assert web._wbr("war-mango.DDNS.net") == "war-mango.<wbr>DDNS.<wbr>net"
    assert web._wbr("pi@host:/srv/x") == "pi@<wbr>host:<wbr>/<wbr>srv/<wbr>x"
    # Decimals and versions keep their dots (no mid-number break).
    assert web._wbr("393.7") == "393.7"
    assert web._wbr("0.4.8") == "0.4.8"
    # Escaped entities carry no @/./:/ , so they pass through untouched.
    assert web._wbr(web._esc("a & b")) == web._esc("a & b")


def test_update_apply_confirm_goes_through_the_root_helper(monkeypatch, tmp_path):
    client, sent = _update_client(monkeypatch, tmp_path, state=_OFFERABLE,
                                  replies=[{"ok": True, "update": "apply"}])
    page = client.get("/", headers=_basic()).get_data(as_text=True)
    resp = client.post("/update", headers=_basic(),
                       data={"csrf": _csrf(page), "action": "apply",
                             "confirm": "yes", "tag": "0.5.0"})
    assert resp.status_code == 200
    assert sent == [{"target": "update", "action": "apply", "tag": "0.5.0"}]
    assert "Upgrading" in resp.get_data(as_text=True)


def test_update_confirm_page_carries_the_confirmed_tag(monkeypatch, tmp_path):
    client, _sent = _update_client(monkeypatch, tmp_path, state=_OFFERABLE)
    page = client.get("/", headers=_basic()).get_data(as_text=True)
    body = client.post("/update", headers=_basic(),
                       data={"csrf": _csrf(page), "action": "apply"}).get_data(as_text=True)
    assert 'name="tag" value="0.5.0"' in body


def test_update_apply_refused_when_the_offer_changed_since_confirming(monkeypatch,
                                                                     tmp_path):
    # Confirmed 0.4.9, but a check since moved the offer to 0.5.0: never install
    # something other than what the operator confirmed.
    client, sent = _update_client(monkeypatch, tmp_path, state=_OFFERABLE)
    page = client.get("/", headers=_basic()).get_data(as_text=True)
    resp = client.post("/update", headers=_basic(),
                       data={"csrf": _csrf(page), "action": "apply",
                             "confirm": "yes", "tag": "0.4.9"})
    assert resp.status_code == 400 and sent == []
    assert "changed since you confirmed" in resp.get_data(as_text=True)


def test_update_apply_refused_when_not_offerable_server_side(monkeypatch, tmp_path):
    # A stale button whose state has since flipped to reflash must not apply.
    client, sent = _update_client(monkeypatch, tmp_path, state=_REFLASH)
    page = client.get("/", headers=_basic()).get_data(as_text=True)
    # Forge the submit (the real card shows no button here) to prove the guard.
    resp = client.post("/update", headers=_basic(),
                       data={"csrf": _csrf(page), "action": "apply",
                             "confirm": "yes"})
    assert resp.status_code == 400 and sent == []
    assert "No in-place update is available" in resp.get_data(as_text=True)


def test_update_post_without_csrf_is_refused(monkeypatch, tmp_path):
    client, sent = _update_client(monkeypatch, tmp_path, state=_OFFERABLE)
    resp = client.post("/update", headers=_basic(),
                       data={"action": "apply", "confirm": "yes"})
    assert resp.status_code == 400 and sent == []


def test_update_unknown_action_changes_nothing(monkeypatch, tmp_path):
    client, sent = _update_client(monkeypatch, tmp_path, state=_OFFERABLE)
    page = client.get("/", headers=_basic()).get_data(as_text=True)
    resp = client.post("/update", headers=_basic(),
                       data={"csrf": _csrf(page), "action": "wat"})
    assert sent == []
    assert "Nothing changed." in resp.get_data(as_text=True)


# -- generic plugin row buttons (__actions__ + /plugins/<name>/<path>) -------
# The five bespoke button routes (backup/restore, remote-train, update-check,
# wpa-sec cracked) are gone: a plugin now declares up to two row buttons via
# __actions__/webhook_actions and renders/handles them in its own on_webhook.
# These tests cover the generic core mechanism; each plugin's own behaviour is
# tested with the plugin (test_reference_plugins.py, test_updates.py).
class _FakeActionPlugin:
    """A minimal loaded plugin for exercising the generic row-button mechanism:
    a controllable action list and an on_webhook that records the call and
    returns whatever marker/result the test wants."""

    def __init__(self, actions=(), handler=None):
        self._actions = tuple(actions)
        self._handler = handler
        self.calls = []
        self.context = types.SimpleNamespace(form_csrf="", web_authed=False)

    def webhook_actions(self):
        return self._actions

    def on_webhook(self, path, request):
        self.calls.append((path, request.method, self.context.form_csrf,
                           self.context.web_authed))
        return self._handler(path, request) if self._handler else None


def _action_client(monkeypatch, tmp_path, web_cfg=None, enabled=True,
                   actions=(), handler=None):
    pytest.importorskip("flask")
    from kestrel_pi.loader import PluginRecord
    web_cfg = web_cfg if web_cfg is not None else BASIC
    monkeypatch.setattr(web, "iface_ipv4", lambda iface: None)
    monkeypatch.setattr(web, "ACK_FILE", str(tmp_path / "no-ack"))
    daemon = _FakeDaemon(web_cfg, {"enabled": False})
    rec = PluginRecord("demo", "/x/demo.py")
    rec.capabilities = ("ui",)
    rec.state = "loaded" if enabled else "disabled"
    rec.reason = "" if enabled else "off"
    plugin = _FakeActionPlugin(actions=actions, handler=handler)
    rec.instance = plugin
    daemon.loader.records["demo"] = rec
    daemon.config["plugins"]["demo"] = {"enabled": enabled}
    return web.create_app(daemon).test_client(), plugin


def test_get_action_renders_as_a_navigation_button(monkeypatch, tmp_path):
    client, _ = _action_client(
        monkeypatch, tmp_path,
        actions=[{"label": "Show", "path": "view", "method": "get"}])
    page = client.get("/plugins", headers=_basic()).get_data(as_text=True)
    assert ">Show</button>" in page
    assert 'method="get" action="/plugins/demo/view"' in page


def test_post_action_renders_with_a_csrf_field(monkeypatch, tmp_path):
    client, _ = _action_client(
        monkeypatch, tmp_path,
        actions=[{"label": "Go", "path": "act", "method": "post"}])
    page = client.get("/plugins", headers=_basic()).get_data(as_text=True)
    assert ">Go</button>" in page
    assert 'method="post" action="/plugins/demo/act"' in page
    assert 'name="csrf"' in page            # a POST button carries the token


def test_actions_absent_without_web_auth(monkeypatch, tmp_path):
    client, _ = _action_client(
        monkeypatch, tmp_path, web_cfg={"auth": "none"},
        actions=[{"label": "Show", "path": "view"}])
    page = client.get("/plugins").get_data(as_text=True)
    assert ">Show</button>" not in page     # read-only: no action buttons


def test_actions_absent_when_the_plugin_is_disabled(monkeypatch, tmp_path):
    client, _ = _action_client(
        monkeypatch, tmp_path, enabled=False,
        actions=[{"label": "Show", "path": "view"}])
    page = client.get("/plugins", headers=_basic()).get_data(as_text=True)
    assert ">Show</button>" not in page


def test_at_most_two_actions_are_shown_in_order(monkeypatch, tmp_path):
    client, _ = _action_client(monkeypatch, tmp_path, actions=[
        {"label": "Second", "path": "b", "order": 2},
        {"label": "First", "path": "a", "order": 1},
        {"label": "Third", "path": "c", "order": 3},   # capped away (only 2)
    ])
    page = client.get("/plugins", headers=_basic()).get_data(as_text=True)
    assert ">First</button>" in page and ">Second</button>" in page
    assert ">Third</button>" not in page
    assert page.index(">First</button>") < page.index(">Second</button>")


def test_action_buttons_are_grouped_in_one_right_aligned_box(monkeypatch, tmp_path):
    # Both action buttons live inside a single ``.rowactions`` box so they stay
    # together on one line and wrap below as a unit, instead of each floating
    # right onto its own staggered row.
    client, _ = _action_client(monkeypatch, tmp_path, actions=[
        {"label": "First", "path": "a", "order": 1},
        {"label": "Second", "path": "b", "order": 2},
    ])
    page = client.get("/plugins", headers=_basic()).get_data(as_text=True)
    assert page.count('<div class="rowactions">') == 1
    box = page.split('<div class="rowactions">', 1)[1]
    assert box.index(">First</button>") < box.index(">Second</button>") \
        < box.index("</div>")


def test_get_route_wraps_a_PluginPage_in_the_shared_shell(monkeypatch, tmp_path):
    def handler(path, request):
        from kestrel_pi.plugins import PluginPage
        return PluginPage("demo title - Kestrel", "<h1>Hello body</h1>")

    client, plugin = _action_client(monkeypatch, tmp_path, handler=handler)
    resp = client.get("/plugins/demo/view", headers=_basic())
    body = resp.get_data(as_text=True)
    assert resp.status_code == 200
    assert "<title>demo title - Kestrel</title>" in body
    assert "<h1>Hello body</h1>" in body
    assert "docfoot" in body                       # the shared page shell
    assert plugin.calls[0][0:2] == ("view", "GET")


def test_get_route_needs_no_auth_or_csrf(monkeypatch, tmp_path):
    # A GET is plain navigation to a page the plugin renders (it gates its own
    # sensitive data on web_authed), so it is reachable with auth off.
    called = {}

    def handler(path, request):
        from kestrel_pi.plugins import PluginPage
        called["web_authed"] = request  # marker; context read below
        return PluginPage("t", "<p>ok</p>")

    client, plugin = _action_client(monkeypatch, tmp_path,
                                    web_cfg={"auth": "none"}, handler=handler)
    resp = client.get("/plugins/demo/view")
    assert resp.status_code == 200 and "<p>ok</p>" in resp.get_data(as_text=True)
    # context.web_authed is False when the login is off.
    assert plugin.calls[0][3] is False


def test_post_route_turns_a_PluginRedirect_into_a_banner(monkeypatch, tmp_path):
    def handler(path, request):
        from kestrel_pi.plugins import PluginRedirect
        return PluginRedirect("Done.")

    client, plugin = _action_client(monkeypatch, tmp_path, handler=handler)
    page = client.get("/plugins", headers=_basic()).get_data(as_text=True)
    resp = client.post("/plugins/demo/act", headers=_basic(),
                       data={"csrf": _csrf(page)})
    assert resp.status_code == 303
    assert resp.headers["Location"] == "/plugins?msg=Done."
    # ...and the plugins page renders that banner on the follow-up GET.
    follow = client.get("/plugins?msg=Done.", headers=_basic()).get_data(as_text=True)
    assert "Done." in follow
    assert plugin.calls and plugin.calls[0][0:2] == ("act", "POST")


def test_post_route_error_redirect_uses_the_err_param(monkeypatch, tmp_path):
    def handler(path, request):
        from kestrel_pi.plugins import PluginRedirect
        return PluginRedirect("nope", error=True)

    client, _ = _action_client(monkeypatch, tmp_path, handler=handler)
    page = client.get("/plugins", headers=_basic()).get_data(as_text=True)
    resp = client.post("/plugins/demo/act", headers=_basic(),
                       data={"csrf": _csrf(page)})
    assert resp.status_code == 303 and resp.headers["Location"] == "/plugins?err=nope"


def test_post_route_requires_csrf(monkeypatch, tmp_path):
    called = {"n": 0}

    def handler(path, request):
        called["n"] += 1

    client, _ = _action_client(monkeypatch, tmp_path, handler=handler)
    resp = client.post("/plugins/demo/act", headers=_basic(), data={})
    assert resp.status_code == 400 and called["n"] == 0


def test_post_route_requires_web_auth(monkeypatch, tmp_path):
    # With the login off the dashboard is read-only, so a privileged POST to a
    # plugin is refused before the plugin's on_webhook runs - like every core
    # control POST.
    called = {"n": 0}

    def handler(path, request):
        called["n"] += 1

    client, _ = _action_client(monkeypatch, tmp_path, web_cfg={"auth": "none"},
                               handler=handler)
    resp = client.post("/plugins/demo/act", data={})
    assert resp.status_code == 403 and called["n"] == 0


def test_route_passes_the_csrf_token_to_the_plugin(monkeypatch, tmp_path):
    # The plugin gets the page's CSRF token (for a confirm form it renders) and
    # the signed-in flag per request.
    def handler(path, request):
        from kestrel_pi.plugins import PluginPage
        return PluginPage("t", "token=[" + request.args.get("probe", "") + "]")

    client, plugin = _action_client(monkeypatch, tmp_path, handler=handler)
    client.get("/plugins/demo/view", headers=_basic())
    # form_csrf is a non-empty token, web_authed True under basic auth.
    assert plugin.calls[0][2] and plugin.calls[0][3] is True


def test_route_passes_through_a_tuple_and_none(monkeypatch, tmp_path):
    def handler(path, request):
        return ("plain", 201) if path == "tuple" else None

    client, _ = _action_client(monkeypatch, tmp_path, handler=handler)
    tup = client.get("/plugins/demo/tuple", headers=_basic())
    assert tup.status_code == 201 and tup.get_data(as_text=True) == "plain"
    nothing = client.get("/plugins/demo/none", headers=_basic())
    assert nothing.status_code == 204


def test_unknown_plugin_route_is_404(monkeypatch, tmp_path):
    client, _ = _action_client(monkeypatch, tmp_path)
    assert client.get("/plugins/nope/x", headers=_basic()).status_code == 404


# -- documentation footer + user-guide page ------------------------------
def test_every_page_has_a_documentation_footer():
    # _page() appends a Documentation link to the user guide on every page.
    html = web._render_status(_status([]))
    assert "docfoot" in html
    assert "href='/guide'" in html
    assert ">Documentation<" in html


def test_guide_index_renders():
    # /guide is now a table of contents, not the whole guide in one scroll.
    html = web._render_guide({"qs": ""})
    assert "<title>Documentation - Kestrel</title>" in html
    assert "<article class='guide'>" in html
    assert "&larr; Dashboard" in html
    assert "<h1" in html                      # the guide's own title heading
    assert "guideindex" in html               # the contents list
    assert 'href="/guide/capturing-handshakes"' in html   # a section link
    assert 'href="/guide/all"' in html        # the whole-guide-on-one-page link


def test_guide_section_renders_with_nav():
    html = web._render_guide_section("troubleshooting", {"qs": ""})
    assert "<title>Documentation - Kestrel</title>" in html
    assert 'id="troubleshooting"' in html     # the section's own heading
    assert "&larr; All topics" in html        # back to the contents
    assert "guidenav" in html                 # prev/next navigation
    # troubleshooting sits between Command reference and Learning algorithms.
    assert 'href="/guide/command-reference"' in html
    assert 'href="/guide/learning-algorithms-in-depth"' in html


def test_guide_section_unknown_returns_none():
    assert web._render_guide_section("not-a-section", {"qs": ""}) is None


def test_guide_cross_section_links_point_at_the_owning_page():
    # A #anchor in troubleshooting that lives in another section is rewritten to
    # that section's page; it must not stay a bare in-page anchor.
    html = web._render_guide_section("troubleshooting", {"qs": ""})
    assert 'href="/guide/getting-online#getting-online"' in html
    assert 'href="#getting-online"' not in html


def test_guide_full_page_has_every_section_on_one_page():
    html = web._render_guide_full({"qs": ""})
    assert 'id="plugins"' in html and 'id="troubleshooting"' in html
    assert "&larr; All topics" in html


def test_guide_token_auth_keeps_links_signed_in():
    html = web._render_guide({"qs": "?token=sek"})
    assert 'href="/guide/plugins?token=sek"' in html


def test_guide_routes(monkeypatch, tmp_path):
    client, _ = _client(monkeypatch, tmp_path, {"auth": "none"})
    assert client.get("/guide").status_code == 200
    assert client.get("/guide/plugins").status_code == 200
    assert client.get("/guide/all").status_code == 200
    assert client.get("/guide/bogus").status_code == 404


# -- the tiny Markdown renderer ------------------------------------------
def test_markdown_inline_and_headings():
    html = web._render_markdown(
        "# Title\n\nA **bold** word, `code`, and a [link](https://ex.test).\n")
    assert '<h1 id="title">Title</h1>' in html
    assert "<strong>bold</strong>" in html
    assert "<code>code</code>" in html
    assert '<a href="https://ex.test">link</a>' in html


def test_markdown_lists_code_and_table():
    md = (
        "- one\n- two\n\n"
        "```\nraw <b> text\n```\n\n"
        "| Key | Default |\n| --- | --- |\n| `enabled` | false |\n")
    html = web._render_markdown(md)
    assert "<ul><li>one</li><li>two</li></ul>" in html
    assert "<pre><code>raw &lt;b&gt; text</code></pre>" in html
    assert "<table><thead>" in html
    assert 'data-label="Key"' in html and 'data-label="Default"' in html
    assert "<code>enabled</code>" in html


def test_markdown_table_cell_keeps_escaped_pipe():
    # A literal pipe in a cell is written \| and must survive as one cell.
    html = web._render_markdown(
        "| Opt | Values |\n| --- | --- |\n| band | 2.4\\|5\\|any |\n")
    assert "2.4|5|any" in html
    # one body cell for the value, not three
    assert html.count('data-label="Values"') == 1


def test_markdown_escapes_html_and_blocks_bad_links():
    html = web._render_markdown(
        "A <script>alert(1)</script> tag.\n\n[x](javascript:alertxss)\n")
    assert "<script>" not in html and "&lt;script&gt;" in html
    assert "<a " not in html               # javascript: link is not linkified
    assert "javascript:" not in html       # and its URL is dropped, text kept
    assert "<p>x</p>" in html


# -- the "What's new" update notes page / link ------------------------------
def _update_state_with_notes():
    return {
        "installed": "0.4.1", "latest": "0.4.3",
        "update_available": True, "offerable": True,
        "notes_url": "https://example.test/r/0.4.3",
        "notes": [
            {"version": "0.4.3", "date": "2026-01-03",
             "body": "**New**\n\n- add a <b>bold</b> feature & more"},
            {"version": "0.4.2", "date": "2026-01-02", "body": "- fix a thing"},
        ],
    }


def test_update_notes_page_renders_each_release_and_escapes():
    html = web._render_update_notes(_ctl(), _update_state_with_notes())
    assert "What's new" in html
    assert "Combined changes from 0.4.1 to 0.4.3" in html
    assert ">0.4.3 " in html and ">0.4.2 " in html       # both version headings
    assert "add a" in html and "fix a thing" in html
    # release bodies are external text -> escaped, never raw HTML
    assert "<b>bold</b>" not in html and "&lt;b&gt;bold&lt;/b&gt;" in html
    assert "&amp; more" in html


def test_update_notes_page_falls_back_to_github_when_no_notes():
    state = dict(_update_state_with_notes(), notes=[])
    html = web._render_update_notes(_ctl(), state)
    assert "aren't on the device yet" in html
    assert 'href="https://example.test/r/0.4.3"' in html


def test_update_notes_fallback_links_only_https():
    state = dict(_update_state_with_notes(), notes=[],
                 notes_url="javascript:alert(1)")
    html = web._render_update_notes(_ctl(), state)
    assert "javascript:" not in html
    assert "View the latest release" not in html


def test_update_card_shows_whats_new_link(monkeypatch):
    monkeypatch.setattr(web, "_read_update_state", lambda *a, **k:
                        _update_state_with_notes())
    ctl = {"ready": True, "csrf": "t", "qs": "", "enabled": {"updates": True}}
    html = web._update_controls(ctl)
    assert 'href="/update/notes"' in html
    assert "What's new in 0.4.3" in html
    assert 'class="advlink"' in html


def test_update_card_omits_link_without_notes_or_url(monkeypatch):
    bare = {"installed": "0.4.1", "latest": "0.4.3", "update_available": True,
            "offerable": True, "notes": [], "notes_url": ""}
    monkeypatch.setattr(web, "_read_update_state", lambda *a, **k: bare)
    ctl = {"ready": True, "csrf": "t", "qs": "", "enabled": {"updates": True}}
    html = web._update_controls(ctl)
    assert "/update/notes" not in html
    assert "Upgrade to 0.4.3" in html      # the button itself still shows



# -- #4: on_webhook soft timeout -------------------------------------------
def test_webhook_timeout_seconds_parsing():
    assert web.webhook_timeout_seconds({}) == 30                       # default
    assert web.webhook_timeout_seconds({"webhook_timeout_seconds": 5}) == 5
    assert web.webhook_timeout_seconds({"webhook_timeout_seconds": 0}) == 0   # off
    assert web.webhook_timeout_seconds({"webhook_timeout_seconds": "x"}) == 30


def test_run_with_timeout_returns_result_when_fast():
    ok, result = web._run_with_timeout(lambda: 7, 1.0)
    assert ok is True and result == 7


def test_run_with_timeout_reports_an_overrun():
    import time as _time
    ok, result = web._run_with_timeout(lambda: (_time.sleep(1.0), 1)[1], 0.2)
    assert ok is False and result is None


def test_run_with_timeout_reraises_the_callable_error():
    def boom():
        raise ValueError("nope")
    with pytest.raises(ValueError):
        web._run_with_timeout(boom, 1.0)


def test_slow_on_webhook_returns_a_timeout_page(monkeypatch, tmp_path):
    import time as _time
    monkeypatch.setattr(web, "webhook_timeout_seconds", lambda cfg: 0.3)

    def slow(path, request):
        _time.sleep(1.0)
        return "late"

    client, _ = _action_client(
        monkeypatch, tmp_path,
        actions=[{"label": "Go", "path": "view", "method": "get"}], handler=slow)
    resp = client.get("/plugins/demo/view", headers=_basic())
    assert resp.status_code == 504
    assert "Still working" in resp.get_data(as_text=True)


def test_a_stuck_on_webhook_refuses_new_calls_until_it_ends(monkeypatch, tmp_path):
    # A hung plugin must leak one worker thread, not one per request: while the
    # overrun call is still running, later calls get a 503 without starting
    # another; once it ends, the plugin is called normally again.
    monkeypatch.setattr(web, "webhook_timeout_seconds", lambda cfg: 0.2)
    release = threading.Event()
    calls = []

    def handler(path, request):
        calls.append(path)
        if len(calls) == 1:
            release.wait(5)
        return "ok"

    client, _ = _action_client(
        monkeypatch, tmp_path,
        actions=[{"label": "Go", "path": "view", "method": "get"}], handler=handler)
    assert client.get("/plugins/demo/view", headers=_basic()).status_code == 504
    busy = client.get("/plugins/demo/view", headers=_basic())
    assert busy.status_code == 503 and "Still busy" in busy.get_data(as_text=True)
    assert busy.headers.get("Retry-After")
    assert len(calls) == 1                       # refused without calling it
    release.set()
    deadline = time.monotonic() + 5
    while threading.active_count() and time.monotonic() < deadline:
        resp = client.get("/plugins/demo/view", headers=_basic())
        if resp.status_code != 503:
            break
        time.sleep(0.05)
    assert resp.status_code == 200 and len(calls) == 2


def test_on_webhook_runs_with_the_concrete_request(monkeypatch, tmp_path):
    # The handler runs in a worker thread; passing the resolved request (not the
    # Flask proxy) keeps its method/args readable there.
    seen = {}

    def handler(path, request):
        seen["method"] = request.method
        seen["q"] = request.args.get("q")
        return "ok:" + (request.args.get("q") or "")

    client, _ = _action_client(
        monkeypatch, tmp_path,
        actions=[{"label": "Go", "path": "view", "method": "get"}], handler=handler)
    resp = client.get("/plugins/demo/view?q=hi", headers=_basic())
    assert resp.status_code == 200
    assert "ok:hi" in resp.get_data(as_text=True)
    assert seen == {"method": "GET", "q": "hi"}


# -- #6: single-flight system-login verification ---------------------------
def _run_login_burst(monkeypatch, ok):
    import threading as _th
    import time as _time
    calls = []
    gate = _th.Event()

    def fake_check(user, password, **kwargs):
        calls.append((user, password))
        gate.wait(2)
        return {"ok": ok}

    monkeypatch.setattr(web, "_auth_check", fake_check)
    web._auth_cache.clear()
    web._auth_inflight.clear()
    results = []

    def run():
        results.append(web._verify_system_login("pi", "pw"))

    threads = [_th.Thread(target=run) for _ in range(5)]
    for t in threads:
        t.start()
    _time.sleep(0.3)        # let all five reach the in-flight wait
    gate.set()
    for t in threads:
        t.join(3)
    return calls, results


def test_system_login_single_flights_a_burst(monkeypatch):
    calls, results = _run_login_burst(monkeypatch, ok=True)
    assert results == [True] * 5
    assert len(calls) == 1          # one verification served the whole burst


def test_system_login_single_flight_propagates_a_failure(monkeypatch):
    calls, results = _run_login_burst(monkeypatch, ok=False)
    assert results == [False] * 5
    assert len(calls) == 1


# -- #7: reconnect poll on the reboot / update "applying" pages ------------
_CTL = {"qs": ""}


def test_reboot_done_page_polls_to_reconnect():
    html = web._render_power_done("reboot", _CTL, 1234.0)
    assert "Rebooting" in html
    assert "/status.json" in html and "id=\"wait\"" in html
    assert "fetch(" in html         # it polls for the device to come back


def test_shutdown_done_page_does_not_poll():
    html = web._render_power_done("shutdown", _CTL, 1234.0)
    assert "Shutting down" in html
    assert "/status.json" not in html      # device stays down: no reconnect


def test_update_done_page_polls_to_reconnect():
    html = web._render_update_done({"latest": "0.9.9"}, _CTL, 1234.0)
    assert "Upgrading" in html
    assert "/status.json" in html and "fetch(" in html


def test_reconnect_script_waits_for_a_fresh_daemon():
    script = web._reconnect_script("/", "/status.json", 1000.0)
    # Redirects only when a different (fresh) daemon answers.
    assert "s.started!==started" in script
    assert "location.href=target" in script


def test_reconnect_script_waits_about_two_minutes_by_default():
    # A restart's bounded plugin unload (25s) plus a cold start must fit the
    # default window, so the page doesn't give up on a restart in progress.
    assert "max=120," in web._reconnect_script("/", "/status.json", 1000.0)


def test_web_connections_time_out_on_a_stalled_request(monkeypatch):
    # A client that goes silent mid-request (a phone that left range, no FIN)
    # must not pin its server thread forever: the server closes it.
    import socket

    pytest.importorskip("werkzeug")
    from flask import Flask

    monkeypatch.setattr(web, "_CONNECTION_TIMEOUT", 0.5)
    app = Flask("t")
    app.add_url_rule("/", "i", lambda: "ok")
    server = web._default_make_server("127.0.0.1", 0, app)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        conn = socket.create_connection(("127.0.0.1", server.server_port), timeout=5)
        with conn:
            # Headers never finished: without the timeout the server thread
            # waits in recv() for the rest, and this recv() times out instead.
            conn.sendall(b"GET / HTTP/1.1\r\nHost: x\r\n")
            started = time.monotonic()
            while conn.recv(4096):
                pass
            assert time.monotonic() - started < 3
    finally:
        server.shutdown()
