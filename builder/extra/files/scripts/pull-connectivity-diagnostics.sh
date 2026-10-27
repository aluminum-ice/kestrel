#!/usr/bin/env bash
#
# Pull a PII-free connectivity snapshot off a field unit, so a device that will
# not reach the internet -- the classic "phone is tethering but the Pi is
# offline" case -- can be triaged off-device (Phase 1 connectivity:
# docs/user-guide/troubleshooting.md).
#
# Runs on your WORKSTATION (not the device) and reads the device READ-ONLY over
# SSH. It ships in the image under /usr/local/share/kestrel/scripts/ so you can
# copy it off a unit, but you run it from your own machine. It collects:
#   - Bluetooth PAN: whether a phone is paired (yes/no only), whether the PAN
#     NetworkManager profiles exist and are up, and the bnep0 link;
#   - Wi-Fi station: whether the kestrel-wifi profile exists and is connected,
#     and its interface link (the network name is masked, see below);
#   - interfaces, default routes and DNS resolvers as the device sees them;
#   - what the device thinks of its own connectivity (NetworkManager's state
#     and a name lookup), and the connectivity journal lines (bt-pan,
#     bt-onboard, wifi-provision and the daemon's internet-available probe);
#   - the non-secret BT PAN tunables from /etc/default/kestrel.
#
# Approach-A redaction: the whole report is passed through a MAC/IPv4/IPv6
# masker ON THE DEVICE before it leaves, so the paired phone's address, link
# IPs, gateway and resolver addresses are all masked, and so is the Wi-Fi
# network name wherever a log line or a connection name would show it. It
# never reads the Wi-Fi passphrase, never dumps NetworkManager secrets and
# never touches config.toml, capture output or keys, so the bundle is safe to
# share.
#
# Usage (from a repo checkout, or a copy pulled off a unit):
#   pull-connectivity-diagnostics.sh user@host [ssh-port]
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
out="connectivity-diagnostics-$stamp"
mkdir -p "$out"

echo "Collecting connectivity diagnostics from $target ..."

# One remote shell gathers everything and pipes it through the redactor, so the
# whole bundle is one SSH round-trip and nothing leaves the device unmasked.
# bash is used so the shared common.sh (which names the kestrel NM profiles and
# the paired-phone check) can be sourced. The heredoc is quoted so it runs
# verbatim on the device.
# shellcheck disable=SC2086
$ssh_cmd "$target" 'bash -s' > "$out/connectivity-report.txt" 2>&1 <<'REMOTE' || \
  echo "  note: the SSH session returned an error; the partial report was still saved." >&2
set -u
sec() { printf "\n===== %s =====\n" "$1"; }

# Shared helpers name the fixed kestrel profiles and the paired-phone file.
# Sourcing is read-only (it only sets vars and sources /etc/default/kestrel).
BT_PAN_CON=kestrel-bt-pan
BT_PAN_STATIC_CON=kestrel-bt-pan-static
WIFI_CON=kestrel-wifi
if [ -r /usr/local/lib/kestrel/common ]; then
  # shellcheck disable=SC1091
  . /usr/local/lib/kestrel/common 2>/dev/null || true
  BT_PAN_CON=${PWN_BT_PAN_CON:-$BT_PAN_CON}
  BT_PAN_STATIC_CON=${PWN_BT_PAN_STATIC_CON:-$BT_PAN_STATIC_CON}
  WIFI_CON=${PWN_WIFI_CON:-$WIFI_CON}
fi

# Mask the Wi-Fi network name the provisioning and connect messages quote
# ("created for", "set for", "connect to"), then hardware (MAC) and peer
# (IPv4/IPv6) addresses: MAC first, then IPv6 (compressed :: and >=4-group
# forms, leaving HH:MM:SS clock times alone), then IPv4.
redact() {
  sed -E \
    -e 's/(created for|station set for|connect to) "[^"]*"/\1 "<ssid>"/g' \
    -e 's/\b([0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}\b/<mac>/g' \
    -e 's/\b([0-9A-Fa-f]{1,4}:){1,}:([0-9A-Fa-f]{1,4}:)*[0-9A-Fa-f]{1,4}\b/<ipv6>/g' \
    -e 's/\b([0-9A-Fa-f]{1,4}:){4,}[0-9A-Fa-f]{1,4}\b/<ipv6>/g' \
    -e 's/\b([0-9]{1,3}\.){3}[0-9]{1,3}\b/<ipv4>/g'
}

