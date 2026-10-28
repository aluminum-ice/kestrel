"""gps - the device's one position source.

Reads a location source and keeps the current fix. The source is ``auto`` by
default: it uses ``gpsd`` when that daemon is running, and otherwise reads a
serial NMEA receiver from the configured device if one is present, so a
plugged-in USB GPS works with no setup. Set ``source`` to ``gpsd`` or
``serial`` to pin one transport. With a fix it does two things:

* on ``on_handshake`` it writes a location **sidecar** next to the hashline in
  the capture store (``<capture>.hc22000.gps.json``), a BSSID-keyed map of the
  fix at capture time; and
* it ``provides`` a ``position`` service so other plugins (and later the UI and
  the agent) read the current fix without owning a GPS reader; and
* it shows a readout card on the dashboard (the shared UI model): the fix status
  and satellite count, the signal strength (the strongest satellite's C/N0 in
  dB-Hz) with a plain-language read of what it means for getting a fix, and the
  current latitude/longitude/altitude when fixed.

Local only: it transmits nothing. Capabilities: ``location`` (keeps where the
device has been), ``capture-read`` (tags store files) and ``ui`` (the dashboard
readout). A real fix always wins; with no receiver it reports "no fix" and tags
nothing, breaking nothing.
"""

from __future__ import annotations

import glob
import json
import os
import select
import socket
import threading
import time

from kestrel_pi import Plugin

# The cross-process GPS seam (Phase 4.8): the latest fix, written here atomically
# whenever it updates, for the out-of-process learning agent to read best-effort
# each decision window (it never imports this plugin). tmpfs: live-only state.
# The schema is {ts, lat, lon, alt, fix, sats, hdop, cn0}; see
# docs/developer/agent-interface.md. systemd gives the daemon the directory
# (RuntimeDirectory=kestrel); writing is best-effort so a missing dir or a
# read-only /run simply means the agent sees no GPS, never an error here.
POSITION_FILE = "/run/kestrel/position.json"

# C/N0 (carrier-to-noise, dB-Hz) guides for the dashboard's signal read. The
# strongest satellite's C/N0 is the clearest single "can this lock" measure: a
# fix needs a few satellites above roughly 30 dB-Hz, so a strong peak with
# several usable satellites means a lock is imminent, while an all-weak sky
# (everything below ~25) never locks until the receiver can see more open sky.
STRONG_CN0 = 30.0       # a satellite at or above this is usable for a fix
GOOD_CN0 = 35.0         # a healthy open-sky signal; a lock should follow

# A healthy receiver streams NMEA (or gpsd reports) about once a second even
# with no fix, so a gap this long means it has gone silent - unplugged, powered
# off, or a stalled USB link. Past it the card shows a dead-receiver state
# instead of the last (now meaningless) fix and signal. Comfortably above the
# serial reader's 20 s read timeout so a healthy receiver never trips it.
SIGNAL_STALE_S = 30.0


