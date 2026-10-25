# Vendored: Waveshare e-Paper driver (2.7" family)

These three files are copied **verbatim** from Waveshare's e-Paper demo library:

- Upstream: `waveshare/e-Paper`, path `RaspberryPi_JetsonNano/python/lib/waveshare_epd/`
- Branch: `master`, retrieved 2026-10-21
- Driver header version: `epd2in7_V2.py` V1.0 (2022-09-17); `epdconfig.py` V1.2 (2022-10-29)

Files kept (only what the 2.7" HAT needs):

| File | Purpose |
| --- | --- |
| `epd2in7_V2.py` | Current **V2** 2.7" mono panel (264×176); `display_Partial`, 4-gray |
| `epd2in7.py` | Original **V1** 2.7" mono panel |
| `epdconfig.py` | Shared GPIO/SPI backend (selects `RaspberryPi` via `gpiozero`+`spidev`) |

## Why vendored

No reliably pinnable PyPI package exists for the Waveshare drivers; the image
build is offline and pinned, so the needed files are carried in-tree instead.
They live inside the `kestrel_pi` package so an app-layer update reinstalls
them with the package (a loose `files/` tree would not be carried).

## Licence

Upstream is MIT-licensed; the permission header is preserved verbatim at the
top of each `.py` file. Unmodified except for this note and the package
`__init__.py`.

## Import-time note

`epdconfig.py` instantiates a GPIO backend at import (pulling in
`spidev`/`gpiozero`, or `Jetson.GPIO` off a Pi). The face plugin imports these
lazily inside `try/except`, only on a real device, so importing the plugin
module alone (as the test suite does) never touches the hardware backends.
