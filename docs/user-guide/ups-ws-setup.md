# UPS HAT battery plugin (`ups-ws`)

Operator guide for the `ups-ws` reference plugin, which reads the
[Waveshare UPS HAT](https://www.waveshare.com/wiki/UPS_HAT) battery and can
cleanly shut the device down before the pack runs flat. Like every reference
plugin it ships **disabled**.

`ups-ws` is the plugin that used to be called `ups-hat`, under a name that says
which board it drives (`ups-ws` plus Waveshare's model letter; the plain "UPS
HAT" has none): the same readout, auto-shutdown and options. `ups-hat` still
works but is deprecated; see [Switching from `ups-hat`](#switching-from-ups-hat).

## What it does

- Reads the HAT's INA219 fuel gauge over I2C and shows a **battery card** on the
  web dashboard: charge percent (with a gauge bar), charging/discharging state,
  voltage, current and power. When auto-shutdown is set, the card also shows the
  **Auto-shutdown** threshold as text and **marks it on the charge gauge**, and
  while discharging shows a **Time to shutdown** estimate (from the recent
  discharge trend; it reads "estimating…" until there's enough trend, and "—"
  when not on battery).
- Optionally **auto-shuts-down** the device at or below a charge you choose
  (`shutdown_percent`), while on battery, so the SD card is never cut off
  mid-write by a flat pack.

It reads only the local I2C bus — nothing leaves the device — so it needs no
authorized-use acknowledgment.

## Hardware

Supported: the Waveshare **UPS HAT** and **UPS HAT (B)** - the same design, two
18650 cells in series with the INA219 at `0x42` (Waveshare's own demo code is the
same file for both). The **UPS HAT (C)** and **(D)** are different boards (one
cell, the INA219 at `0x43`) and are **not** supported here: setting `address` to
`0x43` would read their charge wrongly.

The image already enables I2C (`dtparam=i2c_arm=on`, the `i2c-dev` module) and
installs `python3-smbus`, and the `pi` user is in the `i2c` group, so a seated
HAT works with no extra setup. The plugin **confirms the HAT is present** before
doing anything: on each poll it round-trips the INA219's calibration register and
checks for a plausible pack voltage. On a board without the HAT the card simply
reads "no UPS HAT detected" and nothing else happens.

The HAT's INA219 is at I2C address `0x42` by default. To check it is seen:

```
i2cdetect -y 1      # expect a device at 42
```

## Enabling

From the **web dashboard** (with login on): open **Plugins**, enable **ups-ws**,
set `shutdown_percent` (`0` to monitor only, or e.g. `5` for a low-battery
auto-shutdown), and save.

The same from the terminal:

```
# monitor only (no auto-shutdown):
sudo kestrel plugins enable ups-ws --set shutdown_percent=0

# with a low-battery auto-shutdown at 5%:
sudo kestrel plugins enable ups-ws --set shutdown_percent=5
```

## Options

| Option | Default | Meaning |
| --- | --- | --- |
| `shutdown_percent` | `5` | Auto-shutdown at or below this charge while on battery. **`0` disables** the auto-shutdown (monitor only). |
| `poll_seconds` | `20` | How often the battery is read. |
| `i2c_bus` | `1` | I2C bus the HAT is on (`/dev/i2c-1` on a Pi). |
| `address` | `0x42` | The HAT's INA219 I2C address. |

## Auto-shutdown behaviour

- Runs **only while the plugin is enabled**.
- Fires **only when the pack is discharging** (never while charging or on
  external power), and only after the low reading persists for a few consecutive
  polls, so a brief voltage sag under load won't trip it.
- When it fires it powers off **immediately, with no confirmation** — a dying
  battery can't wait for a prompt. (The dashboard's manual Reboot/Shut down
  buttons keep their confirm step; that is a separate, deliberate action.)

The daemon runs as the `pi` user and can't power the device off itself, so the
shutdown goes through the same root helper the web power buttons use; it works
whenever the plugin is active, independent of whether the web UI's auth is on.

The charge percent is derived from the pack voltage (the 2-cell Li-ion span, ~6.0 V
empty to ~8.4 V full), the same formula as Waveshare's own example, so it tracks
the vendor's reading.

**One UPS plugin at a time.** Two UPS plugins enabled together never both read
the board: the second stands down (its card names the one that reads the
battery) and passes that one's readings on, so the dashboard and the e-ink face
keep their battery readout. The deprecated `ups-hat` always gives way to
`ups-ws`.

## Switching from `ups-hat`

Before this release the plugin was called `ups-hat`. It still ships and still
works, but it is **deprecated** and will be removed in a future release that
requires reflashing the SD card. `ups-ws` is the same plugin for the same HAT
with the same options, so switching takes a minute:

1. Note the settings you changed for `ups-hat`: `sudo kestrel plugins info ups-hat`
   lists them (typically `shutdown_percent`).
2. Enable `ups-ws` with the same settings: on the dashboard's Plugins page, or
   `sudo kestrel plugins enable ups-ws --set shutdown_percent=5`.
3. Disable `ups-hat`: `sudo kestrel plugins disable ups-hat`.

If you forget step 3, nothing breaks: `ups-hat` stands down while `ups-ws` is
enabled, and says so on its card.

