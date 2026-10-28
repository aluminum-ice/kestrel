"""The recovered-networks core service: known Wi-Fi passphrases, from potfiles.

When a captured network's passphrase is known -- because it was recovered, wherever
the crack happened -- this surfaces it so the UI can annotate that network. The
feature is **plugin-agnostic**: the contribution point is the directory
``<store>/cracked/``, and *any* ``*.potfile`` in it is a source. The ``wpa-sec``
plugin writes ``wpa-sec.potfile`` there, a manual upload or CLI import writes
``imported-<stamp>.potfile`` there, and a file copied in over SSH is picked up
with no further action. Nothing here imports a plugin, and nothing depends on a
plugin being loaded.

This is strictly for the documented offline analysis of networks you own or are
authorized to test (see ``CLAUDE.md``, "Authorized use only"): it displays
results the operator already produced and never cracks, deauthenticates, or acts
on a network. Passphrases are secrets -- the recovered dir is ``0700``, every file
``0600``, owned by ``pi`` -- so the daemon (also ``pi``) reads them and no root
step is involved.

Two colon-delimited potfile layouts are read, both carrying the BSSID and ESSID
directly so neither needs the ``.hc22000`` hash decoded::

    hashcat separated (5 fields):  <32-hex PMKID/MIC hash>:<BSSID>:<STA_MAC>:<ESSID>:<PSK>
    wpa-sec download   (4 fields):  <BSSID>:<STA_MAC>:<ESSID>:<PSK>

Parsing is anchored on the **first adjacent pair of MAC-shaped fields** (each
exactly 12 hex once separators are stripped), not on the field count: the 32-hex
hash that leads the hashcat line is not MAC-shaped, so the same rule finds the
BSSID/STA pair at fields 2-3 there and at fields 1-2 in the wpa-sec line. The
password is the **last** field (so an ESSID may itself contain ``:``); the ESSID
is everything between the station MAC and that final field. A line with no
adjacent MAC pair, or nothing after the station MAC, is skipped -- which is how
the *default* ``hashcat.potfile`` line (``<WPA* hash>:<password>``, no MAC pair)
is skipped (supporting it would need a second parser that decodes the WPA* hash;
deferred, see ``docs/developer/deferred.md``).
"""

from __future__ import annotations

import os
import threading
import time
from dataclasses import dataclass

from .capture import _format_mac

# Where sources live, relative to the capture store, and the suffix that marks
# one. The on-disk directory name stays ``cracked/`` even though the feature is
# "recovered networks": the wpa-sec plugin already writes wpa-sec.potfile there,
# so renaming the directory would be a capture-store layout change for no gain.
STORE_SUBDIR = "cracked"
POTFILE_SUFFIX = ".potfile"

# Default ingest/parse caps. The web upload and CLI import make these
# configurable via the ``[recovered]`` config table and pass the resolved values
# in; the module itself reads no config, so it stays pure and unit-testable.
DEFAULT_IMPORT_MB = 2
DEFAULT_IMPORT_LINES = 50_000

# A synthetic station MAC written into imported potfiles. The parser needs an
# adjacent MAC pair, and an import keeps only (BSSID, ESSID, PSK) -- the original
# station MAC is dropped on the way in -- so a canonical zero MAC stands in. It
# round-trips cleanly because the parser ignores the station MAC.
_ZERO_STA = "000000000000"

_HEX = frozenset("0123456789abcdefABCDEF")


def _normalize_mac(field):
    """The bare lower-hex form of a MAC-shaped field, or ``""`` if not MAC-shaped.

    Accepts bare 12-hex and hyphen/dot-separated forms (``aa-bb-...`` or Cisco
    ``aabb.ccdd.eeff``). Colon-separated MACs cannot occur mid-line here because
    the line is split on ``:`` before a field reaches this function."""
    stripped = field.strip().replace("-", "").replace(".", "").replace(":", "")
    if len(stripped) == 12 and all(c in _HEX for c in stripped):
        return stripped.lower()
    return ""


@dataclass
class RecoveredRecord:
    """One known passphrase. ``source`` is the potfile basename it came from, for
    display/debugging -- never a secret."""

    bssid: str   # normalized AA:BB:CC:DD:EE:FF
    essid: str   # as captured (not decoded); "" when hidden/absent in the source
    psk: str     # never contains ':' (it is the last ':'-split field)
    source: str = ""


