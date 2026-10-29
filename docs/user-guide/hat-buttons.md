# The e-Paper HAT's buttons

The Waveshare 2.7" e-Paper HAT that shows the [e-ink face](eink-setup.md) has
four buttons along its edge, KEY1 to KEY4. The `hat-buttons` plugin gives them
jobs you want with no phone at hand. It ships **disabled**; it uses only local
hardware, so turning it on needs no authorized-use acknowledgment.

## Turning it on

On the dashboard's **Plugins** page, enable **hat-buttons** (and **eink**, so
the panel can show what the buttons do). From a terminal:

```bash
sudo kestrel plugins enable hat-buttons
```

## What the buttons do

| Button | Short press | Long press (3 seconds) |
| --- | --- | --- |
| **KEY1** | Info screen for 30 seconds: version, the device's addresses, the dashboard's HTTPS fingerprint, uptime and free space. Press again to close. | — |
| **KEY2** | A reminder of what the long press does | **Stop capture** |
| **KEY3** | A reminder | **Reboot**, after you press KEY3 again |
| **KEY4** | A reminder | **Shut down**, after you press KEY4 again |

- **Reboot and shut down ask first.** After the long press, the panel says
  "Press KEY4 again to shut down"; press it again within 5 seconds to confirm.
  Do nothing and it's cancelled, so a press in a pocket won't power the device
  off.
- **Buttons can stop capture, never start it.** Starting capture takes the
  authorized-use acknowledgment, which you give on the dashboard or in a
  terminal.
- **The dashboard card** shows the last key pressed, which is handy for checking
  the wiring.

## If the buttons don't respond

- The plugin's health note on the Plugins page says why if it couldn't use the
  buttons (for example "buttons unavailable" with no HAT attached).
- The default pins are BCM GPIO 5, 6, 13 and 19, which suit the Waveshare 2.7"
  HAT. A different board or revision may wire them elsewhere: set `pins` to the
  four GPIO numbers for KEY1 to KEY4, in order.

## Options

| Option | Default | Meaning |
| --- | --- | --- |
| `pins` | `5,6,13,19` | BCM GPIO pins of KEY1 to KEY4. |
| `hold_seconds` | `3` | How long a press counts as a long press. |
| `allow_power` | `true` | Let KEY3/KEY4 reboot or shut down. |
| `allow_capture_stop` | `true` | Let KEY2 stop capture. |
