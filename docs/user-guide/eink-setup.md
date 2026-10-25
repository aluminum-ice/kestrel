# e-ink face plugin (`eink`)

Operator guide for the `eink` reference plugin, which draws the classic
animated "tamagotchi" face on a
[Waveshare 2.7" e-Paper HAT](https://www.waveshare.com/wiki/2.7inch_e-Paper_HAT_Manual)
so the device shows what it is doing at a glance, with no phone. Like every
reference plugin it ships **disabled**.

## What it does

- Renders an **animated face** whose expression follows real device and
  learning-agent state — never a canned loop. A priority ladder decides the
  mood: a fault shows a broken face, a low battery a tired one, a just-promoted
  model a celebration, a fresh handshake an excited face, and while capturing it
  reflects the agent's scan **posture**. The face only shows the "smart"
  expression when a *learned* policy is actually driving; the scripted baseline
  gets plainer posture faces. The principle is that **the face never lies**.
- **Sleeps when capture is off.** With capture disabled the device has nothing
  to hunt, so the face rests with a sleeping expression (`(⇀‿‿↼)`, deepening the
  longer it idles) and a matching "zzz…" voice, rather than an awake/idle face.
  The tiers above still win, so a fault, low battery, a promotion or a fresh
  handshake is never hidden behind a sleeping face.
- Shows a **top icon strip**: BT PAN and Wi-Fi link symbols (each slashed when
  that link is down); a **GPS satellite** with signal bars when a receiver is
  present (outlined while searching, solid once locked); a **motion glyph** once
  you're moving — a walking person or a moving car (parked shows nothing here,
  since the Kestrel's hover pose denotes stopped); and
  a battery symbol with its percent (with `ups-hat`).
- Gives the mascot a **voice**: a short, cute phrase beside the face that
  matches its mood (and changes when the mood does), just like classic
  pwnagotchi. Override any mood's phrases in `config.toml` under
  `[plugins.eink.voices]`.
- Shows a **compact status on one line** just under the face: a chip glyph with
  the **agent** mode (`off` / `baseline` / `learned`) and an antenna glyph with
  the **capture** state. The mood is the face itself.
- Shows an **info row**: `UP` the device uptime, and `CH` the channel plan
  capture is sweeping this window (a channel list, a band like `2.4G`, `sweep`
  or `hunt`) while capturing. `CH` is the configured plan, not a live tuned
  channel (the radio hops within the plan).
- Shows a **bottom counter row**: `PWND` captured handshakes (a key glyph) and
  `AP` distinct captured access points, each the lifetime total with the count
  from this session in parentheses, e.g. `200 (30)`.
- Draws the **Kestrel brand mark** once, in the lower-right corner; its pose
  reflects motion — stooping (diving) when moving, hovering when parked.
- **Motion** (parked / walking / driving) is estimated from your GPS speed
  across successive fixes, with hysteresis so it doesn't flicker at a stop light.
  Walking tops out near 11 km/h; above that it reads as driving.
- Serves a **live web mirror** of the exact panel frame (see below).

It reads and drives only local hardware — nothing leaves the device — so it
needs no authorized-use acknowledgment. It is also harmless with **no HAT
attached**: the face still renders to the web mirror.

On shutdown — the dashboard power-off button (and its confirmation) or the
`ups-hat` low-battery auto-off — the panel is **blanked** before the device
powers down, so the last face is not left frozen on the e-paper (which keeps
its image with no power).

## Hardware

The target panel is the Waveshare 2.7" e-Paper HAT (264×176, black/white),
which ships in two hardware revisions (V1 and V2). The image installs the SPI
and GPIO Python backends (`python3-spidev`, `python3-gpiozero`) and the fonts
the faces need (`fonts-dejavu-core`), and the driver is **vendored** in the
`kestrel_pi` package, so a seated HAT works with no extra install. SPI must
be enabled (it is, in the stock image). The plugin **probes** the panel on
start: if no panel responds it logs that and keeps running, rendering only to
the web mirror.

The HAT's four on-board buttons are **reserved** for now — no action is wired to
them.

## Enabling

From the **web dashboard** (with login on): open **Plugins**, enable **eink**,
and save. Then use the **View** button on the Plugins page to see the face.

The same from the terminal:

```
sudo kestrel plugins enable eink
```

## The live web mirror

The Plugins page gains a **View** button that opens a page showing a
live mirror of the panel — the *exact* frame the e-ink shows, as a PNG. This
lets you see the face from a phone at any time, and it works even with no HAT
attached (it shows the frame the device would display). The mirror refreshes at
the same cadence as the panel, reading the **same stored frame**, so the two can
never drift apart.

## Options

| Option | Default | Meaning |
| --- | --- | --- |
| `variant` | `auto` | Panel driver: `auto` (probe the hardware revision), `v2`, or `v1`. |
| `rotation` | `0` | Rotate the image `0` / `90` / `180` / `270` if the HAT is mounted turned. |
| `refresh_seconds` | `10` | How often the face re-renders (`10` is the floor). The panel only redraws when the face actually changes, to limit e-ink wear, so a faster cadence mainly makes it react sooner. |
| `show_mascot` | `true` | Draw the emoticon face (and its voice line). |
| `show_brand_glyph` | `true` | Draw the small corner Kestrel mark. |
| `battery_low_percent` | `10` | Show the tired face at or below this charge while on battery (needs `ups-hat`). |
| `bored_epochs` | `15` | Idle event-loop epochs before the face looks bored. |
| `sad_epochs` | `25` | Idle event-loop epochs before the face looks sad. |

## Faces and voices

The faces are the classic pwnagotchi emoticons. You can override any mood's face
in `config.toml` under `[plugins.eink.faces]` (for example to use a different
glyph for `excited`), which is the path toward custom face sets later. Leave it
unset to keep the shipped set.

Each mood also has a set of cute **voice** phrases (the line beside the face).
Override them per mood under `[plugins.eink.voices]` — give a single phrase or a
list to rotate through, for example:

```toml
[plugins.eink.voices]
excited = ["gotcha!", "another one!"]
bored = "anything out there?"
```

Leave it unset to keep the shipped phrases.

## What it reads from other plugins

The face is richer when the **learning agent** and the **`ups-hat`** plugin are
present: it reads the agent's posture, policy and reward trend to choose working
moods, and the battery to show charge on the status line and the tired face. Both
are optional — with neither enabled the face still reflects online/capture/idle
state and looks after itself.

## Troubleshooting

**The panel stays blank and the face only shows on the web mirror.** Check the
daemon log:

```sh
journalctl -u kestrel-daemon --no-pager | grep -i eink
```

- `eink: <driver> panel ready` — the driver initialised. If the panel is still
  blank, it is a panel-revision or mounting issue: try `variant = v1` (or `v2`)
  and check the ribbon seating.
- `eink: no panel (Unable to load any default pin factory!)` — the GPIO backend
  could not start. On a Pi 5 the only working backend is `lgpio`, which needs a
  writable working directory for its `.lgd-nfy` file; the daemon service sets
  `WorkingDirectory`/`LG_WD` to its runtime dir so this works out of the box. If
  you are on an older image without that fix, add a drop-in:

  ```sh
  sudo install -d /etc/systemd/system/kestrel-daemon.service.d
  sudo tee /etc/systemd/system/kestrel-daemon.service.d/10-gpio-workdir.conf >/dev/null <<'CONF'
  [Service]
  WorkingDirectory=/run/kestrel
  Environment=LG_WD=/run/kestrel
  CONF
  sudo systemctl daemon-reload
  sudo systemctl restart kestrel-daemon
  ```

- `eink: no panel (...)` with another error — confirm SPI is enabled
  (`ls /dev/spidev*` lists a node) and the HAT is seated.

**"Display: detected" vs "not detected" on the dashboard.** The dashboard shows
whether a panel was found. Because an e-Paper panel has no way to identify
itself, the plugin confirms one is really wired by checking that the panel
*drives* its BUSY line during start-up; a bare Pi — or a different HAT such as
the `ups-hat`, which does not touch that line — reads **not detected**, and the
face renders only to the web mirror. So "not detected" with another HAT seated
is expected, not a fault. If your e-ink HAT reads "not detected", re-seat the
ribbon and, if needed, pin `variant`.