class GPS(Plugin):
    __author__ = "kestrel"
    __version__ = "0.1.0"
    __license__ = "GPL3"
    __description__ = "GPS position source: tags handshakes and shares the fix."

    __target_api__ = 2
    __capabilities__ = ("location", "capture-read", "ui")
    __provides__ = ("position",)
    __options__ = {
        "source": {"type": "str", "default": "auto"},      # auto | gpsd | serial
        "host": {"type": "str", "default": "127.0.0.1"},
        "port": {"type": "int", "default": 2947},
        "device": {"type": "str", "default": "/dev/ttyACM0"},
        "baud": {"type": "int", "default": 9600},
    }

    def on_loaded(self):
        self.log = self.context.logger
        self._fix = None
        self._mode = 0          # 0/1 no fix, 2 = 2D, 3 = 3D (gpsd TPV modes)
        self._sats = None       # satellites in use, when the source reports it
        self._hdop = None       # horizontal dilution of precision (GGA / gpsd)
        self._cn0 = None        # mean carrier-to-noise, dB-Hz (GSV / gpsd SKY)
        self._cn0_max = None    # strongest satellite's C/N0, dB-Hz (card signal)
        self._heard = None      # satellites currently heard (reporting a C/N0)
        self._strong = None     # of those, the count at >= STRONG_CN0 (fix-usable)
        self._gsv = {}          # per-talker GSV accumulator for the C/N0 mean
        self._last_rx = None    # monotonic time of the last sentence/report seen
        self._active = None     # the transport actually in use (auto picks one)
        self._fix_lock = threading.Lock()
        self._stop = threading.Event()
        self._reader = threading.Thread(target=self._run, name="gps-reader", daemon=True)
        self._reader.start()
        self.log.info("gps loaded (source=%s)", self.options.get("source", "auto"))

    def on_unload(self):
        self._stop.set()

    # -- dashboard readout ----------------------------------------------
    def on_ui_setup(self, ui):
        ui.add_element("status", "no fix", label="GPS")
        ui.add_element("sats", "-", label="Satellites", kind="number")
        ui.add_element("signal", "-", label="Signal", kind="number")
        ui.add_element("signal_read", "-", label="Signal quality")
        ui.add_element("lat", "-", label="Latitude")
        ui.add_element("lon", "-", label="Longitude")
        ui.add_element("alt", "-", label="Altitude")

    def on_ui_update(self, ui, state):
        with self._fix_lock:
            mode, sats = self._mode, self._sats
            fix = dict(self._fix) if self._fix else None
            # getattr-guarded like the seam writer: tests build partial state.
            cn0_max = getattr(self, "_cn0_max", None)
            strong = getattr(self, "_strong", None)
            last_rx = getattr(self, "_last_rx", None)
        # A receiver that has gone silent (unplugged, powered off, stalled link)
        # would otherwise leave the last fix and signal frozen on the card. Once
        # nothing has arrived for SIGNAL_STALE_S, show the dead-receiver state so
        # that is obvious at a glance rather than a stale reading that looks live.
        if last_rx is not None and time.monotonic() - last_rx > SIGNAL_STALE_S:
            ui.set("status", "no signal")
            ui.set("sats", "?")
            ui.set("signal", "-")
            ui.set("signal_read", "no signal - receiver not responding")
            ui.set("lat", "-")
            ui.set("lon", "-")
            ui.set("alt", "-")
            return
        ui.set("status", {2: "2D fix", 3: "3D fix"}.get(mode, "no fix"))
        ui.set("sats", "?" if sats is None else str(sats))
        number, read = self._signal_text(mode, cn0_max, strong)
        ui.set("signal", number)
        ui.set("signal_read", read)
        if mode >= 2 and fix:
            ui.set("lat", "%.5f" % fix["lat"])
            ui.set("lon", "%.5f" % fix["lon"])
            ui.set("alt", ("%.1f m" % fix["alt"]) if fix.get("alt") is not None else "-")
        else:
            ui.set("lat", "-")
            ui.set("lon", "-")
            ui.set("alt", "-")

    @staticmethod
    def _signal_text(mode, cn0_max, strong):
        """The card's signal readout: the strongest satellite's C/N0 as a
        number, plus a plain-language read of what it means for getting a fix,
        so the operator does not have to read raw NMEA to know whether the
        antenna needs a clearer sky or a lock is just moments away."""
        if cn0_max is None:
            return "-", "no signal - check the antenna and move to open sky"
        number = "%d dB-Hz" % round(cn0_max)
        if mode >= 2:
            return number, "good - position locked"
        strong = strong or 0
        if cn0_max >= GOOD_CN0 and strong >= 4:
            return number, "strong - a fix should lock shortly; keep it still"
        if cn0_max >= GOOD_CN0:
            return number, "strong signal - acquiring; needs a few more satellites"
        if cn0_max >= STRONG_CN0:
            return number, "moderate - acquiring; a fix is likely under open sky"
        if cn0_max >= 25:
            return number, "weak - move to clearer, more open sky"
        return number, "very weak - needs open sky; indoors will not lock"

    # -- position provider ----------------------------------------------
    def provide_position(self):
        """The current fix dict, or None when there is no fix.

        Carries the live signal readout alongside the position (``fix`` mode,
        ``sats`` count, ``cn0`` strongest-satellite C/N0 in dB-Hz, and ``strong``
        the count of fix-usable satellites) so a consumer such as the e-ink face
        can show GPS status and signal without owning a reader. Additive: the
        base keys (lat/lon/alt/fix_time/source) are unchanged."""
        with self._fix_lock:
            if not self._fix:
                return None
            fix = dict(self._fix)
            fix["fix"] = self._mode
            fix["sats"] = self._sats
            fix["cn0"] = getattr(self, "_cn0_max", None)
            fix["strong"] = getattr(self, "_strong", None)
            return fix

    def _set_fix(self, lat, lon, alt=None):
        with self._fix_lock:
            self._fix = {
                "lat": lat, "lon": lon, "alt": alt,
                "fix_time": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                # the transport that produced the fix, so "auto" is recorded as
                # the gpsd/serial it actually used.
                "source": getattr(self, "_active", None) or self.options.get("source", "auto"),
            }
        # Publish the latest fix to the agent seam. A new position is the moment
        # the fix updates; mode/sats/hdop/cn0 carry their most recent values.
        self._publish_latest_fix()

    def _set_mode(self, mode):
        with self._fix_lock:
            self._mode = int(mode)

    def _set_sats(self, sats):
        with self._fix_lock:
            self._sats = int(sats)

    def _set_hdop(self, hdop):
        with self._fix_lock:
            self._hdop = float(hdop)

    def _set_cn0(self, cn0):
        with self._fix_lock:
            self._cn0 = float(cn0)

    def _set_signal(self, maximum, heard, strong):
        """Record the signal stats the card's readout needs: the strongest
        satellite's C/N0, how many satellites are heard at all, and how many of
        those are strong enough (>= STRONG_CN0) to contribute to a fix."""
        with self._fix_lock:
            self._cn0_max = float(maximum) if maximum is not None else None
            self._heard = int(heard) if heard is not None else None
            self._strong = int(strong) if strong is not None else None

    def _mark_rx(self):
        """Note that a sentence/report just arrived, so the card can tell a
        receiver that is still streaming (even with no fix) from one that has
        gone silent. Any data from the transport counts as alive."""
        with self._fix_lock:
            self._last_rx = time.monotonic()

    def _publish_latest_fix(self):
        """Write the latest-fix seam file (:data:`POSITION_FILE`) atomically for
        the out-of-process agent. Best-effort: a missing run dir or a read-only
        /run just means the agent sees no GPS, so a failure logs at debug and is
        otherwise ignored (it must never disturb position tagging or the UI)."""
        with self._fix_lock:
            fix = dict(self._fix) if self._fix else None
            mode, sats = self._mode, self._sats
            # getattr-guarded like _active: tests build partial state by hand.
            hdop, cn0 = getattr(self, "_hdop", None), getattr(self, "_cn0", None)
        if not fix:
            return
        payload = {
            "ts": time.time(),
            "lat": fix["lat"], "lon": fix["lon"], "alt": fix.get("alt"),
            "fix": mode, "sats": sats, "hdop": hdop, "cn0": cn0,
        }
        tmp = POSITION_FILE + ".tmp"
        try:
            os.makedirs(os.path.dirname(POSITION_FILE), exist_ok=True)
            with open(tmp, "w", encoding="utf-8") as handle:
                json.dump(payload, handle)
            os.replace(tmp, POSITION_FILE)
        except OSError as exc:
            log = getattr(self, "log", None)
            if log:
                log.debug("could not write %s: %s", POSITION_FILE, exc)

    # -- handshake tagging ----------------------------------------------
    def on_handshake(self, event):
        fix = self.provide_position()
        if not fix or not event.path:
            return  # no fix: tag nothing (consumers tolerate a missing fix)
        sidecar = event.path + ".gps.json"
        key = event.bssid or "unknown"
        data = {}
        if os.path.exists(sidecar):
            try:
                with open(sidecar, "r", encoding="utf-8") as handle:
                    data = json.load(handle)
            except (OSError, ValueError):
                data = {}
        data[key] = fix
        tmp = sidecar + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as handle:
                json.dump(data, handle)
            os.replace(tmp, sidecar)
        except OSError as exc:
            self.log.warning("could not write GPS sidecar %s: %s", sidecar, exc)

    # -- readers ---------------------------------------------------------
    def _run(self):
        source = self.options.get("source", "auto")
        while not self._stop.is_set():
            try:
                self._read_once(source)
            except Exception as exc:  # noqa: BLE001 - reader errors must not crash the daemon
                self.log.debug("gps reader error (%s); retrying", exc)
            self._stop.wait(5)

    def _read_once(self, source):
        """One read attempt for the configured source. ``auto`` (the default)
        prefers gpsd and falls back to a serial receiver when gpsd is not
        reachable, so a plugged-in USB GPS works with no configuration; explicit
        ``gpsd``/``serial`` use only that transport. With neither available in
        ``auto`` this returns at once and the caller retries after a short wait."""
        if source == "serial":
            self._active = "serial"
            self._read_serial()
        elif source == "gpsd":
            self._active = "gpsd"
            self._read_gpsd()
        else:  # auto: gpsd if it is up, else a serial receiver if one is present
            if self._gpsd_reachable():
                self._active = "gpsd"
                self._read_gpsd()
            elif self._serial_present():
                self._active = "serial"
                self._read_serial()

    def _gpsd_reachable(self):
        """True if a gpsd is accepting connections at the configured host/port.
        A quick probe connect, closed at once; the real read reconnects."""
        host = self.options.get("host", "127.0.0.1")
        port = int(self.options.get("port", 2947))
        try:
            with socket.create_connection((host, port), timeout=3):
                return True
        except OSError:
            return False

    def _serial_candidates(self):
        """Serial nodes to try, in order: the configured device first, then any
        other node in the same family (e.g. ``/dev/ttyACM*``). A USB receiver
        that re-enumerates after a stall often comes back on a different node
        (ttyACM0 -> ttyACM1); scanning the family finds it without a reboot,
        while still preferring the configured node and never straying outside
        its family (so an unrelated UART is not grabbed by mistake)."""
        device = self.options.get("device", "/dev/ttyACM0")
        candidates = [device]
        base = device.rstrip("0123456789")      # /dev/ttyACM0 -> /dev/ttyACM
        if base and base != device:             # only when the node ends in a number
            for node in sorted(glob.glob(base + "*")):
                if node not in candidates:
                    candidates.append(node)
        return candidates

    def _resolve_serial_device(self):
        """The serial node to read now: the configured device if present, else
        another node in the same family (re-enumeration recovery), or None when
        no receiver node exists."""
        for node in self._serial_candidates():
            if os.path.exists(node):
                return node
        return None

    def _serial_present(self):
        """True if a serial receiver node is available - the configured one, or
        another in the same family after a re-enumeration."""
        return self._resolve_serial_device() is not None

    def _read_gpsd(self):
        host = self.options.get("host", "127.0.0.1")
        port = int(self.options.get("port", 2947))
        with socket.create_connection((host, port), timeout=10) as sock:
            sock.sendall(b'?WATCH={"enable":true,"json":true}\n')
            buf = b""
            sock.settimeout(10)
            while not self._stop.is_set():
                chunk = sock.recv(4096)
                if not chunk:
                    return
                buf += chunk
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    self._handle_gpsd_line(line)

    def _handle_gpsd_line(self, line):
        line = line.strip()
        if not line:
            return
        try:
            report = json.loads(line)
        except ValueError:
            return
        self._mark_rx()         # a report arrived: gpsd/receiver is alive
        cls = report.get("class")
        if cls == "TPV":
            self._set_mode(report.get("mode", 0) or 0)
            # some gpsd builds carry the sat count on the TPV report
            for key in ("uSat", "nSat"):
                if report.get(key) is not None:
                    self._set_sats(report[key])
                    break
            if report.get("hdop") is not None:
                self._set_hdop(report["hdop"])
            if (report.get("mode", 0) or 0) >= 2:
                lat, lon = report.get("lat"), report.get("lon")
                if lat is not None and lon is not None:
                    self._set_fix(lat, lon, report.get("alt"))
        elif cls == "SKY":
            satellites = report.get("satellites")
            used = report.get("uSat")
            if used is None and isinstance(satellites, list):
                used = sum(1 for sat in satellites if sat.get("used"))
            if used is not None:
                self._set_sats(used)
            if report.get("hdop") is not None:
                self._set_hdop(report["hdop"])
            # Mean C/N0 of the satellites gpsd lists an SNR (``ss``) for, used
            # ones when it marks them, else all with a reading.
            if isinstance(satellites, list):
                ss = [float(s["ss"]) for s in satellites
                      if s.get("used") and s.get("ss") is not None]
                if not ss:
                    ss = [float(s["ss"]) for s in satellites
                          if s.get("ss") is not None]
                if ss:
                    self._set_cn0(sum(ss) / len(ss))
                # The card's signal read uses every satellite heard (not only
                # the used ones), so it guides acquisition before a fix exists.
                heard = [float(s["ss"]) for s in satellites
                         if s.get("ss") is not None]
                if heard:
                    self._set_signal(max(heard), len(heard),
                                     sum(1 for s in heard if s >= STRONG_CN0))

    def _read_serial(self):
        # Resolve the node each pass so a receiver that re-enumerated to a
        # different node (ttyACM0 -> ttyACM1) is picked up without a reboot, for
        # explicit source=serial as well as auto. None means no node yet; return
        # and let _run retry.
        device = self._resolve_serial_device()
        if device is None:
            return
        # O_NOCTTY is essential: the daemon is a systemd session leader with no
        # controlling terminal, so opening a tty without it makes this serial
        # port the daemon's controlling terminal -- and then unplugging the USB
        # receiver triggers a tty hangup that sends SIGHUP to the whole daemon,
        # killing it (systemd counts SIGHUP as a clean exit, so Restart=on-failure
        # does not bring it back). O_NONBLOCK keeps the open itself from blocking
        # in the tty layer on a half-present node. The daemon also ignores SIGHUP
        # as a backstop, but no serial open here may ever acquire a controlling
        # terminal. Read with a liveness timeout (via select), mirroring
        # _read_gpsd's settimeout: a stalled/suspended CDC-ACM link (the indoor
        # no-fix case) would otherwise wedge the reader inside the read so _run
        # never re-opens; on a long silence we return and _run re-resolves the
        # node. A healthy receiver streams NMEA about once a second even with no
        # fix, so the timeout never trips in normal use.
        fd = os.open(device, os.O_RDONLY | os.O_NOCTTY | os.O_NONBLOCK | os.O_CLOEXEC)
        try:
            buf = b""
            while not self._stop.is_set():
                ready, _, _ = select.select([fd], [], [], 20)
                if not ready:
                    return  # no NMEA for 20s: stalled link; reopen next pass
                try:
                    chunk = os.read(fd, 4096)
                except BlockingIOError:
                    continue  # select raced; nothing to read yet
                except OSError:
                    return    # device went away mid-read; reopen next pass
                if not chunk:
                    return  # EOF
                buf += chunk
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    self._handle_nmea(line.decode("ascii", "replace").strip())
        finally:
            os.close(fd)

    def _handle_nmea(self, sentence):
        if not sentence:
            return
        self._mark_rx()         # a sentence arrived: the receiver is alive
        # Drop the checksum (``*hh``) so the trailing C/N0 field of a GSV
        # sentence parses; the GGA fields this reads all sit before it.
        body = sentence.split("*", 1)[0]
        kind = body[3:6]            # talker-independent: GP/GN/GL/GA... + GGA/GSV
        if kind == "GSV":
            self._handle_gsv(body)
            return
        if kind != "GGA":
            return
        parts = body.split(",")
        if len(parts) < 10 or not parts[2] or not parts[4]:
            return
        try:
            quality = int(parts[6]) if parts[6] else 0
            sats = int(parts[7]) if parts[7] else None
            hdop = float(parts[8]) if parts[8] else None
            lat = _nmea_coord(parts[2], parts[3])
            lon = _nmea_coord(parts[4], parts[5])
            alt = float(parts[9]) if parts[9] else None
        except (ValueError, IndexError):
            return
        if sats is not None:
            self._set_sats(sats)
        if hdop is not None:
            self._set_hdop(hdop)
        # GGA can't tell 2D from 3D; treat a fix with altitude as 3D, else 2D.
        if quality >= 1:
            self._set_mode(3 if alt is not None else 2)
            self._set_fix(lat, lon, alt)
        else:
            self._set_mode(0)

    def _handle_gsv(self, body):
        """Accumulate per-satellite C/N0 from ``GSV`` and set the running mean
        once a talker's group completes. GSV lists the satellites in view in
        groups of up to four per sentence across ``<total>`` sentences; the
        fourth field of each satellite block is its C/N0 in dB-Hz (empty when
        the satellite is not tracked). The mean is pooled across the most recent
        completed group of each talker (GP/GL/GA/...), a robust open-sky vs
        enclosed proxy without needing to know which satellites the fix used."""
        parts = body.split(",")
        if len(parts) < 4:
            return
        talker = body[1:3]
        try:
            total = int(parts[1])
            msgnum = int(parts[2])
        except (ValueError, IndexError):
            return
        snrs = []
        for base in range(4, len(parts), 4):   # 4-field blocks: prn, elev, azi, snr
            block = parts[base:base + 4]
            if len(block) < 4:
                break
            if block[3]:
                try:
                    snrs.append(float(block[3]))
                except ValueError:
                    pass
        buf = self._gsv.setdefault(talker, {})
        if msgnum == 1:
            buf.clear()
        buf["total"] = total
        buf.setdefault("msgs", {})[msgnum] = snrs
        if len(buf["msgs"]) >= total:
            collected = [snr for group in self._gsv.values()
                         for lst in group.get("msgs", {}).values() for snr in lst]
            if collected:
                self._set_cn0(sum(collected) / len(collected))
                self._set_signal(max(collected), len(collected),
                                 sum(1 for s in collected if s >= STRONG_CN0))


def _nmea_coord(value, hemi):
    """Convert an NMEA ddmm.mmmm / dddmm.mmmm coordinate to signed degrees."""
    dot = value.index(".")
    deg_len = dot - 2
    degrees = int(value[:deg_len])
    minutes = float(value[deg_len:])
    result = degrees + minutes / 60.0
    if hemi in ("S", "W"):
        result = -result
    return round(result, 6)
