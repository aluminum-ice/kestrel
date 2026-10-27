# Shared settings and helpers for the kestrel network scripts.
# Sourced by the scripts in /usr/local/lib/kestrel and by kestrel.
# shellcheck shell=bash
# shellcheck disable=SC2034  # variables are used by the scripts that source this

# As root, never resolve a command through an inherited PATH: pi's login shell
# activates the pi-owned venv, whose python3 runs pi-writable site-packages code
# (-I does not stop that). sudo already resets PATH (secure_path); this covers a
# root shell that kept pi's. Python run as root is always `python3 -I` (no cwd,
# PYTHONPATH or user site on sys.path), and never the venv's.
if [ "$(id -u)" -eq 0 ]; then
    PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
    export PATH
fi

PWN_DEFAULTS=${PWN_DEFAULTS:-/etc/default/kestrel}
PWN_LIB_DIR=${PWN_LIB_DIR:-/usr/local/lib/kestrel}
PWN_STATE_DIR=${PWN_STATE_DIR:-/var/lib/kestrel}
PWN_BT_PHONE_FILE=$PWN_STATE_DIR/bt-phone
PWN_BT_PAN_CON=kestrel-bt-pan
PWN_BT_PAN_STATIC_CON=kestrel-bt-pan-static
PWN_WIFI_CON=kestrel-wifi
PWN_WIFI_DRIVER=brcmfmac   # the built-in chip; the capture dongle never matches

# Capture (Phase 2). config.toml is the capture config; config-get reads it and
# config-set edits one key inside one table (never a file-wide sed: several
# tables share keys such as `enabled`).
PWN_CONFIG=${PWN_CONFIG:-/etc/kestrel/config.toml}
PWN_CONFIG_GET=${PWN_CONFIG_GET:-$PWN_LIB_DIR/config-get}
PWN_CONFIG_SET=${PWN_CONFIG_SET:-$PWN_LIB_DIR/config-set}
PWN_CAPTURE_DIR=${PWN_CAPTURE_DIR:-$PWN_STATE_DIR/capture}
# AngryOxide (root) writes its raw output here, so it sits in the root-owned
# state dir, not in the pi-owned capture store: pi can't plant a symlink in it
# for root to write through. The store's own .run/ is pi's scratch (restore
# state) and a legacy location the collector still drains.
PWN_CAPTURE_RUN_DIR=${PWN_CAPTURE_RUN_DIR:-$PWN_STATE_DIR/capture-run}
PWN_CAPTURE_ARCHIVE_DEFAULT=$PWN_CAPTURE_DIR/archive
PWN_WHITELIST_FILE=${PWN_WHITELIST_FILE:-$PWN_STATE_DIR/whitelist}
PWN_CAPTURE_SERVICE=kestrel-capture.service
# Marker that the authorized-use acknowledgment was given; root-owned, in the
# config dir. Its presence means "remembered"; 'capture reset-ack' removes it.
PWN_CAPTURE_ACK_FILE=${PWN_CAPTURE_ACK_FILE:-/etc/kestrel/capture-ack}

# Plugin host (Phase 3). The daemon runs in the pi venv; the shipped reference
# plugins are read-only, operator plugins are user-added. plugins-ctl is the
# introspection/config helper; it reads plugin declarations statically and runs
# under the system `python3 -I`, never the venv (PWN_VENV_PYTHON is only ever
# run as pi, via run_as_pi). The same authorized-use ack as capture gates
# device-leaving plugin capabilities (network).
PWN_DAEMON_SERVICE=kestrel-daemon.service
PWN_VENV_PYTHON=${PWN_VENV_PYTHON:-/home/pi/.venv/bin/python}

# Learning agent (Phase 4). The agent runs in its own service (pi venv) and
# never rewrites config.toml: it writes its per-window scan plan to the override
# file below, which the capture launcher honours only while [agent] enabled is
# true. The runtime dir holds the model, episode logs and status.json.
PWN_AGENT_DIR=${PWN_AGENT_DIR:-$PWN_STATE_DIR/agent}
PWN_AGENT_OVERRIDE=${PWN_AGENT_OVERRIDE:-$PWN_AGENT_DIR/scan-override.json}
PWN_AGENT_SERVICE=kestrel-agent.service
PWN_PLUGINS_DIR=${PWN_PLUGINS_DIR:-$PWN_LIB_DIR/plugins}
PWN_PLUGINS_USER_DIR=${PWN_PLUGINS_USER_DIR:-/etc/kestrel/plugins}
PWN_PLUGINS_CTL=${PWN_PLUGINS_CTL:-$PWN_LIB_DIR/plugins-ctl}

