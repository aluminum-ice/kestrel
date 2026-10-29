"""kestrel_pi.webtls: the dashboard's certificate, TLS context, signed session
cookie and sign-in throttle. The conftest points ``state_dir()`` at a per-test
directory, so nothing here touches a real home directory."""

import logging
import os
import ssl
import stat
import types
from datetime import datetime, timedelta, timezone

import pytest

from kestrel_pi import webtls

try:
    import cryptography  # noqa: F401
    _HAVE_CRYPTO = True
except ImportError:
    _HAVE_CRYPTO = False

# The certificate needs `cryptography` (in the device's venv); the session and
# throttle code is stdlib-only, so those tests always run.
needs_crypto = pytest.mark.skipif(not _HAVE_CRYPTO, reason="cryptography not installed")


def _mode(path):
    return stat.S_IMODE(os.stat(path).st_mode)


# -- certificate ------------------------------------------------------------------

@needs_crypto
def test_certificate_is_made_once_with_private_files(tmp_path):
    d = str(tmp_path / "web")
    cert, key = webtls.ensure_certificate("kestrel", d)
    assert _mode(d) == 0o700
    assert _mode(cert) == 0o600 and _mode(key) == 0o600
    first = webtls.fingerprint(cert)
    assert first and len(first.split(":")) == 32
    # A second start reuses it: the fingerprint operators compared stays put.
    webtls.ensure_certificate("kestrel", d)
    assert webtls.fingerprint(cert) == first


@needs_crypto
def test_certificate_names_and_validity(tmp_path):
    from cryptography import x509

    cert, _ = webtls.ensure_certificate("kestrel.example", str(tmp_path))
    with open(cert, "rb") as handle:
        parsed = x509.load_pem_x509_certificate(handle.read())
    names = parsed.extensions.get_extension_for_class(
        x509.SubjectAlternativeName).value.get_values_for_type(x509.DNSName)
    assert names == ["kestrel.local", "kestrel"]
    span = parsed.not_valid_after - parsed.not_valid_before
    assert span <= timedelta(days=webtls.CERT_DAYS + 2)    # phones refuse > 825 days


@needs_crypto
def test_certificate_is_reissued_when_missing_or_near_expiry(tmp_path):
    d = str(tmp_path)
    cert, key = webtls.ensure_certificate("kestrel", d)
    first = webtls.fingerprint(cert)
    os.unlink(key)
    webtls.ensure_certificate("kestrel", d)
    second = webtls.fingerprint(cert)
    assert second and second != first
    later = datetime.now(timezone.utc) + timedelta(days=webtls.CERT_DAYS - 10)
    webtls.ensure_certificate("kestrel", d, now=later)
    assert webtls.fingerprint(cert) != second


@needs_crypto
def test_server_context_and_missing_fingerprint(tmp_path):
    cert, key = webtls.ensure_certificate("kestrel", str(tmp_path))
    ctx = webtls.server_context(cert, key)
    assert ctx.minimum_version == ssl.TLSVersion.TLSv1_2
    assert webtls.fingerprint(str(tmp_path / "nope.pem")) is None


@needs_crypto
def test_daemon_web_tls_follows_the_setting(tmp_path):
    from kestrel_pi.daemon import Daemon

    log = logging.getLogger("test-webtls")
    on = Daemon._web_tls(types.SimpleNamespace(web_cfg={}, log=log))
    assert on is not None and on[0] == 8443 and isinstance(on[1], ssl.SSLContext)
    assert Daemon._web_tls(types.SimpleNamespace(web_cfg={"tls": "off"}, log=log)) is None
    custom = Daemon._web_tls(types.SimpleNamespace(web_cfg={"tls_port": 9443}, log=log))
    assert custom[0] == 9443


# -- sessions ---------------------------------------------------------------------

class _Clock:
    def __init__(self, now=1_000_000.0):
        self.now = now

    def __call__(self):
        return self.now


