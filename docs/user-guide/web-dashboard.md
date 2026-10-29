# Using the device web dashboard

The daemon serves a web dashboard over the **management link** (your Bluetooth
tether or Wi-Fi) — never over the capture dongle. It shows device and capture
status and the cards contributed by your enabled plugins, and (when login is on)
lets you control capture, plugins, and power from a browser.

## Opening it

From a device on the same management link (your tethering phone, or a machine on
the same Wi-Fi):

```
https://kestrel.local:8443/
```

or use the device's IP on the link, e.g. `https://172.20.10.14:8443/` (the e-ink
panel's address row shows it). The HTTPS port is `8443` by default
(`[web] tls_port`); an old `http://…:8080` link forwards there by itself.

## Logging in

By default (`[web] auth = system`) the dashboard logs in with the **device
account** — the same username and password you SSH in with. There is no separate
web password stored anywhere, so the web login can never drift from the SSH
login: change the Pi password with `passwd` and the dashboard login changes with
it. (See [First boot](first-boot.md).)

Other modes, set with `sudo kestrel web auth <mode>`:

- `system` *(default)* — device account login; required to view and to make changes.
- `basic` — a web-only username/password.
- `token` — a generated token (printed once).
- `none` — open, **read-only** page with no controls.

## Signing in and HTTPS

Off the device the dashboard is served over **HTTPS** with a certificate the
device makes for itself, so the first visit shows a "connection isn't private"
warning: no public authority vouches for a device on your own network. Before
going past it, compare the certificate's SHA-256 fingerprint (in the browser's
certificate details) with the device's:

- the e-ink panel shows its start for the first ten minutes after start-up;
- `sudo kestrel web fingerprint` prints it in full.

If they don't match, don't sign in. Otherwise proceed (iPhone: **Show Details →
visit this website**; Android: **Advanced → Proceed**).

You sign in once on the sign-in page; the browser stays signed in for up to a
week, or until it's been idle for 12 hours. **Sign out** is at the bottom of every
page. After changing the device password, `sudo kestrel web logout-all` signs
every browser out. Several wrong passwords in a row make the page ask you to wait
before the next try.

Scripts can still send HTTP Basic credentials over HTTPS, and tools on the device
use `http://127.0.0.1:8080/`. `sudo kestrel web tls off` serves plain HTTP instead
if a browser can't cope (the sign-in is then not encrypted on the network),
`sudo kestrel web tls on` turns HTTPS back on, and `sudo kestrel web tls
regenerate` makes a new certificate.

## What you see

- **Status** — whether the device is online, capture state, and key counts.
- **Environment** — a card showing **what the radio can see around you
  recently**: a density word and count (*"Moderate · 23 radios nearby"*, counting
  access points and clients together) with a bar, and a "your yield this session"
  line showing what you've captured — so a quiet spot and a target-rich one no
  longer look the same. It's a sliding-window reading (the radio sweeps channels
  and flushes its data in bursts), not an instant count, so the card says *"as of
  N ago"* when the reading isn't fresh and falls back to *"Unknown"* when it goes
  stale. It only reads the air **while capture is running**; with capture off it
  says *"Unknown — enable capture to scan the area"* rather than a misleading
  zero. Tune or turn it off with `[capture.environment]` (see
  [Capture setup](capture-setup.md)).
- **Plugin cards** — each enabled readout plugin contributes a card (battery from
  `ups-hat`, memory/temp from `memtemp`, upload status from `wpa-sec`/`wigle`,
  and so on). You can **drag the cards to reorder** them; the order is remembered
  in your browser.
- **AI Agent** — a compact card (like the Plugins card) showing whether the
  learning agent is on and which policy it's using. Tap it to open the agent
  page, where you pick the policy (Baseline, the scripted sweep, or Learned, a
  trained model) and, for Learned, the algorithm, and enable or disable it.
  While the agent is on, a **Train now** button there starts an on-device
  training run on demand (it's greyed out with the reason when the agent can't
  train yet — no data, a run already going, or low memory / too hot). It's kept
  off the main view so it doesn't crowd the dashboard.
