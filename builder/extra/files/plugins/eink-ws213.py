"""e-ink "tamagotchi" face for the Waveshare 2.13" e-Paper HAT.

Renders the Kestrel face - the classic pwnagotchi-style emoticons, driven by
live device and learning-agent state - to the 2.13" e-Paper HAT (250x122,
black and white; panel revisions V2, V3 and V4, chosen with ``variant``). It
mirrors the ``eink-ws27`` plugin for the 2.7" HAT: the same mood ladder, voice,
mirror and dashboard card, with a lean layout for the smaller panel (see
:meth:`EinkWs213._compose_image`) and no keys (this HAT has none).

The face itself - faces, voices, the mood ladder, the motion estimate, the
Kestrel mark and the glyphs - comes from the shared ``eink_face`` plugin library
(``_lib_eink_face.py``), which ``eink-ws27`` uses too; a fix to the face is made
there once. This file holds what is particular to this HAT: its options, its
layout, how it brings the panel up and redraws it, and the web mirror.
Waveshare's own driver ships beside it unmodified as helper files
(``_eink-ws213_epdconfig.py``, ``_eink-ws213_epd2in13_V4.py``, ``_V3``, ``_V2``;
see ``docs/developer/vendored-code.md``) loaded with :func:`kestrel_pi.load_helper`.

The panel is redrawn as Waveshare recommends for it: quick partial redraws (no
flash) and a periodic full refresh that clears ghosting. Not yet tested on
hardware: the docs ask testers to report results (CONTRIBUTING.md).

Operator guide: ``docs/user-guide/eink-ws213-setup.md``. The plugin is optional
and absent-safe: the Waveshare driver and PIL are imported lazily, only on a
real device, so importing this module (and running on a HAT-less device or the
test host) never touches the hardware. It ships disabled.

The face never lies: every expression maps to a real device/agent state (for
example ``smart`` shows only when a *learned* policy is actually driving, not
the scripted baseline fallback), arbitrated by an explicit priority ladder in
:func:`decide_mood` in the face library.
"""

import threading
import time
from urllib.parse import urlencode

from kestrel_pi import Plugin, load_helper, load_library, webtls

# The face itself - faces, voices, the mood ladder, motion, the Kestrel mark,
# glyphs and text helpers - is the shared eink_face plugin library
# (_lib_eink_face.py); this file holds what is particular to this panel.
face = load_library(__file__, "eink_face", api=1)

# Panel geometry: the 2.13" HAT is 250x122 landscape (122x250 native portrait;
# the Waveshare getbuffer rotates a landscape image to fit).
PANEL_W, PANEL_H = 250, 122


