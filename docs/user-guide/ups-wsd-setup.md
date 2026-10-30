# UPS HAT (D) battery plugin (`ups-wsd`)

Operator guide for the `ups-wsd` reference plugin, which reads the battery of a
[Waveshare UPS HAT (D)](https://www.waveshare.com/wiki/UPS_HAT_(D)) - 21700
cells in parallel (one cell's voltage), charged over USB-C, on pogo pins - and
can cleanly shut the device down before it runs flat. It is the (D)'s sibling of
[`ups-ws`](ups-ws-setup.md) (UPS HAT and UPS HAT (B)): the same card,
auto-shutdown and options. Like every reference plugin it ships **disabled**.

> **Not yet tested on hardware.** The plugin follows Waveshare's own demo code for
> the (D) (an INA219 at `0x43`, its calibration, and the charge as
> `(V - 3) / 1.2` of one cell), but nobody has run it on a UPS HAT (D) yet. If
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
round-trip its calibration register and read a plausible voltage. The board's
controller at `0x2D` must answer: the UPS HAT (C) has none, and reports current
the other way round. If it doesn't answer, the card says "this looks like a UPS
HAT (C): use ups-wsc". The (C) and (D) look the same on the bus otherwise, and
the wrong plugin would read charging as discharging, so its auto-shutdown would
never fire on battery. This plugin reads the current negated, as Waveshare's
demo does: the (D)'s INA219 is wired the other way round from the (C)'s, so this
keeps discharging negative.

The image already enables I2C, so a seated board needs no extra setup. To check
it is seen:

```
i2cdetect -y 1      # expect a device at 43, and one at 2d
```

**One UPS plugin at a time.** With two UPS plugins enabled, one stands down and
its card names the plugin that reads the battery.

## Enabling

From the **web dashboard** (with login on): open **Plugins**, enable
**ups-wsd**, set `shutdown_percent` (`0` to monitor only), and save. From the
terminal:

```
sudo kestrel plugins enable ups-wsd --set shutdown_percent=5
```

## Options

| Option | Default | Meaning |
| --- | --- | --- |
| `shutdown_percent` | `5` | Auto-shutdown at or below this charge while on battery. **`0` disables** it (monitor only). |
| `poll_seconds` | `20` | How often the battery is read. |
| `i2c_bus` | `1` | I2C bus the board is on. |
| `address` | `0x43` | The board's INA219 I2C address. |

## Reporting results

Until someone has run it on a real UPS HAT (D), results from the field are what
make this plugin trustworthy. Send what you find as described in [Reporting
hardware test results](../../CONTRIBUTING.md#reporting-hardware-test-results):
whether the card finds the board, whether **Charging** and **Discharging** match
what the board is doing (plugged in or not), whether the charge percent looks
right against the board's own LEDs, and - carefully, with nothing important
running - whether a raised `shutdown_percent` shuts the device down on battery.
