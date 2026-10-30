# Deferred improvements

Known improvements that were consciously put off, so they aren't lost. Each
entry says what it is, why it waited, what doing it involves, and what should
trigger picking it up.

How to use this file:

- **Defer something:** add an entry in the same change that decides to defer it,
  under the area it belongs to. Write it impersonally (what and why, not who).
- **Do something:** remove its entry in the same change that does it, and say so
  in the PR description.
- Keep entries short; link to the code or doc that has the detail.
- **Never record a security weakness here** (a vulnerability, an unfixed
  review finding, a way to attack or abuse a feature), or anywhere else in the
  repo: it is public. Keep those in a private tracker outside the repository,
  and report new ones as [SECURITY.md](../../SECURITY.md) describes.

## In-place updates (`updates` plugin and update engine)

### Remove systemd units a release drops

- **What:** the engine removes shipped plugins, lib scripts and operator
  scripts that a release no longer carries, but leaves dropped unit files in
  place (`removal_plan` in `builder/extra/files/update-apply.py`).
- **Why deferred:** removing a unit safely means disabling (and stopping) it
  first, and a rollback would then have to re-enable it.
- **What to do:** record units and their enable state in the install list,
  `systemctl disable --now` a dropped unit before removing it, and restore both
  on rollback.
- **Pick up when:** the first release that renames or drops a unit.

### Kill the whole backup process group on a pre-update-backup timeout

- **What:** the bounded pre-update backup (`run_backup` in
  `builder/extra/files/update-apply.py`, `backup_timeout_seconds`) stops *waiting*
  after the timeout and lets the update proceed to cancel/restart, but
  `subprocess` kills only the direct child (`kestrel backup now`), not the
  `rsync`/`ssh` grandchild it spawned. A doomed transfer can keep running in the
  background until it dies on its own (broken pipe / its own timeout).
- **Why deferred:** the operator-visible symptom — the dashboard held down for
  many minutes — is fixed (the daemon restarts at the timeout); the lingering
  rsync is harmless and short-lived, so a clean process-group kill was not worth
  widening the generic `run()` wrapper for right now.
- **What to do:** run the backup in its own session/process group
  (`start_new_session=True`) and, on `TimeoutExpired`, signal the whole group so
  the rsync/ssh child is torn down with its parent.
- **Pick up when:** the lingering transfer is shown to cause real trouble (e.g.
  two overlapping backups contending), or the `run()` wrapper gains process-group
  handling for another reason.

### Don't make beta testers reflash for a stable release after a reflash beta

- **What:** a stable release's manifest is built against the previous stable
  release, so if any beta in the cycle needed a reflash, the stable release is
  reflash too — even for devices that already flashed that beta.
- **Why deferred:** conservative and correct; it only costs beta testers an
  unnecessary flash.
- **What to do:** have the Release workflow also record, per stable release, the
  oldest version it can be applied on in place (`min_update_from`, already
  honoured by the plugin and `--verify`) computed across the betas.
- **Pick up when:** beta cycles with reflash betas become routine.

### One implementation of the update decision

- **What:** the plugin (`plugins/updates.py`, as pi) and the engine's `--verify`
  (`update-apply.py`, as root) each implement version ordering and the
  skipped-reflash check. A parity test covers `version_key` only.
- **Why deferred:** root must not import pi-writable code (the plugin imports
  from the pi venv), so sharing needs a stdlib-only module installed root-owned
  and loaded by both.
- **What to do:** move the shared logic into such a module (installed as a lib
  script), have both sides use it, and drop the duplicate.
- **Pick up when:** either side's decision logic next changes.

## Capture output

### Make `per-ap` the default output mode

- **What:** `[capture.output] mode` ships `per-ssid` but supports `per-ap`, which
  keys each `.hc22000` file on the AP BSSID so unrelated APs that reuse a network
  name (every `AndroidAP`, POS/printer/router default) are never merged into one
  file. `per-ap` is the more correct default; it is only opt-in today to avoid
  forcing a reflash.
- **Why deferred:** the default lives in `builder/extra/files/config.toml`, which
  the in-place update engine skips (operator config), so changing it would reach
  only freshly flashed devices and silently diverge from app-layer-updated ones.
  That makes the default-flip reflash-class on its own — too much to justify for
  this change alone.
- **What to do:** set `mode = "per-ap"` in `config.toml`, bump
  `contract_version` 3→4 in `docs/developer/capture-interface.md` (the filenames
  every consumer sees change), and update the mode's "default" wording in
  `config_catalog.py`, the user guides and `GUIDE_MD`. Force `update_kind=reflash`
  for that release — `auto` does not watch `config.toml`, so it would mislabel it
  app-layer.
- **Pick up when:** the next release that is already reflash-class for a larger
  reason, so this rides along at no extra reflash cost.

### Confirm the collector can still read the Kismet database

- **What:** the capture collector runs as `pi` and reads AngryOxide's Kismet
  database read-only from `/var/lib/kestrel/capture-run`. If that database uses
  SQLite WAL mode, the read-only open can fail and the dashboard's Environment
  card stays at "scanning" while capture keeps working.
- **Why deferred:** needs a device to check.
- **What to do:** check on a device; if the read fails, read a copy of the
  database instead.
- **Pick up when:** before the next release.

## Recovered networks (`kestrel_pi.recovered`)

### Support the default `hashcat.potfile` format

