#!/bin/bash
# kestrel: the unified config tool. The `network` subcommand (Wi-Fi
# station, BT PAN status, BT re-pair) is Phase 1; the `capture` subcommand
# (AngryOxide capture, opt-in) is Phase 2; `setup` is the guided entry point the
# MOTD points to.
#
# Interactive whiptail menus by default; flags do the same without prompts:
#   kestrel setup
#   kestrel network                       # menu
#   sudo kestrel network wifi --ssid NAME [--psk PASS] [--hidden] [--country GB] [--band 2.4|5|any]
#   sudo kestrel network wifi --band 2.4|5|any   # change the saved network's band
#   kestrel network status
#   sudo kestrel network bt-pair          # forget the phone, reopen onboarding
#   kestrel capture status
#   sudo kestrel capture enable [--interface auto|wlanX] [--yes]
#   sudo kestrel capture disable
#   sudo kestrel capture archive          # move captures aside, start fresh
#   sudo kestrel capture clear --yes      # delete the live store
set -euo pipefail
# shellcheck source=common.sh
. "${PWN_LIB_DIR:-/usr/local/lib/kestrel}/common"

PROG=kestrel

have_whiptail() { [ -t 0 ] && command -v whiptail >/dev/null; }
need_root() { [ "$(id -u)" = 0 ] || die "this needs root; re-run with sudo"; }
# Single-quote a value for safe embedding in a shell command (e.g. a remote
# command string run over SSH), escaping any embedded single quotes.
shell_quote() { printf "'%s'" "$(printf '%s' "$1" | sed "s/'/'\\\\''/g")"; }

# whiptail has no --programbox (that is a dialog widget), so run a command and
# show its combined output in a scrollable msgbox instead. Never aborts the
# menu: a failing command still shows its output and the loop keeps going.
run_box() {
    local title=$1 height=$2 width=$3; shift 3
    local out
    out=$("$@" 2>&1) || true
    whiptail --title "$title" --scrolltext --msgbox "$out" "$height" "$width" || true
}

usage() {
    cat <<USAGE
Usage: $PROG <command>

  setup              Guided first-time setup (network and capture)
  network            Network menu (Wi-Fi station, Bluetooth status, re-pair)
  network status     Show Bluetooth PAN and Wi-Fi state
  network wifi ...    Set Wi-Fi station credentials (--ssid, --psk, --hidden, --country,
                     --band 2.4|5|any); --band alone changes the saved network
  network bt-pair    Forget the paired phone and reopen Bluetooth onboarding
  capture            Capture menu (AngryOxide; opt-in, off by default)
  capture status     Show capture state, adapter and store
  capture enable ...  Enable capture (--interface auto|wlanX, --yes to skip the prompt)
  capture disable    Disable capture
  capture reset-ack  Forget the authorized-use acknowledgment (re-ask on next enable)
  capture whitelist [list|add <AP>|remove <AP>]   APs never actively attacked
  capture archive    Archive the capture store and start fresh
  capture rotate     Apply the housekeeping policy now (used by the timer)
  capture clear --yes [--purge]   Delete the live store (and archives with --purge)
  plugins            Plugin menu (list, info, enable, disable)
  plugins list       List plugins and whether each is enabled
  plugins info NAME  Show a plugin's capabilities, options, deps and health
  plugins enable NAME [--yes] [--set k=v ...]   Enable a plugin: asks the
                     authorized-use ack for uploaders, then (in a terminal)
                     walks its options (secrets masked); --set k=v is scriptable
  plugins disable NAME   Disable a plugin
  web                Web page menu (address, login, plugin controls)
  web status         Show the web page address and whether plugin controls are on
  web auth system [--user NAME]
                     Log in with the device account (same as SSH); the default
  web auth basic [--user NAME] [--password-stdin]
                     Require a web-only login instead
  web auth token     Require a generated token (printed once)
  web auth none      No login: read-only page, plugin controls off
  backup             Backup menu (remote target, key setup, run now)
  backup status      Show the remote target and whether passwordless SSH works
  backup setup       Install the device's SSH key on the remote (asks its
                     password once, stores nothing) for passwordless copying
  backup now         Run a one-off backup of the capture data now
  backup restore [--yes]   Restore capture data and the agent model from the
                     remote backup onto a fresh image (data only, not settings)
  agent              Learning-agent menu (status, enable, algorithm, metrics)
  agent status       Show the agent state, policy, algorithm and last status
  agent enable [--algorithm A] [--policy P]   Enable the agent (needs capture on)
  agent disable      Disable the agent; capture returns to the scripted sweep
  agent algorithm A  Set the RL algorithm (RecurrentPPO|PPO|DQN|QR-DQN)
  agent policy P     Set what drives capture (baseline|learned)
  agent import FILE --algorithm A   Install a trained model and use it
  agent train [--algorithm A] [--timesteps N]   Train from the logged episodes
  agent train-now    Ask the running agent to train now (non-blocking)
  agent metrics      Show the agent-vs-sweep comparison
  remote-train       Remote-training menu (train the agent on a desktop host)
  remote-train status   Show the training host, SSH state and host job state
  remote-train setup    Install the device's SSH key on the training host (asks
                     its password once, stores nothing) for passwordless training
  remote-train now   Reconcile now: push data, train due models, pull results
  update             Update menu (check for and apply a new release)
  update status      Show the installed version and any available release
  update check       Check for a new release now (don't wait out the interval)
  update apply [--ref TAG] [--yes]   Install the latest app-layer release in
                     place (needs the 'updates' plugin's check first); a release
                     that changed the base OS is flagged reflash-only
  help               Show this help
USAGE
}

## network status ##########################################################
cmd_status() {
    local phone bnep wifi wifi_dev
    phone=$(bt_phone)
    echo "Bluetooth PAN:"
    if [ -n "$phone" ]; then
        echo "  paired phone: $phone"
        if nm_con_active "$PWN_BT_PAN_CON" || nm_con_active "$PWN_BT_PAN_STATIC_CON"; then
            # NM names the PAN device after the phone's address; the IP
            # interface is bnep0.
            bnep=$(iface_ipv4 bnep0)
            echo "  link: up${bnep:+ ($bnep)}"
        else
            echo "  link: down (is tethering on, in range?)"
        fi
    else
        echo "  no phone paired; run 'sudo $PROG network bt-pair' to onboard one"
    fi
    echo "Wi-Fi station:"
    if nm_has_con "$PWN_WIFI_CON"; then
        if nm_con_active "$PWN_WIFI_CON"; then
            # The profile follows the built-in chip, which may not be wlan0.
            wifi_dev=$(nmcli -g GENERAL.DEVICES connection show "$PWN_WIFI_CON" 2>/dev/null || true)
            wifi=$(iface_ipv4 "${wifi_dev:-wlan0}")
            echo "  connected${wifi:+ ($wifi)}"
        else
            echo "  configured but not connected"
        fi
    else
        echo "  not configured; set it from 'sudo $PROG network wifi ...' or the menu"
    fi
}

## network wifi ############################################################
cmd_wifi() {
    need_root
    local ssid= psk= hidden=no country= band=
    while [ $# -gt 0 ]; do
        case "$1" in
            --ssid) ssid=$2; shift 2 ;;
            --psk) psk=$2; shift 2 ;;
            --hidden) hidden=yes; shift ;;
            --country) country=$2; shift 2 ;;
            --band) band=$2; shift 2 ;;
            *) die "unknown option for 'network wifi': $1" ;;
        esac
    done
    if [ -z "$ssid" ] && [ -n "$band" ] && [ -z "$psk" ] && [ -z "$country" ] && [ "$hidden" = no ]; then
        wifi_set_band "$band"
        log "Wi-Fi band set to $band"
        return 0
    fi
    [ -n "$ssid" ] || die "network wifi needs --ssid"
    if [ -n "$country" ]; then
        [[ $country =~ ^[A-Za-z]{2}$ ]] || die "country must be a two-letter code"
        raspi-config nonint do_wifi_country "${country^^}"
    fi
    wifi_apply "$ssid" "$psk" "$hidden" "$band"
    log "Wi-Fi station set for \"$ssid\""
}

## network bt-pair #########################################################
cmd_bt_pair() {
    need_root
    local phone
    phone=$(bt_phone)
    if [ -n "$phone" ]; then
        bluetoothctl remove "$phone" >/dev/null 2>&1 || true
        rm -f "$PWN_BT_PHONE_FILE"
    fi
    for con in "$PWN_BT_PAN_CON" "$PWN_BT_PAN_STATIC_CON"; do
        nm_has_con "$con" && nmcli connection delete "$con" >/dev/null || true
    done
    systemctl stop kestrel-bt-pan.service 2>/dev/null || true
    log "forgot the paired phone; opening the onboarding window"
    # --no-block: the web re-pair reaches this through the root apply socket,
    # whose helper is killed after 50s; a restart's stop phase can outlast that
    # and the page would report a timeout for a re-pair that went through.
    systemctl restart --no-block kestrel-bt-onboard.service
    log "pair your phone now (see 'journalctl -u kestrel-bt-onboard')"
}

## interactive menu ########################################################
menu_wifi() {
    local ssid psk hidden=no
    ssid=$(whiptail --inputbox "Wi-Fi network name (SSID):" 9 60 3>&1 1>&2 2>&3) || return 0
    [ -n "$ssid" ] || { whiptail --msgbox "No SSID entered." 8 50; return 0; }
    psk=$(whiptail --passwordbox "Passphrase (leave empty for an open network):" 9 60 3>&1 1>&2 2>&3) || return 0
    whiptail --yesno "Is this a hidden network?" 8 50 && hidden=yes || hidden=no
    if cmd_wifi --ssid "$ssid" ${psk:+--psk "$psk"} $([ "$hidden" = yes ] && echo --hidden); then
        whiptail --msgbox "Wi-Fi saved for \"$ssid\"." 8 50
    else
        whiptail --msgbox "Could not save Wi-Fi settings (see the terminal)." 8 60
    fi
}

cmd_network_menu() {
    have_whiptail || { cmd_status; echo; echo "Run in a terminal for the interactive menu, or use the flags ($PROG help)."; return 0; }
    while true; do
        local choice
        choice=$(whiptail --title "kestrel network" --menu "Choose an option" 16 64 5 \
            status "Show Bluetooth and Wi-Fi status" \
            wifi "Set Wi-Fi station credentials" \
            bt-pair "Forget phone and re-run Bluetooth onboarding" \
            quit "Back" 3>&1 1>&2 2>&3) || return 0
        case "$choice" in
            status) run_box "Status" 18 70 cmd_status ;;
            wifi) menu_wifi ;;
            bt-pair) whiptail --yesno "Forget the paired phone and reopen onboarding?" 8 60 && run_box "Re-pair" 14 70 cmd_bt_pair ;;
            quit) return 0 ;;
        esac
    done
}

## capture #################################################################
CAPTURE_ACK_TEXT="AngryOxide transmits (deauthentication frames) to capture WPA\
 handshakes. Use it only on networks you own or are explicitly authorized to\
 test. Unauthorized use may be illegal."

# Set a key in one config.toml table. The value is passed already
# TOML-formatted (true/false, a number, or a quoted string). Scoped to the
# table: `enabled` also lives in [daemon], [web] and every [plugins.*], and a
# file-wide sed of it used to switch all of them along with capture.
config_set() {
    [ -f "$PWN_CONFIG" ] || die "missing $PWN_CONFIG"
    PWN_CONFIG="$PWN_CONFIG" "$PWN_CONFIG_SET" "$1" "$2" "$3" \
        || die "could not set $1.$2 in $PWN_CONFIG"
}

