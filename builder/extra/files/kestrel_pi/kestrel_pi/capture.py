"""The capture-store surface the daemon sources ``on_handshake`` from.

This reads the Phase 2 capture store (``/var/lib/kestrel/capture/``,
``contract_version 1``) and nothing else, per ``docs/developer/capture-interface.md``:
the store holds append-only, exact-line-deduped hashcat-22000 (`.hc22000`)
lines, and a new handshake is a store file that is created or grows. The
watcher polls the store (no inotify dependency), tracks a byte offset per file,
parses the new lines, and hands each one to a callback as a
:class:`HandshakeEvent`.
"""

from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import dataclass, field

# The best-effort "what's in range" file the collector writes into the store
# (contract_version 3). Read by the daemon for the Environment readout; absent,
# unreadable, stale, or not-scanning all mean "unknown".
STATUS_NAME = "capture-status.json"
# The collector's per-AP first/last-seen index (contract_version 4), keyed by
# BSSID; "last" is when a new unique hashline for that AP was last drained.
SEEN_NAME = "capture-seen.json"

# Density buckets: the upper bound (inclusive) of distinct radios-in-range for
# each of the first four labels; anything above the last bound is the fifth.
# The count is transmitting radios (APs and clients together), so these run
# higher than an APs-only count would. A starting calibration, overridable via
# ``[capture.environment] thresholds``.
DENSITY_LABELS = ("Quiet", "Light", "Moderate", "Busy", "Dense")
DEFAULT_DENSITY_THRESHOLDS = (0, 10, 30, 60)

# How old the newest capture data may be before the readout stops being called a
# current reading and degrades to "unknown". Some lag is normal (the collector
# reads AngryOxide's capture file every poll); past this it is no longer
# "nearby now".
DEFAULT_ENV_MAX_AGE_S = 900


def classify_density(count, thresholds=None):
    """Map a radios-in-range count to ``(level, label)`` where ``level`` is 0-4.

    ``thresholds`` is four ascending inclusive upper bounds; a bad override falls
    back to the defaults so the UI never breaks on operator config."""
    bounds = DEFAULT_DENSITY_THRESHOLDS
    if thresholds:
        try:
            cand = tuple(int(x) for x in thresholds)
            if len(cand) == 4 and list(cand) == sorted(cand):
                bounds = cand
        except (TypeError, ValueError):
            pass
    try:
        count = int(count)
    except (TypeError, ValueError):
        count = 0
    level = 0
    for bound in bounds:
        if count <= bound:
            break
        level += 1
    return level, DENSITY_LABELS[level]


@dataclass
class HandshakeEvent:
    """A single new hashline, as handed to ``on_handshake``."""

    path: str          # the store file the line landed in
    essid: str         # decoded ESSID, or "" if unknown/hidden
    bssid: str         # AP MAC as AA:BB:CC:DD:EE:FF, or ""
    type: str          # "pmkid", "eapol", or "unknown"
    raw: str = field(repr=False, default="")


_HC22000_TYPE = {"01": "pmkid", "02": "eapol"}


def _format_mac(hex12):
    hex12 = hex12.strip().lower()
    if len(hex12) != 12 or not all(c in "0123456789abcdef" for c in hex12):
        return ""
    return ":".join(hex12[i : i + 2] for i in range(0, 12, 2)).upper()


def parse_hashline(line):
    """Parse one ``.hc22000`` line into a :class:`HandshakeEvent` (path left
    blank; the caller fills it). Returns ``None`` for a blank/garbage line.

    Layout: ``WPA*TYPE*PMKID_or_MIC*MAC_AP*MAC_STA*ESSID_hex*...`` (fields are
    ``*``-separated). Only the AP MAC, type, and ESSID are extracted; the line
    is never rewritten.
    """
    line = line.strip()
    if not line:
        return None
    fields = line.split("*")
    if len(fields) < 6 or fields[0] != "WPA":
        return None
    htype = _HC22000_TYPE.get(fields[1], "unknown")
    bssid = _format_mac(fields[3])
    essid = ""
    essid_hex = fields[5]
    try:
        essid = bytes.fromhex(essid_hex).decode("utf-8", "replace") if essid_hex else ""
    except ValueError:
        essid = ""
    return HandshakeEvent(path="", essid=essid, bssid=bssid, type=htype, raw=line)


