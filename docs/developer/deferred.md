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
