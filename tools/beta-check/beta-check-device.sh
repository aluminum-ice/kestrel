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

# ================================================================ 1.0.14-beta.2 checks
# 1.0.14-beta.2 is an in-place update on top of 1.0.14-beta.1: the census
# carries the board family (#177) as section 2, and the agent diagnostics
# script counts from-scratch contests (#179) as 3. Sections 4-9 repeat beta 1's
# checks as regressions (the learning agent's #175 and the GPS fix #174),
# 10-23 1.0.13's, and 24-29 1.0.12's, 1.0.11's and 1.0.10's.
# Nothing here reads capture content: only the installed code, the counts the
# dashboard already shows, and counts over the agent's own logs. The CHECK ids
# match the checklist issue for 1.0.14-beta.2.
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

# ---------------------------------------------------------------- 2. census: the board family (#177)
sec "2. census: the board family (#177)"
cp=$LIBDIR/plugins
census_on=$(cfg plugins.census.enabled false)
# The family this board should report, from its device-tree name by the
# census's own prefixes (the 400/500 and Compute Modules share their family's);
# only the family's name is printed.
board_names=("not reported" "Zero 2 W" "Pi 3 family" "Pi 4 family" "Pi 5 family" "another board")
model=$(tr -d '\0' 2>/dev/null </proc/device-tree/model)
case "$model" in
  "") want_board=0 ;;
  "Raspberry Pi Zero 2"*) want_board=1 ;;
  "Raspberry Pi Compute Module 3"*|"Raspberry Pi 3"*) want_board=2 ;;
  "Raspberry Pi Compute Module 4"*|"Raspberry Pi 4"*) want_board=3 ;;
  "Raspberry Pi Compute Module 5"*|"Raspberry Pi 5"*) want_board=4 ;;
  *) want_board=5 ;;
esac
want_name=${board_names[$want_board]}
echo "  this board's family: $want_name"
# 2.1: the installed census is 0.2.0, and its helper has the board families.
c_ver=$(sed -n 's/^ *__version__ = "\([0-9.]*\)".*/\1/p' "$cp/census.py" 2>/dev/null | head -1)
if [ "$c_ver" = "0.2.0" ] && grep -q '^TOKEN_MARKER = b"kb"$' "$cp/_census_proto.py" 2>/dev/null \
    && grep -q '^BOARD_FAMILIES = ' "$cp/_census_proto.py" \
    && grep -q '^def board_family(model):' "$cp/_census_proto.py" \
    && grep -q '^def token_board(token):' "$cp/_census_proto.py" \
    && grep -q '^def device_board(' "$cp/census.py"; then
  check 2.1 PASS "census 0.2.0 is installed and its helper knows the board families" ""
else
  check 2.1 FAIL "census 0.2.0 is installed and its helper knows the board families" "version ${c_ver:-?}, or markers missing in the installed census files"
fi
# 2.2: the installed helper's own answer for this board, run as the device user
# with the venv's Python (as the daemon runs it; the helper needs its packages).
got_board=$(as_dev "$VENV_PY" -I - "$cp/_census_proto.py" <<'PY' 2>&1 | tail -1
import importlib.util, sys
spec = importlib.util.spec_from_file_location("kbc_census_proto", sys.argv[1])
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
try:
    with open("/proc/device-tree/model", "rb") as h:
        print(mod.board_family(h.read(256)))
except OSError:
    print(mod.BOARD_UNKNOWN)
PY
)
case "$got_board" in
  [0-9]|1[0-5])
    got_name=${board_names[$got_board]:-another board}
    if [ "$got_board" = "$want_board" ]; then
      check 2.2 PASS "the census reads this board's family from its device-tree name" "$got_name"
    else
      check 2.2 FAIL "the census reads this board's family from its device-tree name" "census reads $got_name; the name says $want_name"
    fi ;;
  *) check 2.2 FAIL "the census reads this board's family from its device-tree name" "the helper didn't answer: $(printf '%s' "$got_board" | cut -c1-120)" ;;
esac
# 2.3 and 2.4: today's number and the history, read as the device user. The
# number is decoded here and never printed.
cstate=$DEV_HOME/.local/state/kestrel-census
upd_at=$(stat -c %Y "$upd" 2>/dev/null || echo 0)
day_start=$(date -u -d "$(date -u +%F)" +%s)
if [ "$census_on" != true ]; then
  check 2.3 SKIP "today's census number carries this board's family" "census not enabled"
  check 2.4 SKIP "the census history records board counts since the update" "census not enabled"
else
  tb=$(as_dev /usr/bin/python3 -I - "$cstate/day.json" <<'PY' 2>&1 | tail -1
import json, sys, time
try:
    day = json.load(open(sys.argv[1], encoding="utf-8"))
except FileNotFoundError:
    print("missing"); sys.exit(0)
except Exception as exc:
    print("unreadable", type(exc).__name__); sys.exit(0)
if not isinstance(day, dict) or day.get("day") != time.strftime("%Y-%m-%d", time.gmtime()):
    print("not-today"); sys.exit(0)
try:
    t = bytes.fromhex(str(day.get("token") or ""))
except ValueError:
    t = b""
print("none" if len(t) != 8 or t[:2] != b"kb" or t[2] & 1 else "board %d" % (t[2] >> 4))
PY
)
  case "$tb" in
    "board $want_board") check 2.3 PASS "today's census number carries this board's family" "$want_name" ;;
    board\ *) n=${tb#board }
      check 2.3 FAIL "today's census number carries this board's family" "it carries ${board_names[$n]:-another board}; this board is $want_name" ;;
    none)
      if [ "$upd_at" -ge "$day_start" ]; then
        check 2.3 INFO "today's census number carries this board's family" "no board yet: the update was applied this UTC day, so today's number may predate it; rerun after 00:00 UTC"
      else
        check 2.3 FAIL "today's census number carries this board's family" "today's number carries no board"
      fi ;;
    not-today|missing) check 2.3 INFO "today's census number carries this board's family" "no census day started yet today (paused?)" ;;
    *) check 2.3 FAIL "today's census number carries this board's family" "$tb" ;;
  esac
  # 2.4: days after the update's UTC day carry board counts; older days are
  # still in the file, so they survived the daemon's load and save. Counts only.
  hb=$(as_dev /usr/bin/python3 -I - "$cstate/history.json" "$upd_at" "$want_board" <<'PY' 2>&1 | tail -1
import json, sys, time
upd_day = time.strftime("%Y-%m-%d", time.gmtime(int(sys.argv[2]))) if int(sys.argv[2]) else ""
try:
    days = json.load(open(sys.argv[1], encoding="utf-8")).get("days")
except FileNotFoundError:
    print("missing"); sys.exit(0)
except Exception as exc:
    print("unreadable", type(exc).__name__); sys.exit(0)
days = days if isinstance(days, dict) else {}
after = [d for d in days if d > upd_day]
boards = [d for d in after if isinstance(days[d], dict) and isinstance(days[d].get("boards"), dict)]
mine = [d for d in boards if (days[d]["boards"].get(sys.argv[3]) or [0])[0] >= 1]
print("after=%d with=%d mine=%d older=%d" % (len(after), len(boards), len(mine), len(days) - len(after)))
PY
)
  case "$hb" in
    after=0\ *) check 2.4 INFO "the census history records board counts since the update" "no day after the update's yet; ${hb##*older=} older day(s) kept" ;;
    after=*)
      n_after=$(printf '%s' "$hb" | sed -n 's/^after=\([0-9]*\) .*/\1/p')
      n_with=$(printf '%s' "$hb" | sed -n 's/.* with=\([0-9]*\) .*/\1/p')
      n_mine=$(printf '%s' "$hb" | sed -n 's/.* mine=\([0-9]*\) .*/\1/p')
      n_older=$(printf '%s' "$hb" | sed -n 's/.* older=\([0-9]*\)$/\1/p')
      detail="$n_with of $n_after day(s) since carry board counts, this board's family on $n_mine; $n_older older day(s) kept"
      if [ "$n_with" = "$n_after" ]; then
        check 2.4 PASS "the census history records board counts since the update" "$detail"
      else
        check 2.4 FAIL "the census history records board counts since the update" "$detail"
      fi ;;
    missing) check 2.4 INFO "the census history records board counts since the update" "no history.json yet" ;;
    *) check 2.4 FAIL "the census history records board counts since the update" "$hb" ;;
  esac
fi
# 2.5 and 2.6 come from the dashboard section below.
check 2.7 MANUAL "see the checklist" "by hand (the Boards card on the phone)"
check 2.8 MANUAL "see the checklist" "by hand (the desktop census report's board tables)"

# ---------------------------------------------------------------- 3. agent diagnostics: from-scratch contests (#179)
sec "3. agent diagnostics: from-scratch contests (#179)"
# 3.1: the shipped diagnostics script's contest pattern takes the optional
# "(from scratch)" a from-scratch candidate's contest line carries.
if grep -qF '( \(from scratch\))? candidate vs active' "$SCRIPTS_DIR/pull-agent-diagnostics.sh" 2>/dev/null; then
  check 3.1 PASS "the installed diagnostics script recognizes a from-scratch contest" ""
else
  check 3.1 FAIL "the installed diagnostics script recognizes a from-scratch contest" "the installed pull-agent-diagnostics.sh is older"
fi
check 3.2 MANUAL "see the checklist" "by hand (a from-scratch row in the diagnostics)"

# ---------------------------------------------------------------- 4. regressions from 1.0.14-beta.1: the learning agent tries other scans (#175)
sec "4. regressions from 1.0.14-beta.1: the learning agent tries other scans (#175)"
# The venv is the device user's, so it's found and read as that user.
pkg_dir=$(as_dev "$VENV_PY" -I -c 'import os, kestrel_pi; print(os.path.dirname(kestrel_pi.__file__))' 2>/dev/null)
pkg_has() { [ -n "$pkg_dir" ] && as_dev grep -q -- "$2" "$pkg_dir/$1" 2>/dev/null; }
pkg_note=""; [ -n "$pkg_dir" ] || pkg_note=" (package not found)"
since=""
[ -f "$upd" ] && since="@$(stat -c %Y "$upd")"
since_ts=${since#@}
# The agent's first journal line since the update: the journal may not reach
# back to the update, so 4.4 compares only the span it still holds.
if [ -n "$since" ]; then
  jfrom=$(journalctl -q --no-pager -u kestrel-agent -o short-unix --since "$since" 2>/dev/null | head -1 | cut -d' ' -f1)
else
  jfrom=$(journalctl -q --no-pager -b -u kestrel-agent -o short-unix 2>/dev/null | head -1 | cut -d' ' -f1)
fi
# One pass as the device user over the installed agent code, config.toml (read,
# never written: the listen-only case is an in-memory copy), the agent's status,
# its model records and the episode rows logged since the update. Only counts,
# codes and settings come out: no observation, reward or capture detail.
cfg_sum=$(sha256sum "$CONFIG" 2>/dev/null | cut -d' ' -f1)
facts=$(as_dev "$VENV_PY" -I - "${since_ts:-0}" "$AGENT_DIR" "${jfrom:-}" <<'PY' 2>&1
import copy, glob, json, os, re, sys, tomllib
since, agent_dir = float(sys.argv[1] or 0), sys.argv[2]
try:
    jfrom = float(sys.argv[3])
except (IndexError, ValueError):
    jfrom = None            # no agent line in the journal since the update
def put(key, value):
    print("%s=%s" % (key, value))
try:
    from kestrel_pi.agent import controller, explore, policies, spec
except Exception as exc:
    put("import", "failed:%s" % type(exc).__name__)
    sys.exit(0)
put("import", "ok")

def main():
    KEYS = ("explore_rate", "explore_windows", "fresh_every", "stuck_contests", "fresh_timesteps")
    put("defaults", " ".join(str(controller.AGENT_DEFAULTS.get(k)) for k in KEYS))
    with open("/etc/kestrel/config.toml", "rb") as handle:
        cfg = tomllib.load(handle)
    put("overrides", ",".join(k for k in KEYS if k in (cfg.get("agent") or {})) or "none")
    acfg = controller.agent_config(cfg)
    put("effective", "%g %d" % (explore.clamp_rate(acfg.get("explore_rate", 0.05)),
                                explore.clamp_windows(acfg.get("explore_windows", 2))))
    base = policies.BaselinePolicy(dwell_seconds=acfg.get("dwell_seconds", 5)).predict(None)[0]
    allowed = set(explore.allowed_actions(acfg.get("action"), base))
    put("allowed", len(allowed))
    listen = copy.deepcopy(cfg)
    listen.setdefault("agent", {})["action"] = {"transmit_modes": ["passive"]}
    passive = explore.allowed_actions(controller.agent_config(listen).get("action"), base)
    put("passive", "%d %s" % (len(passive), all(spec.decode(a)[2] == "passive" for a in passive)))
    try:
        st = json.load(open(os.path.join(agent_dir, "status.json")))
    except Exception as exc:
        st = None
        put("status", "none" if isinstance(exc, FileNotFoundError) else "unreadable")
    if isinstance(st, dict):
        e = st.get("explore") if isinstance(st.get("explore"), dict) else {}
        local = ((st.get("promotion_tally") or {}).get("local") or {})
        put("status", "policy=%s rate=%s windows=%s allowed=%s tried=%s last_hour=%s" % (
            st.get("policy"), e.get("rate"), e.get("windows"), e.get("allowed"),
            e.get("tried"), e.get("last_hour")))
        put("fresh_tally", "%s/%s" % (local.get("fresh_won"), local.get("fresh_lost"))
            if "fresh_won" in local or "fresh_lost" in local else "none")
    metas = (glob.glob(os.path.join(agent_dir, "model-*.meta.json"))
             + glob.glob(os.path.join(agent_dir, ".candidates", "*", "*", "*.meta.json"))
             + glob.glob(os.path.join(agent_dir, ".remote-staging", "*", "*.meta.json")))
    new = seeded = unsupported = 0
    for path in metas:
        try:
            meta = json.load(open(path))
        except Exception:
            continue
        if "fresh" in meta:
            new += 1
            seeded += bool(meta.get("fresh")) and isinstance(meta.get("seed"), int)
        unsupported += "unsupported_actions" in (meta.get("provenance") or {})
    put("metas", "%d new=%d fresh_seeded=%d unsupported=%d" % (len(metas), new, seeded, unsupported))
    ACT = re.compile(r'"action": (\d+)[,}]')
    TS = re.compile(r'"ts": ([0-9.]+)')
    POL = re.compile(r'"policy": "([a-z]+)"')
    rows = learned = baseline = explored = explored_j = no_p = off_policy = outside = 0
    runs, cur = [], None
    files = sorted(f for f in glob.glob(os.path.join(agent_dir, "episodes", "*.jsonl"))
                   if os.path.getmtime(f) >= since)
    for path in files:
        with open(path, errors="replace") as handle:
            for line in handle:
                ts = TS.search(line)
                if not ts or float(ts.group(1)) < since:
                    continue
                rows += 1
                m = ACT.search(line)
                action = int(m.group(1)) if m else None
                m = POL.search(line)
                policy = m.group(1) if m else ""
                learned += policy == "learned"
                baseline += policy == "baseline"
                if action is not None and action not in allowed:
                    outside += 1
                if '"explore": true' in line:
                    explored += 1
                    explored_j += jfrom is not None and float(ts.group(1)) >= jfrom
                    off_policy += policy != "learned"
                    no_p += '"explore_p": ' not in line
                    if cur is not None and cur[0] == action:
                        cur[1] += 1
                    else:
                        if cur is not None:
                            runs.append(cur[1])
                        cur = [action, 1]
                elif cur is not None:
                    runs.append(cur[1])
                    cur = None
    last = cur[1] if cur is not None else 0
    put("episodes", "rows=%d learned=%d baseline=%d explored=%d explored_j=%d no_p=%d "
        "off_policy=%d outside=%d"
        % (rows, learned, baseline, explored, explored_j, no_p, off_policy, outside))
    put("runs", "n=%d ones=%d twos=%d longer=%d open=%d" % (
        len(runs) + (cur is not None), runs.count(1), runs.count(2),
        sum(1 for r in runs + [last] if r > 2), last))

try:
    main()
except Exception as exc:          # a broken config or record: say so, and stop
    put("error", type(exc).__name__)
PY
)
cfg_after=$(sha256sum "$CONFIG" 2>/dev/null | cut -d' ' -f1)
printf '%s\n' "$facts" | sed 's/^/  /'
fact() { printf '%s\n' "$facts" | sed -n "s/^$1=//p" | head -1; }
num() { printf '%s\n' "$1" | sed -n "s/.*\\b$2=\\([0-9]*\\).*/\\1/p"; }
ast=$(fact status)
apol=$(printf '%s' "$ast" | sed -n 's/.*policy=\([a-z]*\).*/\1/p')
echo "agent policy (status): ${apol:-?}; configured: $(cfg agent.policy baseline)"
ep=$(fact episodes); rn=$(fact runs)
ep_rows=$(num "$ep" rows); ep_learned=$(num "$ep" learned); ep_base=$(num "$ep" baseline)
ep_exp=$(num "$ep" explored); ep_expj=$(num "$ep" explored_j); ep_nop=$(num "$ep" no_p); ep_off=$(num "$ep" off_policy)
ep_out=$(num "$ep" outside)
rn_n=$(num "$rn" n); rn_ones=$(num "$rn" ones); rn_twos=$(num "$rn" twos)
rn_long=$(num "$rn" longer)
ferr=$(fact error)
if [ -n "$ferr" ]; then
  check 4.1 FAIL "the explorer is installed and the agent reports exploring at its defaults" "couldn't read the agent's code, settings or logs: $ferr"
elif [ "$(fact import)" != ok ]; then
  check 4.1 FAIL "the explorer is installed and the agent reports exploring at its defaults" "agent code: $(fact import)$pkg_note"
elif ! pkg_has agent/explore.py 'class Explorer:'; then
  check 4.1 FAIL "the explorer is installed and the agent reports exploring at its defaults" "agent/explore.py missing$pkg_note"
elif [ "$(fact defaults)" != "0.05 2 4 3 0" ] || [ "$(fact effective)" != "0.05 2" ]; then
  check 4.1 INFO "the explorer is installed and the agent reports exploring at its defaults" "code defaults $(fact defaults), in effect $(fact effective) (set in config.toml: $(fact overrides))"
else
  case "$ast" in
    *"rate=0.05 windows=2 allowed=$(fact allowed) "*)
      check 4.1 PASS "the explorer is installed and the agent reports exploring at its defaults" "5%, 2 windows, $(fact allowed) scans allowed" ;;
    none|unreadable|*"rate=None"*)
      check 4.1 INFO "the explorer is installed and the agent reports exploring at its defaults" "installed; the agent hasn't written its exploring status (status: ${ast:-none})" ;;
    *) check 4.1 FAIL "the explorer is installed and the agent reports exploring at its defaults" "status: $ast; expected allowed=$(fact allowed)" ;;
  esac
