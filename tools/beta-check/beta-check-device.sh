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
# readout, redaction) stay; the per-beta checks are rewritten for each beta.

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

# ---------------------------------------------------------------- 1. update
sec "1. in-place update"
ver=$(cat /etc/kestrel/version 2>/dev/null || echo "")
if [ -z "$EXPECT_VERSION" ]; then
  check 1.3 INFO "version" "/etc/kestrel/version = $ver (no expected version given)"
elif [ "$ver" = "$EXPECT_VERSION" ]; then
  check 1.3 PASS "version" "/etc/kestrel/version = $ver"
else
  check 1.3 FAIL "version" "/etc/kestrel/version = ${ver:-missing}, expected $EXPECT_VERSION"
fi
check 1.1 MANUAL "beta off: not offered" "can't be observed after upgrading; see the by-hand list"
echo "updates plugin: enabled=$(cfg plugins.updates.enabled false) beta=$(cfg plugins.updates.beta false)"
echo "update-run status (root-owned):"
/usr/bin/python3 -I - <<'PY' 2>&1 | sed 's/^/  /'
import json, os, time
p = "/var/lib/kestrel/update-run/status.json"
try:
    st = os.stat(p)
    data = json.load(open(p))
    print("mtime:", time.strftime("%Y-%m-%d %H:%M:%SZ", time.gmtime(st.st_mtime)))
    for k, v in sorted(data.items()):
        if isinstance(v, (str, int, float, bool)) or v is None:
            print("%s: %s" % (k, v))
except FileNotFoundError:
    print("(none: this device has never applied an in-place update)")
except Exception as exc:
    print("unreadable:", type(exc).__name__)
PY
echo "updates plugin state (device-user-owned, read as the device user):"
as_dev /usr/bin/python3 -I - <<'PY' 2>&1 | sed 's/^/  /'
import json, time
try:
    data = json.load(open("/var/lib/kestrel/update/state.json"))
except Exception as exc:
    print("(no state: %s)" % type(exc).__name__)
