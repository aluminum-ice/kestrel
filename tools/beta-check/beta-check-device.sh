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

# ================================================================ 1.0.9 checks (#50-#55, #58-#68)
# Beta 1: HTTPS dashboard and sign-in (#51), the e-ink address row (#50), and
# four new plugins: downloads (#52), map (#53), notify (#54), hat-buttons (#55).
# Beta 2: the certificate within Apple's limit (#58), the plugin row buttons
# (#59) and the report's redaction (#60), as sections 9-11. Beta 3: notify split
# into ntfy and telegram (#62), the map's OpenStreetMap background (#64) and the
# Captured networks controls (#65), as sections 12-14; notify's 6.x checks are
# replaced by section 12. Beta 4: map markers coloured by recovered status (#68),
# as section 15. Earlier betas' checks stay, so one report serves every
# 1.0.9 checklist issue. The CHECK ids match them.
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

# ---------------------------------------------------------------- 2. HTTPS dashboard and sign-in (#51)
sec "2. HTTPS dashboard and sign-in (#51)"
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
  check 2.1 PASS "certificate made and private" "dir 700, cert.pem and key.pem 600"
elif [ "$tls" = off ]; then
  check 2.1 SKIP "certificate made and private" "[web] tls is off"
else
  check 2.1 FAIL "certificate made and private" "see the modes above"
fi
# The fingerprint itself never goes in the report: only whether they match.
fp_cli=$(timeout 20 "$CLI" web fingerprint 2>/dev/null | tail -1)
fp_log=$(journalctl -b -q --no-pager -u kestrel-daemon 2>/dev/null \
  | grep -oE 'HTTPS certificate SHA-256 [0-9A-F:]+' | tail -1 | awk '{print $NF}')
if [ -n "$fp_cli" ] && [ "$fp_cli" = "$fp_log" ]; then
  check 2.2 PASS "kestrel web fingerprint matches the daemon's journal" "values not shown"
elif [ -z "$fp_log" ]; then
  check 2.2 INFO "kestrel web fingerprint matches the daemon's journal" "no HTTPS line in this boot's daemon journal"
else
  check 2.2 FAIL "kestrel web fingerprint matches the daemon's journal" "they differ (values not shown)"
fi
# Listening sockets only (read-only, no request): HTTPS and the forwarder on each
# management address, plain HTTP on loopback. Addresses are counted, not shown.
lst=$(ss -ltnH 2>/dev/null | awk '{print $4}')
https_ext=$(printf '%s\n' "$lst" | grep -E ":$tport\$" | grep -vcE '^(127\.|\[::1\])' || true)
http_ext=$(printf '%s\n' "$lst" | grep -E ":$port\$" | grep -vcE '^(127\.|\[::1\])' || true)
http_lo=$(printf '%s\n' "$lst" | grep -cE "^127\.[0-9.]+:$port\$" || true)
echo "  listeners: $tport off-loopback=$https_ext  $port off-loopback=$http_ext  $port loopback=$http_lo"
if [ "$tls" = off ]; then
  check 2.3 SKIP "HTTPS and the forwarder listen on each management address" "[web] tls is off"
elif [ "${https_ext:-0}" -ge 1 ] && [ "$https_ext" = "$http_ext" ] && [ "${http_lo:-0}" -ge 1 ]; then
  check 2.3 PASS "HTTPS and the forwarder listen on each management address" "$https_ext address(es); loopback plain HTTP too"
else
  check 2.3 FAIL "HTTPS and the forwarder listen on each management address" "see the listener counts above"
