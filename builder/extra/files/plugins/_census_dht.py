"""census helper: a small, read-only BitTorrent DHT client.

Just enough of the Mainline DHT for the census: KRPC queries (BEP 5) in
read-only mode (BEP 43: it sends ``ro = 1`` and answers nothing), node IDs
checked against their addresses (BEP 42), and signed mutable items (BEP 44).
Every packet it accepts is a response to one of its own queries, from the
address it was sent to, and is decoded with hard size, depth and item caps.

Loaded by the ``census`` plugin with ``kestrel_pi.load_helper``; the desktop
census report loads it the same way. Stdlib + ``cryptography``.
"""

import collections
import errno
import hashlib
import ipaddress
import json
import os
import select
import socket
import time

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

# IPv4 + UDP headers, counted with every packet against the data budget.
UDP_IP_OVERHEAD = 28


# -- bencode -------------------------------------------------------------------

class BencodeError(ValueError):
    """A packet that isn't well-formed bencode within the caps."""


def bencode(obj):
    """Encode ``bytes``, ``int``, ``list`` and ``dict`` (with ``bytes`` keys,
    written in sorted order)."""
    if isinstance(obj, bool):
        raise TypeError("bencode has no booleans")
    if isinstance(obj, int):
        return b"i%de" % obj
    if isinstance(obj, (bytes, bytearray)):
        return b"%d:" % len(obj) + bytes(obj)
    if isinstance(obj, str):
        return bencode(obj.encode())
    if isinstance(obj, (list, tuple)):
        return b"l" + b"".join(bencode(item) for item in obj) + b"e"
    if isinstance(obj, dict):
        items = []
        for key in sorted(obj):
            if not isinstance(key, bytes):
                raise TypeError("bencode dict keys must be bytes")
            items.append(bencode(key) + bencode(obj[key]))
        return b"d" + b"".join(items) + b"e"
    raise TypeError("can't bencode %s" % type(obj).__name__)


def bdecode(data, max_len=2048, max_depth=4, max_items=64):
    """Decode one bencoded value that fills ``data`` exactly.

    Refuses anything over ``max_len`` bytes, nested deeper than ``max_depth``,
    or with more than ``max_items`` items in one list or dict; leading zeros,
    ``-0``, integers outside 64 bits, duplicate dict keys, strings running past
    the end and trailing bytes. Any failure is one ``BencodeError``."""
    if not isinstance(data, (bytes, bytearray)):
        raise BencodeError("not bytes")
    data = bytes(data)
    if len(data) > max_len:
        raise BencodeError("too long")
    value, end = _decode(data, 0, 0, max_depth, max_items)
    if end != len(data):
        raise BencodeError("trailing data")
    return value


_DIGITS = b"0123456789"
_DIGIT_BYTES = frozenset(bytes([c]) for c in _DIGITS)


def _decode(buf, i, depth, max_depth, max_items):
    if i >= len(buf):
        raise BencodeError("truncated")
    lead = buf[i:i + 1]
    if lead == b"i":
        end = buf.find(b"e", i + 1, i + 22)
        if end < 0:
            raise BencodeError("bad integer")
        text = buf[i + 1:end]
        digits = text[1:] if text[:1] == b"-" else text
        if (not digits or any(c not in _DIGITS for c in digits)
                or (digits[:1] == b"0" and len(digits) > 1) or text == b"-0"):
            raise BencodeError("bad integer")
        value = int(text)
        if not -(1 << 63) <= value < (1 << 63):
            raise BencodeError("integer out of range")
        return value, end + 1
    if lead in (b"l", b"d"):
        if depth >= max_depth:
            raise BencodeError("nested too deep")
        i += 1
        if lead == b"l":
            items = []
            while buf[i:i + 1] != b"e":
                if len(items) >= max_items:
                    raise BencodeError("too many items")
                item, i = _decode(buf, i, depth + 1, max_depth, max_items)
                items.append(item)
            return items, i + 1
        out = {}
        while buf[i:i + 1] != b"e":
            if len(out) >= max_items:
                raise BencodeError("too many items")
            if buf[i:i + 1] not in _DIGIT_BYTES:
                raise BencodeError("dict key isn't a string")
            key, i = _decode(buf, i, depth + 1, max_depth, max_items)
            if key in out:
                raise BencodeError("duplicate dict key")
            out[key], i = _decode(buf, i, depth + 1, max_depth, max_items)
        return out, i + 1
    if lead in _DIGIT_BYTES:
        colon = buf.find(b":", i, i + 8)
        if colon < 0:
            raise BencodeError("bad string length")
        text = buf[i:colon]
        if any(c not in _DIGITS for c in text) or (text[:1] == b"0" and len(text) > 1):
            raise BencodeError("bad string length")
        start = colon + 1
        end = start + int(text)
        if end > len(buf):
            raise BencodeError("string runs past the end")
        return buf[start:end], end
    raise BencodeError("unknown type")


