"""HTTPS and sign-in support for the dashboard: the device certificate, the TLS
context, the signed session cookie and the sign-in throttle.

Everything here runs as ``pi`` and keeps its files in ``pi``'s own state
directory (``~/.local/state/kestrel-web/``, mode ``0700``), so no root step and
no build change is needed:

- ``cert.pem`` / ``key.pem``: a self-signed ECDSA P-256 certificate for
  ``<hostname>.local`` and ``<hostname>``, made on first start. The browser
  trusts it on first use; the operator compares its SHA-256 fingerprint with
  ``kestrel web fingerprint`` or the e-ink panel. It is reissued only when it is
  missing, unreadable or near expiry, so the fingerprint stays stable.
- ``session.key``: the secret that signs session cookies. Deleting it (``kestrel
  web logout-all``) signs every browser out.

No Flask import, so the e-ink plugin can read the fingerprint too.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import math
import os
import secrets
import ssl
import threading
import time
from datetime import datetime, timedelta, timezone

# Apple platforms refuse TLS server certificates valid for more than 825 days,
# so a phone can always proceed past the warning; reissued 60 days before expiry.
CERT_DAYS = 825
RENEW_DAYS = 60
CERT_NAME = "cert.pem"
KEY_NAME = "key.pem"
SESSION_KEY_NAME = "session.key"

# Session lifetime: idle and absolute, in seconds.
SESSION_IDLE = 12 * 3600
SESSION_ABSOLUTE = 7 * 86400
# A live session's cookie is re-issued (sliding the idle window) at most this often.
SESSION_REISSUE = 600
# A sign-in form is good for this long.
LOGIN_TOKEN_TTL = 900
COOKIE_NAME = "kestrel_session"


def state_dir():
    """``~/.local/state/kestrel-web`` of the account the daemon runs as."""
    try:
        import pwd

        home = pwd.getpwuid(os.getuid()).pw_dir
    except Exception:  # noqa: BLE001 - fall back to $HOME
        home = os.path.expanduser("~")
    return os.path.join(home, ".local", "state", "kestrel-web")


def _private_dir(path):
    os.makedirs(path, mode=0o700, exist_ok=True)
    os.chmod(path, 0o700)


def _write_private(path, data):
    """Write ``data`` to ``path`` as ``0600``, atomically and without following a
    planted symlink at the temporary name."""
    tmp = "%s.%s.tmp" % (path, secrets.token_hex(4))
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# -- certificate ------------------------------------------------------------------

def cert_path(directory=None):
    return os.path.join(directory or state_dir(), CERT_NAME)


def _usable_cert(cert_file, key_file, now):
    """True if the pair loads and the certificate isn't close to expiry."""
    try:
        from cryptography import x509

        with open(cert_file, "rb") as handle:
            cert = x509.load_pem_x509_certificate(handle.read())
        ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER).load_cert_chain(cert_file, key_file)
    except Exception:  # noqa: BLE001 - anything unreadable is regenerated
        return False
    expires = getattr(cert, "not_valid_after_utc", None)
    if expires is None:     # cryptography < 42
        expires = cert.not_valid_after.replace(tzinfo=timezone.utc)
    return expires - now > timedelta(days=RENEW_DAYS)


def ensure_certificate(hostname, directory=None, now=None):
    """Return ``(cert_file, key_file)``, making a new self-signed pair if the
    current one is missing, unreadable or within ``RENEW_DAYS`` of expiry."""
    directory = directory or state_dir()
    now = now or datetime.now(timezone.utc)
    cert_file = os.path.join(directory, CERT_NAME)
    key_file = os.path.join(directory, KEY_NAME)
    if _usable_cert(cert_file, key_file, now):
        return cert_file, key_file

    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

    host = (hostname or "kestrel").split(".")[0] or "kestrel"
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, host + ".local")])
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(days=2))
        .not_valid_after(now + timedelta(days=CERT_DAYS))
        .add_extension(x509.SubjectAlternativeName(
            [x509.DNSName(host + ".local"), x509.DNSName(host)]), critical=False)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]),
                       critical=False)
        .sign(key, hashes.SHA256())
    )
    _private_dir(directory)
    _write_private(key_file, key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption()))
    _write_private(cert_file, cert.public_bytes(serialization.Encoding.PEM))
    return cert_file, key_file


def fingerprint(cert_file=None):
    """The certificate's SHA-256 fingerprint as ``AB:CD:...`` (the form browsers
    show), or None when there is no readable certificate."""
    try:
        from cryptography import x509
        from cryptography.hazmat.primitives import serialization

        with open(cert_file or cert_path(), "rb") as handle:
            der = x509.load_pem_x509_certificate(handle.read()).public_bytes(
                serialization.Encoding.DER)
    except Exception:  # noqa: BLE001
        return None
    digest = hashlib.sha256(der).hexdigest().upper()
    return ":".join(digest[i:i + 2] for i in range(0, len(digest), 2))