def parse_potfile_line(line):
    """Parse one potfile line into a :class:`RecoveredRecord` (``source`` left
    blank; the caller fills it). Returns ``None`` for a blank/garbage line or one
    with no adjacent MAC pair / nothing after the station MAC.

    The password is the last field and therefore never contains ``:``; an ESSID
    containing ``:`` is preserved (it is everything between the station MAC and
    the password). A password that itself contained ``:`` in the source would
    have only its final ``:``-separated segment kept -- an inherent limit of
    these colon-delimited formats, matched on write so records round-trip."""
    line = line.strip()
    if not line:
        return None
    fields = line.split(":")
    anchor = None
    for i in range(len(fields) - 1):
        if _normalize_mac(fields[i]) and _normalize_mac(fields[i + 1]):
            anchor = i
            break
    if anchor is None:
        return None
    bssid = _format_mac(_normalize_mac(fields[anchor]))
    if not bssid:
        return None
    rest = fields[anchor + 2:]
    if not rest:
        return None
    psk = rest[-1]
    essid = ":".join(rest[:-1])
    if not psk and not essid:
        return None
    return RecoveredRecord(bssid=bssid, essid=essid, psk=psk, source="")


def _dedup_key(rec):
    """Records are deduped on (BSSID, ESSID, PSK), ignoring the station MAC: the
    same AP recurs across many potfile lines with differing station MACs but one
    passphrase, and those are one record."""
    return (rec.bssid, rec.essid, rec.psk)


class RecoveredStore:
    """Read-only view of the recovered potfiles under ``<store>/cracked/``.

    Owned by the daemon and read by the web layer; never imports a plugin. The
    data is the directory, not any plugin API, so the feature does not depend on
    a plugin being loaded. Parsing is cached and only re-done when a potfile's
    name/size/mtime changes -- the same signature-cache pattern
    :class:`~kestrel_pi.capture.CaptureInterface` uses -- so rendering a page is
    cheap no matter how often it is asked.
    """

    def __init__(self, store_dir, max_bytes=None, max_lines=None):
        self.store_dir = store_dir
        self.recovered_dir = os.path.join(store_dir, STORE_SUBDIR)
        self._max_bytes = (DEFAULT_IMPORT_MB * 1024 * 1024
                           if max_bytes is None else int(max_bytes))
        self._max_lines = (DEFAULT_IMPORT_LINES
                           if max_lines is None else int(max_lines))
        # (signature, by_bssid, by_essid, records); guarded because the web
        # server is threaded.
        self._cache = None
        self._lock = threading.Lock()

    def _potfiles(self):
        """Every ``*.potfile`` in the recovered dir (sorted), or ``[]``."""
        try:
            names = os.listdir(self.recovered_dir)
        except OSError:
            return []
        return sorted(os.path.join(self.recovered_dir, n)
                      for n in names if n.endswith(POTFILE_SUFFIX))

    def _read_lines(self, path):
        """Lines of one potfile, byte- and line-capped, decoded with replacement.
        A read error yields ``[]`` -- a hostile or truncated file never raises."""
        try:
            with open(path, "rb") as handle:
                raw = handle.read(self._max_bytes)
        except OSError:
            return []
        text = raw.decode("utf-8", "replace")
        return text.splitlines()[:self._max_lines]

    def _parse(self):
        """Parse every potfile once into ``(by_bssid, by_essid, records)``.

        ``by_bssid`` maps a BSSID to its first record (the by-AP lookup);
        ``by_essid`` maps a non-empty ESSID to the list of its distinct records
        (the by-network fallback -- different BSSIDs under one name may carry
        different passphrases, so it is a list, not one record); ``records`` is
        the deduped list in first-seen order. Cached on the dir's file signature
        and reused until a potfile changes."""
        paths = self._potfiles()
        signature = []
        for path in paths:
            try:
                st = os.stat(path)
            except OSError:
                continue
            signature.append((path, st.st_size, st.st_mtime_ns))
        signature = tuple(signature)
        with self._lock:
            cached = self._cache
        if cached is not None and cached[0] == signature:
            return cached[1], cached[2], cached[3]

        by_bssid = {}
        by_essid = {}
        records = []
        seen = set()
        for path in paths:
            source = os.path.basename(path)
            for line in self._read_lines(path):
                rec = parse_potfile_line(line)
                if rec is None:
                    continue
                rec.source = source
                key = _dedup_key(rec)
                if key in seen:
                    continue
                seen.add(key)
                records.append(rec)
                by_bssid.setdefault(rec.bssid, rec)
                if rec.essid:
                    by_essid.setdefault(rec.essid, []).append(rec)

        with self._lock:
            self._cache = (signature, by_bssid, by_essid, records)
        return by_bssid, by_essid, records

    def lookup_bssid(self, bssid):
        """The :class:`RecoveredRecord` for ``bssid`` (normalized before lookup),
        or ``None``. The exact, unambiguous by-AP join."""
        if not bssid:
            return None
        key = _format_mac(_normalize_mac(bssid))
        if not key:
            return None
        by_bssid, _by_essid, _records = self._parse()
        return by_bssid.get(key)

    def lookup_essid(self, essid):
        """The list of :class:`RecoveredRecord`\\ s whose ESSID matches ``essid``
        exactly (empty list if none). The by-network fallback for a recovered
        record whose BSSID matches no captured AP; a list because one name may
        span several BSSIDs with different passphrases."""
        if not essid:
            return []
        _by_bssid, by_essid, _records = self._parse()
        return list(by_essid.get(essid, ()))

    def counts(self):
        """``{"recovered_aps", "recovered_essids"}`` -- distinct recovered BSSIDs and
        distinct non-empty recovered ESSIDs across every potfile. A diagnostic
        total (it counts records with no matching captured AP too); the
        dashboard's "recovered N of M" N is computed at the capture join, not
        here."""
        by_bssid, by_essid, _records = self._parse()
        return {"recovered_aps": len(by_bssid), "recovered_essids": len(by_essid)}

    def all(self):
        """Every distinct recovered record (deduped, first-seen order)."""
        _by_bssid, _by_essid, records = self._parse()
        return list(records)

    def ingest(self, text, *, max_lines=None):
        """Parse ``text`` and merge its records into the recovered dir, returning
        ``{"imported": N, "skipped": M}`` (see :func:`ingest_text`). The caller
        (web upload / CLI) has already enforced the byte cap; ``max_lines`` falls
        back to this store's configured line cap. The next read re-parses, since
        a new potfile changes the directory signature."""
        return ingest_text(
            self.store_dir, text,
            max_lines=self._max_lines if max_lines is None else max_lines)


