import json
import os
import time

from kestrel_pi.capture import (
    STATUS_NAME,
    CaptureInterface,
    CaptureWatcher,
    classify_density,
    parse_hashline,
)

# A real-shape hashcat-22000 EAPOL line: WPA*02*MIC*AP_MAC*STA_MAC*ESSID_hex*...
# ESSID "Home" = 486f6d65, AP MAC aabbccddeeff.
EAPOL = "WPA*02*0123456789abcdef0123456789abcdef*aabbccddeeff*112233445566*486f6d65*0*0*00"
PMKID = "WPA*01*cafebabecafebabecafebabecafebabe*aabbccddee01*112233445500**00*00"


def test_parse_eapol():
    event = parse_hashline(EAPOL)
    assert event is not None
    assert event.type == "eapol"
    assert event.bssid == "AA:BB:CC:DD:EE:FF"
    assert event.essid == "Home"


def test_parse_pmkid_unknown_essid():
    event = parse_hashline(PMKID)
    assert event.type == "pmkid"
    assert event.bssid == "AA:BB:CC:DD:EE:01"
    assert event.essid == ""


def test_parse_garbage_is_none():
    assert parse_hashline("") is None
    assert parse_hashline("not a hashline") is None


def test_watcher_only_fires_on_new_lines(tmp_path):
    store = tmp_path
    f = store / "Home.hc22000"
    f.write_text(EAPOL + "\n")

    events = []
    watcher = CaptureWatcher(str(store), events.append)
    # First poll from a cold start sees the existing line.
    assert watcher.poll_once() == 1
    assert events[0].path.endswith("Home.hc22000")
    # Nothing new: no further events.
    assert watcher.poll_once() == 0
    # Append a new line: exactly one new event.
    with open(f, "a", encoding="utf-8") as handle:
        handle.write(PMKID + "\n")
    assert watcher.poll_once() == 1
    assert len(events) == 2


def test_watcher_prime_suppresses_existing(tmp_path):
    f = tmp_path / "Home.hc22000"
    f.write_text(EAPOL + "\n")
    events = []
    watcher = CaptureWatcher(str(tmp_path), events.append)
    watcher.prime()
    assert watcher.poll_once() == 0  # primed: existing line not re-announced
    assert events == []


# ESSID "Cafe" = 43616665; a second Home AP MAC and a hidden PMKID.
HOME_A2 = "WPA*02*0123456789abcdef0123456789abcdef*aabbccddeeff*112233445599*486f6d65*0*0*00"
HOME_B = "WPA*02*0123456789abcdef0123456789abcdef*aabbccddee02*112233445566*486f6d65*0*0*00"
CAFE = "WPA*02*0123456789abcdef0123456789abcdef*111111111111*112233445566*43616665*0*0*00"


def test_captured_networks_dedupes_by_essid_and_counts(tmp_path):
    # Two files, merged: Home seen 3x across 2 BSSIDs, Cafe once, one hidden AP.
    (tmp_path / "Home.hc22000").write_text("\n".join([EAPOL, HOME_A2, HOME_B]) + "\n")
    (tmp_path / "Other.hc22000").write_text("\n".join([CAFE, PMKID]) + "\n")

    nets = CaptureInterface(str(tmp_path)).captured_networks()
    by_essid = {n["essid"]: n for n in nets}

    assert by_essid["Home"]["handshakes"] == 3
    assert by_essid["Home"]["bssids"] == 2        # aa:..:ff and aa:..:02
    assert by_essid["Cafe"]["handshakes"] == 1 and by_essid["Cafe"]["bssids"] == 1
    assert by_essid[""]["handshakes"] == 1        # the hidden PMKID AP
    # Sorted case-insensitively with the hidden (empty) ESSID last.
    assert [n["essid"] for n in nets] == ["Cafe", "Home", ""]


def test_captured_networks_empty_store(tmp_path):
    assert CaptureInterface(str(tmp_path)).captured_networks() == []


