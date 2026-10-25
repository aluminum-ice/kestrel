#!/bin/bash
# Bluetooth onboarding window: make the adapter discoverable and pairable with
# a Just Works agent until the first phone pairs or BT_ONBOARD_MINUTES pass.
# The first phone to pair is trusted and recorded; then the window closes and
# the BT PAN service takes over. Runs from kestrel-bt-onboard.service on
# every boot until a phone is recorded (the unit's ConditionPathExists).
#
# The controller attaches over UART a few seconds into boot and BlueZ settles a
# little after that, so each setup step is retried and the window is re-asserted
# across its lifetime: a transient failure at boot must not leave the adapter
# invisible until someone runs 'kestrel network bt-pair' by hand.
set -euo pipefail
# shellcheck source=common.sh
. "${PWN_LIB_DIR:-/usr/local/lib/kestrel}/common"

# Must be at least 1: BlueZ treats a discoverable timeout of 0 as "forever".
[[ $BT_ONBOARD_MINUTES =~ ^[1-9][0-9]*$ ]] ||
    die "BT_ONBOARD_MINUTES must be a whole number of minutes, at least 1 (got '$BT_ONBOARD_MINUTES')"

if [ -n "$(bt_phone)" ]; then
    log "a phone is already recorded; run 'sudo kestrel network bt-pair' to pair a new one"
    exit 0
fi

# A non-zero exit here (controller not attached within the wait) is caught by
# the unit's Restart=on-failure, which retries the whole run a few times so a
# slow UART attach at boot reopens the window instead of giving up until reboot.
rfkill unblock bluetooth 2>/dev/null || true
bt_wait_for_adapter 90 || die "no Bluetooth controller found"
bt_power_on 30 || die "Bluetooth controller would not power on"

adapter=$(bt_adapter_mac)
alias="kestrel-${adapter:12:2}${adapter:15:2}"
bluetoothctl system-alias "$alias" >/dev/null 2>&1 || true

window=$((BT_ONBOARD_MINUTES * 60))
before=$(bt_paired_macs)

agent=
start_agent() {
    "$PWN_LIB_DIR/bt-agent" &
    agent=$!
}
cleanup() {
    bt_close_window
    if [ -n "$agent" ]; then
        kill "$agent" 2>/dev/null || true
        wait "$agent" 2>/dev/null || true
    fi
}
trap cleanup EXIT
start_agent
sleep 2  # let the agent register before pairing can start

bluetoothctl discoverable-timeout "$window" >/dev/null 2>&1 ||
    log "warning: couldn't set discoverable-timeout; BlueZ may stop advertising before the window ends"
bt_open_window
log "onboarding window open for $BT_ONBOARD_MINUTES min: pair your phone with \"$alias\" (turn on Bluetooth tethering first)"

phone=
agent_restarts=0
# Time the window with a monotonic clock, not bash's $SECONDS (which tracks the
# wall clock): an RTC-less Pi boots with a stale clock and NTP can jump it by
# months mid-window, and a $SECONDS deadline then fires the instant the clock is
# set, closing the window seconds after it opened with no phone paired. BlueZ's
# own discoverable-timeout is already monotonic, so this keeps the two in step.
started=$(mono_seconds)
while [ "$(( $(mono_seconds) - started ))" -lt "$window" ] && [ -z "$phone" ]; do
    # The agent must stay up for Just Works pairing. If it drops (a boot race
    # with dbus/bluetoothd can make RegisterAgent fail), restart it a few times
    # rather than abandon the window.
    if ! kill -0 "$agent" 2>/dev/null; then
        [ "$agent_restarts" -lt 5 ] || die "pairing agent keeps exiting"
        agent_restarts=$((agent_restarts + 1))
        log "pairing agent exited; restarting it ($agent_restarts)"
        start_agent
        sleep 2
    fi
    # Keep advertising for the whole window: re-assert if BlueZ dropped
    # discoverable/pairable, which it can do just after the controller powers on.
    bt_window_is_open || bt_open_window
    for mac in $(bt_paired_macs); do
        if ! grep -qxF "$mac" <<<"$before"; then
            phone=$mac
            break
        fi
    done
    [ -n "$phone" ] || sleep 2
done

if [ -z "$phone" ]; then
    log "onboarding window closed with no phone paired"
    exit 0
fi

bluetoothctl trust "$phone" >/dev/null 2>&1 || true
bt_record_phone "$phone"
log "paired and trusted $phone; onboarding window closed"
systemctl --no-block start kestrel-bt-pan.service
