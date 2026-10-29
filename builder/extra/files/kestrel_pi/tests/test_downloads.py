"""downloads plugin: zips and combined hashlines from the capture store, behind
the web login and an encrypted (or on-device) connection.

Hashlines are built at test time from RFC 7042 placeholder MACs, and the
plugin's state directory is redirected into tmp_path."""

import importlib.util
import io
import logging
import os
import types
import zipfile

import pytest

from kestrel_pi.capture import CaptureInterface
from kestrel_pi.context import PluginContext, ServiceRegistry
from kestrel_pi.plugins import PluginPage

PLUGIN_DIR = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", "..", "plugins"))


def _mod():
    spec = importlib.util.spec_from_file_location(
        "downloads_under_test", os.path.join(PLUGIN_DIR, "downloads.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


DL = _mod()


def _line(n, essid="My Network"):
    mac = "00005e0053%02x" % n
    return "*".join(["WPA", "02", "%032x" % n, mac, "00005e0053ff",
                     essid.encode().hex(), "aa", "bb", "00"])


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(DL, "_state_dir", lambda: str(tmp_path / "state"))
    d = tmp_path / "capture"
    d.mkdir()
    (d / "My Network.hc22000").write_text(_line(1) + "\n" + _line(2) + "\n")
    (d / "Cafe Guest.hc22000").write_text(_line(3, "Cafe Guest") + "\n" + _line(1) + "\n")
    (d / "My Network.hc22000.gps.json").write_text('{"00:00:5E:00:53:01": {"lat": 12.34567}}')
    (d / "cracked").mkdir()
    (d / "cracked" / "wpa-sec.potfile").write_text("secret\n")
    return d


def _plugin(store, options=None, authed=True):
    p = DL.Downloads()
    p.options = dict({"include_locations": False, "max_mb": 200}, **(options or {}))
    p.context = PluginContext(
        name="downloads", options=p.options, capture=CaptureInterface(str(store)),
        connectivity=types.SimpleNamespace(online=False), services=ServiceRegistry(),
        capabilities=("capture-read", "web"))
    p.context.web_authed = authed
    p.on_loaded()
    return p


def _req(secure=True, addr="192.0.2.5", args=None):
    return types.SimpleNamespace(is_secure=secure, remote_addr=addr, args=args or {})


def _zip(result):
    body, status, headers = result
    assert status == 200 and headers["Content-Type"] == "application/zip"
    return zipfile.ZipFile(io.BytesIO(b"".join(body)))


def test_all_handshakes_zip_with_manifest_and_nothing_else(store):
    archive = _zip(_plugin(store).on_webhook("handshakes.zip", _req()))
    names = sorted(archive.namelist())
    assert names == ["Cafe Guest.hc22000", "MANIFEST.txt", "My Network.hc22000"]
    manifest = archive.read("MANIFEST.txt").decode()
    assert "Files: 2" in manifest and "Hashlines: 4" in manifest


def test_the_zip_is_deleted_once_sent(store, tmp_path):
    body, _, _ = _plugin(store).on_webhook("handshakes.zip", _req())
    state = tmp_path / "state"
    assert [n for n in os.listdir(state) if n.startswith("export-")]
    b"".join(body)
    assert not [n for n in os.listdir(state) if n.startswith("export-")]


def test_new_since_last_download(store):
    p = _plugin(store)
    _zip(p.on_webhook("handshakes.zip", _req()))
    assert p.on_webhook("new.zip", _req()).status == 404       # nothing changed
    with open(store / "Cafe Guest.hc22000", "a") as handle:
        handle.write(_line(4, "Cafe Guest") + "\n")
    assert sorted(_zip(p.on_webhook("new.zip", _req())).namelist()) == \
        ["Cafe Guest.hc22000", "MANIFEST.txt"]


def test_combined_hashlines_are_deduplicated(store):
    body, status, headers = _plugin(store).on_webhook("handshakes.hc22000", _req())
    assert status == 200 and "attachment" in headers["Content-Disposition"]
    lines = body.decode().splitlines()
    assert len(lines) == 3 and len(set(lines)) == 3


def test_locations_only_when_enabled(store):
    assert _plugin(store).on_webhook("with-locations.zip", _req()).status == 404
    archive = _zip(_plugin(store, {"include_locations": True})
                   .on_webhook("with-locations.zip", _req()))
    assert "My Network.hc22000.gps.json" in archive.namelist()


def test_refused_without_login_or_encryption(store):
    signed_out = _plugin(store, authed=False).on_webhook("handshakes.zip", _req())
    assert isinstance(signed_out, PluginPage) and signed_out.status == 403
    plain = _plugin(store).on_webhook("handshakes.zip", _req(secure=False))
    assert plain.status == 403 and "encrypted" in plain.body
    # On the device itself (loopback), plain HTTP is fine.
    _zip(_plugin(store).on_webhook("handshakes.zip", _req(secure=False, addr="127.0.0.1")))


def test_symlinks_and_other_files_are_never_served(store, tmp_path):
    outside = tmp_path / "outside.hc22000"
    outside.write_text("not from the store\n")
    os.symlink(str(outside), str(store / "Library WiFi.hc22000"))
    archive = _zip(_plugin(store).on_webhook("handshakes.zip", _req()))
    assert "Library WiFi.hc22000" not in archive.namelist()
    assert not any("potfile" in n or "cracked" in n for n in archive.namelist())


def test_size_limit(store):
    page = _plugin(store, {"max_mb": 0}).on_webhook("handshakes.zip", _req())
    # max_mb is floored at 1 MB, so this store still fits.
    assert not isinstance(page, PluginPage)
    big = store / "Big.hc22000"
    big.write_bytes(b"x" * (2 * 1024 * 1024))
    page = _plugin(store, {"max_mb": 1}).on_webhook("handshakes.zip", _req())
    assert isinstance(page, PluginPage) and page.status == 413


def test_index_lists_downloads_and_keeps_the_token(store):
    page = _plugin(store).on_webhook("", _req(args={"token": "abc"}))
    assert isinstance(page, PluginPage)
    assert 'href="handshakes.zip?token=abc"' in page.body
    assert "2 files" in page.body
    assert '<a href="/plugins?token=abc">&larr; Plugins</a>' in page.body
    refused = _plugin(store, authed=False).on_webhook("", _req())
    for target in ("handshakes.zip", "new.zip", "handshakes.hc22000"):
        assert 'href="%s' % target not in refused.body
    assert "login" in refused.body and "&larr; Plugins" in refused.body


def test_unknown_path_and_empty_store(tmp_path, monkeypatch):
    monkeypatch.setattr(DL, "_state_dir", lambda: str(tmp_path / "state"))
    empty = tmp_path / "empty"
    empty.mkdir()
    p = _plugin(empty)
    assert p.on_webhook("nope", _req()).status == 404
    assert p.on_webhook("handshakes.zip", _req()).status == 404
    assert p.on_webhook("handshakes.hc22000", _req()).status == 404


def test_leftover_zips_are_cleared_at_load(store, tmp_path):
    (tmp_path / "state").mkdir()
    (tmp_path / "state" / "export-old.zip").write_bytes(b"x")
    _plugin(store)
    assert not (tmp_path / "state" / "export-old.zip").exists()


def test_declarations_are_readable_by_root():
    from kestrel_pi.declarations import read_declarations

    decl = read_declarations(os.path.join(PLUGIN_DIR, "downloads.py"))["downloads"]
    caps = decl.get("capabilities") or decl.get("__capabilities__")
    assert set(caps) == {"capture-read", "web"}            # no network: no ack
