#!/bin/bash
# kestrel capture launcher: the ExecStart for kestrel-capture.service.
# Reads /etc/kestrel/config.toml, resolves the capture interface, keeps
# NetworkManager off it, starts the hashline collector, and runs AngryOxide on
# the external USB dongle with the configured scan profile.
#
# Exits 0 (not a failure) when capture is disabled or no supported adapter is
# present, so the unit never crash-loops on a stale enable or an unplugged
# dongle. While capture IS enabled it supervises AngryOxide: if AngryOxide
# exits for any reason other than a deliberate stop -- it self-terminates, or
# the USB dongle browns out / drops and re-enumerates (seen as a clean exit) --
# the launcher re-resolves the interface and relaunches it, so the session
# self-heals instead of staying dead until someone notices. A backoff and a
# fast-failure cap keep a persistent fault (gone dongle, bad config) from
# becoming a hot loop. See docs/developer/capture-interface.md.
set -euo pipefail
# shellcheck source=common.sh
. "${PWN_LIB_DIR:-/usr/local/lib/kestrel}/common"

ANGRYOXIDE=${ANGRYOXIDE:-/usr/bin/angryoxide}
COLLECT=${PWN_CAPTURE_COLLECT:-$PWN_LIB_DIR/capture-collect}
iface=
collector_pid=
ao_pid=
stopping=no

cleanup() {
    [ -n "$collector_pid" ] && kill "$collector_pid" 2>/dev/null || true
    # Hand the dongle back to NetworkManager.
    [ -n "$iface" ] && nmcli device set "$iface" managed yes >/dev/null 2>&1 || true
}
trap cleanup EXIT

# Interruptible sleep: a stop signal lands promptly instead of after the full
# interval (a backgrounded sleep that `wait` can be interrupted out of).
nap() {
    sleep "$1" &
    wait "$!" 2>/dev/null || true
}

# 1. Enabled?
if [ "$(cfg capture.enabled false)" != true ]; then
    log "capture is disabled in $PWN_CONFIG; nothing to do"
    exit 0
fi

want_iface=$(cfg capture.interface auto)
iface_wait=${PWN_CAPTURE_IFACE_WAIT:-60}

# Resolve the capture interface, waiting a bounded time for the USB dongle to
# (re-)enumerate -- at cold boot it can appear a few seconds after this service
# starts, and after an over-current/drop it re-appears a moment later. Sets
# $iface and returns 0, or returns 1 if none showed up within the wait (or a
# stop was requested).
resolve_iface() {
    local deadline
    deadline=$(( $(date +%s) + iface_wait ))
    while :; do
        if iface=$(capture_resolve_iface "$want_iface"); then
            return 0
        fi
        iface=
        [ "$stopping" = yes ] && return 1
        [ "$(date +%s)" -ge "$deadline" ] && return 1
        nap 3
        [ "$stopping" = yes ] && return 1
    done
}

# True while the Phase 4 learning agent is enabled. When it is, the agent writes
# its chosen scan plan to $PWN_AGENT_OVERRIDE each decision window and the
# launcher builds AngryOxide's scan flags from that instead of [capture.scan];
# disabling the agent (or a missing override) falls straight back to the
# configured sweep, so the agent never has to touch config.toml.
agent_active() {
    [ "$(cfg agent.enabled false)" = true ] && [ -f "$PWN_AGENT_OVERRIDE" ]
}

