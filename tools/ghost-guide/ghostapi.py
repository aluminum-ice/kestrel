"""A small Ghost Admin API client: read and update one page, upload images.

Authenticates with an Admin API key (``<id>:<hex secret>``, from a Custom
Integration in Ghost) as a short-lived HS256 token, signed with the standard
library. The key comes only from the environment and is never printed: errors
report the HTTP status and Ghost's message, not the request.
"""

import base64
import hashlib
import hmac
import json
import time

import requests

API = "/ghost/api/admin"
_TIMEOUT = 60


class GhostError(RuntimeError):
    pass


def _b64(data):
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


class Ghost:
    def __init__(self, url, admin_key):
        try:
            key_id, secret = admin_key.split(":", 1)
            self._secret = bytes.fromhex(secret)
        except ValueError:
            raise GhostError("GHOST_ADMIN_API_KEY must look like <id>:<hex secret>") from None
        self._key_id = key_id
        self.url = url.rstrip("/")

    def _token(self):
        now = int(time.time())
        header = {"alg": "HS256", "typ": "JWT", "kid": self._key_id}
        payload = {"iat": now, "exp": now + 300, "aud": "/admin/"}
        signing = (_b64(json.dumps(header, separators=(",", ":")).encode()) + "."
                   + _b64(json.dumps(payload, separators=(",", ":")).encode()))
        sig = hmac.new(self._secret, signing.encode(), hashlib.sha256).digest()
        return signing + "." + _b64(sig)

    def _request(self, method, path, **kwargs):
        headers = {"Authorization": "Ghost " + self._token(),
                   "Accept-Version": "v5.0"}
        resp = requests.request(method, self.url + API + path, headers=headers,
                                timeout=_TIMEOUT, **kwargs)
        if resp.status_code >= 400:
            try:
                errors = resp.json().get("errors") or []
                detail = "; ".join(e.get("message", "") + (" " + e["context"] if e.get("context") else "")
                                   for e in errors)
            except ValueError:
                detail = resp.text[:200]
            raise GhostError("%s %s: HTTP %d %s" % (method, path.split("?")[0],
                                                    resp.status_code, detail))
        return resp.json()

    def page_by_slug(self, slug):
        """The page (any status) with ``html`` and ``codeinjection_foot``, or None."""
        try:
            data = self._request("GET", "/pages/slug/%s/?formats=html" % slug)
        except GhostError as exc:
            if "HTTP 404" in str(exc):
                return None
            raise
        return data["pages"][0]

    def update_page(self, page, html=None, **fields):
        """Update ``page`` (as returned by :meth:`page_by_slug`). With ``html``
        the body is replaced and converted into Ghost's cards (``source=html``).
        ``updated_at`` comes from ``page``, so Ghost refuses the write if the
        page changed since it was read."""
        body = dict(fields, updated_at=page["updated_at"])
        query = "?formats=html"
        if html is not None:
            body["html"] = html
            query += "&source=html"
        data = self._request("PUT", "/pages/%s/%s" % (page["id"], query),
                             json={"pages": [body]})
        return data["pages"][0]

    def create_draft_page(self, title, slug, html):
        data = self._request("POST", "/pages/?source=html&formats=html",
                             json={"pages": [{"title": title, "slug": slug,
                                              "status": "draft", "html": html}]})
        return data["pages"][0]

    def upload_image(self, path, name):
        """Upload a PNG and return its URL. Ghost suffixes -1, -2, ... when
        ``name`` is taken, so the stem still identifies the figure."""
        with open(path, "rb") as handle:
            data = self._request("POST", "/images/upload/",
                                 files={"file": (name, handle, "image/png")},
                                 data={"purpose": "image", "ref": name})
        return data["images"][0]["url"]
