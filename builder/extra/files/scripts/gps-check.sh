#!/usr/bin/env bash
# gps-check.sh - check the GPS on a Kestrel device from your desktop over SSH.
#
# Runs entirely over one SSH connection; nothing needs to be pre-installed on
# the device. Two modes:
#
#   (default) read-only, non-disruptive. Reports: serial node present, pi in
#             dialout, the kestrel-daemon service, whether the gps plugin is
#             enabled and its source, the daemon's live fix (/run/kestrel/
#             position.json, which exists only once a fix has been obtained),
#             and recent gps log lines.
#
#   --sample [N]  definitive receiver test. Briefly STOPS the daemon, reads N
#             seconds of raw NMEA straight off the serial port (default 15),
#             classifies it as FIX / STREAMING (heard, no lock) / SILENT, prints
#             the strongest C/N0 and how many satellites are fix-usable (>=30
#             dB-Hz), then restarts the daemon. Needs sudo on the device.
#
# Usage:
#   ./gps-check.sh [user@]host [--sample [SECONDS]] [--serial /dev/ttyACM0]
#
# Examples:
#   ./gps-check.sh kestrel.local
#   ./gps-check.sh pi@kestrel.local --sample 20
#   ./gps-check.sh pi@192.168.1.50 --serial /dev/ttyACM1
#
# Defaults: user "pi", host "kestrel.local".
set -euo pipefail

TARGET=""
SAMPLE=0
SAMPLE_SECS=15
SERIAL=""

while [ $# -gt 0 ]; do
    case "$1" in
        --sample)
            SAMPLE=1
            # optional numeric argument
            if [ "${2:-}" ] && printf '%s' "$2" | grep -qE '^[0-9]+$'; then
                SAMPLE_SECS=$2; shift
            fi ;;
        --serial) SERIAL=${2:-}; shift ;;
        -h|--help) sed -n '2,30p' "$0"; exit 0 ;;
        -*) echo "unknown option: $1" >&2; exit 2 ;;
        *)  TARGET=$1 ;;
    esac
    shift
done

[ -n "$TARGET" ] || TARGET="kestrel.local"
case "$TARGET" in *@*) : ;; *) TARGET="pi@${TARGET}" ;; esac

echo "== Kestrel GPS check: ${TARGET} =="
echo

# The remote script runs on the device. It is passed SAMPLE/SAMPLE_SECS/SERIAL
# as positional args so no desktop values leak into the remote quoting.
# shellcheck disable=SC2029
ssh -o ConnectTimeout=10 -o BatchMode=no "$TARGET" \
    "bash -s -- '$SAMPLE' '$SAMPLE_SECS' '$SERIAL'" <<'REMOTE'
set -u
SAMPLE=$1; SAMPLE_SECS=$2; SERIAL_ARG=$3
CFG=/etc/kestrel/config.toml
DAEMON=kestrel-daemon.service
POS=/run/kestrel/position.json
pass() { printf '  [ ok ] %s\n' "$1"; }
warn() { printf '  [warn] %s\n' "$1"; }
fail() { printf '  [FAIL] %s\n' "$1"; }

# --- resolve the serial node (configured device, else first of its family) ---
cfg_source=$(sed -n 's/^[[:space:]]*source[[:space:]]*=[[:space:]]*"\{0,1\}\([a-z]*\).*/\1/p' \
             "$CFG" 2>/dev/null | head -1)
cfg_dev=$(sed -n 's#^[[:space:]]*device[[:space:]]*=[[:space:]]*"\{0,1\}\(/dev/[^" ]*\).*#\1#p' \
          "$CFG" 2>/dev/null | head -1)
dev="$SERIAL_ARG"
[ -n "$dev" ] || dev="$cfg_dev"
[ -n "$dev" ] || dev=/dev/ttyACM0
if [ ! -e "$dev" ]; then
    base=${dev%[0-9]}
    alt=$(ls "${base}"* 2>/dev/null | head -1 || true)
    [ -n "$alt" ] && dev="$alt"
fi

echo "Hardware & OS"
if ls /dev/ttyACM* /dev/ttyUSB* >/dev/null 2>&1; then
    pass "serial node(s): $(ls /dev/ttyACM* /dev/ttyUSB* 2>/dev/null | tr '\n' ' ')"
else
    fail "no /dev/ttyACM* or /dev/ttyUSB* present - receiver not enumerated (USB power/cable?)"
fi
if id -nG pi 2>/dev/null | tr ' ' '\n' | grep -qx dialout; then
    pass "user pi is in group dialout"
else
    warn "user pi is NOT in dialout - the daemon may not be able to open $dev"
fi
echo

