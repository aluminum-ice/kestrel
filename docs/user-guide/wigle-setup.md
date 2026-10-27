# WiGLE upload setup

The `wigle` reference plugin submits the networks you observe, with their GPS
coordinates, to [wigle.net](https://wigle.net) — the crowd-sourced wardriving
database. It is opt-in and off by default. This is the operator guide; GPS itself
is covered in [GPS setup](gps-setup.md).

## Getting your WiGLE API token (the part that trips everyone up)

WiGLE does **not** authenticate with your website username and password. It uses
a separate pair of **API credentials** tied to your account:

1. Make a free account at [wigle.net](https://wigle.net) and log in.
2. Go to **Account → "Show my token"** (<https://wigle.net/account>).
3. That page shows three things. You need the **first two**, each copied into the
   matching plugin option:

   | WiGLE calls it | Looks like | Plugin option |
   | --- | --- | --- |
   | **API Name** | `AIDxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx` | `api_name` |
   | **API Token** | a long random string | `api_token` |
   | "Encoded for use" | a base64 blob | **not used — ignore it** |

Common mistakes that cause an **HTTP 401**:

- using your wigle.net **login** username/password instead of the API Name/Token,
- pasting the **"Encoded for use"** base64 string into one of the fields,
- swapping **API Name** and **API Token**.

The API Name is the username half and the API Token is the password half of the
credential — keep them in that order. Regenerating the token on the WiGLE site
invalidates the old one, so update the device if you do that.

## Enabling the plugin

The plugin transmits off-device, so enabling it takes the authorized-use
acknowledgment (same as the other uploaders).

From the **web dashboard** (with login on): open **Plugins**, enable **wigle**,
fill in `api_name` and `api_token` (both are masked and never echoed back),
acknowledge the authorized-use prompt, and save. Saving reloads the plugin, so it
picks up the new credentials and starts uploading as soon as it is online — no
daemon restart needed.

The same from the terminal:

```bash
sudo kestrel plugins enable wigle \
    --set api_name='AIDxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx' \
    --set api_token='your-long-api-token'
```

Optional `--set` keys: `geolocate` (default `false`),
`geolocate_max_lookups` (default `5`), `retry_seconds` (default `300`),
`timeout` (default `30`), `endpoint` (default `https://api.wigle.net`). The
`endpoint` must be an `https://` URL: your API name and token are never sent in
clear text, and never sent on to wherever a redirect points (the card shows the
redirect as an error instead).

## Modes: what it will and won't upload

WiGLE uploads only ever carry **real GPS fixes**, so the public dataset is never
polluted with approximate coordinates. Which mode you are in depends on GPS and
the `geolocate` option (the dashboard card shows it as **Mode**):

- **gps** — the `gps` plugin is enabled (or a receiver is providing fixes). The
  plugin uploads observed networks with their real coordinates. This is
  wardriving, and the normal mode. See [`gps-setup.md`](gps-setup.md).
- **gps + geolocate** — GPS is present *and* `geolocate = true`. Observations with
  a real fix upload as in **gps**; any observed while there is no live fix are
  looked up for on-device display only (as in **geolocate** below) and still
  never uploaded. The card names both so it is clear the lookups are active.
- **geolocate** — `geolocate = true` and no GPS. Nothing is uploaded; instead the
  plugin looks up a network's *approximate, already-known* position from WiGLE's
  search API for on-device display only. Those looked-up positions are cached
  separately (`geo/wigle-cache.json`) and are **never** submitted. Lookups are
  cached per network and capped per flush to respect your query quota.
- **idle** — no GPS and `geolocate = false`. Nothing to do: enable the `gps`
  plugin or set `geolocate = true`.

## The dashboard card

The `wigle` card shows:

- **Uploaded** — observations uploaded this daemon session (resets on restart).
- **Queued** — recorded but not yet uploaded.
- **Networks** — unique networks recorded (persists across reboots).
- **Mode** — `gps` / `gps + geolocate` / `geolocate` / `idle`, as above.
- **Upload** — the last upload outcome:
  - `ok` — the last upload was accepted.
  - `waiting (offline)` — there are queued observations but the device has no
    internet; they are held safely and go up when the link returns.
  - `error: HTTP 401 (check api_name/api_token)` — WiGLE rejected the
    credentials. Fix them (see above); the plugin **stops retrying** until you
    change them (which reloads it), so it doesn't hammer WiGLE with a bad token.
  - `error: HTTP <code>` / `error: <message>` — a transport or API error; the
    observations stay queued and it retries.
  - `rejected: <reason>` — WiGLE accepted the connection but refused the upload
    itself (rare: a content or quota issue).

## When it uploads

An observation is recorded on each handshake that has a live GPS fix, and the
queue is flushed: on a new handshake while online, on every reconnect, and every
`retry_seconds` (default 300s) while anything is queued. The queue and the
already-seen set persist across reboots, so nothing is lost if the device
restarts before a chance to upload.

## Troubleshooting

- **`error: HTTP 401`** — credentials, the number-one issue. Re-check API Name vs
  API Token per the table above (not your login, not the "Encoded for use"
  string). After you re-save them the card flips to `ok` within a few seconds.
- **`waiting (offline)`** — a connectivity problem, not a WiGLE one. Check the
  dashboard's Internet row, or on the device `nmcli networking connectivity` and
  `curl -sS -m 10 -o /dev/null -w '%{http_code}\n' https://api.wigle.net`.
- **Nothing ever queues (Mode `idle`)** — no GPS fixes are reaching the plugin.
  Enable and confirm the `gps` plugin ([`gps-setup.md`](gps-setup.md)).
- **Live state**: `sudo kestrel plugins info wigle`, and the daemon log with
  `sudo journalctl -u kestrel-daemon | grep -i wigle`.
- Only upload observations you are authorized to collect and share.

## Where the data lives

- Upload queue + dedup set: `/var/lib/kestrel/capture/.wigle-state.json`
- Geolocate lookup cache (display-only, never uploaded):
  `/var/lib/kestrel/capture/geo/wigle-cache.json`
