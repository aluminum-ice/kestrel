# Keeping the device updated

The `updates` plugin tells you when a newer kestrel release is out and, when
it is safe to do so, lets you upgrade the device **in place** — without taking the
SD card out and reflashing it. It ships **off**, and it never updates anything on
its own: a new release only ever raises an alert, and applying it is always a
deliberate action you confirm.

## How updates work, in short

- While the plugin is on, the device checks GitHub now and then for the latest
  release and compares it to the version it is running. The check is read-only —
  nothing about your device is sent anywhere.
- If a newer release exists, the dashboard shows it on an **Updates** card. If
  that release can be applied in place, an **Upgrade** button appears (dashboard
  login required). If it can't, the card says so and no button is offered.
- You decide when to upgrade. There is no schedule and no silent update.

## Turn it on

From the dashboard **Plugins** page (with login on), enable **updates**. The same
from the terminal:

```bash
sudo kestrel plugins enable updates
```

Because checking GitHub uses the network, enabling `updates` asks you to
acknowledge authorized use once, the same as the other networked plugins — if you
have already done that for another plugin or for capture, it is silent. See
[Managing plugins](managing-plugins.md) for how enabling and the acknowledgment
work.

The device has to be able to reach the internet over its management link for the
check to run, so set up [Wi-Fi](wifi-setup.md) or a [Bluetooth phone
tether](bluetooth-setup.md) first.

## What the Updates card tells you

The card shows the version you are running, the latest release it found, and a
status line. Status is always spelled out in words:

| Status | What it means |
| --- | --- |
| `up to date` | The latest release is not newer than what you are running. |
| `update available` | A newer release exists and can be applied in place — the **Upgrade** button is shown. |
| `update available — needs reflash` | A newer release exists, but it changed parts of the device that can't be swapped in place, so you reflash the card instead (see below). |
| `dev build (not checked)` | This image has no release version, so it is treated as a development build and never offered updates. |

### Check now, don't wait for the timer

To save battery and data the device only checks every few hours. To make it check
right away — for example just after a new release is published — use the **Check**
button on the `updates` row of the **Plugins** page, or from the terminal:

```bash
kestrel update check
```

Either one asks the device to re-check within a minute while it is online. It only
checks; it never installs anything. The result then shows on the Updates card and
in `kestrel update status`.

## What an update can and cannot change

An in-place update is deliberately limited to the parts of the device that are
safe to replace while it is running. Knowing the line matters:

**It can replace** the kestrel program itself — the background service, the
plugins, the helper commands, the web dashboard and its built-in services. Most
releases only change these, and those are the ones you can upgrade in place.

**It cannot change the base operating system** — the system packages, the Linux
kernel, or the boot configuration. A release that touches any of those is flagged
**needs reflash**, and the Upgrade button is deliberately withheld rather than
half-applying something it can't finish. For those you write the new image to the
SD card the normal way (a reflash). The card tells you which kind a release is, so
you are never guessing. A very old device may also be told to reflash if too much
has changed since it was flashed to patch safely.

**It does not touch your settings or your data.** Your configuration, which
plugins you have enabled, your captured handshakes and your trained learning-agent
model are all left exactly as they were. One consequence worth knowing: if a new
release changes a *default* value for a setting you already have, your existing
value is kept — the new default does not override it. Brand-new settings that
didn't exist before do take their built-in default. To adopt a changed default,
set it yourself.

## The steps an upgrade goes through

When you confirm an upgrade, a confirmation page (and the terminal command) first
spells out exactly what will happen. Then the device, in order:

1. **Downloads** the new release and checks it is intact. This is the only step
   that needs the internet, and it happens before anything on the device is
   touched. It is retried if the link is flaky, and if it can't finish, nothing
   has changed and you stay on your current version.
2. **Pauses** capture, the learning agent (so any training stops cleanly) and the
   dashboard, remembering which were running.
3. **Backs up first, if you have the backup plugin set up** — it mirrors your
   latest captured data and the agent model to your remote before replacing
   anything. If that backup can't finish, the whole update is **cancelled** and
   the paused services are started again; nothing is installed. (If you haven't
   set backup up, this step is skipped — see [Remote backup](backup-setup.md).)
4. **Saves a copy of the current program files, then lays the new ones down** and
   reinstalls, all from the copy it already downloaded — so a dropped connection
   at this point can't interrupt it.
5. **Checks the new program loads** before committing to it. If something is
   wrong, it puts the old files back and abandons the update, leaving you on the
   working version.
6. **Records the new version and starts back up** whatever it had paused — the
   dashboard first, then capture and the agent. Anything you had stopped by hand
   stays stopped.

Because only the download (step 1) and the optional backup (step 3) use the
network, and both run before any installed file is changed, an internet drop
during an update can only cancel it cleanly — it never leaves the device
half-upgraded.

The same thing from the terminal, which prints the same summary and asks you to
confirm:

```bash
kestrel update status     # what the last check found
kestrel update check      # check now, don't wait for the timer
sudo kestrel update apply # download and apply the latest in-place release
```

## Updating from your own private fork

By default the plugin reads the public kestrel releases with no sign-in. If
you run your own **private** fork — to keep your own changes or deployment files
off the public internet — GitHub hides it from anonymous requests, so the check
just reports it can't see a release. To let *your* device read *your* private
fork, give it a GitHub personal-access token.

- Make a read-only token on GitHub, scoped as narrowly as it will go: a
  fine-grained token with **Contents: Read-only** on just your fork, or a classic
  token with the `repo` scope.
- Set it on the device (and point at your fork if its owner or name differs):

  ```bash
  sudo kestrel plugins enable updates --set repo=you/your-fork   # then asks for the token, hidden
  # or from a script, the token on stdin (never --set token=..., which lands in
  # your shell history and the system journal):
  read -rs TOKEN; printf '%s\n' "$TOKEN" | sudo kestrel plugins enable updates --set-stdin token
  ```

The token is stored only on your device and is never shown back to you afterwards.
To change it, run the command again and enter the new token; to go back to the public
releases, clear it with `--set token=`. A wrong or expired token shows up as a
sign-in error in the logs, and an unauthenticated check of a private fork looks
like "no release found".

## Good practice and limits

- **Run an update on external power, or with a good charge.** The dashboard and
  the background services — including the UPS-HAT low-battery auto-shutdown — are
  paused for the minute or two the update takes.
- **You need the internet** over the management link to check and to download an
  update; a private fork also needs the token above.
- **Development builds are never offered updates** (the card reads `dev build`).
- **No automatic updates.** Every upgrade is something you start and confirm
  yourself.
