"""Tests for the recovered-networks core service.

All data here is invented, per CLAUDE.md "Nothing real in the repo": RFC 7042
``00:00:5E:00:53:xx`` MACs (bare-hex form ``00005e0053xx``), invented ESSIDs and
fake passphrases, and a throwaway 32-hex field for the hashcat-line fixtures.
"""

import os
import stat

from kestrel_pi import recovered
from kestrel_pi.recovered import (
    RecoveredStore,
    ingest_text,
    parse_potfile_line,
)

# Invented placeholders.
HASH32 = "0123456789abcdef0123456789abcdef"   # a throwaway PMKID/MIC field
BSSID_BARE = "00005e0053aa"
BSSID_NORM = "00:00:5E:00:53:AA"
STA_BARE = "00005e005301"
BSSID2_BARE = "00005e0053bb"
BSSID2_NORM = "00:00:5E:00:53:BB"


# -- parser ----------------------------------------------------------------

def test_parse_hashcat_five_field():
    rec = parse_potfile_line(
        "%s:%s:%s:My Network:correct-horse" % (HASH32, BSSID_BARE, STA_BARE))
    assert rec is not None
    assert rec.bssid == BSSID_NORM          # anchored on fields 2-3, not the hash
    assert rec.essid == "My Network"
    assert rec.psk == "correct-horse"


def test_parse_wpa_sec_four_field():
    rec = parse_potfile_line("%s:%s:Cafe Guest:s3cret" % (BSSID_BARE, STA_BARE))
    assert rec.bssid == BSSID_NORM          # anchored on fields 1-2
    assert rec.essid == "Cafe Guest"
    assert rec.psk == "s3cret"


def test_parse_essid_containing_colon():
    rec = parse_potfile_line("%s:%s:My: Net:pw123" % (BSSID_BARE, STA_BARE))
    assert rec.essid == "My: Net"           # everything between STA and the last field
    assert rec.psk == "pw123"


def test_parse_password_containing_colon_keeps_last_segment():
    # An inherent limit of colon-delimited potfiles: the password is the last
    # field, so a source password with ':' keeps only its final segment.
    rec = parse_potfile_line("%s:%s:%s:Name:pa:ss" % (HASH32, BSSID_BARE, STA_BARE))
    assert rec.essid == "Name:pa"
    assert rec.psk == "ss"


def test_parse_hidden_or_empty_essid():
    rec = parse_potfile_line("%s:%s:%s::pwonly" % (HASH32, BSSID_BARE, STA_BARE))
    assert rec.bssid == BSSID_NORM
    assert rec.essid == ""                  # hidden/absent: BSSID-only match
    assert rec.psk == "pwonly"


def test_parse_separated_macs_dash_and_dot():
    dash = parse_potfile_line(
        "%s:00-00-5e-00-53-aa:00-00-5e-00-53-01:Net:pw" % HASH32)
    assert dash.bssid == BSSID_NORM
    dot = parse_potfile_line("0000.5e00.53aa:0000.5e00.5301:Net:pw")
    assert dot.bssid == BSSID_NORM


def test_parse_garbage_is_skipped():
    assert parse_potfile_line("") is None
    assert parse_potfile_line("just some text") is None
    assert parse_potfile_line("::::") is None


def test_parse_default_hashcat_potfile_line_is_skipped():
    # The default hashcat.potfile line is "<WPA* hash>:<password>" -- one '*'-
    # delimited field then the password, with no adjacent MAC pair.
    line = ("WPA*02*%s*%s*%s*486f6d65*00:hunter2"
            % (HASH32, BSSID_BARE, STA_BARE))
    assert parse_potfile_line(line) is None


def test_parse_lone_mac_without_neighbour_is_skipped():
    # A 12-hex field whose neighbour is not MAC-shaped is not an anchor.
    assert parse_potfile_line("%s:notamacfield:Net:pw" % BSSID_BARE) is None


# -- RecoveredStore ----------------------------------------------------------

def _write_potfile(store_dir, name, text):
    recovered_dir = os.path.join(store_dir, "cracked")
    os.makedirs(recovered_dir, exist_ok=True)
    path = os.path.join(recovered_dir, name)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(text)
    return path


