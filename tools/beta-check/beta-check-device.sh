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

# ================================================================ checks for 1.0.6-beta.3
# The ids are the item numbers of this beta's checklist issue.

# ---------------------------------------------------------------- 1. the update
sec "1. update to this beta"
ver=$(cat /etc/kestrel/version 2>/dev/null || echo "")
upd=/var/lib/kestrel/update-run/status.json
echo "update-run status (root-owned):"
updinfo=$(/usr/bin/python3 -I - <<'PY' 2>&1
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
)
printf '%s\n' "$updinfo" | sed 's/^/  /'
if [ -n "$EXPECT_VERSION" ] && printf '%s\n' "$updinfo" | grep -qx "status: ok" \
    && printf '%s\n' "$updinfo" | grep -qx "installed: $EXPECT_VERSION"; then
  check 1.1 PASS "update offered and applied in place" "update-run: status ok, installed $EXPECT_VERSION"
elif [ -z "$EXPECT_VERSION" ]; then
  check 1.1 INFO "update offered and applied in place" "no expected version given"
else
  check 1.1 FAIL "update offered and applied in place" "update-run status doesn't show $EXPECT_VERSION installed ok"
fi
if [ -z "$EXPECT_VERSION" ]; then
  check 1.2 INFO "version" "/etc/kestrel/version = $ver (no expected version given)"
elif [ "$ver" = "$EXPECT_VERSION" ]; then
  check 1.2 PASS "version" "/etc/kestrel/version = $ver"
else
  check 1.2 FAIL "version" "/etc/kestrel/version = ${ver:-missing}, expected $EXPECT_VERSION"
fi
# NetworkManager-wait-online already failed on 1.0.6-beta.1 and is tracked as
# its own by-hand item, so it alone doesn't fail this one.
failed=$(systemctl --failed --no-legend --plain 2>/dev/null | awk '{print $1}' | tr '\n' ' ')
others=$(printf '%s\n' $failed | grep -vx 'NetworkManager-wait-online.service' | tr '\n' ' ')
if [ -z "${others// /}" ]; then
  check 1.3 PASS "no new failed units" "failed: ${failed:-none}"
else
  check 1.3 FAIL "no new failed units" "failed: $failed"
fi
tb=$(journalctl -b -q --no-pager -u kestrel-daemon -u kestrel-agent -u kestrel-capture 2>/dev/null | grep -c 'Traceback' || true)
if [ "${tb:-0}" -eq 0 ]; then
  check 1.4 PASS "no tracebacks this boot (daemon, agent, capture)" ""
else
  check 1.4 FAIL "tracebacks this boot (daemon, agent, capture)" "$tb"
  journalctl -b -q --no-pager -u kestrel-daemon -u kestrel-agent 2>/dev/null \
    | grep -E '(Error|Exception)(:|$)' | tail -n 8 | jscrub
fi
if [ -f "$upd" ] && [ "$CONFIG" -ot "$upd" ]; then
  check 1.5 PASS "settings unchanged by the update" "config.toml is older than the update run"
elif [ -f "$upd" ]; then
  check 1.5 INFO "settings unchanged by the update" "config.toml changed after the update (by you, or a setting saved since)"
else
  check 1.5 SKIP "settings unchanged by the update" "no in-place update recorded"
fi

# ---------------------------------------------------------------- 2. capture relaunches (#27)
sec "2. capture relaunches and the in-range reading"
cap_state=$(svc_active kestrel-capture.service)
cap_since=$(systemctl show -p ActiveEnterTimestamp --value kestrel-capture.service 2>/dev/null)
cap_since_s=$(date -d "$cap_since" +%s 2>/dev/null || echo 0)
cap_age=$(( $(date +%s) - cap_since_s ))
echo "capture enabled=$(cfg capture.enabled false) service=$cap_state since=${cap_since:-?} (${cap_age}s)"
echo "agent unit: active=$(svc_active kestrel-agent) policy=$(cfg agent.policy baseline) decision_seconds=$(cfg agent.decision_seconds 30)"
# Counts only: the capture journal can carry network names, so no lines leave.
nre=$(journalctl -q --no-pager -u kestrel-capture --since "-30 min" 2>/dev/null \
      | grep -c 'agent updated the scan plan' || true)
