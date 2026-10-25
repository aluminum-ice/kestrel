#!/bin/bash
# Provision a Wi-Fi station profile from credentials staged on the boot
# partition, then delete the file so the passphrase doesn't stay on the FAT
# partition. Runs from kestrel-wifi-provision.service when the file exists.
#
# File: /boot/firmware/kestrel-wifi.txt, one key=value per line:
#   ssid=My Network
#   psk=my passphrase        (leave empty or omit for an open network)
#   hidden=yes               (optional)
#   country=GB               (optional; Wi-Fi regulatory country)
#   band=5                   (optional; 2.4, 5 or any; default WIFI_BAND)
set -euo pipefail
# shellcheck source=common.sh
. "${PWN_LIB_DIR:-/usr/local/lib/kestrel}/common"

file=${1:-/boot/firmware/kestrel-wifi.txt}
[ -f "$file" ] || exit 0

ssid= psk= hidden=no country= band=
# Parse, don't source: the file is user-edited and may have DOS line endings.
while IFS= read -r line || [ -n "$line" ]; do
    line=${line%$'\r'}
    case "$line" in ''|'#'*) continue ;; esac
    key=${line%%=*}
    value=${line#*=}
    case "$key" in
        ssid) ssid=$value ;;
        psk) psk=$value ;;
        hidden) hidden=$value ;;
        country) country=$value ;;
        band) band=$value ;;
        *) log "ignoring unknown key '$key' in $file" ;;
    esac
done < "$file"

# Remove the file first, so a bad entry can't leave the passphrase behind.
rm -f "$file"

case "$hidden" in yes|no) ;; *) die "hidden must be yes or no (got '$hidden')" ;; esac
if [ -n "$country" ]; then
    [[ $country =~ ^[A-Za-z]{2}$ ]] || die "country must be a two-letter code (got '$country')"
    raspi-config nonint do_wifi_country "${country^^}"
fi

wifi_apply "$ssid" "$psk" "$hidden" "$band"
log "Wi-Fi station profile created for \"$ssid\" on the built-in Wi-Fi"