# -- CRC32-C and BEP 42 ------------------------------------------------------------

def _crc_table():
    table = []
    for n in range(256):
        c = n
        for _ in range(8):
            c = (c >> 1) ^ 0x82F63B78 if c & 1 else c >> 1
        table.append(c)
    return table


_CRC32C = _crc_table()


def crc32c(data):
    """CRC32-C (Castagnoli), which BEP 42 uses; ``zlib.crc32`` is a different
    polynomial."""
    c = 0xFFFFFFFF
    for byte in data:
        c = _CRC32C[(c ^ byte) & 0xFF] ^ (c >> 8)
    return c ^ 0xFFFFFFFF


def ip_int(ip):
    """An IPv4 address as an integer; ValueError for anything else."""
    try:
        return int.from_bytes(socket.inet_aton(ip), "big")
    except (OSError, TypeError):
        raise ValueError("not an IPv4 address: %r" % (ip,)) from None


# BEP 42 doesn't apply to these, so a node there may have any ID.
_BEP42_EXEMPT = ((0x0A000000, 8), (0xAC100000, 12), (0xC0A80000, 16),
                 (0xA9FE0000, 16), (0x7F000000, 8))


def bep42_exempt(ip):
    value = ip_int(ip)
    return any(value >> (32 - bits) == net >> (32 - bits) for net, bits in _BEP42_EXEMPT)


def _bep42_crc(ip, rand):
    return crc32c(((ip_int(ip) & 0x030F3FFF) | ((rand & 7) << 29)).to_bytes(4, "big"))


def bep42_valid(node_id, ip):
    """Whether a node ID belongs to the IPv4 address it answered from: its
    first 21 bits come from a CRC32-C of the masked address and the 3 random
    bits carried in its last byte (BEP 42)."""
    if not isinstance(node_id, bytes) or len(node_id) != 20:
        return False
    try:
        if bep42_exempt(ip):
            return True
        crc = _bep42_crc(ip, node_id[19])
    except ValueError:
        return False
    return (node_id[0] == (crc >> 24) & 0xFF and node_id[1] == (crc >> 16) & 0xFF
            and (node_id[2] & 0xF8) == ((crc >> 8) & 0xF8))


def bep42_make_id(ip, rng):
    """A BEP 42-valid node ID for ``ip``, with the rest random. For tests."""
    rand = rng.randrange(256)
    crc = _bep42_crc(ip, rand)
    head = bytes([(crc >> 24) & 0xFF, (crc >> 16) & 0xFF,
                  ((crc >> 8) & 0xF8) | rng.randrange(8)])
    return head + bytes(rng.randrange(256) for _ in range(16)) + bytes([rand])


# -- transport -------------------------------------------------------------------

# A send or receive failing with one of these means the interface the socket is
# bound to has gone (a phone tether that dropped).
_LINK_ERRNOS = frozenset({errno.ENODEV, errno.ENETDOWN, errno.ENETUNREACH,
                          errno.EADDRNOTAVAIL})


