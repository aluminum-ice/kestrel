"""The eink_face plugin library (``plugins/_lib_eink_face.py``): the face the
e-ink plugins share - the mood ladder, voices, the motion estimate, the Kestrel
mark, glyphs and text helpers - tested once here. Each plugin's own tests
(test_eink.py, test_eink_ws213.py) cover its layout, panel and wiring. PIL is
on the test host, so drawing runs for real; nothing touches hardware."""

import os
import time
import types

import pytest

from kestrel_pi import load_library
from kestrel_pi.ui import UIState

PLUGIN_DIR = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", "..", "plugins"))
FACE = load_library(os.path.join(PLUGIN_DIR, "eink-ws27.py"), "eink_face", api=1)


def _M(**kw):
    return FACE.MoodInputs(**kw)


def test_mood_priority_ladder():
    d = FACE.decide_mood
    # fault wins over everything below it
    assert d(_M(fault=True, battery_low=True, just_promoted=True)) == "broken"
    assert d(_M(battery_low=True, just_promoted=True)) == "tired"
    assert d(_M(just_promoted=True, fresh_handshake=True)) == "levelup"
    assert d(_M(fresh_handshake=True, capture_active=True,
               learned_active=True, reward_trend=1)) == "excited"


def test_mood_agent_postures_are_honest():
    d = FACE.decide_mood
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
    d = FACE.decide_mood
    assert d(_M(capture_active=True, mobility="driving")) == "excited"
    assert d(_M(capture_active=True, mobility="walking")) == "excited"
    assert d(_M(capture_active=True, mobility="parked")) == "focused"
    # capture on with nothing else going on falls through to the awake base
    assert d(_M(capture_active=True)) == "awake"


def test_mood_sleeps_when_capture_disabled():
    d = FACE.decide_mood
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
    assert FACE.voice_phrases("sleep", {}) and FACE.voice_phrases("sleep2", {})


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
        moods.add(FACE.decide_mood(_M(**kw)))
    assert moods <= set(FACE.FACES), moods - set(FACE.FACES)


def test_kestrel_paths_flatten_to_closed_in_box_polygons():
    for d in (FACE.KESTREL_HOVER, FACE.KESTREL_STOOP):
        pts = FACE.flatten_svg_path(d)
        assert len(pts) > 20
        assert pts[0] == pts[-1], "path not closed"
        assert all(0 <= x <= 64 and 0 <= y <= 64 for x, y in pts)


def test_signal_level_tracks_cn0_only_when_locked():
    assert FACE.signal_level(None, True) == 0
    assert FACE.signal_level(40.0, False) == 0      # no fix -> no bars
    assert FACE.signal_level(26.0, True) == 1
    assert FACE.signal_level(31.0, True) == 2
    assert FACE.signal_level(36.0, True) == 3


def test_agent_label_is_honest():
    assert FACE.agent_label(None) == "off"
    assert FACE.agent_label({"running": False}) == "off"
    assert FACE.agent_label({"running": True, "policy": "baseline"}) == "baseline"
    assert FACE.agent_label({"running": True, "policy": "RecurrentPPO"}) == "learned"


def test_every_mood_has_a_voice():
    # Every face the ladder can return must have a matching phrase set, so the
    # mascot never "speaks" a fallback that mismatches its face.
    for mood in FACE.FACES:
        assert FACE.voice_phrases(mood, None), mood


def test_fmt_uptime():
    assert FACE.fmt_uptime(None) is None
    assert FACE.fmt_uptime(-1) is None
    # Minute granularity on purpose (seconds would redraw the panel every cycle).
    assert FACE.fmt_uptime(0) == "0:00"
    assert FACE.fmt_uptime(5025) == "1:23"              # 1h23m45s -> 1:23
    assert FACE.fmt_uptime(90061) == "25:01"            # days fold into hours


def test_density_meter_fills_more_bars_at_higher_levels():
    from PIL import Image, ImageDraw
    def inked(level):
        img = Image.new("1", (30, 20), 255)
        FACE.draw_density(ImageDraw.Draw(img), 2, 4, 18, 11, level)
        return img.histogram()[0]   # count of black (0) pixels
    # a denser environment fills more of the four bars, so more ink
    assert inked(0) < inked(2) < inked(4)


def test_wrap_text_respects_width():
    from PIL import Image, ImageDraw
    draw = ImageDraw.Draw(Image.new("1", (10, 10), 255))
    font = FACE.font(11)
    lines = FACE.wrap_text(draw, "so many signals here", font, 60)
    assert len(lines) >= 2
    assert all(isinstance(ln, str) and ln for ln in lines)
    # A single over-wide word is never split, it just sits on its own line.
    assert FACE.wrap_text(draw, "supercalifragilistic", font, 10) == \
        ["supercalifragilistic"]


