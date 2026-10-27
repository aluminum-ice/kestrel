## What this PR does

<!-- One or two sentences: what changes and why. -->

## Checklist

<!--
CI enforces the first two automatically on every PR. The rest are quick
disclosures that let the maintainer merge on the green checks without a manual
audit. Tick what applies; a red check will block, an unticked box will not.
-->

- [ ] **Lint passes** — `make lint` (ansible `--syntax-check`, `yamllint`, `shellcheck --severity=error`, and the real-data check). The **Lint** workflow runs this on the PR.
- [ ] **Tests pass** — `make test` (the `kestrel_pi` pytest suite and the real-data check's tests). The **Test** workflow runs this on the PR.
- [ ] **Nothing real: no secrets, no personal information, no real data.** This is a public repository. The diff contains no real account passwords or hashes, Wi-Fi passphrases, private keys, API tokens, `.env` files, real names, personal emails, device serials, and no real captured or device data: SSIDs, MAC addresses, GPS coordinates, capture files, diagnostics bundles, logs, or screenshots of a real device — placeholders only. The real-data check catches MACs, emails, keys and capture files; real SSIDs, coordinates and screenshots are on you.
- [ ] **Consent gates intact.** Capture and network plugins still ship disabled, enabling them still takes the authorized-use acknowledgment (never pre-ticked or given by code), and the capture whitelist is still enforced.
- [ ] **Docs updated in the same PR**, if this changes something an operator sees or configures: the README package table, the relevant page under `docs/`, and the on-device operator guide (`GUIDE_MD` in `kestrel_pi/userguide.py`) where it applies.
- [ ] Commit messages are short and imperative.

## Release impact

<!--
This tells the maintainer whether the next release carrying this change is an
in-place (app-layer) update or needs a reflash. See the app-layer/reflash rule
in CLAUDE.md. Pick one.
-->

- [ ] **App-layer-safe** — changes only files the in-place update engine carries: the `kestrel_pi` package, `plugins/*.py`, systemd units, the journald drop-in, a lib script already in the engine's `LIB_SCRIPTS`, or the CLI.
- [ ] **Reflash-class** — touches a build input the engine does not carry onto a running device: apt or pip packages, boot options / `config.txt` / `cmdline`, kernel modules, the base image or partition size, `builder/config.json`, `builder/config.yml`, the `Makefile`, `.forgejo/workflows/`, a shipped config default (`config.toml` / `default.conf`), or a brand-new lib script.

## Notes for review

<!-- Anything the maintainer should know before merging. The decision to merge is the maintainer's. -->
