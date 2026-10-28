"""The embedded web subsystem: a status page, plugin controls and plugin
routes, bound to the management link only.

The daemon serves a built-in dashboard (the device's live state - plugin host
version, internet, capture - plus any plugin readout widgets), a separate
plugins page (each plugin's state, health and controls) reached by a link from
the dashboard, and dispatches ``/plugins/<name>/...`` to each plugin's
``on_webhook``. It binds to the management interfaces (``bnep0``, ``wlan0``) and
loopback, by interface address rather than a wildcard, so the capture dongle is
never served. Access control is a config toggle: ``system``
(the default) requires the device login account, so the web login is the same
credential used to SSH in and the two never drift; ``basic``/``token`` use a
web-only credential; ``none`` leaves the page open and read-only.

With web auth on, ``/manage/<name>`` also lets the operator enable or disable a
plugin and edit its options (secrets masked, never echoed back). The daemon
runs as pi and can't write the root-owned config.toml, so it hands each change
to the root apply helper on a local socket (``plugins-ctl apply`` behind
``kestrel-plugins-apply.socket``), which validates it against the
plugin's declared options, writes only that plugin's table, and restarts the
daemon. With auth off the page stays read-only.

Flask is imported lazily so the rest of the package imports and unit-tests
without it installed.
"""

from __future__ import annotations

import hmac
import json
import logging
import os
import re
import secrets
import socket
import subprocess
import threading
import time
from urllib.parse import quote, urlencode

from . import config_catalog
from .applyclient import APPLY_SOCKET, apply_change
from .connectivity import MANAGEMENT_INTERFACES
from .plugins import PluginPage, PluginRedirect

log = logging.getLogger(__name__)

# The root side of the system-login check (kestrel-web-auth.socket).
AUTH_SOCKET = "/run/kestrel-web-auth.sock"
# The authorized-use acknowledgment the CLI records (capture, network plugins).
ACK_FILE = "/etc/kestrel/capture-ack"
NETWORK_ACK_TEXT = (
    "This plugin transmits data off the device (captured handshakes, or "
    "observed networks and their locations). Enable it only for data you are "
    "authorized to collect and share. Unauthorized use may be illegal."
)
CAPTURE_ACK_TEXT = (
    "Capture runs AngryOxide, which transmits deauthentication frames to force "
    "WPA handshakes. Enable it only against networks you own or are explicitly "
    "authorized to test. Unauthorized use may be illegal."
)
CONTROLS_OFF = (
    "Plugin controls are off: web auth is \"none\", so the page is read-only. "
    "The controls use the device login; turn them on with: "
    "sudo kestrel web auth system"
)
# Shared state the `updates` plugin writes (installed vs latest release, whether
# an in-place update is offerable). The dashboard reads it to decide whether to
# show the upgrade button; the root helper re-verifies before applying.
UPDATE_STATE_FILE = "/var/lib/kestrel/update/state.json"
# The update engine's own run status (root-owned, world-readable). Separate from
# the plugin's offer state above: this records whether the last attempt failed
# and why, so the dashboard can show the cause instead of silently re-offering.
UPDATE_RUN_STATE_FILE = "/var/lib/kestrel/update-run/status.json"
# The learning agent's enums for the dashboard dropdowns. Kept in step with
# kestrel_pi.agent.policies (ALGORITHMS / POLICIES); the root apply helper
# re-validates against the same lists, so this is only what the <select> offers.
AGENT_ALGORITHMS = ("RecurrentPPO", "PPO", "DQN", "QR-DQN")
AGENT_POLICIES = ("baseline", "learned")
# The agent readout card's widget-group key (kestrel_pi.agent.readout.CARD_NAME),
# hardcoded like the enums above so web.py does not import the agent package. The
# card is shown on the /agent page, not the dashboard widget grid.
AGENT_CARD_NAME = "Learning agent"
# One-line descriptions for the policy radio group on the /agent page.
_AGENT_POLICY_DESC = {
    "baseline": "The scripted channel sweep. No trained model is used.",
    "learned": "A trained model chooses the scan posture (needs an imported model).",
}
# The agent's runtime dir and per-algorithm model naming, mirrored here (like
# the enums above) so the /agent page can tell whether a trained model already
# exists for an algorithm without importing the agent package: switching to an
# algorithm with no model starts a training run, so the page confirms that
# first. Kept in step with kestrel_pi.agent.model_slug / model_name_for and
# the agent's AGENT_DIR.
AGENT_DIR = "/var/lib/kestrel/agent"


def _agent_model_slug(algorithm):
    text = (algorithm or "").strip().lower()
    slug = "".join(ch if (ch.isalnum() or ch == "-") else "-" for ch in text)
    return slug.strip("-") or "default"


def _agent_has_model(algorithm, agent_dir=AGENT_DIR):
    """Whether a trained model already exists for ``algorithm`` (its
    per-algorithm file, or a legacy ``model.zip`` whose sidecar matches), so the
    page can skip the train-confirm when switching to an already-trained one."""
    if not algorithm:
        return False
    per_algo = os.path.join(agent_dir, "model-" + _agent_model_slug(algorithm) + ".zip")
    if os.path.exists(per_algo):
        return True
    if os.path.exists(os.path.join(agent_dir, "model.zip")):
        try:
            with open(os.path.join(agent_dir, "model.meta.json"), "r",
                      encoding="utf-8") as handle:
                meta = json.load(handle)
        except (OSError, ValueError):
            meta = {}
        return meta.get("algorithm") in (None, algorithm)
    return False

# A successful system login is cached for a short time so basic auth (which the
# browser resends on every request) doesn't verify against the root helper on
# each hit. The cache stores a per-run keyed HMAC of the password, never the
# password itself, and is dropped when the daemon restarts.
_AUTH_TTL = 300.0
_AUTH_HMAC_KEY = secrets.token_bytes(32)
_auth_cache = {}
_auth_cache_lock = threading.Lock()

# Single-flight coalescing of concurrent verifications of the SAME credential:
# right after a daemon restart the success cache is empty, so a burst of
# auto-refreshing tabs/devices would each open a root-helper socket at once and
# saturate it. The first request verifies; the rest wait for its result. Keyed
# by (username, HMAC tag) - never the plaintext password, like the cache above.
_auth_inflight = {}
_auth_inflight_lock = threading.Lock()
# Just over the root helper's own socket timeout (10s), so a follower waits out
# the leader's verification rather than giving up early and failing closed.
_AUTH_WAIT_SECONDS = 12.0


class _AuthFlight:
    __slots__ = ("done", "ok")

    def __init__(self):
        self.done = threading.Event()
        self.ok = False


# Interface addresses, cached briefly. The dashboard and every control POST
# list the management addresses, and the connectivity loop re-resolves the bind
# set every poll; each used to fork `ip` per interface. Concurrent misses for an
# interface wait on one lookup (the per-interface lock) instead of each forking.
_ADDR_TTL = 5.0
_addr_cache = {}                 # iface -> (ip or None, expires)
_addr_locks = {}                 # iface -> Lock held while probing it
_addr_cache_lock = threading.Lock()


