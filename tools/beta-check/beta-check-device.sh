#!/usr/bin/env bash
#
# beta-check-device.sh - the device side of pull-beta-check.sh. Don't run it by
# hand: pull-beta-check.sh copies it to a private temp folder on the device and
# runs it once with sudo as
#   sudo bash check.sh <output-dir> [expected-version]
# It only reads (see pull-beta-check.sh for the full list of limits) and writes
# one redacted report.txt into <output-dir> as the SSH login user.
#
# The CHECK ids match the numbered items of the beta's checklist issue. The
# generic parts (device info, the throwaway-config harness, the dashboard
# readout, redaction) stay; the per-release checks cover the whole 1.0.6 beta
# line (beta.1 through beta.4), so one run verifies every 1.0.6 change.

set -u

if [ "$(id -u)" -ne 0 ]; then
  echo "kbc: needs root (run through sudo)" >&2
  exit 1
fi
OUT_DIR=${1:?output dir}
EXPECT_VERSION=${2:-}

# Low priority, so the daemon and the management link always win.
if [ -z "${KBC_NICED:-}" ]; then
  export KBC_NICED=1
  exec nice -n 15 ionice -c 3 bash "$0" "$@"
fi

PATH=/usr/sbin:/usr/bin:/sbin:/bin
export PATH
CONFIG=/etc/kestrel/config.toml
LIBDIR=/usr/local/lib/kestrel
CLI=/usr/local/bin/kestrel
CTL=$LIBDIR/plugins-ctl
STATE=/var/lib/kestrel
STORE=$STATE/capture
RUNDIR=$STATE/capture-run
LEGACY_RUN=$STORE/.run
AGENT_DIR=$STATE/agent
# The device account is whoever the daemon runs as; nothing here assumes its name.
DEV_USER=$(systemctl show -p User --value kestrel-daemon.service 2>/dev/null)
[ -n "$DEV_USER" ] || DEV_USER=$(stat -c %U "$STATE/capture" 2>/dev/null)
DEV_HOME=$(getent passwd "$DEV_USER" | cut -d: -f6)
LOGIN_USER=${SUDO_USER:-root}
VENV_PY=$DEV_HOME/.venv/bin/python
SCRIPTS_DIR=/usr/local/share/kestrel/scripts
# Shared paths: not every beta's checks use all of them.
: "$CLI" "$CTL" "$AGENT_DIR" "$VENV_PY" "$SCRIPTS_DIR" "$LEGACY_RUN"

stamp=$(date -u +%Y%m%dT%H%M%SZ)
work=$(mktemp -d /tmp/kbc.XXXXXX)          # root-only (0700)
trap 'rm -rf "$work"' EXIT
raw=$work/report.raw

as_dev() { setpriv --reuid="$DEV_USER" --regid="$DEV_USER" --init-groups --reset-env -- "$@"; }
cfg() { timeout 10 "$LIBDIR/config-get" "$1" "${2:-}" 2>/dev/null; }
sec() { printf '\n===== %s =====\n' "$1"; }
# check ID RESULT "what" "detail"   RESULT: PASS FAIL INFO MANUAL SKIP
# Journal excerpts: quoted text blanked (it can hold names), lines cut short.
jscrub() { sed -E "s/'[^']*'/'<q>'/g; s/\"[^\"]*\"/\"<q>\"/g" | cut -c1-300 | sed 's/^/  /'; }
check() { printf 'CHECK %-5s %-6s %s :: %s\n' "$1" "$2" "$3" "${4:-}"; }
svc_active() { systemctl is-active "$1" 2>/dev/null || true; }

# Optional dashboard login, asked up front so the rest runs unattended.
web_auth=$(cfg web.auth system)
web_pw=""
if [ "$web_auth" = "system" ] && [ -t 0 ]; then
  echo "Optional: the dashboard's /status.json adds plugin health, agent and"
  echo "environment state to the report. Type the device login password"
  printf 'to include it (input hidden), or press Enter to skip: '
  IFS= read -rs web_pw || web_pw=""
  echo
fi

echo "Collecting... (a minute or two; nothing is changed)"

