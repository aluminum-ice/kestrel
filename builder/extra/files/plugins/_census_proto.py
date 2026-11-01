"""census helper: the ``kestrel-census/1`` format.

Each UTC day has 64 slots, each a BEP 44 mutable item whose signing key comes
from a public string (``kestrel-census/1|<day>|<slot>``), so every device, and
anyone else, can read and write every slot. A slot's value is a run of 20-byte
entries, one per device that wrote in the last two hours:

    token(8) | hour(1) | version(2) | agent(1) | contests(4) | pow_nonce(4)

A writer reads the slot, keeps the entries from this hour and the last, puts
its own in, and writes the union back. A slot that is full, or that can't be
written, sends the writer to the slot's next alternate (three per slot).
docs/developer/census-protocol.md describes the format in full.

Loaded by the ``census`` plugin with ``kestrel_pi.load_helper``, after its
``_census_dht`` helper.
"""

import hashlib
import re
import struct
import time
from collections import Counter

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

PROTOCOL = "kestrel-census/1"
SLOTS = 64
ALTERNATES = 3
ENTRY_SIZE = 20
MAX_ENTRIES = 49            # 49 x 20 bytes fit one BEP 44 value
# The most entries a well-formed census can hold in a day: every slot and
# alternate full. A reader never keeps more than this for one day.
DAY_ENTRIES = SLOTS * MAX_ENTRIES * (ALTERNATES + 1)
POW_BITS = 20
POW_TAG = b"kc1|"
# Writers use seq = minutes since UTC midnight x 100 + 0-99, so a slot whose
# stored seq is above this can't be written: the writer moves on.
SEQ_CEILING = 1_000_000

# The agent byte: state(2) | algorithm(3) | remote(3).
AGENT_NONE, AGENT_STOPPED, AGENT_SWEEP, AGENT_LEARNED = 0, 1, 2, 3
AGENT_STATES = {AGENT_NONE: "no agent", AGENT_STOPPED: "not running",
                AGENT_SWEEP: "scripted sweep", AGENT_LEARNED: "learned"}
ALGORITHMS = {"RecurrentPPO": 1, "PPO": 2, "DQN": 3, "QR-DQN": 4}
ALGORITHM_NAMES = {code: name for name, code in ALGORITHMS.items()}
REMOTE_OFF, REMOTE_UNCONFIGURED, REMOTE_FAILING, REMOTE_IDLE, REMOTE_WORKING = range(5)
REMOTE_STATES = {REMOTE_OFF: "not enabled",
                 REMOTE_UNCONFIGURED: "enabled, not configured",
                 REMOTE_FAILING: "configured, failing",
                 REMOTE_IDLE: "configured, no new model",
                 REMOTE_WORKING: "working"}


# Readers drop an entry's contests when a day's count is above these: the agent
# runs at most one on-device contest every ~30 minutes, and one per remote model.
CAP_LOCAL = 100
CAP_REMOTE = 50


# -- slots ---------------------------------------------------------------------------

def slot_key(day, index, alt=0):
    """``(private_key, public_key, target)`` for slot ``index`` (or one of its
    alternates) on ``day`` (``YYYY-MM-DD``, UTC)."""
    text = "%s|%s|%d" % (PROTOCOL, day, index) + ("|a%d" % alt if alt else "")
    private = Ed25519PrivateKey.from_private_bytes(hashlib.sha256(text.encode()).digest())
    public = private.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    return private, public, hashlib.sha1(public).digest()


# -- the version and agent fields --------------------------------------------------------

_VERSION_RE = re.compile(r"(\d+)\.(\d+)\.(\d+)(?:-beta\.(\d+))?")