# Defaults; /etc/default/kestrel overrides them. These code defaults are
# the source of truth on an in-place (app-layer) update, which does not touch
# /etc/default, so a new key MUST be given its default here, not only in
# default.conf.
BT_ONBOARD_MINUTES=10
BT_PAN_DHCP_TIMEOUT=30
BT_PAN_RETRY_SECONDS=30
BT_PAN_STATIC_ADDRESSES=172.20.10.14/28,192.168.44.44/24
BT_PAN_FALLBACK_DNS=2606:4700:4700::1111,2606:4700:4700::1001
# Last-ditch Bluetooth-stack reset for a wedged controller stuck on
# 'bluetooth-failed'. OFF by default and UNTESTED on hardware; see the warning
# in bt-pan.sh before enabling. BT_PAN_RESET_AFTER is how many consecutive
# failed reconnects must pass before one reset is attempted.
BT_PAN_STACK_RESET=no
BT_PAN_RESET_AFTER=4
WIFI_BAND=2.4
# shellcheck source=/dev/null
[ -r "$PWN_DEFAULTS" ] && . "$PWN_DEFAULTS"

log() { echo "$*"; }
die() { echo "error: $*" >&2; exit 1; }

# Monotonic seconds since boot, for timing a window that must survive a
# wall-clock correction. An RTC-less Pi boots with a stale clock and NTP can
# later jump it by months; bash's $SECONDS and `date +%s` both jump with it, so
# a deadline built from them can fire the instant the clock is set. /proc/uptime
# counts monotonically from boot and is unaffected. Echoes 0 if it can't read it.
mono_seconds() {
    local up
    read -r up _ < /proc/uptime 2>/dev/null || { echo 0; return; }
    printf '%s\n' "${up%.*}"
}

valid_mac() { [[ $1 =~ ^([0-9A-F]{2}:){5}[0-9A-F]{2}$ ]]; }

# The recorded phone's MAC, or nothing.
bt_phone() {
    local mac
    [ -r "$PWN_BT_PHONE_FILE" ] || return 0
    mac=$(head -n1 "$PWN_BT_PHONE_FILE")
    if valid_mac "$mac"; then echo "$mac"; fi
}

bt_record_phone() {
    mkdir -p "$PWN_STATE_DIR"
    echo "$1" > "$PWN_BT_PHONE_FILE.tmp"
    mv "$PWN_BT_PHONE_FILE.tmp" "$PWN_BT_PHONE_FILE"
}

# Helpers capture command output before matching on it: the scripts run with
# pipefail, and piping into grep -q or awk's exit can SIGPIPE the writer.

# Wait up to $1 seconds (default 60) for a Bluetooth controller; on the Zero
# 2 W it attaches over UART some seconds after boot.
bt_wait_for_adapter() {
    local tries=${1:-60} out
    while [ "$tries" -gt 0 ]; do
        out=$(bluetoothctl show 2>/dev/null || true)
        [[ $out == *Controller\ * ]] && return 0
        sleep 1
        tries=$((tries - 1))
    done
    return 1
}

bt_adapter_mac() {
    local out
    out=$(bluetoothctl show 2>/dev/null || true)
    awk '/^Controller /{print toupper($2); exit}' <<<"$out"
}

# Paired device MACs, one per line. Asks each known device instead of using a
# "devices Paired" filter, which older bluetoothctl versions silently ignore.
bt_paired_macs() {
    local devices mac info
    devices=$(bluetoothctl devices 2>/dev/null || true)
    for mac in $(awk '$1 == "Device" {print toupper($2)}' <<<"$devices" | sort -u); do
        info=$(bluetoothctl info "$mac" 2>/dev/null || true)
        if grep -q '^[[:space:]]*Paired: yes' <<<"$info"; then echo "$mac"; fi
    done
    return 0
}

# Power the controller on, retrying until it reports Powered: yes or $1 seconds
# (default 30) pass. At boot the adapter attaches over UART a little before
# BlueZ will accept "power on", so a single attempt can lose the race and leave
# the onboarding window shut. Tolerates a transient non-zero from bluetoothctl.
bt_power_on() {
    local tries=${1:-30} out
    while [ "$tries" -gt 0 ]; do
        bluetoothctl power on >/dev/null 2>&1 || true
        out=$(bluetoothctl show 2>/dev/null || true)
        [[ $out == *"Powered: yes"* ]] && return 0
        sleep 1
        tries=$((tries - 1))
    done
    return 1
}