# A fingerprint of the agent override's content ("none" if absent or not a
# small regular file), so the supervise loop relaunches AngryOxide only when the
# agent's plan actually changes - every relaunch restarts the scan. The file is
# pi's, so root opens it without following a symlink and without blocking on a
# FIFO, and reads only a small regular file.
agent_override_sum() {
    PWN_AGENT_OVERRIDE="$PWN_AGENT_OVERRIDE" python3 -I - <<'PY' 2>/dev/null || echo none
import hashlib, os, stat
data = None
try:
    fd = os.open(os.environ["PWN_AGENT_OVERRIDE"], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        st = os.fstat(fd)
        if stat.S_ISREG(st.st_mode) and st.st_size <= 65536:
            data = os.read(fd, 65537)
    finally:
        os.close(fd)
except OSError:
    pass
print(hashlib.sha256(data).hexdigest() if data is not None else "none")
PY
}

# Read the agent override JSON into the scan flags in the global `args`, and set
# `dwell_sec` from it. Returns non-zero (leaving args untouched) if the file is
# missing or unreadable, so the caller falls back to the configured sweep.
build_agent_args() {
    local parsed kind band channels transmit dwell
    parsed=$(PWN_AGENT_OVERRIDE="$PWN_AGENT_OVERRIDE" python3 -I - <<'PY' 2>/dev/null
import json, os, stat
# pi's file: no symlink, no FIFO, only a small regular file (see agent_override_sum).
try:
    fd = os.open(os.environ["PWN_AGENT_OVERRIDE"], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_size > 65536:
            raise SystemExit(1)
        o = json.loads(os.read(fd, 65537))
    finally:
        os.close(fd)
except SystemExit:
    raise
except Exception:
    raise SystemExit(1)
kind = str(o.get("kind", ""))
band = str(o.get("band", ""))
chans = o.get("channels") or []
chans = ",".join(str(int(c)) for c in chans) if isinstance(chans, list) else ""
transmit = str(o.get("transmit", "active"))
try:
    dwell = int(o.get("dwell", 5))
except Exception:
    dwell = 5
print("\t".join([kind, band, chans, transmit, str(dwell)]))
PY
    ) || return 1
    IFS=$'\t' read -r kind band channels transmit dwell <<<"$parsed"
    [[ $dwell =~ ^[0-9]+$ ]] && [ "$dwell" -ge 1 ] || dwell=5
    dwell_sec=$dwell
    case "$kind" in
        autohunt)
            args+=(--autohunt) ;;
        channels)
            if [[ $channels =~ ^[0-9]+(,[0-9]+)*$ ]]; then
                args+=(--channel "$channels")
            else
                log "agent override: empty channel list; sweeping all channels"
                local chans
                chans=$(capture_auto_channels "$iface" || true)
                [ -n "$chans" ] && args+=(--channel "$chans")
            fi ;;
        band|*)
            if [[ $band =~ ^(2|5|6|60)$ ]]; then
                args+=(--band "$band")
            else
                # "both"/unknown: sweep every channel the adapter supports.
                local chans
                chans=$(capture_auto_channels "$iface" || true)
                [ -n "$chans" ] && args+=(--channel "$chans")
            fi ;;
    esac
    [ "$transmit" = passive ] && args+=(--notransmit)
    log "agent scan plan: kind=$kind band=${band:-} channels=${channels:-} dwell=${dwell}s transmit=$transmit"
    return 0
}