fi
# 2.4 (loopback Basic) and 2.5 (nosniff) come from the dashboard section below.
cat_ok=$(as_dev "$VENV_PY" -I -c 'from kestrel_pi import config_catalog as c
a, b = c.find("web", "tls"), c.find("web", "tls_port")
print(bool(a) and bool(b) and a.get("editable") is False and b.get("editable") is False)' 2>&1)
if [ "$cat_ok" = True ]; then
  check 2.6 PASS "Advanced settings lists web.tls and web.tls_port read-only" ""
else
  check 2.6 FAIL "Advanced settings lists web.tls and web.tls_port read-only" "$(printf '%s' "$cat_ok" | tr '\n' ' ' | cut -c1-120)"
fi
# 2.7 (web.tls refused through the settings channel) is in the config tests below.
for id in 2.8 2.9 2.10 2.11 2.12 2.13 2.14 2.15 2.16 2.17 2.18; do
  check "$id" MANUAL "see the checklist" "by hand"
done

# ---------------------------------------------------------------- 3-7. the plugins (#50, #52-#55)
sec "3-7. plugins (#50, #52-#55)"
# 3.1, 4.1, 5.1, 6.1, 7.1 and 8.1 read the plugin declarations statically in the
# config tests below.
n=$(as_dev sh -c 'ls "$1" 2>/dev/null | grep -c "^export-.*\.zip$"' _ "$DEV_HOME/.local/state/kestrel-downloads" || true)
if as_dev test -d "$DEV_HOME/.local/state/kestrel-downloads"; then
  [ "${n:-0}" -eq 0 ] && check 4.2 PASS "no leftover export zips" "" \
    || check 4.2 FAIL "no leftover export zips" "$n left"
else
  check 4.2 SKIP "no leftover export zips" "downloads hasn't been used yet"
fi
loc=$(as_dev "$VENV_PY" -I -c 'from kestrel_pi.geo import GeoStore
print(callable(getattr(GeoStore, "locations", None)))' 2>&1)
[ "$loc" = True ] && check 5.2 PASS "installed package provides GeoStore.locations()" "" \
  || check 5.2 FAIL "installed package provides GeoStore.locations()" "$(printf '%s' "$loc" | tr '\n' ' ' | cut -c1-120)"
# 6.x (notify) is replaced by section 12 (ntfy and telegram) in beta 3.
for id in 3.2 3.3 3.4 4.3 4.4 4.5 4.6 4.7 4.8 5.3 5.4 5.5 5.6 5.7 5.8 5.9 \
          7.2 7.3 7.4 7.5 7.6 7.7 7.8 7.9; do
  check "$id" MANUAL "see the checklist" "by hand"
done

# ---------------------------------------------------------------- 9-11. beta 2 fixes (#58-#60)
sec "9. certificate within Apple's 825-day limit (#58)"
# The span is read from the public certificate as the device user; the
# certificate and its fingerprint never go in the report.
span=$(as_dev "$VENV_PY" -I - "$WEBSTATE/cert.pem" <<'PY' 2>&1
import sys
from cryptography import x509
with open(sys.argv[1], "rb") as handle:
    cert = x509.load_pem_x509_certificate(handle.read())
start = getattr(cert, "not_valid_before_utc", None) or cert.not_valid_before
end = getattr(cert, "not_valid_after_utc", None) or cert.not_valid_after
print((end - start).days)
PY
)
echo "  certificate validity span: $span days"
if [ "$tls" = off ]; then
  check 9.1 SKIP "certificate valid for no more than 825 days" "[web] tls is off"
elif printf '%s' "$span" | grep -qxE '[0-9]+' && [ "$span" -le 825 ]; then
  check 9.1 PASS "certificate valid for no more than 825 days" "$span days"
else
  check 9.1 FAIL "certificate valid for no more than 825 days" "$(printf '%s' "$span" | tr '\n' ' ' | cut -c1-120)"
fi
started=$(printf '%s\n' "$updinfo" | sed -n 's/^started_at: //p' | cut -d. -f1)
made=$(as_dev stat -c '%Y' "$WEBSTATE/cert.pem" 2>/dev/null)
if [ "$tls" = off ] || [ -z "$made" ]; then
  check 9.2 SKIP "the beta 1 certificate was replaced after the update" "no certificate"
elif [ -z "$started" ]; then
  check 9.2 SKIP "the beta 1 certificate was replaced after the update" "no in-place update recorded"
elif [ "$made" -ge "$started" ]; then
  check 9.2 PASS "the beta 1 certificate was replaced after the update" "made after the update started"
else
  check 9.2 INFO "the beta 1 certificate was replaced after the update" "made before this update (a fresh flash, or already compliant)"
fi
for id in 9.3 9.4 9.5; do
  check "$id" MANUAL "see the checklist" "by hand"
done

sec "10. plugin row buttons (#59)"
# Reads the installed plugins' __actions__ declarations (plugin source only, no
# capture content) and runs them through the daemon's own normalizer.
btn=$(as_dev "$VENV_PY" -I - "$LIBDIR/plugins" <<'PY' 2>&1
import ast, glob, os, sys
from kestrel_pi.plugins import normalize_actions
dropped, labels = [], {}
for path in sorted(glob.glob(os.path.join(sys.argv[1], "*.py"))):
    name = os.path.basename(path)[:-3]
    with open(path, encoding="utf-8") as handle:
        tree = ast.parse(handle.read())
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id == "__actions__" for t in node.targets):
            try:
                declared = ast.literal_eval(node.value)
            except ValueError:
                dropped.append(name + " (not a literal)")
                continue
            kept = normalize_actions(declared)
            labels[name] = [a["label"] for a in kept]
            if len(kept) != len(declared):
                dropped.append(name)
want = {"downloads": "Download", "map": "Map", "ntfy": "Status", "telegram": "Status"}
missing = sorted(n for n, label in want.items() if label not in labels.get(n, []))
print("PASS" if not dropped and not missing else "FAIL",
      "dropped=%s missing=%s" % (sorted(dropped), missing))
PY
)
case "$btn" in
  PASS*) check 10.1 PASS "every installed plugin's row buttons are kept" "${btn#PASS }" ;;
  *)     check 10.1 FAIL "every installed plugin's row buttons are kept" "$(printf '%s' "${btn#FAIL }" | tr '\n' ' ' | cut -c1-160)" ;;
esac
# 10.2 (the buttons in the running daemon's /status.json) is in the dashboard section.
check 10.3 MANUAL "see the checklist" "by hand"

sec "11. report redaction (#60)"
check 11.1 MANUAL "see the checklist" "by hand"

sec "12. ntfy and telegram (#62)"
# 12.1 (declarations) is in the config tests below.
if [ -e "$LIBDIR/plugins/notify.py" ]; then
  check 12.2 FAIL "the old notify plugin is gone" "notify.py is still installed"
else
  check 12.2 PASS "the old notify plugin is gone" "the update removed notify.py"
fi
topic=$DEV_HOME/.local/state/kestrel-ntfy/topic
if as_dev test -f "$topic"; then
  [ "$(as_dev stat -c '%a' "$topic" 2>/dev/null)" = 600 ] \
    && check 12.3 PASS "generated ntfy topic file is private" "0600 (contents not read)" \
    || check 12.3 FAIL "generated ntfy topic file is private" "mode $(as_dev stat -c '%a' "$topic" 2>/dev/null)"
else
  check 12.3 SKIP "generated ntfy topic file is private" "no generated topic yet"
fi
for id in 12.4 12.5 12.6 12.7; do
  check "$id" MANUAL "see the checklist" "by hand"
done

sec "13. map background (#64)"
# 13.1 (tile_url declared) is in the config tests; 13.2 (the page) is in the
# dashboard section.
for id in 13.3 13.4 13.5 13.6 13.7 13.8; do
  check "$id" MANUAL "see the checklist" "by hand"
done

sec "14. Captured networks controls (#65)"
# 14.1 (the page) is in the dashboard section.
for id in 14.2 14.3; do
  check "$id" MANUAL "see the checklist" "by hand"
done

sec "15. map markers by recovered status (#68)"
# 15.1 and 15.2 (the map page) are in the dashboard section.
for id in 15.3 15.4 15.5 15.6; do
  check "$id" MANUAL "see the checklist" "by hand"
done

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
# 2.7: HTTPS can only be switched from a terminal, never through the settings socket.
before = read("web.tls")
r = apply({"target": "config-set", "table": "web", "key": "tls", "value": "off"})
check("2.7", not r.get("ok") and read("web.tls") == before,
      "web.tls is refused through the settings channel (CLI only)", "error=%s" % r.get("error"))
# Plugin declarations, read statically (nothing imported or run).
plugs = ctl.static_plugins()
def decl(name):
    found = plugs.get(name)
    return (found[0] or {}) if found else {}
def caps(name): return set(decl(name).get("capabilities") or ())
def state(name): return "enabled" if read("plugins.%s.enabled" % name) is True else "disabled"
opts = decl("eink").get("options") or {}
check("3.1", (opts.get("show_ip") or {}).get("default") is True
      and (opts.get("show_fingerprint_minutes") or {}).get("default") == 10,
      "eink declares show_ip (on) and show_fingerprint_minutes (10)", "")
check("4.1", caps("downloads") == {"capture-read", "web"},
      "downloads: capture-read + web, no network", state("downloads"))
check("5.1", caps("map") == {"capture-read", "location", "web"},
      "map: capture-read + location + web, no network", state("map"))
check("7.1", caps("hat-buttons") == {"ui"} and "buttons" in (decl("hat-buttons").get("provides") or ()),
      "hat-buttons: local only (ui), provides buttons", state("hat-buttons"))
expected = {"memtemp", "session-stats", "gps", "wpa-sec", "wigle", "backup", "ups-hat", "eink",
            "updates", "remote-train", "downloads", "map", "ntfy", "telegram", "hat-buttons"}
invalid = sorted(n for n, v in plugs.items() if v[0] is None)
missing = sorted(expected - set(plugs))
check("8.1", not invalid and not missing, "all fifteen shipped plugins present and readable",
      "missing=%s invalid=%s" % (missing, invalid))
# 12.1: one plugin per service, each with only its own secret options.
def secrets(name):
    return {k for k, v in (decl(name).get("options") or {}).items() if (v or {}).get("secret")}
check("12.1", "network" in caps("ntfy") and "network" in caps("telegram")
      and secrets("ntfy") == {"topic", "token"} and secrets("telegram") == {"bot_token", "chat_id"}
      and "notify" not in plugs,
      "ntfy and telegram declare network and only their own secrets; no notify",
      "ntfy %s, telegram %s" % (state("ntfy"), state("telegram")))
if read("plugins.notify") is not None:
    print("note: config.toml still has an unused [plugins.notify] table")
# 13.1: the map's background is OpenStreetMap by default, over https.
tile = (decl("map").get("options") or {}).get("tile_url") or {}
check("13.1", tile.get("default") == "https://tile.openstreetmap.org/{z}/{x}/{y}.png",
      "map declares tile_url, OpenStreetMap by default",
      "tile_url %s" % ("set" if read("plugins.map.tile_url") is not None else "default"))
PY
[ "$(sha256sum "$CONFIG" | awk '{print $1}')" = "$cfg_sum_before" ] \
  && check 0.1 PASS "real config.toml untouched by the tests" "checksum identical" \
  || check 0.1 FAIL "real config.toml untouched by the tests" "checksum changed - tell Claude"

} >"$raw" 2>&1

