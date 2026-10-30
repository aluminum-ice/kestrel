"""Core stays lean: kestrel_pi holds what the daemon, the agent and the shipped
scripts run, plus a few general services offered to every plugin - never code
that serves particular plugins. A feature belongs in its plugin, even at the
cost of a copy in each plugin that needs it (docs/developer/plugin-development.md,
"Keep plugins standalone")."""

import ast
import os
import re

PKG_ROOT = os.path.normpath(os.path.join(os.path.dirname(__file__), ".."))
PKG = os.path.join(PKG_ROOT, "kestrel_pi")
FILES = os.path.normpath(os.path.join(PKG_ROOT, ".."))

# General services core offers to any plugin. Each is for every plugin that
# needs it (a policy, an API), not one plugin's feature; add to this list only
# with that bar in mind.
PLUGIN_SERVICES = {
    "kestrel_pi.plugins",       # the plugin API: Plugin, load_helper, page markers
    "kestrel_pi.httpsafe",      # how a plugin sends a credential: https only, no redirects
}

# Where core starts: the package's console scripts.
ENTRY_POINTS = {"kestrel_pi.daemon", "kestrel_pi.agent.controller", "kestrel_pi.agent.train"}


def _modules():
    found = {}
    for dirpath, dirnames, filenames in os.walk(PKG):
        dirnames[:] = [d for d in dirnames if d != "__pycache__"]
        for fname in filenames:
            if not fname.endswith(".py"):
                continue
            rel = os.path.relpath(os.path.join(dirpath, fname), PKG_ROOT)[:-3]
            name = rel.replace(os.sep, ".")
            if name.endswith(".__init__"):
                name = name[: -len(".__init__")]
            found[name] = os.path.join(dirpath, fname)
    return found


def _imports(name, path, modules):
    """The kestrel_pi modules ``path`` imports (absolute or relative)."""
    package = name if path.endswith("__init__.py") else name.rpartition(".")[0]
    out = set()
    for node in ast.walk(ast.parse(open(path, encoding="utf-8").read())):
        targets = []
        if isinstance(node, ast.Import):
            targets = [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            base = node.module or ""
            if node.level:
                parts = package.split(".")
                parts = parts[: len(parts) - (node.level - 1)]
                base = ".".join(parts + ([base] if base else []))
            targets = [base] + ["%s.%s" % (base, a.name) for a in node.names]
        for target in targets:
            while target:
                if target in modules:
                    out.add(target)
                target = target.rpartition(".")[0]
    return out


def _script_roots(modules):
    """kestrel_pi modules the shipped scripts and helpers name directly."""
    roots = set()
    for fname in os.listdir(FILES):
        if fname.endswith((".py", ".sh")):
            text = open(os.path.join(FILES, fname), encoding="utf-8").read()
            for match in re.findall(r"kestrel_pi(?:\.[a-z_]+)+", text):
                while match and match not in modules:
                    match = match.rpartition(".")[0]
                if match:
                    roots.add(match)
    return roots


def test_every_core_module_is_used_by_core():
    modules = _modules()
    seen, todo = set(), list(ENTRY_POINTS | _script_roots(modules) | {"kestrel_pi"})
    while todo:
        name = todo.pop()
        if name in seen or name not in modules:
            continue
        seen.add(name)
        todo.extend(_imports(name, modules[name], modules))
    plugin_only = set(modules) - seen - PLUGIN_SERVICES
    assert not plugin_only, (
        "only plugins use %s: move the code into the plugin that needs it "
        "(a copy in each, if several do)" % ", ".join(sorted(plugin_only)))


def test_the_plugin_services_are_still_there():
    modules = _modules()
    assert PLUGIN_SERVICES <= set(modules)
