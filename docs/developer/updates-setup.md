# In-place updates (`updates` plugin)

Operator guide for the `updates` plugin: how the device tells you a newer
release is out, and how to apply an **app-layer** update in place — without
reflashing the SD card — from the web dashboard or the terminal.

The plugin ships **off** and never updates anything on its own. There is no
auto-update: a new release only ever produces an *alert*; applying it is a
deliberate, confirmed action you take.

## Why "app-layer" (and when you still need to reflash)

A kestrel release is a whole SD-card image. You cannot write an `.img`
onto the root filesystem a device is currently booted from — that is a reflash.
But the part of the image that actually changes between most releases is the
**application layer**: the `kestrel_pi` package, the reference plugins, the
helper scripts, the systemd units and new config defaults — everything built
from `builder/extra/files`. That layer *can* be replaced on a running device.

So the plugin splits updates in two:

- **App-layer update** — the release only changed application files. These can
  be applied in place: the device fetches the release's source, lays the new
  files down, reinstalls the Python package into the pi virtualenv and restarts
  the services. This is what the upgrade button does.
- **Needs reflash** — the release also changed the base OS (new apt packages, a
  kernel bump, boot-config changes). That cannot be done safely in place, so the
  plugin *withholds* the upgrade button and tells you to reflash instead.

Which one a release is comes from its **update manifest** (below). A release
with no manifest is treated as "needs reflash" — the plugin never guesses.

The plugin checks the **whole chain**, not just the latest release. An app-layer
update can only be applied on top of every build-layer release since the one the
device is on, so before offering an in-place upgrade the plugin looks at every
published release between the installed version and the latest: if any of them is
reflash-only (or carries no manifest), it withholds the in-place offer and tells
you to reflash. For example, a device on 0.6.3 offered 0.6.5 (app-layer) is still
told to reflash when 0.6.4 in between was reflash-only — the device can't skip the
base-OS change 0.6.4 carried. (Drafts are ignored. Betas are checked only when
the offered release is itself a beta: a stable release's manifest is built
against the previous stable release, so it already accounts for any betas in
between.)

## Stable and beta releases

