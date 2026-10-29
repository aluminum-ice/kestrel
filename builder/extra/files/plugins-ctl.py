#!/usr/bin/env python3
"""Plugin config/introspection helper for the `kestrel plugins` tool.

Run by the system Python in isolated mode (`/usr/bin/python3 -I`), never the
pi-owned venv: the CLI runs it as root under sudo. It reads the shipped and
operator plugin files' declarations statically (ast, never importing the plugin
or kestrel_pi) and reads/edits the `[plugins.<name>]` tables in config.toml. The TOML writer is a line editor that
preserves the rest of the file (tomllib is read-only); it only ever touches the
named plugin's table.

Usage:
  plugins-ctl list                     name<TAB>enabled<TAB>source, per plugin
  plugins-ctl info <name>              human-readable declarations + config
  plugins-ctl exists <name>            exit 0 if a plugin file of that name exists
  plugins-ctl caps <name>              declared capabilities, one per line
  plugins-ctl missing <name>           required options with no value set
  plugins-ctl schema <name>            TSV of options for the interactive enable
  plugins-ctl set <name> <key> <val>   set a key in [plugins.<name>]
  plugins-ctl set-stdin <name> <key>   the same, value read from stdin (one
                                       line), so a secret is never an argument
  plugins-ctl apply                    one JSON plugin change on stdin, a JSON
                                       reply on stdout (the root side of the
                                       web page's plugin controls)
  plugins-ctl checkauth                verify a web login against the device
                                       account: {"username","password"} JSON on
                                       stdin, {"ok": bool} on stdout

`apply` and `checkauth` run as root (kestrel-plugins-apply@.service and
kestrel-web-auth@.service, one instance per connection on sockets only
the pi group can reach) under the system Python in isolated mode. No subcommand
imports kestrel_pi or a plugin: declarations are read with ast, so nothing
the pi user can write is executed as root. `checkauth` reads /etc/shadow (root
only, which is why web-login verification lives here) and never logs the
password.
"""
import ast
import hmac
import json
import math
import os
import re
import subprocess
import sys
import time

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - needs Python 3.11+
    tomllib = None

CONFIG = os.environ.get("PWN_CONFIG", "/etc/kestrel/config.toml")
PLUGIN_PATHS = os.environ.get(
    "PWN_PLUGIN_PATHS",
    "/usr/local/lib/kestrel/plugins:/etc/kestrel/plugins",
).split(":")
# The same authorized-use acknowledgment the CLI records for capture and for
# enabling a device-leaving (network) plugin.
ACK_FILE = os.environ.get("PWN_CAPTURE_ACK_FILE", "/etc/kestrel/capture-ack")
DAEMON_SERVICE = "kestrel-daemon.service"
# apply restarts the daemon from a transient timer a moment later, so the
# daemon can finish answering the browser before it is restarted.
RESTART_CMD = ["systemd-run", "--quiet", "--collect", "--on-active=2",
               "systemctl", "restart", DAEMON_SERVICE]
# Reboot/poweroff run the same way (a transient timer a moment later) so the
# reply reaches the browser before the device goes down. Driven from the web
# power buttons and from a plugin that asks for a low-battery shutdown.
POWER_CMDS = {
    "reboot": ["systemctl", "reboot"],
    "shutdown": ["systemctl", "poweroff"],
}
# The shipped CLI (root-owned, trusted - not plugin code) drives capture
# enable/disable, so the web path runs the exact same flow as
# `kestrel capture ...` (adapter detection, config write, service enable).
# Overridable for tests.
PWN_TOOL = os.environ.get("PWN_CTL_TOOL", "/usr/local/bin/kestrel")
MAX_REQUEST = 64 * 1024
MAX_STRING = 1024

# The learning agent's enums. The [agent] algorithm selects a Python class to
# import, so a value from the web page is an input-validation boundary: it is
# checked against these fixed allow-lists before anything is written or the
# agent service is (re)started - the same discipline this helper applies to
# plugin declarations. Kept in step with kestrel_pi.agent.policies
# (ALGORITHMS / POLICIES); this helper never imports that package.
AGENT_ALGORITHMS = ("RecurrentPPO", "PPO", "DQN", "QR-DQN")
AGENT_POLICIES = ("baseline", "learned")

# The allow-list for the web Settings page's one-key config.toml edits: every
# "table.key" an operator may set from the form, with the validator spec checked
# here before anything is written (type, and min/max/choices/maxlen). This is
# the authoritative gate; the form's inline hints only mirror it. It MUST equal
# kestrel_pi.config_catalog.EDITABLE (the display catalog the daemon renders
# from) - this helper can't import that package (it runs as root against a
# pi-writable venv), so a drift test keeps the two identical. Keep both in step.
CONFIG_SETTABLE = {
    # capture
    "capture.interface": {"type": "str", "maxlen": 32},
    "capture.scan.mode": {"type": "enum", "choices": ["sweep", "autohunt", "fixed"]},
    "capture.scan.dwell_seconds": {"type": "int", "min": 1},
    "capture.scan.bands": {"type": "str", "maxlen": 64},
    "capture.output.mode": {
        "type": "enum", "choices": ["per-ssid", "per-ap", "single"]},
    "capture.output.single_file": {"type": "str", "maxlen": 128,
                                   "pattern": "[A-Za-z0-9_-][A-Za-z0-9._-]*"},
    "capture.angryoxide.headless": {"type": "bool"},
    "capture.housekeeping.action": {"type": "enum", "choices": ["off", "rotate", "archive"]},
    "capture.housekeeping.max_store_mb": {"type": "int", "min": 0},
    "capture.housekeeping.max_age_days": {"type": "int", "min": 0},
    "capture.housekeeping.archive_dir": {"type": "str", "maxlen": 256,
                                         "under": "/var/lib/kestrel/capture"},
    "capture.housekeeping.keep_archives": {"type": "int", "min": 0},
    "capture.housekeeping.max_run_mb": {"type": "int", "min": 0},
    "capture.environment.enabled": {"type": "bool"},
    "capture.environment.window_s": {"type": "int", "min": 1},
    "capture.environment.max_age_s": {"type": "int", "min": 1},
    # agent
    "agent.decision_seconds": {"type": "int", "min": 1},
    "agent.log_episodes": {"type": "bool"},
    "agent.train": {"type": "enum", "choices": ["auto", "off", "idle"]},
    "agent.train_while_capturing": {"type": "enum", "choices": ["auto", "true", "false"]},
    "agent.train_min_new_episodes": {"type": "int", "min": 1},
    "agent.train_timesteps": {"type": "int", "min": 1},
    "agent.train_max_temp_c": {"type": "int", "min": 1, "max": 85},
    "agent.train_min_free_mb": {"type": "int", "min": 1},
    "agent.interleave_compare": {"type": "bool"},
    "agent.compare_block_minutes": {"type": "int", "min": 1},
    "agent.metrics_window_minutes": {"type": "int", "min": 1},
    "agent.log_max_mb": {"type": "int", "min": 1},
    "agent.action.lock_subset_n": {"type": "int", "min": 1},
    "agent.reward.per_handshake": {"type": "float", "min": 0},
    "agent.reward.time_penalty": {"type": "float", "min": 0},
    "agent.reward.idle_penalty": {"type": "float", "min": 0},
    # daemon and web
    "daemon.epoch_seconds": {"type": "int", "min": 1},
    "daemon.max_hook_seconds": {"type": "int", "min": 1},
    "daemon.hook_workers": {"type": "int", "min": 1},
    "daemon.capture_poll_seconds": {"type": "int", "min": 1},
    "daemon.connectivity_poll_seconds": {"type": "int", "min": 1},
    "web.refresh_seconds": {"type": "int", "min": 0},
    "web.show_passphrases": {"type": "bool"},
    "web.show_locations": {"type": "bool"},
    "web.probe_url": {"type": "str", "maxlen": 256},
    # recovered networks (import caps; read live, so no restart)
    "recovered.max_import_mb": {"type": "int", "min": 1},
    "recovered.max_import_lines": {"type": "int", "min": 1},
}