if [ "$cap_state" != "active" ]; then
  check 2.1 SKIP "relaunches for a new plan, last 30 min" "capture not running"
elif [ "$cap_age" -lt 1800 ]; then
  check 2.1 SKIP "relaunches for a new plan, last 30 min" "capture has run only ${cap_age}s; run again after 30 min ($nre so far)"
elif [ "${nre:-0}" -lt 17 ]; then
  check 2.1 PASS "relaunches for a new plan, last 30 min" "$nre (beta.2: 17)"
else
  check 2.1 FAIL "relaunches for a new plan, last 30 min" "$nre (beta.2: 17)"
fi
nk=$(find "$RUNDIR" -maxdepth 1 -type f -name '*.kismet' 2>/dev/null | wc -l)
nnamed=$(find "$RUNDIR" -maxdepth 1 -type f -regextype posix-extended \
         -regex '.*/kestrel-[0-9]+-[0-9]{6}[^/]*\.kismet' 2>/dev/null | wc -l)
echo "kismet DBs in capture-run: $nk (per-launch names: $nnamed)"
if [ "$cap_state" != "active" ]; then
  check 2.2 SKIP "capture-run holds this run's DB and at most the previous one" "capture not running"
elif [ "$nk" -ge 1 ] && [ "$nk" -le 2 ] && [ "$nnamed" -eq "$nk" ]; then
  check 2.2 PASS "capture-run holds this run's DB and at most the previous one" "$nk DB(s), per-launch names"
else
  check 2.2 FAIL "capture-run holds this run's DB and at most the previous one" "$nk DB(s), $nnamed with per-launch names"
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
if [ "$cap_state" != "active" ]; then
  check 2.3 SKIP "Environment reading is fresh" "capture not running"
elif [ "$cap_age" -lt 600 ]; then
  check 2.3 SKIP "Environment reading is fresh" "capture has run only ${cap_age}s; run again after 10 min"
elif printf '%s\n' "$envstat" | grep -q '^ENV fresh'; then
  check 2.3 PASS "Environment reading is fresh" "the collector read packets within 15 min"
else
  check 2.3 FAIL "Environment reading is fresh" "no fresh reading after ${cap_age}s of capture"
fi

# ---------------------------------------------------------------- 3. carried over from beta.2
sec "3. store, collector and run folder (carried over)"
nroot=$(find "$STORE" -mindepth 1 -maxdepth 5 ! -type l -user root 2>/dev/null | wc -l)
if [ "$nroot" -eq 0 ]; then
  check 3.1 PASS "no root-owned files or folders in the store" ""
else
  check 3.1 FAIL "no root-owned files or folders in the store" "$nroot"
fi
if [ "$cap_state" = "active" ] && [ -n "$cap_since" ]; then
  capj=$(journalctl -q --no-pager -u kestrel-capture --since "$cap_since" 2>/dev/null)
  pd=$(printf '%s\n' "$capj" | grep -c 'Permission denied' || true)
  nad=$(printf '%s\n' "$capj" | grep -c 'capture-collect: adopt:' || true)
  capj=""
  if [ "${pd:-0}" -eq 0 ]; then
    check 3.2 PASS "no 'Permission denied' from the collector since capture started" ""
  else
    check 3.2 FAIL "no 'Permission denied' from the collector since capture started" "$pd line(s) (not shown)"
  fi
  if [ "${nad:-0}" -eq 0 ]; then
    check 3.3 PASS "the store hand-over stays quiet once done" "no hand-over line since this capture start"
  else
    check 3.3 INFO "the store hand-over stays quiet once done" "$nad hand-over line(s) since this capture start"
  fi
