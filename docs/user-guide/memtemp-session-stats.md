# memtemp and session-stats

Two small, local-only reference plugins that put device health and session
counters on the dashboard. Neither sends anything off the device, so neither
needs the authorized-use acknowledgment — just enable and go. The easiest way to
enable either is the dashboard **Plugins** page (with login on); the terminal
commands below do the same. For managing plugins in general see [Managing
plugins](managing-plugins.md).

## memtemp — memory, CPU, temperature (and fan)

Shows a readout card with:

- **Memory** — percent used (drawn as a bar on the web card).
- **CPU load** — the 1-minute load average.
- **Temperature** — CPU temperature.
- **Fan** — speed and/or RPM, **only if a fan is detected** (e.g. the Raspberry
  Pi 5 active cooler). Boards with no fan show no fan row.

Enable it from the dashboard **Plugins** page, or the terminal:

```bash
sudo kestrel plugins enable memtemp
```

Option:

- `fahrenheit` (default `false`) — show temperature in °F instead of °C:

  ```bash
  sudo kestrel plugins enable memtemp --set fahrenheit=true
  ```

## session-stats — per-session counters

Counts activity for the current daemon session and shows a readout card with:

- **Handshakes** — handshakes seen this session.
- **Unique APs** — distinct access points seen this session.
- **Epochs** — event-loop ticks elapsed.
- **Uptime** — how long the daemon has been running.

These reset when the daemon restarts (they count the current session, not the
all-time capture store — that lives on the [Captured networks](web-dashboard.md)
page).

Enable it from the dashboard **Plugins** page, or the terminal:

```bash
sudo kestrel plugins enable session-stats
```

It has no options. It also serves a tiny page and a JSON feed on the management
link at `/plugins/session-stats/` (`stats.json` for the raw numbers).

## Seeing the cards

Both appear as cards on the device web dashboard once enabled (and in the daemon
log). See [Using the web dashboard](web-dashboard.md).