fi
if [ "${ep_learned:-0}" -eq 0 ]; then
  check 4.2 SKIP "exploring windows are logged, with their chance, in runs of two" "no learned-policy window since the update (policy: ${apol:-?})"
elif [ "${ep_exp:-0}" -eq 0 ]; then
  check 4.2 INFO "exploring windows are logged, with their chance, in runs of two" "none yet in $ep_learned learned window(s)"
elif [ "${ep_nop:-0}" -gt 0 ] || [ "${rn_long:-0}" -gt 0 ]; then
  check 4.2 FAIL "exploring windows are logged, with their chance, in runs of two" "$ep_exp exploring window(s), $ep_nop without a chance; runs: $rn"
elif [ "${rn_twos:-0}" -ge "${rn_ones:-0}" ]; then
  check 4.2 PASS "exploring windows are logged, with their chance, in runs of two" "$ep_exp exploring window(s) in $rn_n run(s): $rn_twos of two, $rn_ones of one"
else
  check 4.2 INFO "exploring windows are logged, with their chance, in runs of two" "more single windows than pairs (a restart or policy change cuts a run short): $rn"
fi
if [ "${ep_rows:-0}" -eq 0 ]; then
  check 4.3 SKIP "no window outside the learned policy is marked as exploring" "no window logged since the update"
elif [ "${ep_off:-0}" -eq 0 ]; then
  check 4.3 PASS "no window outside the learned policy is marked as exploring" "$ep_base scripted-sweep window(s), none exploring"
else
  check 4.3 FAIL "no window outside the learned policy is marked as exploring" "$ep_off exploring window(s) outside the learned policy"
fi
alog=$work/agent-since.log
if [ -n "$since" ]; then
  journalctl -q --no-pager -u kestrel-agent --since "$since" >"$alog" 2>/dev/null
else
  journalctl -q --no-pager -b -u kestrel-agent >"$alog" 2>/dev/null
fi
# The agent logs a line for every exploring window (both of a run of two), so
# the lines are compared with the exploring windows logged since the journal's
# first agent line. The window in progress has its line but no row yet.
trying=$(grep -c 'explore: trying ' "$alog" || true)
jspan="since the update"
[ -n "$jfrom" ] && [ "${jfrom%.*}" -gt "${since_ts:-0}" ] \
  && jspan="since $(date -u -d "@${jfrom%.*}" +%FT%TZ)"
echo "  journal $jspan: $trying 'explore: trying' line(s), ${ep_expj:-0} exploring window(s) logged"
grep 'explore: trying ' "$alog" | tail -3 | jscrub
gap=$(( ${trying:-0} - ${ep_expj:-0} )); [ "$gap" -lt 0 ] && gap=$(( -gap ))
slack=$(( ${ep_expj:-0} / 20 )); [ "$slack" -lt 2 ] && slack=2
if [ "${ep_expj:-0}" -eq 0 ] && [ "${trying:-0}" -eq 0 ]; then
  check 4.4 SKIP "the journal has one 'explore: trying' line per exploring window" "no exploring window $jspan"
elif [ "${trying:-0}" -eq 0 ]; then
  check 4.4 FAIL "the journal has one 'explore: trying' line per exploring window" "${ep_expj} exploring window(s) $jspan, no line"
elif [ "$gap" -le "$slack" ]; then
  check 4.4 PASS "the journal has one 'explore: trying' line per exploring window" "$trying line(s), ${ep_expj:-0} window(s) $jspan"
elif [ "${trying:-0}" -gt "${ep_expj:-0}" ]; then
  check 4.4 INFO "the journal has one 'explore: trying' line per exploring window" "$trying line(s), ${ep_expj:-0} window(s) $jspan (a capture stop mid-window logs a line without a row)"
else
  check 4.4 INFO "the journal has one 'explore: trying' line per exploring window" "$trying line(s), ${ep_expj:-0} window(s) $jspan (fewer lines than windows: journald may have dropped some)"
fi
# 4.5 comes from the dashboard section below.
check 4.6 MANUAL "see the checklist" "by hand (the Exploring row on the phone over a day or two)"
check 4.7 MANUAL "see the checklist" "by hand, optional (explore_rate = 0 turns it off)"

# ---------------------------------------------------------------- 5. regressions from 1.0.14-beta.1: training from scratch now and then (#175)
sec "5. regressions from 1.0.14-beta.1: training from scratch now and then (#175)"
if [ "$(fact import)" = ok ] && [ "$(fact defaults)" = "0.05 2 4 3 0" ] \
    && pkg_has agent/train.py '"--seed"' && pkg_has agent/train.py '"--no-warm-start"'; then
  case ",$(fact overrides)," in
    *,fresh_every,*|*,stuck_contests,*|*,fresh_timesteps,*)
      check 5.1 INFO "from-scratch settings at their defaults; the trainer takes --seed and --no-warm-start" "set in config.toml: $(fact overrides)" ;;
    *) check 5.1 PASS "from-scratch settings at their defaults; the trainer takes --seed and --no-warm-start" "fresh_every 4, stuck_contests 3, fresh_timesteps 0" ;;
  esac
else
  check 5.1 FAIL "from-scratch settings at their defaults; the trainer takes --seed and --no-warm-start" "defaults $(fact defaults)$pkg_note"
fi
runs=$(grep -c ' training finished (' "$alog" || true)
fresh=$(grep -c ' training from scratch (' "$alog" || true)
echo "  since the update: $runs on-device run(s), $fresh from scratch"
grep ' training from scratch (' "$alog" | tail -3 | jscrub
if [ "${fresh:-0}" -ge 1 ]; then
  check 5.2 PASS "a run from scratch comes at least every 4th run" "$fresh of $runs run(s) from scratch"
elif [ "${runs:-0}" -lt 4 ]; then
  check 5.2 INFO "a run from scratch comes at least every 4th run" "$runs run(s) so far; none from scratch yet"
else
  check 5.2 FAIL "a run from scratch comes at least every 4th run" "$runs run(s), none from scratch"
fi
judged=$(grep -c ' candidate vs active (' "$alog" || true)
agreed=$(grep ' candidate vs active (' "$alog" | grep -c 'same choice as the active model on [0-9]*% of rows' || true)
scratch=$(grep -c ' (from scratch) candidate vs active (' "$alog" || true)
echo "  contests since the update: $judged; with the agreement figure: $agreed; from scratch: $scratch"
grep ' candidate vs active (' "$alog" | tail -3 | jscrub
if [ "${judged:-0}" -eq 0 ]; then
  check 5.3 SKIP "contests say how often the candidate chose like the active model" "no contest since the update"
elif [ "$agreed" -eq "$judged" ] && { [ "${fresh:-0}" -eq 0 ] || [ "${scratch:-0}" -ge 1 ]; }; then
  check 5.3 PASS "contests say how often the candidate chose like the active model" "$agreed of $judged; $scratch marked from scratch"
elif [ "${agreed:-0}" -ge 1 ]; then
  check 5.3 INFO "contests say how often the candidate chose like the active model" "$agreed of $judged (a contest with no active model has no figure); $scratch marked from scratch"
else
  check 5.3 FAIL "contests say how often the candidate chose like the active model" "none of $judged"
fi
mt=$(fact metas)
if pkg_has agent/train.py '"fresh": not warm_start' && pkg_has agent/train.py '"seed": int(seed) if seed is not None else None'; then
  check 5.4 PASS "a model trained from scratch records that, with its seed" "trainer records both; model records here: $mt"
else
  check 5.4 FAIL "a model trained from scratch records that, with its seed" "markers missing$pkg_note"
fi
# 5.5 comes from the dashboard section below.
check 5.6 MANUAL "see the checklist" "by hand (train-now --fresh and a plain train-now)"
check 5.7 MANUAL "see the checklist" "by hand (Last contest's agreement after a day or two)"

# ---------------------------------------------------------------- 6. regressions from 1.0.14-beta.1: untried scans aren't taken on trust (#175)
sec "6. regressions from 1.0.14-beta.1: untried scans aren't taken on trust (#175)"
if pkg_has agent/train.py 'def _cautious_floor(per_action_mean, min_support):' \
    && pkg_has agent/train.py 'min_support=min_samples'; then
  check 6.1 PASS "a scan with under 8 logged windows is scored at the weakest well-tried scan's mean" ""
else
  check 6.1 FAIL "a scan with under 8 logged windows is scored at the weakest well-tried scan's mean" "markers missing$pkg_note"
fi
case "$mt" in
  *" new=0 "*) check 6.2 INFO "a model trained since the update lists the scans it had too little data on" "no model from this release kept to look at (a losing candidate is deleted): $mt" ;;
  *" unsupported=0") check 6.2 FAIL "a model trained since the update lists the scans it had too little data on" "$mt" ;;
  *unsupported=*) check 6.2 PASS "a model trained since the update lists the scans it had too little data on" "$mt" ;;
  *) check 6.2 INFO "a model trained since the update lists the scans it had too little data on" "${mt:-no answer}" ;;
esac

# ---------------------------------------------------------------- 7. regressions from 1.0.14-beta.1: scan limits apply to every agent scan (#175)
sec "7. regressions from 1.0.14-beta.1: scan limits apply to every agent scan (#175)"
if pkg_has agent/controller.py 'action = explore_mod.nearest_allowed(wanted, self._action_set)'; then
  check 7.1 PASS "every scan the agent runs is mapped into the [agent.action] lists" ""
else
  check 7.1 FAIL "every scan the agent runs is mapped into the [agent.action] lists" "marker missing$pkg_note"
fi
if [ -z "$cfg_sum" ] || [ "$cfg_sum" != "$cfg_after" ]; then
  check 7.2 FAIL "listen-only lists allow only the 16 listen-only scans; config.toml unchanged" "config.toml checksum changed or unreadable"
elif [ "$(fact passive)" = "16 True" ]; then
  check 7.2 PASS "listen-only lists allow only the 16 listen-only scans; config.toml unchanged" "an in-memory copy; config.toml checksum unchanged"
else
  check 7.2 FAIL "listen-only lists allow only the 16 listen-only scans; config.toml unchanged" "allowed: $(fact passive)${ferr:+ (error: $ferr)}"
fi
if [ "${ep_rows:-0}" -eq 0 ]; then
  check 7.3 SKIP "every window logged since the update ran an allowed scan" "no window logged since the update"
elif [ "${ep_out:-0}" -eq 0 ]; then
  check 7.3 PASS "every window logged since the update ran an allowed scan" "$ep_rows window(s), $(fact allowed) scans allowed"
else
  check 7.3 FAIL "every window logged since the update ran an allowed scan" "$ep_out of $ep_rows outside the lists"
fi
check 7.4 MANUAL "see the checklist" "by hand, optional (listen-only lists on the device)"

