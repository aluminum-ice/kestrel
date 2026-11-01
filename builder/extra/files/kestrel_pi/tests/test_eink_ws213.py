"""Tests for the ``eink-ws213`` plugin: the e-ink face on the Waveshare 2.13"
e-Paper HAT.

The plugin is standalone - its own copy of the face - so it has its own copy
of the face tests (mood ladder, voices, mirror, card, address row,
fingerprint), plus what is particular to it: the lean 250x122 layout, the
partial/full redraw schedule, the panel revisions and their driver helper
files. Fake drivers stand in through the plugin's ``load_helper``, so no SPI or
GPIO is touched; PIL (python3-pil) composes frames for real."""

import importlib.util
import logging
import os
import threading
import time
import types

import pytest

from kestrel_pi.capture import CaptureInterface
from kestrel_pi.connectivity import ConnectivityState
from kestrel_pi.context import PluginContext, ServiceRegistry
from kestrel_pi.declarations import read_declarations
from kestrel_pi.loader import Loader
from kestrel_pi.plugins import PluginPage
from kestrel_pi.ui import StubUI, UIState

PLUGIN_DIR = os.path.normpath(
    os.path.join(os.path.dirname(__file__), "..", "..", "plugins")
)
PLUGIN = os.path.join(PLUGIN_DIR, "eink-ws213.py")


def _mod():
    spec = importlib.util.spec_from_file_location("eink_ws213_under_test", PLUGIN)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


WS = _mod()
FACE = WS.face          # the eink_face library the plugin loads
W, H = WS.PANEL_W, WS.PANEL_H


def test_mirror_page_starts_with_its_heading(tmp_path):
    # The page shell adds "← Plugins · Dashboard" above every plugin page.
    page = _plugin(tmp_path)._mirror_page()
    assert page.body.startswith("<h1>") and "&larr; Plugins" not in page.body


def _plugin(tmp_path, options=None, gps=False):
    """A plugin instance wired with a context but WITHOUT starting the render
    worker thread (so tests drive composition deterministically)."""
    p = WS.EinkWs213()
    p.options = options or {}
    services = ServiceRegistry()
    if gps:
        services.provide("position", lambda: None)
    p.context = PluginContext(
        name="eink-ws213", options=options or {},
        capture=CaptureInterface(str(tmp_path)),
        connectivity=types.SimpleNamespace(online=False),
        services=services, capabilities=("ui", "web"),
    )
    p._setup()
    p._started -= 3600          # past the fingerprint window unless a test wants it
    p._addresses = lambda: []
    return p


def _ink(img, box):
    """Black pixels inside ``box`` (x0, y0, x1, y1)."""
    return sum(1 for px in img.crop(box).getdata() if px == 0)


FULL = UIState(online=True, capture_active=True, uptime=5025.0, channel="1,6,11",
               capture_total=1240, capture_session=30, aps_total=85, networks_total=52,
               battery={"percent": 77, "status": "discharging"},
               agent={"running": True, "policy": "RecurrentPPO"},
               position={"lat": 1.0, "lon": 2.0, "fix": 3, "cn0": 38.0},
               env={"available": True, "level": 3, "radios_recent": 23})


# --- the plugin ---------------------------------------------------------------
def test_plugin_loads_and_degrades_without_a_hat(tmp_path):
    cfg = {"plugins": {"eink-ws213": {"enabled": True}}}
    loader = Loader(
        config=cfg, capture=CaptureInterface(str(tmp_path)),
        connectivity=ConnectivityState(), services=ServiceRegistry(),
        search_paths=[PLUGIN_DIR], logger=logging.getLogger("test.eink-ws213"))
    loaded = loader.load_all()
    try:
        assert [r.name for r in loaded] == ["eink-ws213"]
        rec = loaded[0]
        assert rec.state == "loaded"
        assert rec.options["variant"] == "v4"
        assert set(rec.capabilities) == {"ui", "web"}
    finally:
        for r in loaded:
            loader.unload(r)


def test_a_variant_outside_the_choices_is_refused_at_load(tmp_path):
    cfg = {"plugins": {"eink-ws213": {"enabled": True, "variant": "V4"}}}
    loader = Loader(
        config=cfg, capture=CaptureInterface(str(tmp_path)),
        connectivity=ConnectivityState(), services=ServiceRegistry(),
        search_paths=[PLUGIN_DIR], logger=logging.getLogger("test.eink-ws213"))
    loader.load_all()
    rec = loader.records["eink-ws213"]
    assert rec.state != "loaded" and "variant" in rec.reason


