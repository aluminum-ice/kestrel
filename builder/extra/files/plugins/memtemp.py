"""memtemp - show memory, CPU load and temperature on the display.

A display plugin (``on_ui_setup`` / ``on_ui_update``). Until Phase 5 it renders
through the stub UI (so its values appear on the web status page and in the
log), proving the display hooks are wired. It reads only local /proc and the
thermal zone, so it declares just the ``ui`` capability.

Ported shape from legacy pwnagotchi's memtemp: the hook names and the
mem/cpu/temp idea are the same; the difference is it draws through
``context``'s stub UI instead of a global display singleton.
"""

from __future__ import annotations

import glob
import os

from kestrel_pi import Plugin


def _fan_pwm_percent():
    """A PWM/cooling fan's current speed as a percent of its max, read from a
    ``pwm-fan`` thermal cooling device (e.g. the Raspberry Pi 5 active cooler),
    or ``None`` when no such fan is present. 0 (idle) still counts as present."""
    for base in sorted(glob.glob("/sys/class/thermal/cooling_device*")):
        try:
            with open(base + "/type", "r", encoding="ascii") as handle:
                if "fan" not in handle.read().strip().lower():
                    continue
            with open(base + "/cur_state", "r", encoding="ascii") as handle:
                cur = int(handle.read().strip())
            with open(base + "/max_state", "r", encoding="ascii") as handle:
                mx = int(handle.read().strip())
        except (OSError, ValueError):
            continue
        if mx > 0:
            return int(round(100.0 * min(cur, mx) / mx))
    return None


def _fan_rpm():
    """A fan tachometer reading (RPM) from a hwmon ``fanN_input``, or ``None``.
    Covers the Pi's ``cooling_fan`` hwmon and generic hwmon fan inputs."""
    patterns = (
        "/sys/devices/platform/cooling_fan/hwmon/hwmon*/fan?_input",
        "/sys/class/hwmon/hwmon*/fan?_input",
    )
    for pattern in patterns:
        for path in sorted(glob.glob(pattern)):
            try:
                with open(path, "r", encoding="ascii") as handle:
                    return int(handle.read().strip())
            except (OSError, ValueError):
                continue
    return None


def _mem_used_percent():
    try:
        with open("/proc/meminfo", "r", encoding="ascii") as handle:
            info = {}
            for line in handle:
                key, _, rest = line.partition(":")
                info[key.strip()] = int(rest.strip().split()[0])  # kB
        total = info.get("MemTotal", 0)
        avail = info.get("MemAvailable", info.get("MemFree", 0))
        if total:
            return round(100.0 * (total - avail) / total, 1)
    except (OSError, ValueError):
        pass
    return None


def _cpu_load():
    try:
        return round(os.getloadavg()[0], 2)
    except OSError:
        return None


def _cpu_temp_c():
    for zone in ("/sys/class/thermal/thermal_zone0/temp",):
        try:
            with open(zone, "r", encoding="ascii") as handle:
                return round(int(handle.read().strip()) / 1000.0, 1)
        except (OSError, ValueError):
            continue
    return None


class MemTemp(Plugin):
    __author__ = "kestrel"
    __version__ = "0.1.0"
    __license__ = "GPL3"
    __description__ = "Show memory, CPU load and temperature on the display."

    __target_api__ = 2
    __capabilities__ = ("ui",)
    __options__ = {
        "fahrenheit": {"type": "bool", "default": False},
    }

    def on_loaded(self):
        self.log = self.context.logger
        # A PWM/cooling fan is hardware, so detect it once at load. Prefer the
        # speed percent (a cooling device) for the bar; fall back to RPM only.
        self._fan_pct = _fan_pwm_percent() is not None
        self._has_fan = self._fan_pct or _fan_rpm() is not None
        self.log.info("memtemp loaded (fan=%s)", "yes" if self._has_fan else "no")

    def on_ui_setup(self, ui):
        # Labels and a render hint use the typed element API (Phase 3.5); the
        # old positional form still works, so this stays a plain port. "mem" is
        # a percent so the web card can draw a bar from its value; the e-ink
        # view just shows the text. The call is source-compatible with the
        # Phase 3 stub (label/kind are optional keywords).
        ui.add_element("mem", "-", label="Memory", kind="percent")
        ui.add_element("cpu", "-", label="CPU load")
        ui.add_element("temp", "-", label="Temperature")
        # Only show a fan element when a fan is actually present; percent gets a
        # bar, a tach-only fan shows RPM as text.
        if self._has_fan:
            ui.add_element("fan", "-", label="Fan",
                           kind="percent" if self._fan_pct else "text")

    def on_ui_update(self, ui, state):
        mem = _mem_used_percent()
        cpu = _cpu_load()
        temp = _cpu_temp_c()
        if temp is not None and self.options.get("fahrenheit"):
            temp = round(temp * 9 / 5 + 32, 1)
            unit = "F"
        else:
            unit = "C"
        ui.set("mem", "%s%%" % mem if mem is not None else "?")
        ui.set("cpu", "%s" % cpu if cpu is not None else "?")
        ui.set("temp", "%s%s" % (temp, unit) if temp is not None else "?")
        if self._has_fan:
            ui.set("fan", _fan_text())


def _fan_text():
    """A compact fan readout: ``60% · 2400 rpm`` / ``60%`` / ``2400 rpm`` /
    ``?``. The percent leads so a percent-kind element draws its bar from it."""
    pct = _fan_pwm_percent()
    rpm = _fan_rpm()
    if pct is not None and rpm is not None:
        return "%d%% · %d rpm" % (pct, rpm)
    if pct is not None:
        return "%d%%" % pct
    if rpm is not None:
        return "%d rpm" % rpm
    return "?"
