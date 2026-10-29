"""map - every located captured network on one map, drawn on the device.

A row button on the Plugins page ("Map") opens a page with an SVG map of every
captured network whose location is known: recorded by the onboard GPS (the
`gps` plugin's sidecars) or estimated from an outside source such as WiGLE. It
is the all-at-once view of the per-row locations on the Captured networks page.

The background is OpenStreetMap tiles (``tile_url``), drawn by the browser
under the pins: only the tiles for the area in view, each sending just the
device's origin as its referrer (the tile policy needs one; the rest of the
dashboard sends none), with the OpenStreetMap attribution on the map. So the
tile server sees the areas you view, as it would on openstreetmap.org. Leave
``tile_url`` empty for no background: the page then draws a light grid of
latitude/longitude lines instead. There is no JavaScript.

A marker is green with a lock when the network's passphrase has been recovered
(read from the core ``recovered`` service, by BSSID) and gray when not; a
cluster is green if any network in it is recovered.

Nearby points are grouped into a counted cluster; following a cluster's link
zooms the page to it. Networks captured from one spot share its fix, so no zoom
separates them: such a cluster lists its networks under the map instead. A
single point links to its row on the Captured networks page.

The device itself reads the location files already in the capture store
through the core reader (``kestrel_pi.geo``) and sends nothing anywhere, so it
needs no authorized-use acknowledgment. Locations are treated like the rest of
the dashboard treats them: shown only with the web login on, and hidden while
``[web] show_locations`` is off (read live from the config).

The projection is Web Mercator, matching the tiles; a set of points spanning
the antimeridian is not supported.
"""

from __future__ import annotations

import math
import os
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode

from kestrel_pi import Plugin
from kestrel_pi.config import DEFAULT_CONFIG_PATH, load_config
from kestrel_pi.geo import GeoStore

# Two renderings share one drawing: a landscape map for wide screens and a
# portrait one for phones, swapped by the shared stylesheet's .wide-only and
# .narrow-only, so markers and labels stay finger-sized on a phone instead of
# shrinking with a wide map.
WIDE = (1000, 640)
NARROW = (420, 560)
W, H = WIDE                 # the default viewBox
CELL = 34                   # cluster grid, in viewBox units
EARTH_CIRCUMFERENCE_M = 40075016.686
MAX_LAT = 85.05112878       # Web Mercator's limit
MAX_TILE_ZOOM = 19
DEFAULT_TILE_URL = "https://tile.openstreetmap.org/{z}/{x}/{y}.png"
# A marker is green when the network's passphrase has been recovered and gray
# when not, and a recovered one also carries a lock, so the difference never
# rests on colour alone.
RECOVERED_FILL = "#2e7d32"
PLAIN_FILL = "#6b6b75"


def _lock(cx, cy, colour):
    """A small padlock (about 8 x 10 viewBox units) centred on ``cx, cy``."""
    return ('<rect x="%.1f" y="%.1f" width="8" height="6" rx="1" fill="%s"/>'
            '<path d="M%.1f %.1f v-2.5 a2.5 2.5 0 0 1 5 0 v2.5" fill="none" '
            'stroke="%s" stroke-width="1.6"/>'
            % (cx - 4, cy - 1, colour, cx - 2.5, cy - 1, colour))


def _lock_badge(cx, cy):
    """The lock in a white disc, pinned to a ring or a square's corner."""
    return ('<circle cx="%.1f" cy="%.1f" r="7.5" fill="#fff" stroke="%s" '
            'stroke-width="1.2"/>' % (cx, cy, RECOVERED_FILL) + _lock(cx, cy, RECOVERED_FILL))
FILTERS = (("all", "All"), ("recorded", "Recorded"), ("estimated", "Estimated"),
           ("week", "Last 7 days"))


def _nice(value):
    """The largest 1/2/5 x 10^n not above ``value`` (value > 0)."""
    exp = math.floor(math.log10(value))
    for mult in (5, 2, 1):
        step = mult * 10 ** exp
        if step <= value:
            return step
    return 10 ** exp


