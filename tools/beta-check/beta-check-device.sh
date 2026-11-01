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
# readout, redaction) stay; the per-release checks below are this beta's.

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
: "$CLI" "$CTL" "$AGENT_DIR" "$VENV_PY" "$SCRIPTS_DIR" "$LEGACY_RUN" "$RUNDIR"

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
# KESTREL_WEB_PASSWORD, when set (e.g. forwarded by pull-beta-check.sh over
# SSH's env channel, never on a command line), is used as-is and the prompt is
# skipped, so the fetch can run unattended. Capture it into web_pw and drop it
# from the environment so the many child processes below don't inherit it.
web_auth=$(cfg web.auth system)
web_pw="${KESTREL_WEB_PASSWORD:-}"
unset KESTREL_WEB_PASSWORD 2>/dev/null || true
if [ -z "$web_pw" ] && [ "$web_auth" = "system" ] && [ -t 0 ]; then
  echo "Optional: the dashboard's /status.json adds plugin health, agent and"
  echo "environment state to the report. Type the device login password"
  echo "(or set KESTREL_WEB_PASSWORD to skip this prompt)."
  printf 'Type it to include it (input hidden), or press Enter to skip: '
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

# ================================================================ 1.0.12-beta.1 checks
# 1.0.12-beta.1 is an in-place update on top of 1.0.11: remote-train 0.2.0 (an
# unreachable host, the remote-training service; #129) as section 2, and the
# census plugin (#130, #131) as section 3. Sections 4-7 repeat 1.0.11's checks
# (its beta 3, beta 2, beta 1 and 1.0.10 sections) as regressions. The CHECK
# ids match the checklist issue for 1.0.12-beta.1.
upd=/var/lib/kestrel/update-run/status.json
cap_on=$(svc_active kestrel-capture.service)
echo "capture service: $cap_on"

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

check 1.6 MANUAL "see the checklist" "by hand (the beta channel offer)"

since=""
[ -f "$upd" ] && since="@$(stat -c %Y "$upd")"
# ---------------------------------------------------------------- 2. remote-train 0.2.0 (#129)
sec "2. remote-train 0.2.0: an unreachable host, and the remote-training service (#129)"
rt=$LIBDIR/plugins/remote-train.py
rt_ver=$(sed -n 's/^ *__version__ = "\([0-9.]*\)".*/\1/p' "$rt" 2>/dev/null | head -1)
if [ "$rt_ver" = "0.2.0" ] && grep -q '^ *__provides__ = ("remote-training",)' "$rt" \
    && grep -q 'def provide_remote_training' "$rt"; then
  check 2.1 PASS "remote-train 0.2.0 is installed and provides remote-training" ""
else
  check 2.1 FAIL "remote-train 0.2.0 is installed and provides remote-training" "version ${rt_ver:-?}"
fi
# 2.2: the last contact and last model survive a restart, in a status file in
# the agent's own folder (read as the device user; ages only, no host).
if [ "$(cfg plugins.remote-train.enabled false)" != true ]; then
  check 2.2 SKIP "remote-train keeps its last contact and model in remote-status.json" "remote-train not enabled"
else
  rs=$(as_dev /usr/bin/python3 -I - "$AGENT_DIR/.remote-staging/remote-status.json" <<'PY' 2>&1
import json, os, stat, sys, time
path = sys.argv[1]
try:
    st = os.lstat(path)
    data = json.load(open(path, encoding="utf-8"))
except FileNotFoundError:
    print("missing"); sys.exit(0)
except Exception as exc:
    print("unreadable", type(exc).__name__); sys.exit(0)
def age(v):
    return "never" if not isinstance(v, (int, float)) else "%dh ago" % ((time.time() - v) // 3600)
print("ok" if stat.S_ISREG(st.st_mode) and isinstance(data, dict) else "bad",
      "last contact %s, last model %s (%s)" % (age(data.get("last_contact")),
      age(data.get("last_model")), data.get("last_model_algorithm") or "-"))
PY
)
  case "$rs" in
    ok\ *) check 2.2 PASS "remote-train keeps its last contact and model in remote-status.json" "${rs#ok }" ;;
    missing) check 2.2 INFO "remote-train keeps its last contact and model in remote-status.json" "not written yet (no SSH session since the update?)" ;;
    *) check 2.2 FAIL "remote-train keeps its last contact and model in remote-status.json" "$rs" ;;
  esac
fi
check 2.3 MANUAL "see the checklist" "by hand (an unreachable host on the card)"
check 2.4 MANUAL "see the checklist" "by hand (the card after a daemon restart)"

# ---------------------------------------------------------------- 3. the census plugin (#130, #131)
sec "3. the census plugin (#130, #131)"
bad=""
for f in census.py _census_dht.py _census_proto.py; do
  st=$(stat -c '%U:%G %a' "$LIBDIR/plugins/$f" 2>/dev/null || echo missing)
  [ "$st" = "root:root 644" ] || bad="$bad $f=$st"
done
[ -z "$bad" ] && check 3.1 PASS "census and its two helper files are installed, root-owned 0644" "" \
  || check 3.1 FAIL "census and its two helper files are installed, root-owned 0644" "$bad"
# 3.2 and 3.3 are in the config tests below; 3.4 and 3.9 come from the dashboard.
census_on=$(cfg plugins.census.enabled false)
census_tether=$(cfg plugins.census.tether_only true)
echo "census: enabled=$census_on tether_only=$census_tether"
cstate=$DEV_HOME/.local/state/kestrel-census
if [ "$census_on" != true ]; then
  for id in 3.5 3.6 3.7 3.8; do
    check "$id" SKIP "census state and traffic" "census not enabled"
  done
else
  # 3.5-3.7: its state, read as the device user. Counts, hours and modes only:
  # never a token, slot or node address.
  as_dev /usr/bin/python3 -I - "$cstate" "$(cfg plugins.census.daily_mb 5)" <<'PY' 2>&1
import json, os, stat, sys, time
d, budget_mb = sys.argv[1], sys.argv[2]
def chk(cid, result, what, detail=""):
    print("CHECK %-5s %-6s %s :: %s" % (cid, result, what, detail))
try:
    st = os.lstat(d)
except FileNotFoundError:
    for cid in ("3.5", "3.6", "3.7"):
        chk(cid, "INFO", "census state", "no state folder yet (paused since it was enabled?)")
    sys.exit(0)
files = sorted(n for n in os.listdir(d) if not n.endswith(".tmp"))
modes = {n: oct(os.lstat(os.path.join(d, n)).st_mode & 0o777) for n in files}
ok = stat.S_IMODE(st.st_mode) == 0o700 and all(m == "0o600" for m in modes.values())
chk("3.5", "PASS" if ok else "FAIL", "the census state folder is 0700 and its files 0600",
    "folder %s; %s" % (oct(stat.S_IMODE(st.st_mode)),
                       ", ".join("%s %s" % (n, m) for n, m in modes.items())))
def load(name):
    try:
        return json.load(open(os.path.join(d, name), encoding="utf-8"))
    except Exception:
        return None
t = time.gmtime()
today, hour = time.strftime("%Y-%m-%d", t), t.tm_hour
day = load("day.json") or {}
wrote = day.get("wrote") if day.get("day") == today else None
used = (day.get("bytes") or 0) if day.get("day") == today else 0
try:
    budget = int(budget_mb) * 1024 * 1024
except ValueError:
    budget = 5 * 1024 * 1024