class TetherBindError(OSError):
    """The socket couldn't be bound to the requested interface."""


class LinkDown(OSError):
    """The interface the socket is bound to went away mid-cycle."""


class UdpTransport:
    """A non-blocking UDP socket on an ephemeral port, optionally bound to one
    interface (``SO_BINDTODEVICE``, allowed for an unprivileged user since
    Linux 5.7), so that its packets leave only by that interface."""

    def __init__(self, bind_device=None):
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        if bind_device:
            try:
                sock.setsockopt(socket.SOL_SOCKET,
                                getattr(socket, "SO_BINDTODEVICE", 25),
                                bind_device.encode() + b"\0")
            except OSError as exc:
                sock.close()
                raise TetherBindError(exc.errno, "can't bind to %s: %s"
                                      % (bind_device, exc.strerror)) from None
        sock.bind(("0.0.0.0", 0))
        sock.setblocking(False)
        self._sock = sock

    def send(self, data, addr):
        """True when the packet went out; LinkDown when the interface is gone."""
        try:
            self._sock.sendto(data, addr)
            return True
        except OSError as exc:
            if exc.errno in _LINK_ERRNOS:
                raise LinkDown(exc.errno, exc.strerror) from None
            return False

    def recv(self, timeout):
        """``(data, (ip, port))``, or None once ``timeout`` seconds pass with
        nothing to read."""
        deadline = time.monotonic() + max(0.0, timeout)
        while True:
            left = deadline - time.monotonic()
            if left <= 0:
                return None
            try:
                ready, _w, _x = select.select([self._sock], [], [], left)
            except InterruptedError:
                continue
            if not ready:
                return None
            try:
                data, addr = self._sock.recvfrom(4096)
            except (BlockingIOError, InterruptedError):
                continue
            except OSError as exc:
                if exc.errno in _LINK_ERRNOS:
                    raise LinkDown(exc.errno, exc.strerror) from None
                continue
            return data, (addr[0], addr[1])

    def close(self):
        self._sock.close()


# -- KRPC ------------------------------------------------------------------------

