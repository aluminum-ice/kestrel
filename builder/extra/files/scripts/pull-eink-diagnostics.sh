#!/usr/bin/env bash
#
# Pull a PII-free e-ink diagnostics report off a field unit, so a blank or
# not-updating panel can be triaged off-device. It works for any e-ink plugin:
# --plugin names it (default eink, the deprecated name of eink-ws27; eink-ws27
# for the 2.7" HAT: docs/user-guide/eink-ws27-setup.md; eink-ws213 for the
# 2.13" HAT: docs/user-guide/eink-ws213-setup.md).
#
# Runs on your WORKSTATION (not the device) and reads the device READ-ONLY over
# SSH. It ships in the image under /usr/local/share/kestrel/scripts/ so you can
# copy it off a unit, but you run it from your own machine. It collects the
# things that decide why a panel shows nothing:
#   - the plugin's reported state (`kestrel plugins info <plugin>`) and the
#     plugin list (is it loaded / enabled / healthy, panel detected?);
#   - the [plugins.<plugin>] config table ONLY (variant/rotation/etc.) -- no
#     other config table, so none of the capture whitelist is pulled;
#   - SPI state: /dev/spidev* nodes, the spi kernel module, the config.txt
#     dtparam, and raspi-config's SPI flag;
#   - whether python3-spidev / gpiozero / PIL import in the pi venv, and the
#     plugin's own driver helper files (_<plugin>_*.py beside it);
#   - the daemon journal's lines from the e-ink plugins only (the "panel ready" /
#     "no panel (...)" / "panel display failed" lines that say which probe path
#     ran) and the "loaded N plugin(s)" lines - no other plugin's or the web
#     server's lines, which can name the devices that used the dashboard;
#   - kernel SPI/GPIO messages from dmesg, the board model and the image version.
#
# It deliberately does NOT pull config.toml wholesale, capture output, the
# capture whitelist, SSH keys, Wi-Fi/NetworkManager config or any credential.
# Before anything leaves the device the report is redacted: IPv4/IPv6 and MAC
# addresses, coordinates and the device's hostname (unless it is the shipped
# default "kestrel") are masked. It is saved as ONE plain text file, never an
# archive, since an archive made here would record this computer's account
# names. Read it before you share it.
#
# Usage (from a repo checkout, or a copy pulled off a unit):
#   pull-eink-diagnostics.sh [--plugin NAME] user@host [ssh-port]
#
# Env overrides:
#   SSH_KEY   identity file to use (default: your ssh default)
#
# Then send the printed .txt back for review.

set -eu

plugin=eink
case "${1:-}" in
  --plugin) [ "$#" -ge 2 ] || { echo "usage: $0 [--plugin NAME] user@host [ssh-port]" >&2; exit 2; }
            plugin=$2; shift 2 ;;
  --plugin=*) plugin=${1#--plugin=}; shift ;;
esac
# The name goes into the remote command line, so only a plain plugin name.
case "$plugin" in
  ""|*[!a-z0-9-]*) echo "--plugin: a plugin name is lowercase letters, digits and -" >&2; exit 2 ;;
esac

if [ "$#" -lt 1 ] || [ "$#" -gt 2 ]; then
  echo "usage: $0 [--plugin NAME] user@host [ssh-port]" >&2
  exit 2
fi

target=$1
port=${2:-22}

ssh_cmd="ssh -p $port -o BatchMode=yes -o ConnectTimeout=20"
if [ -n "${SSH_KEY:-}" ]; then
  ssh_cmd="$ssh_cmd -i $SSH_KEY"
fi

stamp=$(date -u +%Y%m%dT%H%M%SZ)
report="$plugin-diagnostics-$stamp.txt"
# The device's address or name stays out of a report meant for sharing, and so
# does the login name unless it is the image's documented default account.
case "$target" in
  pi@*) shown="pi@<host>" ;;
  *@*) shown="<user>@<host>" ;;
  *) shown="<host>" ;;
esac
{
  echo "$plugin e-ink diagnostics"
  echo "collected: $stamp"
  echo "target:    $shown (port $port)"
  echo "contents:  version, board, $plugin plugin state, [plugins.$plugin] config,"
  echo "           SPI nodes/module/config.txt/raspi-config, venv imports, the"
  echo "           e-ink plugins' daemon log lines, service state, dmesg SPI/GPIO,"
  echo "           HAT id. Redacted on the device (addresses, coordinates, hostname)."
} > "$report"

echo "Collecting $plugin diagnostics from $target ..."

# One remote shell gathers everything into a single labelled text stream, so the
# whole bundle is one SSH round-trip and no match / missing tool is never fatal.
# Everything here is read-only. The heredoc is quoted so it runs verbatim on the
# device (no local expansion); the (validated) plugin name is its one input.
# shellcheck disable=SC2086
$ssh_cmd "$target" "PLUGIN=$plugin sh -s" >> "$report" 2>&1 <<'REMOTE' || \
  echo "  note: the SSH session returned an error; the partial report was still saved." >&2