else:
    for k in ("current", "latest", "update_available", "kind", "error",
              "last_error", "checked_at", "beta"):
        if k in data:
            v = data[k]
            if k == "checked_at" and isinstance(v, (int, float)):
                v = "%s (%d min ago)" % (time.strftime("%Y-%m-%d %H:%M:%SZ", time.gmtime(v)),
                                         (time.time() - v) // 60)
            print("%s: %s" % (k, v))
PY
check 1.2 INFO "beta on: offered" "see the update-run status above (an applied update to this version means it was offered)"

failed=$(systemctl --failed --no-legend --plain 2>/dev/null | awk '{print $1}' | tr '\n' ' ')
if [ -z "$failed" ]; then
  check 1.4a PASS "no failed units" ""
else
  check 1.4a FAIL "failed units" "$failed"
fi
tb=$(journalctl -b -q --no-pager -u kestrel-daemon -u kestrel-agent 2>/dev/null | grep -c 'Traceback' || true)
if [ "${tb:-0}" -eq 0 ]; then
  check 1.4b PASS "no tracebacks this boot (daemon, agent)" ""
else
  check 1.4b FAIL "tracebacks this boot (daemon, agent)" "$tb; last exception lines below"
  journalctl -b -q --no-pager -u kestrel-daemon -u kestrel-agent 2>/dev/null \
    | grep -E '(Error|Exception)(:|$)' | tail -n 8 | jscrub
fi
echo "config.toml mtime: $(date -u -r "$CONFIG" '+%Y-%m-%d %H:%M:%SZ' 2>/dev/null)"
check 1.5 INFO "settings unchanged by the update" "compare config.toml mtime above with the update-run mtime; config is never written by the engine"

# ---------------------------------------------------------------- 2-3, 7: config tests on a throwaway copy
sec "config tests (throwaway copy of config.toml; commands blocked)"
cfg_sum_before=$(sha256sum "$CONFIG" | awk '{print $1}')
/usr/bin/python3 -I - "$work" <<'PY' 2>&1
import importlib.machinery, importlib.util, io, os, shutil, subprocess, sys, tomllib

def _blocked(*a, **k):
    raise RuntimeError("blocked: a config test tried to run a command")
for name in ("run", "Popen", "call", "check_call", "check_output"):
    setattr(subprocess, name, _blocked)
os.system = _blocked

work = sys.argv[1]
cfg = os.path.join(work, "config.toml")
shutil.copyfile("/etc/kestrel/config.toml", cfg)
os.chmod(cfg, 0o600)
os.environ["PWN_CONFIG"] = cfg
os.environ["PWN_CAPTURE_ACK_FILE"] = os.path.join(work, "capture-ack")

loader = importlib.machinery.SourceFileLoader("plugins_ctl", "/usr/local/lib/kestrel/plugins-ctl")
spec = importlib.util.spec_from_loader("plugins_ctl", loader)
ctl = importlib.util.module_from_spec(spec)
loader.exec_module(ctl)
ctl.CONFIG = cfg
ctl.ACK_FILE = os.environ["PWN_CAPTURE_ACK_FILE"]
ctl.web_control_ready = lambda config: True     # test the checks, not the auth gate
assert os.path.realpath(ctl.CONFIG) != "/etc/kestrel/config.toml"

def read(dotted, default=None):
    with open(cfg, "rb") as h:
        node = tomllib.load(h)
    for part in dotted.split("."):
        if not isinstance(node, dict) or part not in node:
            return default
        node = node[part]
    return node

def check(cid, ok, what, detail=""):
    print("CHECK %-5s %-6s %s :: %s" % (cid, "PASS" if ok else "FAIL", what, detail))

def apply(change):
    try:
        return ctl.apply_change(change)
    except Exception as exc:
        return {"ok": False, "error": "exception %s: %s" % (type(exc).__name__, exc)}

# 2: root_only options refused through the socket path
for opt, val in (("host", "https://git.example.com"), ("repo", "example/kestrel"),
                 ("token", "example-not-a-token")):
    before = read("plugins.updates." + opt)
    r = apply({"plugin": "updates", "set": {opt: val}})
    ok = (not r.get("ok")) and "terminal" in str(r.get("error", "")) \
        and read("plugins.updates." + opt) == before
    check("2.0" + opt[0], ok, "socket refuses updates.%s" % opt,
          "reply ok=%s error=%r" % (r.get("ok"), r.get("error", ""))[:200])

# 2.2: a normal updates option still saves through the socket path
cur = read("plugins.updates.check_interval_seconds", 21600)
want = int(cur) + 1
r = apply({"plugin": "updates", "set": {"check_interval_seconds": want}})
check("2.2", bool(r.get("ok")) and read("plugins.updates.check_interval_seconds") == want,
      "socket saves updates.check_interval_seconds", "reply ok=%s" % r.get("ok"))

# 2.3: the terminal path (plugins-ctl set, what `sudo kestrel plugins ... --set` runs)
try:
    rc = ctl.cmd_set("updates", "host", "https://git.example.com")
except Exception as exc:
    rc = "exception %s" % type(exc).__name__
check("2.3", rc == 0 and read("plugins.updates.host") == "https://git.example.com",
      "terminal sets updates.host", "rc=%s" % rc)

# 3.10: archive_dir constrained to the capture store
before = read("capture.housekeeping.archive_dir")
for val in ("/", "/tmp/kbc-x", "/var/lib/kestrel", "/var/lib/kestrel/capture/../x",
            "/var/lib/kestrel/capture"):
    r = apply({"target": "config-set", "table": "capture.housekeeping",
               "key": "archive_dir", "value": val})
    check("3.10", (not r.get("ok")) and read("capture.housekeeping.archive_dir") == before,
          "archive_dir %r refused" % val, str(r.get("error", ""))[:160])
r = apply({"target": "config-set", "table": "capture.housekeeping",
           "key": "archive_dir", "value": "/var/lib/kestrel/capture/archive-kbc"})
check("3.10", bool(r.get("ok")), "archive_dir inside the store accepted", str(r.get("error", "")))

# 3.11: single_file must be a plain file name
before = read("capture.output.single_file")
for val in ("../x", "a/b", ".hidden", "/etc/passwd"):
    r = apply({"target": "config-set", "table": "capture.output",
               "key": "single_file", "value": val})
    check("3.11", (not r.get("ok")) and read("capture.output.single_file") == before,
          "single_file %r refused" % val, str(r.get("error", ""))[:160])
r = apply({"target": "config-set", "table": "capture.output",
           "key": "single_file", "value": "handshakes"})
check("3.11", bool(r.get("ok")), "plain single_file accepted", str(r.get("error", "")))

# 7.2: --set-stdin path stores a value read from stdin
sys.stdin = io.StringIO("example-not-a-real-key\n")
try:
    rc = ctl.cmd_set_stdin("wpa-sec", "api_key")
except Exception as exc:
    rc = "exception %s" % type(exc).__name__
check("7.2a", rc == 0 and read("plugins.wpa-sec.api_key") == "example-not-a-real-key",
      "set-stdin stores the value from stdin", "rc=%s" % rc)
# 6.1/6.3: effective uploader endpoints (config value, else the declared default)
plugins = ctl.static_plugins()
for cid, name in (("6.1", "wpa-sec"), ("6.3", "wigle")):
    decl = (plugins.get(name) or (None,))[0] or {}
    default = ((decl.get("options") or {}).get("endpoint") or {}).get("default", "")
    ep = read("plugins.%s.endpoint" % name) or default
    check(cid, str(ep).lower().startswith("https://"), "%s endpoint is https" % name,
          "enabled=%s; upload health is in the dashboard section" % read("plugins.%s.enabled" % name, False))
PY
if [ "$(sha256sum "$CONFIG" | awk '{print $1}')" = "$cfg_sum_before" ]; then
  check 0.1 PASS "real config.toml untouched by the tests" "checksum identical before and after"
else
  check 0.1 FAIL "real config.toml untouched by the tests" "checksum changed - tell Claude"
fi

# ---------------------------------------------------------------- 2. terminal-only flags
sec "2. update settings are terminal-only"
info=$(timeout 20 /usr/bin/python3 -I "$CTL" info updates 2>&1)
n=$(printf '%s\n' "$info" | grep -E '^  (repo|host|token) ' | grep -c 'terminal only' || true)
if [ "${n:-0}" -eq 3 ]; then
  check 2.1 PASS "repo/host/token marked terminal only" "web display is in the by-hand list"
else
  check 2.1 FAIL "repo/host/token marked terminal only" "$n of 3"
fi
printf '%s\n' "$info" | grep -E '^  (repo|host|token|beta|check_interval)' | sed 's/^/  /'
check 2.4 INFO "update check works" "see updates plugin state checked_at/error in section 1"

# ---------------------------------------------------------------- 3. capture
sec "3. capture run folder and collector"
cap_state=$(svc_active kestrel-capture.service)
echo "capture enabled=$(cfg capture.enabled false) service=$cap_state"
if [ -d "$RUNDIR" ]; then
  perm=$(stat -c '%U:%G %a' "$RUNDIR")
  if [ "$perm" = "root:$DEV_USER 750" ]; then
    check 3.1 PASS "capture-run ownership" "$perm"
  else
    check 3.1 FAIL "capture-run ownership" "$perm (expected root:<device user> 750)"
  fi
else
  check 3.1 SKIP "capture-run ownership" "folder not created yet (capture never started on this release)"
fi
if [ "$cap_state" = "active" ]; then
  npi=$(pgrep -c -u "$DEV_USER" -f capture-collect || true)
  nroot=$(pgrep -c -u root -f capture-collect || true)
  if [ "${npi:-0}" -ge 1 ] && [ "${nroot:-0}" -eq 0 ]; then
    check 3.2 PASS "collector runs as the device user" "user=$npi root=$nroot"
  else
    check 3.2 FAIL "collector runs as the device user" "user=$npi root=$nroot"
  fi
  nk=$(find "$RUNDIR" -maxdepth 1 -name '*.kismet' 2>/dev/null | wc -l)
  np=$(find "$RUNDIR" -maxdepth 1 -name '*.pcapng' 2>/dev/null | wc -l)
  # The legacy .run is drained but never deleted by design, so files from an
  # older release may stay; what matters is that nothing new lands there.
  upd=/var/lib/kestrel/update-run/status.json
  if [ -f "$upd" ]; then
    nnew=$(find "$LEGACY_RUN" -maxdepth 1 -type f -newer "$upd" 2>/dev/null | wc -l)
  else
    nnew=0
  fi
  if [ "$((nk + np))" -ge 1 ] && [ "$nnew" -eq 0 ]; then
    check 3.3 PASS "raw files go to capture-run, nothing new in .run" "kismet=$nk pcapng=$np new-in-legacy=$nnew"
  else
    check 3.3 FAIL "raw files go to capture-run, nothing new in .run" "kismet=$nk pcapng=$np new-in-legacy=$nnew"
  fi
else
  check 3.2 SKIP "collector runs as the device user" "capture not running"
  check 3.3 SKIP "raw files in capture-run" "capture not running"
fi
nh=$(find "$STORE" -maxdepth 1 -name '*.hc22000' 2>/dev/null | wc -l)
nnp=$(find "$STORE" -maxdepth 1 -name '*.hc22000' ! -user "$DEV_USER" 2>/dev/null | wc -l)
newest=$(find "$STORE" -maxdepth 1 -name '*.hc22000' -printf '%T@\n' 2>/dev/null | sort -n | tail -1)
age=""
[ -n "$newest" ] && age="newest $(( ($(date +%s) - ${newest%.*}) / 60 )) min ago"
if [ "$nh" -ge 1 ] && [ "$nnp" -eq 0 ]; then
  check 3.4 PASS "hashline files owned by the device user" "$nh file(s), $age (that it is YOUR test AP is on you)"
elif [ "$nh" -eq 0 ]; then
  check 3.4 SKIP "hashline files owned by the device user" "no hashline files yet"
else
  check 3.4 FAIL "hashline files owned by the device user" "$nnp of $nh not owned by the device user"
fi

echo "environment readout (capture-status.json, read as the device user):"
envstat=$(as_dev /usr/bin/python3 -I - <<'PY' 2>&1
import json, time
try:
    s = json.load(open("/var/lib/kestrel/capture/capture-status.json"))
except Exception as exc:
    print("unreadable: %s" % type(exc).__name__)
else:
    now = time.time()
    for k in ("scanning", "radios_recent", "window_s"):
        print("%s: %s" % (k, s.get(k)))
    for k in ("updated_at", "data_ts"):
        v = s.get(k)
        print("%s: %s" % (k, "%d s ago" % (now - v) if isinstance(v, (int, float)) else v))
    if isinstance(s.get("radios_recent"), int) and isinstance(s.get("data_ts"), (int, float)) \
            and now - s["data_ts"] < 900:
        print("ENV fresh")
PY
)
printf '%s\n' "$envstat" | grep -v '^ENV ' | sed 's/^/  /'
echo "kismet DB read test as the device user (counts only):"
envres=$(as_dev /usr/bin/python3 -I - <<'PY' 2>&1
import glob, os, sqlite3, time


def read_once(db):
    conn = sqlite3.connect("file:%s?mode=ro" % db, uri=True, timeout=2)
    try:
        ts = conn.execute("SELECT MAX(ts_sec) FROM packets").fetchone()[0]
        n = conn.execute("SELECT COUNT(*) FROM packets").fetchone()[0]
    finally:
        conn.close()
    return n, ts


dbs = sorted(glob.glob("/var/lib/kestrel/capture-run/*.kismet"), key=os.path.getmtime)
if not dbs:
    print("RESULT none no kismet DB in capture-run")
else:
    db = dbs[-1]
    side = [s for s in ("-wal", "-shm", "-journal") if os.path.exists(db + s)]
    print("db mode %o, sidecars %s" % (os.stat(db).st_mode & 0o777, side or "none"))
    # AngryOxide holds the write lock while it commits, so a busy or locked DB
    # is retried with backoff, as the collector does. Only another error, or a
    # lock that outlasts every try, is a failure.
    result = None
    for attempt in range(8):
        try:
            n, ts = read_once(db)
            result = "RESULT ok packets=%s newest_ts=%s (try %d)" % (n, ts, attempt + 1)
            break
        except sqlite3.OperationalError as exc:
            result = "RESULT error %s: %s" % (type(exc).__name__, exc)
            msg = str(exc).lower()
            if "lock" not in msg and "busy" not in msg:
                break
            time.sleep(0.5 * (attempt + 1))
        except sqlite3.Error as exc:
            result = "RESULT error %s: %s" % (type(exc).__name__, exc)
            break
    print(result)
PY
)
printf '%s\n' "$envres" | sed 's/^/  /'
case "$envres" in
  *"RESULT ok"*) check 3.5 PASS "device user can read the kismet DB" "read test succeeded" ;;
  *)
    if printf '%s\n' "$envstat" | grep -q '^ENV fresh'; then
      check 3.5 PASS "device user can read the kismet DB" "the collector's own reading is fresh (read test: $(printf '%s\n' "$envres" | grep RESULT))"
    elif printf '%s\n' "$envres" | grep -q 'RESULT error'; then
      check 3.5 FAIL "device user can read the kismet DB" "$(printf '%s\n' "$envres" | grep RESULT); no fresh collector reading"
    else
      check 3.5 SKIP "device user can read the kismet DB" "no kismet DB (capture not running?)"
    fi ;;