# Build the AngryOxide argv for the current $iface into the global `args`. Rerun
# on every (re)launch so a dongle that came back as a different name, a config
# edit, or a new agent scan plan is picked up. dwell_seconds goes straight to
# --dwell (seconds).
build_args() {
    local dwell_sec mode bands chans extra
    dwell_sec=$(cfg capture.scan.dwell_seconds 5)
    [[ $dwell_sec =~ ^[0-9]+$ ]] && [ "$dwell_sec" -ge 1 ] || dwell_sec=5

    args=(--interface "$iface"
        --output "$PWN_CAPTURE_RUN_DIR/$ao_base" --notar)

    # The learning agent drives the scan when enabled; otherwise the configured
    # [capture.scan] sweep/autohunt/fixed profile does (the Phase 2 behaviour).
    if agent_active && build_agent_args; then
        :
    else
    mode=$(cfg capture.scan.mode sweep)
    bands=$(cfg capture.scan.bands auto)
    case "$mode" in
        autohunt)
            args+=(--autohunt)
            ;;
        fixed)
            if [[ $bands =~ ^[0-9]+(,[0-9]+)*$ ]]; then
                args+=(--channel "$bands")
            else
                log "capture.scan.bands is not a channel list for mode=fixed; using AngryOxide defaults"
            fi
            ;;
        sweep | *)
            [ "$mode" = sweep ] || log "unknown capture.scan.mode '$mode'; using sweep"
            if [[ $bands =~ ^(2|5|6|60)$ ]]; then
                args+=(--band "$bands")
            elif [[ $bands =~ ^[0-9]+(,[0-9]+)*$ ]]; then
                args+=(--channel "$bands")
            else
                # auto: sweep every channel the adapter supports.
                chans=$(capture_auto_channels "$iface" || true)
                if [ -n "$chans" ]; then
                    args+=(--channel "$chans")
                else
                    log "could not enumerate channels; using AngryOxide's default set"
                fi
            fi
            ;;
    esac
    fi

    # --dwell is set last so an agent override's dwell (in dwell_sec) wins.
    args+=(--dwell "$dwell_sec")

    [ "$(cfg capture.output.mode per-ap)" = single ] && args+=(--combine)
    # AngryOxide's TUI needs a controlling terminal. The systemd service has none,
    # so headless=false there makes AngryOxide fail to start and the unit dies.
    # Honour headless=false only when run interactively (a TTY on stdin+stdout);
    # otherwise force --headless so capture keeps running, and say how to get the TUI.
    if [ "$(cfg capture.angryoxide.headless true)" = true ]; then
        args+=(--headless)
    elif ! { [ -t 0 ] && [ -t 1 ]; }; then
        log "headless=false ignored: no terminal (running as a service), so capture runs headless. Run $0 in a terminal to use the TUI."
        args+=(--headless)
    fi
    [ -s "$PWN_WHITELIST_FILE" ] && args+=(--whitelist "$PWN_WHITELIST_FILE")

    # Extra pass-through flags, one per line.
    while IFS= read -r extra; do
        [ -n "$extra" ] && args+=("$extra")
    done < <("$PWN_CONFIG_GET" capture.angryoxide.extra_args "")
}

# On stop, break the supervisor loop and let the EXIT trap stop the collector
# and hand the dongle back to NetworkManager.
trap 'stopping=yes; if [ -n "${ao_pid:-}" ]; then kill -TERM "$ao_pid" 2>/dev/null || true; fi' INT TERM

# Prepare directories and the whitelist file from config (once; the collector
# and whitelist persist across AngryOxide relaunches). The run dir is root's
# (AngryOxide writes it as root) and pi may only read it, so nothing pi plants
# there can redirect a root write; install -d also fixes an older dir's mode.
mkdir -p "$PWN_CAPTURE_DIR"
install -d -o root -g pi -m 0750 "$PWN_CAPTURE_RUN_DIR"
"$PWN_CONFIG_GET" capture.whitelist "" | sed '/^$/d' > "$PWN_WHITELIST_FILE"

# An older release ran the collector as root, so files it left in the store are
# root's and the pi collector below can't append to them. Hand them to pi first
# (descriptor-based and symlink-safe; see adopt_store in capture-collect). A
# no-op once done; a failure here must not stop capture.
PWN_CAPTURE_DIR="$PWN_CAPTURE_DIR" python3 -I "$COLLECT" --adopt-store pi || true

# Start the collector that merges run-dir hashlines into the append-only store.
# It runs as pi: it only reads the run dir and writes the pi-owned store, so a
# symlink planted in the store can't make it write anywhere as root. setpriv
# execs in place, so $collector_pid is the collector itself (for the trap).
PWN_CAPTURE_RUN_DIR="$PWN_CAPTURE_RUN_DIR" PWN_CAPTURE_DIR="$PWN_CAPTURE_DIR" \
    PWN_CONFIG="$PWN_CONFIG" \
    setpriv --reuid=pi --regid=pi --init-groups -- "$COLLECT" &