# ---------------------------------------------------------------- 8. regressions from 1.0.14-beta.1: GPS: captures tagged only with a current location (#174)
sec "8. regressions from 1.0.14-beta.1: GPS: captures tagged only with a current location (#174)"
echo "gps: enabled=$(cfg plugins.gps.enabled false)"
if grep -q 'FIX_MAX_AGE_S = SIGNAL_STALE_S' "$LIBDIR/plugins/gps.py" 2>/dev/null \
    && grep -q 'time.monotonic() - taken > FIX_MAX_AGE_S' "$LIBDIR/plugins/gps.py" 2>/dev/null \
    && grep -q 'if not self._fix or self._mode < 2:' "$LIBDIR/plugins/gps.py" 2>/dev/null; then
  check 8.1 PASS "gps stops sharing its position once the fix is lost or over 30 s old" ""
else
  check 8.1 FAIL "gps stops sharing its position once the fix is lost or over 30 s old" "markers missing"
fi
check 8.2 MANUAL "see the checklist" "by hand (lose the fix while capturing)"

# ---------------------------------------------------------------- 9. regressions from 1.0.14-beta.1: the user guide (#175)
sec "9. regressions from 1.0.14-beta.1: the user guide (#175)"
# 9.1 comes from the dashboard section below.
check 9.2 MANUAL "see the checklist" "by hand (What's new reads normally)"

# ---------------------------------------------------------------- 10. regressions from 1.0.13: captured networks search (#167)
sec "10. regressions from 1.0.13: captured networks search (#167)"
# 10.1: the by-AP search matches BSSIDs only for a MAC-shaped query. Code marker only.
if pkg_has web.py 'def _mac_query(query):' && pkg_has web.py 'mac_needle = _mac_query(query)'; then
  check 10.1 PASS "a by-AP search matches BSSIDs only when it looks like one" ""
else
  check 10.1 FAIL "a by-AP search matches BSSIDs only when it looks like one" "markers missing$pkg_note"
fi
# 10.2 comes from the dashboard section below.
check 10.3 MANUAL "see the checklist" "by hand (a name search and a pasted BSSID on the phone)"

# ---------------------------------------------------------------- 11. regressions from 1.0.13: the agent's coverage figure (#169)
sec "11. regressions from 1.0.13: the agent's coverage figure (#169)"
if pkg_has agent/env.py 'if event is not None and event.bssid and not event.apless:'; then
  check 11.1 PASS "the agent's coverage figure skips AP-less handshakes" ""
else
  check 11.1 FAIL "the agent's coverage figure skips AP-less handshakes" "marker missing$pkg_note"
fi

# ---------------------------------------------------------------- 12. regressions from 1.0.13: recovered passphrases matched by network (#161)
sec "12. regressions from 1.0.13: recovered passphrases matched by network (#161)"
# 12.1: the installed package reads the client and the AP-less bit from each
# hashline and matches a recovered passphrase by network, and the dashboard's
# count and the map use that match. Code markers only; no capture is read.
if pkg_has capture.py '_APLESS_BIT = 0x10' \
    && pkg_has capture.py 'def handshake_keys(self):' \
    && pkg_has recovered.py 'def match_records(records, keys):' \
    && pkg_has recovered.py 'def capture_matches(self, capture):' \
    && pkg_has daemon.py 'recovered_bssids(self.capture)' \
    && grep -q 'recovered_bssids(self.context.capture)' "$LIBDIR/plugins/map.py" 2>/dev/null; then
  check 12.1 PASS "recovered passphrases are matched by network (package, dashboard count, map)" ""
else
  check 12.1 FAIL "recovered passphrases are matched by network (package, dashboard count, map)" "markers missing$pkg_note"
fi
# 12.2 comes from the dashboard section below.
check 12.3 MANUAL "see the checklist" "by hand (a recovered result for a phone-answered capture)"
check 12.4 MANUAL "see the checklist" "by hand (earlier imports, and a new import keeping the client)"

# ---------------------------------------------------------------- 13. regressions from 1.0.13: rogue addresses aren't access points (#162)
sec "13. regressions from 1.0.13: rogue addresses aren't access points (#162)"
# 13.1: the installed package leaves rogue addresses out of the access-point
# counts and locations, and the map doesn't draw them. Code markers only.
if pkg_has capture.py 'def apless_addresses(self):' \
    && pkg_has capture.py 'bssids -= self.apless_addresses()' \
    && pkg_has web.py 'Not an access point' \
    && grep -q 'rogue = self.context.capture.apless_addresses()' "$LIBDIR/plugins/map.py" 2>/dev/null; then
  check 13.1 PASS "rogue addresses are left out of the access-point counts, locations and the map" ""
else
  check 13.1 FAIL "rogue addresses are left out of the access-point counts, locations and the map" "markers missing$pkg_note"
fi
# 13.2 comes from the dashboard section below.
check 13.3 MANUAL "see the checklist" "by hand (rogue address rows, the map and the e-ink AP count)"

# ---------------------------------------------------------------- 14. regressions from 1.0.13: wigle skips AP-less handshakes (#163)
sec "14. regressions from 1.0.13: wigle skips AP-less handshakes (#163)"
if grep -q 'if getattr(event, "apless", False):' "$LIBDIR/plugins/wigle.py" 2>/dev/null; then
  check 14.1 PASS "the installed wigle skips AP-less handshakes" ""
else
  check 14.1 FAIL "the installed wigle skips AP-less handshakes" "marker missing"
fi

# ---------------------------------------------------------------- 15. regressions from 1.0.13: census: a fresh socket after the tether reconnects (#156)
sec "15. regressions from 1.0.13: census: a fresh socket after the tether reconnects (#156)"
census_on=$(cfg plugins.census.enabled false)
census_tether=$(cfg plugins.census.tether_only true)
echo "census: enabled=$census_on tether_only=$census_tether"
# 15.1: the installed census checks its socket's binding before every send, and
# opens a fresh socket at the start of a cycle when the tether has reconnected.
if grep -q 'def bound(self):' "$LIBDIR/plugins/_census_dht.py" 2>/dev/null \
    && grep -q 'if not self.bound():' "$LIBDIR/plugins/_census_dht.py" 2>/dev/null \
    && grep -q 'the tether reconnected; opening a fresh socket' "$LIBDIR/plugins/census.py" 2>/dev/null; then
  check 15.1 PASS "the census checks its socket's binding and reopens it after a reconnect" ""
else
  check 15.1 FAIL "the census checks its socket's binding and reopens it after a reconnect" "the installed census is older"
fi
if [ "$census_on" != true ]; then
  check 15.2 SKIP "the census socket is bound to the tether" "census not enabled"
else
  # 15.2: with tether_only, the daemon's census socket is bound to the tether.
  # Counts only: no socket address is printed.
  dpid=$(systemctl show -p MainPID --value kestrel-daemon.service 2>/dev/null)
  # A Bluetooth PAN link has no carrier state, so a working tether reads
  # "unknown": count it as up when the interface itself is up (IFF_UP).
  tether_up=$(cat /sys/class/net/bnep0/operstate 2>/dev/null || echo absent)
  tether_flags=$(cat /sys/class/net/bnep0/flags 2>/dev/null || echo 0)
  if [ "$tether_up" = unknown ] && [ $((tether_flags & 1)) -eq 1 ]; then
    tether_up=up
  fi
  # The kernel's own answer: each of the daemon's UDP sockets, by the interface
  # it's bound to (names and counts only). A copy of each descriptor is asked and
  # closed at once (pidfd_getfd, as root); the daemon's sockets are untouched. Not
  # ss: without the udp_diag module it reads /proc/net/udp, which never shows the
  # interface, so a bound socket looks unbound there.
  socks=$(/usr/bin/python3 -I - "${dpid:-0}" <<'PY' 2>/dev/null
import collections, ctypes, os, socket, sys
pid = int(sys.argv[1])
try:
    pidfd = os.pidfd_open(pid)
except (OSError, AttributeError, ValueError) as exc:
    print("unknown(%s)" % type(exc).__name__)
    sys.exit(0)
libc = ctypes.CDLL(None, use_errno=True)
counts = collections.Counter()
for name in os.listdir("/proc/%d/fd" % pid):
    try:
        if not os.readlink("/proc/%d/fd/%s" % (pid, name)).startswith("socket:"):
            continue
    except OSError:
        continue
    dup = libc.syscall(438, pidfd, int(name), 0)   # pidfd_getfd
    if dup < 0:
        counts["not-inspected"] += 1
        continue
    s = socket.socket(fileno=dup)
    try:
        if s.family == socket.AF_INET and s.type == socket.SOCK_DGRAM:
            dev = s.getsockopt(socket.SOL_SOCKET, 25, 16).rstrip(b"\0").decode()
            counts[dev or "any"] += 1
    finally:
        s.close()
print(" ".join("%s=%d" % kv for kv in sorted(counts.items())))
PY
)
  bound=$(printf '%s\n' "$socks" | grep -oE '(^| )bnep0=[0-9]+' | cut -d= -f2)
  # Why, when it isn't: the sockets above, when this daemon started, and the
  # census's own pause and failure lines from this daemon process (time and
  # message only; the census logs no addresses).
  if [ "$census_tether" = true ]; then
    echo "  daemon started: $(systemctl show -p ActiveEnterTimestamp --value kestrel-daemon.service 2>/dev/null)"
    echo "  daemon UDP sockets by interface: ${socks:-none}"
    echo "  bnep0: $tether_up"
    clog=$(journalctl -q --no-pager -o short-iso _PID="${dpid:-0}" 2>/dev/null \
      | grep -E 'census (paused|cycle failed)|census: (the link went down|the tether reconnected|couldn.t write slot|entry missing)' \
      | sed -E 's/^([^ ]+) [^ ]+ [^:]+: /\1 /')
    echo "  census pauses, failures and reconnects since the daemon started: $(printf '%s' "$clog" | grep -c . || true)"
    printf '%s\n' "$clog" | grep . | tail -8 | jscrub
  fi
  # When the tether last came up (NetworkManager's activation line; the time
  # only) and when the census last reported (its status file, read as the
  # device user), so a socket from before a reconnect isn't judged as unbound.
  # NetworkManager names a Bluetooth device by its address, not bnep0, so the
  # line is matched by that shape; only the leading timestamp is kept.
  tether_since=$(journalctl -q --no-pager -o short-unix -u NetworkManager --since "-3 days" 2>/dev/null \
    | grep -E 'device \((bnep0|([0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2})\): Activation: successful' \
    | tail -1 | cut -d. -f1)
  census_wrote=$(as_dev /usr/bin/python3 -I -c 'import json, sys
try:
    d = json.load(open(sys.argv[1]))
    print(int(max(float((d.get(k) or {}).get("at") or 0) for k in ("write", "read"))) or "")
except Exception:
    pass' "$DEV_HOME/.local/state/kestrel-census/status.json" 2>/dev/null)
  echo "  tether up since: ${tether_since:-unknown}  census last push or read: ${census_wrote:-unknown} (epoch seconds)"
  if [ "$census_tether" != true ]; then
    check 15.2 SKIP "the census socket is bound to the tether" "tether_only is off"
  elif [ "$tether_up" != up ]; then
    check 15.2 SKIP "the census socket is bound to the tether" "bnep0 is $tether_up"
  elif [ "${socks#unknown}" != "$socks" ]; then
    check 15.2 INFO "the census socket is bound to the tether" "can't read the daemon's sockets: $socks"
  elif [ "${bound:-0}" -ge 1 ]; then
    check 15.2 PASS "the census socket is bound to the tether" "$bound daemon UDP socket(s) on bnep0"
  elif [ -n "$tether_since" ] && [ -n "$census_wrote" ] && [ "$tether_since" -gt "$census_wrote" ]; then
    # A socket from before the tether came back: the census sends nothing on
    # it and opens a fresh one at its next cycle, so judge it then.
    check 15.2 INFO "the census socket is bound to the tether" "the tether came back $(( $(date +%s) - tether_since ))s ago, after the census's last push or read ($(( $(date +%s) - census_wrote ))s ago); rerun after its next cycle"
  else
    check 15.2 FAIL "the census socket is bound to the tether" "no daemon UDP socket on bnep0 (see the sockets and census lines above)"
  fi
fi
check 15.3 MANUAL "see the checklist" "by hand (the tether off and on, then this check again)"

# ---------------------------------------------------------------- 16. regressions from 1.0.13: map: Last 24 hours and Reset view (#154)
sec "16. regressions from 1.0.13: map: Last 24 hours and Reset view (#154)"
# 16.1 and 16.2 come from the dashboard section below.
check 16.3 MANUAL "see the checklist" "by hand (the map's filters and Reset view on the phone)"

# ---------------------------------------------------------------- 17. regressions from 1.0.13: plugins page: View buttons (#155)
sec "17. regressions from 1.0.13: plugins page: View buttons (#155)"
# 17.1 comes from the dashboard section below.

# ---------------------------------------------------------------- 18. regressions from 1.0.13: privileged helpers (#145)
sec "18. regressions from 1.0.13: privileged helpers (#145)"
# 18.1 and 18.2 are in the config tests below; 18.3 comes from the dashboard.
# 18.4: trigger files root drops for the device user's processes are made as
# that user: regular files it owns, never links. Metadata only, read as the
# device user, and the files are often already consumed (then nothing to see).
rt_dir=$(cfg plugins.remote-train.agent_dir /var/lib/kestrel/agent)
dev_uid=$(id -u "$DEV_USER" 2>/dev/null)
trig=""
trig_bad=""
for f in "$AGENT_DIR/train-request" "$rt_dir/.remote-train-now"; do
  meta=$(as_dev find -P "$f" -maxdepth 0 -printf '%y %U %n' 2>/dev/null)
  [ -n "$meta" ] || continue
  read -r t_type t_uid t_links <<<"$meta"
  if [ "$t_type" = f ] && [ "$t_uid" = "$dev_uid" ] && [ "$t_links" = 1 ]; then
    trig="$trig ${f##*/}: a regular file of the device user's;"
  else
    trig_bad="$trig_bad ${f##*/}: type $t_type, uid $t_uid, links $t_links;"
  fi
done
if [ -n "$trig_bad" ]; then
  check 18.4 FAIL "trigger files are regular files owned by the device user" "$trig_bad"
elif [ -n "$trig" ]; then
  check 18.4 PASS "trigger files are regular files owned by the device user" "$trig"
else
  check 18.4 INFO "trigger files are regular files owned by the device user" "none present now (they go once picked up); see 18.6"
fi
# 18.5: the login helper's shared failure count lives in a root-only folder.
if [ -e /run/kestrel-auth ] || [ -L /run/kestrel-auth ]; then
  ad=$(stat -c '%F %a %U' /run/kestrel-auth 2>/dev/null)
  af=$(stat -c '%F %a %U' /run/kestrel-auth/fails.json 2>/dev/null)
  if [ "$ad" = "directory 700 root" ] && { [ -z "$af" ] || [ "$af" = "regular file 600 root" ]; }; then
    check 18.5 PASS "the login helper's state folder is root-only" "folder: $ad; fails.json: ${af:-none}"
  else
    check 18.5 FAIL "the login helper's state folder is root-only" "folder: $ad; fails.json: ${af:-none}"
  fi
else
  check 18.5 INFO "the login helper's state folder is root-only" "not made yet (no failed sign-in since boot)"
fi
check 18.6 MANUAL "see the checklist" "by hand (train-now and remote-train now still work)"
check 18.7 MANUAL "see the checklist" "by hand (a root-only setting from the terminal)"
check 18.8 MANUAL "see the checklist" "by hand (beta updates switched from the terminal)"

# ---------------------------------------------------------------- 19. regressions from 1.0.13: the dashboard: sign-in and limits (#146)
sec "19. regressions from 1.0.13: the dashboard: sign-in and limits (#146)"
# 19.1: the daemon's unit and its running process.
d_pt=$(systemctl show -p PrivateTmp --value kestrel-daemon.service 2>/dev/null)
d_nf=$(systemctl show -p LimitNOFILE --value kestrel-daemon.service 2>/dev/null)
d_pid=$(systemctl show -p MainPID --value kestrel-daemon.service 2>/dev/null)
d_run=$(awk '/^Max open files/ {print $4}' "/proc/${d_pid:-0}/limits" 2>/dev/null)
if [ "$d_pt" = yes ] && [ "$d_nf" = 4096 ] && [ "$d_run" = 4096 ]; then
  check 19.1 PASS "the daemon runs with PrivateTmp and LimitNOFILE=4096" "unit and running process"
else
  check 19.1 FAIL "the daemon runs with PrivateTmp and LimitNOFILE=4096" "PrivateTmp=$d_pt LimitNOFILE=$d_nf running=${d_run:-?}"
fi
# 19.2-19.4 come from the dashboard section below (19.4 runs last).
check 19.5 MANUAL "see the checklist" "by hand (the sign-in page's wait message)"
check 19.6 MANUAL "see the checklist" "by hand (a potfile import and its limit)"
check 19.7 MANUAL "see the checklist" "by hand (What's new reads normally)"

# ---------------------------------------------------------------- 20. regressions from 1.0.13: updates (#140, #148)
sec "20. regressions from 1.0.13: updates (#140, #148)"
eng=$LIBDIR/update-apply
if grep -q '^class _Redirects(urllib.request.HTTPRedirectHandler):' "$eng" 2>/dev/null \
    && grep -q '^MAX_REDIRECTS = 5$' "$eng" && grep -q 'raise Refused("the release server redirected to a non-https address")' "$eng" \
    && grep -q 'out.remove_header("Authorization")' "$eng" && grep -q '_OPENER.open(req, timeout=timeout)' "$eng"; then
  check 20.1 PASS "the update engine follows https redirects only, at most five, token kept on the host" ""
else
  check 20.1 FAIL "the update engine follows https redirects only, at most five, token kept on the host" "markers missing in $eng"
fi
if grep -qF 'GIT_CONFIG_KEY_1="http.followRedirects" GIT_CONFIG_VALUE_1="false"' "$CLI" 2>/dev/null \
    && grep -qF 'GIT_CONFIG_COUNT=2' "$CLI"; then
  check 20.2 PASS "the CLI's token clone follows no redirect" ""
else
  check 20.2 FAIL "the CLI's token clone follows no redirect" "markers missing in $CLI"
fi
check 20.3 MANUAL "see the checklist" "by hand (a private-repo update, if you use one)"

# ---------------------------------------------------------------- 21. regressions from 1.0.13: census (#141, #147)
sec "21. regressions from 1.0.13: census (#141, #147)"
# 21.1 comes from the dashboard section below.
cp=$LIBDIR/plugins
if grep -q '^DAY_ENTRIES = SLOTS \* MAX_ENTRIES \* (ALTERNATES + 1)$' "$cp/_census_proto.py" 2>/dev/null \
    && grep -q '^SLOTS = 64$' "$cp/_census_proto.py" && grep -q '^ALTERNATES = 3$' "$cp/_census_proto.py" \
    && grep -q '^MAX_ENTRIES = 49 ' "$cp/_census_proto.py" \
    && grep -q '^def union(values, day, bits=POW_BITS, limit=MAX_ENTRIES):' "$cp/_census_proto.py" \
    && grep -q 'len(rows) >= proto.DAY_ENTRIES' "$cp/census.py" \
    && grep -q 'def _take(data, src, pending, results):' "$cp/_census_dht.py"; then
  check 21.2 PASS "census reads keep at most a slot's worth per slot and 12,544 a day" "and malformed replies are set aside"
else
  check 21.2 FAIL "census reads keep at most a slot's worth per slot and 12,544 a day" "markers missing in the installed census files"
fi
# 21.3: the saved census state holds no more than a full census for any day.
# Counts only, read as the device user.
seen=$(as_dev /usr/bin/python3 -I - "$DEV_HOME/.local/state/kestrel-census/seen.json" <<'PY' 2>&1 | tail -1
import json, sys
try:
    data = json.load(open(sys.argv[1], encoding="utf-8"))
except FileNotFoundError:
    print("missing"); sys.exit(0)
except Exception as exc:
    print("unreadable", type(exc).__name__); sys.exit(0)
days = data.get("days") if isinstance(data, dict) else None
counts = [len((d or {}).get("entries") or {}) for d in (days or {}).values() if isinstance(d, dict)]
over = sum(int((d or {}).get("over") or 0) for d in (days or {}).values() if isinstance(d, dict))
print("days %d, most in a day %d, not counted %d" % (len(counts), max(counts or [0]), over))
PY
)
case "$seen" in
  missing) check 21.3 SKIP "no census day holds more than 12,544 entries" "no seen.json (census never read)" ;;
  days\ *)
    most=$(printf '%s' "$seen" | sed -n 's/.*most in a day \([0-9]*\).*/\1/p')
    if [ "${most:-0}" -le 12544 ]; then
      check 21.3 PASS "no census day holds more than 12,544 entries" "$seen"
    else
      check 21.3 FAIL "no census day holds more than 12,544 entries" "$seen"
    fi ;;
  *) check 21.3 FAIL "no census day holds more than 12,544 entries" "$seen" ;;
