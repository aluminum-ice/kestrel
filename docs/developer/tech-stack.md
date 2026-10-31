# Tech stack

Everything kestrel is built from, layer by layer, with the reason each
piece is there. This is a reference for the current build; for *how the pieces
fit together* at runtime see [Architecture](architecture.md).

The project has two very different halves, and most "what language/tool is
this?" confusion comes from mixing them up:

- **The build** turns a stock Raspberry Pi OS image into a finished `.img` on an
  x86 or arm64 Linux host. It is infrastructure-as-code: Packer, Ansible, a few
  pinned tools. None of it ships to the device.
- **The device** runs the finished image: Python application code, shell
  helpers, and systemd units, on top of Raspberry Pi OS. None of the build tools
  are present there.

Keep the halves straight and the stack is small.

---

## Target hardware and OS

| | |
| --- | --- |
| Architecture | **arm64 (aarch64)** only. One image runs on every supported board. |
| Boards | **Raspberry Pi 5** (Cortex-A76) is the primary, standard board. The Raspberry Pi Zero 2 W (Cortex-A53) is also supported as a smaller, lower-power option; the Pi 4 runs it too. |
| Base OS | **Raspberry Pi OS bookworm, 64-bit Lite** — Debian 12, from Raspberry Pi's `raspios_oldstable_lite_arm64` downloads. |
| System Python | **3.11** (what bookworm ships). All device code targets 3.11; no 3.12+ syntax. |
| Init / services | **systemd** (units, socket activation, timers). |

The base image URL and its SHA-256 are pinned in `builder/config.json`
(`iso_url` / `iso_checksum`); bump them together, and check `target_image_size`
(currently ~7.8 GB) still fits.

Capture always uses an **external USB Wi-Fi adapter**. The built-in chip is never
patched (no nexmon, no firmware fields): the adapter does monitor mode, the
built-in radio stays on the management link.

---

## Build-time stack (host only)

The build runs on a Linux host with root, loop devices, `qemu-user-static` and
~15 GB free. `make image` orchestrates it; the full sequence is in the top-level
[`Makefile`](../../Makefile).

| Tool | Version / pin | Why it's here |
| --- | --- | --- |
| **GNU Make** | host | Single entry point (`image`, `lint`, `test`, `clean`). Encodes the tool pins and the build order. |
| **Packer** | **1.9.5** | Drives the image build from `builder/config.json` (a legacy JSON template). Downloaded by the Makefile into `/tmp/raspberrypi/`, not installed system-wide, and checked against the zip's pinned SHA-256 (`PACKER_SHA256_<goarch>`, from HashiCorp's signed `SHA256SUMS`); `tools-check` re-checks the cached binary before each build. Bump the sums with `PACKER_VERSION`. |
| **solo-io/arm-image plugin** | source commit `e1ace42…` (tag `v0.2.8`) | Packer builder that mounts the ARM image's partitions and chroots into it. Built from pinned Go source by `make plugin`, **not** `packer plugins install` (which resolves to v0.2.7, whose partition-scan race fails builds). |
| **Go** | host toolchain | Only to compile the arm-image plugin. Not otherwise part of the project. |
| **QEMU user-mode** (`qemu-user-static`) | host | Runs arm64 binaries inside the chroot on an x86 host, so the playbook can execute in the image. |
| **Ansible** (on host) | from `requirements-lint.txt` | Host-side only for `--syntax-check` in `make lint`. The real playbook run happens *inside* the image with a different Ansible (below). |
| **PiShrink** | commit `a5f9463…` (`v26.03.16`), SHA-256 verified | Shrinks the finished image to its used size. Pinned to a release commit and checksum-checked by the Makefile; bump `PISHRINK_COMMIT` and `PISHRINK_SHA256` together. Run with `-s` because the OS's own first-boot hook grows the rootfs. |
| **p7zip** (`7z`) | host | Archives the final `.img` into the released `.7z`. |
| `curl`, `unzip`, `git`, `sha256sum` | host | Fetch and verify the pinned downloads. |
| `unshare --uts` | host | Wraps Packer so the playbook's hostname change can't leak to the build host. Keep the wrapper. |

### What the build produces

`kestrel-<version>.{img,sha256,7z,7z.sha256}` plus, in the release
workflow, an `update-manifest.json` (see [In-field updates](updates-setup.md)).

### Lint toolchain

Pinned in [`requirements-lint.txt`](../../requirements-lint.txt), run by
`make lint` (and the Lint CI workflow) with no image build:

