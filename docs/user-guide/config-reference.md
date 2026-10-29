# The configuration file reference (`config.toml`)

Almost everything about how the device captures, runs plugins, serves the
dashboard and runs the learning agent is controlled by one file,
`/etc/kestrel/config.toml`. This page describes **every setting** in it:
what it does, the values it accepts, and the shipped default.

**You rarely edit this file by hand.** Prefer the web dashboard (it validates a
change and writes one setting at a time), or the `kestrel` commands, which
do the same safely. On the dashboard, the core tables below (`[capture]`,
`[agent]`, `[daemon]`, `[web]`) have a per-key editor on the **Advanced
settings** page — the discreet link at the bottom of the dashboard, or
`/settings` — and each plugin's options are on its own plugin page. If you do
edit the file directly, change **one key at a time** — several sections reuse the
same key name (`enabled` appears in many), so a careless find-and-replace can
flip settings you didn't mean to. Restart the affected service afterward (or
reboot).

A second file, `/etc/default/kestrel`, holds low-level network timings; it
is covered at the end.

> **Secrets are set at enable time, not here.** Plugin passwords and tokens
> (`wpa-sec` key, `wigle` credentials, the `updates` token, the dashboard
> password) are collected when you enable the plugin or set the login, and are
> never shown back. Don't type them into this file by hand.

---

## `[capture]` — the capture service

| Key | Values | Default | What it does |
| --- | --- | --- | --- |
| `enabled` | `true` / `false` | `false` | Whether capture runs. Turn it on with the capture control or `capture enable` (which records the authorized-use acknowledgment), not by editing this. |
| `interface` | `auto` or an interface name (`wlan1`) | `auto` | Which USB adapter captures. `auto` picks the monitor-capable one that isn't the built-in chip; set a name only if detection chooses wrong. |
| `whitelist` | list of names/MACs | `[]` | Networks never actively attacked (still observed). Manage it from the whitelist control or `capture whitelist`, not as raw text. |

## `[capture.scan]` — how the field scan behaves

| Key | Values | Default | What it does |
| --- | --- | --- | --- |
| `mode` | `sweep` / `autohunt` / `fixed` | `sweep` | `sweep` rotates through channels; `autohunt` scans once then locks onto channels with targets; `fixed` uses the `bands` list. |
| `dwell_seconds` | whole number ≥ 1 | `5` | Seconds spent on each channel before moving on. Lower revisits faster (good when moving); higher gives more time to catch a handshake. |
| `bands` | `auto`, a single band (`2`/`5`/`6`/`60`), or a channel list (`1,6,11,36`) | `auto` | Which channels the scan covers. `auto` is every channel the adapter supports. `2` means the 2.4 GHz band. |

## `[capture.output]` — where handshakes are written

| Key | Values | Default | What it does |
| --- | --- | --- | --- |
| `mode` | `per-ssid` / `per-ap` / `single` | `per-ssid` | One file per network (`per-ssid`), one file per access point keyed on BSSID so unrelated APs sharing a name never merge (`per-ap`), or one combined file (`single`). All are only ever appended to. |
| `single_file` | filename | `all.hc22000` | The combined file's name when `mode = single`: a plain file name in the store (letters, digits, `.`, `_`, `-`; no `/`, no leading `.`). Anything else is refused, and the collector falls back to `all.hc22000`. |

## `[capture.angryoxide]` — the capture engine

| Key | Values | Default | What it does |
| --- | --- | --- | --- |
| `headless` | `true` / `false` | `true` | The background service always runs headless; `false` only shows the capture tool's text UI when you launch it by hand in a terminal. Leave `true`. |
| `extra_args` | list of strings | `[]` | Extra flags passed straight to the capture engine, e.g. `["--notransmit"]` for passive-only (no deauth). Advanced — a bad flag stops capture. |

## `[capture.environment]` — the in-range readout

Shows how busy the RF environment is (distinct radios nearby — access points
and clients together) on the dashboard and e-ink face. Derived from what the
capture radio sees, so it only reads while capture runs; with capture off it
shows "unknown", never a misleading zero.