def test_classify_speed_thresholds():
    assert FACE.classify_speed(0.0) == "parked"
    assert FACE.classify_speed(0.5) == "parked"
    assert FACE.classify_speed(0.75) is None          # parked/walking deadband
    assert FACE.classify_speed(1.4) == "walking"
    assert FACE.classify_speed(3.0) == "walking"
    assert FACE.classify_speed(4.0) == "driving"


def test_iface_ipv4_is_none_for_a_missing_interface():
    assert FACE.iface_ipv4("kestrel-none0") is None

# --- what the library adds over the old per-plugin code ---------------------
def test_both_e_ink_plugins_use_this_library():
    for plugin in ("eink-ws27.py", "eink-ws213.py"):
        src = open(os.path.join(PLUGIN_DIR, plugin), encoding="utf-8").read()
        assert 'load_library(__file__, "eink_face", api=1)' in src, plugin
        # no copy of the face left in the plugin
        for name in ("def decide_mood", "FACES = {", "def draw_bt", "_KESTREL_HOVER"):
            assert name not in src, (plugin, name)
    assert load_library(os.path.join(PLUGIN_DIR, "eink-ws213.py"), "eink_face", api=1) is FACE


def test_face_glyph_and_voice_honour_overrides():
    assert FACE.face_glyph("cool") == FACE.FACES["cool"]
    assert FACE.face_glyph("nope") == FACE.FACES["awake"]
    assert FACE.face_glyph("awake", {"awake": ":-)"}) == ":-)"
    phrases = FACE.VOICES["cool"]
    assert FACE.voice_for("cool", 0) == phrases[0]
    assert FACE.voice_for("cool", len(phrases)) == phrases[0]          # wraps
    assert FACE.voice_for("awake", 5, {"awake": "ready!"}) == "ready!"
    assert FACE.voice_for("awake", 1, {"awake": ["one", "two"]}) == "two"
    assert FACE.voice_for(None, 0) in FACE.VOICES["awake"]


def _gps_fix(metres, t):
    return {"lat": metres / 111_320.0, "lon": 0.0, "fix": 3,
            "fix_time": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t))}


def test_mobility_tracker_needs_a_sustained_reading():
    m = FACE.MobilityTracker()
    assert m.state == "unknown"
    t0 = 1_700_000_000
    assert m.update(_gps_fix(0, t0)) == "unknown"            # first fix: no speed yet
    assert m.update(_gps_fix(22 * 10, t0 + 10)) == "unknown"  # one driving reading
    assert m.update(dict(_gps_fix(22 * 10, t0 + 10))) == "unknown"   # same fix again
    assert m.update(_gps_fix(22 * 20, t0 + 20)) == "driving"  # sustained
    assert m.state == "driving"
    assert m.update(None) == "unknown"                       # fix lost


def test_mobility_tracker_uses_a_provider_speed():
    m = FACE.MobilityTracker()
    m.update({"lat": 1.0, "lon": 1.0, "ts": 0.0, "speed": 1.4})
    m.update({"lat": 1.0, "lon": 1.0, "ts": 10.0, "speed": 1.4})
    assert m.update({"lat": 1.0, "lon": 1.0, "ts": 20.0, "speed": 1.4}) == "walking"


def test_fix_seconds_prefers_ts_then_fix_time():
    assert FACE.fix_seconds({"ts": 12.5, "fix_time": "2023-11-14T22:13:20Z"}) == 12.5
    assert FACE.fix_seconds({"fix_time": "2023-11-14T22:13:20Z"}) == 1_700_000_000.0
    assert FACE.fix_seconds({"fix_time": "not a time"}) is None
    assert FACE.fix_seconds({}) is None


def _state(**kw):
    base = dict(online=True, epoch=10, capture_active=True,
                agent={"running": True, "policy": "baseline", "posture": "autohunt"})
    base.update(kw)
    return UIState(**base)


def test_mood_tracker_decides_from_the_snapshot():
    m = FACE.MoodTracker()
    assert m.decide(_state(), {}, "unknown") == "cool"
    assert m.decide(_state(capture_active=False, agent=None), {}, "unknown") == "sleep"
    assert m.decide(_state(), {}, "parked", fault=True) == "broken"
    low = _state(battery={"percent": 9, "status": "discharging"})
    assert m.decide(low, {}, "unknown") == "tired"
    assert m.decide(low, {"battery_low_percent": 5}, "unknown") == "cool"
    assert m.decide(None, {}, "unknown") == "sleep"


def test_mood_tracker_handshake_is_brief_and_counts_as_activity():
    m = FACE.MoodTracker()
    now = 1000.0
    idle = _state(capture_active=False, agent=None, epoch=5)
    m.decide(idle, {}, "unknown", now=now)                     # starts the idle count
    m.handshake(epoch=40, now=now)
    assert m.decide(_state(epoch=40), {}, "unknown", now=now + 1) == "excited"
    assert m.decide(_state(epoch=40), {}, "unknown", now=now + 9) == "cool"
    assert m.idle_epochs(_state(epoch=45)) == 5


