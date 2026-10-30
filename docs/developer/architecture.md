# Architecture

How kestrel is put together on a running device, and why it is built that
way. This describes the current design; for the concrete tools and versions see
[Tech stack](tech-stack.md).

Deeper rationale for individual subsystems lives in the other developer docs
([Capture interface](capture-interface.md), [Web UI
conventions](web-ui-conventions.md), [In-field updates](updates-setup.md),
[Remote training](remote-training.md)).

---

## Two halves

There is a hard line between **the build** and **the device**:

- The build (Packer + Ansible, on a Linux host) bakes a stock Raspberry Pi OS
  image into a finished `.img`. It installs packages, writes config, drops in the
  first-party code, and enables services. It does not run on the device.
- The device runs that image: a set of systemd services written in Python and
  shell, on top of Raspberry Pi OS.

Everything below is the device side. The build exists to assemble exactly this
layout; see [Tech stack](tech-stack.md) for the build toolchain.

---

## Process model

The device runs a handful of independent systemd services, each with one job.
Separate processes (rather than one monolith) mean a crash or a slow operation
in one is contained, and each can have exactly the privileges it needs.

| Service | Runs as | Ships | Role |
| --- | --- | --- | --- |
| `kestrel-daemon.service` | `pi` | **enabled** (inert with no plugins) | Plugin host + web dashboard. The always-on brain. |
| `kestrel-capture.service` | root | **off** | Runs the AngryOxide capture engine. Turned on only after the authorized-use acknowledgment. |
| `kestrel-capture-housekeeping.{service,timer}` | root | timer enabled | Daily archive/rotate of the capture store. Never silently deletes. |
| `kestrel-agent.service` | `pi` | off | The learning agent process (torch/SB3), kept separate from the daemon. |
| `kestrel-bt-onboard.service`, `kestrel-bt-pan.service` | root | enabled | Bluetooth pairing window and phone-tether (PAN) link. |
| `kestrel-wifi-provision.service`, `kestrel-capture-whitelist-provision.service`, `kestrel-agent-provision.service` | root | one-shot | First-boot provisioning (boot-folder seeds, agent venv/model). |
| `kestrel-plugins-apply@.service` (+ `.socket`) | root | socket-activated | The privileged "apply" helper (see below). |
| `kestrel-web-auth@.service` (+ `.socket`) | root | socket-activated | The privileged login check (see below). |

Installed layout: first-party scripts go to `/usr/local/bin/kestrel` and
`/usr/local/lib/kestrel/`, the `kestrel_pi` package into the pi venv,
units into `/etc/systemd/system/`, runtime config into `/etc/kestrel/`.

---

## Privilege separation (the central security design)

The daemon and web server run as the unprivileged **`pi`** user. They can read
state freely but cannot change the system directly. Every privileged action goes
through a **root helper reached over a systemd-activated Unix socket**, never by
giving the daemon root:

- **`kestrel-plugins-apply.socket` → `plugins-ctl apply`** performs config
  writes, plugin enable/disable, the capture on/off toggle, and reboot/shutdown.
  The web page and the CLI both drive this same helper, so a change made in the
  browser is identical to the terminal path (same adapter detection, same config
  write, same service action).
- **`kestrel-web-auth.socket` → `plugins-ctl checkauth`** verifies the web
  login against `/etc/shadow` live. The daemon is `pi` and cannot read shadow;
  this is why the web login *is* the device account and no web password is ever
  stored or able to drift from the SSH password.

Two rules make the root helper safe to expose this way, and both are load-bearing:

