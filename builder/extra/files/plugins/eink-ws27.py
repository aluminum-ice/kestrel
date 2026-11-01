"""e-ink "tamagotchi" face for the Waveshare 2.7" e-Paper HAT (V1 and V2).

Renders the classic pwnagotchi-style animated face to a Waveshare 2.7" e-Paper
HAT (264x176, 1-bit mono): a top icon strip (BT PAN + Wi-Fi link glyphs, a GPS
satellite + signal bars when a receiver is present, a person/car motion glyph
while moving, and a battery gauge); the mascot face offset to the left with its
voice line - a short mood-matched phrase - beside it; a compact symbol-led status
row (a chip glyph + agent mode, an antenna glyph + capture state); an info row
(uptime and, while capturing, the channel plan); a bottom counter row (captured
handshakes with session count, then distinct access points and distinct networks);
and the Kestrel mark once in the lower-right corner, its pose doubling as the
motion readout (parked/walking/driving). The face's mood is driven from live
device and learning-agent state via the shared UI model (``on_ui_update``'s
:class:`~kestrel_pi.ui.UIState` snapshot, plus the ``agent``/``battery``/
``position`` services) - the web dashboard and the e-ink panel are two views of
one state model.

It replaced the ``eink`` plugin (the same face under a name that says which
display it drives, like ``eink-ws213``); ``eink`` was removed in 1.0.11.

The face itself - faces, voices, the mood ladder, the motion estimate, the
Kestrel mark and the glyphs - comes from the shared ``eink_face`` plugin library
(``_lib_eink_face.py``), which ``eink-ws213`` uses too. This file holds what is
particular to this HAT: its options, its layout, how it brings the panel up, the
HAT's four keys (on the shared ``hat_keys`` library) and the web mirror.
Waveshare's own driver ships beside it unmodified as helper files
(``_eink-ws27_epdconfig.py``, ``_eink-ws27_epd2in7.py``,
``_eink-ws27_epd2in7_V2.py``; see ``docs/developer/vendored-code.md``) loaded
with :func:`kestrel_pi.load_helper`.

Operator guide: ``docs/user-guide/eink-ws27-setup.md``. The plugin is optional
and absent-safe: the Waveshare driver, gpiozero and PIL are imported lazily, only
on a real device, so importing this module (and running on a HAT-less device or
the test host) never touches the hardware. It ships disabled.

The face never lies: every expression maps to a real device/agent state (for
example ``smart`` shows only when a *learned* policy is actually driving, not
the scripted baseline fallback), arbitrated by an explicit priority ladder in
:func:`decide_mood` in the face library.
"""

import os
import threading
import time

from kestrel_pi import Plugin, load_helper, load_library, webtls

# The face itself - faces, voices, the mood ladder, motion, the Kestrel mark,
# glyphs and text helpers - is the shared eink_face plugin library
# (_lib_eink_face.py); this file holds what is particular to this panel.
face = load_library(__file__, "eink_face", api=1)
# The HAT's keys: gestures, prompts and the safety rules for capture and power.
keys = load_library(__file__, "hat_keys", api=0)

# The plugin's name is its file name: it starts the log lines and names the
# helper files, the poll thread and the mirror's ETag.
NAME = os.path.splitext(os.path.basename(__file__))[0]

# Panel geometry: the 2.7" HAT is 264x176 landscape (176x264 native portrait;
# the Waveshare getbuffer rotates a landscape image to fit).
PANEL_W, PANEL_H = 264, 176


