#!/usr/bin/env bash
#
# Pull a PII-free web-dashboard diagnostics bundle off a field unit, so a
# dashboard that won't come up (while SSH still works) can be triaged
# off-device. The dashboard is served by the kestrel-daemon itself (it binds
# [web] port, management link only), NOT a separate web unit, so this reports
# the daemon's web subsystem, not a standalone server.
#
# Runs on your WORKSTATION (not the device) and reads the device READ-ONLY over
# SSH. It ships in the image under /usr/local/share/kestrel/scripts/ so you can
# copy it off a unit, but you run it from your own machine. It collects the
# things that decide why the page is dark:
#   - the image version (/etc/kestrel/version), board model, OS and kernel, and
#     `kestrel update status` (what the device thinks it last updated to);
#   - the update run-state: the engine's status.json (status + detail), the
#     in-progress marker, kestrel-update-recover.service and the apply journal
#     -- so a FAILED in-place update that rolled back to the prior version
#     (the page dark for a few minutes, then back on the OLD version) is told
#     apart from a dashboard that simply never started;
#   - kestrel-daemon active/enabled state, `systemctl status` tail and the
#     daemon journal filtered to the web subsystem (the "web: serving on ...",
#     "web: cannot bind ...", "web subsystem unavailable ..." and
#     "Address already in use" lines that say why the server didn't come up);
#   - the login-helper units (kestrel-web-auth.socket / @.service) state and
#     journal -- used only when [web] auth = system, not the server itself;
#   - the safe [web] scalars via config-get (enabled/bind/port/auth/
#     refresh_seconds/webhook_timeout_seconds) -- never username/password/token;
#   - whether the web port is actually bound (ss / netstat), and an on-device
#     HTTP probe of http://127.0.0.1:<port>/ (its status code + time) to tell
#     "daemon not serving" apart from a link/network problem;
#   - a pi-venv import check of flask and the web module, to catch a
#     post-update import/dependency break (the usual cause of a dark page after
#     an app-layer update);
#   - disk and inode usage (a full / or out-of-inodes rootfs breaks startup).
#
# Approach-A redaction: the whole report is passed through a MAC/IPv4/IPv6
# masker ON THE DEVICE before it leaves, so no hardware or peer address (nor the
# bound IPs from `ss`/the probe) is in the bundle. It never dumps config.toml
# wholesale and never reads the web username, password or token, so the bundle
# is safe to share; give it a glance before sharing all the same.
#
# Usage (from a repo checkout, or a copy pulled off a unit):
#   pull-web-diagnostics.sh user@host [ssh-port]
#
# Env overrides:
#   SSH_KEY   identity file to use (default: your ssh default)
#
# Then send the printed .tar.gz back for review.

set -eu

if [ "$#" -lt 1 ] || [ "$#" -gt 2 ]; then
  echo "usage: $0 user@host [ssh-port]" >&2
  exit 2
fi

target=$1
port=${2:-22}

ssh_cmd="ssh -p $port -o BatchMode=yes -o ConnectTimeout=20"
if [ -n "${SSH_KEY:-}" ]; then
  ssh_cmd="$ssh_cmd -i $SSH_KEY"
fi

stamp=$(date -u +%Y%m%dT%H%M%SZ)
out="web-diagnostics-$stamp"
mkdir -p "$out"

echo "Collecting web diagnostics from $target ..."

# One remote shell gathers everything into a single labelled text stream and
# pipes it through the redactor, so the whole bundle is one SSH round-trip, no
# missing tool is ever fatal, and nothing leaves the device unmasked. The
# heredoc is quoted so it runs verbatim on the device (no local expansion).
# shellcheck disable=SC2086
$ssh_cmd "$target" 'sh -s' > "$out/web-report.txt" 2>&1 <<'REMOTE' || \
  echo "  note: the SSH session returned an error; the partial report was still saved." >&2
set -u
sec() { printf "\n===== %s =====\n" "$1"; }
cfg() { /usr/local/lib/kestrel/config-get "$1" "${2:-}" 2>/dev/null || echo "?"; }

# Mask hardware (MAC) and peer (IPv4/IPv6) addresses. MAC first, then IPv6
# (compressed :: form and >=4-group form, so clock times like 06:15:33 with
# only two colons are left alone), then IPv4.
redact() {
  sed -E \
    -e 's/\b([0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}\b/<mac>/g' \
    -e 's/\b([0-9A-Fa-f]{1,4}:){1,}:([0-9A-Fa-f]{1,4}:)*[0-9A-Fa-f]{1,4}\b/<ipv6>/g' \
    -e 's/\b([0-9A-Fa-f]{1,4}:){4,}[0-9A-Fa-f]{1,4}\b/<ipv6>/g' \
    -e 's/\b([0-9]{1,3}\.){3}[0-9]{1,3}\b/<ipv4>/g'
}

