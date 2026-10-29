# Working with your captures

Once capture is running, the device writes what it catches to an on-device
store. This guide is about those files: what they are, where they live, how to
get them off the device, and how to turn them into recovered passwords.

See [Setting up capture](capture-setup.md) for turning capture on in the first
place.

## What you capture

The device captures **WPA handshakes and PMKIDs** and writes them as **hashcat
22000** lines — the modern `hc22000` format, one hashline per line, ready for
`hashcat -m 22000` or for upload to a cracking service. The device never stores
or recovers the password itself; it stores the material a cracker needs.

## Where it all lives

Everything is under the capture store, **`/var/lib/kestrel/capture/`**:

| Path | What it is |
| --- | --- |
| `*.hc22000` | Your handshakes. In the default **per-SSID** layout, one file per network (`<name>.hc22000`); in **per-AP** layout, one file per access point (`<name>_<bssid>.hc22000`); in **single** layout, one combined file (`all.hc22000`). |
| `bssid-<MAC>.hc22000` | (per-SSID) Handshakes whose network name isn't known yet (PMKID-only or hidden). |
| `__hidden_<MAC>.hc22000` | (per-AP) Handshakes from an access point with no/hidden network name. |
| `unknown.hc22000` | Handshakes that couldn't be attributed to an access point. |
| `*.gps.json` | A GPS location sidecar for a capture, when the `gps` plugin is providing fixes. |
| `cracked/wpa-sec.potfile` | Passwords recovered for your key, downloaded by the `wpa-sec` plugin. |
| `cracked/*.potfile` | Any potfile of known passphrases the device reads to annotate your captured networks: the `wpa-sec` download above, an `imported-<stamp>.potfile` from a dashboard upload or `kestrel recovered import`, or a file you copied in over SSH. Private (`0700` dir, `0600` files). The device never cracks anything — it only displays results you produced. |
| `geo/wigle-cache.json` | WiGLE geolocation lookups (display-only; never uploaded). |
| `archive/` | Snapshots made by housekeeping (archive/rotate). |
| `.run/` | Small working state (backup restore status, and raw capture files from older releases) — **not** your handshakes. Ignore it. Raw packet capture now lives outside the store, in `/var/lib/kestrel/capture-run/`. |

The store files are **append-only**: a reboot or a capture restart adds new
unique lines, never truncates. Nothing is deleted except by the housekeeping you
configure (below).

Whether you get one file per network, one file per access point, or one combined
file is the `[capture.output] mode` setting (`per-ssid`, `per-ap` or `single`) —
see [the config reference](config-reference.md). Choose `per-ap` when you need
one file to mean exactly one physical AP: network names are heavily reused (every
`AndroidAP`, every POS/printer default), so `per-ssid` can pile unrelated APs into
one file, whereas `per-ap` keys on the BSSID and keeps them apart.

## Seeing what you've got

- **The dashboard, no terminal needed.** The **Captured networks** page has two
  views you can toggle between: **by network** (one row per SSID, with its
  distinct-BSSID count and handshakes) and **by access point** (one row per
  BSSID, so APs that reuse a name stay separate; search also matches a pasted
  BSSID). It defaults to match your `[capture.output] mode`, but you can flip the
  view anytime — it reads the whole store either way, regardless of how each
  handshake was captured. Both are searchable and paginated, sortable by name
  (default) or by **most recently captured**, and show a **last seen** column
  (when the AP last produced a new handshake). Captures made before you updated
  to the release that added this show "last seen" from the file's timestamp, or
  "never". The dashboard card shows the network and access-point totals. If any
  of your networks' passphrases are known (see [Cracking them](#cracking-them)),
  the card also shows a **recovered N of M** count and each recovered row gets a
  **recovered** badge. The `session-stats` plugin card shows live counts for the
  current session.
- **From the terminal:** `kestrel capture status` reports the store size
  and how many capture files it holds.

## Getting the captures off the device

Three ways, depending on whether you want it automatic or one-off:

1. **The backup plugin (recommended, automatic).** The `backup` plugin mirrors
   the whole capture store — and the trained learning-agent model — to a remote
   host over SSH, incrementally and on a schedule. This is the durable option: a
   lost or reflashed card doesn't take your captures with it, and you can restore
   onto a fresh image. See [Remote backup](backup-setup.md).