esac
check 21.4 MANUAL "see the checklist" "by hand (the Census page after a daemon restart)"

# ---------------------------------------------------------------- 22. regressions from 1.0.13: plugin pages (#142-#144)
sec "22. regressions from 1.0.13: plugin pages (#142-#144)"
# 22.1 comes from the dashboard section below.
check 22.2 MANUAL "see the checklist" "by hand (each plugin page's links on the phone)"
check 23.1 MANUAL "see the checklist" "by hand, optional (a build with your own password hash)"

since=""
[ -f "$upd" ] && since="@$(stat -c %Y "$upd")"
# ---------------------------------------------------------------- 24. regressions from 1.0.12: remote-train 0.2.0 (#129)
sec "24. regressions from 1.0.12: remote-train 0.2.0: an unreachable host, and the remote-training service (#129)"
rt=$LIBDIR/plugins/remote-train.py
rt_ver=$(sed -n 's/^ *__version__ = "\([0-9.]*\)".*/\1/p' "$rt" 2>/dev/null | head -1)
if [ "$rt_ver" = "0.2.0" ] && grep -q '^ *__provides__ = ("remote-training",)' "$rt" \
    && grep -q 'def provide_remote_training' "$rt"; then
  check 24.1 PASS "remote-train 0.2.0 is installed and provides remote-training" ""
else
  check 24.1 FAIL "remote-train 0.2.0 is installed and provides remote-training" "version ${rt_ver:-?}"
fi
# 24.2: the last contact and last model survive a restart, in a status file in
# the agent's own folder (read as the device user; ages only, no host).
if [ "$(cfg plugins.remote-train.enabled false)" != true ]; then
  check 24.2 SKIP "remote-train keeps its last contact and model in remote-status.json" "remote-train not enabled"
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
    ok\ *) check 24.2 PASS "remote-train keeps its last contact and model in remote-status.json" "${rs#ok }" ;;
    missing) check 24.2 INFO "remote-train keeps its last contact and model in remote-status.json" "not written yet (no SSH session since the update?)" ;;
    *) check 24.2 FAIL "remote-train keeps its last contact and model in remote-status.json" "$rs" ;;
  esac
fi
check 24.3 MANUAL "see the checklist" "by hand (an unreachable host on the card)"
check 24.4 MANUAL "see the checklist" "by hand (the card after a daemon restart)"

# ---------------------------------------------------------------- 25. regressions from 1.0.12: the census plugin (#130, #131)
sec "25. regressions from 1.0.12: the census plugin (#130, #131)"
bad=""
for f in census.py _census_dht.py _census_proto.py; do
  st=$(stat -c '%U:%G %a' "$LIBDIR/plugins/$f" 2>/dev/null || echo missing)
  [ "$st" = "root:root 644" ] || bad="$bad $f=$st"
done
[ -z "$bad" ] && check 25.1 PASS "census and its two helper files are installed, root-owned 0644" "" \
  || check 25.1 FAIL "census and its two helper files are installed, root-owned 0644" "$bad"
# 25.2 and 25.3 are in the config tests below; 25.4 and 25.8 come from the dashboard.
# census_on and census_tether are read in section 15.
cstate=$DEV_HOME/.local/state/kestrel-census
if [ "$census_on" != true ]; then
  for id in 25.5 25.6 25.7; do
    check "$id" SKIP "census state and traffic" "census not enabled"
  done
else
  # 25.5-25.7: its state, read as the device user. Counts, hours and modes only:
  # never a token, slot or node address.
  as_dev /usr/bin/python3 -I - "$cstate" "$(cfg plugins.census.daily_mb 5)" <<'PY' 2>&1
import json, os, stat, sys, time
d, budget_mb = sys.argv[1], sys.argv[2]
def chk(cid, result, what, detail=""):
    print("CHECK %-5s %-6s %s :: %s" % (cid, result, what, detail))
try:
    st = os.lstat(d)
except FileNotFoundError:
    for cid in ("25.5", "25.6", "25.7"):
        chk(cid, "INFO", "census state", "no state folder yet (paused since it was enabled?)")
    sys.exit(0)
files = sorted(n for n in os.listdir(d) if not n.endswith(".tmp"))
modes = {n: oct(os.lstat(os.path.join(d, n)).st_mode & 0o777) for n in files}
ok = stat.S_IMODE(st.st_mode) == 0o700 and all(m == "0o600" for m in modes.values())
chk("25.5", "PASS" if ok else "FAIL", "the census state folder is 0700 and its files 0600",
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
    chk("25.6", "PASS", "the census reported this hour or the last", "last report hour %02d UTC; %s" % (wrote, use))
else:
    chk("25.6", "INFO", "the census reported this hour or the last",
        "last report hour %s; %s" % (wrote if wrote is not None else "none today", use))
seen = ((load("seen.json") or {}).get("days") or {}).get(today) or {}
read_at = seen.get("read_at")
entries = seen.get("entries") or {}
mine = day.get("token") in entries if day.get("day") == today else False
if not isinstance(read_at, (int, float)):
    chk("25.7", "INFO", "the census was read today and this device is in it", "not read yet today")
else:
    chk("25.7", "PASS" if mine else "FAIL", "the census was read today and this device is in it",
        "read %d min ago: %d devices seen, %s online; this device %s"
        % ((time.time() - read_at) // 60, len(entries), seen.get("online"),
           "is in it" if mine else "is NOT in it"))
PY
fi
check 25.9 MANUAL "see the checklist" "by hand (the tether off and on)"
check 25.10 MANUAL "see the checklist" "by hand (the desktop census report)"

# ---------------------------------------------------------------- 26. regressions from 1.0.11: AngryOxide stopped cleanly (#126)
sec "26. regressions from 1.0.11: AngryOxide stopped cleanly (#126)"
# The unit: systemd signals only the launcher, with time for a clean stop.
km=$(systemctl show -p KillMode --value kestrel-capture.service 2>/dev/null)
ts=$(systemctl show -p TimeoutStopUSec --value kestrel-capture.service 2>/dev/null)
echo "  kestrel-capture: KillMode=$km TimeoutStopUSec=$ts"
if [ "$km" = mixed ] && [ -n "$ts" ] && [ "$ts" != infinity ]; then
  check 26.1 PASS "the capture unit signals only the launcher, with a stop timeout" "KillMode=$km, TimeoutStopUSec=$ts"
else
  check 26.1 FAIL "the capture unit signals only the launcher, with a stop timeout" "KillMode=$km, TimeoutStopUSec=$ts"
fi
# The running AngryOxide catches SIGINT (its handler is installed and SIGINT
# isn't ignored): the signal masks from /proc only.
ao=$(pgrep -x angryoxide | head -1)
if [ -z "$ao" ]; then
  check 26.2 SKIP "the running AngryOxide catches SIGINT" "AngryOxide is not running"
else
  cgt=$(awk '/^SigCgt:/ {print $2}' "/proc/$ao/status" 2>/dev/null)
  ign=$(awk '/^SigIgn:/ {print $2}' "/proc/$ao/status" 2>/dev/null)
  echo "  angryoxide signal masks: SigCgt=$cgt SigIgn=$ign"
  if [ -n "$cgt" ] && [ $(( 0x$cgt & 2 )) -ne 0 ] && [ $(( 0x${ign:-0} & 2 )) -eq 0 ]; then
    check 26.2 PASS "the running AngryOxide catches SIGINT" "SIGINT caught, not ignored"
  else
    check 26.2 FAIL "the running AngryOxide catches SIGINT" "SigCgt=$cgt SigIgn=$ign"
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
  check 26.3 SKIP "the last stop was clean and left no kismet-journal" "capture is running (run once with it off)"
elif [ -z "$stops" ]; then
  check 26.3 SKIP "the last stop was clean and left no kismet-journal" "no stop since the update"
else
  last=${stops% }; last=${last##* }
  if [ "$last" = 0 ] && [ "$left" -eq 0 ]; then
    check 26.3 PASS "the last stop was clean and left no kismet-journal" "rc=0, no journal left"
  else
    check 26.3 FAIL "the last stop was clean and left no kismet-journal" "rc=$last, $left journal file(s) left"
  fi
fi
if [ -z "$relaunches" ] && [ "${killed:-0}" -eq 0 ]; then
  check 26.4 SKIP "every relaunch and roll since the update stopped AngryOxide cleanly" "none since the update"
elif [ "${killed:-0}" -eq 0 ] && [ -z "$(printf '%s\n' $relaunches | grep -vx 0 || true)" ]; then
  check 26.4 PASS "every relaunch and roll since the update stopped AngryOxide cleanly" "exit codes: $relaunches"
else
  check 26.4 FAIL "every relaunch and roll since the update stopped AngryOxide cleanly" "exit codes: ${relaunches:-none}; killed: $killed"
fi
check 26.5 MANUAL "see the checklist" "by hand (stop and start capture from the dashboard)"

# ---------------------------------------------------------------- 27. regressions from 1.0.11-beta.2: fair model contests (#120)
sec "27. regressions from 1.0.11-beta.2: fair model contests (#120)"
# The contest code, imported as the device user (no torch: model loads are lazy).
fair=$(as_dev "$VENV_PY" -I -c 'from kestrel_pi.agent import controller, metrics, train
print(metrics.HOLDOUT_SCHEME, hasattr(metrics, "contest_decision"),
      hasattr(controller.AgentController, "_run_contest"), hasattr(train, "evaluate"),
      hasattr(metrics, "promotion_decision"))' 2>&1 | tail -1)
echo "  contest code: $fair"
if [ "$fair" = "block-hash-v1 True True True False" ]; then
  check 27.1 PASS "the fair-contest code is installed and the old promotion rule is gone" "$fair"
else
  check 27.1 FAIL "the fair-contest code is installed and the old promotion rule is gone" "$(printf '%s' "$fair" | cut -c1-120)"
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
  none|unreadable*) check 27.2 SKIP "the agent publishes the contest tally and last decision" "no agent status" ;;
  shape=contests*) check 27.2 PASS "the agent publishes the contest tally and last decision" "$tally" ;;
  *) check 27.2 FAIL "the agent publishes the contest tally and last decision" "$tally" ;;
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
  check 27.3 SKIP "each on-device run since the update was followed by a contest" "no run or contest yet"
