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

# ================================================================ 1.0.10 checks (#81-#89)
# Beta 1: plugins made standalone with their own helper files (#81), option
# choices (#82), the eink-ws213 plugin for the 2.13" HAT (#83), e-ink motion
# timed by each GPS fix (#84), shared plugin libraries (#85) - alerts (#86),
# eink_face (#87) and hat_keys (#88) - and eink-ws27 beside a deprecated eink
# (#89), as sections 2-8. Section 9 repeats the cheap 1.0.9 checks as
# regressions. The CHECK ids match the checklist issue for 1.0.10-beta.1.
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

# ---------------------------------------------------------------- 2. standalone plugins and shared libraries (#81, #85-#88)
sec "2. standalone plugins and shared libraries (#81, #85-#88)"
# Code that served particular plugins left the kestrel_pi package (#81): the
# HAT keys, the alert code and the vendored e-Paper driver. An in-place update
# reinstalls the package, so they must be gone from the venv.
gone=$(as_dev "$VENV_PY" -I -c 'import importlib.util
names = ("kestrel_pi.hatkeys", "kestrel_pi.alerts", "kestrel_pi.vendor")
print(" ".join(n for n in names if importlib.util.find_spec(n) is not None) or "none")' 2>&1 | tail -1)
if [ "$gone" = none ]; then
  check 2.1 PASS "plugin-only modules are gone from the installed package" "no hatkeys, alerts or vendor"
else
  check 2.1 FAIL "plugin-only modules are gone from the installed package" "still there: $(printf '%s' "$gone" | cut -c1-120)"
fi
bad=""
for f in _lib_alerts.py _lib_eink_face.py _lib_hat_keys.py; do
  st=$(stat -c '%U %a' "$LIBDIR/plugins/$f" 2>/dev/null || echo missing)
  echo "  $f: $st"
  [ "$st" = "root 644" ] || bad="$bad $f($st)"
done
if [ -z "$bad" ]; then
  check 2.2 PASS "the three plugin libraries are installed, root-owned 0644" ""
else
  check 2.2 FAIL "the three plugin libraries are installed, root-owned 0644" "$bad"
fi
# Load each library the way a plugin does, as the device user in the venv.
# Loading runs no plugin and touches no hardware (drawing and GPIO are lazy).
apis=$(as_dev "$VENV_PY" -I - "$LIBDIR/plugins" <<'PY' 2>&1 | tail -1
import os, sys
from kestrel_pi import load_library
caller = os.path.join(sys.argv[1], "eink-ws27.py")
out = []
for name, api in (("alerts", 1), ("eink_face", 1), ("hat_keys", 0)):
    try:
        mod = load_library(caller, name, api=api)
        out.append("%s=%d.%d" % ((name,) + tuple(mod.LIB_API)))
    except Exception as exc:
        out.append("%s=FAIL(%s)" % (name, type(exc).__name__))
print(" ".join(out))
PY
)
echo "  libraries: $apis"
case "$apis" in
  "alerts=1."*" eink_face=1."*" hat_keys=0."*) check 2.3 PASS "each library loads with the API its plugins ask for" "$apis" ;;
  *) check 2.3 FAIL "each library loads with the API its plugins ask for" "$(printf '%s' "$apis" | cut -c1-160)" ;;
esac
# 2.4 (_lib_ files and the name lib never listed as plugins) is in the config tests.
miss=""
for f in _eink_epdconfig.py _eink_epd2in7.py _eink_epd2in7_V2.py \
         _eink-ws27_epdconfig.py _eink-ws27_epd2in7.py _eink-ws27_epd2in7_V2.py \
         _eink-ws213_epdconfig.py _eink-ws213_epd2in13_V2.py _eink-ws213_epd2in13_V3.py \
         _eink-ws213_epd2in13_V4.py; do
  [ -f "$LIBDIR/plugins/$f" ] || miss="$miss $f"
done
if [ -z "$miss" ]; then
  check 2.5 PASS "each e-ink plugin's driver helper files are installed" "eink, eink-ws27, eink-ws213"
else
  check 2.5 FAIL "each e-ink plugin's driver helper files are installed" "missing:$miss"
fi
liberr=$(journalctl -b -q --no-pager -u kestrel-daemon 2>/dev/null \
  | grep -cE 'library API|plugin library .* not found|has no LIB_API' || true)