# ---------------------------------------------------------------- dashboard status (optional)
{
sec "dashboard /status.json (selected fields)"
printf '%s\n' "$web_pw" | /usr/bin/python3 -I - <<'PY' 2>&1
import base64, json, sys, tomllib, urllib.error, urllib.request
pw = sys.stdin.readline().rstrip("\n")
with open("/etc/kestrel/config.toml", "rb") as h:
    cfg = tomllib.load(h)
web = cfg.get("web", {}) or {}
mode = str(web.get("auth", "system")).lower()
port = int(web.get("port", 8080))
def chk(cid, result, what, detail=""):
    print("CHECK %-5s %-6s %s :: %s" % (cid, result, what, detail))
# 2.5: every response carries nosniff, the sign-in refusal included (no password needed).
try:
    hdrs = urllib.request.urlopen("http://127.0.0.1:%d/status.json" % port, timeout=15).headers
except urllib.error.HTTPError as exc:
    hdrs = exc.headers
except Exception as exc:
    hdrs = None
    chk("2.5", "INFO", "responses carry X-Content-Type-Options: nosniff", "no answer: %s" % type(exc).__name__)
if hdrs is not None:
    chk("2.5", "PASS" if hdrs.get("X-Content-Type-Options") == "nosniff" else "FAIL",
        "responses carry X-Content-Type-Options: nosniff", "")
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
        chk("2.4", "SKIP", "loopback /status.json with Basic credentials", "no password given")
        chk("10.2", "SKIP", "the running daemon offers the row buttons", "no password given")
        chk("13.2", "SKIP", "the map page draws the attributed background", "no password given")
        chk("14.1", "SKIP", "Captured networks has its controls at the top", "no password given")
        chk("15.1", "SKIP", "the map explains its recovered colours", "no password given")
        sys.exit(0)
    req.add_header("Authorization", "Basic " + base64.b64encode(
        ("%s:%s" % (user, pw)).encode()).decode())
try:
    s = json.load(urllib.request.urlopen(req, timeout=15))
except Exception as exc:
    print("(not available: %s)" % type(exc).__name__)
    chk("2.4", "FAIL" if mode in ("basic", "system") else "INFO",
        "loopback /status.json with Basic credentials", type(exc).__name__)
    chk("10.2", "SKIP", "the running daemon offers the row buttons", "no /status.json")
    chk("13.2", "SKIP", "the map page draws the attributed background", "no /status.json")
    chk("14.1", "SKIP", "Captured networks has its controls at the top", "no /status.json")
    chk("15.1", "SKIP", "the map explains its recovered colours", "no /status.json")
    sys.exit(0)
if mode in ("basic", "system"):
    chk("2.4", "PASS", "loopback /status.json with Basic credentials", "plain HTTP on 127.0.0.1")
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
# 10.2: an enabled, loaded downloads/map/ntfy/telegram offers its main-page button.
offered = {p.get("name"): [a.get("label") for a in (p.get("actions") or []) if isinstance(a, dict)]
           for p in s.get("plugins", []) if p.get("state") == "loaded"}
want = {"downloads": "Download", "map": "Map", "ntfy": "Status", "telegram": "Status"}
on = sorted(n for n in want if n in offered)
if not on:
    chk("10.2", "SKIP", "the running daemon offers the row buttons",
        "none of downloads, map, ntfy, telegram loaded")
else:
    bad = [n for n in on if want[n] not in offered[n]]
    chk("10.2", "FAIL" if bad else "PASS", "the running daemon offers the row buttons",
        ("missing: %s" % ", ".join(bad)) if bad else "checked: %s" % ", ".join(on))
# 13.2 and 14.1: pages read over loopback with the same credentials. Only
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
    chk("13.2", "SKIP", "the map page draws the attributed background", "map not loaded")
    chk("15.1", "SKIP", "the map explains its recovered colours", "map not loaded")
else:
    code, hdrs, body = page("/plugins/map/")
    policy = (hdrs or {}).get("Referrer-Policy") if hdrs else None
    if code != 200:
        chk("13.2", "FAIL", "the map page draws the attributed background", "HTTP %s" % code)
    elif "OpenStreetMap</a> contributors" in body:
        ok = 'referrerpolicy="origin"' in body and policy == "no-referrer"
        chk("13.2", "PASS" if ok else "FAIL", "the map page draws the attributed background",
            "tiles send the origin only; page Referrer-Policy %s" % policy)
    else:
        chk("13.2", "INFO", "the map page draws the attributed background",
            "no background drawn (no located networks, or tile_url empty)")
    # 15.1/15.2: the legend proves the new markers; the counts are tooltips
    # only (never a name, location or passphrase).
    if code == 200 and "Green with a" in body and "not recovered" in body:
        chk("15.1", "PASS", "the map explains its recovered colours", "legend present")
        import re
        titles = re.findall(r"<title>([^<]*)</title>",
                            body.split('class="narrow-only"', 1)[0])
        pins = [t for t in titles if t.endswith("recovered")]
        squares = [int(m.group(1)) for m in
                   (re.search(r"; (\d+) recovered\)", t) for t in titles) if m]
        print("CHECK %-5s %-6s %s :: markers %d recovered / %d not; squares %d with "
              "a recovered network / %d without" % (
                  "15.2", "INFO", "map markers by recovered status (this view)",
                  sum(1 for t in pins if t.endswith("passphrase recovered")),
                  sum(1 for t in pins if t.endswith("not recovered")),
                  sum(1 for n in squares if n), sum(1 for n in squares if not n)))
    elif code == 200 and "No captured network matching" in body:
        chk("15.1", "SKIP", "the map explains its recovered colours", "no located networks")
    else:
        chk("15.1", "FAIL", "the map explains its recovered colours", "legend missing")
code, hdrs, body = page("/networks")
if code != 200:
    chk("14.1", "FAIL", "Captured networks has its controls at the top", "HTTP %s" % code)
else:
    top = body.split("<h1>", 1)[0]
    # With the web login off the import control is hidden on purpose.
    ok = ('class="nethead"' in top and 'class="countbar"' in body
          and ("Import results" in top or mode == "none"))
    chk("14.1", "PASS" if ok else "FAIL", "Captured networks has its controls at the top",
        ("Import results above the heading" if mode != "none" else "web login off: no import")
        if ok else "markers missing")
w = s.get("widgets", {}) or {}
for name in ("wpa-sec", "wigle", "backup", "remote-train", "updates", "ntfy", "telegram",
             "hat-buttons"):
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
1.6  with the updates plugin's beta OFF this beta is NOT offered; with beta ON it
     is offered and applies in place
2.x  kestrel web status shows https:// and "HTTPS: on"; first visit on iPhone and
     Android (warning, fingerprint matches, proceed); an old :8080 bookmark lands
     on the same page over HTTPS; sign in / wrong password / back to the page;
     still signed in after a settings save; Sign out; the wait after wrong
     passwords; logout-all; tls off then on keeps the fingerprint; e-ink shows
     "HTTPS key" for ten minutes after boot
3.x  the e-ink address row matches kestrel web status; one link up shows one
     address; show_ip = false hides the row
4.x  enabling downloads asks for no acknowledgment; each download from a phone
     over HTTPS; hashcat accepts the combined file; "new since last"; no
     cracked/ or archive/ in a zip; refused with the web login off
5.x  map markers, zoom and Show all, row links, filters, phone and dark mode,
     hidden with show_locations off, works with the device offline
7.x  each key on the "Last key press" card (confirms the pins); info screen;
     confirm prompt (also in the first ten minutes after boot) and cancel;
     shutdown; reboot; KEY2 stops capture; Pi 5 and Zero 2 W, V1 and V2 HATs
9.x  Brave or Chrome on a Mac no longer calls the certificate "not standards
     compliant"; iPhone Safari lets you proceed; a browser that trusted the
     beta 1 certificate asks once more, and the new fingerprint matches
10.x Download, Map, Status and Send test show on the Plugins page for the
     enabled plugins, and each opens its page
11.x this report reads "kestrel web fingerprint", not "<hostname> web ..."
12.x enabling ntfy or telegram asks for the acknowledgment; ntfy Status shows a
     topic and Send test arrives; a digest (counts only); on battery; capture
     stopped vs switched off; offline queue; telegram Send test reaches your chat
13.x the map shows streets under the pins and they line up; the attribution;
     a square at one spot lists its networks; Reset view; back links; phone
     and dark mode; tile_url empty shows the grid
14.x Import results at the top opens the upload form in place; the arrows
     beside the count turn the page; the phone layout
15.x a recovered network's pin is green with a lock, others gray; a square is
     green with a lock badge if any network in it is recovered; the one-spot
     list leads with a lock for recovered networks and 📍 or ≈○, no words;
     the passphrase never shows on the map
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
