#!/bin/bash
# Keep a Bluetooth PAN link to the recorded phone (phone = NAP, Pi = PANU).
# Brings up bnep0 by DHCP; if DHCP yields nothing, falls back to static
# addresses for the iPhone and Android tethering subnets. Retries every
# BT_PAN_RETRY_SECONDS, so the link comes back after a reboot or when the phone
# turns tethering back on. Runs from kestrel-bt-pan.service.
set -euo pipefail
# shellcheck source=common.sh
. "${PWN_LIB_DIR:-/usr/local/lib/kestrel}/common"

for var in BT_PAN_DHCP_TIMEOUT BT_PAN_RETRY_SECONDS; do
    [[ ${!var} =~ ^[1-9][0-9]*$ ]] || die "$var must be a positive whole number of seconds (got '${!var}')"
done
[[ ${BT_PAN_RESET_AFTER:-4} =~ ^[1-9][0-9]*$ ]] || die "BT_PAN_RESET_AFTER must be a positive whole number (got '${BT_PAN_RESET_AFTER}')"

phone=$(bt_phone)
[ -n "$phone" ] || die "no phone recorded; run 'sudo kestrel network bt-pair'"

rfkill unblock bluetooth 2>/dev/null || true
bt_wait_for_adapter 90 || die "no Bluetooth controller found"
bluetoothctl power on >/dev/null
bt_close_window

# (Re)create the profiles if missing or pointing at another phone; otherwise
# keep an existing profile's settings (e.g. the fallback DNS below) in sync via
# a plain modify, which does not tear the link down.
ensure_profile() {
    local name=$1; shift
    if nm_has_con "$name"; then
        if [ "$(nmcli -g bluetooth.bdaddr connection show "$name" 2>/dev/null | tr a-f A-F)" = "$phone" ]; then
            [ "$#" -gt 0 ] && nmcli connection modify "$name" "$@" >/dev/null
            return 0
        fi
        nmcli connection delete "$name" >/dev/null
    fi
    nmcli connection add type bluetooth con-name "$name" bluetooth.bdaddr "$phone" bluetooth.type panu \
        connection.autoconnect no "$@" >/dev/null
}

# A tethering phone often routes the Pi over IPv6 while the only resolver it
# advertises is a link-local IPv6 address glibc cannot use. BT_PAN_FALLBACK_DNS
# gives both profiles a routable resolver and drops the auto IPv6 DNS so lookups
# succeed over the phone's IPv6. IPv4 entries go on the DHCP profile only: the
# static fallback has no IPv4 gateway, so an IPv4 resolver there is unreachable
# and would only add lookup latency.
dns_v4=""
dns_v6=""
if [ -n "${BT_PAN_FALLBACK_DNS:-}" ]; then
    IFS=',' read -ra fallback_dns <<<"$BT_PAN_FALLBACK_DNS"
    for d in "${fallback_dns[@]}"; do
        d=${d//[[:space:]]/}
        [ -n "$d" ] || continue
        case "$d" in
            *:*) dns_v6="${dns_v6:+$dns_v6 }$d" ;;
            *)   dns_v4="${dns_v4:+$dns_v4 }$d" ;;
        esac
    done
fi
dhcp_dns=()
static_dns=()
if [ -n "$dns_v6" ]; then
    dhcp_dns+=(ipv6.dns "$dns_v6" ipv6.ignore-auto-dns yes)
    static_dns+=(ipv6.dns "$dns_v6" ipv6.ignore-auto-dns yes)
fi
[ -n "$dns_v4" ] && dhcp_dns+=(ipv4.dns "$dns_v4")

# ipv4.may-fail no: an IPv6-only link must not count as up, or the static
# IPv4 fallback would never be tried.
ensure_profile "$PWN_BT_PAN_CON" ipv4.method auto ipv4.may-fail no ipv4.dhcp-timeout "$BT_PAN_DHCP_TIMEOUT" ${dhcp_dns[@]+"${dhcp_dns[@]}"}
ensure_profile "$PWN_BT_PAN_STATIC_CON" ipv4.method manual ipv4.addresses "$BT_PAN_STATIC_ADDRESSES" ${static_dns[@]+"${static_dns[@]}"}

# Drop any leftover addresses from a previous lease before a (re)connect, so
# bnep0 never accumulates a stale DHCP address alongside the static fallback
# (which left it with an address but no gateway). NetworkManager re-adds the
# active connection's own address when it brings the link up.
flush_bnep0_stale() {
    ip addr flush dev bnep0 2>/dev/null || true
}

# Best-effort nudge before NetworkManager activates the PANU profile: ask the
# recorded phone's NAP to wake. A bare `nmcli connection up` often does not
# prompt the phone to offer the PAN, but a Pi-initiated connect to the phone
# does. Idempotent (a no-op when already connected), failure ignored, and
# time-boxed so a wedged controller can never hang the loop.
bt_prod_nap() {
    timeout 15 bluetoothctl connect "$phone" >/dev/null 2>&1 || true
}

# Holds NetworkManager's reason from the last failed activation, so the loop can
# say *why* bnep0 would not come up (e.g. 'bluetooth-failed') instead of leaving
# the operator to guess from the journal.
last_nm_reason=""