if [ "${liberr:-0}" -eq 0 ]; then
  check 2.6 PASS "no plugin-library load errors in this boot's daemon journal" ""
else
  check 2.6 FAIL "no plugin-library load errors in this boot's daemon journal" "$liberr line(s)"
fi

# ---------------------------------------------------------------- 3. option choices (#82)
sec "3. option choices (#82)"
# 3.1 (declared choices) and 3.2 (a value outside them refused) are in the config tests.
for id in 3.3 3.4; do
  check "$id" MANUAL "see the checklist" "by hand"
done

# ---------------------------------------------------------------- 4. ntfy and telegram on the alerts library (#86)
sec "4. ntfy and telegram on the alerts library (#86)"
# 4.1 (declarations) is in the config tests below.
topic=$DEV_HOME/.local/state/kestrel-ntfy/topic
if as_dev test -f "$topic"; then
  [ "$(as_dev stat -c '%a' "$topic" 2>/dev/null)" = 600 ] \
    && check 4.2 PASS "generated ntfy topic file is still private" "0600 (contents not read)" \
    || check 4.2 FAIL "generated ntfy topic file is still private" "mode $(as_dev stat -c '%a' "$topic" 2>/dev/null)"
else
  check 4.2 SKIP "generated ntfy topic file is still private" "no generated topic"
fi
# The library keeps each plugin's queue where the plugin's own copy kept it.
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
  none) check 4.3 SKIP "the alert queues read back" "no queue file yet" ;;
  *BAD*) check 4.3 FAIL "the alert queues read back" "$queues" ;;
  *) check 4.3 PASS "the alert queues read back" "queued: $queues" ;;
esac
for id in 4.4 4.5 4.6; do
  check "$id" MANUAL "see the checklist" "by hand"
done

# ---------------------------------------------------------------- 5. the e-ink face library and motion (#84, #87)
sec "5. the e-ink face library and motion (#84, #87)"
# 5.1 (each enabled e-ink plugin loaded, with its Motion card) is in the dashboard section.
for id in 5.2 5.3 5.4; do
  check "$id" MANUAL "see the checklist" "by hand"
done

# ---------------------------------------------------------------- 6. the HAT keys library (#88)
sec "6. the HAT keys library (#88)"
# 6.1 (the key options) is in the config tests below.
nokeys=$(journalctl -b -q --no-pager -u kestrel-daemon 2>/dev/null | grep -c 'HAT keys unavailable' || true)
if [ "${nokeys:-0}" -eq 0 ]; then
  check 6.2 INFO "HAT keys started" "no 'HAT keys unavailable' line this boot"
else
  check 6.2 INFO "HAT keys started" "$nokeys 'HAT keys unavailable' line(s): no HAT, keys off, or bad pins"
  journalctl -b -q --no-pager -u kestrel-daemon 2>/dev/null | grep 'HAT keys unavailable' | tail -3 | jscrub
fi
for id in 6.3 6.4 6.5 6.6 6.7; do
  check "$id" MANUAL "see the checklist" "by hand"
done

# ---------------------------------------------------------------- 7. eink-ws27 and the deprecated eink (#89)
sec "7. eink-ws27 and the deprecated eink (#89)"
# 7.1 (all sixteen plugins) and 7.3 (the descriptions) are in the config tests;
# 7.4 (the deprecation on eink's health note) is in the dashboard section.
diff_helpers=""
for f in epdconfig epd2in7 epd2in7_V2; do
  h_old=$(sha256sum "$LIBDIR/plugins/_eink_$f.py" 2>/dev/null | awk '{print $1}')
  h_new=$(sha256sum "$LIBDIR/plugins/_eink-ws27_$f.py" 2>/dev/null | awk '{print $1}')
  { [ -n "$h_old" ] && [ "$h_old" = "$h_new" ]; } || diff_helpers="$diff_helpers $f"
done
if [ -z "$diff_helpers" ]; then
  check 7.2 PASS "eink-ws27's driver helpers are the same files as eink's" "sha256 identical"
else
  check 7.2 FAIL "eink-ws27's driver helpers are the same files as eink's" "differ or missing:$diff_helpers"