class KrpcClient:
    """Sends KRPC queries and matches their replies.

    Every query carries ``ro = 1`` (BEP 43: don't add this node to routing
    tables) and no client version. A packet is used only when it is a reply
    (``y`` is ``r`` or ``e``) to a query still outstanding, from the exact
    address that query went to; everything else is dropped and counted. At most
    ``max_in_flight`` queries are outstanding at once, and at most ``rate``
    are sent per second. Bytes sent and received (with IP and UDP headers) are
    counted for the data budget."""

    def __init__(self, transport, node_id=None, clock=time.monotonic,
                 sleep=time.sleep, timeout=2.0, max_in_flight=8, rate=10.0):
        self.transport = transport
        self.node_id = node_id or os.urandom(20)
        self.clock = clock
        self.sleep = sleep
        self.timeout = timeout
        self.max_in_flight = max_in_flight
        self.rate = float(rate)
        self.sent_bytes = 0
        self.received_bytes = 0
        self.queries = 0
        self.dropped = 0
        self._allowance = self.rate
        self._last = clock()

    def query(self, requests):
        """Send each ``(addr, method, args)`` and wait for the replies. Returns,
        in order, ``(reply_dict, None)`` or ``(None, reason)`` with reason
        ``"timeout"``, ``"unsent"``, ``"malformed"`` or ``"e<code>"`` for a
        KRPC error reply. Raises LinkDown when the interface goes away."""
        results = []
        for start in range(0, len(requests), self.max_in_flight):
            results.extend(self._batch(requests[start:start + self.max_in_flight]))
        return results

    def _batch(self, requests):
        results = [(None, "timeout")] * len(requests)
        pending = {}
        for idx, (addr, method, args) in enumerate(requests):
            self._throttle()
            tid = os.urandom(2)
            while tid in pending:
                tid = os.urandom(2)
            body = dict(args)
            body[b"id"] = self.node_id
            packet = bencode({b"t": tid, b"y": b"q", b"q": method, b"a": body, b"ro": 1})
            if not self.transport.send(packet, addr):
                results[idx] = (None, "unsent")
                continue
            self.sent_bytes += len(packet) + UDP_IP_OVERHEAD
            self.queries += 1
            pending[tid] = (idx, (addr[0], addr[1]))
        deadline = self.clock() + self.timeout
        while pending:
            left = deadline - self.clock()
            if left <= 0:
                break
            got = self.transport.recv(left)
            if got is None:
                break
            data, src = got
            self.received_bytes += len(data) + UDP_IP_OVERHEAD
            try:
                msg = bdecode(data)
            except BencodeError:
                msg = None
            kind = msg.get(b"y") if isinstance(msg, dict) else None
            entry = pending.get(msg.get(b"t")) if kind in (b"r", b"e") else None
            if entry is None or entry[1] != (src[0], src[1]):
                self.dropped += 1
                continue
            del pending[msg[b"t"]]
            idx = entry[0]
            if kind == b"r" and isinstance(msg.get(b"r"), dict):
                results[idx] = (msg[b"r"], None)
            elif kind == b"e":
                err = msg.get(b"e")
                code = err[0] if isinstance(err, list) and err and isinstance(err[0], int) else 0
                results[idx] = (None, "e%d" % code)
            else:
                results[idx] = (None, "malformed")
        return results

    def _throttle(self):
        """A token bucket: ``rate`` sends a second, bursting up to ``rate``."""
        now = self.clock()
        self._allowance = min(self.rate, self._allowance + (now - self._last) * self.rate)
        self._last = now
        if self._allowance < 1.0:
            self.sleep((1.0 - self._allowance) / self.rate)
            now = self.clock()
            self._allowance = min(self.rate, self._allowance + (now - self._last) * self.rate)
            self._last = now
        self._allowance -= 1.0


# -- nodes -------------------------------------------------------------------------

def public_ipv4(ip):
    """Whether ``ip`` is a public unicast IPv4 address. Nodes advertised at any
    other address (private, loopback, link-local, multicast, reserved) are
    never kept, so a reply can't point the device at hosts on its own network."""
    try:
        addr = ipaddress.IPv4Address(ip)
    except ValueError:
        return False
    return addr.is_global and not addr.is_multicast


def parse_nodes(compact):
    """``[(node_id, ip, port)]`` from BEP 5 compact node info (26 bytes each)."""
    out = []
    if not isinstance(compact, bytes):
        return out
    for i in range(0, len(compact) - len(compact) % 26, 26):
        port = int.from_bytes(compact[i + 24:i + 26], "big")
        if port:
            out.append((compact[i:i + 20], socket.inet_ntoa(compact[i + 20:i + 24]), port))
    return out


def compact_node(node_id, ip, port):
    return node_id + socket.inet_aton(ip) + port.to_bytes(2, "big")


def distance(a, b):
    return int.from_bytes(a, "big") ^ int.from_bytes(b, "big")


