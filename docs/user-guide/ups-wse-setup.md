# UPS HAT (E) battery plugin (`ups-wse`)

Operator guide for the `ups-wse` reference plugin, which reads the battery of a
[Waveshare UPS HAT (E)](https://www.waveshare.com/wiki/UPS_HAT_(E)) - four 21700
cells in series, USB-C fast charging, on pogo pins - and can cleanly shut the
device down before it runs flat. It is the (E)'s sibling of
[`ups-ws`](ups-ws-setup.md): the same card and auto-shutdown, plus a row for each
cell, a low-cell rule and an option to have the HAT power the Pi on again once
it has charged. Like every reference plugin it ships **disabled**.

> **Not yet tested on hardware.** The plugin follows Waveshare's own demo code
> for the (E) (its controller at `0x2D` and that controller's registers), but
> nobody has run it on a UPS HAT (E) yet. If you have one, please try it and send
> what you find (see [Reporting results](#reporting-results)).

## What it does

- A **battery card** on the dashboard, as for [`ups-ws`](ups-ws-setup.md#what-it-does):
  charge percent with a gauge bar, charging or discharging, voltage, current,
  power and, on battery, the time to shutdown. The percent and the charge state
  come from the HAT's own controller, and a **Cells** row shows each of the four
  cells' voltages.
- An optional **auto-shutdown** while on battery, at or below `shutdown_percent`
  **or** when any one cell drops under 3.15 V (the limit Waveshare's demo uses:
  with four cells in series one weak cell can be in danger before the pack's
  percent looks low). Either way it fires only after three low polls in a row,
  only once, and powers off at once with no prompt. `shutdown_percent = 0` turns
  both off (monitor only).
- With **`power_on_when_charged`** on, just before an auto-shutdown the plugin
  tells the HAT to power the Raspberry Pi on again once it has charged. It is
  **off by default**, and it is the only thing the plugin ever writes to the HAT.

It reads (and, with that option, writes) only the local I2C bus; nothing leaves
the device, so it needs no authorized-use acknowledgment.

## Hardware

It **confirms the board before doing anything**: the controller at `0x2D` must
answer with a plausible four-cell pack (9-18 V, a percent from 0 to 100, every
cell between 2.5 and 4.5 V), and there must be **no** INA219 at `0x43`. The UPS
HAT (D) has a controller at `0x2D` too, but also an INA219 at `0x43`; if one
answers, the card says "this looks like a UPS HAT (D): use ups-wsd".

The image already enables I2C, so a seated board needs no extra setup. To check
it is seen:

```
i2cdetect -y 1      # expect a device at 2d, and none at 43
```

**One UPS plugin at a time.** With two UPS plugins enabled, one stands down and
its card names the plugin that reads the battery.

## Enabling

From the **web dashboard** (with login on): open **Plugins**, enable
**ups-wse**, set `shutdown_percent` (`0` to monitor only) and, if you want it,
tick `power_on_when_charged`, and save. From the terminal:

```
sudo kestrel plugins enable ups-wse --set shutdown_percent=5
sudo kestrel plugins enable ups-wse --set shutdown_percent=5 --set power_on_when_charged=true
```

## Options

| Option | Default | Meaning |
| --- | --- | --- |
| `shutdown_percent` | `5` | Auto-shutdown at or below this charge, or with a cell under 3.15 V, while on battery. **`0` disables** both (monitor only). |
| `power_on_when_charged` | `false` | Before an auto-shutdown, tell the HAT to power the Pi on again once it has charged. |
| `poll_seconds` | `20` | How often the battery is read. |
| `i2c_bus` | `1` | I2C bus the board is on. |
| `address` | `0x2D` | The HAT's controller I2C address. |

## Reporting results

Until someone has run it on a real UPS HAT (E), results from the field are what
make this plugin trustworthy. Send what you find as described in
[Reporting hardware test results](../../CONTRIBUTING.md#reporting-hardware-test-results):
whether the card finds the board, whether **Charging** and **Discharging** match
what the board is doing, whether the percent and the four cell voltages match
the HAT's own readings, and - carefully, with nothing important running -
whether a raised `shutdown_percent` shuts the device down on battery and, with
`power_on_when_charged`, whether the Pi comes back once the HAT has charged.