# [web] keys the dashboard reads live on each render, so a change takes effect
# without bouncing the plugin host (unlike every other [web]/[daemon] key).
_WEB_LIVE_KEYS = frozenset({"show_passphrases", "show_locations"})


def load_config():
    if tomllib is None:
        return {}
    try:
        with open(CONFIG, "rb") as handle:
            return tomllib.load(handle)
    except FileNotFoundError:
        return {}
    except (OSError, tomllib.TOMLDecodeError) as exc:
        sys.stderr.write("plugins-ctl: %s: %s\n" % (CONFIG, exc))
        return {}


def plugin_table(config, name):
    return (config.get("plugins", {}) or {}).get(name, {}) or {}


def find_plugin(name):
    """The named plugin's static declarations, or None (with the reason on
    stderr) if there is no such plugin or its declarations can't be read."""
    decl, source = static_plugins().get(name, (None, None))
    if source is None:
        sys.stderr.write("no such plugin: %s\n" % name)
    elif decl is None:
        sys.stderr.write("plugin %s (%s) has declarations that aren't plain "
                         "literals; the daemon refuses it too\n" % (name, source))
    return decl


def cmd_list():
    config = load_config()
    for name, (decl, source) in sorted(static_plugins().items()):
        if decl is None:
            state = "invalid"
        elif plugin_table(config, name).get("enabled", False):
            state = "enabled"
        else:
            state = "disabled"
        print("%s\t%s\t%s" % (name, state, source))
    return 0


def cmd_exists(name):
    """Exit 0 if a plugin file of this name exists, readable or not (so an
    invalid one can still be disabled)."""
    return 0 if name in static_plugins() else 1


def cmd_caps(name):
    decl = find_plugin(name)
    if decl is None:
        return 1
    for cap in decl["capabilities"]:
        print(cap)
    return 0


def cmd_missing(name):
    decl = find_plugin(name)
    if decl is None:
        return 1
    table = plugin_table(load_config(), name)
    for opt, spec in decl["options"].items():
        spec = spec or {}
        if spec.get("required") and "default" not in spec and opt not in table:
            print(opt)
    return 0


def _render(value):
    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None:
        return ""
    return str(value)


def cmd_schema(name):
    """Emit one TSV row per declared option for the interactive enable flow:
    name<TAB>type<TAB>required(0/1)<TAB>secret(0/1)<TAB>has_value(0/1)<TAB>current

    current is the config value or the default, rendered as a string; it is
    always empty for a secret option (a stored secret is never echoed), while
    has_value still reflects whether one is set."""
    decl = find_plugin(name)
    if decl is None:
        return 1
    table = plugin_table(load_config(), name)
    for opt, spec in decl["options"].items():
        spec = spec or {}
        required = "1" if spec.get("required") else "0"
        secret = "1" if spec.get("secret") else "0"
        has_value = "1" if opt in table else "0"
        if secret == "1":
            current = ""
        elif opt in table:
            current = _render(table[opt])
        elif "default" in spec:
            current = _render(spec["default"])
        else:
            current = ""
        print("\t".join([opt, spec.get("type", "str"), required, secret,
                         has_value, current]))
    return 0


def cmd_info(name):
    decl = find_plugin(name)
    if decl is None:
        return 1
    source = static_plugins()[name][1]
    table = plugin_table(load_config(), name)
    print("name:          %s" % name)
    print("source:        %s" % source)
    print("enabled:       %s" % bool(table.get("enabled", False)))
    print("description:   %s" % (decl["description"] or "-"))
    print("version:       %s" % decl["version"])
    print("target_api:    %s" % decl["target_api"])
    print("capabilities:  %s" % (", ".join(decl["capabilities"]) or "-"))
    print("provides:      %s" % (", ".join(decl["provides"]) or "-"))
    print("requires:      %s" % (", ".join(decl["requires"]) or "-"))
    print("optional:      %s" % (", ".join(decl["optional_requires"]) or "-"))
    opts = decl["options"]
    if opts:
        print("options:")
        for opt, spec in opts.items():
            spec = spec or {}
            flags = []
            if spec.get("required"):
                flags.append("required")
            if spec.get("root_only"):
                flags.append("terminal only")
            if "default" in spec:
                flags.append("default=%r" % spec["default"])
            cur = table.get(opt)
            if cur is not None:
                flags.append("set")
            print("  %-16s %s %s" % (opt, spec.get("type", "?"),
                                     "(" + ", ".join(flags) + ")" if flags else ""))
    return 0


_CONFIG_SET = None