set -u
sec() { printf "\n===== %s =====\n" "$1"; }
gather() {

sec "image version"
cat /etc/kestrel/version 2>/dev/null || echo "(no /etc/kestrel/version)"

sec "board model"
tr -d '\0' < /proc/device-tree/model 2>/dev/null; echo
grep -E '^(PRETTY_NAME|VERSION)=' /etc/os-release 2>/dev/null || true
echo "kernel: $(uname -srvm)"

sec "$PLUGIN plugin state (plugins info $PLUGIN)"
kestrel plugins info "$PLUGIN" 2>&1 || echo "(kestrel plugins info $PLUGIN failed)"

sec "plugin list"
kestrel plugins list 2>&1 || echo "(kestrel plugins list failed)"

sec "[plugins.$PLUGIN] config table only"
awk -v table="[plugins.$PLUGIN]" '
  /^\[/ { keep = ($0 == table) }
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
  PLUGIN="$PLUGIN" "$VENV" - <<'PY' 2>&1
for name in ("spidev", "gpiozero", "PIL"):
    try:
        m = __import__(name)
        print(f"{name}: OK ({getattr(m, '__version__', 'no __version__')})")
    except Exception as e:
        print(f"{name}: FAIL ({e!r})")
import glob
import os
# An e-ink plugin's driver ships beside it as its helper files (_<plugin>_*.py),
# epdconfig first since each driver imports it. Before that release the eink
# plugin's driver was a package inside kestrel_pi.
name = os.environ["PLUGIN"]
plugin = next((p for p in ("/etc/kestrel/plugins/%s.py" % name,
                           "/usr/local/lib/kestrel/plugins/%s.py" % name)
               if os.path.exists(p)), None)
helpers = sorted(os.path.basename(h) for h in glob.glob(
    os.path.join(os.path.dirname(plugin), "_%s_*.py" % name))) if plugin else []
helpers.sort(key=lambda h: not h.endswith("_epdconfig.py"))
def pins_held(exc):
    # epdconfig claims the e-Paper pins when it is imported. While the daemon
    # runs an e-ink plugin (even one that found no panel) it holds them, so an
    # import here can't claim them: that says nothing about the driver.
    text = repr(exc).lower()
    return "busy" in text or type(exc).__name__.endswith("PinInUse") or "in use" in text
if plugin is None:
    print(f"no {name}.py in /etc/kestrel/plugins or /usr/local/lib/kestrel/plugins")
elif helpers:
    from kestrel_pi import load_helper
    for helper in helpers:
        try:
            load_helper(plugin, helper)
            print(f"{helper} (beside {plugin}): import OK")
        except Exception as e:
            if pins_held(e):
                print(f"{helper}: the e-Paper pins are held by the running daemon (an "
                      "e-ink plugin is enabled there), so the driver isn't imported here; "
                      "disable the e-ink plugins to check it")
                break
            print(f"{helper} (beside {plugin}): import FAIL ({e!r})")
elif name == "eink":
    for mod in ("kestrel_pi.vendor.waveshare_epd.epd2in7_V2",
                "kestrel_pi.vendor.waveshare_epd.epd2in7"):
        try:
            __import__(mod)
            print(f"{mod}: import OK")
        except Exception as e:
            print(f"{mod}: import FAIL ({e!r})")
else:
    print(f"no helper files beside {plugin}")
PY
else
  echo "(no pi venv at $VENV)"
fi

sec "pi user groups (needs spi/gpio)"
id pi 2>&1 || echo "(no pi account)"

sec "daemon journal: the e-ink plugins' lines (last 4000 lines searched)"
journalctl -u kestrel-daemon --no-pager -n 4000 2>&1 \
  | awk '/kestrel_pi\.loader/ && /eink/ || /kestrel_pi: loaded [0-9]+ plugin/' \
  | tail -n 300 \
  || echo "(no matching journal lines, or the journal needs a privileged account -- rerun as a user that can read it)"

sec "daemon service state"
systemctl is-active kestrel-daemon 2>&1 || true
systemctl show kestrel-daemon -p ActiveEnterTimestamp -p NRestarts -p MainPID 2>&1 || true

sec "kernel SPI/GPIO messages (dmesg)"
dmesg 2>/dev/null | grep -iE 'spi|gpio' | tail -n 40 \
  || echo "(dmesg not readable without privileges)"

sec "HAT EEPROM id (if any)"
for f in /proc/device-tree/hat/product /proc/device-tree/hat/vendor; do
  [ -e "$f" ] && { printf "%s: " "$f"; tr -d '\0' < "$f"; echo; }
done
echo "(done)"
}

# Everything is redacted here, on the device, before it leaves.
gather 2>&1 | /usr/bin/python3 -I -c '
import re, socket, sys
text = sys.stdin.read()
host = socket.gethostname()
if host and host != "kestrel":
    text = re.sub(r"(?<![\w./-])%s(?![\w./-])" % re.escape(host), "<hostname>", text)
text = re.sub(r"\b(?:[0-9A-Fa-f]{2}:){7,}[0-9A-Fa-f]{2}\b", "<fingerprint>", text)
text = re.sub(r"\b([0-9A-Fa-f]{2}[:-]){5}[0-9A-Fa-f]{2}\b", "<mac>", text)
text = re.sub(r"\b(?:\d{1,3}\.){3}\d{1,3}\b", "<ipv4>", text)
text = re.sub(r"(?<![\w:])(?:[0-9A-Fa-f]{1,4}:)*[0-9A-Fa-f]{0,4}::(?:[0-9A-Fa-f]{1,4}:)*[0-9A-Fa-f]{1,4}(?![\w:])", "<ipv6>", text)
text = re.sub(r"\b(?:[0-9A-Fa-f]{1,4}:){3,7}[0-9A-Fa-f]{1,4}\b", "<ipv6>", text)
text = re.sub(r"-?\b\d{1,3}\.\d{4,}\s*,\s*-?\d{1,3}\.\d{4,}\b", "<coord>, <coord>", text)
sys.stdout.write(text)
'
REMOTE

chmod 600 "$report" 2>/dev/null || true
echo "Done. Read it, then share this report:"
echo "  $(pwd)/$report"
