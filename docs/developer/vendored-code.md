# Vendored code

Third-party code carried in the repository, where it lives, and why. Vendored
code belongs to the plugin that uses it, as that plugin's
[helper files](plugin-development.md#helper-files), never to the `kestrel_pi`
core.

## Waveshare e-Paper driver (`eink` plugin)

Waveshare's driver for the 2.7" e-Paper HAT, copied from Waveshare's e-Paper
demo library with the file contents unchanged:

- Upstream: `waveshare/e-Paper`, path `RaspberryPi_JetsonNano/python/lib/waveshare_epd/`
- Branch: `master`, retrieved 2026-10-21
- Driver header version: `epd2in7_V2.py` V1.0 (2022-09-17); `epdconfig.py` V1.2 (2022-10-29)

| File (in `builder/extra/files/plugins/`) | Upstream file | Purpose |
| --- | --- | --- |
| `_eink_epd2in7_V2.py` | `epd2in7_V2.py` | Current **V2** 2.7" mono panel (264×176); `display_Partial`, 4-gray |
| `_eink_epd2in7.py` | `epd2in7.py` | Original **V1** 2.7" mono panel |
| `_eink_epdconfig.py` | `epdconfig.py` | Shared GPIO/SPI backend (selects `RaspberryPi` via `gpiozero`+`spidev`) |

Only the file names change, to the `_eink_` helper prefix. The drivers'
`from . import epdconfig` still works: `kestrel_pi.load_helper` registers a
plugin's helpers as one package, and the plugin loads `epdconfig` before a
driver.

**Why vendored:** no reliably pinnable PyPI package exists for the Waveshare
drivers, and the image build is offline and pinned, so the needed files are
carried in-tree. As plugin files they are installed by the image build and by
app-layer updates (every `plugins/*.py`).

**Licence:** upstream is MIT-licensed; the permission header is kept verbatim
at the top of each file.

**Import-time note:** `epdconfig` sets up a GPIO backend when imported (pulling
in `spidev`/`gpiozero`, or `Jetson.GPIO` off a Pi). The plugin loads the
helpers lazily, inside a `try/except`, only when it looks for a panel, so
importing the plugin (as the test suite does) never touches the hardware.

**Updating:** copy the new upstream files over the helpers, keeping the
`_eink_` names, update the version and date above, and run the e-ink tests
(`test_eink.py`). A driver for another panel goes beside the plugin that drives
it, with that plugin's prefix.
