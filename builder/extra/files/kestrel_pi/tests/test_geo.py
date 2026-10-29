"""Tests for the network-location core service (:mod:`kestrel_pi.geo`).

All data here is invented, per CLAUDE.md "Nothing real in the repo": RFC 7042
``00:00:5E:00:53:xx`` MACs (bare-hex ``00005e0053xx`` / colon ``00:00:5E:...``
forms) and generic coordinates around ``12.34567, -45.67890``. No real SSIDs,
BSSIDs or positions.
"""

import json
import os

from kestrel_pi import geo
from kestrel_pi.geo import (
    GeoStore,
    Location,
    classify,
    fmt_coord,
    osm_url,
)

# Invented placeholders.
BSSID_A = "00:00:5E:00:53:AA"
BSSID_A_BARE = "00005e0053aa"
BSSID_B = "00:00:5E:00:53:BB"
BSSID_B_BARE = "00005e0053bb"

LAT = 12.34567
LON = -45.67890


# -- classify --------------------------------------------------------------

def test_classify_recorded_transports():
    for src in ("gpsd", "serial", "auto"):
        cls, label = classify(src)
        assert cls == "recorded"
        assert label == "onboard GPS"


def test_classify_known_estimated_source():
    assert classify("wigle-search") == ("estimated", "WiGLE")


def test_classify_unknown_source_is_estimated_with_derived_label():
    # A brand-new producer shows up as estimated with no code change.
    assert classify("some-new-provider") == ("estimated", "some-new-provider")


def test_classify_missing_source_is_generic_external():
    assert classify("") == ("estimated", "external")
    assert classify(None) == ("estimated", "external")


# -- fmt_coord / osm_url ---------------------------------------------------

def test_fmt_coord_five_places():
    assert fmt_coord(12.3456789) == "12.34568"
    assert fmt_coord(-45.6) == "-45.60000"
    assert fmt_coord(0) == "0.00000"


def test_fmt_coord_non_number_is_blank():
    assert fmt_coord("not a number") == ""
    assert fmt_coord(None) == ""
    assert fmt_coord(float("nan")) == ""
    assert fmt_coord(float("inf")) == ""


def test_osm_url_uses_full_precision_and_only_coords():
    url = osm_url(LAT, LON)
    assert url.startswith("https://www.openstreetmap.org/")
    assert "mlat=12.34567" in url
    assert "mlon=-45.6789" in url
    assert "#map=16/12.34567/-45.6789" in url
    # The URL carries nothing but the coordinate.
    assert "00" not in url.split("#")[0].replace("mlat=", "").replace("mlon=", "")


def test_osm_url_bad_coords_is_blank():
    assert osm_url(None, LON) == ""
    assert osm_url(LAT, "x") == ""


# -- fixtures --------------------------------------------------------------

def _write_sidecar(store_dir, name, mapping):
    """Write a ``<name>.hc22000.gps.json`` sidecar (BSSID-keyed map of fixes)."""
    path = os.path.join(store_dir, name + ".hc22000.gps.json")
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(mapping, handle)
    return path


def _write_geo_cache(store_dir, name, mapping):
    """Write a ``geo/<name>.json`` estimated cache (BSSID-keyed map of records)."""
    geo_dir = os.path.join(store_dir, "geo")
    os.makedirs(geo_dir, exist_ok=True)
    path = os.path.join(geo_dir, name + ".json")
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(mapping, handle)
    return path


def _recorded(lat, lon, fix_time, source="gpsd"):
    return {"lat": lat, "lon": lon, "alt": 100.0,
            "fix_time": fix_time, "source": source}


def _estimated(bssid, lat, lon, query_time, source="wigle-search"):
    return {"bssid": bssid, "lat": lat, "lon": lon,
            "source": source, "query_time": query_time}


# -- GeoStore --------------------------------------------------------------

def test_store_empty_dir_is_benign(tmp_path):
    store = GeoStore(str(tmp_path))
    assert store.counts() == {"located_aps": 0}
    assert store.lookup_bssid(BSSID_A) is None
    assert store.lookup_bssid("") is None


