# Setting up the capture service

Capture is kestrel's core job: it runs [AngryOxide](https://github.com/Ragnt/AngryOxide)
on your **external USB Wi-Fi dongle** to discover networks and collect WPA2
handshakes and PMKIDs into a local store. It is **opt-in and off by default** —
nothing captures until you enable it and acknowledge authorized use.

Capture uses only the USB dongle; the built-in Wi-Fi and Bluetooth stay free for
management, so you can stay connected over your phone or Wi-Fi while capturing.

> **Only capture networks you are authorized to.** Enabling capture requires an
> explicit authorized-use acknowledgment. The whitelist (see
> [The capture whitelist](capture-whitelist.md)) protects networks that must
> never be attacked.

## Enabling capture

First plug in a monitor-mode-capable USB dongle. Then enable capture one of two
ways — both detect the dongle, present the same authorized-use acknowledgment
(which is never pre-ticked — you have to confirm it), and start capturing with
**no reboot**.

### From the web dashboard (easiest)

If you've set up the [web dashboard](web-dashboard.md) with its login on, this is
the simplest path:

1. Open the dashboard (e.g. `http://kestrel.local:8080/`) and log in.
2. Flip the **Capture** control on.
3. Confirm the **authorized-use acknowledgment** when prompted.

Capture starts immediately. (The control is read-only if the dashboard login is
off — see [Using the web dashboard](web-dashboard.md) to turn it on.)

### From the command line

```bash
sudo kestrel capture enable
```

This detects the dongle, shows it, presents the authorized-use acknowledgment
(you must say yes), writes the config, and starts capturing. The non-interactive
form for scripts:

```bash
sudo kestrel capture enable --interface auto --yes
```

`--interface auto` picks the monitor-capable wlan that isn't the built-in chip;
pass an explicit `--interface wlan1` to pin one.

### Turning it off

Either flip the **Capture** control off on the dashboard, or from the shell:

```bash
sudo kestrel capture disable
```

## What it does by default (scripted sweep)

Out of the box capture runs a **channel sweep**: it rotates through every channel
your dongle supports, dwelling a few seconds on each while it discovers APs and
captures handshakes, then moves on and loops. This makes a freshly enabled device
useful in the field immediately, with no manual tuning.

Capture runs **headless** (no terminal UI) as a service, keeps running (never
auto-exits), and resumes automatically after a reboot — even re-finding the
dongle if it comes back with a different interface name.

## Configuration

Capture settings live in `/etc/kestrel/config.toml` under the `[capture]`
tables. The ones you'll most likely touch:

| Setting | Meaning |
| --- | --- |
| `[capture.scan] mode` | `sweep` (default), `autohunt` (scan once then lock onto channels with targets — good for a stationary device), or `fixed` (an explicit channel list). |
| `[capture.scan] dwell_seconds` | Seconds on each channel (default 5). Lower = faster revisits for a moving device; higher = more time to catch a handshake per channel. |
| `[capture.scan] bands` | `auto` (all supported), `"2"`/`"5"` for one band, or a channel list like `"1,6,11,36,149"`. |
| `[capture.output] mode` | `per-ssid` (one `<ssid>.hc22000` per network, default) or `single` (one combined file). |

After editing, restart capture:

```bash
sudo systemctl restart kestrel-capture.service
```

Edit one table/key at a time; several tables share an `enabled` key, so never
do a file-wide find-and-replace.

### The environment readout

While capture runs, the dashboard and e-ink face show how busy the RF
environment is — how many distinct radios (access points and clients together)
are in range recently — so you can tell a target-rich spot from an empty one.
It's derived from what the capture radio is already seeing, so it only reads the
air while capturing; with capture off it shows "unknown". The capture data lags
a little (the radio flushes it in bursts), so the card says "as of N ago" when
the reading isn't fresh. Tune it under `[capture.environment]`:

| Setting | Meaning |
| --- | --- |
| `enabled` | `true` (default) shows the readout; `false` hides it. |
| `window_s` | How many seconds of recent capture data counts as "recently seen" (default 300), anchored to the newest packet rather than the clock. A density reading over the window, not an instant count. |
| `max_age_s` | How old the newest capture data may be (default 900) before the card reads "as of N ago" and, past this, falls back to "unknown" instead of a stale count. |
| `thresholds` | The radio-count cut-offs for the Quiet / Light / Moderate / Busy / Dense labels (default `[0, 10, 30, 60]`); these count APs and clients together. A city centre and a rural lane differ — tune to taste. |

## Where captures are stored

The persistent store is `/var/lib/kestrel/capture/`, with one
`<ssid>.hc22000` file per network (or one combined file in `single` mode). It is
**append-and-dedup, never truncated** — reboots and attack restarts add new
unique handshakes to the existing files rather than replacing them.

AngryOxide's raw working files (packet captures, kismet DB) live in a scratch
directory, `/var/lib/kestrel/capture-run/`, that is size-capped (`max_run_mb`, default 512 MB) so it
can't fill the card — only the raw scratch is pruned, never your handshakes.

## Checking status

```bash
sudo kestrel capture status
```

Shows whether capture is enabled and running, the resolved dongle and driver,
the store size, oldest/newest capture, and archive count. The dashboard's capture
card and the daemon log (`sudo journalctl -u kestrel-capture`) show the same.

## Housekeeping (managing store size)

The store only grows (that's the point), so you manage it explicitly — nothing is
ever deleted silently:

- `sudo kestrel capture archive` — move the current store into a timestamped
  archive set and start fresh. Capture keeps running. Non-destructive; this is
  the default.
- `sudo kestrel capture rotate` — archive only if a size/age limit is
  exceeded (what the optional daily timer runs; ships off).
- `sudo kestrel capture clear --yes` — delete the live store (add `--purge`
  to drop archives too). The only action that deletes, and it needs the explicit
  `--yes`.

An optional `[capture.housekeeping]` policy can automate `rotate`, but it ships
off, so the default is "keep everything."

## See also

- [The capture whitelist](capture-whitelist.md) — protect networks from attack.
