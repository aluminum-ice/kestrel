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

# The root side of the plugin controls (kestrel-plugins-apply.socket).
APPLY_SOCKET = "/run/kestrel-plugins-apply.sock"


def apply_change(change, socket_path=None, timeout=20):
    """Send one plugin change to the root apply helper; return its reply."""
    data = (json.dumps(change) + "\n").encode("utf-8")
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.settimeout(timeout)
            sock.connect(socket_path or APPLY_SOCKET)
            sock.sendall(data)
            sock.shutdown(socket.SHUT_WR)
            chunks = []
            while True:
                chunk = sock.recv(65536)
                if not chunk:
                    break
                chunks.append(chunk)
        reply = json.loads(b"".join(chunks).decode("utf-8") or "{}")
    except (OSError, ValueError) as exc:
        return {"ok": False, "error": "could not reach the settings helper (%s)" % exc}
    if not isinstance(reply, dict):
        return {"ok": False, "error": "unexpected reply from the settings helper"}
    return reply