elif [ "${contests:-0}" -ge "${runs:-0}" ] && [ "${contests:-0}" -ge 1 ]; then
  check 27.3 PASS "each on-device run since the update was followed by a contest" "$runs run(s), $contests contest line(s)"
else
  check 27.3 FAIL "each on-device run since the update was followed by a contest" "$runs run(s), $contests contest line(s)"
fi
# remote-train: the host trains the agent's own algorithm. Algorithm names and
# counts only; the host is never named.
if [ "$(cfg plugins.remote-train.enabled false)" != true ]; then
  check 27.4 SKIP "the training host trains the agent's own algorithm" "remote-train not enabled"
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
    check 27.4 PASS "the training host trains the agent's own algorithm" "$algo: $launched launch(es); host models: ${slugs:-none}"
  else
    check 27.4 INFO "the training host trains the agent's own algorithm" "not yet ($algo launches once enough new rows have been logged)"
  fi
fi
# 27.5 comes from the dashboard section below.
check 27.6 MANUAL "see the checklist" "by hand (a day of capture, then the diagnostics script)"

# ---------------------------------------------------------------- 27. regressions from 1.0.11-beta.2: every desktop script in the image (#121)
sec "27. regressions from 1.0.11-beta.2: every desktop script in the image (#121)"
want="README.md:root:644 gps-check.sh:root:755 pull-agent-diagnostics.sh:root:755 pull-connectivity-diagnostics.sh:root:755 pull-eink-diagnostics.sh:root:755 pull-health.sh:root:755 pull-web-diagnostics.sh:root:755"
have=$(cd "$SCRIPTS_DIR" 2>/dev/null && for f in *; do [ -f "$f" ] && printf '%s:%s ' "$f" "$(stat -c '%U:%a' "$f")"; done)
echo "  installed: ${have:-none}"
if [ "$(printf '%s\n' $have | sort | tr '\n' ' ')" = "$(printf '%s\n' $want | sort | tr '\n' ' ')" ]; then
  check 27.7 PASS "every desktop script is installed, .sh 0755 and the README 0644" "7 files"
else
  check 27.7 FAIL "every desktop script is installed, .sh 0755 and the README 0644" "see the list above"
fi
check 27.8 MANUAL "see the checklist" "by hand (runs on a computer)"

# ---------------------------------------------------------------- 27. regressions from 1.0.11-beta.2: the release version on the dashboard (#123)
sec "27. regressions from 1.0.11-beta.2: the release version on the dashboard (#123)"
# 27.9 and 27.10 come from the dashboard section below.

# ---------------------------------------------------------------- 28. regressions from 1.0.11-beta.1: AngryOxide v0.9.4 (#111)
sec "28. regressions from 1.0.11-beta.1: AngryOxide v0.9.4 (#111)"
# --version only: it opens no interface. A test build set through ANGRYOXIDE in
# the capture service (a systemd drop-in) is noted; the stock binary is checked.
aov=$(timeout 10 /usr/bin/angryoxide --version 2>/dev/null | head -1)
ao_env=$(systemctl show kestrel-capture -p Environment --value 2>/dev/null | tr ' ' '\n' | grep -c '^ANGRYOXIDE=' || true)
echo "  /usr/bin/angryoxide: ${aov:-no answer}   ANGRYOXIDE set in the capture service: $ao_env"
case "$aov" in
  *" 0.9.4"|*" 0.9.4 "*) check 28.1 PASS "AngryOxide is v0.9.4" "$aov" ;;
  *) check 28.1 FAIL "AngryOxide is v0.9.4" "${aov:-no answer}" ;;
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
    check 28.2 PASS "the kismet database keeps up with capture" "$lag"
  elif [ -n "$lag_s" ] && [ "$lag_s" -gt 60 ]; then
    check 28.2 FAIL "the kismet database keeps up with capture" "$lag"
  else
    check 28.2 INFO "the kismet database keeps up with capture" "$lag"
  fi
  rss=$(ps -o rss= -C angryoxide 2>/dev/null | awk '{s+=$1} END {print s+0}')
  total=$(awk '/MemTotal/ {print $2}' /proc/meminfo)
  pct=$(( rss * 100 / (total > 0 ? total : 1) ))
  echo "  AngryOxide memory: $((rss / 1024)) MB of $((total / 1024)) MB ($pct%)"
  if [ "$rss" -eq 0 ]; then
    check 28.3 INFO "AngryOxide uses under 30% of RAM" "not running right now"
  elif [ "$pct" -lt 30 ]; then
    check 28.3 PASS "AngryOxide uses under 30% of RAM" "$((rss / 1024)) MB, $pct%"
  else
    check 28.3 FAIL "AngryOxide uses under 30% of RAM" "$((rss / 1024)) MB, $pct%"
  fi
else
  check 28.2 SKIP "the kismet database keeps up with capture" "capture is off"
  check 28.3 SKIP "AngryOxide uses under 30% of RAM" "capture is off"
fi
check 28.4 MANUAL "see the checklist" "by hand (an hour somewhere busy, if not done on beta 1)"

# ---------------------------------------------------------------- 28. regressions from 1.0.11-beta.1: Environment from the capture file (#108)
sec "28. regressions from 1.0.11-beta.1: Environment from the capture file (#108)"
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
  check 28.5 SKIP "capture-status.json has a fresh radio count" "capture is off"
elif [ -n "$age" ] && [ "$age" -le 120 ] && printf '%s' "$envst" | grep -q 'radios=[0-9]'; then
  check 28.5 PASS "capture-status.json has a fresh radio count" "$envst"
else
  check 28.5 FAIL "capture-status.json has a fresh radio count" "$envst"
fi
# 28.6 comes from the dashboard section below.

# ---------------------------------------------------------------- 28. regressions from 1.0.11-beta.1: eink removed (#112)
sec "28. regressions from 1.0.11-beta.1: eink removed (#112)"
left=""
for f in eink.py _eink_epdconfig.py _eink_epd2in7.py _eink_epd2in7_V2.py; do
  [ -e "$LIBDIR/plugins/$f" ] && left="$left $f"
done
miss=""
for f in eink-ws27.py _eink-ws27_epdconfig.py _eink-ws27_epd2in7.py _eink-ws27_epd2in7_V2.py; do
  [ -f "$LIBDIR/plugins/$f" ] || miss="$miss $f"
done
if [ -z "$left$miss" ]; then
  check 28.7 PASS "eink and its helpers are gone; eink-ws27 and its helpers are installed" ""
else
  check 28.7 FAIL "eink and its helpers are gone; eink-ws27 and its helpers are installed" \
    "${left:+still there:$left }${miss:+missing:$miss}"
fi
# 28.8, 28.9 and 28.12 read config.toml's table names and the declarations in the
# config tests below; 28.10 and 28.13 come from the dashboard section.

# ---------------------------------------------------------------- 28. regressions from 1.0.11-beta.1: ups-hat removed (#113)
sec "28. regressions from 1.0.11-beta.1: ups-hat removed (#113)"
if [ ! -e "$LIBDIR/plugins/ups-hat.py" ] && [ -f "$LIBDIR/plugins/ups-ws.py" ]; then
  check 28.11 PASS "ups-hat is gone; ups-ws is installed" ""
else
  check 28.11 FAIL "ups-hat is gone; ups-ws is installed" \
    "ups-hat.py $( [ -e "$LIBDIR/plugins/ups-hat.py" ] && echo present || echo gone), ups-ws.py $( [ -f "$LIBDIR/plugins/ups-ws.py" ] && echo present || echo missing)"
fi

# ---------------------------------------------------------------- 28. regressions from 1.0.11-beta.1: per-ap by default (#116)
sec "28. regressions from 1.0.11-beta.1: per-ap by default (#116)"
mode=$(cfg capture.output.mode per-ap)
if [ "$mode" = per-ap ]; then
  check 28.14 PASS "[capture.output] mode is per-ap" ""
else
  check 28.14 INFO "[capture.output] mode is per-ap" "it is $mode (changed after the flash?)"
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
  check 28.16 SKIP "the store's files are named per access point" "mode is $mode"
elif [ "${n_ap:-0}" -eq 0 ] && [ "${n_other:-0}" -eq 0 ]; then
  check 28.16 SKIP "the store's files are named per access point" "no handshakes captured yet"
elif [ "${n_other:-0}" -eq 0 ]; then
  check 28.16 PASS "the store's files are named per access point" "$layout"
else
  check 28.16 INFO "the store's files are named per access point" "$layout (other names: a restored store?)"
fi
# 28.15 is in the config tests below; 28.17 comes from the dashboard section.

# ---------------------------------------------------------------- 28. regressions from 1.0.11-beta.1: pinned Python packages (#115)
sec "28. regressions from 1.0.11-beta.1: pinned Python packages (#115)"
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
    check 28.18 PASS "the venv's packages match kestrel-constraints.txt" "$(printf '%s' "$pins" | cut -d' ' -f1-2)" ;;
  noconstraints) check 28.18 FAIL "the venv's packages match kestrel-constraints.txt" "no constraints file in the venv" ;;
  *) check 28.18 FAIL "the venv's packages match kestrel-constraints.txt" "$(printf '%s' "$pins" | cut -c1-200)" ;;
esac
case "$pins" in
  *"pytest=False"*) check 28.19 PASS "pytest is not installed on the device" "" ;;
  *"pytest=True"*) check 28.19 FAIL "pytest is not installed on the device" "pytest found" ;;
  *) check 28.19 INFO "pytest is not installed on the device" "$(printf '%s' "$pins" | cut -c1-80)" ;;
esac

# ---------------------------------------------------------------- 29. regressions from 1.0.10
sec "29. regressions from 1.0.10"
gone=$(as_dev "$VENV_PY" -I -c 'import importlib.util
names = ("kestrel_pi.hatkeys", "kestrel_pi.alerts", "kestrel_pi.vendor")
print(" ".join(n for n in names if importlib.util.find_spec(n) is not None) or "none")' 2>&1 | tail -1)
if [ "$gone" = none ]; then
  check 29.1 PASS "plugin-only modules are gone from the installed package" "no hatkeys, alerts or vendor"
else
  check 29.1 FAIL "plugin-only modules are gone from the installed package" "still there: $(printf '%s' "$gone" | cut -c1-120)"
fi
bad=""
for f in _lib_alerts.py _lib_eink_face.py _lib_hat_keys.py _lib_ups.py _lib_ina219.py; do
  st=$(stat -c '%U %a' "$LIBDIR/plugins/$f" 2>/dev/null || echo missing)
  echo "  $f: $st"
  [ "$st" = "root 644" ] || bad="$bad $f($st)"
done
if [ -z "$bad" ]; then
  check 29.2 PASS "the five plugin libraries are installed, root-owned 0644" "alerts, eink_face, hat_keys, ups, ina219"
else
  check 29.2 FAIL "the five plugin libraries are installed, root-owned 0644" "$bad"
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
    check 29.3 PASS "each library loads with the API its plugins ask for" "$apis" ;;
  *) check 29.3 FAIL "each library loads with the API its plugins ask for" "$(printf '%s' "$apis" | cut -c1-160)" ;;
esac
liberr=$(journalctl -b -q --no-pager -u kestrel-daemon 2>/dev/null \
  | grep -cE 'library API|plugin library .* not found|has no LIB_API' || true)
if [ "${liberr:-0}" -eq 0 ]; then
  check 29.4 PASS "no plugin-library load errors in this boot's daemon journal" ""
else
  check 29.4 FAIL "no plugin-library load errors in this boot's daemon journal" "$liberr line(s)"
fi
# 29.5-29.9 are in the config tests below; 29.10 and 29.12 come from the dashboard.
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
  check 29.11 SKIP "one e-ink plugin drives the panel, the other stands down" "no e-ink plugin enabled"
elif [ -z "$inv" ]; then
  check 29.11 INFO "one e-ink plugin drives the panel, the other stands down" "no daemon run to read"
elif [ "$in_use" -eq 0 ] && [ "$n_standing" -eq $((n_on - 1)) ]; then
  check 29.11 PASS "one e-ink plugin drives the panel, the other stands down" "$n_on enabled, $n_standing standing down"
else
  check 29.11 FAIL "one e-ink plugin drives the panel, the other stands down" \
    "$n_on enabled, $n_standing standing down, $in_use 'already in use' line(s)"
fi
nopanel=$(journalctl -b -q --no-pager -u kestrel-daemon 2>/dev/null | grep -c 'no panel (' || true)
plain=$(journalctl -b -q --no-pager -u kestrel-daemon 2>/dev/null | grep -c 'BUSY line never driven (no panel wired)' || true)
if [ -z "$eon" ]; then
  check 29.13 SKIP "with no HAT, the journal says no panel is wired" "no e-ink plugin enabled"
elif [ "${nopanel:-0}" -eq 0 ]; then
  check 29.13 INFO "with no HAT, the journal says no panel is wired" "no 'no panel' line: a panel answered"
elif [ "${plain:-0}" -ge 1 ]; then
  check 29.13 PASS "with no HAT, the journal says no panel is wired" "$plain line(s)"
else
  check 29.13 FAIL "with no HAT, the journal says no panel is wired" "'no panel' without the plain reason"
fi
topic=$DEV_HOME/.local/state/kestrel-ntfy/topic
if as_dev test -f "$topic"; then
  [ "$(as_dev stat -c '%a' "$topic" 2>/dev/null)" = 600 ] \
    && check 29.14 PASS "generated ntfy topic file is private" "0600 (contents not read)" \
    || check 29.14 FAIL "generated ntfy topic file is private" "mode $(as_dev stat -c '%a' "$topic" 2>/dev/null)"
else
  check 29.14 SKIP "generated ntfy topic file is private" "no generated topic"
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
  none) check 29.15 SKIP "the alert queues read back" "no queue file yet" ;;
  *BAD*) check 29.15 FAIL "the alert queues read back" "$queues" ;;
  *) check 29.15 PASS "the alert queues read back" "queued: $queues" ;;
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
  check 29.16 PASS "certificate made and private" "dir 700, cert.pem and key.pem 600"
elif [ "$tls" = off ]; then
  check 29.16 SKIP "certificate made and private" "[web] tls is off"