def test_declarations_choose_the_revision_and_stay_local():
    decl = read_declarations(PLUGIN)["eink-ws213"]
    opts = decl["options"]
    assert opts["variant"]["choices"] == ["v4", "v3", "v2"]
    assert opts["variant"]["default"] == "v4"
    assert opts["rotation"]["choices"] == [0, 90, 180, 270]
    assert "network" not in decl["capabilities"]
    # no keys on this HAT, and no 2.7"-only options
    for gone in ("buttons", "pins", "hold_seconds", "allow_capture", "allow_power",
                 "greyscale"):
        assert gone not in opts
    assert "not yet tested on hardware" in decl["description"].lower()


def test_its_own_driver_helpers_and_the_shared_face():
    names = ("epdconfig", "epd2in13_V4", "epd2in13_V3", "epd2in13_V2")
    for name in names:
        path = os.path.join(PLUGIN_DIR, "_eink-ws213_%s.py" % name)
        with open(path, encoding="utf-8") as handle:
            text = handle.read()
        assert "Permission is hereby granted" in text, name     # MIT header kept
    src = open(PLUGIN, encoding="utf-8").read()
    # nothing from the eink plugin or its helpers: the face it shares with eink
    # comes from the eink_face plugin library
    assert "_eink_epd" not in src and "eink.py" not in src
    assert 'load_library(__file__, "eink_face", api=1)' in src


def test_view_button_is_declared():
    assert WS.EinkWs213.__actions__[0]["label"] == "View"
    assert WS.EinkWs213.__actions__[0]["path"] == "display"


# --- the lean layout ----------------------------------------------------------
def test_compose_runs_with_no_panel(tmp_path):
    p = _plugin(tmp_path)
    assert p._absent is True
    assert p._compose_image(UIState(), "awake").size == (W, H)
    p.context.options = dict(p.context.options, rotation=90)
    assert p._compose_image(UIState(), "awake").size == (H, W)


def test_the_full_frame_fits_and_leaves_the_channel_plan_and_near_out(tmp_path):
    p = _plugin(tmp_path, gps=True)
    p._addresses = lambda: [("bnep0", "192.0.2.10"), ("wlan0", "198.51.100.7")]
    with_data = p._compose_image(FULL, "motivated", "on a roll!")
    # CH and NEAR are not drawn: the same frame without them is identical
    bare = UIState(**{k: getattr(FULL, k) for k in (
        "online", "capture_active", "uptime", "capture_total", "capture_session",
        "aps_total", "networks_total", "battery", "agent", "position")})
    assert with_data.tobytes() == p._compose_image(bare, "motivated", "on a roll!").tobytes()


def test_rows_stop_before_the_brand_mark_with_huge_values(tmp_path):
    p = _plugin(tmp_path, {"show_brand_glyph": False})
    p._addresses = lambda: [("bnep0", "198.51.100.254"), ("wlan0", "203.0.113.254"),
                            ("wlan0", "192.0.2.254")]
    huge = UIState(capture_active=True, uptime=99999999.0, capture_total=99999999,
                   capture_session=999999, aps_total=999999, networks_total=999999,
                   agent={"running": True, "policy": "baseline"})
    img = p._compose_image(huge, "sad", "a very long voice line that has to wrap a lot")
    # nothing in the brand mark's corner, nothing at the right edge of a row
    assert _ink(img, (W - 29, 86, W, H)) == 0      # (the divider ends at W - 30)
    assert _ink(img, (W - 3, 17, W, H)) == 0


def test_the_counters_are_drawn(tmp_path):
    p = _plugin(tmp_path, {"show_brand_glyph": False})
    band = (0, 89, W, H)
    assert _ink(p._compose_image(UIState(), "awake"), band) == 0
    counted = UIState(capture_total=1240, capture_session=30, aps_total=85, networks_total=52)
    assert _ink(p._compose_image(counted, "awake"), band) > 0


def test_link_glyphs_follow_the_same_addresses_as_the_address_row(tmp_path):
    p = _plugin(tmp_path)
    strip = (0, 0, 36, 15)                            # the BT and Wi-Fi glyphs
    p._addresses = lambda: [("bnep0", "192.0.2.10"), ("wlan0", "198.51.100.7")]
    stale = p._compose_image(UIState(paths={"bnep0": False, "wlan0": False}), "awake")
    fresh = p._compose_image(UIState(paths={"bnep0": True, "wlan0": True}), "awake")
    assert stale.crop(strip).tobytes() == fresh.crop(strip).tobytes()
    p._addresses = lambda: []
    down = p._compose_image(UIState(paths={"bnep0": True, "wlan0": True}), "awake")
    assert down.crop(strip).tobytes() != fresh.crop(strip).tobytes()


