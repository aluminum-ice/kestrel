# UPS HAT (C) battery plugin (`ups-wsc`)

Operator guide for the `ups-wsc` reference plugin, which reads the battery of a
[Waveshare UPS HAT (C)](https://www.waveshare.com/wiki/UPS_HAT_(C)) - one 803040
Li-po cell (1000 mAh), on pogo pins under a Raspberry Pi Zero - and can cleanly
shut the device down before it runs flat. It is the (C)'s sibling of
[`ups-ws`](ups-ws-setup.md) (UPS HAT and UPS HAT (B)): the same card,
auto-shutdown and options. Like every reference plugin it ships **disabled**.

> **Not yet tested on hardware.** The plugin follows Waveshare's own demo code for
> the (C) (an INA219 at `0x43`, its calibration, and the charge as
> `(V - 3) / 1.2` of one cell), but nobody has run it on a UPS HAT (C) yet. If
> you have one, please try it and send what you find (see
> [Reporting results](#reporting-results)).

## What it does

The same as [`ups-ws`](ups-ws-setup.md#what-it-does): a **battery card** on the
dashboard (charge percent with a gauge bar, charging/discharging, voltage,
current, power, and the time to shutdown while on battery), and an optional
**auto-shutdown** at or below `shutdown_percent` while on battery, which fires
only after three low polls in a row and powers off at once with no prompt. It
reads only the local I2C bus, so it needs no authorized-use acknowledgment.

## Hardware

It **confirms the board before doing anything**: the INA219 at `0x43` must
round-trip its calibration register and read a plausible voltage. There must be
**no** controller at `0x2D`: that is the UPS HAT (D), which reports current the
other way round. If one answers, the card says "this looks like a UPS HAT (D):
use ups-wsd". The (C) and (D) look the same on the bus otherwise, and the wrong
plugin would read charging as discharging, so its auto-shutdown would never fire
on battery. This plugin reads the current as the chip reports it (negative while
the cell discharges).

The image already enables I2C, so a seated board needs no extra setup. To check
it is seen:

```
i2cdetect -y 1      # expect a device at 43
```

**One UPS plugin at a time.** With two UPS plugins enabled, one stands down and
its card names the plugin that reads the battery.

## Enabling

From the **web dashboard** (with login on): open **Plugins**, enable
**ups-wsc**, set `shutdown_percent` (`0` to monitor only), and save. From the
terminal:

```
sudo kestrel plugins enable ups-wsc --set shutdown_percent=5
```

## Options

| Option | Default | Meaning |
| --- | --- | --- |
| `shutdown_percent` | `5` | Auto-shutdown at or below this charge while on battery. **`0` disables** it (monitor only). |
| `poll_seconds` | `20` | How often the battery is read. |
| `i2c_bus` | `1` | I2C bus the board is on. |
| `address` | `0x43` | The board's INA219 I2C address. |

## Reporting results

Until someone has run it on a real UPS HAT (C), results from the field are what
make this plugin trustworthy. Send what you find as described in [Reporting
hardware test results](../../CONTRIBUTING.md#reporting-hardware-test-results):
whether the card finds the board, whether **Charging** and **Discharging** match
what the board is doing (plugged in or not), whether the charge percent looks
right against the board's own LEDs, and - carefully, with nothing important
running - whether a raised `shutdown_percent` shuts the device down on battery.