def config_set_module():
    """The shared table-scoped config writer (config-set, installed next to
    this file; config-set.py in the source tree). Loaded by path so the CLI,
    the web-apply helper and the capture tool all write config.toml the same
    way."""
    global _CONFIG_SET
    if _CONFIG_SET is None:
        import importlib.machinery
        import importlib.util

        here = os.path.dirname(os.path.abspath(__file__))
        for fname in ("config-set", "config-set.py"):
            path = os.path.join(here, fname)
            if os.path.isfile(path):
                loader = importlib.machinery.SourceFileLoader("pwn_config_set", path)
                spec = importlib.util.spec_from_loader("pwn_config_set", loader)
                module = importlib.util.module_from_spec(spec)
                loader.exec_module(module)
                _CONFIG_SET = module
                break
        else:
            raise ImportError("config-set not found next to %s" % __file__)
    return _CONFIG_SET


def _toml_value(raw):
    """Render a CLI string as a TOML scalar: true/false -> bool, digits -> int,
    everything else -> a quoted string."""
    low = raw.lower()
    if low in ("true", "false"):
        return low
    if re.fullmatch(r"-?\d+", raw):
        return raw
    return config_set_module().toml_string(raw)


def cmd_set(name, key, raw):
    if not re.fullmatch(r"[A-Za-z0-9_-]+", name):
        sys.stderr.write("invalid plugin name: %s\n" % name)
        return 2
    if not re.fullmatch(r"[A-Za-z0-9_-]+", key):
        sys.stderr.write("invalid option key: %s\n" % key)
        return 2
    cs = config_set_module()
    try:
        cs.set_value(CONFIG, "plugins." + name, key, _toml_value(raw))
    except (OSError, cs.ConfigSetError) as exc:
        sys.stderr.write("plugins-ctl: %s\n" % exc)
        return 1
    return 0


def cmd_set_stdin(name, key):
    """``cmd_set`` with the value read from one line of stdin, so a secret
    never shows in this process's arguments (visible to every local user)."""
    raw = sys.stdin.readline()
    if raw.endswith("\n"):
        raw = raw[:-1]
    if raw.endswith("\r"):
        raw = raw[:-1]
    return cmd_set(name, key, raw)


# -- static plugin declarations ---------------------------------------------
# Every subcommand reads plugin declarations with ast and never imports the
# plugin: this helper runs as root. This is a mirror of read_declarations in
# kestrel_pi/declarations.py (which this helper can't import: it is in the
# pi-writable venv); the daemon's loader refuses any plugin this reader can't
# read, and a drift test keeps the two identical. Change both together.

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


def static_plugins():
    """{name: (declarations or None, source)} across the search paths, read
    statically; a later path overrides an earlier one, as in the loader. A file
    whose declarations can't be read statically maps to None."""
    found = {}
    for directory in PLUGIN_PATHS:
        if not os.path.isdir(directory):
            continue
        for fname in sorted(os.listdir(directory)):
            if not fname.endswith(".py") or fname.startswith("_"):
                continue
            source = os.path.join(directory, fname)
            try:
                for name, decl in read_declarations(source).items():
                    found[name] = (decl, source)
            except (OSError, UnicodeDecodeError, DeclarationError) as exc:
                found[os.path.splitext(fname)[0]] = (None, source)
                sys.stderr.write("plugins-ctl: %s\n" % exc)
    return found


# -- apply: the root side of the web plugin controls ------------------------
def web_control_ready(config):
    """Plugin changes from the web page need web auth on, with its credential
    set. Checked here as well as in the daemon, so the root side enforces it."""
    web = config.get("web", {}) or {}
    mode = str(web.get("auth") or "none").lower()
    if mode == "system":
        # The page authenticates against the device login account; the login
        # is verified live (checkauth), so there is no stored credential here.
        return True
    if mode == "basic":
        return bool(web.get("username")) and bool(web.get("password"))
    if mode == "token":
        return bool(web.get("token"))
    return False


def web_literal(opt, spec, value):
    """Check a submitted value against the option's declared type and render
    it as a TOML literal. Raises ValueError with an operator-facing message."""
    type_name = (spec or {}).get("type", "str")
    if type_name == "bool":
        if not isinstance(value, bool):
            raise ValueError("%s must be on or off" % opt)
        return "true" if value else "false"
    if type_name == "int":
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError("%s must be a whole number" % opt)
        return str(value)
    if type_name == "float":
        if isinstance(value, bool) or not isinstance(value, (int, float)) \
                or not math.isfinite(value):
            raise ValueError("%s must be a number" % opt)
        return repr(float(value))
    if type_name == "str":
        if not isinstance(value, str):
            raise ValueError("%s must be text" % opt)
        if len(value) > MAX_STRING:
            raise ValueError("%s is too long" % opt)
        cs = config_set_module()
        try:
            return cs.toml_string(value)
        except cs.ConfigSetError:
            raise ValueError("%s contains control characters" % opt) from None
    raise ValueError("%s (%s) can't be edited from the web page; use the CLI"
                     % (opt, type_name))


def record_ack():
    """Remember the authorized-use acknowledgment, as the CLI does."""
    os.makedirs(os.path.dirname(ACK_FILE), exist_ok=True)
    fd = os.open(ACK_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
    with os.fdopen(fd, "w") as handle:
        handle.write("acknowledged %s\n" % time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))


def _silent_cli_error(what, rc):
    """A message for when the CLI failed with no output of its own, so the web
    page shows something actionable instead of a bare generic string. The empty
    output means the CLI aborted before it could log (e.g. a `set -e` abort), so
    point at the journal where the real cause is recorded."""
    return ("could not change %s (helper exited %d with no message); "
            "see 'journalctl -u kestrel-plugins-apply' and run the same "
            "command by hand to see why" % (what, rc))


# The apply timeouts nest so each layer answers before the one waiting on it
# gives up: this CLI run (40s) < the helper unit's RuntimeMaxSec (50s) < the
# daemon's socket client (applyclient, 55s). A CLI run that overruns comes back
# as an error the page can show, instead of the client timing out on a change
# that then lands anyway. The CLI keeps at least the ~30s it effectively had
# before (the old RuntimeMaxSec), since killing it can leave a change half
# done. The slow targets (capture, agent, config) come from core web routes,
# which have no on_webhook timeout; the plugin-button targets are quick
# fire-and-forget CLI calls well inside a plugin page's 30s on_webhook timeout.
CLI_TIMEOUT = 40


def _run_capture_cli(args):
    """Run the shipped CLI to change capture, capturing its output. The CLI is
    root-owned and trusted (not plugin code), the same trust level as the
    systemctl/systemd-run calls here. Returns (returncode, combined_output)."""
    try:
        proc = subprocess.run(
            [PWN_TOOL] + list(args), capture_output=True, text=True,
            timeout=CLI_TIMEOUT, check=False, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError) as exc:
        return 1, str(exc)
    return proc.returncode, ((proc.stderr or "") + (proc.stdout or "")).strip()


