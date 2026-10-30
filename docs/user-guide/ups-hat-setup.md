# UPS HAT battery plugin (`ups-hat`) - deprecated

> **Deprecated:** `ups-hat` is replaced by [`ups-ws`](ups-ws-setup.md) and will
> be removed in a future release that requires reflashing the SD card.

`ups-hat` is deprecated. `ups-ws` is the same plugin for the same board (the
[Waveshare UPS HAT](https://www.waveshare.com/wiki/UPS_HAT), and the UPS HAT
(B)) with the same options, under a name that says which board it drives.
Switch now: enable `ups-ws` (with the same settings) and disable `ups-hat`.
`ups-hat` keeps working until it is removed in a future release that requires
reflashing the SD card; that release's notes will say so.

While `ups-hat` is enabled, the Plugins page and `sudo kestrel plugins info
ups-hat` show this notice, and the daemon log says so once at start-up. If
`ups-ws` is enabled too, `ups-hat` stands down - it reads nothing and never
shuts the device down - and its card says it was replaced.

## Switching to `ups-ws`

Follow [Switching from `ups-hat`](ups-ws-setup.md#switching-from-ups-hat) in the
`ups-ws` guide. In short:

```sh
sudo kestrel plugins info ups-hat                              # note shutdown_percent
sudo kestrel plugins enable ups-ws --set shutdown_percent=5    # the same value
sudo kestrel plugins disable ups-hat
```

## Until you switch

Everything in the [`ups-ws` guide](ups-ws-setup.md) applies to `ups-hat` as
well: read `ups-hat` for `ups-ws` in plugin names and log lines. The readout,
the auto-shutdown and every option behave the same.