class NodePool:
    """The DHT nodes this device knows: ``{node_id: [ip, port, bep42, last_ok]}``,
    with ``last_ok`` the time it last answered (0 if never). Over ``capacity``,
    nodes that never answered go first, then the longest silent."""

    def __init__(self, capacity=300, accept=public_ipv4, clock=time.time):
        self.capacity = capacity
        self.accept = accept
        self.clock = clock
        self.nodes = {}

    def add(self, node_id, ip, port, ok=False):
        if (not isinstance(node_id, bytes) or len(node_id) != 20
                or not 0 < port < 65536 or not self.accept(ip)):
            return
        entry = self.nodes.get(node_id)
        if entry is None:
            self.nodes[node_id] = [ip, port, bep42_valid(node_id, ip),
                                   self.clock() if ok else 0.0]
        elif ok:
            # Only the node itself, answering, moves it to a new address.
            entry[0], entry[1], entry[2] = ip, port, bep42_valid(node_id, ip)
            entry[3] = self.clock()
        if len(self.nodes) > self.capacity:
            self._evict()

    def forget(self, node_id):
        self.nodes.pop(node_id, None)

    def _evict(self):
        extra = len(self.nodes) - self.capacity
        for node_id in sorted(self.nodes, key=lambda n: self.nodes[n][3])[:extra]:
            del self.nodes[node_id]

    def closest(self, target, count, only_bep42=False):
        """``[(node_id, (ip, port))]``: the ``count`` known nodes nearest
        ``target``, BEP 42-valid ones only if asked."""
        items = [(n, e) for n, e in self.nodes.items() if e[2] or not only_bep42]
        items.sort(key=lambda item: distance(item[0], target))
        return [(n, (e[0], e[1])) for n, e in items[:count]]

    def responsive(self):
        return sum(1 for e in self.nodes.values() if e[3])

    def save(self, path):
        """Write the pool atomically, readable by its owner only."""
        rows = [[n.hex(), e[0], e[1], e[3]] for n, e in self.nodes.items()]
        tmp = path + ".tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump({"nodes": rows}, handle)
        os.replace(tmp, path)

    def load(self, path):
        """Add the nodes saved at ``path``; a missing or malformed file adds
        nothing."""
        try:
            with open(path, "r", encoding="utf-8") as handle:
                rows = json.load(handle).get("nodes")
        except (OSError, ValueError, AttributeError):
            return
        for row in rows if isinstance(rows, list) else []:
            try:
                node_id, ip, port, last_ok = bytes.fromhex(row[0]), row[1], int(row[2]), float(row[3])
            except (TypeError, ValueError, IndexError):
                continue
            if isinstance(ip, str):
                self.add(node_id, ip, port)
                if node_id in self.nodes:
                    self.nodes[node_id][3] = max(0.0, last_ok)


# Bootstrap rounds, in seconds from the first: a phone hotspot waking from idle
# can drop the first burst of packets.
BOOTSTRAP_BACKOFF = (0, 10, 30, 90)


def _resolve(host, port):
    try:
        return socket.getaddrinfo(host, port, socket.AF_INET, socket.SOCK_DGRAM)[0][4][:2]
    except (OSError, IndexError, UnicodeError):
        return None


def bootstrap(client, pool, routers, resolve=_resolve, wait=None,
              backoff=BOOTSTRAP_BACKOFF):
    """Ask the bootstrap ``routers`` (``[(host, port)]``) for nodes near this
    client's ID, in rounds at the ``backoff`` offsets, until one answers. Names
    are resolved again each round. ``wait(seconds)`` sleeps between rounds and
    returns True to give up early (the plugin is stopping). Returns how many
    routers answered in the round that worked, 0 if none did."""
    wait = wait or (lambda seconds: time.sleep(seconds) or False)
    elapsed = 0
    for at in backoff:
        if at > elapsed:
            if wait(at - elapsed):
                return 0
            elapsed = at
        addrs = [addr for addr in (resolve(host, port) for host, port in routers) if addr]
        if not addrs:
            continue
        replies = client.query([(addr, b"find_node", {b"target": client.node_id})
                                for addr in addrs])
        answered = 0
        for reply, _err in replies:
            if reply:
                answered += 1
                for node_id, ip, port in parse_nodes(reply.get(b"nodes")):
                    pool.add(node_id, ip, port)
        if answered:
            return answered
    return 0


# -- lookups -----------------------------------------------------------------------

