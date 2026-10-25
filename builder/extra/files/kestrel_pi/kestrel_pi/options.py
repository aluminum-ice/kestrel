"""Options-schema validation (improvement 5).

A plugin declares ``__options__`` as ``{name: spec}`` where ``spec`` is a dict
with any of ``type`` (one of the names below), ``default``, and ``required``.
``validate_options`` checks the plugin's ``[plugins.<name>]`` table against the
schema, applies defaults, coerces where safe, and returns ``(values, warnings)``
so a typo surfaces as a startup warning instead of a silent no-op. A missing
required option (with no default) raises :class:`OptionsError`, which the loader
turns into a disabled-with-reason plugin rather than a crash.
"""

from __future__ import annotations

# Map a schema type name to the accepted Python types.
_TYPES = {
    "str": (str,),
    "int": (int,),
    "float": (float, int),
    "bool": (bool,),
    "list": (list,),
    "dict": (dict,),
}


class OptionsError(ValueError):
    """A plugin's options cannot be satisfied (missing required / bad type)."""


def _type_ok(value, type_name):
    accepted = _TYPES.get(type_name)
    if accepted is None:
        # Unknown type name in the schema itself: don't block the plugin on it.
        return True
    # bool is an int subclass in Python; keep them distinct both ways.
    if type_name == "bool":
        return isinstance(value, bool)
    if type_name in ("int", "float") and isinstance(value, bool):
        return False
    return isinstance(value, accepted)


def validate_options(schema, table):
    """Validate ``table`` against ``schema``.

    Returns ``(values, warnings)``. ``values`` always carries ``enabled``
    through untouched (it is framework config, not a plugin option). Raises
    :class:`OptionsError` for a missing required option or a declared option of
    the wrong type.
    """
    schema = schema or {}
    table = dict(table or {})
    warnings = []
    values = {}

    # Apply declared options: defaults, required checks, type checks.
    for name, spec in schema.items():
        spec = spec or {}
        type_name = spec.get("type")
        if name in table:
            value = table[name]
            if type_name and not _type_ok(value, type_name):
                raise OptionsError(
                    "option %r must be of type %s, got %r"
                    % (name, type_name, type(value).__name__)
                )
            values[name] = value
        elif "default" in spec:
            values[name] = spec["default"]
        elif spec.get("required"):
            raise OptionsError("required option %r is not set" % name)
        else:
            values[name] = None

    # Flag unknown keys (everything in the table that is neither a declared
    # option nor the framework's own ``enabled`` flag).
    known = set(schema) | {"enabled"}
    for key in table:
        if key not in known:
            warnings.append("unknown option %r (ignored)" % key)

    return values, warnings