echo "Daemon & plugin"
state=$(systemctl is-active "$DAEMON" 2>/dev/null || true)
if [ "$state" = active ]; then pass "$DAEMON is active"
else fail "$DAEMON is $state"; fi
# gps plugin enabled flag lives under [plugins.gps]
gps_enabled=$(awk '
    /^\[/{inblk=0}
    /^\[plugins\.gps\]/{inblk=1; next}
    inblk && /^[[:space:]]*enabled[[:space:]]*=/{
        gsub(/[^a-z]/,"",$0); print; exit}' "$CFG" 2>/dev/null)
if [ "$gps_enabled" = true ]; then pass "gps plugin is enabled (source=${cfg_source:-auto})"
else warn "gps plugin is NOT enabled - run: sudo kestrel plugins enable gps --yes"; fi
echo

echo "Live fix (from the running daemon)"
if [ -r "$POS" ]; then
    python3 - "$POS" <<'PY'
import json,sys,time,calendar
try:
    d=json.load(open(sys.argv[1]))
except Exception as e:
    print("  [warn] position.json unreadable: %s"%e); sys.exit(0)
lat=d.get("lat"); lon=d.get("lon"); alt=d.get("alt")
fix=d.get("fix"); sats=d.get("sats"); cn0=d.get("cn0"); strong=d.get("strong")
ft=d.get("fix_time")
age=""
if ft:
    try:
        age=" (%.0fs ago)"%(time.time()-calendar.timegm(time.strptime(ft,"%Y-%m-%dT%H:%M:%SZ")))
    except Exception: pass
mode={2:"2D",3:"3D"}.get(fix, str(fix))
print("  [ ok ] fix=%s lat=%s lon=%s alt=%s"%(mode,lat,lon,alt))
print("         sats=%s  strongest C/N0=%s dB-Hz  fix-usable(>=30)=%s"%(sats,cn0,strong))
print("         last fix%s"%(age or ": unknown age"))
PY
else
    warn "no $POS yet - the daemon has not obtained a fix this run"
    warn "a fix needs open sky, the antenna still, and up to ~15 min cold-start"
fi
echo

echo "Recent gps log lines"
journalctl -u "$DAEMON" -n 200 --no-pager 2>/dev/null \
    | grep -i gps | tail -6 | sed 's/^/  /' || echo "  (none)"

if [ "$SAMPLE" = 1 ]; then
    echo
    echo "NMEA sample (${SAMPLE_SECS}s) on ${dev} - stopping the daemon briefly"
    if [ ! -e "$dev" ]; then fail "serial node $dev not present; cannot sample"; exit 0; fi
    sudo systemctl stop "$DAEMON" 2>/dev/null || { fail "could not stop daemon (sudo?)"; exit 0; }
    raw=$(mktemp)
    sudo timeout "$SAMPLE_SECS" cat "$dev" > "$raw" 2>/dev/null || true
    sudo systemctl start "$DAEMON" 2>/dev/null || true
    lines=$(wc -l < "$raw" | tr -d ' ')
    python3 - "$raw" <<'PY'
import sys,re
maxsnr=0.0; strong=0; have_fix=False; nmea=0
for line in open(sys.argv[1],"rb"):
    try: s=line.decode("ascii","ignore").strip()
    except Exception: continue
    if not s.startswith("$"): continue
    nmea+=1
    body=s.split("*")[0]
    f=body.split(",")
    if f[0].endswith("GGA") and len(f)>7:
        try:
            if int(f[6] or 0)>0: have_fix=True
        except ValueError: pass
    if f[0].endswith("GSV"):
        # satellite groups of 4 starting at index 4: prn,elev,az,snr
        i=7
        while i<len(f):
            try:
                v=float(f[i])
                if v>maxsnr: maxsnr=v
                if v>=30.0: strong+=1
            except ValueError: pass
            i+=4
print("  NMEA sentences: %d"%nmea)
if nmea==0:
    print("  [FAIL] SILENT - no NMEA at all. Receiver not talking (power/cable/wrong node).")
elif have_fix:
    print("  [ ok ] FIX - a GGA sentence reports a position fix.")
    print("         strongest C/N0=%.0f dB-Hz, %d satellite-reads >=30 (fix-usable)."%(maxsnr,strong))
else:
    print("  [warn] STREAMING, NO LOCK - receiver is alive but has no fix yet.")
    print("         strongest C/N0=%.0f dB-Hz, %d satellite-reads >=30."%(maxsnr,strong))
    if maxsnr<30:
        print("         All signals <30 dB-Hz: too weak. Needs genuine open sky, antenna up & still.")
    else:
        print("         Some signals >=30: give it time (cold start up to ~15 min, longer if idle long).")
PY
    rm -f "$raw"
fi
REMOTE

echo
echo "Done. Re-run with --sample for a definitive receiver test if the live fix is empty."