bring_up_dhcp() {
    bt_prod_nap
    local err
    if err=$(nmcli --wait $((BT_PAN_DHCP_TIMEOUT + 15)) connection up "$PWN_BT_PAN_CON" 2>&1 >/dev/null); then
        last_nm_reason=""
        return 0
    fi
    last_nm_reason=$(printf '%s\n' "$err" | sed -n 's/^Error: //p' | head -n1)
    return 1
}

# Whether the experimental Bluetooth-stack reset is enabled (default no).
bt_stack_reset_enabled() {
    case "${BT_PAN_STACK_RESET:-no}" in
        1 | y | Y | yes | Yes | YES | true | True | TRUE) return 0 ;;
        *) return 1 ;;
    esac
}

# DANGER / UNTESTED: a last-ditch reset of the Bluetooth host controller for the
# case where it is wedged and every connect returns 'bluetooth-failed'. OFF by
# default (BT_PAN_STACK_RESET=no) and NOT yet validated on hardware. The risks,
# all real:
#   * It power-cycles the controller, so running it while a link is up or coming
#     up would DROP that link. The caller gates it to the all-down branch, after
#     BT_PAN_RESET_AFTER consecutive failed reconnects, and never while a link is
#     active or the onboarding window is open - but that gating is not yet
#     hardware-proven.
#   * It only clears a wedged *controller*; it does nothing for the common real
#     causes (the phone stopped offering PAN, or the pairing bond was lost), so
#     it is rate-limited to one reset per streak rather than looping on it.
#   * Bluetooth stack timing is finicky, so the reconnect is left to the next
#     loop iteration once the adapter has re-attached.
# To bench-test: set BT_PAN_STACK_RESET=yes in /etc/default/kestrel,
# induce a bluetooth-failed loop, and confirm it recovers AND never disturbs a
# healthy link or the onboarding window.
bt_stack_reset() {
    log "BT PAN: resetting the Bluetooth controller after $fail_streak consecutive failures (BT_PAN_STACK_RESET=yes; experimental)"
    bluetoothctl power off >/dev/null 2>&1 || true
    rfkill unblock bluetooth 2>/dev/null || true
    sleep 2
    bt_wait_for_adapter 30 || true
    bt_power_on 30 || true
}

log "keeping a BT PAN link to $phone"
state=down
# Consecutive failed reconnects where bnep0 would not come up at all. Reset to 0
# whenever the link is up (on either profile), so it only grows while the link
# is genuinely absent - the one situation the optional stack reset acts on.
fail_streak=0
while true; do
    # A real DHCP lease is the good state (gateway + the phone's own resolver).
    if nm_con_active "$PWN_BT_PAN_CON"; then
        state=dhcp
        fail_streak=0
        sleep "$BT_PAN_RETRY_SECONDS"
        continue
    fi
    # On the static fallback the link is up but has NO gateway and no reachable
    # DNS, so it cannot actually reach the internet. Don't sit on it: keep
    # trying to upgrade to a real DHCP lease every cycle (the phone's DHCP is
    # usually just slow after it has been idle, not absent). Take the static
    # down first so the lease isn't blocked, and restore it if DHCP still fails
    # so the link is never left with nothing.
    if nm_con_active "$PWN_BT_PAN_STATIC_CON"; then
        # The link is physically up (bnep0 has an address), so the controller is
        # not wedged: clear the streak regardless of the DHCP-upgrade outcome.
        fail_streak=0
        nmcli connection down "$PWN_BT_PAN_STATIC_CON" >/dev/null 2>&1 || true
        flush_bnep0_stale
        if bring_up_dhcp; then
            state=dhcp
            log "bnep0 upgraded from the static fallback to DHCP"
        else
            nmcli --wait 30 connection up "$PWN_BT_PAN_STATIC_CON" >/dev/null 2>&1 || true
            state=static
        fi
        sleep "$BT_PAN_RETRY_SECONDS"
        continue
    fi
    # Neither connection is up: (re)connect from a clean slate, prodding the
    # phone's NAP first (inside bring_up_dhcp).
    [ "$state" = down ] || log "BT PAN link lost; reconnecting"
    state=down
    flush_bnep0_stale
    if bring_up_dhcp; then
        state=dhcp
        fail_streak=0
        log "bnep0 up by DHCP"
    elif nmcli --wait 30 connection up "$PWN_BT_PAN_STATIC_CON" >/dev/null 2>&1; then
        state=static
        fail_streak=0
        log "bnep0 up with static fallback ($BT_PAN_STATIC_ADDRESSES); DHCP gave no address — will keep retrying DHCP"
    else
        fail_streak=$((fail_streak + 1))
        log "BT PAN: bnep0 did not come up (attempt $fail_streak)${last_nm_reason:+: $last_nm_reason}"
        # Optional, off by default: reset a wedged controller after a streak of
        # failures, but never while the onboarding window / service is active
        # (it owns the adapter) - see bt_stack_reset's warning.
        if bt_stack_reset_enabled && [ "$fail_streak" -ge "${BT_PAN_RESET_AFTER:-4}" ] &&
            ! systemctl is-active --quiet kestrel-bt-onboard.service 2>/dev/null &&
            ! bt_window_is_open; then
            bt_stack_reset
            fail_streak=0
        fi
    fi
    sleep "$BT_PAN_RETRY_SECONDS"
done
