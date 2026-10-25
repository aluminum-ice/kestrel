#!/usr/bin/env python3
"""Set keys in one table of the kestrel config.toml, in place.

Usage:
  config-set <table> <key> <toml-value>      e.g. config-set capture enabled true
  config-set --string <table> <key>          the raw string value is read from
                                             stdin (kept off the command line,
                                             for passwords) and quoted here
  config-set --add-str <table> <key>         read a string from stdin and append
                                             it to the array at table.key (a
                                             no-op if already present)
  config-set --del-str <table> <key>         read a string from stdin and remove
                                             it from the array at table.key

<toml-value> is already TOML-formatted (true/false, a number, or a quoted
string). Only the named table is touched: the key is replaced inside
[<table>] (keeping a trailing comment on a simple value), or inserted right
after the header when absent; a missing table is appended. tomllib is
read-only, so this is a careful line editor rather than a round-trip.

It is table-scoped on purpose: config.toml has the same key in several tables
(`enabled` is in [capture], [daemon], [web] and every [plugins.*]), so a
file-wide `sed s/^enabled = .*/` rewrites all of them. As a guard, every edit
is re-parsed and compared with the original: the result must differ from it
in exactly the one key, or nothing is written.

The replacement keeps the file's mode and owner (config.toml is 0640 root:pi
because plugin options hold operator secrets), via a 0600 temp file, so there
is no window where the new content is world-readable.

Also imported by plugins-ctl, so the CLI and the web control share one writer.
"""
import copy
import json
import os
import re
import stat
import sys
import tempfile

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python < 3.11 has no tomllib
    tomllib = None

CONFIG = os.environ.get("PWN_CONFIG", "/etc/kestrel/config.toml")

_TABLE_RE = re.compile(r"[A-Za-z0-9_-]+(?:\.[A-Za-z0-9_-]+)*")
_KEY_RE = re.compile(r"[A-Za-z0-9_-]+")
# A table header line ([a.b] or [[a.b]]), optionally followed by a comment.
_HEADER_RE = re.compile(r"^\s*\[\[?\s*([A-Za-z0-9_.\-\"' ]+?)\s*\]\]?\s*(?:#.*)?$")
# A simple single-line scalar that can be split safely from a "# comment".
_SIMPLE_VALUE = r'(?:true|false|[+-]?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?|"(?:[^"\\\n]|\\.)*")'


class ConfigSetError(ValueError):
    """The edit is invalid or would change more than the one key."""


def parse_value(literal):
    """Parse one single-line TOML value literal; raise ConfigSetError if not."""
    if tomllib is None:
        raise ConfigSetError("tomllib unavailable (needs Python 3.11+)")
    if "\n" in literal or "\r" in literal:
        raise ConfigSetError("value must be a single line")
    try:
        return tomllib.loads("v = %s\n" % literal)["v"]
    except tomllib.TOMLDecodeError as exc:
        raise ConfigSetError("not a TOML value: %s (%s)" % (literal, exc)) from None


def toml_string(value):
    """Render ``value`` as a TOML basic string. Control characters are refused
    rather than escaped: no config value here legitimately carries them."""
    if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in value):
        raise ConfigSetError("value contains control characters")
    # JSON string escaping is valid TOML once non-ASCII is left as UTF-8
    # (TOML rejects the surrogate-pair \u escapes ensure_ascii would emit).
    return json.dumps(value, ensure_ascii=False)


def _table_span(lines, table):
    """(header_index, end_index) of ``[table]`` in ``lines``, or (None, None)."""
    header_idx = None
    for i, line in enumerate(lines):
        match = _HEADER_RE.match(line)
        if not match:
            continue
        if header_idx is not None:
            return header_idx, i
        name = ".".join(part.strip() for part in match.group(1).split("."))
        if name == table and not line.lstrip().startswith("[["):
            header_idx = i
    if header_idx is None:
        return None, None
    return header_idx, len(lines)


def _set_path(data, table, key, value):
    node = data
    for part in table.split("."):
        node = node.setdefault(part, {})
    node[key] = value


