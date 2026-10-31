"""The capture collector's environment (in-range) readout.

``capture-collect.py`` is an installed script (hyphenated, run as ``__main__``
on the device), so it is loaded here by path. These cover reading the tail of
AngryOxide's pcapng (distinct transmitters in the most recent window of frames,
parsed incrementally) and the small status file it writes into the store - the
device-independent parts. The capture files here are built from invented
frames with placeholder addresses."""

import importlib.util
import json
import os
import struct
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
    m.LEGACY_RUN_DIR = os.path.join(m.STORE_DIR, ".run")
    os.makedirs(m.RUN_DIR)
    os.makedirs(m.STORE_DIR)
    return m


# -- invented pcapng files ---------------------------------------------------------

def _mac(n):
    return "00:00:5e:00:53:%02x" % n


def _block(btype, body, endian="<"):
    body += b"\0" * (-len(body) % 4)
    length = len(body) + 12
    return struct.pack(endian + "II", btype, length) + body + struct.pack(endian + "I", length)


def _header(endian="<", tsresol=None):
    shb = _block(0x0A0D0D0A, struct.pack(endian + "IHHq", 0x1A2B3C4D, 1, 0, -1), endian)
    opts = b""
    if tsresol is not None:
        opts = struct.pack(endian + "HH", 9, 1) + bytes([tsresol]) + b"\0\0\0" \
            + struct.pack(endian + "HH", 0, 0)
    idb = _block(0x00000001, struct.pack(endian + "HHI", 127, 0, 0) + opts, endian)
    return shb + idb


def _frame(ts, transmitter, kind="data", endian="<", unit=1e-6):
    """One Enhanced Packet Block: a radiotap header and an 802.11 frame whose
    addr2 is ``transmitter`` (an int for _mac, or raw bytes)."""
    fc = {"data": 0x08, "beacon": 0x80, "ack": 0xD4, "rts": 0xB4}[kind]
    addr2 = bytes(int(x, 16) for x in _mac(transmitter).split(":")) \
        if isinstance(transmitter, int) else transmitter
    radiotap = struct.pack("<BBHI", 0, 0, 8, 0)
    if kind == "ack":
        dot11 = struct.pack("<BBH", fc, 0, 0) + b"\xff" * 6
    else:
        dot11 = struct.pack("<BBH", fc, 0, 0) + b"\xff" * 6 + addr2 + b"\x00" * 8
    packet = radiotap + dot11
    stamp = int(round(ts / unit))
    epb = struct.pack(endian + "IIIII", 0, stamp >> 32, stamp & 0xFFFFFFFF, len(packet), len(packet))
    return _block(0x00000006, epb + packet, endian)


def _make_pcapng(run_dir, frames, name="kestrel.pcapng", endian="<"):
    """Write a capture file of ``frames``, ``(ts, transmitter[, kind])``."""
    path = os.path.join(run_dir, name)
    with open(path, "wb") as fh:
        fh.write(_header(endian))
        for f in frames:
            fh.write(_frame(*f, endian=endian) if len(f) == 3 else _frame(f[0], f[1], endian=endian))
    return path


def _append(path, frames):
    with open(path, "ab") as fh:
        for f in frames:
            fh.write(_frame(*f))


def _aged(path, seconds_ago):
    stamp = time.time() - seconds_ago
    os.utime(path, (stamp, stamp))


# -- the transmitter address ---------------------------------------------------------

def test_transmitter_reads_addr2_and_skips_frames_without_one(mod):
    data = _frame(1, 1)[8 + 20 + 8:-4]          # the 802.11 frame inside the block
    assert mod.transmitter(data[:24]) == _mac(1)
    beacon = _frame(1, 2, "beacon")[8 + 20 + 8:-4]
    assert mod.transmitter(beacon[:24]) == _mac(2)
    rts = _frame(1, 3, "rts")[8 + 20 + 8:-4]
    assert mod.transmitter(rts[:24]) == _mac(3)
    ack = _frame(1, 4, "ack")[8 + 20 + 8:-4]
    assert mod.transmitter(ack) is None                          # ACK names no transmitter
    assert mod.transmitter(b"\x08\x00") is None                  # truncated
    group = struct.pack("<BBH", 0x08, 0, 0) + b"\xff" * 6 + b"\x01\x00\x5e\x00\x00\x01" + b"\0" * 8
    assert mod.transmitter(group) is None                        # group address
    null = struct.pack("<BBH", 0x08, 0, 0) + b"\xff" * 6 + b"\0" * 6 + b"\0" * 8
    assert mod.transmitter(null) is None                         # all-zero address


# -- environment_snapshot -------------------------------------------------------------

