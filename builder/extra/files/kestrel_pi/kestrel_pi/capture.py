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
import time
from dataclasses import dataclass, field

# The best-effort "what's in range" file the collector writes into the store
# (contract_version 3). Read by the daemon for the Environment readout; absent,
# unreadable, stale, or not-scanning all mean "unknown".
STATUS_NAME = "capture-status.json"

# Density buckets: the upper bound (inclusive) of distinct radios-in-range for
# each of the first four labels; anything above the last bound is the fifth.
# The count is transmitting radios (APs and clients together), so these run
# higher than an APs-only count would. A starting calibration, overridable via
# ``[capture.environment] thresholds``.
DENSITY_LABELS = ("Quiet", "Light", "Moderate", "Busy", "Dense")
DEFAULT_DENSITY_THRESHOLDS = (0, 10, 30, 60)

# How old the newest capture data may be before the readout stops being called a
# current reading and degrades to "unknown". AngryOxide flushes its kismet DB in
# bursts, so some lag is normal; past this it is no longer "nearby now".
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

    def captured_networks(self):
        """Unique captured APs across the whole store, keyed by ESSID.

        Returns a list of ``{"essid", "bssids", "handshakes"}`` sorted by ESSID
        (case-insensitive; an empty/hidden ESSID sorts last). This reads every
        hashline file, so a caller rendering it should search and paginate the
        returned list rather than the store."""
        nets = {}
        for path in self.hashline_files():
            for event in self.read_events(path):
                slot = nets.get(event.essid)
                if slot is None:
                    slot = nets[event.essid] = {"bssids": set(), "handshakes": 0}
                slot["handshakes"] += 1
                if event.bssid:
                    slot["bssids"].add(event.bssid)
        out = [
            {"essid": essid, "bssids": len(data["bssids"]),
             "handshakes": data["handshakes"]}
            for essid, data in nets.items()
        ]
        out.sort(key=lambda net: (net["essid"] == "", net["essid"].lower()))
        return out

    def environment(self, stale_after=180):
        """The collector's in-range readout (``capture-status.json``), or ``None``.

        Returns the parsed dict only when the file exists, is scanning, and was
        written within ``stale_after`` seconds (i.e. the collector is alive);
        absent/unreadable/stale/not-scanning all return ``None`` so a consumer
        shows "unknown" rather than a stale count. A scanning file with no counts
        yet (the DB wasn't readable) returns the dict without ``radios_recent``.
        The data's own freshness (``data_ts``) is judged separately by the
        daemon, since AngryOxide flushes its kismet DB in bursts."""
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
