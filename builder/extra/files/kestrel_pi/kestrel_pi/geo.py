"""The network-location core service: a captured network's latitude/longitude.

When a captured radio's position is known -- because the onboard GPS recorded a
fix at handshake time, or an external source (WiGLE today, not limited to it)
estimated where the radio is -- this surfaces it so the UI can annotate that
network on ``/networks``. Like :mod:`kestrel_pi.recovered`, the feature is
**plugin-agnostic**: the contribution points are files in the capture store, and
*any* producer that writes them is a source. Nothing here imports a plugin, calls
a plugin service, or checks whether ``gps``/``wigle`` are loaded; a device with
neither plugin simply has no files and shows nothing.

This is strictly for the documented offline analysis of networks you own or are
authorized to test (see ``CLAUDE.md``, "Authorized use only"): it displays
positions the operator already recorded or looked up and never derives a position
itself, cracks, deauthenticates, or acts on a network. A recorded location is
effectively where the operator was when they captured the handshake, so it is
privacy-sensitive; the web layer gates it behind the login and a hide toggle.

Two on-disk formats are read, both BSSID-keyed maps already written today
(pinned in ``docs/developer/capture-interface.md``, the "geo" section):

    recorded  -- <capture>.hc22000.gps.json : {BSSID: {lat, lon, alt?, fix_time,
                 source, ...}}  (source gpsd|serial|auto), written by ``gps``
    estimated -- geo/<name>-cache.json       : {BSSID: {bssid, lat, lon, source,
                 query_time}}   (source e.g. "wigle-search"), written by ``wigle``

The reader globs *every* ``*.gps.json`` sidecar and *every* ``geo/*.json`` cache,
so a brand-new producer is additive -- drop another file and it is read with no
code change. A missing, partial or corrupt file is skipped (never raised), so an
absent plugin is just an absent file. For one BSSID, a recorded fix beats an
estimated one, and among recorded fixes the newest (by ``fix_time``) wins.
"""

from __future__ import annotations

import json
import math
import os
import threading
from dataclasses import dataclass

from .capture import _format_mac

# Where the estimated caches live, relative to the capture store.
GEO_SUBDIR = "geo"
# The recorded-fix sidecar suffix (each next to its ``.hc22000`` capture file).
SIDECAR_SUFFIX = ".gps.json"

# Display precision for coordinates in the UI (~1 m at the equator). The map
# link uses the full stored precision; only the shown text is rounded.
DISPLAY_PLACES = 5

# source string -> (class, friendly label). An *unknown* source falls back to
# "estimated" with a label derived from the string, so a new external producer
# shows up with no code change ("not limited to WiGLE", by construction). The
# recorded transports are the ``gps`` plugin's: ``auto`` resolves to the
# gpsd/serial it actually used, but may still be recorded literally.
_SOURCES = {
    "gpsd": ("recorded", "onboard GPS"),
    "serial": ("recorded", "onboard GPS"),
    "auto": ("recorded", "onboard GPS"),
    "wigle-search": ("estimated", "WiGLE"),
}

RECORDED = "recorded"
ESTIMATED = "estimated"

_HEX = frozenset("0123456789abcdefABCDEF")


def _normalize_mac(key):
    """The bare lower-hex form of a MAC-shaped key, or ``""`` if not MAC-shaped.

    Producers key these maps by the capture layer's ``AA:BB:CC:DD:EE:FF`` form,
    but this tolerates the hyphen/dot/colon-separated variants too so a lookup
    matches regardless of casing or separator; a non-MAC key (the sidecar's
    ``"unknown"`` slot) normalizes to ``""`` and is skipped."""
    stripped = str(key).strip().replace("-", "").replace(".", "").replace(":", "")
    if len(stripped) == 12 and all(c in _HEX for c in stripped):
        return stripped.lower()
    return ""


def classify(source):
    """Map a ``source`` string to ``(cls, label)``.

    ``cls`` is ``"recorded"`` or ``"estimated"``. A known source uses its
    registered label; an unknown (or missing) source is ``"estimated"`` with a
    label derived from the string -- the bare source for display, or a generic
    ``"external"`` when there is nothing to show."""
    key = (source or "").strip()
    hit = _SOURCES.get(key)
    if hit is not None:
        return hit
    return (ESTIMATED, key or "external")


def _coord(value):
    """``value`` as a finite float, or ``None`` if it is not a real number."""
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(out):
        return None
    return out


def fmt_coord(value, places=DISPLAY_PLACES):
    """A coordinate formatted for display at ``places`` decimals, or ``""`` when
    it is not a finite number."""
    out = _coord(value)
    if out is None:
        return ""
    return "%.*f" % (places, out)


def osm_url(lat, lon):
    """An OpenStreetMap marker URL for ``lat``/``lon`` at full stored precision,
    or ``""`` when either coordinate is not a finite number. The URL carries only
    the coordinate -- never a BSSID or ESSID."""
    flat, flon = _coord(lat), _coord(lon)
    if flat is None or flon is None:
        return ""
    return ("https://www.openstreetmap.org/?mlat=%s&mlon=%s#map=16/%s/%s"
            % (flat, flon, flat, flon))


@dataclass
class Location:
    """One known position for a radio.

    ``cls`` is ``"recorded"`` or ``"estimated"``; ``label`` is the friendly
    source (e.g. "onboard GPS", "WiGLE"); ``source`` is the raw source string;
    ``when`` is the fix/query timestamp (ISO-8601 Z string) or ``""``. ``lat`` and
    ``lon`` are finite floats in range -- a record that is not is dropped before a
    :class:`Location` is built."""

    lat: float
    lon: float
    cls: str
    source: str
    label: str
    when: str = ""

    @property
    def recorded(self):
        return self.cls == RECORDED

    def osm_url(self):
        return osm_url(self.lat, self.lon)