def test_store_recorded_sidecar_lookup(tmp_path):
    _write_sidecar(str(tmp_path), "cap-1",
                   {BSSID_A: _recorded(LAT, LON, "2026-01-01T00:00:00Z")})
    store = GeoStore(str(tmp_path))
    loc = store.lookup_bssid(BSSID_A)
    assert loc is not None
    assert loc.cls == "recorded"
    assert loc.label == "onboard GPS"
    assert loc.lat == LAT and loc.lon == LON
    assert loc.recorded is True
    assert store.counts() == {"located_aps": 1}


def test_store_estimated_cache_lookup(tmp_path):
    _write_geo_cache(str(tmp_path), "wigle-cache",
                     {BSSID_A: _estimated(BSSID_A, LAT, LON,
                                          "2026-01-01T00:00:00Z")})
    store = GeoStore(str(tmp_path))
    loc = store.lookup_bssid(BSSID_A)
    assert loc.cls == "estimated"
    assert loc.label == "WiGLE"
    assert loc.recorded is False


def test_store_recorded_beats_estimated_regardless_of_time(tmp_path):
    # The estimated fix is newer, but a recorded fix still wins.
    _write_geo_cache(str(tmp_path), "wigle-cache",
                     {BSSID_A: _estimated(BSSID_A, 1.0, 1.0,
                                          "2026-06-01T00:00:00Z")})
    _write_sidecar(str(tmp_path), "cap-1",
                   {BSSID_A: _recorded(LAT, LON, "2026-01-01T00:00:00Z")})
    store = GeoStore(str(tmp_path))
    loc = store.lookup_bssid(BSSID_A)
    assert loc.cls == "recorded"
    assert loc.lat == LAT


def test_store_newest_recorded_fix_wins(tmp_path):
    # The same BSSID across two capture files: newest fix_time wins.
    _write_sidecar(str(tmp_path), "cap-old",
                   {BSSID_A: _recorded(1.0, 2.0, "2026-01-01T00:00:00Z")})
    _write_sidecar(str(tmp_path), "cap-new",
                   {BSSID_A: _recorded(LAT, LON, "2026-05-05T12:00:00Z")})
    store = GeoStore(str(tmp_path))
    loc = store.lookup_bssid(BSSID_A)
    assert loc.lat == LAT and loc.lon == LON


def test_store_newest_estimated_wins_across_caches(tmp_path):
    _write_geo_cache(str(tmp_path), "a-cache",
                     {BSSID_A: _estimated(BSSID_A, 1.0, 2.0,
                                          "2026-01-01T00:00:00Z")})
    _write_geo_cache(str(tmp_path), "b-cache",
                     {BSSID_A: _estimated(BSSID_A, LAT, LON,
                                          "2026-09-09T00:00:00Z",
                                          source="other-provider")})
    store = GeoStore(str(tmp_path))
    loc = store.lookup_bssid(BSSID_A)
    assert loc.lat == LAT
    assert loc.cls == "estimated"
    assert loc.label == "other-provider"   # unknown source, derived label


def test_store_lookup_normalizes_input(tmp_path):
    _write_sidecar(str(tmp_path), "cap-1",
                   {BSSID_A: _recorded(LAT, LON, "2026-01-01T00:00:00Z")})
    store = GeoStore(str(tmp_path))
    assert store.lookup_bssid(BSSID_A) is not None
    assert store.lookup_bssid(BSSID_A_BARE) is not None       # bare hex
    assert store.lookup_bssid("00:00:5e:00:53:aa") is not None  # lower colon
    assert store.lookup_bssid(BSSID_B) is None


def test_store_skips_non_mac_keys(tmp_path):
    # The sidecar's "unknown" slot (a handshake with no parseable BSSID) is not
    # a radio and must not produce a location.
    _write_sidecar(str(tmp_path), "cap-1",
                   {"unknown": _recorded(LAT, LON, "2026-01-01T00:00:00Z"),
                    BSSID_A: _recorded(LAT, LON, "2026-01-01T00:00:00Z")})
    store = GeoStore(str(tmp_path))
    assert store.counts() == {"located_aps": 1}


