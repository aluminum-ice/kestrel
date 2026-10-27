"""The capture collector's environment (in-range) readout.

``capture-collect.py`` is an installed script (hyphenated, run as ``__main__``
on the device), so it is loaded here by path. These cover the kismet-DB query
(distinct transmitting radios in the most recent window of packet data) and the
small status file it writes into the store - the device-independent parts; the
live DB schema is verified on hardware (see the design note)."""

import importlib.util
import json
import os
import sqlite3
import time

import pytest

_HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_COLLECT = os.path.join(os.path.dirname(_HERE), "capture-collect.py")


def _load():
    spec = importlib.util.spec_from_file_location("capture_collect", _COLLECT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def mod(tmp_path):
    m = _load()
    m.RUN_DIR = str(tmp_path / "run")
    m.STORE_DIR = str(tmp_path / "store")
    os.makedirs(m.RUN_DIR)
    os.makedirs(m.STORE_DIR)
    return m


def _make_kismet(run_dir, rows, name="kestrel.kismet"):
    """Write a minimal kismetdb-shaped ``packets`` table (the table AngryOxide
    actually fills; it leaves ``devices`` empty). ``rows`` is
    ``(ts_sec, sourcemac)`` tuples."""
    path = os.path.join(run_dir, name)
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE packets (ts_sec INTEGER, sourcemac TEXT)")
    conn.executemany("INSERT INTO packets VALUES (?, ?)", rows)
    conn.commit()
    conn.close()
    return path


def test_environment_snapshot_counts_recent_distinct_radios(mod):
    # The window is anchored to the DB's OWN newest packet, not wall-clock: the
    # newest row here is at 1000, so a 60 s window is [940, 1000].
    _make_kismet(mod.RUN_DIR, [
        (1000, "aa:00"),   # newest
        (970, "aa:01"),    # in window
        (960, "aa:00"),    # in window, duplicate radio
        (900, "aa:02"),    # too old
        (995, "00:00:00:00:00:00"),  # null transmitter, excluded
    ])
    radios, data_ts = mod.environment_snapshot(60)
    assert radios == 2 and data_ts == 1000


def test_environment_snapshot_none_without_a_db(mod):
    assert mod.environment_snapshot(60) is None


def test_environment_snapshot_none_when_no_packets(mod):
    # DB exists but AngryOxide hasn't flushed any packets yet.
    _make_kismet(mod.RUN_DIR, [])
    assert mod.environment_snapshot(60) is None


def test_update_environment_writes_counts(mod):
    now = int(time.time())
    _make_kismet(mod.RUN_DIR, [(now, "aa:00"), (now, "cc:00")])
    mod.update_environment(300)
    data = json.loads(open(os.path.join(mod.STORE_DIR, mod.STATUS_NAME)).read())
    assert data["scanning"] is True and data["window_s"] == 300
    assert data["radios_recent"] == 2 and data["data_ts"] == now


def test_update_environment_marks_scanning_when_db_unreadable(mod):
    # No DB yet: still writes a scanning marker, but with no counts, so the UI
    # shows "scanning" rather than a stale/wrong number.
    mod.update_environment(300)
    data = json.loads(open(os.path.join(mod.STORE_DIR, mod.STATUS_NAME)).read())
    assert data["scanning"] is True and "radios_recent" not in data


def test_environment_snapshot_busy_when_db_stays_locked(mod, monkeypatch):
    # A dense environment keeps AngryOxide writing, so a read can hit a locked
    # DB. That must not read as "no data" (which blanked the card forever); it
    # returns the _BUSY sentinel so the caller keeps the last good count.
    _make_kismet(mod.RUN_DIR, [(1000, "aa:00")])

    def locked(*_a, **_k):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(mod.sqlite3, "connect", locked)
    assert mod.environment_snapshot(60, retries=1) is mod._BUSY


def test_environment_snapshot_none_on_structural_error(mod, monkeypatch):
    # A non-lock operational error (e.g. a missing table) is structural, not
    # transient: give up with None rather than retrying or signalling busy.
    _make_kismet(mod.RUN_DIR, [(1000, "aa:00")])

    def broken(*_a, **_k):
        raise sqlite3.OperationalError("no such table: packets")

    monkeypatch.setattr(mod.sqlite3, "connect", broken)
    assert mod.environment_snapshot(60) is None


def test_update_environment_keeps_last_count_when_db_locked(mod, monkeypatch):
    # First a good read populates the count; then the DB goes busy. The card
    # must keep the last count (freshness ages via data_ts) instead of blanking.
    now = int(time.time())
    _make_kismet(mod.RUN_DIR, [(now, "aa:00"), (now, "cc:00")])
    mod.update_environment(300)
    monkeypatch.setattr(mod, "environment_snapshot", lambda *_a, **_k: mod._BUSY)
    mod.update_environment(300)
    data = json.loads(open(os.path.join(mod.STORE_DIR, mod.STATUS_NAME)).read())
    assert data["scanning"] is True
    assert data["radios_recent"] == 2 and data["data_ts"] == now


def test_update_environment_keeps_last_count_across_relaunch(mod, monkeypatch):
    # The launcher clears the run dir and starts a fresh (empty) kismet DB on
    # every AngryOxide relaunch, so environment_snapshot returns None (no packets
    # yet). With a prior good reading, the count must be carried through rather
    # than blanked to "scanning" on every relaunch - otherwise the card flaps.
    now = int(time.time())
    _make_kismet(mod.RUN_DIR, [(now, "aa:00"), (now, "cc:00")])
    mod.update_environment(300)
    monkeypatch.setattr(mod, "environment_snapshot", lambda *_a, **_k: None)
    mod.update_environment(300)
    data = json.loads(open(os.path.join(mod.STORE_DIR, mod.STATUS_NAME)).read())
    assert data["scanning"] is True
    assert data["radios_recent"] == 2 and data["data_ts"] == now


def test_status_file_is_not_a_hashline_file(mod):
    # It lands in the store but must never be mistaken for capture data.
    mod.update_environment(60)
    names = os.listdir(mod.STORE_DIR)
    assert mod.STATUS_NAME in names
    assert not any(n.endswith(".hc22000") for n in names)


# --- target_for: routing a hashline to its store file --------------------

def _hashline(bssid, essid, type_="02", sta="00:11:22:33:44:55"):
    """One hc22000 line: WPA*TYPE*HASH*BSSID*STA*ESSID_HEX*... ``essid`` may be a
    str (UTF-8 encoded to the hex field) or raw bytes (for non-UTF-8 names)."""
    raw = essid.encode("utf-8") if isinstance(essid, str) else essid
    return "*".join(["WPA", type_, "deadbeef", bssid, sta, raw.hex(), "", ""])


def test_target_for_single_mode_combines(mod):
    line = _hashline("aa:bb:cc:dd:ee:ff", "poswifi")
    assert mod.target_for(line, "single", "all.hc22000") == "all.hc22000"


def test_target_for_per_ssid_merges_reused_names(mod):
    # The old default: two unrelated APs sharing the name "poswifi" land in the
    # SAME file -- exactly what per-ap fixes.
    a = _hashline("aa:bb:cc:dd:ee:ff", "poswifi")
    b = _hashline("11:22:33:44:55:66", "poswifi")
    assert mod.target_for(a, "per-ssid", "all.hc22000") == "poswifi.hc22000"
    assert mod.target_for(b, "per-ssid", "all.hc22000") == "poswifi.hc22000"


def test_target_for_per_ap_splits_reused_names_by_bssid(mod):
    # Validation: a reused name produces one file PER BSSID, never a merge.
    a = _hashline("aa:bb:cc:dd:ee:ff", "poswifi")
    b = _hashline("11:22:33:44:55:66", "poswifi")
    ta = mod.target_for(a, "per-ap", "all.hc22000")
    tb = mod.target_for(b, "per-ap", "all.hc22000")
    assert ta == "poswifi_aa_bb_cc_dd_ee_ff.hc22000"
    assert tb == "poswifi_11_22_33_44_55_66.hc22000"
    assert ta != tb


def test_target_for_per_ap_keeps_pmkid_and_eapol_together(mod):
    # Both record types for one AP route to the same file.
    pmkid = _hashline("aa:bb:cc:dd:ee:ff", "poswifi", type_="01")
    eapol = _hashline("aa:bb:cc:dd:ee:ff", "poswifi", type_="02")
    assert mod.target_for(pmkid, "per-ap", "all.hc22000") == \
        mod.target_for(eapol, "per-ap", "all.hc22000")


def test_target_for_per_ap_composite_splits_multi_ssid_bssid(mod):
    # One BSSID advertising two ESSIDs splits naturally on the composite key.
    a = _hashline("aa:bb:cc:dd:ee:ff", "guest")
    b = _hashline("aa:bb:cc:dd:ee:ff", "corp")
    assert mod.target_for(a, "per-ap", "all.hc22000") == \
        "guest_aa_bb_cc_dd_ee_ff.hc22000"
    assert mod.target_for(b, "per-ap", "all.hc22000") == \
        "corp_aa_bb_cc_dd_ee_ff.hc22000"


def test_target_for_per_ap_hidden_essid_routes_by_bssid(mod):
    line = _hashline("aa:bb:cc:dd:ee:ff", "")
    assert mod.target_for(line, "per-ap", "all.hc22000") == \
        "__hidden_aa_bb_cc_dd_ee_ff.hc22000"


def test_target_for_per_ap_no_bssid_is_unknown(mod):
    line = _hashline("", "poswifi")
    assert mod.target_for(line, "per-ap", "all.hc22000") == "unknown.hc22000"


# --- capture-seen.json: the per-AP last/first-seen index -----------------

def test_line_bssid_formats_the_ap_mac(mod):
    line = "WPA*02*deadbeef*aabbccddeeff*001122334455*6e6574*0*0"
    assert mod.line_bssid(line) == "AA:BB:CC:DD:EE:FF"   # colon-upper, reader-matching
    assert mod.line_bssid("not*a*real*line") == ""       # field 4 not 12 hex


def test_stamp_seen_sets_first_once_and_last_each_time(mod):
    t = {}
    mod.stamp_seen(t, {"AA:BB:CC:DD:EE:01"}, 1000)
    assert t == {"AA:BB:CC:DD:EE:01": {"first": 1000, "last": 1000}}
    mod.stamp_seen(t, {"AA:BB:CC:DD:EE:01"}, 2000)
    assert t == {"AA:BB:CC:DD:EE:01": {"first": 1000, "last": 2000}}   # first kept


def test_seen_times_round_trip(mod):
    aps = {"AA:BB:CC:DD:EE:01": {"first": 10, "last": 20}}
    mod.write_seen_times(aps)
    assert mod.read_seen_times() == aps
    # The index file is not mistaken for capture data.
    assert mod.SEEN_NAME in os.listdir(mod.STORE_DIR)
    assert not mod.SEEN_NAME.endswith(".hc22000")


def test_read_seen_times_tolerates_a_bad_file(mod):
    with open(os.path.join(mod.STORE_DIR, mod.SEEN_NAME), "w") as fh:
        fh.write("not json{")
    assert mod.read_seen_times() == {}


def test_target_for_per_ap_decodes_non_utf8_essid_via_latin1(mod):
    # A non-UTF-8 ESSID must route deterministically, not crash: 0xff is invalid
    # UTF-8 but decodes under latin-1, then sanitizes to a safe filename.
    line = _hashline("aa:bb:cc:dd:ee:ff", b"caf\xe9")
    target = mod.target_for(line, "per-ap", "all.hc22000")
    assert target.endswith("_aa_bb_cc_dd_ee_ff.hc22000")
    assert set(target) <= set(
        "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-")