| Tool | Version | Checks |
| --- | --- | --- |
| **ansible** (full package) | `14.5.0` | `ansible-playbook --syntax-check` on the playbook. The full package (not just `ansible-core`) is needed for the `community.*` modules the playbook uses. |
| **yamllint** | `1.38.0` | All YAML. Config in `.yamllint`: formatting nits off, **duplicate keys are errors**. |
| **shellcheck-py** | `0.11.0.1` | `shellcheck --severity=error` on every `*.sh`. |

`make lint` also byte-compiles the device Python under `builder/extra/files` to
catch syntax errors, redirecting the bytecode cache out of the tree (nothing may
be left under `builder/extra/`).

---

## In-image provisioning

Inside the chroot, the build is pure **Ansible**:

- Packer installs the **`ansible-core` apt package** from the bookworm repos
  (**ansible-core 2.14**). The playbook therefore must not use modules or options
  newer than 2.14 (e.g. `command` no longer accepts `args: warn:`). The only
  collection it uses, `community.general` (for `timezone`), is extracted from
  the bookworm `ansible` package into a temporary collections path instead of
  installing that whole package, whose post-install byte-compiles about 100
  collections (~100 s under QEMU).
- Ansible runs with pipelining on and the minimal fact subset, and the
  playbook installs multi-file groups with one `install`/`cp` command rather
  than a per-file `copy`: every module run starts Python under QEMU (~1.5 s).
  The large `pip install` runs in the background (`async`) while the other
  tasks proceed, and is collected before the `kestrel_pi` package install.
- `builder/config.yml` runs via the `ansible-local` provisioner against
  `127.0.0.1` *inside the image*. All customization lives here; its top-level
  `vars:` block (`system`, `packages`, `pip`, `services`) is where tunables go
  rather than new tasks.
- Ansible (and the extracted collection) is purged again at the end, so it does
  not ship in the image.

`builder/extra/` is the Ansible `playbook_dir`; everything under it is copied
into the image and is a build input (so nothing generated may be left there).

---

## On-device runtime stack

What is actually present and running on a booted device.

### System packages (apt)

Installed by the playbook's `packages.general.install`. The load-bearing ones:

| Package | Used for |
| --- | --- |
| `bluez` | Bluetooth stack for phone-tether (PAN) onboarding. |
| `python3-dbus`, `python3-gi` | The Bluetooth pairing agent (D-Bus + GLib main loop). |
| `iw`, `ethtool`, `rfkill` | Wi-Fi adapter inspection (monitor-mode / driver) and unblocking radios. |
| `hcxtools` | Converts captured `hc22000` to `.cap` for wpa-sec uploads. |
| `python3-smbus` | I2C access for the UPS HAT fuel gauge (INA219). |
| `python3-pil` | Image work for the e-ink / UI rendering path. |
| `rsync` | The `backup` and `remote-train` plugins' transport. |
| `sqlite3`, `p7zip-full`, `git`, `curl`, `ca-certificates` | General device tooling (updates clone, archives, DB). |
| `raspi-config` | Non-interactive system config (`raspi-config nonint do_ssh`). |

Stock Raspberry Pi OS packages in `packages.general.protected` (e.g.
`raspi-config`, `raspberrypi-sys-mods`, `pi-bluetooth`, `rpi-eeprom`,
`userconf-pi`) must survive the apt install — the build fails if they are
removed. Extend that list rather than weakening it.

### Python environment

Device Python is **3.11**. Application dependencies do **not** go into system
Python (bookworm blocks that under PEP 668); they go into the **pi user's
virtualenv at `/home/pi/.venv`** (created with system site-packages), installed
by the playbook's `pip.local.install`.

| venv package | Role |
| --- | --- |
| `flask` | The embedded web dashboard / daemon web subsystem. |
| `requests`, `urllib3` | HTTP for the uploader plugins (wpa-sec, WiGLE) and the updates check. |
| `cryptography` | TLS / crypto support for the above. |
| `pyyaml`, `python-dateutil`, `typing-extensions`, `tqdm` | Daemon and plugin support libraries. |
| `rpi-hardware-pwm` | Fan readout (memtemp fan stats). |
| `vcgencmd` | Board sensor readout (temperature). |
| `stable-baselines3` | Learning agent: PPO / DQN. |
| `sb3-contrib` | Learning agent: RecurrentPPO (default) and QR-DQN. |
| `gymnasium` | The capture environment's reinforcement-learning interface. |
| **`torch==2.10.0`** | The RL backend. Pinned to a release with a **Python 3.11 / arm64 wheel** — the image has no compiler, so no source builds. |
| `numpy` | Numeric backbone for the agent. |
| `pytest` | On-device framework tests (`make test`). |