esac
# Counts only: the capture journal can carry network names, so no lines leave.
echo "collector/launcher error counts this boot (kestrel-capture journal):"
capj=$(journalctl -b -q --no-pager -u kestrel-capture 2>/dev/null)
for pat in 'Permission denied' 'sqlite' 'readonly' 'unable to open' 'Traceback' \
           'setpriv' 'capture-collect:' 'rolling capture'; do
  printf '  %-18s %s\n' "$pat" "$(printf '%s\n' "$capj" | grep -c -- "$pat" || true)"
done
capj=""

# The collector drains (reads) the legacy .run but never deletes it, so the
# check is that the device user can read every hashline file left there.
if [ -d "$LEGACY_RUN" ]; then
  lres=$(as_dev /usr/bin/python3 -I - <<'PY' 2>&1
import glob
names = glob.glob("/var/lib/kestrel/capture/.run/*.hc22000")
bad = 0
for name in names:
    try:
        with open(name, "rb") as handle:
            handle.read(1)
    except OSError:
        bad += 1
print("files=%d unreadable=%d" % (len(names), bad))
PY
)
  case "$lres" in
    *"unreadable=0"*) check 3.6 PASS "legacy .run readable by the device user" "$lres (kept by design)" ;;
    *) check 3.6 FAIL "legacy .run readable by the device user" "$lres" ;;
  esac
