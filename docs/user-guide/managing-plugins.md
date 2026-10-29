# Managing plugins

kestrel ships a small daemon (`kestrel-daemon`) that hosts plugins
and serves the device web dashboard. The daemon ships **enabled** but does
nothing on its own — it just runs its event loop and the status page until you
enable a plugin. Every bundled plugin ships **disabled**.

The fourteen reference plugins are: `memtemp`, `session-stats`, `gps`, `wpa-sec`,
`wigle`, `backup`, `ups-hat`, `eink`, `updates`, `remote-train`, `downloads`,
`map`, `notify`, and `hat-buttons`.
Each has its
own setup guide where it needs one (see the [index](user-guides.md)).

## From the web dashboard (easiest)

With the dashboard login on (`[web] auth` is not `none`), the **Plugins** page is
the simplest way to manage plugins. It lists every plugin with an on/off control
and its options, and from there you can enable or disable a plugin, edit its
options, and give the authorized-use acknowledgment for a network plugin — all
without a reboot or even a daemon restart. Secret options are masked and never
echoed back. See [Using the web dashboard](web-dashboard.md).

Everything below is the same from the terminal, and is also what you use before
the web login is set up.

## Listing and inspecting

```bash
sudo kestrel plugins list          # every plugin and whether it's enabled
sudo kestrel plugins info gps      # one plugin's capabilities, options, deps, health
```

`plugins info` is the quickest way to see a plugin's available options and its
current state (loaded / disabled / failed).

## Enabling a plugin

```bash
sudo kestrel plugins enable session-stats
```

For plugins that take settings, pass them with `--set key=value` (repeatable):

```bash
sudo kestrel plugins enable gps --set source=serial --set device=/dev/ttyACM0
```

Run in a terminal, `plugins enable` also walks you through the plugin's options
(secrets are masked). Enabling does **not** require a reboot or even a daemon
restart for the common cases.

### The authorized-use acknowledgment

A plugin that sends data off the device — anything with the `network` capability
(`wpa-sec`, `wigle`, `backup`, `updates`, `remote-train`, and `notify`) — requires the same
explicit authorized-use acknowledgment as capture before it turns on. In a
terminal you'll be prompted; for scripts, add `--yes` to acknowledge
non-interactively. Local-only plugins (`memtemp`, `session-stats`, `ups-hat`,
`eink`, and `gps` reading a local receiver) don't need it.

## Disabling a plugin

```bash
sudo kestrel plugins disable wigle
```

## Where plugins and their config live

- **Bundled plugins:** `/usr/local/lib/kestrel/plugins/`
- **Your own / overrides:** `/etc/kestrel/plugins/` — a plugin here with
  the same name as a bundled one takes precedence, so you can override a
  reference plugin without touching the image.
- **Config:** each plugin has a `[plugins.<name>]` table in
  `/etc/kestrel/config.toml`. Prefer the `plugins` subcommand (or the web
  page) over hand-editing — they validate the change and edit just the one key.

## If a plugin shows an error

`sudo kestrel plugins info <name>` reports a failed plugin's health, and
`sudo journalctl -u kestrel-daemon` shows why it failed to load (bad
option, missing dependency, etc.). A plugin that fails to load doesn't stop the
daemon or the other plugins.