use = "%d KB used today%s" % (used // 1024, " of %d MB" % (budget >> 20) if budget else "")
if isinstance(wrote, int) and wrote in (hour, hour - 1):
    chk("3.6", "PASS", "the census reported this hour or the last", "last report hour %02d UTC; %s" % (wrote, use))
else:
    chk("3.6", "INFO", "the census reported this hour or the last",
        "last report hour %s; %s" % (wrote if wrote is not None else "none today", use))
seen = ((load("seen.json") or {}).get("days") or {}).get(today) or {}
read_at = seen.get("read_at")
entries = seen.get("entries") or {}
mine = day.get("token") in entries if day.get("day") == today else False
if not isinstance(read_at, (int, float)):
    chk("3.7", "INFO", "the census was read today and this device is in it", "not read yet today")
else:
    chk("3.7", "PASS" if mine else "FAIL", "the census was read today and this device is in it",
        "read %d min ago: %d devices seen, %s online; this device %s"
        % ((time.time() - read_at) // 60, len(entries), seen.get("online"),
           "is in it" if mine else "is NOT in it"))
PY
  # 3.8: with tether_only, the daemon's census socket is bound to the tether.
  # Counts only: no socket address is printed.
  dpid=$(systemctl show -p MainPID --value kestrel-daemon.service 2>/dev/null)
  # A Bluetooth PAN link has no carrier state, so a working tether reads
  # "unknown": count it as up when the interface itself is up (IFF_UP).
  tether_up=$(cat /sys/class/net/bnep0/operstate 2>/dev/null || echo absent)
  tether_flags=$(cat /sys/class/net/bnep0/flags 2>/dev/null || echo 0)
  if [ "$tether_up" = unknown ] && [ $((tether_flags & 1)) -eq 1 ]; then
    tether_up=up
  fi
  bound=$(ss -uanp 2>/dev/null | grep "pid=${dpid:-none}," | grep -c '%bnep0:' || true)
  if [ "$census_tether" != true ]; then
    check 3.8 SKIP "the census socket is bound to the tether" "tether_only is off"
  elif [ "$tether_up" != up ]; then
    check 3.8 SKIP "the census socket is bound to the tether" "bnep0 is $tether_up"
  elif [ "${bound:-0}" -ge 1 ]; then
    check 3.8 PASS "the census socket is bound to the tether" "$bound daemon UDP socket(s) on bnep0"
  else
    check 3.8 FAIL "the census socket is bound to the tether" "no daemon UDP socket on bnep0 (paused, or between cycles?)"
  fi
fi
check 3.10 MANUAL "see the checklist" "by hand (the tether off and on)"
check 3.11 MANUAL "see the checklist" "by hand (the desktop census report)"

# ---------------------------------------------------------------- 4. regressions from 1.0.11: AngryOxide stopped cleanly (#126)
sec "4. regressions from 1.0.11: AngryOxide stopped cleanly (#126)"
# The unit: systemd signals only the launcher, with time for a clean stop.
km=$(systemctl show -p KillMode --value kestrel-capture.service 2>/dev/null)
ts=$(systemctl show -p TimeoutStopUSec --value kestrel-capture.service 2>/dev/null)
echo "  kestrel-capture: KillMode=$km TimeoutStopUSec=$ts"
if [ "$km" = mixed ] && [ -n "$ts" ] && [ "$ts" != infinity ]; then
  check 4.1 PASS "the capture unit signals only the launcher, with a stop timeout" "KillMode=$km, TimeoutStopUSec=$ts"
else
  check 4.1 FAIL "the capture unit signals only the launcher, with a stop timeout" "KillMode=$km, TimeoutStopUSec=$ts"
fi
# The running AngryOxide catches SIGINT (its handler is installed and SIGINT
# isn't ignored): the signal masks from /proc only.
ao=$(pgrep -x angryoxide | head -1)
if [ -z "$ao" ]; then
  check 4.2 SKIP "the running AngryOxide catches SIGINT" "AngryOxide is not running"
else
  cgt=$(awk '/^SigCgt:/ {print $2}' "/proc/$ao/status" 2>/dev/null)
  ign=$(awk '/^SigIgn:/ {print $2}' "/proc/$ao/status" 2>/dev/null)
  echo "  angryoxide signal masks: SigCgt=$cgt SigIgn=$ign"
  if [ -n "$cgt" ] && [ $(( 0x$cgt & 2 )) -ne 0 ] && [ $(( 0x${ign:-0} & 2 )) -eq 0 ]; then
    check 4.2 PASS "the running AngryOxide catches SIGINT" "SIGINT caught, not ignored"
  else
    check 4.2 FAIL "the running AngryOxide catches SIGINT" "SigCgt=$cgt SigIgn=$ign"
  fi
fi
# The capture unit's own lines since the update: how each AngryOxide run ended.
# Only the exit codes and counts are read (the unit's other lines can carry
# network names).
clog=$work/capture-since.log
if [ -n "${since:-}" ]; then
  journalctl -q --no-pager -u kestrel-capture --since "$since" >"$clog" 2>/dev/null
else
  journalctl -q --no-pager -b -u kestrel-capture >"$clog" 2>/dev/null
fi
stops=$(grep -oE 'stop requested; capture exiting \(AngryOxide rc=[0-9]+' "$clog" | grep -oE '[0-9]+$' | tr '\n' ' ')
relaunches=$(grep -oE '(restarting for the agent.s new scan plan|stopped to roll the run dir) \(rc=[0-9]+' "$clog" | grep -oE '[0-9]+$' | tr '\n' ' ')
killed=$(grep -c "didn't stop within" "$clog" || true)
echo "  since the update: stop exit codes: ${stops:-none}; relaunch/roll exit codes: ${relaunches:-none}; killed after the timeout: $killed"
left=$(find "$RUNDIR" -maxdepth 1 -name '*.kismet-journal' 2>/dev/null | wc -l)
if [ "$cap_on" = active ]; then
  check 4.3 SKIP "the last stop was clean and left no kismet-journal" "capture is running (run once with it off)"
elif [ -z "$stops" ]; then
  check 4.3 SKIP "the last stop was clean and left no kismet-journal" "no stop since the update"
else
  last=${stops% }; last=${last##* }
  if [ "$last" = 0 ] && [ "$left" -eq 0 ]; then
    check 4.3 PASS "the last stop was clean and left no kismet-journal" "rc=0, no journal left"
  else
    check 4.3 FAIL "the last stop was clean and left no kismet-journal" "rc=$last, $left journal file(s) left"
  fi
fi
if [ -z "$relaunches" ] && [ "${killed:-0}" -eq 0 ]; then
  check 4.4 SKIP "every relaunch and roll since the update stopped AngryOxide cleanly" "none since the update"
elif [ "${killed:-0}" -eq 0 ] && [ -z "$(printf '%s\n' $relaunches | grep -vx 0 || true)" ]; then
  check 4.4 PASS "every relaunch and roll since the update stopped AngryOxide cleanly" "exit codes: $relaunches"
else
  check 4.4 FAIL "every relaunch and roll since the update stopped AngryOxide cleanly" "exit codes: ${relaunches:-none}; killed: $killed"
fi
check 4.5 MANUAL "see the checklist" "by hand (stop and start capture from the dashboard)"

# ---------------------------------------------------------------- 5. regressions from 1.0.11-beta.2: fair model contests (#120)
sec "5. regressions from 1.0.11-beta.2: fair model contests (#120)"
# The contest code, imported as the device user (no torch: model loads are lazy).
fair=$(as_dev "$VENV_PY" -I -c 'from kestrel_pi.agent import controller, metrics, train
print(metrics.HOLDOUT_SCHEME, hasattr(metrics, "contest_decision"),
      hasattr(controller.AgentController, "_run_contest"), hasattr(train, "evaluate"),
      hasattr(metrics, "promotion_decision"))' 2>&1 | tail -1)
echo "  contest code: $fair"
if [ "$fair" = "block-hash-v1 True True True False" ]; then
  check 5.1 PASS "the fair-contest code is installed and the old promotion rule is gone" "$fair"
else
  check 5.1 FAIL "the fair-contest code is installed and the old promotion rule is gone" "$(printf '%s' "$fair" | cut -c1-120)"
fi
# The agent's status (pi-owned, read as the device user): the tally's shape and
# counts, and whether a last decision is published. No model or row is read.
tally=$(as_dev /usr/bin/python3 -I - "$AGENT_DIR/status.json" <<'PY' 2>&1 | tail -1
import json, sys, time
try:
    st = json.load(open(sys.argv[1]))
except FileNotFoundError:
    print("none"); sys.exit(0)
except Exception as exc:
    print("unreadable(%s)" % type(exc).__name__); sys.exit(0)
t = st.get("promotion_tally") or {}
new = all(isinstance(t.get(k), dict) for k in ("local", "remote"))
ld = st.get("last_decision")
print("shape=%s local=%s remote=%s before=%s last_decision=%s age=%ds" % (
    "contests" if new else "flat", t.get("local"), t.get("remote"),
    t.get("before_fair_promotion"), (ld or {}).get("result") if isinstance(ld, dict) else ld,
    int(time.time() - float(st.get("updated") or 0))))
PY
)
echo "  agent status: $tally"
case "$tally" in
  none|unreadable*) check 5.2 SKIP "the agent publishes the contest tally and last decision" "no agent status" ;;
  shape=contests*) check 5.2 PASS "the agent publishes the contest tally and last decision" "$tally" ;;
  *) check 5.2 FAIL "the agent publishes the contest tally and last decision" "$tally" ;;
esac
# Since the update: on-device runs that finished, and the contests that followed.
alog=$work/agent-since.log
if [ -n "$since" ]; then
  journalctl -q --no-pager -u kestrel-agent --since "$since" >"$alog" 2>/dev/null
else
  journalctl -q --no-pager -b -u kestrel-agent >"$alog" 2>/dev/null
fi
runs=$(grep -c 'training finished' "$alog" || true)
contests=$(grep -c 'contest (' "$alog" || true)
deferred=$(grep -c ') deferred' "$alog" || true)
promoted=$(grep -c 'promoted the .* candidate' "$alog" || true)
echo "  since the update: $runs on-device run(s), $contests contest line(s) ($deferred deferred, $promoted promotion(s))"
grep -E 'contest \(' "$alog" | tail -4 | jscrub
if [ "${runs:-0}" -eq 0 ] && [ "${contests:-0}" -eq 0 ]; then
  check 5.3 SKIP "each on-device run since the update was followed by a contest" "no run or contest yet"