def test_gps_glyph_only_with_a_receiver(tmp_path):
    gps_box = (38, 0, 100, 15)
    without = _plugin(tmp_path)._compose_image(FULL, "awake")
    assert _ink(without, gps_box) == 0
    with_gps = _plugin(tmp_path, gps=True)._compose_image(FULL, "awake")
    assert _ink(with_gps, gps_box) > 0


def test_ip_row_shows_each_link_and_hides_by_option(tmp_path):
    band = (0, 71, W, 86)
    p = _plugin(tmp_path)
    p._addresses = lambda: [("bnep0", "192.0.2.10"), ("wlan0", "198.51.100.7")]
    img = p._compose_image(UIState(), "awake")
    assert _ink(img, (0, 71, W // 2, 86)) > 0 and _ink(img, (W // 2, 71, W, 86)) > 0
    q = _plugin(tmp_path, {"show_ip": False})
    q._addresses = lambda: [("bnep0", "192.0.2.10")]
    assert _ink(q._compose_image(UIState(), "awake"), band) == 0


def test_voice_takes_two_lines_at_most(tmp_path):
    p = _plugin(tmp_path)
    one = p._compose_image(UIState(), "awake", "short")
    long_ = p._compose_image(UIState(), "awake", "one two three four five six seven "
                                                 "eight nine ten eleven twelve thirteen")
    third_line = (96, 47, W, 54)
    assert _ink(one, third_line) == 0 and _ink(long_, third_line) == 0
    assert _ink(long_, (96, 35, W, 46)) > 0                   # the second line


# --- the HTTPS fingerprint after start-up -------------------------------------
_FP = ":".join(["AB", "CD", "EF", "12", "34", "56", "78", "9A"] + ["00"] * 24)


def test_fingerprint_takes_the_voice_slot_in_two_lines(tmp_path, monkeypatch):
    monkeypatch.setattr(WS.webtls, "fingerprint", lambda *a: _FP)
    p = _plugin(tmp_path)
    p._started += 3600                           # back inside the window
    assert p._fingerprint_lines() == ["HTTPS AB:CD:EF:12", "34:56:78:9A"]
    voiced = p._compose_image(UIState(), "awake", "on a roll!")
    p._started -= 11 * 60
    assert p._fingerprint_lines() == []
    later = p._compose_image(UIState(), "awake", "on a roll!")
    assert voiced.tobytes() != later.tobytes()


def test_fingerprint_off_or_absent(tmp_path, monkeypatch):
    monkeypatch.setattr(WS.webtls, "fingerprint", lambda *a: _FP)
    p = _plugin(tmp_path, {"show_fingerprint_minutes": 0})
    p._started += 3600
    assert p._fingerprint_lines() == []
    monkeypatch.setattr(WS.webtls, "fingerprint", lambda *a: None)
    q = _plugin(tmp_path)
    q._started += 3600
    assert q._fingerprint_lines() == []


# --- the face (its own copy of the eink face) ----------------------------------


def test_voice_and_face_overrides(tmp_path):
    p = _plugin(tmp_path, {"voices": {"awake": "ready!"}, "faces": {"awake": ":-)"}})
    assert p._voice_for("awake", 3) == "ready!"
    assert p._face_glyph("awake") == ":-)"
    assert p._face_glyph("cool") == FACE.FACES["cool"]


def test_battery_low_only_while_discharging(tmp_path):
    p = _plugin(tmp_path, {"battery_low_percent": 10})
    assert p._current_mood(UIState(battery={"percent": 5, "status": "discharging"})) == "tired"
    assert p._current_mood(UIState(battery={"percent": 5, "status": "charging"})) != "tired"


def test_mobility_with_hysteresis(tmp_path):
    p = _plugin(tmp_path)
    p._mobility_state({"lat": 0.0, "lon": 0.0, "ts": 0.0})
    assert p._mobility_state({"lat": 0.0015, "lon": 0.0, "ts": 10.0}) != "driving"
    assert p._mobility_state({"lat": 0.0030, "lon": 0.0, "ts": 20.0}) == "driving"
    assert p._mobility_state(None) == "unknown"


def test_ui_card_reports_mood_and_panel(tmp_path):
    p = _plugin(tmp_path)
    p._mood, p._voice = "cool", "like a boss"
    ui = StubUI()
    view = ui.for_plugin("eink-ws213")
    p.on_ui_setup(view)
    p.on_ui_update(view, UIState(online=True))
    card = {e["name"]: e for e in ui.widgets()["eink-ws213"]}
    assert card["face"]["value"] == FACE.FACES["cool"]
    assert card["panel"]["value"] == "not detected"
    p._absent = False
    p.on_ui_update(view, UIState(online=True))
    assert view.get("panel") == "v4 detected"


# --- the live mirror -----------------------------------------------------------
def test_mirror_png_has_an_etag_and_honours_if_none_match(tmp_path):
    p = _plugin(tmp_path)
    p._tick()
    body, status, headers = p._mirror_png(types.SimpleNamespace(headers={}))
    assert status == 200 and body[:4] == b"\x89PNG"
    assert headers["ETag"] == '"eink-ws213-%d"' % p._frame_seq
    _, status2, _ = p._mirror_png(types.SimpleNamespace(headers={"If-None-Match": headers["ETag"]}))
    assert status2 == 304


def test_mirror_page(tmp_path):
    p = _plugin(tmp_path, {"refresh_seconds": 20})
    page = p.on_webhook("display", types.SimpleNamespace(headers={}))
    assert isinstance(page, PluginPage)
    assert "display.png" in page.body and "20000" in page.body
    assert page.body.startswith("<h1>")          # the shell adds the links above it


def test_frame_seq_only_advances_when_the_frame_changes(tmp_path):
    p = _plugin(tmp_path)
    p._tick()
    seq = p._frame_seq
    p._tick()
    assert p._frame_seq == seq


# --- the panel: revisions and redraws -----------------------------------------
def _fake_driver(monkeypatch, variant="v4", behavior="ok", pins_in_use=False):
    """Fake Waveshare helpers for the plugin's load_helper. ``behavior``: "ok"
    (a panel drives BUSY), "phantom" (BUSY never driven: no panel), "hang" (the
    busy-wait never ends) or None (init raises)."""
    cfg = types.ModuleType("epdconfig")
    cfg.BUSY_PIN = 24
    cfg.level = 0
    cfg.digital_read = lambda pin: cfg.level
    log = {"calls": [], "exited": 0, "loaded": []}
    cfg.module_exit = lambda *a, **k: log.__setitem__("exited", log["exited"] + 1)

    class EPD:
        FULL_UPDATE, PART_UPDATE = 0, 1

        def __init__(self):
            cfg.level = 0

        def init(self, *update):
            log["calls"].append(("init",) + update)
            if behavior is None:
                raise RuntimeError("no panel")
            if behavior == "hang":
                while cfg.digital_read(cfg.BUSY_PIN) != 999:
                    time.sleep(0.003)
            elif behavior == "ok":
                cfg.level = 1
            cfg.digital_read(cfg.BUSY_PIN)
            return 0

        def Clear(self, color=0xFF):
            log["calls"].append(("Clear", color))

        def getbuffer(self, image):
            return "buf"

        def displayPartBaseImage(self, buf):
            log["calls"].append("full")

        def displayPartial(self, buf):
            log["calls"].append("partial")

        def sleep(self):
            log["calls"].append("sleep")

    drv = types.ModuleType("epd2in13")
    drv.EPD = EPD

    def fake_load(plugin_file, filename):
        log["loaded"].append(filename)
        if pins_in_use:
            exc = type("GPIOPinInUse", (Exception,), {})
            raise exc("pin GPIO17 is already in use")
        return cfg if filename.endswith("epdconfig.py") else drv

    monkeypatch.setattr(WS, "load_helper", fake_load)
    return log


@pytest.mark.parametrize("variant, module", [
    ("v4", "_eink-ws213_epd2in13_V4.py"), ("v3", "_eink-ws213_epd2in13_V3.py"),
    ("v2", "_eink-ws213_epd2in13_V2.py")])
def test_the_chosen_revision_picks_its_driver(tmp_path, monkeypatch, variant, module):
    log = _fake_driver(monkeypatch)
    p = _plugin(tmp_path, {"variant": variant})
    assert p._ensure_panel() is not None and p._absent is False
    assert log["loaded"] == ["_eink-ws213_epdconfig.py", module]   # epdconfig first
    init = ("init", 0) if variant == "v2" else ("init",)
    assert log["calls"] == [init, ("Clear", 0xFF)]


def test_no_panel_when_busy_is_never_driven(tmp_path, monkeypatch):
    log = _fake_driver(monkeypatch, behavior="phantom")
    p = _plugin(tmp_path)
    assert p._ensure_panel() is None and p._absent is True
    assert "no 2.13\" e-Paper HAT detected (variant v4)" == p.context.health_note
    assert log["exited"] == 1


def test_a_stuck_panel_times_out(tmp_path, monkeypatch):
    _fake_driver(monkeypatch, behavior="hang")
    p = _plugin(tmp_path)
    p._PROBE_TIMEOUT = 0.05
    assert p._ensure_panel() is None and p._absent is True


def test_the_pins_in_use_say_so(tmp_path, monkeypatch):
    _fake_driver(monkeypatch, pins_in_use=True)
    p = _plugin(tmp_path)
    assert p._ensure_panel() is None
    assert "pins are in use" in p.context.health_note


def test_no_driver_off_a_pi(tmp_path):
    # The real helpers on the test host: no SPI/GPIO, so no panel, no raise.
    p = _plugin(tmp_path)
    p._PROBE_TIMEOUT = 0.05
    assert p._ensure_panel() is None and p._absent is True


def _redraws(p, n, clock, monkeypatch, step=60):
    """Push ``n`` changed frames, ``step`` seconds apart on a fake clock."""
    img = p._compose_image(UIState(), "awake")
    for _ in range(n):
        p._push_panel(img, True)
        clock[0] += step


def test_redraws_are_partial_with_a_periodic_full_refresh(tmp_path, monkeypatch):
    log = _fake_driver(monkeypatch)
    clock = [1000.0]
    monkeypatch.setattr(WS.time, "monotonic", lambda: clock[0])
    p = _plugin(tmp_path, {"partials_per_full": 3})
    _redraws(p, 9, clock, monkeypatch)
    redraws = [c for c in log["calls"] if c in ("full", "partial")]
    # first full, then 3 partials, then full (>= 180 s since: 4 x 60 s) ...
    assert redraws == ["full", "partial", "partial", "partial",
                       "full", "partial", "partial", "partial", "full"]
    p._push_panel(p._compose_image(UIState(), "awake"), False)       # unchanged: nothing
    assert [c for c in log["calls"] if c in ("full", "partial")] == redraws


def test_a_full_refresh_waits_three_minutes(tmp_path, monkeypatch):
    log = _fake_driver(monkeypatch)
    clock = [1000.0]
    monkeypatch.setattr(WS.time, "monotonic", lambda: clock[0])
    p = _plugin(tmp_path, {"partials_per_full": 2})
    _redraws(p, 6, clock, monkeypatch, step=30)        # 30 s apart
    redraws = [c for c in log["calls"] if c in ("full", "partial")]
    # the third redraw is due a full one but only 60 s have passed: partials
    # continue until 180 s after the first full refresh (at the 7th redraw)
    assert redraws == ["full"] + ["partial"] * 5
    _redraws(p, 1, clock, monkeypatch, step=30)
    assert [c for c in log["calls"] if c in ("full", "partial")][-1] == "full"


def test_zero_partials_means_every_redraw_is_full(tmp_path, monkeypatch):
    log = _fake_driver(monkeypatch)
    p = _plugin(tmp_path, {"partials_per_full": 0})
    _redraws(p, 3, [0.0], monkeypatch)
    assert [c for c in log["calls"] if c in ("full", "partial")] == ["full"] * 3


def test_v2_switches_between_full_and_partial_modes(tmp_path, monkeypatch):
    log = _fake_driver(monkeypatch)
    p = _plugin(tmp_path, {"variant": "v2"})
    _redraws(p, 2, [0.0], monkeypatch)
    after_probe = log["calls"][2:]
    assert after_probe == [("init", 0), "full", ("init", 1), "partial"]


def test_unload_blanks_the_panel_after_stopping_the_worker(tmp_path, monkeypatch):
    log = _fake_driver(monkeypatch)
    p = _plugin(tmp_path)
    p._ensure_panel()
    log["calls"].clear()

    def _loop():
        while not p._stop.is_set():
            p._stop.wait(0.01)

    p._worker = threading.Thread(target=_loop, daemon=True)
    p._worker.start()
    p.on_unload()
    assert not p._worker.is_alive()
    assert log["calls"] == [("init",), ("Clear", 0xFF), "sleep"]


def test_unload_is_safe_with_no_panel(tmp_path):
    p = _plugin(tmp_path)
    p.on_unload()
    assert p._stop.is_set()


def test_a_redraw_error_sets_the_fault(tmp_path, monkeypatch):
    _fake_driver(monkeypatch)
    p = _plugin(tmp_path)
    p._ensure_panel()
    p._panel.full = lambda image: 1 / 0
    p._push_panel(p._compose_image(UIState(), "awake"), True)
    assert p._fault is True


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
    monkeypatch.setattr(WS.time, "time", lambda: clock[0])
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
