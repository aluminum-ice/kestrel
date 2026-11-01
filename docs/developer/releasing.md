# Cutting a release

A release is a whole SD-card image, published as a Forgejo release with the
artifacts and a small `update-manifest.json` that tells the `updates` plugin
whether a device can take the release **in place** (app-layer) or must be
reflashed. This note is the maintainer reference for running a release and, in
particular, for choosing the `update_kind`.

The consumer side — how a device discovers and applies an in-place update — is
[In-field updates](updates-setup.md). This note is the producer side.

## Running the Release workflow

Releases are built by the **Release** workflow
(`.forgejo/workflows/main.yml`), triggered manually (`workflow_dispatch`) with
two inputs:

| input | required | default | meaning |
| --- | --- | --- | --- |
| `release_version` | yes | — | the version tag: `X.Y.Z` for a stable release (e.g. `1.2.3`) or `X.Y.Z-beta.N` for a beta (e.g. `1.3.0-beta.1`); anything else is refused. Written into `_version.py` and used as the release tag/title. |
| `update_kind` | no | `auto` | `auto` \| `app-layer` \| `reflash` — how in-place-updatable this release is (see below). |

The workflow sets `_version.py` from `release_version`, builds the image with
`sudo make image` (Packer + Ansible + PiShrink + `7z`), computes the manifest,
generates grouped release notes (see below), and publishes a Forgejo release
through the Forgejo API, tagging the built commit with the `.7z`, its `.sha256`, and
`update-manifest.json`.

## The build host (`image-builder` runner)