| Key | Values | Default | What it does |
| --- | --- | --- | --- |
| `enabled` | `true` / `false` | `true` | Show the environment readout. `false` hides it everywhere. |
| `window_s` | whole number ≥ 1 | `300` | How many seconds of recent capture data counts as "recently seen". Anchored to the newest packet, not the clock, so a bursty radio reads steadily rather than empty. A density reading over the window, not an instant count. |
| `max_age_s` | whole number ≥ 1 | `900` | How old the newest capture data may be before the card reads "as of N ago" and, past this, falls back to "unknown" instead of showing a stale count as current. |
| `thresholds` | four ascending whole numbers | `[0, 10, 30, 60]` | Radio-count cut-offs for the Quiet / Light / Moderate / Busy / Dense labels (anything above the last is Dense). The count is APs and clients together, so these run higher than an APs-only count. A city centre and a rural lane differ — tune to taste. |

## `[capture.housekeeping]` — keeping the store from filling the card

| Key | Values | Default | What it does |
| --- | --- | --- | --- |
| `action` | `off` / `rotate` / `archive` | `off` | Daily maintenance policy. `off` does nothing automatically; `rotate` archives when a limit below is hit; `archive` snapshots every day. It never deletes. |
| `max_store_mb` | whole number (`0` = no limit) | `0` | Store size that triggers `rotate`. |
| `max_age_days` | whole number (`0` = no limit) | `0` | Store age that triggers `rotate`. |
| `archive_dir` | path inside `/var/lib/kestrel/capture/` | `/var/lib/kestrel/capture/archive` | Where archives are written. It must be a folder inside the capture store: housekeeping runs as `pi` and never writes or deletes anywhere else, and a value outside the store is refused (the web page won't save it; housekeeping skips archiving and logs why). |
| `keep_archives` | whole number (`0` = keep all) | `0` | Keep only the newest N archive sets. |
| `max_run_mb` | whole number (`0` = no cap) | `512` | Live cap on the raw packet-capture working directory (not your handshakes). At the cap, capture rolls and drops the raw files; handshakes are always kept. `0` can fill the card on a long session. |

## `[agent]` — the learning capture agent

| Key | Values | Default | What it does |
| --- | --- | --- | --- |
| `enabled` | `true` / `false` | `false` | Whether the agent drives capture (needs capture on). Use the agent control or `agent enable`. |
| `algorithm` | `RecurrentPPO` / `PPO` / `DQN` / `QR-DQN` | `RecurrentPPO` | Which reinforcement-learning algorithm to use. |
| `policy` | `baseline` / `learned` | `baseline` | What actually drives capture: the scripted sweep, or a trained model. `learned` needs a trained or imported model. |
| `model` | path | `/var/lib/kestrel/agent/model.zip` | Where the learned model is read from. Managed by training/import. |
| `decision_seconds` | whole number ≥ 1 | `30` | How long each scan posture runs before the agent can change it. Each change restarts the scan, so keep it coarse. |
| `log_episodes` | `true` / `false` | `true` | Log episodes for later training. Off stops the agent learning from new data. |
| `train` | `auto` / `off` / `idle` | `auto` | Whether on-device training runs. `auto` trains only on boards with enough RAM. |
| `train_while_capturing` | `auto` / `true` / `false` | `auto` | Fine-tune during capture (low-priority) or only when idle. |
| `train_min_new_episodes` | whole number ≥ 1 | `60` | New episodes needed before a fine-tune run. |
| `train_timesteps` | whole number ≥ 1 | `3000` | Training steps per fine-tune (short by design). |
| `train_max_temp_c` | whole number (hard max 85) | `75` | Skip training at or above this CPU temperature. |
| `train_min_free_mb` | whole number (hard floor 256) | `512` | Skip training below this much free memory. |
| `promotion_margin` | number ≥ 0 | `0.10` | How much better (as lift over the scripted baseline) a pulled remote model must score on a held-out slice before it replaces the on-device model. `0.10` = 10%. |
| `promotion_max_stale_episodes` | whole number ≥ 0 | `900` | Don't adopt a remote model trained on data this many episodes behind what the device has since logged, even if it scores well. `0` disables the guard. |
| `interleave_compare` | `true` / `false` | `false` | Benchmark mode: alternate the sweep and the agent for a fair comparison. |
| `compare_block_minutes` | whole number ≥ 1 | `10` | Length of each comparison block. |
| `metrics_window_minutes` | whole number ≥ 1 | `60` | Rolling window for the live agent-vs-sweep numbers. |
| `log_max_mb` | whole number ≥ 1 | `64` | Cap on the episode logs so the agent folder stays bounded. |

### `[agent.action]` and `[agent.reward]` (advanced)

These define the agent's action vocabulary (`postures`, `lock_subset_n`,
`dwell_buckets`, `transmit_modes`) and its reward weights (`per_handshake`,
`time_penalty`, `idle_penalty`). They are exposed for tuning and documented in
[The learning capture agent](agent-setup.md); the defaults suit most use and you
can leave them alone.

