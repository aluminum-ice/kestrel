"""map plugin: an SVG map of located captured networks, drawn on the device.

All data is invented: RFC 7042 MACs, placeholder network names and generic
coordinates around 12.34567, -45.67890."""

import importlib.util
import json
import os
import types
from datetime import datetime, timedelta, timezone

import pytest

from kestrel_pi.capture import CaptureInterface
from kestrel_pi.context import PluginContext, ServiceRegistry
from kestrel_pi.plugins import PluginPage

PLUGIN_DIR = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", "..", "plugins"))


def _mod():
    spec = importlib.util.spec_from_file_location(
        "map_under_test", os.path.join(PLUGIN_DIR, "map.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


MAP = _mod()
LAT, LON = 12.34567, -45.67890
A, B, C = "00:00:5E:00:53:0A", "00:00:5E:00:53:0B", "00:00:5E:00:53:0C"


def _iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _line(mac, essid):
    return "*".join(["WPA", "02", "%032x" % 1, mac.replace(":", "").lower(),
                     "00005e0053ff", essid.encode().hex(), "aa", "bb", "00"])


@pytest.fixture
def store(tmp_path):
    d = tmp_path / "capture"
    d.mkdir()
    now = datetime.now(timezone.utc)
    (d / "My Network.hc22000").write_text(_line(A, "My Network") + "\n"
                                         + _line(B, "<b>Cafe</b>") + "\n")
    # A recorded fix for A (fresh) and an old one for C; an estimate for B.
    (d / "My Network.hc22000.gps.json").write_text(json.dumps({
        A.replace(":", "").lower(): {"lat": LAT, "lon": LON, "fix_time": _iso(now),
                                    "source": "gpsd"},
        C.replace(":", "").lower(): {"lat": LAT + 0.2, "lon": LON + 0.3,
                                    "fix_time": _iso(now - timedelta(days=30)),
                                    "source": "gpsd"},
    }))
    (d / "geo").mkdir()
    (d / "geo" / "wigle-cache.json").write_text(json.dumps({
        B: {"bssid": B, "lat": LAT + 0.01, "lon": LON - 0.01,
            "query_time": _iso(now), "source": "wigle-search"}}))
    return d


def _plugin(store, tmp_path, authed=True, show_locations=None):
    p = MAP.CaptureMap()
    p.options = {}
    p.context = PluginContext(
        name="map", options={}, capture=CaptureInterface(str(store)),
        connectivity=types.SimpleNamespace(online=False), services=ServiceRegistry(),
        capabilities=("capture-read", "location", "web"))
    p.context.web_authed = authed
    p.on_loaded()
    cfg = tmp_path / "config.toml"
    if show_locations is not None:
        cfg.write_text("[web]\nshow_locations = %s\n" % ("true" if show_locations else "false"))
    p._config_path = str(cfg)
    return p


def _page(p, **args):
    page = p.on_webhook("", types.SimpleNamespace(args=args))
    assert isinstance(page, PluginPage)
    return page


# -- geometry -------------------------------------------------------------------

def test_projection_round_trips_and_fits():
    proj = MAP.Projection((LAT, LON, LAT + 0.2, LON + 0.3))
    for lat, lon in ((LAT, LON), (LAT + 0.2, LON + 0.3), (LAT + 0.1, LON + 0.15)):
        x, y = proj.xy(lat, lon)
        assert 0 <= x <= MAP.W and 0 <= y <= MAP.H        # inside, with the margin
        back = proj.latlon(x, y)
        assert abs(back[0] - lat) < 1e-9 and abs(back[1] - lon) < 1e-9
    north, _ = proj.xy(LAT + 0.2, LON)
    south, _ = proj.xy(LAT, LON)
    assert proj.xy(LAT + 0.2, LON)[1] < proj.xy(LAT, LON)[1]   # north is up
    single = MAP.Projection((LAT, LON, LAT, LON))               # one point: still a view
    assert single.scale > 0


def test_nice_steps():
    assert MAP._nice(7.3) == 5 and MAP._nice(0.034) == 0.02 and MAP._nice(1) == 1
    assert MAP._places(0.02) == 2 and MAP._places(5) == 0


def test_close_points_cluster_and_far_ones_do_not():
    from kestrel_pi.geo import Location

    near = [("a", Location(LAT, LON, "recorded", "gpsd", "onboard GPS")),
            ("b", Location(LAT + 1e-6, LON + 1e-6, "estimated", "wigle-search", "WiGLE"))]
    far = [("c", Location(LAT + 0.5, LON + 0.5, "recorded", "gpsd", "onboard GPS"))]
    proj = MAP.Projection((LAT, LON, LAT + 0.5, LON + 0.5))
    groups = MAP.cluster(near + far, proj)
    sizes = sorted(len(g["members"]) for g in groups)
    assert sizes == [1, 2]
    pair = next(g for g in groups if len(g["members"]) == 2)
    assert pair["recorded"] == 1


# -- the page -------------------------------------------------------------------

def test_map_draws_recorded_and_estimated_differently(store, tmp_path):
    body = _page(_plugin(store, tmp_path)).body
    assert "<svg" in body and "Map of located captured networks" in body
    assert "var(--accent)" in body                    # a recorded pin
    assert ">~</text>" in body                        # an estimated ring
    assert "/networks?view=ap&amp;q=00%3A00%3A5E%3A00%3A53%3A0A" in body
    assert "3 of 3 located networks in view" in body
    assert "km" in body or " m<" in body              # a scale bar


def test_names_in_tooltips_are_escaped(store, tmp_path):
    body = _page(_plugin(store, tmp_path)).body
    assert "<b>Cafe</b>" not in body.split("<svg", 1)[1]
    assert "&lt;b&gt;Cafe&lt;/b&gt;" in body


def test_filters(store, tmp_path):
    p = _plugin(store, tmp_path)
    assert "2 of 2" in _page(p, f="recorded").body
    assert "1 of 1" in _page(p, f="estimated").body
    week = _page(p, f="week").body                    # the 30-day-old fix is gone
    assert "2 of 2" in week
    assert "<b>Last 7 days</b>" in week


def test_zoom_box_and_show_all(store, tmp_path):
    p = _plugin(store, tmp_path)
    box = "%f,%f,%f,%f" % (LAT - 0.05, LON - 0.05, LAT + 0.05, LON + 0.05)
    body = _page(p, bbox=box).body
    assert "2 of 3 located networks in view" in body
    assert "Reset view" in body
    assert "Reset view" not in _page(p).body          # nothing to reset
    assert "in view" in _page(p, bbox="nonsense").body   # a bad box is ignored


def test_clusters_link_to_a_zoom_and_keep_the_token(store, tmp_path):
    p = _plugin(store, tmp_path)
    wide = "%f,%f,%f,%f" % (LAT - 40, LON - 60, LAT + 40, LON + 60)
    body = _page(p, bbox=wide, token="abc").body
    assert "networks (" in body and "tap to zoom in" in body
    assert "token=abc" in body
    assert "bbox=" in body


def test_a_cluster_from_one_spot_lists_its_networks_instead_of_zooming(tmp_path):
    d = tmp_path / "capture"
    d.mkdir()
    macs = ["00:00:5E:00:53:%02X" % i for i in range(1, 5)]
    (d / "x.hc22000").write_text("".join(_line(m, "Library WiFi") + "\n" for m in macs))
    now = _iso(datetime.now(timezone.utc))
    (d / "x.hc22000.gps.json").write_text(json.dumps({
        m.replace(":", "").lower(): {"lat": LAT, "lon": LON, "fix_time": now, "source": "gpsd"}
        for m in macs}))
    p = _plugin(d, tmp_path)
    svg = _page(p, token="abc").body.split("<svg", 1)[1].split("</svg>", 1)[0]
    assert "at one spot - tap to list them" in svg and "tap to zoom in" not in svg
    spot = "%f,%f,%f,%f" % (LAT - 1e-5, LON - 1e-5, LAT + 1e-5, LON + 1e-5)
    listed = _page(p, spot=spot, token="abc").body
    assert "4 networks at this spot" in listed
    assert listed.count("/networks?view=ap&amp;q=00%3A00%3A5E%3A00%3A53%3A0") >= 4
    assert "token=abc" in listed


def test_tiles_cover_the_view_and_line_up_with_the_pins(store, tmp_path):
    import math
    import re

    p = _plugin(store, tmp_path)
    proj = MAP.Projection((LAT, LON, LAT + 0.2, LON + 0.3))
    html = p._tiles(proj, MAP.DEFAULT_TILE_URL)
    tiles = re.findall(r'src="https://tile\.openstreetmap\.org/(\d+)/(\d+)/(\d+)\.png" '
                       r'alt="" loading="lazy" decoding="async" referrerpolicy="origin" '
                       r'style="position:absolute;left:([-\d.]+)%;top:([-\d.]+)%;'
                       r'width:calc\(([\d.]+)% \+ 1px\)', html)
    assert 4 <= len(tiles) <= 40
    z = int(tiles[0][0])
    # The standard slippy-map tile of a point must be the tile drawn under its pin.
    n = 2 ** z
    tx = int((LON + 180) / 360 * n)
    lat = math.radians(LAT)
    ty = int((1 - math.log(math.tan(lat) + 1 / math.cos(lat)) / math.pi) / 2 * n)
    x, y = proj.xy(LAT, LON)
    left, top, width = next((float(t[3]), float(t[4]), float(t[5]))
                            for t in tiles if (int(t[1]), int(t[2])) == (tx, ty))
    assert left <= x / proj.w * 100 <= left + width
    assert top <= y / proj.h * 100


def test_tile_url_option(store, tmp_path):
    p = _plugin(store, tmp_path)
    shown = _page(p).body
    assert "tile.openstreetmap.org" in shown and "OpenStreetMap</a> contributors" in shown
    p.options = {"tile_url": ""}
    off = _page(p).body
    assert "<img " not in off and "&#176;</text>" in off     # the grid instead
    assert "background map is off" not in off
    p.options = {"tile_url": "http://tiles.example.com/{z}/{x}/{y}.png"}
    bad = _page(p).body
    assert "<img " not in bad and "background map is off" in bad
    p.options = {"tile_url": "https://tiles.example.com/{z}/{x}/{y}.png?a=\"<x>"}
    assert "<x>" not in _page(p).body.split("<h1>", 1)[1]


def test_the_page_links_back_with_the_token(store, tmp_path):
    body = _page(_plugin(store, tmp_path), token="abc").body
    assert '<a href="/plugins?token=abc">&larr; Plugins</a>' in body
    assert '<a href="/?token=abc">Dashboard</a>' in body


def test_gates(store, tmp_path):
    signed_out = _page(_plugin(store, tmp_path, authed=False)).body
    assert "needs the dashboard login" in signed_out and "<svg" not in signed_out
    hidden = _page(_plugin(store, tmp_path, show_locations=False)).body
    assert "Locations are hidden" in hidden and "<svg" not in hidden
    shown = _page(_plugin(store, tmp_path, show_locations=True)).body
    assert "<svg" in shown


def test_empty_store_and_unknown_path(tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()
    p = _plugin(empty, tmp_path)
    assert "No captured network matching this filter" in _page(p).body
    assert p.on_webhook("nope", types.SimpleNamespace(args={})).status == 404


def test_declarations_are_readable_by_root():
    from kestrel_pi.declarations import read_declarations

    decl = read_declarations(os.path.join(PLUGIN_DIR, "map.py"))["map"]
    caps = decl.get("capabilities") or decl.get("__capabilities__")
    assert set(caps) == {"capture-read", "location", "web"}