def test_hashline_count_sums_nonempty_lines(tmp_path):
    iface = CaptureInterface(str(tmp_path))
    assert iface.hashline_count() == 0
    (tmp_path / "Home.hc22000").write_text("\n".join([EAPOL, HOME_A2, HOME_B]) + "\n")
    (tmp_path / "Other.hc22000").write_text("\n".join([CAFE, PMKID]) + "\n\n")  # blank line ignored
    assert iface.hashline_count() == 5


def test_ap_bssids_counts_distinct_access_points(tmp_path):
    iface = CaptureInterface(str(tmp_path))
    assert iface.ap_bssids() == set()
    # EAPOL and HOME_A2 share aa:bb:cc:dd:ee:ff; HOME_B, CAFE and PMKID add three
    # more distinct BSSIDs, so five hashlines -> four unique APs.
    (tmp_path / "Home.hc22000").write_text("\n".join([EAPOL, HOME_A2, HOME_B]) + "\n")
    (tmp_path / "Other.hc22000").write_text("\n".join([CAFE, PMKID]) + "\n")
    assert iface.ap_bssids() == {
        "AA:BB:CC:DD:EE:FF", "AA:BB:CC:DD:EE:02",
        "11:11:11:11:11:11", "AA:BB:CC:DD:EE:01",
    }
    assert len(iface.ap_bssids()) == 4


# -- environment readout -------------------------------------------------
def test_classify_density_default_buckets():
    assert classify_density(0) == (0, "Quiet")
    assert classify_density(5) == (1, "Light")
    assert classify_density(20) == (2, "Moderate")
    assert classify_density(45) == (3, "Busy")
    assert classify_density(80) == (4, "Dense")
    # boundaries are inclusive upper bounds (0, 10, 30, 60)
    assert classify_density(10) == (1, "Light")
    assert classify_density(11) == (2, "Moderate")
    assert classify_density(60) == (3, "Busy")
    assert classify_density(61) == (4, "Dense")


def test_classify_density_honours_override_and_ignores_a_bad_one():
    assert classify_density(5, [2, 4, 6, 8]) == (2, "Moderate")
    # wrong length / unsorted / junk all fall back to the defaults
    assert classify_density(5, [10]) == (1, "Light")
    assert classify_density(5, [8, 4, 2, 1]) == (1, "Light")
    assert classify_density(5, "nope") == (1, "Light")


def _write_status(tmp_path, payload):
    (tmp_path / STATUS_NAME).write_text(json.dumps(payload))


def test_environment_returns_a_fresh_scanning_reading(tmp_path):
    _write_status(tmp_path, {"updated_at": int(time.time()), "window_s": 300,
                             "radios_recent": 23, "data_ts": int(time.time()),
                             "scanning": True})
    env = CaptureInterface(str(tmp_path)).environment()
    assert env["radios_recent"] == 23


def test_environment_is_none_when_absent_stale_or_not_scanning(tmp_path):
    iface = CaptureInterface(str(tmp_path))
    assert iface.environment() is None  # no file
    _write_status(tmp_path, {"updated_at": int(time.time()), "scanning": False})
    assert iface.environment() is None  # collector stopped
    _write_status(tmp_path, {"updated_at": int(time.time()) - 10000,
                             "radios_recent": 5, "scanning": True})
    assert iface.environment() is None  # stale


def test_environment_scanning_without_counts_yet(tmp_path):
    # Scanning but the kismet DB wasn't readable: the dict comes back with no
    # radios_recent so a consumer shows "scanning", not a wrong number.
    _write_status(tmp_path, {"updated_at": int(time.time()), "window_s": 300,
                             "scanning": True})
    env = CaptureInterface(str(tmp_path)).environment()
    assert env is not None and "radios_recent" not in env


def test_environment_is_not_an_hashline_file(tmp_path):
    # capture-status.json must never be picked up by the hashline readers.
    _write_status(tmp_path, {"updated_at": int(time.time()), "radios_recent": 3,
                             "scanning": True})
    iface = CaptureInterface(str(tmp_path))
    assert iface.hashline_files() == []
    assert iface.hashline_count() == 0
