# Connecting over Bluetooth (phone tether / PAN)

Bluetooth PAN (Personal Area Network) is the primary way to reach a headless
device with no Ethernet: you pair your phone once, turn on its tethering, and the
device uses the phone's connection for management and internet. It uses the Pi's
built-in Bluetooth — the USB capture dongle is never involved — so management and
capture never contend.

## Pairing your phone (the onboarding window)

Pairing is **Just Works** (no PIN to type), but it's only possible during a
short, time-limited **onboarding window**, so a stranger can't pair whenever they
like.

1. **Turn your phone's tethering (Personal Hotspot) ON first.** The device only
   gets a usable connection from a phone that is advertising tethering, so enable
   it *before* you pair.
2. On a fresh device the onboarding window opens automatically on every boot
   until a phone is paired, and stays open for about 10 minutes
   (`BT_ONBOARD_MINUTES`).
3. On your phone's Bluetooth screen, look for a device named **`kestrel-XXXX`**
   (the `XXXX` comes from the adapter's address) and tap to pair. Accept the
   pairing prompt.
4. The first phone that pairs is trusted and remembered; the window then closes.

After pairing, the device brings the PAN link up automatically on every boot and
retries every 30 seconds, so it reconnects on its own after a reboot or after you
toggle tethering off and on.

**iPhone vs Android** differ in where tethering lives (iPhone: *Settings →
Personal Hotspot*; Android: *Settings → Network → Hotspot & tethering → Bluetooth
tethering*) and in the subnet they hand out — the device handles both.

## Logging in over the link

Once the PAN link is up, SSH to the device over it:

```bash
ssh pi@kestrel.local
# or use the device's address on the tether subnet (e.g. 172.20.10.14 on iPhone)
```

`sudo kestrel network status` shows whether the PAN link is up and its IP.

## Re-pairing a different phone

To forget the current phone and pair a new one:

```bash
sudo kestrel network bt-pair
```

This unpairs the recorded phone, removes its network profiles, and reopens the
onboarding window so you can pair again.

## If it says "internet offline" but the link is up

A tethering phone often carries the device's traffic over IPv6 while advertising
only a *link-local* IPv6 DNS server, which Linux can't actually use for lookups —
so the link works but names don't resolve, and the dashboard reads "offline."

The device works around this by applying a routable fallback DNS
(`BT_PAN_FALLBACK_DNS` in `/etc/default/kestrel`, Cloudflare's IPv6
resolvers by default) to the PAN link. If you'd rather use only the DNS your
phone hands out, blank that setting and restart the service (or reboot).

## Where the settings live

Bluetooth and PAN tunables are in `/etc/default/kestrel`
(`BT_ONBOARD_MINUTES`, `BT_PAN_DHCP_TIMEOUT`, `BT_PAN_RETRY_SECONDS`,
`BT_PAN_STATIC_ADDRESSES`, `BT_PAN_FALLBACK_DNS`). Edit, then reboot or restart
the relevant `kestrel-bt-*` service.

## Notes

- Just Works means anyone in range *during an open window* could pair first. The
  window is short, opens only when no phone is recorded (or on explicit request),
  and closes on the first pairing.
- NetworkManager only offers a PAN connection to a phone whose Bluetooth services
  include tethering, which phones advertise while tethering is on — another
  reason to turn tethering on before pairing.
