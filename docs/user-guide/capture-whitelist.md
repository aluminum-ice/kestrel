# The capture whitelist

The whitelist is a list of networks that capture must **never actively attack**.
Whitelisted networks may still be *observed* passively (they show up in scans),
but the device will never send deauthentication frames at them. Use it to protect
networks you must not disrupt — your own home or office Wi-Fi, a neighbour's, or
anything you are not authorized to attack.

AngryOxide enforces the whitelist natively, so entries are genuinely never
targeted.

## What goes on the list

Each entry is either:

- an **SSID** (network name) as a string, e.g. `"MyHomeWiFi"`, or
- a **BSSID** (a MAC address) e.g. `"AA:BB:CC:DD:EE:FF"`.

You can mix both. An SSID entry protects every AP broadcasting that name; a BSSID
entry protects one specific radio.

## Setting it on the device

The easiest way is the **web dashboard**. With the dashboard login on (`[web]
auth` is not `none`), open the **Whitelist** page: it's searchable, and you can
add a new AP (an ESSID or a BSSID) or remove one straight from the page. A
change takes effect the next time capture starts. See [Using the web
dashboard](web-dashboard.md).

The same from the terminal, with the dedicated command — it edits just the
whitelist and is safe for names with spaces or quotes:

```bash
sudo kestrel capture whitelist add MyHomeWiFi
sudo kestrel capture whitelist add AA:BB:CC:DD:EE:FF
kestrel capture whitelist list          # show the current list (no sudo)
sudo kestrel capture whitelist remove MyHomeWiFi
```

Both the dashboard and this command edit only the `whitelist` key. A change is
picked up when capture next starts its scan; restart capture to apply it right
away:

```bash
sudo systemctl restart kestrel-capture.service
```

As a last resort you can edit `/etc/kestrel/config.toml` by hand — but
prefer the dashboard or the command above, which validate the change. If you
do, edit only the `whitelist` line under the `[capture]` table, and nothing
else: other tables share key names (like `enabled`), so never do a file-wide
find-and-replace.

```toml
[capture]
whitelist = ["MyHomeWiFi", "AA:BB:CC:DD:EE:FF"]
```

## Pre-loading it before first use (from the boot partition)

You can prepare the whitelist before the card ever boots — handy for staging a
device for the field, and no SSH needed. Drop a file named
`kestrel-whitelist.txt` on the **boot partition** (the FAT partition
visible when you plug the flashed card into a computer), one SSID or MAC per
line. This and the Wi-Fi file are covered together in
[Preparing the card before first boot](boot-partition-provisioning.md):

```
MyHomeWiFi
AA:BB:CC:DD:EE:FF
Neighbour's Network
```

On the next boot the entries are merged into `[capture] whitelist` and the file
is removed — the same way staged Wi-Fi credentials are consumed.

## Verifying

`sudo kestrel capture status` and the daemon log show the active capture
configuration. A whitelisted network should appear in discovery but never be
attacked.
