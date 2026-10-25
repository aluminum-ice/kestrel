"""The UI element model (Phase 3.5): per-plugin namespacing, the typed element
shape with render hints, and source-compatibility with the Phase 3 stub."""

from kestrel_pi.ui import StubUI


def test_elements_are_grouped_by_plugin_and_source_compatible():
    ui = StubUI()
    a = ui.for_plugin("memtemp")
    a.add_element("mem", "-")                 # old positional form still works
    a.set("mem", "42%")
    a.add_element("cpu", "0.5", label="CPU load", kind="number")
    b = ui.for_plugin("session-stats")
    b.add_element("mem", "ignored")           # same name, other plugin: no clash

    widgets = ui.widgets()
    assert set(widgets) == {"memtemp", "session-stats"}
    assert widgets["memtemp"][0] == {
        "name": "mem", "label": "mem", "value": "42%", "kind": "text"}
    cpu = widgets["memtemp"][1]
    assert cpu["label"] == "CPU load" and cpu["kind"] == "number"
    # insertion order is preserved within a plugin's card
    assert [e["name"] for e in widgets["memtemp"]] == ["mem", "cpu"]
    assert widgets["session-stats"][0]["value"] == "ignored"


def test_unknown_kind_falls_back_to_text():
    ui = StubUI()
    ui.for_plugin("p").add_element("x", "1", kind="bogus")
    assert ui.widgets()["p"][0]["kind"] == "text"


def test_add_element_carries_a_mark_and_set_preserves_it():
    ui = StubUI()
    view = ui.for_plugin("p")
    view.add_element("g", "42%", label="Charge", kind="gauge", mark=5)
    el = ui.widgets()["p"][0]
    assert el["kind"] == "gauge" and el["mark"] == 5
    view.set("g", "50%")                       # an update must not drop the mark
    el = ui.widgets()["p"][0]
    assert el["value"] == "50%" and el["mark"] == 5


def test_add_element_without_mark_has_no_mark_key():
    ui = StubUI()
    ui.for_plugin("p").add_element("t", "x")
    assert "mark" not in ui.widgets()["p"][0]


def test_set_before_add_creates_a_text_element():
    ui = StubUI()
    ui.for_plugin("p").set("late", "v")
    assert ui.widgets()["p"][0] == {
        "name": "late", "label": "late", "value": "v", "kind": "text"}


def test_elements_flat_is_backcompat_last_writer_wins():
    # The flat {name: value} map (status JSON "ui") keeps the Phase 3 shape; a
    # name used by two plugins collapses to the last writer, as it did then.
    ui = StubUI()
    ui.for_plugin("a").add_element("x", "1")
    ui.for_plugin("b").add_element("x", "2")
    assert ui.elements() == {"x": "2"}


def test_get_is_namespaced_to_the_plugin():
    ui = StubUI()
    ui.for_plugin("a").add_element("x", "1")
    assert ui.for_plugin("a").get("x") == "1"
    assert ui.for_plugin("b").get("x", "none") == "none"
