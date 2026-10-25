# Troubleshooting

The problems that come up most in the field, with the quickest fix for each and
a pointer to the full guide. Work top-down: the first two sections (your first
look, then "the device says it's offline") cover most of what you'll hit.

## Your first look

Two places tell you almost everything:

- **The web dashboard.** Its status rows show whether the device is online,
  whether capture is running, and a card for each enabled plugin. If you can
  reach the dashboard at all, start here.
- **The `status` commands over SSH.** Each subsystem has one, and they're
  read-only (no `sudo` needed just to look):

  ```bash
  kestrel capture status     # capture state, the chosen adapter, the store
  kestrel network status     # Wi-Fi and Bluetooth link state
  kestrel web status         # the dashboard address and whether login is on
  kestrel agent status       # the learning agent's state (if enabled)
  sudo kestrel plugins list  # every plugin and whether it's on or errored
  ```

When something is wrong, the matching service's log is the next stop:

```bash
sudo journalctl -u kestrel-daemon -e      # the dashboard + plugins
sudo journalctl -u kestrel-capture -e     # capture
```

## The device says it's "offline"

The device is "online" if **either** its Wi-Fi (`wlan0`) **or** its Bluetooth
phone-tether link can actually reach the internet — it tests a real connection,
not just whether an interface is up. If the dashboard's Internet row reads
offline:

- **Over Bluetooth tether:** the link can be up but name lookups fail, which
  reads as offline. The device ships a DNS fallback for exactly this; if you
  blanked `BT_PAN_FALLBACK_DNS`, restore it. See [Connecting over
  Bluetooth](bluetooth-setup.md).
- **Over Wi-Fi:** confirm the network is saved and in range. See [Connecting
  over Wi-Fi](wifi-setup.md).
- Uploads (wpa-sec, WiGLE) and backups simply **queue** while offline and go out
  on the next reconnect — being offline for a while loses nothing.

## Capture won't start, or catches nothing

Run `kestrel capture status` first — it shows the three things that are
usually the cause:

- **No capture adapter.** Capture needs an **external USB Wi-Fi adapter** that
  supports monitor mode; the Pi's built-in chip is never used for capture. If
  `capture status` shows no adapter, plug one in (or set `[capture] interface`
  if detection picked the wrong one).
- **Capture is still off.** It ships disabled and only turns on after you give
  the one-time authorized-use acknowledgment. Turn it on from the dashboard
  **Capture** control, or `sudo kestrel capture enable`. See [Setting up
  capture](capture-setup.md).
- **Everything's whitelisted / nothing in range.** A whitelisted network is
  never attacked (only observed). Check the list on the dashboard **Whitelist**
  page or with `kestrel capture whitelist list`.

If it's running but you expected a text UI: the background service always runs
**headless** (the text UI only appears when you launch capture by hand in a
terminal) — that's normal.

## No GPS fix

Data can be flowing while the receiver still has no lock. The GPS card shows `no
fix` / `2D` / `3D` and the satellite count; a cold start under open sky can take
a few minutes, and indoors it may never lock. The full checklist (serial device,
NMEA stream, `source` modes, `gpsd`) is in [GPS setup](gps-setup.md).

## Can't log in to the dashboard

By default the dashboard login **is your device account** — the same username
and password as SSH. There's no separate web password to forget.

- **Forgot it?** It's the `pi` account password; change it over SSH with
  `passwd` and the dashboard login changes with it.
- **Locked out but you have SSH?** Switch the login mode:

  ```bash
  sudo kestrel web auth system     # device account (the default)
  sudo kestrel web auth none       # no login — read-only page, controls off
  ```

  `web auth none` always gets you back to a readable page (the on/off controls
  are disabled until a login is set again). See [Using the web
  dashboard](web-dashboard.md).

## A plugin shows an error

A plugin that fails to load never stops the daemon or the other plugins — it
just shows an error state. Find out why:

```bash
sudo kestrel plugins info <name>              # the plugin's health line
sudo journalctl -u kestrel-daemon | grep -i <name>
```

It's almost always a bad option value or a missing prerequisite (no API key, no
GPS, a backup host not set up yet). Fix the option from the dashboard **Plugins**
page or re-enable with the corrected `--set`. See [Managing
plugins](managing-plugins.md).

## No update is ever offered

- A build with no version set reports itself as `master`, a **dev build**, and
  is deliberately never offered updates.
- A newer release that changes the base image can't be applied in place; it
  shows as **needs reflash** rather than offering an in-place update.

How updates work, and what can and can't be applied in place, is in [Updating in
place](updates.md).

## Still stuck

- Re-check the matching `status` command and service log (top of this page) —
  they almost always name the specific failing step.
- The relevant setup guide (linked from the [index](user-guides.md)) has a
  fuller troubleshooting section for each feature.
