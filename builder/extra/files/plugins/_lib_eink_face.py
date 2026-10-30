"""Shared plugin library: the Kestrel e-ink face.

Everything about the face that does not depend on a particular panel, kept
once here for every e-ink plugin (``eink`` for the Waveshare 2.7" HAT and
``eink-ws213`` for the 2.13" one): the faces and their voices, the mood ladder
that picks one from live device and learning-agent state, the motion estimate
from GPS fixes, the Kestrel mark, the small glyphs of the status rows, text
helpers and the address lookup. A plugin keeps what is particular to its panel:
its options, its layout (where each row and glyph goes), its driver and how it
redraws. Load it with ``kestrel_pi.load_library`` (see
docs/developer/plugin-libraries.md)::

    from kestrel_pi import Plugin, load_library

    face = load_library(__file__, "eink_face", api=1)

    class MyPanel(Plugin):
        def _setup(self):
            self._moods = face.MoodTracker()
            self._motion = face.MobilityTracker()

        def _current_mood(self, state):
            mobility = self._motion.update(state.position if state else None)
            return self._moods.decide(state, self.context.options, mobility)

        def _compose_image(self, state, mood, voice):
            from PIL import Image, ImageDraw
            img = Image.new("1", (250, 122), 255)
            draw = ImageDraw.Draw(img)
            draw.text((5, 19), face.face_glyph(mood, self.context.options.get("faces")),
                      font=face.font(20), fill=0)
            face.draw_kestrel(draw, 250 - 27, 122 - 27, 24, self._motion.state in face.MOVING)
            return img

The face never lies: every expression maps to a real device/agent state (for
example ``smart`` shows only when a *learned* policy is actually driving, not
the scripted baseline fallback), arbitrated by an explicit priority ladder in
:func:`decide_mood`. Keep that rule in any change here.

PIL is imported only inside the functions that draw or measure, so loading this
library (off a Pi, in the tests, on the build host) touches no display and needs
no PIL.

``LIB_API`` follows docs/developer/plugin-libraries.md: a new face, voice,
glyph or helper bumps the minor; a renamed or removed name, a changed signature
or a changed mood rule that callers rely on bumps the major. Names with a
leading underscore are private.
"""

import calendar
import fcntl
import io
import math
import re
import socket
import struct
import time
from dataclasses import dataclass

from kestrel_pi.connectivity import MANAGEMENT_INTERFACES

LIB_API = (1, 0)

