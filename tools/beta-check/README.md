# Beta check

Scripts that collect the on-device evidence for a beta's checklist issue, so
the items a script can judge are judged from data rather than by eye. The
process around them is in [CLAUDE.md, "Verifying a beta"](../../CLAUDE.md#verifying-a-beta).

| File | What it is |
| --- | --- |
| `pull-beta-check.sh` | Runs on your computer. Reaches the device over SSH, runs the device part once with sudo, and saves `kestrel-beta-check-<time>.tar.gz` locally. |
| `beta-check-device.sh` | The device part. Copied to a private temp folder on the device, run, then deleted. Never run it by hand. |
| `attach.py` | Zips the two scripts and this README as `beta-check-<version>.zip` and attaches it to the beta's issue (the forge doesn't accept `.sh` attachments). |

## Running it

```bash
unzip beta-check-<version>.zip && cd beta-check-<version>
EXPECT_VERSION=<version> bash pull-beta-check.sh <user>@<host> [ssh-port]
```

It asks for your SSH login once, sudo on the device if it needs it, and
optionally the dashboard password (adds plugin health and agent state; press
Enter to skip). Attach the resulting `.tar.gz` to the issue, or hand it to
Claude, after reading it.

The report has one `CHECK <id> <result>` line per checklist item the script can
judge (`PASS`, `FAIL`, `INFO`, `SKIP`, `MANUAL`), the readings behind them, and
a by-hand list at the end. The ids are the checklist's item numbers.

## Rules for changing these scripts

The scripts are published in this public repository and attached to public
issues, and they run as root on a field unit, so every change keeps all of
these. They come on top of the [Non-negotiable rules](../../CLAUDE.md#non-negotiable-rules).

**Safe to publish**

- No secrets, personal information or real identifiers: no hosts, IPs, URLs,
  account names or passwords. The SSH target is an argument, the device account
  is read from the device (the user `kestrel-daemon` runs as), and default
  endpoints are read from the installed plugin declarations. Test values are
  RFC 2606/5737 placeholders only.
- Passwords are typed only into prompts (SSH, sudo, the optional dashboard
  login), never passed on a command line or stored.

**Read-only, and the management link is untouchable**

- Never call `nmcli`, `bluetoothctl`, `ip`, `rfkill`, `iw` or anything that
  changes NetworkManager, Bluetooth or BT-PAN. Losing the management link
  strands a field unit.
- Never start, stop, restart, enable or disable a service. Reading systemd and
  journal state is fine.
- Never write `/etc/kestrel/config.toml`. A check that needs a write (a refusal
  test, a setter) runs against a throwaway copy in a root-only temp folder, in a
  Python process where running commands is blocked, and a checksum before and
  after proves the real file is unchanged.
- No request off the device (only the optional dashboard call on `127.0.0.1`),
  and nothing heavy: no torch or model load, low CPU and I/O priority.
- Files the device user owns are read as that user (`as_dev`), never as root,
  so a planted symlink can't redirect a root read (see "Anything `pi` can write
  is untrusted input to root" in CLAUDE.md).

**Nothing real leaves the device**

- Never read capture content: no hashlines, SSIDs, BSSIDs or whitelist entries.
  Counts and file metadata only.
- The capture journal can carry network names, so only counts per error pattern
  leave it. Other journal excerpts go through `jscrub` (quoted text blanked,
  lines cut short).
- The report is redacted on the device before it's copied back: MACs, IPs,
  GPS-like coordinates, the hostname, account names, every configured host and
  secret value in `config.toml`, and capture file names. Add a pattern to the
  redactor whenever a new check could print a new kind of identifier.

**Checked before it's attached**

- `make lint` shellchecks both scripts and runs the real-data check on them.
- Run the config harness and the redactor off the device against staged copies
  of `plugins-ctl`, `config-set`, the plugins and `config.toml` (never the
  repo's own files), and once against a deliberately weakened `plugins-ctl`
  copy to show the checks can fail.
- Feed the redactor an invented report (placeholder MACs, IPs, a capture file
  name with a space, a fake secret) and confirm everything is masked.
- These checks run off the device only, so the first run on a real device is
  still its first real test; say so when attaching it.