# Set a string key from stdin, so a secret never appears on a command line.
config_set_string() {
    [ -f "$PWN_CONFIG" ] || die "missing $PWN_CONFIG"
    PWN_CONFIG="$PWN_CONFIG" "$PWN_CONFIG_SET" --string "$1" "$2" \
        || die "could not set $1.$2 in $PWN_CONFIG"
}

# Add/remove one string element of a config array (e.g. the capture whitelist),
# fed from stdin so an ESSID with spaces or quotes is passed safely. Both are
# idempotent (add of a present entry, or remove of an absent one, is a no-op).
config_add_string() {
    [ -f "$PWN_CONFIG" ] || die "missing $PWN_CONFIG"
    PWN_CONFIG="$PWN_CONFIG" "$PWN_CONFIG_SET" --add-str "$1" "$2" \
        || die "could not add to $1.$2 in $PWN_CONFIG"
}
config_del_string() {
    [ -f "$PWN_CONFIG" ] || die "missing $PWN_CONFIG"
    PWN_CONFIG="$PWN_CONFIG" "$PWN_CONFIG_SET" --del-str "$1" "$2" \
        || die "could not remove from $1.$2 in $PWN_CONFIG"
}

# Set a [capture] scalar (enabled/interface).
capture_config_set() { config_set capture "$1" "$2"; }

# True if the authorized-use acknowledgment has been given and remembered.
capture_acked() { [ -f "$PWN_CAPTURE_ACK_FILE" ]; }

# Remember the acknowledgment (called in a root context, from enable).
capture_ack_record() {
    mkdir -p "$(dirname "$PWN_CAPTURE_ACK_FILE")"
    printf 'acknowledged %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" > "$PWN_CAPTURE_ACK_FILE"
}

# Satisfy the authorized-use gate. Remembered once: a stored ack (or --yes)
# skips the prompt on later enables; 'capture reset-ack' clears it. Returns
# non-zero only when the user actively declines an interactive prompt.
capture_ack() {
    capture_acked && return 0
    if [ "${1:-no}" = yes ]; then capture_ack_record; return 0; fi
    if have_whiptail; then
        if whiptail --title "Authorized use only" --yesno \
            "$CAPTURE_ACK_TEXT\n\nDo you confirm you are authorized to test these networks?" 14 72; then
            capture_ack_record
            return 0
        fi
        return 1
    else
        die "capture needs the authorized-use acknowledgment; re-run with --yes to confirm you are authorized"
    fi
}

cmd_capture_reset_ack() {
    need_root
    if capture_acked; then
        rm -f "$PWN_CAPTURE_ACK_FILE"
        log "authorized-use acknowledgment cleared; the next 'capture enable' will ask again"
    else
        log "no stored acknowledgment to clear"
    fi
}

cmd_capture_enable() {
    need_root
    local iface=auto yes=no resolved
    while [ $# -gt 0 ]; do
        case "$1" in
            --interface) iface=$2; shift 2 ;;
            --yes|-y) yes=yes; shift ;;
            *) die "unknown option for 'capture enable': $1" ;;
        esac
    done
    resolved=$(capture_resolve_iface "$iface") \
        || die "no monitor-capable capture adapter found (plug in the USB dongle)"
    capture_ack "$yes" || { log "capture not enabled (acknowledgment declined)"; return 1; }
    capture_config_set enabled true
    if [ "$iface" = auto ]; then
        capture_config_set interface '"auto"'
    else
        capture_config_set interface "\"$iface\""
    fi
    systemctl enable "$PWN_CAPTURE_SERVICE"
    # --no-block on the start: adapter detection and the config write above
    # already happened (a missing dongle has surfaced as an error), so the
    # enablement is persisted and nothing more needs the start job to finish.
    # Waiting for it (the capture service's AngryOxide spin-up can be slow) is
    # what pushed a web-page enable past the plugin-apply socket timeout; the
    # live capture card reads `systemctl is-active`, so it reflects the service
    # once it comes up (same idea as the agent restart's --no-block).
    systemctl start --no-block "$PWN_CAPTURE_SERVICE"
    log "capture enabled on $resolved (interface setting: $iface)"
}

cmd_capture_disable() {
    need_root
    # Config first, then a non-blocking stop. The web toggle reaches this
    # through the root apply socket, whose helper is killed at 50s: a blocking
    # `disable --now` waiting out AngryOxide's stop could be killed before the
    # config write, leaving capture stopped but still enabled (back on at the
    # next boot, toggle pointing the wrong way). The dashboard re-probes the
    # service until it reports stopped.
    capture_config_set enabled false
    systemctl disable --now --no-block "$PWN_CAPTURE_SERVICE" 2>/dev/null || true
    log "capture disabled"
}

cmd_capture_status() {
    local resolved adir wl wl_active run_sz run_cap
    echo "Capture:"
    echo "  config: enabled=$(cfg capture.enabled false) interface=$(cfg capture.interface auto)"
    echo "          mode=$(cfg capture.scan.mode sweep) dwell_seconds=$(cfg capture.scan.dwell_seconds 5) bands=$(cfg capture.scan.bands auto)"
    if systemctl is-active --quiet "$PWN_CAPTURE_SERVICE"; then
        echo "  service: running"
    else
        echo "  service: stopped"
    fi
    if capture_acked; then
        echo "  authorized-use ack: remembered ('capture reset-ack' to clear)"
    else
        echo "  authorized-use ack: not given (asked on next 'capture enable')"
    fi
    # Active whitelist (SSIDs/MACs never attacked). config.toml is the source of
    # truth; flag when the running service loaded a different set (config edited
    # without a restart).
    wl=$(cfg capture.whitelist "" | paste -sd, -)
    echo "  whitelist (never attacked): ${wl:-none}"
    if systemctl is-active --quiet "$PWN_CAPTURE_SERVICE" && [ -f "$PWN_WHITELIST_FILE" ]; then
        wl_active=$(paste -sd, - < "$PWN_WHITELIST_FILE" 2>/dev/null || true)
        [ "$wl_active" = "$wl" ] || echo "          (running service loaded: ${wl_active:-none}; restart to apply)"
    fi
    resolved=$(capture_resolve_iface "$(cfg capture.interface auto)" 2>/dev/null || true)
    echo "  adapter: ${resolved:-none detected}"
    if [ -d "$PWN_CAPTURE_DIR" ]; then
        echo "  store: $(find "$PWN_CAPTURE_DIR" -maxdepth 1 -name '*.hc22000' | wc -l) hashline file(s), $(du -sh "$PWN_CAPTURE_DIR" 2>/dev/null | awk '{print $1}') at $PWN_CAPTURE_DIR"
    fi
    if [ -d "$PWN_CAPTURE_RUN_DIR" ]; then
        run_sz=$(du -sh "$PWN_CAPTURE_RUN_DIR" 2>/dev/null | awk '{print $1}')
        run_cap=$(cfg capture.housekeeping.max_run_mb 512)
        echo "  run dir: ${run_sz:-0} raw pcapng/kismet at $PWN_CAPTURE_RUN_DIR (cap ${run_cap}MB, 0=unlimited)"
    fi
    adir=$(cfg capture.housekeeping.archive_dir "$PWN_CAPTURE_ARCHIVE_DEFAULT")
    [ -d "$adir" ] && echo "  archives: $(find "$adir" -mindepth 1 -maxdepth 1 -type d | wc -l) set(s) at $adir"
}

# Combined network + capture status, used by the setup menu and the top-level
# `status` command.
cmd_status_all() { cmd_status; echo; cmd_capture_status; }

