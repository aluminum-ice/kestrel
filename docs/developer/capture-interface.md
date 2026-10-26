# Capture interface contract

`contract_version: 3`

This is the stable surface the capture service exposes for the plugin system and
the reinforcement-learning agent. It is the promise about what stays put, so
those consumers can build against it without reading the implementation. It is
versioned: a change that breaks a consumer bumps `contract_version`. Pinning
AngryOxide at v0.9.3 is what lets this stay stable; an AngryOxide bump is a
contract review, not a drop-in.

Version 2 added the best-effort in-range readout (`capture-status.json`, under
[Status](#status)); it is additive, so a v1 consumer that reads only the
hashline store is unaffected.

Version 3 corrects that readout's source and shape. AngryOxide (v0.9.3) leaves
the kismet DB's `devices` table empty and fills only the `packets` table, which
it flushes in bursts rather than live, so the v2 query (distinct recent
`devices`, split into `aps_recent`/`clients_recent`, keyed off wall-clock) read
empty or wrong. v3 derives a single `radios_recent` count from the `packets`
table, anchored to the DB's own newest packet, and adds `data_ts` so a consumer
can judge the data's freshness. `aps_recent`/`clients_recent` are gone; a v2
consumer that read them must move to `radios_recent`.

This file is the normative reference.

## Output

- **Persistent store:** `/var/lib/kestrel/capture/`. Consumers read
  hashlines here and nowhere else.
- **Layout** (set by `[capture.output] mode` in `/etc/kestrel/config.toml`):
  - `per-ssid` (default): one `<ssid>.hc22000` per network. The filename is the
    ESSID with every character outside `A-Za-z0-9._-` replaced by `_`, trimmed to
    64 characters. A handshake whose ESSID is unknown (PMKID-only or hidden) is
    written to `bssid-<AP-MAC>.hc22000` until the ESSID is learned; truly unknown
    lines go to `unknown.hc22000`.
  - `per-ap`: one `<ssid>_<bssid>.hc22000` per access point. The grouping key is
    the AP **BSSID** (field 4), the only field that uniquely identifies one AP
    radio — SSIDs are reused across unrelated APs, so `per-ssid` can merge them,
    while `per-ap` never does. The composite BSSID+ESSID is encoded in the name
    (each part sanitized as above), so one BSSID advertising multiple ESSIDs
    splits into one file per ESSID. A handshake with an empty/hidden ESSID routes
    to `__hidden_<bssid>.hc22000`; a line with no BSSID goes to `unknown.hc22000`.
    Both PMKID (`01`) and EAPOL (`02`) records for an AP share its one file.
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
  `/var/lib/kestrel/capture/.run/`. The run directory is an implementation
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
  AngryOxide's kismet DB (a run-dir artifact), so a consumer treats its
  absence/staleness as "unknown". Shape:

  ```json
  {"updated_at": 1730000000, "window_s": 300,
   "radios_recent": 23, "data_ts": 1729999700, "scanning": true}
  ```

  - `radios_recent` — distinct transmitting radios (by `sourcemac`) seen in the
    `packets` table within `window_s` seconds **of the DB's own newest packet**
    (`data_ts`), not of wall-clock. AngryOxide channel-hops and flushes the DB in
    bursts, so this is a sliding-window density indicator anchored to the latest
    data, not an instant count. It counts APs and clients together — the
    `packets` table does not separate them — so it runs higher than an APs-only
    count. May be absent when the kismet DB had no packets / wasn't readable that
    poll (still `scanning`, no count).
  - `data_ts` — epoch of the newest packet the count is drawn from. Because the
    DB lags real time, a consumer judges freshness from `data_ts` (e.g. shows "as
    of N ago", and past `[capture.environment] max_age_s` falls back to
    "unknown") rather than trusting `updated_at`, which only says the collector
    ran.
  - `scanning` — `false` (written once on a clean stop) or a stale `updated_at`
    both mean "not scanning"; the consumer then shows "unknown", never `0`.
  - Enabled and tuned by `[capture.environment]` (`enabled`, `window_s`,
    `max_age_s`, `thresholds`); the density labels (Quiet/Light/Moderate/Busy/
    Dense) are the consumer's, from `thresholds`, not part of the file.

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
