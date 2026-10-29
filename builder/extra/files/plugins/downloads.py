"""downloads - take your capture files home from the dashboard.

A row button on the Plugins page ("Download") opens a page listing what you can
download from the capture store:

- every handshake file (``*.hc22000``) as one zip, with a short manifest;
- only the files that changed since your last zip download;
- every hashline combined into one de-duplicated ``.hc22000`` (the form
  hashcat takes directly);
- optionally (``include_locations``), the handshake files together with their
  GPS sidecars.

Recovered passwords (``cracked/``) and the archive are never offered here; they
stay reachable over SSH.

A download needs the web login and an encrypted connection: the request has to
arrive over HTTPS or from the device itself (loopback), so capture files never
cross the network unencrypted. It is local only - the operator pulls their own
files to their own browser - so the plugin declares no ``network`` capability
and needs no authorized-use acknowledgment.

Everything runs as ``pi``. Only regular files at the top level of the store
are read, each opened without following symlinks; a zip is built in the
plugin's own state directory and deleted as soon as it has been sent.
"""

from __future__ import annotations

import json
import os
import stat
import tempfile
import time
import zipfile
from urllib.parse import quote

from kestrel_pi import Plugin

SIDECAR = ".gps.json"
_CHUNK = 64 * 1024


def _state_dir():
    """``~/.local/state/kestrel-downloads`` of the account the daemon runs as."""
    try:
        import pwd

        home = pwd.getpwuid(os.getuid()).pw_dir
    except Exception:  # noqa: BLE001 - fall back to $HOME
        home = os.path.expanduser("~")
    return os.path.join(home, ".local", "state", "kestrel-downloads")


def _read_regular(path):
    """The bytes of ``path`` if it is a regular file, never following a symlink.
    ``O_NONBLOCK`` so a FIFO swapped in after the listing can't hang the open."""
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as handle:
        if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
            raise OSError("not a regular file: %s" % path)
        return handle.read()


def _stream_then_delete(path):
    """Yield ``path`` in chunks, then delete it, even if the client goes away."""
    try:
        with open(path, "rb") as handle:
            while True:
                chunk = handle.read(_CHUNK)
                if not chunk:
                    break
                yield chunk
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


def _size(n):
    if n >= 1024 * 1024:
        return "%.1f MB" % (n / (1024.0 * 1024))
    if n >= 1024:
        return "%.0f KB" % (n / 1024.0)
    return "%d bytes" % n


def _is_local(addr):
    return bool(addr) and (addr == "::1" or addr.startswith("127."))


