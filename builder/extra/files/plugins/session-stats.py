"""session-stats - per-session counters with a dashboard readout and web view.

Exercises the timer, UI and web surfaces together (``on_epoch`` + ``on_ui_*`` +
``on_webhook``, plus ``on_handshake`` to count). It accumulates counts for the
current daemon session - handshakes seen, unique APs, uptime - shows them as a
readout card on the dashboard (the shared UI model), and serves them as JSON and
a tiny HTML page under ``/plugins/session-stats/`` on the management link. Local
only, so it declares the ``ui`` (dashboard readout) and ``web`` (its page)
capabilities.
"""

from __future__ import annotations

import json
import threading
import time

from kestrel_pi import Plugin


def _format_uptime(seconds):
    """A short, human uptime: ``2h 05m`` / ``5m 12s`` / ``9s``."""
    seconds = max(0, int(seconds))
    hours, rem = divmod(seconds, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return "%dh %02dm" % (hours, minutes)
    if minutes:
        return "%dm %02ds" % (minutes, secs)
    return "%ds" % secs


class SessionStats(Plugin):
    __author__ = "kestrel"
    __version__ = "0.1.0"
    __license__ = "GPL3"
    __description__ = "Per-session handshake/AP counters with a web view."

    __target_api__ = 2
    __capabilities__ = ("ui", "web")
    __options__ = {}

    def on_loaded(self):
        self.log = self.context.logger
        self._lock = threading.Lock()
        # Uptime is measured from a monotonic clock, not wall-clock time: an
        # RTC-less Pi often boots with a stale clock that NTP then jumps forward
        # once the network is up, and a wall-clock delta would swallow that jump
        # (and the whole powered-off gap), so uptime looked like it never reset
        # across reboots. time.monotonic() can't jump, so uptime always counts
        # from this daemon launch.
        self._started_monotonic = time.monotonic()
        self._handshakes = 0
        self._aps = set()
        self._epochs = 0

    def on_ui_setup(self, ui):
        ui.add_element("handshakes", "0", label="Handshakes", kind="number")
        ui.add_element("aps", "0", label="Unique APs", kind="number")
        ui.add_element("epochs", "0", label="Epochs", kind="number")
        ui.add_element("uptime", "-", label="Uptime")

    def on_ui_update(self, ui, state):
        snap = self._snapshot()
        ui.set("handshakes", str(snap["handshakes"]))
        ui.set("aps", str(snap["unique_aps"]))
        ui.set("epochs", str(snap["epochs"]))
        ui.set("uptime", _format_uptime(snap["uptime_seconds"]))

    def on_handshake(self, event):
        with self._lock:
            self._handshakes += 1
            if event.bssid:
                self._aps.add(event.bssid)

    def on_epoch(self, epoch, info):
        with self._lock:
            self._epochs = epoch

    def _snapshot(self):
        with self._lock:
            return {
                "uptime_seconds": int(time.monotonic() - self._started_monotonic),
                "handshakes": self._handshakes,
                "unique_aps": len(self._aps),
                "epochs": self._epochs,
            }

    def on_webhook(self, path, request):
        snap = self._snapshot()
        if path == "stats.json":
            return (json.dumps(snap), 200, {"Content-Type": "application/json"})
        body = (
            "<!doctype html><meta charset='utf-8'><title>session-stats</title>"
            "<h1>session-stats</h1><ul>"
            "<li>uptime: {uptime_seconds}s</li>"
            "<li>handshakes: {handshakes}</li>"
            "<li>unique APs: {unique_aps}</li>"
            "<li>epochs: {epochs}</li></ul>"
            "<p><a href='stats.json'>stats.json</a></p>"
        ).format(**snap)
        return (body, 200, {"Content-Type": "text/html"})