def encode_version(text):
    """``major(3) | minor(4) | patch(6) | beta(3)``: beta 0 for a stable
    release, 1-6 for ``-beta.1``-``-beta.6``, 7 for ``-beta.7`` or later. 0 for
    anything else (a dev build, or a part out of range)."""
    match = _VERSION_RE.fullmatch((text or "").strip())
    if not match:
        return 0
    major, minor, patch = (int(match.group(n)) for n in (1, 2, 3))
    beta = int(match.group(4)) if match.group(4) else 0
    if major > 7 or minor > 15 or patch > 63 or (match.group(4) and beta < 1):
        return 0
    return (major << 13) | (minor << 9) | (patch << 3) | min(beta, 7)


def decode_version(value):
    """The version as text (``1.0.12``, ``1.0.12-beta.3``, ``1.0.12-beta.7+``),
    or ``unknown`` for 0."""
    if not value:
        return "unknown"
    beta = value & 7
    text = "%d.%d.%d" % (value >> 13, (value >> 9) & 15, (value >> 3) & 63)
    return text + ("-beta.%d%s" % (beta, "+" if beta == 7 else "") if beta else "")


def version_sort_key(value):
    """Newest first when sorted ascending; ``unknown`` last; a stable release
    after its betas."""
    if not value:
        return (1, 0)
    base, beta = value >> 3, value & 7
    return (0, -base, 0 if beta == 0 else 1, -beta)


def encode_agent(state, algorithm, remote):
    return ((state & 3) << 6) | ((algorithm & 7) << 3) | (remote & 7)


def decode_agent(byte):
    """``(state, algorithm, remote)``."""
    return byte >> 6, (byte >> 3) & 7, byte & 7


# -- entries --------------------------------------------------------------------------

_ENTRY = struct.Struct("!8sBHB4s4s")


class Entry:
    """One device's report for the day."""

    __slots__ = ("token", "hour", "version", "agent", "contests", "nonce")

    def __init__(self, token, hour, version, agent, contests, nonce):
        self.token = token
        self.hour = hour
        self.version = version
        self.agent = agent
        self.contests = tuple(contests)
        self.nonce = nonce

    def encode(self):
        return _ENTRY.pack(self.token, self.hour, self.version, self.agent,
                           bytes(min(255, max(0, int(c))) for c in self.contests),
                           self.nonce)

    @classmethod
    def decode(cls, data):
        token, hour, version, agent, contests, nonce = _ENTRY.unpack(data)
        return cls(token, hour, version, agent, tuple(contests), nonce)

    def __eq__(self, other):
        return isinstance(other, Entry) and self.encode() == other.encode()

    def __repr__(self):
        return "Entry(%s, h%d, %s)" % (self.token.hex(), self.hour,
                                        decode_version(self.version))


# -- proof of work ----------------------------------------------------------------------

def pow_ok(day, token, nonce, bits=POW_BITS):
    digest = hashlib.sha256(POW_TAG + day.encode() + token + nonce).digest()
    return int.from_bytes(digest, "big") >> (256 - bits) == 0


def find_pow(day, token, rng, bits=POW_BITS, batch=50000, should_stop=None):
    """A 4-byte nonce that makes ``token`` valid for ``day``, searched from a
    random start in batches, yielding the CPU between them so the daemon's
    other threads keep running. None if ``should_stop()`` says to give up."""
    start = rng.randrange(1 << 32)
    for offset in range(0, 1 << 32, batch):
        for i in range(offset, min(offset + batch, 1 << 32)):
            nonce = ((start + i) & 0xFFFFFFFF).to_bytes(4, "big")
            if pow_ok(day, token, nonce, bits):
                return nonce
        if should_stop is not None and should_stop():
            return None
        time.sleep(0)
    return None


# -- reading and merging slot values --------------------------------------------------------