else
  check 3.6 PASS "legacy .run readable by the device user" "no legacy folder"
fi

hk=$(cfg capture.housekeeping.max_run_mb 512)
wrong=$(cfg capture.max_run_mb "")
rsz=$(du -sm "$RUNDIR" 2>/dev/null | awk '{print $1}')
rolls=$(journalctl -q --no-pager -u kestrel-capture --since "-7 days" 2>/dev/null | grep -c 'rolling capture' || true)
check 3.7 INFO "raw-data rollover" "cap [capture.housekeeping] max_run_mb=$hk; run dir now ${rsz:-0} MB; rolls in last 7 days: $rolls"
[ -n "$wrong" ] && echo "  note: [capture] max_run_mb=$wrong is also set - the launcher ignores that key (see below)"

adir=$(cfg capture.housekeeping.archive_dir "$STORE/archive")
if [ -d "$adir" ]; then
  ns=$(find "$adir" -mindepth 1 -maxdepth 1 -type d 2>/dev/null | wc -l)
  nnp=$(find "$adir" -mindepth 1 -maxdepth 1 ! -user "$DEV_USER" 2>/dev/null | wc -l)
  if [ "$nnp" -eq 0 ]; then
    check 3.8 PASS "archive sets owned by the device user" "$ns set(s) under the configured archive_dir"
  else
    check 3.8 FAIL "archive sets owned by the device user" "$nnp of $ns not owned by the device user"
  fi