def test_mood_tracker_reward_trend_and_promotion():
    m = FACE.MoodTracker()
    assert m.reward_trend(None) == 0
    assert m.reward_trend(0.1) == 0                            # first reading
    assert m.reward_trend(1.0) == 1
    assert m.reward_trend(-2.0) == -1
    agent = {"model_source": "local", "last_trained": "t1"}
    assert m.promotion(agent, 0.0) is False                    # first model seen
    assert m.promotion(dict(agent, last_trained="t2"), 1.0) is True
    assert m.promotion(dict(agent, last_trained="t2"), 12.0) is False


def test_mood_tracker_alternates_the_look_when_sweeping():
    m = FACE.MoodTracker()
    sweep = _state(agent={"running": True, "policy": "baseline", "posture": "sweep-band"})
    looks = {m.decide(sweep, {}, "unknown") for _ in range(2)}
    assert looks == {"look_l", "look_r"}


def test_draw_kestrel_pose_follows_motion():
    from PIL import Image, ImageDraw

    def drawn(moving):
        img = Image.new("1", (40, 40), 255)
        FACE.draw_kestrel(ImageDraw.Draw(img), 4, 4, 28, moving)
        return img.tobytes()

    assert drawn(True) != drawn(False)
    assert drawn(False) != Image.new("1", (40, 40), 255).tobytes()


def test_addresses_lists_only_links_with_an_address(monkeypatch):
    monkeypatch.setattr(FACE, "iface_ipv4", lambda iface: {"wlan0": "192.0.2.20"}.get(iface))
    assert FACE.addresses() == [("wlan0", "192.0.2.20")]
    assert FACE.addresses(("wlan0", "bnep0", "wlan0")) == [("wlan0", "192.0.2.20")] * 2


def test_the_library_needs_no_pil_to_load():
    # PIL is imported inside the drawing functions only, so a plugin can load
    # the library (and decide moods) on a host without PIL.
    src = open(os.path.join(PLUGIN_DIR, "_lib_eink_face.py"), encoding="utf-8").read()
    top = src.split("\nLIB_API", 1)[0]
    assert "PIL" not in top.split('"""', 2)[2]
    assert FACE.LIB_API == (1, 1)


@pytest.mark.parametrize("mood", sorted(FACE.FACES))
def test_every_face_renders(mood):
    from PIL import Image, ImageDraw

    img = Image.new("1", (200, 40), 255)
    ImageDraw.Draw(img).text((2, 2), FACE.face_glyph(mood), font=FACE.font(20), fill=0)
    assert img.tobytes() != Image.new("1", (200, 40), 255).tobytes()


def test_the_documented_example_plugin_works(tmp_path, monkeypatch):
    # docs/developer/plugin-libraries.md shows a minimal plugin on eink_face; it
    # must load through the real loader and draw a frame.
    import io
    import logging
    import re

    from PIL import Image

    from kestrel_pi import plugins as plugin_api
    from kestrel_pi.capture import CaptureInterface
    from kestrel_pi.connectivity import ConnectivityState
    from kestrel_pi.context import ServiceRegistry
    from kestrel_pi.loader import Loader

    doc = os.path.join(PLUGIN_DIR, "..", "..", "..", "..", "docs", "developer",
                       "plugin-libraries.md")
    text = open(doc, encoding="utf-8").read().split("## `eink_face`", 1)[1]
    code = re.search(r"### A minimal plugin\n.*?```python\n(.*?)```", text, re.S).group(1)
    plugins = tmp_path / "plugins"
    plugins.mkdir()
    (plugins / "tinyface.py").write_text(code)
    monkeypatch.setattr(plugin_api, "SHIPPED_PLUGINS_DIR", PLUGIN_DIR)   # the shipped library
    monkeypatch.setattr(FACE, "iface_ipv4", lambda iface: None)         # no real addresses
    loader = Loader(config={"plugins": {"tinyface": {"enabled": True}}},
                    capture=CaptureInterface(str(tmp_path)), connectivity=ConnectivityState(),
                    services=ServiceRegistry(), search_paths=[str(plugins)],
                    logger=logging.getLogger("test.eink_face.doc"))
    loader.load_all()
    record = loader.records["tinyface"]
    assert record.state == "loaded", getattr(record, "reason", "")
    plugin = record.instance
    plugin.on_ui_update(None, _state())
    plugin.on_handshake(types.SimpleNamespace(bssid="00:00:5E:00:53:01", essid="My Network"))
    body, status, headers = plugin.on_webhook("display", None)
    assert status == 200 and headers["Content-Type"] == "image/png"
    image = Image.open(io.BytesIO(body))
    assert image.size == (212, 104)
