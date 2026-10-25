#!/usr/bin/env python3
"""Read one value from the kestrel capture config for the shell scripts.

Usage: config-get <dotted.key> [default]

Prints the value for a dotted key path in /etc/kestrel/config.toml:
booleans as "true"/"false", a list as one item per line, scalars as-is. A
missing key prints the default (empty if none). It never fails the caller: a
missing or unparseable file falls back to the default and warns on stderr, so
the launcher can proceed with its own defaults.
"""
import os
import sys

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python < 3.11 has no tomllib
    tomllib = None

CONFIG = os.environ.get("PWN_CONFIG", "/etc/kestrel/config.toml")


def load():
    if tomllib is None:
        sys.stderr.write("config-get: tomllib unavailable (needs Python 3.11+)\n")
        return {}
    try:
        with open(CONFIG, "rb") as handle:
            return tomllib.load(handle)
    except FileNotFoundError:
        return {}
    except (OSError, tomllib.TOMLDecodeError) as exc:
        sys.stderr.write("config-get: %s: %s\n" % (CONFIG, exc))
        return {}


def emit(value):
    if isinstance(value, bool):
        print("true" if value else "false")
    elif isinstance(value, list):
        for item in value:
            emit(item)
    else:
        print(value)


def main(argv):
    if len(argv) < 2:
        sys.stderr.write("usage: config-get <dotted.key> [default]\n")
        return 2
    key = argv[1]
    default = argv[2] if len(argv) > 2 else ""
    node = load()
    for part in key.split("."):
        if isinstance(node, dict) and part in node:
            node = node[part]
        else:
            print(default)
            return 0
    emit(node)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