2. **Copy them yourself with `scp`/`rsync` (one-off).** The store is readable by
   the device account, so from another machine:

   ```bash
   # pull the whole store (adjust the device's address):
   rsync -az pi@kestrel.local:/var/lib/kestrel/capture/ ./captures/
   # or a single network's file:
   scp pi@kestrel.local:/var/lib/kestrel/capture/MyNetwork.hc22000 .
   ```

3. **Upload for cracking (handshakes only).** The `wpa-sec` plugin uploads
   handshakes to a cracking service as they come in and downloads any recovered
   passwords. See [wpa-sec](wpa-sec-setup.md).

## Cracking them

An `hc22000` file is ready for either path:

- **Hand it to wpa-sec.** Enable the `wpa-sec` plugin and it uploads for you and
  pulls back the cracked passwords into `cracked/wpa-sec.potfile`. See
  [wpa-sec](wpa-sec-setup.md).
- **Crack it yourself with hashcat** on your own machine — the format is
  hashcat's WPA mode:

  ```bash
  hashcat -m 22000 MyNetwork.hc22000 wordlist.txt
  ```

  If a tool needs the older `.cap`/`.pcap` form, the image ships `hcxtools`
  (`hcxhash2cap`) to convert a hashline file back to a capture file.

Only crack and upload handshakes you are authorized to capture.

## Seeing which networks are recovered

However a passphrase was recovered — the `wpa-sec` plugin, hashcat on your own
machine, or anywhere else — the device can show it against the matching captured
network. It reads **potfiles** from `cracked/` in the store (above); it never
cracks anything itself.

- A recovered network's row on the **Captured networks** page gets a **recovered**
  badge, and the dashboard card shows a **recovered N of M access points** count.
  Neither is a secret, so both show whenever you can see the page.
- With the **web login on**, expand a recovered row to see its passphrase. To keep
  the badge and count but hide the passphrase — handy when screen-sharing — set
  `show_passphrases` to `false` on the **Settings** page (or in `config.toml`'s
  `[web]` table). The passphrase never shows when the login is off.

### Importing a potfile

Two formats are accepted, both carrying the BSSID, network name and passphrase
per line: the **wpa-sec download** (`bssid:station:essid:psk`) and the **hashcat
separated** potfile (`hash:bssid:station:essid:psk` — e.g. `hcxpsktool` output,
or `hashcat --outfile-format`). The default `hashcat.potfile` (the full `*`-hash
followed by the password) is **not** supported — export with a separated outfile
format, or upload a wpa-sec file.

- **Dashboard:** on the Captured networks page (web login on), open **Import
  recovered results** and choose the file.
- **Terminal:**

  ```bash
  kestrel recovered import results.potfile   # parse and merge into the store
  kestrel recovered list                     # counts by source (no passphrases)
  kestrel recovered clear --yes              # remove imported files (--source NAME for one)
  ```

Imports are size- and line-capped (`[recovered] max_import_mb` and
`max_import_lines` — see [the config reference](config-reference.md)). Only
import passphrases for networks you are authorized to test.

## Location tagging and wardriving

When the `gps` plugin is providing fixes, each capture gets a `*.gps.json`
sidecar recording where it was taken, and the `wigle` plugin can submit observed
networks with their coordinates to WiGLE. See [GPS](gps-setup.md) and
[WiGLE](wigle-setup.md).

When a network's location is known, it also shows on its row on the **Captured
networks** page: a small coordinate linking to OpenStreetMap, with a solid pin
for a location **recorded** by the onboard GPS and an *approximately* mark for
one **estimated** from an external source like WiGLE (a recorded fix wins when
both exist). Like a passphrase, it shows only with the web login on; set
`[web] show_locations = false` to keep a muted "location hidden" marker while
hiding the coordinates (for screen-sharing). It is display-only — the device
never looks a location up or sends one anywhere — and a network with no known
location shows nothing.

## Keeping the store from filling the card

The store only grows unless you let housekeeping manage it — and housekeeping
**never silently deletes** your handshakes. You can configure a daily
archive/rotate policy, or run it on demand:

```bash
sudo kestrel capture archive     # snapshot the store into archive/ and start fresh
sudo kestrel capture rotate      # apply the configured housekeeping policy now
```

The policy and its size/age limits are covered under housekeeping in [Setting up
capture](capture-setup.md). `capture clear --yes` wipes the live store (add
`--purge` to drop archives too) — only when you really mean it.