- **Captured networks** — a page listing the unique APs you've captured, with
  BSSID and handshake counts, searchable by name and paginated, so it scales to
  thousands of networks. A network whose passphrase is known shows a **recovered**
  badge, and the dashboard card shows a **recovered N of M access points** count;
  with a login on, expand a recovered row to see the passphrase (unless
  `show_passphrases` is off). A network whose location is known also shows a
  small coordinate linking to OpenStreetMap on its row — a solid pin for a
  location recorded by the onboard GPS, an *approximately* mark for one estimated
  from a source like WiGLE — shown only with a login on, and hidden (leaving a
  muted "location hidden" marker) when `show_locations` is off. See [Working with
  your captures](working-with-captures.md#seeing-which-networks-are-recovered).
- **Whitelist** — directly below Captured networks: a page of the APs that are
  never actively attacked (deauth) — they're still observed passively. It's
  searchable, and with a login you can **add a new AP** (an ESSID or a BSSID
  like `AA:BB:CC:DD:EE:FF`) or remove one, straight from the page. A change
  takes effect the next time capture starts. You can still manage it from the
  terminal with `sudo kestrel capture whitelist add <AP>` /
  `... remove <AP>` / `... list`.
- The dashboard **auto-refreshes** every `refresh_seconds` (default 30; set 0 to
  disable). Only the read-only view refreshes — it won't reload a form under you.

## Controls (login required)

With a login on, the dashboard can drive the same actions as the terminal,
through a small privileged helper (the daemon itself runs unprivileged):

- **Capture on/off** — toggles capture, including the authorized-use
  acknowledgment (never pre-ticked). It does not restart the daemon.
- **Plugins** — a page to enable/disable plugins and edit their options, with the
  authorized-use acknowledgment for network plugins. See
  [Managing plugins](managing-plugins.md).
- **Import recovered results** — the **Import results** button at the top of the
  Captured networks page, for uploading a potfile of known passphrases (wpa-sec
  or hashcat separated format).
  See [Working with your
  captures](working-with-captures.md#importing-a-potfile).
- **Reboot / Shut down** — each opens a confirmation page first. (Shutting down
  won't come back on its own; reboot will.)
- **Pair over Bluetooth** — a small button that appears **only when no phone is
  paired**. It reopens the Bluetooth pairing window (after a confirmation page)
  so you can pair a phone for the tether link without the terminal; once a phone
  is paired it disappears. You can still do this from the terminal with
  `sudo kestrel network bt-pair`.
- **Advanced settings** — a per-key editor for the core `config.toml` settings,
  reached by a discreet **Advanced settings** link at the very bottom of the
  dashboard. See below.

## Advanced settings

Most settings have a purpose-built control — the capture toggle, the plugin
pages, the AI Agent page. The **Advanced settings** page (the discreet link at
the bottom of the dashboard, or `/settings` directly) is for the rest: the core
`config.toml` keys that otherwise only the CLI reaches, in one page with sections
for **Capture**, the **learning agent**, and the **daemon and web** server. Like
the other controls it needs the login on; with the login off it is read-only.

- Each key is edited **on its own**, showing its current value, a short note of
  what it does, and the limits the device enforces. This is the same
  one-key-at-a-time write the CLI uses, so a save can never change more than the
  setting you touched.
- **Reset** on any key puts it back to the value a fresh image ships with.
- Some things are shown but **not editable here**, each saying why: a setting a
  dedicated control owns (with a link to it, e.g. capture on/off or the agent
  page); a **secret** (shown only as *configured* or *not set* — its value is
  never displayed or entered on this page); and the low-level network plumbing in
  `/etc/default/kestrel`, which stays terminal-only because a wrong value
  there can drop your management link.
- **Per-plugin options are not here** — each plugin keeps its own config page
  (see [Managing plugins](managing-plugins.md)); the Plugins area links to them.
- Saving a **daemon** or **web** key restarts the dashboard to apply it (the page
  waits and reloads). A **capture** or **agent** key is saved right away but takes
  effect the next time that service (re)starts, which the page tells you.

The full list of keys, with types, defaults and recommendations, is the
[configuration reference](config-reference.md).

## Security notes

The page binds to the management interfaces and loopback only (never the capture
dongle), enforces its login, protects write actions with CSRF tokens, sets
frame/cache/referrer headers, and never echoes a stored secret back. Status is
never shown by colour alone, and the page is mobile-first and dark-mode aware.
