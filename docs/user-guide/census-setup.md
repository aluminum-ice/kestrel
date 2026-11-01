# Census

The `census` plugin counts the Kestrels in use, with no server. Each device with
the plugin on adds a small, anonymous report to the public BitTorrent DHT once
an hour and reads everyone's reports back, so every device can show:

- how many Kestrels are online now and how many were active today;
- which releases they run;
- how their learning agents are set up (off, scripted sweep or learned, and
  which algorithm);
- whether remote training is set up and working for them;
- how often a model trained on a desktop beats the model in use: the card's
  "desktop won X of Y training contests this week".

It's project telemetry: it shows which releases are in the field and whether
desktop training pays off. The plugin ships **disabled**. Because it sends data
off the device, turning it on takes the same authorized-use acknowledgment as
capture.

## Turning it on

From the dashboard's **Plugins** page, or from the terminal:

```bash
sudo kestrel plugins enable census
```

With the defaults it waits for your phone's Bluetooth tether (see
[Which connection does the census use?](#which-connection-does-the-census-use)),
joins the DHT, and writes its first report 5-30 minutes later, then once an
hour at a random minute. It reads the whole census a few minutes after that
first report (so its own report is in the totals), then every `poll_hours`.

## What leaves the device

Once an hour, one 20-byte report:

| Field | What it is |
| --- | --- |
| Token | A random number, new every UTC day, so a device counts once a day and one day can't be linked to the next. |
| Hour | The UTC hour of the report. |
| Release | The device's release version, such as `1.0.12`. A development build reports "unknown". |
| Learning agent | Off, scripted sweep or learned, and the configured algorithm. |
| Remote training | A code: not enabled, enabled but not configured, configured but failing, configured but no new model this week, or working. Never the training host's name, address or user. |
| Contests today | How many model contests on-device and remote candidates won and lost since UTC midnight. |
| Proof-of-work | A number that took the device a second or two to find, which makes flooding the census with fake reports slower. |

Nothing from your captures, no network names, no locations, no handshake
counts. The **Census** page shows exactly what this device reports today, in
words.

## Which connection does the census use?

The computers that store the census (ordinary DHT nodes run by BitTorrent
users) see the internet address each report or read comes from, as any
internet service would. A report doesn't carry that address, so other readers
don't see it.

- **`tether_only` on (the default):** the census goes out only over your phone's
  Bluetooth tether, and pauses while the tether is down. The address the DHT
  sees is your mobile carrier's, shared by many customers, which says little
  about you.
- **`tether_only` off:** the census uses whichever connection the device has.
  Over home or office Wi-Fi the DHT sees that network's own address, which can
  point to where you are. Turn it off only if you're fine with that.

On its first join (or after a long time off) the plugin looks up the two public
DHT routers in `bootstrap` by name, while the tether is up. It saves their
addresses and uses those next time, so the lookup is rare.

## Reading the page

Press **Census** on the plugin's row on the Plugins page:

- **Status:** running or paused (and why), the last report and whether its
  check found it stored, the last read, and the data used today.
- **Today (UTC):** Kestrels online now and active today, the 7-day average,
  and the contest line.
- **Releases, Learning agent, Remote training:** counts and shares of today's
  devices.
- **Training contests:** how often on-device and remote candidates won, today
  and over 7 and 30 days, for all devices and for devices whose remote
  training works.
- **Last 30 days:** one line per day.
- **What this device reports today.**

The numbers are **approximate and unverified**. Anyone can write to the public
census, and only Kestrels with the plugin on are counted. A report stays in the
census about two hours after its device's last write, so "online" means
"reported this hour or the last".

## Options

| Option | Default | Meaning |
| --- | --- | --- |
| `report` | on | Add this device to the census each hour. |
| `read` | on | Fetch the census and show the totals. Off, the plugin only reports, and uses far less data. |
| `poll_hours` | 4 | How often to fetch the whole census, 1-24 hours. |
| `tether_only` | on | Send and fetch only over the phone's Bluetooth tether; pause while it's down. |
| `daily_mb` | 5 | Data budget per UTC day, in MB (0 = no limit). Over it, the plugin stops fetching; over half as much again, it stops reporting too. With the defaults it uses about 2 MB a day. |
| `bootstrap` | the two public DHT routers | Comma-separated `host:port` routers, used only for the first join or when the saved node list has gone stale. |

## Turning it off

```bash
sudo kestrel plugins disable census
```

or the Disable control on the dashboard. Its last report drops out of the
census within about two hours. The plugin's state (today's token, the node list
and the 30-day history) is in `~pi/.local/state/kestrel-census/`; delete that
folder to clear it.

## Troubleshooting

| The card or page says | What to do |
| --- | --- |
| paused: waiting for the clock to be set | The census is keyed by the UTC date, so it waits for time sync. Get the device online; it syncs within a minute or two. |
| paused: waiting for the phone's Bluetooth tether | Connect the phone's tether, or turn `tether_only` off (see above). |
| paused: offline | The device has no internet connection. |
| can't join the DHT | No router or saved node answered. Some networks block BitTorrent traffic; try another connection. It tries again every 10 minutes. |
| can't bind to bnep0 | The tether went away while the plugin was opening its connection. It tries again. |
| paused: over today's data budget | It has used 1.5 × `daily_mb` today and stops until UTC midnight. Raise `daily_mb`, or turn `read` off. |

`sudo journalctl -u kestrel-daemon | grep census` shows what the plugin did.

There's also a desktop script, `tools/census-report/`, that reads the census
from a computer and prints the same totals as plain tables.
