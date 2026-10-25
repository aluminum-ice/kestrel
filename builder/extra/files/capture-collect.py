#!/usr/bin/env python3
"""Merge AngryOxide hashlines into the append-only kestrel store.

The capture launcher runs this alongside AngryOxide. AngryOxide writes its
per-run artifacts (pcapng, hashlines, logs) into the run directory; this watches
for *.hc22000 files there and appends each unique hashline to the persistent
store, routing by SSID in "per-ssid" mode (one <ssid>.hc22000 per network) or to
one combined file in "single" mode.

The store is only ever appended to and de-duplicated, never truncated, so
handshakes survive reboots and attack restarts. Dedup is exact-line and seeded
from the existing store at start, so a restart does not re-append old lines.
See docs/developer/capture-interface.md.
"""
import glob
import json
import os
import re
import sqlite3
import sys
import time

RUN_DIR = os.environ.get(
    "PWN_CAPTURE_RUN_DIR", "/var/lib/kestrel/capture/.run"
)
STORE_DIR = os.environ.get("PWN_CAPTURE_DIR", "/var/lib/kestrel/capture")
CONFIG = os.environ.get("PWN_CONFIG", "/etc/kestrel/config.toml")
POLL_SECONDS = int(os.environ.get("PWN_CAPTURE_POLL", "10"))

# The small best-effort "what's in range" file this writes into the store next to
# the hashlines (contract_version 3, docs/developer/capture-interface.md). It is
# NOT an .hc22000 file, so the store's hashline readers never pick it up.
STATUS_NAME = "capture-status.json"
# Distinct transmitting radios seen in the most recent ENV_WINDOW_DEFAULT seconds
# OF CAPTURE DATA (anchored to the newest packet, not wall-clock) is the density
# signal. AngryOxide flushes the kismet DB in bursts, so a window of a few
# minutes reads stably rather than coming back empty. Config can override it.
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


def output_config():
    """(mode, single_file) from config, with safe defaults."""
    mode, single = "per-ssid", "all.hc22000"
    out = _capture_config().get("output", {}) or {}
    mode = out.get("mode", mode)
    single = out.get("single_file", single)
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


def target_for(line, mode, single):
    """Store filename for one hc22000 line.

    hc22000 layout: WPA*TYPE*PMKID_or_MIC*MAC_AP*MAC_STA*ESSID_hex*...
    Route by ESSID; fall back to the AP BSSID when the ESSID is unknown
    (PMKID-only / hidden network).
    """
    if mode == "single":
        return single
    parts = line.split("*")
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


def _newest_kismet_db():
    """Path of the most recent ``*.kismet`` (kismetdb) in the run dir, or None.

    AngryOxide writes the kismet DB (and pcapng) into the run dir under the
    ``--output`` prefix. It is transient (the run dir is size-bounded and
    cleared), so this is best-effort by design."""
    try:
        dbs = glob.glob(os.path.join(RUN_DIR, "*.kismet"))
    except OSError:
        return None
    if not dbs:
        return None
    try:
        return max(dbs, key=os.path.getmtime)
    except OSError:
        return None


# A sourcemac that carries no real transmitter (null or the all-zero address) is
# excluded so it never inflates the density count.
_ZERO_MAC = "00:00:00:00:00:00"

# How long a read may wait for AngryOxide to release the DB lock, and how many
# times to retry a lock before giving up. AngryOxide writes the kismet DB
# continuously in a dense environment and briefly holds its write lock at each
# commit; a too-short timeout made the read fail and the card sit at "scanning"
# forever even while capture was healthy. Overridable for tests / tuning.
ENV_DB_TIMEOUT = float(os.environ.get("PWN_CAPTURE_DB_TIMEOUT", "5.0"))
ENV_DB_RETRIES = int(os.environ.get("PWN_CAPTURE_DB_RETRIES", "3"))

# Returned by environment_snapshot when the DB exists but stayed locked for the
# whole retry budget - distinct from None ("nothing to read yet") so the caller
# keeps the last good reading instead of blanking the card to "scanning".
_BUSY = object()


