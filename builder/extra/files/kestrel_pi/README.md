# kestrel_pi

The on-device Python runtime for Kestrel, a Raspberry Pi Wi-Fi
capture-and-learning device. It bundles three cooperating pieces:

- **A plugin host daemon** (`kestrel-daemon`) that loads plugins and
  dispatches a pwnagotchi-compatible `on_*` hook API off its event loop,
  drawing on the device's connectivity and capture state.
- **A web dashboard** served on the device's management link — a
  dependency-free, server-rendered status and control surface for capture,
  plugins, and device actions.
- **A reinforcement-learning agent framework** (`kestrel-agent`,
  `kestrel-agent-train`): an optional on-device policy that tunes the capture
  scan posture, with offline training kept out of the always-on daemon.

It also carries the shared UI state model and a vendored e-ink panel driver
that plugins use without pulling hardware imports into the daemon.

The package is installed into the `pi` user's virtualenv (`/home/pi/.venv`) by
the Kestrel image build. The reference plugins ship separately under
`/usr/local/lib/kestrel/plugins/`, and operator plugins live in
`/etc/kestrel/plugins/`.

See `docs/developer/plugin-development.md` in the repository for the plugin API
and the `Plugin` base class in `kestrel_pi/plugins.py`.

Run the tests with `pytest` from this directory.