elif [ "${contests:-0}" -ge "${runs:-0}" ] && [ "${contests:-0}" -ge 1 ]; then
  check 5.3 PASS "each on-device run since the update was followed by a contest" "$runs run(s), $contests contest line(s)"
else
  check 5.3 FAIL "each on-device run since the update was followed by a contest" "$runs run(s), $contests contest line(s)"
fi
# remote-train: the host trains the agent's own algorithm. Algorithm names and
# counts only; the host is never named.
if [ "$(cfg plugins.remote-train.enabled false)" != true ]; then
  check 5.4 SKIP "the training host trains the agent's own algorithm" "remote-train not enabled"
else
  algo=$(cfg agent.algorithm RecurrentPPO)
  launched=$(journalctl -q --no-pager -u kestrel-daemon ${since:+--since "$since"} 2>/dev/null \
    | grep -c "remote-train: launched $algo on the host" || true)
  slugs=$(as_dev /usr/bin/python3 -I -c 'import json, sys
try:
    print(" ".join(sorted(json.load(open(sys.argv[1])))))
except Exception:
    print("")' "$AGENT_DIR/.remote-staging/host-models.json" 2>/dev/null)
  echo "  remote: $launched launch(es) of $algo since the update; host models seen: ${slugs:-none}"
  slug=$(printf '%s' "$algo" | tr 'A-Z' 'a-z' | tr -c 'a-z0-9-\n' '-')
  if [ "${launched:-0}" -ge 1 ] || printf ' %s ' "$slugs" | grep -q " $slug "; then
    check 5.4 PASS "the training host trains the agent's own algorithm" "$algo: $launched launch(es); host models: ${slugs:-none}"
  else
    check 5.4 INFO "the training host trains the agent's own algorithm" "not yet ($algo launches once enough new rows have been logged)"
  fi
fi
# 5.5 comes from the dashboard section below.
check 5.6 MANUAL "see the checklist" "by hand (a day of capture, then the diagnostics script)"

# ---------------------------------------------------------------- 5. regressions from 1.0.11-beta.2: every desktop script in the image (#121)
sec "5. regressions from 1.0.11-beta.2: every desktop script in the image (#121)"
want="README.md:root:644 gps-check.sh:root:755 pull-agent-diagnostics.sh:root:755 pull-connectivity-diagnostics.sh:root:755 pull-eink-diagnostics.sh:root:755 pull-health.sh:root:755 pull-web-diagnostics.sh:root:755"
have=$(cd "$SCRIPTS_DIR" 2>/dev/null && for f in *; do [ -f "$f" ] && printf '%s:%s ' "$f" "$(stat -c '%U:%a' "$f")"; done)
echo "  installed: ${have:-none}"
if [ "$(printf '%s\n' $have | sort | tr '\n' ' ')" = "$(printf '%s\n' $want | sort | tr '\n' ' ')" ]; then
  check 5.7 PASS "every desktop script is installed, .sh 0755 and the README 0644" "7 files"
else
  check 5.7 FAIL "every desktop script is installed, .sh 0755 and the README 0644" "see the list above"
fi
check 5.8 MANUAL "see the checklist" "by hand (runs on a computer)"

# ---------------------------------------------------------------- 5. regressions from 1.0.11-beta.2: the release version on the dashboard (#123)
sec "5. regressions from 1.0.11-beta.2: the release version on the dashboard (#123)"
# 5.9 and 5.10 come from the dashboard section below.

# ---------------------------------------------------------------- 6. regressions from 1.0.11-beta.1: AngryOxide v0.9.4 (#111)
sec "6. regressions from 1.0.11-beta.1: AngryOxide v0.9.4 (#111)"
# --version only: it opens no interface. A test build set through ANGRYOXIDE in
# the capture service (a systemd drop-in) is noted; the stock binary is checked.
aov=$(timeout 10 /usr/bin/angryoxide --version 2>/dev/null | head -1)
ao_env=$(systemctl show kestrel-capture -p Environment --value 2>/dev/null | tr ' ' '\n' | grep -c '^ANGRYOXIDE=' || true)
echo "  /usr/bin/angryoxide: ${aov:-no answer}   ANGRYOXIDE set in the capture service: $ao_env"
case "$aov" in
  *" 0.9.4"|*" 0.9.4 "*) check 6.1 PASS "AngryOxide is v0.9.4" "$aov" ;;
  *) check 6.1 FAIL "AngryOxide is v0.9.4" "${aov:-no answer}" ;;
esac
# How far the kismet database's newest row trails AngryOxide's capture file (its
# last write), as pull-health.sh measures it, so a quiet spot isn't read as lag.
# One read-only query; no row content is read. The run folder is root-owned.
if [ "$cap_on" = active ]; then
  lag=$(/usr/bin/python3 -I - "$RUNDIR" <<'PY' 2>&1 | tail -1
import glob, os, sqlite3, sys
dbs = glob.glob(os.path.join(sys.argv[1], "*.kismet"))
caps = glob.glob(os.path.join(sys.argv[1], "*.pcapng"))
if not dbs or not caps:
    print("nodb")
    sys.exit(0)
db = max(dbs, key=os.path.getmtime)
ref = max(os.path.getmtime(c) for c in caps)
try:
    c = sqlite3.connect("file:%s?mode=ro" % db, uri=True, timeout=5)
    rows, newest = c.execute("SELECT COUNT(*), MAX(ts_sec) FROM packets").fetchone()
    print("rows=%d lag=%d" % (rows or 0, max(0, int(ref - newest)) if newest else -1))
except Exception as exc:
    print("unreadable(%s)" % type(exc).__name__)
PY
)
  echo "  kismet database (lag = seconds behind the capture file): $lag"
  lag_s=$(printf '%s' "$lag" | sed -nE 's/.*lag=(-?[0-9]+).*/\1/p')
  if [ -n "$lag_s" ] && [ "$lag_s" -ge 0 ] && [ "$lag_s" -le 60 ]; then
    check 6.2 PASS "the kismet database keeps up with capture" "$lag"
  elif [ -n "$lag_s" ] && [ "$lag_s" -gt 60 ]; then
    check 6.2 FAIL "the kismet database keeps up with capture" "$lag"
  else
    check 6.2 INFO "the kismet database keeps up with capture" "$lag"
  fi
  rss=$(ps -o rss= -C angryoxide 2>/dev/null | awk '{s+=$1} END {print s+0}')
  total=$(awk '/MemTotal/ {print $2}' /proc/meminfo)
  pct=$(( rss * 100 / (total > 0 ? total : 1) ))
  echo "  AngryOxide memory: $((rss / 1024)) MB of $((total / 1024)) MB ($pct%)"
  if [ "$rss" -eq 0 ]; then
    check 6.3 INFO "AngryOxide uses under 30% of RAM" "not running right now"
  elif [ "$pct" -lt 30 ]; then
    check 6.3 PASS "AngryOxide uses under 30% of RAM" "$((rss / 1024)) MB, $pct%"
  else
    check 6.3 FAIL "AngryOxide uses under 30% of RAM" "$((rss / 1024)) MB, $pct%"
  fi
else
  check 6.2 SKIP "the kismet database keeps up with capture" "capture is off"
  check 6.3 SKIP "AngryOxide uses under 30% of RAM" "capture is off"
fi
check 6.4 MANUAL "see the checklist" "by hand (an hour somewhere busy, if not done on beta 1)"

# ---------------------------------------------------------------- 6. regressions from 1.0.11-beta.1: Environment from the capture file (#108)
sec "6. regressions from 1.0.11-beta.1: Environment from the capture file (#108)"
envst=$(as_dev /usr/bin/python3 -I - "$STORE" <<'PY' 2>&1 | tail -1
import json, os, sys, time
try:
    s = json.load(open(os.path.join(sys.argv[1], "capture-status.json")))
    ts = s.get("data_ts")
    print("scanning=%s radios=%s data_age=%s" % (
        s.get("scanning"), s.get("radios_recent", "-"),
        int(time.time() - ts) if isinstance(ts, (int, float)) else "-"))
except FileNotFoundError:
    print("none")
except Exception as exc:
    print("unreadable(%s)" % type(exc).__name__)
PY
)
echo "  capture-status.json: $envst"
age=$(printf '%s' "$envst" | sed -nE 's/.*data_age=([0-9]+).*/\1/p')
if [ "$cap_on" != active ]; then
  check 6.5 SKIP "capture-status.json has a fresh radio count" "capture is off"
elif [ -n "$age" ] && [ "$age" -le 120 ] && printf '%s' "$envst" | grep -q 'radios=[0-9]'; then
  check 6.5 PASS "capture-status.json has a fresh radio count" "$envst"
