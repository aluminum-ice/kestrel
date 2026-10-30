#!/usr/bin/env bash
#
# Pull a PII-free e-ink (Waveshare 2.7" e-Paper HAT) diagnostics bundle off a
# field unit, so a blank/not-updating panel can be triaged off-device
# (Phase 5 e-ink face plugin: docs/user-guide/eink-setup.md).
#
# Runs on your WORKSTATION (not the device) and reads the device READ-ONLY over
# SSH. It ships in the image under /usr/local/share/kestrel/scripts/ so you can
# copy it off a unit, but you run it from your own machine. It collects the
# things that decide why a panel shows nothing:
#   - the eink plugin's reported state (`kestrel plugins info eink`) and the
#     plugin list (is it loaded / enabled / healthy, panel detected?);
#   - the [plugins.eink] config table ONLY (variant/rotation/etc.) -- no other
#     config table, so none of the capture whitelist is pulled;
#   - SPI state: /dev/spidev* nodes, the spi kernel module, the config.txt
#     dtparam, and raspi-config's SPI flag;
#   - whether python3-spidev / gpiozero / PIL import in the pi venv;
#   - the daemon journal's eink lines (the "panel ready" / "no panel (...)" /
#     "panel display failed" / traceback lines that say which probe path ran);
#   - kernel SPI/GPIO messages from dmesg, the board model and the image version.
#
# It deliberately does NOT pull config.toml wholesale, capture output, the
# capture whitelist, SSH keys, Wi-Fi/NetworkManager config or any credential, so
# the bundle is safe to share.
#
# Usage (from a repo checkout, or a copy pulled off a unit):
#   pull-eink-diagnostics.sh user@host [ssh-port]
#
# Env overrides:
#   SSH_KEY   identity file to use (default: your ssh default)
#
# Then send the printed .tar.gz back for review.

set -eu

if [ "$#" -lt 1 ] || [ "$#" -gt 2 ]; then
  echo "usage: $0 user@host [ssh-port]" >&2
  exit 2
fi

target=$1
port=${2:-22}

ssh_cmd="ssh -p $port -o BatchMode=yes -o ConnectTimeout=20"
if [ -n "${SSH_KEY:-}" ]; then
  ssh_cmd="$ssh_cmd -i $SSH_KEY"
fi

stamp=$(date -u +%Y%m%dT%H%M%SZ)
out="eink-diagnostics-$stamp"
mkdir -p "$out"

echo "Collecting e-ink diagnostics from $target ..."

# One remote shell gathers everything into a single labelled text stream, so the
# whole bundle is one SSH round-trip and no match / missing tool is never fatal.
# Everything here is read-only. The heredoc is quoted so it runs verbatim on the
# device (no local expansion).
# shellcheck disable=SC2086
$ssh_cmd "$target" 'sh -s' > "$out/eink-report.txt" 2>&1 <<'REMOTE' || \
  echo "  note: the SSH session returned an error; the partial report was still saved." >&2
set -u
sec() { printf "\n===== %s =====\n" "$1"; }

sec "image version"
cat /etc/kestrel/version 2>/dev/null || echo "(no /etc/kestrel/version)"

sec "board model"
tr -d '\0' < /proc/device-tree/model 2>/dev/null; echo
grep -E '^(PRETTY_NAME|VERSION)=' /etc/os-release 2>/dev/null || true
echo "kernel: $(uname -a)"

sec "eink plugin state (plugins info eink)"
kestrel plugins info eink 2>&1 || echo "(kestrel plugins info eink failed)"

sec "plugin list"
kestrel plugins list 2>&1 || echo "(kestrel plugins list failed)"

