# The `kestrel` command reference

`kestrel` is the single command-line tool for configuring and running the
device: network, capture, plugins, the web dashboard, backups, the learning
agent, remote training and updates. It is also available as `pwn-config`.

**Prefer the web dashboard for day-to-day use.** Almost everything below can be
done from the dashboard over the management link — enabling capture, enabling and
configuring plugins, running and steering the agent, backups, updates, reboot and
shutdown — and the guides lead with that. This reference is the full-power,
scriptable equivalent, and it also covers the handful of things only the terminal
can do: first boot and SSH, adding a Wi-Fi network, setting the dashboard login
mode, capture tuning and housekeeping, `backup setup` / `remote-train setup`, and
importing a model. Use it when you want to script the device or reach those.

**Running a command:**

- Run `kestrel` with **no arguments**, or any top-level command on its own
  (`kestrel capture`), to open a menu on the device terminal.
- Run `kestrel help` (or `-h`) for the built-in command list.
- **Reading** commands (`status`, `list`, `info`, `metrics`) need no `sudo`.
  Commands that **change the device** need `sudo`; they are shown with it below.
  A few that only touch the device account's own files (`backup`, `agent train`,
  `remote-train`, `update check`) run as the `pi` account or with `sudo` — either
  works.

## Setup and status

| Command | What it is for |
| --- | --- |
| `sudo kestrel setup` | Guided, menu-driven first-time setup: network, then capture. |
| `kestrel status` | Show network **and** capture state together (a quick overview). |
| `kestrel help` | Show the built-in command list. |

## Network

| Command | What it is for |
| --- | --- |
| `kestrel network` | Open the network menu (Wi-Fi, Bluetooth status, re-pair). |
| `kestrel network status` | Show Bluetooth PAN and Wi-Fi state and IP addresses. |
| `sudo kestrel network wifi --ssid NAME [--psk PASS] [--hidden] [--country GB] [--band 2.4\|5\|any]` | Save the Wi-Fi station network. `--psk` omitted = open network; `--hidden` for a non-broadcast SSID; `--country` is the two-letter regulatory code; `--band` picks the built-in chip's band. |
| `sudo kestrel network wifi --band 2.4\|5\|any` | Change only the saved network's band (no other change). |
| `sudo kestrel network bt-pair` | Forget the paired phone and reopen the Bluetooth onboarding window to pair a new one. |

See [Connecting over Wi-Fi](wifi-setup.md) and [over Bluetooth](bluetooth-setup.md).

## Capture

| Command | What it is for |
| --- | --- |
| `kestrel capture` | Open the capture menu. |
| `kestrel capture status` | Show capture state, the capture adapter, and store size/counts. |
| `sudo kestrel capture enable [--interface auto\|wlanX] [--yes]` | Enable capture. Asks the authorized-use acknowledgment (once per device); `--interface` pins the capture dongle (default auto-detect); `--yes` skips the prompt (for scripts). |
| `sudo kestrel capture disable` | Disable capture. |
| `sudo kestrel capture reset-ack` | Forget the authorized-use acknowledgment, so the next enable asks again. |
| `kestrel capture whitelist list` | List the protected networks (never actively attacked). |
| `sudo kestrel capture whitelist add <AP>` | Protect a network by ESSID or BSSID. |
| `sudo kestrel capture whitelist remove <AP>` | Stop protecting a network. |
| `sudo kestrel capture archive` | Archive the current store into a timestamped set and start a fresh one. |
| `sudo kestrel capture rotate` | Apply the housekeeping policy now (the same thing the daily timer runs). |
| `sudo kestrel capture clear --yes [--purge]` | Delete the live store (and archived sets too with `--purge`). Irreversible. |

See [Setting up capture](capture-setup.md) and [the capture whitelist](capture-whitelist.md).

## Plugins

| Command | What it is for |
| --- | --- |
| `kestrel plugins` | Open the plugin menu. |
| `kestrel plugins list` | List all plugins and whether each is enabled. |
| `kestrel plugins info NAME` | Show a plugin's capabilities, options, dependencies and health. |
| `sudo kestrel plugins enable NAME [--yes] [--set k=v ...]` | Enable a plugin. Asks the authorized-use acknowledgment for ones that send data off the device; in a terminal it then walks the options (secrets masked). `--set k=v` sets an option non-interactively (repeatable); `--yes` skips the prompts. |
| `sudo kestrel plugins disable NAME` | Disable a plugin. |