else
  check 6.5 FAIL "capture-status.json has a fresh radio count" "$envst"
fi
# 6.6 comes from the dashboard section below.

# ---------------------------------------------------------------- 6. regressions from 1.0.11-beta.1: eink removed (#112)
sec "6. regressions from 1.0.11-beta.1: eink removed (#112)"
left=""
for f in eink.py _eink_epdconfig.py _eink_epd2in7.py _eink_epd2in7_V2.py; do
  [ -e "$LIBDIR/plugins/$f" ] && left="$left $f"
done
miss=""
for f in eink-ws27.py _eink-ws27_epdconfig.py _eink-ws27_epd2in7.py _eink-ws27_epd2in7_V2.py; do
  [ -f "$LIBDIR/plugins/$f" ] || miss="$miss $f"
done
if [ -z "$left$miss" ]; then
  check 6.7 PASS "eink and its helpers are gone; eink-ws27 and its helpers are installed" ""
else
  check 6.7 FAIL "eink and its helpers are gone; eink-ws27 and its helpers are installed" \
    "${left:+still there:$left }${miss:+missing:$miss}"
fi
# 6.8, 6.9 and 6.12 read config.toml's table names and the declarations in the
# config tests below; 6.10 and 6.13 come from the dashboard section.

# ---------------------------------------------------------------- 6. regressions from 1.0.11-beta.1: ups-hat removed (#113)
sec "6. regressions from 1.0.11-beta.1: ups-hat removed (#113)"
if [ ! -e "$LIBDIR/plugins/ups-hat.py" ] && [ -f "$LIBDIR/plugins/ups-ws.py" ]; then
  check 6.11 PASS "ups-hat is gone; ups-ws is installed" ""
else
  check 6.11 FAIL "ups-hat is gone; ups-ws is installed" \
    "ups-hat.py $( [ -e "$LIBDIR/plugins/ups-hat.py" ] && echo present || echo gone), ups-ws.py $( [ -f "$LIBDIR/plugins/ups-ws.py" ] && echo present || echo missing)"
fi

# ---------------------------------------------------------------- 6. regressions from 1.0.11-beta.1: per-ap by default (#116)
sec "6. regressions from 1.0.11-beta.1: per-ap by default (#116)"
mode=$(cfg capture.output.mode per-ap)
if [ "$mode" = per-ap ]; then
  check 6.14 PASS "[capture.output] mode is per-ap" ""
else
  check 6.14 INFO "[capture.output] mode is per-ap" "it is $mode (changed after the flash?)"
fi
# File names hold network names, so only how many follow each layout is
# reported, never a name.
layout=$(as_dev /usr/bin/python3 -I - "$STORE" <<'PY' 2>&1 | tail -1
import os, re, sys
per_ap = re.compile(r".+_[0-9A-Fa-f]{12}\.hc22000$")
hidden = re.compile(r"__hidden_[0-9A-Fa-f]{12}\.hc22000$")
n_ap = n_other = 0
try:
    names = os.listdir(sys.argv[1])
except FileNotFoundError:
    names = []
for n in names:
    if not n.endswith(".hc22000") or n in ("unknown.hc22000", "all.hc22000"):
        continue
    if per_ap.match(n) or hidden.match(n):
        n_ap += 1
    else:
        n_other += 1
print("per_ap=%d other=%d" % (n_ap, n_other))
PY
)
echo "  store files: $layout"
n_ap=$(printf '%s' "$layout" | sed -nE 's/.*per_ap=([0-9]+).*/\1/p')
n_other=$(printf '%s' "$layout" | sed -nE 's/.*other=([0-9]+).*/\1/p')
if [ "$mode" != per-ap ]; then
  check 6.16 SKIP "the store's files are named per access point" "mode is $mode"
elif [ "${n_ap:-0}" -eq 0 ] && [ "${n_other:-0}" -eq 0 ]; then
  check 6.16 SKIP "the store's files are named per access point" "no handshakes captured yet"
elif [ "${n_other:-0}" -eq 0 ]; then
  check 6.16 PASS "the store's files are named per access point" "$layout"
else
  check 6.16 INFO "the store's files are named per access point" "$layout (other names: a restored store?)"
fi
# 6.15 is in the config tests below; 6.17 comes from the dashboard section.

# ---------------------------------------------------------------- 6. regressions from 1.0.11-beta.1: pinned Python packages (#115)
sec "6. regressions from 1.0.11-beta.1: pinned Python packages (#115)"
pins=$(as_dev "$VENV_PY" -I - "$DEV_HOME/.venv/kestrel-constraints.txt" <<'PY' 2>&1 | tail -1
import importlib.metadata as md, importlib.util, sys
try:
    lines = [l.strip() for l in open(sys.argv[1]) if "==" in l]
except FileNotFoundError:
    print("noconstraints")
    sys.exit(0)
bad = []
for line in lines:
    name, want = line.split("==", 1)
    try:
        have = md.version(name)
    except md.PackageNotFoundError:
        have = "missing"
    if have != want:
        bad.append("%s %s!=%s" % (name, have, want))
pytest = importlib.util.find_spec("pytest") is not None
print("pins=%d mismatched=%d pytest=%s %s" % (len(lines), len(bad), pytest, "; ".join(bad)[:200]))
PY
)
echo "  venv: $pins"
case "$pins" in
  "pins="*" mismatched=0 "*)
    check 6.18 PASS "the venv's packages match kestrel-constraints.txt" "$(printf '%s' "$pins" | cut -d' ' -f1-2)" ;;
  noconstraints) check 6.18 FAIL "the venv's packages match kestrel-constraints.txt" "no constraints file in the venv" ;;
  *) check 6.18 FAIL "the venv's packages match kestrel-constraints.txt" "$(printf '%s' "$pins" | cut -c1-200)" ;;
esac
case "$pins" in
  *"pytest=False"*) check 6.19 PASS "pytest is not installed on the device" "" ;;
  *"pytest=True"*) check 6.19 FAIL "pytest is not installed on the device" "pytest found" ;;
  *) check 6.19 INFO "pytest is not installed on the device" "$(printf '%s' "$pins" | cut -c1-80)" ;;
esac

# ---------------------------------------------------------------- 7. regressions from 1.0.10
sec "7. regressions from 1.0.10"
gone=$(as_dev "$VENV_PY" -I -c 'import importlib.util
names = ("kestrel_pi.hatkeys", "kestrel_pi.alerts", "kestrel_pi.vendor")
print(" ".join(n for n in names if importlib.util.find_spec(n) is not None) or "none")' 2>&1 | tail -1)
if [ "$gone" = none ]; then
  check 7.1 PASS "plugin-only modules are gone from the installed package" "no hatkeys, alerts or vendor"
else
  check 7.1 FAIL "plugin-only modules are gone from the installed package" "still there: $(printf '%s' "$gone" | cut -c1-120)"
fi
bad=""
for f in _lib_alerts.py _lib_eink_face.py _lib_hat_keys.py _lib_ups.py _lib_ina219.py; do
  st=$(stat -c '%U %a' "$LIBDIR/plugins/$f" 2>/dev/null || echo missing)
  echo "  $f: $st"
  [ "$st" = "root 644" ] || bad="$bad $f($st)"
done
if [ -z "$bad" ]; then
  check 7.2 PASS "the five plugin libraries are installed, root-owned 0644" "alerts, eink_face, hat_keys, ups, ina219"
else
  check 7.2 FAIL "the five plugin libraries are installed, root-owned 0644" "$bad"
fi
# Load each library the way a plugin does, as the device user in the venv.
# Loading runs no plugin and touches no hardware (drawing, GPIO and I2C are lazy).
apis=$(as_dev "$VENV_PY" -I - "$LIBDIR/plugins" <<'PY' 2>&1 | tail -1
import os, sys
from kestrel_pi import load_library
out = []
for caller, name, api in (("ntfy.py", "alerts", 1), ("eink-ws27.py", "eink_face", 1),
                          ("eink-ws27.py", "hat_keys", 0), ("ups-ws.py", "ups", 0),
                          ("ups-ws.py", "ina219", 1)):
    try:
        mod = load_library(os.path.join(sys.argv[1], caller), name, api=api)
        out.append("%s=%d.%d" % ((name,) + tuple(mod.LIB_API)))
    except Exception as exc:
        out.append("%s=FAIL(%s)" % (name, type(exc).__name__))
print(" ".join(out))
PY
)
echo "  libraries: $apis"
case "$apis" in
  "alerts=1."*" eink_face=1."[1-9]*" hat_keys=0."*" ups=0."[3-9]*" ina219=1."[1-9]*)
    check 7.3 PASS "each library loads with the API its plugins ask for" "$apis" ;;
  *) check 7.3 FAIL "each library loads with the API its plugins ask for" "$(printf '%s' "$apis" | cut -c1-160)" ;;