def apply_capture(change):
    """Enable or disable capture from the web page:

      {"target": "capture", "enabled": bool, "ack": bool}

    Enabling needs the authorized-use acknowledgment (the same ack file as the
    CLI and as a network plugin). Once confirmed, the CLI's `capture enable
    --yes` records the ack and does adapter detection, the config write and the
    service enable, so a missing dongle surfaces as its error. No daemon restart:
    the daemon reads capture state live."""
    enabled = change.get("enabled")
    if not isinstance(enabled, bool):
        return {"ok": False, "error": "bad request"}
    if enabled:
        if not os.path.exists(ACK_FILE) and change.get("ack") is not True:
            return {"ok": False, "need_ack": True,
                    "error": "capture transmits to force handshakes: confirm "
                    "authorized use to enable it"}
        rc, out = _run_capture_cli(["capture", "enable", "--yes"])
        verb = "enabled"
    else:
        rc, out = _run_capture_cli(["capture", "disable"])
        verb = "disabled"
    if rc != 0:
        detail = out.splitlines()[-1] if out else _silent_cli_error("capture", rc)
        return {"ok": False, "error": detail}
    sys.stderr.write("plugins-ctl apply: capture %s via web\n" % verb)
    return {"ok": True, "capture": enabled, "restart": False}


def apply_agent(change):
    """Set the learning agent's algorithm/policy and enable/disable it from the
    web page:

      {"target": "agent", "algorithm": <enum>, "policy": <enum>, "enabled": bool}

    Each value is enum-validated here (an unknown algorithm/policy is rejected
    and never written, since the value selects a class to import), then the
    change is applied by shelling to the trusted, root-owned CLI (`kestrel
    agent ...`) so the config write, the capture-enabled precondition and the
    service restart match exactly what the terminal does and the two paths never
    drift. No daemon restart: the daemon reads the agent's state live."""
    # A manual "train now" request: fire-and-forget to the trusted CLI, which
    # drops a request file the running agent picks up. No config change and no
    # restart; it just asks the agent to fine-tune.
    action = change.get("action")
    if action is not None:
        if action != "train":
            return {"ok": False, "error": "bad request"}
        rc, out = _run_capture_cli(["agent", "train-now"])
        if rc != 0:
            detail = out.splitlines()[-1] if out else _silent_cli_error("agent", rc)
            return {"ok": False, "error": detail}
        sys.stderr.write("plugins-ctl apply: agent: train requested via web\n")
        return {"ok": True, "changed": ["train"], "restart": False}

    algorithm = change.get("algorithm")
    policy = change.get("policy")
    enabled = change.get("enabled")
    if algorithm is not None and algorithm not in AGENT_ALGORITHMS:
        return {"ok": False, "error": "unknown algorithm: %s" % algorithm}
    if policy is not None and policy not in AGENT_POLICIES:
        return {"ok": False, "error": "unknown policy: %s" % policy}
    if enabled is not None and not isinstance(enabled, bool):
        return {"ok": False, "error": "bad request"}

    set_args = []
    if algorithm is not None:
        set_args += ["--algorithm", algorithm]
    if policy is not None:
        set_args += ["--policy", policy]
    changed = [k for k in ("algorithm", "policy") if change.get(k) is not None]

    if enabled is True:
        # `agent enable` takes the same flags and applies them as it enables.
        rc, out = _run_capture_cli(["agent", "enable", "--yes"] + set_args)
        if "enabled" not in changed:
            changed.append("enabled")
    else:
        # Apply algorithm/policy first (restarts the agent if it is running),
        # then disable if asked.
        if set_args:
            rc, out = _run_capture_cli(["agent", "set"] + set_args)
            if rc != 0:
                detail = out.splitlines()[-1] if out else _silent_cli_error("agent", rc)
                return {"ok": False, "error": detail}
        if enabled is False:
            rc, out = _run_capture_cli(["agent", "disable"])
            changed.append("enabled")
        elif not set_args:
            return {"ok": True, "changed": [], "restart": False}
        else:
            rc, out = 0, ""
    if rc != 0:
        detail = out.splitlines()[-1] if out else _silent_cli_error("agent", rc)
        return {"ok": False, "error": detail}
    sys.stderr.write("plugins-ctl apply: agent: changed %s via web\n"
                     % ", ".join(changed))
    return {"ok": True, "changed": changed, "restart": False}


def apply_bt_pair(change):
    """Restart Bluetooth pairing from the web page:

      {"target": "bt-pair"}

    Forgets any currently paired phone and reopens the onboarding window by
    shelling to the trusted, root-owned CLI (`kestrel network bt-pair`),
    so the web path does exactly what the terminal does. Gated on web auth by
    apply_change before this runs. No daemon restart - it only touches the
    Bluetooth onboarding service."""
    rc, out = _run_capture_cli(["network", "bt-pair"])
    if rc != 0:
        detail = out.splitlines()[-1] if out else "could not restart pairing"
        return {"ok": False, "error": detail}
    sys.stderr.write("plugins-ctl apply: bt-pair (reopened onboarding) via web\n")
    return {"ok": True, "bt_pair": True, "restart": False}


# Generous upper bound on a whitelisted entry from the web (an ESSID is <=32
# bytes, a MAC 17); just bounds abuse of the free-text field.
WHITELIST_MAX = 64


def apply_capture_whitelist(change):
    """Add or remove one capture-whitelist entry from the web page:

      {"target": "capture-whitelist", "action": "add"|"remove", "entry": str}

    The entry (an ESSID or BSSID that must never be actively attacked) is handed
    to the trusted CLI, which writes the [capture] whitelist array through the
    array-aware config writer, so the web path matches the terminal exactly.
    Gated on web auth by apply_change. No daemon or capture restart: the
    whitelist is read when capture next (re)starts."""
    action = change.get("action")
    entry = change.get("entry")
    if action not in ("add", "remove") or not isinstance(entry, str):
        return {"ok": False, "error": "bad request"}
    entry = entry.strip()
    if not entry:
        return {"ok": False, "error": "enter an AP name or MAC to whitelist"}
    if len(entry) > WHITELIST_MAX:
        return {"ok": False, "error": "that entry is too long"}
    if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in entry):
        return {"ok": False, "error": "that entry has invalid characters"}
    rc, out = _run_capture_cli(["capture", "whitelist", action, entry])
    if rc != 0:
        detail = out.splitlines()[-1] if out else _silent_cli_error("the whitelist", rc)
        return {"ok": False, "error": detail}
    sys.stderr.write("plugins-ctl apply: capture whitelist %s via web\n" % action)
    return {"ok": True, "whitelist": action, "restart": False}


