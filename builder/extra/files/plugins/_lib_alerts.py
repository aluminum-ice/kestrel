"""Shared plugin library: alerts to a messaging service.

The alert logic behind the ``ntfy`` and ``telegram`` plugins, kept once here
so a new service is only its settings and one request. Load it with
``kestrel_pi.load_library`` (see docs/developer/plugin-libraries.md)::

    from kestrel_pi import Plugin, httpsafe, load_library

    alerts = load_library(__file__, "alerts", api=1)

    class MyService(Plugin):
        __capabilities__ = ("network", "capture-read", "ui", "web")
        __options__ = {...}      # the alert options below, plus your own

        def on_loaded(self):
            self.alerts = alerts.Alerts(self, "my-service", self._deliver,
                                        alerts.state_dir("kestrel-my-service"))
            self.alerts.start()

        def on_unload(self):
            self.alerts.stop()

        # forward on_handshake, on_internet_available, on_ui_setup and
        # on_ui_update to the same-named Alerts methods

        def _deliver(self, requests, msg, timeout):
            # One request to the service. msg is {kind, title, body, urgent, ts}.
            return requests.post("https://service.example.com/send",
                                 json={"text": msg["title"] + "\n" + msg["body"]},
                                 timeout=timeout, allow_redirects=False, stream=True)

What :class:`Alerts` handles: a digest of new handshakes every
``digest_minutes`` (counts only unless ``include_names``), a low battery or a
switch to battery power (needs a UPS plugin), a newer release (the ``updates``
state file), capture stopping while it is switched on, and (opt-in) the device
coming online. When to send: ``max_per_hour``, ``quiet_hours`` (urgent alerts
get through) and backoff after a failure. An offline queue (``QUEUE_MAX``, kept
in the plugin's own state directory), a worker thread that drains it, the
dashboard card (``on_ui_setup``/``on_ui_update``), the Send test button
(``send_test``) and the shared lines of the status page (``status_page``).

The options it reads live from ``plugin.options``, which the plugin declares
itself: ``events``, ``digest_minutes``, ``include_names``,
``battery_percent``, ``max_per_hour``, ``quiet_hours`` and ``timeout``.

Rules it keeps for every service:

- Messages are built from typed fields only - counts, a percentage, a release
  tag and, only when asked, cleaned network names - so a hashline, an address,
  a location or a recovered password can never end up in one.
- Every send needs the plugin's ``network`` capability, and goes through
  ``kestrel_pi.httpsafe``: the plugin's request must not follow redirects, and
  the reply is size-capped.
- A transport error is reduced to its type before it is logged or shown, since
  its text carries the URL and a credential may ride in it (Telegram's bot
  token is in the path).

``LIB_API`` follows docs/developer/plugin-libraries.md: a new option or
function bumps the minor; a changed signature of ``Alerts``, ``deliver`` or a
public function bumps the major. Names with a leading underscore are private.
"""

import json
import os
import tempfile
import threading
import time

from kestrel_pi import httpsafe
from kestrel_pi.config import DEFAULT_CONFIG_PATH, load_config

LIB_API = (1, 0)

UPDATE_STATE = "/var/lib/kestrel/update/state.json"
EVENTS = ("handshakes", "battery", "update", "capture", "online")
DEFAULT_EVENTS = "handshakes,battery,update,capture"
QUEUE_MAX = 50
MAX_NAMES = 5


def state_dir(name):
    """``~/.local/state/<name>`` of the account the daemon runs as."""
    try:
        import pwd

        home = pwd.getpwuid(os.getuid()).pw_dir
    except Exception:  # noqa: BLE001 - fall back to $HOME
        home = os.path.expanduser("~")
    return os.path.join(home, ".local", "state", name)


def write_json(path, data):
    directory = os.path.dirname(path)
    os.makedirs(directory, mode=0o700, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".tmp-", dir=directory)
    with os.fdopen(fd, "w") as handle:
        json.dump(data, handle)
    os.replace(tmp, path)


