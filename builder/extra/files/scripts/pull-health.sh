#!/usr/bin/env bash
#
# pull-health.sh - pull a read-only health check off a Kestrel device to your
# computer, and say plainly whether anything looks wrong. The "grab this first"
# check: it replaced pull-device-diagnostics.sh and pull-capture-health.sh.
#
# Run it on your COMPUTER (not the device):
#
#   bash pull-health.sh <user>@<host> [ssh-port]
#
# It makes ONE SSH connection, runs a read-only collection on the device as your
# login user (no sudo), redacts the result ON THE DEVICE, and saves a single
# private text file, kestrel-health-<time>.txt (mode 600), in OUT_DIR (default:
# the current folder; it refuses a folder inside a git checkout so the report
# can't be committed by accident).
#
# What it checks:
#   - version, update status, uptime, temperature, throttling, disk, memory;
#   - every kestrel unit: state, when it last started, restart count; failed units;
#   - capture: running?, adapter present, AngryOxide and the collector running,
#     how fresh the capture database is, how fresh the dashboard's
#     "Environment" reading is (the "Scanning the area..." card), store counts;
#   - the learning agent's state and whether it is driving the scan plan;
#   - journal: error/traceback COUNTS per unit, plus scrubbed recent warnings
#     from the daemon, agent and update units (never the capture unit's text);
#   - kernel messages about USB/Wi-Fi resets and disconnects (scrubbed).
#   A VERDICT section at the top lists OK / WARN / FAIL lines.
#
# Read-only: it never starts, stops or restarts anything, never touches the
# management link (no nmcli, bluetoothctl, ip, rfkill, iw), and never reads
# capture content (no hashlines, network names, BSSIDs, whitelist entries or
# capture file names) - counts, sizes, ages and states only (from AngryOxide's
# database, only the newest row's time). Before the report
# leaves the device, MAC addresses, IP addresses, coordinates, a non-default
# hostname, the login name and quoted text in log lines are masked. Glance at it
# before sharing all the same.
#
# Env: SSH_KEY (identity file), OUT_DIR (where to save the report).

set -eu

if [ "$#" -lt 1 ] || [ "$#" -gt 2 ]; then
  echo "usage: bash $0 <user>@<host> [ssh-port]" >&2
  exit 2
fi
target=$1
port=${2:-22}
out_dir=${OUT_DIR:-.}
mkdir -p "$out_dir"
if git -C "$out_dir" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  echo "refusing: $out_dir is inside a git checkout; set OUT_DIR to a folder outside it" >&2
  exit 2
fi

ssh_opts=(-p "$port" -o ConnectTimeout=20)
[ -n "${SSH_KEY:-}" ] && ssh_opts+=(-i "$SSH_KEY")

stamp=$(date -u +%Y%m%dT%H%M%SZ)
report="$out_dir/kestrel-health-$stamp.txt"
umask 077

echo "Collecting from the device (read-only; about a minute)..."
# The heredoc is quoted, so it runs verbatim on the device.
# shellcheck disable=SC2029
if ! ssh "${ssh_opts[@]}" "$target" 'bash -s' >"$report.tmp" 2>&1 <<'REMOTE'
set -u
export LC_ALL=C
LIBDIR=/usr/local/lib/kestrel
STORE=/var/lib/kestrel/capture
RUNDIR=/var/lib/kestrel/capture-run
AGENT_DIR=/var/lib/kestrel/agent
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT
raw=$work/raw.txt
verdict=$work/verdict.txt
: >"$verdict"

sec() { printf '\n===== %s =====\n' "$1"; }
cfg() { timeout 10 "$LIBDIR/config-get" "$1" "${2:-}" 2>/dev/null; }
say() { printf '%-5s %s\n' "$1" "$2" >>"$verdict"; }   # OK / WARN / FAIL / INFO
# journal lines: quoted text blanked (it can hold names), cut short
scrub() { sed -E "s/'[^']*'/'<q>'/g; s/\"[^\"]*\"/\"<q>\"/g" | cut -c1-220 | sed 's/^/  /'; }
age_of() { echo $(( $(date +%s) - $(stat -c %Y "$1" 2>/dev/null || date +%s) )); }