report() {
  # The configured web port drives the bind check and the local probe below.
  web_port=$(cfg web.port 8080)
  case "$web_port" in *[!0-9]*|'') web_port=8080 ;; esac

  sec "image version"
  cat /etc/kestrel/version 2>/dev/null || echo "(no /etc/kestrel/version)"

  sec "update status (what the device last updated to)"
  kestrel update status 2>&1 || echo "(kestrel update status failed)"

  sec "update run-state (did an in-place update fail and roll back?)"
  # The engine writes status.json world-readable: status=ok|error|running and a
  # detail line. status=error with a detail like "installed <tag> but restart
  # failed ..." or "... rolled back", while the device is back on the OLD
  # version, is a failed update that reverted (the page is dark for the minutes
  # the install+rollback takes, then returns on the previous version).
  cat /var/lib/kestrel/update-run/status.json 2>/dev/null; echo
  [ -f /var/lib/kestrel/update-run/status.json ] \
    || echo "(no update-run/status.json -- no in-place update has run here, or it is unreadable)"
  echo "  --- in-progress marker (present = an update was cut short) ---"
  ls -l /var/lib/kestrel/update-run/in-progress.json 2>&1 \
    || echo "(no in-progress marker -- no update is mid-flight)"
  echo "  --- other update-run entries (backups kept for rollback) ---"
  ls -1 /var/lib/kestrel/update-run/ 2>&1 | grep -vE '^(status\.json|lock)$' \
    || echo "(update-run not listable without privileges)"

  sec "kestrel-update-recover.service (boot-time rollback of a cut-short update)"
  printf '  active=%s enabled=%s\n' \
    "$(systemctl is-active kestrel-update-recover.service 2>/dev/null || echo ?)" \
    "$(systemctl is-enabled kestrel-update-recover.service 2>/dev/null || echo ?)"
  journalctl -u kestrel-update-recover.service --no-pager -n 80 2>&1 \
    | tail -n 80 || echo "(no recover journal, or needs a privileged account)"

  sec "update apply journal (kestrel-plugins-apply@, where the engine logs)"
  # A dashboard- or CLI-triggered 'update apply' runs the engine under this
  # unit; its restart-failure / rollback lines and any traceback land here.
  journalctl -u 'kestrel-plugins-apply@*' --no-pager -n 300 2>&1 \
    | grep -iE 'update|rollback|roll back|restart|version|error|exception|traceback' \
    | tail -n 150 \
    || echo "(no matching apply-journal lines, or needs a privileged account)"

  sec "board model / OS / kernel"
  tr -d '\0' < /proc/device-tree/model 2>/dev/null; echo
  grep -E '^(PRETTY_NAME|VERSION)=' /etc/os-release 2>/dev/null || true
  echo "kernel: $(uname -a)"

  sec "[web] config (safe scalars only -- no username/password/token)"
  printf '  web.enabled                 = %s\n' "$(cfg web.enabled true)"
  printf '  web.bind                    = %s\n' "$(cfg web.bind management)"
  printf '  web.port                    = %s\n' "$web_port"
  printf '  web.auth                    = %s\n' "$(cfg web.auth system)"
  printf '  web.refresh_seconds         = %s\n' "$(cfg web.refresh_seconds 0)"
  printf '  web.webhook_timeout_seconds = %s\n' "$(cfg web.webhook_timeout_seconds 30)"
  printf '  web.probe_url set           = %s\n' \
    "$([ -n "$(cfg web.probe_url '')" ] && echo yes || echo no)"

  sec "kestrel-daemon: state (the dashboard IS this service)"
  printf '  active=%s enabled=%s\n' \
    "$(systemctl is-active kestrel-daemon 2>/dev/null || echo ?)" \
    "$(systemctl is-enabled kestrel-daemon 2>/dev/null || echo ?)"
  systemctl status kestrel-daemon --no-pager -n 20 2>&1 | tail -n 22 || true

  sec "kestrel-daemon journal: web subsystem lines (last 4000 scanned)"
  # The web server logs "web: serving on <host>:<port>" on success and
  # "web: cannot bind <host>:<port> (<exc>)" / "web subsystem unavailable: ..."
  # / "web subsystem bound no interfaces" on failure; a crashed start shows a
  # traceback. "Address already in use" means a stale listener still holds the
  # port.
  journalctl -u kestrel-daemon --no-pager -n 4000 2>&1 \
    | grep -iE 'web|bind|port|address already in use|traceback|error|exception' \
    | tail -n 300 \
    || echo "(no matching journal lines, or the journal needs a privileged account -- rerun as a user that can read it)"

  sec "login helper (kestrel-web-auth, only used when [web] auth = system)"
  printf '  socket active=%s enabled=%s\n' \
    "$(systemctl is-active kestrel-web-auth.socket 2>/dev/null || echo ?)" \
    "$(systemctl is-enabled kestrel-web-auth.socket 2>/dev/null || echo ?)"
  systemctl status kestrel-web-auth.socket --no-pager -n 8 2>&1 | tail -n 10 || true
  echo "  --- recent kestrel-web-auth@ instance journal (login attempts) ---"
  journalctl -u 'kestrel-web-auth@*' --no-pager -n 60 2>&1 \
    | tail -n 60 || echo "(none, or needs a privileged account)"

  sec "is the web port bound? (listening sockets on :$web_port)"
  if command -v ss >/dev/null 2>&1; then
    ss -ltnH 2>/dev/null | grep -E "[:.]$web_port([^0-9]|$)" \
      || echo "(nothing is listening on :$web_port -- the daemon's web server is not up)"
  elif command -v netstat >/dev/null 2>&1; then
    netstat -ltn 2>/dev/null | grep -E "[:.]$web_port([^0-9]|$)" \
      || echo "(nothing is listening on :$web_port -- the daemon's web server is not up)"
  else
    echo "(neither ss nor netstat available)"
  fi

  sec "on-device HTTP probe: http://127.0.0.1:$web_port/"
  # Separates "daemon not serving" (connection refused/timeout) from a
  # link/network problem (which this loopback probe bypasses). A 401 is a
  # healthy answer when [web] auth is on -- the server IS up, it just wants a
  # login. curl if present, else a stdlib-python fallback (always available).
  if command -v curl >/dev/null 2>&1; then
    curl -sS -o /dev/null -m 10 \
      -w 'HTTP %{http_code} in %{time_total}s\n' \
      "http://127.0.0.1:$web_port/" 2>&1 \
      || echo "(no HTTP response -- the daemon is not serving on this port)"
  else
    /usr/bin/python3 -I - "$web_port" <<'PY' 2>&1 || true
