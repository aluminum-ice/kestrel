"""notify - short push alerts to your phone when something worth knowing happens.

Sends a few kinds of alert to an **ntfy** topic (self-hostable; the default is
the public ntfy.sh server) or a **Telegram** chat:

- ``handshakes``: a digest of new handshakes every ``digest_minutes`` - counts
  only by default, network names only with ``include_names``;
- ``battery``: the battery is low (``battery_percent``), or the device switched
  to battery power (needs the ``ups-hat`` plugin);
- ``update``: a newer Kestrel release is available (read from the ``updates``
  plugin's state file);
- ``capture``: capture stopped while it is still switched on;
- ``online``: the device is online after start-up (off by default).

Messages are built from typed fields - counts, a percentage, a release tag and,
only when asked, network names - so a hashline, an address, a location or a
recovered password can never end up in one.

It sends data off the device, so it declares the ``network`` capability:
enabling it takes the authorized-use acknowledgment, and it ships disabled.
Every request goes through ``kestrel_pi.httpsafe`` (https only, no redirects,
capped reads). Alerts raised while offline wait in a small queue in
``~/.local/state/kestrel-notify/`` and are sent when a connection comes back;
``max_per_hour`` and ``quiet_hours`` keep them from becoming noise.

There are no commands back: the plugin only sends.
"""

from __future__ import annotations

import json
import os
import secrets
import tempfile
import threading
import time

from kestrel_pi import Plugin, httpsafe
from kestrel_pi.config import DEFAULT_CONFIG_PATH, load_config

UPDATE_STATE = "/var/lib/kestrel/update/state.json"
TELEGRAM_API = "https://api.telegram.org"
EVENTS = ("handshakes", "battery", "update", "capture", "online")
DEFAULT_EVENTS = "handshakes,battery,update,capture"
QUEUE_MAX = 50
MAX_NAMES = 5


def _state_dir():
    """``~/.local/state/kestrel-notify`` of the account the daemon runs as."""
    try:
        import pwd

        home = pwd.getpwuid(os.getuid()).pw_dir
    except Exception:  # noqa: BLE001 - fall back to $HOME
        home = os.path.expanduser("~")
    return os.path.join(home, ".local", "state", "kestrel-notify")


def _write_json(path, data):
    directory = os.path.dirname(path)
    os.makedirs(directory, mode=0o700, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".tmp-", dir=directory)
    with os.fdopen(fd, "w") as handle:
        json.dump(data, handle)
    os.replace(tmp, path)


def _read_json(path, default):
    try:
        with open(path) as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return default


def _clean_name(name):
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
        shown = sorted(_clean_name(n) for n in names)
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