class Downloads(Plugin):
    __author__ = "kestrel"
    __version__ = "0.1.0"
    __license__ = "GPL3"
    __description__ = "Download your handshake files from the dashboard."

    __target_api__ = 2
    # Reads the capture store and serves its own page; nothing leaves the
    # device except to the operator's own signed-in browser.
    __capabilities__ = ("capture-read", "web")
    __actions__ = ({"label": "Download", "path": "index", "method": "get"},)
    __options__ = {
        "include_locations": {
            "type": "bool", "default": False,
            "help": "Also offer the handshake files with their GPS sidecars "
                    "(where each capture was made)."},
        "max_mb": {
            "type": "int", "default": 200,
            "help": "Refuse a download larger than this many megabytes."},
    }

    def on_loaded(self):
        self.log = self.context.logger
        # A crash or power loss mid-download can leave a temporary zip behind.
        try:
            for name in os.listdir(_state_dir()):
                if name.startswith("export-") and name.endswith(".zip"):
                    os.unlink(os.path.join(_state_dir(), name))
        except OSError:
            pass

    # -- what's in the store ----------------------------------------------------
    def _files(self, with_locations=False):
        """``[(name, path, size, mtime_ns), ...]`` for the handshake files (and,
        with ``with_locations``, their GPS sidecars): regular files at the top
        level of the store only, so a planted symlink or subdirectory is
        skipped."""
        store = os.path.normpath(self.context.capture.store_dir)
        paths = list(self.context.capture.hashline_files())
        if with_locations:
            paths += [self.context.capture.sidecar_path(p, SIDECAR) for p in paths]
        out = []
        for path in paths:
            if os.path.normpath(os.path.dirname(path)) != store:
                continue
            try:
                info = os.lstat(path)
            except OSError:
                continue
            if stat.S_ISREG(info.st_mode):
                out.append((os.path.basename(path), path, info.st_size, info.st_mtime_ns))
        return out

    def _seen(self):
        try:
            with open(os.path.join(_state_dir(), "seen.json")) as handle:
                data = json.load(handle)
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def _remember(self, files):
        """Record what a zip download just carried, for "new since last"."""
        seen = self._seen()
        for name, _path, size, mtime in files:
            if name.endswith(".hc22000"):
                seen[name] = [size, mtime]
        directory = _state_dir()
        os.makedirs(directory, mode=0o700, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix="seen-", suffix=".tmp", dir=directory)
        with os.fdopen(fd, "w") as handle:
            json.dump(seen, handle)
        os.replace(tmp, os.path.join(directory, "seen.json"))

    def _changed(self):
        seen = self._seen()
        return [f for f in self._files() if seen.get(f[0]) != [f[2], f[3]]]

    def _cap_bytes(self):
        try:
            return max(1, int(self.options.get("max_mb", 200))) * 1024 * 1024
        except (TypeError, ValueError):
            return 200 * 1024 * 1024

    # -- gates --------------------------------------------------------------------
    def _refusal(self, request):
        """Why a download can't be served on this request, or None."""
        if not getattr(self.context, "web_authed", False):
            return ("Downloads need the dashboard login on. Turn it on with "
                    "<code>sudo kestrel web auth system</code>.")
        if not (getattr(request, "is_secure", False)
                or _is_local(getattr(request, "remote_addr", ""))):
            return ("Downloads need the encrypted dashboard, so capture files "
                    "never cross the network unencrypted. Open the dashboard over "
                    "<b>https://</b> and try again.")
        return None

    # -- pages and files ----------------------------------------------------------
    def on_webhook(self, path, request):
        refusal = self._refusal(request)
        if path in ("", "index"):
            return self._index(request, refusal)
        if refusal:
            return self.context.page("Downloads", "<p>" + refusal + "</p>", status=403)
        if path == "handshakes.zip":
            return self._send_zip(self._files(), "handshakes", remember=True)
        if path == "new.zip":
            return self._send_zip(self._changed(), "new-handshakes", remember=True)
        if path == "with-locations.zip" and self.options.get("include_locations"):
            return self._send_zip(self._files(with_locations=True),
                                  "handshakes-with-locations", remember=False)
        if path == "handshakes.hc22000":
            return self._send_combined()
        return self.context.page("Downloads", "<p>There's nothing here.</p>", status=404)

    def _too_big(self, total):
        return self.context.page(
            "Downloads",
            "<p>That download would be " + self.context.esc(_size(total))
            + ", over this plugin's limit (<code>max_mb</code>). Copy the capture "
            "store over SSH instead, or raise the limit in the plugin's settings.</p>",
            status=413)

    def _send_zip(self, files, label, remember):
        if not files:
            return self.context.page("Downloads", "<p>There's nothing to download "
                                     "yet.</p>", status=404)
        total = sum(f[2] for f in files)
        if total > self._cap_bytes():
            return self._too_big(total)
        directory = _state_dir()
        os.makedirs(directory, mode=0o700, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix="export-", suffix=".zip", dir=directory)
        lines = 0
        sent = []
        try:
            with os.fdopen(fd, "wb") as raw, \
                    zipfile.ZipFile(raw, "w", zipfile.ZIP_DEFLATED) as archive:
                for name, path, size, mtime in files:
                    try:
                        data = _read_regular(path)
                    except OSError:
                        continue        # vanished or replaced since the listing
                    if name.endswith(".hc22000"):
                        lines += sum(1 for line in data.splitlines() if line.strip())
                    archive.writestr(name, data)
                    sent.append((name, path, size, mtime))
                archive.writestr("MANIFEST.txt", (
                    "Kestrel capture export\n"
                    "Created: %s UTC\n"
                    "Files: %d\n"
                    "Hashlines: %d\n" % (time.strftime("%Y-%m-%d %H:%M", time.gmtime()),
                                         len(sent), lines)))
        except Exception:
            os.unlink(tmp)
            raise
        if remember:
            self._remember(sent)
        self.log.info("downloads: sent %s (%d files, %s)", label, len(sent),
                      _size(os.path.getsize(tmp)))
        filename = "kestrel-%s-%s.zip" % (label, time.strftime("%Y%m%dT%H%MZ", time.gmtime()))
        return (_stream_then_delete(tmp), 200, {
            "Content-Type": "application/zip",
            "Content-Disposition": 'attachment; filename="%s"' % filename,
            "Content-Length": str(os.path.getsize(tmp)),
        })

    def _send_combined(self):
        files = self._files()
        total = sum(f[2] for f in files)
        if total > self._cap_bytes():
            return self._too_big(total)
        seen, lines = set(), []
        for _name, path, _size, _mtime in files:
            try:
                data = _read_regular(path)
            except OSError:
                continue
            for line in data.splitlines():
                line = line.strip()
                if line and line not in seen:
                    seen.add(line)
                    lines.append(line)
        if not lines:
            return self.context.page("Downloads", "<p>There's nothing to download "
                                     "yet.</p>", status=404)
        self.log.info("downloads: sent combined hashlines (%d)", len(lines))
        filename = "kestrel-handshakes-%s.hc22000" % time.strftime("%Y%m%dT%H%MZ",
                                                                   time.gmtime())
        return (b"\n".join(lines) + b"\n", 200, {
            "Content-Type": "text/plain; charset=utf-8",
            "Content-Disposition": 'attachment; filename="%s"' % filename,
        })

    def _index(self, request, refusal):
        esc = self.context.esc
        token = ""
        try:
            token = request.args.get("token") or ""
        except AttributeError:
            pass
        qs = ("?token=" + quote(token, safe="")) if token else ""
        files = self._files()
        changed = self._changed()
        total = sum(f[2] for f in files)

        def item(href, title, detail):
            link = ('<a href="' + esc(href + qs) + '" download><b>' + esc(title) + "</b></a>"
                    if not refusal else "<b>" + esc(title) + "</b>")
            return "<li>" + link + ' <span class="smeta">' + esc(detail) + "</span></li>"

        items = [
            item("handshakes.zip", "All handshake files (.zip)",
                 "%d files, %s" % (len(files), _size(total))),
            item("new.zip", "New since your last download (.zip)",
                 "%d files changed" % len(changed)),
            item("handshakes.hc22000", "Everything as one .hc22000",
                 "every hashline once, ready for hashcat"),
        ]
        if self.options.get("include_locations"):
            items.append(item("with-locations.zip", "Handshake files with locations (.zip)",
                              "adds the GPS sidecars"))
        body = "<h1>Downloads</h1>"
        if refusal:
            body += '<p class="msg err" role="alert">' + refusal + "</p>"
        body += ("<p>Take your capture files home. Only networks you're allowed to "
                 "test belong in here.</p><ul>" + "".join(items) + "</ul>"
                 '<p class="hint">Recovered passwords and the archive aren\'t offered '
                 "here; copy them over SSH if you need them.</p>")
        return self.context.page("Downloads", body)
