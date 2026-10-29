"""The web Settings catalog and the root helper's config-set allow-list must
stay identical.

The daemon renders the Settings form from ``kestrel_pi.config_catalog``;
``plugins-ctl`` validates config-set writes against its own ``CONFIG_SETTABLE``
copy, because it must not import the (pi-writable) venv package as root. A
shared import would break that privilege boundary, so the two are kept in step
by this drift test — the same mirror-and-test discipline the agent enums use.
"""
import importlib.machinery
import importlib.util
import os

from kestrel_pi import config_catalog as cc

FILES = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))


def _load(name, path):
    loader = importlib.machinery.SourceFileLoader(name, path)
    spec = importlib.util.spec_from_loader(name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


ctl = _load("pwn_plugins_ctl_catalog_test", os.path.join(FILES, "plugins-ctl.py"))


def test_editable_allowlist_matches_the_root_helper():
    # The exact invariant: the form's allow-list and the root validator's are one.
    assert cc.EDITABLE == ctl.CONFIG_SETTABLE


def test_recovered_networks_settings_are_settable():
    # The passphrase-visibility toggle is a plain bool, and the import caps are
    # positive ints -- settable from the Settings page and the apply socket.
    assert cc.EDITABLE["web.show_passphrases"] == {"type": "bool"}
    assert cc.EDITABLE["recovered.max_import_mb"] == {"type": "int", "min": 1}
    assert cc.EDITABLE["recovered.max_import_lines"] == {"type": "int", "min": 1}
    # show_passphrases is read live, so it must not be marked as needing a restart.
    assert cc.find("web", "show_passphrases")["restart"] is cc.RESTART_NONE


def test_show_locations_is_a_live_web_toggle():
    # The location-visibility toggle mirrors show_passphrases: a plain bool,
    # settable, read live (no restart) and on the root helper's live-key set.
    assert cc.EDITABLE["web.show_locations"] == {"type": "bool"}
    assert cc.find("web", "show_locations")["restart"] is cc.RESTART_NONE
    assert cc.find("web", "show_locations")["default"] is True
    assert "show_locations" in ctl._WEB_LIVE_KEYS


def test_sections_and_entries_are_well_formed():
    seen = set()
    for section in cc.SECTIONS:
        assert section["title"] and section["anchor"]
        for entry in section["entries"]:
            pair = (entry["table"], entry["key"])
            assert pair not in seen, "duplicate %s" % (pair,)
            seen.add(pair)
            assert entry.get("help")
            if entry.get("editable"):
                assert entry["type"] in ("bool", "int", "float", "enum", "str")
                assert "default" in entry
                assert entry.get("restart") in (
                    cc.RESTART_NONE, cc.RESTART_DAEMON, cc.RESTART_CAPTURE,
                    cc.RESTART_AGENT)
                if entry["type"] == "enum":
                    assert entry.get("choices")
            else:
                assert entry.get("reason") in (
                    cc.RO_CONTROL, cc.RO_LOCKOUT, cc.RO_SECRET, cc.RO_CLI)


def test_editable_map_keys_resolve_back_to_an_entry():
    for full in cc.EDITABLE:
        table, key = full.rsplit(".", 1)
        entry = cc.find(table, key)
        assert entry is not None and entry.get("editable")


def test_find_returns_none_for_an_unknown_setting():
    assert cc.find("nope", "nope") is None
