# Remote training (train the agent on a desktop)

The `remote-train` plugin offloads the [learning agent](agent-setup.md)'s
*training* to a more powerful Linux machine, so a small board doesn't have to do
the heavy compute itself. The device keeps capturing and making decisions as
usual; only the slow part — building a better model from the episodes it has
logged — happens on the desktop, and the finished model is pulled back
automatically. It ships **off** and is opt-in.

You want this if on-device training is too slow or your board is too small to
train well (training is capped by RAM and heat), but you have a desktop or server
the device can reach.

## Before you start

You need an always-on Linux machine the device can reach over SSH (your training
*host*). The plugin builds its own isolated environment there, so the host only
needs a few standard packages and a few GB of free disk for the machine-learning
download. Install, on the host:

- **Debian / Ubuntu / Raspberry Pi OS:** `sudo apt install -y python3-venv python3-pip rsync openssh-server`
- **Fedora / RHEL:** `sudo dnf install -y python3 python3-pip rsync openssh-server`
- **Arch:** `sudo pacman -S --needed python python-pip rsync openssh` then `sudo systemctl enable --now sshd`

On Debian/Ubuntu `python3-venv` is the one most often missing — without it the
host can't build its environment and training never starts, so don't skip it.

You also need the agent itself turned on — see [The learning capture
agent](agent-setup.md).

## Turn it on

From the dashboard **Plugins** page (with login on), enable **remote-train** and
set the training host, account and working directory in its options. The same
from the terminal:

```bash
sudo kestrel plugins enable remote-train \
  --set train_host=desktop.lan --set train_user=me --set train_path=/srv/pwn-train
```

Because it sends training data off the device, enabling it asks you to acknowledge
authorized use once, the same as the uploaders (see [Managing
plugins](managing-plugins.md)).

Then **install the device's key on the host** — this step is terminal-only,
because it asks for the host password once (it stores nothing afterward, and all
later training needs no password):

```bash
sudo kestrel remote-train setup
```

That same step also builds the training environment on the host and installs the
machine-learning stack there in the background (the first install pulls PyTorch,
so give it several minutes).

That's all. From here the plugin runs on its own: it checks whenever the device is
online and launches a training run once enough new episodes have been logged, then
pulls the finished model back. The agent picks up a newly pulled model live,
without a restart — and only if it is actually better than what the device already
has, so a stale remote result never replaces a good local one.

## Keeping an eye on it

The plugin's dashboard card shows the training host, the host environment's state,
the live training status, and when a model was last pulled. The same from the
terminal:

```bash
kestrel remote-train status   # host, SSH reachability, and host job state
kestrel remote-train now      # push data, train what's due, pull results now
```

If the card's **Host stack** reads `failed`, the host environment didn't build —
almost always a missing `python3-venv` on a Debian/Ubuntu host. Install the
packages above and run `remote-train setup` again; `remote-train status` shows the
reason.

## Options

Set these when you enable, on the Plugins page, or later with
`sudo kestrel plugins enable remote-train --set key=value`.

| Option | Default | What it does |
| --- | --- | --- |
| `train_host` | — | The training host's hostname or IP. |
| `train_user` | — | The account on the host to train as. |
| `train_path` | — | A working directory on the host (created if missing); everything the plugin uses lives under it. |
| `train_port` | `22` | SSH port on the host. |
| `algorithms` | all four | Which models to keep trained, a comma-separated subset of `RecurrentPPO,PPO,DQN,QR-DQN`. The algorithm the agent runs is always trained, first, even when it isn't listed or the list is empty. |
| `remote_train_min_new_episodes` | `300` | Start a run for a model only once this many new episodes have been logged since it was last trained. |
| `timesteps` | `200000` | Training steps per run (a desktop can afford far more than the device). |
| `interval_seconds` | `3600` | At most this often; it also runs at start, on reconnect, and on `remote-train now`. |
| `timeout` | `1800` | Abort a single transfer or SSH step after this many seconds. |

The training host is independent of your [backup](backup-setup.md) host — they can
be the same machine or different ones.

## Turning it off

Disable it on the device with `sudo kestrel plugins disable remote-train`,
and delete the working directory (`train_path`) on the host.
