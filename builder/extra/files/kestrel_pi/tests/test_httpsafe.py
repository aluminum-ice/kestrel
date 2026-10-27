"""The HTTP rules plugins use when they send credentials (kestrel_pi.httpsafe)."""

import pytest

from kestrel_pi import httpsafe


class _Resp:
    def __init__(self, status=200, body=b"", headers=None):
        self.status_code = status
        self._body = body
        self.headers = headers or {}
        self.closed = False

    def iter_content(self, chunk_size=1):
        for start in range(0, len(self._body), chunk_size):
            yield self._body[start:start + chunk_size]

    def close(self):
        self.closed = True


@pytest.mark.parametrize("value, default, expected", [
    ("https://api.example.org/", "https://x.example", "https://api.example.org"),
    ("", "https://default.example", "https://default.example"),
    (None, "https://default.example/", "https://default.example"),
    ("  HTTPS://Mixed.Example  ", "", "HTTPS://Mixed.Example"),
])
def test_https_endpoints_pass(value, default, expected):
    assert httpsafe.https_endpoint(value, default) == expected


@pytest.mark.parametrize("value", [
    "http://api.example.org", "ftp://x.example", "api.example.org", "https://", "//x",
])
def test_anything_but_https_is_refused(value):
    with pytest.raises(httpsafe.UnsafeResponse, match="https://"):
        httpsafe.https_endpoint(value, "https://default.example")


@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
def test_redirects_are_refused(status):
    resp = _Resp(status, headers={"Location": "https://elsewhere.example/"})
    with pytest.raises(httpsafe.UnsafeResponse, match="elsewhere.example"):
        httpsafe.check_response(resp)
    assert resp.closed


def test_non_redirects_pass_the_check():
    for status in (200, 204, 401, 404, 500):
        httpsafe.check_response(_Resp(status))


def test_read_capped_reads_up_to_the_limit():
    assert httpsafe.read_capped(_Resp(body=b"x" * 100), limit=100) == b"x" * 100


def test_read_capped_refuses_a_body_over_the_limit():
    resp = _Resp(body=b"x" * 101)
    with pytest.raises(httpsafe.UnsafeResponse, match="limit"):
        httpsafe.read_capped(resp, limit=100)
    assert resp.closed


def test_read_capped_refuses_an_oversized_content_length_before_reading():
    resp = _Resp(body=b"", headers={"Content-Length": str(10 ** 9)})
    with pytest.raises(httpsafe.UnsafeResponse, match="1000000000"):
        httpsafe.read_capped(resp, limit=100)


def test_read_json_parses_and_raises_value_error_on_junk():
    assert httpsafe.read_json(_Resp(body=b'{"success": true}')) == {"success": True}
    with pytest.raises(ValueError):
        httpsafe.read_json(_Resp(body=b"not json"))
