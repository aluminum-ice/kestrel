#!/usr/bin/env bash
#
# Pull a PII-free general health snapshot off a field unit, so a misbehaving
# device can be triaged off-device. This is the "grab this first" bundle: it
# says what the image is, how the hardware is doing, which services are up and
# which have failed, and which features are enabled.
#
# Runs on your WORKSTATION (not the device) and reads the device READ-ONLY over
# SSH. It ships in the image under /usr/local/share/kestrel/scripts/ so you can
# copy it off a unit, but you run it from your own machine. It collects:
#   - the image version (/etc/kestrel/version), board model, OS and kernel;
#   - hardware health: the firmware throttling word (vcgencmd get_throttled),
#     SoC temperature, disk usage, memory, uptime and load;
#   - every kestrel-* systemd unit's active/enabled state, plus the output of
#     `systemctl --failed` (what is broken right now);
#   - which features are on: the capture/daemon/web/agent `enabled` flags and
#     the `kestrel plugins list` summary (plugin names + health);
#   - recent WARNING-and-above journal lines from the kestrel units.
#
# Approach-A redaction: the whole report is passed through a MAC/IPv4/IPv6
# masker ON THE DEVICE before it leaves, so no hardware or peer address is in
# the bundle. It never runs `kestrel status` (which prints the paired phone's
# address and link IPs), never dumps config.toml (Wi-Fi creds, remote hosts,
# the capture whitelist) and never touches capture output, keys or config, so
# the bundle is safe to share. Network SSIDs are not deliberately collected;
# give the report a glance before sharing all the same.
#
# Usage (from a repo checkout, or a copy pulled off a unit):
#   pull-device-diagnostics.sh user@host [ssh-port]
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
out="device-diagnostics-$stamp"
mkdir -p "$out"

echo "Collecting device diagnostics from $target ..."

# One remote shell gathers everything into a single labelled text stream and
# pipes it through the redactor, so the whole bundle is one SSH round-trip, no
# missing tool is ever fatal, and nothing leaves the device unmasked. The
# heredoc is quoted so it runs verbatim on the device (no local expansion).
# shellcheck disable=SC2086
$ssh_cmd "$target" 'sh -s' > "$out/device-report.txt" 2>&1 <<'REMOTE' || \
  echo "  note: the SSH session returned an error; the partial report was still saved." >&2
set -u
sec() { printf "\n===== %s =====\n" "$1"; }
cfg() { /usr/local/lib/kestrel/config-get "$1" "${2:-}" 2>/dev/null || echo "?"; }

# Mask hardware (MAC) and peer (IPv4/IPv6) addresses. MAC first, then IPv6
# (compressed :: form and >=4-group form, so clock times like 06:15:33 with
# only two colons are left alone), then IPv4.
redact() {
  sed -E \
    -e 's/\b([0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}\b/<mac>/g' \
    -e 's/\b([0-9A-Fa-f]{1,4}:){1,}:([0-9A-Fa-f]{1,4}:)*[0-9A-Fa-f]{1,4}\b/<ipv6>/g' \
    -e 's/\b([0-9A-Fa-f]{1,4}:){4,}[0-9A-Fa-f]{1,4}\b/<ipv6>/g' \
    -e 's/\b([0-9]{1,3}\.){3}[0-9]{1,3}\b/<ipv4>/g'
}

report() {
  sec "image version"
  cat /etc/kestrel/version 2>/dev/null || echo "(no /etc/kestrel/version)"

  sec "board model / OS / kernel"
  tr -d '\0' < /proc/device-tree/model 2>/dev/null; echo
  grep -E '^(PRETTY_NAME|VERSION)=' /etc/os-release 2>/dev/null || true
  echo "kernel: $(uname -a)"

  sec "firmware throttling (0x0 = never throttled)"
  if command -v vcgencmd >/dev/null 2>&1; then
    vcgencmd get_throttled 2>&1 || true
    vcgencmd measure_temp 2>&1 || true
    echo "  bits: 0=under-voltage 1=arm-freq-cap 2=throttled 3=soft-temp-limit;"
    echo "        16-19 = the same conditions seen SINCE BOOT."
  else
    echo "(vcgencmd unavailable)"
  fi

  sec "uptime / load"
  uptime 2>&1 || cat /proc/uptime 2>/dev/null || true

  sec "disk usage"
  df -h 2>&1 | grep -vE '^(tmpfs|devtmpfs|overlay|udev)' || true

  sec "memory"
  free -h 2>&1 || true

  sec "kestrel units: active / enabled state"
  for u in kestrel-daemon kestrel-agent kestrel-capture \
           kestrel-bt-pan kestrel-bt-onboard kestrel-wifi-provision \
           kestrel-agent-provision kestrel-capture-housekeeping.timer \
           kestrel-plugins-apply.socket kestrel-web-auth.socket; do
    printf '  %-34s active=%s enabled=%s\n' "$u" \
      "$(systemctl is-active "$u" 2>/dev/null || echo ?)" \
      "$(systemctl is-enabled "$u" 2>/dev/null || echo ?)"
  done

  sec "failed units (systemctl --failed)"
  systemctl --failed --no-legend --no-pager 2>&1 || echo "(not readable)"

  sec "features enabled"
  printf '  capture.enabled = %s\n' "$(cfg capture.enabled false)"
  printf '  daemon.enabled  = %s\n' "$(cfg daemon.enabled false)"
  printf '  web.enabled     = %s (auth=%s)\n' "$(cfg web.enabled false)" "$(cfg web.auth none)"
  printf '  agent.enabled   = %s (algorithm=%s)\n' "$(cfg agent.enabled false)" "$(cfg agent.algorithm baseline)"

  sec "plugins (kestrel plugins list)"
  kestrel plugins list 2>&1 || echo "(kestrel plugins list failed)"

  sec "kestrel journal: WARNING and above (last 200)"
  journalctl -p warning --no-pager -n 4000 \
      -u kestrel-daemon -u kestrel-agent -u kestrel-capture \
      -u kestrel-bt-pan -u kestrel-bt-onboard -u kestrel-wifi-provision 2>&1 \
    | tail -n 200 \
    || echo "(no matching journal lines, or the journal needs a privileged account -- rerun as a user that can read it)"
  echo "(done)"
}

report | redact
REMOTE

# A small manifest naming what was collected and from where.
{
  echo "collected: $stamp"
  echo "target:    $target (port $port)"
  echo "contents:"
  echo "  device-report.txt  one labelled, MAC/IP-redacted read-only report:"
  echo "                     version, board/OS/kernel, throttling+temp, disk,"
  echo "                     memory, uptime/load, kestrel unit states, failed"
  echo "                     units, enabled features, plugin list, warning+"
  echo "                     journal lines."
} > "$out/MANIFEST.txt"

bundle="$out.tar.gz"
tar -czf "$bundle" "$out"
rm -rf "$out"

echo "Done. Share this bundle:"
echo "  $(pwd)/$bundle"