{
sec "device"
echo "collected:  $(date -u +%Y-%m-%dT%H:%M:%SZ)"
echo "version:    $(cat /etc/kestrel/version 2>/dev/null || echo '?')"
echo "model:      $( { tr -d '\0' </proc/device-tree/model; } 2>/dev/null || echo '?')"
echo "kernel:     $(uname -r)"
echo "uptime:     $(uptime -p 2>/dev/null)   load: $(cut -d' ' -f1-3 /proc/loadavg)"
temp=$(awk '{printf "%.1f", $1/1000}' /sys/class/thermal/thermal_zone0/temp 2>/dev/null || echo '?')
thr=$(vcgencmd get_throttled 2>/dev/null | cut -d= -f2)
echo "SoC temp:   ${temp} C   throttled: ${thr:-?} (0x0 = never)"
echo "memory:     $(awk '/MemTotal|MemAvailable/ {printf "%s %d MB  ", $1, $2/1024}' /proc/meminfo)"
df -h / /boot/firmware 2>/dev/null | sed 's/^/  /'
case "$thr" in 0x0|"") ;; *) say WARN "the firmware reports throttling or under-voltage ($thr): check the power supply" ;; esac
du=$(df --output=pcent / 2>/dev/null | tail -1 | tr -dc 0-9)
[ -n "$du" ] && { [ "$du" -ge 90 ] && say FAIL "root disk ${du}% full" || say OK "root disk ${du}% used"; }

sec "update status"
python3 -I - <<'PY' 2>&1 | sed 's/^/  /'
import json
try:
    d = json.load(open("/var/lib/kestrel/update-run/status.json"))
    for k in ("status", "installed", "detail"):
        print("%s: %s" % (k, d.get(k)))
except FileNotFoundError:
    print("(no in-place update recorded)")
except Exception as exc:
    print("unreadable:", type(exc).__name__)
PY

sec "kestrel units: state, last start, restarts"
failed_units=""
for u in $(systemctl list-unit-files --no-legend 'kestrel*' 2>/dev/null | awk '{print $1}'); do
  case "$u" in *@.service) continue ;; esac
  printf '  %-42s %s\n' "$u" "$(systemctl show "$u" -p ActiveState -p SubState -p UnitFileState \
    -p ActiveEnterTimestamp -p NRestarts --value 2>/dev/null | tr '\n' ' ')"
done
echo "failed units:"
systemctl --failed --no-legend --plain 2>/dev/null | sed 's/^/  /'
failed_units=$(systemctl --failed --no-legend --plain 2>/dev/null | awk '{print $1}' \
  | grep -vx 'NetworkManager-wait-online.service' | tr '\n' ' ')
[ -z "${failed_units// /}" ] && say OK "no failed units" || say FAIL "failed units: $failed_units"
for u in kestrel-daemon kestrel-capture; do
  n=$(systemctl show "$u" -p NRestarts --value 2>/dev/null)
  [ "${n:-0}" -gt 3 ] 2>/dev/null && say WARN "$u has restarted $n times since boot"
done

sec "capture"
cap_on=$(cfg capture.enabled false)
cap_state=$(systemctl is-active kestrel-capture 2>/dev/null)
echo "capture.enabled: $cap_on   service: $cap_state   interface setting: $(cfg capture.interface auto)"
echo "scan mode: $(cfg capture.scan.mode '?')   dwell: $(cfg capture.scan.dwell_seconds '?')s   bands: $(cfg capture.scan.bands '?')"
# Which AngryOxide the capture service runs: the stock one, or one set with
# ANGRYOXIDE in the unit's environment (a systemd drop-in, to test a build).
ao_bin=$(systemctl show kestrel-capture -p Environment --value 2>/dev/null | tr ' ' '\n' \
  | sed -n 's/^ANGRYOXIDE=//p' | tail -1)
if [ -n "$ao_bin" ] && [ "$ao_bin" != /usr/bin/angryoxide ]; then
  ao_src="set by ANGRYOXIDE in the capture service (a test build?)"