def test_store_out_of_range_and_bad_coords_dropped(tmp_path):
    _write_sidecar(str(tmp_path), "cap-1", {
        BSSID_A: _recorded(999.0, LON, "2026-01-01T00:00:00Z"),      # bad lat
        BSSID_B: _recorded(LAT, "nope", "2026-01-01T00:00:00Z"),     # bad lon
    })
    store = GeoStore(str(tmp_path))
    assert store.lookup_bssid(BSSID_A) is None
    assert store.lookup_bssid(BSSID_B) is None
    assert store.counts() == {"located_aps": 0}


def test_store_tolerates_missing_and_corrupt_files(tmp_path):
    # A corrupt sidecar and a corrupt cache: skipped, never raised.
    with open(os.path.join(str(tmp_path), "bad.hc22000.gps.json"), "w",
              encoding="utf-8") as handle:
        handle.write("{ not json")
    os.makedirs(os.path.join(str(tmp_path), "geo"), exist_ok=True)
    with open(os.path.join(str(tmp_path), "geo", "bad.json"), "w",
              encoding="utf-8") as handle:
        handle.write("}}}")
    # A good sidecar alongside the bad one still reads.
    _write_sidecar(str(tmp_path), "cap-1",
                   {BSSID_A: _recorded(LAT, LON, "2026-01-01T00:00:00Z")})
    store = GeoStore(str(tmp_path))
    assert store.counts() == {"located_aps": 1}
    assert store.lookup_bssid(BSSID_A) is not None


def test_store_merges_multiple_sidecars(tmp_path):
    _write_sidecar(str(tmp_path), "cap-1",
                   {BSSID_A: _recorded(LAT, LON, "2026-01-01T00:00:00Z")})
    _write_sidecar(str(tmp_path), "cap-2",
                   {BSSID_B: _recorded(1.0, 2.0, "2026-01-02T00:00:00Z")})
    store = GeoStore(str(tmp_path))
    assert store.counts() == {"located_aps": 2}
    assert store.lookup_bssid(BSSID_A).lat == LAT
    assert store.lookup_bssid(BSSID_B).lat == 1.0


def test_store_non_dict_file_is_skipped(tmp_path):
    # A JSON file that is a list, not a BSSID map, is ignored.
    path = os.path.join(str(tmp_path), "list.hc22000.gps.json")
    with open(path, "w", encoding="utf-8") as handle:
        json.dump([1, 2, 3], handle)
    store = GeoStore(str(tmp_path))
    assert store.counts() == {"located_aps": 0}


def test_location_osm_url_method():
    loc = Location(lat=LAT, lon=LON, cls="recorded",
                   source="gpsd", label="onboard GPS", when="")
    assert loc.osm_url() == osm_url(LAT, LON)


def test_cache_invalidates_on_new_file(tmp_path):
    store = GeoStore(str(tmp_path))
    assert store.counts() == {"located_aps": 0}       # primes the cache
    _write_sidecar(str(tmp_path), "cap-1",
                   {BSSID_A: _recorded(LAT, LON, "2026-01-01T00:00:00Z")})
    assert store.counts() == {"located_aps": 1}       # signature changed


def test_locations_lists_every_radio_with_the_same_choice_as_lookup(tmp_path):
    _write_sidecar(str(tmp_path), "My Network",
                   {BSSID_A_BARE: _recorded(LAT, LON, "2026-01-02T03:04:05Z")})
    _write_geo_cache(str(tmp_path), "wigle-cache", {
        BSSID_A: _estimated(BSSID_A, LAT + 1, LON + 1, "2026-01-03T00:00:00Z"),
        BSSID_B: _estimated(BSSID_B, LAT - 1, LON - 1, "2026-01-03T00:00:00Z"),
    })
    store = GeoStore(str(tmp_path))
    found = store.locations()
    assert set(found) == {BSSID_A, BSSID_B}
    assert found[BSSID_A].recorded and found[BSSID_A] == store.lookup_bssid(BSSID_A)
    assert not found[BSSID_B].recorded
    found.clear()                                   # a copy: the cache is untouched
    assert len(store.locations()) == 2
