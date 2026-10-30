"""e-ink "tamagotchi" face for the Waveshare 2.13" e-Paper HAT.

Renders the Kestrel face - the classic pwnagotchi-style emoticons, driven by
live device and learning-agent state - to the 2.13" e-Paper HAT (250x122,
black and white; panel revisions V2, V3 and V4, chosen with ``variant``). It
mirrors the ``eink`` plugin for the 2.7" HAT: the same mood ladder, voice,
mirror and dashboard card, with a lean layout for the smaller panel (see
:meth:`EinkWs213._compose_image`) and no keys (this HAT has none).

The plugin is standalone: everything it needs is in this file, except
Waveshare's own driver, which ships beside it unmodified as helper files
(``_eink-ws213_epdconfig.py``, ``_eink-ws213_epd2in13_V4.py``, ``_V3``, ``_V2``;
see ``docs/developer/vendored-code.md``) loaded with :func:`kestrel_pi.load_helper`.
It shares no code with ``eink``; a fix to the face is made to both copies.

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
:func:`decide_mood`.
"""

import calendar
import fcntl
import io
import math
import re
import socket
import struct
import threading
import time
from dataclasses import dataclass

from kestrel_pi import Plugin, load_helper, webtls
from kestrel_pi.connectivity import MANAGEMENT_INTERFACES

# Panel geometry: the 2.13" HAT is 250x122 landscape (122x250 native portrait;
# the Waveshare getbuffer rotates a landscape image to fit).
PANEL_W, PANEL_H = 250, 122

# --- faces -----------------------------------------------------------------
# The 26 classic pwnagotchi faces (kept for their charm), plus a few Phase 5
# additions. All overridable from config via [plugins.eink-ws213.faces] (key -> str).
FACES = {
    "look_r": "( ⚆_⚆)", "look_l": "(☉_☉ )",
    "look_r_happy": "( ◕‿◕)", "look_l_happy": "(◕‿◕ )",
    "sleep": "(⇀‿‿↼)", "sleep2": "(≖‿‿≖)", "awake": "(◕‿‿◕)",
    "bored": "(-__-)", "intense": "(°▃▃°)", "cool": "(⌐■_■)",
    "happy": "(•‿‿•)", "grateful": "(^‿‿^)", "excited": "(ᵔ◡◡ᵔ)",
    "motivated": "(☼‿‿☼)", "demotivated": "(≖__≖)", "smart": "(✜‿‿✜)",
    "lonely": "(ب__ب)", "sad": "(╥☁╥ )", "angry": "(-_-')",
    "friend": "(♥‿‿♥)", "broken": "(☓‿‿☓)", "debug": "(#__#)",
    "upload": "(1__0)", "upload1": "(1__1)", "upload2": "(0__1)",
    # Phase 5 additions (kept ASCII-leaning so they render without exotic glyphs):
    "tired": "(=__=)",         # low battery, honestly tired
    "levelup": "(^‿‿^)",       # a better model was just promoted
    "determined": "(>__<)",    # learned policy, trying, reward dipping
    "focused": "(•_•)",        # parked and productively camping a channel
    "searching": "(·_·)",      # GPS requested, no fix yet
}

# --- voice -----------------------------------------------------------------
# A cute phrase for each mood (the mascot "speaks", like classic pwnagotchi).
# One per mood is picked per render and rotated over time, so the face has a
# voice without ever claiming a state it is not in - the key is the decided
# mood, so the words always match the face. Operator-overridable via
# [plugins.eink-ws213.voices] (mood -> a phrase or a list of phrases). Kept short so
# they wrap into the narrow band beside the face.
_VOICES = {
    "look_r": ("who's out there?", "scanning around", "ooh, what's this?"),
    "look_l": ("anything nearby?", "having a look", "hmm, over there"),
    "look_r_happy": ("hello there!", "found a friend!", "hi hi!"),
    "look_l_happy": ("well hello!", "nice to see you", "hey friend"),
    "sleep": ("zzz...", "dreaming of handshakes", "do not disturb"),
    "sleep2": ("so sleepy", "five more minutes", "snooze..."),
    "awake": ("ready to hunt", "ears up!", "let's find wifi"),
    "bored": ("so quiet out here", "anything happening?", "...meh"),
    "intense": ("c'mon, handshake!", "almost got it", "locked on"),
    "cool": ("like a boss", "watch this", "too easy"),
    "happy": ("good vibes", "having fun!", "this is nice"),
    "grateful": ("thanks, stranger", "much appreciated", "you're too kind"),
    "excited": ("here we go!", "so many signals!", "oh oh oh!"),
    "motivated": ("on a roll!", "learning fast", "getting smarter"),
    "demotivated": ("tough crowd", "need a new plan", "this is hard"),
    "smart": ("big brain mode", "thinking...", "I've got a plan"),
    "lonely": ("anyone there?", "feeling alone", "come say hi"),
    "sad": ("no luck today", "rough patch", "sigh..."),
    "angry": ("quit hiding!", "grr, open up", "not cool"),
    "friend": ("a fellow traveler!", "let's team up", "nice to meet you"),
    "broken": ("ow, something broke", "halp!", "I don't feel good"),
    "debug": ("poking at myself", "debug mode", "what was that?"),
    "upload": ("sending it off", "uploading...", "off it goes"),
    "upload1": ("uploading...", "almost sent", "beaming up"),
    "upload2": ("off it goes", "sent!", "done uploading"),
    "tired": ("low on juice", "need a nap", "running on fumes"),
    "levelup": ("level up!", "new model!", "I leveled up"),
    "determined": ("won't give up", "try again!", "push through"),
    "focused": ("camping this channel", "patience...", "steady now"),
    "searching": ("finding the sky", "where are the sats?", "looking up..."),
}