def read_json(path, default):
    try:
        with open(path) as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return default


def clean_name(name):
    """A network name made safe for a one-line message: printable, short."""
    text = "".join(ch for ch in str(name) if ch.isprintable()).strip()
    return text[:32] or "(hidden)"


def message(kind, title, body, urgent=False):
    return {"kind": kind, "title": title, "body": body, "urgent": bool(urgent),
            "ts": int(time.time())}


def handshake_digest(count, names, minutes, include_names):
    """The handshake digest. ``names`` is the set of network names seen."""
    noun = "handshake" if count == 1 else "handshakes"
    nets = len(names)
    body = "%d new %s from %d network%s in the last %d minutes." % (
        count, noun, nets, "" if nets == 1 else "s", minutes)
    if include_names and names:
        shown = sorted(clean_name(n) for n in names)
        extra = len(shown) - MAX_NAMES
        body += " Networks: " + ", ".join(shown[:MAX_NAMES])
        if extra > 0:
            body += " (+%d more)" % extra
        body += "."
    return message("handshakes", "Kestrel: %d new %s" % (count, noun), body)


def in_quiet_hours(spec, hour):
    """``spec`` like ``"22-07"`` (local hours); True when ``hour`` is inside."""
    try:
        start, end = (int(part) for part in str(spec).split("-"))
    except ValueError:
        return False
    if not (0 <= start <= 23 and 0 <= end <= 23) or start == end:
        return False
    return start <= hour < end if start < end else (hour >= start or hour < end)