class EinkWs27(Plugin):
    __author__ = "kestrel"
    __version__ = "0.2.0"
    __license__ = "GPL3"
    __description__ = "e-ink tamagotchi face for the Waveshare 2.7\" e-Paper HAT (V1/V2)."

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
        "variant": {"type": "str", "default": "auto", "choices": ["auto", "v2", "v1"],
                    "help": "The panel revision: auto tries V2, then V1."},
        "rotation": {"type": "int", "default": 0, "choices": [0, 90, 180, 270],
                     "help": "Turn the picture; 180 for a HAT mounted the other way up."},
        "refresh_seconds": {"type": "int", "default": 10},  # render cadence
        "greyscale": {"type": "bool", "default": False},    # 4-gray (slow) off
        "show_mascot": {"type": "bool", "default": True},
        "show_brand_glyph": {"type": "bool", "default": True},
        # The address row: each management link's IPv4, so a headless device
        # can be found without the router's client list.
        # For this many minutes after the daemon starts, the voice line shows
        # the start of the dashboard's HTTPS certificate fingerprint, to compare
        # with the browser on first sign-in. 0 never shows it.
        "show_fingerprint_minutes": {
            "type": "int", "default": 10,
            "help": "Minutes after start-up that the panel shows the start of the "
                    "dashboard's HTTPS certificate fingerprint (0 = never)."},
        "show_ip": {"type": "bool", "default": True,
                    "help": "Show the Bluetooth tether and Wi-Fi addresses on the "
                            "panel, so you can find the dashboard without a "
                            "router's client list."},
        # The HAT's four keys (hat_keys library): KEY1 info screen, KEY2 stop
        # or (after a second press) start capture, KEY3/KEY4 reboot/shut down
        # after a second press.
        "buttons": {"type": "bool", "default": True,
                    "help": "Use the HAT's four keys: info screen, start and stop "
                            "capture, reboot, shut down."},
        "pins": {"type": "str", "default": "5,6,13,19",
                 "help": "BCM GPIO pins of KEY1-KEY4, comma-separated. The defaults "
                         "suit the Waveshare 2.7\" e-Paper HAT."},
        "hold_seconds": {"type": "int", "default": 3,
                         "help": "How long a press counts as a long press."},
        "allow_capture": {"type": "bool", "default": True,
                          "help": "Let a long KEY2 press stop capture, or start it "
                                  "after a second press (only once you've accepted "
                                  "the authorized-use acknowledgment)."},
        "allow_power": {"type": "bool", "default": True,
                        "help": "Let a long KEY3/KEY4 press, then a second press, "
                                "reboot or shut down."},
        "battery_low_percent": {"type": "int", "default": 10},
        "bored_epochs": {"type": "int", "default": 15},
        "sad_epochs": {"type": "int", "default": 25},
        # Optional per-mood face overrides: a {mood_key: glyph} table under
        # [plugins.<name>.faces]. Unset keeps the shipped classic emoticons.
        "faces": {"type": "dict"},
        # Optional per-mood voice overrides: a {mood_key: phrase-or-[phrases]}
        # table under [plugins.<name>.voices]. Unset keeps the shipped phrases.
        "voices": {"type": "dict"},
    }

    # The mirror has no cadence of its own: the page reloads at refresh_seconds
    # and re-reads this one frame store, so the panel and the mirror can't drift.
    _MIN_REFRESH = 10
    # How long the render worker waits for on_ready before going on anyway.
    _READY_WAIT = 60

    # -- lifecycle -------------------------------------------------------
    def on_loaded(self):
        self._setup()
        # One e-ink plugin drives the panel (the HATs share its pins and the
        # keys); which one is settled in on_ready, once every plugin has loaded.
        face.claim_panel(self, NAME)
        self._worker = threading.Thread(
            target=self._run, name=NAME + "-face", daemon=True)
        self._worker.start()

    def on_ready(self):
        """Every plugin has loaded, so the panel's owner is settled: start the
        keys only when this plugin drives the panel, then let the worker bring
        the panel up (or stand down)."""
        owner = face.panel_owner()
        if owner not in (None, NAME):
            self._stand_down(owner)
        elif self.context.options.get("buttons", True):
            self._start_keys()
        self._ready.set()

    def _stand_down(self, owner):
        """Another e-ink plugin drives the panel: never touch the pins or the
        keys, keep the web mirror, and say which plugin has the panel."""
        self._standing_down = owner
        self._absent = True
        self._epd_tried = True
        text = "%s drives the e-Paper panel (one e-ink plugin at a time)" % owner
        self._note("standing down: " + text)
        self.log.info("%s: standing down: %s", NAME, text)

    def _start_keys(self):
        """The HAT's four keys through the hat_keys library. A bad ``pins``
        value leaves them off, with the reason on the dashboard card."""
        try:
            pins = keys.parse_pins(self.context.options.get("pins"), 4, DEFAULT_PINS)
        except ValueError as exc:
            self._keys_note = "keys unavailable: %s" % exc
            self.log.info("%s: HAT keys unavailable (%s)", NAME, exc)
            return
        self._keys = keys.Keys(
            NAME,
            [keys.Key("KEY1", pins[0], press=self._toggle_info),
             keys.Key("KEY2", pins[1], hold="capture"),
             keys.Key("KEY3", pins[2], hold="reboot"),
             keys.Key("KEY4", pins[3], hold="shutdown")],
            apply=self.context.apply, logger=self.log,
            options=lambda: self.context.options)
        self._keys.start()

    def _toggle_info(self):
        """KEY1: show the info screen for INFO_SECONDS, or close it."""
        now = time.monotonic()
        with self._lock:
            self._screen_until = 0.0 if self._screen_until > now else now + INFO_SECONDS
        self._keys.clear_prompt()

    def _setup(self):
        """Initialise state without the panel or the worker thread, so the
        tests can exercise the mood ladder and the frame compositor directly."""
        self.log = self.context.logger
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._epd = None
        self._epd_tried = False
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
        self._keys = None               # the HAT keys (hat_keys library), if on
        self._keys_note = ""            # why the keys are off, if pins was bad
        self._screen_until = 0.0        # the info screen (KEY1) shows until
        self._buttons_seq = None        # last key-state seq drawn

    def on_unload(self):
        # Blank the panel on the way down so a shutdown - the power-off button
        # (and its confirm) or the UPS-HAT low-battery auto-off - never leaves
        # the last face frozen on the e-paper, which keeps its image with no
        # power. Both paths end in `systemctl poweroff`, which stops the daemon
        # and runs this unload before the device goes down.
        #
        # Stop and JOIN the render worker first: an in-flight panel refresh on
        # the worker thread must not race our Clear() on the SPI bus, nor push a
        # fresh frame right after it and redraw the face. With no panel there is
        # nothing to blank (the plugin is optional and HAT-less-safe).
        if self._keys is not None:
            self._keys.stop()
        self._stop.set()
        self._ready.set()
        worker = getattr(self, "_worker", None)
        if worker is not None and worker.is_alive():
            worker.join(timeout=self._FORCED_TIMEOUT)
        epd = self._epd
        if epd is not None:
            try:
                epd.Clear()
                epd.sleep()
            except Exception as exc:  # noqa: BLE001 - best effort on the way out
                self.log.debug("%s: shutdown render failed: %s", NAME, exc)
        face.release_panel(self)

    # -- web dashboard data (the shared model's other view) --------------
    def on_ui_setup(self, ui):
        ui.add_element("face", "(◕‿‿◕)", label="Face")
        ui.add_element("mood", "awake", label="Mood")
        ui.add_element("voice", "", label="Says")
        ui.add_element("mobility", "gps?", label="Motion")
        ui.add_element("panel", "checking", label="Display")
        if self._keys is not None or self._keys_note:
            last = self._keys.last if self._keys is not None else self._keys_note
            ui.add_element("keys", last, label="Last key press")

    def on_ui_update(self, ui, state):
        # Fast path only: stash the state and update the web card from the last
        # mood the render worker computed. The worker is the single owner of the
        # mood trackers (reward EMA, look-toggle, mobility), so the dashboard
        # shows exactly what the panel shows and nothing races the slow refresh.
        with self._lock:
            self._state = state
            mood = self._mood
            voice = self._voice
        ui.set("face", self._face_glyph(mood) if mood else face.FACES["awake"])
        ui.set("mood", mood or "awake")
        ui.set("voice", voice or "")
        ui.set("mobility", face.MOBILITY_LABEL.get(self._motion.state, "gps?"))
        ui.set("panel", ("used by %s" % self._standing_down) if self._standing_down
               else "not detected" if self._absent else "detected")
        if self._keys is not None:
            self._keys.set_capturing(bool(getattr(state, "capture_active", False)))
            ui.set("keys", self._keys.last)

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
                self.log.debug("%s: render tick failed: %s", NAME, exc)
            # Sleep until the next render, but wake within a second when a HAT
            # key is pressed (the key state's seq changes), so the info
            # screen or a prompt appears at once.
            deadline = time.monotonic() + self._refresh_seconds()
            while not self._stop.is_set() and time.monotonic() < deadline:
                if self._buttons_state().get("seq") != self._buttons_seq:
                    break
                self._stop.wait(timeout=1)

    def _note(self, text):
        """Set the plugin's health note (the Plugins page and ``kestrel plugins
        info``)."""
        self.context.health_note = text

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

    def _push_panel(self, image, changed):
        """Push to the physical panel when one is present and the frame changed.
        A change-gated full refresh keeps e-ink wear and flashing down; partial
        refresh is a Gate-5 hardware-tuned refinement."""
        epd = self._ensure_panel()
        if epd is None or not changed:
            return
        try:
            epd.display(epd.getbuffer(image))
        except Exception as exc:  # noqa: BLE001
            self._fault = True
            self.log.debug("%s: panel display failed: %s", NAME, exc)

    # Driver module per 2.7" HAT revision. "auto" tries them in this order,
    # bounding each bring-up so a wrong revision falls back instead of hanging.
    _VARIANTS = {"v2": "epd2in7_V2", "v1": "epd2in7"}
    _AUTO_ORDER = ("epd2in7_V2", "epd2in7")
    # A correct panel's init+Clear finishes in a few seconds. The V1 and V2
    # drivers poll the BUSY line with opposite polarity, so the wrong driver's
    # busy-wait never returns; bound a probe so auto gives up and tries the other
    # driver. A forced v1/v2 gets a longer leash (nothing to fall back to).
    _PROBE_TIMEOUT = 8.0
    _FORCED_TIMEOUT = 20.0

    def _ensure_panel(self):
        """Lazily bring the panel up, once. With variant=auto, try each driver
        in turn with a bounded busy-wait and keep the first that initialises; a
        forced v1/v2 tries only that one. Returns the EPD handle or None (a
        HAT-less device / build host degrades cleanly to the web mirror)."""
        if self._epd is not None or self._epd_tried:
            return self._epd
        owner = face.panel_owner()
        if owner not in (None, NAME):
            self._stand_down(owner)
            return None
        self._epd_tried = True
        variant = str(self.context.options.get("variant", "auto") or "auto").lower()
        if variant in self._VARIANTS:
            attempts = ((self._VARIANTS[variant], self._FORCED_TIMEOUT),)
        else:
            attempts = tuple((m, self._PROBE_TIMEOUT) for m in self._AUTO_ORDER)
        failures = []
        for module, timeout in attempts:
            epd, err = self._try_panel(module, timeout)
            if epd is not None:
                self._epd = epd
                self._absent = False
                self.log.info("%s: %s panel ready", NAME, module)
                return self._epd
            failures.append(err)
        self._absent = True
        self._note("no e-Paper HAT detected")
        # Every attempt, not just the last: with variant=auto the V2 probe's
        # reason would otherwise be lost behind the V1 one.
        self.log.info("%s: no panel (%s); rendering to the web mirror only",
                      NAME, "; ".join(failures) or "no driver tried")
        return None

    def _try_panel(self, module, timeout):
        """Import one Waveshare driver and run init()+Clear(), bounding the
        driver's BUSY polling with a deadline so the wrong revision for this HAT
        raises instead of looping forever. Returns (epd, None) on success or
        (None, reason). On failure only SPI is closed and the shared GPIO pins
        are left open, so auto can try the other driver cleanly.

        A panel has no readback, so init()+Clear() completing is not by itself
        proof one is wired: the header's BUSY line is pulled low, and the V2
        busy-wait returns while it reads idle (0: idle, 1: busy), so a HAT-less
        device - or a different HAT such as a UPS HAT - sails through init()
        and would read as "detected". The real tell is that a present panel
        *drives* the BUSY line: V1 holds it at its idle level (reads 1) and V2
        asserts it (reads 1) while the controller works through the init/Clear
        busy-waits. So the probe also requires the BUSY line to be observed
        driven high; with it never driven, there is no panel."""
        try:
            # The drivers ship beside this plugin as helper files, epdconfig
            # first: each driver does ``from . import epdconfig``.
            epdconfig = load_helper(__file__, "_%s_epdconfig.py" % NAME)
            drv = load_helper(__file__, "_%s_%s.py" % (NAME, module))
        except Exception as exc:  # noqa: BLE001 - driver/SPI/GPIO absent off-Pi
            return None, "%s unavailable (%s)" % (module, exc)
        epd = drv.EPD()
        orig_read = epdconfig.digital_read
        deadline = time.monotonic() + timeout
        saw_busy_driven = False

        def _guarded_read(pin):
            nonlocal saw_busy_driven
            if time.monotonic() > deadline:
                raise TimeoutError(
                    "%s busy-wait exceeded %gs (wrong panel revision?)"
                    % (module, timeout))
            value = orig_read(pin)
            # BUSY is the only line the drivers read. A 1 means a panel is
            # driving it; a HAT-less header sits at the pull-down and reads 0.
            if value == 1:
                saw_busy_driven = True
            return value

        # ReadBusy() is the only path that polls the BUSY line; guard it so a
        # mismatched driver's infinite busy loop becomes a timeout we can catch,
        # and so we can tell a driven line (a real panel) from a pulled-low one.
        epdconfig.digital_read = _guarded_read
        try:
            epd.init()
            epd.Clear()
        except Exception as exc:  # noqa: BLE001 - timed out / no panel: degrade
            try:
                epdconfig.module_exit()   # close SPI; keep GPIO for next attempt
            except Exception:             # noqa: BLE001
                pass
            if isinstance(exc, TimeoutError) and not saw_busy_driven:
                # The driver waited for a busy signal that never came: nothing
                # drives the BUSY line, so no panel is wired - not the wrong
                # revision (with a panel of the other revision BUSY reads 1).
                return None, "%s: BUSY line never driven (no panel wired)" % module
            return None, "%s did not initialise (%s)" % (module, exc)
        finally:
            epdconfig.digital_read = orig_read
        if not saw_busy_driven:
            # init()+Clear() completed but the BUSY line never asserted: no panel
            # is wired (a pulled-low BUSY makes the V2 busy-wait return at once),
            # so a UPS-HAT-only or bare device is not mistaken for a panel.
            try:
                epdconfig.module_exit()
            except Exception:             # noqa: BLE001
                pass
            return None, "%s: BUSY line never driven (no panel wired)" % module
        return epd, None

    def _compose_image(self, state, mood, voice=None):
        """Compose the 1-bit frame for a mood the caller already decided.

        Layout: a top icon strip (BT PAN + Wi-Fi link glyphs, a GPS satellite
        with signal bars when a receiver is present, a person/car motion glyph
        while moving, and a battery symbol with its percent on the right); the
        mascot face offset to the left (as in classic pwnagotchi; the face is the
        mood readout) with its voice line - a short mood-matched phrase - beside
        it; a compact symbol-led status row (a chip glyph + the agent's mode, an
        antenna glyph + capture state); an info row (``UP`` uptime and ``CH`` the
        channel plan while capturing); an address row (each management link's
        IPv4 behind its BT/Wi-Fi glyph, when ``show_ip`` is on); a bottom
        counter row (``PWND`` captured
        handshakes with this session's count in parens, then ``AP`` distinct
        radios and ``NET`` distinct networks, both lifetime); and the Kestrel
        brand mark once in the
        lower-right - stooping when moving, hovering when parked. Also used for the
        web mirror, so it runs with no panel present."""
        from PIL import Image, ImageDraw
        opts = self.context.options
        buttons = self._buttons_state()
        self._buttons_seq = buttons.get("seq")
        if buttons.get("screen") == "info":
            return self._rotated(self._info_image(state))
        prompt = buttons.get("prompt")
        img = Image.new("1", (PANEL_W, PANEL_H), 255)   # white
        draw = ImageDraw.Draw(img)
        tiny = face.font(11)
        small = face.font(13)
        big = face.font(30)

        # One fresh address lookup drives both the link glyphs in the top strip
        # and the address row, so they always agree. The daemon's
        # UIState.paths means the same thing (an IPv4 on the link) but is only
        # refreshed on its connectivity poll, so at boot it can still say "no
        # link" while the addresses are already up.
        addresses = self._addresses()
        linked = {iface for iface, _ip in addresses}
        battery = (state.battery if state is not None else None) or {}
        pct = face.as_float(battery.get("percent"))
        position = state.position if state is not None else None
        gps_present = bool(self.context and self.context.services
                           and self.context.services.has("position"))

        # top icon strip: link + GPS symbols on the left, battery on the right.
        face.draw_bt(draw, 6, 4, 10, 15, "bnep0" in linked)
        face.draw_wifi(draw, 28, 6, 18, 13, "wlan0" in linked)
        if gps_present:
            # position is the fix dict (None until locked); a solid satellite
            # means locked, an outlined one means searching. Signal bars read
            # the strongest satellite's C/N0 once there is a fix.
            locked = position is not None and int(face.as_float(position.get("fix")) or 0) >= 2
            cn0 = face.as_float(position.get("cn0")) if position else None
            face.draw_sat(draw, 54, 4, 16, 15, locked)
            face.draw_sigbars(draw, 76, 6, 11, 13, face.signal_level(cn0, locked))
            # a motion glyph only while moving: a walking person or a moving car.
            # Parked needs no glyph here - the Kestrel's hover pose shows stopped,
            # and dropping it keeps the top strip light when stationary.
            if self._motion.state in face.MOVING:
                face.draw_motion(draw, 96, 4, 20, 15, self._motion.state)
        if pct is not None:
            face.draw_battery(draw, 230, 6, 24, 12, pct)
            face.text_right(draw, 225, 5, "%d%%" % int(pct), tiny)
        draw.line([(4, 24), (PANEL_W - 4, 24)], fill=0, width=1)

        # the mascot face, offset to the left (the face is the mood readout), with
        # its voice line - a short mood-matched phrase - wrapped in the band beside
        # it, so the mascot "speaks" like classic pwnagotchi without claiming any
        # state the face is not already in.
        if opts.get("show_mascot", True):
            glyph = self._face_glyph(mood)
            draw.text((10, 32), glyph, font=big, fill=0)
            vx = min(max(10 + face.text_w(draw, glyph, big) + 10, 150), 170)
            # The voice slot, in order: a HAT-key prompt (it answers a press
            # just made, e.g. "Press KEY4 again to shut down"), then just after
            # start-up the start of the HTTPS fingerprint (for checking the
            # browser's certificate warning), then the mood's voice.
            if prompt:
                lines = face.wrap_text(draw, prompt, tiny, PANEL_W - 6 - vx)[:3]
            else:
                lines = self._fingerprint_lines()
            if not lines and voice:
                lines = face.wrap_text(draw, voice, tiny, PANEL_W - 6 - vx)[:3]
            vy = 34
            for line in lines:
                draw.text((vx, vy), line, font=tiny, fill=0)
                vy += 13

        # compact, symbol-led status on one line: a chip glyph + the agent mode,
        # then an antenna glyph + capture state. Mood is the face itself; motion
        # is the top glyph + the Kestrel's pose.
        agent = state.agent if state is not None else None
        capturing = bool(state is not None and state.capture_active)
        agent_txt = face.agent_label(agent)
        cap_txt = "capturing" if capturing else "idle"
        sy = 82
        face.draw_chip(draw, 12, sy + 1, 12, 11)
        draw.text((30, sy), agent_txt, font=small, fill=0)
        ax = 30 + face.text_w(draw, agent_txt, small) + 12
        face.draw_antenna(draw, ax, sy, 11, 13, capturing)
        draw.text((ax + 16, sy), cap_txt, font=small, fill=0)

        # info row: uptime (UP) and, while capturing, the channel plan (CH). Text
        # labels, as in classic pwnagotchi. Both are omitted when unknown.
        uptime_txt = face.fmt_uptime(state.uptime if state is not None else None)
        channel = state.channel if state is not None else None
        iy = 106
        ix = 12
        if uptime_txt:
            seg = "UP " + uptime_txt
            draw.text((ix, iy), seg, font=small, fill=0)
            ix += face.text_w(draw, seg, small) + 18
        if channel:
            seg = "CH " + str(channel)
            draw.text((ix, iy), seg, font=small, fill=0)
            ix += face.text_w(draw, seg, small) + 18
        # NEAR: distinct radios the collector saw in range recently, with a
        # density meter. Only present while capturing and when the reading is
        # usable (the daemon reports available False when stale), like CH.
        env = state.env if state is not None else None
        if env and env.get("available"):
            level = int(env.get("level") or 0)
            face.draw_density(draw, ix, iy + 2, 18, 11, level)
            draw.text((ix + 22, iy), "NEAR %d" % int(env.get("radios_recent") or 0),
                      font=small, fill=0)

        # address row: each management link's IPv4 behind the same BT/Wi-Fi
        # glyph as the top strip, so a headless device can be found without the
        # router's client list. A link with no address is left out, and an
        # address that won't fit is dropped rather than drawn off the edge.
        if opts.get("show_ip", True):
            ay = 125
            x = 12
            for iface, ip in addresses:
                gap = 12 if iface == "bnep0" else 18     # glyph width + spacing
                need = gap + face.text_w(draw, ip, tiny)
                if x + need > PANEL_W - 10:
                    break
                if iface == "bnep0":
                    face.draw_bt(draw, x, ay + 1, 7, 11, True)
                else:
                    face.draw_wifi(draw, x + 3, ay + 2, 10, 9, True)
                draw.text((x + gap, ay), ip, font=tiny, fill=0)
                x += need + 14

        # bottom counter row, on the Kestrel's baseline: the plain-English trio
        # "N handshakes from M access points across K networks". Handshakes
        # (PWND, a key glyph) carry this session's count in parens, e.g.
        # "240 (30)" - the one session delta worth watching; AP (distinct
        # radios/BSSIDs) and NET (distinct names/ESSIDs) are lifetime only, so
        # all three fit one row left of the brand mark. Segments advance by
        # measured width and stop before the mark so a big count can't overlap it.
        by = 146
        right_limit = PANEL_W - 40   # keep clear of the lower-right brand glyph
        x = 14
        total = state.capture_total if state is not None else None
        if total is not None:
            sess = (state.capture_session
                    if state is not None and state.capture_session is not None else 0)
            face.draw_key(draw, x, by + 2, 13, 11)
            x += 18
            seg = "%d (%d)" % (int(total), int(sess))
            draw.text((x, by), seg, font=small, fill=0)
            x += face.text_w(draw, seg, small) + 16
        for value, label in ((state.aps_total if state is not None else None, "AP"),
                             (state.networks_total if state is not None else None, "NET")):
            if value is None:
                continue
            seg = "%s %d" % (label, int(value))
            if x + face.text_w(draw, seg, small) > right_limit:
                break
            draw.text((x, by), seg, font=small, fill=0)
            x += face.text_w(draw, seg, small) + 16

        # the Kestrel brand mark, once, lower-right corner; its pose doubles as
        # the motion readout - stooping (diving) when moving, hovering when not.
        if opts.get("show_brand_glyph", True):
            face.draw_kestrel(draw, PANEL_W - 36, PANEL_H - 36, 28, self._motion.state in face.MOVING)

        return self._rotated(img)

    def _rotated(self, img):
        rotation = int(self.context.options.get("rotation", 0) or 0)
        return img.rotate(rotation, expand=True) if rotation else img

    def _buttons_state(self):
        """The HAT keys' ``{"seq", "screen", "prompt"}``, or ``{}`` when off:
        the library's prompt and press count, and this plugin's info screen."""
        hat = getattr(self, "_keys", None)
        if hat is None:
            return {}
        state = hat.state()
        state["screen"] = "info" if time.monotonic() < self._screen_until else None
        return state

    def _info_image(self, state):
        """The info screen a short KEY1 press asks for: version, addresses, the
        dashboard's HTTPS fingerprint (when the dashboard has one), uptime and
        free disk space."""
        from PIL import Image, ImageDraw
        img = Image.new("1", (PANEL_W, PANEL_H), 255)
        draw = ImageDraw.Draw(img)
        small, title = face.font(13), face.font(18)
        draw.text((10, 6), "Kestrel", font=title, fill=0)
        face.text_right(draw, PANEL_W - 10, 10, _installed_version(), small)
        draw.line([(4, 30), (PANEL_W - 4, 30)], fill=0, width=1)
        y = 38
        addresses = self._addresses()
        for iface, ip in addresses:
            if iface == "bnep0":
                face.draw_bt(draw, 12, y + 1, 7, 11, True)
            else:
                face.draw_wifi(draw, 15, y + 2, 10, 9, True)
            draw.text((32, y), ip, font=small, fill=0)
            y += 18
        if not addresses:
            draw.text((12, y), "No network link yet", font=small, fill=0)
            y += 18
        fp = _dashboard_fingerprint()
        if fp:
            draw.text((12, y), "HTTPS " + ":".join(fp.split(":")[:8]), font=small, fill=0)
            y += 18
        uptime = face.fmt_uptime(state.uptime if state is not None else None)
        if uptime:
            draw.text((12, y), "UP " + uptime, font=small, fill=0)
        free = _free_disk_text()
        if free:
            face.text_right(draw, PANEL_W - 12, y, free, small)
        draw.line([(4, PANEL_H - 24), (PANEL_W - 4, PANEL_H - 24)], fill=0, width=1)
        draw.text((10, PANEL_H - 20), "KEY1 to close", font=face.font(11), fill=0)
        return img

    def _fingerprint_lines(self):
        """``["HTTPS key", "AB:CD:EF:12", "34:56:78:9A"]`` (the start of the
        dashboard certificate's SHA-256 fingerprint) during the first
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
        return ["HTTPS key", ":".join(parts[:4]), ":".join(parts[4:8])]

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
            return self._mirror_page()
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
        etag = '"%s-%d"' % (NAME, seq)
        headers = {"Content-Type": "image/png", "Cache-Control": "no-store",
                   "ETag": etag}
        if request is not None and request.headers.get("If-None-Match") == etag:
            return ("", 304, headers)
        return (png, 200, headers)

    def _mirror_page(self):
        esc = self.context.esc
        every = self._refresh_seconds()
        if self._standing_down:
            note = (" (%s drives the panel - this is the frame this plugin would "
                    "show)" % self._standing_down)
        else:
            note = (" (no HAT attached - this is the frame it would show)"
                    if self._absent else "")
        body = (
            "<h1>E-ink display</h1>"
            '<p class="muted">A live mirror of what is on the panel' + esc(note)
            + ". It reloads every " + str(every)
            + " s, in step with the panel, from the same frame.</p>"
            '<p><img src="display.png" alt="e-ink display" '
            'style="width:100%;max-width:528px;image-rendering:pixelated;'
            'border:1px solid #888;background:#fff"></p>'
            "<script>setTimeout(function(){location.reload();}, "
            + str(every * 1000) + ");</script>")
        return self.context.page("e-ink display", body)


# --- the HAT's four keys ----------------------------------------------------
# The 2.7" HAT has four push-buttons beside the panel. They are part of the same
# HAT, so this plugin owns them (option ``buttons``), through the shared
# hat_keys plugin library, which reads them, turns presses into gestures and
# keeps the rules for the built-in actions (see _lib_hat_keys.py):
#
#   Key   Short press             Long press (``hold_seconds``, 3 s)
#   KEY1  info screen for 30 s    -
#   KEY2  (a hint)                stop capture at once; start it after a second press
#   KEY3  (a hint)                reboot, after a second press
#   KEY4  (a hint)                shut down, after a second press
#
# A second press of the same key within 5 s confirms; any other key cancels.
# Starting capture never sends the authorized-use acknowledgment: the root
# helper starts it only if the operator has already given it, and the panel
# otherwise points to the dashboard (a rule the library enforces).
INFO_SECONDS = 30
DEFAULT_PINS = "5,6,13,19"  # BCM pins of KEY1-KEY4 on the Waveshare 2.7" HAT


# --- small helpers ---------------------------------------------------------
VERSION_FILE = "/etc/kestrel/version"


def _installed_version():
    """The installed version stamp (``master`` on a development build)."""
    try:
        with open(VERSION_FILE) as handle:
            return handle.readline().strip()[:24] or "unknown"
    except OSError:
        return "unknown"


def _dashboard_fingerprint():
    """The dashboard's HTTPS certificate fingerprint, or None when there is no
    certificate (HTTPS off, or the daemon hasn't made one yet)."""
    try:
        return webtls.fingerprint()
    except Exception:  # noqa: BLE001
        return None


def _free_disk_text(path="/"):
    try:
        st = os.statvfs(path)
    except OSError:
        return ""
    free = st.f_bavail * st.f_frsize
    return "%.1f GB free" % (free / 1e9)
