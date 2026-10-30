# kestrel user guides

Step-by-step guides for setting up and running a kestrel device, written
for the person using the device.

## About

- [Philosophy and the reason why](philosophy.md) — what kestrel is for and
  the thinking behind it. (A stub for now.)

## Getting started

- [Preparing the card before first boot](boot-partition-provisioning.md) —
  optionally seed Wi-Fi and the capture whitelist (and install a trained agent
  model) by dropping files on the boot partition, no SSH needed.
- [First boot: log in and change the default password](first-boot.md) — do this
  first thing once the device boots.
- [Connecting over Wi-Fi](wifi-setup.md) — add a Wi-Fi network (the image ships
  with none).
- [Connecting over Bluetooth (phone tether / PAN)](bluetooth-setup.md) — pair a
  phone and reach the device over its tethering.

## Capturing

- [Setting up the capture service](capture-setup.md) — enable AngryOxide capture,
  scan modes, output, housekeeping.
- [The capture whitelist](capture-whitelist.md) — protect networks from attack.
- [The learning capture agent](agent-setup.md) — let the agent pick scan
  postures instead of the fixed sweep, and tell whether it's actually winning.
- [Working with your captures](working-with-captures.md) — where handshakes are
  stored, getting them off the device, and cracking them.

## Plugins

- [Managing plugins](managing-plugins.md) — enable, disable, options, and the
  authorized-use acknowledgment.
- [GPS](gps-setup.md) — a position source that tags handshakes and feeds wardriving.
- [wpa-sec](wpa-sec-setup.md) — upload captured handshakes and fetch your cracked results.
- [WiGLE](wigle-setup.md) — wardriving uploads of observed networks with GPS.
- [Remote backup](backup-setup.md) — mirror the capture store off-device over SSH.
- [UPS HAT](ups-ws-setup.md) — Waveshare UPS HAT (and UPS HAT (B)) battery readout and low-battery auto-shutdown (`ups-ws`).
- [UPS HAT, the old name](ups-hat-setup.md) — **deprecated:** `ups-hat` is replaced by `ups-ws` and will be removed in a future release that requires a reflash; how to switch.
- [e-ink face for the 2.7" HAT](eink-ws27-setup.md) — animated tamagotchi face on the Waveshare 2.7" e-Paper HAT, with a live web mirror and the HAT's four keys (`eink-ws27`).
- [e-ink face, the old name](eink-setup.md) — **deprecated:** `eink` is replaced by `eink-ws27` and will be removed in a future release that requires a reflash; how to switch.
- [e-ink face for the 2.13" HAT](eink-ws213-setup.md) — the same face on the smaller Waveshare 2.13" e-Paper HAT (V2/V3/V4), with a lean layout; not yet tested on hardware.
- [memtemp and session-stats](memtemp-session-stats.md) — built-in local readout cards.
- [Remote training](remote-training.md) — train the learning agent on a desktop host over SSH.
- [Downloads](downloads.md) — download your handshake files from the dashboard.
- [Capture map](map.md) — every located captured network on one map, drawn on the device.
- [Notifications](notifications.md) — the `ntfy` and `telegram` plugins: alerts for new handshakes, battery, updates.

## The device web dashboard

- [Using the dashboard and its controls](web-dashboard.md) — status, plugin cards,
  captured networks, capture/plugin/power controls, and login.

## Keeping the device updated

- [Updating in place](updates.md) — get alerted to new releases and upgrade
  without reflashing the card.

## When something goes wrong

- [Troubleshooting](troubleshooting.md) — the problems that come up most in the
  field (offline, capture won't start, no GPS fix, locked out of the dashboard)
  with the quickest fix for each.

## Reference

- [The `kestrel` command reference](cli-reference.md) — every CLI command.
- [The configuration file (`config.toml`)](config-reference.md) — every setting,
  its valid values and what it does.