def _unique_import_path(recovered_dir):
    """A fresh ``imported-<UTC-stamp>.potfile`` path opened ``O_EXCL`` so a
    concurrent import can never clobber another's file. Returns the open fd and
    the path; the caller writes and closes. The name is device-generated, never
    taken from an upload, so an upload can't escape the dir."""
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    for attempt in range(100):
        suffix = "" if attempt == 0 else "-%d" % attempt
        path = os.path.join(recovered_dir, "imported-%s%s%s"
                            % (stamp, suffix, POTFILE_SUFFIX))
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            continue
        return fd, path
    raise OSError("could not create a unique import file in %s" % recovered_dir)


def ingest_text(store_dir, text, *, source_label="imported",
                max_lines=DEFAULT_IMPORT_LINES):
    """Parse ``text`` (already byte-capped and decoded by the caller) and write
    the parseable records to a new ``0600`` potfile in ``<store>/cracked/``,
    owned by whoever runs this (always ``pi`` -- the web daemon or the CLI).

    Returns ``{"imported": N, "skipped": M}``: ``N`` distinct records written,
    ``M`` non-blank lines that did not parse (including lines past ``max_lines``).
    Nothing is written when no line parses. The record set is deduped and written
    in canonical 4-field form (``<bssid>:<zero-sta>:<essid>:<psk>``, bare-hex
    MACs) so :class:`RecoveredStore` re-parses it identically. ``source_label`` is
    accepted for symmetry with future sources but the on-disk name is always
    ``imported-<stamp>.potfile``.
    """
    records = []
    seen = set()
    skipped = 0
    for i, line in enumerate(text.splitlines()):
        if i >= max_lines:
            if line.strip():
                skipped += 1
            continue
        rec = parse_potfile_line(line)
        if rec is None:
            if line.strip():
                skipped += 1
            continue
        key = _dedup_key(rec)
        if key in seen:
            continue
        seen.add(key)
        records.append(rec)

    if not records:
        return {"imported": 0, "skipped": skipped}

    recovered_dir = os.path.join(store_dir, STORE_SUBDIR)
    os.makedirs(recovered_dir, mode=0o700, exist_ok=True)
    os.chmod(recovered_dir, 0o700)
    fd, _path = _unique_import_path(recovered_dir)
    os.fchmod(fd, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        for rec in records:
            bare = rec.bssid.replace(":", "").lower()
            handle.write("%s:%s:%s:%s\n" % (bare, _ZERO_STA, rec.essid, rec.psk))
    return {"imported": len(records), "skipped": skipped}
