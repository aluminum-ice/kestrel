#!/usr/bin/env python3
"""Merge AngryOxide hashlines into the append-only kestrel store.

The capture launcher runs this alongside AngryOxide. AngryOxide writes its
per-run artifacts (pcapng, hashlines, logs) into the run directory; this watches
for *.hc22000 files there and appends each unique hashline to the persistent
store, routing by access point in "per-ap" mode (the default: one
<ssid>_<bssid>.hc22000 per network+radio, keyed on the BSSID so unrelated APs
that share a name never merge), by SSID in "per-ssid" mode (one <ssid>.hc22000
per network), or to one combined file in "single" mode. (This is a file-layout choice only; the
dashboard's "access points" count is distinct radios regardless of layout.)

The store is only ever appended to and de-duplicated, never truncated, so
handshakes survive reboots and attack restarts. Dedup is exact-line and seeded
from the existing store at start, so a restart does not re-append old lines.
See docs/developer/capture-interface.md.
"""
import glob
import json
import os
import re
import stat
import struct
import sys
import time

RUN_DIR = os.environ.get(
    "PWN_CAPTURE_RUN_DIR", "/var/lib/kestrel/capture-run"
)
STORE_DIR = os.environ.get("PWN_CAPTURE_DIR", "/var/lib/kestrel/capture")
# Where the run dir used to be, inside the store. Hashlines an older release
# left there are still drained, so an in-place update loses none.
LEGACY_RUN_DIR = os.path.join(STORE_DIR, ".run")
CONFIG = os.environ.get("PWN_CONFIG", "/etc/kestrel/config.toml")
POLL_SECONDS = int(os.environ.get("PWN_CAPTURE_POLL", "10"))

# The small best-effort "what's in range" file this writes into the store next to
# the hashlines (contract_version 3, docs/developer/capture-interface.md). It is
# NOT an .hc22000 file, so the store's hashline readers never pick it up.
STATUS_NAME = "capture-status.json"
# The per-AP last/first-seen index this maintains next to the hashlines
# (contract_version 4). Keyed by BSSID (AA:BB:CC:DD:EE:FF); "last" is when a NEW
# unique hashline for that AP was most recently drained into the store (not a
# live sighting). It is NOT an .hc22000 file, so the hashline readers skip it.
SEEN_NAME = "capture-seen.json"
# Distinct transmitting radios seen in the most recent ENV_WINDOW_DEFAULT seconds
# OF CAPTURE DATA (anchored to the newest packet, not wall-clock) is the density
# signal. Radios transmit in bursts, so a window of a few minutes reads stably
# rather than coming back empty. Config can override it.
ENV_WINDOW_DEFAULT = 300

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python < 3.11 has no tomllib
    tomllib = None

UNSAFE = re.compile(r"[^A-Za-z0-9._-]+")


def _capture_config():
    """The ``[capture]`` table from config, or ``{}`` on any read error."""
    if tomllib is None:
        return {}
    try:
        with open(CONFIG, "rb") as handle:
            return tomllib.load(handle).get("capture", {}) or {}
    except (OSError, tomllib.TOMLDecodeError):
        return {}


def run_sources():
    """Paths of the *.hc22000 files to drain: the run dir's, plus any an older
    release left in the store's legacy .run/ (drained, never deleted here)."""
    sources = []
    for run_dir in (RUN_DIR, LEGACY_RUN_DIR):
        try:
            sources += [os.path.join(run_dir, n) for n in sorted(os.listdir(run_dir))
                        if n.endswith(".hc22000")]
        except OSError:
            pass
    return sources


# A single_file value must be a plain file name in the store: no directory part
# and no leading dot, so it can't name a path outside the store.
_SINGLE_FILE_RE = re.compile(r"[A-Za-z0-9_-][A-Za-z0-9._-]*")


def output_config():
    """(mode, single_file) from config, with safe defaults. A single_file that
    isn't a plain file name falls back to the default."""
    mode, single = "per-ap", "all.hc22000"
    out = _capture_config().get("output", {}) or {}
    mode = out.get("mode", mode)
    configured = out.get("single_file", single)
    if isinstance(configured, str) and _SINGLE_FILE_RE.fullmatch(configured):
        single = configured
    else:
        sys.stderr.write("capture-collect: ignoring single_file %r (not a plain "
                         "file name); using %s\n" % (configured, single))
    return mode, single