- **What:** the parser reads the wpa-sec download (`bssid:sta:essid:psk`) and the
  hashcat **separated** potfile (`hash:bssid:sta:essid:psk`), both of which carry
  the BSSID and ESSID in the clear. It does not read hashcat's *default*
  `hashcat.potfile`, whose line is `<full WPA* 22000 hash>:<password>` with the
  ESSID/BSSID hex-encoded inside the `*`-delimited hash; such a line has no
  adjacent MAC pair, so it is skipped. The import UI says to export with a
  separated outfile format instead.
- **Why deferred:** reading it means a second parser that decodes the `WPA*`
  hash to recover the BSSID/ESSID — more surface for a format the operator can
  avoid by exporting differently.
- **What to do:** add a WPA*-hash decoder behind the same `RecoveredStore` (a line
  with one long hash field then a password), feeding the same `RecoveredRecord`;
  drop the "not supported" note from the import UI.
- **Pick up when:** operators ask for it, or a common tool only emits the default
  potfile.

### Show imported-but-not-captured records

- **What:** a recovered record whose BSSID matches no captured access point is not
  shown on `/networks` (there is nothing to annotate); it still counts in
  `RecoveredStore.counts()`. Such records are simply invisible today.
- **Why deferred:** needs its own small "imported, not captured" list/section and
  a decision on how it reconciles when the network is later captured; out of
  scope for annotating captures.
- **What to do:** add an opt-in list of unmatched recovered records (counts are
  already available), or fold them in once their BSSID is captured.
- **Pick up when:** an operator imports results for networks they have not yet
  captured and wants to see them.

### Retire the `wpa-sec` plugin's cracked page

- **What:** the `wpa-sec` plugin still renders its own `/plugins/wpa-sec/cracked`
  page. The core recovered service now reads `wpa-sec.potfile` like any other
  source and shows it on `/networks`, so the plugin page is redundant.
- **Why deferred:** removing it is a behaviour change (a link some operators may
  use) best done on its own, not folded into adding the core feature.
- **What to do:** drop the plugin's `on_webhook` cracked page (and its `ui`/row
  button), or redirect it to `/networks`; update the plugin's docs and
  `GUIDE_MD`.
- **Pick up when:** the unified `/networks` view has shipped and settled.

## Network locations (`kestrel_pi.geo`)

### Surface the located-APs count on the dashboard and e-ink

- **What:** `status()["capture"]` carries a `located_aps` figure (captured APs
  with a known location, joined like `recovered_aps`), but nothing shows it yet —
  the dashboard navcard shows only the recovered count, and the e-ink face does
  not read it. The per-row location on `/networks` is the only surface today.
- **Why deferred:** the per-row display is the feature; a "located N of M" navcard
  line and an e-ink readout are additive polish, and the field is already in
  `status()` so no plumbing is lost by waiting.
- **What to do:** render `located_aps` on the Captured-networks navcard next to
  the recovered line (`_render_status`), and optionally feed it to the e-ink
  face.
- **Pick up when:** the per-row display has settled, or an operator asks for the
  at-a-glance count.

## Ghost user-guide publisher (`tools/ghost-guide`)

### Remove superseded screenshots from Ghost

- **What:** each publish uploads any screenshot whose pixels changed, and the
  replaced image stays in Ghost's media library.
- **Why deferred:** Ghost's Admin API has no image delete, so it means working
  on the server's content directory or by hand in Ghost, outside the API key's
  reach.
- **What to do:** periodically delete `content/images/**/<figure key>-N.png`
  files no page references, on the Ghost host.
- **Trigger:** the media library grows enough to matter.

### Run the publisher's tests in CI

- **What:** `python3 -m pytest tools/ghost-guide` (renderer, framing, pixel
  comparison) runs only on a desktop.
- **Why deferred:** the Test workflow installs only what `make test` needs; this
  adds markdown-it-py (the browser isn't needed for these tests).
- **What to do:** add a step to `.forgejo/workflows/test.yml` that installs
  `markdown-it-py` and `Pillow` and runs the tool's tests.
- **Trigger:** the tool's renderer starts changing often.

## E-ink face (`eink` plugin)

### Rename `eink` to name the HAT it drives

- **What:** rename the plugin `eink` to `eink-ws27`, matching `eink-ws213` (the
  2.13" HAT's plugin), so e-ink plugins are named `eink-ws<size>` and fit a
  phone's Plugins row. That renames `plugins/eink.py`, its helper files
  (`_eink_*.py` become `_eink-ws27_*.py`), the `[plugins.eink]` table
  with its `faces`/`voices` sub-tables, and the plugin's mentions in the docs,
  `GUIDE_MD`, the tests, `tools/beta-check/`, `tools/ghost-guide/` and
  `scripts/pull-eink-diagnostics.sh` (its `--plugin` default).
  At the same time, give `variant` (`auto`/`v2`/`v1`) and `rotation`
  (0/90/180/270) `choices`, like `eink-ws213`'s.
- **Why deferred:** the plugin name is its config table, so the rename changes
  the shipped `config.toml`, which makes the release reflash-class: the update
  engine leaves operator config alone, so an updated device would keep the old
  table and the face would stop. `choices` waits for the same release: a
  stored value outside them (such as `variant = "V2"`, accepted today) would
  disable the plugin on a device updated in place.
- **What to do:** make the rename in a release that is reflash-class for
  another reason, say in its notes to enable `eink-ws27` again after
  reflashing, and keep the Ghost guide fixture working with both names until the
  rename is in a stable release (it renders the latest stable release's code).
- **Pick up when:** a release has to be reflash-class anyway.
