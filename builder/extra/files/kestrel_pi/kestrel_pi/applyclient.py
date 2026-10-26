"""The client side of the root plugin-apply helper.

The daemon runs as ``pi`` and cannot write the root-owned ``config.toml`` or run
privileged plugin actions; it sends a JSON change over a Unix socket to the root
``plugins-ctl apply`` helper (``kestrel-plugins-apply.socket``), which
validates it against a fixed target allow-list and applies it. This is a flask-
free module so both the web layer and plugin code (via ``context.apply``) can use
it without importing the web server.
"""

from __future__ import annotations

import json
import socket
import time

# The root side of the plugin controls (kestrel-plugins-apply.socket).
APPLY_SOCKET = "/run/kestrel-plugins-apply.sock"
# The whole exchange's budget. Above the helper unit's RuntimeMaxSec (50s), so a
# slow change comes back as the helper's own error rather than a client timeout;
# see CLI_TIMEOUT in plugins-ctl for the full ladder.
APPLY_TIMEOUT = 55


def apply_change(change, socket_path=None, timeout=APPLY_TIMEOUT):
    """Send one plugin change to the root apply helper; return its reply.

    ``timeout`` bounds the whole exchange, not each socket call. The reply is
    one JSON line, so it returns as soon as that line is in: the helper keeps
    the connection open while it schedules a deferred restart/reboot after
    replying, and waiting for its exit would spend the head start that the
    deferral gives the page to reach the browser."""
    data = (json.dumps(change) + "\n").encode("utf-8")
    deadline = time.monotonic() + timeout
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.settimeout(timeout)
            sock.connect(socket_path or APPLY_SOCKET)
            sock.sendall(data)
            sock.shutdown(socket.SHUT_WR)
            chunks = []
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise socket.timeout("timed out")
                sock.settimeout(remaining)
                chunk = sock.recv(65536)
                if not chunk:
                    break
                chunks.append(chunk)
                if b"\n" in chunk:
                    break
        reply = json.loads(b"".join(chunks).decode("utf-8") or "{}")
    except (OSError, ValueError) as exc:
        return {"ok": False, "error": "could not reach the settings helper (%s)" % exc}
    if not isinstance(reply, dict):
        return {"ok": False, "error": "unexpected reply from the settings helper"}
    return reply