def environment_config():
    """(enabled, window_s) for the in-range readout, with safe defaults."""
    env = _capture_config().get("environment", {}) or {}
    enabled = bool(env.get("enabled", True))
    try:
        window_s = int(env.get("window_s", ENV_WINDOW_DEFAULT))
    except (TypeError, ValueError):
        window_s = ENV_WINDOW_DEFAULT
    if window_s <= 0:
        window_s = ENV_WINDOW_DEFAULT
    return enabled, window_s


def sanitize(name):
    name = UNSAFE.sub("_", name).strip("._")
    return name[:64] or "unknown"


def decode_essid(essid_hex):
    """Decode a hex ESSID field to text: UTF-8 first, then latin-1 as a fallback
    so a non-UTF-8 name still yields a stable label instead of replacement
    characters. Empty or non-hex input yields ``""``."""
    try:
        raw = bytes.fromhex(essid_hex)
    except ValueError:
        return ""
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode("latin-1")


def target_for(line, mode, single):
    """Store filename for one hc22000 line.

    hc22000 layout: WPA*TYPE*PMKID_or_MIC*MAC_AP*MAC_STA*ESSID_hex*... The
    grouping key is always read from the record's own fields, never from a
    filename (capture filenames are not a trustworthy key):

    - ``single``: everything into one combined file.
    - ``per-ap`` (default): one file per physical AP, keyed on the BSSID (field 4) -- the
      only field that uniquely identifies one AP radio, since SSIDs are heavily
      reused across unrelated APs. The composite BSSID+ESSID is encoded in the
      name ``<ssid>_<bssid>.hc22000`` so a BSSID advertising multiple SSIDs
      splits naturally; an empty/hidden ESSID routes to
      ``__hidden_<bssid>.hc22000``. The BSSID in the name keeps unrelated APs
      that share a name (e.g. "poswifi") in separate files, and both PMKID (01)
      and EAPOL (02) records for an AP land in its one file.
    - ``per-ssid``: route by ESSID, falling back to the AP BSSID when
      the ESSID is unknown (PMKID-only / hidden network).
    """
    if mode == "single":
        return single

    parts = line.split("*")

    if mode == "per-ap":
        bssid = parts[3] if len(parts) >= 4 else ""
        if not bssid:
            return "unknown.hc22000"
        essid = decode_essid(parts[5]) if len(parts) >= 6 else ""
        if essid.strip():
            return sanitize(essid) + "_" + sanitize(bssid) + ".hc22000"
        return "__hidden_" + sanitize(bssid) + ".hc22000"

    # per-ssid: route by ESSID, fall back to the AP BSSID when the
    # ESSID is unknown (PMKID-only / hidden network).
    essid, bssid = "", ""
    if len(parts) >= 6:
        bssid = parts[3]
        try:
            essid = bytes.fromhex(parts[5]).decode("utf-8", "replace")
        except ValueError:
            essid = ""
    if essid.strip():
        return sanitize(essid) + ".hc22000"
    if bssid:
        return "bssid-" + sanitize(bssid) + ".hc22000"
    return "unknown.hc22000"


def seen_lines(path):
    lines = set()
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                line = line.rstrip("\n")
                if line:
                    lines.add(line)
    except FileNotFoundError:
        pass
    return lines


# The in-range readout is read from the tail of AngryOxide's pcapng capture
# file, never from its kismet DB. AngryOxide inserts and commits one kismet row
# per frame, which an SD card can't keep up with in a busy RF environment: the
# DB falls minutes behind real time (and its rollback-journal locking can't be
# read safely from here), while the pcapng is a plain append that keeps up. So
# the collector remembers how far it has read each pcapng and, every poll,
# parses only the blocks added since, counting each frame's transmitter
# address. No locks, no database, nothing that can slow AngryOxide down.

# At most this many pcapng files are read, newest first: the current launch's
# and, across a relaunch for a new agent plan, the previous one's.
ENV_MAX_FILES = 2
# At most this many new bytes are parsed per file per poll, so a large file met
# for the first time (a collector restart mid-run) is caught up over a few polls
# instead of in one long pause.
ENV_MAX_READ = int(os.environ.get("PWN_CAPTURE_ENV_MAX_READ", str(16 * 1024 * 1024)))

_PCAPNG_SHB = 0x0A0D0D0A
_PCAPNG_IDB = 0x00000001
_PCAPNG_EPB = 0x00000006
_BYTE_ORDER_MAGIC = 0x1A2B3C4D
_LINKTYPE_80211 = 105
_LINKTYPE_RADIOTAP = 127
# A block longer than this is treated as corruption, not a frame.
_MAX_BLOCK = 1 << 20