# --- faces -----------------------------------------------------------------
# The 26 classic pwnagotchi faces (kept for their charm), plus a few Phase 5
# additions. Each is overridable through the plugin's ``faces`` option
# (mood -> glyph; see face_glyph).
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
# the plugin's ``voices`` option (mood -> a phrase or a list of phrases). Kept short so
# they wrap into the narrow band beside the face.
VOICES = {
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


def voice_phrases(mood, overrides):
    """The phrase list for a mood: an operator override (a str or list under
    the plugin's ``voices`` option) if present, else the shipped set, else the base."""
    if isinstance(overrides, dict) and mood in overrides:
        v = overrides[mood]
        phrases = list(v) if isinstance(v, (list, tuple)) else [v]
        phrases = [str(p) for p in phrases if str(p)]
        if phrases:
            return phrases
    return list(VOICES.get(mood) or VOICES["awake"])


def fmt_uptime(seconds):
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


def wrap_text(draw, text, font, max_w):
    """Greedy word-wrap ``text`` to lines no wider than ``max_w`` px. A single
    word longer than the width is left on its own line (never split)."""
    words = str(text).split()
    lines, cur = [], ""
    for word in words:
        trial = word if not cur else cur + " " + word
        if text_w(draw, trial, font) <= max_w or not cur:
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
PARK_MS = 0.6       # at/below -> parked
WALK_MS = 0.9       # at/above (and below drive) -> walking
DRIVE_MS = 3.5      # at/above -> driving (vehicle)
MOBILITY_SUSTAIN = 2

# --- Kestrel brand mark (drawn from its SVG path, no binary asset) ---------
# The two poses from assets/branding/ (viewBox 0 0 64 64): hover
# (holding still = parked) and stoop (diving = moving). Solid silhouettes, so
# a filled polygon renders them perfectly in 1-bit.
KESTREL_HOVER = ("M32 7 C29.6 10 29 15 31 18 C23 12 12 11 5 18 C11 25 22 28 30 25 "
                  "L29 31 L30 37 L27 52 L32 47 L37 52 L34 37 L35 31 L34 25 "
                  "C42 28 53 25 59 18 C52 11 41 12 33 18 C35 15 34.4 10 32 7 Z")
KESTREL_STOOP = ("M10 8 C20 12 30 19 39 29 C45 23 53 21 59 24 C54 29 47 32 41 32 "
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


def flatten_svg_path(d, steps=8):
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


def scaled_polygon(points, box, origin):
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


MOBILITY_LABEL = {"parked": "parked", "walking": "walking", "driving": "driving",
                   "unknown": "gps?"}
# the motion states that count as "moving" (for the Kestrel's stoop pose)
MOVING = ("walking", "driving")


def haversine_m(a, b):
    """Great-circle distance in metres between two (lat, lon) pairs."""
    r = 6371000.0
    lat1, lon1, lat2, lon2 = (math.radians(a[0]), math.radians(a[1]),
                              math.radians(b[0]), math.radians(b[1]))
    dlat, dlon = lat2 - lat1, lon2 - lon1
    h = (math.sin(dlat / 2) ** 2
         + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2)
    return 2 * r * math.asin(min(1.0, math.sqrt(h)))


def classify_speed(speed):
    """Map a speed (m/s) to parked / walking / driving, or None in the
    parked-to-walking deadband (hold the current state there)."""
    if speed <= PARK_MS:
        return "parked"
    if speed >= DRIVE_MS:
        return "driving"
    if speed >= WALK_MS:
        return "walking"
    return None


def fix_seconds(fix):
    """When a GPS fix was taken, in epoch seconds: the provider's ``ts`` if it
    gives one, else its ``fix_time`` (UTC, whole seconds, as the ``gps`` plugin
    reports it), else None."""
    ts = as_float(fix.get("ts"))
    if ts is not None:
        return ts
    stamp = fix.get("fix_time")
    if isinstance(stamp, str):
        try:
            return float(calendar.timegm(time.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ")))
        except ValueError:
            pass
    return None


class MobilityTracker:
    """Parked / walking / driving / unknown, estimated from successive GPS
    fixes (a provider speed is used if present, else derived from the fix
    delta), with a per-state sustain count so it does not flicker.

    Speed is measured between *fixes*, timed by when each was taken
    (:func:`fix_seconds`), not by when the face read it: a face redraws more
    often than a fix may change, and reading the same fix again is not a stop.
    A fix with no timestamp at all is timed by the clock, and only when its
    position changed. :attr:`state` is the current estimate."""

    def __init__(self):
        self.state = "unknown"
        self._prev_fix = None           # (ts, lat, lon)
        self._counts = {}               # candidate state -> consecutive fixes

    def update(self, fix):
        """Fold in the latest fix (a ``position`` service dict, or None when
        there is no fix) and return the motion estimate."""
        if not fix or as_float(fix.get("lat")) is None:
            self._prev_fix = None
            self._counts = {}
            self.state = "unknown"
            return "unknown"
        here = (fix["lat"], fix["lon"])
        prev = self._prev_fix
        ts = fix_seconds(fix)
        if ts is None:
            if prev is not None and here == (prev[1], prev[2]):
                return self.state               # the same fix, read again
            ts = time.time()
        if prev is not None and ts <= prev[0]:
            return self.state                   # the same fix, read again
        self._prev_fix = (ts, here[0], here[1])
        if prev is None:
            return self.state
        dt = ts - prev[0]
        speed = as_float(fix.get("speed"))
        if speed is None or speed < 0:
            speed = haversine_m((prev[1], prev[2]), here) / dt
        candidate = classify_speed(speed)
        if candidate is None:                       # deadband - hold state
            return self.state
        counts = self._counts
        counts[candidate] = counts.get(candidate, 0) + 1
        for other in list(counts):
            if other != candidate:
                counts[other] = 0
        if counts[candidate] >= MOBILITY_SUSTAIN:
            self.state = candidate
        return self.state


class MoodTracker:
    """The state behind the mood that changes from frame to frame - the agent's
    smoothed reward trend, a just-promoted model, a fresh handshake, idle
    epochs and the look-left/look-right toggle - and :meth:`decide`, which
    builds :class:`MoodInputs` from a ``UIState`` snapshot and runs the ladder.
    One per plugin, driven from the plugin's render thread."""

    def __init__(self):
        self.look_toggle = False
        self.hs_until = 0.0             # monotonic time a fresh handshake shows until
        self.last_activity_epoch = None
        self._reward_ema = None
        self._promo_marker = None
        self._promo_until = 0.0

    def handshake(self, epoch, now=None):
        """A handshake was just captured: an excited face for a few seconds,
        and it counts as activity (resets the idle ladder) at ``epoch``."""
        now = time.monotonic() if now is None else now
        self.hs_until = now + 8.0
        if epoch is not None:
            self.last_activity_epoch = epoch

    def decide(self, state, options, mobility, fault=False, now=None):
        """The mood for this frame: a key into :data:`FACES`. ``state`` is the
        plugin's last ``UIState`` (or None), ``options`` its live options
        (``battery_low_percent``, ``bored_epochs``, ``sad_epochs``),
        ``mobility`` the :class:`MobilityTracker` estimate and ``fault`` True
        after a render or panel error."""
        now = time.monotonic() if now is None else now
        opts = options or {}
        self.look_toggle = not self.look_toggle

        agent = (state.agent if state is not None else None) or {}
        running = bool(agent.get("running"))
        policy = (agent.get("policy") or "baseline")
        learned = running and policy != "baseline"
        trend = self.reward_trend(agent.get("last_reward"))
        promoted = self.promotion(agent, now)

        battery = (state.battery if state is not None else None) or {}
        low_pct = int(opts.get("battery_low_percent", 10) or 10)
        battery_low = (battery.get("status") == "discharging"
                       and as_float(battery.get("percent")) is not None
                       and as_float(battery.get("percent")) <= low_pct)

        idle = self.idle_epochs(state)

        inp = MoodInputs(
            fault=bool(fault),
            battery_low=bool(battery_low),
            just_promoted=promoted,
            fresh_handshake=(now < self.hs_until),
            capture_active=bool(state is not None and state.capture_active),
            learned_active=learned,
            posture=str(agent.get("posture") or ""),
            transmit=bool(agent.get("transmit")),
            reward_trend=trend,
            mobility=mobility,
            idle_epochs=idle,
            bored_after=int(opts.get("bored_epochs", 15) or 15),
            sad_after=int(opts.get("sad_epochs", 25) or 25),
            look_toggle=self.look_toggle,
        )
        return decide_mood(inp)

    def reward_trend(self, reward):
        """-1, 0 or +1: the smoothed trend of the agent's last reward, with a
        deadband, so the face doesn't flicker between motivated and determined."""
        r = as_float(reward)
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

    def promotion(self, agent, now):
        """True for 10 s after the agent's model changes (a new model was
        promoted)."""
        marker = (agent.get("model_source"), agent.get("last_trained"))
        if marker != (None, None) and self._promo_marker is not None \
                and marker != self._promo_marker:
            self._promo_until = now + 10.0
        if marker != (None, None):
            self._promo_marker = marker
        return now < self._promo_until

    def idle_epochs(self, state):
        """Epochs since the last activity (a handshake), from ``state.epoch``."""
        if state is None:
            return 0
        if self.last_activity_epoch is None:
            self.last_activity_epoch = state.epoch
            return 0
        return max(0, state.epoch - self.last_activity_epoch)


def face_glyph(mood, overrides=None):
    """The face for ``mood``: an operator override (the plugin's ``faces``
    option, a ``{mood: glyph}`` table) if it has one, else the shipped face."""
    if isinstance(overrides, dict) and mood in overrides:
        return str(overrides[mood])
    return FACES.get(mood, FACES["awake"])


def voice_for(mood, rot, overrides=None):
    """A phrase matching ``mood`` (``overrides`` is the plugin's ``voices``
    option). ``rot`` advances on each mood change, so the words vary over time
    while always matching the face and the frame stays stable within a mood
    (no e-ink redraw just to reword)."""
    phrases = voice_phrases(mood or "awake", overrides)
    return phrases[rot % len(phrases)]


def draw_kestrel(draw, x, y, size, moving):
    """The Kestrel mark as a filled ``size``-px square at (x, y): stooping
    (diving) when ``moving``, hovering otherwise, so its pose doubles as the
    motion readout."""
    path = KESTREL_STOOP if moving else KESTREL_HOVER
    draw.polygon(scaled_polygon(flatten_svg_path(path), size, (x, y)), fill=0)


def addresses(interfaces=MANAGEMENT_INTERFACES):
    """``[(iface, ipv4), ...]`` for the management links that have an address,
    in ``interfaces`` order (Bluetooth tether first)."""
    out = []
    for iface in interfaces:
        ip = iface_ipv4(iface)
        if ip:
            out.append((iface, ip))
    return out


# --- text, glyphs and the address lookup ----------------------------------
_SIOCGIFADDR = 0x8915


def iface_ipv4(iface):
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


def as_float(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def font(size):
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


def text_w(draw, text, font):
    """Pixel width of ``text`` in ``font`` (for laying glyphs out inline)."""
    try:
        box = draw.textbbox((0, 0), text, font=font)
        return box[2] - box[0]
    except Exception:  # noqa: BLE001 - very old PIL
        return int(draw.textlength(text, font=font))


def text_right(draw, x_right, y, text, font):
    """Draw ``text`` right-aligned so it ends at ``x_right`` (for the battery %)."""
    try:
        box = draw.textbbox((0, 0), text, font=font)
        tw = box[2] - box[0]
    except Exception:  # noqa: BLE001 - very old PIL
        tw = draw.textlength(text, font=font)
    draw.text((x_right - tw, y), text, font=font, fill=0)


def agent_label(agent):
    """The learning agent's mode for the status line: ``off`` when it is not
    running, else ``learned`` (a trained policy is driving) or ``baseline`` (the
    scripted sweep). Mirrors the honest ``smart`` rule in the mood ladder."""
    if not agent or not agent.get("running"):
        return "off"
    return "baseline" if (agent.get("policy") or "baseline") == "baseline" else "learned"


def draw_bt(draw, x, y, w, h, connected):
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


def draw_wifi(draw, x, y, w, h, connected):
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


def signal_level(cn0, locked):
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


def draw_sat(draw, x, y, w, h, locked):
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


def draw_sigbars(draw, x, y, w, h, level):
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


def draw_density(draw, x, y, w, h, level):
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


def draw_chip(draw, x, y, w, h):
    """A CPU/chip glyph (the learning agent): an outlined square with a filled
    core and short pins on each side."""
    draw.rectangle([x, y, x + w, y + h], outline=0, width=1)
    draw.rectangle([x + w * 0.32, y + h * 0.32, x + w * 0.68, y + h * 0.68], fill=0)
    for t in (0.3, 0.7):
        draw.line([(x + w * t, y), (x + w * t, y - 2)], fill=0, width=1)
        draw.line([(x + w * t, y + h), (x + w * t, y + h + 2)], fill=0, width=1)
        draw.line([(x, y + h * t), (x - 2, y + h * t)], fill=0, width=1)
        draw.line([(x + w, y + h * t), (x + w + 2, y + h * t)], fill=0, width=1)


def draw_antenna(draw, x, y, w, h, active):
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


def draw_motion(draw, x, y, w, h, mobility):
    """The motion readout, drawn only while moving: a walking person
    (``walking``) or a moving car (``driving``). Parked shows nothing here (the
    Kestrel's hover pose denotes stopped)."""
    if mobility == "walking":
        draw_person(draw, x, y, w, h)
    else:
        draw_car(draw, x, y, w, h, moving=True)


def draw_person(draw, x, y, w, h):
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


def draw_car(draw, x, y, w, h, moving):
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


def draw_key(draw, x, y, w, h):
    """A key glyph (captured handshakes): a ring bow, a shaft and two teeth."""
    r = h * 0.38
    cy = y + h / 2.0
    draw.ellipse([x, cy - r, x + 2 * r, cy + r], outline=0, width=1)
    draw.line([(x + 2 * r, cy), (x + w, cy)], fill=0, width=1)
    draw.line([(x + w, cy), (x + w, cy + h * 0.30)], fill=0, width=1)
    draw.line([(x + w - 3, cy), (x + w - 3, cy + h * 0.22)], fill=0, width=1)


def draw_battery(draw, x, y, w, h, pct):
    """A battery outline with a nub, filled proportionally to ``pct`` (0-100)."""
    draw.rectangle([x, y, x + w, y + h], outline=0, width=1)
    draw.rectangle([x + w, y + h * 0.30, x + w + 2, y + h * 0.70], fill=0)
    inner = (w - 4) * max(0.0, min(100.0, float(pct))) / 100.0
    if inner > 0:
        draw.rectangle([x + 2, y + 2, x + 2 + inner, y + h - 2], fill=0)


def png_bytes(image):
    buf = io.BytesIO()
    image.save(buf, format="PNG")
    return buf.getvalue()
