from kestrel_pi.options import OptionsError, validate_options

import pytest


def test_defaults_applied():
    schema = {"a": {"type": "int", "default": 5}, "b": {"type": "str", "default": "x"}}
    values, warnings = validate_options(schema, {"a": 9})
    assert values == {"a": 9, "b": "x"}
    assert warnings == []


def test_unknown_key_is_warned_not_fatal():
    values, warnings = validate_options({"a": {"type": "int", "default": 1}},
                                        {"a": 2, "enabled": True, "bogus": 1})
    assert values["a"] == 2
    assert any("bogus" in w for w in warnings)


def test_required_missing_raises():
    with pytest.raises(OptionsError):
        validate_options({"key": {"type": "str", "required": True}}, {})


def test_wrong_type_raises():
    with pytest.raises(OptionsError):
        validate_options({"n": {"type": "int"}}, {"n": "not-an-int"})


def test_bool_is_not_int():
    # bool must not satisfy an int option and vice versa.
    with pytest.raises(OptionsError):
        validate_options({"n": {"type": "int"}}, {"n": True})
    values, _ = validate_options({"flag": {"type": "bool"}}, {"flag": True})
    assert values["flag"] is True