def apply_power(change):
    """Reboot or shut the device down:

      {"target": "power", "action": "reboot" | "shutdown"}

    The daemon runs as `pi` and can't power the device off itself, so the real
    command is deferred to a transient timer (see run_power) a moment after the
    reply, letting it reach the browser (or the plugin that asked) first."""
    action = change.get("action")
    if action not in POWER_CMDS:
        return {"ok": False, "error": "unknown power action"}
    sys.stderr.write("plugins-ctl apply: %s via web\n" % action)
    return {"ok": True, "power": action}


# The release tags the Release workflow cuts (X.Y.Z or X.Y.Z-beta.N).
_UPDATE_TAG_RE = re.compile(r"[0-9]+\.[0-9]+\.[0-9]+(-beta\.[0-9]+)?")


def apply_update(change):
    """Apply an in-place app-layer update from the web page:

      {"target": "update", "action": "apply", "tag": "0.7.0"}

    The daemon runs as `pi`; the real work (clone the release, overwrite the
    installed scripts/package/units, restart services) needs root and is the
    shipped CLI's `update apply`, so it is deferred to a transient timer a
    moment after the reply (see run_update), letting the browser get its answer
    before the daemon restarts under it. ``tag`` is the release the operator
    confirmed; the CLI refuses unless that is still the one it would install,
    and re-verifies the release with the server as root, so nothing here
    trusts the daemon's view."""
    if change.get("action") != "apply":
        return {"ok": False, "error": "bad request"}
    tag = change.get("tag")
    if not isinstance(tag, str) or not _UPDATE_TAG_RE.fullmatch(tag):
        return {"ok": False, "error": "bad request"}
    # Optional "update without a fresh backup": the CLI re-verifies the release
    # either way, so this only skips the pre-update mirror, granting no new
    # privilege. Coerced to a plain bool so the marker is never attacker-shaped.
    no_backup = bool(change.get("no_backup"))
    sys.stderr.write("plugins-ctl apply: update apply %s via web%s\n"
                     % (tag, " (no-backup)" if no_backup else ""))
    return {"ok": True, "update": "apply", "update_tag": tag,
            "update_no_backup": no_backup}


def apply_update_check(change, config):
    """Ask the updates plugin to check for a new release now, from the web page:

      {"target": "update-check", "action": "check-now"}

    Fire-and-forget to the trusted, root-owned CLI (`update check`), which drops
    a trigger file the running plugin picks up on its next epoch - the same path
    the terminal uses. No config change and no daemon restart. A quick pre-flight
    (the updates plugin is enabled) makes the page show a useful error instead of
    a silent no-op. Gated on web auth by apply_change. Additive and read-only: it
    only requests a check, it never applies anything."""
    if change.get("action") != "check-now":
        return {"ok": False, "error": "bad request"}
    if not plugin_table(config, "updates").get("enabled", False):
        return {"ok": False, "error": "enable the updates plugin first"}
    rc, out = _run_capture_cli(["update", "check"])
    if rc != 0:
        detail = out.splitlines()[-1] if out else _silent_cli_error("update", rc)
        return {"ok": False, "error": detail}
    sys.stderr.write("plugins-ctl apply: update check via web\n")
    return {"ok": True, "update_check": True, "restart": False}


def apply_backup_restore(change, config):
    """Restore the capture data and agent model from the remote backup:

      {"target": "backup-restore", "action": "restore"}

    The privileged, possibly slow work (an rsync pull of the whole store, then a
    chown) is the shipped CLI's `backup restore --yes`, deferred to a transient
    timer a moment after the reply (see run_backup_restore) so the browser gets
    its answer first. A quick pre-flight here catches the common
    not-configured/not-enabled cases synchronously so the page shows a useful
    error instead of a silent background no-op. Gated on web auth by
    apply_change. Never deletes anything: a restore only adds/overwrites."""
    if change.get("action") != "restore":
        return {"ok": False, "error": "bad request"}
    table = plugin_table(config, "backup")
    if not table.get("enabled", False):
        return {"ok": False, "error": "enable the backup plugin first"}
    missing = [k for k in ("host", "user", "path")
               if not str(table.get(k) or "").strip()]
    if missing:
        return {"ok": False, "error": "set the backup remote first (%s)"
                % ", ".join(missing)}
    # The passwordless-SSH (key existence) check lives in the daemon (web
    # `_backup_restore_ready`, and the `backup restore` CLI's own check), NOT
    # here: this helper runs under a hardened systemd unit with ProtectHome=yes,
    # so /home is empty to it and os.path.exists("/home/pi/.ssh/...") is always
    # false - checking the key here false-reported "not set up" even when the
    # key was present. The deferred restore still records an error on its state
    # card if the key really is missing.
    sys.stderr.write("plugins-ctl apply: backup restore via web\n")
    return {"ok": True, "backup_restore": True, "restart": False}


def apply_backup(change, config):
    """Start a manual backup now, from the web page:

      {"target": "backup", "action": "now"}

    The privileged, possibly slow work (an rsync/scp push of the whole store) is
    the shipped CLI's `backup now`, deferred to a transient timer a moment after
    the reply (see run_backup_now) so the browser gets its answer first. It is the
    same push the plugin runs automatically, writing the same state file the backup
    card reads, so the dashboard reflects the manual run on the plugin's next epoch.
    A quick pre-flight here (enabled, remote set) makes the page show a useful error
    instead of a silent no-op. The passwordless-SSH (key existence) check lives in
    the daemon and the `backup now` CLI, NOT here: this helper runs under a hardened
    unit with ProtectHome=yes, so /home is empty to it and a key check would always
    false-fail. Gated on web auth by apply_change. Additive, never destructive: a
    backup only copies to the remote."""
    if change.get("action") != "now":
        return {"ok": False, "error": "bad request"}
    table = plugin_table(config, "backup")
    if not table.get("enabled", False):
        return {"ok": False, "error": "enable the backup plugin first"}
    missing = [k for k in ("host", "user", "path")
               if not str(table.get(k) or "").strip()]
    if missing:
        return {"ok": False, "error": "set the backup remote first (%s)"
                % ", ".join(missing)}
    sys.stderr.write("plugins-ctl apply: backup now via web\n")
    return {"ok": True, "backup_now": True, "restart": False}