def test_store_empty_dir_is_benign(tmp_path):
    store = RecoveredStore(str(tmp_path))
    assert store.all() == []
    assert store.counts() == {"recovered_aps": 0, "recovered_essids": 0}
    assert store.lookup_bssid(BSSID_NORM) is None
    assert store.lookup_essid("My Network") == []


def test_store_merges_and_dedupes_across_potfiles(tmp_path):
    _write_potfile(str(tmp_path), "wpa-sec.potfile",
                   "%s:%s:My Network:pw-a\n" % (BSSID_BARE, STA_BARE))
    # A second file repeats the same (bssid, essid, psk) under a different
    # station MAC -- kept, since an AP-less result is known only for its
    # client -- repeats the first line exactly (it collapses) and adds a
    # distinct AP.
    _write_potfile(str(tmp_path), "imported-x.potfile",
                   "%s:00005e0053ff:My Network:pw-a\n"
                   "%s:%s:My Network:pw-a\n"
                   "%s:%s:Other:pw-b\n"
                   % (BSSID_BARE, BSSID_BARE, STA_BARE, BSSID2_BARE, STA_BARE))
    store = RecoveredStore(str(tmp_path))
    assert len(store.all()) == 3
    assert store.counts() == {"recovered_aps": 2, "recovered_essids": 2}


def test_store_lookup_bssid_is_exact_and_normalizes_input(tmp_path):
    _write_potfile(str(tmp_path), "a.potfile",
                   "%s:%s:My Network:pw-a\n" % (BSSID_BARE, STA_BARE))
    store = RecoveredStore(str(tmp_path))
    assert store.lookup_bssid(BSSID_NORM).psk == "pw-a"
    assert store.lookup_bssid(BSSID_BARE).psk == "pw-a"       # bare hex
    assert store.lookup_bssid("00:00:5e:00:53:aa").psk == "pw-a"  # lower colon
    assert store.lookup_bssid(BSSID2_NORM) is None


def test_store_lookup_essid_lists_multiple_psks_under_one_name(tmp_path):
    _write_potfile(str(tmp_path), "a.potfile",
                   "%s:%s:SharedName:pw-a\n"
                   "%s:%s:SharedName:pw-b\n"
                   % (BSSID_BARE, STA_BARE, BSSID2_BARE, STA_BARE))
    store = RecoveredStore(str(tmp_path))
    recs = store.lookup_essid("SharedName")
    assert len(recs) == 2
    assert {(r.bssid, r.psk) for r in recs} == {
        (BSSID_NORM, "pw-a"), (BSSID2_NORM, "pw-b")}


def test_store_hidden_essid_not_counted_as_essid_but_is_an_ap(tmp_path):
    _write_potfile(str(tmp_path), "a.potfile",
                   "%s:%s::pwonly\n" % (BSSID_BARE, STA_BARE))
    store = RecoveredStore(str(tmp_path))
    assert store.counts() == {"recovered_aps": 1, "recovered_essids": 0}
    assert store.lookup_bssid(BSSID_NORM).psk == "pwonly"


def test_store_records_carry_their_source_file(tmp_path):
    _write_potfile(str(tmp_path), "wpa-sec.potfile",
                   "%s:%s:My Network:pw-a\n" % (BSSID_BARE, STA_BARE))
    store = RecoveredStore(str(tmp_path))
    assert store.all()[0].source == "wpa-sec.potfile"


def test_store_cache_reparses_only_on_change(tmp_path, monkeypatch):
    _write_potfile(str(tmp_path), "a.potfile",
                   "%s:%s:My Network:pw-a\n" % (BSSID_BARE, STA_BARE))
    calls = {"n": 0}
    real = recovered.parse_potfile_line

    def counting(line):
        calls["n"] += 1
        return real(line)

    monkeypatch.setattr(recovered, "parse_potfile_line", counting)
    store = RecoveredStore(str(tmp_path))

    store.all()
    after_first = calls["n"]
    assert after_first > 0

    store.all()                        # unchanged dir -> cache hit, no re-parse
    assert calls["n"] == after_first

    _write_potfile(str(tmp_path), "b.potfile",
                   "%s:%s:Other:pw-b\n" % (BSSID2_BARE, STA_BARE))
    store.all()                        # new file -> signature changed -> re-parse
    assert calls["n"] > after_first


# -- matching records to captured networks ---------------------------------

