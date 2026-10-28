"""wpa-sec - upload captured handshakes to wpa-sec, fetch your cracked results.

A connectivity-gated uploader (``on_handshake`` + ``on_internet_available``):
it queues new hashlines from the capture store and uploads them to wpa-sec only
when a management path is online, proving the two new event sources interact.

wpa-sec ingests only raw captures (pcapng/pcap/cap), **not** hashcat-22000
(``.hc22000``) hashlines. So before each upload the queued hashlines are
repackaged into a libpcap ``.cap`` with ``hcxhash2cap`` (from the ``hcxtools``
package) and that capture is uploaded; AngryOxide already extracted the
hashlines, so the reconstructed cap carries exactly the crackable material. If
the conversion yields nothing (tool missing, or no convertible line) the batch
stays queued and is retried on the next online edge rather than dropped.

Because it transmits captured material off-device it is **opt-in**: it declares
the ``network`` capability (so enabling it requires the authorized-use
acknowledgment), ships disabled with no key baked in, and queues rather than
drops when offline.

While enabled it also keeps a local potfile of **your cracked results**: every
network recovered under your wpa-sec key, however it was submitted (this device
or any other), not just this device's uploads - wpa-sec's ``dl`` endpoint is
keyed by your API key and returns them all. It fetches once at load (if
online), on every reconnect, and periodically (``results_interval_seconds``)
so newly-cracked networks keep arriving. ``download_results`` is on by default;
set it false to upload only. The recovered passwords are sensitive and stay
on-device in ``<store>/cracked/wpa-sec.potfile``.

The exact wpa-sec endpoint/parameters are confirmed against the service on
hardware (Gate 3); the behavior here - cookie-authed upload, key-potfile
results fetch - is the committed contract.
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import threading
import time
from urllib.parse import quote, urlencode

from kestrel_pi import Plugin, httpsafe

# The cracked-networks page shows at most this many records per page, so the
# list stays light even with thousands of recovered networks.
CRACKED_PER_PAGE = 100


class WpaSec(Plugin):
    __author__ = "kestrel"
    __version__ = "0.2.0"
    __license__ = "GPL3"
    __description__ = "Upload hashlines to wpa-sec; fetch your recovered results."

    __target_api__ = 2
    __capabilities__ = ("capture-read", "network", "ui")
    # One row button on the Plugins page: "Show" opens the cracked-networks
    # page this plugin renders itself (on_webhook, path "cracked").
    __actions__ = ({"label": "Show", "path": "cracked", "method": "get"},)
    __options__ = {
        "api_key": {"type": "str", "required": True, "secret": True},
        "endpoint": {"type": "str", "default": "https://wpa-sec.stanev.org"},
        "download_results": {"type": "bool", "default": True},
        # How often, at most, to re-fetch the cracked potfile while online.
        "results_interval_seconds": {"type": "int", "default": 3600},
        "timeout": {"type": "int", "default": 30},
    }

    def on_loaded(self):
        self.log = self.context.logger
        self._lock = threading.Lock()
        self._run_lock = threading.Lock()   # at most one network pass at a time
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._state_path = os.path.join(
            self.context.capture.store_dir, ".wpa-sec-uploaded.json"
        )
        self._uploaded = self._load_state()
        self._last_results = 0.0
        # Dashboard readout counters: hashlines uploaded this daemon session
        # (resets on restart, unlike the persisted dedup set) and the cracked
        # records last seen in the potfile (seeded from any file on disk).
        self._uploaded_session = 0
        self._cracked = self._count_potfile()
        # Seed the queue from any store hashlines not yet uploaded, so a reboot
        # re-queues rather than drops.
        self._queue = []
        for path in self.context.capture.hashline_files():
            for event in self.context.capture.read_events(path):
                self._enqueue(event.raw)
        self.log.info("wpa-sec loaded; %d hashline(s) queued", len(self._queue))
        # The convert (hcxhash2cap) + upload + results download are slow network
        # /subprocess work, so they run on this worker thread, never inline in a
        # hook: a hook that blocked on them would hold a dispatch worker and, past
        # the stuck ceiling, be quarantined. The hooks just wake the worker, which
        # also does an initial pass in case we are already online at load.
        self._worker = threading.Thread(
            target=self._run, name="wpa-sec-worker", daemon=True)
        self._worker.start()

    def on_unload(self):
        self._stop.set()
        self._wake.set()

    def _run(self):
        # One pass at load, then wake on a trigger (new handshake / reconnect /
        # epoch) or the results throttle timeout.
        self._tick()
        while not self._stop.is_set():
            self._wake.wait(timeout=self._worker_interval())
            self._wake.clear()
            if self._stop.is_set():
                return
            self._tick()

    def _worker_interval(self):
        # Wake at least as often as the results throttle so cracked results keep
        # arriving even with no handshakes or reconnects; never busier than 60s.
        try:
            return max(60, int(self.options.get("results_interval_seconds", 3600)))
        except (TypeError, ValueError):
            return 3600

    def _tick(self):
        if not self.context.connectivity.online:
            return
        if not self._run_lock.acquire(blocking=False):
            return  # a pass is already running
        try:
            self._flush()
            self._maybe_download_results()
        finally:
            self._run_lock.release()

    def on_epoch(self, epoch, info):
        # Nudge the worker; it re-fetches results on a throttle so newly cracked
        # networks show up without waiting for a reconnect. The work is on the
        # worker thread, so this hook returns at once.
        self._wake.set()

    # -- dashboard readout ----------------------------------------------
    def on_ui_setup(self, ui):
        ui.add_element("uploaded", "0", label="Uploaded", kind="number")
        ui.add_element("queued", "0", label="Queued", kind="number")
        # The widget label is the operator-facing term ("Recovered"); the
        # element key stays "cracked" to match the on-disk store dir.
        ui.add_element("cracked", "0", label="Recovered", kind="number")

    def on_ui_update(self, ui, state):
        with self._lock:
            uploaded, queued, cracked = (
                self._uploaded_session, len(self._queue), self._cracked)
        ui.set("uploaded", str(uploaded))
        ui.set("queued", str(queued))
        ui.set("cracked", str(cracked))

    # -- queueing --------------------------------------------------------
    def _enqueue(self, raw):
        if not raw:
            return
        with self._lock:
            if raw in self._uploaded or raw in self._queue:
                return
            self._queue.append(raw)

    def on_handshake(self, event):
        self._enqueue(event.raw)
        # Hand the (slow) convert+upload to the worker rather than blocking the
        # hook on it.
        self._wake.set()

    def on_internet_available(self):
        # A reconnect: let the worker flush the queue and fetch results.
        self._wake.set()

    # -- upload ----------------------------------------------------------
    def _flush(self):
        if not self.context.require_capability("network"):
            return
        with self._lock:
            batch = list(self._queue)
        if not batch:
            return
        # wpa-sec only ingests raw captures, so repackage the queued hashlines
        # into a .cap first; a conversion that yields nothing stays queued.
        cap = self._convert_to_cap(batch)
        if not cap:
            return
        if self._upload(cap):
            with self._lock:
                for raw in batch:
                    self._uploaded.add(raw)
                    if raw in self._queue:
                        self._queue.remove(raw)
                self._uploaded_session += len(batch)
            self._save_state()
            self.log.info("wpa-sec uploaded %d hashline(s)", len(batch))

    def _convert_to_cap(self, hashlines):
        """Repackage hc22000 hashlines into a libpcap ``.cap`` for wpa-sec.

        wpa-sec rejects raw 22000 hashlines, so write the batch to a temp
        hashfile and run ``hcxhash2cap`` (hcxtools) to produce a cap. Returns
        the cap bytes, or ``None`` when nothing was produced (tool missing or no
        convertible line) so the caller keeps the batch queued.
        """
        tool = "hcxhash2cap"
        with tempfile.TemporaryDirectory(prefix="wpa-sec-") as tmp:
            hashfile = os.path.join(tmp, "batch.hc22000")
            capfile = os.path.join(tmp, "batch.cap")
            try:
                with open(hashfile, "w", encoding="utf-8") as handle:
                    handle.write("\n".join(hashlines) + "\n")
            except OSError as exc:
                self.log.warning("wpa-sec: could not stage hashlines: %s", exc)
                return None
            try:
                proc = subprocess.run(
                    [tool, "--pmkid-eapol=" + hashfile, "-c", capfile],
                    capture_output=True, text=True, timeout=120, check=False,
                )
            except FileNotFoundError:
                self.log.warning(
                    "wpa-sec: %s not found; install the hcxtools package to "
                    "upload captures", tool)
                return None
            except (OSError, subprocess.SubprocessError) as exc:
                self.log.warning("wpa-sec: hcxhash2cap failed: %s", exc)
                return None
            try:
                with open(capfile, "rb") as handle:
                    cap = handle.read()
            except OSError:
                cap = b""
        if not cap:
            self.log.warning(
                "wpa-sec: hcxhash2cap produced no capture (rc=%s) %s",
                proc.returncode, (proc.stderr or "").strip()[:200])
            return None
        return cap

    def _upload(self, cap):
        import requests

        key = self.options.get("api_key") or ""
        try:
            # https only and no redirects: the key travels as a cookie, which
            # requests would re-send to wherever a redirect points.
            endpoint = httpsafe.https_endpoint(self.options.get("endpoint"),
                                               "https://wpa-sec.stanev.org")
            resp = requests.post(
                endpoint,
                cookies={"key": key},
                files={"file": ("kestrel.cap", cap)},
                timeout=int(self.options.get("timeout", 30)),
                allow_redirects=False, stream=True,
            )
            httpsafe.check_response(resp)
            if resp.status_code >= 400:
                self.log.warning("wpa-sec upload HTTP %s", resp.status_code)
                return False
            body = httpsafe.read_capped(resp, httpsafe.MAX_JSON)
            if b" already submitted" in body:
                self.log.info("wpa-sec: capture already submitted")
            return True
        except Exception as exc:  # noqa: BLE001 - stay queued on any failure
            self.log.warning("wpa-sec upload failed: %s", exc)
            return False

    # -- results download ------------------------------------------------
    def _maybe_download_results(self):
        """Fetch the cracked potfile if enabled and the throttle has elapsed."""
        if not self.options.get("download_results", True):
            return
        interval = int(self.options.get("results_interval_seconds", 3600))
        now = time.monotonic()
        if self._last_results and now - self._last_results < max(0, interval):
            return
        self._last_results = now
        self._download_results()

    def _potfile_path(self):
        return os.path.join(self.context.capture.store_dir, "cracked",
                            "wpa-sec.potfile")

    def _count_potfile(self):
        """Non-blank lines in the on-disk potfile (cracked records), or 0."""
        try:
            with open(self._potfile_path(), "r", encoding="utf-8") as handle:
                return sum(1 for line in handle if line.strip())
        except OSError:
            return 0

    # -- cracked-networks page (the "Show" row button) -------------------
    def on_webhook(self, path, request):
        """The ``/plugins/wpa-sec/cracked`` page: the SSID and passphrase of
        every network recovered under this wpa-sec key, searchable by SSID and
        paginated. The passphrases are sensitive, so the list renders only when
        the web login is on and the request is signed in (``context.web_authed``);
        otherwise the page shows a login notice. The plugin owns this page, so
        core never special-cases wpa-sec."""
        if path != "cracked":
            return self.context.page("Not found - kestrel",
                                     "<h1>Not found</h1>", status=404)
        esc = self.context.esc
        token = request.args.get("token") or ""
        back = ('<p><a href="/plugins' + self._qs({"token": token})
                + '">&larr; Plugins</a></p>')
        if not self.context.web_authed:
            return self.context.page(
                "recovered networks - kestrel",
                back + "<h1>Recovered networks</h1>"
                + '<p class="notice">These are recovered Wi-Fi passphrases, so '
                "the list is shown only with the web login on and you signed in. "
                "Turn on <code>[web] auth</code> to view them.</p>")

        records = self._parse_cracked_potfile(self._read_cracked_text())
        query = request.args.get("q", "") or ""
        total = len(records)
        needle = query.strip().lower()
        shown = ([r for r in records if needle in (r.get("essid") or "").lower()]
                 if needle else records)
        matched = len(shown)

        per_page = CRACKED_PER_PAGE
        pages = max(1, (matched + per_page - 1) // per_page)
        try:
            page = int(request.args.get("page", 1))
        except (TypeError, ValueError):
            page = 1
        page = min(max(1, page), pages)
        start = (page - 1) * per_page
        window = shown[start:start + per_page]

        rows = []
        for rec in window:
            essid = rec.get("essid") or ""
            name = esc(essid) if essid else '<span class="smeta">(hidden)</span>'
            rows.append(
                '<tr><td data-label="network">' + name + "</td>"
                '<td data-label="bssid">' + esc(rec.get("bssid") or "") + "</td>"
                '<td data-label="passphrase"><code>' + esc(rec.get("psk") or "")
                + "</code></td></tr>")
        body_rows = "".join(rows) or (
            "<tr><td colspan='3'>" + ("no recovered networks match that search"
                                      if needle else "no recovered networks yet")
            + "</td></tr>")

        if matched:
            if needle:
                count = ("%d of %d recovered networks match &ldquo;%s&rdquo;"
                         % (matched, total, esc(query)))
            else:
                count = "%d recovered network(s)" % total
        else:
            count = (("no match in %d recovered network(s)" % total) if total
                     else "no recovered networks yet")

        hidden_token = ('<input type="hidden" name="token" value="' + esc(token)
                        + '">' if token else "")
        search = (
            '<form class="search" method="get" action="/plugins/wpa-sec/cracked">'
            + hidden_token
            + '<input type="text" name="q" value="' + esc(query)
            + '" placeholder="Search SSID" aria-label="Search SSID">'
            + '<button type="submit">Search</button></form>')

        pager = ""
        if pages > 1:
            prev_cls = ' class="off"' if page <= 1 else ""
            next_cls = ' class="off"' if page >= pages else ""
            pager = (
                '<div class="pager"><a' + prev_cls + ' href="'
                + esc(self._page_link(max(1, page - 1), query, token))
                + '">&larr; Prev</a>'
                + '<span class="sub">page ' + str(page) + " of " + str(pages)
                + "</span>" + '<a' + next_cls + ' href="'
                + esc(self._page_link(min(pages, page + 1), query, token))
                + '">Next &rarr;</a></div>')

        return self.context.page(
            "recovered networks - kestrel",
            back + "<h1>Recovered networks</h1>" + search
            + '<p class="count">' + count + "</p>"
            + "<div class='wrap'><table><thead><tr><th>network</th><th>bssid</th>"
            "<th>passphrase</th></tr></thead><tbody>" + body_rows
            + "</tbody></table></div>" + pager)

    @staticmethod
    def _qs(params):
        """A ``?a=b`` query string from the non-empty params, or ""."""
        items = {k: v for k, v in params.items() if v}
        return ("?" + urlencode(items)) if items else ""

    def _page_link(self, target, query, token):
        params = {}
        if query:
            params["q"] = query
        if target != 1:
            params["page"] = target
        if token:
            params["token"] = token
        return "/plugins/wpa-sec/cracked" + self._qs(params)

    def _read_cracked_text(self):
        """The raw potfile text, or "" when absent/unreadable."""
        try:
            with open(self._potfile_path(), "r", encoding="utf-8",
                      errors="replace") as handle:
                return handle.read()
        except OSError:
            return ""

    @staticmethod
    def _parse_cracked_potfile(text):
        """Parse the wpa-sec results potfile into cracked-network records.

        Each non-blank line is wpa-sec's download format
        ``apmac:stationmac:essid:psk`` (bare 12-hex MACs). The ESSID may itself
        contain ``:``, so the passphrase is the last field and the ESSID is
        everything between the station MAC and it. Lines with fewer than four
        fields are skipped, and exact duplicate (bssid, essid, psk) records are
        collapsed with order kept."""
        records, seen = [], set()
        for line in (text or "").splitlines():
            line = line.strip()
            if not line:
                continue
            parts = line.split(":")
            if len(parts) < 4 or not parts[0]:
                continue
            bssid, psk = parts[0], parts[-1]
            essid = ":".join(parts[2:-1])
            key = (bssid, essid, psk)
            if key in seen:
                continue
            seen.add(key)
            records.append({"bssid": bssid, "essid": essid, "psk": psk})
        return records

    def _download_results(self):
        if not self.context.require_capability("network"):
            return
        import requests

        key = self.options.get("api_key") or ""
        potfile = self._potfile_path()
        cracked_dir = os.path.dirname(potfile)
        try:
            endpoint = httpsafe.https_endpoint(self.options.get("endpoint"),
                                               "https://wpa-sec.stanev.org")
            # dl=1 is keyed by the API key, so this returns every network
            # cracked under your key, however it was submitted - not just this
            # device's.
            self.log.info("wpa-sec: fetching cracked results from %s", endpoint)
            resp = requests.get(
                endpoint + "/?api&dl=1",
                cookies={"key": key},
                timeout=int(self.options.get("timeout", 30)),
                allow_redirects=False, stream=True,
            )
            httpsafe.check_response(resp)
            if resp.status_code >= 400:
                self.log.warning("wpa-sec results HTTP %s", resp.status_code)
                return
            body = httpsafe.read_capped(resp).decode("utf-8", errors="replace")
            networks = sum(1 for line in body.splitlines() if line.strip())
            # Recovered passphrases: readable by the pi account only.
            os.makedirs(cracked_dir, mode=0o700, exist_ok=True)
            os.chmod(cracked_dir, 0o700)
            tmp = potfile + ".tmp"
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(body)
            os.replace(tmp, potfile)
            with self._lock:
                self._cracked = networks
            if networks:
                self.log.info("wpa-sec: %d cracked network(s) in %s",
                              networks, potfile)
            else:
                self.log.info("wpa-sec: reached the service; no cracked results "
                              "yet for your key")
        except Exception as exc:  # noqa: BLE001
            self.log.warning("wpa-sec results download failed: %s", exc)

    # -- state -----------------------------------------------------------
    def _load_state(self):
        try:
            with open(self._state_path, "r", encoding="utf-8") as handle:
                return set(json.load(handle))
        except (OSError, ValueError):
            return set()

    def _save_state(self):
        tmp = self._state_path + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as handle:
                json.dump(sorted(self._uploaded), handle)
            os.replace(tmp, self._state_path)
        except OSError as exc:
            self.log.warning("could not save wpa-sec state: %s", exc)
