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
base-OS change 0.6.4 carried. (Draft and pre-releases are ignored; they are never
on the stable update path.)

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

1. **fetches** the release's source at its exact commit and verifies it. This is
   the one step that needs the network up front; it is retried with backoff over
   a flaky link, and if it can't complete, nothing on the device has changed yet.
2. **pauses** the capture service, the learning agent (so training stops cleanly
   on a checkpoint) and the plugin daemon, remembering which were running.
3. **backs up first, if the backup plugin is set up** — it mirrors the latest
   capture data and the agent model to your remote before anything is replaced,
   so a current copy is safe. If that backup can't finish, the update is
   **cancelled** and the paused services are restarted; nothing is installed.
4. **backs up the current application files**, lays the new files down and
   reinstalls the `kestrel_pi` package **offline** (an app-layer release never
   adds a dependency, so nothing is fetched and a dropped link can't stall it).
5. **validates** the new code imports before committing to it, else restores the
   previous files and abandons the update.
6. **stamps** the new version and **restarts whatever was running** — the daemon,
   then capture and the agent (a service you had stopped by hand stays stopped).

Your `config.toml` is **not** overwritten — new keys fall back to their built-in
defaults, and your existing settings, enabled plugins, captured data and the
trained agent model are left untouched.

> **Flaky connections are expected.** Only the fetch (step 1) and the optional
> backup (step 3) use the network, and both happen *before* any installed file
> is touched and are retried. So an internet drop mid-update can only cancel it
> cleanly — leaving the device on the current version, running — never leave it
> half-upgraded. The install itself (steps 4–6) is entirely local.

The same thing from the terminal, which prints the same step-by-step summary and
asks to confirm:

```bash
kestrel update status    # show what the last check found
kestrel update check     # re-check now, ignoring the 6h interval
sudo kestrel update apply # fetch + apply the latest app-layer release
```

> **Scope of rollback.** The backup covers the file layer (scripts, plugins,
> units, the CLI). The Python package is installed forward; the pre-restart
> import check makes a broken boot very unlikely, but a package that still
> misbehaves is recovered by re-running `update apply` on a good release or by
> reflashing. The update never touches the base OS, so the system stays
> bootable regardless.

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
  requirement).
- **Dev builds are never offered updates.** A local `make image` build with no
  `MY_VERSION` is stamped `master`, which reads as a dev build.
- **Needs a management link online.** The check and the fetch need the
  release server reachable over a management path (BT-PAN or Wi-Fi); a private repo also needs a
  token (above).

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
release touched build-layer inputs (apt package lists, the base image, boot
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

## How the device knows its own version

The build stamps the release version into `/etc/kestrel/version`. The
plugin reads it to compare against the latest release. A local `make image`
build with no `MY_VERSION` stamps `master`, which reads as a dev build and is
never offered an update.
