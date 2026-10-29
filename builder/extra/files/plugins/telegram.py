"""telegram - short alerts to your Telegram chat when something worth knowing happens.

Make a bot with Telegram's BotFather, start a chat with it, and set
``bot_token`` and ``chat_id``. Both are secret options: never shown back, and
the token is kept out of every error message (it rides in the request path).

The alerts themselves - new handshakes (counts only unless ``include_names``),
battery, a new release, capture stopping, coming online - and the queue, rate
limit and quiet hours are shared with the ``ntfy`` plugin through
``kestrel_pi.alerts``; this file holds only what is particular to Telegram.

It sends data off the device, so it declares the ``network`` capability:
enabling it takes the authorized-use acknowledgment, and it ships disabled.
Requests go through ``kestrel_pi.httpsafe`` (https only, no redirects, capped
reads). There are no commands back: the plugin only sends.
"""

from kestrel_pi import Plugin, httpsafe
from kestrel_pi.alerts import Alerts, state_dir

TELEGRAM_API = "https://api.telegram.org"


class Telegram(Plugin):
    __author__ = "kestrel"
    __version__ = "0.1.0"
    __license__ = "GPL3"
    __description__ = "Alerts to your Telegram chat: new handshakes, battery, updates."

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
        "bot_token": {"type": "str", "default": "", "secret": True,
                      "help": "The bot token from Telegram's BotFather."},
        "chat_id": {"type": "str", "default": "", "secret": True,
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

    def on_loaded(self):
        self.alerts = Alerts(self, "telegram", self._deliver, state_dir("kestrel-telegram"))
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

    def _deliver(self, requests, msg, timeout):
        token = (self.options.get("bot_token") or "").strip()
        chat = (self.options.get("chat_id") or "").strip()
        if not token or not chat:
            raise httpsafe.UnsafeResponse("bot_token and chat_id are needed")
        return requests.post(
            "%s/bot%s/sendMessage" % (TELEGRAM_API, token),
            json={"chat_id": chat, "text": msg["title"] + "\n" + msg["body"],
                  "disable_web_page_preview": True},
            timeout=timeout, allow_redirects=False, stream=True)

    def on_webhook(self, path, request):
        if path == "test" and getattr(request, "method", "GET") == "POST":
            return self.alerts.send_test()
        if path not in ("", "index"):
            return self.context.page("Telegram alerts", "<p>There's nothing here.</p>",
                                     status=404)
        rows = ["<li>Bot token: %s</li>" % ("set" if self.options.get("bot_token")
                                            else "<b>not set</b>"),
                "<li>Chat: %s</li>" % ("set" if self.options.get("chat_id")
                                       else "<b>not set</b>")]
        return self.context.page("Telegram alerts",
                                 self.alerts.status_page(
            "Telegram alerts", rows,
            token=(getattr(request, "args", {}) or {}).get("token") or ""))
