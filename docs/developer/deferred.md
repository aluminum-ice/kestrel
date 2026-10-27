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

## In-place updates (`updates` plugin and update engine)

### Sign releases and verify the signature before installing

- **What:** release authenticity today rests only on TLS to the configured host
  and the Forgejo account (or `RELEASE_TOKEN`) that publishes releases. Whoever
  controls either can publish a release that runs as root on every device whose
  operator taps Upgrade.
- **Why deferred:** needs a signing key kept off the build host, a key-rotation
  story, and a one-time reflash to ship the public key; the root-side
  verification that now exists (`update-apply --verify`) was the prerequisite.
- **What to do:** sign `update-manifest.json` (for example `ssh-keygen -Y sign`
  or minisign) in the Release workflow, with the private key held outside the
  runner; ship the public key in the image (root-owned); make `--verify` refuse
  a release whose manifest signature doesn't check out. Signed tags plus
  `git verify-tag` are an alternative.
- **Pick up when:** before the device is used beyond the maintainer's own
  hardware, or if the release account or runner is shared.

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

## Security review findings (2026-10-27)

A full read-through of the code, build and docs on 2026-10-27 found these
alongside the root-boundary and model-loading issues fixed then (PRs #15 and
#16). Those fixes came first because they let code running as `pi`, or a remote
host, reach root. Everything below is lower severity or needs a design call; it
is listed roughly by impact.

### Make the default login impossible to keep

- **What:** the image ships `pi` / `raspberry` (documented), SSH password login
  is on, the dashboard's `auth = system` uses the same login, and there is no
  firewall. A device built without `MY_PI_PASSWORD_HASH` is open to anyone on a
  management link (any Wi-Fi it joins, or a Bluetooth PAN) who knows the public
  default. README and `first-boot.md` warn, but nothing enforces a change.
- **Why deferred:** it changes first-boot behavior and needs a design choice
  (force a change, or refuse controls until one) plus device testing.
- **What to do:** expire the password at first login (`chage -d 0 pi` in
  `builder/config.yml`, reflash-class), and/or have the web page and
  `capture enable` refuse while the default hash is in place. Consider a
  minimal firewall that only lets SSH and the dashboard in on management links.
- **Pick up when:** before any device leaves the maintainer's own hardware, or
  together with the next reflash-class release.

### Rate-limit dashboard logins

- **What:** `_check_auth`/`_verify_system_login` (`web.py`) and `plugins-ctl
  checkauth` have no delay, failure counter or lockout, and don't go through PAM,
  so `faillock` doesn't apply. With the default login above, guessing is cheap.
- **Why deferred:** needs per-client state in the daemon and a choice of policy
  (backoff vs lockout) that can't lock the owner out.
- **What to do:** per-address exponential backoff in the web auth path; log
  repeated failures. Optionally route `checkauth` through PAM.
- **Pick up when:** with the default-login item, or when a device is exposed on
  a shared network.

### Stop reopening Bluetooth pairing on devices that never paired

- **What:** the Just Works onboarding window (`kestrel-bt-onboard.service`,
  `bt-onboard.sh`) reopens for 10 minutes on every boot until a phone is
  recorded. A device set up only over Wi-Fi advertises open pairing after every
  boot, indefinitely; a stranger who pairs first becomes "the phone" and reaches
  SSH and the dashboard over `bnep0`.
- **Why deferred:** onboarding UX change; needs device testing with real phones.
- **What to do:** stop auto-reopening once Wi-Fi has been provisioned or after
  the first few boots, and require an explicit trigger (CLI or a button) after
  that. Mention the never-paired case in `bluetooth-setup.md`.
- **Pick up when:** with the default-login item.

### Protect the dashboard login on the wire

- **What:** `auth = system` sends the device password (also the SSH and sudo
  password) as HTTP Basic over plain HTTP on every request. On a shared or open
  Wi-Fi the device has joined, anyone listening can read it. The management
  bind includes `wlan0` whatever network it has joined.
- **Why deferred:** TLS on a headless device needs a certificate story (self-
  signed with pinning, or a local CA) and changes how phones connect.
- **What to do:** options are a session login (password sent once, then a
  cookie) plus TLS with a per-device self-signed certificate, or binding the
  dashboard to the Bluetooth PAN only unless explicitly allowed on Wi-Fi.
- **Pick up when:** before recommending Wi-Fi management on shared networks.

### Allow-list the Host header (DNS rebinding)

- **What:** `web.py` accepts any `Host`. With `[web] auth = none`, a malicious
  page can rebind its name to the device and read every GET page (captured
  networks, whitelist, `/status.json`). With auth on, the browser's stored login
  doesn't carry over and writes still need the CSRF token.
- **Why deferred:** only matters with auth off, which is not the default.
- **What to do:** accept only the bound addresses, `localhost` and the device's
  hostname in `Host`; refuse others with 421.
- **Pick up when:** any change to the web auth path.

### Verify the Packer download and harden the build-tool cache

- **What:** `Makefile` fetches Packer with `curl -L` (no `-f`, no SHA-256 check)
  and runs it under `sudo make image`. Built tools are cached in predictable
  `/tmp/raspberrypi/` paths (and `$HOME/.cache/kestrel-go` in
  `.forgejo/workflows/main.yml`) and reused without re-checking, so another
  local user on a shared build host could plant one first.
- **Why deferred:** build-pipeline change; the release runner is single-user.
- **What to do:** pin `PACKER_SHA256` and verify it (as PiShrink and Go already
  are), add `curl -f`, and keep the tool cache in a root-owned or per-build
  directory, or re-verify cached tools before use.
- **Pick up when:** the next Packer bump, or if the build host gains users.

### Pin the release job's publishing tools

- **What:** `.forgejo/workflows/main.yml` runs `pip install --upgrade build
  twine` (unpinned) with `TWINE_PASSWORD` in the step's environment, on the
  root build host.
- **Why deferred:** low likelihood; needs a pinned requirements file with hashes.
- **What to do:** pin `build` and `twine` with hashes, or switch to PyPI trusted
  publishing so no long-lived token sits in the job.
- **Pick up when:** the next release-workflow change.

### Pin the device's pip packages

- **What:** `pip.*` in `builder/config.yml` pins only `torch`; the rest
  (including the small third-party `vcgencmd` package) install whatever is
  current at build time, unhashed. `pytest` also ships on the device though only
  CI needs it.
- **Why deferred:** reflash-class, and pinning the whole ML stack needs a tested
  set of arm64 wheels.
- **What to do:** a constraints file with versions (and hashes where practical)
  for the venv install; drop `pytest` from the image.
- **Pick up when:** the next reflash-class release.

### Validate `capture.interface` and stop ssh option injection

- **What:**
  - `capture.interface` is a free string up to 32 characters, settable through
    the apply socket. `capture_resolve_iface` (`common.sh`) uses an explicit
    value as-is. It is always quoted as one argument, so there is no injection,
    but it can name the built-in management chip, taking it off the network.
  - The backup and remote-train `user`/`host` options are joined as
    `user@host` with no check, so a value starting with `-` becomes an ssh
    option that runs a local command as `pi`. `backup.py` also passes the remote
    path unquoted to a remote `mkdir -p`. Only an authenticated operator can set
    these, so no privilege boundary is crossed today.
- **Why deferred:** no boundary crossed; found late in the review.
- **What to do:** add a `pattern` (interface name) to `capture.interface` in
  `CONFIG_SETTABLE`/`config_catalog.py` and refuse the built-in chip; reject a
  leading `-` in `user`/`host`, pass `--` before the destination, and
  `shlex.quote` the remote path.
- **Pick up when:** any change to the capture launcher or the SSH plugins.

### Extract boot-partition episode archives as `pi`

- **What:** `agent-provision.sh` runs `tar -xzf` as root into the `pi`-owned
  episodes directory, then `chown -R pi:pi`. As root, tar keeps owners and modes
  and can create device nodes, which the `chown` then hands to `pi`, and a
  symlink `pi` planted there can redirect the extract.
- **Why deferred:** whoever can write the boot partition can already get root
  through `cmdline.txt`, so the practical risk is low.
- **What to do:** extract as `pi` (`run_as_pi`) with `--no-same-owner
  --no-same-permissions`, or accept regular files only.
- **Pick up when:** any change to agent provisioning.

### Keep secrets off command lines

- **What:** docs and `GUIDE_MD` show `--psk "..."`, `--set api_key=...` and
  `--set token=...`. Those land in shell history, and sudo logs the full command
  to the journal, which is persistent on the card and readable by `pi` (`adm`).
  The diagnostics bundle from `pull-connectivity-diagnostics.sh` also carries
  the Wi-Fi network name (its journal filter catches `wifi-provision` lines) and
  each bundle's `MANIFEST.txt` records the raw `user@host`.
- **Why deferred:** docs and diagnostics change, no code path at risk.
- **What to do:** lead with the interactive prompts (they already mask input),
  add a `--psk-stdin`/`--set-stdin` like `web auth --password-stdin`, redact
  SSIDs and the target in the diagnostics bundles.
- **Pick up when:** the next docs pass on onboarding or plugins.

### Smaller web and root-helper items

- **What:**
  - Any `pi` process can give the authorized-use acknowledgment itself through
    the apply socket (`ack: true`), so the ack is a consent step in the UI, not
    a barrier against `pi` code. Either document it that way or gate it.
  - `target: power` needs no auth (intended for the UPS auto-shutdown), so a
    `pi` process can reboot the device repeatedly.
  - A disabled plugin's module-level code still runs when the loader scans the
    plugin directories (root-owned, so only root can add one).
  - `[web] bind` accepts `0.0.0.0` (root-only setting).
  - No `X-Content-Type-Options: nosniff`; Basic auth has no logout; the 5-minute
    login cache accepts an old password after a change; a crafted release-notes
    body can make `/update/notes` return 500.
  - `checkauth` timing differs between known and unknown accounts.
  - The GPS reader's line buffer has no size cap.
  - `.claude/settings.json` denies only the Read tool on `.env` and `*.key`;
    shell `cat` and `*.pem`/`id_*` files are not covered.
- **Why deferred:** each is low impact on its own.
- **What to do:** fix opportunistically when touching the file concerned.
- **Pick up when:** the next change to the file in question.

### Confirm on a device and on the runners

- **What:** three things the review couldn't settle from the repo:
  - whether stock Raspberry Pi OS's `/etc/sudoers.d/010_pi-nopasswd` survives
    the build (`sudo -l -U pi`). If it does, the `pi` login is effectively root,
    which raises the default-login item to critical;
  - whether the CI runner labelled `docker`, which runs fork pull requests'
    code, shares a machine with the `image-builder` runner, which has
    passwordless sudo;
  - whether the capture collector, now running as `pi` (PR #15), can still read
    AngryOxide's Kismet database read-only from `/var/lib/kestrel/capture-run`.
    If the database uses SQLite WAL mode, the dashboard's Environment card
    would stay at "scanning" while capture keeps working; reading a copy fixes it.
- **Why deferred:** needs a device and access to the runner hosts.
- **What to do:** check each; turn any "yes" for the first two into a fix, and
  note the result here.
- **Pick up when:** before the next release.