def apply_remote_train(change, config):
    """Ask the remote-train plugin to reconcile and launch any due training runs
    now, from the web page:

      {"target": "remote-train", "action": "train-now"}

    Fire-and-forget to the trusted, root-owned CLI (`remote-train now`), which
    drops a trigger file the running plugin picks up on its next epoch - the same
    path the terminal uses. No config change and no daemon restart. A quick
    pre-flight here (enabled, host set, key installed) makes the page show a
    useful error instead of a silent no-op. Gated on web auth by apply_change.
    Additive, never destructive: it only requests training."""
    if change.get("action") != "train-now":
        return {"ok": False, "error": "bad request"}
    table = plugin_table(config, "remote-train")
    if not table.get("enabled", False):
        return {"ok": False, "error": "enable the remote-train plugin first"}
    missing = [k for k in ("train_host", "train_user", "train_path")
               if not str(table.get(k) or "").strip()]
    if missing:
        return {"ok": False, "error": "set the training host first (%s)"
                % ", ".join(missing)}
    # The SSH-key existence check lives in the daemon (web `_remote_train_ready`,
    # and the plugin/CLI), NOT here: this helper runs under a hardened systemd
    # unit with ProtectHome=yes, so /home is empty to it and
    # os.path.exists("/home/pi/.ssh/...") is always false - checking the key here
    # false-reported "not set up" even when the key was present.
    rc, out = _run_capture_cli(["remote-train", "now"])
    if rc != 0:
        detail = out.splitlines()[-1] if out else _silent_cli_error("remote-train", rc)
        return {"ok": False, "error": detail}
    sys.stderr.write("plugins-ctl apply: remote-train now via web\n")
    return {"ok": True, "remote_train": True, "restart": False}


def _path_under(value, base):
    """True if ``value`` is an absolute path strictly inside ``base``, with no
    '.' or '..' component. Checked on the text, never resolved: a symlink in a
    pi-owned tree is pi's to plant, and the CLI re-checks before using it."""
    if not value.startswith(base.rstrip("/") + "/"):
        return False
    parts = value.rstrip("/").split("/")
    return "." not in parts and ".." not in parts and len(parts) > len(base.rstrip("/").split("/"))


def _config_literal(spec, value):
    """Validate a submitted config value against its CONFIG_SETTABLE spec and
    render it as a TOML literal. Raises ValueError with an operator-facing
    message. This is the authoritative check - the web form's hints only mirror
    it."""
    vtype = spec["type"]
    if vtype == "bool":
        if not isinstance(value, bool):
            raise ValueError("must be on or off")
        return "true" if value else "false"
    if vtype == "int":
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError("must be a whole number")
        if "min" in spec and value < spec["min"]:
            raise ValueError("must be at least %d" % spec["min"])
        if "max" in spec and value > spec["max"]:
            raise ValueError("must be at most %d" % spec["max"])
        return str(value)
    if vtype == "float":
        if isinstance(value, bool) or not isinstance(value, (int, float)) \
                or not math.isfinite(value):
            raise ValueError("must be a number")
        value = float(value)
        if "min" in spec and value < spec["min"]:
            raise ValueError("must be at least %s" % spec["min"])
        if "max" in spec and value > spec["max"]:
            raise ValueError("must be at most %s" % spec["max"])
        return repr(value)
    if vtype == "enum":
        if not isinstance(value, str) or value not in spec["choices"]:
            raise ValueError("must be one of: %s" % ", ".join(spec["choices"]))
        return config_set_module().toml_string(value)
    if vtype == "str":
        if not isinstance(value, str):
            raise ValueError("must be text")
        if len(value) > spec.get("maxlen", MAX_STRING):
            raise ValueError("is too long")
        if "pattern" in spec and not re.fullmatch(spec["pattern"], value):
            raise ValueError("must be a plain file name (letters, digits, '.', '_', "
                             "'-'; no '/' and no leading '.')")
        if "under" in spec and not _path_under(value, spec["under"]):
            raise ValueError("must be a folder inside %s" % spec["under"])
        cs = config_set_module()
        try:
            return cs.toml_string(value)
        except cs.ConfigSetError:
            raise ValueError("contains control characters") from None
    raise ValueError("can't be edited from the web page")


def _config_restart(table, key=""):
    """What restarting a change to ``table``/``key`` needs: ``(restart_daemon,
    hint)``. A [daemon]/[web] change needs the plugin host restarted (done after
    the reply, like a plugin change); capture/agent changes are picked up when
    those services next (re)start, so the page just says so and nothing is
    bounced. A handful of [web] display keys are read live on each render, so
    they take effect without a restart."""
    if table == "web" and key in _WEB_LIVE_KEYS:
        return False, "Saved."
    if table in ("daemon", "web"):
        return True, "The dashboard will restart to apply this."
    if table == "capture" or table.startswith("capture."):
        return False, "Saved. Capture will use it the next time it (re)starts."
    if table == "agent" or table.startswith("agent."):
        return False, "Saved. The agent will use it the next time it (re)starts."
    return False, "Saved."


def apply_config_set(change):
    """Set one allow-listed config.toml key from the web Settings page:

      {"target": "config-set", "table": ..., "key": ..., "value": ...}

    Only a (table, key) in CONFIG_SETTABLE is accepted; the value is checked
    against its declared type/range here (the authoritative gate), then written
    through the single-key config-set writer - the same guardrail the CLI uses,
    which re-parses and refuses any edit that changes more than the one key.
    Returns a restart flag/hint so the page can show an accurate notice."""
    table = change.get("table")
    key = change.get("key")
    value = change.get("value")
    if not isinstance(table, str) or not isinstance(key, str):
        return {"ok": False, "error": "bad request"}
    spec = CONFIG_SETTABLE.get("%s.%s" % (table, key))
    if spec is None:
        return {"ok": False, "error": "%s.%s can't be edited from the web page; "
                "use the CLI or edit config.toml" % (table, key)}
    try:
        literal = _config_literal(spec, value)
    except ValueError as exc:
        return {"ok": False, "error": "%s.%s %s" % (table, key, exc)}
    cs = config_set_module()
    try:
        cs.set_value(CONFIG, table, key, literal)
    except (OSError, cs.ConfigSetError) as exc:
        return {"ok": False, "error": "could not write the config: %s" % exc}
    restart, hint = _config_restart(table, key)
    sys.stderr.write("plugins-ctl apply: config-set %s.%s via web\n" % (table, key))
    return {"ok": True, "changed": ["%s.%s" % (table, key)],
            "restart": restart, "restart_hint": hint}