import sys, time, urllib.request, urllib.error
port = sys.argv[1]
url = "http://127.0.0.1:%s/" % port
t = time.time()
try:
    with urllib.request.urlopen(url, timeout=10) as r:
        print("HTTP %s in %.3fs" % (r.status, time.time() - t))
except urllib.error.HTTPError as e:
    # 401/403 etc. still mean the server answered (it is up).
    print("HTTP %s in %.3fs (server answered)" % (e.code, time.time() - t))
except Exception as e:
    print("no HTTP response (%r) -- the daemon is not serving on this port" % (e,))
PY
  fi

  sec "pi venv imports (flask / web module / the daemon's web symbols)"
  # A dark page right after an app-layer update is usually an import break here:
  # the daemon logs "web subsystem unavailable: <exc>" and never starts serving.
  VENV=/home/pi/.venv/bin/python
  if [ -x "$VENV" ]; then
    "$VENV" - <<'PY' 2>&1
for name in ("flask", "werkzeug", "jinja2"):
    try:
        m = __import__(name)
        print("%s: OK (%s)" % (name, getattr(m, "__version__", "no __version__")))
    except Exception as e:
        print("%s: FAIL (%r)" % (name, e))
for mod in ("kestrel_pi", "kestrel_pi.applyclient", "kestrel_pi.web"):
    try:
        __import__(mod)
        print("%s: import OK" % mod)
    except Exception as e:
        print("%s: import FAIL (%r)" % (mod, e))
# The exact trio the daemon lazily imports to start the web server.
try:
    from kestrel_pi.web import WebServer, create_app, resolve_bind_hosts  # noqa: F401
    print("kestrel_pi.web {WebServer,create_app,resolve_bind_hosts}: OK")
except Exception as e:
    print("kestrel_pi.web symbols: FAIL (%r)" % (e,))
PY
  else
    echo "(no pi venv at $VENV)"
  fi

  sec "disk usage"
  df -h 2>&1 | grep -vE '^(tmpfs|devtmpfs|overlay|udev)' || true

  sec "inodes (a rootfs out of inodes fails startup even with free space)"
  df -i 2>&1 | grep -vE '^(tmpfs|devtmpfs|overlay|udev)' || true

  echo "(done)"
}

report | redact
REMOTE

# A small manifest naming what was collected and from where.
{
  echo "collected: $stamp"
  # The device's address or name stays out of a bundle meant for sharing.
  case "$target" in *@*) shown="${target%%@*}@<host>" ;; *) shown="<host>" ;; esac
  echo "target:    $shown (port $port)"
  echo "contents:"
  echo "  web-report.txt   one labelled, MAC/IP-redacted read-only report:"
  echo "                   version, update status, update run-state + rollback"
  echo "                   (status.json / recover service / apply journal),"
  echo "                   board/OS/kernel, safe [web] config, kestrel-daemon"
  echo "                   state + web-subsystem journal, login-helper units,"
  echo "                   port-bound check, on-device HTTP probe, pi-venv web"
  echo "                   imports, disk + inode usage."
} > "$out/MANIFEST.txt"

bundle="$out.tar.gz"
tar -czf "$bundle" "$out"
rm -rf "$out"

echo "Done. Share this bundle:"
echo "  $(pwd)/$bundle"