def _probe_ipv4(iface):
    """``(ip_or_None, ok)``: the first IPv4 address on ``iface``; ``ok`` is
    False when ``ip`` itself failed (couldn't run or timed out), which says
    nothing about whether the interface has an address."""
    try:
        out = subprocess.run(
            ["ip", "-4", "-o", "addr", "show", "dev", iface],
            capture_output=True, text=True, timeout=5, check=False,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return None, False
    for token in out.split():
        if "/" in token and token.split("/")[0].count(".") == 3:
            return token.split("/")[0], True
    return None, True


def iface_ipv4(iface):
    """The first IPv4 address on ``iface`` (no CIDR), or None."""
    with _addr_cache_lock:
        lock = _addr_locks.setdefault(iface, threading.Lock())
    with lock:
        with _addr_cache_lock:
            cached = _addr_cache.get(iface)
        if cached and cached[1] > time.monotonic():
            return cached[0]
        ip, ok = _probe_ipv4(iface)
        if ok:
            # A failed lookup is not cached, so the next caller retries it.
            with _addr_cache_lock:
                _addr_cache[iface] = (ip, time.monotonic() + _ADDR_TTL)
        return ip


def resolve_bind_hosts(bind_setting, interfaces=MANAGEMENT_INTERFACES):
    """Resolve ``[web] bind`` to a list of concrete bind addresses.

    ``management`` (the default) is loopback plus whichever management
    interfaces currently have an IPv4 address - never the capture dongle. An
    explicit IP is used as-is. Loopback is always included so on-device tools
    reach the page."""
    hosts = ["127.0.0.1"]
    if bind_setting in (None, "", "management", "mgmt"):
        for iface in interfaces:
            ip = iface_ipv4(iface)
            if ip and ip not in hosts:
                hosts.append(ip)
    elif bind_setting in ("loopback", "localhost"):
        pass
    else:
        if bind_setting not in hosts:
            hosts.append(bind_setting)
    return hosts


def management_addresses(interfaces=MANAGEMENT_INTERFACES):
    """``[(iface, ipv4-or-None), ...]`` for each management interface, for the
    dashboard's network-address disclosure. Loopback and the capture dongle are
    never listed; an interface with no address reads as not connected."""
    return [(iface, iface_ipv4(iface)) for iface in interfaces]


def refresh_seconds(web_cfg):
    """``[web] refresh_seconds`` as a non-negative int (0 = no auto-refresh).
    A bad or missing value disables the refresh rather than erroring."""
    try:
        n = int((web_cfg or {}).get("refresh_seconds", 0))
    except (TypeError, ValueError):
        return 0
    return n if n >= 1 else 0


_WEBHOOK_TIMEOUT_DEFAULT = 30


def webhook_timeout_seconds(web_cfg):
    """``[web] webhook_timeout_seconds`` as a non-negative int. A missing value
    uses the default; an explicit 0 (or a negative/bad value) disables the
    on_webhook timeout rather than erroring."""
    raw = (web_cfg or {}).get("webhook_timeout_seconds", _WEBHOOK_TIMEOUT_DEFAULT)
    try:
        n = int(raw)
    except (TypeError, ValueError):
        return _WEBHOOK_TIMEOUT_DEFAULT
    return n if n >= 1 else 0


def _run_with_timeout(func, timeout, on_overrun=None):
    """Run ``func()`` in a daemon worker thread, waiting up to ``timeout``
    seconds. Returns ``(True, result)`` if it finished (re-raising any exception
    it raised) or ``(False, None)`` if it overran. Python can't kill the worker,
    so an overrun leaks that one thread rather than pinning the request thread
    forever; it is a per-call thread, never a shared pool, so a hang can't starve
    other requests. ``on_overrun(worker)``, if given, is handed the still-running
    thread so the caller can refuse new calls until it ends."""
    box = {}

    def run():
        try:
            box["result"] = func()
        except Exception as exc:  # noqa: BLE001 - surfaced to the caller
            box["error"] = exc

    worker = threading.Thread(target=run, name="pwn-webhook", daemon=True)
    worker.start()
    worker.join(timeout)
    if worker.is_alive():
        if on_overrun is not None:
            on_overrun(worker)
        return False, None
    if "error" in box:
        raise box["error"]
    return True, box.get("result")


def _same(given, wanted):
    """Constant-time string comparison."""
    return hmac.compare_digest(str(given).encode("utf-8"), str(wanted).encode("utf-8"))


def system_login_user(web_cfg):
    """The account the ``system`` auth mode checks against: ``[web] username``
    if set, else the account the daemon runs as (the device's login user, e.g.
    ``pi``), so the web login is the same credential used to SSH in."""
    name = web_cfg.get("username")
    if name:
        return str(name)
    try:
        import pwd

        return pwd.getpwuid(os.getuid()).pw_name
    except Exception:  # noqa: BLE001 - fall back to the stock account name
        return "pi"


def _verify_system_login(username, password):
    """Verify a device-account login via the root helper, with a short success
    cache so basic auth isn't re-verified on every request, and single-flight
    coalescing so a burst of identical logins (several auto-refreshing tabs
    hitting the empty cache right after a restart) makes one verification, not
    one root-socket connection each."""
    if not username or not password:
        return False
    tag = hmac.new(_AUTH_HMAC_KEY, password.encode("utf-8"), "sha256").digest()
    now = time.monotonic()
    with _auth_cache_lock:
        cached = _auth_cache.get(username)
        if cached and cached[1] > now and hmac.compare_digest(cached[0], tag):
            return True
    key = (username, tag)
    with _auth_inflight_lock:
        flight = _auth_inflight.get(key)
        leader = flight is None
        if leader:
            flight = _AuthFlight()
            _auth_inflight[key] = flight
    if not leader:
        # Another request is already verifying this exact credential; wait for
        # its result instead of opening a second root-helper socket.
        return flight.ok if flight.done.wait(_AUTH_WAIT_SECONDS) else False
    try:
        ok = bool(_auth_check(username, password).get("ok"))
        flight.ok = ok
        if ok:
            with _auth_cache_lock:
                _auth_cache[username] = (tag, now + _AUTH_TTL)
        return ok
    finally:
        # Release waiters and clear the slot even on an unexpected error, so a
        # failed leader never leaves followers blocked.
        with _auth_inflight_lock:
            _auth_inflight.pop(key, None)
        flight.done.set()


def _check_auth(web_cfg, request):
    """Fail closed: an empty credential or an unknown mode lets nobody in."""
    mode = (web_cfg.get("auth") or "none").lower()
    if mode == "none":
        return True
    if mode == "token":
        want = str(web_cfg.get("token") or "")
        got = request.headers.get("Authorization", "")
        if got.startswith("Bearer "):
            got = got[len("Bearer ") :]
        else:
            got = request.args.get("token", "")
        return bool(want) and _same(got, want)
    if mode == "basic":
        user = str(web_cfg.get("username") or "")
        password = str(web_cfg.get("password") or "")
        auth = request.authorization
        return bool(
            user and password and auth
            and _same(auth.username or "", user)
            and _same(auth.password or "", password)
        )
    if mode == "system":
        auth = request.authorization
        if not auth or not auth.username or not auth.password:
            return False
        if not _same(auth.username, system_login_user(web_cfg)):
            return False
        return _verify_system_login(auth.username, auth.password)
    return False


def control_ready(web_cfg):
    """Plugin controls need web auth on, with its credential set. The root
    apply helper checks the same rule before it writes anything."""
    mode = (web_cfg.get("auth") or "none").lower()
    if mode == "system":
        return True
    if mode == "basic":
        return bool(web_cfg.get("username")) and bool(web_cfg.get("password"))
    if mode == "token":
        return bool(web_cfg.get("token"))
    return False


def _auth_check(username, password, socket_path=None, timeout=10):
    """Ask the root helper to verify a device-account login; return its reply."""
    data = (json.dumps({"username": username, "password": password}) + "\n").encode("utf-8")
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.settimeout(timeout)
            sock.connect(socket_path or AUTH_SOCKET)
            sock.sendall(data)
            sock.shutdown(socket.SHUT_WR)
            chunks = []
            while True:
                chunk = sock.recv(4096)
                if not chunk:
                    break
                chunks.append(chunk)
        reply = json.loads(b"".join(chunks).decode("utf-8") or "{}")
    except (OSError, ValueError):
        return {"ok": False}
    return reply if isinstance(reply, dict) else {"ok": False}


def _configured_table(daemon, name):
    return ((daemon.config.get("plugins") or {}).get(name) or {})


def _backup_ready(daemon):
    """True once the backup plugin's SSH key exists (``backup setup`` has run).
    Used by the in-place update flow to decide whether to offer "back up first";
    the backup plugin gates its own row buttons separately via webhook_actions."""
    table = _configured_table(daemon, "backup")
    key = str(table.get("ssh_key") or "/home/pi/.ssh/id_ed25519").strip()
    try:
        return bool(key) and os.path.exists(key)
    except OSError:
        return False


def build_change(daemon, record, form):
    """Turn a submitted form into the change for the apply helper.

    The status page's quick toggle posts ``action=enable|disable``. The manage
    form posts every rendered option; only values that differ from the current
    one (config value, else the declared default) are sent, so saving never
    writes defaults into config.toml. An empty text field is "unchanged":
    secrets are never prefilled. Raises ValueError for a malformed number."""
    name = record.name
    change = {"plugin": name}
    if form.get("ack") == "yes":
        change["ack"] = True
    action = form.get("action")
    if action in ("enable", "disable"):
        change["enabled"] = action == "enable"
        return change

    table = _configured_table(daemon, name)
    schema = getattr(record.cls, "__options__", {}) or {}
    updates = {}
    for opt, spec in schema.items():
        spec = spec or {}
        if spec.get("root_only"):
            continue  # terminal only; the root helper refuses it from here
        type_name = spec.get("type", "str")
        field = "opt-" + opt
        current = table.get(opt, spec.get("default"))
        if type_name == "bool":
            if ("bool-" + opt) not in form:
                continue
            value = form.get(field) == "on"
            current = bool(current)
        elif type_name in ("int", "float"):
            raw = (form.get(field) or "").strip()
            if raw == "":
                continue
            try:
                value = int(raw) if type_name == "int" else float(raw)
            except ValueError:
                raise ValueError("%s must be a number" % opt) from None
        elif type_name == "str":
            value = form.get(field)
            if not value:
                continue
        else:
            continue
        if value == current and type(value) is type(current):
            continue
        updates[opt] = value
    if updates:
        change["set"] = updates
    if "enabled-field" in form:
        wanted = form.get("enabled") == "on"
        if wanted != bool(table.get("enabled", False)):
            change["enabled"] = wanted
    return change


def _refused_table(daemon, record, change):
    """The plugin's table as a refused change would have left it, so the page
    comes back with what the operator entered instead of losing it. Secrets
    are left out: they are never put back into a page."""
    table = dict(_configured_table(daemon, record.name))
    schema = getattr(record.cls, "__options__", {}) or {}
    for opt, value in (change.get("set") or {}).items():
        if not (schema.get(opt) or {}).get("secret"):
            table[opt] = value
    if "enabled" in change:
        table["enabled"] = change["enabled"]
    return table


def create_app(daemon):
    """Build the Flask app for ``daemon`` (a :class:`~kestrel_pi.daemon.Daemon`)."""
    from flask import Flask, Response, jsonify, redirect, request

    app = Flask("kestrel_pi")
    web_cfg = daemon.config.get("web", {}) or {}
    # Per-run CSRF token for the plugin control forms: with basic auth the
    # browser resends the login by itself, so a form posted from some other
    # site must not be able to change settings. A daemon restart rotates it.
    csrf_token = secrets.token_urlsafe(32)
    # Per plugin, the on_webhook worker that overran its timeout and is still
    # running. While it is, new calls to that plugin get a 503 instead of a new
    # thread, so a hung plugin leaks one thread rather than one per request (a
    # self-reloading page against it would otherwise grow them without bound).
    stuck_webhooks = {}
    stuck_lock = threading.Lock()

    def control():
        token = request.args.get("token")
        return {
            "ready": control_ready(web_cfg),
            "csrf": csrf_token,
            # Token auth over the browser rides in the query string; keep it on
            # every link and form so navigation stays signed in.
            "qs": ("?token=" + quote(token, safe="")) if token else "",
            # The raw token, for a GET form that must carry it as a hidden field
            # (the browser rebuilds the query string from inputs on submit).
            "token": token or "",
            "enabled": {
                name: bool(_configured_table(daemon, name).get("enabled", False))
                for name in daemon.loader.records
            },
            "acked": os.path.exists(ACK_FILE),
            # For the in-place update flow's "back up first" option (the backup
            # plugin gates its own row buttons via webhook_actions).
            "backup_ready": _backup_ready(daemon),
        }

    def html(body, status=200):
        return Response(body, status=status, mimetype="text/html")

    @app.before_request
    def _auth_gate():  # noqa: ANN202
        if not _check_auth(web_cfg, request):
            resp = Response("authentication required", 401)
            if (web_cfg.get("auth") or "none").lower() in ("basic", "system"):
                resp.headers["WWW-Authenticate"] = 'Basic realm="Kestrel"'
            return resp
        return None

    @app.get("/status.json")
    def status_json():  # noqa: ANN202
        return jsonify(daemon.status())

    @app.get("/")
    @app.get("/status")
    def status_page():  # noqa: ANN202
        return html(_render_status(daemon.status(), control(),
                                   addresses=management_addresses(),
                                   refresh=refresh_seconds(web_cfg)))

    @app.get("/plugins")
    def plugins_page():  # noqa: ANN202
        # A plugin action that redirects back here carries a one-line banner in
        # ``msg`` (ok) or ``err`` (error); the renderer escapes it.
        err = request.args.get("err")
        msg = err or request.args.get("msg")
        return html(_render_plugins(daemon.status(), control(),
                                    message=msg, error=bool(err)))

    @app.get("/networks")
    def networks_page():  # noqa: ANN202
        # The view (by network / by access point) defaults to match the capture
        # file layout -- per-ap mode opens the by-AP view -- but the operator can
        # flip it freely, because the data supports both regardless of how the
        # store was captured (see _render_networks / captured_access_points).
        cfg = daemon._fresh_config()
        mode = ((cfg.get("capture") or {}).get("output") or {}).get(
            "mode", "per-ssid")
        requested = request.args.get("view")
        if requested in ("ssid", "ap"):
            view = requested
        else:
            view = "ap" if mode == "per-ap" else "ssid"
        if view == "ap":
            data = daemon.capture.captured_access_points()
        else:
            data = daemon.capture.captured_networks()
        sort = "recent" if request.args.get("sort") == "recent" else "name"
        ctl = control()
        # The passphrase shows only with web auth on AND show_passphrases true
        # (read live, so a toggle takes effect without a restart); the badge and
        # count are not secrets and show regardless.
        show_pass = bool((cfg.get("web") or {}).get("show_passphrases", True))
        by_bssid, by_essid = _cracked_maps(daemon, view, data)
        # A one-line banner after an import (set by /cracked/import's redirect).
        err = request.args.get("err")
        msg = err or request.args.get("msg")
        return html(_render_networks(
            data, ctl, query=request.args.get("q", ""),
            page=request.args.get("page", 1), view=view, sort=sort,
            cracked_by_bssid=by_bssid, cracked_by_essid=by_essid,
            show_passphrases=show_pass, auth_on=bool(ctl.get("ready")),
            message=msg, error=bool(err)))

    def _back_to_networks(message, error=False, view=None, code=303):
        # POST-redirect-GET back to /networks with a one-line banner, keeping the
        # view and (for token auth) the token so the page stays signed in.
        params = {}
        if view in ("ssid", "ap"):
            params["view"] = view
        token = request.args.get("token") or request.form.get("token")
        if token:
            params["token"] = token
        params["err" if error else "msg"] = message
        return redirect("/networks?" + urlencode(params), code)

    @app.post("/cracked/import")
    def cracked_import():  # noqa: ANN202
        # Import a potfile of known passphrases into the cracked store. Runs as
        # the daemon (pi) into the pi-owned store -- no root, no new privilege.
        ctl = control()
        view = request.form.get("view")
        view = view if view in ("ssid", "ap") else None
        if not ctl["ready"]:
            return _back_to_networks(
                "Turn on the web login to import cracked results.",
                error=True, view=view)
        if not _same(request.form.get("csrf") or "", csrf_token):
            return _back_to_networks(
                "This page is out of date (the daemon restarted). Reload and "
                "try again.", error=True, view=view)
        upload = request.files.get("potfile")
        if upload is None or not upload.filename:
            return _back_to_networks("No file selected.", error=True, view=view)
        cfg = daemon._fresh_config()
        cracked_cfg = cfg.get("cracked") or {}
        try:
            max_mb = max(1, int(cracked_cfg.get("max_import_mb", 2)))
        except (TypeError, ValueError):
            max_mb = 2
        try:
            max_lines = max(1, int(cracked_cfg.get("max_import_lines", 50000)))
        except (TypeError, ValueError):
            max_lines = 50000
        # The file is untrusted: read at most the cap+1 (so an oversize file is
        # rejected, not streamed whole), decode with replacement, parse in pure
        # Python -- never a shell -- and write under a device-generated name.
        max_bytes = max_mb * 1024 * 1024
        raw = upload.read(max_bytes + 1)
        if len(raw) > max_bytes:
            return _back_to_networks(
                "That file is larger than the %d MB limit." % max_mb,
                error=True, view=view)
        try:
            result = daemon.cracked.ingest(raw.decode("utf-8", "replace"),
                                           max_lines=max_lines)
        except OSError as exc:
            log.warning("web: cracked import failed: %s", exc)
            return _back_to_networks("Could not save the import.",
                                     error=True, view=view)
        return _back_to_networks(
            "Imported %d record(s); skipped %d line(s)."
            % (result.get("imported", 0), result.get("skipped", 0)), view=view)

    @app.get("/agent")
    def agent_page():  # noqa: ANN202
        return html(_render_agent(daemon.status(), control()))

    @app.get("/settings")
    def settings_page():  # noqa: ANN202
        return html(_render_settings(daemon._fresh_config(), control()))

    @app.post("/settings")
    def settings_submit():  # noqa: ANN202
        ctl = control()
        if not ctl["ready"]:
            return html(_render_settings(daemon._fresh_config(), ctl,
                                         message=CONTROLS_OFF, error=True), 403)
        if not _same(request.form.get("csrf") or "", csrf_token):
            return html(_render_settings(
                daemon._fresh_config(), ctl, error=True,
                message="This page is out of date (the daemon restarted). "
                        "It has been reloaded; make the change again.",
            ), 400)
        table = request.form.get("table") or ""
        key = request.form.get("key") or ""
        entry = config_catalog.find(table, key)
        if entry is None or not entry.get("editable"):
            return html(_render_settings(daemon._fresh_config(), ctl, error=True,
                                         message="That setting can't be changed "
                                         "here."), 400)
        if request.form.get("reset"):
            value = entry.get("default")
        else:
            try:
                value = _coerce_setting(entry, request.form.get("value"))
            except ValueError as exc:
                return html(_render_settings(daemon._fresh_config(), ctl,
                                             message=str(exc), error=True), 400)
        reply = apply_change({"target": "config-set", "table": table,
                              "key": key, "value": value})
        if reply.get("ok"):
            if reply.get("restart"):
                return html(_render_settings_applied(
                    ctl, reply, daemon.status().get("started")))
            # Re-read: capture/agent changes don't restart anything, so show the
            # new value straight away with the "takes effect on restart" hint.
            return html(_render_settings(daemon._fresh_config(), ctl,
                                         message=reply.get("restart_hint") or "Saved."))
        log.info("web: config-set %s.%s refused: %s", table, key, reply.get("error"))
        return html(_render_settings(daemon._fresh_config(), ctl,
                                     message=reply.get("error") or "not applied",
                                     error=True), 400)

    @app.get("/whitelist")
    def whitelist_page():  # noqa: ANN202
        return html(_render_whitelist(
            daemon.capture_whitelist(), control(),
            query=request.args.get("q", ""), page=request.args.get("page", 1)))

    @app.get("/guide")
    def guide_page():  # noqa: ANN202
        return html(_render_guide(control()))

    @app.get("/guide/<slug>")
    def guide_section_page(slug):  # noqa: ANN202
        ctl = control()
        if slug == _GUIDE_FULL_SLUG:
            return html(_render_guide_full(ctl))
        page = _render_guide_section(slug, ctl)
        if page is None:
            return Response("no such section", 404)
        return html(page)

    @app.get("/update/notes")
    def update_notes_page():  # noqa: ANN202
        # The merged release notes between the installed version and the latest
        # one, read from the updates plugin's shared state. Read-only, no auth
        # needed to view (it echoes only release notes, never a secret).
        return html(_render_update_notes(control(), _read_update_state()))

    @app.get("/manage/<name>")
    def manage_page(name):  # noqa: ANN202
        record = daemon.loader.records.get(name)
        if record is None:
            return Response("no such plugin", 404)
        return html(_render_manage(daemon, record, control()))

    @app.post("/manage/<name>")
    def manage_submit(name):  # noqa: ANN202
        record = daemon.loader.records.get(name)
        if record is None:
            return Response("no such plugin", 404)
        ctl = control()
        if not ctl["ready"]:
            return html(_render_manage(daemon, record, ctl, message=CONTROLS_OFF,
                                       error=True), 403)
        if not _same(request.form.get("csrf") or "", csrf_token):
            return html(_render_manage(
                daemon, record, ctl, error=True,
                message="This page is out of date (the daemon restarted). "
                        "It has been reloaded; make the change again.",
            ), 400)
        try:
            change = build_change(daemon, record, request.form)
        except ValueError as exc:
            return html(_render_manage(daemon, record, ctl, message=str(exc),
                                       error=True), 400)
        if "set" not in change and "enabled" not in change:
            return html(_render_manage(daemon, record, ctl, message="Nothing changed."))
        reply = apply_change(change)
        if reply.get("ok"):
            if reply.get("restart"):
                return html(_render_applied(record.name, reply, ctl,
                                            daemon.status().get("started")))
            return html(_render_manage(daemon, record, ctl, message="Nothing changed."))
        log.info("web: change to plugin %s refused: %s", name, reply.get("error"))
        return html(_render_manage(
            daemon, record, ctl, message=reply.get("error") or "not applied",
            error=True, missing=reply.get("missing") or (),
            need_ack=bool(reply.get("need_ack")),
            table=_refused_table(daemon, record, change),
        ), 400)

    @app.post("/capture")
    def capture_submit():  # noqa: ANN202
        ctl = control()
        status = daemon.status()
        addrs = management_addresses()
        rfr = refresh_seconds(web_cfg)
        if not ctl["ready"]:
            return html(_render_status(status, ctl, message=CONTROLS_OFF,
                                       error=True, addresses=addrs, refresh=rfr), 403)
        if not _same(request.form.get("csrf") or "", csrf_token):
            return html(_render_status(
                status, ctl, error=True, addresses=addrs, refresh=rfr,
                message="This page is out of date (the daemon restarted). "
                        "It has been reloaded; try again.",
            ), 400)
        action = request.form.get("action")
        if action not in ("enable", "disable"):
            return html(_render_status(status, ctl, message="Nothing changed.",
                                       addresses=addrs, refresh=rfr))
        change = {"target": "capture", "enabled": action == "enable"}
        if request.form.get("ack") == "yes":
            change["ack"] = True
        reply = apply_change(change)
        if reply.get("ok"):
            # Re-read: the capture service state is live (systemctl), so the
            # dashboard comes back showing the new state and the flipped toggle.
            # The status() read above cached the pre-change state, and enable
            # starts the service --no-block, so first drop that and wait
            # (bounded) for systemd to reach the new state; otherwise the page
            # shows the old state and the toggle needs a second tap.
            # No success banner: the capture row already shows the state, so a
            # "Capture enabled" message beside a "running" row is just noise.
            daemon.wait_capture_state(action == "enable")
            return html(_render_status(daemon.status(), control(),
                                       addresses=management_addresses(), refresh=rfr))
        if reply.get("need_ack"):
            return html(_render_capture_confirm(ctl, reply.get("error") or ""))
        log.info("web: capture change refused: %s", reply.get("error"))
        return html(_render_status(status, ctl, error=True, addresses=addrs,
                                   refresh=rfr,
                                   message=reply.get("error") or "not applied"), 400)

    @app.post("/agent")
    def agent_submit():  # noqa: ANN202
        ctl = control()
        status = daemon.status()
        if not ctl["ready"]:
            return html(_render_agent(status, ctl, message=CONTROLS_OFF,
                                      error=True), 403)
        if not _same(request.form.get("csrf") or "", csrf_token):
            return html(_render_agent(
                status, ctl, error=True,
                message="This page is out of date (the daemon restarted). "
                        "It has been reloaded; try again.",
            ), 400)
        action = request.form.get("action")
        if action == "train_mode":
            # The on-device training on/off radio writes the [agent] train key
            # through the validated config-set apply (same path as the settings
            # page): "on" -> board-aware auto, "off" -> never. It restarts the
            # agent so it re-resolves the mode, and never touches policy or
            # algorithm.
            value = "off" if request.form.get("train_mode") == "off" else "auto"
            reply = apply_change({"target": "config-set", "table": "agent",
                                  "key": "train", "value": value})
            if reply.get("ok"):
                msg = ("On-device training turned off." if value == "off"
                       else "On-device training is on.")
                if reply.get("restart_hint"):
                    msg += " " + reply["restart_hint"]
                return html(_render_agent(daemon.status(), control(), message=msg))
            log.info("web: train-mode refused: %s", reply.get("error"))
            return html(_render_agent(status, ctl, error=True,
                                      message=reply.get("error") or "not applied"), 400)
        if action == "train":
            # A "train now" click is its own action: never rewrite the
            # algorithm/policy the form also carries - just ask the agent to
            # train. The agent is the final gate (it refuses under memory/
            # thermal pressure and says so on the card).
            change = {"target": "agent", "action": "train"}
            ok_message = "Training requested; it will start shortly."
        else:
            change = {"target": "agent"}
            algorithm = request.form.get("algorithm")
            policy = request.form.get("policy")
            if algorithm:
                change["algorithm"] = algorithm
            if policy:
                change["policy"] = policy
            if action in ("enable", "disable"):
                change["enabled"] = action == "enable"
            if len(change) == 1:   # nothing but the target
                return html(_render_agent(status, ctl, message="Nothing changed."))
            # Switching to an algorithm that has no trained model yet starts a
            # background training run to build one. Make that an explicit,
            # accepted choice: confirm once before applying. Switching to an
            # algorithm already trained just reloads its saved model, so it
            # needs no confirm; neither does disabling.
            cur_algo = (status.get("agent") or {}).get("algorithm")
            if (algorithm and action != "disable" and algorithm != cur_algo
                    and algorithm in AGENT_ALGORITHMS
                    and not _agent_has_model(algorithm)
                    and request.form.get("train_ack") != "yes"):
                return html(_render_agent_train_confirm(
                    ctl, algorithm, policy, action))
            ok_message = "Agent updated."
        reply = apply_change(change)
        if reply.get("ok"):
            # Stay on the agent page; its readout and the service state are read
            # live, so a fresh status reflects the change on its own.
            return html(_render_agent(daemon.status(), control(),
                                      message=ok_message))
        log.info("web: agent change refused: %s", reply.get("error"))
        return html(_render_agent(status, ctl, error=True,
                                  message=reply.get("error") or "not applied"), 400)

    @app.post("/power")
    def power_submit():  # noqa: ANN202
        ctl = control()
        status = daemon.status()
        addrs = management_addresses()
        rfr = refresh_seconds(web_cfg)
        if not ctl["ready"]:
            return html(_render_status(status, ctl, message=CONTROLS_OFF,
                                       error=True, addresses=addrs, refresh=rfr), 403)
        if not _same(request.form.get("csrf") or "", csrf_token):
            return html(_render_status(
                status, ctl, error=True, addresses=addrs, refresh=rfr,
                message="This page is out of date (the daemon restarted). "
                        "It has been reloaded; try again.",
            ), 400)
        action = request.form.get("action")
        if action not in _POWER_WORDS:
            return html(_render_status(status, ctl, message="Nothing changed.",
                                       addresses=addrs, refresh=rfr))
        if request.form.get("confirm") != "yes":
            # First tap lands on a confirm page (naming what will happen), so a
            # stray tap never reboots or powers the device down.
            return html(_render_power_confirm(ctl, action))
        reply = apply_change({"target": "power", "action": action})
        if reply.get("ok"):
            # The root helper defers the command a moment, so this reply still
            # reaches the browser before the device goes down. For a reboot the
            # page polls until the device is back (passing the current daemon's
            # started so it waits for a fresh one).
            return html(_render_power_done(action, ctl, status.get("started")))
        log.info("web: power %s refused: %s", action, reply.get("error"))
        return html(_render_status(status, ctl, error=True, addresses=addrs,
                                   refresh=rfr,
                                   message=reply.get("error") or "not applied"), 400)

    @app.post("/update")
    def update_submit():  # noqa: ANN202
        ctl = control()
        status = daemon.status()
        addrs = management_addresses()
        rfr = refresh_seconds(web_cfg)
        if not ctl["ready"]:
            return html(_render_status(status, ctl, message=CONTROLS_OFF,
                                       error=True, addresses=addrs, refresh=rfr), 403)
        if not _same(request.form.get("csrf") or "", csrf_token):
            return html(_render_status(
                status, ctl, error=True, addresses=addrs, refresh=rfr,
                message="This page is out of date (the daemon restarted). "
                        "It has been reloaded; try again.",
            ), 400)
        if request.form.get("action") != "apply":
            return html(_render_status(status, ctl, message="Nothing changed.",
                                       addresses=addrs, refresh=rfr))
        state = _read_update_state()
        if not state.get("offerable"):
            # Re-checked server-side: never apply off a stale button.
            return html(_render_status(status, ctl, error=True, addresses=addrs,
                                       refresh=rfr,
                                       message="No in-place update is available."), 400)
        no_backup = request.form.get("no_backup") == "yes"
        if request.form.get("confirm") != "yes":
            # First tap lands on a confirm page naming the version and that the
            # services will restart, so a stray tap never triggers an upgrade.
            # ``no_backup`` carries the retry-without-backup choice through so
            # the confirm page reflects it.
            return html(_render_update_confirm(ctl, state, no_backup=no_backup))
        # Install exactly the release the operator confirmed: if a check since
        # changed the offer, show it again rather than install something else.
        # The root helper and CLI re-check this tag (and re-verify the release
        # with the server) too.
        tag = request.form.get("tag") or ""
        if tag != (state.get("latest") or ""):
            return html(_render_status(
                status, ctl, error=True, addresses=addrs, refresh=rfr,
                message="The available update changed since you confirmed it; "
                        "review it again."), 400)
        reply = apply_change({"target": "update", "action": "apply", "tag": tag,
                              "no_backup": no_backup})
        if reply.get("ok"):
            # The root helper runs the update in the background and restarts the
            # daemon when done; this reply still reaches the browser first, then
            # the page polls until the new version is up.
            return html(_render_update_done(state, ctl, status.get("started")))
        log.info("web: update refused: %s", reply.get("error"))
        return html(_render_status(status, ctl, error=True, addresses=addrs,
                                   refresh=rfr,
                                   message=reply.get("error") or "not applied"), 400)

    @app.post("/bt-pair")
    def bt_pair_submit():  # noqa: ANN202
        ctl = control()
        status = daemon.status()
        addrs = management_addresses()
        rfr = refresh_seconds(web_cfg)
        if not ctl["ready"]:
            return html(_render_status(status, ctl, message=CONTROLS_OFF,
                                       error=True, addresses=addrs, refresh=rfr), 403)
        if not _same(request.form.get("csrf") or "", csrf_token):
            return html(_render_status(
                status, ctl, error=True, addresses=addrs, refresh=rfr,
                message="This page is out of date (the daemon restarted). "
                        "It has been reloaded; try again.",
            ), 400)
        if request.form.get("confirm") != "yes":
            # First tap lands on a confirm page, so a stray tap never drops a
            # paired phone / reopens onboarding.
            return html(_render_bt_pair_confirm(ctl))
        reply = apply_change({"target": "bt-pair"})
        if reply.get("ok"):
            return html(_render_bt_pair_done(ctl))
        log.info("web: bt-pair refused: %s", reply.get("error"))
        return html(_render_status(status, ctl, error=True, addresses=addrs,
                                   refresh=rfr,
                                   message=reply.get("error") or "not applied"), 400)

    @app.post("/whitelist")
    def whitelist_submit():  # noqa: ANN202
        ctl = control()
        if not ctl["ready"]:
            return html(_render_whitelist(daemon.capture_whitelist(), ctl,
                                          message=CONTROLS_OFF, error=True), 403)
        if not _same(request.form.get("csrf") or "", csrf_token):
            return html(_render_whitelist(
                daemon.capture_whitelist(), ctl, error=True,
                message="This page is out of date (the daemon restarted). "
                        "It has been reloaded; try again."), 400)
        action = request.form.get("action")
        entry = request.form.get("entry") or ""
        reply = apply_change({"target": "capture-whitelist",
                              "action": action, "entry": entry})
        if reply.get("ok"):
            verb = "Added" if action == "add" else "Removed"
            return html(_render_whitelist(
                daemon.capture_whitelist(), control(),
                message="%s %s." % (verb, entry.strip())))
        log.info("web: whitelist change refused: %s", reply.get("error"))
        return html(_render_whitelist(daemon.capture_whitelist(), ctl, error=True,
                                      message=reply.get("error") or "not applied"), 400)

    @app.after_request
    def _page_headers(resp):  # noqa: ANN202
        # The pages carry controls and, with token auth, the token in their
        # links: never framed by another site, cached, or sent on in a Referer.
        resp.headers.setdefault("X-Frame-Options", "DENY")
        resp.headers.setdefault("Content-Security-Policy", "frame-ancestors 'none'")
        resp.headers.setdefault("Referrer-Policy", "no-referrer")
        resp.headers.setdefault("Cache-Control", "no-store")
        return resp

    def _plugins_redirect_url(redirect, ctl):
        # Back to the plugins page with the token (if any) and a one-line banner.
        params = {}
        if redirect.message:
            params["err" if redirect.error else "msg"] = redirect.message
        token = ctl.get("token") or ""
        if token:
            params["token"] = token
        return "/plugins" + ("?" + urlencode(params) if params else "")

    @app.route("/plugins/<name>/", defaults={"subpath": ""},
               methods=["GET", "POST"])
    @app.route("/plugins/<name>/<path:subpath>", methods=["GET", "POST"])
    def plugin_route(name, subpath):  # noqa: ANN202
        record = daemon.loader.records.get(name)
        if record is None or record.state != "loaded":
            return Response("no such plugin", 404)
        # A POST to a plugin is a privileged state change, so it needs the web
        # login on (like every core control POST) and carries the CSRF token the
        # same way; the plugin never has to check either itself. A GET is plain
        # navigation to a page the plugin renders (which gates its own sensitive
        # data on ``context.web_authed``), so it is not auth-gated here.
        if request.method == "POST":
            if not control_ready(web_cfg):
                return html(_render_plugins(daemon.status(), control(),
                                            message=CONTROLS_OFF, error=True), 403)
            if not _same(request.form.get("csrf") or "", csrf_token):
                return html(_render_plugins(
                    daemon.status(), control(), error=True,
                    message="This page is out of date (the daemon restarted). "
                            "It has been reloaded; try again."), 400)
        # Hand the plugin the current page's CSRF token so a page it renders can
        # carry a valid form (e.g. a confirm step) back to itself, and whether
        # the web login is on (a page with sensitive data refuses without it).
        ctx = getattr(record.instance, "context", None)
        if ctx is not None:
            ctx.form_csrf = csrf_token
            ctx.web_authed = control_ready(web_cfg)
        timeout = webhook_timeout_seconds(web_cfg)
        if timeout > 0:
            with stuck_lock:
                previous = stuck_webhooks.get(name)
                if previous is not None and not previous.is_alive():
                    stuck_webhooks.pop(name, None)
                    previous = None
            if previous is not None:
                resp = html(_render_webhook_timeout(name, control(), busy=True), 503)
                resp.headers["Retry-After"] = str(int(timeout))
                return resp

        def remember_stuck(worker):
            with stuck_lock:
                stuck_webhooks[name] = worker

        try:
            if timeout > 0:
                # Resolve the proxy to the concrete request and parse its body
                # and query here, in the request thread, so the worker (which
                # runs outside Flask's request context) reads only cached values.
                req = request._get_current_object()
                _ = (req.form, req.args)
                finished, result = _run_with_timeout(
                    lambda: record.instance.on_webhook(subpath, req), timeout,
                    on_overrun=remember_stuck)
                if not finished:
                    daemon.log.warning(
                        "plugin %s on_webhook exceeded %ss; returned a timeout "
                        "page (the call is left running in the background)",
                        name, timeout)
                    return html(_render_webhook_timeout(name, control()), 504)
            else:
                result = record.instance.on_webhook(subpath, request)
        except Exception:  # noqa: BLE001 - a plugin route must not 500 the daemon
            daemon.log.exception("plugin %s on_webhook raised", name)
            return Response("plugin error", 500)
        if isinstance(result, PluginPage):
            return html(_page(result.title, result.body), result.status)
        if isinstance(result, PluginRedirect):
            return Response("", 303, headers={
                "Location": _plugins_redirect_url(result, control())})
        if result is None:
            return Response("", 204)
        return result

    return app


# Kept out of the page templates on purpose: the inline CSS has literal ``{}``
# that ``str.format`` would read as replacement fields (it did, and 500'd the
# page with KeyError: 'font-family'). Pages are assembled by concatenation.
# The page is built to read well on a phone: a fluid container, touch-sized
# controls, a dark-mode palette, and a plugin table that reflows into stacked
# cards below 40em (see the @media block). Everything is inline and
# dependency-free so the daemon still serves one self-contained page over the
# management link. Colours are CSS variables so light/dark share one ruleset.
_STATUS_CSS = (
    ":root{"
    "--bg:#f6f7f9;--fg:#1b1b1f;--muted:#5b5b66;--card:#fff;--border:#e4e4ea;"
    "--accent:#2e7d32;--link:#1565c0;"
    "--on-bg:#e6f4ea;--on-bd:#2e7d32;--off-bg:#f0f0f2;--off-bd:#9e9e9e;"
    "--err-bg:#fbeaea;--err-bd:#c62828;--shadow:0 1px 2px rgba(0,0,0,.08);"
    "--msg-bg:#eef6ee;--msg-bd:#9c9;--msgerr-bg:#fbeeee;--msgerr-bd:#d99;}"
    "@media(prefers-color-scheme:dark){:root{"
    "--bg:#15161a;--fg:#e7e7ea;--muted:#9a9aa6;--card:#1f2026;--border:#30313a;"
    "--accent:#388e3c;--link:#64b5f6;"
    "--on-bg:#16331e;--off-bg:#272830;--err-bg:#3a1d1d;--shadow:0 1px 2px rgba(0,0,0,.5);"
    "--msg-bg:#14301a;--msg-bd:#2e7d32;--msgerr-bg:#3a1d1d;--msgerr-bd:#c62828;}}"
    "*{box-sizing:border-box;}"
    "html{-webkit-text-size-adjust:100%;}"
    # The stack ends in sans-serif so the page still has a sane font off-system.
    "body{font-family:system-ui,-apple-system,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;"
    "margin:0 auto;max-width:60em;padding:1em 1em 3em;line-height:1.5;"
    "color:var(--fg);background:var(--bg);}"
    "h1{font-size:1.5rem;margin:.1em 0 .1em;}"
    "h2{font-size:1.15rem;margin:1.4em 0 .5em;}"
    "a{color:var(--link);}"
    # header subtitle: what the daemon is, with its version and epoch
    ".tagline{color:var(--muted);margin:.1em 0 1em;font-size:.95rem;}"
    # status card: a labelled row per fact (internet, capture) with a status dot
    ".dot{width:.6em;height:.6em;border-radius:50%;background:var(--off-bd);flex:none;}"
    ".dot.ok{background:var(--on-bd);}.dot.no{background:var(--err-bd);}"
    ".card{background:var(--card);border:1px solid var(--border);border-radius:12px;"
    "box-shadow:var(--shadow);overflow:hidden;margin:.3em 0 1em;}"
    # a card holding free content (headings, text, a form) rather than .srow
    # rows, which pad themselves: pad it the same, and keep its first and last
    # child's margins from doubling that inset.
    ".card.pad{padding:.85em 1em;}"
    ".card.pad>:first-child{margin-top:0;}.card.pad>:last-child{margin-bottom:0;}"
    ".srow{display:flex;align-items:center;gap:.75em;padding:.85em 1em;"
    "border-bottom:1px solid var(--border);flex-wrap:wrap;}"
    ".srow:last-child{border-bottom:none;}"
    ".srow .dot{width:.7em;height:.7em;}"
    ".scol{display:flex;flex-direction:column;line-height:1.3;min-width:0;}"
    ".skey{font-size:.72rem;text-transform:uppercase;letter-spacing:.04em;color:var(--muted);}"
    ".sval{font-weight:600;font-size:1.05rem;}"
    ".smeta{color:var(--muted);font-weight:400;font-size:.88rem;}"
    ".sact{margin-left:auto;}"
    # plugins nav: a tappable card linking to the plugins page
    ".navcard{display:flex;align-items:center;gap:.75em;padding:.85em 1em;"
    "text-decoration:none;color:var(--fg);background:var(--card);"
    "border:1px solid var(--border);border-radius:12px;box-shadow:var(--shadow);"
    "margin:.3em 0 1em;}"
    ".navcard:hover,.navcard:focus{border-color:var(--link);}"
    ".navcard .arrow{margin-left:auto;color:var(--muted);font-size:1.3rem;line-height:1;}"
    # plugin table (wide screens)
    ".wrap{overflow-x:auto;}"
    "table{border-collapse:collapse;width:100%;background:var(--card);"
    "border:1px solid var(--border);border-radius:10px;overflow:hidden;box-shadow:var(--shadow);}"
    "th,td{padding:.6em .8em;text-align:left;vertical-align:top;"
    "border-bottom:1px solid var(--border);}"
    "th{font-size:.78rem;text-transform:uppercase;letter-spacing:.03em;color:var(--muted);}"
    "tbody tr:last-child td{border-bottom:none;}"
    # the plugin state is the name cell's shade: green on, gray off, red error
    "td.plug{font-weight:600;border-left:4px solid var(--off-bd);}"
    ".s-on{background:var(--on-bg);}.s-off{background:var(--off-bg);}.s-err{background:var(--err-bg);}"
    "td.plug.s-on{border-left-color:var(--on-bd);}"
    "td.plug.s-off{border-left-color:var(--off-bd);}"
    "td.plug.s-err{border-left-color:var(--err-bd);}"
    ".legend{font-size:.85rem;color:var(--muted);margin:.3em 0 .8em;display:flex;"
    "flex-wrap:wrap;gap:.5em;align-items:center;}"
    ".legend span{padding:.1em .6em;border-radius:6px;border:1px solid var(--border);}"
    # forms / messages
    "form.inline{display:inline;margin:0;}"
    ".msg{padding:.6em .8em;border:1px solid var(--msg-bd);background:var(--msg-bg);border-radius:8px;}"
    ".msg.err{border-color:var(--msgerr-bd);background:var(--msgerr-bg);}"
    ".notice,.hint{color:var(--muted);}"
    ".hint{font-size:.9em;}"
    # Discreet dashboard link to the advanced Settings page: muted and centred,
    # sitting quietly at the bottom of the management UI (not a prominent card).
    ".advlink{margin:1.6em 0 .2em;text-align:center;font-size:.85rem;}"
    ".advlink a{color:var(--muted);}"
    ".advlink a:hover,.advlink a:focus{color:var(--link);}"
    # Settings page: one key per row, each its own small Apply/Reset form.
    ".setfield{padding:.75em 0;border-bottom:1px solid var(--border);}"
    ".setfield .setkey{display:inline-block;font-weight:600;font-size:.95rem;}"
    ".setfield .roval{color:var(--muted);}"
    ".setform{display:flex;flex-wrap:wrap;gap:.5em;align-items:center;margin:.4em 0 0;}"
    ".setform input,.setform select{flex:1 1 12em;margin:0;}"
    ".setform button{margin:0;padding:.4em .9em;font-size:.9rem;}"
    ".setform button.reset{background:var(--card);color:var(--muted);"
    "border:1px solid var(--border);}"
    "label{display:block;margin:.9em 0 .25em;font-weight:600;}"
    "label.check{font-weight:normal;}"
    "input[type=text],input[type=password],input[type=number],select"
    "{width:100%;max-width:28em;padding:.5em;border:1px solid var(--border);border-radius:8px;"
    "background:var(--card);color:var(--fg);font-size:1rem;}"
    # learning-agent control card: the two dropdowns and the Save / Enable row
    ".agentform{padding:.85em 1em;margin:.3em 0 1em;}"
    ".agentform label{margin:.6em 0 .25em;}"
    ".agentbtns{display:flex;flex-wrap:wrap;gap:.6em;}"
    ".agentbtns button{flex:1 1 10em;}"
    # policy radio group on the /agent page: each choice is a tappable card with
    # a bold label and a muted one-line description. The algorithm field below
    # reveals only for the "learned" policy (JS toggles [hidden]).
    ".radios{display:flex;flex-direction:column;gap:.5em;margin:.2em 0 .6em;}"
    ".radio{font-weight:400;margin:0;padding:.6em .75em;border:1px solid var(--border);"
    "border-radius:8px;background:var(--card);cursor:pointer;}"
    ".radio .rlabel{font-weight:600;}"
    ".radio .rdesc{display:block;color:var(--muted);font-size:.88rem;margin-top:.15em;}"
    ".agent-algo{margin:.2em 0 .2em;}"
    ".missing{color:var(--err-bd);}"
    "button{padding:.55em 1.2em;margin-top:1em;border:1px solid transparent;border-radius:8px;"
    "background:var(--accent);color:#fff;font-size:1rem;cursor:pointer;}"
    # Disable is destructive, so its button is red rather than the green accent.
    "button.danger{background:var(--err-bd);}"
    # A secondary action (e.g. "retry without a fresh backup") reads as the
    # lesser choice next to the primary button: outlined, not filled.
    "button.secondary{background:transparent;color:var(--link);"
    "border-color:var(--border);}"
    # A checkbox+label line on the update confirm page (skip the pre-update backup).
    ".chk{display:flex;align-items:flex-start;gap:.5em;margin:1em 0 .2em;"
    "font-size:.92rem;color:var(--muted);}"
    ".chk input{margin-top:.2em;}"
    "form.inline button{margin:0;padding:.35em .9em;font-size:.9rem;}"
    # The plugins-page config cell lays out Disable/settings on the left; the
    # per-plugin action buttons (Restore / Train, neutral blue) are grouped in
    # one box pushed to the far right, so they stay side by side and wrap below
    # together (not each onto its own staggered row) when the column is narrow.
    ".cfgrow{display:flex;align-items:center;gap:.5em;flex-wrap:wrap;}"
    ".cfgrow .rowactions{margin-left:auto;display:flex;gap:.5em;flex-wrap:wrap;}"
    ".cfgrow form.rowaction{margin:0;}"
    "button.rowaction{background:var(--link);}"
    # device power controls: a labelled row of two buttons (reboot / shut down)
    # under the plugins nav. Side by side on wide screens; each stacks to its own
    # full-width row on phones (see the media query). Shut down is the red danger
    # button; reboot keeps the green accent.
    ".pwrlabel{font-size:.72rem;text-transform:uppercase;letter-spacing:.04em;"
    "color:var(--muted);margin:.4em 0 .3em;}"
    ".power{display:flex;flex-wrap:wrap;gap:.6em;margin:0 0 1.2em;}"
    ".power form{flex:1 1 11em;margin:0;}"
    ".power button{width:100%;margin:0;}"
    # Re-pair control: deliberately understated so it does not dominate - a small,
    # muted, outline-only button that sits quietly below the rest.
    ".btpair{margin:0 0 1.2em;}"
    ".btpair button{width:auto;margin:0;padding:.4em .8em;font-size:.82rem;"
    "background:transparent;color:var(--muted);border:1px solid var(--border);}"
    ".btpair button:hover,.btpair button:focus{color:var(--link);border-color:var(--link);}"
    # plugin widgets: a responsive grid of per-plugin readout cards. Each card
    # lists the plugin's labelled values; a percent/gauge element also draws a
    # bar from the numeric part of its value (the e-ink view shows only text).
    ".widgets{display:grid;grid-template-columns:repeat(auto-fill,minmax(14em,1fr));"
    "gap:.8em;margin:.3em 0 .6em;}"
    ".widget{position:relative;background:var(--card);border:1px solid var(--border);"
    "border-radius:10px;box-shadow:var(--shadow);padding:.7em .85em;}"
    ".widget h3{font-size:.95rem;margin:0 0 .5em;}"
    # a stopped/quarantined plugin: a subtle pill on the card header (with a text
    # label, never colour alone) linking to the Plugins page where the reason is
    # shown. The card border also picks up the error tone.
    ".widget[data-state=stopped]{border-color:var(--msgerr-bd);}"
    ".wbadge{display:inline-block;margin-left:.5em;padding:.02em .5em;border-radius:999px;"
    "font-size:.7rem;font-weight:600;vertical-align:middle;text-decoration:none;"
    "background:var(--msgerr-bg);color:var(--fg);border:1px solid var(--msgerr-bd);}"
    ".wbadge:hover,.wbadge:focus{border-color:var(--link);color:var(--link);}"
    # drag-to-reorder: a grip handle the reorder script adds to each card. The
    # cards still render (in default order) with JS off; the grip only appears
    # when the script wires it up. touch-action:none so a drag from the grip
    # reorders instead of scrolling the page; a generous tap target and the
    # -webkit- select/callout/tap-highlight resets make it grabbable on a phone
    # without iOS Safari firing its text-selection or callout on a touch-hold.
    ".widget .grip{position:absolute;top:.25em;right:.3em;cursor:grab;color:var(--muted);"
    "font-size:1.3rem;line-height:1;padding:.3em .45em;touch-action:none;"
    "user-select:none;-webkit-user-select:none;-webkit-touch-callout:none;"
    "-webkit-tap-highlight-color:transparent;}"
    ".widget.dragging{opacity:.6;box-shadow:0 3px 12px rgba(0,0,0,.3);}"
    # While a drag is in flight the script adds .dragging to the grid; kill text
    # selection and the iOS callout across the whole grid so dragging a card up
    # or down never highlights the text it passes over.
    ".widgets.dragging,.widgets.dragging *{user-select:none;-webkit-user-select:none;"
    "-webkit-touch-callout:none;}"
    ".widget dl{margin:0;display:grid;grid-template-columns:auto 1fr;gap:.3em .8em;align-items:baseline;}"
    ".widget dt{color:var(--muted);font-size:.9rem;}"
    ".widget dd{margin:0;text-align:left;overflow-wrap:anywhere;font-variant-numeric:tabular-nums;font-weight:600;}"
    ".bar{position:relative;grid-column:1/-1;height:.45em;border-radius:999px;"
    "background:var(--off-bg);overflow:hidden;margin:-.1em 0 .25em;}"
    ".bar>span{display:block;height:100%;background:var(--accent);}"
    # reference marker on a gauge (e.g. the battery shutdown threshold). The
    # value it marks is also shown as text, so the mark is never the only signal.
    ".bar .mark{position:absolute;top:0;bottom:0;width:2px;margin-left:-1px;"
    "background:var(--fg);opacity:.7;}"
    # network addresses: a light, native <details> disclosure under the status
    # card (the management-link addresses the dashboard is reachable at)
    ".addrs{margin:-.5em 0 1em;}"
    ".addrs summary{cursor:pointer;color:var(--link);font-size:.92rem;padding:.3em 0;}"
    ".addrs dl{display:grid;grid-template-columns:auto 1fr;gap:.3em .9em;margin:.4em 0 0;"
    "background:var(--card);border:1px solid var(--border);border-radius:10px;"
    "padding:.6em .85em;box-shadow:var(--shadow);}"
    ".addrs dt{color:var(--muted);font-family:ui-monospace,SFMono-Regular,Menlo,monospace;}"
    ".addrs dd{margin:0;text-align:right;font-family:ui-monospace,SFMono-Regular,Menlo,monospace;}"
    # captured-networks page: a view toggle, a search form and a pager
    ".viewtabs{display:flex;gap:.5em;margin:.3em 0 .6em;flex-wrap:wrap;}"
    ".viewtabs a{padding:.4em .9em;border:1px solid var(--border);border-radius:8px;"
    "text-decoration:none;color:var(--link);background:var(--card);font-size:.9rem;}"
    ".viewtabs a.on{font-weight:700;box-shadow:inset 0 -2px 0 var(--link);}"
    ".sorttabs{margin:.2em 0 .6em;}"
    ".viewtabs .tablabel{align-self:center;color:var(--muted);font-size:.85rem;}"
    ".search,.wladd{display:flex;gap:.5em;margin:.3em 0 .8em;}"
    ".search input[type=text],.wladd input[type=text]{flex:1 1 auto;max-width:none;}"
    ".search button,.wladd button{margin:0;}"
    ".count{color:var(--muted);font-size:.9rem;margin:.2em 0 .8em;}"
    ".pager{display:flex;justify-content:space-between;align-items:center;gap:1em;margin:1em 0;}"
    ".pager a{padding:.45em 1em;border:1px solid var(--border);border-radius:8px;"
    "text-decoration:none;color:var(--link);background:var(--card);}"
    ".pager .off{opacity:.4;pointer-events:none;}"
    # the authorized-use confirm block (capture / network plugins)
    ".ack{border:1px solid var(--msgerr-bd);background:var(--msgerr-bg);border-radius:8px;"
    "padding:.7em .85em;margin:.6em 0;}"
    # phones: the table becomes stacked cards, each cell labelled by its header
    "@media(max-width:40em){"
    ".widgets{grid-template-columns:1fr;}"
    # the capture action drops to its own full-width row under the capture line
    ".sact{margin-left:0;flex-basis:100%;}"
    ".sact button{width:100%;margin-top:.2em;}"
    # the reboot/shut-down buttons each take their own full-width row
    ".power form{flex-basis:100%;}"
    "body{padding:.8em .8em 3em;}"
    ".wrap{overflow-x:visible;}"
    "table{border:none;border-radius:0;background:transparent;box-shadow:none;}"
    "thead{position:absolute;width:1px;height:1px;overflow:hidden;clip:rect(0 0 0 0);}"
    "tbody tr{display:block;background:var(--card);border:1px solid var(--border);"
    "border-radius:10px;box-shadow:var(--shadow);margin-bottom:.8em;overflow:hidden;}"
    "tbody td{display:block;border:none;border-bottom:1px solid var(--border);padding:.55em .9em;}"
    "tbody tr td:last-child{border-bottom:none;}"
    "td[data-label]::before{content:attr(data-label);display:block;font-size:.72rem;"
    "text-transform:uppercase;letter-spacing:.03em;color:var(--muted);}"
    "td.plug{font-size:1.1rem;border-left:none;border-bottom:none;padding:.65em .9em;}"
    "td.plug.s-on{border-top:4px solid var(--on-bd);}"
    "td.plug.s-off{border-top:4px solid var(--off-bd);}"
    "td.plug.s-err{border-top:4px solid var(--err-bd);}"
    "}"
    # documentation footer (every page) + the user-guide page (/guide)
    ".docfoot{margin:2.5em auto 0;max-width:60em;padding:1em .2em 0;"
    "border-top:1px solid var(--border);text-align:center;font-size:.9rem;"
    "color:var(--muted);}"
    ".docfoot a{color:var(--link);text-decoration:none;}"
    ".docfoot a:hover,.docfoot a:focus{text-decoration:underline;}"
    ".guide{line-height:1.55;}"
    ".guide h1{font-size:1.5rem;margin:.2em 0 .5em;}"
    ".guide h2{font-size:1.2rem;margin:1.5em 0 .5em;padding-top:.7em;"
    "border-top:1px solid var(--border);}"
    ".guide h3{font-size:1.03rem;margin:1.1em 0 .3em;}"
    ".guide p{margin:.5em 0;}"
    ".guide ul,.guide ol{margin:.5em 0;padding-left:1.4em;}"
    ".guide li{margin:.25em 0;}"
    ".guide code{background:var(--card);border:1px solid var(--border);"
    "border-radius:5px;padding:.08em .35em;font-size:.9em;"
    "font-family:ui-monospace,SFMono-Regular,Menlo,monospace;}"
    ".guide pre{background:var(--card);border:1px solid var(--border);"
    "border-radius:10px;padding:.8em 1em;overflow-x:auto;}"
    ".guide pre code{background:none;border:none;padding:0;font-size:.88em;}"
    ".guide hr{border:none;border-top:1px solid var(--border);margin:1.5em 0;}"
    # the /guide table of contents + the per-section prev/next nav
    ".guideindex ol{margin:.3em 0;}"
    ".guideindex>ol>li{margin:.55em 0;}"
    ".guideindex ul{margin:.2em 0 .7em;}"
    ".guidefull{font-size:.92rem;color:var(--muted);}"
    ".guidenav{display:flex;justify-content:space-between;gap:1em;margin-top:2em;"
    "padding-top:1em;border-top:1px solid var(--border);}"
    ".guidenav a{color:var(--link);text-decoration:none;}"
    ".guidenav a:hover,.guidenav a:focus{text-decoration:underline;}"
    # Cracked-networks marker and passphrase disclosure on /networks. The badge
    # is a lock glyph plus the word "cracked" (never colour alone). The
    # passphrase lives in a <details> so it is not rendered into the row until
    # the operator expands it (dependency-free, keyboard-accessible).
    ".crk{display:inline-flex;align-items:center;gap:.3em;margin-left:.5em;"
    "font-size:.78rem;font-weight:600;color:var(--on-bd);white-space:nowrap;}"
    ".crk .lock{font-size:.9em;}"
    "details.crk-d>summary{cursor:pointer;list-style:none;}"
    "details.crk-d>summary::-webkit-details-marker{display:none;}"
    "details.crk-d>summary::after{content:'\\0020\\25be';color:var(--muted);"
    "font-size:.8em;}"
    "details.crk-d[open]>summary::after{content:'\\0020\\25b4';}"
    ".crk-p{margin:.4em 0 .1em;font-size:.9rem;}"
    ".crk-p code{word-break:break-all;}"
    ".crk-p .bssid{color:var(--muted);}"
    ".crk-hidden{color:var(--muted);font-style:italic;}"
    ".crkimport{margin:1.2em 0;}"
    ".crkimport>summary{cursor:pointer;color:var(--link);}"
    ".crkimport form{margin-top:.7em;display:flex;flex-wrap:wrap;gap:.6em;"
    "align-items:center;}"
    ".crkimport .hint{flex-basis:100%;margin:0;}"
)


def _doc_footer():
    """A 'Documentation' link at the very bottom of every page, pointing at the
    on-device user guide (/guide). Under token auth it carries the token query
    string so the link stays signed in; outside a request context (unit tests),
    the plain /guide link is used."""
    qs = ""
    try:
        from flask import has_request_context, request
        if has_request_context():
            token = request.args.get("token")
            if token:
                qs = "?token=" + quote(token, safe="")
    except Exception:  # noqa: BLE001 - the footer must never break a page
        return ""
    return ("<footer class='docfoot'><a href='/guide" + qs
            + "'>Documentation</a></footer>")


def _page(title, body, head=""):
    return (
        "<!doctype html><html><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width,initial-scale=1'>"
        "<title>" + _esc(title) + "</title>"
        "<style>" + _STATUS_CSS + "</style>" + head + "</head><body>"
        + body + _doc_footer() + "</body></html>"
    )


# -- a tiny, safe Markdown renderer for the shipped user guide (/guide) -------
_MD_LINK = re.compile(r"\[([^\]]+)\]\(([^)]+)\)")
_MD_BOLD = re.compile(r"\*\*(\S(?:[^*]*\S)?)\*\*")
_MD_ITALIC = re.compile(r"\*(\S(?:[^*]*\S)?)\*")
# A GitHub-style table separator row, e.g. ``| --- | :--: |``.
_MD_TABLE_SEP = re.compile(r"^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-+:?\s*)+\|?\s*$")


def _md_link_sub(match):
    label, url = match.group(1), match.group(2)
    # url has already been through _esc(); only allow benign schemes so a
    # javascript:/data: link in the guide source renders as plain text.
    if url.strip().lower().startswith(("http://", "https://", "#", "/", "mailto:")):
        return '<a href="' + url + '">' + label + "</a>"
    return label


def _md_inline(text):
    """Inline Markdown on one line: escape first, then apply inline code, links,
    bold and italic. Inline-code spans are stashed so their contents are not
    re-formatted or treated as links."""
    text = _esc(text)
    spans = []

    def _stash(match):
        spans.append(match.group(1))
        return "\x00%d\x00" % (len(spans) - 1)

    text = re.sub(r"`([^`]+)`", _stash, text)
    text = _MD_LINK.sub(_md_link_sub, text)
    text = _MD_BOLD.sub(r"<strong>\1</strong>", text)
    text = _MD_ITALIC.sub(r"<em>\1</em>", text)
    return re.sub(r"\x00(\d+)\x00",
                  lambda m: "<code>" + spans[int(m.group(1))] + "</code>", text)


def _md_slug(text):
    """A GitHub-ish anchor slug for a heading, so in-page [x](#slug) links work:
    lowercase, inline-markup chars dropped, spaces to hyphens."""
    s = re.sub(r"[`*]", "", text).strip().lower()
    s = re.sub(r"[^a-z0-9\s-]", "", s)
    return re.sub(r"\s+", "-", s)


def _md_cells(row):
    row = row.strip().replace("\\|", "\x00")   # protect escaped pipes in a cell
    if row.startswith("|"):
        row = row[1:]
    if row.endswith("|"):
        row = row[:-1]
    return [c.strip().replace("\x00", "|") for c in row.split("|")]


def _md_breaks_para(line):
    s = line.strip()
    return bool(s.startswith(("```", "#"))
                or re.match(r"[-*]\s+", s) or re.match(r"\d+\.\s+", s)
                or re.fullmatch(r"-{3,}", s))


def _render_markdown(md):
    """Render the guide's Markdown subset to HTML. Supports #/##/### headings,
    paragraphs, ``-``/``1.`` single-level lists, ``` fenced code, `inline code`,
    **bold**, *italic*, [text](url) links (http/https/#/mailto/relative only),
    ``---`` rules, and simple pipe tables (emitted with ``data-label`` cells so
    the shared mobile CSS reflows them to cards). Everything is escaped."""
    lines = md.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    out = []
    i, n = 0, len(lines)
    while i < n:
        line = lines[i]
        stripped = line.strip()

        if stripped.startswith("```"):              # fenced code
            i += 1
            buf = []
            while i < n and not lines[i].strip().startswith("```"):
                buf.append(lines[i])
                i += 1
            i += 1                                  # closing fence (or EOF)
            out.append("<pre><code>" + _esc("\n".join(buf)) + "</code></pre>")
            continue

        if not stripped:                            # blank
            i += 1
            continue

        if re.fullmatch(r"-{3,}", stripped):        # horizontal rule
            out.append("<hr>")
            i += 1
            continue

        m = re.match(r"(#{1,3})\s+(.*)$", stripped)  # heading
        if m:
            lvl = len(m.group(1))
            out.append('<h%d id="%s">%s</h%d>'
                       % (lvl, _md_slug(m.group(2)), _md_inline(m.group(2)), lvl))
            i += 1
            continue

        if "|" in line and i + 1 < n and _MD_TABLE_SEP.match(lines[i + 1]):  # table
            headers = _md_cells(line)
            i += 2
            rows = []
            while i < n and lines[i].strip() and "|" in lines[i]:
                rows.append(_md_cells(lines[i]))
                i += 1
            thead = "".join("<th>" + _md_inline(h) + "</th>" for h in headers)
            trs = []
            for cells in rows:
                tds = []
                for idx, cell in enumerate(cells):
                    label = headers[idx] if idx < len(headers) else ""
                    tds.append('<td data-label="' + _esc(label) + '">'
                               + _md_inline(cell) + "</td>")
                trs.append("<tr>" + "".join(tds) + "</tr>")
            out.append("<table><thead><tr>" + thead + "</tr></thead><tbody>"
                       + "".join(trs) + "</tbody></table>")
            continue

        if re.match(r"[-*]\s+", stripped):          # unordered list
            items = []
            while i < n and re.match(r"[-*]\s+", lines[i].strip()):
                items.append("<li>" + _md_inline(
                    re.sub(r"^[-*]\s+", "", lines[i].strip())) + "</li>")
                i += 1
            out.append("<ul>" + "".join(items) + "</ul>")
            continue

        if re.match(r"\d+\.\s+", stripped):         # ordered list
            items = []
            while i < n and re.match(r"\d+\.\s+", lines[i].strip()):
                items.append("<li>" + _md_inline(
                    re.sub(r"^\d+\.\s+", "", lines[i].strip())) + "</li>")
                i += 1
            out.append("<ol>" + "".join(items) + "</ol>")
            continue

        para = []                                   # paragraph
        while i < n and lines[i].strip() and not _md_breaks_para(lines[i]):
            para.append(lines[i].strip())
            i += 1
        out.append("<p>" + " ".join(_md_inline(p) for p in para) + "</p>")
    return "".join(out)


# The guide is one Markdown source (userguide.GUIDE_MD) but is served as an
# indexed set of pages: /guide is a table of contents, /guide/<section> shows
# one top-level (##) section, and /guide/all is the whole guide on one page
# (handy for searching or printing). Splitting keeps each page short on a phone
# over a slow Bluetooth link instead of one long scroll.
_GUIDE_FULL_SLUG = "all"
_HREF_ANCHOR = re.compile(r'href="#([a-z0-9-]+)"')
_GUIDE_MODEL = None


def _guide_model():
    """Parse GUIDE_MD once into (preamble_md, sections, anchors). ``sections`` is
    an ordered list of ``{slug, title, body, subs}`` split at ``##`` headings
    (``subs`` are the section's ``###`` ``(slug, title)`` pairs). ``anchors`` maps
    every heading slug (## and ###) to the slug of the section that contains it,
    so a cross-section ``[x](#anchor)`` link can be pointed at the right page."""
    global _GUIDE_MODEL
    if _GUIDE_MODEL is not None:
        return _GUIDE_MODEL
    from .userguide import GUIDE_MD
    lines = GUIDE_MD.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    preamble, sections, anchors = [], [], {}
    cur = None
    in_code = False
    for line in lines:
        s = line.strip()
        if s.startswith("```"):
            in_code = not in_code
            (cur["body"] if cur else preamble).append(line)
            continue
        m = None if in_code else re.match(r"(#{2,3})\s+(.*)$", s)
        if m:
            title = m.group(2)
            slug = _md_slug(title)
            if len(m.group(1)) == 2:               # ## -> a new top-level section
                cur = {"slug": slug, "title": title, "body": [line], "subs": []}
                sections.append(cur)
                anchors[slug] = slug
                continue
            if cur is not None:                     # ### -> subsection of current
                cur["subs"].append((slug, title))
                anchors[slug] = cur["slug"]
        (cur["body"] if cur else preamble).append(line)
    _GUIDE_MODEL = ("\n".join(preamble), sections, anchors)
    return _GUIDE_MODEL


def _guide_resolve_links(html_body, current_slug, anchors, control):
    """Rewrite in-page ``#anchor`` links that belong to another section so they
    open that section's page; anchors in the current section stay in-page."""
    def repl(m):
        slug = m.group(1)
        owner = anchors.get(slug)
        if owner and owner != current_slug:
            return ('href="' + _esc(_url("/guide/" + owner, control))
                    + "#" + slug + '"')
        return m.group(0)
    return _HREF_ANCHOR.sub(repl, html_body)


def _render_guide(control):
    """The guide landing page: a table of contents linking into each section."""
    preamble, sections, _ = _guide_model()
    back = '<p><a href="' + _esc(_url("/", control)) + '">&larr; Dashboard</a></p>'
    items = []
    for sec in sections:
        href = _esc(_url("/guide/" + sec["slug"], control))
        subs = "".join('<li><a href="' + href + "#" + sslug + '">'
                       + _md_inline(stitle) + "</a></li>"
                       for sslug, stitle in sec["subs"])
        subs = ("<ul>" + subs + "</ul>") if subs else ""
        items.append('<li><a href="' + href + '">' + _md_inline(sec["title"])
                     + "</a>" + subs + "</li>")
    full = _esc(_url("/guide/" + _GUIDE_FULL_SLUG, control))
    toc = ("<nav class='guideindex'><ol>" + "".join(items) + "</ol></nav>"
           + '<p class="guidefull"><a href="' + full
           + '">Open the whole guide on one page</a> (handy for searching).</p>')
    return _page("Documentation - Kestrel",
                 back + "<article class='guide'>"
                 + _render_markdown(preamble) + toc + "</article>")


def _render_guide_full(control):
    """The whole guide on one page (search/print), with a back-to-contents link."""
    from .userguide import GUIDE_MD
    back = ('<p><a href="' + _esc(_url("/guide", control))
            + '">&larr; All topics</a></p>')
    return _page("Documentation - Kestrel",
                 back + "<article class='guide'>"
                 + _render_markdown(GUIDE_MD) + "</article>")


def _render_guide_section(slug, control):
    """One ``##`` section as its own page, with back links and prev/next
    navigation. Returns ``None`` if the slug names no section."""
    _preamble, sections, anchors = _guide_model()
    idx = next((k for k, s in enumerate(sections) if s["slug"] == slug), None)
    if idx is None:
        return None
    sec = sections[idx]
    back = ('<p><a href="' + _esc(_url("/guide", control))
            + '">&larr; All topics</a> &middot; <a href="'
            + _esc(_url("/", control)) + '">Dashboard</a></p>')
    body = _guide_resolve_links(_render_markdown("\n".join(sec["body"])),
                                slug, anchors, control)
    if idx > 0:
        prev = sections[idx - 1]
        left = ('<a href="' + _esc(_url("/guide/" + prev["slug"], control))
                + '">&larr; ' + _md_inline(prev["title"]) + "</a>")
    else:
        left = "<span></span>"
    if idx < len(sections) - 1:
        nxt = sections[idx + 1]
        right = ('<a href="' + _esc(_url("/guide/" + nxt["slug"], control))
                 + '">' + _md_inline(nxt["title"]) + " &rarr;</a>")
    else:
        right = "<span></span>"
    return _page("Documentation - Kestrel",
                 back + "<article class='guide'>" + body
                 + "<nav class='guidenav'>" + left + right + "</nav></article>")


def _url(path, control):
    return path + ((control or {}).get("qs") or "")


def _manage_url(name, control):
    return _url("/manage/" + quote(str(name), safe=""), control)


def _csrf_field(control):
    return '<input type="hidden" name="csrf" value="' + _esc(control["csrf"]) + '">'


_STATE_CLASS = {"loaded": "s-on", "failed": "s-err"}


_LEADING_NUM = re.compile(r"-?\d+(?:\.\d+)?")


def _parse_ratio(value):
    """The leading number of ``value`` as an int clamped to 0-100, for a
    percent/gauge bar, or None when it doesn't start with a number:
    ``'42.0%'`` -> 42, ``'55C'`` -> 55, ``'-'`` -> None."""
    match = _LEADING_NUM.match(str(value).strip())
    if not match:
        return None
    try:
        num = float(match.group(0))
    except ValueError:
        return None
    return int(round(min(100.0, max(0.0, num))))


def _wbr(text):
    """Insert ``<wbr>`` break opportunities after host/URL/path delimiters so a
    long value (e.g. ``user@host.example.net:/srv/path``) wraps at the @/./:/
    boundaries instead of mid-word, keeping a card's value column tidy. Operates
    on already-escaped text: ``<wbr>`` is the only markup added, and ``_esc``
    produces no entity containing these delimiters. A ``.`` between digits is
    left alone, so decimals like ``393.7`` and versions like ``0.4.8`` are not
    split."""
    text = re.sub(r"([@:/])", r"\1<wbr>", text)
    return re.sub(r"\.(?!\d)", ".<wbr>", text)


def _render_widgets(widgets, plugins=None, plugins_url="/plugins"):
    """Per-plugin readout cards from the grouped UI element model
    (``{plugin: [{name,label,value,kind}, ...]}``). Values are escaped; a
    ``percent``/``gauge`` element adds a bar from the numeric part of its value.
    A plugin that is quarantined/failed (from the ``plugins`` health list) gets a
    subtle "stopped" badge linking to the Plugins page, where the reason shows.
    Returns an empty string when no plugin registered any element."""
    health = {p.get("name"): p for p in (plugins or [])}
    cards = []
    for plugin in sorted(widgets or {}):
        elements = widgets[plugin] or []
        if not elements:
            continue
        rows = []
        for element in elements:
            label = _esc(element.get("label") or element.get("name") or "")
            rows.append("<dt>" + label + "</dt><dd>"
                        + _wbr(_esc(_shown(element.get("value")))) + "</dd>")
            if element.get("kind") in ("percent", "gauge"):
                ratio = _parse_ratio(element.get("value"))
                if ratio is not None:
                    mark = _parse_ratio(element.get("mark"))
                    marker = ""
                    if mark is not None:
                        # A reference marker on the bar (e.g. a battery gauge's
                        # shutdown threshold). The value it marks is also shown as
                        # text, so this is never the only signal.
                        marker = ('<i class="mark" style="left:' + str(mark)
                                  + '%" aria-hidden="true"></i>')
                    rows.append('<div class="bar"><span style="width:'
                                + str(ratio) + '%"></span>' + marker + "</div>")
        h = health.get(plugin) or {}
        badge = attr = ""
        if h.get("state") == "failed":
            # Quarantined (or otherwise stopped): a subtle badge on the card so
            # the operator sees it on the dashboard, linking to the Plugins page
            # where the reason is shown in full.
            reason = h.get("reason") or "stopped"
            badge = ('<a class="wbadge" href="' + _esc(plugins_url)
                     + '" title="' + _esc(reason) + '">stopped</a>')
            attr = ' data-state="stopped"'
        cards.append('<div class="widget"' + attr + ' data-plugin="'
                     + _esc(plugin) + '"><h3>' + _esc(plugin) + badge
                     + "</h3><dl>" + "".join(rows) + "</dl></div>")
    if not cards:
        return ""
    # No section heading: each card is titled with its plugin name, so a
    # "widgets" label above them is redundant.
    return '<div class="widgets">' + "".join(cards) + "</div>"


def _capture_toggle(cap, control):
    """The Enable/Disable capture button for the summary, when controls are on.
    It points by the live service state: Disable when capture is running, Enable
    otherwise (so a config-enabled-but-not-running capture, e.g. no dongle, still
    offers Enable to retry). Posts to ``/capture`` with CSRF; the ack step is
    handled there."""
    if control is None or not control.get("ready"):
        return ""
    action = "disable" if cap.get("state") == "running" else "enable"
    btn_class = ' class="danger"' if action == "disable" else ""
    return (
        '<form class="inline" method="post" action="/capture'
        + _esc(control.get("qs", "")) + '">' + _csrf_field(control)
        + '<input type="hidden" name="action" value="' + action + '">'
        + '<button type="submit"' + btn_class + ">" + action.capitalize()
        + " capture</button></form>"
    )


# The two device-power actions, with the words each page needs. Shut down is the
# destructive one (the device will not come back on its own), so it is red.
_POWER_WORDS = {
    "reboot": {
        "title": "Reboot",
        "verb": "reboot",
        "gerund": "Rebooting",
        "button": "",
        "warn": "The device will restart. This dashboard will be unreachable for "
                "a short while, then come back on its own.",
        "done": "The device is rebooting. Reload this page once it has come back up.",
    },
    "shutdown": {
        "title": "Shut down",
        "verb": "shut down",
        "gerund": "Shutting down",
        "button": ' class="danger"',
        "warn": "The device will power off and will NOT come back on its own - "
                "you will need to remove and restore power to start it again.",
        "done": "The device is shutting down. Remove and restore power to turn it "
                "on again.",
    },
}


def _power_controls(control):
    """The Reboot and Shut down buttons for the dashboard, shown only when web
    auth is on (read-only otherwise, like the capture toggle). Each posts to
    ``/power`` with CSRF; the first post lands on a confirm page, so a stray tap
    never powers the device down. The daemon runs as ``pi`` and can't power the
    device off itself, so the apply goes through the same root helper socket as
    the capture/plugin controls (a ``power`` target)."""
    if control is None or not control.get("ready"):
        return ""
    qs = _esc(control.get("qs", ""))
    buttons = []
    for action in ("reboot", "shutdown"):
        words = _POWER_WORDS[action]
        buttons.append(
            '<form method="post" action="/power' + qs + '">' + _csrf_field(control)
            + '<input type="hidden" name="action" value="' + action + '">'
            + '<button type="submit"' + words["button"] + ">"
            + _esc(words["title"]) + "</button></form>"
        )
    return ('<p class="pwrlabel">Device power</p><div class="power">'
            + "".join(buttons) + "</div>")


def _read_update_state(path=UPDATE_STATE_FILE):
    """The `updates` plugin's shared state, or {} if it hasn't run / isn't on."""
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return {}


def _read_update_run_state(path=UPDATE_RUN_STATE_FILE):
    """The update engine's run status (root-owned but world-readable), or {} if
    no in-place update has ever run. Carries ``status`` (ok|error|running) and a
    plain-language ``detail`` for the most recent attempt - the only record of
    *why* an update failed, which the dashboard otherwise never showed."""
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _update_failure(run_state):
    """Classify the last update attempt for the dashboard, or ``None`` when the
    most recent run did not fail. Returns ``{detail, verdict, backup}`` where
    ``backup`` is True for the pre-update-backup class (safe to retry, and the
    'update without a fresh backup' path applies) and ``verdict`` is the plain
    retry guidance keyed to the failure class."""
    if (run_state or {}).get("status") != "error":
        return None
    detail = str(run_state.get("detail") or "").strip()
    low = detail.lower()
    if "backup before updating" in low:
        return {"detail": detail, "backup": True,
                "verdict": "The device was not changed. Retry when the backup "
                           "host is reachable, or update without a fresh backup."}
    if "restart failed" in low or "rolled back" in low or "interrupted" in low:
        return {"detail": detail, "backup": False,
                "verdict": "The previous version was restored. The new version "
                           "did not come up, so retrying the same release may "
                           "fail again - check diagnostics before retrying."}
    if ("could not fetch" in low or "commit mismatch" in low
            or "verify" in low or "download" in low):
        return {"detail": detail, "backup": False,
                "verdict": "Nothing was changed. This is usually a transient "
                           "network problem - safe to retry."}
    return {"detail": detail, "backup": False,
            "verdict": "Nothing was changed; safe to retry."}


def _update_controls(control):
    """The release-update card: an 'Upgrade to X' button when the updates plugin
    found a newer, app-layer-updatable release, or a plain 'needs reflash' note
    when the newer release changed the base OS. Shown only when web auth is on
    (read-only otherwise) and the updates plugin is enabled (it writes the shared
    state). The upgrade posts to /update with CSRF; the first post lands on a
    confirm page. The daemon runs as pi, so the apply goes through the same root
    helper socket as the other controls (an `update` target)."""
    if control is None or not control.get("ready"):
        return ""
    if not control.get("enabled", {}).get("updates"):
        return ""
    state = _read_update_state()
    if not state.get("update_available"):
        return ""
    latest = _esc(state.get("latest") or "a newer release")
    qs = _esc(control.get("qs", ""))
    # A discreet "What's new" link to the merged release notes between the
    # installed version and the latest one. Shown whenever the plugin captured
    # notes (or at least a release URL to fall back to), as a muted secondary
    # link so it never competes with the upgrade button.
    notes_link = ""
    if state.get("notes") or state.get("notes_url"):
        notes_link = ('<p class="advlink"><a href="/update/notes' + qs + '">'
                      + "What's new in " + latest + "?</a></p>")
    if not state.get("offerable"):
        reason = _esc(state.get("reason") or "it changed the base OS")
        return ('<div class="card pad"><h3>Update available</h3><p>Version ' + latest
                + ' is available, but it needs a reflash (' + reason
                + "). It can't be applied in place.</p>" + notes_link + "</div>")

    # If the last in-place attempt failed, say so and why, with retry guidance
    # keyed to the failure class - otherwise the card just silently re-offers
    # the same upgrade that already failed.
    fail = _update_failure(_read_update_run_state())
    fail_html = ""
    retry_no_backup = ""
    if fail:
        fail_html = ('<div class="msg err"><strong>The last update attempt '
                     'failed.</strong><br>' + _esc(fail["detail"]) + "<br>"
                     + _esc(fail["verdict"]) + "</div>")
        if fail["backup"]:
            # A one-tap retry that skips the pre-update backup (lands on the
            # confirm page with the skip box ticked).
            retry_no_backup = (
                '<form method="post" action="/update' + qs + '">'
                + _csrf_field(control)
                + '<input type="hidden" name="action" value="apply">'
                + '<input type="hidden" name="no_backup" value="yes">'
                + '<button type="submit" class="secondary">Retry without a '
                  "fresh backup</button></form>")

    verb = "Retry" if fail else "Upgrade to " + latest
    return ('<div class="card pad"><h3>Update available</h3>' + fail_html
            + '<p>Version ' + latest + ' can be installed in place.</p>'
            + '<form method="post" action="/update' + qs + '">' + _csrf_field(control)
            + '<input type="hidden" name="action" value="apply">'
            + '<button type="submit">' + verb + "</button></form>"
            + retry_no_backup + notes_link + "</div>")


def _bt_pair_control(status, control):
    """A discreet "pair a phone" control, shown only when **no** phone is paired
    (and web auth is on). Deliberately small - a single secondary link-button, so
    it does not dominate the dashboard. It posts to ``/bt-pair`` (CSRF, with a
    confirm step) rather than linking, since it changes device state; the root
    helper runs the same ``kestrel network bt-pair`` the CLI does. Shown
    only when unpaired, both because that is when it is useful and because it
    then cannot drop a BT PAN link the viewer is on."""
    if control is None or not control.get("ready"):
        return ""
    bt = status.get("bluetooth") or {}
    if bt.get("paired"):
        return ""
    qs = _esc(control.get("qs", ""))
    return ('<form class="btpair" method="post" action="/bt-pair' + qs + '">'
            + _csrf_field(control)
            + '<button type="submit">No phone paired &middot; pair over Bluetooth'
            + "</button></form>")


def _action_button(name, action, control):
    """One declared plugin row button (from the plugin's ``__actions__``). A
    GET action navigates to the plugin's own page (``/plugins/<name>/<path>``,
    carrying the token so token-auth navigation stays signed); a POST action is
    a state change and carries the CSRF token. The button label is plugin text,
    so it is escaped. The caller only renders these when web auth is on and the
    plugin is enabled, so this helper does no further gating."""
    if control is None:
        return ""
    path = action.get("path") or ""
    target = "/plugins/" + name + "/" + path
    label = _esc(action.get("label") or "")
    if (action.get("method") or "get").lower() == "post":
        qs = _esc(control.get("qs", ""))
        return ('<form class="inline rowaction" method="post" action="'
                + _esc(target) + qs + '">' + _csrf_field(control)
                + '<button type="submit" class="rowaction">' + label
                + '</button></form>')
    token = control.get("token") or ""
    hidden = ('<input type="hidden" name="token" value="' + _esc(token) + '">'
              if token else "")
    return ('<form class="inline rowaction" method="get" action="' + _esc(target)
            + '">' + hidden
            + '<button type="submit" class="rowaction">' + label
            + '</button></form>')


def _agent_controls(agent, control):
    """The learning-agent controls for the ``/agent`` config page: a two-choice
    policy radio group, an algorithm ``<select>`` shown only for the "learned"
    policy, and a Save / Enable-Disable row. All POST to ``/agent`` with CSRF;
    the root apply helper enum-validates and (for a running agent) restarts the
    service. Returns a read-only line when web auth is off, and "" when there is
    no agent state at all."""
    if not agent:
        return ""
    if control is None or not control.get("ready"):
        return ('<p class="hint">The dashboard is read-only. Log in '
                "(enable web auth) to change the agent.</p>")
    qs = _esc(control.get("qs", ""))
    cur_algo = agent.get("algorithm") or "RecurrentPPO"
    cur_policy = agent.get("policy") or "baseline"
    enabled = bool(agent.get("enabled"))

    radios = []
    for v in AGENT_POLICIES:
        desc = _AGENT_POLICY_DESC.get(v, "")
        checked = " checked" if v == cur_policy else ""
        radios.append(
            '<label class="radio"><input type="radio" name="policy" value="'
            + _esc(v) + '"' + checked + '> <span class="rlabel">'
            + _esc(v.capitalize()) + "</span>"
            + ('<span class="rdesc">' + _esc(desc) + "</span>" if desc else "")
            + "</label>")
    policy_group = ('<p class="pwrlabel">Policy</p><div class="radios">'
                    + "".join(radios) + "</div>")

    options = "".join(
        '<option value="' + _esc(a) + '"' + (" selected" if a == cur_algo else "")
        + ">" + _esc(a) + "</option>" for a in AGENT_ALGORITHMS)
    # Shown only for the learned policy; a small inline script (added by
    # _render_agent) toggles it on the radio choice. Default-visible so with JS
    # off it is always reachable (and the value is harmless under baseline).
    algo_hidden = "" if cur_policy == "learned" else " hidden"
    algo_wrap = ('<div class="agent-algo" id="agent-algo"' + algo_hidden + ">"
                 '<label for="agent-algo-sel">Algorithm</label>'
                 '<select id="agent-algo-sel" name="algorithm">' + options
                 + "</select></div>")

    action = "disable" if enabled else "enable"
    btn_class = ' class="danger"' if action == "disable" else ""
    # A "Train now" button, only while the agent is enabled (its process must be
    # running to pick the request up). It's disabled with a one-line reason when
    # the agent says it can't train yet (a run in progress, no episode data, or
    # low memory / too hot), read live from the agent's status.
    st = agent.get("status") or {}
    train_btn = ""
    train_hint = ""
    if enabled:
        if st.get("training"):
            train_btn = ('<button type="submit" name="action" value="train" '
                         "disabled>Training&hellip;</button>")
        elif st.get("can_train"):
            train_btn = ('<button type="submit" name="action" value="train">'
                         "Train now</button>")
        else:
            train_btn = ('<button type="submit" name="action" value="train" '
                         "disabled>Train now</button>")
            reason = st.get("train_blocked") or "not ready to train right now"
            train_hint = ('<p class="hint">Training unavailable: '
                          + _esc(reason) + "</p>")
    # On-device training on/off. A separate little form so saving it is one
    # clear action, independent of the policy/algorithm save. "On" maps to the
    # board-aware default ("auto": trains on >= ~4 GB boards, never on smaller
    # ones); "off" never trains. The radio reflects the configured [agent] train
    # (off vs anything-else), so an "idle"/"auto" setting both read as On.
    cur_train = agent.get("train") or "auto"
    train_off = cur_train == "off"
    train_radios = (
        '<label class="radio"><input type="radio" name="train_mode" value="on"'
        + ("" if train_off else " checked")
        + '> <span class="rlabel">On</span>'
        + '<span class="rdesc">Fine-tune on-device when the board can handle it'
        + " (about 4 GB RAM or more; smaller boards never train).</span></label>"
        + '<label class="radio"><input type="radio" name="train_mode" value="off"'
        + (" checked" if train_off else "")
        + '> <span class="rlabel">Off</span>'
        + '<span class="rdesc">Never train on-device. Inference still runs;'
        + " use remote training instead.</span></label>")
    training_form = (
        '<form method="post" action="/agent' + qs + '" class="card agentform">'
        + _csrf_field(control)
        + '<p class="pwrlabel">On-device training</p><div class="radios">'
        + train_radios
        + '</div><div class="agentbtns">'
        + '<button type="submit" name="action" value="train_mode">Save</button>'
        + "</div>"
        + '<p class="hint">Turns the periodic on-device fine-tune on or off.'
        + " Off lets a remote trainer's model win without the device"
        + " out-training it. Changing this restarts the agent.</p></form>")
    return (
        '<form method="post" action="/agent' + qs + '" class="card agentform">'
        + _csrf_field(control) + policy_group + algo_wrap
        + '<div class="agentbtns">'
        + '<button type="submit" name="action" value="save">Save</button>'
        + '<button type="submit" name="action" value="' + action + '"' + btn_class
        + ">" + action.capitalize() + " agent</button>"
        + train_btn + "</div>" + train_hint
        + '<p class="hint">The agent only runs while capture is on, and never '
        "rewrites your scan config. Switching algorithm restarts it. Training "
        "runs in the background on-device and won't interrupt capture.</p></form>"
        + training_form
    )


# The /agent page's only client-side JS: reveal the Algorithm field only for the
# "learned" policy. Dependency-free and degrades gracefully - with JS off the
# field stays visible (its value is harmless under baseline). Assembled by
# concatenation (literal {} would break any later str.format).
_AGENT_REVEAL_JS = (
    "<script>(function(){"
    "var algo=document.getElementById('agent-algo');if(!algo)return;"
    "var r=document.getElementsByName('policy');"
    "var sync=function(){var learned=false,i;"
    "for(i=0;i<r.length;i++){if(r[i].checked&&r[i].value==='learned')learned=true;}"
    "algo.hidden=!learned;};"
    "for(var i=0;i<r.length;i++){r[i].addEventListener('change',sync);}"
    "sync();})();</script>"
)

# Auto-refresh used ONLY while the agent is starting up (enabled but no readout
# published yet), so the /agent page flips to the live dashboard on its own once
# the agent's first status lands. Paused while the tab is hidden. The page is not
# refreshed in any other state, so the control form is never reloaded under the
# user mid-edit (per docs/developer/web-ui-conventions.md).
_AGENT_STARTING_JS = (
    "<script>(function(){setInterval(function(){"
    "if(!document.hidden){location.reload();}},4000);})();</script>"
)


def _agent_nav(agent, cap, control):
    """The dashboard's "AI Agent" nav card: a compact tappable card (like the
    Plugins card) with a one-line state summary, linking to the /agent page.
    Keeps the agent's controls and readout off the main status view."""
    agent = agent or {}
    cap = cap or {}
    enabled = bool(agent.get("enabled"))
    policy = agent.get("policy") or "baseline"
    algo = agent.get("algorithm") or "RecurrentPPO"
    training = bool((agent.get("status") or {}).get("training"))
    if enabled:
        summ = "On &middot; " + _esc(policy)
        if policy == "learned":
            summ += " (" + _esc(algo) + ")"
        # Enabled but idle: the agent is capture-gated, so it does not drive
        # while capture is off. Say so here rather than reading as plain "On".
        if not cap.get("enabled"):
            summ += " &middot; idle (capture off)"
        if training:
            summ += " &middot; training…"
    else:
        summ = "Off"
    return (
        '<a class="navcard" href="' + _esc(_url("/agent", control)) + '">'
        '<div class="scol"><span class="skey">AI Agent</span>'
        '<span class="sval">' + summ + "</span></div>"
        '<span class="arrow" aria-hidden="true">&rarr;</span></a>'
    )


_MAC_RE = re.compile(r"^([0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}$")


def _is_mac(value):
    return bool(_MAC_RE.match((value or "").strip()))


def _whitelist_nav(cap, control):
    """The dashboard's "Whitelist" nav card, placed directly below Captured
    networks: a count of the protected APs, linking to the /whitelist page."""
    try:
        count = int((cap or {}).get("whitelist", 0))
    except (TypeError, ValueError):
        count = 0
    val = "none yet" if not count else ("%d protected AP(s)" % count)
    return (
        '<a class="navcard" href="' + _esc(_url("/whitelist", control)) + '">'
        '<div class="scol"><span class="skey">Whitelist</span>'
        '<span class="sval">' + val + "</span>"
        '<span class="smeta">APs never actively attacked</span></div>'
        '<span class="arrow" aria-hidden="true">&rarr;</span></a>'
    )


def _render_agent(status, control=None, message=None, error=False):
    """The AI agent page, reached from the dashboard's "AI Agent" nav card: the
    agent's live readout card (the same one the dashboard used to inline) plus
    its controls. Kept off the dashboard so the agent does not crowd the main
    status view."""
    agent = status.get("agent") or {}
    cap = status.get("capture") or {}
    widgets = status.get("widgets") or {}
    elements = widgets.get(AGENT_CARD_NAME)
    # Just after enabling, the agent is enabled in config but its own process is
    # still coming up (loading its model - several seconds), so it hasn't
    # published a readout yet. Show a "starting" note and auto-refresh *only* in
    # that transient state, so the page flips to the agent dashboard on its own
    # once the readout lands - without the user having to leave and come back.
    # The page isn't auto-refreshed otherwise (it carries the control form).
    starting = bool(agent.get("enabled")) and not elements
    if elements:
        readout = _render_widgets({AGENT_CARD_NAME: elements})
    elif starting:
        readout = ('<p class="count">The agent is starting up (loading its '
                   "model); this page refreshes on its own and the readout will "
                   "appear in a moment.</p>")
    else:
        readout = ('<p class="count">The agent has not run yet, so there is '
                   "nothing to show. Enable it below (capture must be on) to "
                   "start it.</p>")
    banner = ""
    if message:
        banner = ('<p class="msg' + (" err" if error else "") + '">'
                  + _esc(message) + "</p>")
    # Enabled but capture off: the agent idles (does not drive) until capture is
    # back on, so it shouldn't read as "running" here. The readout card already
    # shows State: stopped with a "capture disabled; agent idle" note; this adds
    # the one actionable line.
    capture_off = ""
    if bool(agent.get("enabled")) and not cap.get("enabled"):
        capture_off = ('<p class="hint">The agent is enabled but idle because '
                       "capture is off. Turn capture on from the dashboard to "
                       "let it run.</p>")
    controls = _agent_controls(agent, control)
    back = '<p><a href="' + _esc(_url("/", control)) + '">&larr; Dashboard</a></p>'
    tail = _AGENT_REVEAL_JS + (_AGENT_STARTING_JS if starting else "")
    return _page(
        "AI agent - Kestrel",
        back + "<h1>AI agent</h1>" + banner + capture_off + readout + controls + tail,
    )


def _render_addresses(addresses):
    """A light, native ``<details>`` disclosure of the management-link addresses
    (the IPs the dashboard is reachable at). ``addresses`` is a list of
    ``(iface, ip-or-None)``; an interface with no address reads as not connected.
    Returns an empty string when nothing was passed."""
    if not addresses:
        return ""
    rows = []
    for iface, ip in addresses:
        shown = _esc(ip) if ip else '<span class="smeta">not connected</span>'
        rows.append("<dt>" + _esc(iface) + "</dt><dd>" + shown + "</dd>")
    return ('<details class="addrs"><summary>Network addresses</summary><dl>'
            + "".join(rows) + "</dl></details>")


# The dashboard's only client-side JS, inline and dependency-free (no framework,
# no build step). Two progressive enhancements: (1) drag-to-reorder the plugin
# widget cards via a grip handle, with the order saved per-browser in
# localStorage (works with mouse and touch via pointer events); (2) a self-reload
# on the configured interval, paused while dragging or when the tab is hidden.
# With JS off the cards still render in default order and nothing breaks.
#
# The pointer is captured on the grid (which never moves), not on the grip:
# reordering moves the dragged card in the DOM, and iOS Safari releases pointer
# capture held by a moved element, which froze touch drags on iPhone. Capturing
# on the stable grid and listening there keeps the move stream alive across the
# reorder. touch-action:none on the grip (in the CSS) stops the drag from
# scrolling the page.
#
# Two phone-specific touches: (1) while dragging, .dragging on the grid disables
# text selection and the iOS callout across every card (CSS), so dragging past a
# card never highlights its text. (2) because touch-action:none means the page
# no longer scrolls under a drag, an edge auto-scroll kicks in when the finger
# nears the top/bottom of the viewport, so a card can be moved past the fold on
# a long phone list; it runs on a timer (not just pointermove) so holding still
# at the edge keeps scrolling, and getBoundingClientRect/clientY are both
# viewport-relative so the insert point stays correct as the page scrolls.
_REORDER_JS = """
var grid=document.querySelector('.widgets');
if(grid){
  var KEY='pwn-widgets-order';
  var cards=function(){return Array.prototype.slice.call(grid.querySelectorAll('.widget'));};
  var save=function(){try{localStorage.setItem(KEY,JSON.stringify(cards().map(function(c){return c.getAttribute('data-plugin');})));}catch(e){}};
  (function(){var order;try{order=JSON.parse(localStorage.getItem(KEY)||'[]')||[];}catch(e){return;}
    if(!order.length)return;var placed={};
    order.forEach(function(n){var sel='.widget[data-plugin="'+((window.CSS&&CSS.escape)?CSS.escape(n):n)+'"]';var el=grid.querySelector(sel);if(el){grid.appendChild(el);placed[n]=1;}});
    cards().forEach(function(c){if(!placed[c.getAttribute('data-plugin')])grid.appendChild(c);});
  })();
  cards().forEach(function(c){var g=document.createElement('span');g.className='grip';g.textContent='\\u283f';g.setAttribute('title','Drag to reorder');g.setAttribute('aria-hidden','true');c.appendChild(g);});
  var scroll=0,timer=null;
  var tick=function(){if(scroll&&dragging)window.scrollBy(0,scroll);};
  var reorder=function(y){var cs=cards();var ref=null;for(var i=0;i<cs.length;i++){var c=cs[i];if(c===dragging)continue;var r=c.getBoundingClientRect();if(y<r.top+r.height/2){ref=c;break;}}grid.insertBefore(dragging,ref);};
  var onMove=function(e){var h=window.innerHeight,y=e.clientY,M=70;
    scroll=y<M?-Math.ceil((M-y)/5):(y>h-M?Math.ceil((y-(h-M))/5):0);
    reorder(y);};
  var end=function(e){if(!dragging)return;try{grid.releasePointerCapture(e.pointerId);}catch(_){}
    if(timer){clearInterval(timer);timer=null;}scroll=0;
    dragging.classList.remove('dragging');grid.classList.remove('dragging');dragging=null;save();};
  grid.addEventListener('pointerdown',function(e){var g=e.target.closest('.grip');if(!g)return;dragging=g.parentNode;e.preventDefault();dragging.classList.add('dragging');grid.classList.add('dragging');try{grid.setPointerCapture(e.pointerId);}catch(_){}if(!timer)timer=setInterval(tick,16);});
  grid.addEventListener('pointermove',function(e){if(dragging){e.preventDefault();onMove(e);}});
  grid.addEventListener('pointerup',end);
  grid.addEventListener('pointercancel',end);
}
"""

_REFRESH_JS = ("if(REFRESH>0){setInterval(function(){"
               "if(!dragging&&!document.hidden){location.reload();}},REFRESH*1000);}")


def _dashboard_script(refresh, has_widgets):
    """The dashboard's inline script: card reorder (when there are widgets) and
    auto-refresh (when ``refresh`` > 0). Returns an empty string when neither
    applies, so a plain dashboard carries no script at all."""
    refresh = int(refresh) if refresh else 0
    if refresh <= 0 and not has_widgets:
        return ""
    return ("<script>(function(){var dragging=null;"
            + (_REORDER_JS if has_widgets else "")
            + "var REFRESH=" + str(refresh) + ";" + _REFRESH_JS
            + "})();</script>")


# Below this age the environment reading reads as current ("in the last N");
# above it the card says "as of N ago" so the kismet-DB flush lag is honest.
_ENV_FRESH_WITHIN_S = 90


def _fmt_span(seconds):
    """A short human duration: "45 s", "5 min", "2 hr"."""
    s = max(0, int(seconds))
    if s < 90:
        return "%d s" % s
    m = s // 60
    if m < 90:
        return "%d min" % m
    return "%d hr" % (m // 60)


def _render_environment(env):
    """The "Environment" card: distinct transmitting radios in RF range recently.

    ``env`` is the daemon's ``status["environment"]``: ``None`` when capture is
    off (the radio is deaf, so "unknown" - never a misleading 0), ``available``
    False while scanning before the first usable reading, else a radios count with
    a density ``level``/``label`` and ``data_age_s`` freshness. AngryOxide flushes
    its kismet DB in bursts, so the reading can lag; we say "as of N ago" rather
    than pretend it is live, and never show a bare 0. Status is carried by the word
    and number, not colour alone."""
    def _srow(dot, key, val, meta=""):
        meta_html = (' <span class="smeta">' + _esc(meta) + "</span>") if meta else ""
        return ('<div class="srow"><span class="dot ' + dot + '"></span>'
                '<div class="scol"><span class="skey">' + key + '</span>'
                '<span class="sval">' + val + meta_html + "</span></div></div>")

    def _yield_row():
        # The session yield is real capture data, independent of the density, so
        # it is shown as a plain count - never "caught X of ~Y". Spelled out in
        # full ("N APs and M handshakes") so the two numbers are unambiguous
        # (the distinct access points caught from, and the total handshakes).
        caught = int((env or {}).get("caught_session") or 0)
        hs = int((env or {}).get("handshakes_session") or 0)
        val = "Caught %d %s and %d %s" % (
            caught, "AP" if caught == 1 else "APs",
            hs, "handshake" if hs == 1 else "handshakes")
        return _srow("ok" if caught else "", "Your yield this session",
                     _esc(val))

    if env is None:
        body = _srow("", "Environment", "Unknown",
                     "enable capture to scan the area")
        return '<div class="card">' + body + "</div>"
    if not env.get("available"):
        head = _srow("", "Environment", "Unknown", "Scanning the area…")
        return '<div class="card">' + head + _yield_row() + "</div>"

    radios = int(env.get("radios_recent") or 0)
    label = env.get("label") or ""
    window = int(env.get("window_s") or 0)
    age = env.get("data_age_s")
    noun = "radio nearby" if radios == 1 else "radios nearby"
    if age is not None and age > _ENV_FRESH_WITHIN_S:
        meta = "as of " + _fmt_span(age) + " ago"
    elif window:
        meta = "in the last " + _fmt_span(window)
    else:
        meta = ""
    headline = _srow(
        "ok" if radios > 0 else "",
        "Environment",
        _esc(label) + " &middot; " + str(radios) + " " + noun,
        meta,
    )
    bar = ('<div class="srow"><div class="scol" style="flex:1">'
           '<div class="bar"><span style="width:' + str(int(env.get("bar_pct") or 0))
           + '%"></span></div></div></div>')
    return ('<div class="card">' + headline + bar + _yield_row() + "</div>")


def _render_status(status, control=None, message=None, error=False, addresses=None,
                   refresh=0):
    """The dashboard: the device's live state as a labelled status card (plugin
    host version, internet, capture), the management-link addresses (when
    ``addresses`` is given), any plugin readout widgets, a capture enable/disable
    control (when ``control`` is given and web auth is on), and a tappable card
    linking to the plugins page. ``message`` shows a one-line banner (red when
    ``error``); a capture error surfaces here, but a successful capture toggle
    shows no banner - the capture row already reflects the state."""
    conn = status.get("connectivity", {}) or {}
    cap = status.get("capture", {}) or {}
    plugins = status.get("plugins", []) or []

    online = bool(conn.get("online"))
    paths = ", ".join(conn.get("paths") or [])
    net_meta = (' <span class="smeta">' + _esc(paths) + "</span>") if (online and paths) else ""
    net_row = (
        '<div class="srow"><span class="dot ' + ("ok" if online else "no") + '"></span>'
        '<div class="scol"><span class="skey">Internet</span>'
        '<span class="sval">' + ("Online" if online else "Offline") + net_meta
        + "</span></div></div>"
    )

    cap_state = cap.get("state", "unknown")
    cap_dot = {"running": "ok", "failed": "no"}.get(cap_state, "")
    # The store-wide count of capture files (one per network in the default
    # per-ssid mode), cheap to compute (a directory listing). Labelled "capture
    # file(s)" rather than "hashline(s)" so it is not misread as a count of
    # captured handshakes: that per-session count is the session-stats widget's
    # job, and the per-network breakdown lives on the /networks page.
    files = cap.get("hashline_files", 0)
    cap_meta = (' <span class="smeta">' + _esc(files) + " capture file(s)</span>") if files else ""
    cap_action = _capture_toggle(cap, control)
    if cap_action:
        cap_action = '<div class="sact">' + cap_action + "</div>"
    cap_row = (
        '<div class="srow"><span class="dot ' + cap_dot + '"></span>'
        '<div class="scol"><span class="skey">Capture</span>'
        '<span class="sval">' + _esc(cap_state.capitalize()) + cap_meta + "</span></div>"
        + cap_action + "</div>"
    )

    tagline = ('<p class="tagline">plugin host &middot; v' + _esc(status.get("version"))
               + " &middot; epoch " + _esc(status.get("epoch")) + "</p>")
    banner = ""
    if message:
        banner = ('<p class="msg' + (" err" if error else "")
                  + '">' + _esc(message) + "</p>")
    # The agent's own readout card lives on the /agent page now, not the
    # dashboard grid, so drop it here (it is still in status["widgets"]).
    dash_widgets = {k: v for k, v in (status.get("widgets") or {}).items()
                    if k != AGENT_CARD_NAME}
    widgets_html = _render_widgets(dash_widgets, plugins=plugins,
                                   plugins_url=_url("/plugins", control))

    total = len(plugins)
    if control is not None:
        enabled_count = sum(1 for v in (control.get("enabled") or {}).values() if v)
    else:
        enabled_count = sum(1 for p in plugins if p.get("state") == "loaded")
    plug_val = "none installed" if total == 0 else ("%d of %d enabled" % (enabled_count, total))
    nav = (
        '<a class="navcard" href="' + _esc(_url("/plugins", control)) + '">'
        '<div class="scol"><span class="skey">Plugins</span>'
        '<span class="sval">' + plug_val + "</span></div>"
        '<span class="arrow" aria-hidden="true">&rarr;</span></a>'
        + _agent_nav(status.get("agent"), cap, control)
        + '<a class="navcard" href="' + _esc(_url("/networks", control)) + '">'
        '<div class="scol"><span class="skey">Captured networks</span>'
        '<span class="sval">' + _esc(cap.get("handshakes", 0))
        + " handshake(s) &middot; " + _esc(cap.get("access_points", 0))
        + " access point(s) &middot; " + _esc(cap.get("networks", 0))
        + ' network(s)</span>'
        # "cracked N of M" access points, when the cracked store could be read
        # (the daemon omits the key on a read failure). Not a secret -- it is the
        # count only -- so it always shows when present.
        + (('<span class="smeta">' + _esc(cap.get("cracked_aps"))
            + " of " + _esc(cap.get("access_points", 0))
            + " access point(s) cracked</span>")
           if cap.get("cracked_aps") is not None else "")
        + '<span class="smeta">Browse by network or access point</span></div>'
        '<span class="arrow" aria-hidden="true">&rarr;</span></a>'
        + _whitelist_nav(cap, control)
    )

    power = _power_controls(control)
    updates = _update_controls(control)
    bt_pair = _bt_pair_control(status, control)

    notice = ""
    if control is not None and not control.get("ready"):
        notice = '<p class="notice">' + _esc(CONTROLS_OFF) + "</p>"

    # A discreet link to the advanced Settings page: visible, but low on the
    # page and muted, since it is an advanced-operator surface.
    adv = ('<p class="advlink"><a href="' + _esc(_url("/settings", control))
           + '">Advanced settings</a></p>')

    return _page(
        "Kestrel",
        "<h1>Kestrel</h1>" + tagline + banner
        + '<div class="card">' + net_row + cap_row + "</div>"
        + _render_environment(status.get("environment"))
        + updates
        + _render_addresses(addresses)
        + widgets_html + nav + power + bt_pair + notice + adv
        + _dashboard_script(refresh, bool(widgets_html)),
    )


def _render_plugins(status, control=None, message=None, error=False):
    """The plugins page: every loaded/disabled/failed plugin with its API level,
    capabilities and health note, plus (when ``control`` is given and web auth
    is on) a per-plugin enable/disable control and a link to its settings. The
    plugin's state is shown by shading the name cell (green loaded, gray
    disabled, red failed) rather than a column. Each plugin row is formatted on
    its own (only ``{}`` placeholders, no literal braces) and its result is
    never re-interpolated. ``message`` shows a one-line banner (red when
    ``error``)."""
    rows = []
    for plugin in status.get("plugins", []):
        name = plugin.get("name")
        state = plugin.get("state")
        cls = _STATE_CLASS.get(state, "s-off")
        caps = ", ".join(plugin.get("capabilities") or []) or "-"
        viol = ", ".join(plugin.get("capability_violations") or [])
        reason = plugin.get("reason") or plugin.get("last_error") or ""
        note = plugin.get("note") or ""
        detail = "; ".join(part for part in (reason, note) if part)
        if viol:
            detail += " (undeclared: %s)" % viol
        # The name cell carries the state as a colour and a hover/screen-reader
        # title, so dropping the state column loses no information.
        row = ('<tr><td class="plug {}" title="{}">{}</td>'
               '<td data-label="api">{}</td>'
               '<td data-label="capabilities">{}</td>'
               '<td data-label="note">{}</td>').format(
            cls, _esc(state), _esc(name),
            _esc(plugin.get("target_api")), _esc(caps), _esc(detail),
        )
        if control is not None:
            enabled = bool((control.get("enabled") or {}).get(name))
            if control.get("ready"):
                action = "disable" if enabled else "enable"
                # Disable is destructive (it turns a running plugin off), so it
                # reads red; Enable stays the default (green) action.
                btn_class = ' class="danger"' if action == "disable" else ""
                toggle = (
                    '<form class="inline" method="post" action="'
                    + _esc(_manage_url(name, control)) + '">' + _csrf_field(control)
                    + '<input type="hidden" name="action" value="' + action + '">'
                    + '<button type="submit"' + btn_class + ">" + action.capitalize()
                    + "</button></form> "
                )
            else:
                toggle = ("on" if enabled else "off") + " &middot; "
            # Disable + settings sit on the left; the plugin's own declared
            # action buttons (up to two, via __actions__) are pushed to the far
            # right, in the order the plugin gives. They are shown only for an
            # enabled plugin with web auth on - a button either performs a
            # privileged action (POST) or opens a page that may carry sensitive
            # data, both of which the route itself also gates on the login.
            actions = (plugin.get("actions") or []
                       if enabled and control.get("ready") else [])
            # The plugin's own action buttons are grouped in one right-aligned
            # box so they stay together on one line (and wrap as a unit on a
            # narrow column) rather than each floating right onto its own row.
            actions_html = ""
            if actions:
                actions_html = ('<div class="rowactions">'
                                + "".join(_action_button(name, a, control)
                                          for a in actions)
                                + "</div>")
            row += ('<td data-label="config"><div class="cfgrow">' + toggle
                    + '<a href="' + _esc(_manage_url(name, control))
                    + '">settings</a>' + actions_html
                    + '</div></td>')
        rows.append(row + "</tr>")
    columns = 5 if control is not None else 4
    body_rows = "".join(rows) or (
        "<tr><td colspan='%d'>no plugins installed</td></tr>" % columns
    )
    banner = ""
    if message:
        banner = ('<p class="msg' + (" err" if error else "")
                  + '">' + _esc(message) + "</p>")
    notice = ""
    if control is not None and not control.get("ready"):
        notice = '<p class="notice">' + _esc(CONTROLS_OFF) + "</p>"
    legend = ('<p class="legend">name shading &mdash; '
              '<span class="s-on">on</span>'
              '<span class="s-off">off</span>'
              '<span class="s-err">error</span></p>')
    back = '<p><a href="' + _esc(_url("/", control)) + '">&larr; Dashboard</a></p>'
    return _page(
        "plugins - Kestrel",
        back + "<h1>Plugins</h1>" + banner + notice + legend
        + "<div class='wrap'><table><thead><tr><th>name</th><th>api</th>"
        "<th>capabilities</th><th>note</th>"
        + ("<th>config</th>" if control is not None else "")
        + "</tr></thead><tbody>" + body_rows + "</tbody></table></div>",
    )


NETWORKS_PER_PAGE = 100


def _mac_norm(value):
    """A BSSID reduced to its hex digits, lowercased, so a search matches no
    matter how the operator punctuates it (``AA:BB``, ``aabb``, ``aa-bb``)."""
    return "".join(ch for ch in (value or "").lower() if ch in "0123456789abcdef")


def _ago(epoch, now=None):
    """A short relative time for an epoch — "just now" / "3m ago" / "2h ago" /
    "5d ago", a plain date past two weeks, and "never" for missing/0. Used for
    the networks page's "last seen" column."""
    if not epoch:
        return "never"
    now = time.time() if now is None else now
    delta = int(now - epoch)
    if delta < 0:
        delta = 0
    if delta < 45:
        return "just now"
    if delta < 3600:
        return "%dm ago" % (delta // 60)
    if delta < 86400:
        return "%dh ago" % (delta // 3600)
    if delta < 86400 * 14:
        return "%dd ago" % (delta // 86400)
    return time.strftime("%Y-%m-%d", time.localtime(epoch))


def _cracked_maps(daemon, view, data):
    """Build the cracked-annotation maps for ``/networks`` from the ``cracked``
    service. Returns ``(cracked_by_bssid, cracked_by_essid)`` -- both empty when
    the store is absent or unreadable, so the page just shows no cracked markers.

    By-AP view: each captured BSSID joined to its passphrase. By-network view: a
    network is cracked if any of its captured BSSIDs is cracked (grouped under
    the names that AP advertised) or an ESSID-name record matches; each distinct
    ``(bssid, psk)`` is kept so differing passphrases under one name are shown."""
    cracked = daemon.services.get("cracked")
    by_bssid, by_essid = {}, {}
    if cracked is None:
        return by_bssid, by_essid
    try:
        if view == "ap":
            for row in data:
                bssid = row.get("bssid")
                if not bssid:
                    continue
                rec = cracked.lookup_bssid(bssid)
                if rec:
                    by_bssid[bssid] = rec.psk
        else:
            def _add(essid, rec):
                pairs = by_essid.setdefault(essid, [])
                if (rec.bssid, rec.psk) not in pairs:
                    pairs.append((rec.bssid, rec.psk))

            for ap in daemon.capture.captured_access_points():
                bssid = ap.get("bssid")
                rec = cracked.lookup_bssid(bssid) if bssid else None
                if rec:
                    for essid in ap.get("essids") or []:
                        _add(essid, rec)
            for row in data:
                essid = row.get("essid")
                if essid:
                    for rec in cracked.lookup_essid(essid):
                        _add(essid, rec)
    except Exception:  # noqa: BLE001 - a cracked-store read must never break the page
        return {}, {}
    return by_bssid, by_essid


def _cracked_cell(summary_html, pairs, auth_on, show_passphrases, with_bssid):
    """Wrap a row's name/BSSID cell so a cracked row shows the badge and, when
    allowed, its passphrase in a ``<details>`` disclosure. ``pairs`` is a list of
    ``(bssid, psk)``.

    The passphrase is a secret, shown only when BOTH gates pass; the badge is
    not a secret and always shows:
    - auth off: badge only, no secret panel (there is no login to gate it).
    - auth on, ``show_passphrases`` off: badge + a muted 'hidden' note, so the
      operator still knows a passphrase exists.
    - auth on, ``show_passphrases`` on: badge + the passphrase(s)."""
    badge = ('<span class="crk"><span class="lock" aria-hidden="true">'
             "&#128274;</span>cracked</span>")
    if not auth_on:
        return summary_html + " " + badge
    if not show_passphrases:
        panel = ('<div class="crk-p crk-hidden">Passphrase hidden '
                 "(<code>show_passphrases = true</code> to reveal)</div>")
    else:
        parts = []
        for bssid, psk in pairs:
            if with_bssid and bssid:
                parts.append('<div class="crk-p"><span class="bssid">'
                             + _esc(bssid) + "</span> <code>" + _esc(psk)
                             + "</code></div>")
            else:
                parts.append('<div class="crk-p">Passphrase: <code>'
                             + _esc(psk) + "</code></div>")
        panel = "".join(parts)
    return ('<details class="crk-d"><summary>' + summary_html + " " + badge
            + "</summary>" + panel + "</details>")


def _cracked_import_form(control, view):
    """The "Import cracked results" control on /networks, shown only with web
    auth on. A multipart POST to /cracked/import, CSRF-protected like every
    other write; the file is parsed server-side (see the route)."""
    csrf = (control or {}).get("csrf") or ""
    token = (control or {}).get("token") or ""
    hidden = ('<input type="hidden" name="csrf" value="' + _esc(csrf) + '">'
              + '<input type="hidden" name="view" value="' + _esc(view) + '">'
              + ('<input type="hidden" name="token" value="' + _esc(token)
                 + '">' if token else ""))
    return (
        '<details class="crkimport"><summary>Import cracked results</summary>'
        '<form method="post" action="/cracked/import" '
        'enctype="multipart/form-data">' + hidden
        + '<p class="hint">Upload a wpa-sec results file or a hashcat '
        "<em>separated</em> potfile (BSSID, ESSID and passphrase per line). The "
        "default hashcat.potfile is not supported &mdash; export with a "
        "separated outfile format instead. These are passphrases for networks "
        "you are authorized to test.</p>"
        '<input type="file" name="potfile" accept=".potfile,.txt,text/plain" '
        'required aria-label="Potfile to import">'
        '<button type="submit">Import</button></form></details>')


def _render_networks(rows_in, control=None, query="", page=1,
                     per_page=NETWORKS_PER_PAGE, view="ssid", sort="name",
                     cracked_by_bssid=None, cracked_by_essid=None,
                     show_passphrases=True, auth_on=False,
                     message=None, error=False):
    """The captured-networks page, in one of two views over the same store:

    - ``view="ssid"`` (by network): one row per ESSID, with its distinct-BSSID
      count and handshake count. This is the default under ``per-ssid`` capture.
    - ``view="ap"`` (by access point): one row per ESSID+BSSID, with that AP's
      BSSID and handshake count (no BSSID-count column). The default under
      ``per-ap`` capture. Search also matches a pasted BSSID.

    The view is a presentation choice -- both are built from the same parsed
    store (``captured_networks`` / ``captured_access_points``) regardless of the
    capture file layout -- so the operator can flip it anytime via the toggle.
    ``query`` filters (case-insensitive) and the list is paginated so the page
    stays light no matter how much the store holds.

    A row whose network/AP has a known passphrase gets a "cracked" badge (never
    colour alone), and -- only when ``auth_on`` and ``show_passphrases`` -- the
    passphrase itself in a disclosure. ``cracked_by_bssid`` maps a BSSID to its
    passphrase (by-AP view); ``cracked_by_essid`` maps an ESSID to its distinct
    ``(bssid, psk)`` pairs (by-network view). Both are built by the handler from
    the core cracked service; empty means nothing to annotate."""
    rows_in = rows_in or []
    cracked_by_bssid = cracked_by_bssid or {}
    cracked_by_essid = cracked_by_essid or {}
    ap_view = view == "ap"
    # The by-AP list may carry a trailing "(unknown BSSID)" fold row for
    # handshakes whose BSSID didn't parse; it is shown but is not an access
    # point, so the headline count excludes it to match the dashboard navcard.
    total = (sum(1 for r in rows_in if r.get("bssid")) if ap_view
             else len(rows_in))
    needle = (query or "").strip().lower()
    if needle:
        mac_needle = _mac_norm(query)

        def _matches(r):
            names = r.get("essids") if ap_view else [r.get("essid") or ""]
            if any(needle in (e or "").lower() for e in (names or [])):
                return True
            if ap_view and mac_needle and mac_needle in _mac_norm(r.get("bssid")):
                return True
            return False

        shown = [r for r in rows_in if _matches(r)]
    else:
        shown = rows_in
    matched = len(shown)

    # Default order is by name (as the readers return it); "recent" re-sorts by
    # last-captured, newest first, with never-stamped rows last.
    if sort == "recent":
        shown = sorted(shown, key=lambda r: (r.get("last_seen") or 0),
                       reverse=True)

    per_page = max(1, int(per_page))
    pages = max(1, (matched + per_page - 1) // per_page)
    try:
        page = int(page)
    except (TypeError, ValueError):
        page = 1
    page = min(max(1, page), pages)
    start = (page - 1) * per_page
    window = shown[start:start + per_page]

    noun = "access point" if ap_view else "network"
    rows = []
    for r in window:
        seen_cell = ('<td data-label="last seen"><span class="smeta">'
                     + _esc(_ago(r.get("last_seen"))) + "</span></td>")
        if ap_view:
            # One row per radio (BSSID); the names it advertised follow.
            bssid = r.get("bssid") or ""
            radio = (_esc(bssid) if bssid
                     else '<span class="smeta">(unknown BSSID)</span>')
            if bssid and bssid in cracked_by_bssid:
                radio = _cracked_cell(radio, [(bssid, cracked_by_bssid[bssid])],
                                      auth_on, show_passphrases, with_bssid=False)
            essids = r.get("essids") or []
            if essids:
                names = ", ".join(_esc(e) for e in essids)
            else:
                names = '<span class="smeta">(hidden)</span>'
            rows.append(
                "<tr><td data-label=\"access point\">" + radio + "</td>"
                "<td data-label=\"network(s)\">" + names + "</td>"
                "<td data-label=\"handshakes\">" + _esc(r.get("handshakes", 0))
                + "</td>" + seen_cell + "</tr>"
            )
        else:
            essid = r.get("essid") or ""
            name = _esc(essid) if essid else '<span class="smeta">(hidden)</span>'
            pairs = cracked_by_essid.get(essid) if essid else None
            if pairs:
                name = _cracked_cell(name, pairs, auth_on, show_passphrases,
                                     with_bssid=True)
            rows.append(
                "<tr><td data-label=\"network\">" + name + "</td>"
                "<td data-label=\"access points\">" + _esc(r.get("bssids", 0))
                + "</td>"
                "<td data-label=\"handshakes\">" + _esc(r.get("handshakes", 0))
                + "</td>" + seen_cell + "</tr>"
            )
    cols = 4
    body_rows = "".join(rows) or (
        "<tr><td colspan='" + str(cols) + "'>"
        + (("no %ss match that search" % noun) if needle
           else ("no %ss captured yet" % noun)) + "</td></tr>"
    )

    token = (control or {}).get("token") or ""

    def link(**over):
        # Always carry the explicit view so paging/searching never silently
        # reverts to the mode-default view (which only seeds the first visit).
        params = {}
        q = over.get("q", query)
        if q:
            params["q"] = q
        params["view"] = over.get("view", view)
        s = over.get("sort", sort)
        if s and s != "name":
            params["sort"] = s
        p = over.get("page", 1)
        if p != 1:
            params["page"] = p
        if token:
            params["token"] = token
        return "/networks?" + urlencode(params)

    if matched:
        if needle:
            count = ("%d of %d %ss match &ldquo;%s&rdquo;"
                     % (matched, total, noun, _esc(query)))
        else:
            count = "%d %s(s) captured" % (total, noun)
    else:
        count = (("no match in %d %s(s)" % (total, noun)) if total
                 else "nothing captured yet")

    # By-network / by-access-point toggle. Switching views resets to page 1 but
    # keeps the search term.
    def tab(label, target):
        return ('<a' + (' class="on"' if target == view else "")
                + ' href="' + _esc(link(view=target, page=1)) + '">'
                + label + "</a>")

    tabs = ('<div class="viewtabs">' + tab("By network", "ssid")
            + tab("By access point", "ap") + "</div>")

    # Sort toggle: name (default) or most-recently-captured. Resets to page 1,
    # keeps the view and search term.
    def sorttab(label, target):
        return ('<a' + (' class="on"' if target == sort else "")
                + ' href="' + _esc(link(sort=target, page=1)) + '">'
                + label + "</a>")

    sorttabs = ('<div class="viewtabs sorttabs"><span class="tablabel">Sort:'
                "</span>" + sorttab("Name", "name")
                + sorttab("Recently captured", "recent") + "</div>")

    # Carry the current view through a search submit (always, so a search on the
    # non-default view keeps that view instead of reverting to the mode-default).
    hidden_fields = (
        ('<input type="hidden" name="token" value="' + _esc(token) + '">'
         if token else "")
        + '<input type="hidden" name="view" value="' + view + '">'
        + ('<input type="hidden" name="sort" value="' + sort + '">'
           if sort != "name" else "")
    )
    placeholder = "Search name or BSSID" if ap_view else "Search AP name"
    search = (
        '<form class="search" method="get" action="/networks">'
        + hidden_fields
        + '<input type="text" name="q" value="' + _esc(query)
        + '" placeholder="' + placeholder + '" aria-label="' + placeholder + '">'
        + '<button type="submit">Search</button></form>'
    )

    pager = ""
    if pages > 1:
        prev_cls = ' class="off"' if page <= 1 else ""
        next_cls = ' class="off"' if page >= pages else ""
        pager = (
            '<div class="pager"><a' + prev_cls + ' href="'
            + _esc(link(page=max(1, page - 1))) + '">&larr; Prev</a>'
            + '<span class="sub">page ' + str(page) + " of " + str(pages) + "</span>"
            + '<a' + next_cls + ' href="' + _esc(link(page=min(pages, page + 1)))
            + '">Next &rarr;</a></div>'
        )

    if ap_view:
        heading = "Captured access points"
        head_cells = ("<th>access point</th><th>network(s)</th>"
                      "<th>handshakes</th><th>last seen</th>")
    else:
        heading = "Captured networks"
        head_cells = ("<th>network</th><th>access points</th>"
                      "<th>handshakes</th><th>last seen</th>")

    back = '<p><a href="' + _esc(_url("/", control)) + '">&larr; Dashboard</a></p>'
    banner = ""
    if message:
        banner = ('<p class="msg' + (" err" if error else "") + '">'
                  + _esc(message) + "</p>")
    # The import control writes to the cracked store, so it needs web auth on
    # (like every other write); read-only pages don't show it.
    import_form = _cracked_import_form(control, view) if auth_on else ""
    return _page(
        "captured " + noun + "s - Kestrel",
        back + "<h1>" + heading + "</h1>" + banner + tabs + sorttabs + search
        + '<p class="count">' + count + "</p>"
        + "<div class='wrap'><table><thead><tr>" + head_cells
        + "</tr></thead><tbody>" + body_rows
        + "</tbody></table></div>" + pager + import_form,
    )


def _render_whitelist(entries, control=None, query="", page=1,
                      per_page=NETWORKS_PER_PAGE, message=None, error=False):
    """The whitelist page, reached from the dashboard's Whitelist nav card: the
    ESSIDs/BSSIDs that are never actively attacked. Mirrors the captured-networks
    page (search + pager) and, when web auth is on, adds an inline form to add a
    new AP and a Remove button per row. Writes go through /whitelist (CSRF) to
    the root apply helper, so the terminal and the page share one writer."""
    entries = list(entries or [])
    total = len(entries)
    needle = (query or "").strip().lower()
    shown = [e for e in entries if needle in e.lower()] if needle else entries
    matched = len(shown)

    per_page = max(1, int(per_page))
    pages = max(1, (matched + per_page - 1) // per_page)
    try:
        page = int(page)
    except (TypeError, ValueError):
        page = 1
    page = min(max(1, page), pages)
    start = (page - 1) * per_page
    window = shown[start:start + per_page]

    ready = bool(control and control.get("ready"))
    qs = _esc((control or {}).get("qs", "")) if control else ""

    rows = []
    for entry in window:
        kind = "BSSID" if _is_mac(entry) else "ESSID"
        remove = ""
        if ready:
            remove = (
                '<form class="inline" method="post" action="/whitelist' + qs + '">'
                + _csrf_field(control)
                + '<input type="hidden" name="action" value="remove">'
                + '<input type="hidden" name="entry" value="' + _esc(entry) + '">'
                + '<button type="submit" class="danger">Remove</button></form>')
        rows.append(
            '<tr><td data-label="ap">' + _esc(entry) + "</td>"
            '<td data-label="type">' + kind + "</td>"
            '<td data-label="">' + remove + "</td></tr>")
    body_rows = "".join(rows) or (
        "<tr><td colspan='3'>" + ("no entries match that search" if needle
                                  else "nothing whitelisted yet") + "</td></tr>")

    token = (control or {}).get("token") or ""

    def page_link(target):
        params = {}
        if query:
            params["q"] = query
        if target != 1:
            params["page"] = target
        if token:
            params["token"] = token
        return "/whitelist" + ("?" + urlencode(params) if params else "")

    banner = ""
    if message:
        banner = ('<p class="msg' + (" err" if error else "") + '">'
                  + _esc(message) + "</p>")

    if ready:
        add_form = (
            '<form class="wladd" method="post" action="/whitelist' + qs + '">'
            + _csrf_field(control)
            + '<input type="hidden" name="action" value="add">'
            + '<input type="text" name="entry" maxlength="64" '
            'placeholder="AP name or BSSID (AA:BB:CC:DD:EE:FF)" '
            'aria-label="AP name or BSSID to whitelist">'
            + '<button type="submit">Add</button></form>')
    else:
        add_form = ('<p class="hint">The dashboard is read-only. Log in (enable '
                    "web auth) to add or remove entries.</p>")

    hidden_token = ('<input type="hidden" name="token" value="' + _esc(token) + '">'
                    if token else "")
    search = (
        '<form class="search" method="get" action="/whitelist">' + hidden_token
        + '<input type="text" name="q" value="' + _esc(query)
        + '" placeholder="Search whitelist" aria-label="Search whitelist">'
        + '<button type="submit">Search</button></form>')

    if matched:
        count = (("%d of %d match &ldquo;%s&rdquo;" % (matched, total, _esc(query)))
                 if needle else ("%d AP(s) whitelisted" % total))
    else:
        count = ("no match in %d" % total) if total else "nothing whitelisted yet"

    pager = ""
    if pages > 1:
        prev_cls = ' class="off"' if page <= 1 else ""
        next_cls = ' class="off"' if page >= pages else ""
        pager = (
            '<div class="pager"><a' + prev_cls + ' href="'
            + _esc(page_link(max(1, page - 1))) + '">&larr; Prev</a>'
            + '<span class="sub">page ' + str(page) + " of " + str(pages) + "</span>"
            + '<a' + next_cls + ' href="' + _esc(page_link(min(pages, page + 1)))
            + '">Next &rarr;</a></div>')

    note = ('<p class="hint">Whitelisted APs are never actively attacked (deauth); '
            "they are still observed passively. A change takes effect the next "
            "time capture starts.</p>")
    back = '<p><a href="' + _esc(_url("/", control)) + '">&larr; Dashboard</a></p>'
    return _page(
        "whitelist - Kestrel",
        back + "<h1>Whitelist</h1>" + banner + note + add_form + search
        + '<p class="count">' + count + "</p>"
        + "<div class='wrap'><table><thead><tr><th>ap</th><th>type</th><th></th>"
        "</tr></thead><tbody>" + body_rows + "</tbody></table></div>" + pager,
    )


def _render_capture_confirm(control, error=""):
    """The authorized-use confirmation shown before capture is enabled from the
    web, mirroring the CLI acknowledgment. The box must be ticked; the confirm
    re-posts the enable with the ack, and the root helper records it once."""
    note = ('<p class="msg err">' + _esc(error) + "</p>") if error else ""
    qs = _esc(control.get("qs", ""))
    body = (
        "<h1>Enable capture</h1>" + note
        + '<div class="ack"><p>' + _esc(CAPTURE_ACK_TEXT) + "</p>"
        + '<form method="post" action="/capture' + qs + '">'
        + _csrf_field(control)
        + '<input type="hidden" name="action" value="enable">'
        + '<label class="check"><input type="checkbox" name="ack" value="yes"> '
        + "I am authorized to test the networks in range.</label>"
        + '<button type="submit">Enable capture</button></form></div>'
        + '<p><a href="/' + qs + '">Cancel</a></p>'
    )
    return _page("Kestrel - enable capture", body)


def _render_agent_train_confirm(control, algorithm, policy, action):
    """The confirm page before switching to an algorithm with no trained model
    yet, since that starts a background training run on the device. The confirm
    re-posts the change with ``train_ack=yes`` (carrying the chosen policy and
    the original enable/save action); Cancel returns to the agent page."""
    qs = _esc(control.get("qs", ""))
    algo = _esc(algorithm)
    policy_field = ('<input type="hidden" name="policy" value="' + _esc(policy)
                    + '">') if policy else ""
    body = (
        "<h1>Train a model for " + algo + "?</h1>"
        + '<div class="ack"><p>There is no trained model for ' + algo + " yet, so "
        "switching to it will start a training run on the device to build one. "
        "Training runs in the background from the logged episodes and won't "
        "interrupt capture; it needs some captured episodes to learn from and a "
        "board with enough memory (it is skipped on a small board, or while the "
        "CPU is hot or memory is low). Each algorithm keeps its own model, so "
        "switching back to one you've already trained reloads it with no "
        "retraining.</p>"
        + '<form method="post" action="/agent' + qs + '">' + _csrf_field(control)
        + '<input type="hidden" name="action" value="' + _esc(action or "save") + '">'
        + '<input type="hidden" name="algorithm" value="' + algo + '">'
        + policy_field
        + '<input type="hidden" name="train_ack" value="yes">'
        + '<button type="submit">Switch to ' + algo + " and train</button></form></div>"
        + '<p><a href="' + _esc(_url("/agent", control)) + '">Cancel</a></p>'
    )
    return _page("Kestrel - train " + algorithm, body)


def _render_power_confirm(control, action):
    """The confirm page shown before a reboot/shutdown from the web. The confirm
    re-posts the action with ``confirm=yes``; Cancel returns to the dashboard."""
    words = _POWER_WORDS[action]
    qs = _esc(control.get("qs", ""))
    body = (
        "<h1>" + _esc(words["title"]) + "?</h1>"
        + '<div class="ack"><p>' + _esc(words["warn"]) + "</p>"
        + '<form method="post" action="/power' + qs + '">' + _csrf_field(control)
        + '<input type="hidden" name="action" value="' + _esc(action) + '">'
        + '<input type="hidden" name="confirm" value="yes">'
        + '<button type="submit" class="danger">Yes, ' + _esc(words["verb"])
        + " now</button></form></div>"
        + '<p><a href="/' + qs + '">Cancel</a></p>'
    )
    return _page("Kestrel - " + words["verb"], body)


def _render_power_done(action, control=None, started=None):
    """The page shown once a reboot/shutdown is accepted. The action is deferred
    a moment by the root helper, so this reply still reaches the browser. For a
    **reboot** the page then polls /status.json and returns to the dashboard
    once the device is back (a fresh daemon answers); a **shutdown** stays down,
    so no reconnect poll - the device is simply unreachable."""
    words = _POWER_WORDS[action]
    body = ("<h1>" + _esc(words["gerund"]) + "…</h1>"
            + "<p>" + _esc(words["done"]) + "</p>")
    if action == "reboot" and control is not None:
        target = _url("/", control)
        probe = _url("/status.json", control)
        body += ('<p id="wait">Waiting for the device to come back; this page '
                 "returns to the dashboard once it has rebooted.</p>")
        body += _reconnect_script(
            target, probe, started, max_tries=150, start_ms=5000, delay_ms=2000,
            giveup="The device has not come back yet; reload this page once it "
                   "has finished rebooting.")
    return _page("Kestrel - " + words["verb"], body)


def _render_webhook_timeout(name, control, busy=False):
    """Shown when a plugin's on_webhook overruns the soft deadline: the page
    returns a 504 instead of the browser hanging on a stuck plugin. The plugin's
    call is left running in the background; this just frees the request. With
    ``busy`` (a 503) the earlier overrun call is still running, so this request
    was refused without calling the plugin at all."""
    plugins = _url("/plugins", control)
    if busy:
        body = ("<h1>Still busy…</h1>"
                "<p>The <b>" + _esc(name) + "</b> plugin has not finished an earlier "
                "request yet, so this one was not started. Try again in a little "
                'while, or go back to <a href="' + _esc(plugins) + '">Plugins</a>.</p>')
        return _page(name + " - Kestrel", body)
    body = ("<h1>Still working…</h1>"
            "<p>The <b>" + _esc(name) + "</b> plugin's page is taking longer than "
            "expected and did not finish in time. It is still running in the "
            'background &mdash; reload to try again, or go back to <a href="'
            + _esc(plugins) + '">Plugins</a>.</p>')
    return _page(name + " - Kestrel", body)


def _render_update_confirm(control, state, no_backup=False):
    """The confirm page before an in-place upgrade. It spells out exactly what
    the update does - pause capture, training and the daemon, optionally back up
    first, install, then restart what was running - matching the CLI's prompt.
    When a pre-update backup would run, it offers a "skip the pre-update backup"
    checkbox (pre-ticked when the operator arrived via the retry-without-backup
    button), so a device whose backup remote is unreachable can still update.
    The confirm re-posts with ``confirm=yes``; Cancel returns to the dashboard."""
    qs = _esc(control.get("qs", ""))
    latest = _esc(state.get("latest") or "the latest release")
    backup_on = bool(control.get("enabled", {}).get("backup")
                     and control.get("backup_ready"))
    # "Skip the backup" only makes sense when a pre-update backup would run.
    skip_backup = bool(no_backup) and backup_on
    # Soft single-link guardrail: warn when the device is reachable on only one
    # management link right now, since losing it mid-update (especially a BT PAN
    # link that then fails to re-establish) can leave the device unreachable.
    # Always point at the boot-folder Wi-Fi recovery; sharpen it when there is no
    # second link up as a fallback.
    active_links = [iface for iface, ip in management_addresses() if ip]
    if len(active_links) <= 1:
        where = (" (" + _esc(active_links[0]) + ")") if active_links else ""
        lead = ("<strong>You are reachable on only one connection right now"
                + where + ".</strong> If it drops during or after the update, the "
                "device can become unreachable until that link comes back.")
    else:
        lead = ("<strong>Heads up:</strong> if your only link is Bluetooth PAN, a "
                "dropped connection during or after the update can leave the "
                "device unreachable until it re-establishes.")
    link_warn = (
        '<div class="msg">' + lead
        + " To recover without reflashing, stage a Wi-Fi network on the boot "
        "partition (a <code>kestrel-wifi.txt</code> file) and reboot: the "
        "device joins Wi-Fi and the dashboard and SSH come back on it. Setting a "
        "Wi-Fi fallback before updating avoids this entirely.</div>"
    )
    steps = ["<li>stop capture</li>",
             "<li>stop the learning agent (pausing training)</li>"]
    if backup_on and not skip_backup:
        steps.append("<li>back up the latest capture data and the learning model "
                     "to your remote first &mdash; and <strong>cancel the "
                     "update</strong> if that backup fails, so it only proceeds "
                     "with a current copy safe</li>")
    elif skip_backup:
        steps.append("<li><strong>skip the pre-update backup</strong> &mdash; "
                     "updating without a fresh copy on your remote</li>")
    steps.append("<li>stop the plugin daemon</li>")
    steps.append("<li>install " + latest + ", then restart whatever was running</li>")
    # The network sentence names the backup only when a pre-update backup runs.
    if backup_on and not skip_backup:
        net_line = ("Only the download and the backup use the network, both "
                    "before anything on the device is changed and both retried, "
                    "so a dropped connection cancels the update with nothing "
                    "altered")
    else:
        net_line = ("Only the download uses the network, before anything on the "
                    "device is changed, and it is retried, so a dropped "
                    "connection cancels the update with nothing altered")
    body = (
        "<h1>Upgrade to " + latest + "?</h1>"
        + '<div class="ack"><p>This installs release ' + latest + " in place. In "
        "order it will:</p><ol>" + "".join(steps) + "</ol>"
        + "<p>The dashboard is briefly unavailable while the daemon restarts, then "
        "comes back on the new version. Your settings, enabled plugins and "
        "captured data are kept. " + net_line + "; if anything "
        "else goes wrong the previous version is restored automatically.</p>"
        + link_warn
        + '<form method="post" action="/update' + qs + '">' + _csrf_field(control)
        + '<input type="hidden" name="action" value="apply">'
        + '<input type="hidden" name="confirm" value="yes">'
        + '<input type="hidden" name="tag" value="'
        + _esc(state.get("latest") or "") + '">'
        + (('<label class="chk"><input type="checkbox" name="no_backup" '
            'value="yes"' + (" checked" if skip_backup else "") + "> Skip the "
            "pre-update backup (update without a fresh copy on your remote "
            "&mdash; use this if the backup host is unreachable)</label>")
           if backup_on else "")
        + '<button type="submit">Yes, upgrade now</button></form></div>'
        + '<p><a href="/' + qs + '">Cancel</a></p>'
    )
    return _page("Kestrel - upgrade", body)


def _render_update_done(state, control=None, started=None):
    """The page shown once an upgrade is accepted. The root helper runs it in the
    background and restarts the daemon when done, so this reply still lands. The
    page then polls /status.json and returns to the dashboard on the new version
    once the restarted daemon answers, rather than asking the operator to reload
    by hand."""
    latest = _esc(state.get("latest") or "the latest release")
    body = ("<h1>Upgrading…</h1>"
            + "<p>Fetching and installing " + latest + " in the background. The "
            "device restarts its services when it is done. If anything goes wrong "
            "the previous version is restored.</p>")
    if control is not None:
        target = _url("/", control)
        probe = _url("/status.json", control)
        body += ('<p id="wait">This page returns to the dashboard once the new '
                 "version is up; you can keep waiting here.</p>")
        body += _reconnect_script(
            target, probe, started, max_tries=180, start_ms=5000, delay_ms=2000,
            giveup="Still installing, or the device is still restarting; reload "
                   "this page in a minute.")
    else:
        body += ("<p>Reload this page in a minute or two.</p>")
    return _page("Kestrel - upgrade", body)


def _render_update_notes(control, state):
    """The "What's new" page: the merged release notes for every release between
    the installed version and the latest available one, newest first. The notes
    are captured by the updates plugin when it checks (stored in the shared
    state), so this page needs no network of its own and works even if the link
    is flaky. Read-only; linked discreetly from the update card. Release bodies
    are external text, so they go through the escaping Markdown renderer."""
    qs = _esc((control or {}).get("qs", ""))
    back = '<p><a href="/' + qs + '">&larr; Dashboard</a></p>'
    installed = _esc(state.get("installed") or "the installed version")
    latest = _esc(state.get("latest") or "the latest release")
    notes = state.get("notes") or []
    if notes:
        intro = ('<p class="tagline">Combined changes from ' + installed + " to "
                 + latest + ".</p>")
        parts = []
        for note in notes:
            ver = _esc((note.get("version") or "").strip() or "release")
            date = _esc((note.get("date") or "").strip())
            head = ver + (" &middot; " + date if date else "")
            body_md = note.get("body") or "_No notes for this release._"
            parts.append("<h2>" + head + "</h2>" + _render_markdown(body_md))
        article = "<article class='guide'>" + "".join(parts) + "</article>"
    else:
        # No notes captured (an older plugin, offline when it last checked, or a
        # private repo without a token). Fall back to the release page if known.
        intro = ""
        url = (state.get("notes_url") or "").strip()
        link = ""
        # The URL comes from the release server via pi-owned state: link it
        # only as https, never javascript: or another scheme.
        if url.startswith("https://"):
            link = ('<p><a href="' + _esc(url) + '" target="_blank" '
                    'rel="noopener noreferrer">View the latest release</a></p>')
        article = ("<article class='guide'><p>Release notes aren't on the device "
                   "yet. They are collected the next time the updates plugin checks "
                   "for a release.</p>" + link + "</article>")
    return _page("What's new - Kestrel",
                 back + "<h1>What's new</h1>" + intro + article)


def _render_bt_pair_confirm(control):
    """The confirm page before reopening Bluetooth pairing from the web. The
    confirm re-posts with ``confirm=yes``; Cancel returns to the dashboard."""
    qs = _esc(control.get("qs", ""))
    body = (
        "<h1>Pair a phone over Bluetooth?</h1>"
        + '<div class="ack"><p>This reopens the Bluetooth onboarding window so '
        "you can pair a phone for the PAN (tether) link. Any previously paired "
        "phone is forgotten first. Then, on your phone, pair with the device and "
        "turn on Bluetooth tethering.</p>"
        + '<form method="post" action="/bt-pair' + qs + '">' + _csrf_field(control)
        + '<input type="hidden" name="confirm" value="yes">'
        + '<button type="submit">Open pairing now</button></form></div>'
        + '<p><a href="/' + qs + '">Cancel</a></p>'
    )
    return _page("Kestrel - pair a phone", body)


def _render_bt_pair_done(control):
    """The page shown once pairing has been reopened."""
    qs = _esc(control.get("qs", ""))
    body = (
        "<h1>Pairing window open</h1>"
        + "<p>The device is discoverable now. On your phone, open Bluetooth, pair "
        "with the device, then turn on Bluetooth tethering (Personal Hotspot) so "
        "it can reach the internet over the phone.</p>"
        + '<p><a href="/' + qs + '">Back to the dashboard</a></p>'
    )
    return _page("Kestrel - pair a phone", body)


def _cfg_get(cfg, table, key, default=None):
    """The value of ``table.key`` in a parsed config dict (``table`` may be
    dotted, e.g. ``capture.scan``), or ``default`` when unset."""
    node = cfg or {}
    for part in table.split("."):
        if not isinstance(node, dict):
            return default
        node = node.get(part)
        if node is None:
            return default
    if not isinstance(node, dict):
        return default
    return node.get(key, default)


def _coerce_setting(entry, raw):
    """Coerce a submitted form string to the setting's declared type. The apply
    helper re-validates authoritatively; this just turns the string into the
    right JSON type (and reports a bad number with an operator-facing message)."""
    raw = "" if raw is None else str(raw)
    vtype = entry["type"]
    if vtype == "bool":
        return raw.strip().lower() in ("1", "true", "on", "yes")
    if vtype == "int":
        try:
            return int(raw.strip())
        except ValueError:
            raise ValueError(entry["table"] + "." + entry["key"]
                             + " must be a whole number") from None
    if vtype == "float":
        try:
            return float(raw.strip())
        except ValueError:
            raise ValueError(entry["table"] + "." + entry["key"]
                             + " must be a number") from None
    return raw  # enum / str: the helper checks membership and length


def _settings_input(entry, current, disabled):
    """The ``<input>``/``<select>`` for one editable setting (no buttons)."""
    fid = "f-" + entry["table"].replace(".", "-") + "-" + entry["key"]
    dis = " disabled" if disabled else ""
    vtype = entry["type"]
    if vtype == "bool":
        on = bool(current)
        return ('<select name="value" id="' + _esc(fid) + '"' + dis + ">"
                '<option value="true"' + (" selected" if on else "") + ">on</option>"
                '<option value="false"' + ("" if on else " selected") + ">off</option>"
                "</select>")
    if vtype == "enum":
        cur = "" if current is None else str(current)
        opts = "".join(
            '<option value="' + _esc(choice) + '"'
            + (" selected" if choice == cur else "") + ">" + _esc(choice) + "</option>"
            for choice in entry["choices"])
        return ('<select name="value" id="' + _esc(fid) + '"' + dis + ">" + opts
                + "</select>")
    if vtype in ("int", "float"):
        step = "1" if vtype == "int" else "any"
        val = "" if current is None else str(current)
        return ('<input type="number" step="' + step + '" name="value" id="'
                + _esc(fid) + '" value="' + _esc(val) + '"' + dis + ">")
    val = "" if current is None else str(current)
    return ('<input type="text" name="value" id="' + _esc(fid) + '" value="'
            + _esc(val) + '"' + dis + ">")


def _settings_field_row(entry, cfg, control, ready):
    """One editable setting as its own one-key form (Apply + Reset)."""
    table, key = entry["table"], entry["key"]
    fid = "f-" + table.replace(".", "-") + "-" + key
    current = _cfg_get(cfg, table, key, entry.get("default"))
    parts = ['<div class="setfield">',
             '<label class="setkey" for="' + _esc(fid) + '">' + _esc(table + "." + key)
             + "</label>",
             '<div class="hint">' + _esc(entry["help"]) + "</div>",
             '<div class="hint">default: ' + _esc(_shown(entry.get("default")))
             + "</div>"]
    form = ['<form class="setform" method="post" action="'
            + _esc(_url("/settings", control)) + '">']
    if ready:
        form.append(_csrf_field(control))
    form.append('<input type="hidden" name="table" value="' + _esc(table) + '">')
    form.append('<input type="hidden" name="key" value="' + _esc(key) + '">')
    form.append(_settings_input(entry, current, not ready))
    if ready:
        form.append('<button type="submit">Apply</button>')
        form.append('<button type="submit" class="reset" name="reset" value="1">'
                    "Reset</button>")
    form.append("</form>")
    parts.append("".join(form))
    parts.append("</div>")
    return "".join(parts)


def _settings_ro_row(entry, cfg, control):
    """One read-only setting: shown so the operator knows it exists, with where
    it is actually changed. A secret's value is never shown - only whether it is
    configured."""
    table, key = entry["table"], entry["key"]
    if entry.get("secret"):
        shown = "configured" if str(_cfg_get(cfg, table, key, "") or "").strip() \
            else "not set"
    else:
        shown = _shown(_cfg_get(cfg, table, key, ""))
    link = ""
    cli = entry.get("cli") or ""
    if entry.get("reason") == config_catalog.RO_CONTROL and cli.startswith("/"):
        link = (' &middot; <a href="' + _esc(_url(cli, control))
                + '">change it &rarr;</a>')
    return ('<div class="setfield"><span class="setkey">' + _esc(table + "." + key)
            + '</span> <span class="roval">' + _esc(shown) + "</span>" + link
            + '<div class="hint">' + _esc(entry["help"]) + "</div></div>")


def _render_settings(cfg, control=None, message=None, error=False):
    """The advanced Settings page: the editable config.toml tunables grouped by
    area (each its own one-key form with inline help, default and reset),
    read-only rows for secrets and dedicated-control/lockout keys, links to each
    plugin's own settings, and a read-only note for the off-limits network file.
    Writes need web auth on (read-only otherwise)."""
    ready = bool(control and control.get("ready"))
    parts = ['<p><a href="' + _esc(_url("/", control)) + '">&larr; Dashboard</a></p>',
             "<h1>Advanced settings</h1>",
             '<p class="msg">Advanced settings. A wrong value can take the device '
             "off the air or stop capture. Change one thing at a time; each "
             "field&#39;s help lists safe values.</p>"]
    if message:
        parts.append('<p class="msg' + (" err" if error else "") + '">'
                     + _esc(message) + "</p>")
    if not ready:
        parts.append('<p class="notice">' + _esc(CONTROLS_OFF)
                     + " You can read the settings below; turn web auth on to "
                     "change them.</p>")
    toc = " &middot; ".join(
        '<a href="#' + _esc(s["anchor"]) + '">' + _esc(s["title"]) + "</a>"
        for s in config_catalog.SECTIONS)
    parts.append('<p class="hint">' + toc
                 + ' &middot; <a href="#plugins">Plugins</a>'
                 ' &middot; <a href="#network">Network file</a></p>')
    for section in config_catalog.SECTIONS:
        parts.append('<h2 id="' + _esc(section["anchor"]) + '">'
                     + _esc(section["title"]) + "</h2>")
        if section.get("intro"):
            parts.append('<p class="hint">' + _esc(section["intro"]) + "</p>")
        for entry in section["entries"]:
            if entry.get("editable"):
                parts.append(_settings_field_row(entry, cfg, control, ready))
            else:
                parts.append(_settings_ro_row(entry, cfg, control))

    parts.append('<h2 id="plugins">Plugins</h2>')
    parts.append('<p class="hint">Enable/disable and per-plugin options (including '
                 "secret keys, handled safely) live on each plugin&#39;s own "
                 "page.</p>")
    names = sorted((control or {}).get("enabled") or {})
    if names:
        for name in names:
            parts.append('<a class="navcard" href="' + _esc(_manage_url(name, control))
                         + '"><div class="scol"><span class="skey">Plugin</span>'
                         '<span class="sval">' + _esc(name) + "</span></div>"
                         '<span class="arrow" aria-hidden="true">&rarr;</span></a>')
    else:
        parts.append('<p class="hint">No plugins installed.</p>')

    note = config_catalog.ETC_DEFAULT_NOTE
    parts.append('<h2 id="network">Network file (not editable here)</h2>'
                 '<div class="card pad"><div class="scol">'
                 '<span class="skey">' + _esc(note["file"]) + "</span>"
                 '<span class="smeta">' + _esc(", ".join(note["keys"])) + "</span></div>"
                 '<div class="hint">' + _esc(note["why"]) + "</div>"
                 '<div class="hint">' + _esc(note["cli"]) + "</div></div>")
    parts.append('<p class="advlink"><a href="' + _esc(_url("/", control))
                 + '">&larr; Back to dashboard</a></p>')
    return _page("Settings - Kestrel", "".join(parts))


# A daemon restart (a settings save) can take the bounded plugin unload (25s)
# plus a cold start that re-reads the capture store and probes the e-ink panel,
# so the default window is about two minutes, not the old ~40s that gave up on
# a restart that was still in progress.
def _reconnect_script(target, probe, started, max_tries=120, start_ms=2000,
                      delay_ms=1000, giveup=None, fetch_ms=5000):
    """Inline poll used by every page that waits out a service gap: hit ``probe``
    (/status.json) until a *fresh* daemon answers (its ``started`` differs from
    ``started`` at render time), then go to ``target``. A failed fetch (the
    device still down) just retries. Covers a daemon restart, a reboot and an
    in-place update - a reboot/update needs a longer window than a restart, so
    ``max_tries``/``delay_ms`` size it. On running out it drops a message into
    the ``#wait`` element rather than redirecting.

    Each probe is aborted after ``fetch_ms``: a fetch stuck on a half-dead
    connection (the device dropped off the link mid-reboot) otherwise never
    settles, the try count stops advancing, and the page neither returns nor
    gives up until the browser's own TCP timeout, minutes later. Browsers
    without AbortController just fetch without the limit."""
    giveup = giveup or ("The dashboard has not come back yet; reload this page "
                        "to retry.")
    return (
        "<script>(function(){var target=" + _js(target)
        + ",probe=" + _js(probe) + ",started=" + _js(started)
        + ",max=" + str(int(max_tries)) + ",delay=" + str(int(delay_ms))
        + ",limit=" + str(int(fetch_ms))
        + ",tries=0;function poll(){tries++;if(tries>max){"
        "document.getElementById('wait').textContent=" + _js(giveup) + ";return;}"
        "var opts={cache:'no-store'},ctl=null,t=null;"
        "if(window.AbortController){ctl=new AbortController();opts.signal=ctl.signal;"
        "t=setTimeout(function(){ctl.abort();},limit);}"
        "fetch(probe,opts).then(function(r){return r.ok?r.json():null;})"
        ".then(function(s){if(t)clearTimeout(t);"
        "if(s&&s.started!==started){location.href=target;}"
        "else{setTimeout(poll,delay);}},"
        "function(){if(t)clearTimeout(t);setTimeout(poll,delay);});}"
        "setTimeout(poll," + str(int(start_ms)) + ");})();</script>"
    )


def _render_settings_applied(control, reply, started):
    """Shown after a [daemon]/[web] change that needs the plugin host restarted:
    poll /status.json until a new daemon answers, then return to Settings."""
    target = _url("/settings", control)
    probe = _url("/status.json", control)
    changed = ", ".join(reply.get("changed") or []) or "nothing"
    script = _reconnect_script(
        target, probe, started,
        giveup="The dashboard has not come back yet; check "
               "journalctl -u kestrel-daemon.")
    return _page(
        "Settings - Kestrel",
        "<h1>Advanced settings</h1>"
        '<p class="msg">Saved (' + _esc(changed) + ").</p>"
        '<p id="wait">Restarting the dashboard to apply it; this page returns to '
        'settings when it is back.</p><p><a href="' + _esc(target)
        + '">Back to settings</a></p>' + script,
    )


def _option_field(opt, spec, table, missing, disabled, plugin=""):
    """The form field for one declared option. Secrets are a password field
    that is never prefilled; leaving it empty keeps the stored value. A
    root_only option is shown read-only with the terminal command that sets it
    (the root helper refuses it from the page)."""
    spec = spec or {}
    type_name = spec.get("type", "str")
    field = "opt-" + opt
    fid = "f-" + opt
    required = spec.get("required") and "default" not in spec
    mark = ""
    if opt in missing:
        mark = ' <span class="missing">(required)</span>'
    elif required:
        mark = " *"
    current = table.get(opt, spec.get("default"))
    dis = " disabled" if disabled else ""
    hint = ""
    if spec.get("help"):
        hint += '<div class="hint">' + _esc(spec["help"]) + "</div>"
    if "default" in spec and not spec.get("secret"):
        hint += '<div class="hint">default: ' + _esc(_shown(spec["default"])) + "</div>"

    if spec.get("root_only"):
        if spec.get("secret"):
            shown = "set" if table.get(opt) else "not set"
        else:
            shown = _shown(current) or "not set"
        return (
            '<div class="ro-option"><b>' + _esc(opt) + "</b>: " + _esc(shown) + "</div>"
            '<div class="hint">Set from the terminal only: <code>sudo kestrel plugins '
            "enable " + _esc(plugin) + " --set " + _esc(opt) + "=...</code></div>" + hint
        )

    if type_name == "bool":
        return (
            '<input type="hidden" name="bool-' + _esc(opt) + '" value="1">'
            '<label class="check"><input type="checkbox" id="' + _esc(fid) + '" name="'
            + _esc(field) + '"' + (" checked" if current else "") + dis + "> "
            + _esc(opt) + mark + "</label>" + hint
        )
    label = '<label for="' + _esc(fid) + '">' + _esc(opt) + mark + "</label>"
    if type_name in ("int", "float"):
        step = "1" if type_name == "int" else "any"
        value = "" if current is None else str(current)
        return (label + '<input type="number" step="' + step + '" id="' + _esc(fid)
                + '" name="' + _esc(field) + '" value="' + _esc(value) + '"' + dis
                + ">" + hint)
    if type_name == "str" and spec.get("secret"):
        placeholder = "set (leave empty to keep it)" if table.get(opt) else "not set"
        return (label + '<input type="password" autocomplete="new-password" id="'
                + _esc(fid) + '" name="' + _esc(field) + '" value="" placeholder="'
                + placeholder + '"' + dis + ">")
    if type_name == "str":
        return (label + '<input type="text" id="' + _esc(fid) + '" name="'
                + _esc(field) + '" value="' + _esc("" if current is None else current)
                + '"' + dis + ">" + hint)
    return (label + '<div class="hint">' + _esc(type_name)
            + " option: edit it with the CLI or in config.toml</div>")


def _shown(value):
    if isinstance(value, bool):
        return "on" if value else "off"
    return "" if value is None else str(value)


def _render_manage(daemon, record, control, message=None, error=False,
                   missing=(), need_ack=False, table=None):
    """One plugin's settings page: enable/disable, its options, and (for a
    plugin that sends data off the device) the authorized-use confirmation.
    ``table`` overrides the configured values shown (a refused change)."""
    name = record.name
    health = record.health()
    if table is None:
        table = _configured_table(daemon, name)
    ready = control["ready"]
    caps = list(record.capabilities)
    detail = "; ".join(p for p in (health.get("reason"), health.get("note")) if p)

    parts = ['<p><a href="' + _esc(_url("/plugins", control)) + '">&larr; Plugins</a></p>',
             "<h1>" + _esc(name) + "</h1>"]
    description = getattr(record.cls, "__description__", "") if record.cls else ""
    if description:
        parts.append("<p>" + _esc(description) + "</p>")
    parts.append(
        "<p>state: <b>" + _esc(health.get("state")) + "</b>"
        + ((" (" + _esc(detail) + ")") if detail else "")
        + "<br>capabilities: " + (_esc(", ".join(caps)) or "-") + "</p>"
    )
    if message:
        parts.append('<p class="msg' + (" err" if error else "") + '">'
                     + _esc(message) + "</p>")
    if not ready:
        parts.append('<p class="notice">' + _esc(CONTROLS_OFF) + "</p>")
    if record.cls is None:
        parts.append("<p>This plugin failed to load, so its settings can't be "
                     "shown here.</p>")
        return _page(name + " - Kestrel", "".join(parts))

    dis = "" if ready else " disabled"
    form = ['<form method="post" action="' + _esc(_manage_url(name, control)) + '">']
    if ready:
        form.append(_csrf_field(control))
    form.append(
        '<input type="hidden" name="enabled-field" value="1">'
        '<label class="check"><input type="checkbox" name="enabled"'
        + (" checked" if table.get("enabled") else "") + dis + "> <b>Enabled</b></label>"
    )
    schema = getattr(record.cls, "__options__", {}) or {}
    for opt, spec in schema.items():
        form.append(_option_field(opt, spec, table, missing, not ready, plugin=name))
    if "network" in caps:
        if control.get("acked"):
            form.append('<p class="hint">Authorized-use confirmation: given.</p>')
        else:
            form.append(
                '<p class="msg' + (" err" if need_ack else "") + '">'
                + _esc(NETWORK_ACK_TEXT) + "</p>"
                '<label class="check"><input type="checkbox" name="ack" value="yes"'
                + dis + "> I am authorized to collect and share this data</label>"
            )
    form.append('<button type="submit"' + dis + ">Save</button>")
    form.append('<p class="hint">Saving restarts the plugin host to apply the '
                "change; * marks a required option.</p></form>")
    parts.append("".join(form))
    return _page(name + " - Kestrel", "".join(parts))


def _render_applied(name, reply, control, started):
    """Shown after a change is written: the daemon restarts a moment later, so
    poll /status.json until a new daemon answers, then go back to the plugin."""
    target = _manage_url(name, control)
    probe = _url("/status.json", control)
    changed = ", ".join(reply.get("changed") or []) or "nothing"
    script = _reconnect_script(
        target, probe, started,
        giveup="The plugin host has not come back yet; check "
               "journalctl -u kestrel-daemon.")
    return _page(
        name + " - Kestrel",
        "<h1>" + _esc(name) + "</h1>"
        '<p class="msg">Saved (' + _esc(changed) + ").</p>"
        '<p id="wait">Restarting the plugin host to apply it; this page returns '
        'to the plugin when it is back.</p><p><a href="' + _esc(target)
        + '">Back to ' + _esc(name) + "</a></p>" + script,
    )


def _esc(value):
    text = "" if value is None else str(value)
    return (
        text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def _js(value):
    """A JSON literal that is safe inside an inline <script>."""
    return json.dumps(value).replace("<", "\\u003c").replace(">", "\\u003e")


def _default_make_server(host, port, app):
    """The production server factory: a threaded werkzeug server. Imported
    lazily (and injectable) so the package imports and unit-tests without
    werkzeug installed."""
    from werkzeug.serving import WSGIRequestHandler, make_server

    # No per-request access log in the journal: it would record the token
    # (token auth rides in the query string) and a line a second while the
    # page waits for a restart. Warnings and errors still log.
    logging.getLogger("werkzeug").setLevel(logging.WARNING)
    return make_server(host, port, app, threaded=True,
                       request_handler=_request_handler(WSGIRequestHandler))


# Stall limit on each web connection's socket reads and writes. The threaded
# server has no timeout by default, so a client that connects and then goes
# silent before its request is complete - a phone dropping off BT PAN or Wi-Fi
# mid-request with no FIN, a half-sent form POST - pinned its thread in recv()
# forever, and those threads piled up. This bounds socket I/O only, not how
# long a page takes to build.
_CONNECTION_TIMEOUT = 15


def _request_handler(base):
    """``base`` (werkzeug's WSGIRequestHandler) with the connection timeout."""
    return type("KestrelRequestHandler", (base,), {"timeout": _CONNECTION_TIMEOUT})


class WebServer:
    """Runs one werkzeug server per bound address in a daemon thread, so the
    page is served on loopback and each management interface but never a
    wildcard.

    :meth:`reconcile` lets the daemon (re)bind as management interfaces gain or
    lose an IPv4 address after startup. This matters at boot: the daemon can
    come up before ``wlan0`` has a DHCP lease, in which case the first bind is
    loopback-only and the page would be unreachable off-box until a restart.

    A bound host is released only after ``unbind_after`` consecutive reconciles
    without it: one missed address lookup (``ip`` timing out under load reads as
    "no address") must not drop the page off the interface the operator is on."""

    def __init__(self, app, hosts, port, logger, server_factory=None,
                 unbind_after=2):
        self.app = app
        self.hosts = list(dict.fromkeys(hosts))
        self.port = int(port)
        self.log = logger
        self._make_server = server_factory or _default_make_server
        self._unbind_after = max(1, int(unbind_after))
        self._bound = {}        # host -> (server, thread)
        self._missing = {}      # bound host -> consecutive reconciles without it
        self._lock = threading.Lock()

    def start(self):
        """Bind the initial host set. Returns True if at least one address bound
        (loopback is always in the set, so this is effectively always True)."""
        return self.reconcile(self.hosts)

    def reconcile(self, hosts):
        """Bind every wanted host not already served, and release any served
        host no longer wanted. Idempotent for an unchanged set; returns True if
        any address is bound now."""
        wanted = list(dict.fromkeys(hosts))
        with self._lock:
            for host in wanted:
                self._missing.pop(host, None)
                if host not in self._bound:
                    self._bind_locked(host)
            for host in [h for h in self._bound if h not in wanted]:
                misses = self._missing.get(host, 0) + 1
                if misses >= self._unbind_after:
                    self._unbind_locked(host)
                else:
                    self._missing[host] = misses
            return bool(self._bound)

    def bound_hosts(self):
        with self._lock:
            return sorted(self._bound)

    def stop(self):
        with self._lock:
            for host in list(self._bound):
                self._unbind_locked(host)

    # -- helpers; call with self._lock held ------------------------------
    def _bind_locked(self, host):
        try:
            server = self._make_server(host, self.port, self.app)
        except OSError as exc:
            self.log.warning("web: cannot bind %s:%s (%s)", host, self.port, exc)
            return
        thread = threading.Thread(
            target=server.serve_forever,
            name="pwn-web-%s" % host, daemon=True,
        )
        thread.start()
        self._bound[host] = (server, thread)
        self.log.info("web: serving on %s:%s", host, self.port)

    def _unbind_locked(self, host):
        self._missing.pop(host, None)
        server, _thread = self._bound.pop(host)
        try:
            server.shutdown()
        except Exception:  # noqa: BLE001
            pass
        self.log.info("web: stopped serving on %s:%s", host, self.port)