# Move the store's hashlines into a timestamped archive set, leaving a fresh
# store. Capture, if running, keeps appending (the collector recreates files).
capture_archive_now() {
    need_root
    local adir ts dest files
    adir=$(cfg capture.housekeeping.archive_dir "$PWN_CAPTURE_ARCHIVE_DEFAULT")
    shopt -s nullglob
    files=("$PWN_CAPTURE_DIR"/*.hc22000)
    shopt -u nullglob
    [ ${#files[@]} -gt 0 ] || { log "no hashlines to archive"; return 0; }
    ts=$(date +%Y-%m-%dT%H-%M-%S)
    dest="$adir/$ts"
    mkdir -p "$dest"
    mv "${files[@]}" "$dest"/
    log "archived ${#files[@]} hashline file(s) to $dest"
    capture_prune_archives
}

capture_prune_archives() {
    local keep adir sets excess i
    keep=$(cfg capture.housekeeping.keep_archives 0)
    [[ $keep =~ ^[0-9]+$ ]] && [ "$keep" -gt 0 ] || return 0
    adir=$(cfg capture.housekeeping.archive_dir "$PWN_CAPTURE_ARCHIVE_DEFAULT")
    [ -d "$adir" ] || return 0
    mapfile -t sets < <(find "$adir" -mindepth 1 -maxdepth 1 -type d | sort)
    excess=$(( ${#sets[@]} - keep ))
    for ((i = 0; i < excess; i++)); do
        rm -rf "${sets[$i]}"
        log "pruned old archive ${sets[$i]}"
    done
}

capture_clear() {
    need_root
    local yes=no purge=no a
    for a in "$@"; do
        case "$a" in
            --yes|-y) yes=yes ;;
            --purge) purge=yes ;;
            *) die "unknown option for 'capture clear': $a" ;;
        esac
    done
    [ "$yes" = yes ] || die "capture clear deletes the live store; re-run with --yes"
    shopt -s nullglob
    rm -f "$PWN_CAPTURE_DIR"/*.hc22000
    shopt -u nullglob
    log "cleared the live capture store"
    if [ "$purge" = yes ]; then
        local adir
        adir=$(cfg capture.housekeeping.archive_dir "$PWN_CAPTURE_ARCHIVE_DEFAULT")
        [ -n "$adir" ] && [ -d "$adir" ] && rm -rf "${adir:?}"/* && log "purged archives"
    fi
}

capture_over_limit() {
    local max_mb max_age mb oldest now age_days
    max_mb=$(cfg capture.housekeeping.max_store_mb 0)
    max_age=$(cfg capture.housekeeping.max_age_days 0)
    [[ $max_mb =~ ^[0-9]+$ ]] || max_mb=0
    [[ $max_age =~ ^[0-9]+$ ]] || max_age=0
    [ -d "$PWN_CAPTURE_DIR" ] || return 1
    if [ "$max_mb" -gt 0 ]; then
        mb=$(du -sm "$PWN_CAPTURE_DIR" 2>/dev/null | awk '{print $1}')
        [ -n "$mb" ] && [ "$mb" -ge "$max_mb" ] && return 0
    fi
    if [ "$max_age" -gt 0 ]; then
        oldest=$(find "$PWN_CAPTURE_DIR" -maxdepth 1 -name '*.hc22000' -printf '%T@\n' 2>/dev/null | sort -n | head -n1)
        if [ -n "$oldest" ]; then
            now=$(date +%s)
            age_days=$(( (now - ${oldest%.*}) / 86400 ))
            [ "$age_days" -ge "$max_age" ] && return 0
        fi
    fi
    return 1
}

# Apply the configured housekeeping policy. The housekeeping timer runs this.
capture_rotate() {
    need_root
    local action
    action=$(cfg capture.housekeeping.action off)
    case "$action" in
        off) return 0 ;;
        archive) capture_archive_now ;;
        rotate) capture_over_limit && capture_archive_now || true ;;
        *) log "unknown capture.housekeeping.action '$action'" ;;
    esac
}

cmd_capture_menu() {
    have_whiptail || { cmd_capture_status; return 0; }
    while true; do
        local choice
        choice=$(whiptail --title "kestrel capture" --menu "WiFi capture (AngryOxide)" 18 66 7 \
            status "Show capture status" \
            enable "Enable capture (authorized use only)" \
            disable "Disable capture" \
            reset-ack "Forget the authorized-use acknowledgment" \
            archive "Archive captures and start fresh" \
            clear "Delete the capture store" \
            quit "Back" 3>&1 1>&2 2>&3) || return 0
        case "$choice" in
            status) run_box "Capture status" 20 74 cmd_capture_status ;;
            enable)
                # The ack prompt must run here, outside run_box (a captured
                # subshell can't drive whiptail). Skip it once it's remembered.
                if capture_acked || whiptail --title "Authorized use only" --yesno "$CAPTURE_ACK_TEXT\n\nConfirm you are authorized to test these networks?" 14 72; then
                    run_box "Enable capture" 16 74 cmd_capture_enable --yes
                fi ;;
            disable) run_box "Disable capture" 12 70 cmd_capture_disable ;;
            reset-ack) run_box "Reset acknowledgment" 10 70 cmd_capture_reset_ack ;;
            archive) run_box "Archive" 12 70 capture_archive_now ;;
            clear) whiptail --yesno "Delete all captured hashlines in the live store?" 8 62 \
                && run_box "Clear" 12 70 capture_clear --yes ;;
            quit) return 0 ;;
        esac
    done
}

# The capture whitelist: ESSIDs/MACs that must never be actively attacked
# (still observed passively). Stored as the [capture] whitelist array; add/remove
# go through the array-aware config writer so an ESSID with spaces/quotes is safe.
cmd_capture_whitelist() {
    local sub=${1:-list}
    shift 2>/dev/null || true
    case "$sub" in
        list)
            local wl
            wl=$(cfg capture.whitelist "")
            if [ -n "$wl" ]; then echo "$wl"; else echo "(whitelist empty)"; fi
            ;;
        add)
            need_root
            [ -n "${1:-}" ] || die "usage: $PROG capture whitelist add <ESSID|BSSID>"
            printf '%s' "$1" | config_add_string capture whitelist
            log "whitelisted (never attacked): $1"
            ;;
        remove|rm)
            need_root
            [ -n "${1:-}" ] || die "usage: $PROG capture whitelist remove <ESSID|BSSID>"
            printf '%s' "$1" | config_del_string capture whitelist
            log "removed from whitelist: $1"
            ;;
        *) die "unknown 'capture whitelist' subcommand: $sub (list|add|remove)" ;;
    esac
}

cmd_capture() {
    [ $# -gt 0 ] || { cmd_capture_menu; return; }
    local sub=$1; shift
    case "$sub" in
        status) cmd_capture_status ;;
        enable) cmd_capture_enable "$@" ;;
        disable) cmd_capture_disable ;;
        reset-ack) cmd_capture_reset_ack ;;
        archive) capture_archive_now ;;
        rotate) capture_rotate ;;
        clear) capture_clear "$@" ;;
        whitelist) cmd_capture_whitelist "$@" ;;
        *) die "unknown 'capture' subcommand: $sub" ;;
    esac
}

## plugins (Phase 3) #######################################################
# Enabling a plugin whose capabilities leave the device (network) needs the
# same authorized-use acknowledgment as capture.
PLUGINS_NET_ACK_TEXT="This plugin transmits data off the device (captured\
 handshakes, or observed networks and their locations). Enable it only for\
 data you are authorized to collect and share. Unauthorized use may be illegal."

# Run the plugins-ctl helper with the pi venv Python (kestrel_pi importable).
plugins_ctl() {
    PWN_CONFIG="$PWN_CONFIG" \
    PWN_PLUGIN_PATHS="$PWN_PLUGINS_DIR:$PWN_PLUGINS_USER_DIR" \
        "$PWN_VENV_PYTHON" "$PWN_PLUGINS_CTL" "$@"
}

plugin_has_network_cap() {
    plugins_ctl caps "$1" 2>/dev/null | grep -qx network
}

# Satisfy the device-leaving acknowledgment, reusing the capture ack state.
plugins_network_ack() {
    capture_acked && return 0
    if [ "${1:-no}" = yes ]; then capture_ack_record; return 0; fi
    if have_whiptail; then
        whiptail --title "Authorized use only" --yesno \
            "$PLUGINS_NET_ACK_TEXT\n\nDo you confirm you are authorized?" 14 72 \
            && { capture_ack_record; return 0; }
        return 1
    fi
    die "this plugin transmits off-device; re-run with --yes to confirm you are authorized"
}

# Interactive prompt helpers. whiptail when there is a terminal, plain read
# otherwise. All three are safe inside $(...) and never read the loop's stdin.
prompt_bool() {   # args: title default(yes/no) -> exit 0 if yes
    local title=$1 def=$2 ans
    if have_whiptail; then
        if [ "$def" = yes ]; then
            whiptail --title "$title" --yesno "Enable this option?" 8 60
        else
            whiptail --title "$title" --defaultno --yesno "Enable this option?" 8 60
        fi
    else
        read -r -p "$title — enable? [y/n] (default $def): " ans
        case "$ans" in
            [Yy]*) return 0 ;;
            [Nn]*) return 1 ;;
            *) [ "$def" = yes ] ;;
        esac
    fi
}

prompt_secret() {   # args: title -> echo value (masked; empty = keep current)
    local title=$1 val
    if have_whiptail; then
        val=$(whiptail --title "$title" --passwordbox "Value (empty keeps current):" 9 70 3>&1 1>&2 2>&3) || val=""
    else
        read -r -s -p "$title (empty keeps current): " val; echo >&2
    fi
    printf '%s' "$val"
}

prompt_text() {   # args: title current -> echo value (prefilled with current)
    local title=$1 cur=$2 val
    if have_whiptail; then
        val=$(whiptail --title "$title" --inputbox "Value:" 9 70 "$cur" 3>&1 1>&2 2>&3) || val="$cur"
    else
        read -r -p "$title [$cur]: " val
        [ -n "$val" ] || val="$cur"
    fi
    printf '%s' "$val"
}

cmd_plugins_list() {
    printf '%-16s %-9s %s\n' NAME STATE SOURCE
    plugins_ctl list | while IFS=$'\t' read -r name state source; do
        printf '%-16s %-9s %s\n' "$name" "$state" "$source"
    done
}

cmd_plugins_info() {
    [ $# -ge 1 ] || die "usage: $PROG plugins info <name>"
    plugins_ctl info "$1" || die "no such plugin: $1"
    # Best-effort live health from the running daemon's status page.
    local port health
    port=$(cfg web.port 8080)
    health=$(curl -fsS --max-time 3 "http://127.0.0.1:${port}/status.json" 2>/dev/null || true)
    if [ -n "$health" ]; then
        echo "health:"
        echo "$health" | "$PWN_VENV_PYTHON" -c 'import json,sys
name=sys.argv[1]
data=json.load(sys.stdin)
for p in data.get("plugins", []):
    if p.get("name")==name:
        print("  state: %s" % p.get("state"))
        if p.get("reason"): print("  reason: %s" % p.get("reason"))
        if p.get("last_error"): print("  last_error: %s" % p.get("last_error"))
        print("  overruns: %s" % p.get("overruns"))
        break
else:
    print("  (not loaded by the running daemon)")' "$1" 2>/dev/null || true
    fi
}

cmd_plugins_enable() {
    need_root
    local name= yes=no
    declare -A preset
    while [ $# -gt 0 ]; do
        case "$1" in
            --yes|-y) yes=yes; shift ;;
            --set) [ $# -ge 2 ] || die "--set needs key=value"
                   preset["${2%%=*}"]="${2#*=}"; shift 2 ;;
            --*) die "unknown option for 'plugins enable': $1" ;;
            *) [ -z "$name" ] || die "one plugin at a time"; name=$1; shift ;;
        esac
    done
    [ -n "$name" ] || die "usage: $PROG plugins enable <name> [--yes] [--set k=v ...]"
    plugins_ctl caps "$name" >/dev/null 2>&1 || die "no such plugin: $name"

    echo "Enabling plugin '$name'. Declared capabilities:"
    plugins_ctl caps "$name" | sed 's/^/  - /'
    if plugin_has_network_cap "$name"; then
        plugins_network_ack "$yes" || { log "not enabled (acknowledgment declined)"; return 1; }
    fi

    # Apply explicit --set overrides up front (any key, declared or not).
    local key
    for key in "${!preset[@]}"; do
        plugins_ctl set "$name" "$key" "${preset[$key]}"
    done

    # Interactive = a terminal and not --yes. Then walk every declared option
    # (secrets masked, scalars prefilled with the current/default, bools as
    # yes/no); non-interactively, only enforce that required options have a
    # value (pass the rest with --set). Read the schema into an array first so
    # the prompts never contend with the loop's stdin.
    local interactive=no
    { [ "$yes" != yes ] && [ -t 0 ]; } && interactive=yes
    local -a schema_lines=()
    mapfile -t schema_lines < <(plugins_ctl schema "$name")

    local line opt typ required secret has_value current val
    for line in "${schema_lines[@]}"; do
        IFS=$'\t' read -r opt typ required secret has_value current <<<"$line"
        [ -n "$opt" ] || continue
        # Already supplied via --set: don't prompt for it again.
        [ -n "${preset[$opt]+x}" ] && continue
        if [ "$interactive" != yes ]; then
            if [ "$required" = 1 ] && [ "$has_value" = 0 ]; then
                die "plugin '$name' needs option '$opt'; pass --set $opt=VALUE"
            fi
            continue
        fi
        case "$typ" in
            bool)
                if prompt_bool "$name: $opt" "$([ "$current" = true ] && echo yes || echo no)"; then
                    val=true
                else
                    val=false
                fi
                [ "$val" = "$current" ] || plugins_ctl set "$name" "$opt" "$val"
                ;;
            *)
                if [ "$secret" = 1 ]; then
                    val=$(prompt_secret "$name: $opt")
                    if [ -z "$val" ]; then
                        { [ "$required" = 1 ] && [ "$has_value" = 0 ]; } && die "option '$opt' is required"
                    else
                        plugins_ctl set "$name" "$opt" "$val"
                    fi
                else
                    val=$(prompt_text "$name: $opt" "$current")
                    { [ -z "$val" ] && [ "$required" = 1 ] && [ "$has_value" = 0 ]; } && die "option '$opt' is required"
                    [ "$val" = "$current" ] || plugins_ctl set "$name" "$opt" "$val"
                fi
                ;;
        esac
    done

    plugins_ctl set "$name" enabled true
    systemctl restart "$PWN_DAEMON_SERVICE"
    log "plugin '$name' enabled; daemon restarted"
}

cmd_plugins_disable() {
    need_root
    [ $# -ge 1 ] || die "usage: $PROG plugins disable <name>"
    plugins_ctl caps "$1" >/dev/null 2>&1 || die "no such plugin: $1"
    plugins_ctl set "$1" enabled false
    systemctl restart "$PWN_DAEMON_SERVICE"
    log "plugin '$1' disabled; daemon restarted"
}

cmd_plugins_menu() {
    have_whiptail || { cmd_plugins_list; return 0; }
    while true; do
        local choice
        choice=$(whiptail --title "kestrel plugins" --menu "Plugin host" 16 70 5 \
            list "List plugins and their state" \
            info "Show a plugin's details" \
            enable "Enable a plugin" \
            disable "Disable a plugin" \
            quit "Back" 3>&1 1>&2 2>&3) || return 0
        case "$choice" in
            list) run_box "Plugins" 20 76 cmd_plugins_list ;;
            info)
                local name
                name=$(whiptail --inputbox "Plugin name:" 9 60 3>&1 1>&2 2>&3) || continue
                [ -n "$name" ] && run_box "Plugin: $name" 22 78 cmd_plugins_info "$name" ;;
            enable)
                local name
                name=$(whiptail --inputbox "Plugin to enable:" 9 60 3>&1 1>&2 2>&3) || continue
                # Not run_box: the enable flow drives its own whiptail prompts,
                # which a captured subshell can't. The subshell contains a die.
                [ -n "$name" ] && { ( cmd_plugins_enable "$name" ) || true; } ;;
            disable)
                local name
                name=$(whiptail --inputbox "Plugin to disable:" 9 60 3>&1 1>&2 2>&3) || continue
                [ -n "$name" ] && run_box "Disable $name" 14 70 cmd_plugins_disable "$name" ;;
            quit) return 0 ;;
        esac
    done
}

cmd_plugins() {
    [ $# -gt 0 ] || { cmd_plugins_menu; return; }
    local sub=$1; shift
    case "$sub" in
        list) cmd_plugins_list ;;
        info) cmd_plugins_info "$@" ;;
        enable) cmd_plugins_enable "$@" ;;
        disable) cmd_plugins_disable "$@" ;;
        *) die "unknown 'plugins' subcommand: $sub" ;;
    esac
}

## web (Phase 3) ###########################################################
# The daemon's web page logs in with the device account by default (auth =
# system): the same username/password you SSH in with, so the two never drift.
# With auth set it also enables/disables plugins and edits their settings; those
# changes are validated and written by a root helper on a local socket
# (kestrel-plugins-apply.socket), so config.toml stays root-owned.

# True when web auth is on (plugin controls usable); only "none" is off.
web_controls_on() {
    case "$(cfg web.auth system)" in
        system) return 0 ;;
        basic) [ -n "$(cfg web.username "")" ] && [ -n "$(cfg web.password "")" ] ;;
        token) [ -n "$(cfg web.token "")" ] ;;
        *) return 1 ;;
    esac
}

cmd_web_status() {
    local port iface ip mode
    port=$(cfg web.port 8080)
    mode=$(cfg web.auth system)
    echo "Web page (port $port):"
    echo "  http://127.0.0.1:$port/ (on the device)"
    for iface in bnep0 wlan0; do
        ip=$(iface_ipv4 "$iface")
        [ -n "$ip" ] && echo "  http://${ip%%/*}:$port/ ($iface)"
    done
    if [ "$mode" = system ]; then
        echo "auth: system (device login: $(cfg web.username "$(id -un)"))"
    else
        echo "auth: $mode"
    fi
    if web_controls_on; then
        echo "plugin controls: on (enable/disable plugins and edit settings on the page)"
    else
        echo "plugin controls: off (turn them on with 'sudo $PROG web auth system')"
    fi
}

cmd_web_auth() {
    need_root
    local mode=${1:-} user= pass= pass_set=no token
    [ $# -gt 0 ] && shift
    case "$mode" in
        system)
            # No credential stored: the page verifies the submitted login against
            # the device account at request time. An optional --user overrides
            # which account (default: the login user).
            while [ $# -gt 0 ]; do
                case "$1" in
                    --user) [ $# -ge 2 ] || die "--user needs a name"; user=$2; shift 2 ;;
                    *) die "unknown option for 'web auth system': $1" ;;
                esac
            done
            if [ -n "$user" ]; then
                printf '%s' "$user" | config_set_string web username
            fi
            config_set web auth '"system"'
            ;;
        basic)
            while [ $# -gt 0 ]; do
                case "$1" in
                    --user) [ $# -ge 2 ] || die "--user needs a name"; user=$2; shift 2 ;;
                    --password-stdin) IFS= read -r pass || true; pass_set=yes; shift ;;
                    *) die "unknown option for 'web auth basic': $1" ;;
                esac
            done
            [ -n "$user" ] || user=$(prompt_text "Web page username" "$(cfg web.username pi)")
            [ -n "$user" ] || die "a username is needed"
            if [ "$pass_set" = no ]; then
                pass=$(prompt_secret "Web page password")
            fi
            if [ -z "$pass" ]; then
                [ -n "$(cfg web.password "")" ] || die "a password is needed"
            else
                printf '%s' "$pass" | config_set_string web password
            fi
            printf '%s' "$user" | config_set_string web username
            config_set web auth '"basic"'
            ;;
        token)
            token=$(python3 -c 'import secrets; print(secrets.token_urlsafe(24))')
            printf '%s' "$token" | config_set_string web token
            config_set web auth '"token"'
            echo "token: $token"
            echo "open the page as http://<device>:$(cfg web.port 8080)/?token=$token"
            echo "(or send 'Authorization: Bearer <token>'); it is not shown again"
            ;;
        none)
            config_set web auth '"none"'
            ;;
        *) die "usage: $PROG web auth system [--user NAME] | basic [--user NAME] [--password-stdin] | token | none" ;;
    esac
    systemctl restart "$PWN_DAEMON_SERVICE"
    log "web auth set to $mode; plugin controls are $(web_controls_on && echo on || echo off); daemon restarted"
}

cmd_web_menu() {
    have_whiptail || { cmd_web_status; return 0; }
    while true; do
        local choice
        choice=$(whiptail --title "kestrel web page" --menu "Web page and plugin controls" 17 72 6 \
            status "Show the page address and whether plugin controls are on" \
            system "Log in with the device account, same as SSH (default)" \
            basic "Require a separate web-only username/password" \
            token "Require a generated token (turns plugin controls on)" \
            none "No login: read-only page, plugin controls off" \
            quit "Back" 3>&1 1>&2 2>&3) || return 0
        case "$choice" in
            status) run_box "Web page" 16 72 cmd_web_status ;;
            system) run_box "Web auth" 12 72 cmd_web_auth system ;;
            # Not run_box: basic prompts with its own whiptail boxes.
            basic) ( cmd_web_auth basic ) || true ;;
            token) run_box "Web token" 14 76 cmd_web_auth token ;;
            none) run_box "Web auth" 10 70 cmd_web_auth none ;;
            quit) return 0 ;;
        esac
    done
}

cmd_web() {
    [ $# -gt 0 ] || { cmd_web_menu; return; }
    local sub=$1; shift
    case "$sub" in
        status) cmd_web_status ;;
        auth) cmd_web_auth "$@" ;;
        *) die "unknown 'web' subcommand: $sub" ;;
    esac
}

## backup (Phase 3.5) ######################################################
# The `backup` plugin mirrors the capture store to a remote host over SSH using
# the pi account's key. `backup setup` installs that key on the remote for
# passwordless copying (ssh-copy-id asks for the remote password once and stores
# nothing); the ssh steps run as the pi user so the daemon's own key is the one
# authorized. `backup now` runs a one-off sync; `backup status` shows state.
BACKUP_KEY_DEFAULT=/home/pi/.ssh/id_ed25519

# Run a command as the pi account (the user the daemon copies as). Already pi:
# run it directly. Otherwise drop to pi with runuser, which needs root.
run_as_pi() {
    if [ "$(id -un)" = pi ]; then
        "$@"
    else
        runuser -u pi -- "$@"
    fi
}

# Run rsync (as pi) with a few retries: a flaky BT-PAN/Wi-Fi tether drops a
# transfer mid-stream, and --partial keeps the progress so a retry resumes it
# rather than restarting. Prints the final attempt's combined output; returns
# rsync's exit status. Pass the rsync arguments (without the leading "rsync").
_rsync_retry() {
    local out rc i=1
    while :; do
        out=$(run_as_pi rsync "$@" 2>&1)
        rc=$?
        [ "$rc" -eq 0 ] && break
        [ "$i" -ge 3 ] && break
        sleep $((i * 5))
        i=$((i + 1))
    done
    printf '%s\n' "$out"
    return "$rc"
}

backup_require_pi_or_root() { [ "$(id -un)" = pi ] || need_root; }

cmd_backup_setup() {
    backup_require_pi_or_root
    local host user port key pub
    host=$(cfg plugins.backup.host "")
    user=$(cfg plugins.backup.user "")
    port=$(cfg plugins.backup.port 22)
    key=$(cfg plugins.backup.ssh_key "$BACKUP_KEY_DEFAULT")
    { [ -n "$host" ] && [ -n "$user" ]; } \
        || die "set the remote first: sudo $PROG plugins enable backup --set host=HOST --set user=USER --set path=/remote/dir"
    pub="$key.pub"
    # Generate the pi account's key if it has none (no passphrase: it is the
    # device's own identity for unattended copying).
    if ! run_as_pi test -f "$key"; then
        log "generating an SSH key for the pi account ($key)"
        run_as_pi ssh-keygen -t ed25519 -N "" -f "$key" -C "kestrel-backup" \
            || die "ssh-keygen failed"
    fi
    echo "Installing the device public key on $user@$host."
    echo "You'll be asked for the REMOTE account's password once; it is sent only"
    echo "to ssh-copy-id to authorize the key and is never stored."
    run_as_pi ssh-copy-id -i "$pub" -p "$port" "$user@$host" \
        || die "ssh-copy-id failed (wrong password, or the host refuses password login?)"
    echo "Verifying passwordless login..."
    if run_as_pi ssh -i "$key" -p "$port" -o BatchMode=yes \
        -o StrictHostKeyChecking=accept-new -o ConnectTimeout=15 "$user@$host" true; then
        log "passwordless SSH to $user@$host works; the backup plugin can now copy without a password"
    else
        die "key installed but the test login failed; check the remote account"
    fi
}

cmd_backup_status() {
    local host user path port key tool
    host=$(cfg plugins.backup.host "")
    user=$(cfg plugins.backup.user "")
    path=$(cfg plugins.backup.path "")
    port=$(cfg plugins.backup.port 22)
    key=$(cfg plugins.backup.ssh_key "$BACKUP_KEY_DEFAULT")
    tool=$(cfg plugins.backup.tool rsync)
    echo "Backup:"
    echo "  plugin: $(cfg plugins.backup.enabled false | grep -qx true && echo enabled || echo disabled)"
    if [ -n "$host" ] && [ -n "$user" ] && [ -n "$path" ]; then
        echo "  target: $user@$host:$path (port $port, $tool)"
    else
        echo "  target: not configured (set host/user/path, then '$PROG backup setup')"
    fi
    if run_as_pi test -f "$key"; then
        echo "  device key: $key.pub"
        if { [ -n "$host" ] && [ -n "$user" ]; } && run_as_pi ssh -i "$key" -p "$port" \
            -o BatchMode=yes -o ConnectTimeout=8 \
            -o StrictHostKeyChecking=accept-new "$user@$host" true 2>/dev/null; then
            echo "  passwordless SSH: working"
        else
            echo "  passwordless SSH: not set up (run 'sudo $PROG backup setup')"
        fi
    else
        echo "  device key: none yet (run 'sudo $PROG backup setup')"
    fi
    echo "  last backup: see 'sudo $PROG plugins info backup' or the web dashboard"
}

cmd_backup_now() {
    backup_require_pi_or_root
    local host user path port key store ssh
    host=$(cfg plugins.backup.host "")
    user=$(cfg plugins.backup.user "")
    path=$(cfg plugins.backup.path "")
    port=$(cfg plugins.backup.port 22)
    key=$(cfg plugins.backup.ssh_key "$BACKUP_KEY_DEFAULT")
    { [ -n "$host" ] && [ -n "$user" ] && [ -n "$path" ]; } \
        || die "configure the remote first: sudo $PROG plugins enable backup --set host=HOST --set user=USER --set path=/remote/dir"
    run_as_pi test -f "$key" || die "no device key yet; run 'sudo $PROG backup setup'"
    store=$PWN_CAPTURE_DIR
    [ -d "$store" ] || die "no capture store at $store yet"
    # Same flags the plugin's rsync uses; a one-off manual sync now. --stats lets
    # us record how much moved so the web dashboard's backup card reflects this
    # manual run (the plugin adopts the shared state file on its next epoch).
    ssh="ssh -i $key -p $port -o BatchMode=yes -o StrictHostKeyChecking=accept-new -o ConnectTimeout=20 -o ServerAliveInterval=15 -o ServerAliveCountMax=4"
    local agent_dir out rc all_out
    agent_dir=$(cfg plugins.backup.agent_dir /var/lib/kestrel/agent)
    log "backing up $store -> $user@$host:$path"
    out=$(_rsync_retry -az --partial --mkpath --stats \
        --exclude '/.run/' --exclude '/.run' \
        -e "$ssh" "$store/" "$user@$host:$path")
    rc=$?
    printf '%s\n' "$out"
    all_out=$out
    [ "$rc" -eq 0 ] || die "backup failed (is passwordless SSH set up? run '$PROG backup setup')"
    # Mirror the learning agent's runtime dir (model + episode logs) to
    # <path>/agent/ too, matching the backup plugin, so a manual or pre-update
    # backup also captures the training data. The ephemeral status/override
    # files are left out; an empty agent_dir (operator opt-out) skips it.
    if [ -n "$agent_dir" ] && [ -d "$agent_dir" ]; then
        log "backing up $agent_dir -> $user@$host:${path%/}/agent"
        out=$(_rsync_retry -az --partial --mkpath --stats \
            --exclude '/status.json' --exclude '/scan-override.json' --exclude '/*.tmp' \
            -e "$ssh" "$agent_dir/" "$user@$host:${path%/}/agent")
        rc=$?
        printf '%s\n' "$out"
        all_out="$all_out"$'\n'"$out"
        [ "$rc" -eq 0 ] || die "agent-data backup failed (is passwordless SSH set up? run '$PROG backup setup')"
    fi
    log "backup complete"
    _backup_record_state "$store" "$all_out"
}

# Record a manual backup in the plugin's shared state file so the web dashboard's
# backup card reflects it. Same JSON shape and the same "advance the time only
# when data actually moved" rule the plugin uses (so a no-op sync doesn't reset
# the card to "just now"). Written as pi, since the state file is pi-owned and
# read back by the daemon.
_backup_record_state() {
    local store=$1 out=$2
    run_as_pi python3 -I - "$store/.backup-state.json" "$out" <<'PY'
import json, os, sys, time

path, out = sys.argv[1], sys.argv[2]


def first_int(line):
    digits = "".join(c for c in line.split(":", 1)[-1] if c.isdigit())
    return int(digits) if digits else 0


files = nbytes = 0
for line in out.splitlines():
    low = line.lower()
    if "regular files transferred" in low:
        files += first_int(line)
    elif "total transferred file size" in low:
        nbytes += first_int(line)
try:
    with open(path, encoding="utf-8") as handle:
        state = json.load(handle)
    if not isinstance(state, dict):
        state = {}
except (OSError, ValueError):
    state = {}
if files:
    state["last_ok"] = time.time()
    state["last_status"] = "ok"
else:
    state["last_status"] = "ok (no changes)"
state["last_files"] = files
state["last_bytes"] = nbytes
tmp = path + ".tmp"
try:
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(state, handle)
    os.replace(tmp, path)
except OSError:
    pass
PY
}

# Record restore progress in a small state file the backup plugin reads, so the
# web dashboard's backup card shows a restore as running / ok / error (the web
# "Restore" button runs this same CLI in the background, where nothing else is
# visible). It lives in the capture store's transient .run/ dir, which BOTH
# backup and restore exclude from the sync, so it never crosses the link in
# either direction. Written as pi, since the daemon that reads it runs as pi.
_restore_set_state() {
    local store=$1 status=$2 detail=${3:-} stats=${4:-}
    run_as_pi python3 -I - "$store/.run/restore-state.json" "$status" "$detail" "$stats" <<'PY'
import json, os, sys, time

path, status = sys.argv[1], sys.argv[2]
detail = sys.argv[3] if len(sys.argv) > 3 else ""
stats = sys.argv[4] if len(sys.argv) > 4 else ""


def first_int(line):
    digits = "".join(c for c in line.split(":", 1)[-1] if c.isdigit())
    return int(digits) if digits else 0


os.makedirs(os.path.dirname(path), exist_ok=True)
if status == "running":
    state = {"status": "running", "started": time.time()}
else:
    try:
        with open(path, encoding="utf-8") as handle:
            state = json.load(handle)
        if not isinstance(state, dict):
            state = {}
    except (OSError, ValueError):
        state = {}
    state["status"] = status
    state["finished"] = time.time()
    if detail:
        state["detail"] = detail[:200]
    else:
        state.pop("detail", None)
    # On success record how much the restore pulled (summed across the capture-
    # store and agent --stats blocks) so the dashboard card can report it.
    if status == "ok":
        files = nbytes = 0
        for line in stats.splitlines():
            low = line.lower()
            if "regular files transferred" in low:
                files += first_int(line)
            elif "total transferred file size" in low:
                nbytes += first_int(line)
        state["files"] = files
        state["bytes"] = nbytes
    else:
        state.pop("files", None)
        state.pop("bytes", None)
tmp = path + ".tmp"
try:
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(state, handle)
    os.replace(tmp, path)
except OSError:
    pass
PY
}

# Restore the capture data and the agent model from the remote backup onto a
# fresh image. The mirror image of `backup now`: the plugin lays the capture
# store at <path>/ and nests the agent dir at <path>/agent/, so the store pulls
# back from <path>/ (minus that agent subdir and the transient run dir) and the
# agent model/logs pull back from <path>/agent/. Settings are NOT in the backup
# (operator config is excluded), so this restores data only.
cmd_backup_restore() {
    backup_require_pi_or_root
    local host user path port key store agent ssh yes=no
    while [ $# -gt 0 ]; do
        case "$1" in
            --yes|-y) yes=yes; shift ;;
            *) die "unknown option for 'backup restore': $1" ;;
        esac
    done
    host=$(cfg plugins.backup.host "")
    user=$(cfg plugins.backup.user "")
    path=$(cfg plugins.backup.path "")
    port=$(cfg plugins.backup.port 22)
    key=$(cfg plugins.backup.ssh_key "$BACKUP_KEY_DEFAULT")
    { [ -n "$host" ] && [ -n "$user" ] && [ -n "$path" ]; } \
        || die "configure the remote first: sudo $PROG plugins enable backup --set host=HOST --set user=USER --set path=/remote/dir"
    run_as_pi test -f "$key" || die "no device key yet; run 'sudo $PROG backup setup'"
    command -v rsync >/dev/null 2>&1 || die "rsync is required for restore"
    store=$PWN_CAPTURE_DIR
    agent=$PWN_AGENT_DIR
    ssh="ssh -i $key -p $port -o BatchMode=yes -o StrictHostKeyChecking=accept-new -o ConnectTimeout=20 -o ServerAliveInterval=15 -o ServerAliveCountMax=4"
    if [ "$yes" != yes ]; then
        printf 'Restore capture data and the agent model from %s@%s:%s\ninto %s and %s?\nExisting local files with the same names are overwritten (nothing on the remote is changed). [y/N] ' \
            "$user" "$host" "$path" "$store" "$agent"
        local ans; read -r ans || ans=
        case "$ans" in y|Y|yes|YES) ;; *) die "aborted" ;; esac
    fi
    mkdir -p "$store" "$agent"
    _restore_set_state "$store" running
    local out rc stats=""
    # Capture store: everything under <path>/ except the nested agent dir and the
    # transient run dir. --stats lets us report how much was restored. No
    # --delete: a restore only adds/overwrites, it never wipes local files the
    # backup happens not to contain.
    log "restoring capture store <- $user@$host:$path"
    out=$(_rsync_retry -az --partial --mkpath --stats \
        --exclude '/agent/' --exclude '/agent' --exclude '/.run/' --exclude '/.run' \
        -e "$ssh" "$user@$host:$path/" "$store/")
    rc=$?
    printf '%s\n' "$out"
    if [ "$rc" -ne 0 ]; then
        _restore_set_state "$store" error "capture restore failed"
        die "capture restore failed (is passwordless SSH set up? run '$PROG backup setup')"
    fi
    stats=$out
    # Agent model + episode logs, when the remote has them (older backups, or the
    # CLI `backup now`, may not include the agent dir).
    if run_as_pi ssh -i "$key" -p "$port" -o BatchMode=yes \
        -o StrictHostKeyChecking=accept-new -o ConnectTimeout=20 \
        "$user@$host" test -d "$path/agent" 2>/dev/null; then
        log "restoring agent model/logs <- $user@$host:$path/agent"
        out=$(_rsync_retry -az --partial --mkpath --stats \
            -e "$ssh" "$user@$host:$path/agent/" "$agent/")
        rc=$?
        printf '%s\n' "$out"
        if [ "$rc" -ne 0 ]; then
            _restore_set_state "$store" error "agent restore failed"
            die "agent restore failed"
        fi
        stats="$stats
$out"
    else
        log "no agent backup on the remote; skipping the agent dir"
    fi
    # The daemon, capture and agent all run as pi. If this ran as root (and a
    # dir had to be created here), make sure pi owns everything restored.
    if [ "$(id -un)" != pi ]; then
        chown -R pi:pi "$store" "$agent" 2>/dev/null || true
    fi
    _restore_set_state "$store" ok "" "$stats"
    log "restore complete"
    echo "Capture data and the agent model are restored. Settings are not part of"
    echo "the backup, so re-enable capture/plugins/the agent and re-enter any API"
    echo "keys; the restored model takes effect once the agent is enabled."
}

cmd_backup_menu() {
    have_whiptail || { cmd_backup_status; return 0; }
    while true; do
        local choice
        choice=$(whiptail --title "kestrel backup" --menu "Remote backup of capture data" 17 72 5 \
            status "Show the remote target and SSH key state" \
            setup "Install the device key on the remote (asks its password once)" \
            now "Run a backup now" \
            restore "Restore capture data + agent model from the remote" \
            quit "Back" 3>&1 1>&2 2>&3) || return 0
        case "$choice" in
            status) run_box "Backup status" 16 76 cmd_backup_status ;;
            # Not run_box: setup drives ssh-copy-id's own password prompt, which
            # a captured subshell can't.
            setup) ( cmd_backup_setup ) || true ;;
            now) run_box "Backup now" 16 76 cmd_backup_now ;;
            # Not run_box: restore asks its own y/N confirmation.
            restore) ( cmd_backup_restore ) || true ;;
            quit) return 0 ;;
        esac
    done
}

cmd_backup() {
    [ $# -gt 0 ] || { cmd_backup_menu; return; }
    local sub=$1; shift
    case "$sub" in
        status) cmd_backup_status ;;
        setup) cmd_backup_setup ;;
        now) cmd_backup_now ;;
        restore) cmd_backup_restore "$@" ;;
        *) die "unknown 'backup' subcommand: $sub" ;;
    esac
}

## agent (Phase 4) #########################################################
# The learning agent drives capture (channels/dwell/transmit) in place of the
# scripted sweep. It is opt-in and only runs while capture is enabled; it adds
# no new transmission, so enabling it takes no new acknowledgment beyond the
# Phase 2 capture one. The algorithm is config-selected (RecurrentPPO default);
# the policy is baseline (the sweep) or learned (a trained model.zip). The same
# keys are settable from the web dashboard, which writes them through the root
# apply helper after the same enum validation applied here.
AGENT_ALGORITHMS="RecurrentPPO PPO DQN QR-DQN"
AGENT_POLICIES="baseline learned"

agent_valid_algo() {
    local a
    for a in $AGENT_ALGORITHMS; do [ "$1" = "$a" ] && return 0; done
    return 1
}
agent_valid_policy() {
    local p
    for p in $AGENT_POLICIES; do [ "$1" = "$p" ] && return 0; done
    return 1
}

agent_running() { systemctl is-active --quiet "$PWN_AGENT_SERVICE"; }

# True if a trained model already exists for an algorithm (its per-algorithm
# model file, or a legacy model.zip whose sidecar matches). Uses the package
# helper so the per-algorithm naming never drifts from the code that writes it.
agent_has_model() {
    local algo=$1
    [ -n "$algo" ] || return 1
    run_as_pi "$PWN_VENV_PYTHON" -c '
import sys
from kestrel_pi.agent import policies
sys.exit(0 if policies.has_model_for(sys.argv[1], sys.argv[2]) else 1)
' "$PWN_AGENT_DIR" "$algo" >/dev/null 2>&1
}

# Drop a "train now" request the running agent picks up (within ~15s) and runs
# in the background. Shared by 'agent train-now' and the auto-train-on-switch
# path; the agent is the final gate (board size, memory, temperature).
agent_request_train() {
    mkdir -p "$PWN_AGENT_DIR"
    : > "$PWN_AGENT_DIR/train-request"
    # The agent runs as pi and removes the request; keep it pi-owned when this
    # is run as root (e.g. from the web apply helper).
    chown pi:pi "$PWN_AGENT_DIR/train-request" 2>/dev/null || true
}

# Set [agent] algorithm/policy from already-validated values, then restart the
# agent if it is running so the change (a new SB3 class / model) takes effect.
agent_apply_set() {
    local algo=$1 policy=$2
    [ -n "$algo" ] && config_set agent algorithm "\"$algo\""
    [ -n "$policy" ] && config_set agent policy "\"$policy\""
    # Switching to an algorithm that has no trained model yet: warn and kick off
    # a background training run to build its model. Each algorithm keeps its own
    # model file, so switching back to one you've already trained just reloads
    # that saved model with no retraining. The agent's training thread runs the
    # request when it is running and resources allow (it refuses on a too-small
    # board / under load and says so on the dashboard).
    if [ -n "$algo" ] && ! agent_has_model "$algo"; then
        log "no trained model for $algo yet; starting a background training run to build one (it needs logged episodes and runs when the board/resources allow). Switching back to an algorithm you've already trained reloads its saved model with no retraining."
        agent_request_train
    fi
    # Only restart when the agent is already running; enabling from stopped
    # starts it below. The restart branch must not be a trailing `&&` list: as
    # the function's last command it would return non-zero when the agent is
    # not running, and `set -e` would abort the caller (agent enable/set) before
    # it writes enabled=true or starts the service — silently, which the web UI
    # then reports only as the generic "could not change the agent".
    #
    # --no-block: the config change above is already persisted, so the running
    # agent only needs to be bounced to pick it up. A blocking restart waits for
    # the whole restart job, whose stop phase can sit for up to systemd's
    # TimeoutStopSec while a torch training child (kestrel-agent-train) in the
    # service cgroup winds down. The web /agent switch reaches this through the
    # root apply socket, whose client gives up well before that, so a slow stop made the
    # page report a spurious "could not reach the settings helper (timed out)"
    # even though the change had been written. Enqueue the restart and return at
    # once; the /agent page's "starting up" auto-refresh covers the agent coming
    # back (same idea as bt-pan's `systemctl --no-block start`).
    if { [ -n "$algo" ] || [ -n "$policy" ]; } && agent_running; then
        systemctl restart --no-block "$PWN_AGENT_SERVICE"
        log "agent restart requested to apply the change"
    fi
}

cmd_agent_enable() {
    need_root
    local yes=no algo= policy=
    while [ $# -gt 0 ]; do
        case "$1" in
            --yes|-y) yes=yes; shift ;;
            --algorithm) algo=$2; shift 2 ;;
            --policy) policy=$2; shift 2 ;;
            *) die "unknown option for 'agent enable': $1" ;;
        esac
    done
    [ "$(cfg capture.enabled false)" = true ] \
        || die "enable capture first: sudo $PROG capture enable  (the agent only drives an enabled capture)"
    [ -n "$algo" ] && { agent_valid_algo "$algo" || die "unknown algorithm '$algo' (one of: $AGENT_ALGORITHMS)"; }
    [ -n "$policy" ] && { agent_valid_policy "$policy" || die "unknown policy '$policy' (one of: $AGENT_POLICIES)"; }
    agent_apply_set "$algo" "$policy"
    config_set agent enabled true
    systemctl enable --now "$PWN_AGENT_SERVICE"
    log "learning agent enabled (algorithm=$(cfg agent.algorithm RecurrentPPO) policy=$(cfg agent.policy baseline)); it now chooses channels/dwell in place of the sweep"
    [ "$yes" = yes ] || true
}

cmd_agent_disable() {
    need_root
    # Config first, then a non-blocking stop, for the same reason as the agent
    # restart's --no-block (agent_apply_set): the stop can sit for up to
    # TimeoutStopSec while a torch training child winds down, past the apply
    # socket's budget, and the config write after it would then never happen.
    config_set agent enabled false
    systemctl disable --now --no-block "$PWN_AGENT_SERVICE" 2>/dev/null || true
    log "learning agent disabled; capture returns to the configured sweep"
}

cmd_agent_set() {
    need_root
    local algo= policy=
    while [ $# -gt 0 ]; do
        case "$1" in
            --algorithm) algo=$2; shift 2 ;;
            --policy) policy=$2; shift 2 ;;
            *) die "unknown option for 'agent set': $1" ;;
        esac
    done
    { [ -n "$algo" ] || [ -n "$policy" ]; } || die "nothing to set (give --algorithm and/or --policy)"
    [ -n "$algo" ] && { agent_valid_algo "$algo" || die "unknown algorithm '$algo' (one of: $AGENT_ALGORITHMS)"; }
    [ -n "$policy" ] && { agent_valid_policy "$policy" || die "unknown policy '$policy' (one of: $AGENT_POLICIES)"; }
    agent_apply_set "$algo" "$policy"
    log "agent config set${algo:+ algorithm=$algo}${policy:+ policy=$policy}"
}

cmd_agent_import() {
    need_root
    local model= algo= meta=
    while [ $# -gt 0 ]; do
        case "$1" in
            --algorithm) algo=$2; shift 2 ;;
            --meta) meta=$2; shift 2 ;;
            --*) die "unknown option for 'agent import': $1" ;;
            *) [ -z "$model" ] || die "one model at a time"; model=$1; shift ;;
        esac
    done
    [ -n "$model" ] || die "usage: $PROG agent import <model.zip> --algorithm <$AGENT_ALGORITHMS>"
    [ -f "$model" ] || die "no such model file: $model"
    # The sidecar's algorithm (or --algorithm) must be a known one: the value
    # selects the SB3 class, so it is validated before anything is written.
    if [ -z "$algo" ] && [ -z "$meta" ] && [ -f "$model.meta.json" ]; then
        meta="$model.meta.json"
    fi
    if [ -n "$meta" ]; then
        [ -f "$meta" ] || die "no such sidecar: $meta"
        algo=$(PWN_META="$meta" "$PWN_VENV_PYTHON" -c 'import json,os;print(json.load(open(os.environ["PWN_META"])).get("algorithm",""))' 2>/dev/null || true)
    fi
    [ -n "$algo" ] || die "give the model's algorithm: --algorithm <$AGENT_ALGORITHMS>"
    agent_valid_algo "$algo" || die "unknown algorithm '$algo' (one of: $AGENT_ALGORITHMS)"
    mkdir -p "$PWN_AGENT_DIR"
    # Install under the algorithm's own model name so it coexists with models
    # for other algorithms; write_meta writes the matching per-algorithm sidecar.
    local model_file
    model_file=$(run_as_pi "$PWN_VENV_PYTHON" -c 'import sys; from kestrel_pi.agent import model_name_for; print(model_name_for(sys.argv[1]))' "$algo") \
        || die "could not resolve the model filename for $algo"
    install -o pi -g pi -m 0644 "$model" "$PWN_AGENT_DIR/$model_file"
    run_as_pi "$PWN_VENV_PYTHON" -c "from kestrel_pi.agent import policies; policies.write_meta('$PWN_AGENT_DIR', '$algo')"
    config_set agent algorithm "\"$algo\""
    config_set agent policy '"learned"'
    agent_running && systemctl restart "$PWN_AGENT_SERVICE" || true
    log "imported model into $PWN_AGENT_DIR/$model_file (algorithm $algo, policy=learned); enable the agent to use it"
}

cmd_agent_train() {
    local args=()
    while [ $# -gt 0 ]; do
        case "$1" in
            --algorithm) args+=(--algorithm "$2"); shift 2 ;;
            --timesteps) args+=(--timesteps "$2"); shift 2 ;;
            *) die "unknown option for 'agent train': $1" ;;
        esac
    done
    backup_require_pi_or_root
    log "training from the logged episodes (this can take a while on-device)"
    run_as_pi "$PWN_VENV_PYTHON" -m kestrel_pi.agent.train \
        --agent-dir "$PWN_AGENT_DIR" --config "$PWN_CONFIG" "${args[@]}"
}

# Ask the running agent to start a training run now, without blocking: drop a
# request file the agent's training thread picks up (within ~15s) and runs in
# the background, so it shows on the dashboard like idle training. Unlike
# 'agent train' (which trains synchronously in the foreground), this returns at
# once; it needs the agent to be running (enabled) for anything to pick it up.
cmd_agent_train_now() {
    backup_require_pi_or_root
    agent_request_train
    log "requested an on-device training run; the agent starts it within ~15s if resources allow"
}

cmd_agent_metrics() {
    local status="$PWN_AGENT_DIR/status.json"
    PWN_AGENT_DIR="$PWN_AGENT_DIR" "$PWN_VENV_PYTHON" - <<'PY' || true
import os
from kestrel_pi.agent import metrics
d = os.environ["PWN_AGENT_DIR"]
rows = metrics.load_rows(os.path.join(d, "episodes"))
print(metrics.format_table(metrics.summarize(rows)))
PY
    [ -f "$status" ] || true
}

cmd_agent_status() {
    echo "Learning agent:"
    echo "  config: enabled=$(cfg agent.enabled false) algorithm=$(cfg agent.algorithm RecurrentPPO) policy=$(cfg agent.policy baseline)"
    echo "          decision_seconds=$(cfg agent.decision_seconds 30) train=$(cfg agent.train auto)"
    if agent_running; then
        echo "  service: running"
    else
        echo "  service: stopped"
    fi
    if [ "$(cfg capture.enabled false)" != true ]; then
        echo "  note: capture is off, so the agent does not drive anything until 'capture enable'"
    fi
    local status="$PWN_AGENT_DIR/status.json"
    if [ -f "$status" ]; then
        echo "  last status:"
        PWN_STATUS="$status" "$PWN_VENV_PYTHON" -c 'import json,os
s=json.load(open(os.environ["PWN_STATUS"]))
for k in ("policy","algorithm","posture","dwell","transmit","last_reward","session_yield","decisions","detail"):
    if s.get(k) not in (None,""): print("    %s: %s" % (k, s[k]))' 2>/dev/null || true
    fi
}

cmd_agent_menu() {
    have_whiptail || { cmd_agent_status; return 0; }
    while true; do
        local choice
        choice=$(whiptail --title "kestrel agent" --menu "Learning capture agent" 18 72 7 \
            status "Show the agent state, policy and metrics" \
            enable "Enable the agent (needs capture on)" \
            disable "Disable the agent (back to the sweep)" \
            algorithm "Choose the RL algorithm" \
            policy "Choose baseline (sweep) or learned" \
            metrics "Show the agent-vs-sweep comparison" \
            quit "Back" 3>&1 1>&2 2>&3) || return 0
        case "$choice" in
            status) run_box "Agent status" 20 76 cmd_agent_status ;;
            enable) run_box "Enable agent" 16 76 cmd_agent_enable --yes ;;
            disable) run_box "Disable agent" 12 70 cmd_agent_disable ;;
            algorithm)
                local a
                a=$(whiptail --title "Algorithm" --menu "Pick the RL algorithm" 15 60 4 \
                    RecurrentPPO "recurrent (default)" PPO "" DQN "" QR-DQN "" 3>&1 1>&2 2>&3) || continue
                run_box "Set algorithm" 12 70 cmd_agent_set --algorithm "$a" ;;
            policy)
                local p
                p=$(whiptail --title "Policy" --menu "What drives capture" 12 60 2 \
                    baseline "the scripted sweep" learned "the trained model" 3>&1 1>&2 2>&3) || continue
                run_box "Set policy" 12 70 cmd_agent_set --policy "$p" ;;
            metrics) run_box "Agent vs sweep" 22 78 cmd_agent_metrics ;;
            quit) return 0 ;;
        esac
    done
}

cmd_agent() {
    [ $# -gt 0 ] || { cmd_agent_menu; return; }
    local sub=$1; shift
    case "$sub" in
        status) cmd_agent_status ;;
        enable) cmd_agent_enable "$@" ;;
        disable) cmd_agent_disable ;;
        set) cmd_agent_set "$@" ;;
        algorithm) [ $# -ge 1 ] || die "usage: $PROG agent algorithm <$AGENT_ALGORITHMS>"; cmd_agent_set --algorithm "$1" ;;
        policy) [ $# -ge 1 ] || die "usage: $PROG agent policy <$AGENT_POLICIES>"; cmd_agent_set --policy "$1" ;;
        import) cmd_agent_import "$@" ;;
        train) cmd_agent_train "$@" ;;
        train-now) cmd_agent_train_now ;;
        metrics) cmd_agent_metrics ;;
        *) die "unknown 'agent' subcommand: $sub" ;;
    esac
}

## remote-train (offload agent training to a desktop host) #################
# The `remote-train` plugin trains the learning agent on a more powerful Linux
# host over SSH, using the pi account's key (reused from backup by default, just
# authorized on the training host too). `remote-train setup` installs that key
# on the host; the plugin does the push/train/pull. Independent of the backup
# host. See docs/developer/remote-training.md.
REMOTE_TRAIN_KEY_DEFAULT=/home/pi/.ssh/id_ed25519

remote_train_require_pi_or_root() { [ "$(id -un)" = pi ] || need_root; }

# Where the plugin (and so the "reconcile now" trigger) lives on the device.
rt_agent_dir() { cfg plugins.remote-train.agent_dir "$PWN_AGENT_DIR"; }

cmd_remote_train_setup() {
    remote_train_require_pi_or_root
    local host user port key pub
    host=$(cfg plugins.remote-train.train_host "")
    user=$(cfg plugins.remote-train.train_user "")
    port=$(cfg plugins.remote-train.train_port 22)
    key=$(cfg plugins.remote-train.ssh_key "$REMOTE_TRAIN_KEY_DEFAULT")
    { [ -n "$host" ] && [ -n "$user" ]; } \
        || die "set the training host first: sudo $PROG plugins enable remote-train --set train_host=HOST --set train_user=USER --set train_path=/remote/dir"
    pub="$key.pub"
    # Reuse the pi account's key; generate it only if there is none at all (it is
    # the device's own identity, shared with backup).
    if ! run_as_pi test -f "$key"; then
        log "generating an SSH key for the pi account ($key)"
        run_as_pi ssh-keygen -t ed25519 -N "" -f "$key" -C "kestrel" \
            || die "ssh-keygen failed"
    fi
    echo "Installing the device public key on $user@$host (the training host)."
    echo "You'll be asked for the REMOTE account's password once; it is sent only"
    echo "to ssh-copy-id to authorize the key and is never stored."
    run_as_pi ssh-copy-id -i "$pub" -p "$port" "$user@$host" \
        || die "ssh-copy-id failed (wrong password, or the host refuses password login?)"
    echo "Verifying passwordless login..."
    run_as_pi ssh -i "$key" -p "$port" -o BatchMode=yes \
        -o StrictHostKeyChecking=accept-new -o ConnectTimeout=15 "$user@$host" true \
        || die "key installed but the test login failed; check the remote account"
    log "passwordless SSH to the training host works"
    # Nudge the plugin so host preparation (venv + ML stack) starts now, if it is
    # enabled. Harmless if it is not yet enabled.
    remote_train_touch_now
    echo "The host's training environment (an isolated venv + the ML stack) is"
    echo "prepared in the background on the next run; a first run waits for it."
}

# Drop the "reconcile now" trigger the plugin watches (same idea as the agent's
# train-request): the running plugin picks it up on its next epoch, wakes its
# worker and removes the file. Written as pi so the pi daemon can remove it.
remote_train_touch_now() {
    local dir; dir=$(rt_agent_dir)
    mkdir -p "$dir"
    : > "$dir/.remote-train-now"
    chown pi:pi "$dir/.remote-train-now" 2>/dev/null || true
}

cmd_remote_train_now() {
    remote_train_require_pi_or_root
    [ "$(cfg plugins.remote-train.enabled false)" = true ] \
        || echo "note: the remote-train plugin is not enabled; enable it to train." >&2
    remote_train_touch_now
    echo "Asked remote-train to reconcile now; it pushes data, trains due models"
    echo "on the host and pulls back any finished model within an epoch. Watch the"
    echo "AI Agent dashboard card (or '$PROG remote-train status') for progress."
}

cmd_remote_train_status() {
    local host user path port key
    host=$(cfg plugins.remote-train.train_host "")
    user=$(cfg plugins.remote-train.train_user "")
    path=$(cfg plugins.remote-train.train_path "")
    port=$(cfg plugins.remote-train.train_port 22)
    key=$(cfg plugins.remote-train.ssh_key "$REMOTE_TRAIN_KEY_DEFAULT")
    echo "Remote training:"
    echo "  plugin: $(cfg plugins.remote-train.enabled false | grep -qx true && echo enabled || echo disabled)"
    echo "  algorithms: $(cfg plugins.remote-train.algorithms '[]')"
    if [ -z "$host" ] || [ -z "$user" ] || [ -z "$path" ]; then
        echo "  host: not configured (set train_host/train_user/train_path, then '$PROG remote-train setup')"
        return 0
    fi
    echo "  host: $user@$host:$path"
    if ! run_as_pi test -f "$key"; then
        echo "  passwordless SSH: no device key yet (run 'sudo $PROG remote-train setup')"
        return 0
    fi
    if run_as_pi ssh -i "$key" -p "$port" -o BatchMode=yes \
        -o StrictHostKeyChecking=accept-new -o ConnectTimeout=15 "$user@$host" true 2>/dev/null; then
        echo "  passwordless SSH: works"
    else
        echo "  passwordless SSH: not set up (run 'sudo $PROG remote-train setup')"
        return 0
    fi
    # Peek at the host's job markers so a stuck run is visible from the device.
    run_as_pi ssh -i "$key" -p "$port" -o BatchMode=yes \
        -o StrictHostKeyChecking=accept-new -o ConnectTimeout=15 "$user@$host" \
        "cd $(shell_quote "$path") 2>/dev/null || exit 0; \
         if [ -f .stack-ready ]; then echo '  host stack: ready'; \
         elif [ -f .stack-installing ]; then echo '  host stack: installing'; \
         elif [ -f .stack-error ]; then \
           echo \"  host stack: FAILED - \$(head -c 200 .stack-error)\"; \
         else echo '  host stack: not prepared yet'; fi; \
         for f in jobs/*.lock; do [ -e \"\$f\" ] || continue; \
           echo \"  running: \$(basename \"\$f\" .lock) (since epoch \$(cat \"\$f\"))\"; done; \
         for f in jobs/*.result; do [ -e \"\$f\" ] || continue; \
           echo \"  last run: \$(basename \"\$f\" .result) -> \$(cat \"\$f\")\"; done" \
        2>/dev/null || echo "  host state: unavailable"
    echo "  run progress also shows on the AI Agent dashboard card"
}

cmd_remote_train_menu() {
    have_whiptail || { cmd_remote_train_status; return 0; }
    while true; do
        local choice
        choice=$(whiptail --title "kestrel remote training" \
            --menu "Train the agent on a remote desktop host" 16 72 4 \
            status "Show the training host and job state" \
            setup "Install the device key on the training host (asks its password)" \
            now "Reconcile and train due models now" \
            quit "Back" 3>&1 1>&2 2>&3) || return 0
        case "$choice" in
            status) run_box "Remote training status" 20 76 cmd_remote_train_status ;;
            setup) ( cmd_remote_train_setup ) || true ;;
            now) run_box "Remote train now" 14 72 cmd_remote_train_now ;;
            quit) return 0 ;;
        esac
    done
}

cmd_remote_train() {
    [ $# -gt 0 ] || { cmd_remote_train_menu; return; }
    local sub=$1; shift
    case "$sub" in
        status) cmd_remote_train_status ;;
        setup) cmd_remote_train_setup ;;
        now) cmd_remote_train_now ;;
        *) die "unknown 'remote-train' subcommand: $sub" ;;
    esac
}

## update (in-place app-layer update) #####################################
PWN_UPDATE_STATE=/var/lib/kestrel/update/state.json

# Read one field from the updates plugin's state file ("" if missing). A JSON
# bool is normalised to the strings True/False so callers can test it.
update_state_get() {
    python3 -c 'import json,os,sys
p=sys.argv[1]; k=sys.argv[2]
d=json.load(open(p)) if os.path.exists(p) else {}
v=d.get(k, "")
print(("True" if v else "False") if isinstance(v, bool) else v)' "$1" "$2" 2>/dev/null || true
}

update_state_set() {
    python3 -c 'import json,os,sys
p=sys.argv[1]
d=json.load(open(p)) if os.path.exists(p) else {}
d["status"]=sys.argv[2]; d["detail"]=sys.argv[3]
os.makedirs(os.path.dirname(p), exist_ok=True)
open(p,"w").write(json.dumps(d))' "$1" "$2" "$3" 2>/dev/null || true
}

update_require_pi_or_root() { [ "$(id -un)" = pi ] || need_root; }

# Drop the "check now" trigger the updates plugin watches, so a running plugin
# re-checks on its next epoch instead of waiting out the interval (same idea as
# remote-train's trigger). Written as pi so the pi daemon can remove it after
# consuming it.
update_touch_check() {
    local dir; dir=$(dirname "$PWN_UPDATE_STATE")
    mkdir -p "$dir"
    : > "$dir/.check-now"
    chown pi:pi "$dir/.check-now" 2>/dev/null || true
}

cmd_update_check() {
    update_require_pi_or_root
    [ "$(cfg plugins.updates.enabled false)" = true ] \
        || echo "note: the updates plugin is not enabled; enable it to check." >&2
    update_touch_check
    echo "Asked the updates plugin to check for a new release now; it checks on"
    echo "its next epoch (within a minute, online). Re-run '$PROG update status',"
    echo "or watch the Updates dashboard card, for the result."
}

cmd_update_status() {
    if [ ! -f "$PWN_UPDATE_STATE" ]; then
        echo "No update check has run yet. Enable the updates plugin:"
        echo "  sudo $PROG plugins enable updates"
        return 0
    fi
    python3 -c 'import json,sys
d=json.load(open(sys.argv[1]))
print("installed:", d.get("installed") or "unknown")
print("latest:   ", d.get("latest") or "-")
if d.get("installed_is_dev"):
    st="dev build (not checked)"
elif d.get("update_available"):
    st="update available" + ("" if d.get("offerable") else " (needs reflash)")
else:
    st="up to date"
print("status:   ", st)
if d.get("update_available") and not d.get("offerable") and d.get("reason"):
    print("reason:   ", d.get("reason"))
if d.get("detail"):
    print("last run: ", d.get("detail"))' "$PWN_UPDATE_STATE"
}

# Apply an in-place app-layer update. Clones the target release, verifies its
# commit, then hands off (exec) to the clone's update-apply helper as root.
cmd_update_apply() {
    need_root
    local yes=no ref= force=no
    while [ $# -gt 0 ]; do
        case "$1" in
            --yes|-y) yes=yes; shift ;;
            --ref) ref=$2; shift 2 ;;
            --force) force=yes; shift ;;
            *) die "unknown option for 'update apply': $1" ;;
        esac
    done
    command -v git >/dev/null 2>&1 || die "git is required for in-place updates"
    local repo host tag commit offerable
    repo=$(update_state_get "$PWN_UPDATE_STATE" repo)
    host=$(update_state_get "$PWN_UPDATE_STATE" host)
    tag=$(update_state_get "$PWN_UPDATE_STATE" latest)
    commit=$(update_state_get "$PWN_UPDATE_STATE" commit)
    offerable=$(update_state_get "$PWN_UPDATE_STATE" offerable)
    [ -n "$repo" ] || repo=$(cfg plugins.updates.repo aluminum-ice/kestrel)
    [ -n "$host" ] || host=$(cfg plugins.updates.host https://git.3ntropy.io)
    host=${host%/}
    # The token below is sent to this host, so never over clear text.
    case "$host" in
        https://?*) ;;
        *) die "[plugins.updates] host must be an https:// URL (got '$host')" ;;
    esac
    local token; token=$(cfg plugins.updates.token "")
    if [ -n "$ref" ]; then
        tag=$ref; commit=
    elif [ "$force" != yes ]; then
        [ "$offerable" = True ] || die "no in-place update is available (status: $(update_state_get "$PWN_UPDATE_STATE" status); latest: ${tag:-unknown}). Let the updates plugin check, or pass --ref TAG."
    fi
    [ -n "$tag" ] || die "no target release known; enable the updates plugin or pass --ref TAG"
    if [ "$yes" != yes ]; then
        # Spell out exactly what the update does (the web confirm page matches).
        local bk_line="" bk_en bk_host bk_user bk_path bk_key
        bk_en=$(cfg plugins.backup.enabled false)
        bk_host=$(cfg plugins.backup.host "")
        bk_user=$(cfg plugins.backup.user "")
        bk_path=$(cfg plugins.backup.path "")
        bk_key=$(cfg plugins.backup.ssh_key /home/pi/.ssh/id_ed25519)
        if [ "$bk_en" = true ] && [ -n "$bk_host" ] && [ -n "$bk_user" ] \
            && [ -n "$bk_path" ] && run_as_pi test -f "$bk_key"; then
            bk_line="  - back up the latest capture data and model to $bk_user@$bk_host first (the update is cancelled if that backup fails)"
        fi
        echo "Update to $tag from $repo."
        echo "Before installing it will, in order:"
        echo "  - stop capture"
        echo "  - stop the learning agent (pausing training)"
        [ -n "$bk_line" ] && echo "$bk_line"
        echo "  - stop the plugin daemon"
        # Name the backup in the summary only when a pre-update backup runs.
        local net_subject="the download"
        [ -n "$bk_line" ] && net_subject="the download or backup"
        echo "then install $tag and restart whatever was running. Your settings and"
        echo "captured data are kept; if $net_subject is interrupted the update is"
        echo "cancelled and nothing on the device changes."
        # Soft single-link guardrail: a BT PAN link that fails to re-establish
        # after the update can leave the device unreachable, so warn when only one
        # management link is up and always point at the boot-folder Wi-Fi recovery.
        local up_count=0 only_ifc=""
        for ifc in bnep0 wlan0; do
            if ip -4 -o addr show dev "$ifc" 2>/dev/null | grep -q 'inet '; then
                up_count=$((up_count + 1)); only_ifc=$ifc
            fi
        done
        echo
        if [ "$up_count" -le 1 ]; then
            echo "WARNING: you are reachable on only one connection right now${only_ifc:+ ($only_ifc)}."
            echo "If it drops during or after the update you may lose access until it returns."
        else
            echo "Note: if your only link is Bluetooth PAN, a dropped connection during or"
            echo "after the update can leave the device unreachable until it re-establishes."
        fi
        echo "Recovery without reflashing: stage a Wi-Fi network on the boot partition"
        echo "(a kestrel-wifi.txt file) and reboot; the device joins Wi-Fi and the"
        echo "dashboard and SSH come back. A Wi-Fi fallback set up beforehand avoids this."
        printf 'Continue? [y/N] '
        local ans; read -r ans || ans=
        case "$ans" in y|Y|yes|YES) ;; *) die "aborted" ;; esac
    fi
    local tmp; tmp=$(mktemp -d "${TMPDIR:-/tmp}/pwn-update.XXXXXX") || die "could not make a temp dir"
    trap 'rm -rf "$tmp"' EXIT INT TERM
    log "fetching $repo @ $tag from $host ..."
    # For a private repo, a token (from [plugins.updates] token) authenticates
    # the clone. Pass it as a per-invocation Basic auth header (Forgejo takes
    # the token as the password) scoped to $host, rather than in the URL, so it
    # never lands in the clone's .git/config; the temp dir is deleted either
    # way. No token => an ordinary anonymous clone of a public repo.
    local -a gitauth=()
    if [ -n "$token" ]; then
        local b64; b64=$(printf 'x-access-token:%s' "$token" | base64 | tr -d '\n')
        gitauth=(-c "http.$host/.extraheader=Authorization: Basic $b64")
    fi
    # Clone with a few retries and backoff: the device is often on a flaky
    # BT-PAN/Wi-Fi tether, and a dropped clone must not abort the update. The
    # partial clone dir is removed before each retry (git refuses a non-empty
    # target). This is the only network step before anything on the device is
    # touched, so a clone that never succeeds simply leaves the device unchanged.
    local attempt=1 delay=2 cloned=no
    while [ "$attempt" -le 4 ]; do
        rm -rf "$tmp/src"
        if git "${gitauth[@]}" clone --depth 1 --branch "$tag" "$host/$repo.git" "$tmp/src" >/dev/null 2>&1; then
            cloned=yes
            break
        fi
        if [ "$attempt" -lt 4 ]; then
            log "clone attempt $attempt failed; retrying in ${delay}s ..."
            sleep "$delay"
            delay=$((delay * 2))
        fi
        attempt=$((attempt + 1))
    done
    if [ "$cloned" != yes ]; then
        update_state_set "$PWN_UPDATE_STATE" error "could not fetch release $tag"
        die "could not fetch release $tag from $repo (check connectivity; a private repo needs a valid [plugins.updates] token)"
    fi
    if [ -n "$commit" ]; then
        local got; got=$(git -C "$tmp/src" rev-parse HEAD 2>/dev/null || echo "")
        if [ "$got" != "$commit" ]; then
            update_state_set "$PWN_UPDATE_STATE" error "commit mismatch for $tag"
            die "release $tag commit mismatch (expected $commit, got ${got:-none}); aborting"
        fi
    fi
    local helper="$tmp/src/builder/extra/files/update-apply.py"
    [ -f "$helper" ] || die "the fetched release has no update helper; it predates in-place updates (reflash instead)"
    log "applying $tag; the daemon will restart shortly ..."
    # Hand off FROM THE CLONE so overwriting the installed files (this CLI
    # included) is safe; exec replaces this shell so nothing keeps reading the
    # file we are about to overwrite. The helper removes $tmp when done.
    trap - EXIT INT TERM
    exec python3 -I "$helper" --src "$tmp/src" --tag "$tag" --repo "$repo" \
        --state "$PWN_UPDATE_STATE" --cleanup "$tmp"
}

cmd_update() {
    [ $# -gt 0 ] || { cmd_update_status; return; }
    local sub=$1; shift
    case "$sub" in
        status) cmd_update_status ;;
        check) cmd_update_check ;;
        apply) cmd_update_apply "$@" ;;
        *) die "unknown 'update' subcommand: $sub" ;;
    esac
}

## dispatch ################################################################
cmd_network() {
    [ $# -gt 0 ] || { cmd_network_menu; return; }
    local sub=$1; shift
    case "$sub" in
        status) cmd_status ;;
        wifi) cmd_wifi "$@" ;;
        bt-pair) cmd_bt_pair ;;
        *) die "unknown 'network' subcommand: $sub" ;;
    esac
}

# Guided entry point the MOTD points to: connectivity and capture in one menu.
cmd_setup() {
    if ! have_whiptail; then
        cmd_status
        echo
        cmd_capture_status
        echo
        echo "Run in a terminal for the guided menu, or use the flags ($PROG help)."
        return 0
    fi
    while true; do
        local choice
        choice=$(whiptail --title "kestrel setup" --menu "What would you like to set up?" 19 66 8 \
            network "Connectivity (Wi-Fi station, Bluetooth)" \
            capture "WiFi capture (AngryOxide)" \
            plugins "Plugins (enable/disable features)" \
            web "Web page (login, plugin controls)" \
            backup "Remote backup of capture data" \
            agent "Learning capture agent" \
            remote-train "Remote training on a desktop host" \
            status "Show status" \
            quit "Exit" 3>&1 1>&2 2>&3) || return 0
        case "$choice" in
            network) cmd_network_menu ;;
            capture) cmd_capture_menu ;;
            plugins) cmd_plugins_menu ;;
            web) cmd_web_menu ;;
            backup) cmd_backup_menu ;;
            agent) cmd_agent_menu ;;
            remote-train) cmd_remote_train_menu ;;
            status) run_box "Status" 24 74 cmd_status_all ;;
            quit) return 0 ;;
        esac
    done
}

main() {
    local cmd=${1:-setup}
    [ $# -gt 0 ] && shift || true
    case "$cmd" in
        setup) cmd_setup "$@" ;;
        network) cmd_network "$@" ;;
        capture) cmd_capture "$@" ;;
        plugins) cmd_plugins "$@" ;;
        web) cmd_web "$@" ;;
        backup) cmd_backup "$@" ;;
        agent) cmd_agent "$@" ;;
        remote-train) cmd_remote_train "$@" ;;
        update) cmd_update "$@" ;;
        status) cmd_status_all ;;
        help|-h|--help) usage ;;
        *) usage; exit 1 ;;
    esac
}

main "$@"