# Per-file read state ({path: _Tail}) and the radios seen so far
# ({transmitter: newest frame time}), kept for the life of the collector.
_TAILS = {}
_RADIOS = {}


class _Tail:
    """How far one pcapng has been read, and what its header blocks said."""

    def __init__(self, ident):
        self.ident = ident          # (st_dev, st_ino): a new file resets it
        self.offset = 0
        self.endian = "<"
        self.interfaces = []        # [(linktype, seconds per timestamp unit)]
        self.broken = False         # stop reading a file that isn't pcapng
        self.newest = None          # newest frame time seen in this file


def _capture_files():
    """The run dir's ``*.pcapng`` files, newest first (at most ENV_MAX_FILES)."""
    try:
        paths = glob.glob(os.path.join(RUN_DIR, "*.pcapng"))
    except OSError:
        return []
    stamped = []
    for path in paths:
        try:
            stamped.append((os.path.getmtime(path), path))
        except OSError:
            continue
    stamped.sort(reverse=True)
    return [path for _mtime, path in stamped[:ENV_MAX_FILES]]


def _tsresol(options, endian):
    """Seconds per timestamp unit from an IDB's options (if_tsresol, code 9);
    microseconds when absent."""
    pos = 0
    while pos + 4 <= len(options):
        code, length = struct.unpack(endian + "HH", options[pos:pos + 4])
        if code == 0:
            break
        if code == 9 and length >= 1:
            value = options[pos + 4]
            return 2.0 ** -(value & 0x7F) if value & 0x80 else 10.0 ** -value
        pos += 4 + ((length + 3) & ~3)
    return 1e-6


def transmitter(frame):
    """The transmitter address (addr2) of an 802.11 frame, as lower-case
    ``aa:bb:cc:dd:ee:ff``, or None for frames without one (ACK, CTS, ...), a
    truncated frame, or a null or group address."""
    if len(frame) < 16:
        return None
    ftype = (frame[0] >> 2) & 0x3
    subtype = frame[0] >> 4
    # Management and data frames carry addr2; of the control frames, only
    # BlockAckReq, BlockAck, PS-Poll and RTS name their transmitter.
    if ftype not in (0, 2) and not (ftype == 1 and subtype in (8, 9, 10, 11)):
        return None
    addr = frame[10:16]
    if addr[0] & 0x01 or not any(addr):
        return None
    return ":".join("%02x" % b for b in addr)


def _read_tail(path, tail):
    """Parse the blocks added to ``path`` since ``tail.offset`` (at most
    ENV_MAX_READ bytes), recording each frame's transmitter in ``_RADIOS``.
    A partial block at the end is left for the next poll."""
    try:
        with open(path, "rb") as fh:
            remaining = os.fstat(fh.fileno()).st_size - tail.offset
            if remaining <= 0:
                return
            fh.seek(tail.offset)
            # Bounded by what's there, so a poll with a few new KB doesn't
            # allocate the whole ENV_MAX_READ.
            data = fh.read(min(ENV_MAX_READ, remaining))
    except OSError:
        return
    pos = 0
    while pos + 12 <= len(data):
        if tail.offset == 0 and pos == 0:
            # The section header's byte-order magic decides how to read the rest.
            if struct.unpack("<I", data[0:4])[0] != _PCAPNG_SHB or len(data) < 12:
                tail.broken = True
                return
            magic = data[8:12]
            if struct.unpack("<I", magic)[0] == _BYTE_ORDER_MAGIC:
                tail.endian = "<"
            elif struct.unpack(">I", magic)[0] == _BYTE_ORDER_MAGIC:
                tail.endian = ">"
            else:
                tail.broken = True
                return
        btype, blen = struct.unpack(tail.endian + "II", data[pos:pos + 8])
        if blen < 12 or blen % 4 or blen > _MAX_BLOCK:
            tail.broken = True
            return
        if pos + blen > len(data):
            break                   # the rest of this block isn't written yet
        body = data[pos + 8:pos + blen - 4]
        if btype == _PCAPNG_SHB:
            tail.interfaces = []
        elif btype == _PCAPNG_IDB and len(body) >= 8:
            linktype = struct.unpack(tail.endian + "H", body[0:2])[0]
            tail.interfaces.append((linktype, _tsresol(body[8:], tail.endian)))
        elif btype == _PCAPNG_EPB and len(body) >= 20:
            iface, ts_hi, ts_lo, cap_len = struct.unpack(tail.endian + "IIII", body[0:16])
            if iface < len(tail.interfaces):
                linktype, unit = tail.interfaces[iface]
                packet = body[20:20 + cap_len]
                if linktype == _LINKTYPE_RADIOTAP and len(packet) >= 4:
                    packet = packet[struct.unpack("<H", packet[2:4])[0]:]
                elif linktype != _LINKTYPE_80211:
                    packet = b""
                when = ((ts_hi << 32) | ts_lo) * unit
                mac = transmitter(packet)
                if mac is not None and when > _RADIOS.get(mac, -1):
                    _RADIOS[mac] = when
                if when > (tail.newest or -1):
                    tail.newest = when
        pos += blen
    tail.offset += pos