class EinkWs213(Plugin):
    __author__ = "kestrel"
    __version__ = "0.1.0"
    __license__ = "GPL3"
    __description__ = ("e-ink face for the Waveshare 2.13\" e-Paper HAT (V2/V3/V4, "
                       "black and white). Not yet tested on hardware.")

    __target_api__ = 2
    # Local display + a read-only web mirror page; nothing leaves the device, so
    # no authorized-use ack (only the `network` capability gates that).
    __capabilities__ = ("ui", "web")
    # All optional: the face degrades to capture/online/idle moods when the
    # agent is off, no UPS HAT is present, or there is no GPS fix.
    __optional_requires__ = ("agent", "battery", "position")
    # One row button: a read-only mirror of the exact panel frame (a GET, so no
    # auth/CSRF gate - it shows only the rendered face, no secret).
    __actions__ = ({"label": "View", "path": "display",
                    "method": "get", "order": 1},)
    __options__ = {
        # The revisions can't be told apart by probing (they all drive BUSY the
        # same way), so the operator picks the one on the panel's sticker.
        "variant": {"type": "str", "default": "v4", "choices": ["v4", "v3", "v2"],
                    "help": "The panel revision printed on the sticker on its "
                            "back. V4 is the current one."},
        "rotation": {"type": "int", "default": 0, "choices": [0, 90, 180, 270],
                     "help": "Turn the picture: 180 for a HAT mounted the other "
                             "way up."},
        "refresh_seconds": {"type": "int", "default": 10},  # render cadence
        "partials_per_full": {
            "type": "int", "default": 20,
            "help": "Quick redraws (no flash) between full refreshes. A full "
                    "refresh flashes and clears ghosting, and is never sooner "
                    "than 3 minutes after the last one. 0 makes every redraw a "
                    "full refresh."},
        "show_mascot": {"type": "bool", "default": True},
        "show_brand_glyph": {"type": "bool", "default": True},
        "show_fingerprint_minutes": {
            "type": "int", "default": 10,
            "help": "Minutes after start-up that the panel shows the start of the "
                    "dashboard's HTTPS certificate fingerprint (0 = never)."},
        "show_ip": {"type": "bool", "default": True,
                    "help": "Show the Bluetooth tether and Wi-Fi addresses on the "
                            "panel, so you can find the dashboard without a "
                            "router's client list."},
        "battery_low_percent": {"type": "int", "default": 10},
        "bored_epochs": {"type": "int", "default": 15},
        "sad_epochs": {"type": "int", "default": 25},
        # Optional per-mood face overrides: a {mood_key: glyph} table under
        # [plugins.eink-ws213.faces]. Unset keeps the shipped classic emoticons.
        "faces": {"type": "dict"},
        # Optional per-mood voice overrides: a {mood_key: phrase-or-[phrases]}
        # table under [plugins.eink-ws213.voices]. Unset keeps the shipped phrases.
        "voices": {"type": "dict"},
    }

    # The mirror has no cadence of its own: the page reloads at refresh_seconds
    # and re-reads this one frame store, so the panel and the mirror can't drift.
    _MIN_REFRESH = 10
    # How long the render worker waits for on_ready before going on anyway.
    _READY_WAIT = 60

    # Driver per revision; the helper files ship beside this plugin.
    _VARIANTS = {"v4": "epd2in13_V4", "v3": "epd2in13_V3", "v2": "epd2in13_V2"}
    # A correct panel's init+Clear finishes in a few seconds; bound the BUSY
    # wait so a missing or wrong panel can't hang the render thread.
    _PROBE_TIMEOUT = 20.0
    # Waveshare: keep at least 180 s between full refreshes.
    _FULL_MIN_SECONDS = 180

    # -- lifecycle -------------------------------------------------------
    def on_loaded(self):
        self._setup()
        # One e-ink plugin drives the panel (the HATs share its pins); which
        # one is settled in on_ready, once every plugin has loaded.
        face.claim_panel(self, "eink-ws213")
        self._worker = threading.Thread(
            target=self._run, name="eink-ws213-face", daemon=True)
        self._worker.start()

    def on_ready(self):
        """Every plugin has loaded, so the panel's owner is settled: let the
        worker bring the panel up, or stand down."""
        owner = face.panel_owner()
        if owner not in (None, "eink-ws213"):
            self._stand_down(owner)
        self._ready.set()

    def _stand_down(self, owner):
        """Another e-ink plugin drives the panel: never touch the pins, keep
        the web mirror, and say which plugin has the panel."""
        self._standing_down = owner
        self._absent = True
        self._panel_tried = True
        text = "%s drives the e-Paper panel (one e-ink plugin at a time)" % owner
        self.context.health_note = "standing down: " + text
        self.log.info("eink-ws213: standing down: %s", text)

    def _setup(self):
        """Initialise state without the panel or the worker thread, so the
        tests can exercise the mood ladder and the frame compositor directly."""
        self.log = self.context.logger
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._panel = None
        self._panel_tried = False
        self._absent = True             # no panel until a probe succeeds
        self._ready = threading.Event()  # set in on_ready: the panel's owner is settled
        self._standing_down = None      # the e-ink plugin that drives the panel instead
        self._fault = False
        self._frame_png = None          # last composed frame (PNG bytes)
        self._frame_seq = 0             # increments on every frame change
        self._state = None              # last UIState seen
        self._mood = None               # last mood the worker computed
        self._voice = ""                # last phrase the worker picked
        self._voice_rot = 0             # advances on each mood change (rotates
        #                                 the phrase without a per-tick redraw)
        self._started = time.monotonic()
        self._moods = face.MoodTracker()  # reward trend, promotion, idle, handshake
        self._motion = face.MobilityTracker()  # parked / walking / driving
        self._fp = None                 # dashboard certificate fingerprint
        self._fp_checked = None         # monotonic time of the last lookup
        self._partials = 0              # quick redraws since the last full one
        self._last_full = None          # monotonic time of the last full refresh

    def on_unload(self):
        # Blank the panel on the way down so a shutdown (the power-off button
        # or the UPS-HAT low-battery auto-off) never leaves the last face frozen
        # on the e-paper, which keeps its image with no power. Stop and JOIN the
        # render worker first, so an in-flight redraw can't race the blank on
        # the SPI bus or redraw the face after it.
        self._stop.set()
        self._ready.set()
        worker = getattr(self, "_worker", None)
        if worker is not None and worker.is_alive():
            worker.join(timeout=self._PROBE_TIMEOUT)
        panel = self._panel
        if panel is not None:
            try:
                panel.blank()
            except Exception as exc:  # noqa: BLE001 - best effort on the way out
                self.log.debug("eink-ws213: shutdown render failed: %s", exc)
        face.release_panel(self)

    # -- web dashboard data (the shared model's other view) --------------
    def on_ui_setup(self, ui):
        ui.add_element("face", "(◕‿‿◕)", label="Face")
        ui.add_element("mood", "awake", label="Mood")
        ui.add_element("voice", "", label="Says")
        ui.add_element("mobility", "gps?", label="Motion")
        ui.add_element("panel", "checking", label="Display")

    def on_ui_update(self, ui, state):
        # Fast path only: stash the state and update the web card from the last
        # mood the render worker computed. The worker is the single owner of the
        # mood trackers, so the dashboard shows exactly what the panel shows.
        with self._lock:
            self._state = state
            mood = self._mood
            voice = self._voice
        ui.set("face", self._face_glyph(mood) if mood else face.FACES["awake"])
        ui.set("mood", mood or "awake")
        ui.set("voice", voice or "")
        ui.set("mobility", face.MOBILITY_LABEL.get(self._motion.state, "gps?"))
        ui.set("panel", ("used by %s" % self._standing_down) if self._standing_down
               else "not detected" if self._absent
               else "%s detected" % self._variant())

    def on_handshake(self, event):
        # A fresh handshake lights up an excited face briefly and counts as
        # activity (resets the idle ladder).
        with self._lock:
            st = self._state
            self._moods.handshake(st.epoch if st is not None else None)

    # -- mood ------------------------------------------------------------
    def _current_mood(self, state):
        """Run the mood ladder (face library) on the snapshot and the trackers."""
        mobility = self._mobility_state(self._live_position(state))
        return self._moods.decide(state, self.context.options, mobility, fault=self._fault)

    def _mobility_state(self, fix):
        """Fold the latest GPS fix into the motion estimate (face library)."""
        return self._motion.update(fix)

    def _live_position(self, state):
        """The freshest GPS fix for the motion estimate: the position service
        now, else the snapshot in ``state``. The snapshot is refreshed once per
        daemon epoch (60 s by default), the face every ``refresh_seconds``, so
        reading the service makes motion follow the GPS, not the epoch."""
        live = self.context.services.get("position")
        if live:
            return live
        return state.position if state is not None else None

    def _face_glyph(self, mood):
        return face.face_glyph(mood, self.context.options.get("faces"))

    def _voice_for(self, mood, rot):
        """A phrase matching the decided mood (see ``face.voice_for``)."""
        return face.voice_for(mood, rot, self.context.options.get("voices"))

    # -- rendering + panel ----------------------------------------------
    def _run(self):
        # Wait for on_ready (every plugin loaded, the panel's owner settled);
        # the bound keeps the face going should it never come.
        self._ready.wait(timeout=self._READY_WAIT)
        while not self._stop.is_set():
            try:
                self._tick()
            except Exception as exc:  # noqa: BLE001 - a render error never kills the loop
                self._fault = True
                self.log.debug("eink-ws213: render tick failed: %s", exc)
            self._stop.wait(timeout=self._refresh_seconds())

    def _refresh_seconds(self):
        return max(self._MIN_REFRESH,
                   int(self.context.options.get("refresh_seconds", 10) or 10))

    def _tick(self):
        with self._lock:
            state = self._state
            prev_mood = self._mood
            rot = self._voice_rot
            voice = self._voice
        mood = self._current_mood(state)          # worker owns the trackers
        if mood != prev_mood or not voice:
            rot += 1                               # new mood -> next phrase
            voice = self._voice_for(mood, rot)
        image = self._compose_image(state, mood, voice)
        png = face.png_bytes(image)
        changed = png != self._frame_png
        with self._lock:
            self._mood = mood
            self._voice = voice
            self._voice_rot = rot
            if changed:
                self._frame_png = png
                self._frame_seq += 1
        self._push_panel(image, changed)
        self._fault = False

    def _partials_per_full(self):
        try:
            return max(0, int(self.context.options.get("partials_per_full", 20)))
        except (TypeError, ValueError):
            return 20

    def _push_panel(self, image, changed):
        """Redraw the physical panel when one is present and the frame changed,
        as Waveshare recommends for this panel: quick partial redraws (no
        flash), and a full refresh - which clears the ghosting partial redraws
        leave - first, then after ``partials_per_full`` of them, never sooner
        than _FULL_MIN_SECONDS after the last full one."""
        panel = self._ensure_panel()
        if panel is None or not changed:
            return
        every = self._partials_per_full()
        now = time.monotonic()
        full = (self._last_full is None or every == 0
                or (self._partials >= every
                    and now - self._last_full >= self._FULL_MIN_SECONDS))
        try:
            if full:
                panel.full(image)
                self._last_full = now
                self._partials = 0
            else:
                panel.partial(image)
                self._partials += 1
        except Exception as exc:  # noqa: BLE001
            self._fault = True
            self.log.debug("eink-ws213: panel redraw failed: %s", exc)

    def _variant(self):
        return str(self.context.options.get("variant", "v4") or "v4").lower()

    def _ensure_panel(self):
        """Lazily bring the panel up, once, with the driver for the chosen
        revision. Returns the panel or None (a HAT-less device / build host
        degrades cleanly to the web mirror)."""
        if self._panel is not None or self._panel_tried:
            return self._panel
        owner = face.panel_owner()
        if owner not in (None, "eink-ws213"):
            self._stand_down(owner)
            return None
        self._panel_tried = True
        variant = self._variant()
        module = self._VARIANTS.get(variant)
        if module is None:
            panel, reason = None, "unknown variant %r" % variant
        else:
            panel, reason = self._try_panel(variant, module, self._PROBE_TIMEOUT)
        if panel is not None:
            self._panel = panel
            self._absent = False
            self.log.info("eink-ws213: %s panel ready", module)
            return panel
        self._absent = True
        if "pins are in use" in reason:
            self.context.health_note = ("the e-Paper pins are in use (another "
                                        "e-ink plugin enabled?)")
        else:
            self.context.health_note = ("no 2.13\" e-Paper HAT detected "
                                        "(variant %s)" % variant)
        self.log.info("eink-ws213: no panel (%s); rendering to the web mirror only",
                      reason)
        return None

    def _try_panel(self, variant, module, timeout):
        """Load the revision's Waveshare driver and run its init+Clear, bounding
        the driver's BUSY polling with a deadline so a missing or stuck panel
        raises instead of looping forever. Returns (panel, None) or (None,
        reason). On failure SPI is closed, so the next start can try again.

        A panel has no readback, so init+Clear completing is not by itself
        proof one is wired: the header's BUSY line is pulled low, and the
        busy-wait returns while it reads idle (0: idle, 1: busy), so a HAT-less
        device - or a different HAT such as a UPS HAT - sails through init and
        would read as "detected". A present panel *drives* the BUSY line (reads
        1) while the controller works through init and Clear, so the probe also
        requires the BUSY line to be observed driven high."""
        try:
            # The driver ships beside this plugin as helper files, epdconfig
            # first: each driver does ``from . import epdconfig``.
            epdconfig = load_helper(__file__, "_eink-ws213_epdconfig.py")
            drv = load_helper(__file__, "_eink-ws213_%s.py" % module)
        except Exception as exc:  # noqa: BLE001 - driver/SPI/GPIO absent off-Pi
            if type(exc).__name__.endswith("PinInUse"):
                return None, "the e-Paper pins are in use (%s)" % exc
            return None, "%s unavailable (%s)" % (module, exc)
        panel = _Panel(drv.EPD(), variant)
        orig_read = epdconfig.digital_read
        deadline = time.monotonic() + timeout
        saw_busy_driven = False

        def _guarded_read(pin):
            nonlocal saw_busy_driven
            if time.monotonic() > deadline:
                raise TimeoutError("%s busy-wait exceeded %gs" % (module, timeout))
            value = orig_read(pin)
            # BUSY is the only line the drivers read. A 1 means a panel is
            # driving it; a HAT-less header sits at the pull-down and reads 0.
            if value == 1:
                saw_busy_driven = True
            return value

        epdconfig.digital_read = _guarded_read
        try:
            panel.init_and_clear()
        except Exception as exc:  # noqa: BLE001 - timed out / no panel: degrade
            try:
                epdconfig.module_exit()
            except Exception:             # noqa: BLE001
                pass
            return None, "%s did not initialise (%s)" % (module, exc)
        finally:
            epdconfig.digital_read = orig_read
        if not saw_busy_driven:
            try:
                epdconfig.module_exit()
            except Exception:             # noqa: BLE001
                pass
            return None, "%s: BUSY line never driven (no panel wired)" % module
        return panel, None

    def _compose_image(self, state, mood, voice=None):
        """Compose the 250x122 1-bit frame for a mood the caller already
        decided. The lean layout for this small panel:

        - a top icon strip: BT PAN and Wi-Fi link glyphs, a GPS satellite with
          signal bars when a receiver is present, a walking/driving glyph while
          moving, and the battery gauge with its percent;
        - the mascot face (20 px) with its voice beside it (two lines), or for
          the first ``show_fingerprint_minutes`` the start of the dashboard's
          HTTPS fingerprint;
        - one status row: a chip glyph + the agent's mode, an antenna glyph
          (waves while capturing, slashed when idle) and ``UP`` uptime;
        - the address row: each management link's IPv4 behind its glyph;
        - the counters, larger: ``PWND`` handshakes with this session's count in
          parens, ``AP`` and ``NET``, both lifetime; and the Kestrel mark in the
          lower-right, stooping when moving, hovering when parked.

        The 2.7" panel's channel plan (``CH``) and density meter (``NEAR``) are
        left out: they stay on the dashboard. Every row places its segments by
        measured width and stops before the edge or the brand mark. Also used
        for the web mirror, so it runs with no panel present."""
        from PIL import Image, ImageDraw
        opts = self.context.options
        img = Image.new("1", (PANEL_W, PANEL_H), 255)   # white
        draw = ImageDraw.Draw(img)
        tiny = face.font(10)
        small = face.font(11)
        count = face.font(15)
        big = face.font(20)

        # One fresh address lookup drives both the link glyphs and the address
        # row, so they always agree.
        addresses = self._addresses()
        linked = {iface for iface, _ip in addresses}
        battery = (state.battery if state is not None else None) or {}
        pct = face.as_float(battery.get("percent"))
        position = state.position if state is not None else None
        gps_present = bool(self.context and self.context.services
                           and self.context.services.has("position"))

        # top icon strip: link + GPS symbols on the left, battery on the right.
        face.draw_bt(draw, 4, 2, 8, 12, "bnep0" in linked)
        face.draw_wifi(draw, 19, 4, 14, 10, "wlan0" in linked)
        if gps_present:
            locked = position is not None and int(face.as_float(position.get("fix")) or 0) >= 2
            cn0 = face.as_float(position.get("cn0")) if position else None
            face.draw_sat(draw, 42, 2, 13, 12, locked)
            face.draw_sigbars(draw, 62, 4, 9, 10, face.signal_level(cn0, locked))
            if self._motion.state in face.MOVING:
                # x 84: a car's trailing motion lines reach 7 px to its left
                face.draw_motion(draw, 84, 2, 16, 12, self._motion.state)
        if pct is not None:
            face.draw_battery(draw, 224, 3, 20, 10, pct)
            face.text_right(draw, 219, 2, "%d%%" % int(pct), tiny)
        draw.line([(2, 16), (PANEL_W - 2, 16)], fill=0, width=1)

        # the mascot face with its voice beside it: the fingerprint just after
        # start-up (for checking the browser's certificate warning), else the
        # mood's phrase, two lines at most.
        if opts.get("show_mascot", True):
            glyph = self._face_glyph(mood)
            draw.text((5, 19), glyph, font=big, fill=0)
            vx = min(max(5 + face.text_w(draw, glyph, big) + 8, 96), 120)
            lines = self._fingerprint_lines()
            if not lines and voice:
                lines = face.wrap_text(draw, voice, tiny, PANEL_W - 4 - vx)
            for i, line in enumerate(lines[:2]):
                draw.text((vx, 23 + i * 12), line, font=tiny, fill=0)

        # one status row: chip + agent mode, antenna (capture state), uptime.
        agent = state.agent if state is not None else None
        capturing = bool(state is not None and state.capture_active)
        agent_txt = face.agent_label(agent)
        sy = 55
        face.draw_chip(draw, 6, sy + 2, 10, 9)
        draw.text((20, sy), agent_txt, font=small, fill=0)
        x = 20 + face.text_w(draw, agent_txt, small) + 8
        face.draw_antenna(draw, x, sy, 10, 12, capturing)
        x += 16
        uptime_txt = face.fmt_uptime(state.uptime if state is not None else None)
        if uptime_txt:
            seg = "UP " + uptime_txt
            if x + face.text_w(draw, seg, small) <= PANEL_W - 4:
                draw.text((x, sy), seg, font=small, fill=0)

        # address row: each management link's IPv4 behind its BT/Wi-Fi glyph; an
        # address that won't fit is dropped rather than drawn off the edge.
        if opts.get("show_ip", True):
            ay = 73
            x = 6
            for iface, ip in addresses:
                gap = 10 if iface == "bnep0" else 14      # glyph width + spacing
                need = gap + face.text_w(draw, ip, tiny)
                if x + need > PANEL_W - 4:
                    break
                if iface == "bnep0":
                    face.draw_bt(draw, x, ay + 1, 6, 10, True)
                else:
                    face.draw_wifi(draw, x + 2, ay + 2, 9, 8, True)
                draw.text((x + gap, ay), ip, font=tiny, fill=0)
                x += need + 12

        # the counters, larger than the rest: "N handshakes (this session) from
        # M access points across K networks". Segments stop before the mark.
        by = 92
        right_limit = PANEL_W - 32   # keep clear of the lower-right brand glyph
        draw.line([(2, by - 4), (PANEL_W - 30, by - 4)], fill=0, width=1)
        x = 6
        total = state.capture_total if state is not None else None
        if total is not None:
            sess = (state.capture_session
                    if state.capture_session is not None else 0)
            seg = "%d (%d)" % (int(total), int(sess))
            if 22 + face.text_w(draw, seg, count) <= right_limit:
                face.draw_key(draw, x, by + 5, 12, 10)
                x += 16
                draw.text((x, by), seg, font=count, fill=0)
                x += face.text_w(draw, seg, count) + 12
        for value, label in ((state.aps_total if state is not None else None, "AP"),
                             (state.networks_total if state is not None else None, "NET")):
            if value is None:
                continue
            seg = "%s %d" % (label, int(value))
            if x + face.text_w(draw, seg, count) > right_limit:
                break
            draw.text((x, by), seg, font=count, fill=0)
            x += face.text_w(draw, seg, count) + 12

        # the Kestrel brand mark, lower-right; its pose doubles as the motion
        # readout - stooping (diving) when moving, hovering when not.
        if opts.get("show_brand_glyph", True):
            face.draw_kestrel(draw, PANEL_W - 27, PANEL_H - 27, 24, self._motion.state in face.MOVING)

        return self._rotated(img)

    def _rotated(self, img):
        rotation = int(self.context.options.get("rotation", 0) or 0)
        return img.rotate(rotation, expand=True) if rotation else img

    def _fingerprint_lines(self):
        """``["HTTPS AB:CD:EF:12", "34:56:78:9A"]`` (the start of the dashboard
        certificate's SHA-256 fingerprint) during the first
        ``show_fingerprint_minutes`` after start-up, else ``[]``. A method so the
        user-guide screenshot fixture can turn it off."""
        try:
            minutes = int(self.context.options.get("show_fingerprint_minutes", 10) or 0)
        except (TypeError, ValueError):
            minutes = 10
        now = time.monotonic()
        if minutes <= 0 or now - self._started > minutes * 60:
            return []
        if self._fp is None and (self._fp_checked is None or now - self._fp_checked >= 30):
            self._fp_checked = now
            self._fp = webtls.fingerprint()
        if not self._fp:
            return []
        parts = self._fp.split(":")
        return ["HTTPS " + ":".join(parts[:4]), ":".join(parts[4:8])]

    def _addresses(self):
        """``[(iface, ipv4), ...]`` for the management links that have an
        address, Bluetooth tether first. A method so a caller (the tests, the
        user-guide screenshot fixture) can substitute placeholder addresses."""
        return face.addresses()

    # -- web mirror ------------------------------------------------------
    def on_webhook(self, path, request):
        if path == "display.png":
            return self._mirror_png(request)
        if path == "display":
            return self._mirror_page(request)
        return self.context.redirect("No such view.", error=True)

    def _mirror_png(self, request):
        with self._lock:
            png, seq = self._frame_png, self._frame_seq
        if png is None:
            # Compose on demand so the first view works before the worker runs.
            with self._lock:
                state, mood, voice, rot = (self._state, self._mood,
                                           self._voice, self._voice_rot)
            if mood is None:
                mood = self._current_mood(state)
            if not voice:
                voice = self._voice_for(mood, rot + 1)
            png = face.png_bytes(self._compose_image(state, mood, voice))
            with self._lock:
                if self._frame_png is None:
                    self._frame_png, self._frame_seq = png, self._frame_seq + 1
                    self._mood = mood
                    self._voice = voice
                png, seq = self._frame_png, self._frame_seq
        etag = '"eink-ws213-%d"' % seq
        headers = {"Content-Type": "image/png", "Cache-Control": "no-store",
                   "ETag": etag}
        if request is not None and request.headers.get("If-None-Match") == etag:
            return ("", 304, headers)
        return (png, 200, headers)

    def _mirror_page(self, request=None):
        esc = self.context.esc
        # The standard way back, as on the other plugin pages (a token login
        # carries its token along).
        token = (getattr(request, "args", None) or {}).get("token") or ""
        qs = "?" + urlencode([("token", token)]) if token else ""
        every = self._refresh_seconds()
        if self._standing_down:
            note = (" (%s drives the panel - this is the frame this plugin would "
                    "show)" % self._standing_down)
        else:
            note = (" (no HAT attached - this is the frame it would show)"
                    if self._absent else "")
        body = (
            '<p><a href="' + esc("/plugins" + qs) + '">&larr; Plugins</a> &middot; '
            '<a href="' + esc("/" + qs) + '">Dashboard</a></p>'
            "<h1>E-ink display (2.13&quot;)</h1>"
            '<p class="muted">A live mirror of what is on the panel' + esc(note)
            + ". It reloads every " + str(every)
            + " s, in step with the panel, from the same frame.</p>"
            '<p><img src="display.png" alt="e-ink display" '
            'style="width:100%;max-width:500px;image-rendering:pixelated;'
            'border:1px solid #888;background:#fff"></p>'
            "<script>setTimeout(function(){location.reload();}, "
            + str(every * 1000) + ");</script>")
        return self.context.page("e-ink display (2.13\")", body)