fi
for id in 7.5 7.6 7.7; do
  check "$id" MANUAL "see the checklist" "by hand"
done

# ---------------------------------------------------------------- 8. the diagnostics script (#83)
sec "8. the e-ink diagnostics script (#83)"
check 8.1 MANUAL "see the checklist" "by hand (runs on a computer)"

# ---------------------------------------------------------------- 9. regressions from 1.0.9
sec "9. regressions from 1.0.9"
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
  check 9.1 PASS "certificate made and private" "dir 700, cert.pem and key.pem 600"
elif [ "$tls" = off ]; then
  check 9.1 SKIP "certificate made and private" "[web] tls is off"
else
  check 9.1 FAIL "certificate made and private" "see the modes above"
fi
# The fingerprint itself never goes in the report: only whether they match.
fp_cli=$(timeout 20 "$CLI" web fingerprint 2>/dev/null | tail -1)
fp_log=$(journalctl -b -q --no-pager -u kestrel-daemon 2>/dev/null \
  | grep -oE 'HTTPS certificate SHA-256 [0-9A-F:]+' | tail -1 | awk '{print $NF}')
if [ -n "$fp_cli" ] && [ "$fp_cli" = "$fp_log" ]; then
  check 9.2 PASS "kestrel web fingerprint matches the daemon's journal" "values not shown"
elif [ -z "$fp_log" ]; then
  check 9.2 INFO "kestrel web fingerprint matches the daemon's journal" "no HTTPS line in this boot's daemon journal"
else
  check 9.2 FAIL "kestrel web fingerprint matches the daemon's journal" "they differ (values not shown)"
fi
# Listening sockets only (read-only, no request): HTTPS and the forwarder on each
# management address, plain HTTP on loopback. Addresses are counted, not shown.
lst=$(ss -ltnH 2>/dev/null | awk '{print $4}')
https_ext=$(printf '%s\n' "$lst" | grep -E ":$tport\$" | grep -vcE '^(127\.|\[::1\])' || true)
http_ext=$(printf '%s\n' "$lst" | grep -E ":$port\$" | grep -vcE '^(127\.|\[::1\])' || true)
http_lo=$(printf '%s\n' "$lst" | grep -cE "^127\.[0-9.]+:$port\$" || true)
echo "  listeners: $tport off-loopback=$https_ext  $port off-loopback=$http_ext  $port loopback=$http_lo"
if [ "$tls" = off ]; then
  check 9.3 SKIP "HTTPS and the forwarder listen on each management address" "[web] tls is off"
elif [ "${https_ext:-0}" -ge 1 ] && [ "$https_ext" = "$http_ext" ] && [ "${http_lo:-0}" -ge 1 ]; then
  check 9.3 PASS "HTTPS and the forwarder listen on each management address" "$https_ext address(es); loopback plain HTTP too"
else
  check 9.3 FAIL "HTTPS and the forwarder listen on each management address" "see the listener counts above"
