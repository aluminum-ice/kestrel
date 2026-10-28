"""gps plugin: the serial open must never take a controlling terminal.

Regression: the reader opened the USB NMEA device with a plain ``open()`` (no
``O_NOCTTY``). A systemd service is a session leader with no controlling
terminal, so that open made the serial port the daemon's controlling terminal;
unplugging the receiver then sent SIGHUP to the whole daemon and killed it
(systemd counts SIGHUP as a clean exit, so it was not restarted). The open now
passes ``O_NOCTTY`` (and ``O_NONBLOCK``), and the daemon ignores SIGHUP as a
backstop (see test_daemon)."""

import importlib.util
import logging
import os
import threading

PLUGIN_DIR = os.path.normpath(
    os.path.join(os.path.dirname(__file__), "..", "..", "plugins"))


def _mod():
    path = os.path.join(PLUGIN_DIR, "gps.py")
    spec = importlib.util.spec_from_file_location("gps_under_test", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


GPS = _mod()


def _plugin(device):
    """A GPS instance wired by hand, WITHOUT on_loaded (no reader thread)."""
    p = GPS.GPS()
    p.options = {"device": device, "source": "serial"}
    p.log = logging.getLogger("test.gps")
    p._stop = threading.Event()
    p._fix_lock = threading.Lock()
    return p


def test_read_serial_opens_with_no_controlling_terminal(tmp_path, monkeypatch):
    node = tmp_path / "ttyACM0"
    node.write_bytes(b"$GPGGA,,,,,,0,,,,,,,,*00\n")   # one line, then EOF -> returns

    captured = {}
    real_open = os.open

    def recording_open(path, flags, *a, **k):
        captured["flags"] = flags
        return real_open(path, flags, *a, **k)

    monkeypatch.setattr(GPS.os, "open", recording_open)
    _plugin(str(node))._read_serial()      # reads the file to EOF and returns

    assert "flags" in captured, "the serial device was never opened"
    assert captured["flags"] & os.O_NOCTTY, "serial open must set O_NOCTTY"
    assert captured["flags"] & os.O_NONBLOCK, "serial open must set O_NONBLOCK"


def test_read_serial_no_node_returns_without_opening(tmp_path, monkeypatch):
    opened = []
    monkeypatch.setattr(GPS.os, "open", lambda *a, **k: opened.append(a))
    _plugin(str(tmp_path / "absent"))._read_serial()
    assert opened == []


def test_read_serial_survives_the_device_vanishing_mid_read(tmp_path, monkeypatch):
    node = tmp_path / "ttyACM0"
    node.write_bytes(b"partial")
    real_read = os.read

    def failing_read(fd, n):
        real_read(fd, n)
        raise OSError(5, "Input/output error")   # device yanked mid-read

    monkeypatch.setattr(GPS.os, "read", failing_read)
    # Must return cleanly (so _run re-resolves the node), never raise.
    _plugin(str(node))._read_serial()