## `[daemon]` — the plugin host

| Key | Values | Default | What it does |
| --- | --- | --- | --- |
| `enabled` | `true` / `false` | `true` | The background service that hosts plugins and serves the dashboard. Leave on. |
| `epoch_seconds` | whole number ≥ 1 | `60` | How often plugins get their periodic tick. |
| `max_hook_seconds` | whole number ≥ 1 | `30` | A plugin action running longer than this is logged as slow, so its lateness is visible. A plugin is not stopped for being slow — only for getting stuck (below). |
| `hook_stuck_seconds` | whole number (`0` = off) | `300` | A plugin action still running this long has stopped responding (not just slow), so the plugin is stopped to keep it from tying up the pool. `0` disables. |
| `hook_workers` | whole number ≥ 1 | `4` | How many plugin actions can run at once. |
| `capture_poll_seconds` | whole number ≥ 1 | `5` | How often new handshakes are picked up. |
| `connectivity_poll_seconds` | whole number ≥ 1 | `15` | How often the internet connection is checked. |

## `[web]` — the dashboard

| Key | Values | Default | What it does |
| --- | --- | --- | --- |
| `enabled` | `true` / `false` | `true` | Whether the dashboard is served. |
| `bind` | `management` | `management` | Serve only over the management link (your tether/Wi-Fi), never the capture dongle. Leave as is. |
| `port` | 1–65535 | `8080` | The dashboard's plain-HTTP port. With `tls` on it serves the device itself (`127.0.0.1`) and only forwards to HTTPS on the management links. Changing it drops your current session and needs a daemon restart. |
| `tls` | `on` / `off` | `on` | Serve the dashboard over HTTPS on the management links, with a sign-in page. Set it with `sudo kestrel web tls on\|off`. |
| `tls_port` | 1–65535 | `8443` | The HTTPS port. Changing it drops your current session and needs a daemon restart. |
| `refresh_seconds` | whole number (`0` = off) | `30` | How often the read-only dashboard refreshes itself. Below `epoch_seconds` it just re-draws the same numbers. |
| `webhook_timeout_seconds` | whole number (`0` = off) | `30` | How long a plugin's own page or action may run before the browser gets a "still working" page instead of hanging; the plugin's work keeps going in the background. `0` waits forever. |
| `auth` | `system` / `basic` / `token` / `none` | `system` | How the dashboard logs you in. `system` uses your device account (same as SSH). Set this with `web auth`, not by hand — a wrong value can lock you out. |
| `username` | text | `""` | `system`: the account to check (default: the login user). `basic`: the web username. |
| `show_passphrases` | `true` / `false` | `true` | Show a recovered network's passphrase in its card on the Captured networks page. Needs the web login on; off keeps the "recovered" badge and count but hides the passphrase (for screen-sharing). Takes effect immediately. |
| `show_locations` | `true` / `false` | `true` | Show a captured network's location (lat/long) on the Captured networks page as an OpenStreetMap link. Needs the web login on; off keeps a muted "location hidden" marker but hides the coordinates (for screen-sharing). Takes effect immediately. |
| `probe_url` | URL or empty | `""` | An explicit address to test for "online". Empty uses the built-in check. |

