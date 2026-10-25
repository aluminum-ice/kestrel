# GPS setup

The `gps` reference plugin is the device's single position source: it keeps the
current fix, tags captured handshakes with a location sidecar, and shares the
fix with other plugins (e.g. `wigle`) and the dashboard. This guide covers
getting a receiver working.

## What the image ships

The image does **not** install `gpsd`. A USB GPS receiver works without it,
because the plugin can read the receiver's serial device directly. Install
`gpsd` only if you specifically want it (see below).

## The `source` option

`[plugins.gps] source` selects the transport:

| `source`            | Behaviour |
| ------------------- | --------- |
| `auto` *(default)*  | Use `gpsd` if it is running; otherwise read the serial device in `[plugins.gps] device` when it is present. A plugged-in USB receiver works with no configuration. |
| `serial`            | Only read the serial device (never try `gpsd`). |
| `gpsd`              | Only talk to `gpsd` at `host`/`port` (never read a serial device). |

Most people never change this: plug a USB GPS in, enable the plugin, done.

## USB serial receiver (the common case)

A USB GPS (e.g. a u-blox dongle) enumerates as a serial device, usually
`/dev/ttyACM0` (CDC-ACM) or `/dev/ttyUSB0` (FTDI/PL2303/CP210x). Check which:

```bash
ls -l /dev/ttyACM* /dev/ttyUSB*
dmesg | grep -iE 'usb|tty|gps' | tail
```

Confirm it is streaming NMEA (you are in the `dialout` group, so no `sudo`):

```bash
cat /dev/ttyACM0      # expect $GPGGA / $GNGGA / $GPRMC lines; Ctrl-C to stop
```

In a `*GGA` line, field 6 is the fix quality (`0` = no lock yet) and field 7 is
the number of satellites used. In a `*RMC` line, `A` = valid, `V` = no fix.

Now enable the plugin. The `auto` default picks the serial device up on its own;
pin `source=serial` (and `device`) only if you want to force it or your receiver
is at a non-default path.

From the **web dashboard** (with login on): open **Plugins**, enable **gps**, and
— only if you need to pin the transport — set `source` to `serial` and `device`
to the path you found above, then save.

The same from the terminal:

```bash
sudo kestrel plugins enable gps --yes
# or, to pin the device explicitly:
sudo kestrel plugins enable gps --yes --set source=serial --set device=/dev/ttyUSB0
sudo kestrel plugins info gps      # check the source and that it loaded
```

`baud` is ignored for USB CDC-ACM receivers (they ignore the line speed); set it
only for a true UART (`/dev/ttyAMA*`, `/dev/ttyS*`) that needs a specific rate.

## Using gpsd instead

If you prefer `gpsd` (for example to share one receiver with other software):

```bash
sudo apt install gpsd gpsd-clients
# point gpsd at the receiver and start it (adjust the device):
sudo dpkg-reconfigure gpsd          # or edit /etc/default/gpsd: DEVICES="/dev/ttyACM0"
sudo systemctl enable --now gpsd
cgps -s                              # should show mode 2D/3D and satellites
gpspipe -w -n 5                      # the exact JSON stream the plugin reads
```

With `gpsd` running, `source = auto` uses it automatically. Use `source = gpsd`
to require it (the plugin then never falls back to a serial device).

## Verifying the fix on the device

Once a plugin source is working, the GPS card on the web dashboard shows the fix
status (`no fix` / `2D fix` / `3D fix`), satellites in use, and
latitude/longitude/altitude. Satellites and the fix stay at `no fix` / `0` until
the receiver actually locks, which needs a reasonably clear view of the sky; a
cold start can take a few minutes. Indoors it may never lock even though data is
flowing — the raw `cat` above is the proof the data path is good.

## Troubleshooting "no fix"

1. `source` is `gpsd` but `gpsd` is not installed/running → switch to `auto` or
   `serial`, or install `gpsd`. (`systemctl is-active gpsd`,
   `ss -lnt | grep 2947`.)
2. No serial device node → the receiver is not plugged in or not enumerating
   (`ls -l /dev/ttyACM* /dev/ttyUSB*`, `dmesg`).
3. Device streams NMEA but quality is `0` / `V` → the receiver has no lock yet;
   give it sky view and time.
4. Permission denied on the device → the account must be in the `dialout` group
   (the image adds `pi` already; `groups` to confirm).
