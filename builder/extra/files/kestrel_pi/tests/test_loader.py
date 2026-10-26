import logging
import time

from kestrel_pi.capture import CaptureInterface
from kestrel_pi.connectivity import ConnectivityState
from kestrel_pi.context import ServiceRegistry
from kestrel_pi.loader import Loader

LOG = logging.getLogger("test.loader")

PLUGINS = {
    "good": (
        "from kestrel_pi import Plugin\n"
        "class Good(Plugin):\n"
        "    __target_api__ = 1\n"
        "    def on_loaded(self):\n"
        "        self.loaded = True\n"
    ),
    "future": (
        "from kestrel_pi import Plugin\n"
        "class Future(Plugin):\n"
        "    __target_api__ = 99\n"
    ),
    "prov": (
        "from kestrel_pi import Plugin\n"
        "class Prov(Plugin):\n"
        "    __provides__ = ('svc',)\n"
        "    def provide_svc(self):\n"
        "        return {'ok': True}\n"
    ),
    "cons": (
        "from kestrel_pi import Plugin\n"
        "class Cons(Plugin):\n"
        "    __requires__ = ('svc',)\n"
    ),
    "optcons": (
        "from kestrel_pi import Plugin\n"
        "class OptCons(Plugin):\n"
        "    __optional_requires__ = ('svc',)\n"
        "    def on_loaded(self):\n"
        "        self.loaded = True\n"
    ),
    "broken": (
        "from kestrel_pi import Plugin\n"
        "class Broken(Plugin):\n"
        "    def on_loaded(self):\n"
        "        raise RuntimeError('fail')\n"
    ),
    "badopts": (
        "from kestrel_pi import Plugin\n"
        "class BadOpts(Plugin):\n"
        "    __options__ = {'key': {'type': 'str', 'required': True}}\n"
    ),
}


def _make_loader(tmp_path, enabled):
    for name, body in PLUGINS.items():
        (tmp_path / (name + ".py")).write_text(body)
    config = {"plugins": {name: {"enabled": name in enabled} for name in PLUGINS}}
    # give any extra per-plugin tables a home
    return Loader(
        config=config,
        capture=CaptureInterface(str(tmp_path)),
        connectivity=ConnectivityState(),
        services=ServiceRegistry(),
        search_paths=[str(tmp_path)],
        logger=LOG,
    )


def test_enabled_loads_disabled_does_not(tmp_path):
    loader = _make_loader(tmp_path, enabled={"good"})
    loaded = loader.load_all()
    assert [r.name for r in loaded] == ["good"]
    assert loader.records["good"].state == "loaded"
    assert loader.records["future"].state == "disabled"
    assert loader.records["future"].reason == "not enabled in config"


def test_incompatible_target_api_disabled_with_reason(tmp_path):
    loader = _make_loader(tmp_path, enabled={"future"})
    loader.load_all()
    rec = loader.records["future"]
    assert rec.state == "disabled"
    assert "hook API" in rec.reason


def test_provides_requires_ordering_and_service(tmp_path):
    loader = _make_loader(tmp_path, enabled={"prov", "cons"})
    loaded = loader.load_all()
    names = [r.name for r in loaded]
    assert names.index("prov") < names.index("cons")   # provider first
    assert loader.services.get("svc") == {"ok": True}


def test_missing_required_service_disabled_with_reason(tmp_path):
    loader = _make_loader(tmp_path, enabled={"cons"})
    loader.load_all()
    rec = loader.records["cons"]
    assert rec.state == "disabled"
    assert "missing required service" in rec.reason


def test_missing_optional_service_still_loads(tmp_path):
    # A soft dependency on an absent provider must not block the load.
    loader = _make_loader(tmp_path, enabled={"optcons"})
    loaded = loader.load_all()
    assert [r.name for r in loaded] == ["optcons"]
    assert loader.records["optcons"].state == "loaded"
    assert not loader.services.has("svc")


def test_optional_service_present_orders_provider_first(tmp_path):
    # When the optional provider IS enabled, it is still ordered ahead.
    loader = _make_loader(tmp_path, enabled={"prov", "optcons"})
    loaded = loader.load_all()
    names = [r.name for r in loaded]
    assert names.index("prov") < names.index("optcons")
    assert loader.services.get("svc") == {"ok": True}


def test_broken_plugin_quarantined_others_survive(tmp_path):
    loader = _make_loader(tmp_path, enabled={"broken", "good"})
    loaded = loader.load_all()
    assert "good" in [r.name for r in loaded]
    assert loader.records["good"].state == "loaded"
    assert loader.records["broken"].state == "failed"
    assert "on_loaded raised" in loader.records["broken"].reason


def test_bad_options_disabled_with_reason(tmp_path):
    loader = _make_loader(tmp_path, enabled={"badopts"})
    loader.load_all()
    rec = loader.records["badopts"]
    assert rec.state == "disabled"
    assert "bad options" in rec.reason


def test_operator_path_overrides_shipped(tmp_path):
    shipped = tmp_path / "shipped"
    operator = tmp_path / "operator"
    shipped.mkdir()
    operator.mkdir()
    (shipped / "good.py").write_text(
        "from kestrel_pi import Plugin\n"
        "class Good(Plugin):\n"
        "    __version__ = 'shipped'\n"
    )
    (operator / "good.py").write_text(
        "from kestrel_pi import Plugin\n"
        "class Good(Plugin):\n"
        "    __version__ = 'operator'\n"
    )
    loader = Loader(
        config={"plugins": {"good": {"enabled": True}}},
        capture=CaptureInterface(str(tmp_path)),
        connectivity=ConnectivityState(),
        services=ServiceRegistry(),
        search_paths=[str(shipped), str(operator)],
        logger=LOG,
    )
    loaded = loader.load_all()
    assert len(loaded) == 1
    assert loaded[0].cls.__version__ == "operator"   # operator wins


