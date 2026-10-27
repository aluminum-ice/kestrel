#!/usr/bin/env bash
#
# Pull a PII-free capture HEALTH snapshot off a field unit: is capture
# configured and running, is an adapter present, and how big is the store --
# without ever reading what was captured (Phase 2 capture:
# docs/developer/capture-interface.md).
#
# Runs on your WORKSTATION (not the device) and reads the device READ-ONLY over
# SSH. It ships in the image under /usr/local/share/kestrel/scripts/ so you can
# copy it off a unit, but you run it from your own machine. It collects:
#   - the non-secret [capture] config (enabled, interface, scan mode/dwell/
#     bands) -- but NOT the whitelist, whose SSIDs/MACs are real networks;
#   - the authorized-use acknowledgment state;
#   - the detected capture adapter (kestrel's own resolver), and the
#     kestrel-capture unit's systemd health (state, result, restart count);
#   - aggregate store size: hashline-file COUNT and byte size of the capture
#     store, the raw run dir and the archive set count -- counts only.
#
# This script DELIBERATELY reads no capture content: not the .hc22000
# hashlines, not the raw pcapng/kismet, not the whitelist, and not the capture
# service's journal (which carries observed BSSIDs/SSIDs). Everything it prints
# is an aggregate number, a config constant or a systemd state, so the bundle is
# safe to share. It is passed through a MAC/IP masker on the device as a
# backstop.
#
# Usage (from a repo checkout, or a copy pulled off a unit):
#   pull-capture-health.sh user@host [ssh-port]
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
out="capture-health-$stamp"
mkdir -p "$out"

echo "Collecting capture health from $target ..."

# One remote shell gathers everything. bash is used so the shared common.sh
# (config reader, adapter resolver, capture paths) can be reused, so the numbers
# match `kestrel capture status` exactly -- minus the whitelist line. The
# heredoc is quoted so it runs verbatim on the device.
# shellcheck disable=SC2086
$ssh_cmd "$target" 'bash -s' > "$out/capture-health.txt" 2>&1 <<'REMOTE' || \
  echo "  note: the SSH session returned an error; the partial report was still saved." >&2
sec() { printf "\n===== %s =====\n" "$1"; }

# Source the shared helpers (read-only: sets vars, reads /etc/default/kestrel)
# with set -u off, since common.sh is not written to be -u clean on its own.
set +u
if [ -r /usr/local/lib/kestrel/common ]; then
  # shellcheck disable=SC1091
  . /usr/local/lib/kestrel/common 2>/dev/null || true
fi
: "${PWN_CAPTURE_DIR:=/var/lib/kestrel/capture}"
: "${PWN_CAPTURE_RUN_DIR:=$PWN_CAPTURE_DIR/.run}"
: "${PWN_CAPTURE_ARCHIVE_DEFAULT:=$PWN_CAPTURE_DIR/archive}"
: "${PWN_WHITELIST_FILE:=/var/lib/kestrel/whitelist}"
: "${PWN_CAPTURE_SERVICE:=kestrel-capture.service}"
set -u

cfg() { /usr/local/lib/kestrel/config-get "$1" "${2:-}" 2>/dev/null || echo "?"; }
redact() {
  sed -E \
    -e 's/\b([0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}\b/<mac>/g' \
    -e 's/\b([0-9]{1,3}\.){3}[0-9]{1,3}\b/<ipv4>/g'
}

report() {
  sec "capture config (non-secret)"
  printf '  enabled       = %s\n' "$(cfg capture.enabled false)"
  printf '  interface     = %s\n' "$(cfg capture.interface auto)"
  printf '  scan.mode     = %s\n' "$(cfg capture.scan.mode sweep)"
  printf '  scan.dwell_s  = %s\n' "$(cfg capture.scan.dwell_seconds 5)"
  printf '  scan.bands    = %s\n' "$(cfg capture.scan.bands auto)"

  sec "authorized-use acknowledgment"
  if type capture_acked >/dev/null 2>&1 && capture_acked; then
    echo "  remembered (clear with 'sudo kestrel capture reset-ack')"
  elif [ -f /etc/kestrel/capture-ack ]; then
    echo "  remembered (clear with 'sudo kestrel capture reset-ack')"
  else
    echo "  not given (asked on the next 'capture enable')"
  fi

  sec "whitelist size (entries only -- contents NOT read)"
  wl_n=$(cfg capture.whitelist "" | grep -c . 2>/dev/null || echo 0)
  echo "  config whitelist: ${wl_n} entr$([ "$wl_n" = 1 ] && echo y || echo ies)"
  if [ -f "$PWN_WHITELIST_FILE" ]; then
    run_n=$(grep -c . "$PWN_WHITELIST_FILE" 2>/dev/null || echo 0)
    echo "  running service:  ${run_n} loaded"
    [ "$run_n" = "$wl_n" ] || echo "  (count differs -- config edited without a restart?)"
  fi

  sec "adapter (kestrel's resolver)"
  if type capture_resolve_iface >/dev/null 2>&1; then
    r=$(capture_resolve_iface "$(cfg capture.interface auto)" 2>/dev/null || true)
    echo "  detected: ${r:-none detected}"
  else
    echo "  (resolver unavailable; monitor-capable interfaces:)"
    ls /sys/class/net 2>/dev/null | grep -E '^wlan' | sed 's/^/    /' || true
  fi

  sec "capture service health (systemd metadata only)"
  systemctl show "$PWN_CAPTURE_SERVICE" \
    -p ActiveState,SubState,Result,ExecMainStatus,NRestarts,UnitFileState 2>&1 \
    || echo "(systemctl show unavailable)"

  sec "store size (counts only)"
  if [ -d "$PWN_CAPTURE_DIR" ]; then
    n=$(find "$PWN_CAPTURE_DIR" -maxdepth 1 -name '*.hc22000' 2>/dev/null | wc -l | tr -d ' ')
    sz=$(du -sh "$PWN_CAPTURE_DIR" 2>/dev/null | awk '{print $1}')
    echo "  store:   ${n} hashline file(s), ${sz:-?} at $PWN_CAPTURE_DIR"
  else
    echo "  store:   (no capture dir yet)"
  fi
  if [ -d "$PWN_CAPTURE_RUN_DIR" ]; then
    echo "  run dir: $(du -sh "$PWN_CAPTURE_RUN_DIR" 2>/dev/null | awk '{print $1}') raw at $PWN_CAPTURE_RUN_DIR (cap $(cfg capture.housekeeping.max_run_mb 512)MB)"
  fi
  adir=$(cfg capture.housekeeping.archive_dir "$PWN_CAPTURE_ARCHIVE_DEFAULT")
  [ -d "$adir" ] && echo "  archives: $(find "$adir" -mindepth 1 -maxdepth 1 -type d 2>/dev/null | wc -l | tr -d ' ') set(s) at $adir"
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
  echo "  capture-health.txt  capture config (no whitelist), ack state,"
  echo "                      whitelist entry COUNT, adapter, capture service"
  echo "                      systemd health, and aggregate store sizes. No"
  echo "                      capture content is ever read."
} > "$out/MANIFEST.txt"

bundle="$out.tar.gz"
tar -czf "$bundle" "$out"
rm -rf "$out"

echo "Done. Share this bundle:"
echo "  $(pwd)/$bundle"