def server_context(cert_file, key_file):
    """A TLS 1.2+ server context for the dashboard."""
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.load_cert_chain(cert_file, key_file)
    return ctx


# -- signed sessions --------------------------------------------------------------

def _b64(data):
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _unb64(text):
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


class SessionSigner:
    """Issues and checks the session cookie and the sign-in form token.

    A cookie is ``v1.<user>.<born>.<seen>.<nonce>.<mac>``: no server-side store,
    so a daemon restart (every settings save) doesn't sign anyone out. The key is
    read lazily from ``session.key`` (made on first use), so nothing touches disk
    until a sign-in actually happens."""

    def __init__(self, directory=None, clock=time.time):
        self._dir = directory
        self._clock = clock
        self._key = None
        self._lock = threading.Lock()

    def _secret(self):
        with self._lock:
            if self._key is None:
                directory = self._dir or state_dir()
                path = os.path.join(directory, SESSION_KEY_NAME)
                try:
                    with open(path, "rb") as handle:
                        key = handle.read()
                except OSError:
                    key = b""
                if len(key) < 32:
                    key = secrets.token_bytes(32)
                    _private_dir(directory)
                    _write_private(path, key)
                self._key = key
            return self._key

    def _mac(self, purpose, payload):
        return _b64(hmac.new(self._secret(), (purpose + "|" + payload).encode(),
                             "sha256").digest())

    def issue(self, user, born=None):
        now = int(self._clock())
        payload = "v1.%s.%d.%d.%s" % (_b64(user.encode()), born or now, now,
                                      secrets.token_urlsafe(6))
        return payload + "." + self._mac("session", payload)

    def verify(self, value):
        """``(user, born, seen)`` for a valid, unexpired cookie, else None."""
        try:
            version, user, born, seen, _nonce, mac = (value or "").split(".")
            payload = value.rsplit(".", 1)[0]
            if version != "v1" or not hmac.compare_digest(mac, self._mac("session", payload)):
                return None
            born, seen = int(born), int(seen)
            user = _unb64(user).decode()
        except (ValueError, TypeError, UnicodeDecodeError):
            return None
        now = self._clock()
        if now - seen > SESSION_IDLE or now - born > SESSION_ABSOLUTE or seen > now + 300:
            return None
        return user, born, seen

    def needs_reissue(self, seen):
        return self._clock() - seen >= SESSION_REISSUE

    def max_age(self, born):
        return max(0, int(born + SESSION_ABSOLUTE - self._clock()))

    def login_token(self):
        payload = "%d.%s" % (int(self._clock()), secrets.token_urlsafe(6))
        return payload + "." + self._mac("login", payload)

    def check_login_token(self, value):
        try:
            issued, _nonce, mac = (value or "").split(".")
            payload = value.rsplit(".", 1)[0]
            if not hmac.compare_digest(mac, self._mac("login", payload)):
                return False
            return 0 <= self._clock() - int(issued) <= LOGIN_TOKEN_TTL
        except (ValueError, TypeError):
            return False


# -- sign-in throttle -------------------------------------------------------------

class LoginThrottle:
    """Per-address backoff for failed sign-ins: ``free`` failures in ``window``
    seconds are free, then each further attempt waits 2, 4, 8 ... seconds, capped
    at ``cap``. Nothing is permanent, and a success clears the address."""

    def __init__(self, free=5, window=600, cap=60, clock=time.monotonic, max_addrs=1024):
        self.free, self.window, self.cap = free, window, cap
        self._clock = clock
        self._max = max_addrs
        self._fails = {}
        self._lock = threading.Lock()

    def _recent(self, addr, now):
        times = [t for t in self._fails.get(addr, ()) if now - t < self.window]
        if times:
            self._fails[addr] = times
        else:
            self._fails.pop(addr, None)
        return times

    def wait_seconds(self, addr):
        with self._lock:
            now = self._clock()
            times = self._recent(addr, now)
            if len(times) < self.free:
                return 0
            delay = min(self.cap, 2 ** (len(times) - self.free + 1))
            return max(0, math.ceil(times[-1] + delay - now))

    def fail(self, addr):
        with self._lock:
            now = self._clock()
            times = self._recent(addr, now)
            times.append(now)
            self._fails[addr] = times
            if len(self._fails) > self._max:
                oldest = min(self._fails, key=lambda a: self._fails[a][-1])
                self._fails.pop(oldest, None)

    def success(self, addr):
        with self._lock:
            self._fails.pop(addr, None)