{
sec "device"
echo "collected:  $stamp"
echo "version:    $(cat /etc/kestrel/version 2>/dev/null || echo '?')"
echo "model:      $(tr -d '\0' </proc/device-tree/model 2>/dev/null || echo '?')"
echo "kernel:     $(uname -r)"
echo "memory:     $(awk '/MemTotal|MemAvailable/ {printf "%s %d MB  ", $1, $2/1024}' /proc/meminfo)"
echo "uptime:     $(uptime -p 2>/dev/null)"
echo "python3:    $(/usr/bin/python3 -V 2>&1)"
echo "web auth:   $web_auth"

# ================================================================ 1.0.6 checks (beta.1 - beta.4)
# One script for the whole 1.0.6 beta line. CHECK ids match the checklist issue.
pid=$(systemctl show -p MainPID --value kestrel-daemon.service 2>/dev/null)
cap_state=$(svc_active kestrel-capture.service)
cap_since=$(systemctl show -p ActiveEnterTimestamp --value kestrel-capture.service 2>/dev/null)
cap_since_s=$(date -d "$cap_since" +%s 2>/dev/null || echo 0)
cap_age=$(( $(date +%s) - cap_since_s ))
upd=/var/lib/kestrel/update-run/status.json

# ---------------------------------------------------------------- 1. update to this beta
sec "1. update to this beta"
ver=$(cat /etc/kestrel/version 2>/dev/null || echo "")
echo "update-run status (root-owned):"
updinfo=$(/usr/bin/python3 -I - <<'PY' 2>&1
import json, os, time
try:
    st = os.stat("/var/lib/kestrel/update-run/status.json")
    data = json.load(open("/var/lib/kestrel/update-run/status.json"))
    print("mtime:", time.strftime("%Y-%m-%d %H:%M:%SZ", time.gmtime(st.st_mtime)))
    for k, v in sorted(data.items()):
        if isinstance(v, (str, int, float, bool)) or v is None:
            print("%s: %s" % (k, v))
except FileNotFoundError:
    print("(none: never applied an in-place update)")
except Exception as exc:
    print("unreadable:", type(exc).__name__)
PY
)
printf '%s\n' "$updinfo" | sed 's/^/  /'
if [ -z "$EXPECT_VERSION" ]; then
  check 1.1 INFO "version" "/etc/kestrel/version = $ver"
elif [ "$ver" = "$EXPECT_VERSION" ]; then
  check 1.1 PASS "version" "$ver"
else
  check 1.1 FAIL "version" "${ver:-missing}, expected $EXPECT_VERSION"
fi
if [ -n "$EXPECT_VERSION" ] && printf '%s\n' "$updinfo" | grep -qx "status: ok" \
    && printf '%s\n' "$updinfo" | grep -qx "installed: $EXPECT_VERSION"; then
  check 1.2 PASS "update applied in place" "update-run: ok, installed $EXPECT_VERSION"
else
  check 1.2 INFO "update applied in place" "see update-run status above"
fi
failed=$(systemctl --failed --no-legend --plain 2>/dev/null | awk '{print $1}' | tr '\n' ' ')
others=$(printf '%s\n' $failed | grep -vx 'NetworkManager-wait-online.service' | tr '\n' ' ')
if [ -z "${others// /}" ]; then
  check 1.3 PASS "no new failed units" "failed: ${failed:-none}"
else
  check 1.3 FAIL "new failed units" "$failed"
fi
tb=$(journalctl -b -q --no-pager -u kestrel-daemon -u kestrel-agent -u kestrel-capture 2>/dev/null | grep -c 'Traceback' || true)
if [ "${tb:-0}" -eq 0 ]; then
  check 1.4 PASS "no tracebacks this boot (daemon, agent, capture)" ""
else
  check 1.4 FAIL "tracebacks this boot" "$tb"
  journalctl -b -q --no-pager -u kestrel-daemon -u kestrel-agent -u kestrel-capture 2>/dev/null \
    | grep -E '(Error|Exception|KeyError)(:|$)' | tail -8 | jscrub
fi
if [ -f "$upd" ] && [ "$CONFIG" -ot "$upd" ]; then
  check 1.5 PASS "settings unchanged by the update" "config.toml older than the update run"
elif [ -f "$upd" ]; then
  check 1.5 INFO "settings unchanged by the update" "config.toml changed after the update (by you?)"
else
  check 1.5 SKIP "settings unchanged by the update" "no in-place update recorded"
fi

# ---------------------------------------------------------------- 2. update settings terminal-only (#15)
sec "2. update settings are terminal-only (#15)"
info=$(timeout 20 /usr/bin/python3 -I "$CTL" info updates 2>&1)
n=$(printf '%s\n' "$info" | grep -E '^  (repo|host|token) ' | grep -c 'terminal only' || true)
if [ "${n:-0}" -eq 3 ]; then
  check 2.1 PASS "repo/host/token marked terminal only" "web shows them read-only (by hand)"
else
  check 2.1 FAIL "repo/host/token marked terminal only" "$n of 3"
fi

# ---------------------------------------------------------------- 3. capture folder, collector, hand-over, relaunch
sec "3. capture run folder, collector, hand-over and relaunch (#15 #24 #27)"
echo "capture enabled=$(cfg capture.enabled false) service=$cap_state since=${cap_since:-?} (${cap_age}s)"
if [ -d "$RUNDIR" ]; then
  perm=$(stat -c '%U:%G %a' "$RUNDIR")
  [ "$perm" = "root:$DEV_USER 750" ] && check 3.1 PASS "capture-run ownership" "$perm" \
    || check 3.1 FAIL "capture-run ownership" "$perm (expected root:<device user> 750)"
else
  check 3.1 SKIP "capture-run ownership" "folder not created yet"
fi
if [ "$cap_state" = active ]; then
  npi=$(pgrep -c -u "$DEV_USER" -f capture-collect || true)
  nrt=$(pgrep -c -u root -f capture-collect || true)
  [ "${npi:-0}" -ge 1 ] && [ "${nrt:-0}" -eq 0 ] \
    && check 3.2 PASS "collector runs as the device user" "user=$npi root=$nrt" \
    || check 3.2 FAIL "collector runs as the device user" "user=$npi root=$nrt"
  nk=$(find "$RUNDIR" -maxdepth 1 -name '*.kismet' 2>/dev/null | wc -l)
  np=$(find "$RUNDIR" -maxdepth 1 -name '*.pcapng' 2>/dev/null | wc -l)
  if [ -f "$upd" ]; then nnew=$(find "$LEGACY_RUN" -maxdepth 1 -type f -newer "$upd" 2>/dev/null | wc -l); else nnew=0; fi
  [ "$((nk + np))" -ge 1 ] && [ "$nnew" -eq 0 ] \
    && check 3.3 PASS "raw files go to capture-run, nothing new in .run" "kismet=$nk pcapng=$np new-in-legacy=$nnew" \
    || check 3.3 FAIL "raw files go to capture-run, nothing new in .run" "kismet=$nk pcapng=$np new-in-legacy=$nnew"
else
  check 3.2 SKIP "collector runs as the device user" "capture not running"
  check 3.3 SKIP "raw files go to capture-run" "capture not running"
fi
nroot=$(find "$STORE" -mindepth 1 -maxdepth 5 ! -type l -user root 2>/dev/null | wc -l)
[ "$nroot" -eq 0 ] && check 3.4 PASS "no root-owned files/folders left in the store (#24)" "" \
  || check 3.4 FAIL "no root-owned files/folders left in the store (#24)" "$nroot"
if [ "$cap_state" = active ] && [ -n "$cap_since" ]; then
  pd=$(journalctl -q --no-pager -u kestrel-capture --since "$cap_since" 2>/dev/null | grep -c 'Permission denied' || true)
  [ "${pd:-0}" -eq 0 ] && check 3.5 PASS "no 'Permission denied' from the collector since start" "" \
    || check 3.5 FAIL "no 'Permission denied' from the collector since start" "$pd line(s)"
else
  check 3.5 SKIP "no 'Permission denied' from the collector since start" "capture not running"
fi
if [ -d "$LEGACY_RUN" ]; then
  lown=$(stat -c '%U' "$LEGACY_RUN" 2>/dev/null)
  lres=$(as_dev /usr/bin/python3 -I -c 'import glob
bad=0
for n in glob.glob("/var/lib/kestrel/capture/.run/*.hc22000"):
    try: open(n,"rb").read(1)
    except OSError: bad+=1
print("files=%d unreadable=%d"%(len(glob.glob("/var/lib/kestrel/capture/.run/*.hc22000")),bad))' 2>&1)
  [ "$lown" = "$DEV_USER" ] && printf '%s' "$lres" | grep -q 'unreadable=0' \
    && check 3.6 PASS "legacy .run owned+readable by the device user" "$lres (kept by design)" \
    || check 3.6 FAIL "legacy .run owned+readable by the device user" "owner=$lown $lres"
else
  check 3.6 PASS "legacy .run owned+readable by the device user" "no legacy folder"
fi
adir=$(cfg capture.housekeeping.archive_dir "$STORE/archive")
if [ -d "$adir" ]; then
  nnp=$(find "$adir" ! -type l ! -user "$DEV_USER" 2>/dev/null | wc -l)
  [ "$nnp" -eq 0 ] && check 3.7 PASS "archive sets owned by the device user" "" \
    || check 3.7 FAIL "archive sets owned by the device user" "$nnp entr(ies) not owned by the device user"
else
  check 3.7 SKIP "archive sets owned by the device user" "no archive folder"
fi
nre=$(journalctl -q --no-pager -u kestrel-capture --since "-30 min" 2>/dev/null | grep -c 'agent updated the scan plan' || true)
if [ "$cap_state" != active ]; then
  check 3.8 SKIP "relaunches for a new plan, last 30 min (#27)" "capture not running"
elif [ "$cap_age" -lt 1800 ]; then
  check 3.8 SKIP "relaunches for a new plan, last 30 min (#27)" "capture up only ${cap_age}s; re-run after 30 min ($nre so far)"
elif [ "${nre:-0}" -lt 17 ]; then
  check 3.8 PASS "relaunches for a new plan, last 30 min (#27)" "$nre (beta.2 was 17)"
else
  check 3.8 FAIL "relaunches for a new plan, last 30 min (#27)" "$nre (beta.2 was 17)"
fi
nkd=$(find "$RUNDIR" -maxdepth 1 -type f -name '*.kismet' 2>/dev/null | wc -l)
nnamed=$(find "$RUNDIR" -maxdepth 1 -type f -regextype posix-extended -regex '.*/kestrel-[0-9]+-[0-9]{6}[^/]*\.kismet' 2>/dev/null | wc -l)
if [ "$cap_state" != active ]; then
  check 3.9 SKIP "run dir holds this run's DB + at most the previous (#27)" "capture not running"
elif [ "$nkd" -ge 1 ] && [ "$nkd" -le 2 ] && [ "$nnamed" -eq "$nkd" ]; then
  check 3.9 PASS "run dir holds this run's DB + at most the previous (#27)" "$nkd DB(s), per-launch names"
else
  check 3.9 FAIL "run dir holds this run's DB + at most the previous (#27)" "$nkd DB(s), $nnamed per-launch names"
fi
envstat=$(as_dev /usr/bin/python3 -I -c 'import json,time
try: s=json.load(open("/var/lib/kestrel/capture/capture-status.json"))
except Exception as e: print("unreadable",type(e).__name__); raise SystemExit
now=time.time()
for k in ("scanning","radios_recent","window_s"): print(k,s.get(k))
dt=s.get("data_ts")
print("data_age", "%d s"%(now-dt) if isinstance(dt,(int,float)) else dt)
if isinstance(s.get("radios_recent"),int) and isinstance(dt,(int,float)) and now-dt<900: print("ENV fresh")' 2>&1)
printf '%s\n' "$envstat" | grep -v '^ENV ' | sed 's/^/  /'
if [ "$cap_state" != active ]; then
  check 3.10 SKIP "Environment reading is fresh (#27)" "capture not running"
elif [ "$cap_age" -lt 600 ]; then
  check 3.10 SKIP "Environment reading is fresh (#27)" "capture up only ${cap_age}s; re-run after 10 min"
elif printf '%s\n' "$envstat" | grep -q '^ENV fresh'; then
  check 3.10 PASS "Environment reading is fresh (#27)" "read within 15 min"
else
  check 3.10 FAIL "Environment reading is fresh (#27)" "no fresh reading after ${cap_age}s"
fi

# ---------------------------------------------------------------- 5. model loading (#16)
sec "5. learning-agent model loading (#16)"
echo "agent unit: enabled=$(systemctl is-enabled kestrel-agent 2>/dev/null) active=$(svc_active kestrel-agent) policy=$(cfg agent.policy baseline)"
find "$AGENT_DIR" -maxdepth 1 -name 'model*' -printf '  %f  %s bytes\n' 2>/dev/null | sort
agerr=$(journalctl -b -q --no-pager -u kestrel-agent 2>/dev/null | grep -ciE 'error|traceback|could not load|fall(ing)? ?back' || true)
[ "${agerr:-0}" -eq 0 ] && check 5.1 PASS "no agent load errors this boot" "" \
  || check 5.1 FAIL "agent errors this boot" "$agerr line(s)"
shkbad=""
for f in "$LIBDIR/plugins/backup.py" "$LIBDIR/plugins/remote-train.py" "$CLI"; do
  grep -q 'StrictHostKeyChecking=accept-new' "$f" 2>/dev/null && shkbad="$shkbad $(basename "$f")"
done
[ -z "$shkbad" ] && check 5.2 PASS "no StrictHostKeyChecking=accept-new in installed ssh callers" "" \
  || check 5.2 FAIL "accept-new still present" "$shkbad"
check 5.3 MANUAL "an existing learned model still loads (policy=learned, /agent not baseline)" "by hand"

# ---------------------------------------------------------------- 6. ssh host keys (#16)
sec "6. known SSH host keys (#16)"
for spec in "6.1 backup host port" "6.2 remote-train train_host train_port"; do
  set -- $spec; cid=$1 plugin=$2 hkey=$3 pkey=$4
  en=$(cfg "plugins.$plugin.enabled" false); host=$(cfg "plugins.$plugin.$hkey" ""); port=$(cfg "plugins.$plugin.$pkey" 22)
  if [ "$en" != true ] || [ -z "$host" ]; then
    check "$cid" SKIP "$plugin host key known" "enabled=$en host set=$([ -n "$host" ] && echo yes || echo no)"; continue
  fi
  name=$host; [ "$port" != 22 ] && name="[$host]:$port"
  as_dev ssh-keygen -F "$name" -f "$DEV_HOME/.ssh/known_hosts" >/dev/null 2>&1 \
    && check "$cid" PASS "$plugin host key known" "in the device user's known_hosts" \
    || check "$cid" FAIL "$plugin host key known" "not in known_hosts; re-run 'sudo kestrel $plugin setup'"
done
hkv=$(journalctl -q --no-pager -u kestrel-daemon --since "-7 days" 2>/dev/null | grep -c 'Host key verification failed' || true)
check 6.3 INFO "'Host key verification failed' in the daemon journal, 7 days" "$hkv"

# ---------------------------------------------------------------- 7. uploaders https-only (#18)
sec "7. uploaders: https only (#18)"
for spec in "7.1 wpa-sec https://wpa-sec.stanev.org" "7.2 wigle https://api.wigle.net"; do
  set -- $spec; ep=$(cfg "plugins.$2.endpoint" "$3")
  case "$ep" in https://*) check "$1" PASS "$2 endpoint is https" "enabled=$(cfg plugins.$2.enabled false)" ;;
    *) check "$1" FAIL "$2 endpoint is https" "endpoint not https://" ;; esac
done
hs=$(as_dev "$VENV_PY" -I -c 'from kestrel_pi import httpsafe
try: httpsafe.https_endpoint("http://example.com",""); print("http accepted")
except httpsafe.UnsafeResponse: print("http refused")
try: httpsafe.https_endpoint("https://example.com/",""); print("https accepted")
except httpsafe.UnsafeResponse: print("https refused")' 2>&1)
printf '%s' "$hs" | grep -q 'http refused' && printf '%s' "$hs" | grep -q 'https accepted' \
  && check 7.3 PASS "installed httpsafe refuses http://" "" \
  || check 7.3 FAIL "installed httpsafe refuses http://" "$(printf '%s' "$hs" | tr '\n' ' ' | cut -c1-120)"
if [ -e "$STORE/cracked/wpa-sec.potfile" ] || [ -d "$STORE/cracked" ]; then
  dp=$(stat -c '%U %a' "$STORE/cracked" 2>/dev/null); fp=$(stat -c '%U %a' "$STORE/cracked/wpa-sec.potfile" 2>/dev/null || echo absent)
  { [ "$dp" = "$DEV_USER 700" ] && { [ "$fp" = "$DEV_USER 600" ] || [ "$fp" = absent ]; }; } \
    && check 7.4 PASS "cracked dir 0700, potfile 0600" "dir=$dp potfile=$fp" \
    || check 7.4 FAIL "cracked dir 0700, potfile 0600" "dir=$dp potfile=$fp"
else
  check 7.4 SKIP "cracked dir 0700, potfile 0600" "no cracked results yet"
fi

# ---------------------------------------------------------------- 8. secrets off the command line (#19)
sec "8. secrets off the command line (#19)"
if grep -q -- '--set-stdin' "$CLI" && grep -q 'set-stdin' "$CTL" && grep -q -- '--psk-stdin' "$CLI"; then
  check 8.1 PASS "installed CLI has --set-stdin and --psk-stdin" "hidden prompts are by hand"
else
  check 8.1 FAIL "installed CLI has --set-stdin and --psk-stdin" ""
fi
grep -q 'shell history' "$CLI" && check 8.2 PASS "installed CLI warns about secrets on the command line" "" \
  || check 8.2 FAIL "installed CLI warns about secrets on the command line" ""
since_upd=$(date -u -r "$upd" '+%Y-%m-%d %H:%M:%S' 2>/dev/null || echo "")
sj=$(journalctl -q --no-pager _COMM=sudo ${since_upd:+--since "$since_upd"} 2>/dev/null \
      | grep -E 'COMMAND=' | grep -cE -- '(api_key|api_token|token|password)=|--psk( |=)' || true)
[ "${sj:-0}" -eq 0 ] && check 8.3 PASS "no secrets in sudo journal since the update" "lines not shown" \
  || check 8.3 FAIL "secrets in sudo journal since the update" "$sj line(s) (not shown; rotate journal + change them)"
f=$SCRIPTS_DIR/pull-connectivity-diagnostics.sh
{ [ -f "$f" ] && grep -q '<ssid>' "$f" && grep -q '@<host>' "$f"; } \
  && check 8.4 PASS "installed connectivity diag redacts SSIDs + host" "running it is by hand" \
  || check 8.4 FAIL "installed connectivity diag redacts SSIDs + host" ""

# ---------------------------------------------------------------- 9. one definition for the counts (#30)
sec "9. handshakes / access points / networks, one definition (#30)"
cnt=$(as_dev "$VENV_PY" -I -c 'from kestrel_pi.capture import CaptureInterface
c=CaptureInterface("/var/lib/kestrel/capture")
counts=c.capture_counts()
aps=[a for a in c.captured_access_points() if a.get("bssid")]
print("counts", counts)
print("ap_rows", len(aps))
print("OK" if counts["access_points"]==len(aps) else "MISMATCH")' 2>&1)
printf '%s\n' "$cnt" | grep -v '^OK$\|^MISMATCH$' | sed 's/^/  /'
printf '%s\n' "$cnt" | grep -q '^OK$' \
  && check 9.1 PASS "access points == distinct radios == by-AP row count" "navcard and page agree" \
  || check 9.1 FAIL "access points == distinct radios == by-AP row count" "$(printf '%s' "$cnt" | tr '\n' ' ' | cut -c1-160)"
check 9.2 MANUAL "the e-ink shows PWND / AP / NET and they match the dashboard" "by hand"

# ---------------------------------------------------------------- 10. GPS unplug resilience (#31)
sec "10. GPS unplug does not kill the daemon (#31)"
if [ -n "${pid:-}" ] && [ -r "/proc/$pid/status" ]; then
  sigign=$(awk '/^SigIgn:/{print $2}' "/proc/$pid/status")
  # SIGHUP is signal 1 -> bit 0 of the ignore mask.
  if [ -n "$sigign" ] && [ $(( 0x$sigign & 0x1 )) -ne 0 ]; then
    check 10.1 PASS "running daemon ignores SIGHUP" "SigIgn=$sigign"
  else
    check 10.1 FAIL "running daemon ignores SIGHUP" "SigIgn=${sigign:-?}"
  fi
else
  check 10.1 SKIP "running daemon ignores SIGHUP" "daemon not running"
fi
grep -q 'O_NOCTTY' "$LIBDIR/plugins/gps.py" 2>/dev/null \
  && check 10.2 PASS "installed gps opens the serial port with O_NOCTTY" "" \
  || check 10.2 FAIL "installed gps opens the serial port with O_NOCTTY" ""
check 10.3 MANUAL "unplug the GPS live: card goes to no-signal, daemon + dashboard stay up" "by hand"

# ---------------------------------------------------------------- 11. wigle response crash fix (#32)
sec "11. wigle survives a dict-shaped response (#32)"
grep -q '_first_record' "$LIBDIR/plugins/wigle.py" 2>/dev/null \
  && check 11.1 PASS "installed wigle has the results-shape fix (_first_record)" "" \
  || check 11.1 FAIL "installed wigle has the results-shape fix (_first_record)" ""
wke=$(journalctl -b -q --no-pager -u kestrel-daemon 2>/dev/null | grep -c 'wigle-worker' || true)
[ "${wke:-0}" -eq 0 ] && check 11.2 PASS "no wigle-worker crash this boot" "" \
  || check 11.2 INFO "wigle-worker lines this boot" "$wke (check it is not a crash)"

# ---------------------------------------------------------------- 12. open question
sec "12. passwordless sudo for the device user (deferred review item)"
if sudo -l -U "$DEV_USER" 2>/dev/null | grep -q 'NOPASSWD'; then
  check 12.1 INFO "device user has passwordless sudo" "YES - $(ls /etc/sudoers.d 2>/dev/null | tr '\n' ' ')"
else
  check 12.1 INFO "device user has passwordless sudo" "no - $(ls /etc/sudoers.d 2>/dev/null | tr '\n' ' ')"
fi

# ---------------------------------------------------------------- config tests (throwaway copy)
sec "config tests (throwaway copy of config.toml; commands blocked)"
cfg_sum_before=$(sha256sum "$CONFIG" | awk '{print $1}')
/usr/bin/python3 -I - "$work" <<'PY' 2>&1
import importlib.machinery, importlib.util, os, shutil, subprocess, sys, tomllib
def _blocked(*a, **k): raise RuntimeError("blocked: a config test tried to run a command")
for name in ("run", "Popen", "call", "check_call", "check_output"): setattr(subprocess, name, _blocked)
os.system = _blocked
work = sys.argv[1]; cfg = os.path.join(work, "config.toml")
shutil.copyfile("/etc/kestrel/config.toml", cfg); os.chmod(cfg, 0o600)
os.environ["PWN_CONFIG"] = cfg; os.environ["PWN_CAPTURE_ACK_FILE"] = os.path.join(work, "capture-ack")
loader = importlib.machinery.SourceFileLoader("plugins_ctl", "/usr/local/lib/kestrel/plugins-ctl")
spec = importlib.util.spec_from_loader("plugins_ctl", loader)
ctl = importlib.util.module_from_spec(spec); loader.exec_module(ctl)
ctl.CONFIG = cfg; ctl.ACK_FILE = os.environ["PWN_CAPTURE_ACK_FILE"]
ctl.web_control_ready = lambda config: True
assert os.path.realpath(ctl.CONFIG) != "/etc/kestrel/config.toml"
def read(d, default=None):
    node = tomllib.load(open(cfg, "rb"))
    for p in d.split("."):
        if not isinstance(node, dict) or p not in node: return default
        node = node[p]
    return node
def check(cid, ok, what, detail=""): print("CHECK %-5s %-6s %s :: %s" % (cid, "PASS" if ok else "FAIL", what, detail))
def apply(ch):
    try: return ctl.apply_change(ch)
    except Exception as e: return {"ok": False, "error": "exc %s" % type(e).__name__}
# 2.2: root_only options refused via the socket, then the terminal path sets one
res = []
for opt, val in (("host","https://git.example.com"),("repo","example/kestrel"),("token","example-not-a-token")):
    before = read("plugins.updates."+opt)
    r = apply({"plugin":"updates","set":{opt:val}})
    res.append((not r.get("ok")) and read("plugins.updates."+opt)==before)
check("2.2", all(res), "socket refuses updates repo/host/token", "%d/3 refused" % sum(res))
cur = read("plugins.updates.check_interval_seconds", 21600); want = int(cur)+1
r = apply({"plugin":"updates","set":{"check_interval_seconds":want}})
check("2.3", bool(r.get("ok")) and read("plugins.updates.check_interval_seconds")==want, "socket saves a normal updates option", "ok=%s"%r.get("ok"))
try: rc = ctl.cmd_set("updates","host","https://git.example.com")
except Exception as e: rc = "exc %s"%type(e).__name__
check("2.4", rc==0 and read("plugins.updates.host")=="https://git.example.com", "terminal sets updates.host", "rc=%s"%rc)
# 3.11/3.12: archive_dir + single_file constraints
before = read("capture.housekeeping.archive_dir"); ok=True
for val in ("/","/tmp/kbc-x","/var/lib/kestrel","/var/lib/kestrel/capture/../x"):
    r = apply({"target":"config-set","table":"capture.housekeeping","key":"archive_dir","value":val})
    ok = ok and (not r.get("ok")) and read("capture.housekeeping.archive_dir")==before
r = apply({"target":"config-set","table":"capture.housekeeping","key":"archive_dir","value":"/var/lib/kestrel/capture/archive-kbc"})
check("3.11", ok and bool(r.get("ok")), "archive_dir restricted to the capture store", "")
before = read("capture.output.single_file"); ok=True
for val in ("../x","a/b",".hidden","/etc/passwd"):
    r = apply({"target":"config-set","table":"capture.output","key":"single_file","value":val})
    ok = ok and (not r.get("ok")) and read("capture.output.single_file")==before
r = apply({"target":"config-set","table":"capture.output","key":"single_file","value":"handshakes"})
check("3.12", ok and bool(r.get("ok")), "single_file must be a plain file name", "")
# 4.1: raw-data cap saves under [capture.housekeeping], not [capture]
cur = read("capture.housekeeping.max_run_mb",512); want = 511 if cur!=511 else 513
r = apply({"target":"config-set","table":"capture.housekeeping","key":"max_run_mb","value":want})
rold = apply({"target":"config-set","table":"capture","key":"max_run_mb","value":want})
check("4.1", bool(r.get("ok")) and read("capture.housekeeping.max_run_mb")==want and not rold.get("ok"),
      "raw-data cap saves under [capture.housekeeping] (#22)", "old [capture] key accepted=%s"%rold.get("ok"))
PY
[ "$(sha256sum "$CONFIG" | awk '{print $1}')" = "$cfg_sum_before" ] \
  && check 0.1 PASS "real config.toml untouched by the tests" "checksum identical" \
  || check 0.1 FAIL "real config.toml untouched by the tests" "checksum changed - tell Claude"
stray=$(cfg capture.max_run_mb "")
[ -z "$stray" ] && check 4.2 PASS "no stray [capture] max_run_mb (#22)" "" \
  || check 4.2 FAIL "no stray [capture] max_run_mb (#22)" "[capture] max_run_mb=$stray set and ignored; remove it"

} >"$raw" 2>&1

# ---------------------------------------------------------------- dashboard status (optional)
{
sec "dashboard /status.json (selected fields)"
printf '%s\n' "$web_pw" | /usr/bin/python3 -I - <<'PY' 2>&1
import base64, json, sys, tomllib, urllib.request
pw = sys.stdin.readline().rstrip("\n")
with open("/etc/kestrel/config.toml", "rb") as h:
    cfg = tomllib.load(h)
web = cfg.get("web", {}) or {}
mode = str(web.get("auth", "system")).lower()
port = int(web.get("port", 8080))
req = urllib.request.Request("http://127.0.0.1:%d/status.json" % port)
if mode == "token":
    req.add_header("Authorization", "Bearer %s" % web.get("token", ""))
elif mode in ("basic", "system"):
    if mode == "basic":
        user, pw = str(web.get("username", "")), str(web.get("password", ""))
    else:
        import subprocess
        user = str(web.get("username") or subprocess.run(
            ["systemctl", "show", "-p", "User", "--value", "kestrel-daemon.service"],
            capture_output=True, text=True).stdout.strip())
    if not pw:
        print("(skipped: no password given)")
        sys.exit(0)
    req.add_header("Authorization", "Basic " + base64.b64encode(
        ("%s:%s" % (user, pw)).encode()).decode())
try:
    s = json.load(urllib.request.urlopen(req, timeout=15))
except Exception as exc:
    print("(not available: %s)" % type(exc).__name__)
    sys.exit(0)
print("version:", s.get("version"))
cap = s.get("capture", {}) or {}
print("capture:", {k: cap.get(k) for k in ("state", "enabled", "hashline_files")})
print("environment:", s.get("environment"))
ag = s.get("agent", {}) or {}
print("agent:", {k: v for k, v in ag.items() if isinstance(v, (str, int, float, bool)) or v is None})
st = ag.get("status") if isinstance(ag.get("status"), dict) else {}
print("agent status:", {k: v for k, v in st.items() if isinstance(v, (str, int, float, bool))})
for p in s.get("plugins", []):
    keep = {k: p.get(k) for k in ("state", "reason", "last_error", "warnings", "overruns",
                                   "capability_violations") if p.get(k) not in (None, "", [], 0)}
    print("plugin %-13s %s" % (p.get("name"), keep))
w = s.get("widgets", {}) or {}
for name in ("wpa-sec", "wigle", "backup", "remote-train", "updates"):
    if name in w:
        els = w[name]
        if isinstance(els, dict):
            els = list(els.values())
        vals = ["%s=%s" % (e.get("label"), e.get("value")) for e in els if isinstance(e, dict)]
        print("widget %-13s %s" % (name, ", ".join(vals)))
PY
} >>"$raw" 2>&1
web_pw=""

cat >>"$raw" <<'EOF'

===== by hand (the script can't see these) =====
1.x  with the updates plugin's beta OFF, this beta is NOT offered; with beta ON
     it is offered and applies in place
2.1  the Plugins page shows updates repo/host/token read-only (terminal only)
3.x  a new handshake from your own test AP is saved (dashboard count goes up)
3.10 the Environment card shows a radios-in-range count, not "unknown"
5.3  an existing learned model still loads (/agent shows learned, not baseline)
6.x  re-run setup on a failing host / set up a new host
7.x  a real wpa-sec / wigle upload of your own test capture
8.1  the hidden prompts, the --set/--psk warnings printing, Wi-Fi via --psk-stdin
9.2  the e-ink shows PWND / AP / NET and they match the dashboard numbers
10.3 unplug the GPS live: the card goes to no-signal and the daemon + dashboard
     stay up (the regression fixed in beta.4)
12   the failed NetworkManager-wait-online unit: new, or present on 1.0.5 too?
     fresh-flash smoke test; how the dashboard and /guide look on a phone
EOF

# ---------------------------------------------------------------- redact and save
# Written as the SSH login user into that user's private temp folder.
as_login() {
  if [ "$LOGIN_USER" = "root" ]; then "$@"; else
    setpriv --reuid="$LOGIN_USER" --regid="$LOGIN_USER" --init-groups --reset-env -- "$@"; fi
}
/usr/bin/python3 -I - "$raw" "$DEV_USER" "$LOGIN_USER" <<'PY' | as_login sh -c 'umask 077; cat > "$1"' _ "$OUT_DIR/report.txt"
import importlib.machinery, importlib.util, re, socket, sys, tomllib
text = open(sys.argv[1], encoding="utf-8", errors="replace").read()
try:
    with open("/etc/kestrel/config.toml", "rb") as h:
        cfg = tomllib.load(h)
except Exception:
    cfg = {}
# Values a plugin declares as its default are public (upstream endpoints, the
# project's own release repo); anything else configured is the operator's own.
public = set()
try:
    loader = importlib.machinery.SourceFileLoader("plugins_ctl", "/usr/local/lib/kestrel/plugins-ctl")
    spec = importlib.util.spec_from_loader("plugins_ctl", loader)
    ctl = importlib.util.module_from_spec(spec)
    loader.exec_module(ctl)
    for decl, _src in ctl.static_plugins().values():
        for opt in (decl.get("options") or {}).values():
            if isinstance((opt or {}).get("default"), str):
                public.add(opt["default"])
except Exception:
    pass
secret_keys = {"api_key", "api_token", "api_name", "token", "password", "username"}
host_keys = {"host", "train_host", "user", "train_user", "probe_url", "endpoint",
             "repo", "path", "train_path"}
subs = []
def walk(node):
    for k, v in node.items():
        if isinstance(v, dict):
            walk(v)
        elif isinstance(v, str) and len(v) >= 3 and v not in public:
            if k in secret_keys:
                subs.append((v, "<secret>"))
            elif k in host_keys:
                subs.append((v, "<host>"))
walk(cfg)
try:
    hostname = socket.gethostname()
except Exception:
    hostname = ""
for value, tag in sorted(subs, key=lambda s: -len(s[0])):
    if value:
        text = text.replace(value, tag)
# The hostname often matches a path or project name (/var/lib/<name>), so only a
# standalone token is masked, never part of a path, URL or file name.
if hostname:
    text = re.sub(r"(?<![\w./-])%s(?![\w./-])" % re.escape(hostname), "<hostname>", text)
for name in set(sys.argv[2:]):
    if name and name != "root":
        text = re.sub(r"(?<![\w-])%s(?![\w-])" % re.escape(name), "<user>", text)
text = re.sub(r"\b([0-9A-Fa-f]{2}[:-]){5}[0-9A-Fa-f]{2}\b", "<mac>", text)
text = re.sub(r"\b(?:\d{1,3}\.){3}\d{1,3}\b", "<ipv4>", text)
text = re.sub(r"(?<![\w:])(?:[0-9A-Fa-f]{1,4}:)*[0-9A-Fa-f]{0,4}::(?:[0-9A-Fa-f]{1,4}:)*[0-9A-Fa-f]{1,4}(?![\w:])", "<ipv6>", text)
text = re.sub(r"\b(?:[0-9A-Fa-f]{1,4}:){3,7}[0-9A-Fa-f]{1,4}\b", "<ipv6>", text)
# Coordinates: a "lat, lon" pair, or a number after a lat/lon/latitude/longitude
# label. Other decimals (rewards, timings) are left alone.
text = re.sub(r"-?\b\d{1,3}\.\d{4,}\s*,\s*-?\d{1,3}\.\d{4,}\b", "<coord>, <coord>", text)
text = re.sub(r"(?i)\b(lat|lon|lng|latitude|longitude)([^\w-]{1,4})-?\d{1,3}\.\d+", r"\1\2<coord>", text)
text = re.sub(r"/var/lib/kestrel/capture/[^\n]*?\.(hc22000|pcapng|kismet)", r"<store>/<file>.\1", text)
sys.stdout.write(text)
PY
echo "kbc: report written"