def test_session_round_trip_and_tampering(tmp_path):
    clock = _Clock()
    s = webtls.SessionSigner(str(tmp_path), clock=clock)
    value = s.issue("pi")
    user, born, seen = s.verify(value)
    assert user == "pi" and born == seen == int(clock.now)
    assert _mode(str(tmp_path / webtls.SESSION_KEY_NAME)) == 0o600
    head, mac = value.rsplit(".", 1)
    assert s.verify(head + "." + ("A" if mac[0] != "A" else "B") + mac[1:]) is None
    assert s.verify(value.replace(".cGk.", ".cm9vdA.")) is None    # another user
    for junk in (None, "", "v1", "v2." + value[3:], "a.b.c.d.e.f"):
        assert s.verify(junk) is None


def test_session_idle_and_absolute_expiry(tmp_path):
    clock = _Clock()
    s = webtls.SessionSigner(str(tmp_path), clock=clock)
    value = s.issue("pi")
    clock.now += webtls.SESSION_IDLE + 1
    assert s.verify(value) is None                     # idle too long
    clock.now = 1_000_000.0
    born = int(clock.now)
    clock.now += webtls.SESSION_ABSOLUTE - 60
    fresh = s.issue("pi", born=born)                   # re-issued, same birth
    assert s.verify(fresh) is not None
    clock.now += 120
    assert s.verify(fresh) is None                     # past the absolute limit


def test_rotating_the_key_signs_everyone_out(tmp_path):
    value = webtls.SessionSigner(str(tmp_path)).issue("pi")
    os.unlink(str(tmp_path / webtls.SESSION_KEY_NAME))     # kestrel web logout-all
    assert webtls.SessionSigner(str(tmp_path)).verify(value) is None


def test_reissue_window_and_max_age(tmp_path):
    clock = _Clock()
    s = webtls.SessionSigner(str(tmp_path), clock=clock)
    _, born, seen = s.verify(s.issue("pi"))
    assert not s.needs_reissue(seen)
    clock.now += webtls.SESSION_REISSUE
    assert s.needs_reissue(seen)
    assert s.max_age(born) == webtls.SESSION_ABSOLUTE - webtls.SESSION_REISSUE


def test_login_token_is_signed_and_short_lived(tmp_path):
    clock = _Clock()
    s = webtls.SessionSigner(str(tmp_path), clock=clock)
    token = s.login_token()
    assert s.check_login_token(token)
    assert not s.check_login_token(token[:-1] + ("A" if token[-1] != "A" else "B"))
    assert not s.check_login_token(s.issue("pi"))      # a cookie isn't a form token
    clock.now += webtls.LOGIN_TOKEN_TTL + 1
    assert not s.check_login_token(token)
    assert not s.check_login_token(None)


def test_nothing_touches_disk_until_a_key_is_needed(tmp_path):
    s = webtls.SessionSigner(str(tmp_path / "web"))
    assert s.verify(None) is None
    assert not (tmp_path / "web").exists()


# -- throttle ---------------------------------------------------------------------

def test_throttle_backs_off_then_forgets():
    clock = _Clock(100.0)
    t = webtls.LoginThrottle(free=5, window=600, cap=60, clock=clock)
    for _ in range(5):
        assert t.wait_seconds("192.0.2.9") == 0
        t.fail("192.0.2.9")
    assert t.wait_seconds("192.0.2.9") == 2
    t.fail("192.0.2.9")
    assert t.wait_seconds("192.0.2.9") == 4
    assert t.wait_seconds("198.51.100.4") == 0         # per address
    for _ in range(20):
        t.fail("192.0.2.9")
    assert t.wait_seconds("192.0.2.9") == 60           # capped
    clock.now += 601
    assert t.wait_seconds("192.0.2.9") == 0            # the window passed
    t.fail("192.0.2.9")
    t.success("192.0.2.9")
    assert t.wait_seconds("192.0.2.9") == 0
