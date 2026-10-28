#!/usr/bin/env bash
#
# pull-beta-check.sh - collect the on-device evidence for a Kestrel beta's
# checklist into one local text report you can review. Keep
# beta-check-device.sh (the part that runs on the device) in the same folder.
# The process is in CLAUDE.md, "Verifying a beta"; the rules for changing
# these scripts are in tools/beta-check/README.md.
#
# Runs on your COMPUTER and reaches the device over SSH. Nothing in this script
# names a device, address, account or password: you pass the SSH target, and
# the device account is read from the device (the user the daemon runs as).
#
# Usage:
#   pull-beta-check.sh user@host [ssh-port]
#
# Env overrides:
#   SSH_KEY          identity file to use (default: your ssh default)
#   OUT_DIR          where to save the report (default: the current folder).
#                    It must not be inside a git checkout, so a report can't
#                    be committed by accident.
#   EXPECT_VERSION   the version the device should be on (e.g. 1.0.6-beta.1);
#                    without it the version is reported but not judged
#
# You'll be asked for your SSH login (once) and, on the device, for sudo and
# optionally the dashboard password (all typed on the device's terminal, never
# stored or passed on a command line).
#
# What it does on the device: copies beta-check-device.sh into a private temp folder,
# runs it once with sudo, reads back the report and deletes the folder. That
# script only READS, with these limits:
#   * it never touches the management link: no nmcli, bluetoothctl, ip,
#     rfkill, iw or NetworkManager/Bluetooth/BT-PAN calls;
#   * it never starts, stops, restarts or enables a service;
#   * it never writes /etc/kestrel/config.toml: the config tests run against a
#     throwaway copy in a root-only temp folder, in a Python process where
#     running any command is blocked, and a checksum proves the real file is
#     unchanged;
#   * it makes no request off the device (only an optional one to the
#     dashboard on 127.0.0.1) and loads no model or torch;
#   * it never reads capture content (hashlines, SSIDs, BSSIDs, whitelist);
#     the kismet DB test only counts rows;
#   * files the device user owns are read as that user, never as root;
#   * it runs at low CPU and I/O priority.
# The report is redacted on the device before it leaves (MACs, IPs, GPS-like
# coordinates, the hostname, account names, configured hosts and every secret
# value in config.toml, capture file names), and journal excerpts are limited to
# error lines. Read it before you share it.

set -eu

if [ "$#" -lt 1 ] || [ "$#" -gt 2 ]; then
  echo "usage: $0 user@host [ssh-port]" >&2
  exit 2
fi
target=$1
port=${2:-22}
expect=${EXPECT_VERSION:-}
case "$expect" in
  *[!0-9A-Za-z.-]*) echo "EXPECT_VERSION may hold only letters, digits, '.' and '-'" >&2; exit 2 ;;
esac
case "$port" in
  ''|*[!0-9]*) echo "ssh-port must be a number" >&2; exit 2 ;;
esac

# One SSH connection for every step, so you authenticate once.
ctl_dir=$(mktemp -d)
ssh_cmd=(ssh -p "$port" -o ConnectTimeout=20 -o ControlMaster=auto
         -o "ControlPath=$ctl_dir/cm" -o ControlPersist=300)
if [ -n "${SSH_KEY:-}" ]; then
  ssh_cmd+=(-i "$SSH_KEY")
fi
remote_dir=""
cleanup() {
  if [ -n "$remote_dir" ]; then
    "${ssh_cmd[@]}" "$target" "rm -rf '$remote_dir'" 2>/dev/null || true
  fi
  "${ssh_cmd[@]}" -O exit "$target" 2>/dev/null || true
  rm -rf "$ctl_dir"
}
trap cleanup EXIT

stamp=$(date -u +%Y%m%dT%H%M%SZ)
out_dir=${OUT_DIR:-.}
if [ ! -d "$out_dir" ]; then
  echo "OUT_DIR $out_dir is not a folder" >&2
  exit 2
fi
out_dir=$(cd "$out_dir" && pwd)
if command -v git >/dev/null 2>&1 \
    && git -C "$out_dir" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  echo "$out_dir is inside a git checkout; set OUT_DIR to a folder outside it" >&2
  exit 2
fi
out=$out_dir/kestrel-beta-check-$stamp.txt
if [ ! -r "$(dirname "$0")/beta-check-device.sh" ]; then
  echo "beta-check-device.sh must be in the same folder as this script" >&2
  exit 2
fi

echo "Connecting ..."
remote_dir=$("${ssh_cmd[@]}" "$target" 'mktemp -d /tmp/kbc.XXXXXX')
case "$remote_dir" in
  /tmp/kbc.[A-Za-z0-9]*) ;;
  *) echo "unexpected remote temp folder: $remote_dir" >&2; remote_dir=""; exit 1 ;;
esac

# The device-side script ships beside this one (beta-check-device.sh).
here=$(cd "$(dirname "$0")" && pwd)
device_script=$here/beta-check-device.sh
"${ssh_cmd[@]}" "$target" "cat > '$remote_dir/check.sh'" < "$device_script"

echo "Running the checks on the device (sudo will ask for your password if it needs it) ..."
"${ssh_cmd[@]}" -t "$target" "sudo bash '$remote_dir/check.sh' '$remote_dir' '$expect'"

# One plain text file: no archive, so no local owner names or OS metadata
# (an archive made on a Mac records both) can travel with it.
umask 077
{
  echo "collected: $stamp"
  echo "target:    <user>@<host> (port <port>)"
  echo "expected:  ${expect:-(not given)}"
  echo "CHECK lines (PASS/FAIL/INFO/SKIP/MANUAL) for the beta checklist, the"
  echo "readings behind them and a by-hand list. Redacted on the device."
  "${ssh_cmd[@]}" "$target" "cat '$remote_dir/report.txt'"
} > "$out"
if ! grep -q '^CHECK ' "$out"; then
  echo "the device produced no report" >&2
  exit 1
fi
echo "Done. Review, then hand over this report (it is private; don't post it):"
echo "  $out"