def _grid_step(span):
    """A 1/2/5 step giving roughly four to seven grid lines across ``span``."""
    return _nice(max(span, 1e-9) / 4.0)


def _places(step):
    return max(0, -int(math.floor(math.log10(step))))


def _parse_when(text):
    """An ISO-8601 timestamp (``Z`` or an offset, optional fraction) as an aware
    UTC datetime, or None."""
    try:
        when = datetime.fromisoformat(str(text).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return when.astimezone(timezone.utc)


def _parse_bbox(text):
    """``(south, west, north, east)`` from ``"s,w,n,e"``, or None."""
    try:
        s, w, n, e = (float(v) for v in (text or "").split(","))
    except ValueError:
        return None
    if not (-90 <= s < n <= 90 and -180 <= w < e <= 180):
        return None
    return s, w, n, e


def _mx(lon):
    """Web Mercator x in world units (0 at 180 W, 1 at 180 E)."""
    return (lon + 180.0) / 360.0


def _my(lat):
    """Web Mercator y in world units (0 at the north edge, 1 at the south)."""
    sin = math.sin(math.radians(max(-MAX_LAT, min(MAX_LAT, lat))))
    return 0.5 - math.log((1 + sin) / (1 - sin)) / (4 * math.pi)


def _lat(my):
    return math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * my))))


class Projection:
    """Web Mercator (as map tiles are), fitted to ``bounds`` with a margin.
    ``scale`` is viewBox units per world unit (the full 360 degrees)."""

    def __init__(self, bounds, margin=0.1, min_span_deg=0.002, size=WIDE):
        self.w, self.h = size
        s, w, n, e = bounds
        x0, x1, y0, y1 = _mx(w), _mx(e), _my(n), _my(s)
        self.cx, self.cy = (x0 + x1) / 2.0, (y0 + y1) / 2.0
        self.lat0 = _lat(self.cy)
        # The projection is conformal, so one minimum span in world units
        # serves both axes.
        least = min_span_deg / 360.0
        span_x = max(x1 - x0, least) * (1 + 2 * margin)
        span_y = max(y1 - y0, least) * (1 + 2 * margin)
        self.scale = min(self.w / span_x, self.h / span_y)

    def xy(self, lat, lon):
        return (self.w / 2.0 + (_mx(lon) - self.cx) * self.scale,
                self.h / 2.0 + (_my(lat) - self.cy) * self.scale)

    def latlon(self, x, y):
        return (_lat(self.cy + (y - self.h / 2.0) / self.scale),
                (self.cx + (x - self.w / 2.0) / self.scale) * 360.0 - 180.0)

    def metres_per_unit(self):
        """Ground metres per viewBox unit at the centre of the view."""
        return EARTH_CIRCUMFERENCE_M * math.cos(math.radians(self.lat0)) / self.scale

    def view_bounds(self):
        """``(south, west, north, east)`` of the whole viewBox."""
        n, w = self.latlon(0, 0)
        s, e = self.latlon(self.w, self.h)
        return s, w, n, e