ROGUE = "00:00:5E:00:53:7E"        # an AP-less capture's rogue address
CLIENT1 = "00:00:5E:00:53:E1"
CLIENT2 = "00:00:5E:00:53:E2"


def _keys(infrastructure=(), apless=()):
    from kestrel_pi.capture import HandshakeKeys
    return HandshakeKeys(frozenset(infrastructure), frozenset(apless))


def _rec(bssid, essid, psk, station=""):
    return recovered.RecoveredRecord(bssid=bssid, essid=essid, psk=psk,
                                     source="test", station=station)


def test_a_rogue_address_never_lends_its_passphrase_to_other_names():
    # One rogue address collected four names from two clients in one run; a
    # result is known for one client's "Cafe Guest" only.
    keys = _keys(apless=[(ROGUE, CLIENT1, "Cafe Guest"), (ROGUE, CLIENT1, "My Network"),
                         (ROGUE, CLIENT2, "Library WiFi"), (ROGUE, CLIENT2, "Home")])
    found = recovered.match_records([_rec(ROGUE, "Cafe Guest", "pw-cafe", CLIENT1)], keys)
    assert [(m.essid, m.station, m.psk, m.apless) for m in found] == [
        ("Cafe Guest", CLIENT1, "pw-cafe", True)]


def test_apless_results_stay_with_their_own_client():
    # Two clients remember different networks under one name; each keeps its own.
    keys = _keys(apless=[(ROGUE, CLIENT1, "Home"), (ROGUE, CLIENT2, "Home")])
    found = recovered.match_records(
        [_rec(ROGUE, "Home", "pw-one", CLIENT1), _rec(ROGUE, "Home", "pw-two", CLIENT2)],
        keys)
    assert {(m.station, m.psk) for m in found} == {(CLIENT1, "pw-one"),
                                                   (CLIENT2, "pw-two")}


def test_a_result_with_no_client_never_matches_an_apless_capture():
    # Imports before 1.0.13 wrote an all-zero client, read back as none.
    keys = _keys(apless=[(ROGUE, CLIENT1, "Home")])
    old = parse_potfile_line("00005e00537e:000000000000:Home:pw-home")
    assert old.station == ""
    assert recovered.match_records([old], keys) == []


def test_a_real_ap_matches_on_its_name_whatever_the_client():
    keys = _keys(infrastructure=[(BSSID_NORM, "My Network"), (BSSID_NORM, "Guest")])
    found = recovered.match_records([
        _rec(BSSID_NORM, "My Network", "pw-a", CLIENT1),
        _rec(BSSID_NORM, "My Network", "pw-a", CLIENT2),   # same result, another client
        _rec(BSSID_NORM, "Office", "pw-office"),           # a name not captured there
        _rec(BSSID2_NORM, "My Network", "pw-b"),           # an address not captured
    ], keys)
    assert [(m.bssid, m.essid, m.psk, m.apless) for m in found] == [
        (BSSID_NORM, "My Network", "pw-a", False)]


def test_a_result_with_no_name_matches_each_name_at_its_address():
    keys = _keys(infrastructure=[(BSSID_NORM, "My Network"), (BSSID_NORM, "Guest")],
                 apless=[(BSSID_NORM, CLIENT1, "Home")])
    found = recovered.match_records([_rec(BSSID_NORM, "", "pwonly", CLIENT1)], keys)
    assert sorted((m.essid, m.apless) for m in found) == [("Guest", False),
                                                          ("My Network", False)]


def test_differing_passphrases_for_one_network_are_all_kept():
    keys = _keys(infrastructure=[(BSSID_NORM, "My Network")])
    found = recovered.match_records([_rec(BSSID_NORM, "My Network", "old-pw"),
                                     _rec(BSSID_NORM, "My Network", "new-pw")], keys)
    assert [m.psk for m in found] == ["old-pw", "new-pw"]


def _hc(bssid_bare, station_bare, essid, pair="00"):
    return "WPA*02*%s*%s*%s*%s*0*0*%s" % (HASH32, bssid_bare, station_bare,
                                         essid.encode().hex(), pair)