class _Panel:
    """One brought-up 2.13" driver, with the redraws Waveshare's demos use for
    its revision. V3 and V4: ``init()``, then ``displayPartBaseImage`` for a
    full refresh that is also the base partial redraws build on, then
    ``displayPartial``. V2 is the same with ``init(FULL_UPDATE)`` before the
    full refresh and ``init(PART_UPDATE)`` before partial redraws."""

    def __init__(self, epd, variant):
        self.epd = epd
        self.variant = variant
        self.name = type(epd).__module__.rpartition(".")[2]

    def _init(self, partial=False):
        if self.variant == "v2":
            self.epd.init(self.epd.PART_UPDATE if partial else self.epd.FULL_UPDATE)
        else:
            self.epd.init()

    def init_and_clear(self):
        self._init()
        self.epd.Clear(0xFF)

    def full(self, image):
        self._init()
        self.epd.displayPartBaseImage(self.epd.getbuffer(image))
        if self.variant == "v2":
            self._init(partial=True)

    def partial(self, image):
        self.epd.displayPartial(self.epd.getbuffer(image))

    def blank(self):
        """Clear the panel and put it to sleep, so it keeps no stale image."""
        self._init()
        self.epd.Clear(0xFF)
        self.epd.sleep()


# --- small helpers ---------------------------------------------------------