1. **`plugins-ctl` never imports plugin code — nor the venv package — in any
   subcommand.** It reads plugin declarations with `ast` (static parse) instead
   of importing them, because it runs as root (the socket helpers, and the
   `kestrel plugins` CLI under sudo) and a plugin is third-party code. For the
   same reason it never imports the `kestrel_pi` package: the venv is
   `pi`-writable, so a root import would be an escalation path. It always runs
   as `/usr/bin/python3 -I`, never the venv's Python. Anything the helper needs
   from the package (e.g. the `config-set` allow-list, the static declaration
   reader in `kestrel_pi/declarations.py`) is mirrored into `plugins-ctl` and
   held in step by a drift test — see [Config writes from the page](#config-writes-from-the-page).
   The loader refuses any plugin that reader can't read, so root and the daemon
   always see the same plugins. Keep it import-free. More generally, the CLI and
   lib scripts run Python as root only as `python3 -I` with a reset `PATH`, and
   the venv's Python only as `pi` (`run_as_pi`); `tests/test_root_python.py`
   enforces both.
2. **`spwd`/`crypt` are imported lazily** inside `plugins-ctl` (they were removed
   in Python 3.13; present on the 3.11 image) so the module still loads under a
   newer host Python. Keep them lazy.

The web server also binds to the **management link only** (the tether/Wi-Fi,
never the capture dongle), and writes are CSRF-protected — see [Web UI
conventions](web-ui-conventions.md).

---

## The daemon (plugin host)

`kestrel-daemon` is the core. Its modules (in `kestrel_pi/`) divide by
responsibility:

- **`daemon.py`** — the service entry point: builds the context, loads plugins,
  starts the web server, and runs the epoch loop.
- **`loader.py`** — discovers plugins from the shipped directory
  (`/usr/local/lib/kestrel/plugins/`), with operator overrides from
  `/etc/kestrel/plugins/` taking precedence.
- **`plugins.py`** + **`__init__.py`** — the plugin registry, the
  legacy-compatible `Plugin` base class / `on_*` hook surface,
  `load_helper`, which loads a plugin's own helper files, and `load_library`,
  which loads a shared plugin library for a plugin that asks for one.
- **`dispatch.py`** — runs hooks **off the main loop** on a small worker pool
  (`hook_workers`), with a per-hook time budget (`max_hook_seconds`) after which a
  slow hook is logged as an overrun. A hook that overruns but then finishes is
  only slow, not stuck, and is left alone; only a hook still running past the
  longer `hook_stuck_seconds` ceiling is quarantined, so a genuinely stuck hook
  can't keep draining the pool. This is why one misbehaving plugin cannot stall
  the daemon or the others.
- **`context.py`** — the typed context object handed to plugins (device state,
  capture info, shared providers like GPS position).
- **`config.py`** / **`options.py`** — config loading, per-plugin option
  resolution, and validation + health reporting (a misconfigured plugin surfaces
  an error state rather than crashing the host).
- **`connectivity.py`** — real-reachability internet detection. It treats the
  device as online if *either* the Bluetooth PAN link (`bnep0`) or `wlan0`
  reaches the internet, using an actual probe rather than NetworkManager's
  connectivity flag (off on the stock image), and debounces so it does not flap.
  `on_internet_available` fires off this.
- **`capture.py`** — reads live capture state (via `systemctl is-active`) and
  polls the store for new handshakes (`capture_poll_seconds`) to feed events.
- **`ui.py`** — the shared per-plugin UI element model (see the plugin UI model
  below).
- **`web.py`** / **`userguide.py`** — the web subsystem and the on-device guide
  content.

Besides these, the package holds the learning agent (`agent/`, its own process),
the services the daemon offers (`geo.py`, `recovered.py`, `webtls.py`) and
`httpsafe.py`, the request policy every credential-sending plugin follows.

**Core stays lean.** `kestrel_pi` holds only what the daemon, the agent and the
shipped scripts run, plus those general services; it never holds code that
exists for particular plugins, and it never imports a plugin. A feature lives in
its plugin. Code several plugins share lives with the plugins as a **shared
plugin library** (`plugins/_lib_<name>.py`), which a plugin loads with
`load_library` and core never loads; until a library exists, each plugin
carries a copy (`ntfy` and `telegram` share the `alerts` library, `eink` and
`eink-ws213` the `eink_face` library, and `eink` the `hat_keys` library for its
HAT's keys; each keeps Waveshare's driver as its own helper files).
`tests/test_core_lean.py` fails on a module only plugins use and on core loading
a library. See [Keep plugins standalone](plugin-development.md#keep-plugins-standalone)
and the [plugin library reference](plugin-libraries.md).

### Epoch loop

The daemon ticks every `epoch_seconds` and dispatches the periodic hook to
plugins. It also pushes one UI update immediately after start, so a freshly
enabled plugin shows real values at once instead of blank until the next tick.

---

## Plugin framework

Plugins are the primary extension point and the first real application code. To
write one, see [Writing a plugin](plugin-development.md); this section is the
design overview.

- **Modelled on legacy pwnagotchi**: a `Plugin` base class and `on_*` hooks with
  the familiar names, but not drop-in compatible: several hooks take a typed
  event and `self.context` instead of pwnagotchi's `agent` object, so a ported
  plugin needs its hook signatures updated. Declarations must be plain literals
  on a direct `Plugin` subclass (root reads them statically), and the legacy
  `@plugins.on` decorator style is not supported.
- **Capabilities and permissions are declared, not implicit.** A plugin declares
  what it needs (e.g. the `network` capability). Enabling a plugin with the
  `network` capability requires the same authorized-use acknowledgment as
  capture.
- **Typed dependencies**: plugins can `provide`/`require` named services (e.g. the
  GPS plugin provides a shared position used by capture sidecars and WiGLE).
- **API versioning**: `HOOK_API_VERSION` in `kestrel_pi/__init__.py` gates the
  hook contract. Bump it only on a breaking hook-signature change.
- **Dispatch is non-blocking** (see `dispatch.py` above), so a plugin doing I/O
  does not block the loop.
- **Standalone**: a plugin is one file plus, if it needs them, its own
  `_<plugin>_*.py` helper files (loaded with `load_helper`) and the shared
  `_lib_<name>.py` plugin libraries it names (loaded with `load_library`:
  beside the plugin first, then the shipped plugins directory; versioned by
  `LIB_API`). The loader skips both kinds of file, and `lib` is reserved as a
  plugin name. Plugins never share code through core, and never through each
  other's helpers.

### How plugins reach the UI

A plugin contributes to the dashboard **as data or by owning a page**, never by
injecting markup (core escapes all widget values):

- **Readout cards** — the plugin emits typed UI elements through the shared model
  in `ui.py` (`StubUI.for_plugin()` namespaces a plugin's elements). Each element
  is `{label, value, kind}` with `kind` in text/number/percent/gauge. `status()`
  exposes a grouped `widgets` map the dashboard renders. This widened shape stayed
  source-compatible, so `HOOK_API_VERSION` did not change for it.
- **Owning a page** — a plugin with an `on_webhook` hook serves its own page.

The web and e-ink views share this state model but not their layout.

### Config writes from the page

The dashboard's controls never let the daemon write `config.toml` directly. They
go through the root `plugins-ctl apply` socket, which performs the
single-key-safe write. Enabling/disabling a plugin does not require a daemon
restart; the capture toggle does not restart the daemon either (capture state is
read live).

The **Advanced settings** page (`/settings`) edits the core `config.toml` tables
(`[capture]`, `[agent]`, `[daemon]`, `[web]`) the same way, through a
`{"target": "config-set", ...}` change on the same socket. Two pieces keep this
safe across the privilege boundary:

- **An allow-list, mirrored.** The settable `(table, key)` pairs and their
  type/range constraints live in `kestrel_pi/config_catalog.py` (read by the
  daemon to render the form and its inline help) and are mirrored as
  `CONFIG_SETTABLE` in `plugins-ctl.py` (the root validator, which must not
  import the package — see above). `tests/test_config_catalog.py` asserts the two
  are equal, so they can't drift. A key not on the list, or a value out of range,
  is refused and nothing is written.
- **Single-key writes and no secrets.** Each save goes through `config-set`,
  which re-parses and refuses any edit that changes more than the one key.
  Secret-bearing keys and list-valued keys are not settable through the page
  (shown read-only with a CLI pointer); per-plugin options stay on the existing
  `/manage/<name>` pages. A `[daemon]`/`[web]` change restarts the daemon; a
  `[capture]`/`[agent]` change is applied on that service's next (re)start.

---

## Capture pipeline

Capture is a separate concern from the plugin host, with its own service, config
file, and versioned interface.

- **Engine**: AngryOxide runs as an external process under
  `kestrel-capture.service`. kestrel launches and steers it; it owns
  the radio.
- **Launcher** (`capture-launch.sh`) forces `--headless` when there is no TTY
  (i.e. under systemd) even if `headless=false` is configured, logging why — the
  text UI with no terminal crashes AngryOxide. The TUI is only for launching by
  hand in a terminal.
- **Collector** (`capture-collect.py`) moves finished handshakes into the store.
- **Output**: `per-ssid` (one `hc22000` per network) or `single` (one combined
  file); both are append-only. The store is under
  `/var/lib/kestrel/capture/`.
- **Whitelist**: networks never actively attacked (still observed). Pre-loadable
  from the boot folder.
- **Housekeeping** (`capture-housekeeping` timer): daily archive/rotate with a
  store size/age policy, plus a live cap on the raw working directory. It never
  silently deletes handshakes.
- **Runtime config** is `/etc/kestrel/config.toml` (parsed with stdlib
  `tomllib` by `config-get`), separate from the Phase-1 network plumbing in
  `/etc/default/kestrel`.
- **Ships off.** Only `capture enable` (CLI) or the dashboard capture control
  turns it on, and both record the authorized-use acknowledgment.

The surface the agent and plugins consume is versioned in [Capture
interface](capture-interface.md) (`contract_version`), so a consumer can detect a
mismatch.

---

## Learning agent

The agent chooses *scan postures* instead of the fixed sweep, and runs in **its
own process** (`kestrel-agent`) to keep torch/SB3 out of the daemon. Its
observation/action/reward contract, the `scan-override.json` control seam, and
the model, episode-log and `status.json` schemas are the normative
[Agent interface](agent-interface.md).

- **Gymnasium environment** (`agent/env.py`) wraps the capture interface as an RL
  environment.
- **Discrete action space** (`agent/spec.py`): an action is a scan *posture*
  (sweep-band, lock-subset, pin-channel, autohunt) crossed with a dwell bucket and
  transmit on/off — not a single channel. Discrete keeps DQN/QR-DQN and the
  policy-gradient algorithms swappable.
- **Rich observations** (`agent/features.py`): BSSID churn, signal drift,
  per-channel revisit intervals, so a recurrent policy can tell moving from
  parked.
- **Algorithms** (`agent/policies.py`): stable-baselines3 / sb3-contrib —
  RecurrentPPO (default), PPO, DQN, QR-DQN.
- **Policy selection**: `baseline` (the scripted sweep) or `learned` (a trained
  model). `learned` needs a trained or imported model; a newly trained/pulled
  model is adopted live only if it beats the current one.
- **Metrics** (`agent/metrics.py`): a live agent-vs-sweep comparison, optionally
  interleaved into fair benchmark blocks.
- **Training** (`agent/train.py`): on-device fine-tuning gated by RAM and CPU
  temperature, or offloaded to a desktop via [Remote training](remote-training.md).

**Design ceiling**: AngryOxide v0.9.3 is controllable only at launch time. Each
posture change restarts the launcher for the next window (`decision_seconds`);
there is no live mid-run steering and no per-target PMKID-vs-handshake choice
(AngryOxide owns that internally). The escape hatch is a `contract_version` bump
if the engine gains live control.

---

## Connectivity and onboarding

- **Wi-Fi** uses NetworkManager profiles (not `wpa_supplicant.conf`). The image
  ships with no networks.
- **Bluetooth PAN** is the headless path: a time-limited pairing window each boot
  (`bt-onboard`), Just-Works pairing (no PIN), then a PAN link (`bt-pan`) with a
  DNS fallback so name resolution works over a phone that only offers an unusable
  resolver.
- **Boot-folder provisioning**: first-boot one-shots can seed Wi-Fi and the
  capture whitelist from files dropped on the FAT partition, so a card can be
  prepared with no SSH. The boot partition is at `/boot/firmware`; reference it
  only through `system.boot_dir`.

---

## In-place updates

The `updates` plugin (opt-in) notices a newer GitHub release; the dashboard or
CLI then applies an **app-layer** update in place, no reflash, via the root
`plugins-ctl apply` `{"target":"update"}` path → the CLI's `update apply`.

The key design point: `update apply` clones the release and `exec`s the clone's
`update-apply.py` engine, so the engine runs from the clone — which is what makes
overwriting the installed CLI, `plugins-ctl` and package safe. The engine
reinstalls everything the playbook installs from under `builder/extra/files` (the
venv package, plugins, systemd units, the journald drop-in, the fixed
`LIB_SCRIPTS` list, the CLI) and deliberately **skips operator config**
(`config.toml`, `/etc/default/kestrel`); new config keys fall back to code
defaults.

A release carries an `update-manifest.json` with `kind: app-layer | reflash`.
Changes the in-place engine cannot apply (apt/pip packages, boot options, kernel
modules, the base image, a brand-new lib script not in `LIB_SCRIPTS`, shipped
config defaults) must be a reflash. Full rules in [In-field
updates](updates-setup.md).

The build stamps `/etc/kestrel/version`; a dev build is `master` and is
never offered updates.

---

## Configuration model

- **`/etc/kestrel/config.toml`** — the runtime config, in tables
  (`[capture]`, `[capture.scan]`, `[agent]`, `[daemon]`, `[web]`, `[plugins.*]`,
  …). Several tables reuse the key name `enabled`, so edits are made **one key at
  a time** through `config-set` (`config_set`/`capture_config_set`), which
  re-parses the result and refuses any edit that changes more than the one key.
  Never `sed` a key across the file.
- **`/etc/default/kestrel`** — low-level network timings, `KEY=value`.
- **Operator plugin overrides** — a plugin dropped in
  `/etc/kestrel/plugins/` overrides the shipped one of the same name.
- Secrets (API keys, tokens, the web password) are collected at enable time and
  never written into or shown from config.

See [the config reference](../user-guide/config-reference.md) for the full key
catalog.

---

## On-disk data layout

| Path | Holds |
| --- | --- |
| `/etc/kestrel/config.toml` | Runtime config. |
| `/etc/kestrel/version` | The installed version stamp. |
| `/etc/kestrel/plugins/` | Operator plugin overrides. |
| `/etc/default/kestrel` | Network timings. |
| `/var/lib/kestrel/capture/` | Handshakes (`hc22000`), GPS sidecars, `cracked/` potfile, `archive/`, `geo/`. |
| `/var/lib/kestrel/agent/` | The learned model and episode logs. |
| `/usr/local/lib/kestrel/` | Installed lib scripts and shipped plugins. |
| `/usr/local/bin/kestrel` | The CLI. |

---

## Versioned interfaces

Three contracts let parts evolve independently:

- **`HOOK_API_VERSION`** — the plugin hook contract, in
  `kestrel_pi/__init__.py`.
- **`contract_version`** in [Capture interface](capture-interface.md) — the
  surface the agent and plugins read capture state through.
- **`SPEC_VERSION`** in [Agent interface](agent-interface.md) — the learning
  agent's observation/action shapes and model format, in `agent/spec.py`.

Bump the relevant one on a breaking change so a consumer can detect the mismatch
instead of failing obscurely.