collector_pid=$!

# Bound the run dir so a long session can't fill the card. AngryOxide's pcapng
# grows for the whole session and the kismet DB grows with every frame it sees;
# these raw working files, not the tiny hashline store, are what exhausts the
# microSD. When the run dir reaches max_run_mb, roll the capture: stop
# AngryOxide, delete the raw files (pcapng/kismet/tars/logs) -- never the
# *.hc22000 the collector drains into the store -- and start fresh. 0 disables
# the cap (unbounded; keeps every packet capture but may fill the card).
max_run_mb=$(cfg capture.housekeeping.max_run_mb 512)
[[ $max_run_mb =~ ^[0-9]+$ ]] || max_run_mb=512
max_run_bytes=$(( max_run_mb * 1024 * 1024 ))
run_check_seconds=${PWN_CAPTURE_RUN_CHECK:-30}

# Relaunch controls: an AngryOxide that stays up at least min_healthy_seconds
# before exiting is treated as a transient fault and relaunched at once; one
# that keeps dying faster than that is backed off, and after max_fast_failures
# in a row the launcher gives up (exit 1) so systemd's Restart=on-failure, not a
# tight in-script loop, owns the retry.
relaunch_backoff=${PWN_CAPTURE_RELAUNCH_BACKOFF:-5}
min_healthy_seconds=${PWN_CAPTURE_MIN_HEALTHY:-60}
max_fast_failures=${PWN_CAPTURE_MAX_FAST_FAILURES:-5}
fast_failures=0

# Remove raw artifacts left by earlier runs (one pcapng per previous start, the
# kismet DB, tars, logs). Their hashlines are already in the store; the per-AP
# *.hc22000 here are left for the collector to drain. With a launch's output
# base as $1, that launch's files are kept: a relaunch for a new agent plan
# keeps the previous run's pcapng so the collector's in-range reading has
# recent frames while the new one fills (the size cap still bounds them all).
clear_raw_artifacts() {
    if [ -n "${1:-}" ]; then
        find "$PWN_CAPTURE_RUN_DIR" -maxdepth 1 -type f ! -name '*.hc22000' ! -name "$1*" \
            -delete 2>/dev/null || true
    else
        find "$PWN_CAPTURE_RUN_DIR" -maxdepth 1 -type f ! -name '*.hc22000' -delete 2>/dev/null || true
    fi
}

# Each launch writes its pcapng / kismet DB under its own output base, so a
# relaunch never reuses (or truncates) the previous run's files. The sequence
# is zero-padded, so no base is a prefix of another.
launch_seq=0
ao_base=
prev_base=
replan=no

# Total bytes of the raw working files -- everything a roll would delete, i.e.
# not the *.hc22000 the collector keeps. The cap bounds this, not the whole run
# dir, so a roll always brings it back under the cap (and the kept hashlines
# never count toward it or trigger a tight re-roll).
raw_run_bytes() {
    find "$PWN_CAPTURE_RUN_DIR" -maxdepth 1 -type f ! -name '*.hc22000' -printf '%s\n' 2>/dev/null \
        | awk '{ s += $1 } END { print s + 0 }'
}

