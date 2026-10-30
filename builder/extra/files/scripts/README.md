# Operator scripts

Curated helper scripts that ship with the image. The build installs this
folder, verbatim, to `/usr/local/share/kestrel/scripts/` (read-only), so
every unit carries a version-matched copy you can find and copy off the device.

**These run on your workstation, not on the Pi.** They are helpers for working
*with* a device (for example over SSH) — on-device actions belong in the
`kestrel` CLI instead. Copy one to your own machine (or run it from a repo
checkout) and point it at a unit:

```sh
scp pi@<device>:/usr/local/share/kestrel/scripts/pull-agent-diagnostics.sh .
./pull-agent-diagnostics.sh pi@<device>
```

## What's here

Each `pull-*` script reads the device read-only over SSH and writes a single
shareable `.tar.gz`; `gps-check.sh` is an interactive live check instead.

- `pull-device-diagnostics.sh` — the "grab this first" general health bundle:
  image version, board/OS/kernel, firmware throttling + temperature, disk,
  memory, uptime, every `kestrel-*` unit's state, failed units, which features
  are enabled and the plugin list. MAC/IP-redacted on the device; never runs
  `kestrel status` or dumps `config.toml`.
- `pull-connectivity-diagnostics.sh` — BT-PAN / Wi-Fi / internet triage:
  profile and link state, routes, DNS, device-side reachability and the
  connectivity journal. MAC/IP-redacted on the device; never reads the Wi-Fi
  SSID or any NetworkManager secret.
- `pull-capture-health.sh` — capture health only: config (no whitelist), the
  authorized-use ack, the detected adapter, the capture unit's systemd health
  and aggregate store sizes. Deliberately reads **no** capture content — not the
  hashlines, raw captures, whitelist or capture journal.
- `pull-agent-diagnostics.sh` — pull a PII-free snapshot of the learning
  agent's model-promotion state off a field unit (read-only over SSH), so
  quality-aware promotion can be reviewed off-device. It never pulls the model
  `.zip` files, raw episode logs, captures, keys or config.
- `pull-web-diagnostics.sh` — triage a web dashboard that won't come up (while
  SSH still works): image + `update status`, the update **run-state and
  rollback** evidence (the engine's `status.json`, the in-progress marker,
  `kestrel-update-recover.service` and the apply journal, so a failed update
  that reverted to the prior version is obvious), the `kestrel-daemon` state and
  its web-subsystem journal (bind / port / "Address already in use" / traceback
  lines — the dashboard is served by the daemon, not a separate unit), the
  login-helper units, the safe `[web]` scalars only, whether the port is bound,
  an on-device HTTP probe of the loopback page, and a pi-venv import check of
  flask and the web module. MAC/IP-redacted on the device; never reads the web
  username, password or token, and never dumps `config.toml` wholesale.
- `pull-eink-diagnostics.sh [--plugin NAME]` — triage a blank / not-updating
  Waveshare e-Paper panel for an e-ink plugin (`eink` by default, or
  `eink-ws213`): its state, its `[plugins.NAME]` table only, SPI
  nodes/module/config, venv imports including its driver helper files, and the
  daemon's e-ink journal lines. Pulls no other config table and no credential.
- `gps-check.sh` — interactive GPS check (not a bundle): serial node, `pi` in
  `dialout`, the daemon's live fix, and an optional `--sample` that briefly
  reads raw NMEA off the receiver to classify FIX / STREAMING / SILENT with the
  strongest C/N0. Prints to your terminal; the live fix shows the device's own
  coordinates, so it is a check you run, not a bundle you forward.

## Adding a script

- Keep it PII-free and secrets-free: this image is public, and the scripts ship
  in it. Use neutral placeholders in examples.
- It must pass `shellcheck --severity=error` (which `make lint` runs on every
  `*.sh`). Keep it POSIX-ish and portable.
- Add a one-line entry above, and a header comment saying what it does and that
  it runs on the workstation.
- The in-place updater carries this folder (see `update-apply.py`'s
  `file_plan`), so a later script change is an app-layer update; the first
  release that *adds* the folder is a reflash (it touches `builder/config.yml`).
