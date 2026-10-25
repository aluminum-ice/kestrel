from kestrel_pi.context import PluginContext, ServiceRegistry


def test_service_registry_provide_get_withdraw():
    reg = ServiceRegistry()
    value = {"lat": 1.0}
    reg.provide("position", lambda: value, owner="gps")
    assert reg.has("position")
    assert reg.get("position") == {"lat": 1.0}
    reg.withdraw_owner("gps")
    assert not reg.has("position")
    assert reg.get("position", default="none") == "none"


def test_service_getter_that_raises_returns_default():
    reg = ServiceRegistry()

    def boom():
        raise RuntimeError("no fix")

    reg.provide("position", boom, owner="gps")
    assert reg.get("position") is None


def test_require_capability_flags_undeclared():
    ctx = PluginContext(
        name="x", options={}, capture=None, connectivity=None,
        services=ServiceRegistry(), capabilities=("capture-read",),
    )
    assert ctx.require_capability("capture-read") is True
    # Undeclared but valid capability: flagged, not raised.
    assert ctx.require_capability("network") is False
    assert "network" in ctx.capability_violations
    # Unknown capability name: rejected.
    assert ctx.require_capability("nonsense") is False


# -- plugin web helpers on the context (Phase 4.8 row buttons) ---------------
from kestrel_pi.plugins import PluginPage, PluginRedirect  # noqa: E402


def _ctx(apply=None):
    return PluginContext(
        name="x", options={}, capture=None, connectivity=None,
        services=ServiceRegistry(), capabilities=(), apply=apply,
    )


def test_context_esc_escapes_html():
    ctx = _ctx()
    assert ctx.esc("a <b> & \"c\"") == "a &lt;b&gt; &amp; &quot;c&quot;"
    assert ctx.esc(None) == ""
    assert ctx.esc(42) == "42"


def test_context_apply_forwards_to_the_channel():
    sent = []
    ctx = _ctx(apply=lambda change: (sent.append(change) or {"ok": True}))
    assert ctx.apply({"target": "x"}) == {"ok": True}
    assert sent == [{"target": "x"}]


def test_context_apply_without_a_channel_fails_closed():
    ctx = _ctx(apply=None)
    reply = ctx.apply({"target": "x"})
    assert reply["ok"] is False and "unavailable" in reply["error"]


def test_context_page_and_redirect_build_markers():
    ctx = _ctx()
    page = ctx.page("Title", "<p>body</p>", status=404)
    assert isinstance(page, PluginPage)
    assert (page.title, page.body, page.status) == ("Title", "<p>body</p>", 404)
    redirect = ctx.redirect("done")
    assert isinstance(redirect, PluginRedirect)
    assert redirect.message == "done" and redirect.error is False
    assert ctx.redirect("bad", error=True).error is True


def test_context_defaults_for_web_fields():
    ctx = _ctx()
    assert ctx.form_csrf == "" and ctx.web_authed is False