def test_store_matches_captures_and_counts_only_real_aps(tmp_path):
    from kestrel_pi.capture import CaptureInterface

    (tmp_path / "x.hc22000").write_text("\n".join([
        _hc(BSSID_BARE, STA_BARE, "My Network"),
        _hc("00005e00537e", "00005e0053e1", "Cafe Guest", "10"),
        _hc("00005e00537e", "00005e0053e2", "Home", "10"),
    ]) + "\n")
    _write_potfile(str(tmp_path), "wpa-sec.potfile",
                   "%s:%s:My Network:pw-a\n"
                   "00005e00537e:00005e0053e1:Cafe Guest:pw-cafe\n" % (BSSID_BARE, STA_BARE))
    store, cap = RecoveredStore(str(tmp_path)), CaptureInterface(str(tmp_path))
    found = store.capture_matches(cap)
    assert {(m.bssid, m.essid, m.psk) for m in found} == {
        (BSSID_NORM, "My Network", "pw-a"), (ROGUE, "Cafe Guest", "pw-cafe")}
    assert store.recovered_bssids(cap) == {BSSID_NORM}
    assert store.capture_matches(cap) is found           # cached
    _write_potfile(str(tmp_path), "imported-y.potfile",
                   "00005e00537e:00005e0053e2:Home:pw-home\n")
    assert len(store.capture_matches(cap)) == 3          # a new potfile re-matches


# -- ingest_text -----------------------------------------------------------

def _mode(path):
    return stat.S_IMODE(os.stat(path).st_mode)


def test_ingest_writes_private_file_and_dir(tmp_path):
    result = ingest_text(
        str(tmp_path),
        "%s:%s:My Network:pw-a\n" % (BSSID_BARE, STA_BARE))
    assert result == {"imported": 1, "skipped": 0}
    recovered_dir = os.path.join(str(tmp_path), "cracked")
    assert _mode(recovered_dir) == 0o700
    files = os.listdir(recovered_dir)
    assert len(files) == 1 and files[0].startswith("imported-")
    assert _mode(os.path.join(recovered_dir, files[0])) == 0o600


def test_ingest_result_round_trips_through_the_store(tmp_path):
    # An imported file must parse back identically, including an ESSID with ':'.
    ingest_text(str(tmp_path),
                "%s:%s:%s:My Network:pw-a\n"
                "%s:%s:My: Net:pw-b\n"
                % (HASH32, BSSID_BARE, STA_BARE, BSSID2_BARE, STA_BARE))
    store = RecoveredStore(str(tmp_path))
    assert store.lookup_bssid(BSSID_NORM).essid == "My Network"
    other = store.lookup_bssid(BSSID2_NORM)
    assert other.essid == "My: Net"
    assert other.psk == "pw-b"


def test_ingest_keeps_the_client(tmp_path):
    # An AP-less result is known only for its client, so an import keeps it;
    # a line without one is written with an all-zero client, read back as none.
    ingest_text(str(tmp_path), "00005e00537e:00005e0053e1:Cafe Guest:pw-cafe\n"
                               "%s:000000000000:My Network:pw-a\n" % BSSID_BARE)
    store = RecoveredStore(str(tmp_path))
    assert {(r.bssid, r.station) for r in store.all()} == {
        (ROGUE, "00:00:5E:00:53:E1"), (BSSID_NORM, "")}


def test_ingest_dedupes_and_counts_skips(tmp_path):
    result = ingest_text(
        str(tmp_path),
        "%s:%s:My Network:pw-a\n"
        "%s:%s:My Network:pw-a\n"          # exact duplicate -> collapsed
        "garbage line\n"                   # unparseable -> skipped
        "\n"                               # blank -> not counted
        % (BSSID_BARE, STA_BARE, BSSID_BARE, STA_BARE))
    assert result == {"imported": 1, "skipped": 1}


def test_ingest_respects_max_lines(tmp_path):
    lines = "\n".join(
        "00005e0053%02x:%s:Net%d:pw%d" % (i, STA_BARE, i, i)
        for i in range(5))
    result = ingest_text(str(tmp_path), lines + "\n", max_lines=2)
    assert result["imported"] == 2
    assert result["skipped"] == 3          # the 3 lines past the cap


def test_ingest_empty_input_writes_nothing(tmp_path):
    result = ingest_text(str(tmp_path), "nothing parseable here\n")
    assert result == {"imported": 0, "skipped": 1}
    assert not os.path.isdir(os.path.join(str(tmp_path), "cracked"))
