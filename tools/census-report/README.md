# Census report

A desktop script that reads the Kestrel census from the public BitTorrent DHT
and prints it as plain tables: how many Kestrels reported on a UTC day, which
releases they run, how their learning agents are set up, whether remote
training works for them, and how their training contests went. It's for seeing
which releases are in the field without a device.

The devices write the census with the `census` plugin (opt-in, off by default).
This script only reads the same public slots a device reads. Nothing reports to
it, and it writes nothing to the DHT. The format is `kestrel-census/1`, in
[docs/developer/census-protocol.md](../../docs/developer/census-protocol.md).

| File | What it is |
| --- | --- |
| `census_report.py` | The script. It loads the plugin's own helper files (`builder/extra/files/plugins/_census_dht.py` and `_census_proto.py`) by path, so it and the devices can't disagree about the format. Not shipped in the image. |

Its tests are in `builder/extra/files/kestrel_pi/tests/test_census_report.py`,
so `make test` runs them; they read a fake, in-process DHT.

## Running it

From a checkout of this repository, with Python 3 and `cryptography`
(`pip install cryptography` if it's missing):

```bash
python3 tools/census-report/census_report.py                 # today, UTC
python3 tools/census-report/census_report.py --day 2027-01-14
python3 tools/census-report/census_report.py --csv ~/census  # also writes ~/census/census-DAY.csv
```

| Option | Default | What it does |
| --- | --- | --- |
| `--day YYYY-MM-DD` | today (UTC) | The UTC day to read. Slots stay readable for a few hours after their last write, so read a day before it's long over. |
| `--csv DIR` | none | Also write `DIR/census-DAY.csv` (`day,table,item,value`), rewritten on each run. |
| `--state DIR` | `~/.cache/kestrel-census-report` | Where it keeps its list of DHT nodes between runs, so later runs don't need the bootstrap routers. |
| `--bootstrap host:port,...` | the plugin's default routers | The DHT routers for the first join. |
| `--parallel N` | 8 | Slots read at once (1-16), each with its own socket. |

A full read takes about two minutes, reading 8 slots at a time, and a few
hundred KB of traffic. Each slot needs its own lookup through the DHT, so one
slot at a time (`--parallel 1`) takes over ten minutes. It exits 0
with "No reports found." when the day's slots are empty, and 2 when it can't
join the DHT.

Example output (invented numbers):

```text
Kestrel census, 2027-01-15 (UTC), read 15:42Z - approximate and unverified
Devices: online now 23 · reported today 41

Release        Devices  Share
1.0.12              31    76%
1.0.12-beta.3        4    10%
1.0.11               5    12%
unknown              1     2%

Agent           Devices  Share
learned              29    71%
scripted sweep        7    17%
not running           4    10%
no agent              1     2%
```

The release, learned-algorithm, remote-training and contest tables follow.

To keep a history, run it from cron late in each UTC day with `--csv`:

```cron
40 23 * * * cd /path/to/kestrel && python3 tools/census-report/census_report.py --csv ~/census >/dev/null
```

## What the numbers mean

- **Approximate and unverified.** Only devices with the census plugin on are
  counted, and contest counts a day can't hold are left out.
- **"Reported today"** counts the devices whose entry is in a slot when the
  script reads it. An entry stays in its slot about two hours after its
  device's last write, so a device that reported early and went offline may be
  missed by a read late in the day.
- **Each device has a new random token every UTC day,** so days can't be added
  up into a count of devices. Early in a UTC day the script suggests reading
  the day before instead.
- **"Online now"** is the devices that wrote this hour or the last.