class Notify(Plugin):
    __author__ = "kestrel"
    __version__ = "0.1.0"
    __license__ = "GPL3"
    __description__ = "Push alerts to ntfy or Telegram: new handshakes, battery, updates."

    __target_api__ = 2
    # Sends alerts off the device (network: enabling it takes the authorized-use
    # acknowledgment), counts new handshakes (capture-read) and shows a small
    # dashboard card (ui) and a status page (web).
    __capabilities__ = ("network", "capture-read", "ui", "web")
    __optional_requires__ = ("battery",)
    __actions__ = (
        {"label": "Status", "path": "index", "method": "get", "order": 1},
        {"label": "Send test", "path": "test", "method": "post", "order": 2},
    )
    __options__ = {
        "backend": {"type": "str", "default": "ntfy",
                    "help": "Where alerts go: ntfy or telegram."},
        "ntfy_server": {"type": "str", "default": "https://ntfy.sh",
                        "help": "The ntfy server (https). A server you run yourself "
                                "keeps alerts off third-party machines."},
        "ntfy_topic": {"type": "str", "default": "", "secret": True,
                       "help": "Leave empty to have a long random topic made for you "
                               "(shown on the plugin's Status page). Anyone who knows "
                               "the topic can read it, so keep it private."},
        "ntfy_token": {"type": "str", "default": "", "secret": True,
                       "help": "An ntfy access token, if your server needs one."},
        "telegram_token": {"type": "str", "default": "", "secret": True,
                           "help": "The bot token from Telegram's BotFather."},
        "telegram_chat": {"type": "str", "default": "", "secret": True,
                          "help": "Your chat id with the bot."},
        "events": {"type": "str", "default": "handshakes,battery,update,capture",
                   "help": "Comma-separated: handshakes, battery, update, capture, "
                           "online."},
        "digest_minutes": {"type": "int", "default": 30,
                           "help": "How often new handshakes are summed into one alert."},
        "include_names": {"type": "bool", "default": False,
                          "help": "Name the networks in the handshake digest. Off "
                                  "sends counts only."},
        "battery_percent": {"type": "int", "default": 20,
                            "help": "Alert when the battery falls to this charge."},
        "max_per_hour": {"type": "int", "default": 6,
                         "help": "At most this many alerts an hour; the rest wait."},
        "quiet_hours": {"type": "str", "default": "",
                        "help": "Hold alerts during these local hours, e.g. 22-07 "
                                "(a low battery still gets through)."},
        "timeout": {"type": "int", "default": 15},
    }

    # -- lifecycle --------------------------------------------------------------
    def on_loaded(self):
        self.log = self.context.logger
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._dir = _state_dir()
        self._config_path = DEFAULT_CONFIG_PATH
        self._update_state = UPDATE_STATE
        self._queue = list(_read_json(os.path.join(self._dir, "queue.json"), []))[-QUEUE_MAX:]
        self._seen = _read_json(os.path.join(self._dir, "seen.json"), {})
        self._sent_times = []
        self._sent_total = 0
        self._last_error = ""
        self._backoff_until = 0.0
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
        self._update_checked = 0.0
        self._worker = threading.Thread(target=self._run, name="notify-worker", daemon=True)
        self._worker.start()

    def on_unload(self):
        self._stop.set()
        self._wake.set()
        self._worker.join(timeout=5)

    # -- options ----------------------------------------------------------------
    def _events(self):
        raw = self.options.get("events")
        raw = DEFAULT_EVENTS if raw is None else raw
        return {e.strip() for e in str(raw).split(",") if e.strip() in EVENTS}

    def _int(self, key, default, low=0):
        try:
            return max(low, int(self.options.get(key, default)))
        except (TypeError, ValueError):
            return default

    def _topic(self):
        """The ntfy topic: the option, else a long random one made once and kept."""
        topic = (self.options.get("ntfy_topic") or "").strip()
        if topic:
            return topic
        path = os.path.join(self._dir, "topic")
        try:
            with open(path) as handle:
                topic = handle.read().strip()
        except OSError:
            topic = ""
        if not topic:
            topic = "kestrel-" + secrets.token_urlsafe(18).replace("_", "x").replace("-", "y")
            os.makedirs(self._dir, mode=0o700, exist_ok=True)
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, "w") as handle:
                handle.write(topic)
        return topic

    # -- events ------------------------------------------------------------------
    def _enqueue(self, msg):
        with self._lock:
            self._queue.append(msg)
            del self._queue[:-QUEUE_MAX]
            _write_json(os.path.join(self._dir, "queue.json"), self._queue)
        self._wake.set()

    def on_handshake(self, event):
        if "handshakes" not in self._events():
            return
        with self._lock:
            if self._hs_since is None:
                self._hs_since = time.monotonic()
            self._hs_count += 1
            self._hs_names.add(getattr(event, "essid", "") or "")

    def on_internet_available(self):
        if "online" in self._events() and not self._online_sent:
            self._online_sent = True
            self._enqueue(message("online", "Kestrel is online",
                                  "The device is online and the dashboard is reachable."))
        self._wake.set()

    def on_ui_setup(self, ui):
        ui.add_element("sent", "0", label="Alerts sent", kind="number")
        ui.add_element("queued", "0", label="Waiting", kind="number")
        ui.add_element("status", "starting", label="Status")

    def on_ui_update(self, ui, state):
        self._check_battery(getattr(state, "battery", None))
        self._check_capture(bool(getattr(state, "capture_active", False)))
        with self._lock:
            ui.set("sent", str(self._sent_total))
            ui.set("queued", str(len(self._queue)))
            ui.set("status", ("error: " + self._last_error[:60]) if self._last_error
                   else "ready")

    def _check_battery(self, battery):
        if "battery" not in self._events() or not battery:
            return
        try:
            pct = float(battery.get("percent"))
        except (TypeError, ValueError):
            return
        status = str(battery.get("status") or "")
        threshold = self._int("battery_percent", 20)
        if (self._battery_status in ("charging", "full") and status == "discharging"):
            self._enqueue(message("battery", "Kestrel is on battery",
                                  "External power was lost; the battery is at %d%%." % pct))
        if status == "discharging" and pct <= threshold and not self._battery_low_sent:
            self._battery_low_sent = True
            self._enqueue(message("battery", "Kestrel battery low",
                                  "The battery is at %d%%." % pct, urgent=True))
        elif status != "discharging" or pct > threshold + 5:
            self._battery_low_sent = False
        self._battery_status = status or self._battery_status

    def _capture_enabled(self):
        try:
            capture = (load_config(self._config_path) or {}).get("capture") or {}
        except Exception:  # noqa: BLE001
            return False
        return capture.get("enabled") is True

    def _check_capture(self, active):
        """Alert once when capture goes down and stays down for two updates in a
        row (so a relaunch between scan plans isn't an alert) while it is still
        switched on in the config (switching it off isn't an alert either)."""
        if "capture" not in self._events():
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
                self._enqueue(message("capture", "Kestrel capture stopped",
                                      "Capture stopped while it is still switched on. "
                                      "Check the dashboard.", urgent=True))

    def _check_update(self):
        if "update" not in self._events() or time.monotonic() - self._update_checked < 1800:
            return
        self._update_checked = time.monotonic()
        state = _read_json(self._update_state, {})
        tag = str(state.get("latest") or "")
        if state.get("update_available") and tag and self._seen.get("update") != tag:
            self._seen["update"] = tag
            _write_json(os.path.join(self._dir, "seen.json"), self._seen)
            self._enqueue(message("update", "Kestrel update available",
                                  "Release %s is available. Open the dashboard to "
                                  "update." % _clean_name(tag)))

    def _flush_digest(self, force=False):
        minutes = self._int("digest_minutes", 30, low=1)
        with self._lock:
            due = self._hs_since is not None and (
                force or time.monotonic() - self._hs_since >= minutes * 60)
            if not due or not self._hs_count:
                return
            msg = handshake_digest(self._hs_count, self._hs_names, minutes,
                                   bool(self.options.get("include_names")))
            self._hs_count, self._hs_names, self._hs_since = 0, set(), None
        self._enqueue(msg)

    # -- sending ------------------------------------------------------------------
    def _run(self):
        while not self._stop.is_set():
            try:
                self._flush_digest()
                self._check_update()
                self._drain()
            except Exception:  # noqa: BLE001 - the worker must keep going
                self.log.exception("notify: worker pass failed")
            self._wake.wait(timeout=30)
            self._wake.clear()

    def _may_send(self, msg, now):
        hour = time.localtime(now).tm_hour
        if not msg.get("urgent") and in_quiet_hours(self.options.get("quiet_hours"), hour):
            return False
        self._sent_times = [t for t in self._sent_times if now - t < 3600]
        return msg.get("urgent") or len(self._sent_times) < self._int("max_per_hour", 6, low=1)

    def _drain(self):
        if not self.context.connectivity.online or time.monotonic() < self._backoff_until:
            return
        for _ in range(QUEUE_MAX + 1):
            now = time.time()
            with self._lock:
                pending = [m for m in self._queue if self._may_send(m, now)]
                pending.sort(key=lambda m: not m.get("urgent"))
                msg = pending[0] if pending else None
            if msg is None:
                return
            try:
                self.send(msg)
            except Exception as exc:  # noqa: BLE001 - kept for retry
                with self._lock:
                    self._last_error = str(exc)[:200]
                self.log.warning("notify: send failed (%s); will retry", exc)
                self._backoff_until = time.monotonic() + self._backoff
                self._backoff = min(3600, self._backoff * 2)
                return
            with self._lock:
                self._queue.remove(msg)
                _write_json(os.path.join(self._dir, "queue.json"), self._queue)
                self._sent_times.append(now)
                self._sent_total += 1
                self._last_error = ""
            self._backoff = 60

    def send(self, msg):
        """Send one message through the chosen backend; raises on failure."""
        if not self.context.require_capability("network"):
            raise httpsafe.UnsafeResponse("the network capability is not declared")
        import requests

        backend = (self.options.get("backend") or "ntfy").strip().lower()
        try:
            resp = self._post(requests, backend, msg)
        except requests.RequestException as exc:
            # The exception text carries the URL, and a Telegram token rides in
            # its path: report the kind of failure only.
            raise httpsafe.UnsafeResponse("%s: %s" % (backend, type(exc).__name__)) from None
        httpsafe.check_response(resp)
        httpsafe.read_capped(resp, 64 * 1024)
        if resp.status_code >= 400:
            raise httpsafe.UnsafeResponse("%s answered HTTP %d" % (backend, resp.status_code))

    def _post(self, requests, backend, msg):
        timeout = self._int("timeout", 15, low=1)
        if backend == "telegram":
            token = (self.options.get("telegram_token") or "").strip()
            chat = (self.options.get("telegram_chat") or "").strip()
            if not token or not chat:
                raise httpsafe.UnsafeResponse("telegram_token and telegram_chat are needed")
            return requests.post(
                "%s/bot%s/sendMessage" % (TELEGRAM_API, token),
                json={"chat_id": chat, "text": msg["title"] + "\n" + msg["body"],
                      "disable_web_page_preview": True},
                timeout=timeout, allow_redirects=False, stream=True)
        server = httpsafe.https_endpoint(self.options.get("ntfy_server"), "https://ntfy.sh")
        headers = {"Title": msg["title"], "Tags": "bird",
                   "Priority": "high" if msg.get("urgent") else "default"}
        token = (self.options.get("ntfy_token") or "").strip()
        if token:
            headers["Authorization"] = "Bearer " + token
        return requests.post("%s/%s" % (server, self._topic()),
                             data=msg["body"].encode("utf-8"), headers=headers,
                             timeout=timeout, allow_redirects=False, stream=True)

    # -- pages ----------------------------------------------------------------------
    def on_webhook(self, path, request):
        if path == "test" and getattr(request, "method", "GET") == "POST":
            try:
                self.send(message("test", "Kestrel test",
                                  "Notifications from this device work."))
            except Exception as exc:  # noqa: BLE001
                with self._lock:
                    self._last_error = str(exc)[:200]
                return self.context.redirect("Test notification failed: %s" % exc,
                                             error=True)
            return self.context.redirect("Test notification sent.")
        if path not in ("", "index"):
            return self.context.page("Notifications", "<p>There's nothing here.</p>",
                                     status=404)
        return self.context.page("Notifications", self._status_html())

    def _status_html(self):
        esc = self.context.esc
        backend = (self.options.get("backend") or "ntfy").strip().lower()
        authed = getattr(self.context, "web_authed", False)
        with self._lock:
            queued, sent, error = len(self._queue), self._sent_total, self._last_error
        rows = ["<li>Backend: <b>%s</b></li>" % esc(backend)]
        if backend == "telegram":
            rows.append("<li>Chat: %s</li>" % ("set" if self.options.get("telegram_chat")
                                               else "<b>not set</b>"))
        elif (self.options.get("ntfy_topic") or "").strip():
            rows.append("<li>Topic: the one you set in the plugin's settings.</li>")
        elif authed:
            # Only a topic this plugin made is shown (a topic you set is a secret
            # option, never echoed); the login is needed to see it.
            server = (self.options.get("ntfy_server") or "https://ntfy.sh").rstrip("/")
            rows.append("<li>Subscribe in the ntfy app to topic <code>%s</code> on "
                        "<code>%s</code>. Keep the topic private: anyone who knows it "
                        "can read your alerts.</li>" % (esc(self._topic()), esc(server)))
        else:
            rows.append("<li>The topic is shown only with the web login on.</li>")
        rows.append("<li>Alerts: %s</li>" % esc(", ".join(sorted(self._events())) or "none"))
        rows.append("<li>Sent since start: %d &middot; waiting: %d</li>" % (sent, queued))
        if error:
            rows.append('<li class="err">Last error: %s</li>' % esc(error))
        return ("<h1>Notifications</h1><ul>" + "".join(rows) + "</ul>"
                '<p class="hint">Use <b>Send test</b> on the Plugins page to check the '
                "setup. Alerts raised while offline wait and are sent when the device "
                "is back online.</p>")