def apply_change(change):
    """Validate and apply one change from the web page.

    A plugin change:
      {"plugin": name, "set": {option: value, ...}, "enabled": bool, "ack": bool}
    or a capture change:
      {"target": "capture", "enabled": bool, "ack": bool}  (see apply_capture)

    For a plugin: only declared options of a scalar type, checked against their
    declared type; an empty text value keeps the current one (secrets are never
    echoed back to the form, so empty means "unchanged"). Enabling needs every
    required option set, and a `network` plugin needs the authorized-use
    acknowledgment. Only [plugins.<name>] is written. Returns the reply; a
    "restart" flag asks the caller to restart the daemon after replying."""
    if not isinstance(change, dict):
        return {"ok": False, "error": "bad request"}
    config = load_config()
    # Power (reboot/shutdown) is allowed regardless of web auth. Unlike the plugin
    # and capture controls (which exist only to serve the web page, so they are
    # inert when the page is read-only), a power action is also driven by
    # device-safety automation - the ups-hat plugin's low-battery auto-shutdown -
    # which must work whenever that plugin is active, not only when the web UI is
    # on. The socket is already restricted to the pi group, and the web /power
    # buttons stay gated on web auth at the route, with their own confirm step.
    if change.get("target") == "power":
        return apply_power(change)
    if not web_control_ready(config):
        return {"ok": False, "error": "plugin controls are off: turn on web auth "
                "first (sudo kestrel web auth basic)"}
    if change.get("target") == "capture":
        return apply_capture(change)
    if change.get("target") == "config-set":
        return apply_config_set(change)
    if change.get("target") == "agent":
        return apply_agent(change)
    if change.get("target") == "bt-pair":
        return apply_bt_pair(change)
    if change.get("target") == "capture-whitelist":
        return apply_capture_whitelist(change)
    if change.get("target") == "update":
        return apply_update(change)
    if change.get("target") == "update-check":
        return apply_update_check(change, config)
    if change.get("target") == "backup-restore":
        return apply_backup_restore(change, config)
    if change.get("target") == "backup":
        return apply_backup(change, config)
    if change.get("target") == "remote-train":
        return apply_remote_train(change, config)
    name = change.get("plugin")
    if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", name):
        return {"ok": False, "error": "bad plugin name"}
    plugins = static_plugins()
    decl = plugins.get(name, (None, None))[0]
    if decl is None:
        # Not found, or its declarations aren't plain literals (reading them
        # would mean running the plugin as root; the daemon refuses it too).
        return {"ok": False, "error": "%s can't be managed: no such plugin, or "
                "its declarations aren't plain literals" % name}
    options = decl["options"]
    updates = change.get("set", {})
    enabled = change.get("enabled")
    if not isinstance(updates, dict) or (enabled is not None and not isinstance(enabled, bool)):
        return {"ok": False, "error": "bad request"}

    table = plugin_table(config, name)
    post = dict(table)
    items, changed = [], []
    for opt, value in updates.items():
        if opt not in options:
            return {"ok": False, "error": "unknown option: %s" % opt}
        spec = options[opt] or {}
        # A root_only option is one root acts on (e.g. where updates come
        # from). Anything that can reach this socket runs as pi, so it may
        # never set one; only the CLI (`plugins-ctl set`, run by sudo) can.
        if spec.get("root_only"):
            return {"ok": False, "error": "%s can only be set from the terminal: "
                    "sudo kestrel plugins enable %s --set %s=..." % (opt, name, opt)}
        if value == "" and spec.get("type", "str") == "str":
            continue
        try:
            literal = web_literal(opt, spec, value)
        except ValueError as exc:
            return {"ok": False, "error": str(exc)}
        if opt in table and table[opt] == value and type(table[opt]) is type(value):
            continue
        items.append((opt, literal))
        changed.append(opt)
        post[opt] = value

    if enabled is True:
        missing = [opt for opt, spec in options.items()
                   if (spec or {}).get("required") and "default" not in (spec or {})
                   and post.get(opt) in (None, "")]
        if missing:
            return {"ok": False, "missing": missing,
                    "error": "set %s before enabling %s" % (", ".join(missing), name)}
        if "network" in decl["capabilities"] and not os.path.exists(ACK_FILE):
            if change.get("ack") is not True:
                return {"ok": False, "need_ack": True,
                        "error": "%s sends data off the device: confirm authorized "
                        "use to enable it" % name}
            record_ack()
    if enabled is not None and bool(table.get("enabled", False)) != enabled:
        items.append(("enabled", "true" if enabled else "false"))
        changed.append("enabled")

    if not items:
        return {"ok": True, "changed": [], "restart": False}
    cs = config_set_module()
    try:
        cs.set_many(CONFIG, "plugins." + name, items)
    except (OSError, cs.ConfigSetError) as exc:
        return {"ok": False, "error": "could not write the config: %s" % exc}
    # Audit line in the journal: option names only, never values.
    sys.stderr.write("plugins-ctl apply: %s: changed %s\n" % (name, ", ".join(
        "%s(secret)" % opt if (options.get(opt) or {}).get("secret") else opt
        for opt in changed)))
    return {"ok": True, "changed": changed, "restart": True}


# -- checkauth: verify a web login against the device account ---------------
def verify_system_password(username, password, getspnam=None, crypt_fn=None):
    """True if ``password`` is the system account ``username``'s password.

    Reads the account's hash from /etc/shadow (root-only, which is why this
    runs here and not in the pi daemon) and compares with crypt, in constant
    time. A locked or password-less account (``*``/``!``/empty hash) never
    matches. ``getspnam``/``crypt_fn`` are injectable for tests; the defaults
    use the stdlib ``spwd``/``crypt`` (present on the image's Python 3.11),
    imported lazily so this module still loads where they are absent."""
    if not username or not password:
        return False
    if getspnam is None:
        try:
            import spwd
        except ImportError:
            return False
        getspnam = spwd.getspnam
    try:
        hashed = getspnam(username).sp_pwdp or ""
    except (KeyError, PermissionError, OSError):
        return False
    if not hashed or hashed[0] in "*!" or ":" in hashed:
        return False
    if crypt_fn is None:
        try:
            import crypt
        except ImportError:
            return False
        crypt_fn = crypt.crypt
    try:
        calc = crypt_fn(password, hashed)
    except Exception:  # noqa: BLE001 - a bad/unsupported hash is just "no match"
        return False
    return bool(calc) and hmac.compare_digest(calc, hashed)