else
  check 3.8 SKIP "archive sets owned by the device user" "no archive folder yet (run 'kestrel capture archive' first)"
fi
check 3.9 MANUAL "capture clear" "by hand"

# ---------------------------------------------------------------- 4. agent
sec "4. agent and model loading"
echo "agent unit: enabled=$(systemctl is-enabled kestrel-agent 2>/dev/null) active=$(svc_active kestrel-agent)"
pol=$(cfg agent.policy baseline)
echo "config: policy=$pol algorithm=$(cfg agent.algorithm RecurrentPPO)"
echo "model files:"
find "$AGENT_DIR" -maxdepth 1 -name 'model*' -printf '  %f  %s bytes  %TY-%Tm-%Td %TH:%TM\n' 2>/dev/null | sort
echo "agent status.json (read as the device user, scalar fields):"
agst=$(as_dev /usr/bin/python3 -I - <<'PY' 2>&1
import json
try:
    s = json.load(open("/var/lib/kestrel/agent/status.json"))
except Exception as exc:
    print("unreadable: %s" % type(exc).__name__)
else:
    for k, v in sorted(s.items()):
        if isinstance(v, (str, int, float, bool)) or v is None:
            print("%s: %s" % (k, str(v)[:200]))
PY
)
printf '%s\n' "$agst" | sed 's/^/  /'
agerr=$(journalctl -b -q --no-pager -u kestrel-agent 2>/dev/null | grep -ciE 'error|traceback|could not load|fall(ing)? ?back' || true)
if [ "$pol" = "learned" ]; then
  check 4.1 INFO "learned model loads" "policy=learned; check the status fields above say learned, not baseline"
