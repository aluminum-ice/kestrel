"""wigle - wardriving uploader that consumes the gps plugin's fixes.

A connectivity-gated uploader (``on_handshake`` / ``on_epoch`` +
``on_internet_available``): it records observed networks with the GPS
coordinates from the ``gps`` plugin's ``position`` service and submits them to
wigle.net when online. This is the reference example of one plugin consuming
another's shared data.

gps is an **optional** dependency (``__optional_requires__`` = ``position``):
when ``gps`` is enabled the loader starts it first and this plugin uploads
observations with real fixes; when it is absent the plugin still loads, because
WiGLE is usable without a local receiver. Two modes cover the no-gps case:

* ``geolocate = false`` (default): nothing to upload and no lookups, so the
  plugin loads idle and says so in its health note - enable ``gps`` or turn on
  ``geolocate``.
* ``geolocate = true``: the ``geolocate`` fallback uses WiGLE's **network
  search** API to look up a network's *approximate*, already-known coordinates
  for on-device display.

Uploads to WiGLE only ever carry real ``gps`` fixes - a looked-up position is
never submitted, so the wardriving dataset is never polluted with approximate
coordinates. Three guardrails hold the ``geolocate`` path: it is approximate
and not yours; it is cached separately (``geo/wigle-cache.json``, ``source``
``wigle-search``) and **never** fed back into an upload; and lookups are cached
per BSSID so a network is queried once, respecting WiGLE's quota.

Same posture as wpa-sec otherwise: opt-in, declares ``network`` (so enabling
requires the authorized-use acknowledgment), operator-supplied API token (never
baked in), queues when offline and persists the queue and dedup set across
reboots (``.wigle-state.json``). Exact WiGLE endpoints/response shapes are
confirmed on hardware (Gate 3).
"""

from __future__ import annotations

import csv
import io
import json
import os
import threading
import time

from kestrel_pi import Plugin, httpsafe