else
  check 3.2 SKIP "no 'Permission denied' from the collector since capture started" "capture not running"
  check 3.3 SKIP "the store hand-over stays quiet once done" "capture not running"
fi
if [ -d "$RUNDIR" ]; then
  perm=$(stat -c '%U:%G %a' "$RUNDIR")
  if [ "$perm" = "root:$DEV_USER 750" ]; then
    check 3.4 PASS "capture-run ownership" "$perm"
  else
    check 3.4 FAIL "capture-run ownership" "$perm (expected root:<device user> 750)"
  fi
else
  check 3.4 SKIP "capture-run ownership" "folder not created yet"
fi
if [ "$cap_state" = "active" ]; then
  npi=$(pgrep -c -u "$DEV_USER" -f capture-collect || true)
  nrt=$(pgrep -c -u root -f capture-collect || true)
  if [ "${npi:-0}" -ge 1 ] && [ "${nrt:-0}" -eq 0 ]; then
    check 3.5 PASS "collector runs as the device user" "user=$npi root=$nrt"
  else
    check 3.5 FAIL "collector runs as the device user" "user=$npi root=$nrt"
  fi
else
  check 3.5 SKIP "collector runs as the device user" "capture not running"
fi

# ---------------------------------------------------------------- config tests on a throwaway copy
sec "config tests (throwaway copy of config.toml; commands blocked)"
cfg_sum_before=$(sha256sum "$CONFIG" | awk '{print $1}')
/usr/bin/python3 -I - "$work" <<'PY' 2>&1
import importlib.machinery, importlib.util, os, shutil, subprocess, sys, tomllib

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

# 3.6: the page's raw-data cap lands in [capture.housekeeping], the table capture reads
cur = read("capture.housekeeping.max_run_mb", 512)
want = 511 if cur != 511 else 513
r = apply({"target": "config-set", "table": "capture.housekeeping",
           "key": "max_run_mb", "value": want})
r_old = apply({"target": "config-set", "table": "capture", "key": "max_run_mb", "value": want})
check("3.6", bool(r.get("ok")) and read("capture.housekeeping.max_run_mb") == want
      and not r_old.get("ok"),
      "Settings saves max_run_mb under [capture.housekeeping]",
      "housekeeping ok=%s; old [capture] key accepted=%s" % (r.get("ok"), r_old.get("ok")))

# 3.7: the root-helper refusals still hold
results = []
for opt, val in (("host", "https://git.example.com"), ("repo", "example/kestrel"),
                 ("token", "example-not-a-token")):
    before = read("plugins.updates." + opt)
    r = apply({"plugin": "updates", "set": {opt: val}})
    results.append((not r.get("ok")) and read("plugins.updates." + opt) == before)
for table, key, vals in (("capture.housekeeping", "archive_dir",
                          ("/", "/tmp/kbc-x", "/var/lib/kestrel", "/var/lib/kestrel/capture/../x")),
                         ("capture.output", "single_file", ("../x", "a/b", ".hidden", "/etc/passwd"))):
    before = read("%s.%s" % (table, key))
    for val in vals:
        r = apply({"target": "config-set", "table": table, "key": key, "value": val})
        results.append((not r.get("ok")) and read("%s.%s" % (table, key)) == before)
check("3.7", all(results), "root helper refusals still hold",
      "%d of %d refused" % (sum(results), len(results)))
PY
if [ "$(sha256sum "$CONFIG" | awk '{print $1}')" = "$cfg_sum_before" ]; then
  check 0.3 PASS "real config.toml untouched by the tests" "checksum identical before and after"
else
  check 0.3 FAIL "real config.toml untouched by the tests" "checksum changed - tell Claude"
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
4.1  a new handshake from your own test AP is saved (the dashboard count goes up)
4.2  the Environment card on the dashboard shows a count, not "unknown"
4.3  the by-hand items still open on the 1.0.6-beta.1 and beta.2 issues, and
     the failed NetworkManager-wait-online unit
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
