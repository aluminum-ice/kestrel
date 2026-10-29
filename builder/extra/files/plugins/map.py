"""map - every located captured network on one map, drawn on the device.

A row button on the Plugins page ("Map") opens a page with an SVG map of every
captured network whose location is known: recorded by the onboard GPS (the
`gps` plugin's sidecars) or estimated from an outside source such as WiGLE. It
is the all-at-once view of the per-row locations on the Captured networks page.

There are no map tiles and no JavaScript: the page draws its own light grid of
latitude/longitude lines, a scale bar and a north arrow, so no tile server ever
learns which areas you have been capturing in. Nearby points are grouped into a
counted cluster; following a cluster's link zooms the page to it, and a single
point links to its row on the Captured networks page (which has its
OpenStreetMap link).

Display only: it reads the location files already in the capture store through
the core reader (``kestrel_pi.geo``) and sends nothing anywhere, so it needs no
authorized-use acknowledgment. Locations are treated like the rest of the
dashboard treats them: shown only with the web login on, and hidden while
``[web] show_locations`` is off (read live from the config).

The projection is equirectangular around the data's mean latitude, accurate at
the city-to-region scale this is for; a set of points spanning the
antimeridian is not supported.
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
METRES_PER_DEG = 111320.0
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


class Projection:
    """Equirectangular around ``lat0``, fitted to ``bounds`` with a margin."""

    def __init__(self, bounds, margin=0.1, min_span_deg=0.002, size=WIDE):
        self.w, self.h = size
        s, w, n, e = bounds
        self.lat0 = (s + n) / 2.0
        self.k = math.cos(math.radians(self.lat0)) or 1e-6
        cy, cx = (s + n) / 2.0, (w + e) / 2.0
        span_y = max(n - s, min_span_deg) * (1 + 2 * margin)
        span_x = max((e - w) * self.k, min_span_deg * self.k) * (1 + 2 * margin)
        self.scale = min(self.w / span_x, self.h / span_y)   # viewBox units per degree-lat
        self.cx, self.cy = cx, cy

    def xy(self, lat, lon):
        return (self.w / 2.0 + (lon - self.cx) * self.k * self.scale,
                self.h / 2.0 - (lat - self.cy) * self.scale)

    def latlon(self, x, y):
        return (self.cy - (y - self.h / 2.0) / self.scale,
                self.cx + (x - self.w / 2.0) / (self.k * self.scale))

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
    __options__ = {}

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
        esc = self.context.esc
        head = "<h1>Map</h1>"
        if not getattr(self.context, "web_authed", False):
            return self.context.page("Map", head + '<p class="msg err">The map needs '
                                     "the dashboard login on. Turn it on with <code>sudo "
                                     "kestrel web auth system</code>.</p>")
        if not self._locations_shown():
            return self.context.page("Map", head + '<p class="msg">Locations are hidden '
                                     "(<code>[web] show_locations = false</code>). Turn "
                                     "them back on in Advanced settings to see the map.</p>")
        points = self._points(which)
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
        margin = 0.1 if bbox is None else 0.0
        shown = 0
        for size, css in ((WIDE, "wide-only"), (NARROW, "narrow-only")):
            proj = Projection(bounds, margin=margin, size=size)
            groups = cluster(points, proj)
            if size == WIDE:
                shown = sum(len(g["members"]) for g in groups)
            body += self._svg(proj, groups, names, which, token, css)
        body += ('<p class="hint">' + esc("%d of %d located networks in view" %
                                          (shown, len(points)))
                 + (" &middot; <a href=\"" + esc(self._url(which, None, token))
                    + '">Show all</a>' if bbox else "") + "</p>")
        body += ('<p class="hint"><b>&#9679; Pin</b>: recorded by the onboard GPS. '
                 "<b>&#9675; ~</b>: estimated from an outside source. <b>Square with a "
                 "number</b>: several networks close together (tap to zoom in). A single "
                 "network links to its row on Captured networks.</p>")
        return self.context.page("Map", body)

    def _url(self, which, bbox, token, base=""):
        params = []
        if which != "all":
            params.append(("f", which))
        if bbox:
            params.append(("bbox", ",".join("%.6f" % v for v in bbox)))
        if token:
            params.append(("token", token))
        return (base or "./") + ("?" + urlencode(params) if params else "")

    def _filter_bar(self, which, bbox, token):
        esc = self.context.esc
        parts = []
        for key, label in FILTERS:
            if key == which:
                parts.append("<b>" + esc(label) + "</b>")
            else:
                parts.append('<a href="' + esc(self._url(key, bbox, token)) + '">'
                             + esc(label) + "</a>")
        return '<p class="smeta">Show: ' + " &middot; ".join(parts) + "</p>"

    def _svg(self, proj, groups, names, which, token, css=""):
        esc = self.context.esc
        out = ['<div class="%s"><svg viewBox="0 0 %d %d" width="100%%" role="img" '
               'aria-label="Map of located captured networks" '
               'style="display:block;max-width:100%%;height:auto;color:var(--fg);'
               'background:var(--card);border:1px solid var(--border);border-radius:8px">'
               % (css, proj.w, proj.h)]
        out.append(self._graticule(proj))
        for g in groups:
            members = g["members"]
            x, y = g["x"], g["y"]
            if len(members) == 1:
                bssid, loc, _, _ = members[0]
                title = "%s\n%s\n%.5f, %.5f\n%s%s" % (
                    ", ".join(names.get(bssid) or ["(hidden network)"]), bssid,
                    loc.lat, loc.lon, loc.label, (" · " + loc.when) if loc.when else "")
                href = "/networks?" + urlencode(
                    [("view", "ap"), ("q", bssid)] + ([("token", token)] if token else []))
                if loc.recorded:
                    shape = ('<path d="M%.1f %.1f c-7 -9 -11 -14 -11 -20 a11 11 0 1 1 22 0 '
                             'c0 6 -4 11 -11 20 z" style="fill:var(--accent)"/>'
                             '<circle cx="%.1f" cy="%.1f" r="4" style="fill:var(--card)"/>'
                             % (x, y, x, y - 20))
                else:
                    shape = ('<circle cx="%.1f" cy="%.1f" r="9" fill="none" '
                             'stroke="currentColor" stroke-width="2.5"/>'
                             '<text x="%.1f" y="%.1f" font-size="14" text-anchor="middle" '
                             'fill="currentColor">~</text>' % (x, y, x, y + 4))
                out.append('<a href="' + esc(href) + '"><title>' + esc(title) + "</title>"
                           + shape + "</a>")
            else:
                lats = [m[1].lat for m in members]
                lons = [m[1].lon for m in members]
                pad = 0.25 * max(max(lats) - min(lats), max(lons) - min(lons), 0.0005)
                zoom = (max(-90, min(lats) - pad), max(-180, min(lons) - pad),
                        min(90, max(lats) + pad), min(180, max(lons) + pad))
                title = "%d networks (%d recorded, %d estimated) - tap to zoom in" % (
                    len(members), g["recorded"], len(members) - g["recorded"])
                out.append('<a href="' + esc(self._url(which, zoom, token)) + '"><title>'
                           + esc(title) + "</title>"
                           '<rect x="%.1f" y="%.1f" width="30" height="30" rx="6" '
                           'style="fill:var(--accent)" fill-opacity="0.85"/>'
                           '<text x="%.1f" y="%.1f" font-size="14" font-weight="700" '
                           'text-anchor="middle" style="fill:var(--card)">%s</text></a>'
                           % (x - 15, y - 15, x, y + 5, esc(str(len(members)))))
        out.append(self._scale_bar(proj))
        out.append('<g aria-hidden="true"><path d="M%d 22 l9 22 l-9 -6 l-9 6 z" '
                   'fill="currentColor"/><text x="%d" y="62" font-size="15" '
                   'text-anchor="middle" fill="currentColor">N</text></g>'
                   % (proj.w - 30, proj.w - 30))
        out.append("</svg></div>")
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

    def _scale_bar(self, proj):
        metres_per_unit = METRES_PER_DEG / proj.scale
        length_m = _nice(metres_per_unit * min(160, proj.w / 4.0))
        px = length_m / metres_per_unit
        label = ("%g km" % (length_m / 1000.0)) if length_m >= 1000 else ("%g m" % length_m)
        x0, y0 = 20, proj.h - 40
        return ('<g aria-hidden="true" fill="currentColor" stroke="currentColor">'
                '<rect x="%d" y="%d" width="%.1f" height="5" stroke="none"/>'
                '<text x="%d" y="%d" font-size="13" stroke="none">%s</text></g>'
                % (x0, y0, px, x0, y0 - 6, label))