# Open the pairing window: pairable and discoverable on.
bt_open_window() {
    bluetoothctl pairable on >/dev/null 2>&1 || true
    bluetoothctl discoverable on >/dev/null 2>&1 || true
}

# True while the adapter is both advertising (discoverable) and accepting
# pairing, so the onboarding loop can re-assert the window if BlueZ drops it
# (which happens right after the controller first powers on at boot).
bt_window_is_open() {
    local out
    out=$(bluetoothctl show 2>/dev/null || true)
    [[ $out == *"Discoverable: yes"* ]] && [[ $out == *"Pairable: yes"* ]]
}

# Close the pairing window: not discoverable, not pairable.
bt_close_window() {
    bluetoothctl discoverable off >/dev/null 2>&1 || true
    bluetoothctl pairable off >/dev/null 2>&1 || true
}

nm_has_con() {
    local out
    out=$(nmcli -g NAME connection show 2>/dev/null || true)
    grep -qxF "$1" <<<"$out"
}

# First IPv4 address (CIDR) on an interface, or nothing. Never fails, so
# callers under `set -e -o pipefail` survive a missing interface.
iface_ipv4() {
    ip -4 -o addr show dev "$1" 2>/dev/null | awk '{ print $4; exit }' || true
}

nm_con_active() {
    local out
    out=$(nmcli -g NAME connection show --active 2>/dev/null || true)
    grep -qxF "$1" <<<"$out"
}

# Map a user-facing band (2.4, 5, any) to NetworkManager's wifi.band value
# (bg, a, or empty for any band).
wifi_band_nm() {
    case "$1" in
        2.4) echo bg ;;
        5) echo a ;;
        any) echo "" ;;
        *) die "band must be 2.4, 5 or any (got '$1')" ;;
    esac
}

# Bring the Wi-Fi profile up now, reporting nmcli's reason if it can't.
wifi_connect() {
    local ssid=$1 err
    if ! err=$(nmcli connection up "$PWN_WIFI_CON" 2>&1 >/dev/null); then
        err=$(printf '%s\n' "$err" | sed -n 's/^Error: //p' | head -n 1)
        log "saved, but couldn't connect to \"$ssid\" yet${err:+: $err}; NetworkManager keeps retrying"
    fi
}