The Release job runs on a self-hosted [Forgejo Runner](https://forgejo.org/docs/latest/admin/actions/runner-installation/)
registered in **host mode** with the `image-builder` label (for example
`--labels image-builder:host`). A container job can't build the image, so the
host itself must provide:

- a Debian/Ubuntu system with `sudo` that needs no password for the runner
  user (the job runs `sudo apt-get`, `sudo make image` and `sudo rm`),
- loop devices and binfmt support for `qemu-user-static` (the job installs
  the package),
- `git`, `curl`, `make`, `unzip`, `7z` (`p7zip-full`), `parted`/`e2fsprogs`
  (for PiShrink) and `python3` with the `venv` module,
- ~15 GB free for the workspace, plus room for the base-image cache in
  `/var/cache/kestrel/packer` (`PACKER_CACHE_DIR`), which is kept between runs.

Go is not a host requirement: the job downloads a pinned, SHA-256-verified Go
from go.dev into `~/.cache/kestrel-go/` (x86_64 or aarch64 hosts). The job
deletes the built image and archive at the end, so they don't pile up.

Release creation uses the `RELEASE_TOKEN` secret if it is set (a Forgejo access
token with repository write scope), otherwise the job's own token. Forgejo's
attachment size limit (and any reverse-proxy upload limit) must allow the
`.7z`.

Lint and Test run on any Docker-capable runner (label `docker`) in stock
`python:3.12-bookworm` / `python:3.11-bookworm` images. All three workflows
check out with plain `git` rather than `actions/checkout`, so no job depends on
an actions mirror or on GitHub.

## Versioning

`_version.py` holds `__version__='x.y.z'` and is the single source of the image
version; the `Makefile` reads it unless `MY_VERSION` is set (the workflow sets
it from `release_version`). A build with no `MY_VERSION` stamps `master`, which
a device reads as a **dev build** and is never offered updates — so only a
proper Release run produces an updatable image.

### Beta releases

A beta is a release whose version is `X.Y.Z-beta.N` (`N` counting from 1, with
the dot, so `beta.10` sorts after `beta.2`). The workflow derives everything
else from that suffix, so the tag and the release type can't disagree:

- the release is published as a Forgejo **pre-release**, so the `updates`
  plugin offers it only to devices with its `beta` option on, and Forgejo's
  "latest release" keeps pointing at the newest stable one;
- versions order by SemVer precedence: `1.3.0-beta.1` < `1.3.0-beta.2` <
  `1.3.0`, so a beta device moves on to the final release when it is cut;
- it is **not** published to PyPI;
- its manifest and notes are built against the release just below it (beta or
  stable), while a **stable** release's are built against the previous
  *stable* release, so they cover every beta in between (see below).

There is no `rc` or `alpha` form: only `-beta.N` is accepted.

## The update manifest

The workflow writes `update-manifest.json` and attaches it to the release:

```json
{
  "schema": 1,
  "version": "1.2.3",
  "kind": "app-layer",
  "commit": "<sha>",
  "requires_reflash_reason": ""
}
```

`kind` is `app-layer` or `reflash`; `requires_reflash_reason` is a short
human string, set only for a reflash. A release with **no** manifest is treated
by the plugin as reflash. An optional `min_update_from` names the oldest
installed version the in-place update may be applied on (see below).

In `auto` mode, `kind` comes from the diff against the **previous release by
version**: for a stable release, the highest stable release below it; for a
beta, the highest release of either kind below it. A stable release is never
compared with a beta, because a device on the stable channel never ran the
betas: if `1.3.0-beta.1` changed the playbook, `1.3.0` must still come out
reflash for a device on `1.2.x`.

A device that flashed that beta doesn't need to flash again, though. So when a
stable release comes out reflash against the previous stable one, the workflow
also diffs it against each beta of its own cycle, newest first, and stops at
the first whose diff touches a build-layer input. If the betas from some
`1.3.0-beta.N` up to the newest all diffed clean, `1.3.0` is `app-layer` with
`"min_update_from": "1.3.0-beta.N"`: devices on that beta or later update in
place, and the plugin and `update-apply --verify` send a device on `1.2.x` (or
an older beta) to reflash. The release notes say so. The logic lives in
`.forgejo/update_manifest.py`, tested by `.forgejo/test_update_manifest.py`
(run by `make test`). Forcing `update_kind` to `app-layer` or `reflash` skips
it, as before.

## Release notes

The release body is generated automatically by
`.forgejo/gen-release-notes.py` (run from the **Generate release notes** step)
rather than being a bare "Built from `<sha>`". The generator:

- collects the commits **unique to this release** — reachable from the built
  commit but not from any earlier tag (so a release cut off a side branch does
  not re-list everything already shipped). For a stable release only stable
  tags count, so its notes cover everything since the previous stable release,
  including what its betas shipped;
- groups their subjects into **New** (subjects starting `Add` / `Introduce` /
  `Create` / `Ship`), **Fixes** (`Fix` / `Correct` / `Prevent` / `Stop`), and
  **Improvements** (everything else);
- adds an install-type line from the manifest (`app-layer` → apply in place,
  naming the oldest version it applies on when there is a `min_update_from`;
  `reflash` → flash, with the reason), and a `Built from <sha>` footer.

Grouping is a first-word heuristic, so the notes are only as good as the commit
subjects — keep subjects short and imperative (the house style). The notes are
plain release text, so a maintainer can edit a published release's body
afterwards to add a headline or regroup. If generation ever fails, the step
falls back to a provenance-only note so a notes problem never blocks a release.

Older releases (0.0.1–0.6.0) were back-filled with the same grouped format;
regenerate or edit any release body in the Forgejo web UI (or with
`PATCH /api/v1/repos/{owner}/{repo}/releases/{id}`).

## Mirroring releases to GitHub

The **Sync Release to GitHub** workflow (`.forgejo/workflows/sync-github.yml`)
copies each Forgejo release to the GitHub mirror, `aluminum-ice/kestrel`: the
title, the notes (with a "Mirrored from" link back), the pre-release flag, and
every attached file. Devices still update from the Forgejo releases; the GitHub
copy is for people who find the project there.

- **When it runs.**
  - When a release is **published**. It first waits, up to 30 minutes, until the
    release has its `.7z`, `.sha256` and `update-manifest.json`, because the
    Release workflow creates the release before it uploads the image.
  - When a release is **edited**, so notes rewritten after publishing reach
    GitHub too.
  - By hand (`workflow_dispatch`, with a `tag`), to sync or re-sync one release,
    such as one from before the workflow existed.
- **Safe to run again.** It creates the GitHub release only if it's missing,
  updates the notes only when they differ, and uploads only the files GitHub
  doesn't have, or has at a different size.
- **Checked.** A file with a `.sha256` beside it, such as the image, is checked
  against it after download, and isn't uploaded if they differ.
- **Needs** the `GH_TOKEN` secret: a GitHub fine-grained token for the mirror
  repository only, with "Contents: read and write". Without it the job skips.
  The token goes only to GitHub; the Forgejo release and its files are read
  without one, since the repository is public.
- **Needs the commit on GitHub.** The released commit and its tag must already
  be there, from a push mirror of this repository (Forgejo's repository
  settings, *Mirror settings*). Otherwise GitHub refuses to create the release,
  and the job says so.
- **Runs on** the `docker` runner, in `python:3.12-bookworm`, and needs to reach
  `api.github.com` and `uploads.github.com`.

## Publishing the `kestrel-pi` package to PyPI

The on-device Python package (`builder/extra/files/kestrel_pi/`) is published
to PyPI as [`kestrel-pi`](https://pypi.org/project/kestrel-pi/) automatically by
the Release workflow, as its final step, once the release is cut. The
package is pure Python, so it builds on the runner from its in-repo source — no
image is involved.

- **Version** is synced to `release_version`: the workflow rewrites
  `pyproject.toml`'s `version` (and `_version.py`) before building, so the
  package installed into the image and the one on PyPI both carry the release
  version. The committed `version` in `pyproject.toml` is a dev fallback only.
- **Auth** is a **project-scoped** PyPI API token stored in the repo secret
  `PYPI_API_TOKEN` (repository Settings → Actions → Secrets). The token
  value lives only as an encrypted Actions secret, never in the repo. Because
  the Release workflow is `workflow_dispatch` (manual, write-access only), the
  secret is never exposed to pull-request runs from forks — safe on a public
  repo. If the secret is unset the publish step is a **no-op**, so a release
  never fails for want of it.
- **Pinned tools:** `build`, `twine` and the `setuptools` backend come from
  `requirements-publish.txt`, pinned with hashes and installed with
  `--require-hashes`; the package is built with `--no-isolation`, so nothing
  unpinned is fetched while the token is in the environment. The file's header
  says how to update it.
- **Betas are skipped:** a `X.Y.Z-beta.N` release is not published to PyPI.
- **Re-cuts are safe:** the upload uses `--skip-existing`, so re-running the
  workflow for a version already on PyPI leaves the published files untouched
  rather than erroring. PyPI versions are immutable, so a *content* fix still
  needs a new `release_version`.

One-time setup: create a project-scoped token at pypi.org (Account settings →
API tokens → scope `kestrel-pi`) and add it as the `PYPI_API_TOKEN` repo
secret. Publishing the package is optional — the image installs `kestrel_pi`
from in-repo source regardless; PyPI only serves an external
`pip install kestrel-pi`.

## Choosing `update_kind`

`auto` (the default) marks the release **reflash** when the diff since the
previous release touches a build-layer input — `builder/config.json`,
`builder/config.yml`, `Makefile`, or the Release workflow itself
(`.forgejo/workflows/main.yml`, or the old `.github/workflows/main.yml`) —
and **app-layer** otherwise. The first-ever release is always reflash. The
other workflows (Lint, Test, the GitHub sync) never change the image, so a
change to them alone stays app-layer.

Because the whole of `builder/config.yml` trips the auto rule, a harmless
playbook edit (say, adding a `copy` task for a file the in-place engine already
ships) is marked reflash by default. That is the one case where you override
`auto` with `app-layer`. Override to `reflash` any time you know a change can't
be applied in place but `auto` wouldn't catch it.

### What the in-place engine actually applies

`update-apply.py` reinstalls, from the release clone under
`builder/extra/files`, exactly:

- the `kestrel_pi` venv package (reinstalled `--no-index`, so an app-layer
  release never adds a pip dependency),
- every `plugins/*.py` reference plugin,
- every `systemd/*.{service,socket,timer}` unit and the journald drop-in,
- the fixed `LIB_SCRIPTS` list of `/usr/local/lib/kestrel` scripts plus
  the `kestrel` CLI.

It deliberately **skips operator config** (`config.toml`,
`/etc/default/kestrel`): a new shipped default never lands on an updated
device — new keys fall back to code defaults instead.

### The per-hunk rule for a `config.yml` change

Set `app-layer` only when **every** hunk of the diff is one the engine above
already carries into a running device, namely:

- a `copy` / `template` task whose **source** is a file under
  `builder/extra/files` **and** whose **destination** is one the engine writes —
  a `plugins/*.py` plugin, a `systemd/*` unit, the journald drop-in, the venv
  package, or the CLI; plus
- pure comment / formatting / `boards:`-reference edits.

It is **reflash** (leave `auto`, or force `reflash`) whenever a hunk touches
anything the engine does not apply:

- apt packages (`packages.*`, including `protected`),
- pip packages (`pip.*`),
- boot options / `config.txt` / `cmdline`, kernel modules,
- the system account / hostname / network,
- the base image or partition size,
- a **brand-new installed lib script** — `LIB_SCRIPTS` is a fixed list in
  `update-apply.py`, so add the new script there in the same change or it won't
  install on an app-layer update,
- a new install **destination** outside the covered set, or
- any shipped-default change to `config.toml` / `default.conf` (the engine skips
  operator config, so the new default never reaches an updated device).

When in doubt, leave it reflash — app-layer is the exception you justify
per-hunk, never the default.

## Checklist

1. Lint is green on the commit (`make lint`); run `make test` if the diff
   touched the `kestrel_pi` package.
2. If the diff touched `builder/config.yml`, walk each hunk against the per-hunk
   rule and decide `update_kind`; if it touched the base image
   (`builder/config.json`), confirm `iso_checksum` and `target_image_size` were
   updated too.
3. If the change added a new lib script, confirm it was also added to
   `LIB_SCRIPTS`.
4. Run the Release workflow with the chosen `release_version` and `update_kind`.
5. Confirm the published manifest's `kind` matches intent (`auto` prints its
   decision and reason in the workflow log).
6. For a stable release, update the public user guide on Ghost:
   `tools/ghost-guide/ghost_guide.py status`, `diff`, then `push` (see
   [Publishing the user guide](user-guide-publishing.md)). Betas are never
   published there.

See also: [In-field updates](updates-setup.md), [Tech stack](tech-stack.md).
