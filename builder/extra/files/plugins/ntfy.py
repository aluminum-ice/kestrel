"""ntfy - short push alerts to an ntfy topic when something worth knowing happens.

ntfy is self-hostable; the default server is the public ntfy.sh. Leave
``topic`` empty and a long random one is made for you, kept ``0600`` in
``~/.local/state/kestrel-ntfy/`` and shown on the Status page (signed in only).

The alerts themselves - new handshakes (counts only unless ``include_names``),
battery, a new release, capture stopping, coming online - and the queue, rate
limit and quiet hours come from the shared ``alerts`` plugin library
(``_lib_alerts.py``), which the ``telegram`` plugin uses too; this file holds only
the ntfy settings, the request that delivers a message and its status page.

It sends data off the device, so it declares the ``network`` capability:
enabling it takes the authorized-use acknowledgment, and it ships disabled.
Requests go through ``kestrel_pi.httpsafe`` (https only, no redirects, capped
reads). There are no commands back: the plugin only sends.
"""

import os
import secrets

from kestrel_pi import Plugin, httpsafe, load_library

alerts = load_library(__file__, "alerts", api=1)


class Ntfy(Plugin):
    __author__ = "kestrel"
    __version__ = "0.1.0"
    __license__ = "GPL3"
    __description__ = "Push alerts to an ntfy topic: new handshakes, battery, updates."

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
        "server": {"type": "str", "default": "https://ntfy.sh",
                   "help": "The ntfy server (https). A server you run yourself "
                           "keeps alerts off third-party machines."},
        "topic": {"type": "str", "default": "", "secret": True,
                  "help": "Leave empty to have a long random topic made for you "
                          "(shown on the plugin's Status page). Anyone who knows "
                          "the topic can read it, so keep it private."},
        "token": {"type": "str", "default": "", "secret": True,
                  "help": "An ntfy access token, if your server needs one."},
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

    def on_loaded(self):
        self.alerts = alerts.Alerts(self, "ntfy", self._deliver,
                                    alerts.state_dir("kestrel-ntfy"))
        self.alerts.start()

    def on_unload(self):
        self.alerts.stop()

    def on_handshake(self, event):
        self.alerts.on_handshake(event)

    def on_internet_available(self):
        self.alerts.on_internet_available()

    def on_ui_setup(self, ui):
        self.alerts.on_ui_setup(ui)

    def on_ui_update(self, ui, state):
        self.alerts.on_ui_update(ui, state)

    def _topic(self):
        """The topic: the option, else a long random one made once and kept."""
        topic = (self.options.get("topic") or "").strip()
        if topic:
            return topic
        path = os.path.join(self.alerts.dir, "topic")
        try:
            with open(path) as handle:
                topic = handle.read().strip()
        except OSError:
            topic = ""
        if not topic:
            topic = "kestrel-" + secrets.token_urlsafe(18).replace("_", "x").replace("-", "y")
            os.makedirs(self.alerts.dir, mode=0o700, exist_ok=True)
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, "w") as handle:
                handle.write(topic)
        return topic

    def _deliver(self, requests, msg, timeout):
        server = httpsafe.https_endpoint(self.options.get("server"), "https://ntfy.sh")
        headers = {"Title": msg["title"], "Tags": "bird",
                   "Priority": "high" if msg.get("urgent") else "default"}
        token = (self.options.get("token") or "").strip()
        if token:
            headers["Authorization"] = "Bearer " + token
        return requests.post("%s/%s" % (server, self._topic()),
                             data=msg["body"].encode("utf-8"), headers=headers,
                             timeout=timeout, allow_redirects=False, stream=True)

    def on_webhook(self, path, request):
        if path == "test" and getattr(request, "method", "GET") == "POST":
            return self.alerts.send_test()
        if path not in ("", "index"):
            return self.context.page("ntfy alerts", "<p>There's nothing here.</p>",
                                     status=404)
        esc = self.context.esc
        if (self.options.get("topic") or "").strip():
            row = "<li>Topic: the one you set in the plugin's settings.</li>"
        elif getattr(self.context, "web_authed", False):
            # Only a topic this plugin made is shown (a topic you set is a secret
            # option, never echoed); the login is needed to see it.
            server = (self.options.get("server") or "https://ntfy.sh").rstrip("/")
            row = ("<li>Subscribe in the ntfy app to topic <code>%s</code> on "
                   "<code>%s</code>. Keep the topic private: anyone who knows it "
                   "can read your alerts.</li>" % (esc(self._topic()), esc(server)))
        else:
            row = "<li>The topic is shown only with the web login on.</li>"
        return self.context.page("ntfy alerts", self.alerts.status_page(
            "ntfy alerts", [row], token=(getattr(request, "args", {}) or {}).get("token") or ""))
