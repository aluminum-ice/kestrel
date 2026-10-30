# e-ink face for the 2.13" HAT (`eink-ws213`)

Operator guide for the `eink-ws213` reference plugin, which draws the Kestrel
"tamagotchi" face on a
[Waveshare 2.13" e-Paper HAT](https://www.waveshare.com/wiki/2.13inch_e-Paper_HAT_Manual)
(250×122, black and white). It is the 2.13" sibling of the
[`eink` plugin](eink-setup.md) for the 2.7" HAT: the same face, moods and voice,
laid out for the smaller panel. Like every reference plugin it ships
**disabled**.

> **Not yet tested on hardware.** The plugin follows Waveshare's own demos for
> each panel revision, but nobody has run it on a 2.13" HAT yet. If you have
> one, please try it and send what you find (see
> [Reporting results](#reporting-results)).

## What it shows

The same face as the `eink` plugin (see
[What it does](eink-setup.md#what-it-does)): the mood follows real device and
learning-agent state, the face never lies, it sleeps when capture is off, and
the voice changes with the mood. On the smaller panel it shows, top to bottom:

- **Top icon strip:** the Bluetooth and Wi-Fi link symbols (slashed when a link
  is down), the GPS satellite with signal bars when a receiver is present, a
  walking person or a car while you're moving, and the battery with its percent
  (with `ups-hat`).
- **The face and its voice**, two lines at most. For the first ten minutes after
  start-up the voice shows the start of the dashboard's HTTPS certificate
  fingerprint instead (`HTTPS AB:CD:EF:12` / `34:56:78:9A`), to compare with
  your browser on first sign-in.
- **One status row:** a chip symbol with the agent mode (`off`, `baseline` or
  `learned`), an antenna symbol (with waves while capturing, slashed when idle)
  and `UP`, the uptime.
- **The address row:** the device's address on each connected link, behind
  the same symbols as the top strip, so you can find the dashboard without your
  router's client list. Turn it off with `show_ip`.
- **The counters, in larger type:** `PWND` captured handshakes with this
  session's count in parentheses, then `AP` access points and `NET` networks,
  both lifetime totals. Very large counts drop `AP` and `NET` rather than run
  into the Kestrel mark, which sits in the lower-right corner (diving when
  you're moving, hovering when parked).

Left out to keep it readable: the 2.7" panel's channel plan (`CH`) and the
nearby-radios meter (`NEAR`). Both stay on the dashboard. This HAT has no keys,
so there is no info screen either.

Like `eink`, it reads and drives only local hardware, so it needs no
authorized-use acknowledgment, it serves a live **web mirror** of the exact
frame (the **View** button on the Plugins page, also with no HAT attached), and
it blanks the panel on shutdown.

## Hardware

The Waveshare 2.13" e-Paper HAT comes in several revisions. This plugin
supports **V4** (the current one), **V3** and **V2**; the revision is printed on
a sticker on the back of the panel. The revisions can't be told apart by
software, so **choose yours** with `variant` (default `v4`). The colour versions
(B, G) and the original V1 are not supported.

The image already has the SPI and GPIO backends and the fonts, and Waveshare's
driver ships with the plugin, so a seated HAT needs no extra install. SPI must be
enabled (it is, in the stock image).

**Use one e-ink plugin at a time.** `eink` and `eink-ws213` drive the same pins;
if both are enabled, only one gets the panel and the other draws only to its web
mirror (`eink-ws213` then says "the e-Paper pins are in use", `eink` says no HAT
was detected).

## Enabling

From the **web dashboard** (with login on): open **Plugins**, pick
**eink-ws213**, choose the **variant** that matches your panel's sticker, and
enable it. Then use **View** to see the face.

From the terminal:

```
sudo kestrel plugins enable eink-ws213
```

It asks for each option; `variant` offers `v4`, `v3` or `v2`. To set it
directly: `sudo kestrel plugins enable eink-ws213 --set variant=v3`.

## How the panel is redrawn

The face is composed every `refresh_seconds` (10 s) and the panel is redrawn
only when the frame changed, about once a minute plus whenever the mood or the
counts change. Following Waveshare's guidance for this panel:

- most redraws are **quick partial redraws** (about 0.3 s, no flash);
- a **full refresh**, which flashes and clears the faint ghosting partial
  redraws leave, comes first and then after every `partials_per_full`
  partial ones (20, so roughly every 20 minutes), never sooner than 3 minutes
  after the previous full refresh.

If partial redraws misbehave on your panel, set `partials_per_full = 0` to make
every redraw a full refresh (it flashes each time), and please report it.

## Options

| Option | Default | Meaning |
| --- | --- | --- |
| `variant` | `v4` | The panel revision on its sticker: `v4`, `v3` or `v2`. |
| `rotation` | `0` | Turn the picture `0` / `90` / `180` / `270`; `180` for a HAT mounted the other way up. |
| `refresh_seconds` | `10` | How often the face is composed (`10` is the floor). The panel redraws only when the frame changes. |
| `partials_per_full` | `20` | Quick redraws between full refreshes; `0` makes every redraw a full refresh. |
| `show_mascot` | `true` | Draw the face and its voice. |
| `show_brand_glyph` | `true` | Draw the Kestrel mark in the corner. |
| `show_fingerprint_minutes` | `10` | Minutes after start-up that the voice shows the start of the HTTPS fingerprint; `0` never. |
| `show_ip` | `true` | Draw the address row. Turn it off if the panel is visible to people who shouldn't see the addresses. |
| `battery_low_percent` | `10` | Show the tired face at or below this charge while on battery (needs `ups-hat`). |
| `bored_epochs` | `15` | Idle epochs before the face looks bored. |
| `sad_epochs` | `25` | Idle epochs before the face looks sad. |

Faces and voices can be overridden per mood under
`[plugins.eink-ws213.faces]` and `[plugins.eink-ws213.voices]`, the same way as
for `eink` (see [Faces and voices](eink-setup.md#faces-and-voices)).

## Troubleshooting

Check the daemon log:

```sh
journalctl -u kestrel-daemon --no-pager | grep -i eink-ws213
```

- `eink-ws213: epd2in13_V4 panel ready` (or `_V3`, `_V2`): the driver started.
  If the panel stays blank or shows noise, the `variant` probably doesn't
  match the sticker; change it and restart the daemon.
- `eink-ws213: no panel (... BUSY line never driven ...)`: no panel answered.
  Check that the HAT is seated and that SPI is enabled (`ls /dev/spidev*`).
- **"the e-Paper pins are in use"** on the dashboard: another e-ink plugin
  (such as `eink`) is enabled. Disable one of them.
- A GPIO backend error on a Pi 5: see the `lgpio` note under
  [Troubleshooting](eink-setup.md#troubleshooting) for the `eink` plugin; it
  applies here too.

To collect a report you can attach when asking for help, run the diagnostics
script from a computer (it reads only, and nothing real leaves the device):

```sh
scripts/pull-eink-diagnostics.sh --plugin eink-ws213 pi@kestrel.local
```

## Reporting results

Until someone has run it on a real HAT, results from the field are what make
this plugin trustworthy. Send what you find as described in
[Reporting hardware test results](../../CONTRIBUTING.md#reporting-hardware-test-results):
which revision (sticker), the `variant` you set, whether the face draws and
the right way up, how the partial redraws and full refreshes look, and whether
ghosting builds up over an hour.