sec "[plugins.eink] config table only"
awk '
  /^\[/ { keep = ($0 == "[plugins.eink]") }
  keep { print }
' /etc/kestrel/config.toml 2>/dev/null || echo "(config.toml not readable)"

sec "SPI: device nodes"
ls -l /dev/spidev* 2>&1 || echo "(no /dev/spidev* -- SPI is almost certainly OFF)"

sec "SPI: kernel module"
lsmod 2>/dev/null | grep -i spi || echo "(no spi module loaded)"

sec "SPI: config.txt dtparam"
grep -nE 'spi|dtoverlay' /boot/firmware/config.txt 2>/dev/null \
  || grep -nE 'spi|dtoverlay' /boot/config.txt 2>/dev/null \
  || echo "(no config.txt spi/dtoverlay lines found)"

sec "SPI: raspi-config flag (0 = enabled)"
raspi-config nonint get_spi 2>&1 || echo "(raspi-config get_spi unavailable)"

sec "pi venv imports (spidev / gpiozero / PIL / driver)"
VENV=/home/pi/.venv/bin/python
if [ -x "$VENV" ]; then
  "$VENV" - <<'PY' 2>&1
for name in ("spidev", "gpiozero", "PIL"):
    try:
        m = __import__(name)
        print(f"{name}: OK ({getattr(m, '__version__', 'no __version__')})")
    except Exception as e:
        print(f"{name}: FAIL ({e!r})")
import os
# The driver ships beside the eink plugin as helper files; before that release
# it was a package inside kestrel_pi.
plugin = next((p for p in ("/etc/kestrel/plugins/eink.py",
                           "/usr/local/lib/kestrel/plugins/eink.py")
               if os.path.exists(p)), None)
helpers = plugin and os.path.exists(os.path.join(os.path.dirname(plugin),
                                                 "_eink_epdconfig.py"))
if helpers:
    from kestrel_pi import load_helper
    for name in ("_eink_epdconfig.py", "_eink_epd2in7_V2.py", "_eink_epd2in7.py"):
        try:
            load_helper(plugin, name)
            print(f"{name} (beside {plugin}): import OK")
        except Exception as e:
            print(f"{name} (beside {plugin}): import FAIL ({e!r})")
else:
    for mod in ("kestrel_pi.vendor.waveshare_epd.epd2in7_V2",
                "kestrel_pi.vendor.waveshare_epd.epd2in7"):
        try:
            __import__(mod)
            print(f"{mod}: import OK")
        except Exception as e:
            print(f"{mod}: import FAIL ({e!r})")
PY
else
  echo "(no pi venv at $VENV)"
fi

sec "pi user groups (needs spi/gpio)"
id pi 2>&1 || id 2>&1 || true

sec "daemon journal: eink lines (last 4000)"
journalctl -u kestrel-daemon --no-pager -n 4000 2>&1 \
  | grep -iE 'eink|epd|e-paper|panel|spi|gpio|variant|traceback|Error' \
  | tail -n 300 \
  || echo "(no matching journal lines, or the journal needs a privileged account -- rerun as a user that can read it)"

sec "daemon service status"
systemctl is-active kestrel-daemon 2>&1 || true
systemctl status kestrel-daemon --no-pager -n 15 2>&1 | tail -n 20 || true

sec "kernel SPI/GPIO messages (dmesg)"
dmesg 2>/dev/null | grep -iE 'spi|gpio' | tail -n 40 \
  || echo "(dmesg not readable without privileges)"

sec "HAT EEPROM id (if any)"
for f in /proc/device-tree/hat/product /proc/device-tree/hat/vendor; do
  [ -e "$f" ] && { printf "%s: " "$f"; tr -d '\0' < "$f"; echo; }
done
echo "(done)"
REMOTE

# A small manifest naming what was collected and from where.
{
  echo "collected: $stamp"
  # The device's address or name stays out of a bundle meant for sharing.
  case "$target" in *@*) shown="${target%%@*}@<host>" ;; *) shown="<host>" ;; esac
  echo "target:    $shown (port $port)"
  echo "contents:"
  echo "  eink-report.txt   one labelled read-only report: version, board, eink"
  echo "                    plugin state, [plugins.eink] config, SPI nodes/module/"
  echo "                    config.txt/raspi-config, venv imports, daemon journal"
  echo "                    eink lines, service status, dmesg SPI/GPIO, HAT id."
} > "$out/MANIFEST.txt"

bundle="$out.tar.gz"
tar -czf "$bundle" "$out"
rm -rf "$out"

echo "Done. Share this bundle:"
echo "  $(pwd)/$bundle"
