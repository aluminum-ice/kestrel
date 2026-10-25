## What this PR does

<!-- One or two sentences: what changes and why. -->

## Checklist

<!--
CI enforces the first two automatically on every PR. The rest are quick
disclosures that let the maintainer merge on the green checks without a manual
audit. Tick what applies; a red check will block, an unticked box will not.
-->

- [ ] **Lint passes** — `make lint` (ansible `--syntax-check`, `yamllint`, `shellcheck --severity=error`). The **Lint** workflow runs this on the PR.
- [ ] **Tests pass** — `make test` (the `kestrel_pi` pytest suite). The **Test** workflow runs this on the PR.
- [ ] **No secrets, no personal information.** This is a public repository. The diff contains no real account passwords or hashes, Wi-Fi passphrases, private keys, API tokens, `.env` files, real names, personal emails, MAC addresses, device serials, or GPS coordinates — placeholders only.
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