def edit_text(text, table, key, literal):
    """Return ``text`` with ``table.key`` set to the TOML ``literal``."""
    if tomllib is None:
        raise ConfigSetError("tomllib unavailable (needs Python 3.11+)")
    if not _TABLE_RE.fullmatch(table):
        raise ConfigSetError("invalid table name: %s" % table)
    if not _KEY_RE.fullmatch(key):
        raise ConfigSetError("invalid key: %s" % key)
    value = parse_value(literal)
    try:
        before = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigSetError("config is not valid TOML: %s" % exc) from None

    lines = text.splitlines(keepends=True)
    if lines and not lines[-1].endswith("\n"):
        lines[-1] += "\n"
    new_line = "%s = %s\n" % (key, literal)
    header_idx, end = _table_span(lines, table)

    if header_idx is None:
        if lines and lines[-1].strip():
            lines.append("\n")
        lines.append("[%s]\n" % table)
        lines.append(new_line)
    else:
        simple = re.compile(
            r"^(\s*%s\s*=\s*)%s(\s+#.*)?\s*$" % (re.escape(key), _SIMPLE_VALUE)
        )
        any_key = re.compile(r"^\s*%s\s*=" % re.escape(key))
        for i in range(header_idx + 1, end):
            if not any_key.match(lines[i]):
                continue
            match = simple.match(lines[i].rstrip("\n"))
            if match:
                lines[i] = match.group(1) + literal + (match.group(2) or "") + "\n"
            else:
                lines[i] = new_line
            break
        else:
            lines.insert(header_idx + 1, new_line)

    new_text = "".join(lines)
    try:
        after = tomllib.loads(new_text)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigSetError(
            "refusing to set %s.%s: the result would not parse (%s); edit "
            "config.toml by hand" % (table, key, exc)
        ) from None
    expected = copy.deepcopy(before)
    _set_path(expected, table, key, value)
    if after != expected:
        raise ConfigSetError(
            "refusing to set %s.%s: the edit would change more than that key"
            % (table, key)
        )
    return new_text


def write_atomic(path, text):
    """Replace ``path`` with ``text``, keeping its mode and owner, with no
    window where the new content is more readable than the original."""
    st = os.stat(path)
    directory = os.path.dirname(os.path.abspath(path))
    fd, tmp = tempfile.mkstemp(prefix=".%s." % os.path.basename(path), dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.chown(tmp, st.st_uid, st.st_gid)
        except PermissionError:
            pass  # unprivileged (tests): keep our own ownership
        os.chmod(tmp, stat.S_IMODE(st.st_mode))
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _descend(data, parts):
    node = data
    for part in parts:
        if not isinstance(node, dict):
            return None
        node = node.get(part)
    return node


def current_string_array(path, table, key):
    """The current ``table.key`` as a list of strings, or ``[]`` when it is unset
    or not a list (non-string elements are dropped)."""
    if tomllib is None:
        raise ConfigSetError("tomllib unavailable (needs Python 3.11+)")
    with open(path, "rb") as handle:
        data = tomllib.load(handle)
    table_node = _descend(data, table.split("."))
    if not isinstance(table_node, dict):
        return []
    value = table_node.get(key)
    if not isinstance(value, list):
        return []
    return [v for v in value if isinstance(v, str)]


def set_string_array(path, table, key, items):
    """Set ``table.key`` to a single-line TOML array of basic strings. Each item
    is escaped by ``toml_string`` (control characters refused), and the usual
    one-key guard in ``edit_text`` still applies."""
    literal = "[" + ", ".join(toml_string(v) for v in items) + "]"
    set_value(path, table, key, literal)


def set_many(path, table, items):
    """Set each ``(key, toml_literal)`` in ``items`` inside ``[table]`` with one
    atomic write. Every edit is validated; nothing is written on any error."""
    with open(path, "r", encoding="utf-8") as handle:
        text = handle.read()
    for key, literal in items:
        text = edit_text(text, table, key, literal)
    write_atomic(path, text)


def set_value(path, table, key, literal):
    set_many(path, table, [(key, literal)])


def _stdin_value():
    raw = sys.stdin.read()
    if raw.endswith("\n"):
        raw = raw[:-1]
    return raw


def main(argv):
    args = argv[1:]
    mode = args[0] if args and args[0] in (
        "--string", "--add-str", "--del-str") else None
    if mode:
        args = args[1:]
    # --string/--add-str/--del-str take (table, key); a plain set takes a value too.
    if len(args) != (2 if mode else 3):
        sys.stderr.write(__doc__)
        return 2
    table, key = args[0], args[1]
    try:
        if mode in ("--add-str", "--del-str"):
            raw = _stdin_value()
            if not raw:
                raise ConfigSetError("empty value")
            current = current_string_array(CONFIG, table, key)
            if mode == "--add-str":
                if raw in current:
                    return 0                      # already present: a no-op
                toml_string(raw)                  # reject control chars early
                set_string_array(CONFIG, table, key, current + [raw])
            else:
                if raw not in current:
                    return 0                      # not there: a no-op
                set_string_array(CONFIG, table, key,
                                 [v for v in current if v != raw])
            return 0
        if mode == "--string":
            literal = toml_string(_stdin_value())
        else:
            literal = args[2]
        set_value(CONFIG, table, key, literal)
    except (OSError, ConfigSetError) as exc:
        sys.stderr.write("config-set: %s\n" % exc)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