def cluster(points, proj):
    """Group points that fall in the same ``CELL``-sized grid square.

    ``points`` are ``(bssid, Location)``; returns a list of dicts with the
    members, their centroid in viewBox units and how many are recorded."""
    cells = {}
    for bssid, loc in points:
        x, y = proj.xy(loc.lat, loc.lon)
        if not (0 <= x <= proj.w and 0 <= y <= proj.h):
            continue
        cells.setdefault((int(x // CELL), int(y // CELL)), []).append((bssid, loc, x, y))
    out = []
    for members in cells.values():
        out.append({
            "members": members,
            "x": sum(m[2] for m in members) / len(members),
            "y": sum(m[3] for m in members) / len(members),
            "recorded": sum(1 for m in members if m[1].recorded),
        })
    out.sort(key=lambda c: (c["y"], c["x"]))
    return out


class CaptureMap(Plugin):
    __plugin_name__ = "map"
    __author__ = "kestrel"
    __version__ = "0.1.0"
    __license__ = "GPL3"
    __description__ = "Every located captured network on one map, drawn on the device."

    __target_api__ = 2
    # Reads the capture store's location files and draws its own page; nothing
    # leaves the device, so no network capability and no acknowledgment.
    __capabilities__ = ("capture-read", "location", "web")
    __actions__ = ({"label": "Map", "path": "index", "method": "get"},)
    __options__ = {
        "tile_url": {"type": "str", "default": "https://tile.openstreetmap.org/{z}/{x}/{y}.png",
                     "help": "The background map's tiles (https, with {z}, {x} and {y}). "
                             "Your browser loads the tiles for the area in view, so the "
                             "tile server sees which areas you look at. Use your own "
                             "tile server, or leave empty for no background."},
    }

    def on_loaded(self):
        self.log = self.context.logger
        self._config_path = DEFAULT_CONFIG_PATH
        self._shown_cache = None     # (mtime, shown)

    # -- data -----------------------------------------------------------------
    def _locations_shown(self):
        """``[web] show_locations``, read live (it takes effect without a
        restart elsewhere on the dashboard too). Defaults to shown."""
        try:
            mtime = os.stat(self._config_path).st_mtime_ns
        except OSError:
            return True
        if self._shown_cache and self._shown_cache[0] == mtime:
            return self._shown_cache[1]
        try:
            web = (load_config(self._config_path) or {}).get("web") or {}
            shown = web.get("show_locations", True) is not False
        except Exception:  # noqa: BLE001 - a bad config never hides the page's reason
            shown = True
        self._shown_cache = (mtime, shown)
        return shown

    def _points(self, which, now=None):
        """``[(bssid, Location), ...]`` for the chosen filter."""
        found = GeoStore(self.context.capture.store_dir).locations()
        cutoff = (now or datetime.now(timezone.utc)) - timedelta(days=7)
        out = []
        for bssid, loc in sorted(found.items()):
            if which == "recorded" and not loc.recorded:
                continue
            if which == "estimated" and loc.recorded:
                continue
            if which == "week":
                when = _parse_when(loc.when)
                if when is None or when < cutoff:
                    continue
            out.append((bssid, loc))
        return out

    def _names(self):
        try:
            return {ap["bssid"]: ap.get("essids") or []
                    for ap in self.context.capture.captured_access_points() if ap.get("bssid")}
        except Exception:  # noqa: BLE001 - names are a nicety on the tooltip
            return {}

    # -- page -----------------------------------------------------------------
    def on_webhook(self, path, request):
        if path not in ("", "index"):
            return self.context.page("Map", "<p>There's nothing here.</p>", status=404)
        args = getattr(request, "args", {}) or {}
        token = args.get("token") or ""
        which = args.get("f") if args.get("f") in dict(FILTERS) else "all"
        bbox = _parse_bbox(args.get("bbox"))
        spot = _parse_bbox(args.get("spot"))
        esc = self.context.esc
        back = "/plugins" + ("?" + urlencode([("token", token)]) if token else "")
        head = ('<p><a href="' + esc(back) + '">&larr; Plugins</a> &middot; <a href="'
                + esc("/" + ("?" + urlencode([("token", token)]) if token else ""))
                + '">Dashboard</a></p><h1>Map</h1>')
        if not getattr(self.context, "web_authed", False):
            return self.context.page("Map", head + '<p class="msg err">The map needs '
                                     "the dashboard login on. Turn it on with <code>sudo "
                                     "kestrel web auth system</code>.</p>")
        if not self._locations_shown():
            return self.context.page("Map", head + '<p class="msg">Locations are hidden '
                                     "(<code>[web] show_locations = false</code>). Turn "
                                     "them back on in Advanced settings to see the map.</p>")
        points = self._points(which)
        tiles = self._tile_url()
        body = head + self._filter_bar(which, bbox, token)
        if not points:
            body += ("<p>No captured network matching this filter has a known "
                     "location yet. Locations come from the <b>gps</b> plugin "
                     "(recorded at capture time) and uploaders such as <b>wigle</b> "
                     "(estimated).</p>")
            return self.context.page("Map", body)
        if bbox is None:
            lats = [loc.lat for _, loc in points]
            lons = [loc.lon for _, loc in points]
            bounds = (min(lats), min(lons), max(lats), max(lons))
        else:
            bounds = bbox
        names = self._names()
        recovered = self._recovered(points)
        margin = 0.1 if bbox is None else 0.0
        shown = 0
        for size, css in ((WIDE, "wide-only"), (NARROW, "narrow-only")):
            proj = Projection(bounds, margin=margin, size=size)
            groups = cluster(points, proj)
            if size == WIDE:
                shown = sum(len(g["members"]) for g in groups)
            body += self._map(proj, groups, names, which, bbox, token, css, tiles, recovered)
        body += ('<p class="hint">' + esc("%d of %d located networks in view" %
                                          (shown, len(points))) + "</p>")
        if spot:
            body += self._spot_list(points, spot, names, token, recovered)
        body += ('<p class="hint"><b>&#9679; Pin</b>: recorded by the onboard GPS. '
                 "<b>&#9675; ~</b>: estimated from an outside source. <b>Square with a "
                 "number</b>: several networks close together (tap to zoom in, or to list "
                 "them when they were captured from the same spot). <b>Green with a "
                 '<span aria-hidden="true">&#128274;</span> lock</b>: the passphrase has '
                 "been recovered (for a square, at least one network in it); <b>gray</b>: "
                 "not recovered. A single network links to its row on Captured "
                 "networks.</p>")
        if self.options.get("tile_url") and not tiles:
            body += ('<p class="msg err">The background map is off: <code>tile_url</code> '
                     "must be an https address with {z}, {x} and {y} in it.</p>")
        return self.context.page("Map", body)

    def _tile_url(self):
        """The tile URL template if usable (https, with {z}/{x}/{y}), else ''."""
        url = self.options.get("tile_url")
        url = DEFAULT_TILE_URL if url is None else str(url).strip()
        if (url.lower().startswith("https://") and len(url) > len("https://")
                and all(part in url for part in ("{z}", "{x}", "{y}"))):
            return url
        return ""

    def _recovered(self, points):
        """The BSSIDs among ``points`` whose passphrase is known, from the core
        ``recovered`` service. Empty when the service is absent or unreadable,
        so the map then shows every marker gray."""
        store = self.context.services.get("recovered")
        if store is None:
            return set()
        try:
            return {bssid for bssid, _loc in points if store.lookup_bssid(bssid)}
        except Exception:  # noqa: BLE001 - a potfile problem never breaks the map
            return set()

    def _spot_list(self, points, spot, names, token, recovered=()):
        """The networks inside ``spot``: a cluster no zoom can separate."""
        esc = self.context.esc
        s, w, n, e = spot
        here = [(b, loc) for b, loc in points if s <= loc.lat <= n and w <= loc.lon <= e]
        if not here:
            return ""
        def glyph(symbol, text, width=""):
            # The glyphs Captured networks uses, each with a tooltip and hidden
            # text (the shared stylesheet's .vh) so it never rests on the glyph
            # alone; an empty symbol still tells a screen reader its text.
            style = ' style="display:inline-block;width:%s"' % width if width else ""
            return ('<span title="' + esc(text) + '"' + style + '>'
                    + ('<span aria-hidden="true">' + symbol + "</span>" if symbol else "")
                    + '<span class="vh">' + esc(text) + "</span></span>")

        rows = []
        for bssid, loc in here:
            href = "/networks?" + urlencode(
                [("view", "ap"), ("q", bssid)] + ([("token", token)] if token else []))
            done = bssid in recovered
            lock = glyph("&#128274;" if done else "", "passphrase recovered" if done
                         else "passphrase not recovered", width="1.4em")
            where = (glyph("&#128205;", "recorded, onboard GPS") if loc.recorded
                     else glyph("&#8776;&#9675;", "estimated, " + loc.label))
            rows.append('<li style="margin:.25em 0">' + lock + where + ' <a href="'
                        + esc(href) + '">'
                        + esc(", ".join(names.get(bssid) or ["(hidden network)"]))
                        + "</a> <small>" + esc(bssid) + "</small></li>")
        return ('<h2 id="spot">%d networks at this spot</h2>' % len(here)
                + '<p class="hint">Captured from the same place, so they share one '
                "location and no zoom separates them. &#128274; passphrase recovered "
                "&middot; &#128205; recorded by the onboard GPS &middot; &#8776;&#9675; "
                'estimated.</p><ul style="list-style:none;padding-left:0">'
                + "".join(rows) + "</ul>")

    def _url(self, which, bbox, token, base="", spot=None):
        params = []
        if which != "all":
            params.append(("f", which))
        if bbox:
            params.append(("bbox", ",".join("%.6f" % v for v in bbox)))
        if spot:
            params.append(("spot", ",".join("%.6f" % v for v in spot)))
        if token:
            params.append(("token", token))
        return (base or "./") + ("?" + urlencode(params) if params else "") + (
            "#spot" if spot else "")

    def _filter_bar(self, which, bbox, token):
        esc = self.context.esc
        parts = []
        for key, label in FILTERS:
            if key == which:
                parts.append("<b>" + esc(label) + "</b>")
            else:
                parts.append('<a href="' + esc(self._url(key, bbox, token)) + '">'
                             + esc(label) + "</a>")
        reset = ""
        if bbox:
            reset = (' &middot; <a href="' + esc(self._url(which, None, token))
                     + '"><b>Reset view</b></a>')
        return '<p class="smeta">Show: ' + " &middot; ".join(parts) + reset + "</p>"

    def _map(self, proj, groups, names, which, bbox, token, css, tiles, recovered=()):
        """One rendering: the tile layer (if any) under a see-through SVG."""
        ratio = "%d/%d" % (proj.w, proj.h)
        out = ['<div class="%s"><div style="position:relative;overflow:hidden;'
               "aspect-ratio:%s;background:var(--card);border:1px solid var(--border);"
               'border-radius:8px">' % (css, ratio)]
        if tiles:
            out.append(self._tiles(proj, tiles))
        out.append(self._svg(proj, groups, names, which, bbox, token, bool(tiles), recovered))
        if tiles:
            # The tile policy asks for the attribution on the map itself.
            out.append('<div style="position:absolute;right:0;bottom:0;font-size:11px;'
                       'line-height:1.4;padding:1px 5px;background:rgba(255,255,255,.8);'
                       'color:#222;border-top-left-radius:4px">&copy; <a style="color:#222" '
                       'href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> '
                       "contributors</div>")
        out.append("</div></div>")
        return "".join(out)

    def _tiles(self, proj, url):
        """``<img>`` tiles covering exactly the view, at the zoom whose tiles are
        nearest 256 viewBox units across (so never stretched more than ~1.4x).
        Lazy, so the rendering the stylesheet hides (wide or narrow) loads none;
        each sends only the origin as referrer."""
        esc = self.context.esc
        z = int(round(math.log2(max(proj.scale / 256.0, 1.0))))
        z = max(0, min(MAX_TILE_ZOOM, z))
        n = 2 ** z
        size = proj.scale / n                       # one tile, in viewBox units
        x0 = proj.cx - proj.w / 2.0 / proj.scale    # the view, in world units
        y0 = proj.cy - proj.h / 2.0 / proj.scale
        x1 = proj.cx + proj.w / 2.0 / proj.scale
        y1 = proj.cy + proj.h / 2.0 / proj.scale
        out = []
        for ty in range(max(0, int(math.floor(y0 * n))), min(n - 1, int(math.floor(y1 * n))) + 1):
            for tx in range(max(0, int(math.floor(x0 * n))), min(n - 1, int(math.floor(x1 * n))) + 1):
                left = (tx / float(n) - x0) * proj.scale / proj.w * 100.0
                top = (ty / float(n) - y0) * proj.scale / proj.h * 100.0
                src = url.replace("{z}", str(z)).replace("{x}", str(tx)).replace("{y}", str(ty))
                out.append('<img src="%s" alt="" loading="lazy" decoding="async" '
                           'referrerpolicy="origin" style="position:absolute;left:%.4f%%;'
                           "top:%.4f%%;width:calc(%.4f%% + 1px);height:calc(%.4f%% + 1px);"
                           'max-width:none">' % (esc(src), left, top, size / proj.w * 100.0,
                                                 size / proj.h * 100.0))
        return "".join(out)

    def _svg(self, proj, groups, names, which, bbox, token, tiles=False, recovered=()):
        esc = self.context.esc
        # Over tiles (light in both themes) the marks are dark with a light
        # halo; without tiles they follow the page's colours.
        colour = "#1b1b1f" if tiles else "var(--fg)"
        out = ['<svg viewBox="0 0 %d %d" role="img" '
               'aria-label="Map of located captured networks" '
               'style="position:absolute;inset:0;width:100%%;height:100%%;color:%s">'
               % (proj.w, proj.h, colour)]
        if not tiles:
            out.append(self._graticule(proj))
        for g in groups:
            members = g["members"]
            x, y = g["x"], g["y"]
            if len(members) == 1:
                bssid, loc, _, _ = members[0]
                done = bssid in recovered
                fill = RECOVERED_FILL if done else PLAIN_FILL
                title = "%s\n%s\n%.5f, %.5f\n%s%s\n%s" % (
                    ", ".join(names.get(bssid) or ["(hidden network)"]), bssid,
                    loc.lat, loc.lon, loc.label, (" · " + loc.when) if loc.when else "",
                    "passphrase recovered" if done else "passphrase not recovered")
                href = "/networks?" + urlencode(
                    [("view", "ap"), ("q", bssid)] + ([("token", token)] if token else []))
                if loc.recorded:
                    shape = ('<path d="M%.1f %.1f c-7 -9 -11 -14 -11 -20 a11 11 0 1 1 22 0 '
                             'c0 6 -4 11 -11 20 z" stroke="#fff" stroke-width="1.5" '
                             'fill="%s"/>' % (x, y, fill))
                    shape += (_lock(x, y - 20, "#fff") if done else
                              '<circle cx="%.1f" cy="%.1f" r="4" fill="#fff"/>' % (x, y - 20))
                else:
                    shape = ('<circle cx="%.1f" cy="%.1f" r="9" fill="#fff" fill-opacity="%s" '
                             'stroke="%s" stroke-width="2.5"/>'
                             '<text x="%.1f" y="%.1f" font-size="14" text-anchor="middle" '
                             'fill="%s">~</text>'
                             % (x, y, "0.85" if tiles else "0", fill, x, y + 4, fill))
                    if done:
                        shape += _lock_badge(x + 10, y - 10)
                out.append('<a href="' + esc(href) + '"><title>' + esc(title) + "</title>"
                           + shape + "</a>")
            else:
                lats = [m[1].lat for m in members]
                lons = [m[1].lon for m in members]
                pad = 0.25 * max(max(lats) - min(lats), max(lons) - min(lons), 0.0005)
                zoom = (max(-90, min(lats) - pad), max(-180, min(lons) - pad),
                        min(90, max(lats) + pad), min(180, max(lons) + pad))
                done = sum(1 for m in members if m[0] in recovered)
                fill = RECOVERED_FILL if done else PLAIN_FILL
                counts = "%d networks (%d recorded, %d estimated; %d recovered)" % (
                    len(members), g["recorded"], len(members) - g["recorded"], done)
                # Zooming in as far as the map goes would still leave them in
                # one square (one capture spot shares one fix): list them.
                deepest = Projection(zoom, margin=0.0, size=(proj.w, proj.h))
                if len(cluster([(m[0], m[1]) for m in members], deepest)) <= 1:
                    spot = (min(lats) - 1e-6, min(lons) - 1e-6,
                            max(lats) + 1e-6, max(lons) + 1e-6)
                    href = self._url(which, bbox, token, spot=spot)
                    title = counts + " at one spot - tap to list them"
                else:
                    href = self._url(which, zoom, token)
                    title = counts + " - tap to zoom in"
                out.append('<a href="' + esc(href) + '"><title>'
                           + esc(title) + "</title>"
                           '<rect x="%.1f" y="%.1f" width="30" height="30" rx="6" '
                           'stroke="#fff" stroke-width="1.5" '
                           'fill="%s" fill-opacity="0.92"/>'
                           '<text x="%.1f" y="%.1f" font-size="14" font-weight="700" '
                           'text-anchor="middle" fill="#fff">%s</text>'
                           % (x - 15, y - 15, fill, x, y + 5, esc(str(len(members))))
                           + (_lock_badge(x + 15, y - 15) if done else "") + "</a>")
        out.append(self._scale_bar(proj, tiles))
        halo = ('<rect x="%d" y="14" width="32" height="54" rx="6" fill="#fff" '
                'fill-opacity="0.75"/>' % (proj.w - 46)) if tiles else ""
        out.append('<g aria-hidden="true">%s<path d="M%d 22 l9 22 l-9 -6 l-9 6 z" '
                   'fill="currentColor"/><text x="%d" y="62" font-size="15" '
                   'text-anchor="middle" fill="currentColor">N</text></g>'
                   % (halo, proj.w - 30, proj.w - 30))
        out.append("</svg>")
        return "".join(out)

    def _graticule(self, proj):
        s, w, n, e = proj.view_bounds()
        lat_step = _grid_step(n - s)
        lon_step = _grid_step(e - w)
        parts = ['<g aria-hidden="true" stroke="currentColor" stroke-opacity="0.15" '
                 'stroke-width="1" font-size="12" fill="currentColor" fill-opacity="0.55">']
        lat = math.ceil(s / lat_step) * lat_step
        while lat <= n:
            _, y = proj.xy(lat, w)
            parts.append('<line x1="0" y1="%.1f" x2="%d" y2="%.1f"/>' % (y, proj.w, y))
            if 18 <= y <= proj.h - 24:       # keep labels clear of the edges
                parts.append('<text x="6" y="%.1f" stroke="none">%.*f&#176;</text>'
                             % (y - 4, _places(lat_step), lat))
            lat += lat_step
        lon = math.ceil(w / lon_step) * lon_step
        while lon <= e:
            x, _ = proj.xy(s, lon)
            parts.append('<line x1="%.1f" y1="0" x2="%.1f" y2="%d"/>' % (x, x, proj.h))
            if 4 <= x <= proj.w - 70:
                parts.append('<text x="%.1f" y="%d" stroke="none">%.*f&#176;</text>'
                             % (x + 4, proj.h - 8, _places(lon_step), lon))
            lon += lon_step
        parts.append("</g>")
        return "".join(parts)

    def _scale_bar(self, proj, tiles=False):
        metres_per_unit = proj.metres_per_unit()
        length_m = _nice(metres_per_unit * min(160, proj.w / 4.0))
        px = length_m / metres_per_unit
        label = ("%g km" % (length_m / 1000.0)) if length_m >= 1000 else ("%g m" % length_m)
        x0, y0 = 20, proj.h - 40
        halo = ('<rect x="%d" y="%d" width="%.1f" height="34" rx="5" fill="#fff" '
                'fill-opacity="0.75" stroke="none"/>'
                % (x0 - 8, y0 - 24, max(px, 9 * len(label)) + 16)) if tiles else ""
        return ('<g aria-hidden="true" fill="currentColor" stroke="currentColor">%s'
                '<rect x="%d" y="%d" width="%.1f" height="5" stroke="none"/>'
                '<text x="%d" y="%d" font-size="13" stroke="none">%s</text></g>'
                % (halo, x0, y0, px, x0, y0 - 6, label))
