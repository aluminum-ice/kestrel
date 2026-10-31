# Capture interface contract

`contract_version: 6`

This is the stable surface the capture service exposes for the plugin system and
the reinforcement-learning agent. It is the promise about what stays put, so
those consumers can build against it without reading the implementation. It is
versioned: a change that breaks a consumer bumps `contract_version`. Pinning
AngryOxide (v0.9.4) is what lets this stay stable; an AngryOxide bump is a
contract review, not a drop-in. (v0.9.3 to v0.9.4 changed only how AngryOxide
writes its kismet database, which nothing here reads, so it needed no new
contract version.)

Version 2 added the best-effort in-range readout (`capture-status.json`, under
[Status](#status)); it is additive, so a v1 consumer that reads only the
hashline store is unaffected.

Version 3 corrects that readout's source and shape. AngryOxide (v0.9.3) leaves
the kismet DB's `devices` table empty and fills only the `packets` table, which
it flushes in bursts rather than live, so the v2 query (distinct recent
`devices`, split into `aps_recent`/`clients_recent`, keyed off wall-clock) read
empty or wrong. v3 derives a single `radios_recent` count from the `packets`
table, anchored to the DB's own newest packet, and adds `data_ts` so a consumer
can judge the data's freshness. (Since 1.0.11 the collector reads the same count
from AngryOxide's pcapng instead of the kismet DB; the shape is unchanged, see
`radios_recent` below.) `aps_recent`/`clients_recent` are gone; a v2
consumer that read them must move to `radios_recent`.

Version 4 adds the best-effort per-AP seen index (`capture-seen.json`, under
[Status](#status)): the collector records when each BSSID last produced a new
hashline, so the networks page can sort by most-recently-captured. It is
additive — a consumer that reads only the hashline store is unaffected — and
best-effort: it is maintained going forward (handshakes already in the store
when v4 shipped are not back-stamped), so a missing entry means "unknown", not
"never seen".

Version 5 pins the per-radio **location** files (under [Geo](#geo-locations)): the
GPS sidecars a position producer writes next to each capture file, and the geo
caches an external-lookup producer writes under `geo/`. It is additive — a
consumer that reads only the hashline store is unaffected — and the files are
best-effort: a producer may write none, so an absent file means "no known
location", never an error. It formalizes an existing layout so a producer and
the core location reader agree without either importing the other.

Version 6 makes `per-ap` the shipped [layout](#output) (`[capture.output] mode`
in the shipped `config.toml` and every code fallback), so a freshly flashed
device names its store files `<ssid>_<bssid>.hc22000` instead of
`<ssid>.hc22000`. The layouts themselves are unchanged and `per-ssid` is still
supported; a device updated in place keeps whatever mode its `config.toml`
holds. A consumer that read every `*.hc22000` in the store (as the daemon, the
agent and the shipped plugins do) is unaffected; one that expected a file per
network name must read all files and group by the hashline's ESSID instead.

This file is the normative reference.

## Output

- **Persistent store:** `/var/lib/kestrel/capture/`. Consumers read
  hashlines here and nowhere else.
- **Layout** (set by `[capture.output] mode` in `/etc/kestrel/config.toml`):
  - `per-ap` (default since contract version 6): one `<ssid>_<bssid>.hc22000`
    per access point. The grouping key is the AP **BSSID** (field 4), the only field that uniquely identifies one AP
    radio — SSIDs are reused across unrelated APs, so `per-ssid` can merge them,
    while `per-ap` never does. The composite BSSID+ESSID is encoded in the name
    (each part sanitized as below), so one BSSID advertising multiple ESSIDs
    splits into one file per ESSID. A handshake with an empty/hidden ESSID routes
    to `__hidden_<bssid>.hc22000`; a line with no BSSID goes to `unknown.hc22000`.
    Both PMKID (`01`) and EAPOL (`02`) records for an AP share its one file.
  - `per-ssid`: one `<ssid>.hc22000` per network. The filename is the
    ESSID with every character outside `A-Za-z0-9._-` replaced by `_`, trimmed to
    64 characters. A handshake whose ESSID is unknown (PMKID-only or hidden) is
    written to `bssid-<AP-MAC>.hc22000` until the ESSID is learned; truly unknown
    lines go to `unknown.hc22000`.
  - `single`: one combined file named by `[capture.output] single_file`
    (default `all.hc22000`).
- **Format:** hashcat 22000 (`.hc22000`) lines, one hashline per line. The
  capture service does not rewrite the line; it routes it by parsing the ESSID
  and AP BSSID out of the standard
  `WPA*TYPE*PMKID_or_MIC*MAC_AP*MAC_STA*ESSID_hex*...` layout.
- **Append-only:** store files are only ever appended to and de-duplicated
  (exact-line), never truncated. A reboot or an attack restart adds new unique
  lines to the existing files. Archiving/clearing happens only via explicit
  housekeeping (below), never implicitly.
- **New-handshake signal:** a store `.hc22000` file is created or grows. The
  plugin system sources `on_handshake` from this. Prefer watching store files
  over parsing AngryOxide's terminal output.
- **Packet captures:** AngryOxide's `.pcapng` and kismet DB (and any other
  per-run artifacts) stay in the run directory
  `/var/lib/kestrel/capture-run/` (root-owned, outside the store, so the
  root-run AngryOxide never writes into a pi-owned directory; releases before
  this used the store's `.run/`, which the collector still drains). Those
  releases also ran the collector as root, so on its first start the launcher
  hands their root-owned store files, `.run/` and archive sets to `pi`
  (`capture-collect --adopt-store`), letting the `pi` collector append to
  them. The run
  directory is an implementation
  detail, not part of the stable surface; only the store above is contractual.
  It is also **size-bounded** (`[capture.housekeeping] max_run_mb`, on by
  default): the service clears stale raw files on start and rolls the capture
  when the dir passes the cap, so raw `.pcapng`/kismet are **transient** —
  extracted hashlines persist in the store, but a consumer must not assume any
  packet capture stays on disk. Read the store, not the run dir.

## Status

- **Service state:** `systemctl is-active kestrel-capture.service`.
- **Config and derived state:** `kestrel capture status` reports
  enabled/running, the resolved adapter and driver, the scan profile, the active
  whitelist, the authorized-use ack state, the store size and hashline-file
  count, and the archive-set count.
- **Live RF state** (APs/clients/channel/counts) comes from AngryOxide's own
  status output; its precise shape is not frozen. A consumer that needs it
  should treat it as best-effort and fall back to the store signal above.
- **In-range readout** (`capture-status.json`, added in v2, reshaped in v3): a
  small file the collector writes into the store each poll, so a consumer can
  show how busy the RF environment is without touching the transient run dir.
  Best-effort: it is present only while capture runs, and is derived from
  AngryOxide's pcapng capture file (a run-dir artifact), so a consumer treats
  its absence/staleness as "unknown". Shape:

  ```json
  {"updated_at": 1730000000, "window_s": 300,
   "radios_recent": 23, "data_ts": 1729999700, "scanning": true}
  ```

  - `radios_recent` — distinct transmitting radios (by each frame's
    transmitter address, 802.11 addr2) seen within `window_s` seconds **of the
    newest captured frame** (`data_ts`), not of wall-clock. AngryOxide
    channel-hops, so this is a sliding-window density indicator anchored to the
    latest data, not an instant count. It counts APs and clients together, so it
    runs higher than an APs-only count. The collector reads it from the tail of
    the run dir's pcapng files (the current launch's and, across an agent
    relaunch, the previous one's), parsing only the blocks added since its last
    poll. It no longer reads AngryOxide's kismet DB: AngryOxide commits one DB row
    per frame, which an SD card can't sustain in a busy RF environment, so the DB
    fell minutes behind real time and the card went "unknown" while capture kept
    working. May be absent before the first frame is written (still `scanning`,
    no count).
  - `data_ts` — epoch of the newest frame the count is drawn from. A consumer
    judges freshness from `data_ts` (e.g. shows "as of N ago", and past
    `[capture.environment] max_age_s` falls back to "unknown") rather than
    trusting `updated_at`, which only says the collector ran.
  - `scanning` — `false` (written once on a clean stop) or a stale `updated_at`
    both mean "not scanning"; the consumer then shows "unknown", never `0`.
  - Enabled and tuned by `[capture.environment]` (`enabled`, `window_s`,
    `max_age_s`, `thresholds`); the density labels (Quiet/Light/Moderate/Busy/
    Dense) are the consumer's, from `thresholds`, not part of the file.
- **Per-AP seen index** (`capture-seen.json`, added in v4): a small file the
  collector writes into the store recording, per BSSID, when that AP last (and
  first) produced a **new** unique hashline — so a consumer can sort/show
  captured APs by recency without a timestamp in the hashline itself. Shape:

  ```json
  {"version": 1,
   "aps": {"AA:BB:CC:DD:EE:FF": {"first": 1730000000, "last": 1730000500}}}
  ```

  - Keyed by BSSID as `AA:BB:CC:DD:EE:FF`; `last`/`first` are epoch seconds.
  - `last` is the time a **new, de-duplicated** hashline for that AP was drained,
    i.e. "last new handshake captured", **not** a live sighting — an AP re-seen
    producing an identical hashline does not advance it.
  - Best-effort and **going-forward**: only handshakes drained after v4 shipped
    are stamped, so a BSSID absent here is "unknown" (a consumer falls back to
    the store file's mtime, then to nothing), never "never captured".
  - Maintained only while the collector runs; granularity is one poll (~10 s).

## Geo (locations)

A captured radio's position, when it is known, is written to the store as one of
two file kinds and read back by the core location reader (`kestrel_pi/geo.py`,
surfaced on the dashboard's Captured-networks page). The store is the only
meeting point: a **producer** (today the `gps` and `wigle` plugins, but not
limited to them) writes these files; the **consumer** reads every matching file
and imports no producer. Both kinds are best-effort and optional — a device with
no position producer simply has none, and the reader shows nothing.

Every record carries a `source` string, which the reader maps to a **class**:

- `gpsd`, `serial`, `auto` → **recorded** (an onboard-GPS fix taken at capture
  time; precise, and it is where the operator was).
- any other value → **estimated** (an external lookup of where the radio is
  publicly known to be; e.g. `wigle-search`). An unrecognized `source` is
  treated as estimated, labelled from the string, so a new producer needs no
  reader change.

For one BSSID a **recorded** fix always wins over an estimated one; within a
class the **newest** timestamp wins (`fix_time` for recorded, `query_time` for
estimated). Coordinates are validated (finite, in range) and a bad record is
dropped.

- **Recorded sidecar** — `<capture-file>.gps.json`, next to its `.hc22000` file
  (so one sidecar per capture file; a BSSID may recur across sidecars). A map
  keyed by BSSID as `AA:BB:CC:DD:EE:FF`:

  ```json
  {"AA:BB:CC:DD:EE:FF": {"lat": 12.34567, "lon": -45.67890, "alt": 100.0,
                         "fix_time": "2026-01-01T00:00:00Z", "source": "gpsd"}}
  ```

  - Required: `lat`, `lon` (decimal degrees), `source`, `fix_time` (ISO-8601 Z).
    `alt` and any further keys (e.g. `fix`/`sats`/`cn0`) are optional and ignored
    by the location reader.
  - A handshake with no parseable BSSID is keyed `"unknown"`; the reader skips
    any non-MAC key, so that slot never becomes a location.

- **Estimated cache** — `geo/<name>-cache.json` (the reader reads **every**
  `geo/*.json`, so additional sources are additive by dropping another file). A
  map keyed by BSSID:

  ```json
  {"AA:BB:CC:DD:EE:FF": {"bssid": "AA:BB:CC:DD:EE:FF", "lat": 12.34567,
                         "lon": -45.67890, "source": "wigle-search",
                         "query_time": "2026-01-01T00:00:00Z"}}
  ```

  - Required: `lat`, `lon`, `source`. `query_time` (ISO-8601 Z) orders duplicates;
    `bssid` is redundant with the key and not required by the reader.
  - Display-only: an estimated location is never uploaded or acted on, and the
    reader never performs a lookup itself — that stays a producer's job.

## Control

- **What a consumer sets, and how:** capture is configured through
  `/etc/kestrel/config.toml`, applied when the service (re)starts:
  - `[capture] interface` — `auto` or an explicit `wlanX`.
  - `[capture] whitelist` — ESSIDs/MACs never actively attacked. Can also be
    pre-loaded from a `kestrel-whitelist.txt` staged on the boot partition
    (one entry per line), merged into this key on boot.
  - `[capture.scan] mode` — `sweep` | `autohunt` | `fixed`.
  - `[capture.scan] dwell_seconds` — seconds dwelled per channel.
  - `[capture.scan] bands` — `auto`, a single band, or an explicit channel list.
  - `[capture.output] mode` / `single_file` — store layout.
  - `[capture.angryoxide] headless` / `extra_args` — run options.
- **Config values are the agent's initial policy:** the `[capture.scan]` settings
  (`mode`, `dwell_seconds`, `bands`) are sensible defaults and the human override;
  the learning agent warm-starts from them and may supersede them at runtime,
  persisting its learned values separately rather than rewriting `config.toml`.
- **Granularity:** in v1 control is **launch-time**. A change takes effect on
  the next service start (`systemctl restart kestrel-capture.service`).
  Live, per-decision control (changing channel or dwell mid-run without a
  restart) is **not** guaranteed in v1.
- **First consumer:** the default `sweep` profile is itself the first consumer of
  this control surface — a scripted baseline that rotates channels and dwells.
  The learning agent makes the same channel/dwell choices here in its place. If
  the agent needs finer-grained live control, it drives capture through
  restart-with-new-config, or this contract gains a richer control channel under
  a new `contract_version`. That two-way door is the reason the surface is
  versioned now.

## Stability

- Stable in v1: the store path and per-SSID/single layout, the append-only and
  dedup guarantee, the `.hc22000` format, the new-handshake signal, and the
  config keys above with their meanings.
- Not frozen in v1: the run-directory contents, AngryOxide's live status shape,
  and anything requiring live per-decision control.
