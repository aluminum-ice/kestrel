"""Tests for the real-data check (stdlib + pytest, no network):

    python3 -m pytest tools/real-data-check

Real-looking samples are assembled at run time, so this file itself stays clean
under the check it tests.
"""

import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import real_data_check as rdc  # noqa: E402

# A MAC in a registered vendor range, and the same as a bare 12-hex string.
REAL_MAC = ":".join(["B8", "27", "EB", "12", "34", "56"])
REAL_MAC_HEX = REAL_MAC.replace(":", "").lower()


def kinds(text):
    return [kind for _no, kind, _value in rdc.scan_text(text)]


def test_placeholder_macs_pass():
    for mac in ("00:00:5E:00:53:F0", "AA:BB:CC:DD:EE:FF", "aa-bb-cc-00-00-01",
                "11:22:33:44:55:66", "00:11:22:33:44:55", "00:00:00:00:00:00",
                "FF:FF:FF:FF:FF:FF", "11:11:11:11:11:11"):
        assert kinds("bssid = %s" % mac) == [], mac


def test_real_mac_fails_and_is_masked():
    found = rdc.scan_text("first line\nbssid = %s\n" % REAL_MAC)
    assert [(no, kind) for no, kind, _ in found] == [(2, "MAC address")]
    assert REAL_MAC not in found[0][2]


def test_ipv6_and_times_are_not_macs():
    assert kinds("fe80::aa:bb:cc:dd:ee:ff and 12:30:45 and 2001:db8::1") == []


def test_hashline_macs_checked():
    clean = "WPA*02*%s*aabbccddeeff*112233445566*486f6d65*0*0*00" % ("0" * 32)
    assert kinds(clean) == []
    real = "WPA*02*%s*%s*112233445566*486f6d65*0*0*00" % ("0" * 32, REAL_MAC_HEX)
    assert "hashcat line with a real MAC" in kinds(real)


def test_emails():
    for text in ("ssh pi@kestrel.local", "user@host.example.net", "a@example.com",
                 "getty@tty1.service", "pi@192.168.1.50", "ssh pi@192.168.x.y",
                 "ssh://git@forge.example.org:2222/o/r.git",
                 "Co-Authored-By: x <noreply@anthropic.com>",
                 "someone@noreply.forge.example"):
        assert kinds(text) == [], text
    person = "jane.doe" + "@" + "mail.com"
    assert kinds("contact %s" % person) == ["email address"]


def test_secrets():
    samples = {
        "private key": "-----BEGIN OPENSSH " + "PRIVATE KEY-----",
        "GitHub token": "ghp_" + "A1" * 18,
        "AWS access key": "AKIA" + "ABCDEFGH12345678",
        "PyPI token": "pypi-AgE" + "x" * 60,
        "Ghost Admin API key": "a" * 24 + ":" + "b" * 64,
        "password hash": "$6$" + "realsalt" + "$" + "Q" * 86,
    }
    for kind, text in samples.items():
        assert kind in kinds(text), kind


def test_placeholders_that_look_like_secrets_pass():
    assert kinds("--set token=ghp_xxx") == []
    assert kinds('shadow = {"pi": "$6$salt$HASH"}') == []
    assert kinds("default('%s')" % rdc.ALLOWED_HASHES[0]) == []


def test_forbidden_names():
    for path in ("captures/Cafe.hc22000", "x/dump.pcapng", "track.gpx",
                 "agent/model-recurrentppo.zip", "secrets/.env", "home/.ssh/id_ed25519",
                 "device-diagnostics-20261027T101500Z/journal.txt",
                 "capture-health-20261027T101500Z/status.txt", "store/capture-seen.json"):
        assert rdc.check_name(path), path
    for path in ("builder/extra/files/scripts/pull-device-diagnostics.sh",
                 "docs/images/web-ui.png", "assets/branding/kestrel.svg",
                 "home/.ssh/id_ed25519.pub", "builder/extra/files/config.toml"):
        assert rdc.check_name(path) is None, path


def _git(repo, *args):
    subprocess.run(["git", "-C", str(repo)] + list(args), check=True, capture_output=True)


def test_main_scans_tracked_and_optionally_untracked(tmp_path, monkeypatch, capsys):
    _git(tmp_path, "init", "-q")
    (tmp_path / "clean.md").write_text("ssh pi@kestrel.local\n")
    _git(tmp_path, "add", "clean.md")
    (tmp_path / "loose.md").write_text("bssid %s\n" % REAL_MAC)
    monkeypatch.chdir(tmp_path)

    assert rdc.main([]) == 0
    assert rdc.main(["--include-untracked"]) == 1
    err = capsys.readouterr().err
    assert "loose.md:1: MAC address" in err and REAL_MAC not in err