def parse_value(value, day, bits=POW_BITS):
    """``({token: Entry}, rejected)`` from one slot value: the newest entry per
    token, skipping entries with a bad hour or proof of work, and counting a
    value that isn't a whole number of entries as rejected."""
    entries, rejected = {}, 0
    if not isinstance(value, bytes):
        return entries, 1
    if len(value) % ENTRY_SIZE:
        rejected += 1
    for i in range(0, len(value) - len(value) % ENTRY_SIZE, ENTRY_SIZE):
        entry = Entry.decode(value[i:i + ENTRY_SIZE])
        if entry.hour > 23 or not pow_ok(day, entry.token, entry.nonce, bits):
            rejected += 1
            continue
        held = entries.get(entry.token)
        if held is None or entry.hour >= held.hour:
            entries[entry.token] = entry
    return entries, rejected


def union(values, day, bits=POW_BITS, limit=MAX_ENTRIES):
    """``({token: Entry}, rejected)`` across every node's copy of a slot.

    One slot holds at most ``MAX_ENTRIES``, so more than ``limit`` can only come
    from copies that disagree: then the entries held by the most copies are
    kept (the later hour, then the token, breaking ties), and the rest count as
    rejected. That bounds what any one read can add."""
    entries, copies, rejected = {}, {}, 0
    for value in values:
        found, bad = parse_value(value, day, bits)
        rejected += bad
        for token, entry in found.items():
            copies[token] = copies.get(token, 0) + 1
            held = entries.get(token)
            if held is None or entry.hour >= held.hour:
                entries[token] = entry
    if len(entries) > limit:
        keep = sorted(entries, key=lambda t: (-copies[t], -entries[t].hour, t))[:limit]
        rejected += len(entries) - limit
        entries = {t: entries[t] for t in keep}
    return entries, rejected


def merge(values, own, day, now_hour, bits=POW_BITS):
    """The value a writer puts back: the union of every copy, keeping entries
    from this hour and the last, with ``own`` in place of any older entry of
    its token. ``(value, count)``, or ``(None, count)`` when the slot is full."""
    entries, _rejected = union(values, day, bits)
    entries = {t: e for t, e in entries.items() if e.hour in (now_hour, now_hour - 1)}
    entries[own.token] = own
    if len(entries) > MAX_ENTRIES:
        return None, len(entries)
    return b"".join(entries[t].encode() for t in sorted(entries)), len(entries)


def new_seq(minute_of_day, max_seen, rng):
    """``minutes since UTC midnight x 100 + 0-99``, above the highest ``seq``
    seen. The random part makes two writers in the same minute unlikely to
    pick the same number."""
    return max(max_seen + 1, minute_of_day * 100 + rng.randrange(100))


# -- writing and reading slots ------------------------------------------------------------------

class Published:
    """How a write went: the alternate used (None when no alternate could be
    written), confirmations, entries in the value, the slots skipped and why."""

    def __init__(self):
        self.alt = None
        self.confirms = 0
        self.count = 0
        self.skipped = []          # [(alt, "full" | "frozen" | "no nodes")]
        self.retried = False


def publish(dht, client, pool, cache, day, index, own, minute_of_day, rng,
            bits=POW_BITS, start_alt=0):
    """Write ``own`` into slot ``index``: read it, merge, put the union back.
    A slot that is full or can't be written moves the write to the next
    alternate. A put refused because a node holds a newer ``seq`` is retried
    once, after reading the slot again. ``dht`` is the ``_census_dht`` helper
    (passed in, so the desktop report can load both helpers by path)."""
    result = Published()
    now_hour = minute_of_day // 60
    for alt in range(start_alt, ALTERNATES + 1):
        private, public, _target = slot_key(day, index, alt)
        fetched = dht.get_item(client, pool, cache, public)
        if not fetched.storing:
            result.skipped.append((alt, "no nodes"))
            continue
        if fetched.max_seq > SEQ_CEILING:
            result.skipped.append((alt, "frozen"))
            continue
        value, count = merge(fetched.values, own, day, now_hour, bits)
        if value is None:
            result.skipped.append((alt, "full"))
            continue
        seq = new_seq(minute_of_day, fetched.max_seq, rng)
        confirms, errors = dht.put_item(client, fetched, private, public, value, seq,
                                        ceiling=SEQ_CEILING)
        if errors.get("e302") and not result.retried:
            result.retried = True
            fetched = dht.get_item(client, pool, cache, public)
            if fetched.max_seq > SEQ_CEILING:
                result.skipped.append((alt, "frozen"))
                continue
            value, count = merge(fetched.values, own, day, now_hour, bits)
            if value is None:
                result.skipped.append((alt, "full"))
                continue
            more, _errors = dht.put_item(client, fetched, private, public, value,
                                         new_seq(minute_of_day, fetched.max_seq, rng),
                                         ceiling=SEQ_CEILING)
            confirms += more
        result.alt, result.confirms, result.count = alt, confirms, count
        return result
    return result