def test_environment_snapshot_counts_recent_distinct_radios(mod):
    # The window is anchored to the newest FRAME, not wall-clock: the newest is
    # at 1000, so a 60 s window is [940, 1000].
    _make_pcapng(mod.RUN_DIR, [
        (900, 2),          # too old
        (960, 0),          # in window
        (970, 1),          # in window
        (995, 5, "ack"),   # no transmitter, not counted
        (1000, 0),         # newest, same radio as at 960
    ])
    assert mod.environment_snapshot(60) == (2, 1000)


def test_environment_snapshot_none_without_a_capture_file(mod):
    assert mod.environment_snapshot(60) is None


def test_environment_snapshot_none_before_the_first_frame(mod):
    _make_pcapng(mod.RUN_DIR, [])
    assert mod.environment_snapshot(60) is None


def test_environment_snapshot_reads_only_new_frames_each_poll(mod):
    path = _make_pcapng(mod.RUN_DIR, [(1000, 0), (1001, 1)])
    assert mod.environment_snapshot(60) == (2, 1001)
    offset = mod._TAILS[path].offset
    assert offset == os.path.getsize(path)
    _append(path, [(1030, 2), (1070, 3)])
    # Window [1010, 1070]: radios 2 and 3; 0 and 1 have aged out.
    assert mod.environment_snapshot(60) == (2, 1070)
    assert mod._TAILS[path].offset == os.path.getsize(path) > offset


def test_environment_snapshot_leaves_a_partial_block_for_the_next_poll(mod):
    path = _make_pcapng(mod.RUN_DIR, [(1000, 0)])
    whole = _frame(1005, 1)
    with open(path, "ab") as fh:
        fh.write(whole[:20])                      # AngryOxide is mid-write
    assert mod.environment_snapshot(60) == (1, 1000)
    with open(path, "ab") as fh:
        fh.write(whole[20:])
    assert mod.environment_snapshot(60) == (2, 1005)


def test_environment_snapshot_catches_up_on_a_big_file_over_several_polls(mod, monkeypatch):
    monkeypatch.setattr(mod, "ENV_MAX_READ", 400)
    _make_pcapng(mod.RUN_DIR, [(1000 + i, i) for i in range(20)])
    seen = [mod.environment_snapshot(600) for _ in range(12)]
    assert seen[0][1] < 1019                      # the first poll read only part
    assert seen[-1] == (20, 1019)


def test_environment_snapshot_reads_the_previous_run_while_the_new_file_is_empty(mod):
    # A relaunch for a new agent plan starts a new, empty pcapng but keeps the
    # previous run's: the reading comes from the previous run.
    old = _make_pcapng(mod.RUN_DIR, [(1000, 0), (990, 1)], name="kestrel-1-000001.pcapng")
    _aged(old, 120)
    _make_pcapng(mod.RUN_DIR, [], name="kestrel-2-000002.pcapng")
    assert mod.environment_snapshot(60) == (2, 1000)


def test_environment_snapshot_counts_across_files_from_the_newest_frame(mod):
    old = _make_pcapng(mod.RUN_DIR, [(990, 1), (995, 2), (900, 9)], name="kestrel-1-000001.pcapng")
    _aged(old, 120)
    _make_pcapng(mod.RUN_DIR, [(1000, 1), (998, 3)], name="kestrel-2-000002.pcapng")
    # Window [940, 1000] over both: 1 (in both, counted once), 2, 3; 9 too old.
    assert mod.environment_snapshot(60) == (3, 1000)


def test_environment_snapshot_reads_at_most_env_max_files(mod, monkeypatch):
    old = _make_pcapng(mod.RUN_DIR, [(1000, 0)], name="kestrel-1-000001.pcapng")
    _aged(old, 120)
    _make_pcapng(mod.RUN_DIR, [], name="kestrel-2-000002.pcapng")
    monkeypatch.setattr(mod, "ENV_MAX_FILES", 1)
    assert mod.environment_snapshot(60) is None


def test_environment_snapshot_starts_over_when_a_file_is_replaced(mod):
    path = _make_pcapng(mod.RUN_DIR, [(1000, 0), (1001, 1), (1002, 2)])
    assert mod.environment_snapshot(60) == (3, 1002)
    os.remove(path)                               # the launcher cleared the run dir
    _make_pcapng(mod.RUN_DIR, [(2000, 7)])        # and a new launch wrote a new file
    radios, newest = mod.environment_snapshot(60)
    assert newest == 2000 and radios == 1         # the old radios are long out of window


def test_environment_snapshot_reads_big_endian_and_other_resolutions(mod):
    _make_pcapng(mod.RUN_DIR, [(1000, 0), (1001, 1)], endian=">")
    assert mod.environment_snapshot(60) == (2, 1001)
    path = os.path.join(mod.RUN_DIR, "ns.pcapng")
    with open(path, "wb") as fh:                  # nanosecond timestamps (if_tsresol 9)
        fh.write(_header(tsresol=9) + _frame(1002.5, 2, unit=1e-9))
    assert mod._tsresol(struct.pack("<HH", 9, 1) + b"\x09\0\0\0", "<") == pytest.approx(1e-9)
    assert mod.environment_snapshot(60) == (3, 1002)


