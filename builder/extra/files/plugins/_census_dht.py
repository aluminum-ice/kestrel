"""census helper: a small, read-only BitTorrent DHT client.

Just enough of the Mainline DHT for the census: KRPC queries (BEP 5) in
read-only mode (BEP 43: it sends ``ro = 1`` and answers nothing), node IDs
checked against their addresses (BEP 42), and signed mutable items (BEP 44).
Every packet it accepts is a response to one of its own queries, from the
address it was sent to, and is decoded with hard size, depth and item caps.

Loaded by the ``census`` plugin with ``kestrel_pi.load_helper``; the desktop
census report loads it the same way. Stdlib + ``cryptography``.
"""

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