# Supervise AngryOxide. Each iteration (re)resolves the interface, keeps
# NetworkManager off it, and runs AngryOxide from the run dir. A clean stop
# stays stopped; a roll restarts immediately; any other exit relaunches (the
# self-healing that turns last night's over-current dongle drop from a dead
# service into a few-second gap).
while :; do
    if ! resolve_iface; then
        [ "$stopping" = yes ] && break
        log "no monitor-capable capture adapter found after ${iface_wait}s; is the USB dongle plugged in?"
        exit 0
    fi
    log "capture interface: $iface"

    # Keep NetworkManager off the dongle for the capture's lifetime so it does
    # not contend with management (BT PAN / built-in Wi-Fi). Re-applied every
    # relaunch because NM may have re-grabbed the dongle when it re-enumerated.
    nmcli device set "$iface" managed no >/dev/null 2>&1 || true

    prev_base=$ao_base
    launch_seq=$(( launch_seq + 1 ))
    ao_base=$(printf 'kestrel-%s-%06d' "$(date +%s)" "$launch_seq")
    build_args

    # AngryOxide writes the per-AP hc22000 files (<essid>_<bssid>.hc22000) into
    # its current working directory; --output only sets the pcapng / kismet
    # base. Without this cd they land in the service's CWD (/) where the
    # collector never sees them. Running here puts them in the run dir the
    # collector watches. Clear stale raw first (hashlines are kept).
    cd "$PWN_CAPTURE_RUN_DIR"
    if [ "$replan" = yes ] && [ -n "$prev_base" ]; then
        clear_raw_artifacts "$prev_base"
    else
        clear_raw_artifacts
    fi

    log "starting: $ANGRYOXIDE ${args[*]}"
    ao_started=$(date +%s)
    ao_override_sum=$(agent_override_sum)
    "$ANGRYOXIDE" "${args[@]}" &
    ao_pid=$!
    rolling=no
    replan=no
    while kill -0 "$ao_pid" 2>/dev/null; do
        nap "$run_check_seconds"
        [ "$stopping" = yes ] && break
        # The agent picked a new scan plan: relaunch AngryOxide with it. The
        # extracted hashlines are already in the append-only store, so a
        # controlled restart loses packet history, never handshakes.
        if agent_active && [ "$(agent_override_sum)" != "$ao_override_sum" ]; then
            log "agent updated the scan plan; relaunching AngryOxide"
            replan=yes
            kill -TERM "$ao_pid" 2>/dev/null || true
            break
        fi
        [ "$max_run_mb" -gt 0 ] || continue
        rb=$(raw_run_bytes) || true
        if [ -n "${rb:-}" ] && [ "$rb" -ge "$max_run_bytes" ]; then
            log "raw capture files at $(( rb / 1048576 ))MB (cap ${max_run_mb}MB); rolling capture, keeping hashlines"
            rolling=yes
            kill -TERM "$ao_pid" 2>/dev/null || true
            break
        fi
    done
    ao_rc=0
    wait "$ao_pid" 2>/dev/null || ao_rc=$?
    ao_pid=
    ran=$(( $(date +%s) - ao_started ))

    if [ "$stopping" = yes ]; then
        log "stop requested; capture exiting (AngryOxide rc=$ao_rc after ${ran}s)"
        break
    fi
    if [ "$rolling" = yes ]; then
        log "AngryOxide stopped to roll the run dir (rc=$ao_rc after ${ran}s); restarting, hashlines kept"
        fast_failures=0
        continue
    fi
    if [ "$replan" = yes ]; then
        log "AngryOxide restarting for the agent's new scan plan (rc=$ao_rc after ${ran}s)"
        fast_failures=0
        continue
    fi

    # Unexpected exit while still enabled (self-exit, or a USB drop AngryOxide
    # saw as a clean shutdown). Relaunch so capture resumes; guard against a
    # hot loop if it keeps dying immediately.
    if [ "$ran" -ge "$min_healthy_seconds" ]; then
        fast_failures=0
        log "AngryOxide exited (rc=$ao_rc) after ${ran}s; relaunching capture"
    else
        fast_failures=$(( fast_failures + 1 ))
        log "AngryOxide exited (rc=$ao_rc) after only ${ran}s (fast failure ${fast_failures}/${max_fast_failures})"
        if [ "$fast_failures" -ge "$max_fast_failures" ]; then
            log "AngryOxide keeps exiting immediately; giving up so the unit backs off (check the dongle, power and config)"
            exit 1
        fi
        nap "$relaunch_backoff"
        [ "$stopping" = yes ] && { log "stop requested; capture exiting"; break; }
    fi
done