fi
# 9.4 (loopback Basic) and 9.5 (nosniff) come from the dashboard section below.
cat_ok=$(as_dev "$VENV_PY" -I -c 'from kestrel_pi import config_catalog as c
a, b = c.find("web", "tls"), c.find("web", "tls_port")
print(bool(a) and bool(b) and a.get("editable") is False and b.get("editable") is False)' 2>&1)
if [ "$cat_ok" = True ]; then
  check 9.6 PASS "Advanced settings lists web.tls and web.tls_port read-only" ""
else
  check 9.6 FAIL "Advanced settings lists web.tls and web.tls_port read-only" "$(printf '%s' "$cat_ok" | tr '\n' ' ' | cut -c1-120)"
fi
# 9.7 (web.tls refused through the settings channel) is in the config tests below.
# The helper run directly with a malformed request: it answers without reading
# any password (a bad request never reaches the check) and writes nothing. It
# must now exit 0 once it has answered, so a refused request leaves no failed
# kestrel-web-auth@ unit.
auth_out=$(printf '{}' | timeout 10 /usr/bin/python3 -I "$CTL" checkauth 2>/dev/null); auth_rc=$?
if [ "$auth_rc" -eq 0 ] && printf '%s' "$auth_out" | grep -q '"ok": false'; then
  check 9.8 PASS "the login helper exits 0 after answering a refused request" "rc=0, refused"
else
  check 9.8 FAIL "the login helper exits 0 after answering a refused request" \
    "rc=$auth_rc reply=$(printf '%s' "$auth_out" | tr -cd '[:alnum:] :{}",_' | cut -c1-80)"
fi
for old in notify hat-buttons; do
  [ -e "$LIBDIR/plugins/$old.py" ] && gone_old="${gone_old:-} $old"
done
if [ -z "${gone_old:-}" ]; then
  check 9.9 PASS "the replaced notify and hat-buttons plugins are gone" ""
else
  check 9.9 FAIL "the replaced notify and hat-buttons plugins are gone" "still installed:$gone_old"
fi
# 9.4, 9.5 and 9.10-9.12 come from the dashboard section below.

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
# 9.7: HTTPS can only be switched from a terminal, never through the settings socket.
before = read("web.tls")
r = apply({"target": "config-set", "table": "web", "key": "tls", "value": "off"})
check("9.7", not r.get("ok") and read("web.tls") == before,
      "web.tls is refused through the settings channel (CLI only)", "error=%s" % r.get("error"))
# Plugin declarations, read statically (nothing imported or run).
plugs = ctl.static_plugins()
def decl(name):
    found = plugs.get(name)
    return (found[0] or {}) if found else {}
def caps(name): return set(decl(name).get("capabilities") or ())
def opts(name): return decl(name).get("options") or {}
def state(name): return "enabled" if read("plugins.%s.enabled" % name) is True else "disabled"
# 2.4: library and helper files are never plugins, and no plugin is called lib.
check("2.4", not any(n.startswith("_") for n in plugs) and "lib" not in plugs,
      "no _lib_/_<plugin>_ file or plugin named lib is listed as a plugin",
      "%d plugins listed" % len(plugs))
# 3.1: choices where a plugin declares a fixed set; none on eink (stored values stay valid).
c213 = (opts("eink-ws213").get("variant") or {}).get("choices")
c27v = (opts("eink-ws27").get("variant") or {}).get("choices")
c27r = (opts("eink-ws27").get("rotation") or {}).get("choices")
check("3.1", c213 == ["v4", "v3", "v2"] and c27v == ["auto", "v2", "v1"]
      and c27r == [0, 90, 180, 270] and "choices" not in (opts("eink").get("variant") or {}),
      "eink-ws213 and eink-ws27 declare their choices; eink none",
      "ws213 %s, ws27 %s" % (state("eink-ws213"), state("eink-ws27")))
# 3.2: a value outside the choices is refused through the settings channel and
# the (throwaway) config is left as it was.
table_before = read("plugins.eink-ws213")
r = apply({"plugin": "eink-ws213", "set": {"variant": "V4"}})
check("3.2", not r.get("ok") and read("plugins.eink-ws213") == table_before
      and "one of" in str(r.get("error", "")),
      "a variant outside the choices is refused through the settings channel",
      "error=%s" % str(r.get("error", ""))[:80])
# 4.1: one plugin per service, each with only its own secret options.
def secrets(name):
    return {k for k, v in opts(name).items() if (v or {}).get("secret")}
check("4.1", "network" in caps("ntfy") and "network" in caps("telegram")
      and secrets("ntfy") == {"topic", "token"} and secrets("telegram") == {"bot_token", "chat_id"},
      "ntfy and telegram still declare network and only their own secrets",
      "ntfy %s, telegram %s" % (state("ntfy"), state("telegram")))
# 6.1: the HAT key options, on eink-ws27 and the deprecated eink alike.
def keyopts(name):
    o = opts(name)
    return ((o.get("buttons") or {}).get("default") is True
            and (o.get("pins") or {}).get("default") == "5,6,13,19"
            and (o.get("hold_seconds") or {}).get("default") == 3
            and (o.get("allow_capture") or {}).get("default") is True
            and (o.get("allow_power") or {}).get("default") is True
            and "network" not in caps(name))
check("6.1", keyopts("eink-ws27") and keyopts("eink"),
      "eink-ws27 and eink declare the HAT key options, local only",
      "buttons %s" % ("off" if read("plugins.eink-ws27.buttons") is False
                      or read("plugins.eink.buttons") is False else "on"))
# 7.1: every shipped plugin is there and readable.
expected = {"memtemp", "session-stats", "gps", "wpa-sec", "wigle", "backup", "ups-hat", "eink",
            "eink-ws213", "eink-ws27", "updates", "remote-train", "downloads", "map", "ntfy",
            "telegram"}
invalid = sorted(n for n, v in plugs.items() if v[0] is None)
missing = sorted(expected - set(plugs))
check("7.1", not invalid and not missing, "all sixteen shipped plugins present and readable",
      "missing=%s invalid=%s" % (missing, invalid))
# 7.3: eink says it is deprecated and slated for removal; eink-ws27 doesn't.
d_eink = str(decl("eink").get("description") or "")
d_27 = str(decl("eink-ws27").get("description") or "")
check("7.3", d_eink.startswith("Deprecated: replaced by eink-ws27") and "reflash" in d_eink
      and not d_27.startswith("Deprecated"),
      "eink's description marks it deprecated; eink-ws27's doesn't",
      "eink %s, eink-ws27 %s" % (state("eink"), state("eink-ws27")))
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
# 9.5: every response carries nosniff, the sign-in refusal included (no password needed).
try:
    hdrs = urllib.request.urlopen("http://127.0.0.1:%d/status.json" % port, timeout=15).headers
except urllib.error.HTTPError as exc:
    hdrs = exc.headers
except Exception as exc:
    hdrs = None
    chk("9.5", "INFO", "responses carry X-Content-Type-Options: nosniff", "no answer: %s" % type(exc).__name__)
if hdrs is not None:
    chk("9.5", "PASS" if hdrs.get("X-Content-Type-Options") == "nosniff" else "FAIL",
        "responses carry X-Content-Type-Options: nosniff", "")
# The checks below need the dashboard's answer; without it each says why.
DASH = [("5.1", "each enabled e-ink plugin is loaded, with its Motion card"),
        ("7.4", "an enabled eink shows its deprecation on the Plugins page"),
        ("9.10", "the running daemon offers the row buttons"),
        ("9.11", "the map page: background, recovered colours, drag and zoom"),
        ("9.12", "Captured networks has its controls at the top")]
def skip_all(why, basic=True):
    if basic:
        chk("9.4", "SKIP", "loopback /status.json with Basic credentials", why)
    for cid, what in DASH:
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
    chk("9.4", "FAIL" if mode in ("basic", "system") else "INFO",
        "loopback /status.json with Basic credentials", type(exc).__name__)
    skip_all("no /status.json", basic=False)
    sys.exit(0)
if mode in ("basic", "system"):
    chk("9.4", "PASS", "loopback /status.json with Basic credentials", "plain HTTP on 127.0.0.1")
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
# 5.1: each enabled e-ink plugin loaded and showing its Motion card.
eplugs = [n for n in ("eink-ws27", "eink", "eink-ws213")
          if (plugins.get(n) or {}).get("state") not in (None, "disabled")]
if not eplugs:
    chk("5.1", "SKIP", DASH[0][1], "no e-ink plugin enabled")
else:
    bad = [n for n in eplugs if plugins[n].get("state") != "loaded" or "mobility" not in widget(n)]
    chk("5.1", "FAIL" if bad else "PASS", DASH[0][1],
        ("not loaded or no Motion card: %s" % ", ".join(bad)) if bad else
        ", ".join("%s motion=%s" % (n, widget(n).get("mobility")) for n in eplugs))
# 7.4: the deprecated eink says so on its health note while it is enabled.
e = plugins.get("eink") or {}
if e.get("state") in (None, "disabled"):
    chk("7.4", "SKIP", DASH[1][1], "eink not enabled")
else:
    note = str(e.get("note") or "")
    chk("7.4", "PASS" if note.startswith("deprecated: replaced by eink-ws27") else "FAIL",
        DASH[1][1], "note starts %r" % note[:40])
# 9.10: an enabled, loaded downloads/map/ntfy/telegram offers its main-page button.
offered = {p.get("name"): [a.get("label") for a in (p.get("actions") or []) if isinstance(a, dict)]
           for p in s.get("plugins", []) if p.get("state") == "loaded"}
want = {"downloads": "Download", "map": "Map", "ntfy": "Status", "telegram": "Status"}
on = sorted(n for n in want if n in offered)
if not on:
    chk("9.10", "SKIP", DASH[2][1], "none of downloads, map, ntfy, telegram loaded")
else:
    bad = [n for n in on if want[n] not in offered[n]]
    chk("9.10", "FAIL" if bad else "PASS", DASH[2][1],
        ("missing: %s" % ", ".join(bad)) if bad else "checked: %s" % ", ".join(on))
# 9.11 and 9.12: pages read over loopback with the same credentials. Only
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
    chk("9.11", "SKIP", DASH[3][1], "map not loaded")
else:
    code, hdrs, body = page("/plugins/map/")
    policy = (hdrs or {}).get("Referrer-Policy") if hdrs else None
    if code != 200:
        chk("9.11", "FAIL", DASH[3][1], "HTTP %s" % code)
    elif "No captured network matching" in body:
        chk("9.11", "SKIP", DASH[3][1], "no located networks")
    else:
        background = ("OpenStreetMap</a> contributors" not in body
                      or ('referrerpolicy="origin"' in body and policy == "no-referrer"))
        legend = "Green with a" in body and "not recovered" in body
        gestures = (body.count('class="kmap"') == 2 and body.count('class="kmap-layer"') == 2
                    and body.count("data-scale=") == 2 and "bboxFor" in body
                    and "touch-action:none" in body)
        ok = background and legend and gestures
        chk("9.11", "PASS" if ok else "FAIL", DASH[3][1],
            "background %s, legend %s, gestures %s" % (background, legend, gestures))
code, hdrs, body = page("/networks")
if code != 200:
    chk("9.12", "FAIL", DASH[4][1], "HTTP %s" % code)
else:
    top = body.split("<h1>", 1)[0]
    # With the web login off the import control is hidden on purpose.
    ok = ('class="nethead"' in top and 'class="countbar"' in body
          and ("Import results" in top or mode == "none"))
    chk("9.12", "PASS" if ok else "FAIL", DASH[4][1],
        ("Import results above the heading" if mode != "none" else "web login off: no import")
        if ok else "markers missing")
for name in ("wpa-sec", "wigle", "backup", "remote-train", "updates", "ntfy", "telegram",
             "eink-ws27", "eink", "eink-ws213"):
    if name in w:
        vals = ["%s=%s" % (k, v) for k, v in widget(name).items()]
        print("widget %-13s %s" % (name, ", ".join(vals)))
PY
} >>"$raw" 2>&1
web_pw=""