# Create or replace the Wi-Fi station profile, bound to the built-in chip.
# Args: ssid psk(empty = open network) hidden(yes/no) band(2.4/5/any; default
# WIFI_BAND)
wifi_apply() {
    local ssid=$1 psk=$2 hidden=${3:-no} band=${4:-$WIFI_BAND} nmband
    [ -n "$ssid" ] || die "SSID is empty"
    nmband=$(wifi_band_nm "$band")
    if [ -n "$psk" ] && { [ ${#psk} -lt 8 ] || [ ${#psk} -gt 64 ]; }; then
        die "passphrase must be 8-63 characters (or 64 hex digits)"
    fi
    rfkill unblock wifi 2>/dev/null || true
    nmcli radio wifi on 2>/dev/null || true
    if nm_has_con "$PWN_WIFI_CON"; then
        nmcli connection delete "$PWN_WIFI_CON" >/dev/null
    fi
    local args=(connection add type wifi con-name "$PWN_WIFI_CON" ifname "*" ssid "$ssid"
        match.driver "$PWN_WIFI_DRIVER" connection.autoconnect yes
        wifi.hidden "$hidden")
    if [ -n "$nmband" ]; then
        args+=(wifi.band "$nmband")
    fi
    if [ -n "$psk" ]; then
        args+=(wifi-sec.key-mgmt wpa-psk wifi-sec.psk "$psk")
    fi
    nmcli "${args[@]}" >/dev/null
    wifi_connect "$ssid"
}

# Change the band of the saved Wi-Fi profile without re-entering credentials.
wifi_set_band() {
    local nmband ssid
    nmband=$(wifi_band_nm "$1")
    nm_has_con "$PWN_WIFI_CON" || die "no Wi-Fi network saved yet; use --ssid"
    nmcli connection modify "$PWN_WIFI_CON" wifi.band "$nmband"
    ssid=$(nmcli -g 802-11-wireless.ssid connection show "$PWN_WIFI_CON" 2>/dev/null || true)
    wifi_connect "$ssid"
}

## Capture helpers (Phase 2) ##############################################

# Read a config.toml value by dotted key, with a fallback default.
cfg() { "$PWN_CONFIG_GET" "$1" "${2:-}"; }

# True if the interface supports monitor mode (asks its wiphy via iw).
iface_monitor_capable() {
    local dev=$1 phy
    phy=$(cat "/sys/class/net/$dev/phy80211/name" 2>/dev/null || true)
    [ -n "$phy" ] || return 1
    iw phy "$phy" info 2>/dev/null | grep -qF '* monitor'
}

# Echo the capture interface: an explicit config value, or the first
# monitor-capable wlan whose driver is not the built-in chip (brcmfmac).
# Returns non-zero (and echoes nothing) when none is found.
capture_resolve_iface() {
    local want=${1:-auto} path dev drv
    if [ -n "$want" ] && [ "$want" != auto ]; then
        echo "$want"
        return 0
    fi
    for path in /sys/class/net/wlan*; do
        [ -e "$path" ] || continue
        dev=$(basename "$path")
        drv=$(ethtool -i "$dev" 2>/dev/null | awk -F': ' '/^driver:/{print $2; exit}')
        [ "$drv" = "$PWN_WIFI_DRIVER" ] && continue   # built-in chip, never capture
        if iface_monitor_capable "$dev"; then
            echo "$dev"
            return 0
        fi
    done
    return 1
}

# Echo a comma-separated list of the non-disabled channels the adapter
# supports across all its bands, for the default sweep. Empty if unknown, so
# the launcher can fall back to AngryOxide's own default channel set.
capture_auto_channels() {
    local dev=$1 phy
    phy=$(cat "/sys/class/net/$dev/phy80211/name" 2>/dev/null || true)
    [ -n "$phy" ] || return 0
    iw phy "$phy" info 2>/dev/null | awk '
        /disabled/ { next }
        match($0, /\[[0-9]+\]/) {
            ch = substr($0, RSTART + 1, RLENGTH - 2)
            printf "%s%s", sep, ch
            sep = ","
        }'
}

# Merge entries (args: ESSIDs and/or BSSIDs) into the [capture] whitelist in
# config.toml: a union with what's already there, de-duplicated, existing order
# kept. BSSID-looking entries are upper-cased for consistency. config.toml keeps
# the whitelist on one line (`whitelist = [...]`). Used by the boot pre-load.
capture_whitelist_merge() {
    [ "$#" -gt 0 ] || return 0
    [ -f "$PWN_CONFIG" ] || die "missing $PWN_CONFIG"
    local current added
    current=$(cfg capture.whitelist "" || true)
    added=$(PWN_CONFIG="$PWN_CONFIG" CURRENT="$current" python3 -I - "$@" <<'PY'
import json
import os
import re
import sys

config = os.environ["PWN_CONFIG"]
current = [e for e in os.environ.get("CURRENT", "").splitlines() if e.strip()]
mac = re.compile(r"^([0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}$")


def norm(entry):
    entry = entry.strip()
    return entry.upper() if mac.match(entry) else entry


merged, seen = [], set()
for entry in current:
    entry = norm(entry)
    if entry and entry not in seen:
        seen.add(entry)
        merged.append(entry)
before = len(merged)
for entry in sys.argv[1:]:
    entry = norm(entry)
    if entry and entry not in seen:
        seen.add(entry)
        merged.append(entry)

array = "[" + ", ".join(json.dumps(e) for e in merged) + "]"
with open(config, "r", encoding="utf-8") as fh:
    lines = fh.readlines()
for i, line in enumerate(lines):
    if re.match(r"^whitelist = ", line):
        lines[i] = "whitelist = %s\n" % array
        break
else:
    sys.exit("could not find a top-level 'whitelist =' line in %s" % config)
# Preserve the config's mode/owner across the atomic replace (config.toml is
# 0640 root:pi to protect plugin secrets); os.replace adopts the temp file's
# metadata otherwise.
try:
    st = os.stat(config)
except OSError:
    st = None
tmp = config + ".tmp"
with open(tmp, "w", encoding="utf-8") as fh:
    fh.writelines(lines)
os.replace(tmp, config)
if st is not None:
    try:
        os.chmod(config, st.st_mode)
        os.chown(config, st.st_uid, st.st_gid)
    except OSError:
        pass
print(len(merged) - before)
PY
    ) || die "failed to update the whitelist in $PWN_CONFIG"
    log "whitelist pre-loaded ($added new); now: $(cfg capture.whitelist "" | paste -sd, -)"
}