report() {
  sec "Bluetooth PAN"
  if type bt_phone >/dev/null 2>&1 && [ -n "$(bt_phone 2>/dev/null)" ]; then
    echo "  paired phone: yes (address redacted)"
  elif [ -r /var/lib/kestrel/bt-phone ]; then
    echo "  paired phone: yes (address redacted)"
  else
    echo "  paired phone: none (run 'sudo kestrel network bt-pair' to onboard one)"
  fi
  for c in "$BT_PAN_CON" "$BT_PAN_STATIC_CON"; do
    if nmcli -t -f NAME connection show 2>/dev/null | grep -qx "$c"; then
      printf '  profile %-22s present, active=%s\n' "$c" \
        "$(nmcli -t -f GENERAL.STATE connection show "$c" 2>/dev/null | grep -q activated && echo yes || echo no)"
    else
      printf '  profile %-22s absent\n' "$c"
    fi
  done
  echo "  bnep0 link:"
  ip -br addr show bnep0 2>&1 | sed 's/^/    /' || echo "    (no bnep0 interface)"

  sec "Wi-Fi station"
  if nmcli -t -f NAME connection show 2>/dev/null | grep -qx "$WIFI_CON"; then
    printf '  profile %s present, active=%s\n' "$WIFI_CON" \
      "$(nmcli -t -f GENERAL.STATE connection show "$WIFI_CON" 2>/dev/null | grep -q activated && echo yes || echo no)"
  else
    echo "  profile $WIFI_CON absent (no Wi-Fi network saved)"
  fi
  echo "  wlan link(s):"
  for i in $(ls /sys/class/net 2>/dev/null | grep -E '^wlan'); do
    ip -br addr show "$i" 2>&1 | sed 's/^/    /'
  done

  sec "interfaces (ip -br)"
  ip -br link 2>&1 || true
  echo "---"
  ip -br addr 2>&1 || true

  sec "default routes"
  ip route show default 2>&1 || true
  ip -6 route show default 2>&1 || true

  sec "DNS resolvers"
  grep -E '^(nameserver|search|domain)' /etc/resolv.conf 2>&1 || echo "(no /etc/resolv.conf)"

  sec "connectivity as the device sees it"
  echo "  NetworkManager: $(nmcli networking connectivity 2>&1 || echo '?')"
  echo "  name lookup (github.com): $(getent hosts github.com >/dev/null 2>&1 && echo ok || echo FAILED)"

  sec "NetworkManager device status"
  # A Wi-Fi profile made outside kestrel (nmtui, nmcli) is named after its
  # SSID, so mask a wifi row's connection name unless it is kestrel's own.
  nmcli -t -f DEVICE,TYPE,STATE,CONNECTION device status 2>&1 \
    | awk -F: -v own="$WIFI_CON" 'BEGIN { OFS = ":" } $2 == "wifi" && $4 != "" && $4 != own { $4 = "<connection>" } { print }' \
    || echo "(nmcli unavailable)"

  sec "BT PAN tunables (/etc/default/kestrel, non-secret)"
  grep -E '^(BT_ONBOARD_MINUTES|BT_PAN_DHCP_TIMEOUT|BT_PAN_RETRY_SECONDS|BT_PAN_STATIC_ADDRESSES|BT_PAN_FALLBACK_DNS|BT_PAN_STACK_RESET|BT_PAN_RESET_AFTER|WIFI_BAND)=' \
    /etc/default/kestrel 2>&1 || echo "(not readable)"

  sec "connectivity journal (last 200)"
  journalctl --no-pager -n 4000 \
      -u kestrel-bt-pan -u kestrel-bt-onboard -u kestrel-wifi-provision -u kestrel-daemon 2>&1 \
    | grep -iE 'pan|bnep|tether|dhcp|wifi|wlan|internet|reachab|offline|online|dns|resolver|gateway|NetworkManager' \
    | tail -n 200 \
    || echo "(no matching journal lines, or the journal needs a privileged account)"
  echo "(done)"
}

report | redact
REMOTE

# A small manifest naming what was collected and from where.
{
  echo "collected: $stamp"
  # The device's address or name stays out of a bundle meant for sharing.
  case "$target" in *@*) shown="${target%%@*}@<host>" ;; *) shown="<host>" ;; esac
  echo "target:    $shown (port $port)"
  echo "contents:"
  echo "  connectivity-report.txt  one labelled, MAC/IP-redacted read-only"
  echo "                           report: BT PAN + Wi-Fi profile/link state,"
  echo "                           interfaces, routes, DNS, device-side"
  echo "                           reachability, NM device status, BT PAN"
  echo "                           tunables and the connectivity journal."
} > "$out/MANIFEST.txt"

bundle="$out.tar.gz"
tar -czf "$bundle" "$out"
rm -rf "$out"

echo "Done. Share this bundle:"
echo "  $(pwd)/$bundle"