esac
liberr=$(journalctl -b -q --no-pager -u kestrel-daemon 2>/dev/null \
  | grep -cE 'library API|plugin library .* not found|has no LIB_API' || true)
if [ "${liberr:-0}" -eq 0 ]; then
  check 7.4 PASS "no plugin-library load errors in this boot's daemon journal" ""
else
  check 7.4 FAIL "no plugin-library load errors in this boot's daemon journal" "$liberr line(s)"
fi
# 7.5-7.9 are in the config tests below; 7.10 and 7.12 come from the dashboard.
# This daemon run only: which e-ink plugin stood down and for whom, and whether
# they still fought over the pins. Counts and plugin names only.
inv=$(systemctl show -p InvocationID --value kestrel-daemon 2>/dev/null)
run_log=$work/daemon-run.log
if [ -n "$inv" ]; then
  journalctl -q --no-pager "_SYSTEMD_INVOCATION_ID=$inv" >"$run_log" 2>/dev/null
else
  : >"$run_log"
fi
eon=""
n_on=0
for n in eink-ws27 eink-ws213; do
  if [ "$(cfg "plugins.$n.enabled" false)" = true ]; then eon="$eon $n"; n_on=$((n_on + 1)); fi
done
standing=$(grep -oE '(eink-ws27|eink-ws213): standing down: (eink-ws27|eink-ws213) drives' "$run_log" | sort -u)
n_standing=$(printf '%s\n' "$standing" | grep -c 'standing down' || true)
in_use=$(grep -c 'already in use' "$run_log" || true)
echo "  enabled e-ink:${eon:- none}   standing down: $n_standing   'already in use': $in_use"
if [ "$n_on" -eq 0 ]; then
  check 7.11 SKIP "one e-ink plugin drives the panel, the other stands down" "no e-ink plugin enabled"
elif [ -z "$inv" ]; then
  check 7.11 INFO "one e-ink plugin drives the panel, the other stands down" "no daemon run to read"
elif [ "$in_use" -eq 0 ] && [ "$n_standing" -eq $((n_on - 1)) ]; then
  check 7.11 PASS "one e-ink plugin drives the panel, the other stands down" "$n_on enabled, $n_standing standing down"
else
  check 7.11 FAIL "one e-ink plugin drives the panel, the other stands down" \
    "$n_on enabled, $n_standing standing down, $in_use 'already in use' line(s)"
fi
nopanel=$(journalctl -b -q --no-pager -u kestrel-daemon 2>/dev/null | grep -c 'no panel (' || true)
plain=$(journalctl -b -q --no-pager -u kestrel-daemon 2>/dev/null | grep -c 'BUSY line never driven (no panel wired)' || true)
if [ -z "$eon" ]; then
  check 7.13 SKIP "with no HAT, the journal says no panel is wired" "no e-ink plugin enabled"
elif [ "${nopanel:-0}" -eq 0 ]; then
  check 7.13 INFO "with no HAT, the journal says no panel is wired" "no 'no panel' line: a panel answered"
elif [ "${plain:-0}" -ge 1 ]; then
  check 7.13 PASS "with no HAT, the journal says no panel is wired" "$plain line(s)"
else
  check 7.13 FAIL "with no HAT, the journal says no panel is wired" "'no panel' without the plain reason"
fi
topic=$DEV_HOME/.local/state/kestrel-ntfy/topic
if as_dev test -f "$topic"; then
  [ "$(as_dev stat -c '%a' "$topic" 2>/dev/null)" = 600 ] \
    && check 7.14 PASS "generated ntfy topic file is private" "0600 (contents not read)" \
    || check 7.14 FAIL "generated ntfy topic file is private" "mode $(as_dev stat -c '%a' "$topic" 2>/dev/null)"
else
  check 7.14 SKIP "generated ntfy topic file is private" "no generated topic"
fi
# Only the number of queued alerts is read, never their text.
queues=$(as_dev /usr/bin/python3 -I - "$DEV_HOME/.local/state" <<'PY' 2>&1
import json, os, sys
out = []
for name in ("ntfy", "telegram"):
    path = os.path.join(sys.argv[1], "kestrel-" + name, "queue.json")
    if not os.path.exists(path):
        continue
    try:
        data = json.load(open(path))
        out.append("%s=%d" % (name, len(data)) if isinstance(data, list) else "%s=BAD" % name)
    except Exception as exc:
        out.append("%s=BAD(%s)" % (name, type(exc).__name__))
print(" ".join(out) or "none")
PY
)
case "$queues" in
  none) check 7.15 SKIP "the alert queues read back" "no queue file yet" ;;
  *BAD*) check 7.15 FAIL "the alert queues read back" "$queues" ;;
  *) check 7.15 PASS "the alert queues read back" "queued: $queues" ;;
esac
tls=$(cfg web.tls on)
port=$(cfg web.port 8080)
tport=$(cfg web.tls_port 8443)
echo "web.tls: $tls   port: $port   tls_port: $tport"
WEBSTATE=$DEV_HOME/.local/state/kestrel-web
for f in "" /cert.pem /key.pem; do
  echo "  <web-state>$f: $(as_dev stat -c '%a' "$WEBSTATE$f" 2>/dev/null || echo missing)"
done
if [ "$(as_dev stat -c '%a' "$WEBSTATE" 2>/dev/null)" = 700 ] \
    && [ "$(as_dev stat -c '%a' "$WEBSTATE/cert.pem" 2>/dev/null)" = 600 ] \
    && [ "$(as_dev stat -c '%a' "$WEBSTATE/key.pem" 2>/dev/null)" = 600 ]; then
  check 7.16 PASS "certificate made and private" "dir 700, cert.pem and key.pem 600"
elif [ "$tls" = off ]; then
  check 7.16 SKIP "certificate made and private" "[web] tls is off"
else
  check 7.16 FAIL "certificate made and private" "see the modes above"
fi
# The fingerprint itself never goes in the report: only whether they match.
fp_cli=$(timeout 20 "$CLI" web fingerprint 2>/dev/null | tail -1)
fp_log=$(journalctl -b -q --no-pager -u kestrel-daemon 2>/dev/null \
  | grep -oE 'HTTPS certificate SHA-256 [0-9A-F:]+' | tail -1 | awk '{print $NF}')
if [ -n "$fp_cli" ] && [ "$fp_cli" = "$fp_log" ]; then
  check 7.17 PASS "kestrel web fingerprint matches the daemon's journal" "values not shown"
elif [ -z "$fp_log" ]; then
  check 7.17 INFO "kestrel web fingerprint matches the daemon's journal" "no HTTPS line in this boot's daemon journal"
else
  check 7.17 FAIL "kestrel web fingerprint matches the daemon's journal" "they differ (values not shown)"
fi
# Listening sockets only (read-only, no request): HTTPS and the forwarder on each
# management address, plain HTTP on loopback. Addresses are counted, not shown.
lst=$(ss -ltnH 2>/dev/null | awk '{print $4}')
https_ext=$(printf '%s\n' "$lst" | grep -E ":$tport\$" | grep -vcE '^(127\.|\[::1\])' || true)
http_ext=$(printf '%s\n' "$lst" | grep -E ":$port\$" | grep -vcE '^(127\.|\[::1\])' || true)
http_lo=$(printf '%s\n' "$lst" | grep -cE "^127\.[0-9.]+:$port\$" || true)
echo "  listeners: $tport off-loopback=$https_ext  $port off-loopback=$http_ext  $port loopback=$http_lo"
if [ "$tls" = off ]; then
  check 7.18 SKIP "HTTPS and the forwarder listen on each management address" "[web] tls is off"
elif [ "${https_ext:-0}" -ge 1 ] && [ "$https_ext" = "$http_ext" ] && [ "${http_lo:-0}" -ge 1 ]; then
  check 7.18 PASS "HTTPS and the forwarder listen on each management address" "$https_ext address(es); loopback plain HTTP too"
else
  check 7.18 FAIL "HTTPS and the forwarder listen on each management address" "see the listener counts above"