def test_environment_snapshot_ignores_a_file_that_is_not_pcapng(mod):
    with open(os.path.join(mod.RUN_DIR, "junk.pcapng"), "wb") as fh:
        fh.write(b"not a capture file at all")
    assert mod.environment_snapshot(60) is None
    assert mod.environment_snapshot(60) is None   # and doesn't keep retrying it


# -- update_environment ---------------------------------------------------------------

def test_update_environment_writes_counts(mod):
    now = int(time.time())
    _make_pcapng(mod.RUN_DIR, [(now, 0), (now, 2)])
    mod.update_environment(300)
    data = json.loads(open(os.path.join(mod.STORE_DIR, mod.STATUS_NAME)).read())
    assert data["scanning"] is True and data["window_s"] == 300
    assert data["radios_recent"] == 2 and data["data_ts"] == now


def test_update_environment_marks_scanning_before_any_capture(mod):
    # No capture file yet: still writes a scanning marker, but with no counts, so
    # the UI shows "scanning" rather than a stale/wrong number.
    mod.update_environment(300)
    data = json.loads(open(os.path.join(mod.STORE_DIR, mod.STATUS_NAME)).read())
    assert data["scanning"] is True and "radios_recent" not in data


def test_update_environment_keeps_last_count_across_relaunch(mod, monkeypatch):
    # The launcher clears the run dir and AngryOxide starts a fresh, empty pcapng
    # on a capture start, so environment_snapshot returns None until the first
    # frame lands. With a prior good reading, the count is carried through rather
    # than blanked to "scanning" on every relaunch - otherwise the card flaps.
    now = int(time.time())
    _make_pcapng(mod.RUN_DIR, [(now, 0), (now, 2)])
    mod.update_environment(300)
    monkeypatch.setattr(mod, "environment_snapshot", lambda *_a, **_k: None)
    mod.update_environment(300)
    data = json.loads(open(os.path.join(mod.STORE_DIR, mod.STATUS_NAME)).read())
    assert data["scanning"] is True
    assert data["radios_recent"] == 2 and data["data_ts"] == now


def test_the_collector_never_opens_the_kismet_database(mod):
    # The reading must not touch AngryOxide's database: it can't keep up in a
    # busy RF environment, and a reader's lock only slows it further.
    with open(os.path.join(mod.RUN_DIR, "kestrel.kismet"), "wb") as fh:
        fh.write(b"SQLite format 3\0")
    assert mod.environment_snapshot(60) is None
    assert "sqlite3" not in open(_COLLECT).read()


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


# --- run dir: root-owned now, the legacy in-store .run/ still drained -------

def test_run_sources_include_the_legacy_store_run_dir(mod):
    # An in-place update moves the run dir out of the store; hashlines an older
    # release left in the store's .run/ must still reach the store.
    os.makedirs(mod.LEGACY_RUN_DIR)
    for directory, name in ((mod.RUN_DIR, "new.hc22000"), (mod.LEGACY_RUN_DIR, "old.hc22000"),
                            (mod.RUN_DIR, "kestrel.pcapng"),
                            (mod.LEGACY_RUN_DIR, "restore-state.json")):
        open(os.path.join(directory, name), "w").close()
    assert mod.run_sources() == [os.path.join(mod.RUN_DIR, "new.hc22000"),
                                 os.path.join(mod.LEGACY_RUN_DIR, "old.hc22000")]


def test_run_sources_tolerate_missing_dirs(mod, tmp_path):
    mod.RUN_DIR = str(tmp_path / "gone")
    assert mod.run_sources() == []


def test_default_run_dir_is_outside_the_store():
    m = _load()
    assert not m.RUN_DIR.startswith(m.STORE_DIR.rstrip("/") + "/")


@pytest.mark.parametrize("value, expected", [
    ("mine.hc22000", "mine.hc22000"),
    ("../../etc/passwd", "all.hc22000"),
    ("/etc/passwd", "all.hc22000"),
    ("sub/x.hc22000", "all.hc22000"),
    ("..", "all.hc22000"),
])
def test_single_file_must_be_a_plain_name_in_the_store(mod, tmp_path, value, expected):
    config = tmp_path / "config.toml"
    config.write_text('[capture.output]\nmode = "single"\nsingle_file = "%s"\n' % value)
    mod.CONFIG = str(config)
    assert mod.output_config() == ("single", expected)


# -- adopt-store: root hands an older release's files to the pi collector ----
# The tests run unprivileged, so "root" is the test's own uid (from_uid) and the
# owner change is recorded instead of made.

