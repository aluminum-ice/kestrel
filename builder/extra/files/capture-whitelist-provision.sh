#!/bin/bash
# Pre-load the capture whitelist from a file staged on the boot partition, then
# remove the file so it is a one-shot seed and config.toml stays the lasting
# source of truth. Runs from kestrel-capture-whitelist-provision.service
# when the file exists. Mirrors the Wi-Fi station provisioning flow.
#
# File: /boot/firmware/kestrel-whitelist.txt, one entry per line:
#   # SSIDs and/or BSSIDs (MACs) that must NEVER be actively attacked.
#   MyHomeWiFi
#   AA:BB:CC:DD:EE:FF
# Blank lines and #-comments are ignored. Entries are merged (union) into the
# existing [capture] whitelist; duplicates are dropped. See
# docs/user-guide/capture-whitelist.md and docs/developer/capture-interface.md.
set -euo pipefail
# shellcheck source=common.sh
. "${PWN_LIB_DIR:-/usr/local/lib/kestrel}/common"

file=${1:-/boot/firmware/kestrel-whitelist.txt}
[ -f "$file" ] || exit 0

entries=()
while IFS= read -r line || [ -n "$line" ]; do
    line=${line%$'\r'}
    # Trim leading and trailing whitespace.
    line=${line#"${line%%[![:space:]]*}"}
    line=${line%"${line##*[![:space:]]}"}
    case "$line" in ''|'#'*) continue ;; esac
    entries+=("$line")
done < "$file"

# One-shot: remove the staged file so config.toml is the lasting source of truth
# and a later edit there is not undone on the next boot.
rm -f "$file"

if [ "${#entries[@]}" -eq 0 ]; then
    log "no whitelist entries in $file; nothing to pre-load"
    exit 0
fi

capture_whitelist_merge "${entries[@]}"