Rules for changing this list: only add what a feature uses, check it has an
arm64 wheel, and keep `torch` on a version that ships a 3.11/arm64 wheel.

### Runtime config and IPC

- **`/etc/kestrel/config.toml`** — the runtime config, parsed with stdlib
  **`tomllib`** (no TOML dependency). Edited one key at a time through
  `config-set`, never a file-wide `sed`.
- **`/etc/default/kestrel`** — low-level network timings, `KEY=value`.
- **Unix-socket IPC via systemd socket activation** — the unprivileged daemon
  (runs as `pi`) reaches the root `plugins-ctl` helper over
  `kestrel-plugins-apply.socket` (config writes, enable/disable, capture
  and power controls) and `kestrel-web-auth.socket` (verifies the web
  login against `/etc/shadow`). This split is why no web password is stored.

### Capture engine

**AngryOxide v0.9.4**, the `angryoxide-linux-aarch64-musl` static build (musl, so
no glibc-version dependency across boards), downloaded and SHA-256 verified by
the playbook. It is the external process that does the actual 802.11 capture;
kestrel launches and steers it.

---

## Application code

The first-party code, all under `builder/extra/files/`, copied into the image
and installed by the playbook.

| Component | Language | Notes |
| --- | --- | --- |
| **`kestrel_pi` package** | Python 3.11 | The plugin-host daemon, web server, plugin framework and learning agent. `pyproject.toml` version `0.3.0`, GPL-3.0-or-later. pip-installed into the venv from in-image source. Entry points: `kestrel-daemon`, `kestrel-agent`, `kestrel-agent-train`. Its runtime deps come from the venv (`pip.local.install`), not from `pyproject.toml`, so the build stays offline and pinned. |
| **Reference plugins** | Python 3.11 | `builder/extra/files/plugins/*.py`. `Plugin` base + `on_*` hooks modelled on legacy pwnagotchi (not drop-in: several hook signatures differ); honor `HOOK_API_VERSION`. |
| **Web pages** | Python (server-rendered) | No JS framework, no client build step — the pipeline has no JS toolchain. Styled with plain CSS. See [Web UI conventions](web-ui-conventions.md). |
| **Phase 1 / capture shell helpers** | POSIX-ish Bash | The `kestrel` CLI, `common.sh`, capture launcher/collector, config tool. Keep a `.sh`/`.py` suffix in-repo (so `make lint` checks them); installed without it. Must pass `shellcheck --severity=error`. |

### Learning agent

Runs in **its own process** (`kestrel-agent`), deliberately keeping
torch/SB3 out of the daemon. Stack: `gymnasium` env → `stable-baselines3` /
`sb3-contrib` algorithms → `torch` + `numpy`. The action space is discrete so the
algorithms stay swappable. Offline training has its own entry point
(`kestrel-agent-train`).

---

## CI/CD

Forgejo Actions, in `.forgejo/workflows/`:

| Workflow | Trigger | What it does |
| --- | --- | --- |
| **Lint** (`lint.yml`) | every push and pull request | `make lint` with the pinned lint tools. Should always be green. |
| **Test** (`test.yml`) | every push and pull request | `make test`, the `kestrel_pi` pytest suite. |
| **Release** (`main.yml`) | manual (`workflow_dispatch`) | Sets `_version.py`, runs `sudo make image` on the self-hosted `image-builder` runner, publishes a Forgejo release with the artifacts and an `update-manifest.json`. See [Cutting a release](releasing.md). |


Framework unit tests (`make test`) run the venv's `pytest` in the
`kestrel_pi` package; they are **not** part of lint/CI (which only carries the
lint tools) and need `flask`/`requests`/`numpy`. See [Running the
tests](testing.md).

---

## Versioning

`_version.py` holds `__version__='x.y.z'` as the single source of the image
version; the Makefile reads it unless `MY_VERSION` is set. A no-`MY_VERSION`
build stamps `master`, which reads as a dev build and is never offered an
update. See [In-field updates](updates-setup.md) for how the version drives the
update flow, and [Cutting a release](releasing.md) for the maintainer runbook.

---

## Languages at a glance

- **Python 3.11** — daemon, plugins, learning agent, some helpers. The bulk of
  first-party code.
- **Bash (POSIX-ish)** — connectivity and capture helpers, the CLI. Linted with
  shellcheck.
- **YAML** — the Ansible playbook (`config.yml`) and CI.
- **JSON** — the Packer template (`config.json`).
- **Go** — *only* to compile the Packer arm-image plugin on the build host.
- **Make** — build orchestration.
