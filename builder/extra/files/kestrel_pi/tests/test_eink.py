"""Tests for the Phase 5 e-ink face plugin.

The mood ladder and the SVG-glyph flattener are pure and tested directly. The
plugin itself is exercised without hardware: the Waveshare driver import fails
on the test host (no SPI/GPIO), so the plugin degrades to the web mirror, which
is exactly the HAT-less path we want to prove. PIL (python3-pil) is in the image
and on the test host, so frame composition runs for real."""

import importlib.util
import logging
import os
import sys
import threading
import time
import types

from kestrel_pi.capture import CaptureInterface
from kestrel_pi.connectivity import ConnectivityState
from kestrel_pi.context import PluginContext, ServiceRegistry
from kestrel_pi.loader import Loader
from kestrel_pi.plugins import PluginPage
from kestrel_pi.ui import StubUI, UIState

PLUGIN_DIR = os.path.normpath(
    os.path.join(os.path.dirname(__file__), "..", "..", "plugins")
)


def _mod():
    path = os.path.join(PLUGIN_DIR, "eink.py")
    spec = importlib.util.spec_from_file_location("eink_under_test", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


EINK = _mod()


def _ctx(tmp_path, options=None):
    return PluginContext(
        name="eink", options=options or {},
        capture=CaptureInterface(str(tmp_path)),
        connectivity=types.SimpleNamespace(online=False),
        services=ServiceRegistry(), capabilities=("ui", "web"),
    )


def _plugin(tmp_path, options=None):
    """A plugin instance wired with a context but WITHOUT starting the render
    worker thread (so tests drive composition deterministically)."""
    p = EINK.EinkFace()
    p.options = options or {}
    p.context = _ctx(tmp_path, options)
    p._setup()
    return p


# --- the mood ladder (pure) ------------------------------------------------
def _M(**kw):
    return EINK.MoodInputs(**kw)


def test_mood_priority_ladder():
    d = EINK.decide_mood
    # fault wins over everything below it
    assert d(_M(fault=True, battery_low=True, just_promoted=True)) == "broken"
    assert d(_M(battery_low=True, just_promoted=True)) == "tired"
    assert d(_M(just_promoted=True, fresh_handshake=True)) == "levelup"
    assert d(_M(fresh_handshake=True, capture_active=True,
               learned_active=True, reward_trend=1)) == "excited"


def test_mood_agent_postures_are_honest():
    d = EINK.decide_mood
    # "smart" only under a real learned policy; reward trend gives hysteresis.
    assert d(_M(capture_active=True, learned_active=True, reward_trend=0)) == "smart"
    assert d(_M(capture_active=True, learned_active=True, reward_trend=1)) == "motivated"
    assert d(_M(capture_active=True, learned_active=True, reward_trend=-1)) == "determined"
    # scripted baseline never claims "smart"; it shows plainer posture faces.
    assert d(_M(capture_active=True, posture="autohunt")) == "cool"
    assert d(_M(capture_active=True, posture="pin-channel", transmit=True)) == "intense"
    assert d(_M(capture_active=True, posture="sweep-band", look_toggle=False)) == "look_r"
    assert d(_M(capture_active=True, posture="sweep-band", look_toggle=True)) == "look_l"


def test_mood_mobility_and_idle():
    d = EINK.decide_mood
    assert d(_M(capture_active=True, mobility="driving")) == "excited"
    assert d(_M(capture_active=True, mobility="walking")) == "excited"
    assert d(_M(capture_active=True, mobility="parked")) == "focused"
    # capture on with nothing else going on falls through to the awake base
    assert d(_M(capture_active=True)) == "awake"


def test_mood_sleeps_when_capture_disabled():
    d = EINK.decide_mood
    # capture off: the device rests with a sleeping face instead of looking busy
    assert d(_M(capture_active=False)) == "sleep"
    # ...and dozes deeper the longer it has been idle
    assert d(_M(capture_active=False, idle_epochs=30, sad_after=25)) == "sleep2"
    assert d(_M(capture_active=False, idle_epochs=5, sad_after=25)) == "sleep"
    # a real event still overrides a sleeping device (the face never lies)
    assert d(_M(capture_active=False, fault=True)) == "broken"
    assert d(_M(capture_active=False, battery_low=True)) == "tired"
    assert d(_M(capture_active=False, just_promoted=True)) == "levelup"
    assert d(_M(capture_active=False, fresh_handshake=True)) == "excited"
    # the sleeping faces carry their own (sleep-themed) voice phrases
    assert EINK._voice_phrases("sleep", {}) and EINK._voice_phrases("sleep2", {})


def test_every_mood_key_has_a_face():
    moods = set()
    for kw in [dict(fault=True), dict(battery_low=True), dict(just_promoted=True),
               dict(fresh_handshake=True),
               dict(capture_active=True, learned_active=True, reward_trend=1),
               dict(capture_active=True, learned_active=True, reward_trend=-1),
               dict(capture_active=True, learned_active=True, reward_trend=0),
               dict(capture_active=True, posture="autohunt"),
               dict(capture_active=True, posture="pin-channel", transmit=True),
               dict(capture_active=True, posture="sweep-band"),
               dict(capture_active=True, mobility="parked"),
               dict(capture_active=True, mobility="walking"),
               dict(capture_active=True, mobility="driving"),
               dict(idle_epochs=30, sad_after=25),
               dict(idle_epochs=18, bored_after=15, sad_after=25), dict()]:
        moods.add(EINK.decide_mood(_M(**kw)))
    assert moods <= set(EINK.FACES), moods - set(EINK.FACES)


# --- the Kestrel glyph (SVG path -> polygon) -------------------------------
def test_kestrel_paths_flatten_to_closed_in_box_polygons():
    for d in (EINK._KESTREL_HOVER, EINK._KESTREL_STOOP):
        pts = EINK._flatten_svg_path(d)
        assert len(pts) > 20
        assert pts[0] == pts[-1], "path not closed"
        assert all(0 <= x <= 64 and 0 <= y <= 64 for x, y in pts)


# --- the plugin, HAT-less --------------------------------------------------
def test_plugin_loads_and_degrades_without_a_hat(tmp_path):
    cfg = {"plugins": {"eink": {"enabled": True}}}
    loader = Loader(
        config=cfg, capture=CaptureInterface(str(tmp_path)),
        connectivity=ConnectivityState(), services=ServiceRegistry(),
        search_paths=[PLUGIN_DIR], logger=logging.getLogger("test.eink"))
    loaded = loader.load_all()
    try:
        assert [r.name for r in loaded] == ["eink"]
        rec = loaded[0]
        assert rec.state == "loaded"
        assert rec.instance.__actions__[0]["path"] == "display"
        assert set(rec.capabilities) == {"ui", "web"}
    finally:
        for r in loaded:
            loader.unload(r)


def test_compose_runs_with_no_panel(tmp_path):
    p = _plugin(tmp_path)
    assert p._absent is True                     # no driver import on the host
    img = p._compose_image(UIState(online=True, capture_active=True, epoch=2),
                           "focused")
    assert img.size == (EINK.PANEL_W, EINK.PANEL_H)
    # rotation expands the canvas
    p.context.options = dict(p.context.options, rotation=90)
    assert p._compose_image(UIState(), "awake").size == (EINK.PANEL_H, EINK.PANEL_W)


def test_compose_renders_the_rich_top_bar(tmp_path):
    # BT/Wi-Fi link glyphs + battery on the top strip, driven by UIState; it must
    # render without a panel and whatever the link/battery state is.
    p = _plugin(tmp_path)
    full = UIState(online=True, capture_active=True,
                   paths={"bnep0": True, "wlan0": False},
                   battery={"percent": 77, "status": "discharging"},
                   agent={"running": True, "policy": "RecurrentPPO"})
    assert p._compose_image(full, "motivated").size == (EINK.PANEL_W, EINK.PANEL_H)
    # no battery data and both links down still composes (symbols just degrade)
    assert p._compose_image(UIState(), "awake").size == (EINK.PANEL_W, EINK.PANEL_H)


def test_link_glyphs_follow_the_same_addresses_as_the_address_row(tmp_path):
    # At boot the daemon's paths can still say "no link" after the links have
    # their addresses; the top strip must follow the addresses the panel shows.
    p = _plugin(tmp_path)
    strip = (0, 0, 50, 22)                            # the BT and Wi-Fi glyphs
    p._addresses = lambda: [("bnep0", "192.0.2.10"), ("wlan0", "198.51.100.7")]
    stale = p._compose_image(UIState(paths={"bnep0": False, "wlan0": False}), "awake")
    fresh = p._compose_image(UIState(paths={"bnep0": True, "wlan0": True}), "awake")
    assert stale.crop(strip).tobytes() == fresh.crop(strip).tobytes()
    p._addresses = lambda: []
    down = p._compose_image(UIState(paths={"bnep0": True, "wlan0": True}), "awake")
    assert down.crop(strip).tobytes() != fresh.crop(strip).tobytes()
    p._addresses = lambda: [("wlan0", "198.51.100.7")]
    wifi_only = p._compose_image(UIState(), "awake")
    assert wifi_only.crop(strip).tobytes() not in (fresh.crop(strip).tobytes(),
                                                   down.crop(strip).tobytes())


def test_compose_shows_gps_only_when_a_receiver_is_present(tmp_path):
    # The GPS satellite + signal glyph is gated on the `position` service being
    # registered, not just on a fix: present-and-searching, present-and-locked,
    # and absent all compose cleanly.
    p = _plugin(tmp_path)
    p.context.services.provide("position", lambda: None)   # present, searching
    searching = UIState(online=True, position=None)
    assert p._compose_image(searching, "awake").size == (EINK.PANEL_W, EINK.PANEL_H)
    locked = UIState(online=True,
                     position={"lat": 1.0, "lon": 2.0, "fix": 3, "cn0": 38.0})
    assert p._compose_image(locked, "motivated").size == (EINK.PANEL_W, EINK.PANEL_H)


def test_signal_level_tracks_cn0_only_when_locked():
    assert EINK._signal_level(None, True) == 0
    assert EINK._signal_level(40.0, False) == 0      # no fix -> no bars
    assert EINK._signal_level(26.0, True) == 1
    assert EINK._signal_level(31.0, True) == 2
    assert EINK._signal_level(36.0, True) == 3


def test_agent_label_is_honest(tmp_path):
    assert EINK._agent_label(None) == "off"
    assert EINK._agent_label({"running": False}) == "off"
    assert EINK._agent_label({"running": True, "policy": "baseline"}) == "baseline"
    assert EINK._agent_label({"running": True, "policy": "RecurrentPPO"}) == "learned"


def test_view_button_is_declared(tmp_path):
    assert EINK.EinkFace.__actions__[0]["label"] == "View"
    assert EINK.EinkFace.__actions__[0]["path"] == "display"


def test_every_mood_has_a_voice():
    # Every face the ladder can return must have a matching phrase set, so the
    # mascot never "speaks" a fallback that mismatches its face.
    for mood in EINK.FACES:
        assert EINK._voice_phrases(mood, None), mood


def test_voice_matches_mood_and_rotates(tmp_path):
    p = _plugin(tmp_path)
    # A phrase for the mood, and advancing the rotation picks the next phrase
    # (wrapping), so the words vary over time while always belonging to the mood.
    phrases = EINK._VOICES["cool"]
    assert p._voice_for("cool", 0) == phrases[0]
    assert p._voice_for("cool", 1) == phrases[1 % len(phrases)]
    assert p._voice_for("cool", len(phrases)) == phrases[0]


def test_voice_override_from_options(tmp_path):
    p = _plugin(tmp_path, options={"voices": {"awake": "ready!",
                                              "cool": ["a", "b"]}})
    assert p._voice_for("awake", 5) == "ready!"          # single str accepted
    assert p._voice_for("cool", 0) == "a"
    assert p._voice_for("cool", 1) == "b"


def test_voice_rotates_only_on_mood_change(tmp_path):
    # The phrase must stay put while the mood holds (so the e-ink frame does not
    # redraw just to reword) and advance when the mood changes.
    p = _plugin(tmp_path)
    p._state = UIState(online=True)                      # stable -> "awake"
    p._tick()
    first = p._voice
    p._tick()                                            # same mood
    assert p._voice == first
    seq = p._frame_seq
    p._tick()
    assert p._frame_seq == seq                           # no redraw while static
    p._mood = "cool"                                     # force a mood change
    p._tick()
    assert p._voice in EINK._voice_phrases(p._mood, None)


def test_fmt_uptime():
    assert EINK._fmt_uptime(None) is None
    assert EINK._fmt_uptime(-1) is None
    # Minute granularity on purpose (seconds would redraw the panel every cycle).
    assert EINK._fmt_uptime(0) == "0:00"
    assert EINK._fmt_uptime(5025) == "1:23"              # 1h23m45s -> 1:23
    assert EINK._fmt_uptime(90061) == "25:01"            # days fold into hours


def test_compose_draws_voice_uptime_channel_and_aps(tmp_path):
    # The new readouts (voice beside the face, UP/CH info row, PWND/AP counters)
    # all compose at panel size, with and without the data present.
    p = _plugin(tmp_path)
    full = UIState(online=True, capture_active=True,
                   uptime=5025.0, channel="1,6,11",
                   capture_total=1240, capture_session=30,
                   aps_total=85, aps_session=12, networks_total=52)
    img = p._compose_image(full, "motivated", "on a roll!")
    assert img.size == (EINK.PANEL_W, EINK.PANEL_H)
    # A pathologically large trio still composes (the counter row stops before
    # the brand mark rather than overlapping it).
    huge = UIState(online=True, capture_active=True, capture_total=999999,
                   capture_session=9999, aps_total=99999, networks_total=99999)
    assert p._compose_image(huge, "awake", None).size == (EINK.PANEL_W, EINK.PANEL_H)
    # None of the fields present (no uptime/channel/aps, no voice) still composes.
    assert p._compose_image(UIState(), "awake", None).size == (EINK.PANEL_W, EINK.PANEL_H)


def test_compose_draws_the_near_segment_when_scanning(tmp_path):
    # The NEAR density readout composes when env is available-and-scanning, and
    # degrades cleanly when env is absent (capture off) or scanning-without-data.
    p = _plugin(tmp_path)
    scanning = UIState(online=True, capture_active=True, channel="1,6,11",
                       env={"available": True, "level": 3, "radios_recent": 23,
                            "label": "Busy"})
    assert p._compose_image(scanning, "focused").size == (EINK.PANEL_W, EINK.PANEL_H)
    # capture off: env None -> no NEAR, still composes
    assert p._compose_image(UIState(online=True), "awake").size == (EINK.PANEL_W, EINK.PANEL_H)
    # scanning but no reading yet: env not available -> no NEAR
    noyet = UIState(online=True, capture_active=True,
                    env={"available": False})
    assert p._compose_image(noyet, "focused").size == (EINK.PANEL_W, EINK.PANEL_H)


def test_density_meter_fills_more_bars_at_higher_levels():
    from PIL import Image, ImageDraw
    def inked(level):
        img = Image.new("1", (30, 20), 255)
        EINK._draw_density(ImageDraw.Draw(img), 2, 4, 18, 11, level)
        return img.histogram()[0]   # count of black (0) pixels
    # a denser environment fills more of the four bars, so more ink
    assert inked(0) < inked(2) < inked(4)


def test_mirror_page_back_link_is_at_the_top(tmp_path):
    # Consistency with the other plugin pages: the "Plugins" back link comes
    # before the heading, not at the bottom of the page.
    p = _plugin(tmp_path)
    page = p._mirror_page()
    assert '<a href="/plugins">' in page.body
    assert page.body.index('href="/plugins"') < page.body.index("<h1>")


def test_wrap_text_respects_width(tmp_path):
    from PIL import Image, ImageDraw
    draw = ImageDraw.Draw(Image.new("1", (10, 10), 255))
    font = EINK._font(11)
    lines = EINK._wrap_text(draw, "so many signals here", font, 60)
    assert len(lines) >= 2
    assert all(isinstance(ln, str) and ln for ln in lines)
    # A single over-wide word is never split, it just sits on its own line.
    assert EINK._wrap_text(draw, "supercalifragilistic", font, 10) == \
        ["supercalifragilistic"]


def test_ui_card_reports_cached_mood_and_panel_state(tmp_path):
    p = _plugin(tmp_path)
    p._mood = "cool"
    p._voice = "like a boss"
    ui = StubUI()
    view = ui.for_plugin("eink")
    p.on_ui_setup(view)
    p.on_ui_update(view, UIState(online=True, capture_active=True))
    card = {e["name"]: e for e in ui.widgets()["eink"]}
    assert card["face"]["value"] == EINK.FACES["cool"]
    assert card["mood"]["value"] == "cool"
    assert card["voice"]["value"] == "like a boss"
    assert card["panel"]["value"] == "not detected"


# --- the live mirror (no drift) -------------------------------------------
def test_mirror_png_is_served_with_an_etag_and_honours_if_none_match(tmp_path):
    p = _plugin(tmp_path)
    p._tick()                                    # compose one frame
    body, status, headers = p._mirror_png(
        types.SimpleNamespace(headers={}))
    assert status == 200
    assert headers["Content-Type"] == "image/png"
    assert body[:4] == b"\x89PNG"
    etag = headers["ETag"]
    # a second fetch with the matching tag is a 304 (the mirror reuses one frame)
    body2, status2, _ = p._mirror_png(
        types.SimpleNamespace(headers={"If-None-Match": etag}))
    assert status2 == 304 and body2 == ""


def test_mirror_page_links_the_png_and_reloads_at_refresh_cadence(tmp_path):
    p = _plugin(tmp_path, options={"refresh_seconds": 20})
    page = p.on_webhook("display", types.SimpleNamespace(headers={}))
    assert isinstance(page, PluginPage)
    assert "display.png" in page.body
    assert "20000" in page.body                  # setTimeout in ms at the cadence


def test_frame_seq_only_advances_when_the_frame_changes(tmp_path):
    p = _plugin(tmp_path)
    p._tick()
    seq1 = p._frame_seq
    p._tick()                                    # identical state -> no new frame
    assert p._frame_seq == seq1


# --- mobility hysteresis ---------------------------------------------------
def test_classify_speed_thresholds():
    assert EINK._classify_speed(0.0) == "parked"
    assert EINK._classify_speed(0.5) == "parked"
    assert EINK._classify_speed(0.75) is None          # parked/walking deadband
    assert EINK._classify_speed(1.4) == "walking"
    assert EINK._classify_speed(3.0) == "walking"
    assert EINK._classify_speed(4.0) == "driving"


def test_mobility_classifies_parked_walking_driving_with_hysteresis(tmp_path):
    p = _plugin(tmp_path)
    assert p._mobility_state(None) == "unknown"
    # ~16 m/s between fixes 10s apart -> driving, but needs sustain = 2
    p._mobility_state({"lat": 0.0, "lon": 0.0, "ts": 0.0})
    one = p._mobility_state({"lat": 0.0015, "lon": 0.0, "ts": 10.0})
    assert one != "driving"
    assert p._mobility_state({"lat": 0.0030, "lon": 0.0, "ts": 20.0}) == "driving"
    # a stationary stretch settles to parked after the sustain count
    p._mobility_state({"lat": 0.0030, "lon": 0.0, "ts": 30.0})
    assert p._mobility_state({"lat": 0.0030, "lon": 0.0, "ts": 40.0}) == "parked"
    # a walking pace (~1.4 m/s over 10s) reads as walking
    q = _plugin(tmp_path)
    q._mobility_state({"lat": 0.0, "lon": 0.0, "ts": 0.0})
    q._mobility_state({"lat": 0.00013, "lon": 0.0, "ts": 10.0})
    assert q._mobility_state({"lat": 0.00026, "lon": 0.0, "ts": 20.0}) == "walking"
    # losing the fix resets to unknown
    assert p._mobility_state(None) == "unknown"


def test_compose_draws_the_capture_counter(tmp_path):
    # lifetime + session counts from UIState render without a panel; absent
    # counts (None) just omit the counter.
    p = _plugin(tmp_path)
    withcount = UIState(online=True, capture_active=True,
                        capture_total=1240, capture_session=30)
    assert p._compose_image(withcount, "motivated").size == (EINK.PANEL_W, EINK.PANEL_H)
    assert p._compose_image(UIState(), "awake").size == (EINK.PANEL_W, EINK.PANEL_H)


def test_face_overrides_from_config(tmp_path):
    p = _plugin(tmp_path, options={"faces": {"awake": ":-)"}})
    assert p._face_glyph("awake") == ":-)"          # overridden
    assert p._face_glyph("cool") == EINK.FACES["cool"]  # untouched falls back


def test_battery_low_only_while_discharging(tmp_path):
    p = _plugin(tmp_path, options={"battery_low_percent": 10})
    # at/below the floor while on battery -> tired; charging at the same charge
    # is not (it is not in trouble), so the face does not lie.
    assert p._current_mood(
        UIState(battery={"percent": 5, "status": "discharging"})) == "tired"
    assert p._current_mood(
        UIState(battery={"percent": 5, "status": "charging"})) != "tired"


# --- variant auto-probe and fallback --------------------------------------
def _install_fake_panels(monkeypatch, v2="ok", v1="ok"):
    """Stand fake Waveshare driver + epdconfig helpers in for the plugin's
    helper files (through its load_helper) so _ensure_panel's
    probe/fallback can be exercised with no hardware. Each driver behaves:
    "ok" (a present panel: init drives the BUSY line, which the probe reads as
    asserted and keeps the driver), "phantom" (init()+Clear() return at once but
    the BUSY line is never driven - a HAT-less or UPS-HAT-only device, which the
    probe must read as absent), "hang" (init polls BUSY forever -> the plugin's
    deadline guard must turn it into a timeout) or None (init raises).

    cfg.level is the level the (pulled-down) BUSY line reads: 0 until a present
    panel drives it to 1, which is exactly how the plugin tells a real panel
    from a header sitting at its pull-down."""
    cfg = types.ModuleType("epdconfig")
    cfg.BUSY_PIN = 24
    cfg.level = 0
    cfg.digital_read = lambda pin: cfg.level
    state = {"constructed": [], "exited": 0}
    cfg.module_exit = lambda *a, **k: state.__setitem__("exited", state["exited"] + 1)

    def make(name, behavior):
        mod = types.ModuleType(name)

        class EPD:
            VARIANT = name

            def __init__(self):
                state["constructed"].append(name)
                cfg.level = 0            # fresh attempt: line back at pull-down

            def init(self):
                if behavior is None:
                    raise RuntimeError("no panel")
                if behavior == "hang":
                    # Mismatched driver: the BUSY line never reads idle, so the
                    # guarded digital_read raises TimeoutError once the deadline
                    # passes (mirrors the real V2-on-V1 infinite busy loop).
                    while cfg.digital_read(cfg.BUSY_PIN) != 999:
                        time.sleep(0.003)
                elif behavior == "phantom":
                    # init completes but the BUSY line is never driven (reads the
                    # pull-down): no panel is actually wired.
                    cfg.digital_read(cfg.BUSY_PIN)
                else:
                    # A present panel drives BUSY while the controller works.
                    cfg.level = 1
                    cfg.digital_read(cfg.BUSY_PIN)
                return 0

            def Clear(self):
                # a present panel also drives BUSY through the Clear busy-wait
                if behavior not in (None, "hang", "phantom"):
                    cfg.level = 1
                    cfg.digital_read(cfg.BUSY_PIN)

            def sleep(self):
                pass

        mod.EPD = EPD
        return mod

    helpers = {"_eink_epdconfig.py": cfg,
               "_eink_epd2in7_V2.py": make("epd2in7_V2", v2),
               "_eink_epd2in7.py": make("epd2in7", v1)}
    monkeypatch.setattr(EINK, "load_helper", lambda plugin_file, filename: helpers[filename])
    return state


def test_auto_falls_back_to_v1_when_v2_hangs(tmp_path, monkeypatch):
    state = _install_fake_panels(monkeypatch, v2="hang", v1="ok")
    p = _plugin(tmp_path, {"variant": "auto"})
    p._PROBE_TIMEOUT = 0.05
    epd = p._ensure_panel()
    assert epd is not None and epd.VARIANT == "epd2in7"
    assert p._absent is False
    # tried V2 first, then fell back to V1; the hung V2 attempt closed SPI
    assert state["constructed"] == ["epd2in7_V2", "epd2in7"]
    assert state["exited"] >= 1


def test_auto_keeps_v2_when_it_initialises(tmp_path, monkeypatch):
    state = _install_fake_panels(monkeypatch, v2="ok", v1="ok")
    p = _plugin(tmp_path, {"variant": "auto"})
    epd = p._ensure_panel()
    assert epd is not None and epd.VARIANT == "epd2in7_V2"
    assert p._absent is False
    assert state["constructed"] == ["epd2in7_V2"]  # V1 never tried


def test_forced_variant_does_not_fall_back(tmp_path, monkeypatch):
    state = _install_fake_panels(monkeypatch, v2="hang", v1="ok")
    p = _plugin(tmp_path, {"variant": "v2"})
    p._FORCED_TIMEOUT = 0.05
    epd = p._ensure_panel()
    assert epd is None  # forced v2 hung: no silent fall back to the other driver
    assert p._absent is True
    assert state["constructed"] == ["epd2in7_V2"]


def test_forced_v1_selects_v1(tmp_path, monkeypatch):
    state = _install_fake_panels(monkeypatch, v2="ok", v1="ok")
    p = _plugin(tmp_path, {"variant": "v1"})
    epd = p._ensure_panel()
    assert epd is not None and epd.VARIANT == "epd2in7"
    assert state["constructed"] == ["epd2in7"]


def test_on_unload_blanks_panel_after_stopping_worker(tmp_path):
    # On shutdown the panel must be cleared so no stale face is frozen on the
    # e-paper, and the render worker must be stopped and joined first so an
    # in-flight refresh can't race or follow the Clear.
    p = _plugin(tmp_path)
    calls = []

    class _FakeEpd:
        def Clear(self):
            calls.append("Clear")

        def sleep(self):
            calls.append("sleep")

    p._epd = _FakeEpd()

    def _loop():
        while not p._stop.is_set():
            p._stop.wait(0.01)

    p._worker = threading.Thread(target=_loop, name="eink-face-test", daemon=True)
    p._worker.start()

    p.on_unload()

    assert p._stop.is_set()
    assert not p._worker.is_alive()        # joined before blanking
    assert calls == ["Clear", "sleep"]     # panel blanked, then put to sleep


def test_on_unload_is_safe_with_no_panel(tmp_path):
    # No HAT (the common case, since the plugin ships disabled and is optional):
    # unload must stop cleanly and never touch a panel that was never brought up.
    p = _plugin(tmp_path)
    assert p._epd is None
    p.on_unload()                           # must not raise
    assert p._stop.is_set()


def test_auto_reports_absent_when_no_panel_initialises(tmp_path, monkeypatch):
    _install_fake_panels(monkeypatch, v2=None, v1=None)
    p = _plugin(tmp_path, {"variant": "auto"})
    p._PROBE_TIMEOUT = 0.05
    assert p._ensure_panel() is None
    assert p._absent is True


def test_auto_reports_absent_when_busy_never_driven(tmp_path, monkeypatch):
    # A HAT-less device - or a different HAT such as a UPS HAT - leaves the BUSY
    # line at the header pull-down, so the V2 busy-wait returns at once and
    # init()+Clear() complete even though no panel is wired. The probe must not
    # be fooled: with the line never driven high, the plugin reports absent
    # rather than mistaking the other hardware for a panel.
    state = _install_fake_panels(monkeypatch, v2="phantom", v1="phantom")
    p = _plugin(tmp_path, {"variant": "auto"})
    p._PROBE_TIMEOUT = 0.05
    assert p._ensure_panel() is None
    assert p._absent is True
    # both drivers were tried and each closed SPI after its phantom init
    assert state["constructed"] == ["epd2in7_V2", "epd2in7"]
    assert state["exited"] >= 2


def test_forced_v2_reports_absent_when_busy_never_driven(tmp_path, monkeypatch):
    # The same guard holds for a forced variant: a pulled-low BUSY is not a panel.
    _install_fake_panels(monkeypatch, v2="phantom")
    p = _plugin(tmp_path, {"variant": "v2"})
    p._FORCED_TIMEOUT = 0.05
    assert p._ensure_panel() is None
    assert p._absent is True


# --- the address row ----------------------------------------------------------

_IP_BAND = (0, 122, EINK.PANEL_W, 140)   # between the info row and the counters


def _ink(img, box):
    """Black pixels inside ``box`` (x0, y0, x1, y1)."""
    return sum(1 for px in img.crop(box).getdata() if px == 0)


def test_ip_row_shows_each_link_address(tmp_path):
    p = _plugin(tmp_path)
    p._addresses = lambda: [("bnep0", "192.0.2.10"), ("wlan0", "198.51.100.7")]
    img = p._compose_image(UIState(), "awake")
    assert _ink(img, (0, 122, EINK.PANEL_W // 2, 140)) > 0       # the first link
    assert _ink(img, (EINK.PANEL_W // 2, 122, EINK.PANEL_W, 140)) > 0   # the second


def test_ip_row_hidden_by_the_option_or_without_an_address(tmp_path):
    p = _plugin(tmp_path, {"show_ip": False})
    p._addresses = lambda: [("bnep0", "192.0.2.10")]
    assert _ink(p._compose_image(UIState(), "awake"), _IP_BAND) == 0
    q = _plugin(tmp_path)
    q._addresses = lambda: []
    assert _ink(q._compose_image(UIState(), "awake"), _IP_BAND) == 0


def test_ip_row_drops_an_address_that_would_not_fit(tmp_path):
    p = _plugin(tmp_path)
    p._addresses = lambda: [("bnep0", "198.51.100.254"), ("wlan0", "203.0.113.254"),
                            ("wlan0", "192.0.2.254")]
    img = p._compose_image(UIState(), "awake")
    assert _ink(img, (EINK.PANEL_W - 8, 122, EINK.PANEL_W, 140)) == 0


def test_addresses_lists_only_links_with_an_address(tmp_path, monkeypatch):
    monkeypatch.setattr(EINK, "_iface_ipv4", lambda iface: {"wlan0": "192.0.2.20"}.get(iface))
    assert _plugin(tmp_path)._addresses() == [("wlan0", "192.0.2.20")]


def test_iface_ipv4_is_none_for_a_missing_interface():
    assert EINK._iface_ipv4("kestrel-none0") is None


# --- the HTTPS fingerprint after start-up -----------------------------------------

_FP = ":".join(["AB", "CD", "EF", "12", "34", "56", "78", "9A"] + ["00"] * 24)


def test_fingerprint_shows_in_the_first_minutes(tmp_path, monkeypatch):
    monkeypatch.setattr(EINK.webtls, "fingerprint", lambda *a: _FP)
    p = _plugin(tmp_path)
    assert p._fingerprint_lines() == ["HTTPS key", "AB:CD:EF:12", "34:56:78:9A"]
    voiced = p._compose_image(UIState(), "awake", "on a roll!")
    p._started -= 11 * 60                       # past the default 10 minutes
    assert p._fingerprint_lines() == []
    later = p._compose_image(UIState(), "awake", "on a roll!")
    assert list(voiced.getdata()) != list(later.getdata())   # the voice is back


def test_fingerprint_off_or_absent(tmp_path, monkeypatch):
    monkeypatch.setattr(EINK.webtls, "fingerprint", lambda *a: _FP)
    assert _plugin(tmp_path, {"show_fingerprint_minutes": 0})._fingerprint_lines() == []
    monkeypatch.setattr(EINK.webtls, "fingerprint", lambda *a: None)
    assert _plugin(tmp_path)._fingerprint_lines() == []

# --- the HAT keys (HatKeys; their own tests are in test_hatkeys.py) -------

def _with_buttons(p, state):
    """Stand in for the plugin's HatKeys with a fixed key state."""
    p._keys = types.SimpleNamespace(state=lambda: dict(state), last="none yet",
                                    set_capturing=lambda active: None, stop=lambda: None)
    return p


def test_info_screen_replaces_the_face(tmp_path, monkeypatch):
    monkeypatch.setattr(EINK, "VERSION_FILE", str(tmp_path / "version"))
    (tmp_path / "version").write_text("1.2.3\n")
    p = _plugin(tmp_path)
    p._addresses = lambda: [("bnep0", "192.0.2.10")]
    face = p._compose_image(UIState(uptime=600.0), "awake", "on a roll!")
    _with_buttons(p, {"seq": 1, "screen": "info", "prompt": None})
    info = p._compose_image(UIState(uptime=600.0), "awake", "on a roll!")
    assert info.size == (EINK.PANEL_W, EINK.PANEL_H)
    assert list(info.getdata()) != list(face.getdata())
    assert EINK._installed_version() == "1.2.3"
    assert p._buttons_seq == 1


def test_a_button_prompt_takes_the_voice_slot(tmp_path):
    p = _plugin(tmp_path)
    plain = p._compose_image(UIState(), "awake", "on a roll!")
    _with_buttons(p, {"seq": 2, "screen": None, "prompt": "Press KEY4 again to shut down"})
    prompted = p._compose_image(UIState(), "awake", "on a roll!")
    assert list(prompted.getdata()) != list(plain.getdata())


def test_no_keys_changes_nothing(tmp_path):
    p = _plugin(tmp_path)
    assert p._buttons_state() == {}
    assert EINK._free_disk_text(str(tmp_path)).endswith("GB free")


def test_a_key_press_wakes_the_render_loop(tmp_path, monkeypatch):
    p = _plugin(tmp_path)
    p.context.options = dict(p.context.options, refresh_seconds=60)
    state = {"seq": 0, "screen": None, "prompt": None}
    _with_buttons(p, state)
    ticks = []
    monkeypatch.setattr(p, "_tick", lambda: ticks.append(time.monotonic())
                        or setattr(p, "_buttons_seq", state["seq"]))
    worker = threading.Thread(target=p._run, daemon=True)
    worker.start()
    deadline = time.monotonic() + 3
    while len(ticks) < 1 and time.monotonic() < deadline:
        time.sleep(0.02)
    state["seq"] = 1                           # a key press
    deadline = time.monotonic() + 3
    while len(ticks) < 2 and time.monotonic() < deadline:
        time.sleep(0.02)
    p._stop.set()
    worker.join(timeout=3)
    assert len(ticks) >= 2 and ticks[1] - ticks[0] < 3   # not the 60 s refresh


def test_a_button_prompt_beats_the_fingerprint_which_beats_the_voice(tmp_path, monkeypatch):
    monkeypatch.setattr(EINK.webtls, "fingerprint", lambda *a: _FP)
    p = _plugin(tmp_path)
    fp_frame = p._compose_image(UIState(), "awake", "on a roll!")     # fingerprint shown
    _with_buttons(p, {"seq": 3, "screen": None, "prompt": "Press KEY4 again to shut down"})
    prompt_frame = p._compose_image(UIState(), "awake", "on a roll!")
    assert list(prompt_frame.getdata()) != list(fp_frame.getdata())
    p._started -= 11 * 60                                            # fingerprint window over
    p._keys = None
    voice_frame = p._compose_image(UIState(), "awake", "on a roll!")
    assert list(voice_frame.getdata()) not in (list(fp_frame.getdata()),
                                               list(prompt_frame.getdata()))


# --- the driver ships beside the plugin ----------------------------------------

def test_the_drivers_are_helper_files_beside_the_plugin():
    # Waveshare's files, kept verbatim, as the plugin's own helper files: the
    # loader skips them, and nothing of the e-ink face is left in kestrel_pi.
    for name in ("_eink_epdconfig.py", "_eink_epd2in7.py", "_eink_epd2in7_V2.py"):
        assert os.path.isfile(os.path.join(PLUGIN_DIR, name)), name
    pkg = os.path.join(PLUGIN_DIR, "..", "kestrel_pi", "kestrel_pi")
    assert not os.path.exists(os.path.join(pkg, "vendor"))
    assert not os.path.exists(os.path.join(pkg, "hatkeys.py"))


def test_a_driver_reaches_epdconfig_through_its_relative_import(monkeypatch):
    # The verbatim drivers do ``from . import epdconfig``; as helpers of one
    # plugin they share a package, so the driver finds the epdconfig loaded
    # before it. A fake epdconfig stands in so no GPIO backend starts.
    from kestrel_pi.plugins import HELPERS_PACKAGE
    fake = types.ModuleType("epdconfig")
    fake.EPD_WIDTH = 0
    monkeypatch.setitem(sys.modules, HELPERS_PACKAGE + ".eink.epdconfig", fake)
    name = HELPERS_PACKAGE + ".eink.epd2in7_V2"
    monkeypatch.delitem(sys.modules, name, raising=False)
    from kestrel_pi import load_helper
    drv = load_helper(os.path.join(PLUGIN_DIR, "eink.py"), "_eink_epd2in7_V2.py")
    try:
        assert drv.epdconfig is fake and hasattr(drv, "EPD")
    finally:
        sys.modules.pop(name, None)


# --- motion from the gps plugin's real fix ----------------------------------
# The gps plugin's position has no "ts", only fix_time (UTC, whole seconds), and
# the daemon refreshes UIState.position once per epoch (60 s) while the face
# redraws every 10 s, so the face reads the same snapshot several times in a row.
_METRES_PER_DEG_LAT = 111_320.0
_T0 = 1_700_000_000


def _gps_fix(metres, t):
    """A fix shaped like gps.provide_position(): no ts, a fix_time string."""
    return {"lat": metres / _METRES_PER_DEG_LAT, "lon": 0.0, "alt": None,
            "source": "serial", "fix": 3, "sats": 9, "cn0": 40, "strong": 6,
            "fix_time": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t))}


def test_driving_is_detected_from_epoch_snapshots_of_the_gps_fix(tmp_path):
    p = _plugin(tmp_path)
    for epoch in range(4):                      # 22 m/s, about 50 mph
        fix = _gps_fix(22.0 * 60 * epoch, _T0 + 60 * epoch)
        for _tick in range(6):                  # six 10 s redraws per 60 s epoch
            mobility = p._mobility_state(dict(fix))
    assert mobility == "driving"


def test_walking_is_detected_from_epoch_snapshots_of_the_gps_fix(tmp_path):
    p = _plugin(tmp_path)
    for epoch in range(4):                      # 1.4 m/s
        fix = _gps_fix(1.4 * 60 * epoch, _T0 + 60 * epoch)
        for _tick in range(6):
            mobility = p._mobility_state(dict(fix))
    assert mobility == "walking"


def test_a_parked_device_still_reads_parked(tmp_path):
    p = _plugin(tmp_path)
    for epoch in range(4):                      # a metre of GPS jitter
        fix = _gps_fix(epoch % 2, _T0 + 60 * epoch)
        for _tick in range(6):
            mobility = p._mobility_state(dict(fix))
    assert mobility == "parked"


def test_a_fix_with_no_timestamp_is_not_a_stop_when_read_again(tmp_path, monkeypatch):
    # Neither ts nor fix_time: re-reading the same position 10 s later is the
    # same fix read again, not a stop.
    p = _plugin(tmp_path)
    clock = [1000.0]
    monkeypatch.setattr(EINK.time, "time", lambda: clock[0])
    for step in range(6):                       # a new position every 30 s
        fix = {"lat": 22.0 * 30 * step / _METRES_PER_DEG_LAT, "lon": 0.0}
        for _read in range(3):                  # read every 10 s
            mobility = p._mobility_state(dict(fix))
            clock[0] += 10.0
    assert mobility == "driving"


def test_motion_reads_the_live_fix_before_the_epoch_snapshot(tmp_path):
    p = _plugin(tmp_path)
    live, snapshot = _gps_fix(500.0, _T0 + 10), _gps_fix(0.0, _T0)
    seen = []
    p._mobility_state = lambda fix: seen.append(fix) or "unknown"
    p._current_mood(UIState(position=snapshot))
    p.context.services.provide("position", lambda: live)
    p._current_mood(UIState(position=snapshot))
    p.context.services.provide("position", lambda: None)     # receiver, no fix yet
    p._current_mood(UIState(position=snapshot))
    assert seen == [snapshot, live, snapshot]