def environment_snapshot(window_s, now=None):
    """Distinct transmitting radios seen in the most recent ``window_s`` seconds
    of capture, read from the new blocks of the run dir's pcapng files (the
    current launch's and, across an agent relaunch, the previous one's).

    The window ends at the newest frame in any of them, never at wall-clock
    ``now``, so a quiet moment reads as "recent" rather than zero and a
    relaunch's new, still-empty file doesn't hide the minutes just before it.
    Returns ``(radios_recent, data_ts)`` where ``data_ts`` is that newest
    frame's epoch (so the reader can judge freshness), or ``None`` when there
    is no capture file or frame yet. ``now`` is accepted for tests but unused."""
    files = _capture_files()
    for path in list(_TAILS):
        if path not in files and not os.path.exists(path):
            del _TAILS[path]
    newest = None
    for path in files:
        try:
            st = os.stat(path)
        except OSError:
            continue
        tail = _TAILS.get(path)
        if tail is None or tail.ident != (st.st_dev, st.st_ino) or st.st_size < tail.offset:
            tail = _TAILS[path] = _Tail((st.st_dev, st.st_ino))   # new or replaced file
        if not tail.broken and st.st_size > tail.offset:
            _read_tail(path, tail)
        if tail.newest is not None:
            newest = tail.newest if newest is None else max(newest, tail.newest)
    if newest is None:
        return None
    cutoff = newest - window_s
    # Forget radios long out of any window, so the map stays small.
    for mac in [m for m, ts in _RADIOS.items() if ts < newest - 2 * window_s]:
        del _RADIOS[mac]
    return sum(1 for ts in _RADIOS.values() if ts >= cutoff), int(newest)


def write_status(payload):
    """Atomically write the small status JSON into the store."""
    dest = os.path.join(STORE_DIR, STATUS_NAME)
    tmp = dest + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as out:
            json.dump(payload, out)
        os.replace(tmp, dest)
    except OSError as exc:
        sys.stderr.write("capture-collect: %s: %s\n" % (dest, exc))


