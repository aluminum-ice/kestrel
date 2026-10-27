# Remote backup setup

The `backup` reference plugin mirrors the on-device capture data — and the
trained learning-agent model — to a remote server over SSH, so a lost or
reflashed SD card doesn't take them with it. This is the operator guide.

## What it backs up

Two things: your capture data, and the learning agent's trained model.

**The capture store** (`/var/lib/kestrel/capture/`), except its transient
`.run/` working dir:

- the hashline files (`*.hc22000`),
- the GPS sidecars (`*.gps.json`, whether captured on-device or looked up),
- the wigle geo-cache (`geo/wigle-cache.json`),
- the wpa-sec cracked potfile (`cracked/wpa-sec.potfile`),
- and any archives under `archive/`.

**The learning agent's data** (`/var/lib/kestrel/agent/`), mirrored into an
`agent/` folder under the remote `path`: the trained model, its metadata sidecar,
and the episode logs — enough for a freshly reflashed device to inherit the
training. Only the ephemeral status/override files are left out. Set the
`agent_dir` option to empty if you'd rather not back the agent data up.

## rsync or scp?

It uses **rsync over SSH by default, and rsync is the recommendation**: it
copies incrementally (only new or changed bytes cross the link) and resumes an
interrupted transfer, so repeated backups over a slow Bluetooth-PAN or Wi-Fi
link are cheap. `tool = "scp"` forces a plain recursive copy instead — every
file, every run, no resume — and is kept only as a lowest-common-denominator
fallback for a remote without rsync. Prefer rsync unless the remote truly can't
run it. (rsync ships in the image; the remote needs it too for the rsync mode.)

## Passwordless copying

The device copies as the `pi` account using its SSH key. No key or password is
ever stored in the image or in `config.toml`. The one-time step that needs the
remote password — installing the device's **public** key in the remote account's
`authorized_keys` — is done from the terminal with `ssh-copy-id`, which takes the
password once and stores nothing. After that the plugin only ever uses key-based,
non-interactive SSH, so it never blocks waiting for a password.

## Setting it up

1. Configure the remote and enable the plugin (it transmits off-device, so this
   takes the authorized-use acknowledgment, same as the uploaders). From the
   **web dashboard** (with login on): open **Plugins**, enable **backup**, set
   `host`, `user` and `path` in its options, acknowledge the authorized-use
   prompt, and save. The same from the terminal:

   ```bash
   sudo kestrel plugins enable backup \
       --set host=backup.example.com \
       --set user=pi \
       --set path=/srv/kestrel-backups/device01
   ```

   `path` is where the capture store is mirrored on the remote. A bare name
   (`kestrel-backups/device01`) is **relative to the remote account's home**;
   a leading slash (`/srv/kestrel-backups/device01`) is an **absolute** path,
   which only works if `user` can write there. The directory is created if
   missing, but only where that account has permission — an absolute path under
   a root-owned directory fails partway with an rsync file-IO error (code 11).

   Optional `--set` keys: `port` (default 22), `tool` (`rsync`/`scp`),
   `interval_seconds` (default 3600), `timeout` (default 1800),
   `ssh_key` (default `/home/pi/.ssh/id_ed25519`), and `agent_dir` (default
   `/var/lib/kestrel/agent`; set empty to skip backing up the agent data).

2. Install the device key on the remote. **This one step is terminal-only** — it
   asks the **remote** account's password once (and generates the pi key first if
   there isn't one); nothing is stored afterward:

   ```bash
   sudo kestrel backup setup
   ```

3. Run a first backup. Once `backup setup` has installed the key, the simplest
   way is the dashboard: the `backup` plugin's row on the **Plugins** page gets a
   **Backup** button (shown once the plugin is enabled and `[web] auth` is on). It
   runs with no confirm step, since a backup only copies to the remote and never
   overwrites local data. The same from the terminal:

   ```bash
   sudo kestrel backup now
   ```

   Or just wait — the plugin also backs up at start, on every reconnect, and
   every `interval_seconds`.

Check state any time from the dashboard `backup` card (time since the last
backup, status, files and bytes moved by the last run, and the remote target),
or the terminal:

```bash
sudo kestrel backup status      # target + whether passwordless SSH works
sudo kestrel plugins info backup # live health from the daemon
```

However a backup runs — the dashboard button, the terminal `backup now`, or an
automatic run — the result lands in the shared state file and the plugin picks it
up on its next epoch (within about a minute), so the card reflects it.

## Restoring onto a fresh image

After you reflash, re-point the backup plugin at the **same** remote (`host`,
`user`, `path`) and run `backup setup` once to reinstall the device key — the
same first two steps as above. Then restore.

