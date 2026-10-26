import json
import socket
import threading
import time

from kestrel_pi.applyclient import apply_change


def _serve_once(path, handler):
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(path)
    server.listen(1)

    def run():
        conn, _ = server.accept()
        with conn:
            handler(conn)
        server.close()

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return thread


def test_returns_on_the_reply_line_without_waiting_for_the_helper_to_exit(tmp_path):
    # The helper replies, then keeps the connection open while it schedules the
    # deferred daemon restart; the client must not spend that time waiting.
    path = str(tmp_path / "apply.sock")
    release = threading.Event()

    def handler(conn):
        conn.recv(65536)
        conn.sendall(json.dumps({"ok": True, "restart": True}).encode() + b"\n")
        release.wait(5)

    thread = _serve_once(path, handler)
    started = time.monotonic()
    reply = apply_change({"plugin": "x"}, socket_path=path, timeout=5)
    elapsed = time.monotonic() - started
    release.set()
    thread.join(5)
    assert reply == {"ok": True, "restart": True}
    assert elapsed < 2


def test_timeout_bounds_the_whole_exchange_not_each_recv(tmp_path):
    # A helper that trickles bytes (never a full line) would reset a per-recv
    # timeout forever; the overall deadline still ends it.
    path = str(tmp_path / "apply.sock")
    stop = threading.Event()

    def handler(conn):
        conn.recv(65536)
        while not stop.is_set():
            try:
                conn.sendall(b" ")
            except OSError:
                return
            time.sleep(0.1)

    thread = _serve_once(path, handler)
    started = time.monotonic()
    reply = apply_change({"plugin": "x"}, socket_path=path, timeout=0.6)
    elapsed = time.monotonic() - started
    stop.set()
    thread.join(5)
    assert reply["ok"] is False and "could not reach" in reply["error"]
    assert elapsed < 2


def test_reply_without_a_newline_is_read_to_eof(tmp_path):
    path = str(tmp_path / "apply.sock")

    def handler(conn):
        conn.recv(65536)
        conn.sendall(b'{"ok": false, "error": "nope"}')

    thread = _serve_once(path, handler)
    reply = apply_change({"plugin": "x"}, socket_path=path, timeout=5)
    thread.join(5)
    assert reply == {"ok": False, "error": "nope"}
