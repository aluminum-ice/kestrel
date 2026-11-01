"""Tests for the census plugin and its helper files (the DHT client and the
``kestrel-census/1`` format). No test touches the network: the DHT is an
in-process fake whose nodes sit at RFC 5737 documentation addresses, and every
key and token is generated here."""

import html
import importlib.util
import json
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
proto = load_helper(CENSUS, "_census_proto.py")


def _load(path, modname):
    spec = importlib.util.spec_from_file_location(modname, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _plugin(tmp_path, online=False, **opts):
    """A Census instance with its context, without its worker running."""
    mod = _load(CENSUS, "census_under_test")
    options = {"report": True, "read": True, "poll_minutes": 90,
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
    assert set(decl["options"]) == {"report", "read", "poll_minutes", "tether_only",
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
        census = loader.services.get("census")             # nothing read yet
        assert census["online"] is None and census["active_today"] is None
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


def test_census_clamps_poll_minutes_and_budget(tmp_path):
    plugin, _mod = _plugin(tmp_path, poll_minutes=5, daily_mb=-3)
    assert plugin._poll_minutes() == 60
    assert plugin._daily_bytes() == 0
    plugin, _mod = _plugin(tmp_path, poll_minutes=9999, daily_mb=2)
    assert plugin._poll_minutes() == 1440
    assert plugin._daily_bytes() == 2 * 1024 * 1024
    plugin, _mod = _plugin(tmp_path, poll_minutes="x", daily_mb="y")
    assert plugin._poll_minutes() == 90
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


# -- step 4: transport and KRPC client -----------------------------------------------

class FakeClock:
    """A clock that moves only when something sleeps on it."""

    def __init__(self, start=1000.0):
        self.now = start
        self.slept = 0.0

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds
        self.slept += seconds


class FakeTransport:
    """Hands each sent packet to ``responder(packet, addr) -> [(data, src)]``
    and queues what it returns for ``recv``. ``recv`` returns None when the
    queue is empty, as the real one does when its timeout passes."""

    def __init__(self, responder):
        self.responder = responder
        self.sent = []
        self.inbox = []
        self.max_outstanding = 0
        self._since_recv = 0

    def send(self, data, addr):
        self.sent.append((data, addr))
        self._since_recv += 1
        self.max_outstanding = max(self.max_outstanding, self._since_recv)
        self.inbox.extend(self.responder(data, addr))
        return True

    def recv(self, timeout):
        self._since_recv = 0
        return self.inbox.pop(0) if self.inbox else None

    def close(self):
        self.closed = True


def _reply(packet, body, src):
    msg = dht.bdecode(packet)
    return (dht.bencode({b"t": msg[b"t"], b"y": b"r", b"r": body}), src)


def test_krpc_queries_are_read_only_and_carry_no_version():
    sent = []

    def responder(packet, addr):
        sent.append(dht.bdecode(packet))
        return [_reply(packet, {b"id": bytes(20)}, addr)]
    clock = FakeClock()
    client = dht.KrpcClient(FakeTransport(responder), node_id=b"N" * 20,
                            clock=clock, sleep=clock.sleep)
    out = client.query([(("192.0.2.1", 6881), b"ping", {}),
                        (("192.0.2.2", 6881), b"get", {b"target": bytes(20)})])
    assert [err for _r, err in out] == [None, None]
    for msg in sent:
        assert msg[b"ro"] == 1 and msg[b"y"] == b"q"
        assert b"v" not in msg and msg[b"a"][b"id"] == b"N" * 20
    assert [m[b"q"] for m in sent] == [b"ping", b"get"]
    assert client.queries == 2 and client.sent_bytes > 0 and client.received_bytes > 0


def test_krpc_drops_anything_that_isnt_a_reply_to_its_own_query():
    def responder(packet, addr):
        msg = dht.bdecode(packet)
        good = _reply(packet, {b"id": bytes(20)}, addr)
        return [
            (good[0], ("198.51.100.9", addr[1])),            # right tid, wrong host
            (good[0], (addr[0], addr[1] + 1)),               # right tid, wrong port
            (dht.bencode({b"t": b"zz", b"y": b"r", b"r": {}}), addr),   # unknown tid
            (dht.bencode({b"t": msg[b"t"], b"y": b"q", b"q": b"ping",
                          b"a": {b"id": bytes(20)}}), addr),            # a query
            (b"\xff garbage", addr),
            good,                                            # finally, the reply
        ]
    clock = FakeClock()
    client = dht.KrpcClient(FakeTransport(responder), clock=clock, sleep=clock.sleep)
    [(reply, err)] = client.query([(("192.0.2.1", 6881), b"ping", {})])
    assert err is None and reply == {b"id": bytes(20)}
    assert client.dropped == 5


def test_krpc_reports_errors_and_timeouts():
    def responder(packet, addr):
        msg = dht.bdecode(packet)
        if addr[0] == "192.0.2.1":
            return [(dht.bencode({b"t": msg[b"t"], b"y": b"e",
                                  b"e": [302, b"sequence number less than current"]}), addr)]
        if addr[0] == "192.0.2.2":
            return [(dht.bencode({b"t": msg[b"t"], b"y": b"r", b"r": b"not a dict"}), addr)]
        return []                                            # silent
    clock = FakeClock()
    client = dht.KrpcClient(FakeTransport(responder), clock=clock, sleep=clock.sleep)
    out = client.query([(("192.0.2.1", 1), b"put", {}), (("192.0.2.2", 1), b"get", {}),
                        (("192.0.2.3", 1), b"get", {})])
    assert [err for _r, err in out] == ["e302", "malformed", "timeout"]


def test_krpc_rate_limit_and_in_flight_cap():
    clock = FakeClock()
    transport = FakeTransport(lambda packet, addr: [_reply(packet, {b"id": bytes(20)}, addr)])
    client = dht.KrpcClient(transport, clock=clock, sleep=clock.sleep)
    out = client.query([(("192.0.2.%d" % i, 6881), b"ping", {}) for i in range(1, 26)])
    assert all(err is None for _r, err in out)
    # 25 sends at 10 a second with a burst of 10: at least 1.5 s of waiting.
    assert clock.slept >= 1.5 - 1e-9
    assert transport.max_outstanding <= 8


def test_krpc_link_down_propagates():
    import pytest

    class Gone(FakeTransport):
        def send(self, data, addr):
            raise dht.LinkDown(19, "No such device")
    clock = FakeClock()
    client = dht.KrpcClient(Gone(lambda p, a: []), clock=clock, sleep=clock.sleep)
    with pytest.raises(dht.LinkDown):
        client.query([(("192.0.2.1", 6881), b"ping", {})])


def test_udp_transport_round_trip_on_loopback():
    import socket
    peer = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    peer.bind(("127.0.0.1", 0))
    transport = dht.UdpTransport()
    try:
        port = transport._sock.getsockname()[1]
        assert transport.send(b"hello", peer.getsockname())
        data, src = peer.recvfrom(64)
        assert data == b"hello"
        peer.sendto(b"back", ("127.0.0.1", port))
        assert transport.recv(2.0) == (b"back", peer.getsockname())
        assert transport.recv(0.05) is None
    finally:
        transport.close()
        peer.close()


def test_udp_transport_bind_to_a_missing_interface_fails_clearly():
    import pytest
    with pytest.raises(dht.TetherBindError):
        dht.UdpTransport(bind_device="kcensus-none0")


# -- step 5: node pool, bootstrap, persistence --------------------------------------

def _test_net(ip):
    """The pool's address rule for tests: the RFC 5737 documentation ranges."""
    return ip.startswith(("192.0.2.", "198.51.100.", "203.0.113."))


def test_public_ipv4_keeps_only_public_unicast():
    for ip in ("10.0.0.1", "192.168.1.1", "172.20.10.2", "127.0.0.1", "169.254.0.1",
               "224.0.0.1", "0.0.0.0", "255.255.255.255", "192.0.2.1", "100.64.0.1",
               "not an ip", "2001:db8::1"):
        assert not dht.public_ipv4(ip), ip
    # A public address, found at run time rather than written here.
    import ipaddress
    public = next(str(ipaddress.IPv4Address(n << 24 | 1)) for n in range(1, 224)
                  if ipaddress.IPv4Address(n << 24 | 1).is_global)
    assert dht.public_ipv4(public)


def test_parse_nodes_reads_compact_info():
    import random
    rng = random.Random(5)
    nid = dht.bep42_make_id("192.0.2.10", rng)
    blob = dht.compact_node(nid, "192.0.2.10", 6881) + dht.compact_node(bytes(20), "192.0.2.11", 0)
    assert dht.parse_nodes(blob + b"xx") == [(nid, "192.0.2.10", 6881)]   # port 0 skipped
    assert dht.parse_nodes(None) == []


def test_node_pool_rules():
    import random
    rng = random.Random(9)
    clock = FakeClock()
    pool = dht.NodePool(capacity=4, accept=_test_net, clock=clock)
    good = dht.bep42_make_id("192.0.2.1", rng)
    pool.add(good, "192.0.2.1", 6881)
    pool.add(bytes(20), "10.0.0.5", 6881)           # not accepted
    pool.add(b"short", "192.0.2.2", 6881)          # bad ID
    pool.add(dht.bep42_make_id("192.0.2.3", rng), "192.0.2.3", 0)   # bad port
    assert list(pool.nodes) == [good] and pool.nodes[good][2] is True
    # Hearsay doesn't move a node; the node itself answering does.
    pool.add(good, "192.0.2.99", 1)
    assert pool.nodes[good][:2] == ["192.0.2.1", 6881]
    pool.add(good, "192.0.2.99", 1, ok=True)
    assert pool.nodes[good][:2] == ["192.0.2.99", 1] and pool.nodes[good][2] is False
    assert pool.responsive() == 1
    # Over capacity, nodes that never answered go first.
    for i in range(10, 16):
        pool.add(dht.bep42_make_id("192.0.2.%d" % i, rng), "192.0.2.%d" % i, 6881)
    assert len(pool.nodes) == 4 and good in pool.nodes
    pool.forget(good)
    assert good not in pool.nodes


def test_node_pool_closest_and_bep42_filter():
    import random
    rng = random.Random(3)
    pool = dht.NodePool(accept=_test_net)
    valid = [dht.bep42_make_id("192.0.2.%d" % i, rng) for i in range(1, 6)]
    for i, nid in enumerate(valid, 1):
        pool.add(nid, "192.0.2.%d" % i, 6881)
    invalid = bytes(20)
    pool.add(invalid, "198.51.100.1", 6881)
    target = bytes(20)
    assert pool.closest(target, 1)[0][0] == invalid          # distance 0
    assert invalid not in [n for n, _a in pool.closest(target, 10, only_bep42=True)]
    ranked = [n for n, _a in pool.closest(target, 10, only_bep42=True)]
    assert ranked == sorted(valid, key=lambda n: dht.distance(n, target))


def test_node_pool_saves_privately_and_reloads(tmp_path):
    import random
    import stat
    rng = random.Random(4)
    clock = FakeClock()
    pool = dht.NodePool(accept=_test_net, clock=clock)
    nid = dht.bep42_make_id("192.0.2.7", rng)
    pool.add(nid, "192.0.2.7", 6881, ok=True)
    path = str(tmp_path / "routing.json")
    pool.save(path)
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    again = dht.NodePool(accept=_test_net)
    again.load(path)
    assert again.nodes[nid] == ["192.0.2.7", 6881, True, clock.now]
    for junk in ("not json", "[]", '{"nodes": 5}',
                 '{"nodes": [["zz", "192.0.2.1", 1, 0], [], ["00", 5, 1, 0]]}'):
        (tmp_path / "bad.json").write_text(junk)
        empty = dht.NodePool(accept=_test_net)
        empty.load(str(tmp_path / "bad.json"))
        assert empty.nodes == {}
    empty.load(str(tmp_path / "missing.json"))


def _router_responder(nodes, lose_first=0):
    """A bootstrap router at 203.0.113.1 handing out ``nodes``; it drops the
    first ``lose_first`` queries it gets."""
    seen = []

    def responder(packet, addr):
        seen.append(addr)
        if addr[0] != "203.0.113.1" or len(seen) <= lose_first:
            return []
        blob = b"".join(dht.compact_node(n, ip, port) for n, ip, port in nodes)
        return [_reply(packet, {b"id": bytes(20), b"nodes": blob}, addr)]
    return responder


def test_bootstrap_joins_and_skips_a_dead_router():
    import random
    rng = random.Random(6)
    nodes = [(dht.bep42_make_id("192.0.2.%d" % i, rng), "192.0.2.%d" % i, 6881)
             for i in range(1, 9)]
    clock = FakeClock()
    client = dht.KrpcClient(FakeTransport(_router_responder(nodes)),
                            clock=clock, sleep=clock.sleep)
    pool = dht.NodePool(accept=_test_net)
    resolve = {"live.example.net": ("203.0.113.1", 6881),
               "dead.example.net": ("203.0.113.2", 6881)}.get
    waits = []
    answered = dht.bootstrap(client, pool, [("nxdomain.example.net", 1),
                                            ("dead.example.net", 6881),
                                            ("live.example.net", 6881)],
                             resolve=lambda h, p: resolve(h),
                             wait=lambda s: waits.append(s) or False)
    assert answered == 1 and len(pool.nodes) == 8 and waits == []


def test_bootstrap_retries_when_the_first_round_is_lost():
    import random
    rng = random.Random(7)
    nodes = [(dht.bep42_make_id("192.0.2.%d" % i, rng), "192.0.2.%d" % i, 6881)
             for i in range(1, 5)]
    clock = FakeClock()
    client = dht.KrpcClient(FakeTransport(_router_responder(nodes, lose_first=2)),
                            clock=clock, sleep=clock.sleep)
    pool = dht.NodePool(accept=_test_net)
    waits = []
    answered = dht.bootstrap(client, pool, [("live.example.net", 6881)],
                             resolve=lambda h, p: ("203.0.113.1", p),
                             wait=lambda s: waits.append(s) or False)
    assert answered == 1 and len(pool.nodes) == 4
    assert waits == [10, 20]                     # rounds at 0, 10 and 30 s


def test_bootstrap_gives_up_after_its_rounds_or_when_stopped():
    clock = FakeClock()
    client = dht.KrpcClient(FakeTransport(lambda p, a: []), clock=clock, sleep=clock.sleep)
    pool = dht.NodePool(accept=_test_net)
    waits = []
    assert dht.bootstrap(client, pool, [("live.example.net", 6881)],
                         resolve=lambda h, p: ("203.0.113.1", p),
                         wait=lambda s: waits.append(s) or False) == 0
    assert waits == [10, 20, 60]
    stopped = []
    assert dht.bootstrap(client, pool, [("live.example.net", 6881)],
                         resolve=lambda h, p: ("203.0.113.1", p),
                         wait=lambda s: stopped.append(s) or True) == 0
    assert stopped == [10]


# -- the fake DHT --------------------------------------------------------------------

class FakeDHT:
    """An in-process DHT of nodes at RFC 5737 addresses. Each node knows a
    random handful of others plus its nearest neighbours, answers ``ping``,
    ``find_node``, ``get`` and ``put`` with BEP 44's storage rules (write
    tokens, rising ``seq``, signatures, the 1000-byte limit), and records every
    query it receives, so tests can assert who saw what."""

    def __init__(self, size=300, invalid=0.3, silent=0.0, seed=1):
        import hashlib
        import random
        self.rng = random.Random(seed)
        self._hashlib = hashlib
        addrs = ["%s.%d" % (net, host) for net in ("192.0.2", "198.51.100", "203.0.113")
                 for host in range(1, 255)][:size]
        self.nodes = {}
        for ip in addrs:
            valid = self.rng.random() >= invalid
            nid = (dht.bep42_make_id(ip, self.rng) if valid
                   else bytes(self.rng.randrange(256) for _ in range(20)))
            self.nodes[(ip, 6881)] = {"id": nid, "valid": dht.bep42_valid(nid, ip),
                                      "silent": self.rng.random() < silent,
                                      "store": {}, "known": []}
        everyone = list(self.nodes)
        for addr, node in self.nodes.items():
            near = sorted(everyone, key=lambda a: dht.distance(self.nodes[a]["id"], node["id"]))
            node["known"] = list(dict.fromkeys(near[1:17] + self.rng.sample(everyone, 24)))
        self.log = []                                   # (addr, method, target)

    def seed_pool(self, pool, count=40):
        for addr in self.rng.sample(list(self.nodes), count):
            pool.add(self.nodes[addr]["id"], addr[0], addr[1])

    def token(self, node_addr, requester):
        return self._hashlib.sha1(("%s|%s" % (node_addr, requester[0])).encode()).digest()[:8]

    def closest_to(self, target, count=8, include_silent=False):
        return sorted((a for a in self.nodes if include_silent or not self.nodes[a]["silent"]),
                      key=lambda a: dht.distance(self.nodes[a]["id"], target))[:count]

    def __call__(self, packet, addr):
        node = self.nodes.get((addr[0], addr[1]))
        if node is None or node["silent"]:
            return []
        msg = dht.bdecode(packet)
        method, args = msg[b"q"], msg[b"a"]
        target = args.get(b"target")
        self.log.append((addr, method, target))
        me = (addr[0], addr[1])
        reply = {b"id": node["id"]}

        def error(code, text):
            return [(dht.bencode({b"t": msg[b"t"], b"y": b"e", b"e": [code, text]}), addr)]
        if method in (b"find_node", b"get"):
            known = sorted(node["known"], key=lambda a: dht.distance(self.nodes[a]["id"], target))[:8]
            reply[b"nodes"] = b"".join(dht.compact_node(self.nodes[a]["id"], a[0], a[1]) for a in known)
        if method == b"get":
            reply[b"token"] = self.token(me, ("client", 0))
            item = node["store"].get(target)
            if item:
                reply.update({b"k": item["k"], b"seq": item["seq"], b"sig": item["sig"],
                              b"v": item["v"]})
        if method == b"put":
            if args.get(b"token") != self.token(me, ("client", 0)):
                return error(203, b"bad token")
            k, seq, sig, v = args.get(b"k"), args.get(b"seq"), args.get(b"sig"), args.get(b"v")
            if len(dht.bencode(v)) > 1000:
                return error(205, b"message too big")
            from cryptography.exceptions import InvalidSignature
            from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
            try:
                Ed25519PublicKey.from_public_bytes(k).verify(
                    sig, b"3:seqi%de1:v" % seq + dht.bencode(v))
            except (InvalidSignature, ValueError):
                return error(206, b"invalid signature")
            key = self._hashlib.sha1(k).digest()
            stored = node["store"].get(key)
            if stored and (seq < stored["seq"] or (seq == stored["seq"] and v != stored["v"])):
                return error(302, b"sequence number less than current")
            node["store"][key] = {"k": k, "seq": seq, "sig": sig, "v": v}
            self.log[-1] = (addr, method, key)
        return [(dht.bencode({b"t": msg[b"t"], b"y": b"r", b"r": reply}), addr)]


def _client(network, seed=0):
    import random
    clock = FakeClock()
    rng = random.Random(seed)
    client = dht.KrpcClient(FakeTransport(network), node_id=bytes(rng.randrange(256) for _ in range(20)),
                            clock=clock, sleep=clock.sleep)
    return client, dht.NodePool(accept=_test_net, clock=clock)


# -- step 6: lookup and node caching ---------------------------------------------------

def test_lookup_converges_on_the_nearest_valid_nodes():
    import os as _os
    net = FakeDHT(invalid=0.3, seed=11)
    client, pool = _client(net)
    net.seed_pool(pool)
    hits = 0
    for _ in range(10):
        target = _os.urandom(20)
        found, stats = dht.lookup(client, pool, target)
        assert stats["queries"] <= 40 and len(found) == 8
        best_valid = [a for a in net.closest_to(target, 40) if net.nodes[a]["valid"]][:8]
        hits += len({a for _n, a, _r in found} & set(best_valid))
        assert all(r.get(b"token") for _n, _a, r in found)
    assert hits >= 60                     # most of the true nearest 8, across 10 lookups


def test_lookup_never_sends_a_census_query_to_a_non_bep42_node():
    import os as _os
    net = FakeDHT(invalid=0.5, seed=12)
    client, pool = _client(net)
    net.seed_pool(pool, 80)               # the pool holds invalid nodes too
    for _ in range(10):
        target = _os.urandom(20)
        found, stats = dht.lookup(client, pool, target)
        assert stats["non_bep42_queried"] == 0
        assert all(dht.bep42_valid(n, a[0]) for n, a, _r in found)
        for addr, method, logged_target in net.log:
            if logged_target == target:
                assert net.nodes[addr]["valid"], "a census query reached a non-BEP 42 node"


def test_lookup_drops_silent_nodes_from_the_pool():
    import os as _os
    net = FakeDHT(invalid=0.0, silent=0.3, seed=13)
    client, pool = _client(net)
    net.seed_pool(pool, 80)
    forgotten = set()
    forget = pool.forget
    pool.forget = lambda node_id: forgotten.add(node_id) or forget(node_id)
    for _ in range(3):
        dht.lookup(client, pool, _os.urandom(20))
    asked = {(addr[0], addr[1]) for _data, addr in client.transport.sent}
    asked_silent = [a for a in asked if net.nodes[a]["silent"]]
    assert asked_silent                    # some silent nodes were tried...
    assert all(net.nodes[a]["id"] in forgotten for a in asked_silent)   # ...and dropped
    # A dropped node may be heard of again from others, but never as answering.
    assert not any(pool.nodes.get(net.nodes[a]["id"], [0, 0, 0, 0])[3] for a in asked_silent)


def test_lookup_respects_its_query_cap():
    import os as _os
    net = FakeDHT(invalid=0.0, seed=14)
    client, pool = _client(net)
    net.seed_pool(pool)
    _found, stats = dht.lookup(client, pool, _os.urandom(20), max_queries=7)
    assert stats["queries"] <= 7
    assert client.queries == stats["queries"]


def test_refresh_may_use_any_node():
    import random
    net = FakeDHT(invalid=0.5, seed=15)
    client, pool = _client(net)
    net.seed_pool(pool, 20)
    before = len(pool.nodes)
    dht.refresh(client, pool, random.Random(3))
    assert len(pool.nodes) > before
    assert all(m == b"find_node" for _a, m, _t in net.log)


def test_slot_cache_round_trips_and_ignores_junk():
    cache = dht.SlotCache()
    target = bytes(range(20))
    cache.set(target, [("192.0.2.1", 6881), ("192.0.2.2", 1)])
    again = dht.SlotCache(cache.to_json())
    assert again.get(target) == [("192.0.2.1", 6881), ("192.0.2.2", 1)]
    again.drop(target)
    assert again.get(target) == []
    junk = dht.SlotCache({"aa": [["192.0.2.1", 0], [5, 1], "x", ["192.0.2.3", 9]], 5: [], "bb": "x"})
    assert junk.to_json() == {"aa": [["192.0.2.3", 9]]}


# -- step 7: BEP 44 get and put ---------------------------------------------------------

def _keypair():
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
    sk = Ed25519PrivateKey.generate()
    return sk, sk.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)


def test_items_sign_and_verify():
    sk, pk = _keypair()
    sig = sk.sign(dht.signed_payload(7, b"value"))
    assert dht.verify_item(pk, 7, b"value", sig)
    assert not dht.verify_item(pk, 8, b"value", sig)
    assert not dht.verify_item(pk, 7, b"other", sig)
    assert not dht.verify_item(_keypair()[1], 7, b"value", sig)
    assert not dht.verify_item(pk, 7, b"value", sig[:-1] + bytes([sig[-1] ^ 1]))
    assert not dht.verify_item(b"short", 7, b"value", sig)


def test_items_match_the_bep44_published_example():
    # BEP 44's own test vector for a mutable item without salt.
    pk = bytes.fromhex("77ff84905a91936367c01360803104f92432fcd904a43511876df5cdf3e7e548")
    sig = bytes.fromhex("305ac8aeb6c9c151fa120f120ea2cfb923564e11552d06a5d856091e5e853cff"
                        "1260d3f39e4999684aa92eb73ffd136e6f4f3ecbfda0ce53a1608ecd7ae21f01")
    assert dht.verify_item(pk, 1, b"Hello World!", sig)
    assert dht.item_target(pk).hex() == "4a533d47ec9c7d95b1ad75f576cffc641853b750"


def test_put_then_get_from_elsewhere():
    net = FakeDHT(invalid=0.3, seed=21)
    writer, wpool = _client(net, seed=1)
    net.seed_pool(wpool)
    sk, pk = _keypair()
    empty = dht.get_item(writer, wpool, None, pk)
    assert empty.values == [] and empty.max_seq == -1 and len(empty.storing) == 8
    confirms, errors = dht.put_item(writer, empty, sk, pk, b"entry-bytes", 5)
    assert confirms == 8 and not errors
    # Another client with its own pool finds it.
    reader, rpool = _client(net, seed=2)
    net.seed_pool(rpool)
    got = dht.get_item(reader, rpool, None, pk)
    assert got.holders >= 3 and set(got.values) == {b"entry-bytes"} and got.max_seq == 5


def test_get_uses_cached_nodes_and_falls_back_to_a_lookup():
    net = FakeDHT(invalid=0.0, seed=22)
    client, pool = _client(net)
    net.seed_pool(pool)
    cache = dht.SlotCache()
    sk, pk = _keypair()
    first = dht.get_item(client, pool, cache, pk)
    cached = cache.get(first.target)
    assert len(cached) == 8
    before = client.queries
    dht.get_item(client, pool, cache, pk)
    assert client.queries - before == 8          # straight to the 8 cached nodes
    # Five of them go quiet: fewer than 4 answer, so it looks the slot up again.
    for addr in cached[:5]:
        net.nodes[addr]["silent"] = True
    before = client.queries
    again = dht.get_item(client, pool, cache, pk)
    assert client.queries - before > 8 and again.answered >= 4
    assert cache.get(first.target) != cached


def test_get_rejects_bad_values():
    net = FakeDHT(invalid=0.0, seed=23)
    client, pool = _client(net)
    net.seed_pool(pool)
    sk, pk = _keypair()
    fetched = dht.get_item(client, pool, None, pk)
    dht.put_item(client, fetched, sk, pk, b"good", 3)
    stored = [n for n in net.nodes.values() if fetched.target in n["store"]]
    stored[0]["store"][fetched.target]["sig"] = bytes(64)                 # bad signature
    stored[1]["store"][fetched.target]["k"] = _keypair()[1]               # another key
    got = dht.get_item(client, pool, None, pk)
    assert got.rejected == 2 and set(got.values) == {b"good"}
    assert got.holders == len(stored) - 2


def test_put_reports_a_lower_seq_and_refuses_a_frozen_slot():
    import pytest
    net = FakeDHT(invalid=0.0, seed=24)
    client, pool = _client(net)
    net.seed_pool(pool)
    sk, pk = _keypair()
    fetched = dht.get_item(client, pool, None, pk)
    assert dht.put_item(client, fetched, sk, pk, b"a", 10)[0] == 8
    confirms, errors = dht.put_item(client, fetched, sk, pk, b"b", 9)
    assert confirms == 0 and errors == {"e302": 8}
    frozen = dht.get_item(client, pool, None, pk)
    dht.put_item(client, frozen, sk, pk, b"c", 5_000_000)
    frozen = dht.get_item(client, pool, None, pk)
    with pytest.raises(dht.SlotFrozen):
        dht.put_item(client, frozen, sk, pk, b"d", 5_000_001, ceiling=1_000_000)
    with pytest.raises(ValueError):
        dht.put_item(client, fetched, sk, pk, b"x" * 1000, 11)


# -- step 8: the census format ---------------------------------------------------------------

DAY = "2026-11-14"


def _entry(rng, hour=5, version="1.0.12", agent=(3, 1, 4), contests=(3, 7, 1, 0), bits=8):
    token = bytes(rng.randrange(256) for _ in range(8))
    nonce = proto.find_pow(DAY, token, rng, bits=bits)
    return proto.Entry(token, hour, proto.encode_version(version),
                       proto.encode_agent(*agent), contests, nonce)


def test_slot_keys_are_public_and_distinct():
    a = proto.slot_key(DAY, 5)
    assert proto.slot_key(DAY, 5)[1:] == a[1:]                   # anyone derives them
    assert a[2] == dht.item_target(a[1])
    others = {proto.slot_key(DAY, 6)[2], proto.slot_key(DAY, 5, alt=1)[2],
              proto.slot_key("2026-11-15", 5)[2]}
    assert a[2] not in others and len(others) == 3


def test_version_encoding():
    enc, dec = proto.encode_version, proto.decode_version
    for text in ("1.0.11", "1.0.12-beta.3", "0.1.0", "7.15.63", "1.0.12-beta.6"):
        assert dec(enc(text)) == text
    assert dec(enc("1.0.12-beta.9")) == "1.0.12-beta.7+"
    for text in ("master", "", None, "2.0", "8.0.0", "1.16.0", "1.0.64", "1.0.11-rc.1",
                 "1.0.11-beta.0", "v1.0.11", " 1.0.11 x"):
        assert enc(text) == 0, text
    assert dec(0) == "unknown"
    order = sorted([enc(v) for v in ("1.0.11", "1.0.12-beta.2", "master", "1.0.12",
                                     "1.0.12-beta.3", "1.0.9")], key=proto.version_sort_key)
    assert [dec(v) for v in order] == ["1.0.12", "1.0.12-beta.3", "1.0.12-beta.2",
                                       "1.0.11", "1.0.9", "unknown"]


def test_agent_byte_round_trips():
    for state in range(4):
        for algorithm in range(8):
            for remote in range(8):
                assert proto.decode_agent(proto.encode_agent(state, algorithm, remote)) == (
                    state, algorithm, remote)


def test_entries_round_trip_and_clamp_counts():
    import random
    rng = random.Random(1)
    entry = _entry(rng, contests=(0, 300, -2, 255))
    data = entry.encode()
    assert len(data) == proto.ENTRY_SIZE
    back = proto.Entry.decode(data)
    assert back.contests == (0, 255, 0, 255) and back.token == entry.token
    assert proto.MAX_ENTRIES * proto.ENTRY_SIZE + 4 <= dht.MAX_VALUE


def test_proof_of_work():
    import random
    rng = random.Random(2)
    token = bytes(8)
    nonce = proto.find_pow(DAY, token, rng, bits=8)
    assert proto.pow_ok(DAY, token, nonce, bits=8)
    # The work is bound to the day (this nonce happens not to pass for the next).
    assert not proto.pow_ok("2026-11-15", token, nonce, bits=8)
    assert proto.find_pow(DAY, token, rng, bits=30, batch=1000,
                          should_stop=lambda: True) is None
    # The real difficulty, once.
    nonce = proto.find_pow(DAY, token, rng)
    assert proto.pow_ok(DAY, token, nonce) and not proto.pow_ok(DAY, bytes([1]) + token[1:], nonce)


def test_parse_value_keeps_good_entries_only():
    import random
    rng = random.Random(3)
    good, older = _entry(rng, hour=7), _entry(rng, hour=6)
    newer_of_older = proto.Entry(older.token, 8, older.version, older.agent,
                                 older.contests, older.nonce)
    bad_pow = proto.Entry(bytes(8), 7, 0, 0, (0, 0, 0, 0), b"\xff" * 4)
    bad_hour = proto.Entry(good.token, 24, 0, 0, (0, 0, 0, 0), good.nonce)
    value = b"".join(e.encode() for e in (good, older, newer_of_older, bad_pow, bad_hour)) + b"xx"
    entries, rejected = proto.parse_value(value, DAY, bits=8)
    assert set(entries) == {good.token, older.token}
    assert entries[older.token].hour == 8
    assert rejected == 3                       # bad PoW, bad hour, the stray bytes
    assert proto.parse_value(None, DAY) == ({}, 1)


def test_merge_keeps_this_hour_and_the_last():
    import random
    rng = random.Random(4)
    now = _entry(rng, hour=10)
    last = _entry(rng, hour=9)
    stale = _entry(rng, hour=7)
    own = _entry(rng, hour=10)
    own_old = proto.Entry(own.token, 8, 0, 0, (0, 0, 0, 0), own.nonce)
    copies = [b"".join(e.encode() for e in (now, stale, own_old)), last.encode()]
    value, count = proto.merge(copies, own, DAY, 10, bits=8)
    entries, _bad = proto.parse_value(value, DAY, bits=8)
    assert count == 3 and set(entries) == {now.token, last.token, own.token}
    assert entries[own.token] == own


def test_merge_reports_a_full_slot():
    import random
    rng = random.Random(5)
    many = [_entry(rng, hour=10) for _ in range(proto.MAX_ENTRIES)]
    value, count = proto.merge([b"".join(e.encode() for e in many)], _entry(rng, hour=10),
                               DAY, 10, bits=8)
    assert value is None and count == proto.MAX_ENTRIES + 1


def test_new_seq_is_random_within_the_minute_and_above_what_is_stored():
    import random
    rng = random.Random(6)
    seqs = {proto.new_seq(600, -1, rng) for _ in range(50)}
    assert min(seqs) >= 60000 and max(seqs) <= 60099 and len(seqs) > 20
    assert proto.new_seq(600, 70000, rng) == 70001


def _census_net(seed):
    import random
    net = FakeDHT(invalid=0.2, seed=seed)
    client, pool = _client(net, seed=seed)
    net.seed_pool(pool)
    return net, client, pool, random.Random(seed)


def test_publish_writes_and_a_second_writer_keeps_the_first():
    net, client, pool, rng = _census_net(31)
    first, second = _entry(rng, hour=10), _entry(rng, hour=10)
    done = proto.publish(dht, client, pool, None, DAY, 7, first, 600, rng, bits=8)
    assert done.alt == 0 and done.confirms >= 6 and done.count == 1
    holders, mine = proto.holds_entry(dht, client, pool, None, DAY, 7, 0, first.token, bits=8)
    assert mine >= 3 and mine == holders
    # Another device, with its own pool, adds itself; both entries survive.
    other, opool = _client(net, seed=99)
    net.seed_pool(opool)
    done = proto.publish(dht, other, opool, None, DAY, 7, second, 601, rng, bits=8)
    assert done.alt == 0 and done.count == 2
    entries, _bad = proto.read_slot(dht, client, pool, None, DAY, 7, bits=8)
    assert set(entries) == {first.token, second.token}


def test_publish_moves_to_an_alternate_when_full_or_frozen():
    net, client, pool, rng = _census_net(32)
    crowd = [_entry(rng, hour=10) for _ in range(proto.MAX_ENTRIES)]
    private, public, _t = proto.slot_key(DAY, 3)
    fetched = dht.get_item(client, pool, None, public)
    dht.put_item(client, fetched, private, public, b"".join(e.encode() for e in crowd), 50)
    own = _entry(rng, hour=10)
    done = proto.publish(dht, client, pool, None, DAY, 3, own, 600, rng, bits=8)
    assert done.alt == 1 and done.skipped == [(0, "full")]
    # A slot whose stored seq is past the ceiling can't be written either.
    private, public, _t = proto.slot_key(DAY, 4)
    fetched = dht.get_item(client, pool, None, public)
    dht.put_item(client, fetched, private, public, b"", proto.SEQ_CEILING + 5)
    done = proto.publish(dht, client, pool, None, DAY, 4, own, 600, rng, bits=8)
    assert done.alt == 1 and done.skipped == [(0, "frozen")]
    # A reader follows a full or frozen slot to its alternate, and stops there.
    entries, _bad = proto.read_slot(dht, client, pool, None, DAY, 3, bits=8)
    assert own.token in entries and len(entries) == proto.MAX_ENTRIES + 1
    before = client.queries
    proto.read_slot(dht, client, pool, None, DAY, 9, bits=8)        # an ordinary slot
    asked = [t for _a, _m, t in net.log[-(client.queries - before):]]
    assert proto.slot_key(DAY, 9, alt=1)[2] not in asked


def test_publish_retries_once_after_a_newer_seq():
    from collections import Counter
    net, client, pool, rng = _census_net(33)
    calls = []

    def put_item(*args, **kwargs):
        calls.append(args[5])
        if len(calls) == 1:
            return 0, Counter({"e302": 8})
        return dht.put_item(*args, **kwargs)
    flaky = types.SimpleNamespace(get_item=dht.get_item, put_item=put_item)
    own = _entry(rng, hour=10)
    done = proto.publish(flaky, client, pool, None, DAY, 2, own, 600, rng, bits=8)
    assert done.retried and len(calls) == 2 and done.confirms >= 6 and done.alt == 0


def test_publish_skips_a_slot_with_no_reachable_nodes():
    import random
    net = FakeDHT(seed=34, silent=1.0)
    client, pool = _client(net)
    net.seed_pool(pool)
    rng = random.Random(34)
    done = proto.publish(dht, client, pool, None, DAY, 1, _entry(rng), 600, rng, bits=8)
    assert done.alt is None
    assert [why for _alt, why in done.skipped] == ["no nodes"] * (proto.ALTERNATES + 1)


# -- step 9: what this device reports ------------------------------------------------

NOW = 1_800_000_000.0


def _status(running=True, policy="learned", algorithm="PPO", age=60, tally=None):
    return {"running": running, "policy": policy, "algorithm": algorithm,
            "updated": NOW - age, "promotion_tally": tally or {}}


def test_agent_state_from_its_status(tmp_path):
    _plugin_obj, mod = _plugin(tmp_path)
    assert mod.agent_fields(None, NOW) == (proto.AGENT_NONE, 0)
    assert mod.agent_fields("junk", NOW) == (proto.AGENT_NONE, 0)
    assert mod.agent_fields(_status(), NOW) == (proto.AGENT_LEARNED, 2)
    assert mod.agent_fields(_status(policy="baseline", algorithm="RecurrentPPO"),
                            NOW) == (proto.AGENT_SWEEP, 1)
    # Stopped (it says so), or a status too old to trust: not running, with
    # the configured algorithm still reported.
    assert mod.agent_fields(_status(running=False, algorithm="QR-DQN"),
                            NOW) == (proto.AGENT_STOPPED, 4)
    assert mod.agent_fields(_status(age=7 * 3600, algorithm="DQN"),
                            NOW) == (proto.AGENT_STOPPED, 3)
    assert mod.agent_fields(_status(algorithm="SAC"), NOW) == (proto.AGENT_LEARNED, 0)


def test_remote_state_from_the_service(tmp_path):
    _plugin_obj, mod = _plugin(tmp_path)
    hour, day = 3600, 86400
    ok = {"configured": True, "host_state": "reachable", "stack": "ready",
          "last_contact": NOW - hour, "last_model": NOW - 2 * day, "running": []}
    assert mod.remote_state(None, NOW) == proto.REMOTE_OFF
    assert mod.remote_state(dict(ok, configured=False), NOW) == proto.REMOTE_UNCONFIGURED
    assert mod.remote_state(dict(ok, stack="failed"), NOW) == proto.REMOTE_FAILING
    assert mod.remote_state(dict(ok, last_contact=None), NOW) == proto.REMOTE_FAILING
    assert mod.remote_state(dict(ok, last_contact=NOW - 25 * hour), NOW) == proto.REMOTE_FAILING
    assert mod.remote_state(dict(ok, last_model=None), NOW) == proto.REMOTE_IDLE
    assert mod.remote_state(dict(ok, last_model=NOW - 8 * day), NOW) == proto.REMOTE_IDLE
    assert mod.remote_state(ok, NOW) == proto.REMOTE_WORKING
    # An unreachable host reached within the day still counts: a desktop
    # that's off overnight isn't failing.
    assert mod.remote_state(dict(ok, host_state="unreachable"), NOW) == proto.REMOTE_WORKING
    assert mod.remote_state(dict(ok, last_contact="soon"), NOW) == proto.REMOTE_FAILING


def test_contest_counts_and_the_daily_baseline(tmp_path):
    _plugin_obj, mod = _plugin(tmp_path)
    tally = {"local": {"won": 3, "lost": 9}, "remote": {"won": 2, "lost": "x"},
             "before_fair_promotion": {"local": 40, "remote": 7}}
    assert mod.contest_counts(None) is None
    assert mod.contest_counts(_status(tally=tally)) == [3, 9, 2, 0]
    assert mod.contest_counts({"promotion_tally": "junk"}) == [0, 0, 0, 0]
    # The first reading of the day becomes the baseline.
    today, base = mod.contests_today([3, 9, 2, 0], None)
    assert today == [0, 0, 0, 0] and base == [3, 9, 2, 0]
    today, base = mod.contests_today([5, 9, 3, 1], base)
    assert today == [2, 0, 1, 1] and base == [3, 9, 2, 0]
    # A tally that went down (the agent's state was reset) resets the baseline.
    today, base = mod.contests_today([1, 0, 0, 0], base)
    assert today == [0, 0, 0, 0] and base == [1, 0, 0, 0]
    # Saturating at 255; no agent status counts nothing and keeps the baseline.
    assert mod.contests_today([400, 0, 0, 0], [0, 0, 0, 0])[0] == [255, 0, 0, 0]
    assert mod.contests_today(None, [1, 2, 3, 4]) == ([0, 0, 0, 0], [1, 2, 3, 4])


def test_facts_from_services_with_the_remote_state_fixed_for_the_day(tmp_path):
    plugin, mod = _plugin(tmp_path)
    mod.device_version = lambda: "1.0.12-beta.2"
    remote = {"configured": True, "stack": "ready", "last_contact": NOW - 60,
              "last_model": NOW - 60}
    status = _status(tally={"local": {"won": 1, "lost": 2}, "remote": {"won": 0, "lost": 0}})
    plugin.context.services.provide("agent", lambda: status, owner="daemon")
    plugin.context.services.provide("remote-training", lambda: dict(remote),
                                     owner="remote-train")
    day = {}
    version, agent, contests = plugin._facts(NOW, day)
    assert proto.decode_version(version) == "1.0.12-beta.2"
    assert proto.decode_agent(agent) == (proto.AGENT_LEARNED, 2, proto.REMOTE_WORKING)
    assert contests == [0, 0, 0, 0] and day["baseline"] == [1, 2, 0, 0]
    # Later the same day: the host fails, but the day's remote state stands.
    remote["stack"] = "failed"
    status["promotion_tally"]["remote"]["won"] = 1
    _v, agent, contests = plugin._facts(NOW + 600, day)
    assert proto.decode_agent(agent)[2] == proto.REMOTE_WORKING
    assert contests == [0, 0, 1, 0]


def test_facts_without_services_or_with_a_provider_that_raises(tmp_path):
    plugin, mod = _plugin(tmp_path)
    mod.device_version = lambda: "master"

    def broken():
        raise RuntimeError("provider failed")
    plugin.context.services.provide("agent", broken, owner="daemon")
    version, agent, contests = plugin._facts(NOW, {})
    assert version == 0 and contests == [0, 0, 0, 0]
    assert proto.decode_agent(agent) == (proto.AGENT_NONE, 0, proto.REMOTE_OFF)


# -- step 10: the worker ---------------------------------------------------------------

class Link(types.SimpleNamespace):
    """The plugin's view of connectivity, which a test can flip."""

    def active_paths(self):
        return list(self.paths)


def _worker(tmp_path, net=None, seed=41, name="dev", **opts):
    """A Census plugin wired to a fake DHT and a fake clock (``NOW``, 08:00
    UTC), with one working router name and one that doesn't resolve. Drive it
    with ``_run_for``."""
    import random
    plugin, mod = _plugin(tmp_path, online=True, **opts)
    net = net or FakeDHT(invalid=0.2, seed=seed)
    clock = FakeClock(start=NOW)
    plugin.context.connectivity = Link(online=True, paths=["bnep0"])
    state = str(tmp_path / ("state-" + name))
    plugin._state_dir = lambda: state
    plugin._now = clock
    plugin._rng = random.Random(seed)
    plugin.opened = []

    def open_transport(device):
        plugin.opened.append(device)
        plugin.transport = FakeTransport(net)
        return plugin.transport
    plugin._open_transport = open_transport
    router = sorted(net.nodes)[0]
    plugin.resolved = []

    def resolve(host, port):
        plugin.resolved.append(host)
        return router if host == "router.example.net" else None
    plugin._resolve = resolve
    plugin._accept = _test_net
    plugin._bits = 8
    plugin._backoff = (0,)
    plugin._client_opts = {"clock": clock, "sleep": clock.sleep}
    plugin._clock_synced = lambda now: True
    plugin.writes = []
    publish = plugin._publish

    def logged(now):
        plugin.writes.append(now)
        publish(now)
    plugin._publish = logged
    mod.device_version = lambda: "1.0.12"
    return plugin, mod, net, clock


def _run_for(plugin, clock, seconds):
    end = clock.now + seconds
    while clock.now < end:
        delay = plugin._tick()
        clock.now = min(end, clock.now + max(1.0, delay))


def _stored(net, day, index, alt=0):
    """Every entry the fake DHT holds for a slot, across its nodes."""
    _private, public, target = proto.slot_key(day, index, alt)
    found = {}
    for node in net.nodes.values():
        item = node["store"].get(target)
        if item:
            found.update(proto.parse_value(item["v"], day, bits=8)[0])
    return found


def test_worker_joins_writes_and_checks(tmp_path):
    plugin, _mod, net, clock = _worker(tmp_path)
    _run_for(plugin, clock, 2 * 3600)
    assert plugin.opened == ["bnep0"]                # bound to the tether
    assert plugin.resolved[:1] == ["router.example.net"]
    day = plugin._day
    # The first write waited 5-30 minutes; then one an hour.
    assert NOW + 300 <= plugin.writes[0] <= NOW + 1800
    assert len(plugin.writes) == 2
    assert {int(t // 3600) for t in plugin.writes} == {int(NOW // 3600), int(NOW // 3600) + 1}
    token = bytes.fromhex(day["token"])
    entry = _stored(net, day["day"], day["slot"], day["alt"])[token]
    assert proto.decode_version(entry.version) == "1.0.12"
    assert plugin._last_write["ok"] and plugin._last_write["held"][0] >= 3
    # State is private, and nothing in it is a node or router address.
    state = tmp_path / "state-dev"
    assert oct(os.stat(state).st_mode & 0o777) == "0o700"
    for name in ("day.json", "seen.json", "pool.json", "routing.json"):
        assert oct(os.stat(state / name).st_mode & 0o777) == "0o600", name
    assert plugin.context.health_note == ""


def test_worker_reads_the_census_and_counts_online_devices(tmp_path):
    net = FakeDHT(invalid=0.2, seed=42)
    writers = [_worker(tmp_path, net=net, seed=50 + i, name="w%d" % i, read=False)
               for i in range(3)]
    for plugin, _mod, _net, clock in writers:
        _run_for(plugin, clock, 3600)
    reader, _mod, _net, clock = _worker(tmp_path, net=net, seed=60, name="r", report=False)
    _run_for(reader, clock, 3600)
    seen = reader._seen[reader._day["day"]]
    tokens = {bytes.fromhex(w[0]._day["token"]).hex() for w in writers}
    assert tokens <= set(seen["entries"])
    assert seen["online"] == len(tokens) and reader._last_read["complete"]
    # The next read comes about poll_minutes on.
    assert 0.9 * 90 * 60 <= reader._next_read - reader._last_read["at"] <= 1.1 * 90 * 60 + 120


def test_worker_gates_pause_it_and_it_resumes(tmp_path):
    plugin, _mod, _net, clock = _worker(tmp_path)
    for gate, reason in ((lambda: setattr(plugin, "_clock_synced", lambda now: False),
                          "waiting for the clock to be set"),
                         (lambda: setattr(plugin.context.connectivity, "online", False),
                          "offline"),
                         (lambda: setattr(plugin.context.connectivity, "paths", ["wlan0"]),
                          "waiting for the phone's Bluetooth tether")):
        gate()
        _run_for(plugin, clock, 3600)
        assert plugin.context.health_note == "paused: " + reason
        assert plugin.opened == [] and plugin.writes == []
        plugin._clock_synced = lambda now: True
        plugin.context.connectivity.online = True
        plugin.context.connectivity.paths = ["bnep0"]
    _run_for(plugin, clock, 3600)
    assert plugin.opened == ["bnep0"] and plugin.writes
    assert plugin.context.health_note == ""


def test_worker_without_tether_only_uses_any_link_unbound(tmp_path):
    plugin, _mod, _net, clock = _worker(tmp_path, tether_only=False)
    plugin.context.connectivity.paths = ["wlan0"]
    _run_for(plugin, clock, 3600)
    assert plugin.opened == [None] and plugin.writes


def test_worker_with_report_and_read_off_does_nothing(tmp_path):
    plugin, _mod, _net, clock = _worker(tmp_path, report=False, read=False)
    _run_for(plugin, clock, 3600)
    assert plugin.opened == []
    assert plugin.context.health_note == "paused: reporting and reading are both off"


def test_worker_link_down_mid_write_pauses_and_the_next_hour_writes(tmp_path):
    plugin, _mod, net, clock = _worker(tmp_path, read=False)
    _run_for(plugin, clock, 200)                    # joined, before the first write
    assert plugin._joined and not plugin.writes
    write = plugin._publish

    def tether_drops_mid_write(now):
        # A few packets in, the phone's hotspot goes to sleep.
        transport, real = plugin.transport, plugin.transport.send
        sent = len(transport.sent)

        def send(data, addr):
            if len(transport.sent) >= sent + 3:
                plugin.context.connectivity.paths = []
                raise dht.LinkDown(19, "No such device")
            return real(data, addr)
        transport.send = send
        plugin._publish = write
        write(now)
    plugin._publish = tether_drops_mid_write
    _run_for(plugin, clock, 1800)
    assert plugin.writes and not plugin._joined and plugin.transport.closed
    assert plugin.context.health_note == (
        "paused: waiting for the phone's Bluetooth tether")
    # The tether comes back: a fresh socket, and the hour is written.
    plugin.context.connectivity.paths = ["bnep0"]
    _run_for(plugin, clock, 2 * 3600)
    day = plugin._day
    assert len(plugin.opened) >= 2
    assert bytes.fromhex(day["token"]) in _stored(net, day["day"], day["slot"], day["alt"])


def test_worker_bootstraps_from_saved_router_addresses_before_names(tmp_path):
    plugin, mod, net, clock = _worker(tmp_path, read=False)
    _run_for(plugin, clock, 200)
    assert plugin.resolved == ["router.example.net", "dht.example.org"]
    state = tmp_path / "state-dev"
    saved = mod.json.loads((state / "routing.json").read_text())
    assert saved == {"routers": [list(sorted(net.nodes)[0])]}
    # A later join with a stale pool goes to the saved address: no DNS lookup.
    (state / "pool.json").unlink()
    again, _mod2, _n, clock2 = _worker(tmp_path, net=net, read=False)
    clock2.now = clock.now
    _run_for(again, clock2, 200)
    assert again._joined and again.resolved == []
    # A fresh pool needs no router at all.
    third, _mod3, _n, clock3 = _worker(tmp_path, net=net, read=False)
    clock3.now = clock2.now
    (state / "routing.json").unlink()
    _run_for(third, clock3, 200)
    assert third._joined and third.resolved == []
    assert not any(addr == sorted(net.nodes)[0] for _d, addr in third.transport.sent[:1])


def test_worker_falls_back_to_names_when_saved_addresses_are_silent(tmp_path):
    plugin, mod, net, clock = _worker(tmp_path, read=False)
    state = tmp_path / "state-dev"
    # An address the fake DHT has no node at: nothing answers there.
    plugin._save_json("routing.json", {"routers": [["203.0.113.254", 6881]]})
    _run_for(plugin, clock, 200)
    assert plugin._joined and "router.example.net" in plugin.resolved
    assert [["203.0.113.254", 6881]] != mod.json.loads(
        (state / "routing.json").read_text())["routers"]


def test_worker_reports_why_it_cant_join(tmp_path):
    plugin, _mod, net, clock = _worker(tmp_path, read=False)
    plugin._resolve = lambda host, port: None
    _run_for(plugin, clock, 300)
    assert not plugin._joined
    assert plugin.context.health_note.startswith("can't join the DHT")

    def unbindable(device):
        raise dht.TetherBindError(19, "can't bind to bnep0: No such device")
    other, _mod2, _n, clock2 = _worker(tmp_path, net=net, name="other")
    other._open_transport = unbindable
    _run_for(other, clock2, 300)
    assert other.context.health_note == "can't bind to bnep0: No such device"


def test_worker_checks_its_write_and_writes_again_when_overwritten(tmp_path):
    import random
    plugin, _mod, net, clock = _worker(tmp_path, read=False)
    while not plugin.writes:
        _run_for(plugin, clock, 10)
    day = plugin._day
    # Another writer's put lands over the slot without this device's entry.
    other, opool = _client(net, seed=77)
    net.seed_pool(opool)
    private, public, _t = proto.slot_key(day["day"], day["slot"], day["alt"])
    fetched = dht.get_item(other, opool, None, public)
    token = os.urandom(8)
    stranger = proto.Entry(token, 8, proto.encode_version("1.0.11"), 0, (0, 0, 0, 0),
                           proto.find_pow(day["day"], token, random.Random(5), bits=8))
    dht.put_item(other, fetched, private, public, stranger.encode(), fetched.max_seq + 50)
    assert bytes.fromhex(day["token"]) not in _stored(net, day["day"], day["slot"], day["alt"])
    _run_for(plugin, clock, 120)
    assert len(plugin.writes) == 2                       # the check wrote it again
    stored = _stored(net, day["day"], day["slot"], day["alt"])
    assert bytes.fromhex(day["token"]) in stored and stranger.token in stored


def test_worker_rolls_the_day_over(tmp_path):
    plugin, mod, net, clock = _worker(tmp_path, read=False)
    tally = {"local": {"won": 4, "lost": 1}, "remote": {"won": 2, "lost": 2}}
    plugin.context.services.provide("agent", lambda: _status(tally=tally), owner="daemon")
    clock.now = NOW + 14 * 3600                          # 22:00 UTC
    _run_for(plugin, clock, 3600)
    first = dict(plugin._day)
    assert first["nonce"] and first["baseline"] == [4, 1, 2, 2]
    tally["local"]["won"] = 5                           # a contest late in the day
    _run_for(plugin, clock, 2400)                        # upkeep notes it (23:40)
    _run_for(plugin, clock, 3600)                        # past midnight
    day = plugin._day
    assert day["day"] != first["day"] and day["token"] != first["token"]
    assert day["baseline"] == [5, 1, 2, 2] and day["bytes"] < first["bytes"] + 10**6
    assert day["nonce"] is None or proto.pow_ok(day["day"], bytes.fromhex(day["token"]),
                                                bytes.fromhex(day["nonce"]), bits=8)


def test_worker_survives_a_restart_without_redoing_its_work(tmp_path):
    plugin, mod, net, clock = _worker(tmp_path, read=False)
    while not plugin.writes:
        _run_for(plugin, clock, 60)
    day = dict(plugin._day)
    again, mod2, _n, clock2 = _worker(tmp_path, net=net, read=False)
    clock2.now = clock.now + 60
    calls = []
    real = mod2.proto.find_pow
    mod2.proto.find_pow = lambda *a, **k: calls.append(1) or real(*a, **k)
    try:
        _run_for(again, clock2, 2 * 3600)
    finally:
        mod2.proto.find_pow = real
    assert again._day["token"] == day["token"] and again._day["slot"] == day["slot"]
    assert calls == [] and again.writes
    # This hour was written before the restart, so the next write is next hour.
    assert int(again.writes[0] // 3600) == int(plugin.writes[0] // 3600) + 1


def test_worker_rebuilds_corrupt_state(tmp_path):
    plugin, _mod, _net, clock = _worker(tmp_path, read=False)
    state = tmp_path / "state-dev"
    state.mkdir()
    (state / "day.json").write_text('{"day": "2027-01-15", "token": "zz", "slot": 99}')
    (state / "seen.json").write_text("[1, 2")
    (state / "pool.json").write_text('{"nodes": [["nothex", 1, 2, 3]]}')
    (state / "routing.json").write_text('{"routers": [["10.0.0.1", 6881], "x"]}')
    _run_for(plugin, clock, 3600)
    assert plugin.writes and len(plugin._day["token"]) == 16


def test_worker_keeps_to_the_data_budget(tmp_path):
    plugin, _mod, _net, clock = _worker(tmp_path, report=False)
    plugin._daily_bytes = lambda: 40000
    _run_for(plugin, clock, 1800)
    assert plugin._last_read and not plugin._last_read["complete"]
    assert plugin._day["bytes"] < 40000 * 1.5
    plugin._day["bytes"] = 70000
    _run_for(plugin, clock, 120)
    assert plugin.context.health_note == "paused: over today's data budget"


def test_worker_stops_promptly_on_unload(tmp_path):
    import time as _time
    plugin, _mod, _net, _clock = _worker(tmp_path)
    plugin._clock_synced = lambda now: False         # paused: it waits on its event
    plugin._now = _time.time
    plugin.on_ready()
    _time.sleep(0.2)
    started = _time.monotonic()
    plugin.on_unload()
    plugin._worker.join(5)
    assert not plugin._worker.is_alive() and _time.monotonic() - started < 5


# -- step 11: totals ---------------------------------------------------------------------

def _row(hour=8, version="1.0.12", state=3, algorithm=1, remote=0, contests=(0, 0, 0, 0)):
    return [hour, proto.encode_version(version), proto.encode_agent(state, algorithm, remote)
            ] + list(contests)


def test_tally_counts_every_field():
    rows = [_row(version="1.0.12", contests=(2, 1, 0, 0)),
            _row(hour=7, version="1.0.12-beta.3", algorithm=2, remote=4, contests=(1, 0, 3, 1)),
            _row(hour=3, version="master", state=2, remote=2),
            _row(hour=8, version="1.0.11", state=0, algorithm=0, remote=1)]
    tally = proto.Tally(rows, hour=8)
    assert tally.active == 4 and tally.online == 3
    assert tally.version_rows() == [("1.0.12", 1), ("1.0.12-beta.3", 1), ("1.0.11", 1),
                                    ("unknown", 1)]
    assert tally.states == {3: 2, 2: 1, 0: 1}
    assert tally.algorithms == {1: 1, 2: 1}                  # learned policies only
    assert tally.remotes == {0: 1, 4: 1, 2: 1, 1: 1}
    assert tally.contests == [3, 1, 3, 1] and tally.working == [1, 0, 3, 1]
    assert proto.Tally([]).version_rows() == [] and proto.Tally([]).active == 0


def test_tally_leaves_out_contests_a_day_cant_hold():
    rows = [_row(contests=(60, 41, 0, 0)), _row(remote=4, contests=(0, 0, 30, 21)),
            _row(contests=(60, 40, 25, 25))]
    tally = proto.Tally(rows)
    assert tally.active == 3 and tally.capped == 2
    assert tally.contests == [60, 40, 25, 25] and tally.working == [0, 0, 0, 0]


def test_win_rates_with_nothing_judged():
    assert proto.win_rate(0, 0) is None
    assert proto.win_rate(3, 1) == 0.75


def test_census_service_before_and_after_a_read(tmp_path):
    plugin, _mod = _plugin(tmp_path)
    service = plugin.provide_census()
    assert set(service) == {"online", "active_today", "active_7d_avg", "versions",
                            "local_win_rate_7d", "remote_win_rate_7d"}
    assert all(value is None for value in service.values())

    net = FakeDHT(invalid=0.2, seed=43)
    for i in range(2):
        writer, _m, _n, clock = _worker(tmp_path, net=net, seed=70 + i, name="w%d" % i,
                                        read=False)
        writer.context.services.provide("agent", lambda: _status(), owner="daemon")
        _run_for(writer, clock, 3600)
    reader, _m, _n, clock = _worker(tmp_path, net=net, seed=80, name="r", report=False)
    _run_for(reader, clock, 3600)
    service = reader.provide_census()
    assert service["online"] == 2 and service["active_today"] == 2
    assert service["versions"] == {"1.0.12": 2} and service["active_7d_avg"] == 2
    assert service["local_win_rate_7d"] is None


def test_history_and_the_seven_day_figures_across_days(tmp_path):
    plugin, mod, _net, clock = _worker(tmp_path, report=False)
    today = mod._utc(NOW)[0]
    days = [mod._utc(NOW - i * 86400)[0] for i in range(9)]
    history = {day: {"active": 10 + i, "online": 3, "capped": 0,
                     "contests": [i, 1, 2, 1], "working": [0, 0, 2, 1]}
               for i, day in enumerate(days) if day != today}
    plugin._save_json("history.json", {"days": history})
    plugin._save_json("seen.json", {"days": {today: {
        "entries": {"%016x" % n: _row(remote=4, contests=(1, 0, 1, 1)) for n in range(5)},
        "read_at": NOW - 5 * 3600, "online": 5, "rejected": 0}}})
    plugin._roll_day(NOW)
    summary = plugin._summary
    assert summary["tally"].active == 5
    # The week is today plus the six days before; the 30 days are everything.
    assert summary["week"] == [sum(range(1, 7)), 6, 12, 6]
    assert summary["month"] == [sum(range(1, 9)), 8, 16, 8]
    assert summary["active_avg"] == round(sum(range(11, 17)) / 6, 1)
    # A read adds today's row; rows past 30 days are dropped.
    plugin._save_json("history.json", {"days": dict(history, **{
        mod._utc(NOW - i * 86400)[0]: history[days[1]] for i in range(20, 45)})})
    again, _m, _n, clock2 = _worker(tmp_path, report=False)
    _run_for(again, clock2, 1800)
    assert today in again._history and len(again._history) == 30
    # Today's row (five devices, one remote win and one loss each) joins the week.
    assert again._summary["week"] == [21 + 5, 6, 12 + 5, 6 + 5]
    assert again.provide_census()["remote_win_rate_7d"] == 17 / 28


def test_corrupt_history_is_dropped_not_fatal(tmp_path):
    plugin, _mod, _net, clock = _worker(tmp_path, report=False)
    plugin._save_json("history.json", {"days": {"2027-01-14": {"active": "x"},
                                                 "2027-01-13": [1, 2]}})
    _run_for(plugin, clock, 1800)
    assert set(plugin._history) == {"2027-01-15"}


# -- step 12: the card and the page ---------------------------------------------------------

def _card(plugin):
    from kestrel_pi.ui import StubUI, UIState
    ui = StubUI()
    view = ui.for_plugin("census")
    plugin.on_ui_setup(view)
    plugin.on_ui_update(view, UIState())
    return {e["name"]: e for e in ui.widgets()["census"]}


def _page(plugin, path="index"):
    page = plugin.on_webhook(path, None)
    return page.body if hasattr(page, "body") else page


def _read_census(tmp_path, writers=2, contests=(2, 1, 3, 1), remote=None, **opts):
    """A reader that has read a census of ``writers`` devices."""
    net = FakeDHT(invalid=0.2, seed=44)
    service = {"configured": True, "stack": "ready", "last_contact": NOW - 60,
               "last_model": NOW - 60}
    for i in range(writers):
        writer, _m, _n, clock = _worker(tmp_path, net=net, seed=90 + i, name="w%d" % i,
                                        read=False)
        tally = {"local": {"won": 0, "lost": 0}, "remote": {"won": 0, "lost": 0}}
        writer.context.services.provide("agent", lambda t=tally: _status(tally=t), owner="daemon")
        writer.context.services.provide("remote-training", lambda: service, owner="remote-train")
        _run_for(writer, clock, 100)                 # joined; baseline at zero
        writer._facts(clock.now, writer._day)
        tally["local"].update(won=contests[0], lost=contests[1])
        tally["remote"].update(won=contests[2], lost=contests[3])
        _run_for(writer, clock, 3500)
    reader, mod, _n, clock = _worker(tmp_path, net=net, seed=99, name="r", report=False, **opts)
    _run_for(reader, clock, 3600)
    return reader, mod


def test_card_shows_the_fleet_and_the_contest_line(tmp_path):
    reader, _mod = _read_census(tmp_path)
    card = _card(reader)
    assert card["online"]["value"] == 2 and card["active"]["value"] == 2
    assert card["contests"]["value"] == "desktop won 6 of 8 training contests this week"


def test_card_contest_line_with_no_contests_and_without_data(tmp_path):
    reader, mod = _read_census(tmp_path, contests=(1, 0, 0, 0))
    assert _card(reader)["contests"]["value"] == "no training contests this week"
    assert mod.contest_line([0, 0, 1, 0]) == "desktop won 1 of 1 training contests this week"
    plugin, _mod = _plugin(tmp_path)
    assert {e["value"] for e in _card(plugin).values()} == {"-"}
    reader.options["read"] = False
    assert {e["value"] for e in _card(reader).values()} == {"-"}


def test_page_before_anything_is_read(tmp_path):
    plugin, _mod = _plugin(tmp_path)
    body = _page(plugin)
    assert "Approximate and unverified" in body and "Starting." in body
    assert "The census hasn't been read yet today." in body
    assert "What this device reports today" in body
    assert "Bluetooth tether only" in body and "no agent" in body
    assert plugin.on_webhook("nope", None).status == 404


def test_status_shows_the_tether_and_when_it_last_pushed_and_retrieved(tmp_path):
    plugin, _mod, _net, clock = _worker(tmp_path)
    body = html.unescape(_page(plugin))
    assert "Phone's Bluetooth tether: up." in body
    assert "Last pushed (this device's report): not yet." in body
    _run_for(plugin, clock, 3600)
    body = html.unescape(_page(plugin))
    assert "Can read and write the census now: yes." in body
    assert "storing nodes hold it" in body
    # Full UTC dates (NOW is 2027-01-15 08:00 UTC) with how long ago.
    assert "report): 2027-01-15 08:" in body and " min ago" in body
    assert "Last retrieved (the whole census): 2027-01-15 08:" in body


def test_status_times_survive_a_restart(tmp_path):
    plugin, _mod, _net, clock = _worker(tmp_path, name="keep")
    _run_for(plugin, clock, 3600)
    pushed, read = plugin._last_write["at"], plugin._last_read["at"]
    again, _m, _n, clock2 = _worker(tmp_path, name="keep")
    clock2.now = clock.now + 600
    again._roll_day(clock2.now)            # the worker's first tick after a restart
    assert again._last_write["at"] == pushed and again._last_write["held"]
    assert again._last_read["at"] == read
    body = html.unescape(_page(again))
    assert "Last pushed (this device's report): 2027-01-15 08:" in body and "storing nodes hold it" in body
    assert "Last retrieved (the whole census): 2027-01-15 08:" in body
    with open(os.path.join(again._state_dir(), "status.json")) as handle:
        assert set(json.load(handle)) == {"write", "read"}


def test_status_says_when_the_tether_is_down(tmp_path):
    plugin, _mod, _net, clock = _worker(tmp_path)
    plugin.context.connectivity.paths = []
    _run_for(plugin, clock, 120)
    body = html.unescape(_page(plugin))
    assert "Phone's Bluetooth tether: down." in body
    assert "Can read and write the census now: no, waiting for the phone's Bluetooth tether." in body
    plugin.options["tether_only"] = False
    assert "Connection: any (tether_only is off)." in html.unescape(_page(plugin))


def test_when_gives_the_date_time_and_how_long_ago(tmp_path):
    _plugin_, mod = _plugin(tmp_path)
    assert mod._when(NOW, NOW + 30) == "2027-01-15 08:00 UTC (just now)"
    assert mod._when(NOW, NOW + 9 * 60) == "2027-01-15 08:00 UTC (9 min ago)"
    assert mod._when(NOW, NOW + 3 * 3600 + 5 * 60) == "2027-01-15 08:00 UTC (3h 05m ago)"
    assert mod._when(NOW, NOW + 3 * 86400) == "2027-01-15 08:00 UTC (3 days ago)"
    assert mod._valid_last({"write": {"at": "x"}, "read": []}) == (None, None)


def test_page_starts_with_its_heading(tmp_path):
    # The page shell adds "← Plugins · Dashboard" above it.
    plugin, _mod = _plugin(tmp_path)
    body = _page(plugin)
    assert body.startswith("<h1>Census</h1>") and "&larr; Plugins" not in body


def test_page_with_a_census_read(tmp_path):
    reader, _mod = _read_census(tmp_path)
    body = _page(reader)
    assert "Running." in body and "Last retrieved (the whole census): 2027-01-15 " in body
    assert "Kestrels online: 2 &middot; active today: 2" in body
    assert "<p>1.0.12: 2 (100%)</p>" in body and "<p>learned: 2 (100%)</p>" in body
    assert "<p>Learned with PPO: 2 (100%)</p>" in body
    assert "<p>working: 2 (100%)</p>" in body
    assert ("<p>Today: on-device won 4 of 6 (67%), remote won 6 of 8 (75%)</p>") in body
    # Short counts stay short at phone width: no tables on this page.
    assert "<table" not in body
    assert "Last 30 days" in body
    # The device's own report: reporting is off on this reader, and says so.
    assert "Reporting is off, so this device isn" in body
    # No token or node address on the page.
    assert reader._day["token"] not in body
    assert not any(net in body for net in ("192.0.2.", "198.51.100.", "203.0.113."))


def test_page_says_why_it_is_paused_and_escapes_what_it_shows(tmp_path):
    plugin, _mod, _net, clock = _worker(tmp_path)
    plugin.context.connectivity.online = False
    _run_for(plugin, clock, 120)
    assert "Paused: offline." in _page(plugin)
    plugin._paused = ""
    plugin._link_error = "can't bind to <b>bnep0</b>"
    body = _page(plugin)
    assert "&lt;b&gt;bnep0&lt;/b&gt;" in body and "<b>bnep0" not in body


def test_hostile_state_files_never_reach_the_page(tmp_path):
    plugin, _mod, _net, clock = _worker(tmp_path, report=False)
    plugin._save_json("history.json", {"days": {"<script>x</script>": {
        "active": 1, "online": 1, "capped": 0, "contests": [0, 0, 0, 0],
        "working": [0, 0, 0, 0]}}})
    plugin._save_json("seen.json", {"days": {"<i>2027-01-15</i>": {
        "entries": {}, "read_at": 1, "online": 0, "rejected": 0}}})
    _run_for(plugin, clock, 60)
    body = _page(plugin)
    assert "script" not in body and "<i>" not in body


def test_worker_reads_after_its_first_write_so_it_sees_itself(tmp_path):
    plugin, _mod, net, clock = _worker(tmp_path)
    reads = []
    read = plugin._read

    def logged(now):
        reads.append(now)
        read(now)
    plugin._read = logged
    _run_for(plugin, clock, 3600)
    assert reads[0] >= plugin.writes[0] + 60
    # Its own entry is in the first read: one device online, one active.
    service = plugin.provide_census()
    assert service["online"] == 1 and service["active_today"] == 1
    # After a restart in an hour it has written, with no read saved, the read
    # comes on the usual schedule, not after the next hour's write.
    (tmp_path / "state-dev" / "seen.json").unlink()
    again, _m, _n, clock2 = _worker(tmp_path, net=net)
    clock2.now = clock.now
    again._roll_day(clock2.now)
    assert again._day["wrote"] is not None
    again._plan_write(clock2.now)
    again._plan_read(clock2.now)
    assert again._next_read <= clock2.now + 600 + 120