From the **web dashboard** (with login on): the `backup` plugin's row has a
discreet **Restore capture data from the backup** button — handy when you're
already on the device's web page after a reflash. It asks to confirm, then runs
the restore in the background through the root helper. Before it starts it checks
that passwordless SSH is set up (the device key exists); if `backup setup` hasn't
been run yet it refuses up front with that message instead of launching a restore
that would fail silently. While it runs, the card's **Restore** line tracks it:
`running`, then `ok` or `error` with the cause once it finishes (the dashboard
auto-refreshes, so it updates on its own).

The same from the terminal (it prints the progress live):

```bash
sudo kestrel backup restore        # asks to confirm; --yes skips the prompt
```

Either way, this is the mirror image of `backup now`. It pulls the capture store
back from `<path>/` into `/var/lib/kestrel/capture/` and the agent's model
and episode logs back from `<path>/agent/` into `/var/lib/kestrel/agent/`,
fixes ownership so the (pi) daemon can read and append to them, and leaves the
remote untouched (there is no `--delete` — a restore only adds and overwrites, it
never wipes). Doing the two directories by hand is error-prone because the agent
dir is nested inside the backup path on the remote and because files pulled as
root end up unreadable by the daemon; `backup restore` handles both.

**It restores data, not settings.** The backup deliberately excludes operator
configuration (`config.toml` and `/etc/default/kestrel`), so a fresh image
comes up with every plugin disabled, capture off and no API keys. After a
restore, re-enable capture / the plugins / the agent and re-enter any keys
(wpa-sec, WiGLE) and your Wi-Fi and `pi` password. The restored `model.zip` only
takes effect once you enable the agent with `policy = learned`. The
`session-stats` counters (handshakes / unique APs this session) are per–daemon
session and always start at zero; the cumulative **Captured networks** page and
the agent-vs-sweep metrics rebuild themselves from the restored files.

> The remote backup is **not** encrypted at rest, and a restore brings back
> captured material. Keep the remote as access-controlled as the device.

## Common mistakes

These are the ones that bite people most often — each lists the symptom and the
fix.

- **An absolute `path` the remote account can't write to.** A leading slash
  (`/srv/...`, `/kestrel`) is an absolute path from the remote filesystem
  root; if `user` can't write there the backup fails partway with `Permission
  denied (13)` and an rsync `code 11` error. **Fix:** use a bare, home-relative
  name like `kestrel-backups/device01` (created under the account's home)
  unless you've deliberately set up a writable absolute location.
- **Running `backup now` (or enabling) before `backup setup`.** Until the device
  key is installed on the remote, every transfer fails with `Permission denied
  (publickey)`. **Fix:** do it in order — configure + enable, then
  `sudo kestrel backup setup`, then `backup now`. `backup status` shows
  "passwordless SSH: not set up" until `setup` has run.
- **A remote that doesn't allow password login.** `backup setup` bootstraps the
  key with `ssh-copy-id`, which needs a one-time password login. If the remote is
  already key-only, `setup` can't log in. **Fix:** add the device's public key
  (`/home/pi/.ssh/id_ed25519.pub`) to the remote's `authorized_keys` yourself,
  then skip `setup`.
- **A remote without rsync.** rsync mode needs rsync installed on *both* ends; if
  the server has none, the transfer errors. **Fix:** install rsync on the remote,
  or switch this device to plain copy with `--set tool=scp`.
- **Wrong `user`/`host`, or a non-standard SSH port.** The target must be exactly
  what you'd `ssh` to by hand. **Fix:** confirm `user@host` works in a normal SSH
  session first, and set `--set port=NN` if the remote isn't on 22.
- **Expecting the remote password to be stored.** It never is — `setup` types it
  into `ssh-copy-id` once and keeps nothing. If passwordless SSH later stops
  working (e.g. the remote key or account changed), just re-run `backup setup`.

The dashboard `backup` card and `sudo kestrel backup status` surface the
*specific* failing line (e.g. the exact `rsync:` error), so read that first — it
usually points straight at which mistake above applies.

## Good to know

- The remote path is created automatically where the account has permission
  (rsync `--mkpath`; scp runs `mkdir -p` first).
- The host key is confirmed once, when `backup setup` runs `ssh-copy-id` (it
  shows the fingerprint and asks). Every later connection requires that recorded
  key (`StrictHostKeyChecking=yes`), so a backup never trusts a host it hasn't
  seen: if the host's key is unknown or has changed, the backup fails with
  "Host key verification failed" until you run `backup setup` again.
- The transfer runs on its own thread, so a large first backup never blocks the
  daemon or the other plugins.
