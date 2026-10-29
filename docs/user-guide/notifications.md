# Notifications

Two plugins send short alerts to your phone when something worth knowing
happens: new handshakes, a low battery, an update, or capture stopping. Pick the
one for the service you use:

- **`ntfy`** sends to an ntfy topic (an app and server you can run yourself);
- **`telegram`** sends to your Telegram chat through a bot.

Each holds only its own service's settings, and both send the same alerts. Both
ship **disabled**. Because they send data off the device, turning one on takes
the same authorized-use acknowledgment as capture. You can run both, though one
is usually enough.

## What you get

| Alert | When | On by default |
| --- | --- | --- |
| `handshakes` | A digest of new handshakes every `digest_minutes` (default 30): "12 new handshakes from 3 networks in the last 30 minutes." | yes |
| `battery` | The battery falls to `battery_percent` (default 20), or the device switches to battery power. Needs the `ups-hat` plugin. | yes |
| `update` | A newer Kestrel release is available (needs the `updates` plugin). | yes |
| `capture` | Capture stopped while it's still switched on. | yes |
| `online` | The device is online after start-up. | no |

Choose with the `events` option, a comma-separated list such as
`handshakes,battery`.

**Alerts carry counts, not capture data.** The handshake digest says how many,
never which networks, unless you turn on `include_names`. Alerts never include
network addresses, locations, handshakes or passwords. Don't paste captured
material into a chat yourself either: only networks you own or have permission
to test belong on the device, and what it captured stays there.

## Setting up ntfy

1. Install the **ntfy** app on your phone.
2. Enable the `ntfy` plugin (dashboard **Plugins** page, or
   `sudo kestrel plugins enable ntfy`) and accept the acknowledgment. Leave
   `topic` empty: the plugin makes a long random topic for you.
3. Open the plugin's **Status** page (its row on the Plugins page). It shows the
   topic and server. In the ntfy app, subscribe to that topic on that server.
4. Press **Send test** on the plugin's row. A test alert should arrive.

**Keep the topic private.** On a public server such as the default
`https://ntfy.sh`, anyone who knows a topic can read it, which is why the
generated one is long and random. For alerts that never touch someone else's
server, run your own ntfy server and set `server` (and `token` if it needs one).

| Option | Default | Meaning |
| --- | --- | --- |
| `server` | `https://ntfy.sh` | The ntfy server (must be `https://`). |
| `topic` | (generated) | Your topic; leave empty for a random one. Secret. |
| `token` | — | An access token for your ntfy server. Secret. |

## Setting up Telegram

1. In Telegram, talk to **@BotFather** to create a bot, and copy its token.
2. Send your new bot a message, then find your chat id (for example with a
   "user info" bot).
3. Enable the `telegram` plugin and set `bot_token` and `chat_id`. Both are kept
   secret: they're asked for when you enable the plugin and never shown
   afterwards.
4. Press **Send test** on the plugin's row.

| Option | Default | Meaning |
| --- | --- | --- |
| `bot_token` | — | Your bot's token. Secret. |
| `chat_id` | — | Your chat id with the bot. Secret. |

## When alerts arrive

- **Offline:** alerts raised while the device is offline wait in a small queue
  (up to 50) and are sent when a connection comes back. The queue survives a
  restart.
- **Rate limit:** at most `max_per_hour` alerts go out an hour (default 6); the
  rest wait their turn.
- **Quiet hours:** with `quiet_hours` set (for example `22-07`, local time),
  alerts wait until morning, except a low battery, which always gets through.
- **Failures:** a failed send is retried later with a growing delay, and the
  dashboard card shows the last error.

There are no commands back: the plugins only send.

## Options both plugins share

| Option | Default | Meaning |
| --- | --- | --- |
| `events` | `handshakes,battery,update,capture` | Which alerts to send. |
| `digest_minutes` | `30` | How often new handshakes are summed into one alert. |
| `include_names` | `false` | Name the networks in the handshake digest. |
| `battery_percent` | `20` | The low-battery threshold. |
| `max_per_hour` | `6` | The hourly limit. |
| `quiet_hours` | — | Local hours to hold alerts, e.g. `22-07`. |
| `timeout` | `15` | Seconds to wait for the server. |