else
  ao_bin=/usr/bin/angryoxide; ao_src="stock"
fi
ao_ver=$(timeout 10 "$ao_bin" --version 2>/dev/null | head -1)
echo "AngryOxide binary: $ao_bin ($ao_src): ${ao_ver:-version unknown}"
say INFO "${ao_ver:-AngryOxide (version unknown)} ($ao_src)"
[ -f /etc/kestrel/capture-ack ] && echo "authorized-use acknowledgment: given" || echo "authorized-use acknowledgment: not given"
echo "whitelist entries: $(python3 -I -c 'import tomllib;print(len(((tomllib.load(open("/etc/kestrel/config.toml","rb")).get("capture") or {}).get("whitelist")) or []))' 2>/dev/null || echo '?') (contents not read)"
ao=$(pgrep -x angryoxide >/dev/null && echo running || echo "not running")
col=$(pgrep -f 'capture-collect' >/dev/null && echo running || echo "not running")
echo "AngryOxide: $ao   collector: $col"
echo "wireless interfaces (driver, operstate; no addresses):"
for n in /sys/class/net/*; do
  [ -d "$n/wireless" ] || [ -d "$n/phy80211" ] || continue
  drv=$(basename "$(readlink -f "$n/device/driver" 2>/dev/null)" 2>/dev/null)
  bus=$(readlink -f "$n/device" 2>/dev/null | grep -q usb && echo usb || echo onboard)
  printf '  %-8s %-12s %-8s %s\n' "$(basename "$n")" "${drv:-?}" "$bus" "$(cat "$n/operstate" 2>/dev/null)"
done
usb_wifi=$(for n in /sys/class/net/*; do { [ -d "$n/wireless" ] || [ -d "$n/phy80211" ]; } && readlink -f "$n/device" | grep -q usb && echo x; done | wc -l)
if [ "$cap_on" = true ]; then
  [ "$cap_state" = active ] && say OK "capture service active" || say FAIL "capture is enabled but its service is $cap_state"
  [ "$usb_wifi" -ge 1 ] && say OK "USB Wi-Fi adapter present" || say FAIL "no USB Wi-Fi adapter visible (capture needs one)"
  [ "$ao" = running ] && say OK "AngryOxide running" || say FAIL "AngryOxide is not running"
  [ "$col" = running ] && say OK "collector running" || say FAIL "the capture collector is not running"
else
  say INFO "capture is off (enabled = $cap_on)"
fi

echo "AngryOxide's output files (run dir; sizes and ages, plus the database's newest row time):"
newest_cap=""
for f in $(ls -t "$RUNDIR"/*.pcapng 2>/dev/null); do
  [ -z "$newest_cap" ] && newest_cap=$f
  printf '  capture file (pcapng): %8s KB, written %ss ago\n' "$(( $(stat -c %s "$f") / 1024 ))" "$(age_of "$f")"
done
for db in $(ls -t "$RUNDIR"/*.kismet 2>/dev/null); do
  printf '  kismet database:       %8s KB, written %ss ago\n' "$(( $(stat -c %s "$db") / 1024 ))" "$(age_of "$db")"
done
[ -e "$(ls "$RUNDIR"/*.kismet-journal 2>/dev/null | head -1)" ] \
  && echo "  a kismet-journal file is present (an insert was cut short, or AngryOxide is mid-write)"
[ -z "$newest_cap" ] && echo "  (no capture file in the run dir)"
# The capture file is a plain append, so its age says whether AngryOxide is
# still capturing. (Its kismet database is no guide: AngryOxide commits one row
# per frame and falls minutes behind in a busy area, while its file still updates.)
if [ "$cap_on" = true ] && [ -n "$newest_cap" ]; then
  a=$(age_of "$newest_cap")
  if [ "$a" -le 120 ]; then say OK "capture file written ${a}s ago"
  elif [ "$a" -le 600 ]; then say WARN "capture file last written ${a}s ago (a quiet channel? check again in a few minutes)"
  else say FAIL "capture file not written for ${a}s: AngryOxide may have stalled or the adapter dropped"; fi
elif [ "$cap_on" = true ]; then
  say WARN "no capture file yet (capture just (re)started?)"
fi
# AngryOxide queues every frame for its database without a limit, so in a busy
# area its memory grows for as long as capture runs.
ao_pid=$(pgrep -x angryoxide | head -1)
if [ -n "$ao_pid" ]; then
  rss_kb=$(awk '/^VmRSS/ {print $2}' "/proc/$ao_pid/status" 2>/dev/null)
  total_kb=$(awk '/^MemTotal/ {print $2}' /proc/meminfo)
  if [ -n "$rss_kb" ] && [ -n "$total_kb" ]; then
    pct=$(( rss_kb * 100 / total_kb ))
    # With how long it has been running, so two checks a while apart show
    # whether memory is flat or climbing.
    up_min=$(( $(ps -o etimes= -p "$ao_pid" 2>/dev/null | tr -d ' ' || echo 0) / 60 ))
    echo "  AngryOxide memory: $(( rss_kb / 1024 )) MB (${pct}% of RAM) after ${up_min} min running"
    if [ "$pct" -ge 30 ]; then
      say WARN "AngryOxide is using ${pct}% of RAM after ${up_min} min; in a busy area its database queue grows while capture runs (restart capture to reset it)"
    else
      say OK "AngryOxide memory $(( rss_kb / 1024 )) MB (${pct}% of RAM) after ${up_min} min running"
    fi
  fi
fi
# How far AngryOxide's kismet database trails its capture file: v0.9.3 committed
# one row per frame and fell minutes behind in a busy area; v0.9.4 batches the
# writes. One read-only query for the newest row's time; nothing else is read.
newest_db=$(ls -t "$RUNDIR"/*.kismet 2>/dev/null | head -1)
if [ -n "$newest_db" ] && [ -n "$newest_cap" ]; then
  db_lag=$(python3 -I - "$newest_db" "$(stat -c %Y "$newest_cap")" <<'PY' 2>/dev/null
import sqlite3, sys
try:
    conn = sqlite3.connect("file:%s?mode=ro" % sys.argv[1], uri=True, timeout=2)
    newest = conn.execute("SELECT MAX(ts_sec) FROM packets").fetchone()[0]
    print(max(0, int(sys.argv[2]) - int(newest)) if newest is not None else "empty")
except Exception as exc:
    print("unreadable:%s" % type(exc).__name__)
PY
)
  case "$db_lag" in
    ''|unreadable:*)
      echo "  AngryOxide's database: couldn't read its newest row just now (${db_lag#unreadable:})"
      say INFO "couldn't read AngryOxide's database just now (busy, or left mid-write)" ;;
    empty)
      echo "  AngryOxide's database: no rows yet" ;;
    *)
      echo "  AngryOxide's database: newest row ${db_lag}s behind its capture file"
      if [ "$cap_on" != true ] || [ -z "$ao_pid" ]; then
        say INFO "the last run's database ended ${db_lag}s behind its capture file"
      elif [ "$db_lag" -le 60 ]; then
        say OK "AngryOxide's database keeps up (${db_lag}s behind its capture file)"
      elif [ "$db_lag" -le 300 ]; then
        say WARN "AngryOxide's database is ${db_lag}s behind its capture file"
      else
        say FAIL "AngryOxide's database is $(( db_lag / 60 )) min behind its capture file: its writes can't keep up (v0.9.4 batches them)"
      fi ;;
  esac
fi

echo "dashboard Environment reading (capture-status.json; counts and ages only):"
cap_age=""; [ -n "$newest_cap" ] && cap_age=$(age_of "$newest_cap")
python3 -I - "$STORE/capture-status.json" "$(cfg capture.environment.max_age_s 900)" "$cap_age" "$cap_on" <<'PY' >"$work/env.txt" 2>&1
import json, sys, time
path, max_age = sys.argv[1], int(sys.argv[2] or 900)
now = time.time()
try:
    d = json.load(open(path))
except FileNotFoundError:
    print("  (no status file)")
    print("VERDICT %s no Environment status file yet" % ("WARN" if sys.argv[4:5] == ["true"] else "INFO"))
    sys.exit()
except Exception as exc:
    print("  unreadable:", type(exc).__name__); print("VERDICT FAIL Environment status file unreadable"); sys.exit()
written = int(now - float(d.get("updated_at") or 0))
data_age = int(now - float(d["data_ts"])) if "data_ts" in d else None
cap_age = int(sys.argv[3]) if len(sys.argv) > 3 and sys.argv[3] else None
capturing = len(sys.argv) > 4 and sys.argv[4] == "true"
print("  scanning: %s   written %ss ago   newest data %s   radios in window: %s   window: %ss"
      % (d.get("scanning"), written, ("%ss old" % data_age) if data_age is not None else "none",
         d.get("radios_recent"), d.get("window_s")))
if not capturing:
    print("VERDICT INFO Environment card: capture is off, so there is no current reading")
elif written > 180:
    print("VERDICT FAIL Environment card: the collector last wrote %ss ago (it should every few seconds)" % written)
elif data_age is None:
    print("VERDICT WARN Environment card: no reading yet, so it shows 'Scanning the area...'")
elif data_age > max_age and cap_age is not None and cap_age <= 300:
    print("VERDICT FAIL Environment card stuck: AngryOxide wrote its capture file %ss ago but the "
          "collector's newest reading is %ss old; capture itself is working. Before 1.0.11 the "
          "collector read AngryOxide's database, which falls behind in a busy area" % (cap_age, data_age))
elif data_age > max_age:
    print("VERDICT WARN Environment card: newest data is %ss old (over %ss), so it shows 'Scanning the area...'" % (data_age, max_age))
else:
    print("VERDICT OK Environment card has a current reading (%s radios)" % d.get("radios_recent"))
PY
grep -v '^VERDICT' "$work/env.txt"
grep '^VERDICT' "$work/env.txt" | sed 's/^VERDICT //' | while read -r lvl rest; do say "$lvl" "$rest"; done

echo "store (counts only):"
echo "  handshake files: $(find "$STORE" -maxdepth 1 -name '*.hc22000' 2>/dev/null | wc -l)   size: $(du -sh "$STORE" 2>/dev/null | cut -f1)   run dir: $(du -sh "$RUNDIR" 2>/dev/null | cut -f1)"

sec "features and plugins"
echo "capture: $(cfg capture.enabled '?')   daemon: $(cfg daemon.enabled '?')   web: $(cfg web.enabled '?')   agent: $(cfg agent.enabled '?')"
timeout 20 /usr/local/bin/kestrel plugins list 2>&1 | sed 's/^/  /'

sec "learning agent"
echo "service: $(systemctl is-active kestrel-agent 2>/dev/null)   [agent] policy: $(cfg agent.policy '?')"
python3 -I - "$AGENT_DIR" <<'PY' 2>&1 | sed 's/^/  /'
import json, os, sys, time
d = sys.argv[1]
for name in ("status.json", "scan-override.json"):
    p = os.path.join(d, name)
    try:
        age = int(time.time() - os.stat(p).st_mtime)
    except OSError:
        print("%s: none" % name); continue
    print("%s: written %ss ago" % (name, age))
    if name == "status.json":
        try:
            s = json.load(open(p))
            keep = {k: v for k, v in s.items() if isinstance(v, (str, int, float, bool))
                    and k in ("state", "policy", "algorithm", "posture", "decisions",
                              "session_handshakes", "last_reward", "model_source")}
            print("  " + ", ".join("%s=%s" % kv for kv in sorted(keep.items())))
        except Exception as exc:
            print("  unreadable:", type(exc).__name__)
PY

sec "journal this boot: errors, warnings and tracebacks per unit (counts)"
for u in kestrel-daemon kestrel-capture kestrel-agent kestrel-capture-housekeeping; do
  # By log priority, not by words in the message: an info line that mentions
  # "failed" is not an error.
  e=$(journalctl -b -q --no-pager -p err -u "$u" 2>/dev/null | grep -c . || true)
  w=$(journalctl -b -q --no-pager -p warning -u "$u" 2>/dev/null | grep -c . || true)
  t=$(journalctl -b -q --no-pager -u "$u" 2>/dev/null | grep -c 'Traceback' || true)
  printf '  %-32s errors: %-4s warnings and above: %-4s tracebacks: %s\n' "$u" "$e" "$w" "$t"
  [ "${e:-0}" -gt 0 ] && say WARN "$u logged $e error line(s) this boot"
  [ "${t:-0}" -gt 0 ] && say WARN "$u logged $t traceback(s) this boot"
done
# The capture unit's text can carry network names: counts only, never its lines.
cj=$(journalctl -b -q --no-pager -u kestrel-capture 2>/dev/null)
echo "capture unit, this boot (counts only): database locked/busy: $(printf '%s\n' "$cj" | grep -ciE 'locked|busy' || true)   timeouts: $(printf '%s\n' "$cj" | grep -ciE 'timed? ?out' || true)   relaunches: $(printf '%s\n' "$cj" | grep -c 'starting: ' || true)"
echo "recent warnings and errors (daemon, agent, update; scrubbed):"
journalctl -b -q --no-pager -p warning -u kestrel-daemon -u kestrel-agent \
  -u kestrel-update-recover 2>/dev/null | tail -25 | scrub

sec "kernel: USB / Wi-Fi resets and disconnects (scrubbed)"
k=$(dmesg 2>/dev/null || journalctl -k -b -q --no-pager 2>/dev/null)
printf '%s\n' "$k" | grep -iE 'usb .*disconnect|reset (high|super)-speed|firmware.*(fail|crash)|wlan.*(error|timeout)|mt76|rtw|ath9k|brcmf.*error' \
  | tail -15 | scrub
n=$(printf '%s\n' "$k" | grep -ciE 'usb .*disconnect' || true)
[ "${n:-0}" -gt 0 ] && say WARN "$n USB disconnect(s) this boot: a USB device (the Wi-Fi adapter?) dropped out"
} >"$raw" 2>&1

# The verdict first, then the readings; then mask identifiers before anything
# leaves the device.
{
  echo "Kestrel health check"
  echo
  echo "===== VERDICT ====="
  if grep -q '^FAIL' "$verdict"; then echo "Something is wrong: see the FAIL lines."
  elif grep -q '^WARN' "$verdict"; then echo "Mostly fine, with warnings worth a look."
  else echo "Everything checked looks healthy."; fi
  sort -s -k1,1 -t' ' "$verdict" | awk '{o=($1=="FAIL")?0:($1=="WARN")?1:($1=="INFO")?2:3; print o"\t"$0}' \
    | sort -s -n -k1,1 | cut -f2-
  cat "$raw"
} | python3 -I -c '
import re, socket, sys, os
t = sys.stdin.read()
t = re.sub(r"\b([0-9A-Fa-f]{2}[:-]){5}[0-9A-Fa-f]{2}\b", "<mac>", t)
t = re.sub(r"\b([0-9A-Fa-f]{1,4}:){2,7}[0-9A-Fa-f]{0,4}(%\w+)?\b(?=[^:]|$)", lambda m: m.group(0) if re.fullmatch(r"\d{1,2}:\d{2}(:\d{2})?", m.group(0)) else "<ipv6>", t)
t = re.sub(r"\b(\d{1,3}\.){3}\d{1,3}\b", "<ipv4>", t)
t = re.sub(r"-?\b\d{1,3}\.\d{4,}\s*,\s*-?\d{1,3}\.\d{4,}\b", "<coords>", t)
host = socket.gethostname()
if host and host != "kestrel":
    t = re.sub(re.escape(host), "<hostname>", t)
user = os.environ.get("USER") or ""
if user and user != "pi":
    t = re.sub(r"\b%s\b" % re.escape(user), "<user>", t)
sys.stdout.write(t)
'
REMOTE
then
  echo "note: the SSH session ended with an error; the partial report was still saved." >&2
fi
mv "$report.tmp" "$report"
chmod 600 "$report"
echo
sed -n '/===== VERDICT =====/,/^$/p' "$report" | sed '/^$/q'
echo "Full report: $report (private; read it before sharing)"