The dashboard `password` and `token` are secrets set by `web auth`; they are not
shown here and shouldn't be typed in by hand.

## `[recovered]` — imported passphrases

Known passphrases for captured networks are parsed from any `*.potfile` dropped
in the recovered store (`<store>/cracked/`): the wpa-sec plugin, a web upload, a
CLI import, or a file copied in over SSH. The device never cracks anything — it
only displays results you produced elsewhere, for networks you are authorized to
test. These caps bound an upload or import; both are read live (no restart).

| Key | Values | Default | What it does |
| --- | --- | --- | --- |
| `max_import_mb` | whole number ≥ 1 | `2` | Largest potfile an import accepts, in MB. |
| `max_import_lines` | whole number ≥ 1 | `50000` | Most lines read from an imported potfile; any beyond this are skipped. |

## `[plugins.*]` — the plugins

Every plugin has an `enabled` key (`true`/`false`), set by enabling or disabling
it rather than by hand. Each plugin's other options are described in its own
guide. Secret options (API keys/tokens) are collected at enable time and masked.

| Plugin | Key non-secret options | Guide |
| --- | --- | --- |
| `memtemp` | — | [memtemp and session-stats](memtemp-session-stats.md) |
| `session-stats` | — | [memtemp and session-stats](memtemp-session-stats.md) |
| `gps` | `source` (`auto`/`gpsd`/`serial`), `host`, `port`, `device`, `baud` | [GPS](gps-setup.md) |
| `wpa-sec` | `endpoint`, `download_results`, `results_interval_seconds` | [wpa-sec](wpa-sec-setup.md) |
| `wigle` | `endpoint`, `geolocate` | [WiGLE](wigle-setup.md) |
| `backup` | `host`, `user`, `path`, `port`, `tool` (`rsync`/`scp`), `agent_dir`, `interval_seconds`, `timeout` | [Remote backup](backup-setup.md) |
| `ups-hat` | `i2c_bus`, `address`, `shutdown_percent` (`0` = monitor only), `poll_seconds` | [UPS HAT](ups-hat-setup.md) |
| `eink` | `variant` (`auto`/`v2`/`v1`), `rotation`, `refresh_seconds`, `show_mascot`, `show_brand_glyph`, `battery_low_percent`, `bored_epochs`, `sad_epochs` | [e-ink face](eink-setup.md) |
| `updates` | `repo`, `check_interval_seconds`, `timeout` | [Keeping the device updated](updates.md) |
| `remote-train` | `train_host`, `train_user`, `train_path`, `train_port`, `algorithms`, `remote_train_min_new_episodes`, `timesteps`, `interval_seconds`, `timeout` | [Remote training](remote-training.md) |

---

## The network-settings file (`/etc/default/kestrel`)

A few low-level network timings live in a separate file, `KEY=value` per line.
They rarely need changing; restart the networking service (or reboot) after an
edit.

| Key | Values | Default | What it does |
| --- | --- | --- | --- |
| `BT_ONBOARD_MINUTES` | whole number ≥ 1 | `10` | How long the Bluetooth pairing window stays open each boot until a phone is paired. |
| `BT_PAN_DHCP_TIMEOUT` | whole number ≥ 1 | `30` | Seconds to wait for an address from the phone before using the static fallback. |
| `BT_PAN_RETRY_SECONDS` | whole number ≥ 1 | `30` | Seconds between Bluetooth reconnect attempts. |
| `BT_PAN_STATIC_ADDRESSES` | comma-separated addresses | iPhone + Android tether ranges | Fallback addresses used only when the phone gives none. Advanced. |
| `BT_PAN_FALLBACK_DNS` | comma-separated IPs, or empty | Cloudflare IPv6 | Name-servers for the Bluetooth link, so lookups work when the phone only offers an unusable one. Empty = use only the phone's. |
| `WIFI_BAND` | `2.4` / `5` / `any` | `2.4` | The built-in chip's Wi-Fi band. Change the saved network's band with `network wifi --band`. |