class Wigle(Plugin):
    __author__ = "kestrel"
    __version__ = "0.2.0"
    __license__ = "GPL3"
    __description__ = "Submit observed networks with GPS to WiGLE (opt-in)."

    __target_api__ = 2
    __capabilities__ = ("capture-read", "location", "network", "ui")
    # gps is optional: with it, uploads carry real fixes; without it the plugin
    # still loads (idle, or geolocate-only display). See the module docstring.
    __optional_requires__ = ("position",)
    __options__ = {
        "api_name": {"type": "str", "required": True, "secret": True},
        "api_token": {"type": "str", "required": True, "secret": True},
        "endpoint": {"type": "str", "default": "https://api.wigle.net"},
        "geolocate": {"type": "bool", "default": False},
        # Cap WiGLE search lookups per flush, to respect the account quota.
        "geolocate_max_lookups": {"type": "int", "default": 5},
        "timeout": {"type": "int", "default": 30},
        # Periodic retry interval for a queued upload (see on_epoch); throttled
        # so a persistent failure doesn't hammer WiGLE every epoch.
        "retry_seconds": {"type": "int", "default": 300},
        # WiGLE rate-limits (HTTP 429) a free account's uploads per rolling
        # window. After a 429, back off this long before trying again - the
        # normal retry_seconds cadence can't succeed until the window resets and
        # only hammers WiGLE. A Retry-After header on the 429 overrides this.
        "rate_limit_seconds": {"type": "int", "default": 3600},
    }

    def on_loaded(self):
        self.log = self.context.logger
        self._lock = threading.Lock()
        self._run_lock = threading.Lock()   # at most one network pass at a time
        self._stop = threading.Event()
        self._wake = threading.Event()
        # Set by a handshake / reconnect so the next worker pass uploads now,
        # bypassing the periodic retry throttle; a plain timeout wake does not.
        self._force_next = False
        # BSSIDs awaiting a display-only geolocate lookup (no GPS fix); drained
        # on the worker thread so the network call never blocks a hook.
        self._geo_queue = []
        self._geo_cache_path = os.path.join(
            self.context.capture.store_dir, "geo", "wigle-cache.json"
        )
        # Persist the pending-upload queue and the observed-bssid dedup set
        # across reboots: an observation pairs a handshake with a live GPS fix,
        # so (unlike wpa-sec's hashlines) it cannot be re-derived from the store.
        self._state_path = os.path.join(
            self.context.capture.store_dir, ".wigle-state.json"
        )
        state = self._load_state()
        self._seen = set(state.get("seen", []))            # dedup (sent+queued)
        self._observations = list(state.get("queue", []))  # not yet uploaded
        # Dashboard readout: observations uploaded this daemon session (resets
        # on restart, unlike the persisted dedup set) and the operating mode.
        self._uploaded_session = 0
        self._mode = "idle"
        # Last upload outcome, shown on the card so a failing/held upload is not
        # silent, and a monotonic marker throttling the periodic retry. None
        # means no attempt yet, so the first on_epoch retry is never throttled -
        # time.monotonic() counts from boot, so a 0.0 start would wrongly
        # suppress that first retry for the first retry_seconds after boot (just
        # when the device first comes online over BT-PAN).
        self._last_status = "idle"
        self._last_attempt = None
        # When WiGLE rate-limits us (HTTP 429), hold off all upload attempts
        # until this monotonic time (0.0 = not rate-limited). Cleared on a
        # successful upload.
        self._rate_limited_until = 0.0
        # A 401 means the api_name/api_token are wrong; retrying the same bad
        # credentials just hammers WiGLE, so latch and stop attempting until the
        # plugin reloads (a credentials change goes through plugins-ctl apply,
        # which reloads the plugin, so a fresh instance clears this).
        self._auth_failed = False
        # The gps plugin registers the position service before this plugin's
        # on_loaded (optional-requires ordering), so this reliably reflects
        # whether gps is enabled. It governs only operator messaging; the upload
        # path checks for an actual fix per observation.
        has_gps = self.context.services.has("position")
        geolocate = bool(self.options.get("geolocate"))
        if has_gps:
            # Real fixes drive uploads; when geolocate is also on, approximate
            # lookups still fill in on-device display for observations that have
            # no live fix (never uploaded), so name both paths - otherwise the
            # card hides that geolocate is doing anything.
            self._mode = "gps + geolocate" if geolocate else "gps"
            self.context.health_note = ""
            self.log.info(
                "wigle loaded: uploading observations with real GPS fixes "
                "(geolocate=%s)", geolocate,
            )
        elif geolocate:
            self._mode = "geolocate"
            self.context.health_note = (
                "no gps: geolocate-only display mode "
                "(approximate coordinates, never uploaded)"
            )
            self.log.info("wigle loaded: %s", self.context.health_note)
        else:
            self._mode = "idle"
            self.context.health_note = (
                "idle: no gps and geolocate=false - enable the gps plugin "
                "or set geolocate=true"
            )
            self.log.warning("wigle loaded but %s", self.context.health_note)

        # The upload (and the optional geolocate lookup) are network calls, so
        # they run on this worker thread, never inline in a hook: a hook that
        # blocked on them would hold a dispatch worker and, past the stuck
        # ceiling, be quarantined. The hooks just wake the worker, which also
        # does an initial pass to flush anything queued before a reboot.
        self._worker = threading.Thread(
            target=self._run, name="wigle-worker", daemon=True)
        self._worker.start()

    def on_unload(self):
        self._stop.set()
        self._wake.set()

    def _run(self):
        self._tick(force=True)
        while not self._stop.is_set():
            self._wake.wait(timeout=self._poll_interval())
            self._wake.clear()
            if self._stop.is_set():
                return
            force = self._force_next
            self._force_next = False
            try:
                self._tick(force=force)
            except Exception as exc:  # noqa: BLE001 - a tick bug must not kill the worker
                self.log.warning("wigle worker tick failed: %s", exc)

    def _poll_interval(self):
        # Periodic nudge so a queued observation retries even with the link up
        # and no new handshake; the retry throttle still gates the actual upload.
        return max(60, self._retry_seconds())

    def _tick(self, force=False):
        if not self.context.connectivity.online:
            return
        if not self._run_lock.acquire(blocking=False):
            return  # a pass is already running
        try:
            self._drain_geo_queue()
            self._maybe_upload(force=force)
        finally:
            self._run_lock.release()

    def _drain_geo_queue(self):
        with self._lock:
            pending = self._geo_queue
            self._geo_queue = []
        for bssid in pending:
            if self._stop.is_set():
                return
            self._geolocate(bssid)

    def _maybe_upload(self, force=False):
        with self._lock:
            pending = bool(self._observations)
        if not pending:
            return
        if (not force and self._last_attempt is not None
                and time.monotonic() - self._last_attempt < self._retry_seconds()):
            return
        self._upload()

    # -- observation -----------------------------------------------------
    def _record(self, event):
        if not event.bssid:
            return
        fix = self.context.services.get("position")
        if fix and fix.get("lat") is not None and fix.get("lon") is not None:
            with self._lock:
                if event.bssid in self._seen:
                    return
                self._seen.add(event.bssid)
                self._observations.append({
                    "bssid": event.bssid,
                    "essid": event.essid,
                    "lat": fix["lat"],
                    "lon": fix["lon"],
                    "alt": fix.get("alt"),
                    # WiGLE's FirstSeen column must be 'YYYY-MM-DD HH:MM:SS'
                    # (UTC), not ISO-8601 with a T/Z - the wrong shape makes
                    # WiGLE drop the row. The handshake time is when we saw it.
                    "time": time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime()),
                })
            self._save_state()
        elif self.options.get("geolocate"):
            # No real fix: optionally tag for on-device display only. This path
            # NEVER contributes to an upload. The lookup is a network call, so
            # queue the bssid for the worker rather than doing it in the hook.
            with self._lock:
                if event.bssid not in self._geo_queue:
                    self._geo_queue.append(event.bssid)

    def on_handshake(self, event):
        self._record(event)
        # Hand the (network) upload to the worker; a handshake forces it to run
        # now rather than waiting out the retry throttle.
        self._force_next = True
        self._wake.set()

    def on_internet_available(self):
        # A reconnect: force an upload pass on the worker.
        self._force_next = True
        self._wake.set()

    def on_epoch(self, epoch, info):
        # Periodic nudge so a queued observation doesn't sit forever (with the
        # link up and no new handshake, nothing else would retry). Not forced,
        # so the worker's retry throttle still gates the actual upload.
        self._wake.set()

    def _retry_seconds(self):
        try:
            return max(60, int(self.options.get("retry_seconds", 300)))
        except (TypeError, ValueError):
            return 300

    def _rate_limit_seconds(self):
        try:
            return max(60, int(self.options.get("rate_limit_seconds", 3600)))
        except (TypeError, ValueError):
            return 3600

    # -- dashboard readout ----------------------------------------------
    def on_ui_setup(self, ui):
        ui.add_element("uploaded", "0", label="Uploaded", kind="number")
        ui.add_element("queued", "0", label="Queued", kind="number")
        ui.add_element("networks", "0", label="Networks", kind="number")
        # Mode explains why the plugin is or isn't uploading: "gps" (uploading
        # real fixes), "gps + geolocate" (same, plus display-only lookups filling
        # in fix-less observations), "geolocate" (display-only lookups, nothing
        # uploaded) or "idle" (no gps and geolocate off).
        ui.add_element("mode", "idle", label="Mode", kind="text")
        # Last upload outcome (ok / rejected / rate-limited / error / waiting),
        # so a stuck or held upload is visible instead of a silent, growing
        # Queued count.
        ui.add_element("status", "idle", label="Upload", kind="text")

    def on_ui_update(self, ui, state):
        with self._lock:
            uploaded = self._uploaded_session
            queued = len(self._observations)
            networks = len(self._seen)
            status = self._last_status
        ui.set("uploaded", str(uploaded))
        ui.set("queued", str(queued))
        ui.set("networks", str(networks))
        ui.set("mode", self._mode)
        # If something is queued but the daemon sees no internet, say so - that
        # is why it isn't uploading, not an upload error.
        if queued and not self.context.connectivity.online:
            status = "waiting (offline)"
        ui.set("status", status)

    # -- upload (real fixes only) ---------------------------------------
    def _upload(self):
        if self._auth_failed:
            return                      # bad credentials; wait for a reload
        if time.monotonic() < self._rate_limited_until:
            return                      # backing off after a WiGLE 429
        if not self.context.require_capability("network"):
            return
        with self._lock:
            batch = list(self._observations)
        if not batch:
            return
        self._last_attempt = time.monotonic()
        import requests

        try:
            # https only, no redirects, and a capped read (kestrel_pi.httpsafe):
            # the api_name/api_token pair goes with every request.
            endpoint = httpsafe.https_endpoint(self.options.get("endpoint"),
                                               "https://api.wigle.net")
            resp = requests.post(
                endpoint + "/api/v2/file/upload",
                auth=(self.options.get("api_name") or "",
                      self.options.get("api_token") or ""),
                files={"file": ("observations.csv", _to_wigle_csv(batch))},
                timeout=int(self.options.get("timeout", 30)),
                allow_redirects=False, stream=True,
            )
            httpsafe.check_response(resp)
            if resp.status_code >= 400:
                if resp.status_code == 401:
                    # Authentication, not a transient error: latch so the retry
                    # stops, and name the fix on the card.
                    self._auth_failed = True
                    self._last_status = "error: HTTP 401 (check api_name/api_token)"
                    self.log.warning("wigle upload HTTP 401 (bad credentials)")
                elif resp.status_code == 429:
                    # Rate/quota limit, not a failure: WiGLE caps uploads per
                    # rolling window. Back off (honoring Retry-After when sent)
                    # instead of retrying on retry_seconds, which can't succeed
                    # until the window resets and only hammers WiGLE. The queue
                    # is kept, so it uploads once the window clears.
                    wait = _retry_after_seconds(resp.headers.get("Retry-After"))
                    if wait is None:
                        wait = self._rate_limit_seconds()
                    self._rate_limited_until = time.monotonic() + wait
                    self._last_status = (
                        "rate-limited; retry in %dm" % max(1, round(wait / 60.0)))
                    self.log.warning(
                        "wigle rate-limited (HTTP 429); backing off %ds", wait)
                else:
                    self._last_status = "error: HTTP %s" % resp.status_code
                    self.log.warning("wigle upload HTTP %s", resp.status_code)
                return
            # WiGLE answers 200 with a JSON body; a rejected file (bad token or
            # bad format) can still be 200 with success=false, so trust the
            # body, not the status code (the wpa-sec lesson). Stay queued on a
            # rejection so a fix/retry can still land.
            try:
                payload = httpsafe.read_json(resp)
            except ValueError:
                payload = {}
            if payload.get("success") is False:
                detail = payload.get("message") or "no detail"
                self._last_status = "rejected: " + str(detail)[:60]
                self.log.warning("wigle upload rejected: %s", detail)
                return
        except Exception as exc:  # noqa: BLE001 - stay queued on failure
            self._last_status = "error: " + str(exc)[:60]
            self.log.warning("wigle upload failed: %s", exc)
            return
        with self._lock:
            self._observations = [o for o in self._observations if o not in batch]
            self._uploaded_session += len(batch)
            self._last_status = "ok"
            self._rate_limited_until = 0.0   # a success clears any 429 backoff
        self._save_state()
        rec = _first_record(payload.get("results"))
        trans = str(rec.get("transid") or rec.get("transId") or "") if rec else ""
        self.log.info("wigle uploaded %d observation(s)%s", len(batch),
                      (" (transid %s)" % trans) if trans else "")

    # -- geolocate fallback (search API; display only, never uploaded) --
    def _geolocate(self, bssid):
        cache = self._load_geo_cache()
        if bssid in cache:
            return  # cached: do not re-query (quota guardrail)
        if not self.context.connectivity.online:
            return
        if not self.context.require_capability("network"):
            return
        import requests

        try:
            endpoint = httpsafe.https_endpoint(self.options.get("endpoint"),
                                               "https://api.wigle.net")
            resp = requests.get(
                endpoint + "/api/v2/network/search",
                params={"netid": bssid},
                auth=(self.options.get("api_name") or "",
                      self.options.get("api_token") or ""),
                timeout=int(self.options.get("timeout", 30)),
                allow_redirects=False, stream=True,
            )
            httpsafe.check_response(resp)
            if resp.status_code >= 400:
                self.log.debug("wigle search HTTP %s", resp.status_code)
                return
            payload = httpsafe.read_json(resp)
            if payload.get("success") is False:
                # e.g. over the daily query quota, or a bad token: do not cache
                # (so a later lookup can retry), and do not treat as a result.
                self.log.debug("wigle search declined: %s",
                               payload.get("message") or "no detail")
                return
            results = payload.get("results") or []
        except Exception as exc:  # noqa: BLE001
            self.log.debug("wigle search failed: %s", exc)
            return
        top = _first_record(results)
        if not top:
            return
        cache[bssid] = {
            "bssid": bssid,
            "lat": top.get("trilat"),
            "lon": top.get("trilong"),
            "source": "wigle-search",
            "query_time": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
        self._save_geo_cache(cache)

    def _load_geo_cache(self):
        try:
            with open(self._geo_cache_path, "r", encoding="utf-8") as handle:
                return json.load(handle)
        except (OSError, ValueError):
            return {}

    def _save_geo_cache(self, cache):
        os.makedirs(os.path.dirname(self._geo_cache_path), exist_ok=True)
        tmp = self._geo_cache_path + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as handle:
                json.dump(cache, handle)
            os.replace(tmp, self._geo_cache_path)
        except OSError as exc:
            self.log.warning("could not write wigle cache: %s", exc)

    def _load_state(self):
        try:
            with open(self._state_path, "r", encoding="utf-8") as handle:
                data = json.load(handle)
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def _save_state(self):
        tmp = self._state_path + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as handle:
                json.dump({"seen": sorted(self._seen),
                           "queue": self._observations}, handle)
            os.replace(tmp, self._state_path)
        except OSError as exc:
            self.log.warning("could not save wigle state: %s", exc)


def _first_record(results):
    """The first record dict from a WiGLE ``results`` field, or ``None``.

    WiGLE has returned ``results`` as a list of objects on some responses and as
    a single object on others, so accept either (a dict is the record itself; a
    list yields its first dict item). Anything else gives ``None``. This keeps a
    shape surprise from crashing the upload worker -- the old code did
    ``results[0]`` on a dict and died with ``KeyError: 0``, killing the thread."""
    if isinstance(results, dict):
        return results
    if isinstance(results, (list, tuple)):
        for item in results:
            if isinstance(item, dict):
                return item
    return None


def _retry_after_seconds(value):
    """Seconds to wait from an HTTP ``Retry-After`` header, or ``None`` if it is
    absent or unparseable. Accepts a delta-seconds integer or an HTTP-date."""
    if not value:
        return None
    text = str(value).strip()
    try:
        return max(0, int(text))
    except ValueError:
        pass
    try:
        from email.utils import parsedate_to_datetime

        when = parsedate_to_datetime(text)
    except (TypeError, ValueError, IndexError, OverflowError):
        return None
    if when is None:
        return None
    try:
        import datetime

        now = datetime.datetime.now(when.tzinfo)
        return max(0, int((when - now).total_seconds()))
    except (TypeError, ValueError, OverflowError):
        return None


def _to_wigle_csv(observations):
    """A minimal WiGLE 1.4 CSV from real-fix observations."""
    buf = io.StringIO()
    buf.write("WigleWifi-1.4,appRelease=kestrel,model=pi,"
              "release=0.3.0,device=kestrel,display=,board=,brand=\n")
    writer = csv.writer(buf)
    writer.writerow([
        "MAC", "SSID", "AuthMode", "FirstSeen", "Channel", "RSSI",
        "CurrentLatitude", "CurrentLongitude", "AltitudeMeters",
        "AccuracyMeters", "Type",
    ])
    for obs in observations:
        writer.writerow([
            obs["bssid"], obs.get("essid", ""), "[WPA2]", obs.get("time", ""),
            "", "", obs["lat"], obs["lon"], obs.get("alt") or "", "", "WIFI",
        ])
    return buf.getvalue()
