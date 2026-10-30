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


def test_a_value_must_be_one_of_the_choices():
    schema = {"variant": {"type": "str", "default": "v4", "choices": ["v4", "v3", "v2"]},
              "rotation": {"type": "int", "default": 0, "choices": [0, 180]}}
    values, _ = validate_options(schema, {"variant": "v2", "rotation": 180})
    assert values["variant"] == "v2" and values["rotation"] == 180
    assert validate_options(schema, {})[0]["variant"] == "v4"      # the default
    with pytest.raises(OptionsError, match="one of v4, v3, v2"):
        validate_options(schema, {"variant": "V2"})                 # exact match
    with pytest.raises(OptionsError):
        validate_options(schema, {"rotation": 90})
    with pytest.raises(OptionsError):
        validate_options(schema, {"rotation": True})                # a bool is not an int