def cmd_checkauth():
    """Read {"username","password"} JSON on stdin, reply {"ok": bool}. Never
    logs or echoes the password; the journal records only the username and the
    verdict."""
    raw = sys.stdin.buffer.read(MAX_REQUEST + 1)
    username = None
    if len(raw) > MAX_REQUEST:
        reply = {"ok": False, "error": "request too large"}
    else:
        try:
            req = json.loads(raw.decode("utf-8"))
            username = req.get("username")
            password = req.get("password")
        except (ValueError, AttributeError):
            reply = {"ok": False, "error": "bad request"}
        else:
            if not isinstance(username, str) or not isinstance(password, str):
                reply = {"ok": False, "error": "bad request"}
            else:
                reply = {"ok": verify_system_password(username, password)}
    if "error" not in reply:
        sys.stderr.write("plugins-ctl checkauth: %s: %s\n"
                         % (username, "ok" if reply["ok"] else "denied"))
    sys.stdout.write(json.dumps(reply) + "\n")
    sys.stdout.flush()
    # A denied login is an answer, not a failure: each connection runs as its
    # own kestrel-web-auth@ instance, and a non-zero exit would leave one
    # "failed" unit behind per wrong password. Only a crash exits non-zero.
    return 0


def restart_daemon():
    # Not on our stdout (the connection), so the daemon sees the reply end now.
    try:
        subprocess.run(RESTART_CMD, check=False, timeout=15,
                       stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError) as exc:
        sys.stderr.write("plugins-ctl apply: could not restart %s: %s\n"
                         % (DAEMON_SERVICE, exc))


def run_power(action):
    # Deferred past the reply (same as restart_daemon) so the browser gets its
    # answer before the device reboots or powers off.
    cmd = POWER_CMDS.get(action)
    if not cmd:
        return
    try:
        subprocess.run(["systemd-run", "--quiet", "--collect", "--on-active=2"] + cmd,
                       check=False, timeout=15,
                       stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError) as exc:
        sys.stderr.write("plugins-ctl apply: could not %s: %s\n" % (action, exc))


def run_update(tag, no_backup=False):
    # Deferred past the reply (same as run_power) so the browser gets its answer
    # before the daemon restarts under the in-place update. The shipped CLI does
    # the privileged work: verify and clone the release, exec the clone's apply
    # engine so overwriting the installed CLI/package is safe, restart services.
    # A fixed unit name means a second request while one is pending or running
    # is refused by systemd rather than started alongside (the engine also
    # takes a lock). --no-backup only skips the pre-update mirror; the CLI still
    # re-verifies the release, so it is not a privilege.
    cmd = ["systemd-run", "--quiet", "--collect", "--on-active=2",
           "--unit=kestrel-update",
           PWN_TOOL, "update", "apply", "--yes", "--expect", tag]
    if no_backup:
        cmd.append("--no-backup")
    try:
        subprocess.run(cmd, check=False, timeout=15,
                       stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError) as exc:
        sys.stderr.write("plugins-ctl apply: could not start update: %s\n" % exc)


def run_backup_restore():
    # Deferred past the reply (same as run_power/run_update) so the browser gets
    # its answer before the restore runs. The shipped CLI pulls the data back and
    # fixes ownership; it validates config/key again and logs to the journal.
    try:
        subprocess.run(["systemd-run", "--quiet", "--collect", "--on-active=2",
                        PWN_TOOL, "backup", "restore", "--yes"],
                       check=False, timeout=15,
                       stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError) as exc:
        sys.stderr.write("plugins-ctl apply: could not start backup restore: %s\n" % exc)


def run_backup_now():
    # Deferred past the reply (same as run_backup_restore) so the browser gets its
    # answer before the backup runs. The shipped CLI pushes the store to the remote
    # and writes the state file the backup card reads; it validates config/key again
    # and logs to the journal.
    try:
        subprocess.run(["systemd-run", "--quiet", "--collect", "--on-active=2",
                        PWN_TOOL, "backup", "now"],
                       check=False, timeout=15,
                       stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError) as exc:
        sys.stderr.write("plugins-ctl apply: could not start backup now: %s\n" % exc)


def cmd_apply():
    raw = sys.stdin.buffer.read(MAX_REQUEST + 1)
    if len(raw) > MAX_REQUEST:
        reply = {"ok": False, "error": "request too large"}
    else:
        try:
            change = json.loads(raw.decode("utf-8"))
        except ValueError:
            reply = {"ok": False, "error": "bad request"}
        else:
            try:
                reply = apply_change(change)
            except Exception as exc:  # noqa: BLE001 - always answer the daemon
                sys.stderr.write("plugins-ctl apply failed: %r\n" % exc)
                reply = {"ok": False, "error": "internal error; see journalctl "
                         "-u 'kestrel-plugins-apply@*'"}
    sys.stdout.write(json.dumps(reply) + "\n")
    sys.stdout.flush()
    if reply.get("restart"):
        restart_daemon()
    if reply.get("power"):
        run_power(reply["power"])
    if reply.get("update"):
        run_update(reply["update_tag"], reply.get("update_no_backup"))
    if reply.get("backup_restore"):
        run_backup_restore()
    if reply.get("backup_now"):
        run_backup_now()
    # A refused change is an answer, not a failure (as in cmd_checkauth): a
    # non-zero exit would leave a "failed" kestrel-plugins-apply@ instance.
    return 0


def main(argv):
    if len(argv) < 2:
        sys.stderr.write(__doc__)
        return 2
    cmd = argv[1]
    if cmd == "list":
        return cmd_list()
    if cmd == "info" and len(argv) == 3:
        return cmd_info(argv[2])
    if cmd == "exists" and len(argv) == 3:
        return cmd_exists(argv[2])
    if cmd == "caps" and len(argv) == 3:
        return cmd_caps(argv[2])
    if cmd == "missing" and len(argv) == 3:
        return cmd_missing(argv[2])
    if cmd == "schema" and len(argv) == 3:
        return cmd_schema(argv[2])
    if cmd == "set" and len(argv) == 5:
        return cmd_set(argv[2], argv[3], argv[4])
    if cmd == "set-stdin" and len(argv) == 4:
        return cmd_set_stdin(argv[2], argv[3])
    if cmd == "apply" and len(argv) == 2:
        return cmd_apply()
    if cmd == "checkauth" and len(argv) == 2:
        return cmd_checkauth()
    sys.stderr.write(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