def lookup(client, pool, target, query=b"get", only_bep42=True, k=8, alpha=3,
           max_queries=40):
    """An iterative lookup toward ``target``: ask the nearest known nodes,
    learn nearer ones from their replies, stop when the nearest ``k`` have
    answered or after ``max_queries``. Census lookups use ``get``, whose
    replies carry the write tokens a later ``put`` needs.

    With ``only_bep42``, a node whose ID doesn't match its address (BEP 42) is
    never sent the query and never returned, whether it came from the pool or
    from a reply. Nodes that don't answer are dropped from the pool.

    Returns ``([(node_id, addr, reply)], stats)``: the nearest ``k``
    responders, nearest first, and ``{"queries", "non_bep42_queried"}``."""
    candidates = dict(pool.closest(target, 32, only_bep42))
    queried, answered = set(), {}
    stats = {"queries": 0, "non_bep42_queried": 0}
    args = {b"target": target}
    while stats["queries"] < max_queries:
        order = sorted(candidates, key=lambda n: distance(n, target))
        if all(n in queried for n in order[:k]):
            break
        batch = [n for n in order if n not in queried][:min(alpha, max_queries - stats["queries"])]
        if not batch:
            break
        for node_id in batch:
            queried.add(node_id)
            if not bep42_valid(node_id, candidates[node_id][0]):
                stats["non_bep42_queried"] += 1
        stats["queries"] += len(batch)
        replies = client.query([(candidates[n], query, args) for n in batch])
        for node_id, (reply, _err) in zip(batch, replies):
            addr = candidates[node_id]
            reply_id = reply.get(b"id") if reply else None
            if not isinstance(reply_id, bytes) or len(reply_id) != 20:
                candidates.pop(node_id, None)
                pool.forget(node_id)
                continue
            pool.add(reply_id, addr[0], addr[1], ok=True)
            if only_bep42 and not bep42_valid(reply_id, addr[0]):
                candidates.pop(node_id, None)
                continue
            if reply_id != node_id:
                candidates.pop(node_id, None)
                candidates[reply_id] = addr
                queried.add(reply_id)
            answered[reply_id] = (addr, reply)
            for found_id, ip, port in parse_nodes(reply.get(b"nodes")):
                if found_id == client.node_id or not pool.accept(ip):
                    continue
                pool.add(found_id, ip, port)
                if only_bep42 and not bep42_valid(found_id, ip):
                    continue
                if found_id not in candidates:
                    candidates[found_id] = (ip, port)
    nearest = sorted(answered, key=lambda n: distance(n, target))[:k]
    return [(n, answered[n][0], answered[n][1]) for n in nearest], stats


def refresh(client, pool, rng):
    """Keep the pool fresh: one ``find_node`` lookup toward a random target.
    It isn't about any census slot, so any node may serve it."""
    target = bytes(rng.randrange(256) for _ in range(20))
    lookup(client, pool, target, query=b"find_node", only_bep42=False, max_queries=24)


class SlotCache:
    """The storing nodes found for each slot today, so that later reads and
    writes go straight to them instead of looking the slot up again."""

    def __init__(self, data=None):
        self._nodes = {}
        for target, addrs in (data or {}).items():
            if isinstance(target, str) and isinstance(addrs, list):
                good = [(a[0], int(a[1])) for a in addrs
                        if isinstance(a, list) and len(a) == 2 and isinstance(a[0], str)
                        and isinstance(a[1], int) and 0 < a[1] < 65536]
                if good:
                    self._nodes[target] = good

    def get(self, target):
        return list(self._nodes.get(target.hex(), ()))

    def set(self, target, addrs):
        self._nodes[target.hex()] = [(a[0], a[1]) for a in addrs]

    def drop(self, target):
        self._nodes.pop(target.hex(), None)

    def to_json(self):
        return {t: [[ip, port] for ip, port in addrs] for t, addrs in self._nodes.items()}


# -- BEP 44 mutable items ------------------------------------------------------------

# A BEP 44 value is at most 1000 bytes, bencoded.
MAX_VALUE = 1000


class SlotFrozen(Exception):
    """The item's stored sequence number is above the writer's ceiling, so the
    writer can't replace it; it moves to another key."""


