"""Tests for the census plugin and its helper files (the DHT client and the
``kestrel-census/1`` format). No test touches the network: the DHT is an
in-process fake whose nodes sit at RFC 5737 documentation addresses, and every
key and token is generated here."""

import importlib.util
import logging
import os
import types

from kestrel_pi import declarations, load_helper
from kestrel_pi.capture import CaptureInterface
from kestrel_pi.connectivity import ConnectivityState
from kestrel_pi.context import PluginContext, ServiceRegistry
from kestrel_pi.loader import Loader

PLUGIN_DIR = os.path.normpath(
    os.path.join(os.path.dirname(__file__), "..", "..", "plugins"))
CENSUS = os.path.join(PLUGIN_DIR, "census.py")
LOG = logging.getLogger("test_census")
dht = load_helper(CENSUS, "_census_dht.py")


def _load(path, modname):
    spec = importlib.util.spec_from_file_location(modname, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _plugin(tmp_path, online=False, **opts):
    """A Census instance with its context, without its worker running."""
    mod = _load(CENSUS, "census_under_test")
    options = {"report": True, "read": True, "poll_hours": 4,
               "tether_only": True, "daily_mb": 5,
               "bootstrap": "router.example.net:6881,dht.example.org:25401"}
    options.update(opts)
    plugin = mod.Census()
    plugin.options = options
    plugin.context = PluginContext(
        name="census", options=options,
        capture=CaptureInterface(str(tmp_path)),
        connectivity=types.SimpleNamespace(online=online,
                                           active_paths=lambda: []),
        services=ServiceRegistry(),
        capabilities=("network", "ui", "web"))
    plugin.on_loaded()
    return plugin, mod


# -- step 1: the plugin's declarations and options ------------------------------

def test_census_loads_with_its_helpers(tmp_path):
    plugin, _mod = _plugin(tmp_path)
    assert plugin._dht.__name__ == "kestrel_plugin_helpers.census.dht"
    assert plugin._proto.__name__ == "kestrel_plugin_helpers.census.proto"
    assert plugin.context.health_note == ""


def test_census_declarations_read_statically():
    # Root and plugins-ctl read declarations with ast, never importing the file.
    decl = declarations.read_declarations(CENSUS)["census"]
    assert tuple(decl["capabilities"]) == ("network", "ui", "web")
    assert tuple(decl["provides"]) == ("census",)
    assert tuple(decl["optional_requires"]) == ("agent", "remote-training")
    assert set(decl["options"]) == {"report", "read", "poll_hours", "tether_only",
                                    "daily_mb", "bootstrap"}
    assert decl["options"]["tether_only"]["default"] is True
    assert decl["options"]["bootstrap"]["default"] == (
        "dht.transmissionbt.com:6881,dht.libtorrent.org:25401")
    # Every option carries help text for the settings page.
    assert all(spec.get("help") for spec in decl["options"].values())


def test_census_loads_after_remote_train(tmp_path):
    config = {"plugins": {
        "census": {"enabled": True},
        "remote-train": {"enabled": True, "train_host": "h", "train_user": "u",
                         "train_path": "/remote"},
    }}
    loader = Loader(config=config, capture=CaptureInterface(str(tmp_path)),
                    connectivity=ConnectivityState(), services=ServiceRegistry(),
                    search_paths=[PLUGIN_DIR], logger=LOG)
    loaded = loader.load_all()
    try:
        order = [r.name for r in loaded]
        assert order.index("remote-train") < order.index("census")
        assert loader.services.has("census")
        assert loader.services.get("census") is None      # nothing read yet
    finally:
        for rec in loaded:
            loader.unload(rec)


def test_census_bootstrap_parsing(tmp_path):
    _plugin_obj, mod = _plugin(tmp_path)
    parse = mod.parse_bootstrap
    assert parse("a.example.net:6881, b.example.org:25401") == (
        [("a.example.net", 6881), ("b.example.org", 25401)], [])
    assert parse("") == ([], [])
    assert parse(" , ,") == ([], [])
    # Duplicates collapse; bad entries are reported, not used.
    routers, bad = parse("a.example.net:6881,a.example.net:6881,nope,"
                         "x.example.net:0,x.example.net:70000,bad host:1,"
                         "-x.example.net:1,192.0.2.7:6881")
    assert routers == [("a.example.net", 6881), ("192.0.2.7", 6881)]
    assert bad == ["nope", "x.example.net:0", "x.example.net:70000",
                   "bad host:1", "-x.example.net:1"]


def test_census_health_note_names_bad_bootstrap_entries(tmp_path):
    plugin, _mod = _plugin(tmp_path, bootstrap="router.example.net:6881,oops")
    assert "oops" in plugin.context.health_note
    plugin, _mod = _plugin(tmp_path, bootstrap="")
    assert "no usable bootstrap router" in plugin.context.health_note


def test_census_clamps_poll_hours_and_budget(tmp_path):
    plugin, _mod = _plugin(tmp_path, poll_hours=0, daily_mb=-3)
    assert plugin._poll_hours() == 1
    assert plugin._daily_bytes() == 0
    plugin, _mod = _plugin(tmp_path, poll_hours=99, daily_mb=2)
    assert plugin._poll_hours() == 24
    assert plugin._daily_bytes() == 2 * 1024 * 1024
    plugin, _mod = _plugin(tmp_path, poll_hours="x", daily_mb="y")
    assert plugin._poll_hours() == 4
    assert plugin._daily_bytes() == 5 * 1024 * 1024


# -- step 2: bencode -------------------------------------------------------------


def test_bencode_round_trips():
    for value in (0, -7, 1 << 62, b"", b"spam", [], [1, b"x", [2]],
                  {b"a": 1, b"b": [b"c", {b"d": b""}]}):
        assert dht.bdecode(dht.bencode(value)) == value
    # Dict keys are written sorted, whatever order they came in.
    assert dht.bencode({b"z": 1, b"a": 2}) == b"d1:ai2e1:zi1ee"


def test_bencode_refuses_what_it_cant_encode():
    import pytest
    for value in (True, 1.5, {"text-key": 1}, object()):
        with pytest.raises(TypeError):
            dht.bencode(value)


def test_bdecode_rejects_malformed_input():
    import pytest
    cases = [b"", b"i01e", b"i-0e", b"ie", b"i-e", b"i1x2e", b"i%de" % (1 << 63),
             b"5:abc", b"01:a", b"3abc", b"l", b"li1e", b"d1:ai1e", b"di1ei1ee",
             b"d1:ai1e1:ai2ee", b"i1ei2e", b"x", b"le" + b"x",
             b"l" * 5 + b"e" * 5,                       # deeper than 4
             b"l" + b"i0e" * 65 + b"e",                  # more than 64 items
             b"5:" + b"a" * 3000]                        # over max_len
    for data in cases:
        with pytest.raises(dht.BencodeError):
            dht.bdecode(data)
    with pytest.raises(dht.BencodeError):
        dht.bdecode("not bytes")
    # Within the caps is fine.
    assert dht.bdecode(b"l" * 4 + b"e" * 4) == [[[[]]]]


def test_bdecode_fuzz_only_ever_raises_bencode_error():
    import random
    rng = random.Random(1234)
    seeds = [dht.bencode({b"t": b"ab", b"y": b"r",
                          b"r": {b"id": bytes(20), b"nodes": bytes(52),
                                 b"token": b"tok", b"seq": 5, b"v": b"x" * 40}}),
             dht.bencode([1, [2, [3]], {b"k": b"v"}])]
    for _ in range(10000):
        data = bytearray(rng.choice(seeds))
        for _m in range(rng.randint(1, 4)):
            pos = rng.randrange(len(data))
            op = rng.random()
            if op < 0.4:
                data[pos] = rng.randrange(256)
            elif op < 0.7:
                del data[pos]
            else:
                data.insert(pos, rng.choice(b"ilde0123456789:-"))
        try:
            dht.bdecode(bytes(data))
        except dht.BencodeError:
            pass


# -- step 3: CRC32-C and BEP 42 ----------------------------------------------------

def test_crc32c_check_value():
    assert dht.crc32c(b"123456789") == 0xE3069283
    assert dht.crc32c(b"") == 0


def test_bep42_ids_made_for_an_address_validate_there_only():
    import random
    rng = random.Random(42)
    for ip in ("192.0.2.1", "198.51.100.77", "203.0.113.250"):
        for _ in range(20):
            nid = dht.bep42_make_id(ip, rng)
            assert len(nid) == 20 and dht.bep42_valid(nid, ip)
            # The same ID from another address, or with a flipped prefix bit,
            # fails (almost always: only 21 bits are checked, so allow none).
            assert not dht.bep42_valid(bytes([nid[0] ^ 0x80]) + nid[1:], ip)
            assert not dht.bep42_valid(nid[:2] + bytes([nid[2] ^ 0x08]) + nid[3:], ip)
    # Only the low 3 bits of the last byte feed the check.
    nid = dht.bep42_make_id("192.0.2.1", rng)
    assert dht.bep42_valid(nid[:19] + bytes([nid[19] ^ 0xF8]), "192.0.2.1")
    changed = [r for r in range(8) if r != nid[19] & 7
               and dht._bep42_crc("192.0.2.1", r) >> 11
               != dht._bep42_crc("192.0.2.1", nid[19]) >> 11]
    assert changed and not dht.bep42_valid(
        nid[:19] + bytes([(nid[19] & 0xF8) | changed[0]]), "192.0.2.1")


def test_bep42_exempt_ranges_and_bad_input():
    for ip in ("10.1.2.3", "172.16.0.9", "172.31.255.1", "192.168.4.4",
               "169.254.1.1", "127.0.0.1"):
        assert dht.bep42_exempt(ip) and dht.bep42_valid(bytes(20), ip)
    for ip in ("172.32.0.1", "192.0.2.1", "11.0.0.1"):
        assert not dht.bep42_exempt(ip)
    assert not dht.bep42_valid(bytes(19), "192.0.2.1")
    assert not dht.bep42_valid("x" * 20, "192.0.2.1")
    assert not dht.bep42_valid(bytes(20), "not-an-ip")
    assert not dht.bep42_valid(bytes(20), "2001:db8::1")