def environment_snapshot(window_s, now=None, timeout=None, retries=None):
    """Distinct transmitting radios seen in the most recent ``window_s`` seconds
    of capture data.

    AngryOxide fills the kismet DB's ``packets`` table (it leaves ``devices``
    empty) and flushes it in bursts, so this counts distinct ``sourcemac`` within
    ``window_s`` of the DB's OWN newest packet timestamp - never wall-clock
    ``now`` - so a flush lag reads as "recent" rather than zero. Returns
    ``(radios_recent, data_ts)`` where ``data_ts`` is that newest packet's epoch
    (so the reader can judge freshness); ``None`` when there is no DB/packets yet
    or the DB is structurally unreadable; or the ``_BUSY`` sentinel when the DB
    exists but AngryOxide held it locked for the whole retry budget. Opened
    read-only with a busy timeout (retried on a lock) so it tolerates AngryOxide
    writing without ever blocking it; ``now`` is accepted for tests but unused."""
    db_path = _newest_kismet_db()
    if not db_path:
        return None
    if timeout is None:
        timeout = ENV_DB_TIMEOUT
    if retries is None:
        retries = ENV_DB_RETRIES
    attempts = max(1, retries)
    for attempt in range(attempts):
        conn = None
        try:
            conn = sqlite3.connect(
                "file:%s?mode=ro" % db_path, uri=True, timeout=timeout
            )
            data_ts = conn.execute(
                "SELECT MAX(ts_sec) FROM packets").fetchone()[0]
            if data_ts is None:
                return None
            cutoff = int(data_ts) - window_s
            radios = conn.execute(
                "SELECT COUNT(DISTINCT sourcemac) FROM packets "
                "WHERE ts_sec >= ? AND sourcemac IS NOT NULL AND sourcemac != ?",
                (cutoff, _ZERO_MAC),
            ).fetchone()[0]
            return int(radios or 0), int(data_ts)
        except sqlite3.OperationalError as exc:
            # A busy/locked DB is transient: AngryOxide holds the write lock
            # only while committing. Back off and retry rather than give up.
            # Any other operational error (e.g. a missing table) is structural.
            msg = str(exc).lower()
            if "lock" not in msg and "busy" not in msg:
                return None
            if attempt + 1 < attempts:
                time.sleep(0.25 * (attempt + 1))
        except sqlite3.Error:
            return None
        finally:
            if conn is not None:
                conn.close()
    return _BUSY


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


def update_environment(window_s):
    """Refresh the in-range readout once. Writes ``radios_recent`` and
    ``data_ts`` (the newest-packet epoch, so the reader can judge freshness)
    when the kismet DB is readable. When the read does not yield a fresh count -
    the DB is momentarily locked by AngryOxide's writing (``_BUSY``), or it is
    empty/rotated (``None``) - the previous reading is carried through (only its
    freshness ages, via ``data_ts``), so the card does not blank to "scanning"
    on every transient gap. The empty/rotated case is the common one: the
    launcher clears the run dir and starts a fresh kismet DB on every AngryOxide
    relaunch, and the learning agent relaunches it each time it picks a new scan
    plan, so a brand-new DB with no packets yet is routine and must not flap the
    card. The carried reading still ages out to "unknown" once ``data_ts`` passes
    the reader's ``max_age_s`` (so a capture that truly stopped is not shown as a
    stale count). Only a cold start with no prior reading writes the
    scanning-but-no-count marker."""
    snap = environment_snapshot(window_s)
    payload = {"updated_at": int(time.time()), "window_s": window_s,
               "scanning": True}
    if snap is not None and snap is not _BUSY:
        payload["radios_recent"], payload["data_ts"] = snap
    else:
        # No fresh count this tick (DB locked, or empty right after a relaunch):
        # keep the last good reading rather than dropping it. Its freshness ages
        # via data_ts, so the reader blanks it once it is genuinely too old.
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

    while True:
        if env_enabled:
            update_environment(env_window)
        try:
            sources = [n for n in os.listdir(RUN_DIR) if n.endswith(".hc22000")]
        except FileNotFoundError:
            sources = []
        for name in sources:
            try:
                with open(
                    os.path.join(RUN_DIR, name),
                    "r",
                    encoding="utf-8",
                    errors="replace",
                ) as handle:
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
            for target, new_lines in pending.items():
                dest = os.path.join(STORE_DIR, target)
                try:
                    with open(dest, "a", encoding="utf-8") as out:
                        out.write("\n".join(new_lines) + "\n")
                except OSError as exc:
                    sys.stderr.write("capture-collect: %s: %s\n" % (dest, exc))
        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
    finally:
        # On stop, mark the readout not-scanning so the UI shows "unknown"
        # immediately rather than the last count going stale.
        write_status({"updated_at": int(time.time()), "scanning": False})