else
  check 4.1 SKIP "learned model loads" "policy=$pol (set learned to test)"
fi
check 4.2 INFO "model trained on this release loads" "compare model file times above with the update time"
if [ "${agerr:-0}" -eq 0 ]; then
  check 4.3 PASS "no agent load errors this boot" ""
else
  check 4.3 FAIL "agent errors this boot" "$agerr line(s); last ones below"
  journalctl -b -q --no-pager -u kestrel-agent 2>/dev/null | grep -iE 'error|traceback|could not load|fall(ing)? ?back' \
    | tail -n 8 | jscrub
fi

# ---------------------------------------------------------------- 5. ssh host keys
sec "5. known host keys for backup and remote-train"
hkv=$(journalctl -q --no-pager -u kestrel-daemon --since "-7 days" 2>/dev/null | grep -c 'Host key verification failed' || true)
for spec in "5.1 backup host port" "5.2 remote-train train_host train_port"; do
  set -- $spec
  cid=$1 plugin=$2 hkey=$3 pkey=$4
  en=$(cfg "plugins.$plugin.enabled" false)
  host=$(cfg "plugins.$plugin.$hkey" "")
  port=$(cfg "plugins.$plugin.$pkey" 22)
  if [ "$en" != "true" ] || [ -z "$host" ]; then
    check "$cid" SKIP "$plugin host key known" "enabled=$en, host set=$([ -n "$host" ] && echo yes || echo no)"
    continue
  fi
  name=$host
  [ "$port" != "22" ] && name="[$host]:$port"
  if as_dev ssh-keygen -F "$name" -f "$DEV_HOME/.ssh/known_hosts" >/dev/null 2>&1; then
    check "$cid" PASS "$plugin host key known" "the device user's known_hosts has the configured host"
  else
    check "$cid" FAIL "$plugin host key known" "not in the device user's known_hosts: re-run 'sudo kestrel $plugin setup'"
  fi