def read_status():
    """The current status JSON (a dict), or None if absent/unreadable - used to
    carry a last good reading across a momentarily locked DB."""
    try:
        with open(os.path.join(STORE_DIR, STATUS_NAME), encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


def _format_mac(hex12):
    """The AP MAC as AA:BB:CC:DD:EE:FF, or "" if not 12 hex digits. Mirrors
    ``kestrel_pi.capture._format_mac`` so capture-seen.json keys match the BSSID
    the readers join on; the collector is a standalone script that does not
    import the package."""
    hex12 = hex12.strip().lower()
    if len(hex12) != 12 or not all(c in "0123456789abcdef" for c in hex12):
        return ""
    return ":".join(hex12[i:i + 2] for i in range(0, 12, 2)).upper()


def line_bssid(line):
    """The AP BSSID (formatted) from one hc22000 line, or "" if unparseable."""
    fields = line.split("*")
    return _format_mac(fields[3]) if len(fields) >= 4 else ""


def read_seen_times():
    """Load ``{bssid: {"first", "last"}}`` from capture-seen.json (carried across
    collector restarts), or ``{}`` if absent/unreadable."""
    try:
        with open(os.path.join(STORE_DIR, SEEN_NAME), encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return {}
    aps = data.get("aps") if isinstance(data, dict) else None
    return aps if isinstance(aps, dict) else {}


def write_seen_times(aps):
    """Atomically write the per-AP first/last-seen index into the store."""
    dest = os.path.join(STORE_DIR, SEEN_NAME)
    tmp = dest + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as out:
            json.dump({"version": 1, "aps": aps}, out)
        os.replace(tmp, dest)
    except OSError as exc:
        sys.stderr.write("capture-collect: %s: %s\n" % (dest, exc))


def stamp_seen(seen_times, bssids, now):
    """Record ``now`` as each BSSID's last-seen (and first-seen the first time).
    Mutates and returns ``seen_times``."""
    for bssid in bssids:
        rec = seen_times.get(bssid)
        if rec is None:
            seen_times[bssid] = {"first": now, "last": now}
        else:
            rec["last"] = now
            rec.setdefault("first", now)
    return seen_times


def update_environment(window_s):
    """Refresh the in-range readout once. Writes ``radios_recent`` and
    ``data_ts`` (the newest-frame epoch, so the reader can judge freshness) from
    the capture files. When there is nothing to read yet - a capture start or a
    roll, when the launcher clears the run dir and AngryOxide starts a fresh
    pcapng - the previous reading is carried through (only its freshness ages,
    via ``data_ts``), so the card does not blank to "scanning" on every
    relaunch. The carried reading still ages out to "unknown" once ``data_ts``
    passes the reader's ``max_age_s`` (so a capture that truly stopped is not
    shown as a stale count). Only a cold start with no prior reading writes the
    scanning-but-no-count marker."""
    snap = environment_snapshot(window_s)
    payload = {"updated_at": int(time.time()), "window_s": window_s,
               "scanning": True}
    if snap is not None:
        payload["radios_recent"], payload["data_ts"] = snap
    else:
        # Nothing to read this tick (a fresh, still-empty capture file right
        # after a relaunch): keep the last good reading rather than dropping it.
        # Its freshness ages via data_ts, so the reader blanks it once it is
        # genuinely too old.
        prev = read_status()
        if prev and "radios_recent" in prev and "data_ts" in prev:
            payload["radios_recent"] = prev["radios_recent"]
            payload["data_ts"] = prev["data_ts"]
    write_status(payload)


def main():
    os.makedirs(STORE_DIR, exist_ok=True)
    mode, single = output_config()
    env_enabled, env_window = environment_config()
    seen = {}  # target filename -> set of lines already in the store
    seen_times = read_seen_times()  # bssid -> {first,last}, carried across restarts

    while True:
        if env_enabled:
            update_environment(env_window)
        stamp = set()  # BSSIDs that got a NEW hashline this poll
        for source in run_sources():
            try:
                with open(source, "r", encoding="utf-8", errors="replace") as handle:
                    run_lines = handle.read().splitlines()
            except OSError:
                continue
            pending = {}
            for line in run_lines:
                line = line.strip()
                if not line:
                    continue
                target = target_for(line, mode, single)
                if target not in seen:
                    seen[target] = seen_lines(os.path.join(STORE_DIR, target))
                if line in seen[target]:
                    continue
                seen[target].add(line)
                pending.setdefault(target, []).append(line)
                bssid = line_bssid(line)
                if bssid:
                    stamp.add(bssid)
            for target, new_lines in pending.items():
                dest = os.path.join(STORE_DIR, target)
                try:
                    with open(dest, "a", encoding="utf-8") as out:
                        out.write("\n".join(new_lines) + "\n")
                except OSError as exc:
                    sys.stderr.write("capture-collect: %s: %s\n" % (dest, exc))
        # Record the last (and first) time each AP produced a new hashline, so
        # the networks page can sort by most-recently-captured. Written once per
        # poll, only when something new arrived.
        if stamp:
            stamp_seen(seen_times, stamp, int(time.time()))
            write_seen_times(seen_times)
        time.sleep(POLL_SECONDS)


# -- adopt-store: hand an older release's root-owned files to the collector --
# Up to 1.0.5 the collector ran as root, so the per-network files it created,
# the legacy .run/ and the archive sets `capture archive` made are root-owned;
# the collector now runs as pi and can't append to them. The launcher (root)
# runs `capture-collect --adopt-store pi` before starting the collector. Root
# acting in the pi-owned store must not be redirectable, so every step works on
# a descriptor opened relative to its parent's descriptor with O_NOFOLLOW, the
# entry is re-checked through that descriptor, and the owner change is fchown
# on it: a symlink, or an entry swapped in mid-run, can't point it elsewhere.
# Only regular files and directories owned by root, with one link (so not a
# system file hard-linked in) and on the store's filesystem, are handed over.
# Nothing is logged but counts: file names here are network names.
ADOPT_MAX_DEPTH = 4   # store/archive/<set>/<file>, with room for a nested archive_dir
_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
_FILE_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_NOCTTY | os.O_CLOEXEC


def adopt_store(store_dir, uid, gid, from_uid=0, chown=os.fchown):
    """Give ``from_uid``'s files and folders in the store to ``uid:gid``.
    Returns ``{"files", "dirs", "skipped"}`` counts. Does nothing unless the
    store itself belongs to ``uid``."""
    counts = {"files": 0, "dirs": 0, "skipped": 0}
    try:
        top = os.open(store_dir, _DIR_FLAGS)
    except OSError as exc:
        sys.stderr.write("capture-collect: adopt: store not opened (%s)\n"
                         % exc.__class__.__name__)
        return counts
    try:
        st = os.fstat(top)
        if st.st_uid != uid:
            sys.stderr.write("capture-collect: adopt: store is not the collector's; "
                             "left alone\n")
            return counts
        _adopt_dir(top, st.st_dev, uid, gid, from_uid, chown, ADOPT_MAX_DEPTH, counts)
    finally:
        os.close(top)
    return counts


def _adopt_dir(dfd, dev, uid, gid, from_uid, chown, depth, counts):
    try:
        names = os.listdir(dfd)
    except OSError:
        return
    for name in names:
        try:
            st = os.stat(name, dir_fd=dfd, follow_symlinks=False)
        except OSError:
            continue
        if st.st_dev != dev:
            counts["skipped"] += 1
            continue
        if stat.S_ISREG(st.st_mode):
            if st.st_uid == from_uid:
                _adopt_file(dfd, name, st, uid, gid, from_uid, chown, counts)
        elif stat.S_ISDIR(st.st_mode):
            if depth > 0:
                _adopt_subdir(dfd, name, st, dev, uid, gid, from_uid, chown,
                              depth, counts)
        elif st.st_uid == from_uid:
            counts["skipped"] += 1   # symlink, fifo, socket, device: never touched


def _same(st, fst):
    return (fst.st_dev, fst.st_ino) == (st.st_dev, st.st_ino)


def _adopt_file(dfd, name, st, uid, gid, from_uid, chown, counts):
    try:
        fd = os.open(name, _FILE_FLAGS, dir_fd=dfd)
    except OSError:
        counts["skipped"] += 1
        return
    try:
        fst = os.fstat(fd)
        if not (_same(st, fst) and stat.S_ISREG(fst.st_mode)
                and fst.st_uid == from_uid and fst.st_nlink == 1):
            counts["skipped"] += 1
            return
        chown(fd, uid, gid)
        counts["files"] += 1
    except OSError:
        counts["skipped"] += 1
    finally:
        os.close(fd)


def _adopt_subdir(dfd, name, st, dev, uid, gid, from_uid, chown, depth, counts):
    try:
        fd = os.open(name, _DIR_FLAGS, dir_fd=dfd)
    except OSError:
        counts["skipped"] += 1
        return
    try:
        fst = os.fstat(fd)
        if not (_same(st, fst) and stat.S_ISDIR(fst.st_mode)):
            counts["skipped"] += 1
            return
        _adopt_dir(fd, dev, uid, gid, from_uid, chown, depth - 1, counts)
        if fst.st_uid == from_uid:
            chown(fd, uid, gid)
            counts["dirs"] += 1
    except OSError:
        counts["skipped"] += 1
    finally:
        os.close(fd)


def adopt_main(user):
    """``capture-collect --adopt-store USER``: run by the launcher as root."""
    import pwd

    if os.geteuid() != 0:
        sys.stderr.write("capture-collect: --adopt-store needs root\n")
        return 2
    try:
        account = pwd.getpwnam(user)
    except KeyError:
        sys.stderr.write("capture-collect: adopt: no such user\n")
        return 2
    counts = adopt_store(STORE_DIR, account.pw_uid, account.pw_gid)
    if counts["files"] or counts["dirs"] or counts["skipped"]:
        sys.stderr.write("capture-collect: adopt: handed %d file(s) and %d folder(s) "
                         "to the collector, skipped %d\n"
                         % (counts["files"], counts["dirs"], counts["skipped"]))
    return 0


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--adopt-store":
        sys.exit(adopt_main(sys.argv[2]))
    try:
        main()
    except KeyboardInterrupt:
        pass
    finally:
        # On stop, mark the readout not-scanning so the UI shows "unknown"
        # immediately rather than the last count going stale.
        write_status({"updated_at": int(time.time()), "scanning": False})