def _make_location(record):
    """Build a :class:`Location` from a raw on-disk record (sidecar fix or geo
    cache entry), or ``None`` when its coordinates are missing/out of range."""
    if not isinstance(record, dict):
        return None
    lat = _coord(record.get("lat"))
    lon = _coord(record.get("lon"))
    if lat is None or lon is None:
        return None
    if not (-90.0 <= lat <= 90.0) or not (-180.0 <= lon <= 180.0):
        return None
    source = record.get("source") or ""
    cls, label = classify(source)
    when = record.get("fix_time") or record.get("query_time") or ""
    return Location(lat=lat, lon=lon, cls=cls, source=str(source),
                    label=label, when=str(when))


class GeoStore:
    """Read-only view of the location files in the capture store.

    Owned by the daemon and read by the web layer; never imports a plugin. The
    data is the files, not any plugin API, so the feature does not depend on a
    plugin being loaded. Parsing is cached on the files' name/size/mtime
    signature -- the same pattern :class:`~kestrel_pi.capture.CaptureInterface`
    and :class:`~kestrel_pi.recovered.RecoveredStore` use -- so rendering a page
    is cheap no matter how often it is asked.
    """

    def __init__(self, store_dir):
        self.store_dir = store_dir
        self.geo_dir = os.path.join(store_dir, GEO_SUBDIR)
        # (signature, by_bssid) cache; guarded because the web server is threaded.
        self._cache = None
        self._lock = threading.Lock()

    def _sidecar_files(self):
        """Every ``*.gps.json`` recorded-fix sidecar in the store (sorted)."""
        try:
            names = os.listdir(self.store_dir)
        except OSError:
            return []
        return sorted(os.path.join(self.store_dir, n)
                      for n in names if n.endswith(SIDECAR_SUFFIX))

    def _geo_files(self):
        """Every ``*.json`` estimated cache in ``<store>/geo/`` (sorted)."""
        try:
            names = os.listdir(self.geo_dir)
        except OSError:
            return []
        return sorted(os.path.join(self.geo_dir, n)
                      for n in names if n.endswith(".json"))

    @staticmethod
    def _load_map(path):
        """A BSSID-keyed map parsed from ``path``, or ``{}`` on any error. A
        hostile or truncated file never raises."""
        try:
            with open(path, "r", encoding="utf-8") as handle:
                data = json.load(handle)
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def _parse(self):
        """Parse every location file once into ``{BSSID: Location}``, applying
        recorded-beats-estimated and (among recorded fixes) newest-by-``fix_time``.

        Cached on the files' signature and reused until one changes. The BSSID is
        normalized to ``AA:BB:CC:DD:EE:FF`` so a lookup matches regardless of the
        producer's casing; a non-MAC key (e.g. the sidecar's ``"unknown"`` slot)
        is skipped."""
        sidecars = self._sidecar_files()
        caches = self._geo_files()
        signature = []
        for path in sidecars + caches:
            try:
                st = os.stat(path)
            except OSError:
                continue
            signature.append((path, st.st_size, st.st_mtime_ns))
        signature = tuple(signature)
        with self._lock:
            cached = self._cache
        if cached is not None and cached[0] == signature:
            return cached[1]

        # Estimated first (lowest priority), then recorded overrides it. Within
        # each class the newest timestamp wins: geo caches compare on
        # ``query_time`` (via ``when``), recorded fixes on ``fix_time``.
        by_bssid = {}
        best_when = {}

        def consider(bssid, loc):
            key = _format_mac(_normalize_mac(bssid))
            if not key or loc is None:
                return
            current = by_bssid.get(key)
            if current is None:
                by_bssid[key] = loc
                best_when[key] = loc.when
                return
            # A recorded fix always beats an estimated one, regardless of time.
            if current.recorded and not loc.recorded:
                return
            if loc.recorded and not current.recorded:
                by_bssid[key] = loc
                best_when[key] = loc.when
                return
            # Same class: keep the newer timestamp (ISO-8601 Z sorts lexically).
            if loc.when >= best_when.get(key, ""):
                by_bssid[key] = loc
                best_when[key] = loc.when

        for path in caches:
            for bssid, record in self._load_map(path).items():
                consider(bssid, _make_location(record))
        for path in sidecars:
            for bssid, record in self._load_map(path).items():
                consider(bssid, _make_location(record))

        with self._lock:
            self._cache = (signature, by_bssid)
        return by_bssid

    def locations(self):
        """Every known location as ``{BSSID: Location}`` (a copy), with the same
        recorded-beats-estimated choice as :meth:`lookup_bssid`. For a view of
        all of them at once, such as the ``map`` plugin."""
        return dict(self._parse())

    def lookup_bssid(self, bssid):
        """The :class:`Location` for ``bssid`` (normalized before lookup), or
        ``None``. The exact, unambiguous by-radio join."""
        if not bssid:
            return None
        key = _format_mac(_normalize_mac(bssid))
        if not key:
            return None
        return self._parse().get(key)

    def counts(self):
        """``{"located_aps": int}`` -- distinct BSSIDs with any known location. A
        diagnostic total (it counts radios with no matching captured handshake
        too); any "located N of M captured" figure is computed at the capture
        join, not here."""
        return {"located_aps": len(self._parse())}
