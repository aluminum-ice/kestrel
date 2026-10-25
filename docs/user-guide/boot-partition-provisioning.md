# Preparing the card before first boot (boot-partition files)

You can set up Wi-Fi and the capture whitelist **before the device ever boots**,
and install a trained learning-agent model at any time, by dropping files onto
the card's **boot partition**. This is the fastest way to stage a device for the
field: flash the image, drop the files, eject, and the device picks them up when
it boots — no SSH, no screen, no keyboard needed.

## What the boot partition is

When you plug a freshly flashed microSD card into a computer, it mounts a small
**FAT partition** (the only one most desktops will show). That's the boot
partition. Drop the files below into its top level, then eject the card and boot
the device.

Each file is **consumed and deleted on first boot** once its setting is applied,
so credentials don't linger on the removable card.

## Seed Wi-Fi — `kestrel-wifi.txt`

Create a file named exactly `kestrel-wifi.txt`:

```
ssid=MyNetwork
psk=MyPassword
#hidden=yes
#country=GB
#band=5
```

- `ssid=` is required; `psk=` is the passphrase (leave it out for an open network).
- The `#`-commented lines are **optional** — delete the `#` to use one:
  - `hidden=yes` — set only if the network doesn't broadcast its name.
  - `country=GB` — two-letter Wi-Fi regulatory country code.
  - `band=` — `2.4`, `5`, or `any` (the device default is 2.4 GHz).

A line starting with `#`, and any blank line, is ignored, so the commented
examples above are skipped until you uncomment them.

On first boot the device creates the Wi-Fi profile (bound to the built-in chip)
and deletes the file. See [Connecting over Wi-Fi](wifi-setup.md) for details and
other ways to add Wi-Fi.

## Seed the capture whitelist — `kestrel-whitelist.txt`

Create a file named exactly `kestrel-whitelist.txt`, with one entry per
line — an SSID (network name) or a BSSID (MAC):

```
MyHomeWiFi
AA:BB:CC:DD:EE:FF
Neighbour's Network
```

On first boot these are merged into the capture whitelist (`[capture] whitelist`)
and the file is removed. Whitelisted networks are observed but **never actively
attacked**. See [The capture whitelist](capture-whitelist.md) for details.

> The whitelist protects networks from attack, but it does **not** turn capture
> on. Capture is still opt-in — enable it with `sudo kestrel capture enable`
> after you're in. See [Setting up the capture service](capture-setup.md).

## Seed a trained agent model — `kestrel-model*.zip`

If you've trained a learning-agent model (on a desktop with [Remote
training](remote-training.md), or copied from another device), you can install
it by dropping it on the boot partition instead of SSHing in. Drop both of
these at the top level of the boot partition:

- `kestrel-model.zip` — the trained model.
- `kestrel-model.meta.json` — its small companion file (its "sidecar"),
  which names the algorithm the model is for.

Optionally, also drop `kestrel-model-episodes.tar.gz` — the model's
training history, so the device can keep learning from where you left off.

The next time the device boots it installs the model, points the agent at it,
and removes the files it installed. It does **not** turn the agent or capture
on — both stay opt-in. Turn the agent on afterwards with [the agent
guide](agent-setup.md) (`sudo kestrel agent enable`, after capture is
enabled).

### Installing more than one model at once

The device keeps a **separate model for each algorithm**
(`RecurrentPPO`, `PPO`, `DQN`, `QR-DQN`), so you can stage several at once and
all of them are installed. Give each extra model a distinct name of the form
`kestrel-model-<anything>.zip`, each paired with its own
`kestrel-model-<anything>.meta.json` sidecar — for example:

```
kestrel-model.zip              kestrel-model.meta.json
kestrel-model-dqn.zip          kestrel-model-dqn.meta.json
kestrel-model-fast.zip         kestrel-model-fast.meta.json
```

What decides where each model lands is the **algorithm named inside its
sidecar**, not the file name — so two files for the same algorithm would
overwrite each other, and the file name is just a label. A model whose sidecar
is missing or names an algorithm the device doesn't know is skipped and **left
on the card** so you can see and fix it; the others still install.

**Which one becomes active.** After importing, the device picks the active
algorithm like this, in order:

1. the one named in an optional `kestrel-model-active` file (a single line
   containing just the algorithm name, e.g. `DQN`);
2. otherwise the plain `kestrel-model.zip`'s algorithm, if that one
   installed;
3. otherwise, if you dropped exactly one model, that one;
4. otherwise it installs them all but selects none — pick one yourself with
   `sudo kestrel agent algorithm <ALGO>`.

Switching between installed algorithms later is instant: the device reloads that
algorithm's saved model instead of retraining.

## Notes

- Use a plain-text editor and save as UTF-8; don't let it add a `.txt.txt`
  extension or rich-text formatting.
- Raspberry Pi Imager's own customization (Wi-Fi, hostname, SSH) also still
  works alongside these files.
- The files above (Wi-Fi, the capture whitelist, and a trained agent model) are
  the supported boot-partition inputs; everything else is configured on the
  device after first boot.