def holds_entry(dht, client, pool, cache, day, index, alt, token, bits=POW_BITS):
    """``(holders, with_token)``: how many nodes hold the slot, and how many of
    those copies carry ``token``'s entry (the check after a write)."""
    _private, public, _target = slot_key(day, index, alt)
    fetched = dht.get_item(client, pool, cache, public)
    mine = sum(1 for value in fetched.values if token in parse_value(value, day, bits)[0])
    return fetched.holders, mine


def read_slot(dht, client, pool, cache, day, index, bits=POW_BITS):
    """``({token: Entry}, rejected)`` for slot ``index`` on ``day``, reading its
    alternates only while the slot read is full or can't be written."""
    entries, rejected = {}, 0
    for alt in range(ALTERNATES + 1):
        _private, public, _target = slot_key(day, index, alt)
        fetched = dht.get_item(client, pool, cache, public)
        found, bad = union(fetched.values, day, bits)
        rejected += bad + fetched.rejected
        for token, entry in found.items():
            held = entries.get(token)
            if held is None or entry.hour >= held.hour:
                entries[token] = entry
        if fetched.max_seq <= SEQ_CEILING and len(found) < MAX_ENTRIES:
            break
    return entries, rejected


# -- counting ----------------------------------------------------------------------------

def win_rate(won, lost):
    """``won / (won + lost)``, or None when nothing was judged."""
    judged = won + lost
    return won / judged if judged else None


class Tally:
    """Counts over a day's entries, each a row ``(hour, version, agent, local
    won, local lost, remote won, remote lost)``: devices active, and online
    (wrote in ``hour`` or the hour before) when ``hour`` is given; devices by
    version, agent state, algorithm (learned policies only) and remote-training
    state; and contest totals, for all devices and for those whose remote
    training works. An entry with more contests than a day can hold has its
    contests left out, and counted in ``capped``."""

    def __init__(self, rows, hour=None):
        self.active = self.online = self.capped = 0
        self.versions, self.states = Counter(), Counter()
        self.algorithms, self.remotes = Counter(), Counter()
        self.contests, self.working = [0, 0, 0, 0], [0, 0, 0, 0]
        for row in rows:
            entry_hour, version, agent = row[0], row[1], row[2]
            counts = list(row[3:7])
            self.active += 1
            if hour is not None and entry_hour in (hour, hour - 1):
                self.online += 1
            state, algorithm, remote = decode_agent(agent)
            self.versions[version] += 1
            self.states[state] += 1
            self.remotes[remote] += 1
            if state == AGENT_LEARNED:
                self.algorithms[algorithm] += 1
            if counts[0] + counts[1] > CAP_LOCAL or counts[2] + counts[3] > CAP_REMOTE:
                self.capped += 1
                continue
            self.contests = [a + b for a, b in zip(self.contests, counts)]
            if remote == REMOTE_WORKING:
                self.working = [a + b for a, b in zip(self.working, counts)]

    def version_rows(self):
        """``[(version text, devices)]``, newest release first, unknown last."""
        return [(decode_version(code), self.versions[code])
                for code in sorted(self.versions, key=version_sort_key)]
