# First boot: log in and change the default password

The public kestrel image ships with a **documented default login** so you
can get in the first time:

- username **`pi`**
- password **`raspberry`**

This default is public, so **change the password before you use the device for
anything real.** This guide covers logging in, changing the password, and the
handful of other first-run settings.

## Log in over SSH

SSH is enabled out of the box. From another machine on the same network:

```bash
ssh pi@kestrel.local      # default hostname is "kestrel"
# or, if .local names don't resolve on your network, use the device's IP:
ssh pi@192.168.x.y
```

Answer `yes` to the first-connection host-key prompt, then enter the default
password `raspberry`.

(If `kestrel.local` doesn't resolve, your network may not do mDNS — find the
device's IP from your router's client list, or connect it over Bluetooth/Wi-Fi
first.)

## Change the password (do this first)

Once logged in:

```bash
passwd
```

Enter the current password (`raspberry`), then your new password twice.

**This matters for more than SSH:** the device's web dashboard login uses this
same account (the `[web] auth = system` default verifies your login against the
device account), so changing the Pi password changes the **web dashboard login at
the same time** — the two never drift apart, and there is no separate web
password stored anywhere. One credential covers both SSH and the dashboard.

## Other first-run settings (optional)

Run `sudo raspi-config` for a menu, or use these directly:

- **Hostname** (how the device shows up on the network): `sudo raspi-config` →
  *System Options → Hostname*, then reboot.
- **SSH on/off**: enabled by default; toggle it under `sudo raspi-config` →
  *Interface Options → SSH* if you ever want it off.
- **Wi-Fi**: the public image bakes in **no** Wi-Fi network. See
  [Connecting over Wi-Fi](wifi-setup.md) to add one, or connect the device over
  [Bluetooth](bluetooth-setup.md) instead. You can also seed Wi-Fi before first
  boot — see [Preparing the card before first boot](boot-partition-provisioning.md).

## Building your own image with the password already set

If you build the image yourself rather than flashing the public one, you can bake
in your own password hash instead of the `pi`/`raspberry` default: set the
`MY_PI_PASSWORD_HASH` environment variable at build time (generate a hash with
`openssl passwd -6`). Keep that hash private and supply it only at build time.
End users who flashed the public image just use `passwd` as above.