else
  check 29.16 FAIL "certificate made and private" "see the modes above"
fi
# The fingerprint itself never goes in the report: only whether they match.
fp_cli=$(timeout 20 "$CLI" web fingerprint 2>/dev/null | tail -1)
fp_log=$(journalctl -b -q --no-pager -u kestrel-daemon 2>/dev/null \
  | grep -oE 'HTTPS certificate SHA-256 [0-9A-F:]+' | tail -1 | awk '{print $NF}')
if [ -n "$fp_cli" ] && [ "$fp_cli" = "$fp_log" ]; then
  check 29.17 PASS "kestrel web fingerprint matches the daemon's journal" "values not shown"
elif [ -z "$fp_log" ]; then
  check 29.17 INFO "kestrel web fingerprint matches the daemon's journal" "no HTTPS line in this boot's daemon journal"
else
  check 29.17 FAIL "kestrel web fingerprint matches the daemon's journal" "they differ (values not shown)"
fi
# Listening sockets only (read-only, no request): HTTPS and the forwarder on each
# management address, plain HTTP on loopback. Addresses are counted, not shown.
lst=$(ss -ltnH 2>/dev/null | awk '{print $4}')
https_ext=$(printf '%s\n' "$lst" | grep -E ":$tport\$" | grep -vcE '^(127\.|\[::1\])' || true)
http_ext=$(printf '%s\n' "$lst" | grep -E ":$port\$" | grep -vcE '^(127\.|\[::1\])' || true)
http_lo=$(printf '%s\n' "$lst" | grep -cE "^127\.[0-9.]+:$port\$" || true)
echo "  listeners: $tport off-loopback=$https_ext  $port off-loopback=$http_ext  $port loopback=$http_lo"
if [ "$tls" = off ]; then
  check 29.18 SKIP "HTTPS and the forwarder listen on each management address" "[web] tls is off"
elif [ "${https_ext:-0}" -ge 1 ] && [ "$https_ext" = "$http_ext" ] && [ "${http_lo:-0}" -ge 1 ]; then
  check 29.18 PASS "HTTPS and the forwarder listen on each management address" "$https_ext address(es); loopback plain HTTP too"
else
  check 29.18 FAIL "HTTPS and the forwarder listen on each management address" "see the listener counts above"