def item_target(public_key):
    """The DHT target of a mutable item with no salt: SHA-1 of its key."""
    return hashlib.sha1(public_key).digest()


def signed_payload(seq, value):
    return b"3:seqi%de1:v" % seq + bencode(value)


def verify_item(public_key, seq, value, sig):
    try:
        Ed25519PublicKey.from_public_bytes(public_key).verify(sig, signed_payload(seq, value))
        return True
    except (InvalidSignature, ValueError, TypeError):
        return False


class Fetched:
    """What a ``get`` of one item found: every valid value (one per node that
    holds one), the highest valid ``seq`` (-1 for none), and the nodes that can
    store it, with their write tokens."""

    def __init__(self, target):
        self.target = target
        self.values = []
        self.max_seq = -1
        self.storing = []          # [(node_id, addr, token)]
        self.answered = 0
        self.rejected = 0

    @property
    def holders(self):
        return len(self.values)


def get_item(client, pool, cache, public_key, only_bep42=True, min_cached=4):
    """``get`` the mutable item for ``public_key``: straight from the storing
    nodes cached for it today while at least ``min_cached`` of them answer,
    otherwise by a lookup (whose nodes are then cached). A value counts only
    when its key matches, its signature verifies and it is within BEP 44's
    size limit."""
    target = item_target(public_key)
    replies = []
    cached = cache.get(target) if cache is not None else []
    if len(cached) >= min_cached:
        out = client.query([(addr, b"get", {b"target": target}) for addr in cached])
        for addr, (reply, _err) in zip(cached, out):
            node_id = reply.get(b"id") if reply else None
            if (isinstance(node_id, bytes) and len(node_id) == 20
                    and (bep42_valid(node_id, addr[0]) or not only_bep42)):
                replies.append((node_id, addr, reply))
    if len(replies) < min_cached:
        replies, _stats = lookup(client, pool, target, b"get", only_bep42)
        if cache is not None:
            if replies:
                cache.set(target, [addr for _n, addr, _r in replies])
            else:
                cache.drop(target)
    fetched = Fetched(target)
    for node_id, addr, reply in replies:
        fetched.answered += 1
        token = reply.get(b"token")
        if isinstance(token, bytes) and 0 < len(token) <= 64:
            fetched.storing.append((node_id, addr, token))
        value, key, seq, sig = (reply.get(b"v"), reply.get(b"k"),
                                reply.get(b"seq"), reply.get(b"sig"))
        if value is None:
            continue
        if (key == public_key and isinstance(seq, int) and seq >= 0
                and isinstance(sig, bytes) and len(sig) == 64
                and len(bencode(value)) <= MAX_VALUE
                and verify_item(public_key, seq, value, sig)):
            fetched.values.append(value)
            fetched.max_seq = max(fetched.max_seq, seq)
        else:
            fetched.rejected += 1
    return fetched


def put_item(client, fetched, private_key, public_key, value, seq, ceiling=None):
    """``put`` ``value`` at ``seq``, signed, to the nodes ``fetched`` found.
    Raises SlotFrozen when the stored ``seq`` is above ``ceiling``. Returns
    ``(confirms, errors)``, errors a Counter of the failures' reasons (``e302``
    means the node holds a higher or equal ``seq``)."""
    if ceiling is not None and fetched.max_seq > ceiling:
        raise SlotFrozen(fetched.max_seq)
    if len(bencode(value)) > MAX_VALUE:
        raise ValueError("value too big for one item")
    sig = private_key.sign(signed_payload(seq, value))
    requests = [(addr, b"put", {b"token": token, b"k": public_key, b"seq": seq,
                                b"sig": sig, b"v": value})
                for _n, addr, token in fetched.storing]
    out = client.query(requests)
    confirms = sum(1 for reply, _err in out if reply is not None)
    errors = collections.Counter(err for reply, err in out if reply is None)
    return confirms, errors
