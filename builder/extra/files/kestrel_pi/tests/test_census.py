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