See [Managing plugins](managing-plugins.md) and the per-plugin guides in the [index](user-guides.md).

## Web dashboard

| Command | What it is for |
| --- | --- |
| `kestrel web` | Open the web-page menu (address, login, controls). |
| `kestrel web status` | Show the dashboard address and whether the write controls are on. |
| `sudo kestrel web auth system [--user NAME]` | Log in with the device account (same as SSH) — the default. `--user` overrides which account is checked. |
| `sudo kestrel web auth basic [--user NAME] [--password-stdin]` | Require a web-only username/password instead. `--password-stdin` reads the password from stdin (for scripts). |
| `sudo kestrel web auth token` | Require a generated access token (printed once). |
| `sudo kestrel web auth none` | No login: a read-only page with the write controls off. |

See [Using the device web dashboard](web-dashboard.md).

## Backup

The `backup` plugin mirrors the capture store to a remote host over SSH. Enable it
first with `sudo kestrel plugins enable backup --set host=... --set user=... --set path=...`.

| Command | What it is for |
| --- | --- |
| `kestrel backup` | Open the backup menu (target, key setup, run now). |
| `kestrel backup status` | Show the remote target and whether passwordless SSH works. |
| `sudo kestrel backup setup` | Install the device's SSH key on the remote (asks the remote password once, stores nothing) so future backups need no password. |
| `kestrel backup now` | Run a one-off backup immediately. |
| `kestrel backup restore [--yes]` | Restore the capture data and agent model from the remote backup onto a fresh image (data only, not settings). `--yes` skips the confirmation. |

See [Remote backup](backup-setup.md).

## Learning agent

| Command | What it is for |
| --- | --- |
| `kestrel agent status` | Show the agent state, policy, algorithm and current posture. |
| `kestrel agent metrics` | Show the agent-vs-sweep comparison table. |
| `sudo kestrel agent enable [--algorithm A] [--policy P]` | Enable the agent (needs capture on; takes the authorized-use ack). |
| `sudo kestrel agent disable` | Disable the agent; capture returns to the scripted sweep. |
| `sudo kestrel agent set [--algorithm A] [--policy P]` | Change the algorithm and/or policy in one call. |
| `sudo kestrel agent algorithm A` | Set the RL algorithm (`RecurrentPPO`, `PPO`, `DQN` or `QR-DQN`). Switching to one with no model yet starts a background training run to build it. |
| `sudo kestrel agent policy P` | Set what drives capture: `baseline` (the scripted sweep) or `learned` (the trained model). |
| `sudo kestrel agent import FILE --algorithm A` | Install a trained model (kept per-algorithm) and switch to it. |
| `kestrel agent train [--algorithm A] [--timesteps N]` | Train a model from the logged episodes now. `--timesteps` sets the run length. |
| `kestrel agent train-now` | Ask the running agent to train now, in the background (non-blocking). |

See [The learning capture agent](agent-setup.md).

## Remote training

The `remote-train` plugin offloads agent training to a desktop host over SSH.
Enable it first with `sudo kestrel plugins enable remote-train --set train_host=... --set train_user=... --set train_path=...`.

| Command | What it is for |
| --- | --- |
| `kestrel remote-train` | Open the remote-training menu. |
| `kestrel remote-train status` | Show the training host, SSH state and host job state. |
| `sudo kestrel remote-train setup` | Install the device's SSH key on the training host (asks its password once, stores nothing). |
| `kestrel remote-train now` | Reconcile now: push new data, train the models that are due, pull back finished models. |

See [The learning capture agent](agent-setup.md) for turning training on.

## Updates

| Command | What it is for |
| --- | --- |
| `kestrel update` | Open the update menu (check for and apply a new release). |
| `kestrel update status` | Show the installed version and what the last check found. |
| `kestrel update check` | Check GitHub for a new release now, without waiting out the interval. |
| `sudo kestrel update apply [--ref TAG] [--yes]` | Download and apply the latest in-place release (needs the `updates` plugin's check first). `--ref` applies a specific tag; `--yes` skips the confirmation. A release that changed the base OS is flagged reflash-only and is refused. |

See [Keeping the device updated](updates.md).
