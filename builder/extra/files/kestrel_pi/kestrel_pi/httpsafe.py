"""HTTP rules for plugins that send credentials (wpa-sec, wigle).

A credential only ever goes to an ``https://`` endpoint, never follows a
redirect (``requests`` re-sends a ``cookies=`` value to wherever it is
redirected), and a response is read with a size cap so a broken or hostile
server can't exhaust the daemon's memory, which every plugin shares. The
``updates`` plugin applies the same https rule to its own host.

Use ``https_endpoint`` before sending, pass ``allow_redirects=False`` and
``stream=True`` to ``requests``, then ``check_response`` and ``read_capped``
(or ``read_json``)."""

import json

# Caps for what a plugin reads back. The wpa-sec potfile lists every network
# cracked under a key; real ones are far smaller than this.
MAX_BODY = 16 * 1024 * 1024
MAX_JSON = 2 * 1024 * 1024


class UnsafeResponse(Exception):
    """A request refused before sending, or a response refused on reading."""


def https_endpoint(value, default):
    """``value`` (else ``default``) without a trailing slash, if it is an
    ``https://`` URL. Raises :class:`UnsafeResponse` otherwise, so nothing is
    sent in clear text."""
    url = (value or default or "").strip().rstrip("/")
    if not url.lower().startswith("https://") or len(url) == len("https://"):
        raise UnsafeResponse("endpoint must be an https:// URL, not %r" % url)
    return url


def check_response(resp):
    """Refuse a redirect: the caller sent a credential with
    ``allow_redirects=False``, and following it would send the credential on
    to wherever it points."""
    if 300 <= resp.status_code < 400:
        resp.close()
        raise UnsafeResponse("HTTP %d redirect to %s, not followed with credentials"
                             % (resp.status_code, resp.headers.get("Location") or "?"))


def read_capped(resp, limit=MAX_BODY):
    """The body of a ``stream=True`` response as bytes, refusing more than
    ``limit`` (checked against Content-Length first, then while reading)."""
    length = str(resp.headers.get("Content-Length") or "")
    if length.isdigit() and int(length) > limit:
        resp.close()
        raise UnsafeResponse("response of %s bytes is over the %d-byte limit"
                             % (length, limit))
    chunks, total = [], 0
    for chunk in resp.iter_content(chunk_size=65536):
        total += len(chunk)
        if total > limit:
            resp.close()
            raise UnsafeResponse("response is over the %d-byte limit" % limit)
        chunks.append(chunk)
    return b"".join(chunks)


def read_json(resp, limit=MAX_JSON):
    """The JSON body of a ``stream=True`` response, read with a size cap."""
    return json.loads(read_capped(resp, limit).decode("utf-8", errors="replace"))