class CaptureInterface:
    """Read-only view of the capture store, handed to plugins as
    ``context.capture``. Thin on purpose: the store path and the helpers a
    plugin needs to read hashlines and write/read the GPS sidecars."""

    def __init__(self, store_dir):
        self.store_dir = store_dir
        # _parse_store() result (shared by the by-network and by-AP views),
        # keyed on the store's file signature.
        self._networks_cache = None
        self._networks_lock = threading.Lock()

    def hashline_files(self):
        """Every ``*.hc22000`` file currently in the store (sorted)."""
        try:
            names = os.listdir(self.store_dir)
        except OSError:
            return []
        return sorted(
            os.path.join(self.store_dir, n)
            for n in names
            if n.endswith(".hc22000")
        )

    def hashline_count(self):
        """Total hashlines across the store (append-only and exact-line deduped),
        i.e. the lifetime count of captured handshakes. Counts non-empty lines
        without parsing, so it is cheap relative to :meth:`captured_networks`."""
        total = 0
        for path in self.hashline_files():
            try:
                with open(path, "r", encoding="utf-8", errors="replace") as handle:
                    total += sum(1 for line in handle if line.strip())
            except OSError:
                continue
        return total

    def ap_bssids(self):
        """The set of unique AP BSSIDs across the store, i.e. the distinct access
        points we hold a handshake from. Reads every hashline file, so a caller
        should snapshot it (e.g. at startup) rather than call it per tick."""
        bssids = set()
        for path in self.hashline_files():
            for event in self.read_events(path):
                if event.bssid:
                    bssids.add(event.bssid)
        return bssids

    def read_events(self, path):
        """Parse every hashline in ``path`` into :class:`HandshakeEvent`s."""
        events = []
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as handle:
                for line in handle:
                    event = parse_hashline(line)
                    if event is not None:
                        event.path = path
                        events.append(event)
        except OSError:
            return []
        return events

    def _parse_store(self):
        """Parse the whole store once into
        ``{essid: {"bssids": {bssid: count}, "no_bssid": int, "handshakes": int}}``.

        Cached and reused while every file's name, size and mtime are unchanged
        (the store only grows by appends and new files), so the store is read at
        most once per change no matter which view asks. Both the by-network view
        (:meth:`captured_networks`) and the by-access-point view
        (:meth:`captured_access_points`) are derived from this single parse,
        because the file layout on disk (``[capture.output] mode``) does not
        affect it -- every hashline carries its own ESSID and BSSID.

        Returns ``(nets, mtime_by_bssid)`` where ``mtime_by_bssid`` maps each
        BSSID to the newest mtime of a store file it appears in -- the fallback
        "last captured" time for APs the collector's seen-index predates (it
        only stamps handshakes drained after contract_version 4 shipped)."""
        paths = self.hashline_files()
        signature = []
        mtimes = {}
        for path in paths:
            try:
                st = os.stat(path)
            except OSError:
                continue
            signature.append((path, st.st_size, st.st_mtime_ns))
            mtimes[path] = st.st_mtime
        signature = tuple(signature)
        with self._networks_lock:
            cached = self._networks_cache
        if cached is not None and cached[0] == signature:
            return cached[1], cached[2]
        nets = {}
        mtime_by_bssid = {}
        for path in paths:
            fmtime = mtimes.get(path, 0)
            for event in self.read_events(path):
                slot = nets.get(event.essid)
                if slot is None:
                    slot = nets[event.essid] = {
                        "bssids": {}, "no_bssid": 0, "handshakes": 0}
                slot["handshakes"] += 1
                if event.bssid:
                    slot["bssids"][event.bssid] = (
                        slot["bssids"].get(event.bssid, 0) + 1)
                    if fmtime > mtime_by_bssid.get(event.bssid, 0):
                        mtime_by_bssid[event.bssid] = fmtime
                else:
                    slot["no_bssid"] += 1
        with self._networks_lock:
            self._networks_cache = (signature, nets, mtime_by_bssid)
        return nets, mtime_by_bssid

    def _read_seen(self):
        """``{bssid: last_epoch}`` from the collector's ``capture-seen.json``
        index, or ``{}`` if absent/unreadable. Read live (not cached with the
        parse) so a fresh stamp is reflected even when the hashline files are
        unchanged."""
        try:
            with open(os.path.join(self.store_dir, SEEN_NAME),
                      "r", encoding="utf-8") as handle:
                data = json.load(handle)
        except (OSError, ValueError):
            return {}
        aps = data.get("aps") if isinstance(data, dict) else None
        if not isinstance(aps, dict):
            return {}
        out = {}
        for bssid, rec in aps.items():
            try:
                out[bssid] = int(rec.get("last"))
            except (AttributeError, TypeError, ValueError):
                continue
        return out

    def _last_seen(self, bssid, seen, mtime_by_bssid):
        """Best "last captured" epoch for a BSSID: the collector's stamp if it
        has one, else the store-file mtime fallback, else ``None``."""
        if bssid in seen:
            return seen[bssid]
        mtime = mtime_by_bssid.get(bssid)
        return int(mtime) if mtime else None

    def captured_networks(self):
        """Unique captured networks across the whole store, keyed by ESSID.

        Returns a list of ``{"essid", "bssids", "handshakes", "last_seen"}``
        (``bssids`` is the count of distinct APs seen under that ESSID;
        ``last_seen`` is the newest capture epoch across them, or ``None``)
        sorted by ESSID (case-insensitive; an empty/hidden ESSID sorts last). A
        caller rendering it should search and paginate the returned list rather
        than the store. This is the by-network view;
        :meth:`captured_access_points` is the by-access-point view over the same
        cached parse."""
        nets, mtime_by_bssid = self._parse_store()
        seen = self._read_seen()
        out = []
        for essid, data in nets.items():
            times = [t for t in (self._last_seen(b, seen, mtime_by_bssid)
                                 for b in data["bssids"]) if t is not None]
            out.append({"essid": essid, "bssids": len(data["bssids"]),
                        "handshakes": data["handshakes"],
                        "last_seen": max(times) if times else None})
        out.sort(key=lambda net: (net["essid"] == "", net["essid"].lower()))
        return out

    def captured_access_points(self):
        """One entry per distinct access point -- that is, per **radio (BSSID)**
        -- across the store.

        Returns a list of ``{"bssid", "essids", "handshakes", "last_seen"}``
        where ``essids`` is the sorted list of distinct names that radio was seen
        advertising (so a radio captured hidden and later named lists both names
        and is still ONE access point), ``handshakes`` is its total across those
        names and ``last_seen`` its newest capture epoch (or ``None``). Sorted by
        the first name (case-insensitive; a radio only ever seen hidden sorts
        last) then BSSID. Handshakes whose BSSID could not be parsed (a corrupt
        line) are folded into a single trailing entry with an empty ``bssid``
        (the page renders it as the "(unknown BSSID)" row); it is a handshake but
        not an access point, so it is excluded from the access-point count. This
        is the by-access-point view; :meth:`captured_networks` is the by-network
        view."""
        nets, mtime_by_bssid = self._parse_store()
        seen = self._read_seen()
        by_bssid = {}   # bssid -> {"essids": set, "handshakes": int}
        unknown = 0
        for essid, data in nets.items():
            for bssid, count in data["bssids"].items():
                slot = by_bssid.get(bssid)
                if slot is None:
                    slot = by_bssid[bssid] = {"essids": set(), "handshakes": 0}
                if essid:
                    slot["essids"].add(essid)
                slot["handshakes"] += count
            unknown += data["no_bssid"]
        out = []
        for bssid, slot in by_bssid.items():
            out.append({
                "bssid": bssid,
                "essids": sorted(slot["essids"], key=str.lower),
                "handshakes": slot["handshakes"],
                "last_seen": self._last_seen(bssid, seen, mtime_by_bssid),
            })
        out.sort(key=lambda ap: (not ap["essids"],
                                 ap["essids"][0].lower() if ap["essids"] else "",
                                 ap["bssid"]))
        if unknown:
            out.append({"bssid": "", "essids": [], "handshakes": unknown,
                        "last_seen": None})
        return out

    def capture_counts(self):
        """``{"handshakes", "access_points", "networks"}`` from the cached parse,
        the three numbers the whole UI speaks in plain English ("N handshakes
        from M access points across K networks"):

        - ``handshakes``: total captured hashlines across the store.
        - ``access_points``: distinct **radios** (BSSIDs). A radio seen under
          more than one name (hidden, then named) counts once.
        - ``networks``: distinct network names (ESSIDs).

        One definition each, used by the dashboard navcard, the /networks page
        and (via the daemon) the e-ink face, so they always agree. The
        "(unknown BSSID)" fold is a handshake but not an access point, so it adds
        to ``handshakes`` and not to ``access_points``."""
        nets, _mtime = self._parse_store()
        bssids = set()
        for data in nets.values():
            bssids.update(data["bssids"])
        return {
            "handshakes": sum(d["handshakes"] for d in nets.values()),
            "access_points": len(bssids),
            "networks": len(nets),
        }

    def environment(self, stale_after=180):
        """The collector's in-range readout (``capture-status.json``), or ``None``.

        Returns the parsed dict only when the file exists, is scanning, and was
        written within ``stale_after`` seconds (i.e. the collector is alive);
        absent/unreadable/stale/not-scanning all return ``None`` so a consumer
        shows "unknown" rather than a stale count. A scanning file with no counts
        yet (no frame captured) returns the dict without ``radios_recent``.
        The data's own freshness (``data_ts``) is judged separately by the
        daemon, since the reading can trail real time."""
        try:
            with open(os.path.join(self.store_dir, STATUS_NAME),
                      "r", encoding="utf-8") as handle:
                data = json.load(handle)
        except (OSError, ValueError):
            return None
        if not isinstance(data, dict) or not data.get("scanning"):
            return None
        updated = data.get("updated_at")
        if stale_after is not None:
            try:
                if time.time() - float(updated) > stale_after:
                    return None
            except (TypeError, ValueError):
                return None
        return data

    def sidecar_path(self, capture_path, suffix):
        """Path for a sidecar next to a capture file, e.g. ``.gps.json``."""
        return capture_path + suffix


