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

# ================================================================ 1.0.7-beta.1 checks (#36)
# Cracked networks: surface known passphrases for captured networks (display
# + import). CHECK ids match the checklist issue.
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

# ---------------------------------------------------------------- 2. cracked-networks core service (#36)
sec "2. cracked-networks core service (#36)"
imp=$(as_dev "$VENV_PY" -I -c 'import kestrel_pi.cracked as c
print(all(hasattr(c, n) for n in ("CrackedStore", "ingest_text", "parse_potfile_line")))' 2>&1)
if [ "$imp" = True ]; then
  check 2.1 PASS "installed package provides kestrel_pi.cracked" "CrackedStore + ingest_text + parser"
else
  check 2.1 FAIL "installed package provides kestrel_pi.cracked" "$(printf '%s' "$imp" | tr '\n' ' ' | cut -c1-120)"
fi
# Round-trip through a throwaway store the device user owns (placeholder data
# only; never the real store): proves the installed parser reads both formats,
# dedups, and writes a 0600 potfile.
dtmp=$(as_dev mktemp -d 2>/dev/null)
if [ -n "$dtmp" ]; then
  rt=$(as_dev "$VENV_PY" -I - "$dtmp" <<'PY' 2>&1
import os, stat, sys
from kestrel_pi import cracked
store = sys.argv[1]
text = ("0123456789abcdef0123456789abcdef:00005e0053aa:00005e005301:My Network:correct-horse\n"
        "00005e0053bb:00005e005302:Cafe Guest:s3cret\n"
        "00005e0053aa:00005e005301:My Network:correct-horse\n"
        "garbage line\n")
res = cracked.ingest_text(store, text)
counts = cracked.CrackedStore(store).counts()
pots = [f for f in os.listdir(os.path.join(store, "cracked")) if f.endswith(".potfile")]
mode = stat.S_IMODE(os.stat(os.path.join(store, "cracked", pots[0])).st_mode) if pots else -1
ok = (res == {"imported": 2, "skipped": 1}
      and counts == {"cracked_aps": 2, "cracked_essids": 2} and mode == 0o600)
print("OK" if ok else "BAD", res, counts, oct(mode))
PY
)
  as_dev rm -rf "$dtmp" 2>/dev/null
  if printf '%s\n' "$rt" | grep -q '^OK'; then
    check 2.2 PASS "parser + store round-trip (both formats, dedup, 0600)" "imported 2, skipped 1, 2 APs"
  else
    check 2.2 FAIL "parser + store round-trip" "$(printf '%s' "$rt" | tr '\n' ' ' | cut -c1-160)"
  fi
else
  check 2.2 SKIP "parser + store round-trip" "could not make a throwaway dir"
fi
# 2.3 (cracked_aps in /status.json) is emitted from the dashboard section below
# when the optional login is given.
check 2.4 MANUAL "/networks shows a cracked badge and the card shows cracked N of M" "by hand"

# ---------------------------------------------------------------- 3. passphrase visibility (#36)
sec "3. passphrase visibility (#36)"
check 3.1 MANUAL "with the web login on, a cracked row reveals its passphrase" "by hand"
check 3.2 MANUAL "show_passphrases=false hides the passphrase; badge+count stay; login off never shows it" "by hand"
# 3.3 (saved live, no restart) is in the config-tests section below.

# ---------------------------------------------------------------- 5. cracked import: CLI (#36)
sec "5. cracked import - CLI (#36)"
if grep -q 'cmd_cracked()' "$CLI" && grep -q 'cracked) cmd_cracked' "$CLI"; then
  check 5.1 PASS "installed CLI has the cracked subcommand" "import | list | clear"
else
  check 5.1 FAIL "installed CLI has the cracked subcommand" ""
fi
clist=$(as_dev "$CLI" cracked list 2>&1)
if printf '%s\n' "$clist" | grep -qi 'cracked record'; then
  check 5.2 PASS "kestrel cracked list prints counts only (no passphrase)" "$(printf '%s' "$clist" | head -1 | cut -c1-80)"
else
  check 5.2 INFO "kestrel cracked list" "ran; output not shown (it can name imported files)"
fi
check 5.3 MANUAL "the web Import cracked results upload accepts a potfile and shows the summary" "by hand"

# ---------------------------------------------------------------- 6. cracked store privacy (#36)
sec "6. cracked store privacy (#36)"
crkdir=$STORE/cracked
if [ -d "$crkdir" ]; then
  dp=$(stat -c '%a' "$crkdir")
  nbad=$(find "$crkdir" -maxdepth 1 -type f -name '*.potfile' ! -perm 600 2>/dev/null | wc -l)
  { [ "$dp" = 700 ] && [ "$nbad" -eq 0 ]; } \
    && check 6.1 PASS "cracked dir 0700, potfiles 0600" "dir=$dp potfiles ok" \
    || check 6.1 FAIL "cracked dir 0700, potfiles 0600" "dir=$dp not-0600=$nbad"
else
  check 6.1 SKIP "cracked dir 0700, potfiles 0600" "no cracked store yet"
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
# cracked networks (#36): the passphrase-visibility toggle and the import caps.
# 3.3: show_passphrases saves live via the socket (not root_only, no restart)
r = apply({"target":"config-set","table":"web","key":"show_passphrases","value":False})
check("3.3", bool(r.get("ok")) and read("web.show_passphrases") is False and not r.get("restart"),
      "web.show_passphrases saved live, no daemon restart", "ok=%s restart=%s" % (r.get("ok"), r.get("restart")))
# 4.1: the new keys are in the root allow-list with the right specs
spec = getattr(ctl, "CONFIG_SETTABLE", {})
want = {"web.show_passphrases": {"type": "bool"},
        "cracked.max_import_mb": {"type": "int", "min": 1},
        "cracked.max_import_lines": {"type": "int", "min": 1}}
check("4.1", all(spec.get(k) == v for k, v in want.items()),
      "show_passphrases + [cracked] caps settable in the allow-list", "")
# 4.2: the apply socket saves the [cracked] import caps
cur = read("cracked.max_import_mb", 2); want2 = int(cur) + 1
r = apply({"target":"config-set","table":"cracked","key":"max_import_mb","value":want2})
check("4.2", bool(r.get("ok")) and read("cracked.max_import_mb") == want2,
      "apply socket saves [cracked] max_import_mb", "ok=%s" % r.get("ok"))
PY
[ "$(sha256sum "$CONFIG" | awk '{print $1}')" = "$cfg_sum_before" ] \
  && check 0.1 PASS "real config.toml untouched by the tests" "checksum identical" \
  || check 0.1 FAIL "real config.toml untouched by the tests" "checksum changed - tell Claude"

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
print("capture:", {k: cap.get(k) for k in ("state", "enabled", "hashline_files", "cracked_aps")})
print("CHECK %-5s %-6s %s :: %s" % ("2.3", "PASS" if "cracked_aps" in cap else "FAIL", "cracked_aps present in /status.json capture", cap.get("cracked_aps")))
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