Releases come in two channels, told apart by the tag: a stable release is
`X.Y.Z`, a beta is `X.Y.Z-beta.N` and is published as a Forgejo pre-release
(see [Cutting a release](releasing.md#beta-releases)). By default the plugin
only offers stable releases. Turn on the `beta` option (a checkbox on the
plugin's settings page, or `sudo kestrel plugins enable updates --set
beta=true`) to also be offered betas:

- versions compare by SemVer precedence, so `0.7.0-beta.1` < `0.7.0-beta.2` <
  `0.7.0`: a device on a beta is offered the final release once it is out;
- the beta channel offers the **highest version** published, beta or stable;
- turning `beta` off while on a beta never downgrades: the device stays where it
  is until a stable release newer than its beta is out. An offer the beta
  channel found is withdrawn as soon as the option changes, so the Upgrade
  button can't install a beta after you opted out;
- a version with any other suffix (`-rc.1`, `-dirty`) is never offered, and a
  device stamped with one reads as a dev build.

`kestrel update status` shows which channel the last check ran on.

## Turn it on

```bash
sudo kestrel plugins enable updates
```

Checking for releases is a network action, so enabling `updates` takes the same
authorized-use acknowledgment as the other networked plugins (if you have
already enabled capture, it is silent — the acknowledgment is remembered
device-wide). Nothing is transmitted off the device; it only makes a read-only
request to the Forgejo releases API on `host` (`/api/v1/repos/<repo>/releases`).

While enabled it checks at most every `check_interval_seconds` (default 6h)
whenever a management path is online, and once at load. The result shows up as
an **Updates** card on the dashboard:

| Status | Meaning |
| --- | --- |
| `up to date` | The latest release is not newer than this image. |
| `update available` | A newer app-layer release exists — the upgrade button is offered. |
| `update available — needs reflash` | A newer release exists but changed the base OS; reflash instead. |
| `dev build (not checked)` | This image has no release version stamped (a local `master` build). |

The card also shows the installed and latest versions. Status is always carried
by text, never colour alone.

### What's new (the release notes between versions)

When an update is available the card carries a discreet **What's new in X?**
link (a muted secondary link, so it never competes with the Upgrade button). It
opens a read-only page with the **merged release notes for every release between
the installed version and the latest one**, newest first — so you can see what
the upgrade actually changes before committing to it.

The notes are collected by the `updates` plugin when it checks (stored in the
shared state file alongside the version info), so the page renders from the
device with no round-trip and works even if the link is flaky when you open it.
If no notes were captured — an older plugin, offline at the last check, or a
private repo without a token — the page falls back to a link to the latest
release page. The notes are release text, so they are rendered through the
escaping Markdown renderer, never injected as raw HTML.

### Check now (don't wait for the timer)

The check is throttled to `check_interval_seconds` (6h by default), so a change
you just made — a new token, a freshly published release — may not be picked up
right away. To force a check immediately, without waiting out the interval:

- **From the web page** (`[web] auth` on): the **Check** button on the
  `updates` plugin's row of the `/plugins` page.
- **From the terminal:** `kestrel update check`.

Either one asks the running plugin to re-check on its next cycle (within a
minute, while online); the result then shows on the **Updates** card and in
`kestrel update status`. It only *checks* — it never installs anything.

## Updating from a private repo

The default repo is public, so the plugin reads its releases with no
credentials. If you run kestrel from a **private** repo or a **private
fork** — e.g. to keep your own changes or deployment payload off the public
internet — Forgejo hides that repo from anonymous requests (the check just sees
"no public release"), so the plugin has to authenticate.

**Why a token.** A Forgejo access token lets the device prove it may read your
private repo. It authenticates all three steps that touch the server: the
periodic release check, the download of each release's update manifest, and the
`git clone` the in-place update performs. Without it, none of them can reach a
private repo.

**Why it is safe.** The token is *yours* and lives *only on the device*, in
`/etc/kestrel/config.toml` (root-owned, readable only by the device
account). It is never committed to the repo, never baked into the SD-card
image, and never shown back in the web UI or the CLI (it is a masked secret).
That keeps the project itself secrets-free while still letting your own device
reach your own private repo. The token is only ever sent to `host`, and only
over `https://`. Scope it as narrowly as possible so a leak off the device is
low-impact.

**Make a token** in Forgejo under **Settings → Applications → Generate New
Token**, as read-only as it will go: only **repository: Read**.

**Set it on the device** (also set `repo`, and `host`, if your fork's
owner/name or server differs):

```bash
sudo kestrel plugins enable updates --set repo=you/your-repo --set token=xxx
sudo kestrel plugins enable updates --set host=https://forgejo.example.org
```

The token is written to `config.toml` and masked everywhere afterwards. To
change it later, re-run the same command with the new token. To go back to a
public repo, clear it:

```bash
sudo kestrel plugins enable updates --set token=
```

From then on the release card, the manifest check and the upgrade button all
work against your private repo exactly as they do for the public one. A wrong or
expired token shows up as an HTTP `401`/`403` from the server in the daemon log
(`journalctl -u kestrel-daemon | grep updates`); an unauthenticated check
of a private repo looks like "no public release".

## Upgrading in place

With `[web] auth` on (so only an authenticated operator can act), the Updates
card shows an **Upgrade to X** button when an app-layer update is available.
Tapping it opens a confirmation page that **spells out every step** before you
commit — the services it will pause, whether it will back up first, and that the
dashboard drops briefly. Confirming starts the update in the background as root.

The device then, in order:

1. **verifies the release as root.** Before anything is fetched, the installed
   update engine (`update-apply --verify`) asks the release server itself -
   using only the `host`, `repo` and `token` in the root-owned `config.toml` and
   the version stamped in `/etc/kestrel/version` - whether the release may be
   installed in place: it must be a published, non-draft release newer than the
   installed one, on your channel (a beta only with `beta` on), with an
   `app-layer` manifest that pins a commit, and with no reflash-only release in
   between. The plugin's shared state only says *which* release to look at; it
   is never trusted (see [Security model](#security-model)). From the web, the
   release must also still be the one you confirmed.
2. **fetches** that release's source and checks it is at exactly the verified
   commit. This is the one step that needs the network up front; it is retried
   with backoff over a flaky link, and if it can't complete, nothing on the
   device has changed yet.
3. **pauses** the capture service, the learning agent (so training stops cleanly
   on a checkpoint) and the plugin daemon, remembering which were running.
4. **backs up first, if the backup plugin is set up** — it mirrors the latest
   capture data and the agent model to your remote before anything is replaced,
   so a current copy is safe. If that backup can't finish, the update is
   **cancelled** and the paused services are restarted; nothing is installed.
   The backup is bounded by `backup_timeout_seconds` (default 180): if the
   remote is unreachable, the update gives up on the backup and restarts the
   daemon rather than holding the dashboard down for the length of a doomed
   transfer. You can also update **without** a fresh backup — see
   [If the pre-update backup fails](#if-the-pre-update-backup-fails).
5. **backs up the current application files and snapshots the installed
   package**, then records an in-progress marker naming that backup.
6. **lays the new files down** — each written beside its target and renamed into
   place, so a power cut leaves the old or the new file, never a truncated one —
   removes shipped files the release dropped (a retired reference plugin, say),
   and reinstalls the `kestrel_pi` package **offline** (an app-layer release
   never adds a dependency, so nothing is fetched and a dropped link can't stall
   it). The new package must import, else everything is rolled back.
7. **stamps** the new version and **restarts whatever was running** — the
   daemon, then capture and the agent (a service you had stopped by hand stays
   stopped).
8. **checks the daemon stays up.** It must come up within 90 seconds and then
   run for 20 seconds without restarting; if it doesn't, the update is **rolled
   back** — files, package and version stamp — and the previous version is
   started again.

Your `config.toml` is **not** overwritten — new keys fall back to their built-in
defaults, and your existing settings, enabled plugins, captured data and the
trained agent model are left untouched.

> **Flaky connections are expected.** Only the verify and fetch (steps 1–2) and
> the optional backup (step 4) use the network, and all happen *before* any
> installed file is touched and are retried. So an internet drop mid-update can
> only cancel it cleanly — leaving the device on the current version, running —
> never leave it half-upgraded. The install itself (steps 5–8) is entirely local.

The same thing from the terminal, which prints the same step-by-step summary and
asks to confirm:

```bash
kestrel update status    # show what the last check found, and the last run
kestrel update check     # re-check now, ignoring the 6h interval
sudo kestrel update apply # verify, fetch + apply the latest app-layer release
```

`sudo kestrel update apply --ref TAG` installs a named release instead of the
one the plugin found. It goes through the same verification (it may be a beta
even with `beta` off, since you named it). `--force` overrides the *safety*
checks — a reflash-only or older release, a skipped reflash, a dev build, a ref
that isn't a published release (whose commit then can't be pinned) — and prints
each one it overrode. It never overrides a draft, a malformed commit, or a
release that changed since you confirmed it.

Only one update runs at a time: a second one (another browser tab, or the CLI
while the web update runs) is refused.

### If an update fails

Every attempt records its outcome in the engine's run-state
(`/var/lib/kestrel/update-run/status.json`, world-readable). When the most
recent attempt **failed**, the dashboard's Updates card says so and shows the
reason in plain words, with guidance keyed to the failure: a failure *before*
anything was installed (the pre-update backup, or the download) is safe to
retry, while "installed … but restart failed … rolled back" means the new
version didn't come up — collect a bundle with
`builder/extra/files/scripts/pull-web-diagnostics.sh` before retrying the same
release. `kestrel update status` shows the same `last run:` detail from the
terminal.

### If the pre-update backup fails

Because the update is **cancelled** when the pre-update backup can't finish (an
unreachable remote, a flaky field uplink), a device whose backup remote is down
would otherwise never be able to update. When the last attempt failed on the
backup, the Updates card offers a **Retry without a fresh backup** button, and
the confirm page has a **skip the pre-update backup** checkbox; either updates
in place without taking a new remote mirror first (the update itself does not
touch your captured data or model, so skipping only forgoes the extra safety
copy). From the terminal it is `sudo kestrel update apply --no-backup`. Tune how
long the backup may run before the update gives up with
`backup_timeout_seconds` (0 waits indefinitely, the old behaviour).

### If an update is interrupted

A power cut or crash mid-update leaves the in-progress marker behind. At the
next boot `kestrel-update-recover.service` runs before the daemon, capture and
the agent, and restores the previous files, package and version stamp from the
backup the marker names; `kestrel update status` then shows the update was
rolled back. Until it has run, a new update is refused. The update never touches
the base OS, so the system stays bootable regardless.

### Security model

The in-place update runs as root, so the inputs that decide *what* it installs
must be ones only root controls:

- The release server, repo and token come only from `config.toml` (root-owned).
  The plugin's shared state (`/var/lib/kestrel/update/state.json`) is written
  by the `pi` daemon - any plugin can write it - so root reads it only as a hint
  for which release to look at, then re-verifies that release with the server.
  A tampered state file can at most point at a different *legitimate* release,
  which must still pass every check.
- Root never writes into the `pi`-owned state directory (a symlink planted
  there could redirect a root write). The CLI creates the "check now" trigger only
  fresh and never through a symlink, and chowns nothing; the engine keeps its run status, lock, in-progress marker and backups
  in the root-owned `/var/lib/kestrel/update-run`. The package is staged in a
  fresh root-created directory and snapshotted/restored as `pi` (it is `pi`'s
  own venv).
- The token is sent only to `host`, over https. The clone gets it through
  git's `GIT_CONFIG_*` environment, so it never appears in a process command
  line other local users can read.
- Release authenticity rests on TLS to `host` and the Forgejo account that
  publishes releases; releases are not yet signed (tracked in
  [Deferred improvements](deferred.md)).

## Limitations

The in-place update is deliberately conservative. Know these before relying on
it:

- **App-layer only.** It replaces application files (the package, plugins,
  scripts, systemd units, the CLI). A release that changed the base OS — apt
  packages, the kernel, boot config, partitioning — is flagged **needs reflash**
  and the upgrade button is withheld; reflash the SD card for those.
- **New config _defaults_ don't reach an updated device.** The update
  deliberately skips your `config.toml`, so a changed shipped default for an
  existing key won't apply on an upgraded device (a brand-new key still falls
  back to its code default, which does work). To pick up a changed default, set
  it yourself or reflash. This is why a release that only changes a shipped
  default is marked reflash.
- **The dashboard and monitoring pause during the update.** The daemon is
  stopped while files are replaced, so the web page is briefly unavailable and
  daemon-hosted plugins — including the UPS-HAT low-battery auto-shutdown — are
  not running for that minute or two. Run an update on external power, or with
  adequate battery.
- **No auto-update.** A new release only ever raises an alert; applying it is
  always a deliberate, confirmed action. There is no schedule and no silent
  upgrade.
- **The pre-update backup needs the backup plugin set up.** Without a configured
  remote and an installed key, no snapshot is taken (an app-layer update does not
  touch your captured data or model, so the backup is an extra safety net, not a
  requirement). When it *is* set up but the remote is unreachable, the backup is
  bounded by `backup_timeout_seconds` and you can update without it (see
  [If the pre-update backup fails](#if-the-pre-update-backup-fails)).
- **Dev builds are never offered updates.** A local `make image` build with no
  `MY_VERSION` is stamped `master`, which reads as a dev build.
- **Needs a management link online.** The check and the fetch need the
  release server reachable over a management path (BT-PAN or Wi-Fi); a private repo also needs a
  token (above).
- **Removed systemd units are left in place.** A release that drops a shipped
  plugin or lib script removes it from updated devices, but a dropped unit file
  stays until a reflash (removing one safely means disabling it first).
- **Too far behind needs a reflash.** The skipped-reflash check reads the newest
  100 releases; a device older than all of them is told to reflash.

## The update manifest (for maintainers)

Each release attaches an `update-manifest.json` asset, generated by the Release
workflow, that tells a device whether the release is app-layer-updatable and how
to apply it:

```json
{
  "schema": 1,
  "version": "0.4.3",
  "kind": "app-layer",
  "commit": "<full git sha of the tag>",
  "min_update_from": "0.4.0",
  "requires_reflash_reason": ""
}
```

- `kind` — `"app-layer"` (in-place updatable) or `"reflash"`.
- `commit` — the tag's commit, so the device checks out exactly what the release
  was built from.
- `min_update_from` *(optional)* — the oldest installed version this in-place
  update may be applied on top of. An installed version older than this is told
  to reflash (the file layout changed too much to patch).
- `requires_reflash_reason` *(optional)* — a short human reason shown when
  `kind` is `"reflash"`.

The workflow marks a release `"reflash"` when the diff since the previous
release (by version; for a stable release, the previous *stable* one) touched build-layer inputs (apt package lists, the base image, boot
config, or the playbook's non-application tasks); otherwise `"app-layer"`.

## Configuration reference

All keys live under `[plugins.updates]` in `/etc/kestrel/config.toml`.

| Key | Default | Meaning |
| --- | --- | --- |
| `enabled` | `false` | Opt-in; off = no checks and no upgrade button. |
| `repo` | `aluminum-ice/kestrel` | Forgejo `owner/repo` to check (override for a fork). |
| `host` | `https://git.3ntropy.io` | The Forgejo (or Gitea) server hosting `repo`. Must be `https://`; the check is skipped otherwise. |
| `token` | `""` | Optional Forgejo access token for a **private** repo/fork (masked secret, device-only). Empty = unauthenticated, public releases only. See [Updating from a private repo](#updating-from-a-private-repo). |
| `check_interval_seconds` | `21600` | At most this often, re-check while online. |
| `timeout` | `30` | Per-request network timeout. |
| `beta` | `false` | Also offer beta (`X.Y.Z-beta.N`) releases. See [Stable and beta releases](#stable-and-beta-releases). |
| `backup_timeout_seconds` | `180` | How long the pre-update backup may run before the update gives up on it (so an unreachable remote can't hold the daemon down). `0` = wait indefinitely. See [If the pre-update backup fails](#if-the-pre-update-backup-fails). |

## How the device knows its own version

The build stamps the release version into `/etc/kestrel/version`. The
plugin reads it to compare against the latest release. A local `make image`
build with no `MY_VERSION` stamps `master`, which reads as a dev build and is
never offered an update.
