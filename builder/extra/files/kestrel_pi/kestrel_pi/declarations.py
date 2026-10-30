"""Static plugin declarations: what a plugin file declares, read without running it.

A plugin's declarations (``__capabilities__``, ``__options__``, ...) are read by
root - the ``kestrel plugins`` CLI under sudo and the web page's ``plugins-ctl
apply`` socket helper - which must never import plugin code or this pi-writable
package. So the declarations must be readable with :mod:`ast` alone: plain
literals in the body of a class that subclasses ``Plugin`` directly.

The loader enforces that contract (:func:`check_classes`), so every plugin the
daemon loads is one root can read. ``read_declarations`` here is a mirror of the
one in ``plugins-ctl`` (which can't import this package); a drift test keeps the
two identical. Change both together.
"""

from __future__ import annotations

import ast
import os

# Declaration attribute -> (key, default). The defaults mirror the Plugin base
# class, so an undeclared attribute reads the same statically as on the class.
DECLARED = {
    "__plugin_name__": ("name", None),
    "__description__": ("description", ""),
    "__version__": ("version", "0.0.0"),
    "__target_api__": ("target_api", 1),
    "__capabilities__": ("capabilities", ()),
    "__options__": ("options", {}),
    "__provides__": ("provides", ()),
    "__requires__": ("requires", ()),
    "__optional_requires__": ("optional_requires", ()),
}
_TUPLES = ("capabilities", "provides", "requires", "optional_requires")


class DeclarationError(ValueError):
    """A plugin file whose declarations can't be read statically."""


# The option types that may declare ``choices``: a fixed set of values for the
# operator to pick from (a dropdown on the Plugins page).
_CHOICE_TYPES = {"str": str, "int": int}


def _check_choices(where, opt, spec):
    """An option's ``choices``: a non-empty list of values of the option's type
    (a str choice non-empty, with no comma or tab, which the CLI uses to list
    them), with the default among them. Raises DeclarationError otherwise."""
    if "choices" not in spec:
        return
    type_name = spec.get("type", "str")
    kind = _CHOICE_TYPES.get(type_name)
    if kind is None:
        raise DeclarationError("%s: option %r: only str and int options take choices"
                               % (where, opt))
    choices = spec["choices"]
    if not isinstance(choices, (list, tuple)) or not choices or not all(
            isinstance(c, kind) and not isinstance(c, bool)
            and (kind is not str or (c and not set(c) & set(",\t\r\n")))
            for c in choices):
        raise DeclarationError("%s: option %r: choices must be a non-empty list of "
                               "%s values" % (where, opt, type_name))
    if "default" in spec and spec["default"] not in choices:
        raise DeclarationError("%s: option %r: the default is not one of its choices"
                               % (where, opt))


def _base_name(node):
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def normalize(raw, where):
    """{key: value} from the raw declared attributes, defaults filled in and the
    shapes checked. Raises DeclarationError for a malformed declaration."""
    decl = {}
    for attr, (key, default) in DECLARED.items():
        value = raw.get(attr, default)
        if key in _TUPLES:
            if value is None:
                value = ()
            if not isinstance(value, (tuple, list)) or not all(
                    isinstance(item, str) for item in value):
                raise DeclarationError("%s: %s is not a list of strings" % (where, attr))
            value = tuple(value)
        elif key == "options":
            value = value or {}
            if not isinstance(value, dict) or not all(
                    isinstance(spec, dict) or spec is None for spec in value.values()):
                raise DeclarationError("%s: __options__ is not a dict of option specs" % where)
            for opt, spec in value.items():
                _check_choices(where, opt, spec or {})
        decl[key] = value
    return decl


def read_declarations(path):
    """{name: declarations} for the classes in ``path`` that subclass ``Plugin``
    directly, read statically: the file is parsed, never imported. Raises
    DeclarationError for a declaration that isn't a plain literal."""
    with open(path, "r", encoding="utf-8") as handle:
        try:
            tree = ast.parse(handle.read(), filename=path)
        except SyntaxError as exc:
            raise DeclarationError("%s: %s" % (path, exc)) from None
    file_default = os.path.splitext(os.path.basename(path))[0]
    found = {}
    for node in tree.body:
        if not isinstance(node, ast.ClassDef):
            continue
        if "Plugin" not in [_base_name(base) for base in node.bases]:
            continue
        raw = {}
        for stmt in node.body:
            if isinstance(stmt, ast.Assign):
                targets = stmt.targets
            elif isinstance(stmt, ast.AnnAssign) and stmt.value is not None:
                targets = [stmt.target]
            else:
                continue
            for target in targets:
                if isinstance(target, ast.Name) and target.id in DECLARED:
                    try:
                        raw[target.id] = ast.literal_eval(stmt.value)
                    except (ValueError, TypeError, SyntaxError, MemoryError,
                            RecursionError):
                        raise DeclarationError("%s: %s.%s is not a plain literal"
                                               % (path, node.name, target.id)) from None
        decl = normalize(raw, "%s: %s" % (path, node.name))
        name = decl["name"] or file_default
        if not isinstance(name, str):
            raise DeclarationError("%s: %s.__plugin_name__ is not a string" % (path, node.name))
        decl["name"] = name
        found[name] = decl
    return found


def class_declarations(cls, name):
    """The same shape as :func:`read_declarations` gives, from an imported class."""
    raw = {attr: getattr(cls, attr) for attr in DECLARED if hasattr(cls, attr)}
    decl = normalize(raw, name)
    decl["name"] = name
    return decl


def check_classes(path, declared, classes):
    """Check the imported ``classes`` (``[(name, cls)]``) match what ``path``
    declares statically. A mismatch means root would see a different plugin
    than the daemon runs - a class reached through an intermediate base, or a
    declaration changed after the class body - so it is refused."""
    names = sorted(name for name, _cls in classes)
    if names != sorted(declared):
        raise DeclarationError(
            "%s: plugin classes must subclass Plugin directly (found %s, "
            "statically readable %s)" % (path, ", ".join(names) or "none",
                                         ", ".join(sorted(declared)) or "none"))
    for name, cls in classes:
        if class_declarations(cls, name) != declared[name]:
            raise DeclarationError(
                "%s: %s's declarations differ from the literals in its class "
                "body; declare them as plain literals only" % (path, name))