class CaptureWatcher:
    """Polls the capture store and fires a callback for each new hashline.

    The store is append-only and exact-line-deduped by the Phase 2 collector,
    so new bytes are new unique hashlines; we track a byte offset per file and
    parse only the tail. A file that shrank or vanished (an archive/clear moved
    it aside) resets its offset. Poll-based so there is no inotify dependency.
    """

    def __init__(self, store_dir, on_event, logger=None):
        self.interface = CaptureInterface(store_dir)
        self._on_event = on_event
        self._log = logger
        self._offsets = {}

    def prime(self):
        """Record current file sizes without firing events, so a restart does
        not re-announce every hashline already in the store."""
        for path in self.interface.hashline_files():
            try:
                self._offsets[path] = os.path.getsize(path)
            except OSError:
                self._offsets[path] = 0

    def poll_once(self):
        """Scan the store once and fire the callback for each new hashline.

        Returns the number of events fired (useful in tests)."""
        fired = 0
        for path in self.interface.hashline_files():
            try:
                size = os.path.getsize(path)
            except OSError:
                continue
            offset = self._offsets.get(path, 0)
            if size < offset:
                # File was truncated/rotated under us; start from the top.
                offset = 0
            if size == offset:
                continue
            try:
                with open(path, "r", encoding="utf-8", errors="replace") as handle:
                    handle.seek(offset)
                    chunk = handle.read()
                    self._offsets[path] = handle.tell()
            except OSError:
                continue
            for line in chunk.splitlines():
                event = parse_hashline(line)
                if event is None:
                    continue
                event.path = path
                fired += 1
                try:
                    self._on_event(event)
                except Exception:  # noqa: BLE001 - never let one callback stop the scan
                    if self._log:
                        self._log.exception("capture watcher callback failed")
        return fired