fi
# 7.19 (loopback Basic) and 7.20 (nosniff) come from the dashboard section below.
cat_ok=$(as_dev "$VENV_PY" -I -c 'from kestrel_pi import config_catalog as c
a, b = c.find("web", "tls"), c.find("web", "tls_port")
print(bool(a) and bool(b) and a.get("editable") is False and b.get("editable") is False)' 2>&1)
if [ "$cat_ok" = True ]; then
  check 7.21 PASS "Advanced settings lists web.tls and web.tls_port read-only" ""
else
  check 7.21 FAIL "Advanced settings lists web.tls and web.tls_port read-only" "$(printf '%s' "$cat_ok" | tr '\n' ' ' | cut -c1-120)"
fi
# 7.22 (web.tls refused through the settings channel) is in the config tests below.
# The helper run directly with a malformed request: it answers without reading
# any password (a bad request never reaches the check) and writes nothing, and
# exits 0 once it has answered, so a refused request leaves no failed unit.
auth_out=$(printf '{}' | timeout 10 /usr/bin/python3 -I "$CTL" checkauth 2>/dev/null); auth_rc=$?
if [ "$auth_rc" -eq 0 ] && printf '%s' "$auth_out" | grep -q '"ok": false'; then
  check 7.23 PASS "the login helper exits 0 after answering a refused request" "rc=0, refused"
else
  check 7.23 FAIL "the login helper exits 0 after answering a refused request" \
    "rc=$auth_rc reply=$(printf '%s' "$auth_out" | tr -cd '[:alnum:] :{}",_' | cut -c1-80)"
fi
gone_old=""
for old in notify hat-buttons; do
  [ -e "$LIBDIR/plugins/$old.py" ] && gone_old="$gone_old $old"
done
if [ -z "$gone_old" ]; then
  check 7.24 PASS "the replaced notify and hat-buttons plugins are gone" ""
else
  check 7.24 FAIL "the replaced notify and hat-buttons plugins are gone" "still installed:$gone_old"
fi
# 7.25-7.27 come from the dashboard section below.
check 8.1 MANUAL "see the checklist" "the closing step"

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
# Plugin declarations, read statically (nothing imported or run).
plugs = ctl.static_plugins()
def decl(name):
    found = plugs.get(name)
    return (found[0] or {}) if found else {}
def caps(name): return set(decl(name).get("capabilities") or ())
def opts(name): return decl(name).get("options") or {}
def defaults(name): return {k: (v or {}).get("default") for k, v in opts(name).items()}
def provides(name): return tuple(decl(name).get("provides") or ())
def state(name): return "enabled" if read("plugins.%s.enabled" % name) is True else "disabled"
tables = set((read("plugins") or {}).keys())
# 6.8 and 6.11's config half: the shipped tables carry the new names.
check("6.8", "eink-ws27" in tables and "eink" not in tables and "eink" not in plugs,
      "config.toml has [plugins.eink-ws27] and no [plugins.eink]; no eink plugin is listed", "")
check("6.12", "ups-ws" in tables and "ups-hat" not in tables and "ups-hat" not in plugs
      and caps("ups-ws") == {"ui"} and provides("ups-ws") == ("battery",)
      and defaults("ups-ws").get("address") == 0x42
      and not str(decl("ups-ws").get("description") or "").startswith("Deprecated"),
      "config.toml has [plugins.ups-ws] and no [plugins.ups-hat]; ups-ws is local only, "
      "provides battery at 0x42, and isn't marked deprecated", "ups-ws %s" % state("ups-ws"))
# 6.9: eink-ws27 is no longer marked deprecated and keeps its choices.
check("6.9", not str(decl("eink-ws27").get("description") or "").startswith("Deprecated")
      and (opts("eink-ws27").get("variant") or {}).get("choices") == ["auto", "v2", "v1"],
      "eink-ws27 isn't marked deprecated and declares its variant choices",
      "eink-ws27 %s" % state("eink-ws27"))
# 7.5: every shipped plugin is there and readable; no library or helper file,
# and nothing named lib, is listed as a plugin.
expected = {"memtemp", "session-stats", "gps", "wpa-sec", "wigle", "backup", "eink-ws213",
            "eink-ws27", "updates", "remote-train", "downloads", "map", "ntfy", "telegram",
            "ups-ws", "ups-wsc", "ups-wsd", "ups-wse", "census"}
invalid = sorted(n for n, v in plugs.items() if v[0] is None)
missing = sorted(expected - set(plugs))
extra = sorted(set(plugs) - expected)
check("7.5", not invalid and not missing and not any(n.startswith("_") for n in plugs)
      and "lib" not in plugs,
      "all nineteen shipped plugins present and readable; no _ file or lib listed",
      "missing=%s invalid=%s other=%s" % (missing, invalid, extra))
# 7.6: choices where a plugin declares a fixed set, and a value outside them is
# refused through the settings channel with the (throwaway) config unchanged.
c213 = (opts("eink-ws213").get("variant") or {}).get("choices")
c27r = (opts("eink-ws27").get("rotation") or {}).get("choices")
table_before = read("plugins.eink-ws213")
r = apply({"plugin": "eink-ws213", "set": {"variant": "V4"}})
check("7.6", c213 == ["v4", "v3", "v2"] and c27r == [0, 90, 180, 270]
      and not r.get("ok") and read("plugins.eink-ws213") == table_before
      and "one of" in str(r.get("error", "")),
      "e-ink choices declared; a value outside them is refused through the settings channel",
      "error=%s" % str(r.get("error", ""))[:80])
# 7.7: one plugin per alert service, each with only its own secret options.
def secrets(name):
    return {k for k, v in opts(name).items() if (v or {}).get("secret")}
check("7.7", "network" in caps("ntfy") and "network" in caps("telegram")
      and secrets("ntfy") == {"topic", "token"} and secrets("telegram") == {"bot_token", "chat_id"},
      "ntfy and telegram declare network and only their own secrets",
      "ntfy %s, telegram %s" % (state("ntfy"), state("telegram")))
# 7.8: eink-ws27's HAT key options, local only.
o = opts("eink-ws27")
check("7.8", (o.get("buttons") or {}).get("default") is True
      and (o.get("pins") or {}).get("default") == "5,6,13,19"
      and (o.get("hold_seconds") or {}).get("default") == 3
      and (o.get("allow_capture") or {}).get("default") is True
      and (o.get("allow_power") or {}).get("default") is True
      and "network" not in caps("eink-ws27"),
      "eink-ws27 declares the HAT key options, local only",
      "buttons %s" % ("off" if read("plugins.eink-ws27.buttons") is False else "on"))
# 7.9: the one-cell and four-cell UPS plugins: local only, the battery service,
# untested, and their defaults (0x43, 0x43, 0x2D, power_on_when_charged off).
new_ups = ("ups-wsc", "ups-wsd", "ups-wse")
d_c, d_d, d_e = defaults("ups-wsc"), defaults("ups-wsd"), defaults("ups-wse")
check("7.9", all(caps(n) == {"ui"} and provides(n) == ("battery",)
                 and "Not yet tested on hardware" in str(decl(n).get("description") or "")
                 for n in new_ups)
      and d_c.get("address") == 0x43 and d_d.get("address") == 0x43
      and d_e.get("address") == 0x2D and d_e.get("power_on_when_charged") is False,
      "ups-wsc, ups-wsd and ups-wse: local only, battery, untested, their defaults",
      ", ".join("%s %s" % (n, state(n)) for n in new_ups))
# 3.2: census's declarations: network/ui/web, the census service, the two
# services it reads, and its defaults (every 90 minutes, tether only).
dc = defaults("census")
check("3.2", caps("census") == {"network", "ui", "web"} and provides("census") == ("census",)
      and tuple(decl("census").get("optional_requires") or ()) == ("agent", "remote-training")
      and dc.get("report") is True and dc.get("read") is True and dc.get("poll_minutes") == 90
      and dc.get("tether_only") is True and dc.get("daily_mb") == 5 and "poll_hours" not in dc,
      "census declares network, ui and web, provides census, and keeps its defaults",
      "census %s" % state("census"))
# 3.3: enabling census through the settings channel needs the authorized-use
# acknowledgment (the throwaway config, and an ack file in the work folder).
before = read("plugins.census")
r1 = apply({"plugin": "census", "enabled": True})
refused = not r1.get("ok") and r1.get("need_ack") is True and read("plugins.census") == before
r2 = apply({"plugin": "census", "enabled": True, "ack": True})
check("3.3", refused and r2.get("ok") is True and read("plugins.census.enabled") is True,
      "enabling census through the settings channel needs the acknowledgment",
      "without: %s" % str(r1.get("error", ""))[:70])
# 7.22: HTTPS can only be switched from a terminal, never through the settings socket.
before = read("web.tls")
r = apply({"target": "config-set", "table": "web", "key": "tls", "value": "off"})
check("7.22", not r.get("ok") and read("web.tls") == before,
      "web.tls is refused through the settings channel (CLI only)", "error=%s" % r.get("error"))
PY
# 6.15: the Advanced page's default for the mode, from the installed package.
cat_mode=$(as_dev "$VENV_PY" -I -c 'from kestrel_pi import config_catalog as c
e = c.find("capture.output", "mode") or {}
print(e.get("default"), (e.get("choices") or [None])[0])' 2>&1 | tail -1)
if [ "$cat_mode" = "per-ap per-ap" ]; then
  check 6.15 PASS "Advanced settings shows per-ap as the mode's default" ""
else
  check 6.15 FAIL "Advanced settings shows per-ap as the mode's default" "$(printf '%s' "$cat_mode" | cut -c1-120)"
fi
[ "$(sha256sum "$CONFIG" | awk '{print $1}')" = "$cfg_sum_before" ] \
  && check 0.1 PASS "real config.toml untouched by the tests" "checksum identical" \
  || check 0.1 FAIL "real config.toml untouched by the tests" "checksum changed - tell Claude"

} >"$raw" 2>&1

# ---------------------------------------------------------------- dashboard status (optional)
{
sec "dashboard /status.json (selected fields)"
# The Python goes in a file in the root-only work folder so stdin can carry the
# password: with "python3 - <<PY" the heredoc would replace the piped password
# and Python would read an empty line.
cat >"$work/dashboard.py" <<'PY'
import base64, json, sys, tomllib, urllib.error, urllib.request
pw = sys.stdin.readline().rstrip("\n")
with open("/etc/kestrel/config.toml", "rb") as h:
    cfg = tomllib.load(h)
web = cfg.get("web", {}) or {}
mode = str(web.get("auth", "system")).lower()
port = int(web.get("port", 8080))
def chk(cid, result, what, detail=""):
    print("CHECK %-5s %-6s %s :: %s" % (cid, result, what, detail))
# 7.20: every response carries nosniff, the sign-in refusal included (no password needed).
try:
    hdrs = urllib.request.urlopen("http://127.0.0.1:%d/status.json" % port, timeout=15).headers
except urllib.error.HTTPError as exc:
    hdrs = exc.headers
except Exception as exc:
    hdrs = None
    chk("7.20", "INFO", "responses carry X-Content-Type-Options: nosniff", "no answer: %s" % type(exc).__name__)
if hdrs is not None:
    chk("7.20", "PASS" if hdrs.get("X-Content-Type-Options") == "nosniff" else "FAIL",
        "responses carry X-Content-Type-Options: nosniff", "")
# The checks below need the dashboard's answer; without it each says why.
DASH = {"3.4": "an enabled census is loaded, with no error",
        "3.9": "the Census page shows what this device reports",
        "5.5": "the agent page shows the contest tally",
        "5.9": "/status.json reports the release version, with the package version beside it",
        "5.10": "the dashboard header shows the release version",
        "6.6": "the Environment card shows a count while capturing",
        "6.10": "an enabled eink-ws27 loads with its Motion card and no deprecation note",
        "6.13": "an enabled ups-ws loads, its card shows a status, and no deprecation note",
        "6.17": "Captured networks opens on the by-access-point view",
        "7.10": "each enabled ups-wsc/ups-wsd/ups-wse is loaded and its card shows a status",
        "7.12": "one e-ink card shows the panel, the other 'used by' it",
        "7.25": "the running daemon offers the row buttons",
        "7.26": "the map page: background, recovered colours, drag and zoom",
        "7.27": "Captured networks has its controls at the top"}
def skip_all(why, basic=True):
    if basic:
        chk("7.19", "SKIP", "loopback /status.json with Basic credentials", why)
    for cid, what in DASH.items():
        chk(cid, "SKIP", what, why)
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
        skip_all("no password given")
        sys.exit(0)
    req.add_header("Authorization", "Basic " + base64.b64encode(
        ("%s:%s" % (user, pw)).encode()).decode())
try:
    s = json.load(urllib.request.urlopen(req, timeout=15))
except Exception as exc:
    print("(not available: %s)" % type(exc).__name__)
    chk("7.19", "FAIL" if mode in ("basic", "system") else "INFO",
        "loopback /status.json with Basic credentials", type(exc).__name__)
    skip_all("no /status.json", basic=False)
    sys.exit(0)
if mode in ("basic", "system"):
    chk("7.19", "PASS", "loopback /status.json with Basic credentials", "plain HTTP on 127.0.0.1")
print("version:", s.get("version"))
cap = s.get("capture", {}) or {}
print("capture:", {k: cap.get(k) for k in ("state", "enabled", "hashline_files", "located_aps")})
print("environment:", s.get("environment"))
ag = s.get("agent", {}) or {}
print("agent:", {k: v for k, v in ag.items() if isinstance(v, (str, int, float, bool)) or v is None})
st = ag.get("status") if isinstance(ag.get("status"), dict) else {}
print("agent status:", {k: v for k, v in st.items() if isinstance(v, (str, int, float, bool))})
for p in s.get("plugins", []):
    keep = {k: p.get(k) for k in ("state", "reason", "last_error", "warnings", "overruns",
                                   "capability_violations") if p.get(k) not in (None, "", [], 0)}
    print("plugin %-13s %s" % (p.get("name"), keep))
plugins = {p.get("name"): p for p in s.get("plugins", []) if isinstance(p, dict)}
w = s.get("widgets", {}) or {}
def widget(name):
    els = w.get(name) or []
    if isinstance(els, dict):
        els = list(els.values())
    return {e.get("name"): e.get("value") for e in els if isinstance(e, dict)}
def enabled(name):
    return (plugins.get(name) or {}).get("state") not in (None, "disabled")
# 6.6: while capturing, the Environment card has a count, not "Unknown".
env = s.get("environment")
if not isinstance(env, dict):
    chk("6.6", "SKIP", DASH["6.6"], "capture is off")
elif env.get("available"):
    chk("6.6", "PASS", DASH["6.6"], "available, data age %s s" % env.get("data_age_s"))
else:
    chk("6.6", "FAIL", DASH["6.6"], "not available while capturing")
# 6.10: eink-ws27 loads with its Motion card, and nothing calls it deprecated.
if not enabled("eink-ws27"):
    chk("6.10", "SKIP", DASH["6.10"], "eink-ws27 not enabled")
else:
    p = plugins["eink-ws27"]
    ok = (p.get("state") == "loaded" and "mobility" in widget("eink-ws27")
          and "deprecated" not in str(p.get("note") or "").lower())
    chk("6.10", "PASS" if ok else "FAIL", DASH["6.10"],
        "state %s, note starts %r" % (p.get("state"), str(p.get("note") or "")[:40]))
# 6.13: ups-ws loads, its card shows a status, and nothing calls it deprecated.
if not enabled("ups-ws"):
    chk("6.13", "SKIP", DASH["6.13"], "ups-ws not enabled")
else:
    p = plugins["ups-ws"]
    status = str(widget("ups-ws").get("status") or "")
    ok = (p.get("state") == "loaded" and status not in ("", "checking")
          and "deprecated" not in str(p.get("note") or "").lower())
    chk("6.13", "PASS" if ok else "FAIL", DASH["6.13"], "status %r" % status[:50])
# 7.10: each enabled one-cell/four-cell UPS plugin loads and shows a status (with
# no board: "no UPS HAT (C) detected" and the like).
new_on = [n for n in ("ups-wsc", "ups-wsd", "ups-wse") if enabled(n)]
if not new_on:
    chk("7.10", "SKIP", DASH["7.10"], "none of ups-wsc, ups-wsd, ups-wse enabled")
else:
    bad = [n for n in new_on if plugins[n].get("state") != "loaded"
           or str(widget(n).get("status") or "") in ("", "checking")]
    chk("7.10", "FAIL" if bad else "PASS", DASH["7.10"],
        ("not loaded or no status: %s" % ", ".join(bad)) if bad else
        "; ".join("%s: %s" % (n, str(widget(n).get("status"))[:50]) for n in new_on))
# 7.12: one enabled e-ink plugin's card shows the panel's state, the other reads
# "used by <that plugin>".
eon = [n for n in ("eink-ws27", "eink-ws213") if enabled(n)]
if len(eon) < 2:
    chk("7.12", "SKIP", DASH["7.12"], "needs both eink-ws27 and eink-ws213 enabled")
else:
    shown = {n: str(widget(n).get("panel") or "") for n in eon}
    drivers = [n for n, v in shown.items() if not v.startswith("used by ")]
    ok = len(drivers) == 1 and all(v == "used by " + drivers[0]
                                   for n, v in shown.items() if n != drivers[0])
    chk("7.12", "PASS" if ok else "FAIL", DASH["7.12"],
        "; ".join("%s: %s" % (n, v[:40]) for n, v in shown.items()))
# 7.25: an enabled, loaded downloads/map/ntfy/telegram offers its main-page button.
offered = {p.get("name"): [a.get("label") for a in (p.get("actions") or []) if isinstance(a, dict)]
           for p in s.get("plugins", []) if p.get("state") == "loaded"}
want = {"downloads": "Download", "map": "Map", "ntfy": "Status", "telegram": "Status"}
on = sorted(n for n in want if n in offered)
if not on:
    chk("7.25", "SKIP", DASH["7.25"], "none of downloads, map, ntfy, telegram loaded")
else:
    bad = [n for n in on if want[n] not in offered[n]]
    chk("7.25", "FAIL" if bad else "PASS", DASH["7.25"],
        ("missing: %s" % ", ".join(bad)) if bad else "checked: %s" % ", ".join(on))
# 6.17, 7.26 and 7.27: pages read over loopback with the same credentials. Only
# markers are checked; nothing from a page (locations, network names) is printed.
def page(path):
    r = urllib.request.Request("http://127.0.0.1:%d%s" % (port, path))
    for k, v in req.header_items():
        r.add_header(k, v)
    try:
        resp = urllib.request.urlopen(r, timeout=20)
        return resp.status, resp.headers, resp.read(4 * 1024 * 1024).decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.headers, ""
    except Exception as exc:
        return None, None, type(exc).__name__
if "map" not in offered:
    chk("7.26", "SKIP", DASH["7.26"], "map not loaded")
else:
    code, hdrs, body = page("/plugins/map/")
    policy = (hdrs or {}).get("Referrer-Policy") if hdrs else None
    if code != 200:
        chk("7.26", "FAIL", DASH["7.26"], "HTTP %s" % code)
    elif "No captured network matching" in body:
        chk("7.26", "SKIP", DASH["7.26"], "no located networks")
    else:
        background = ("OpenStreetMap</a> contributors" not in body
                      or ('referrerpolicy="origin"' in body and policy == "no-referrer"))
        legend = "Green with a" in body and "not recovered" in body
        gestures = (body.count('class="kmap"') == 2 and body.count('class="kmap-layer"') == 2
                    and body.count("data-scale=") == 2 and "bboxFor" in body
                    and "touch-action:none" in body)
        ok = background and legend and gestures
        chk("7.26", "PASS" if ok else "FAIL", DASH["7.26"],
            "background %s, legend %s, gestures %s" % (background, legend, gestures))
code, hdrs, body = page("/networks")
if code != 200:
    chk("6.17", "FAIL", DASH["6.17"], "HTTP %s" % code)
    chk("7.27", "FAIL", DASH["7.27"], "HTTP %s" % code)
else:
    out_mode = str(((cfg.get("capture") or {}).get("output") or {}).get("mode", "per-ap"))
    if out_mode != "per-ap":
        chk("6.17", "SKIP", DASH["6.17"], "mode is %s" % out_mode)
    else:
        chk("6.17", "PASS" if "Captured access points" in body else "FAIL", DASH["6.17"], "")
    top = body.split("<h1>", 1)[0]
    # With the web login off the import control is hidden on purpose.
    ok = ('class="nethead"' in top and 'class="countbar"' in body
          and ("Import results" in top or mode == "none"))
    chk("7.27", "PASS" if ok else "FAIL", DASH["7.27"],
        ("Import results above the heading" if mode != "none" else "web login off: no import")
        if ok else "markers missing")
# 5.9 and 5.10: the device's release version (/etc/kestrel/version) in the status
# and on the dashboard header.
try:
    release = open("/etc/kestrel/version", encoding="utf-8").read().strip()
except OSError:
    release = ""
ok = bool(release) and s.get("version") == release and bool(s.get("package_version"))
chk("5.9", "PASS" if ok else "FAIL", DASH["5.9"],
    "version %r, package %r" % (s.get("version"), s.get("package_version")))
code, _hdrs, body = page("/")
want_hdr = ("v" + release) if release[:1].isdigit() else "dev build (%s)" % release
chk("5.10", "PASS" if code == 200 and ("plugin host &middot; " + want_hdr) in body else "FAIL",
    DASH["5.10"], "HTTP %s, expecting %r" % (code, want_hdr))
# 5.5: the agent card (on /agent) shows the contest tally, or the earlier
# adoptions before the first contest.
code, _hdrs, body = page("/agent")
if code != 200:
    chk("5.5", "FAIL", DASH["5.5"], "HTTP %s" % code)
elif "Model contests" in body or "Earlier adoptions" in body:
    chk("5.5", "PASS", DASH["5.5"], "Model contests" if "Model contests" in body
        else "Earlier adoptions (no contest yet)")
elif "Promotions" in body:
    chk("5.5", "FAIL", DASH["5.5"], "still the old Promotions line")
else:
    chk("5.5", "SKIP", DASH["5.5"], "no tally on the agent card yet")
# 3.4: an enabled census loads with no error; its note is empty, or a pause
# reason in words.
if not enabled("census"):
    chk("3.4", "SKIP", DASH["3.4"], "census not enabled")
else:
    p = plugins["census"]
    ok = p.get("state") == "loaded" and not p.get("last_error")
    chk("3.4", "PASS" if ok else "FAIL", DASH["3.4"],
        "state %s, note %r" % (p.get("state"), str(p.get("note") or "")[:70]))
# 3.9: the Census page says what this device reports: its release, and (with
# tether_only) that it goes over the tether. Markers only.
if "census" not in offered:
    chk("3.9", "SKIP", DASH["3.9"], "census not loaded")
else:
    code, _hdrs, body = page("/plugins/census/index")
    tether = ((cfg.get("plugins") or {}).get("census") or {}).get("tether_only", True)
    want = ["What this device reports today", "Release: " + (release or "unknown")]
    if tether:
        want.append("Sent over: the phone's Bluetooth tether only")
    missing = [m for m in want if m not in body]
    chk("3.9", "PASS" if code == 200 and not missing else "FAIL", DASH["3.9"],
        "HTTP %s%s" % (code, (", missing %s" % missing) if missing else ""))
for name in ("wpa-sec", "wigle", "backup", "remote-train", "census", "updates", "ntfy", "telegram",
             "eink-ws27", "eink-ws213", "ups-ws", "ups-wsc", "ups-wsd", "ups-wse"):
    if name in w:
        vals = ["%s=%s" % (k, v) for k, v in widget(name).items()]
        print("widget %-13s %s" % (name, ", ".join(vals)))
PY
printf '%s\n' "$web_pw" | /usr/bin/python3 -I "$work/dashboard.py" 2>&1
} >>"$raw" 2>&1
web_pw=""

cat >>"$raw" <<'EOF'

===== by hand (the script can't see these) =====
1.6  with the updates plugin's beta option OFF this beta is NOT offered; with
     it ON it is offered and applies in place from the dashboard
2.3  with remote-train set up, make the training host unreachable (shut it
     down or stop its SSH server): the remote-train card reads "can't reach or
     log in to the host (last reached ...)"; bring it back and the card recovers
2.4  restart the daemon (sudo systemctl restart kestrel-daemon): the
     remote-train card still shows the last model pulled
3.10 with the census on: turn the phone's tether off; the Census page reads
     "Paused: waiting for the phone's Bluetooth tether."; turn it on and it
     says "Running." again within a few minutes
3.11 from a computer, python3 tools/census-report/census_report.py lists this
     device's release (with the test seeder running, about 40 more devices),
     and the card's numbers match after the device's next read
4.5  stop capture from the dashboard, then rerun this check with capture off
     (for 4.3); start it again and the dashboard shows it running
5.6  after a day of capture: the /agent card's Last contest shows a result, and
     pull-agent-diagnostics.sh --score (from a computer) shows contests from
     both sources with their reasons
5.8  copy pull-health.sh off the device and run it from a computer: one report
     with a verdict and no addresses, MAC addresses or names in it
6.4  if not done yet: capture for an hour somewhere busy; pull-health.sh shows
     the database lag still small and AngryOxide's memory roughly flat, and the
     Environment card keeps a count
8.1  the closing step: cut 1.0.12 stable (or the next beta); this device is
     offered it and applies it in place
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
secret_keys = {"api_key", "api_token", "api_name", "token", "password", "username",
               "topic", "chat_id"}
# Any *_token / *_topic / *_chat / *_key / *_password option is a secret too
# (ntfy's topic and token, telegram's bot_token and chat_id).
secret_suffixes = ("_token", "_topic", "_chat", "_key", "_password", "_secret")
host_keys = {"host", "train_host", "user", "train_user", "probe_url", "endpoint",
             "repo", "path", "train_path", "server", "ntfy_server", "tile_url"}
subs = []
def walk(node):
    for k, v in node.items():
        if isinstance(v, dict):
            walk(v)
        elif isinstance(v, str) and len(v) >= 3 and v not in public:
            if k in secret_keys or k.endswith(secret_suffixes):
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
# standalone token is masked, never part of a path, URL or file name. The shipped
# default "kestrel" is public and is also the CLI's name, so it is left as is.
if hostname and hostname != "kestrel":
    text = re.sub(r"(?<![\w./-])%s(?![\w./-])" % re.escape(hostname), "<hostname>", text)
for name in set(sys.argv[2:]):
    if name and name != "root":
        text = re.sub(r"(?<![\w-])%s(?![\w-])" % re.escape(name), "<user>", text)
# A certificate fingerprint (colon-separated hex pairs, 8 or more) is device-
# unique: masked before the MAC pattern could take a slice of it.
text = re.sub(r"\b(?:[0-9A-Fa-f]{2}:){7,}[0-9A-Fa-f]{2}\b", "<fingerprint>", text)
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
