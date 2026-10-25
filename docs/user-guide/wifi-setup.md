# Connecting over Wi-Fi

The public kestrel image bakes in **no** Wi-Fi network — you add your own.
Wi-Fi uses the Pi's **built-in** chip and is a convenience path for management;
the external USB capture dongle is never touched by it. Bluetooth tethering to a
phone is the other way in (see [Bluetooth](bluetooth-setup.md)), and both can be
up at once.

The Wi-Fi station profile is bound to the built-in chip's driver (`brcmfmac`), so
it can never land on the capture dongle even if interface numbers change.

## Option A — add Wi-Fi from the device

Once you're logged in (over Bluetooth, Ethernet, or a keyboard/monitor), the
`kestrel` tool saves a network for you:

```bash
sudo kestrel network wifi --ssid "MyNetwork" --psk "MyPassword"
```

Useful flags:

- `--hidden` — the network doesn't broadcast its name.
- `--country GB` — set the Wi-Fi regulatory country (two-letter code).
- `--band 2.4|5|any` — which band to use (default **2.4 GHz**, see below). Run
  `sudo kestrel network wifi --band 5` on its own to change the saved
  network's band later.

`sudo kestrel network` opens a menu for the same thing, and
`sudo kestrel network status` shows whether Wi-Fi is connected and its IP.

You can also use Raspberry Pi OS's own tools directly — `sudo nmtui` (a simple
menu) or `nmcli` — since the device runs NetworkManager.

## Option B — stage Wi-Fi before first boot

To have the device join your network on its very first boot — no SSH needed —
drop a file named `kestrel-wifi.txt` onto the **boot partition** (the small
FAT partition you see when you plug the freshly flashed card into a computer).
This and the whitelist file are covered together in
[Preparing the card before first boot](boot-partition-provisioning.md):

```
ssid=MyNetwork
psk=MyPassword
hidden=yes
band=5
```

Only `ssid=` and `psk=` are required; `hidden=` and `band=` are optional. On the
next boot the device creates the Wi-Fi profile and **deletes the file** so the
passphrase doesn't linger on the boot partition. Raspberry Pi Imager's own Wi-Fi
customization also still works.

## Why 2.4 GHz is the default

The default band is 2.4 GHz because it works on every board (the Pi Zero 2 W is
2.4 GHz only), and because some mesh routers' 5 GHz radios refused the Pi during
testing while 2.4 GHz connected fine. Set `--band 5` or `--band any` if you
prefer 5 GHz and your board and router support it.

## Troubleshooting

- **It won't connect** — check the passphrase, and try `--band 2.4` if you set a
  5 GHz-only band on a 2.4 GHz board (Zero 2 W).
- **Check state**: `sudo kestrel network status`, or `nmcli device status`.
- **Finding the device** on the network: `ssh pi@kestrel.local`, or look in
  your router's client list for the IP.