# -- declarable row buttons: normalize_actions + health() actions ------------
from kestrel_pi.plugins import Plugin, normalize_actions  # noqa: E402


def test_normalize_actions_drops_malformed_and_defaults_method():
    actions = [
        {"label": "Ok", "path": "p"},                 # method defaults to get
        {"label": "Post", "path": "q", "method": "POST"},  # case-folded
        {"label": "Bad", "path": "r", "method": "delete"},  # -> get
        {"label": "", "path": "x"},                    # empty label -> dropped
        {"label": "NoPath"},                           # missing path -> dropped
        "notadict",                                    # not a dict -> dropped
    ]
    out = normalize_actions(actions)
    # Capped at two, so only the first two survivors.
    assert [a["label"] for a in out] == ["Ok", "Post"]
    assert out[0]["method"] == "get" and out[1]["method"] == "post"


def test_normalize_actions_sorts_by_order_then_declaration():
    out = normalize_actions([
        {"label": "B", "path": "b", "order": 2},
        {"label": "A", "path": "a", "order": 1},
        {"label": "N", "path": "n"},                   # no order -> after ordered
    ])
    # Only two shown; ordered ones come first (A before B).
    assert [a["label"] for a in out] == ["A", "B"]


def test_normalize_actions_order_ignores_booleans_and_keeps_confirm():
    out = normalize_actions([
        {"label": "X", "path": "x", "order": True, "confirm": 1},
    ])
    assert len(out) == 1
    assert out[0]["order"] is None            # bool is not a valid order
    assert out[0]["confirm"] is True          # coerced to bool


def test_normalize_actions_caps_at_two():
    many = [{"label": "L%d" % i, "path": "p%d" % i} for i in range(5)]
    assert len(normalize_actions(many)) == 2


def test_plugin_webhook_actions_defaults_to_declared():
    class P(Plugin):
        __actions__ = ({"label": "Go", "path": "g"},)

    assert P().webhook_actions() == ({"label": "Go", "path": "g"},)
    assert Plugin().webhook_actions() == ()


_ACTION_PLUGIN = (
    "from kestrel_pi import Plugin\n"
    "class Act(Plugin):\n"
    "    __actions__ = ({'label': 'Show', 'path': 'view', 'method': 'get'},)\n"
)
_RAISING_ACTION_PLUGIN = (
    "from kestrel_pi import Plugin\n"
    "class Boom(Plugin):\n"
    "    def webhook_actions(self):\n"
    "        raise RuntimeError('boom')\n"
)


def _loader_for(tmp_path, name, body):
    (tmp_path / (name + ".py")).write_text(body)
    return Loader(
        config={"plugins": {name: {"enabled": True}}},
        capture=CaptureInterface(str(tmp_path)),
        connectivity=ConnectivityState(),
        services=ServiceRegistry(),
        search_paths=[str(tmp_path)],
        logger=LOG,
    )


def test_health_actions_surfaces_declared_buttons(tmp_path):
    loader = _loader_for(tmp_path, "act", _ACTION_PLUGIN)
    loader.load_all()
    health = loader.records["act"].health()
    assert health["actions"] == [
        {"label": "Show", "path": "view", "method": "get",
         "order": None, "confirm": False}]


def test_health_actions_degrade_to_none_when_webhook_actions_raises(tmp_path):
    loader = _loader_for(tmp_path, "boom", _RAISING_ACTION_PLUGIN)
    loader.load_all()
    # The plugin loads; a raising webhook_actions must not break the status page.
    assert loader.records["boom"].state == "loaded"
    assert loader.records["boom"].health()["actions"] == []


def test_health_actions_empty_for_a_disabled_plugin(tmp_path):
    loader = _loader_for(tmp_path, "act", _ACTION_PLUGIN)
    loader.config["plugins"]["act"]["enabled"] = False
    loader.load_all()
    assert loader.records["act"].health()["actions"] == []


def test_unload_all_bounds_a_hung_on_unload(tmp_path):
    # One plugin's on_unload never returns in time (a wedged e-ink BUSY pin);
    # unload_all gives up on it at the shared deadline instead of holding the
    # daemon's stop, still unloads the others, and forgets every record.
    (tmp_path / "hang.py").write_text(
        "import time\n"
        "from kestrel_pi import Plugin\n"
        "class Hang(Plugin):\n"
        "    def on_unload(self):\n"
        "        time.sleep(5)\n"
    )
    (tmp_path / "quick.py").write_text(
        "from kestrel_pi import Plugin\n"
        "class Quick(Plugin):\n"
        "    __provides__ = ('qsvc',)\n"
        "    def provide_qsvc(self):\n"
        "        return 1\n"
        "    def on_unload(self):\n"
        "        self.unloaded = True\n"
    )
    loader = Loader(
        config={"plugins": {"hang": {"enabled": True}, "quick": {"enabled": True}}},
        capture=CaptureInterface(str(tmp_path)),
        connectivity=ConnectivityState(),
        services=ServiceRegistry(),
        search_paths=[str(tmp_path)],
        logger=LOG,
    )
    loader.load_all()
    quick = loader.records["quick"].instance
    started = time.monotonic()
    overran = loader.unload_all(0.3)
    assert time.monotonic() - started < 2
    assert overran == ["hang"]
    assert quick.unloaded is True
    assert all(r.state == "disabled" for r in loader.records.values())
    assert not loader.services.has("qsvc")