cat >>"$raw" <<'EOF'

===== by hand (the script can't see these) =====
1.6  with the updates plugin's beta OFF this beta is NOT offered; with beta ON it
     is offered and applies in place
3.x  variant and rotation show as dropdowns on the Plugins page for eink-ws27
     and eink-ws213 (eink keeps a text field); kestrel plugins enable eink-ws27
     offers them as a menu (whiptail), and a value from it is saved
4.x  Send test from each enabled alert plugin arrives; alerts queued before the
     update are sent; the ntfy topic is the one you subscribed to; a handshake
     digest (counts only), on battery, capture stopped
5.x  the e-ink face looks as before on the HAT and in View (eink-ws27, or eink);
     with GPS, walking shows the person and driving the car within about half a
     minute, and the Motion card agrees; eink-ws213 enabled with no 2.13" HAT:
     View shows the 2.13" layout and the card says not detected
6.x  each key's prompt on the panel and the "Last key press" card; a long
     KEY3/KEY4 then a second press reboots / shuts down; waiting 5 s cancels;
     NEW: pressing another key cancels a pending "Press KEY3 again"; KEY1 opens
     and closes the info screen; a long KEY2 stops capture, starts it after a
     second press, and without the acknowledgment points to the dashboard;
     "Starting/Stopping capture..." clears once it has
7.x  with eink enabled before the update, the face still works and the Plugins
     page shows the deprecation; the switch in eink-setup.md (enable eink-ws27
     with the same settings, disable eink) works; eink-ws27 shows the same face
     and keys on the same HAT
8.x  from a computer: pull-eink-diagnostics.sh --plugin eink-ws27 (and
     --plugin eink-ws213) makes a report that names that plugin and its helpers
9.x  first visit over HTTPS still shows the warning and the fingerprint matches;
     sign in / sign out
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