def _adopt(store, uid=None, **kwargs):
    m = _load()
    me = os.getuid()
    changed = set()

    def record(fd, new_uid, new_gid):
        changed.add(os.fstat(fd).st_ino)

    counts = m.adopt_store(str(store), me if uid is None else uid, os.getgid(),
                           from_uid=me, chown=record, **kwargs)
    return counts, changed


def _ino(path):
    return os.lstat(path).st_ino


def test_adopt_hands_over_store_files_legacy_run_and_archive_sets(tmp_path):
    store = tmp_path / "store"
    (store / ".run").mkdir(parents=True)
    (store / "archive" / "20261027T000000").mkdir(parents=True)
    paths = [store / "My Network.hc22000", store / ".run" / "a.hc22000",
             store / "archive" / "20261027T000000" / "b.hc22000"]
    for path in paths:
        path.write_text("line\n")
    counts, changed = _adopt(store)
    dirs = [store / ".run", store / "archive", store / "archive" / "20261027T000000"]
    assert changed == {_ino(p) for p in paths + dirs}
    assert counts == {"files": 3, "dirs": 3, "skipped": 0}


def test_adopt_never_follows_a_symlink(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "shadow").write_text("secret\n")
    store = tmp_path / "store"
    store.mkdir()
    os.symlink(outside / "shadow", store / "x.hc22000")
    os.symlink(outside, store / ".run")
    counts, changed = _adopt(store)
    assert _ino(outside / "shadow") not in changed and _ino(outside) not in changed
    assert changed == set()


def test_adopt_skips_a_hard_linked_file(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "shadow").write_text("secret\n")
    store = tmp_path / "store"
    store.mkdir()
    os.link(outside / "shadow", store / "x.hc22000")
    counts, changed = _adopt(store)
    assert changed == set() and counts["skipped"] == 1


def test_adopt_skips_fifos_without_blocking(tmp_path):
    store = tmp_path / "store"
    store.mkdir()
    os.mkfifo(store / "x.hc22000")
    counts, changed = _adopt(store)
    assert changed == set() and counts["skipped"] == 1


def test_adopt_stops_at_the_depth_limit(tmp_path):
    m = _load()
    store = tmp_path / "store"
    deep = store.joinpath(*["d"] * (m.ADOPT_MAX_DEPTH + 1))
    deep.mkdir(parents=True)
    (deep / "x.hc22000").write_text("line\n")
    counts, changed = _adopt(store)
    assert _ino(deep / "x.hc22000") not in changed
    assert _ino(store.joinpath(*["d"] * m.ADOPT_MAX_DEPTH)) in changed


def test_adopt_leaves_a_store_that_is_not_the_collectors(tmp_path):
    store = tmp_path / "store"
    store.mkdir()
    (store / "x.hc22000").write_text("line\n")
    counts, changed = _adopt(store, uid=os.getuid() + 1)
    assert changed == set() and counts == {"files": 0, "dirs": 0, "skipped": 0}


def test_adopt_skips_an_entry_swapped_after_it_was_listed(tmp_path, monkeypatch):
    # The listing saw a regular file; by the time it is opened a symlink to a
    # file outside has taken its place. O_NOFOLLOW refuses the open, so nothing
    # outside is handed over.
    m = _load()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "shadow").write_text("secret\n")
    store = tmp_path / "store"
    store.mkdir()
    (store / "x.hc22000").write_text("line\n")
    real_open = os.open

    def swapping_open(path, flags, *args, dir_fd=None, **kwargs):
        if path == "x.hc22000" and dir_fd is not None:
            os.unlink(store / "x.hc22000")
            os.symlink(outside / "shadow", store / "x.hc22000")
        return real_open(path, flags, *args, dir_fd=dir_fd, **kwargs)

    monkeypatch.setattr(m.os, "open", swapping_open)
    changed = set()
    counts = m.adopt_store(str(store), os.getuid(), os.getgid(), from_uid=os.getuid(),
                           chown=lambda fd, u, g: changed.add(os.fstat(fd).st_ino))
    assert changed == set() and counts["skipped"] == 1


def test_adopt_needs_root(monkeypatch, capsys):
    m = _load()
    monkeypatch.setattr(m.os, "geteuid", lambda: 1000)
    assert m.adopt_main("pi") == 2
    assert "needs root" in capsys.readouterr().err


def test_launcher_adopts_the_store_before_starting_the_collector():
    with open(os.path.join(os.path.dirname(_HERE), "capture-launch.sh"), encoding="utf-8") as h:
        text = h.read()
    adopt = text.index('python3 -I "$COLLECT" --adopt-store pi')
    start = text.index('setpriv --reuid=pi --regid=pi --init-groups -- "$COLLECT"')
    assert adopt < start