class Alerts:
    """The alert state and worker.

    ``service`` names it in errors and logs; ``deliver(requests, msg, timeout)``
    makes the service's request (``allow_redirects=False``, ``stream=True``) and
    returns the response, raising :class:`httpsafe.UnsafeResponse` if the setup
    is incomplete. Options are read live from ``plugin.options``."""

    def __init__(self, plugin, service, deliver, directory):
        self.plugin = plugin
        self.service = service
        self._deliver = deliver
        self.dir = directory
        self.log = plugin.context.logger
        self.config_path = DEFAULT_CONFIG_PATH
        self.update_state = UPDATE_STATE
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._wake = threading.Event()
        self.queue = list(read_json(os.path.join(directory, "queue.json"), []))[-QUEUE_MAX:]
        self._seen = read_json(os.path.join(directory, "seen.json"), {})
        self._sent_times = []
        self.sent_total = 0
        self.last_error = ""
        self.backoff_until = 0.0
        self._backoff = 60
        # handshake digest window
        self._hs_count = 0
        self._hs_names = set()
        self._hs_since = None
        # edge trackers
        self._battery_status = None
        self._battery_low_sent = False
        self._capture_was_active = None
        self._capture_down_ticks = 0
        self._online_sent = False
        self.update_checked = 0.0
        self.worker = threading.Thread(target=self._run, name=service + "-worker", daemon=True)

    def start(self):
        self.worker.start()

    def stop(self):
        self._stop.set()
        self._wake.set()
        if self.worker.is_alive():
            self.worker.join(timeout=5)

    # -- options ----------------------------------------------------------------
    @property
    def options(self):
        return self.plugin.options

    def events(self):
        raw = self.options.get("events")
        raw = DEFAULT_EVENTS if raw is None else raw
        return {e.strip() for e in str(raw).split(",") if e.strip() in EVENTS}

    def int_option(self, key, default, low=0):
        try:
            return max(low, int(self.options.get(key, default)))
        except (TypeError, ValueError):
            return default

    # -- events ------------------------------------------------------------------
    def enqueue(self, msg):
        with self._lock:
            self.queue.append(msg)
            del self.queue[:-QUEUE_MAX]
            write_json(os.path.join(self.dir, "queue.json"), self.queue)
        self._wake.set()

    def on_handshake(self, event):
        if "handshakes" not in self.events():
            return
        with self._lock:
            if self._hs_since is None:
                self._hs_since = time.monotonic()
            self._hs_count += 1
            self._hs_names.add(getattr(event, "essid", "") or "")

    def on_internet_available(self):
        if "online" in self.events() and not self._online_sent:
            self._online_sent = True
            self.enqueue(message("online", "Kestrel is online",
                                 "The device is online and the dashboard is reachable."))
        self._wake.set()

    def on_ui_setup(self, ui):
        ui.add_element("sent", "0", label="Alerts sent", kind="number")
        ui.add_element("queued", "0", label="Waiting", kind="number")
        ui.add_element("status", "starting", label="Status")

    def on_ui_update(self, ui, state):
        self.check_battery(getattr(state, "battery", None))
        self.check_capture(bool(getattr(state, "capture_active", False)))
        with self._lock:
            ui.set("sent", str(self.sent_total))
            ui.set("queued", str(len(self.queue)))
            ui.set("status", ("error: " + self.last_error[:60]) if self.last_error
                   else "ready")

    def check_battery(self, battery):
        if "battery" not in self.events() or not battery:
            return
        try:
            pct = float(battery.get("percent"))
        except (TypeError, ValueError):
            return
        status = str(battery.get("status") or "")
        threshold = self.int_option("battery_percent", 20)
        if self._battery_status in ("charging", "full") and status == "discharging":
            self.enqueue(message("battery", "Kestrel is on battery",
                                 "External power was lost; the battery is at %d%%." % pct))
        if status == "discharging" and pct <= threshold and not self._battery_low_sent:
            self._battery_low_sent = True
            self.enqueue(message("battery", "Kestrel battery low",
                                 "The battery is at %d%%." % pct, urgent=True))
        elif status != "discharging" or pct > threshold + 5:
            self._battery_low_sent = False
        self._battery_status = status or self._battery_status

    def _capture_enabled(self):
        try:
            capture = (load_config(self.config_path) or {}).get("capture") or {}
        except Exception:  # noqa: BLE001
            return False
        return capture.get("enabled") is True

    def check_capture(self, active):
        """Alert once when capture goes down and stays down for two updates in a
        row (so a relaunch between scan plans isn't an alert) while it is still
        switched on in the config (switching it off isn't an alert either)."""
        if "capture" not in self.events():
            return
        if active:
            self._capture_was_active = True
            self._capture_down_ticks = 0
            return
        if not self._capture_was_active:
            return
        self._capture_down_ticks += 1
        if self._capture_down_ticks >= 2:
            self._capture_was_active = False
            if self._capture_enabled():
                self.enqueue(message("capture", "Kestrel capture stopped",
                                     "Capture stopped while it is still switched on. "
                                     "Check the dashboard.", urgent=True))

    def check_update(self):
        if "update" not in self.events() or time.monotonic() - self.update_checked < 1800:
            return
        self.update_checked = time.monotonic()
        state = read_json(self.update_state, {})
        tag = str(state.get("latest") or "")
        if state.get("update_available") and tag and self._seen.get("update") != tag:
            self._seen["update"] = tag
            write_json(os.path.join(self.dir, "seen.json"), self._seen)
            self.enqueue(message("update", "Kestrel update available",
                                 "Release %s is available. Open the dashboard to "
                                 "update." % clean_name(tag)))

    def flush_digest(self, force=False):
        minutes = self.int_option("digest_minutes", 30, low=1)
        with self._lock:
            due = self._hs_since is not None and (
                force or time.monotonic() - self._hs_since >= minutes * 60)
            if not due or not self._hs_count:
                return
            msg = handshake_digest(self._hs_count, self._hs_names, minutes,
                                   bool(self.options.get("include_names")))
            self._hs_count, self._hs_names, self._hs_since = 0, set(), None
        self.enqueue(msg)

    # -- sending ------------------------------------------------------------------
    def _run(self):
        while not self._stop.is_set():
            try:
                self.flush_digest()
                self.check_update()
                self.drain()
            except Exception:  # noqa: BLE001 - the worker must keep going
                self.log.exception("%s: worker pass failed", self.service)
            self._wake.wait(timeout=30)
            self._wake.clear()

    def _may_send(self, msg, now):
        hour = time.localtime(now).tm_hour
        if not msg.get("urgent") and in_quiet_hours(self.options.get("quiet_hours"), hour):
            return False
        self._sent_times = [t for t in self._sent_times if now - t < 3600]
        return msg.get("urgent") or len(self._sent_times) < self.int_option("max_per_hour", 6, low=1)

    def drain(self):
        if not self.plugin.context.connectivity.online or time.monotonic() < self.backoff_until:
            return
        for _ in range(QUEUE_MAX + 1):
            now = time.time()
            with self._lock:
                pending = [m for m in self.queue if self._may_send(m, now)]
                pending.sort(key=lambda m: not m.get("urgent"))
                msg = pending[0] if pending else None
            if msg is None:
                return
            try:
                self.send(msg)
            except Exception as exc:  # noqa: BLE001 - kept for retry
                with self._lock:
                    self.last_error = str(exc)[:200]
                self.log.warning("%s: send failed (%s); will retry", self.service, exc)
                self.backoff_until = time.monotonic() + self._backoff
                self._backoff = min(3600, self._backoff * 2)
                return
            with self._lock:
                self.queue.remove(msg)
                write_json(os.path.join(self.dir, "queue.json"), self.queue)
                self._sent_times.append(now)
                self.sent_total += 1
                self.last_error = ""
            self._backoff = 60

    def send(self, msg):
        """Deliver one message now; raises on failure."""
        if not self.plugin.context.require_capability("network"):
            raise httpsafe.UnsafeResponse("the network capability is not declared")
        import requests

        try:
            resp = self._deliver(requests, msg, self.int_option("timeout", 15, low=1))
        except requests.RequestException as exc:
            # The exception text carries the URL, and a credential may ride in
            # it (Telegram's bot token is in the path): report the kind only.
            raise httpsafe.UnsafeResponse("%s: %s" % (self.service, type(exc).__name__)) from None
        httpsafe.check_response(resp)
        httpsafe.read_capped(resp, 64 * 1024)
        if resp.status_code >= 400:
            raise httpsafe.UnsafeResponse("%s answered HTTP %d" % (self.service, resp.status_code))

    # -- pages ----------------------------------------------------------------------
    def send_test(self):
        """The Send test button: a redirect back to the Plugins page with the result."""
        context = self.plugin.context
        try:
            self.send(message("test", "Kestrel test", "Notifications from this device work."))
        except Exception as exc:  # noqa: BLE001
            with self._lock:
                self.last_error = str(exc)[:200]
            return context.redirect("Test notification failed: %s" % exc, error=True)
        return context.redirect("Test notification sent.")

    def status_page(self, title, setup_rows, token=""):
        """The plugin's status page: its own ``setup_rows`` (already escaped
        ``<li>`` items), then the shared lines. The links back are added by the
        page shell; ``token`` is no longer used and is kept so callers built for
        API 1.0 still work."""
        esc = self.plugin.context.esc
        with self._lock:
            queued, sent, error = len(self.queue), self.sent_total, self.last_error
        rows = list(setup_rows)
        rows.append("<li>Alerts: %s</li>" % esc(", ".join(sorted(self.events())) or "none"))
        rows.append("<li>Sent since start: %d &middot; waiting: %d</li>" % (sent, queued))
        if error:
            rows.append('<li class="err">Last error: %s</li>' % esc(error))
        return ("<h1>%s</h1><ul>" % esc(title) + "".join(rows) + "</ul>"
                '<p class="hint">Use <b>Send test</b> on the Plugins page to check the '
                "setup. Alerts raised while offline wait and are sent when the device "
                "is back online.</p>")