fi
# 29.19 (loopback Basic) and 29.20 (nosniff) come from the dashboard section below.
cat_ok=$(as_dev "$VENV_PY" -I -c 'from kestrel_pi import config_catalog as c
a, b = c.find("web", "tls"), c.find("web", "tls_port")
print(bool(a) and bool(b) and a.get("editable") is False and b.get("editable") is False)' 2>&1)
if [ "$cat_ok" = True ]; then
  check 29.21 PASS "Advanced settings lists web.tls and web.tls_port read-only" ""
else
  check 29.21 FAIL "Advanced settings lists web.tls and web.tls_port read-only" "$(printf '%s' "$cat_ok" | tr '\n' ' ' | cut -c1-120)"
fi
# 29.22 (web.tls refused through the settings channel) is in the config tests below.
# The helper run directly with a malformed request: it answers without reading
# any password (a bad request never reaches the check) and writes nothing, and
# exits 0 once it has answered, so a refused request leaves no failed unit.
auth_out=$(printf '{}' | timeout 10 /usr/bin/python3 -I "$CTL" checkauth 2>/dev/null); auth_rc=$?
if [ "$auth_rc" -eq 0 ] && printf '%s' "$auth_out" | grep -q '"ok": false'; then
  check 29.23 PASS "the login helper exits 0 after answering a refused request" "rc=0, refused"
else
  check 29.23 FAIL "the login helper exits 0 after answering a refused request" \
    "rc=$auth_rc reply=$(printf '%s' "$auth_out" | tr -cd '[:alnum:] :{}",_' | cut -c1-80)"
fi
gone_old=""
for old in notify hat-buttons; do
  [ -e "$LIBDIR/plugins/$old.py" ] && gone_old="$gone_old $old"
done
if [ -z "$gone_old" ]; then
  check 29.24 PASS "the replaced notify and hat-buttons plugins are gone" ""
else
  check 29.24 FAIL "the replaced notify and hat-buttons plugins are gone" "still installed:$gone_old"
fi
# 29.25-29.27 come from the dashboard section below.
check 30.1 MANUAL "see the checklist" "the closing step"

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
# 28.8 and 28.11's config half: the shipped tables carry the new names.
check("28.8", "eink-ws27" in tables and "eink" not in tables and "eink" not in plugs,
      "config.toml has [plugins.eink-ws27] and no [plugins.eink]; no eink plugin is listed", "")
check("28.12", "ups-ws" in tables and "ups-hat" not in tables and "ups-hat" not in plugs
      and caps("ups-ws") == {"ui"} and provides("ups-ws") == ("battery",)
      and defaults("ups-ws").get("address") == 0x42
      and not str(decl("ups-ws").get("description") or "").startswith("Deprecated"),
      "config.toml has [plugins.ups-ws] and no [plugins.ups-hat]; ups-ws is local only, "
      "provides battery at 0x42, and isn't marked deprecated", "ups-ws %s" % state("ups-ws"))
# 28.9: eink-ws27 is no longer marked deprecated and keeps its choices.
check("28.9", not str(decl("eink-ws27").get("description") or "").startswith("Deprecated")
      and (opts("eink-ws27").get("variant") or {}).get("choices") == ["auto", "v2", "v1"],
      "eink-ws27 isn't marked deprecated and declares its variant choices",
      "eink-ws27 %s" % state("eink-ws27"))
# 29.5: every shipped plugin is there and readable; no library or helper file,
# and nothing named lib, is listed as a plugin.
expected = {"memtemp", "session-stats", "gps", "wpa-sec", "wigle", "backup", "eink-ws213",
            "eink-ws27", "updates", "remote-train", "downloads", "map", "ntfy", "telegram",
            "ups-ws", "ups-wsc", "ups-wsd", "ups-wse", "census"}
invalid = sorted(n for n, v in plugs.items() if v[0] is None)
missing = sorted(expected - set(plugs))
extra = sorted(set(plugs) - expected)
check("29.5", not invalid and not missing and not any(n.startswith("_") for n in plugs)
      and "lib" not in plugs,
      "all nineteen shipped plugins present and readable; no _ file or lib listed",
      "missing=%s invalid=%s other=%s" % (missing, invalid, extra))
# 29.6: choices where a plugin declares a fixed set, and a value outside them is
# refused through the settings channel with the (throwaway) config unchanged.
c213 = (opts("eink-ws213").get("variant") or {}).get("choices")
c27r = (opts("eink-ws27").get("rotation") or {}).get("choices")
table_before = read("plugins.eink-ws213")
r = apply({"plugin": "eink-ws213", "set": {"variant": "V4"}})
check("29.6", c213 == ["v4", "v3", "v2"] and c27r == [0, 90, 180, 270]
      and not r.get("ok") and read("plugins.eink-ws213") == table_before
      and "one of" in str(r.get("error", "")),
      "e-ink choices declared; a value outside them is refused through the settings channel",
      "error=%s" % str(r.get("error", ""))[:80])
# 29.7: one plugin per alert service, each with only its own secret options.
def secrets(name):
    return {k for k, v in opts(name).items() if (v or {}).get("secret")}
check("29.7", "network" in caps("ntfy") and "network" in caps("telegram")
      and secrets("ntfy") == {"topic", "token"} and secrets("telegram") == {"bot_token", "chat_id"},
      "ntfy and telegram declare network and only their own secrets",
      "ntfy %s, telegram %s" % (state("ntfy"), state("telegram")))
# 29.8: eink-ws27's HAT key options, local only.
o = opts("eink-ws27")
check("29.8", (o.get("buttons") or {}).get("default") is True
      and (o.get("pins") or {}).get("default") == "5,6,13,19"
      and (o.get("hold_seconds") or {}).get("default") == 3
      and (o.get("allow_capture") or {}).get("default") is True
      and (o.get("allow_power") or {}).get("default") is True
      and "network" not in caps("eink-ws27"),
      "eink-ws27 declares the HAT key options, local only",
      "buttons %s" % ("off" if read("plugins.eink-ws27.buttons") is False else "on"))
# 29.9: the one-cell and four-cell UPS plugins: local only, the battery service,
# untested, and their defaults (0x43, 0x43, 0x2D, power_on_when_charged off).
new_ups = ("ups-wsc", "ups-wsd", "ups-wse")
d_c, d_d, d_e = defaults("ups-wsc"), defaults("ups-wsd"), defaults("ups-wse")
check("29.9", all(caps(n) == {"ui"} and provides(n) == ("battery",)
                 and "Not yet tested on hardware" in str(decl(n).get("description") or "")
                 for n in new_ups)
      and d_c.get("address") == 0x43 and d_d.get("address") == 0x43
      and d_e.get("address") == 0x2D and d_e.get("power_on_when_charged") is False,
      "ups-wsc, ups-wsd and ups-wse: local only, battery, untested, their defaults",
      ", ".join("%s %s" % (n, state(n)) for n in new_ups))
# 25.2: census's declarations: network/ui/web, the census service, the two
# services it reads, and its defaults (every 90 minutes, tether only).
dc = defaults("census")
check("25.2", caps("census") == {"network", "ui", "web"} and provides("census") == ("census",)
      and tuple(decl("census").get("optional_requires") or ()) == ("agent", "remote-training")
      and dc.get("report") is True and dc.get("read") is True and dc.get("poll_minutes") == 90
      and dc.get("tether_only") is True and dc.get("daily_mb") == 5 and "poll_hours" not in dc,
      "census declares network, ui and web, provides census, and keeps its defaults",
      "census %s" % state("census"))
# 25.3: enabling census through the settings channel needs the authorized-use
# acknowledgment (the throwaway config, and an ack file in the work folder).
before = read("plugins.census")
r1 = apply({"plugin": "census", "enabled": True})
refused = not r1.get("ok") and r1.get("need_ack") is True and read("plugins.census") == before
r2 = apply({"plugin": "census", "enabled": True, "ack": True})
check("25.3", refused and r2.get("ok") is True and read("plugins.census.enabled") is True,
      "enabling census through the settings channel needs the acknowledgment",
      "without: %s" % str(r1.get("error", ""))[:70])
# 18.1 and 18.2: the settings root acts on are root-only, and the settings channel
# refuses them with the (throwaway) config unchanged.
ROOT_ONLY = {"updates": ("beta", "timeout", "backup_timeout_seconds"),
             "remote-train": ("agent_dir",)}
TRIAL = {"beta": True, "timeout": 25, "backup_timeout_seconds": 600,
         "agent_dir": "/var/lib/kestrel/agent-kbc"}
undeclared = ["%s.%s" % (p, o) for p, names in ROOT_ONLY.items() for o in names
              if (opts(p).get(o) or {}).get("root_only") is not True]
check("18.1", not undeclared, "updates' beta/timeout/backup_timeout_seconds and "
      "remote-train's agent_dir are root-only", "not root-only: %s" % (undeclared or "none"))
tried = []
for plugin, names in ROOT_ONLY.items():
    for o in names:
        before = read("plugins.%s" % plugin)
        r = apply({"plugin": plugin, "set": {o: TRIAL[o]}})
        tried.append((o, not r.get("ok") and read("plugins.%s" % plugin) == before,
                      str(r.get("error", ""))[:60]))
check("18.2", all(ok for _o, ok, _e in tried),
      "each is refused through the settings channel (nothing written)",
      "; ".join("%s: %s" % (o, "refused" if ok else "ACCEPTED") for o, ok, _e in tried)
      + " (%s)" % tried[0][2])
# 29.22: HTTPS can only be switched from a terminal, never through the settings socket.
before = read("web.tls")
r = apply({"target": "config-set", "table": "web", "key": "tls", "value": "off"})
check("29.22", not r.get("ok") and read("web.tls") == before,
      "web.tls is refused through the settings channel (CLI only)", "error=%s" % r.get("error"))
PY
# 28.15: the Advanced page's default for the mode, from the installed package.
cat_mode=$(as_dev "$VENV_PY" -I -c 'from kestrel_pi import config_catalog as c
e = c.find("capture.output", "mode") or {}
print(e.get("default"), (e.get("choices") or [None])[0])' 2>&1 | tail -1)
if [ "$cat_mode" = "per-ap per-ap" ]; then
  check 28.15 PASS "Advanced settings shows per-ap as the mode's default" ""
else
  check 28.15 FAIL "Advanced settings shows per-ap as the mode's default" "$(printf '%s' "$cat_mode" | cut -c1-120)"
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
import base64, html, json, re, sys, time, tomllib, urllib.error, urllib.request
pw = sys.stdin.readline().rstrip("\n")
with open("/etc/kestrel/config.toml", "rb") as h:
    cfg = tomllib.load(h)
web = cfg.get("web", {}) or {}
mode = str(web.get("auth", "system")).lower()
port = int(web.get("port", 8080))
def chk(cid, result, what, detail=""):
    print("CHECK %-5s %-6s %s :: %s" % (cid, result, what, detail))
# 29.20: every response carries nosniff, the sign-in refusal included (no password needed).
try:
    hdrs = urllib.request.urlopen("http://127.0.0.1:%d/status.json" % port, timeout=15).headers
except urllib.error.HTTPError as exc:
    hdrs = exc.headers
except Exception as exc:
    hdrs = None
    chk("29.20", "INFO", "responses carry X-Content-Type-Options: nosniff", "no answer: %s" % type(exc).__name__)
if hdrs is not None:
    chk("29.20", "PASS" if hdrs.get("X-Content-Type-Options") == "nosniff" else "FAIL",
        "responses carry X-Content-Type-Options: nosniff", "")
# 19.2: a request body over 64 KB is refused (413) before it's read. Sent to the
# sign-in form over loopback with no credentials, so nothing is checked.
BODY_WHAT = "a request body over 64 KB to the loopback dashboard gets 413"
if mode not in ("basic", "system"):
    chk("19.2", "SKIP", BODY_WHAT, "no sign-in form (web auth %s)" % mode)
else:
    import http.client
    try:
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=15)
        conn.request("POST", "/login", body=b"username=kbc&password=" + b"x" * (70 * 1024),
                     headers={"Content-Type": "application/x-www-form-urlencoded"})
        got = conn.getresponse().status
        conn.close()
        chk("19.2", "PASS" if got == 413 else "FAIL", BODY_WHAT, "HTTP %s" % got)
    except Exception as exc:
        chk("19.2", "INFO", BODY_WHAT, "no answer: %s" % type(exc).__name__)
# The checks below need the dashboard's answer; without it each says why.
DASH = {"2.5": "the Census page has the Boards card, and its Board line names this board's family",
        "2.6": "/guide's census section says the board family is sent, never the exact model",
        "4.5": "under a learned policy, the agent page shows the Exploring row",
        "5.5": "the agent page counts from-scratch wins once one has been judged",
        "9.1": "/guide has the Exploring and training from scratch section",
        "10.2": "a by-AP search for a name matches no BSSID by its hex letters",
        "12.2": "the dashboard's recovered count is no more than its access points",
        "13.2": "the by-AP page's access-point count matches the dashboard's",
        "16.1": "the map's filters include Last 24 hours",
        "16.2": "on a zoomed map, Reset view is on the map, not in the filter row",
        "17.1": "the census and wpa-sec rows offer a View button",
        "18.3": "the Plugins page shows the root-only settings read-only",
        "19.3": "/guide renders, with its headings",
        "19.4": "repeated wrong Basic credentials get 429; the right ones still get in",
        "21.1": "the Census page's Status shows the tether, reach, and dated push and read",
        "22.1": "every plugin page the script can open starts with the links back",
        "25.4": "an enabled census is loaded, with no error",
        "25.8": "the Census page shows what this device reports",
        "27.5": "the agent page shows the contest tally",
        "27.9": "/status.json reports the release version, with the package version beside it",
        "27.10": "the dashboard header shows the release version",
        "28.6": "the Environment card shows a count while capturing",
        "28.10": "an enabled eink-ws27 loads with its Motion card and no deprecation note",
        "28.13": "an enabled ups-ws loads, its card shows a status, and no deprecation note",
        "28.17": "Captured networks opens on the by-access-point view",
        "29.10": "each enabled ups-wsc/ups-wsd/ups-wse is loaded and its card shows a status",
        "29.12": "one e-ink card shows the panel, the other 'used by' it",
        "29.25": "the running daemon offers the row buttons",
        "29.26": "the map page: background, recovered colours, drag and zoom",
        "29.27": "Captured networks has its controls at the top"}
def skip_all(why, basic=True):
    if basic:
        chk("29.19", "SKIP", "loopback /status.json with Basic credentials", why)
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
    why = type(exc).__name__ + (" %s" % exc.code if isinstance(exc, urllib.error.HTTPError) else "")
    print("(not available: %s)" % why)
    chk("29.19", "FAIL" if mode in ("basic", "system") else "INFO",
        "loopback /status.json with Basic credentials", why)
    skip_all("no /status.json", basic=False)
    sys.exit(0)
if mode in ("basic", "system"):
    chk("29.19", "PASS", "loopback /status.json with Basic credentials", "plain HTTP on 127.0.0.1")
print("version:", s.get("version"))
cap = s.get("capture", {}) or {}
print("capture:", {k: cap.get(k) for k in ("state", "enabled", "hashline_files", "access_points",
                                           "recovered_aps", "located_aps")})
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
# 28.6: while capturing, the Environment card has a count, not "Unknown".
env = s.get("environment")
if not isinstance(env, dict):
    chk("28.6", "SKIP", DASH["28.6"], "capture is off")
elif env.get("available"):
    chk("28.6", "PASS", DASH["28.6"], "available, data age %s s" % env.get("data_age_s"))
else:
    chk("28.6", "FAIL", DASH["28.6"], "not available while capturing")
# 28.10: eink-ws27 loads with its Motion card, and nothing calls it deprecated.
if not enabled("eink-ws27"):
    chk("28.10", "SKIP", DASH["28.10"], "eink-ws27 not enabled")
else:
    p = plugins["eink-ws27"]
    ok = (p.get("state") == "loaded" and "mobility" in widget("eink-ws27")
          and "deprecated" not in str(p.get("note") or "").lower())
    chk("28.10", "PASS" if ok else "FAIL", DASH["28.10"],
        "state %s, note starts %r" % (p.get("state"), str(p.get("note") or "")[:40]))
# 28.13: ups-ws loads, its card shows a status, and nothing calls it deprecated.
if not enabled("ups-ws"):
    chk("28.13", "SKIP", DASH["28.13"], "ups-ws not enabled")
else:
    p = plugins["ups-ws"]
    status = str(widget("ups-ws").get("status") or "")
    ok = (p.get("state") == "loaded" and status not in ("", "checking")
          and "deprecated" not in str(p.get("note") or "").lower())
    chk("28.13", "PASS" if ok else "FAIL", DASH["28.13"], "status %r" % status[:50])
# 29.10: each enabled one-cell/four-cell UPS plugin loads and shows a status (with
# no board: "no UPS HAT (C) detected" and the like).
new_on = [n for n in ("ups-wsc", "ups-wsd", "ups-wse") if enabled(n)]
if not new_on:
    chk("29.10", "SKIP", DASH["29.10"], "none of ups-wsc, ups-wsd, ups-wse enabled")
else:
    bad = [n for n in new_on if plugins[n].get("state") != "loaded"
           or str(widget(n).get("status") or "") in ("", "checking")]
    chk("29.10", "FAIL" if bad else "PASS", DASH["29.10"],
        ("not loaded or no status: %s" % ", ".join(bad)) if bad else
        "; ".join("%s: %s" % (n, str(widget(n).get("status"))[:50]) for n in new_on))
# 29.12: one enabled e-ink plugin's card shows the panel's state, the other reads
# "used by <that plugin>".
eon = [n for n in ("eink-ws27", "eink-ws213") if enabled(n)]
if len(eon) < 2:
    chk("29.12", "SKIP", DASH["29.12"], "needs both eink-ws27 and eink-ws213 enabled")
else:
    shown = {n: str(widget(n).get("panel") or "") for n in eon}
    drivers = [n for n, v in shown.items() if not v.startswith("used by ")]
    ok = len(drivers) == 1 and all(v == "used by " + drivers[0]
                                   for n, v in shown.items() if n != drivers[0])
    chk("29.12", "PASS" if ok else "FAIL", DASH["29.12"],
        "; ".join("%s: %s" % (n, v[:40]) for n, v in shown.items()))
# 29.25: an enabled, loaded downloads/map/ntfy/telegram offers its main-page button.
offered = {p.get("name"): [a.get("label") for a in (p.get("actions") or []) if isinstance(a, dict)]
           for p in s.get("plugins", []) if p.get("state") == "loaded"}
want = {"downloads": "Download", "map": "Map", "ntfy": "Status", "telegram": "Status"}
on = sorted(n for n in want if n in offered)
if not on:
    chk("29.25", "SKIP", DASH["29.25"], "none of downloads, map, ntfy, telegram loaded")
else:
    bad = [n for n in on if want[n] not in offered[n]]
    chk("29.25", "FAIL" if bad else "PASS", DASH["29.25"],
        ("missing: %s" % ", ".join(bad)) if bad else "checked: %s" % ", ".join(on))
# 28.17, 29.26 and 29.27: pages read over loopback with the same credentials. Only
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
    chk("29.26", "SKIP", DASH["29.26"], "map not loaded")
else:
    code, hdrs, body = page("/plugins/map/")
    policy = (hdrs or {}).get("Referrer-Policy") if hdrs else None
    if code != 200:
        chk("29.26", "FAIL", DASH["29.26"], "HTTP %s" % code)
    elif "No captured network matching" in body:
        chk("29.26", "SKIP", DASH["29.26"], "no located networks")
    else:
        background = ("OpenStreetMap</a> contributors" not in body
                      or ('referrerpolicy="origin"' in body and policy == "no-referrer"))
        legend = "Green with a" in body and "not recovered" in body
        gestures = (body.count('class="kmap"') == 2 and body.count('class="kmap-layer"') == 2
                    and body.count("data-scale=") == 2 and "bboxFor" in body
                    and "touch-action:none" in body)
        ok = background and legend and gestures
        chk("29.26", "PASS" if ok else "FAIL", DASH["29.26"],
            "background %s, legend %s, gestures %s" % (background, legend, gestures))
# 16.1 and 16.2: the map's filter row, and Reset view on a zoomed map (a box over the
# whole world, so every location is in it): an overlay on each of the two
# renderings, not in the filter row. Markers only, as for 29.26.
def filter_row(body):
    m = re.search(r'<p class="smeta">Show: .*?</p>', body, re.S)
    return m.group(0) if m else ""
if "map" not in offered:
    chk("16.1", "SKIP", DASH["16.1"], "map not loaded")
    chk("16.2", "SKIP", DASH["16.2"], "map not loaded")
else:
    code, _h, body = page("/plugins/map/")
    row = filter_row(body)
    if code != 200 or not row:
        chk("16.1", "FAIL" if code != 200 else "SKIP", DASH["16.1"],
            "HTTP %s" % code if code != 200 else "no filter row (locations hidden?)")
    else:
        ok = "Last 24 hours" in row and "f=day" in row and "Last 7 days" in row
        chk("16.1", "PASS" if ok else "FAIL", DASH["16.1"], "" if ok else "option missing")
    code, _h, body = page("/plugins/map/?bbox=-80,-170,80,170")
    row = filter_row(body)
    corner = body.count('<a href="./" style="position:absolute;left:8px;top:8px')
    if code != 200:
        chk("16.2", "FAIL", DASH["16.2"], "HTTP %s" % code)
    elif "No captured network matching" in body or not row:
        chk("16.2", "SKIP", DASH["16.2"], "no located networks")
    else:
        ok = corner == 2 and "Reset view" not in row
        chk("16.2", "PASS" if ok else "FAIL", DASH["16.2"],
            "on the map %d of 2, in the filter row %s" % (corner, "Reset view" in row))
# 17.1: the census and wpa-sec row buttons read View (they were Census and Show).
views = [n for n in ("census", "wpa-sec") if n in offered]
if not views:
    chk("17.1", "SKIP", DASH["17.1"], "neither census nor wpa-sec loaded")
else:
    bad = [n for n in views if "View" not in offered[n]
           or {"Census", "Show"} & set(offered[n])]
    chk("17.1", "FAIL" if bad else "PASS", DASH["17.1"],
        ("not View: %s" % ", ".join(bad)) if bad else "checked: %s" % ", ".join(views))
# 10.2: a by-AP search for a name no network has, whose only hex letter is "e":
# the old search reduced it to "e" and matched every BSSID with one. Counts
# only: the page's "no match" line is checked, no row is read.
code, _h, body = page("/networks?view=ap&q=zzqzz-e")
if code != 200:
    chk("10.2", "FAIL", DASH["10.2"], "HTTP %s" % code)
elif "nothing captured yet" in body:
    chk("10.2", "SKIP", DASH["10.2"], "no access points captured yet")
else:
    m = re.search(r"(\d+) of (\d+) access points match", body)
    chk("10.2", "PASS" if "no match in" in body else "FAIL", DASH["10.2"],
        "no match" if "no match in" in body
        else "%s of %s matched" % (m.group(1), m.group(2)) if m else "unexpected page")
# 12.2: "recovered N of M": the count is there (the store read worked) and N <= M.
aps, rec = cap.get("access_points"), cap.get("recovered_aps")
if not isinstance(aps, int):
    chk("12.2", "FAIL", DASH["12.2"], "no access-point count")
elif rec is None:
    chk("12.2", "FAIL", DASH["12.2"], "no recovered count (the recovered store couldn't be read)")
else:
    chk("12.2", "PASS" if rec <= aps else "FAIL", DASH["12.2"], "%s of %s" % (rec, aps))
# 13.2: the by-AP page's "N access point(s) captured" leaves rogue addresses out,
# like the dashboard's total. Counts only: rows marked "rogue address" are
# counted on the first page, nothing from a row is printed.
code, _h, body = page("/networks?view=ap")
m = re.search(r"(\d+) access point\(s\) captured", body)
rogue_rows = body.count('data-label="rogue address"')
if code != 200:
    chk("13.2", "FAIL", DASH["13.2"], "HTTP %s" % code)
elif not m or not isinstance(aps, int):
    chk("13.2", "SKIP", DASH["13.2"], "no access points captured yet")
else:
    shown = int(m.group(1))
    chk("13.2", "PASS" if shown == aps else "FAIL", DASH["13.2"],
        "page %d, dashboard %d; rogue-address rows on the first page: %d"
        % (shown, aps, rogue_rows))
code, hdrs, body = page("/networks")
if code != 200:
    chk("28.17", "FAIL", DASH["28.17"], "HTTP %s" % code)
    chk("29.27", "FAIL", DASH["29.27"], "HTTP %s" % code)
else:
    out_mode = str(((cfg.get("capture") or {}).get("output") or {}).get("mode", "per-ap"))
    if out_mode != "per-ap":
        chk("28.17", "SKIP", DASH["28.17"], "mode is %s" % out_mode)
    else:
        chk("28.17", "PASS" if "Captured access points" in body else "FAIL", DASH["28.17"], "")
    top = body.split("<h1>", 1)[0]
    # With the web login off the import control is hidden on purpose.
    ok = ('class="nethead"' in top and 'class="countbar"' in body
          and ("Import results" in top or mode == "none"))
    chk("29.27", "PASS" if ok else "FAIL", DASH["29.27"],
        ("Import results above the heading" if mode != "none" else "web login off: no import")
        if ok else "markers missing")
# 27.9 and 27.10: the device's release version (/etc/kestrel/version) in the status
# and on the dashboard header.
try:
    release = open("/etc/kestrel/version", encoding="utf-8").read().strip()
except OSError:
    release = ""
ok = bool(release) and s.get("version") == release and bool(s.get("package_version"))
chk("27.9", "PASS" if ok else "FAIL", DASH["27.9"],
    "version %r, package %r" % (s.get("version"), s.get("package_version")))
code, _hdrs, body = page("/")
want_hdr = ("v" + release) if release[:1].isdigit() else "dev build (%s)" % release
chk("27.10", "PASS" if code == 200 and ("plugin host &middot; " + want_hdr) in body else "FAIL",
    DASH["27.10"], "HTTP %s, expecting %r" % (code, want_hdr))
# 27.5: the agent card (on /agent) shows the contest tally, or the earlier
# adoptions before the first contest.
code, _hdrs, body = page("/agent")
if code != 200:
    chk("27.5", "FAIL", DASH["27.5"], "HTTP %s" % code)
elif "Model contests" in body or "Earlier adoptions" in body:
    chk("27.5", "PASS", DASH["27.5"], "Model contests" if "Model contests" in body
        else "Earlier adoptions (no contest yet)")
elif "Promotions" in body:
    chk("27.5", "FAIL", DASH["27.5"], "still the old Promotions line")
else:
    chk("27.5", "SKIP", DASH["27.5"], "no tally on the agent card yet")
# 4.5 and 5.5: the agent page's Exploring row and the from-scratch tally on its
# Model contests row. Markers and counts only.
ast = st if isinstance(st, dict) else {}
if code != 200:
    chk("4.5", "FAIL", DASH["4.5"], "HTTP %s" % code)
    chk("5.5", "FAIL", DASH["5.5"], "HTTP %s" % code)
else:
    if ast.get("policy") != "learned":
        chk("4.5", "SKIP", DASH["4.5"], "policy is %s" % ast.get("policy"))
    else:
        dot = r" (?:\u00b7|&middot;|&#183;) "
        row = re.search(r"Exploring.{0,400}?(\d+(?:\.\d+)?% of windows" + dot + r"\d+ of \d+ "
                        r"actions tried" + dot + r"\d+ in the last hour|\boff\b)", body, re.S)
        has_metrics = isinstance((ast.get("metrics") or {}).get("exploring"), dict)
        table = "exploring h/hr" in body
        ok = row is not None and (table or not has_metrics)
        chk("4.5", "PASS" if ok else "FAIL", DASH["4.5"],
            "row %s; exploring line %s" % ("found" if row else "missing",
                                           "shown" if table else ("missing" if has_metrics
                                                                  else "not yet (no exploring window)")))
    local = ((ast.get("promotion_tally") or {}).get("local") or {})
    if "fresh_won" not in local and "fresh_lost" not in local:
        chk("5.5", "SKIP", DASH["5.5"], "no from-scratch candidate judged yet")
    else:
        won, lost = int(local.get("fresh_won") or 0), int(local.get("fresh_lost") or 0)
        want = "from scratch %d of %d won" % (won, won + lost)
        chk("5.5", "PASS" if want in body else "FAIL", DASH["5.5"],
            "%s on the page" % want if want in body else "%r missing" % want)
# 25.4: an enabled census loads with no error; its note is empty, or a pause
# reason in words.
if not enabled("census"):
    chk("25.4", "SKIP", DASH["25.4"], "census not enabled")
else:
    p = plugins["census"]
    ok = p.get("state") == "loaded" and not p.get("last_error")
    chk("25.4", "PASS" if ok else "FAIL", DASH["25.4"],
        "state %s, note %r" % (p.get("state"), str(p.get("note") or "")[:70]))
# 25.8: the Census page says what this device reports: its release, and (with
# tether_only) that it goes over the tether. Markers only.
if "census" not in offered:
    chk("25.8", "SKIP", DASH["25.8"], "census not loaded")
else:
    code, _hdrs, body = page("/plugins/census/index")
    tether = ((cfg.get("plugins") or {}).get("census") or {}).get("tether_only", True)
    want = ["What this device reports today", "Release: " + (release or "unknown")]
    if tether:
        want.append("Sent over: the phone's Bluetooth tether only")
    missing = [m for m in want if m not in body]
    chk("25.8", "PASS" if code == 200 and not missing else "FAIL", DASH["25.8"],
        "HTTP %s%s" % (code, (", missing %s" % missing) if missing else ""))
# 2.5: the Census page's Boards card, and the Board line of what this device
# reports: this board's family, or "not sent today" on the update's UTC day.
# The card, like Releases, appears once the census has today's totals.
if "census" not in offered:
    chk("2.5", "SKIP", DASH["2.5"], "census not loaded")
else:
    code, _hdrs, body = page("/plugins/census/index")
    text = html.unescape(body)
    want_board = sys.argv[1] if len(sys.argv) > 1 else ""
    upd_at = int(sys.argv[2]) if len(sys.argv) > 2 and sys.argv[2].isdigit() else 0
    updated_today = upd_at >= int(time.time()) // 86400 * 86400
    totals = "<h2>Releases</h2>" in text
    card = "<h2>Boards</h2>" in text
    line = bool(want_board) and ("Board: %s</p>" % want_board) in text
    not_sent = "Board: not sent today (from the next UTC day)</p>" in text
    seen = "Boards card %s; Board: %s" % (
        "shown" if card else "missing" if totals else "not yet (no totals today)",
        want_board if line else "not sent today" if not_sent else "missing")
    if code != 200 or (totals and not card) or not (line or not_sent):
        chk("2.5", "FAIL", DASH["2.5"], "HTTP %s, %s" % (code, seen))
    elif not_sent and not updated_today:
        chk("2.5", "FAIL", DASH["2.5"], seen + ", though the update wasn't this UTC day")
    elif line and card:
        chk("2.5", "PASS", DASH["2.5"], seen)
    else:
        chk("2.5", "INFO", DASH["2.5"], seen + "; rerun after %s" % (
            "00:00 UTC" if not_sent else "the census's next read"))
# 18.3: each plugin's settings page shows its root-only options read-only, with
# the terminal command, and no form field for them.
missing = []
for name, names in (("updates", ("beta", "timeout", "backup_timeout_seconds")),
                    ("remote-train", ("agent_dir",))):
    code, _hdrs, body = page("/manage/" + name)
    if code != 200:
        missing.append("%s: HTTP %s" % (name, code))
        continue
    for o in names:
        if ('<div class="ro-option"><b>%s</b>' % o) not in body or ('name="opt-%s"' % o) in body:
            missing.append("%s.%s" % (name, o))
chk("18.3", "FAIL" if missing else "PASS", DASH["18.3"],
    "; ".join(missing) or "read-only, with the terminal command")
# 19.3: the on-device guide renders (the whole guide on one page).
code, _hdrs, body = page("/guide/all")
heads = body.count("<h2")
chk("19.3", "PASS" if code == 200 and heads >= 5 else "FAIL", DASH["19.3"],
    "HTTP %s, %d section headings" % (code, heads))
# 9.1: the guide's new section, on the same page.
chk("9.1", "PASS" if code == 200 and "Exploring and training from scratch" in body else "FAIL",
    DASH["9.1"], "HTTP %s" % code)
# 2.6: the guide's census section, on the same page: the board family is sent,
# never the exact model.
text = html.unescape(body)
chk("2.6", "PASS" if code == 200 and "random apart from the board family it carries" in text
    and "never the exact model, revision or serial number" in text else "FAIL",
    DASH["2.6"], "HTTP %s" % code)
# 21.1: the Census page's Status card. Only which lines are there, the state
# words and how many dated times; nothing else from the page is printed.
if "census" not in offered:
    chk("21.1", "SKIP", DASH["21.1"], "census not loaded")
else:
    code, _hdrs, body = page("/plugins/census/index")
    text = html.unescape(body)
    tether = re.search(r"Phone's Bluetooth tether: (up|down)\.", text)
    reach = re.search(r"Can read and write the census now: (yes|no|not yet)", text)
    pushed = ("Last pushed (this device's report):" in text or "Last push, " in text
              or "Reporting is off" in text)
    reading = ((cfg.get("plugins") or {}).get("census") or {}).get("read", True)
    read_ = "Last retrieved (the whole census):" in text or reading is False
    dated = len(re.findall(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2} UTC", text))
    ok = (code == 200 and (tether or "Connection: any (tether_only is off)." in text)
          and reach and pushed and read_)
    chk("21.1", "PASS" if ok else "FAIL", DASH["21.1"],
        "tether %s, reach %s, push line %s, read line %s, %d dated" % (
            tether.group(1) if tether else "-", reach.group(1) if reach else "-",
            pushed, read_, dated))
# 22.1: every plugin page the row buttons open starts with the links back. Not
# downloads (it serves capture files) or wpa-sec (it shows passphrases).
BACK = re.compile(r'<p><a href="/plugins[^"]*">&larr; Plugins</a> &middot; '
                  r'<a href="/[^"]*">Dashboard</a></p>')
targets = []
for p in s.get("plugins", []):
    if p.get("state") != "loaded" or p.get("name") in ("downloads", "wpa-sec"):
        continue
    for a in p.get("actions") or []:
        if (isinstance(a, dict) and a.get("method", "get") == "get"
                and isinstance(a.get("path"), str)):
            targets.append((p.get("name"), a["path"].lstrip("/")))
seen_pages, lacking = [], []
for name, path in targets:
    code, hdrs, body = page("/plugins/%s/%s" % (name, path))
    ctype = hdrs.get("Content-Type", "") if hdrs else ""
    if code != 200 or not ctype.startswith("text/html"):
        continue                       # an image or a download, not a page
    seen_pages.append(name)
    if not BACK.search(body):
        lacking.append(name)
if not seen_pages:
    chk("22.1", "SKIP", DASH["22.1"], "no plugin page to open")
else:
    chk("22.1", "FAIL" if lacking else "PASS", DASH["22.1"],
        ("no links: %s" % ", ".join(lacking)) if lacking else "checked: %s" % ", ".join(seen_pages))
for name in ("wpa-sec", "wigle", "backup", "remote-train", "census", "updates", "ntfy", "telegram",
             "eink-ws27", "eink-ws213", "ups-ws", "ups-wsc", "ups-wsd", "ups-wse"):
    if name in w:
        vals = ["%s=%s" % (k, v) for k, v in widget(name).items()]
        print("widget %-13s %s" % (name, ", ".join(vals)))
# 19.4 (last, since it leaves a few failed sign-ins from 127.0.0.1): wrong Basic
# credentials until the dashboard backs off, then the right ones, which it
# verified at the start of this run, still get in. The journal is only counted.
if mode not in ("basic", "system"):
    chk("19.4", "SKIP", DASH["19.4"], "web auth is %s" % mode)
else:
    import os, subprocess, time
    started = int(time.time()) - 1
    wrong = urllib.request.Request("http://127.0.0.1:%d/status.json" % port)
    wrong.add_header("Authorization", "Basic " + base64.b64encode(
        ("%s:kbc-wrong-%s" % (user, os.urandom(6).hex())).encode()).decode())
    codes, retry = [], None
    for _ in range(8):
        try:
            urllib.request.urlopen(wrong, timeout=30)
            codes.append(200)
        except urllib.error.HTTPError as exc:
            codes.append(exc.code)
            if exc.code == 429:
                retry = exc.headers.get("Retry-After")
                break
        except Exception as exc:
            codes.append(type(exc).__name__)
            break
    right, _hdrs, _body = page("/status.json")
    journal = subprocess.run(["journalctl", "-q", "--no-pager", "-o", "cat", "-u", "kestrel-daemon",
                              "--since", "@%d" % started], capture_output=True, text=True).stdout
    logged = journal.count("web: failed Basic sign-in from")
    ok = (codes[-1:] == [429] and all(c == 401 for c in codes[:-1])
          and str(retry or "").isdigit() and int(retry) > 0 and right == 200 and logged >= 1)
    chk("19.4", "PASS" if ok else "FAIL", DASH["19.4"],
        "answers %s, Retry-After %s, right credentials HTTP %s, failures logged %d"
        % (codes, retry, right, logged))
PY
printf '%s\n' "$web_pw" | /usr/bin/python3 -I "$work/dashboard.py" "$want_name" "$upd_at" 2>&1
} >>"$raw" 2>&1
web_pw=""

cat >>"$raw" <<'EOF'

===== by hand (the script can't see these) =====
2.7  on the phone, the Census page's Boards card reads normally, and once this
     device has run an on-device contest that week it adds "On-device
     training on the ... family won X of Y (Z%) this week"
2.8  from a computer, python3 tools/census-report/census_report.py shows a
     Board table that counts this device's family, and an "On-device contests
     by board" table
3.2  after a from-scratch training run, pull-agent-diagnostics.sh from a
     computer lists its contest on a "local (from scratch)" row, and the local
     contests add up to the local training runs
4.6  under a learned policy, on the phone: the Agent page's Exploring row reads
     normally, its tried count climbs over a day or two, and during an
     exploring window the agent's note says "trying ..."
4.7  optional: add explore_rate = 0 to [agent], restart the agent; the Exploring
     row reads "off" and no new "explore: trying" lines appear; remove the
     line and restart again
5.6  sudo kestrel agent train-now --fresh: the journal says "manual training
     from scratch (asked for, N steps)"; a plain train-now still fine-tunes
5.7  after a day or two: the Agent page's Last contest says how often the new
     model chose like the active one, and a from-scratch candidate's figure is
     well under 100%
7.4  optional: set transmit_modes = ["passive"] in [agent.action], restart the
     agent; the Exploring row reads "of 16" and every new "explore: trying"
     line says passive; put the line back and restart again
8.2  with a GPS fix, capture until a handshake gets a location; unplug the
     receiver (or lose the fix indoors): after about half a minute the GPS card
     shows no fix and new handshakes get no location; plug it back in and
     tagging resumes with the next fix
9.2  the dashboard's What's new for this beta reads normally
10.3  on the phone, in the by-access-point view: search for one of your network
     names; only rows with that name are listed; paste part of a BSSID (like
     AA:BB:CC) and it still finds the radio
12.3  with a recovered result for a phone-answered capture (from wpa-sec or an
     import): on Captured networks it shows only under its own network name,
     labelled "· client ...", and the other names under that address show no
     recovered badge
12.4  results imported before this beta still show for ordinary access points;
     import a wpa-sec file again and kestrel recovered list counts it
13.3  in the by-access-point view, a rogue address's row says "Not an access
     point" and shows no location; it isn't on the map; the e-ink AP counter
     matches the dashboard's access points
15.3  with the census on and the tether up: turn the tether off and on again;
     at its next cycle (within the hour) the journal shows "census: the tether
     reconnected; opening a fresh socket" (or "the link went down mid-cycle"),
     the census reports again, and a rerun of this check passes 15.2
16.3  on the phone, the map's filters include Last 24 hours and fit the screen;
     zoom in, and Reset view sits in the map's top-left corner, keeps the
     filter, and brings back the whole map
18.6  sudo kestrel agent train-now still starts a training run, and
     sudo kestrel remote-train now still asks for a pull (see their logs)
18.7  sudo kestrel plugins set updates timeout=30 works from the terminal, and
     the dashboard can't change it
18.8  beta updates are switched from the terminal: sudo kestrel plugins set
     updates beta=false stops offering betas, and beta=true offers this one
19.5  on the phone, sign in with a wrong password several times: "Too many
     attempts. Try again in N seconds."; the right password works after waiting
19.6  import a potfile from Captured networks: it imports; a file over
     [recovered] max_import_mb gets "That file is too large to import."
19.7  the dashboard's What's new for this beta reads normally
20.3  only if you update from a private repository with a token: the next
     release still clones and applies
21.4  restart the daemon: the Census page still shows the last push and read
22.2  on the phone, open each plugin's page from the Plugins page and come back
     with the links at the top
23.1  optional: build with sudo make image MY_PI_PASSWORD_HASH='<your hash>';
     the log says "login: custom hash" and never shows the hash
24.3  with remote-train set up, make the training host unreachable (shut it
     down or stop its SSH server): the remote-train card reads "can't reach or
     log in to the host (last reached ...)"; bring it back and the card recovers
24.4  restart the daemon (sudo systemctl restart kestrel-daemon): the
     remote-train card still shows the last model pulled
25.9 with the census on: turn the phone's tether off; the Census page reads
     "Paused: waiting for the phone's Bluetooth tether."; turn it on and it
     says "Running." again within a few minutes
25.10 from a computer, python3 tools/census-report/census_report.py lists this
     device's release (with the test seeder running, about 40 more devices),
     and the card's numbers match after the device's next read
26.5  stop capture from the dashboard, then rerun this check with capture off
     (for 26.3); start it again and the dashboard shows it running
27.6  after a day of capture: the /agent card's Last contest shows a result, and
     pull-agent-diagnostics.sh --score (from a computer) shows contests from
     both sources with their reasons
27.8  copy pull-health.sh off the device and run it from a computer: one report
     with a verdict and no addresses, MAC addresses or names in it
28.4  if not done yet: capture for an hour somewhere busy; pull-health.sh shows
     the database lag still small and AngryOxide's memory roughly flat, and the
     Environment card keeps a count
30.1 the closing step: cut 1.0.14 stable (or the next beta); this device is
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