done
check 5.x INFO "'Host key verification failed' in the daemon journal, last 7 days" "$hkv"

# ---------------------------------------------------------------- 6. uploaders
sec "6. uploaders"
if [ -d "$STORE/cracked" ] || [ -e "$STORE/cracked/wpa-sec.potfile" ]; then
  dp=$(stat -c '%U %a' "$STORE/cracked" 2>/dev/null)
  fp=$(stat -c '%U %a' "$STORE/cracked/wpa-sec.potfile" 2>/dev/null || echo "absent")
  if [ "$dp" = "$DEV_USER 700" ] && { [ "$fp" = "$DEV_USER 600" ] || [ "$fp" = "absent" ]; }; then
    check 6.2 PASS "cracked dir 0700, potfile 0600" "dir=$dp potfile=$fp"
  else
    check 6.2 FAIL "cracked dir 0700, potfile 0600" "dir=$dp potfile=$fp"
  fi
else
  check 6.2 SKIP "cracked dir 0700, potfile 0600" "no cracked results downloaded yet"
fi
hs=$(as_dev "$VENV_PY" -I - <<'PY' 2>&1
from kestrel_pi import httpsafe
try:
    httpsafe.https_endpoint("http://example.com", "")
    print("http accepted")
except httpsafe.UnsafeResponse:
    print("http refused")
try:
    httpsafe.https_endpoint("https://example.com/", "")
    print("https accepted")
except httpsafe.UnsafeResponse:
    print("https refused")
PY
)
if printf '%s' "$hs" | grep -q 'http refused' && printf '%s' "$hs" | grep -q 'https accepted'; then
  check 6.4 PASS "installed httpsafe refuses http://" ""
else
  check 6.4 FAIL "installed httpsafe refuses http://" "$(printf '%s' "$hs" | tr '\n' ' ' | cut -c1-200)"
fi

# ---------------------------------------------------------------- 7. secrets off the command line
sec "7. secrets off the command line"
check 7.1 MANUAL "hidden prompt" "by hand"
if grep -q -- '--set-stdin' "$CLI" && grep -q 'set-stdin' "$CTL"; then
  check 7.2b PASS "installed CLI has --set-stdin" ""
else
  check 7.2b FAIL "installed CLI has --set-stdin" ""
fi
if grep -q 'is now in your shell history' "$CLI" || grep -q 'shell history' "$CLI"; then
  check 7.3 PASS "installed CLI has the --set/--psk warnings" "seeing them printed is by hand"
else
  check 7.3 FAIL "installed CLI has the --set/--psk warnings" ""
fi
if grep -q -- '--psk-stdin' "$CLI"; then
  check 7.4 PASS "installed CLI has --psk-stdin" "connecting is by hand"
else
  check 7.4 FAIL "installed CLI has --psk-stdin" ""
fi
# Counts only: the matching lines are never printed.
since_upd=$(date -u -r /var/lib/kestrel/update-run/status.json '+%Y-%m-%d %H:%M:%S' 2>/dev/null || echo "")
count_secrets() {
  journalctl -q --no-pager _COMM=sudo ${1:+--since "$1"} 2>/dev/null \
    | grep -E 'COMMAND=' | grep -cE -- '(api_key|api_token|token|password)=|--psk( |=)' || true
}
all_hits=$(count_secrets "")
new_hits=$([ -n "$since_upd" ] && count_secrets "$since_upd" || echo "n/a")
if [ "$new_hits" = "0" ]; then
  check 7.5 PASS "no secrets in sudo journal since the update" "all-time hits: $all_hits (old ones: rotate the journal and change those secrets)"