def _voice_phrases(mood, overrides):
    """The phrase list for a mood: an operator override (a str or list under
    [plugins.eink-ws213.voices]) if present, else the shipped set, else the base."""
    if isinstance(overrides, dict) and mood in overrides:
        v = overrides[mood]
        phrases = list(v) if isinstance(v, (list, tuple)) else [v]
        phrases = [str(p) for p in phrases if str(p)]
        if phrases:
            return phrases
    return list(_VOICES.get(mood) or _VOICES["awake"])


def _fmt_uptime(seconds):
    """``H:MM`` (days fold into hours) for the uptime readout. Minute
    granularity on purpose: a ticking seconds field would change the composed
    frame every render and defeat the change-gated redraw (the panel would do a
    full refresh every cycle), which is what lets a faster refresh_seconds stay
    cheap on e-ink."""
    if seconds is None or seconds < 0:
        return None
    total = int(seconds)
    h, m = divmod(total // 60, 60)
    return "%d:%02d" % (h, m)


def _wrap_text(draw, text, font, max_w):
    """Greedy word-wrap ``text`` to lines no wider than ``max_w`` px. A single
    word longer than the width is left on its own line (never split)."""
    words = str(text).split()
    lines, cur = [], ""
    for word in words:
        trial = word if not cur else cur + " " + word
        if _text_w(draw, trial, font) <= max_w or not cur:
            cur = trial
        else:
            lines.append(cur)
            cur = word
    if cur:
        lines.append(cur)
    return lines


# --- mobility --------------------------------------------------------------
# Speed thresholds (m/s), estimated from successive GPS fixes, that split motion
# into parked / walking / driving, with a deadband and a sustain count so the
# readout does not flicker at a stop light. Walking tops out around 11 km/h
# (3 m/s), so 3.5 m/s (~12.6 km/h) is a safe car threshold; the gap between
# parked and walking (0.6-0.9) is a deadband that holds the current state to
# absorb GPS jitter on a stationary device.
_PARK_MS = 0.6       # at/below -> parked
_WALK_MS = 0.9       # at/above (and below drive) -> walking
_DRIVE_MS = 3.5      # at/above -> driving (vehicle)
_MOBILITY_SUSTAIN = 2

# --- Kestrel brand mark (drawn from its SVG path, no binary asset) ---------
# The two poses from assets/branding/ (viewBox 0 0 64 64): hover
# (holding still = parked) and stoop (diving = moving). Solid silhouettes, so
# a filled polygon renders them perfectly in 1-bit.
_KESTREL_HOVER = ("M32 7 C29.6 10 29 15 31 18 C23 12 12 11 5 18 C11 25 22 28 30 25 "
                  "L29 31 L30 37 L27 52 L32 47 L37 52 L34 37 L35 31 L34 25 "
                  "C42 28 53 25 59 18 C52 11 41 12 33 18 C35 15 34.4 10 32 7 Z")
_KESTREL_STOOP = ("M10 8 C20 12 30 19 39 29 C45 23 53 21 59 24 C54 29 47 32 41 32 "
                  "C47 39 51 47 52 56 C44 51 37 44 33 36 C28 43 21 48 13 50 "
                  "C17 43 23 37 28 33 C20 27 14 18 10 8 Z")
_PATH_TOKEN = re.compile(r"[MLCZ]|-?\d*\.?\d+")


def _cubic(p0, p1, p2, p3, u):
    v = 1.0 - u
    x = (v * v * v * p0[0] + 3 * v * v * u * p1[0]
         + 3 * v * u * u * p2[0] + u * u * u * p3[0])
    y = (v * v * v * p0[1] + 3 * v * v * u * p1[1]
         + 3 * v * u * u * p2[1] + u * u * u * p3[1])
    return (x, y)


def _flatten_svg_path(d, steps=8):
    """Flatten an absolute-coordinate SVG path (M/L/C/Z) into a point list for
    a filled polygon. Only the commands the Kestrel marks use are handled."""
    toks = _PATH_TOKEN.findall(d)
    pts, i, cur, start, cmd = [], 0, (0.0, 0.0), (0.0, 0.0), None
    while i < len(toks):
        t = toks[i]
        if t in "MLCZ":
            cmd, i = t, i + 1
            if cmd == "Z":
                pts.append(start)
                break
        if cmd == "M":
            cur = (float(toks[i]), float(toks[i + 1]))
            start, i = cur, i + 2
            pts.append(cur)
            cmd = "L"
        elif cmd == "L":
            cur = (float(toks[i]), float(toks[i + 1]))
            i += 2
            pts.append(cur)
        elif cmd == "C":
            p1 = (float(toks[i]), float(toks[i + 1]))
            p2 = (float(toks[i + 2]), float(toks[i + 3]))
            p3 = (float(toks[i + 4]), float(toks[i + 5]))
            i += 6
            for s in range(1, steps + 1):
                pts.append(_cubic(cur, p1, p2, p3, s / steps))
            cur = p3
        else:
            break
    return pts


def _scaled_polygon(points, box, origin):
    """Scale 64x64 viewBox points into a ``box``-px square at ``origin``."""
    k = box / 64.0
    ox, oy = origin
    return [(ox + x * k, oy + y * k) for (x, y) in points]


# --- the mood engine (pure, testable) --------------------------------------
@dataclass
class MoodInputs:
    """Everything :func:`decide_mood` needs, so the ladder is a pure function
    testable with no hardware. The plugin builds this from the UIState snapshot
    and its own tracked counters each render."""

    fault: bool = False
    battery_low: bool = False
    just_promoted: bool = False
    fresh_handshake: bool = False
    capture_active: bool = False
    learned_active: bool = False
    posture: str = ""
    transmit: bool = False
    reward_trend: int = 0          # -1 falling, 0 flat, +1 rising (smoothed)
    mobility: str = "unknown"      # parked | walking | driving | unknown
    idle_epochs: int = 0
    bored_after: int = 15
    sad_after: int = 25
    look_toggle: bool = False


def decide_mood(inp):
    """Pick a face key from device/agent state, by an explicit priority ladder
    (highest matching tier wins, so moods never fight). The face never claims a
    state the device is not in. Returns a key into :data:`FACES`."""
    # 1. fault
    if inp.fault:
        return "broken"
    # 2. power-critical
    if inp.battery_low:
        return "tired"
    # 3. milestones (brief, set transiently by the plugin)
    if inp.just_promoted:
        return "levelup"
    if inp.fresh_handshake:
        return "excited"
    # (4. uploading tier: no live signal yet - a future UIState field.)
    # 5. agent-posture working moods, only while actually capturing
    if inp.capture_active:
        if inp.learned_active:
            # Honest: "smart" only under a real learned policy. Reward trend
            # (smoothed, with a deadband) gives hysteresis instead of the
            # classic per-epoch motivated/demotivated flicker.
            if inp.reward_trend > 0:
                return "motivated"
            if inp.reward_trend < 0:
                return "determined"
            return "smart"
        # scripted baseline (or no policy): plainer, posture-driven faces.
        if inp.mobility in ("walking", "driving"):
            return "excited"
        if inp.posture == "autohunt":
            return "cool"
        if inp.posture in ("pin-channel", "lock-subset") and inp.transmit:
            return "intense"
        if inp.posture == "sweep-band":
            return "look_l" if inp.look_toggle else "look_r"
        if inp.mobility == "parked":
            return "focused"
        return "awake"
    # 6. capture disabled: the device sleeps. The tiers above still win, so a
    #    fault, low power, a promotion or a fresh handshake takes over and a real
    #    event is never hidden behind a sleeping face; but with capture off there
    #    is nothing to hunt, so the face rests rather than looking busy or bored.
    #    It dozes deeper the longer it has been idle.
    if not inp.capture_active:
        if inp.idle_epochs >= inp.sad_after:
            return "sleep2"
        return "sleep"
    # 7. base
    return "awake"


_MOBILITY_LABEL = {"parked": "parked", "walking": "walking", "driving": "driving",
                   "unknown": "gps?"}
# the motion states that count as "moving" (for the Kestrel's stoop pose)
_MOVING = ("walking", "driving")


def _haversine_m(a, b):
    """Great-circle distance in metres between two (lat, lon) pairs."""
    r = 6371000.0
    lat1, lon1, lat2, lon2 = (math.radians(a[0]), math.radians(a[1]),
                              math.radians(b[0]), math.radians(b[1]))
    dlat, dlon = lat2 - lat1, lon2 - lon1
    h = (math.sin(dlat / 2) ** 2
         + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2)
    return 2 * r * math.asin(min(1.0, math.sqrt(h)))


def _classify_speed(speed):
    """Map a speed (m/s) to parked / walking / driving, or None in the
    parked-to-walking deadband (hold the current state there)."""
    if speed <= _PARK_MS:
        return "parked"
    if speed >= _DRIVE_MS:
        return "driving"
    if speed >= _WALK_MS:
        return "walking"
    return None


def _fix_seconds(fix):
    """When a GPS fix was taken, in epoch seconds: the provider's ``ts`` if it
    gives one, else its ``fix_time`` (UTC, whole seconds, as the ``gps`` plugin
    reports it), else None."""
    ts = _as_float(fix.get("ts"))
    if ts is not None:
        return ts
    stamp = fix.get("fix_time")
    if isinstance(stamp, str):
        try:
            return float(calendar.timegm(time.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ")))
        except ValueError:
            pass
    return None


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
        self._worker = threading.Thread(
            target=self._run, name="eink-ws213-face", daemon=True)
        self._worker.start()

    def _setup(self):
        """Initialise state without the panel or the worker thread, so the
        tests can exercise the mood ladder and the frame compositor directly."""
        self.log = self.context.logger
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._panel = None
        self._panel_tried = False
        self._absent = True             # no panel until a probe succeeds
        self._fault = False
        self._frame_png = None          # last composed frame (PNG bytes)
        self._frame_seq = 0             # increments on every frame change
        self._state = None              # last UIState seen
        self._mood = None               # last mood the worker computed
        self._voice = ""                # last phrase the worker picked
        self._voice_rot = 0             # advances on each mood change (rotates
        #                                 the phrase without a per-tick redraw)
        self._look_toggle = False
        self._started = time.monotonic()
        self._fp = None                 # dashboard certificate fingerprint
        self._fp_checked = None         # monotonic time of the last lookup
        self._partials = 0              # quick redraws since the last full one
        self._last_full = None          # monotonic time of the last full refresh
        # mood trackers
        self._reward_ema = None
        self._promo_marker = None
        self._promo_until = 0.0
        self._hs_until = 0.0
        self._last_activity_epoch = None
        # mobility hysteresis
        self._prev_fix = None           # (ts, lat, lon)
        self._mobility = "unknown"
        self._mobility_counts = {}      # candidate state -> consecutive ticks

    def on_unload(self):
        # Blank the panel on the way down so a shutdown (the power-off button
        # or the UPS-HAT low-battery auto-off) never leaves the last face frozen
        # on the e-paper, which keeps its image with no power. Stop and JOIN the
        # render worker first, so an in-flight redraw can't race the blank on
        # the SPI bus or redraw the face after it.
        self._stop.set()
        worker = getattr(self, "_worker", None)
        if worker is not None and worker.is_alive():
            worker.join(timeout=self._PROBE_TIMEOUT)
        panel = self._panel
        if panel is not None:
            try:
                panel.blank()
            except Exception as exc:  # noqa: BLE001 - best effort on the way out
                self.log.debug("eink-ws213: shutdown render failed: %s", exc)

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
        ui.set("face", self._face_glyph(mood) if mood else FACES["awake"])
        ui.set("mood", mood or "awake")
        ui.set("voice", voice or "")
        ui.set("mobility", _MOBILITY_LABEL.get(self._mobility, "gps?"))
        ui.set("panel", "not detected" if self._absent
               else "%s detected" % self._variant())

    def on_handshake(self, event):
        # A fresh handshake lights up an excited face briefly and counts as
        # activity (resets the idle ladder).
        now = time.monotonic()
        with self._lock:
            self._hs_until = now + 8.0
            st = self._state
            if st is not None:
                self._last_activity_epoch = st.epoch

    # -- mood ------------------------------------------------------------
    def _current_mood(self, state):
        """Build MoodInputs from the snapshot + trackers and run the ladder."""
        now = time.monotonic()
        opts = self.context.options
        self._look_toggle = not self._look_toggle

        agent = (state.agent if state is not None else None) or {}
        running = bool(agent.get("running"))
        policy = (agent.get("policy") or "baseline")
        learned = running and policy != "baseline"
        trend = self._reward_trend(agent.get("last_reward"))
        promoted = self._promotion(agent, now)

        battery = (state.battery if state is not None else None) or {}
        low_pct = int(opts.get("battery_low_percent", 10) or 10)
        battery_low = (battery.get("status") == "discharging"
                       and _as_float(battery.get("percent")) is not None
                       and _as_float(battery.get("percent")) <= low_pct)

        mobility = self._mobility_state(self._live_position(state))
        idle = self._idle_epochs(state)

        inp = MoodInputs(
            fault=self._fault,
            battery_low=bool(battery_low),
            just_promoted=promoted,
            fresh_handshake=(now < self._hs_until),
            capture_active=bool(state is not None and state.capture_active),
            learned_active=learned,
            posture=str(agent.get("posture") or ""),
            transmit=bool(agent.get("transmit")),
            reward_trend=trend,
            mobility=mobility,
            idle_epochs=idle,
            bored_after=int(opts.get("bored_epochs", 15) or 15),
            sad_after=int(opts.get("sad_epochs", 25) or 25),
            look_toggle=self._look_toggle,
        )
        return decide_mood(inp)

    def _reward_trend(self, reward):
        r = _as_float(reward)
        if r is None:
            return 0
        if self._reward_ema is None:
            self._reward_ema = r
            return 0
        prev = self._reward_ema
        self._reward_ema = 0.3 * r + 0.7 * prev
        delta = self._reward_ema - prev
        if delta > 0.02:
            return 1
        if delta < -0.02:
            return -1
        return 0

    def _promotion(self, agent, now):
        marker = (agent.get("model_source"), agent.get("last_trained"))
        if marker != (None, None) and self._promo_marker is not None \
                and marker != self._promo_marker:
            self._promo_until = now + 10.0
        if marker != (None, None):
            self._promo_marker = marker
        return now < self._promo_until

    def _idle_epochs(self, state):
        if state is None:
            return 0
        if self._last_activity_epoch is None:
            self._last_activity_epoch = state.epoch
            return 0
        return max(0, state.epoch - self._last_activity_epoch)

    def _mobility_state(self, fix):
        """Parked / walking / driving / unknown, estimated from successive GPS
        fixes (a provider speed is used if present, else derived from the fix
        delta), with a per-state sustain count so it does not flicker.

        Speed is measured between *fixes*, timed by when each was taken
        (``_fix_seconds``), not by when the face read it: the face redraws more
        often than a fix may change, and reading the same fix again is not a
        stop. A fix with no timestamp at all is timed by the clock, and only
        when its position changed."""
        if not fix or _as_float(fix.get("lat")) is None:
            self._prev_fix = None
            self._mobility_counts = {}
            self._mobility = "unknown"
            return "unknown"
        here = (fix["lat"], fix["lon"])
        prev = self._prev_fix
        ts = _fix_seconds(fix)
        if ts is None:
            if prev is not None and here == (prev[1], prev[2]):
                return self._mobility               # the same fix, read again
            ts = time.time()
        if prev is not None and ts <= prev[0]:
            return self._mobility                   # the same fix, read again
        self._prev_fix = (ts, here[0], here[1])
        if prev is None:
            return self._mobility
        dt = ts - prev[0]
        speed = _as_float(fix.get("speed"))
        if speed is None or speed < 0:
            speed = _haversine_m((prev[1], prev[2]), here) / dt
        candidate = _classify_speed(speed)
        if candidate is None:                       # deadband - hold state
            return self._mobility
        counts = self._mobility_counts
        counts[candidate] = counts.get(candidate, 0) + 1
        for other in list(counts):
            if other != candidate:
                counts[other] = 0
        if counts[candidate] >= _MOBILITY_SUSTAIN:
            self._mobility = candidate
        return self._mobility

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
        overrides = self.context.options.get("faces")
        if isinstance(overrides, dict) and mood in overrides:
            return str(overrides[mood])
        return FACES.get(mood, FACES["awake"])

    def _voice_for(self, mood, rot):
        """A phrase matching the decided mood. ``rot`` advances on each mood
        change, so the words vary over time while always matching the face and
        the frame stays stable within a mood (no e-ink redraw just to reword)."""
        phrases = _voice_phrases(mood or "awake", self.context.options.get("voices"))
        return phrases[rot % len(phrases)]

    # -- rendering + panel ----------------------------------------------
    def _run(self):
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
        png = _png_bytes(image)
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
        tiny = _font(10)
        small = _font(11)
        count = _font(15)
        big = _font(20)

        # One fresh address lookup drives both the link glyphs and the address
        # row, so they always agree.
        addresses = self._addresses()
        linked = {iface for iface, _ip in addresses}
        battery = (state.battery if state is not None else None) or {}
        pct = _as_float(battery.get("percent"))
        position = state.position if state is not None else None
        gps_present = bool(self.context and self.context.services
                           and self.context.services.has("position"))

        # top icon strip: link + GPS symbols on the left, battery on the right.
        _draw_bt(draw, 4, 2, 8, 12, "bnep0" in linked)
        _draw_wifi(draw, 19, 4, 14, 10, "wlan0" in linked)
        if gps_present:
            locked = position is not None and int(_as_float(position.get("fix")) or 0) >= 2
            cn0 = _as_float(position.get("cn0")) if position else None
            _draw_sat(draw, 42, 2, 13, 12, locked)
            _draw_sigbars(draw, 62, 4, 9, 10, _signal_level(cn0, locked))
            if self._mobility in _MOVING:
                # x 84: a car's trailing motion lines reach 7 px to its left
                _draw_motion(draw, 84, 2, 16, 12, self._mobility)
        if pct is not None:
            _draw_battery(draw, 224, 3, 20, 10, pct)
            _text_right(draw, 219, 2, "%d%%" % int(pct), tiny)
        draw.line([(2, 16), (PANEL_W - 2, 16)], fill=0, width=1)

        # the mascot face with its voice beside it: the fingerprint just after
        # start-up (for checking the browser's certificate warning), else the
        # mood's phrase, two lines at most.
        if opts.get("show_mascot", True):
            glyph = self._face_glyph(mood)
            draw.text((5, 19), glyph, font=big, fill=0)
            vx = min(max(5 + _text_w(draw, glyph, big) + 8, 96), 120)
            lines = self._fingerprint_lines()
            if not lines and voice:
                lines = _wrap_text(draw, voice, tiny, PANEL_W - 4 - vx)
            for i, line in enumerate(lines[:2]):
                draw.text((vx, 23 + i * 12), line, font=tiny, fill=0)

        # one status row: chip + agent mode, antenna (capture state), uptime.
        agent = state.agent if state is not None else None
        capturing = bool(state is not None and state.capture_active)
        agent_txt = _agent_label(agent)
        sy = 55
        _draw_chip(draw, 6, sy + 2, 10, 9)
        draw.text((20, sy), agent_txt, font=small, fill=0)
        x = 20 + _text_w(draw, agent_txt, small) + 8
        _draw_antenna(draw, x, sy, 10, 12, capturing)
        x += 16
        uptime_txt = _fmt_uptime(state.uptime if state is not None else None)
        if uptime_txt:
            seg = "UP " + uptime_txt
            if x + _text_w(draw, seg, small) <= PANEL_W - 4:
                draw.text((x, sy), seg, font=small, fill=0)

        # address row: each management link's IPv4 behind its BT/Wi-Fi glyph; an
        # address that won't fit is dropped rather than drawn off the edge.
        if opts.get("show_ip", True):
            ay = 73
            x = 6
            for iface, ip in addresses:
                gap = 10 if iface == "bnep0" else 14      # glyph width + spacing
                need = gap + _text_w(draw, ip, tiny)
                if x + need > PANEL_W - 4:
                    break
                if iface == "bnep0":
                    _draw_bt(draw, x, ay + 1, 6, 10, True)
                else:
                    _draw_wifi(draw, x + 2, ay + 2, 9, 8, True)
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
            if 22 + _text_w(draw, seg, count) <= right_limit:
                _draw_key(draw, x, by + 5, 12, 10)
                x += 16
                draw.text((x, by), seg, font=count, fill=0)
                x += _text_w(draw, seg, count) + 12
        for value, label in ((state.aps_total if state is not None else None, "AP"),
                             (state.networks_total if state is not None else None, "NET")):
            if value is None:
                continue
            seg = "%s %d" % (label, int(value))
            if x + _text_w(draw, seg, count) > right_limit:
                break
            draw.text((x, by), seg, font=count, fill=0)
            x += _text_w(draw, seg, count) + 12

        # the Kestrel brand mark, lower-right; its pose doubles as the motion
        # readout - stooping (diving) when moving, hovering when not.
        if opts.get("show_brand_glyph", True):
            path = _KESTREL_STOOP if self._mobility in _MOVING else _KESTREL_HOVER
            pts = _scaled_polygon(_flatten_svg_path(path), 24,
                                  (PANEL_W - 27, PANEL_H - 27))
            draw.polygon(pts, fill=0)

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
        out = []
        for iface in MANAGEMENT_INTERFACES:
            ip = _iface_ipv4(iface)
            if ip:
                out.append((iface, ip))
        return out

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
            png = _png_bytes(self._compose_image(state, mood, voice))
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

    def _mirror_page(self):
        esc = self.context.esc
        every = self._refresh_seconds()
        note = (" (no HAT attached - this is the frame it would show)"
                if self._absent else "")
        body = (
            '<p><a href="/plugins">&larr; Plugins</a></p>'
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
_SIOCGIFADDR = 0x8915


def _iface_ipv4(iface):
    """The IPv4 address on ``iface``, or None. A ``SIOCGIFADDR`` ioctl: no
    subprocess and no privilege, so it is cheap enough to call every frame."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        packed = fcntl.ioctl(sock.fileno(), _SIOCGIFADDR,
                             struct.pack("256s", iface.encode()[:15]))
    except OSError:
        return None
    finally:
        sock.close()
    return socket.inet_ntoa(packed[20:24])


def _as_float(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _font(size):
    """A TrueType font with the glyph coverage the faces need, falling back to
    PIL's bitmap default if none is installed (the image ships fonts-dejavu)."""
    from PIL import ImageFont
    for path in ("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
                 "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf"):
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            continue
    return ImageFont.load_default()


def _text_w(draw, text, font):
    """Pixel width of ``text`` in ``font`` (for laying glyphs out inline)."""
    try:
        box = draw.textbbox((0, 0), text, font=font)
        return box[2] - box[0]
    except Exception:  # noqa: BLE001 - very old PIL
        return int(draw.textlength(text, font=font))


def _text_right(draw, x_right, y, text, font):
    """Draw ``text`` right-aligned so it ends at ``x_right`` (for the battery %)."""
    try:
        box = draw.textbbox((0, 0), text, font=font)
        tw = box[2] - box[0]
    except Exception:  # noqa: BLE001 - very old PIL
        tw = draw.textlength(text, font=font)
    draw.text((x_right - tw, y), text, font=font, fill=0)


def _agent_label(agent):
    """The learning agent's mode for the status line: ``off`` when it is not
    running, else ``learned`` (a trained policy is driving) or ``baseline`` (the
    scripted sweep). Mirrors the honest ``smart`` rule in the mood ladder."""
    if not agent or not agent.get("running"):
        return "off"
    return "baseline" if (agent.get("policy") or "baseline") == "baseline" else "learned"


def _draw_bt(draw, x, y, w, h, connected):
    """The Bluetooth rune (BT PAN link). Bold when connected; thin with a slash
    through it when not."""
    cx = x + w / 2.0
    top, bot = (cx, y), (cx, y + h)
    ru, rl = (x + w, y + h * 0.30), (x + w, y + h * 0.70)
    lu, ll = (x, y + h * 0.30), (x, y + h * 0.70)
    width = 2 if connected else 1
    for a, b in ((top, bot), (top, ru), (ru, ll), (bot, rl), (rl, lu)):
        draw.line([a, b], fill=0, width=width)
    if not connected:
        draw.line([(x - 2, y + h + 1), (x + w + 2, y - 1)], fill=0, width=1)


def _draw_wifi(draw, x, y, w, h, connected):
    """The Wi-Fi fan (station link): the recognizable dot-plus-expanding-arcs
    glyph is always drawn (a Wi-Fi symbol reads as Wi-Fi whatever the state);
    a slash through it means the link is down. PIL arc angles are clockwise
    from 3 o'clock with y pointing down, so 225-315 is the upward fan above the
    base dot."""
    cx, cy = x + w / 2.0, y + h
    draw.ellipse([cx - 1.6, cy - 1.6, cx + 1.6, cy + 1.6], fill=0)
    for r in (h * 0.42, h * 0.72, h * 1.02):
        draw.arc([cx - r, cy - r, cx + r, cy + r], 225, 315, fill=0, width=2)
    if not connected:
        draw.line([(x - 2, cy + 2), (x + w + 2, y - 2)], fill=0, width=1)


def _signal_level(cn0, locked):
    """GPS signal as 0-3 bars from the strongest satellite's C/N0 (dB-Hz). Only
    meaningful with a fix; searching (no fix) reads as no bars."""
    if not locked or cn0 is None:
        return 0
    if cn0 >= 35:
        return 3
    if cn0 >= 30:
        return 2
    if cn0 >= 25:
        return 1
    return 0


def _draw_sat(draw, x, y, w, h, locked):
    """A small satellite: a diamond body with two solar-panel wings. Filled
    (solid) when the receiver has a lock, outlined when still searching."""
    cx, cy = x + w / 2.0, y + h / 2.0
    body = [(cx, cy - 4), (cx + 4, cy), (cx, cy + 4), (cx - 4, cy)]
    for sign in (-1, 1):
        wx = cx + sign * 3
        wing = [(wx, cy - 3), (wx + sign * 6, cy - 5),
                (wx + sign * 6, cy + 1), (wx, cy + 3)]
        if locked:
            draw.polygon(wing, fill=0)
        else:
            draw.line(wing + [wing[0]], fill=0, width=1)
    if locked:
        draw.polygon(body, fill=0)
    else:
        draw.line(body + [body[0]], fill=0, width=1)


def _draw_sigbars(draw, x, y, w, h, level):
    """Three ascending signal bars; the first ``level`` are filled, the rest
    outlined, so the glyph is legible whatever the strength."""
    bw, gap = 3, 2
    for i in range(3):
        bx = x + i * (bw + gap)
        bh = int(round(h * (i + 1) / 3.0))
        box = [bx, y + h - bh, bx + bw, y + h]
        if i < level:
            draw.rectangle(box, fill=0)
        else:
            draw.rectangle(box, outline=0, width=1)


def _draw_density(draw, x, y, w, h, level):
    """A four-bar density meter (APs in range): the first ``level`` bars filled,
    the rest outlined, so the glyph reads at any density."""
    bw, gap = 3, 2
    for i in range(4):
        bx = x + i * (bw + gap)
        bh = int(round(h * (i + 1) / 4.0))
        box = [bx, y + h - bh, bx + bw, y + h]
        if i < level:
            draw.rectangle(box, fill=0)
        else:
            draw.rectangle(box, outline=0, width=1)


def _draw_chip(draw, x, y, w, h):
    """A CPU/chip glyph (the learning agent): an outlined square with a filled
    core and short pins on each side."""
    draw.rectangle([x, y, x + w, y + h], outline=0, width=1)
    draw.rectangle([x + w * 0.32, y + h * 0.32, x + w * 0.68, y + h * 0.68], fill=0)
    for t in (0.3, 0.7):
        draw.line([(x + w * t, y), (x + w * t, y - 2)], fill=0, width=1)
        draw.line([(x + w * t, y + h), (x + w * t, y + h + 2)], fill=0, width=1)
        draw.line([(x, y + h * t), (x - 2, y + h * t)], fill=0, width=1)
        draw.line([(x + w, y + h * t), (x + w + 2, y + h * t)], fill=0, width=1)


def _draw_antenna(draw, x, y, w, h, active):
    """A broadcast antenna (capture): a mast on a base with a top dot, radiating
    waves when capturing and a slash through it when idle."""
    cx = x + w / 2.0
    base = y + h
    draw.line([(cx, y + 3), (cx, base)], fill=0, width=1)
    draw.line([(cx - 3, base), (cx + 3, base)], fill=0, width=1)
    draw.ellipse([cx - 1.5, y + 1, cx + 1.5, y + 4], fill=0)
    if active:
        for r in (3.5, 6.5):
            draw.arc([cx - r, y + 2 - r, cx + r, y + 2 + r], 220, 320, fill=0, width=1)
    else:
        draw.line([(x - 1, base + 1), (x + w + 1, y - 1)], fill=0, width=1)


def _draw_motion(draw, x, y, w, h, mobility):
    """The motion readout, drawn only while moving: a walking person
    (``walking``) or a moving car (``driving``). Parked shows nothing here (the
    Kestrel's hover pose denotes stopped)."""
    if mobility == "walking":
        _draw_person(draw, x, y, w, h)
    else:
        _draw_car(draw, x, y, w, h, moving=True)


def _draw_person(draw, x, y, w, h):
    """A walking stick figure: head, torso, striding legs and a swung arm."""
    cx = x + w * 0.45
    hr = h * 0.14
    hy = y + hr + 1
    draw.ellipse([cx - hr, hy - hr, cx + hr, hy + hr], outline=0, width=1)
    neck = hy + hr
    hip = y + h * 0.60
    draw.line([(cx, neck), (cx, hip)], fill=0, width=1)
    draw.line([(cx, hip), (cx - w * 0.24, y + h)], fill=0, width=1)   # back leg
    draw.line([(cx, hip), (cx + w * 0.30, y + h)], fill=0, width=1)   # front leg
    sh = neck + (hip - neck) * 0.3
    draw.line([(cx, sh), (cx + w * 0.26, sh + h * 0.14)], fill=0, width=1)  # arm


def _draw_car(draw, x, y, w, h, moving):
    """A small car silhouette (cabin + body + two wheels); trailing motion lines
    behind it when ``moving``, a static car (parked) otherwise."""
    body_top = y + h * 0.50
    roof = [(x + w * 0.24, body_top), (x + w * 0.36, y + h * 0.30),
            (x + w * 0.66, y + h * 0.30), (x + w * 0.78, body_top)]
    draw.line(roof, fill=0, width=1)
    draw.line([(x + w * 0.04, body_top), (x + w * 0.96, body_top)], fill=0, width=1)
    draw.line([(x + w * 0.04, body_top), (x + w * 0.04, y + h * 0.74)], fill=0, width=1)
    draw.line([(x + w * 0.96, body_top), (x + w * 0.96, y + h * 0.74)], fill=0, width=1)
    r = h * 0.15
    for cxw in (x + w * 0.28, x + w * 0.72):
        wy = y + h * 0.78
        draw.ellipse([cxw - r, wy - r, cxw + r, wy + r], fill=0)
    draw.line([(x + w * 0.10, y + h * 0.78), (x + w * 0.90, y + h * 0.78)],
              fill=0, width=1)
    if moving:
        for i, yy in enumerate((y + h * 0.34, y + h * 0.52)):
            draw.line([(x - 4 - i * 3, yy), (x - 1, yy)], fill=0, width=1)


def _draw_key(draw, x, y, w, h):
    """A key glyph (captured handshakes): a ring bow, a shaft and two teeth."""
    r = h * 0.38
    cy = y + h / 2.0
    draw.ellipse([x, cy - r, x + 2 * r, cy + r], outline=0, width=1)
    draw.line([(x + 2 * r, cy), (x + w, cy)], fill=0, width=1)
    draw.line([(x + w, cy), (x + w, cy + h * 0.30)], fill=0, width=1)
    draw.line([(x + w - 3, cy), (x + w - 3, cy + h * 0.22)], fill=0, width=1)


def _draw_battery(draw, x, y, w, h, pct):
    """A battery outline with a nub, filled proportionally to ``pct`` (0-100)."""
    draw.rectangle([x, y, x + w, y + h], outline=0, width=1)
    draw.rectangle([x + w, y + h * 0.30, x + w + 2, y + h * 0.70], fill=0)
    inner = (w - 4) * max(0.0, min(100.0, float(pct))) / 100.0
    if inner > 0:
        draw.rectangle([x + 2, y + 2, x + 2 + inner, y + h - 2], fill=0)


def _png_bytes(image):
    buf = io.BytesIO()
    image.save(buf, format="PNG")
    return buf.getvalue()