elif [ "$new_hits" = "n/a" ]; then
  check 7.5 INFO "secrets in sudo journal" "all-time hits: $all_hits (no update time to compare)"
else
  check 7.5 FAIL "secrets in sudo journal since the update" "$new_hits line(s) since the update, $all_hits all-time (lines not shown)"
fi

# ---------------------------------------------------------------- 8-9. scripts, dashboard, guide
sec "8-9. diagnostics scripts, dashboard and guide (installed files)"
f=$SCRIPTS_DIR/pull-connectivity-diagnostics.sh
if [ -f "$f" ] && grep -q '<ssid>' "$f" && grep -q '@<host>' "$f"; then
  check 8.1 PASS "installed connectivity script redacts SSIDs and host" "running it from a desktop is by hand"
else
  check 8.1 FAIL "installed connectivity script redacts SSIDs and host" ""
fi
bad=""
for f in "$SCRIPTS_DIR"/pull-*.sh; do
  grep -q '@<host>' "$f" || bad="$bad $(basename "$f")"
done
if [ -z "$bad" ]; then check 8.2 PASS "all pull-*.sh show user@<host>" ""; else check 8.2 FAIL "pull-*.sh missing user@<host>" "$bad"; fi
pkg=$(as_dev "$VENV_PY" -I -c 'import kestrel_pi, os; print(os.path.dirname(kestrel_pi.__file__))' 2>/dev/null)
if [ -n "$pkg" ] && grep -q 'card.pad' "$pkg/web.py"; then
  check 9.1 PASS "installed web.py has the card padding" "how it looks is by hand"
else
  check 9.1 FAIL "installed web.py has the card padding" ""
fi
if [ -n "$pkg" ] && grep -q 'set-stdin' "$pkg/userguide.py" && grep -q 'Host key verification failed' "$pkg/userguide.py"; then
  check 9.2 PASS "installed /guide has stdin flags and host-key paragraph" ""
else
  check 9.2 FAIL "installed /guide has stdin flags and host-key paragraph" ""
fi
if [ -f /var/lib/kestrel/update-run/status.json ]; then
  check 10 INFO "fresh flash" "this device was upgraded in place; the fresh-flash smoke test is by hand"
else
  check 10 INFO "fresh flash" "no in-place update recorded: this looks like a fresh flash"
fi

# ---------------------------------------------------------------- 11. sudo
sec "11. passwordless sudo for the device user"
if sudo -l -U "$DEV_USER" 2>/dev/null | grep -q 'NOPASSWD'; then
  check 11.1 INFO "device user has passwordless sudo" "YES - $(ls /etc/sudoers.d 2>/dev/null | tr '\n' ' ')"
else
  check 11.1 INFO "device user has passwordless sudo" "no - $(ls /etc/sudoers.d 2>/dev/null | tr '\n' ' ')"
fi
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
1.1  with the updates plugin's beta off (on a 1.0.5 device), 1.0.6-beta.1 is not offered
2.1  the Plugins page shows updates repo/host/token read-only, marked terminal only
3.4  the handshake came from your own test AP
3.5  the Environment card shows a count (the script tested the device user can read the DB)
3.7  set max_run_mb low and watch a roll: sudo /usr/local/lib/kestrel/config-set capture.housekeeping max_run_mb 5
     then sudo systemctl restart kestrel-capture (capture only; set it back to 512 afterwards)
3.9  sudo kestrel capture clear --yes
3.10/3.11  optional: try the same values on the web Settings page (the script tested the helper behind it)
5.3/5.4  re-run setup on a failing host / set up a new host
6.x  an actual wpa-sec / wigle upload of your own test capture
7.1  the hidden prompt; 7.3 the warning text printing; 7.4 Wi-Fi connects with --psk-stdin
8.x  run scripts/pull-connectivity-diagnostics.sh from a desktop
9.x  how the dashboard and /guide look on a phone and desktop
10   fresh-flash smoke test
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
