"""The config.toml settings catalog for the web Settings page.

This is the single source of the editor's surface: the sections and per-setting
metadata the web Settings page renders from (type, default, help, which service
a change needs restarting, whether a value is editable in the form or read-only
with a pointer to the CLI), plus :data:`EDITABLE`, the ``"table.key" ->
validator spec`` allow-list the form submits against.

The root apply helper (``plugins-ctl``) keeps its own copy of that allow-list
and is the authoritative validator: it must not import this package (it runs as
root against a pi-writable venv, the privilege boundary documented in
``plugins-ctl``), so the two are kept identical by a drift test
(``tests/test_config_catalog.py``) — the same mirror-and-test discipline the
agent enums use. Keep both in step when adding a setting.

Pure data, stdlib only, so the daemon imports it with nothing extra and the
test can compare it against the root helper's copy.

Scope follows ``docs/developer/architecture.md`` ("Config writes from the page"): the ``config.toml`` runtime
surface only. ``/etc/default/kestrel`` (network plumbing) is deliberately
out of scope — a wrong value there can drop the management link — and appears as
a read-only note. Build-time ``builder/config.yml`` is not on a running device
at all. List-valued and lockout/secret keys are shown read-only and left to the
CLI in this version; the scalar tunables are what the form edits.
"""
from __future__ import annotations

# Which service must restart for a change to take effect. A change is written
# the same way regardless; this only drives the notice the page shows and, for
# the daemon, whether the apply helper restarts it after replying.
RESTART_NONE = None
RESTART_DAEMON = "daemon"
RESTART_CAPTURE = "capture"
RESTART_AGENT = "agent"

# Reasons a setting is read-only in the form (shown with a pointer, never a
# blank disabled box): it has a dedicated control elsewhere, it is a lockout
# risk best left to the CLI, it is a secret (value never shown), or it is a
# list/advanced value this version leaves to the CLI.
RO_CONTROL = "control"
RO_LOCKOUT = "lockout"
RO_SECRET = "secret"
RO_CLI = "cli"


def _edit(table, key, vtype, default, help, restart, **limits):
    """An editable entry. ``limits`` carries the validator constraints
    (``min``/``max`` for numbers, ``choices`` for an enum, ``maxlen`` for text)
    that both the form hints and the apply-helper allow-list use."""
    entry = {"table": table, "key": key, "type": vtype, "default": default,
             "help": help, "restart": restart, "editable": True}
    entry.update(limits)
    return entry


def _ro(table, key, reason, help, cli="", secret=False):
    """A read-only entry: shown so the operator knows it exists, with a pointer
    to where it is actually changed. ``secret`` rows never show a value."""
    return {"table": table, "key": key, "editable": False, "reason": reason,
            "help": help, "cli": cli, "secret": secret}


# The network-plumbing file, surfaced as a read-only note (never editable here).
ETC_DEFAULT_NOTE = {
    "file": "/etc/default/kestrel",
    "keys": ["BT_ONBOARD_MINUTES", "BT_PAN_* timings", "BT_PAN_STATIC_ADDRESSES",
             "BT_PAN_FALLBACK_DNS", "WIFI_BAND"],
    "why": ("These are low-level network settings. A wrong value here can drop "
            "the very management link this page is served over, so they are not "
            "editable from the web."),
    "cli": ("Change them from a terminal: edit /etc/default/kestrel and "
            "restart the service, or use 'sudo kestrel network wifi "
            "--band <2.4|5|any>' for the Wi-Fi band."),
}


SECTIONS = [
    {
        "title": "Capture",
        "anchor": "capture",
        "intro": "Scan behaviour and the capture store. Changes here take effect "
                 "the next time capture starts.",
        "entries": [
            _ro("capture", "enabled", RO_CONTROL,
                "On/off carries the authorized-use acknowledgment — use the "
                "capture toggle on the dashboard.", cli="/"),
            _edit("capture", "interface", "str", "auto",
                  "The monitor-capable USB dongle. 'auto' picks the non-built-in "
                  "wlan; set a name only if detection picks wrong.",
                  RESTART_CAPTURE, maxlen=32),
            _ro("capture", "whitelist", RO_CONTROL,
                "Networks never actively attacked — managed on the whitelist "
                "page, with validation.", cli="/whitelist"),
            _edit("capture.scan", "mode", "enum", "sweep",
                  "Field scan behaviour before/without the agent. 'fixed' uses "
                  "the bands list. sweep for walking, autohunt when parked.",
                  RESTART_CAPTURE, choices=["sweep", "autohunt", "fixed"]),
            _edit("capture.scan", "dwell_seconds", "int", 5,
                  "Seconds per channel. Lower = faster revisits (moving); higher "
                  "= more time to force a handshake.", RESTART_CAPTURE, min=1),
            _edit("capture.scan", "bands", "str", "auto",
                  "'auto' = every supported channel; a single band (2/5/6/60) or "
                  "a channel list like 1,6,11,36 narrows it. Used when mode=fixed.",
                  RESTART_CAPTURE, maxlen=64),
            _edit("capture.output", "mode", "enum", "per-ssid",
                  "One hc22000 file per network (per-ssid), one file per access "
                  "point keyed on BSSID (per-ap), or one combined file (single). "
                  "All append-only.", RESTART_CAPTURE,
                  choices=["per-ssid", "per-ap", "single"]),
            _edit("capture.output", "single_file", "str", "all.hc22000",
                  "Name of the combined file when mode=single: a plain file "
                  "name in the store (no '/', no leading '.').", RESTART_CAPTURE,
                  maxlen=128, pattern="[A-Za-z0-9_-][A-Za-z0-9._-]*"),
            _edit("capture.angryoxide", "headless", "bool", True,
                  "The service always runs headless; 'off' only affects a by-hand "
                  "terminal launch. Leave on unless debugging in a terminal.",
                  RESTART_CAPTURE),
            _ro("capture.angryoxide", "extra_args", RO_CLI,
                "Verbatim AngryOxide flags (a list). Advanced; a bad flag stops "
                "capture — edit it in config.toml or via the CLI.", cli=""),
            _edit("capture.housekeeping", "action", "enum", "off",
                  "Daily store-maintenance policy. Never deletes (only 'capture "
                  "clear' does). 'rotate' uses the limits below.",
                  RESTART_CAPTURE, choices=["off", "rotate", "archive"]),
            _edit("capture.housekeeping", "max_store_mb", "int", 0,
                  "Store size that triggers a rotate (0 = no limit).",
                  RESTART_CAPTURE, min=0),
            _edit("capture.housekeeping", "max_age_days", "int", 0,
                  "Store age that triggers a rotate (0 = no limit).",
                  RESTART_CAPTURE, min=0),
            _edit("capture.housekeeping", "archive_dir", "str",
                  "/var/lib/kestrel/capture/archive",
                  "Where archives land: a folder inside /var/lib/kestrel/capture "
                  "(housekeeping never writes or deletes anywhere else).",
                  RESTART_CAPTURE, maxlen=256, under="/var/lib/kestrel/capture"),
            _edit("capture.housekeeping", "keep_archives", "int", 0,
                  "Keep only the newest N archive sets (0 = keep all).",
                  RESTART_CAPTURE, min=0),
            _edit("capture", "max_run_mb", "int", 512,
                  "Live cap on the raw .run/ dir (not hashlines); at the cap "
                  "capture rolls and raw files drop. 0 can fill the card on a "
                  "long session.", RESTART_CAPTURE, min=0),
            _edit("capture.environment", "enabled", "bool", True,
                  "Show the in-range readout (distinct radios nearby) on the "
                  "dashboard and e-ink. Only reads while capturing.",
                  RESTART_CAPTURE),
            _edit("capture.environment", "window_s", "int", 300,
                  "Seconds of recent capture data that count as 'recently "
                  "seen' for the readout. Anchored to the newest packet, not "
                  "wall-clock, so it's a density reading, not an instant "
                  "count.", RESTART_CAPTURE, min=1),
            _edit("capture.environment", "max_age_s", "int", 900,
                  "How old the newest capture data may be before the readout "
                  "is shown as 'as of N ago' and, past this, dropped back to "
                  "'unknown'.", RESTART_CAPTURE, min=1),
            _ro("capture.environment", "thresholds", RO_CLI,
                "Radio-count cut-offs for the Quiet/Light/Moderate/Busy/Dense "
                "labels (a list). Edit it in config.toml or via the CLI.",
                cli=""),
        ],
    },
    {
        "title": "Learning agent",
        "anchor": "agent",
        "intro": "Agent tunables. Changes here take effect the next time the "
                 "agent starts.",
        "entries": [
            _ro("agent", "enabled", RO_CONTROL,
                "On/off is on the agent page (it carries the capture "
                "precondition).", cli="/agent"),
            _ro("agent", "algorithm", RO_CONTROL,
                "Picks the RL algorithm — change it on the agent page.",
                cli="/agent"),
            _ro("agent", "policy", RO_CONTROL,
                "baseline vs learned — change it on the agent page ('learned' "
                "needs a trained/imported model).", cli="/agent"),
            _edit("agent", "decision_seconds", "int", 30,
                  "One action per window; each change relaunches AngryOxide, so "
                  "keep it coarse (>= ~20).", RESTART_AGENT, min=1),
            _edit("agent", "log_episodes", "bool", True,
                  "Append episode logs for offline training. Off stops the agent "
                  "learning from new data.", RESTART_AGENT),
            _edit("agent", "train", "enum", "auto",
                  "'auto' trains on >= ~4 GB boards; a smaller board never trains "
                  "regardless. 'idle' = only when idle, 'off' = never.",
                  RESTART_AGENT, choices=["auto", "off", "idle"]),
            _edit("agent", "train_while_capturing", "enum", "auto",
                  "Fine-tune during capture (niced) vs only when the device is "
                  "idle. 'auto' follows what the board can handle.",
                  RESTART_AGENT, choices=["auto", "true", "false"]),
            _edit("agent", "train_min_new_episodes", "int", 60,
                  "New episodes before a periodic fine-tune run.", RESTART_AGENT,
                  min=1),
            _edit("agent", "train_timesteps", "int", 3000,
                  "Steps per fine-tune. Short by design.", RESTART_AGENT, min=1),
            _edit("agent", "train_max_temp_c", "int", 75,
                  "Skip training at/above this CPU temp (hard-capped at 85).",
                  RESTART_AGENT, min=1, max=85),
            _edit("agent", "train_min_free_mb", "int", 512,
                  "Skip training below this free RAM (hard floor 256).",
                  RESTART_AGENT, min=1),
            _edit("agent", "interleave_compare", "bool", False,
                  "A/B benchmark mode: alternate baseline and agent blocks.",
                  RESTART_AGENT),
            _edit("agent", "compare_block_minutes", "int", 10,
                  "A/B block length in minutes.", RESTART_AGENT, min=1),
            _edit("agent", "metrics_window_minutes", "int", 60,
                  "Rolling window for the live agent-vs-sweep metrics.",
                  RESTART_AGENT, min=1),
            _edit("agent", "log_max_mb", "int", 64,
                  "Episode-log cap (bounds the backed-up agent dir).",
                  RESTART_AGENT, min=1),
            _ro("agent", "model", RO_CLI,
                "Learned-weights path — managed by 'agent import' and training.",
                cli=""),
            _edit("agent.action", "lock_subset_n", "int", 3,
                  "How many top channels the lock-subset posture pins.",
                  RESTART_AGENT, min=1),
            _ro("agent.action", "dwell_buckets", RO_CLI,
                "The dwell vocabulary (a list) — advanced; edit via config.toml.",
                cli=""),
            _ro("agent.action", "transmit_modes", RO_CLI,
                "The transmit vocabulary (a list) — advanced; edit via "
                "config.toml.", cli=""),
            _edit("agent.reward", "per_handshake", "float", 1.0,
                  "Reward per new unique handshake.", RESTART_AGENT, min=0),
            _edit("agent.reward", "time_penalty", "float", 0.01,
                  "Small per-window time cost.", RESTART_AGENT, min=0),
            _edit("agent.reward", "idle_penalty", "float", 0.05,
                  "Extra cost for a window that saw no discoverable APs.",
                  RESTART_AGENT, min=0),
        ],
    },
    {
        "title": "Daemon & web",
        "anchor": "daemon",
        "intro": "The plugin host and the dashboard. A change here restarts the "
                 "dashboard to apply it.",
        "entries": [
            _ro("daemon", "enabled", RO_CLI,
                "Disabling the host from the page it serves is a foot-gun — CLI "
                "only.", cli=""),
            _edit("daemon", "epoch_seconds", "int", 60,
                  "on_epoch cadence in seconds.", RESTART_DAEMON, min=1),
            _edit("daemon", "max_hook_seconds", "int", 30,
                  "Soft per-hook deadline in seconds.", RESTART_DAEMON, min=1),
            _edit("daemon", "hook_workers", "int", 4,
                  "Off-loop dispatch pool size.", RESTART_DAEMON, min=1),
            _edit("daemon", "capture_poll_seconds", "int", 5,
                  "How often the capture store is polled.", RESTART_DAEMON, min=1),
            _edit("daemon", "connectivity_poll_seconds", "int", 15,
                  "How often management paths are probed.", RESTART_DAEMON, min=1),
            _ro("web", "enabled", RO_CLI,
                "Same foot-gun as the daemon — CLI only.", cli=""),
            _ro("web", "bind", RO_LOCKOUT,
                "Never bind the capture dongle. Changing the bind can drop your "
                "session — CLI only.", cli=""),
            _ro("web", "port", RO_LOCKOUT,
                "Changing the port drops your session and needs a restart — use "
                "the CLI.", cli=""),
            _edit("web", "refresh_seconds", "int", 30,
                  "Dashboard auto-refresh in seconds (0 = off). Below "
                  "epoch_seconds just re-renders the same numbers.",
                  RESTART_DAEMON, min=0),
            _ro("web", "auth", RO_LOCKOUT,
                "The login mode. Use 'sudo kestrel web auth <mode>' — a "
                "wrong value can lock you out of the dashboard.", cli=""),
            _ro("web", "username", RO_CLI,
                "The account the login checks (system/basic) — set it with "
                "'web auth'.", cli=""),
            _ro("web", "password", RO_SECRET,
                "Basic-auth password. Set it with 'sudo kestrel web auth "
                "basic'.", cli="", secret=True),
            _ro("web", "token", RO_SECRET,
                "Token-auth token. Set it with 'sudo kestrel web auth "
                "token'.", cli="", secret=True),
            _edit("web", "probe_url", "str", "",
                  "Explicit reachability probe URL for the online check. Empty = "
                  "NetworkManager check, then a default probe.", RESTART_DAEMON,
                  maxlen=256),
        ],
    },
]


def editable_entries():
    """Every editable entry across the sections, in display order."""
    return [e for section in SECTIONS for e in section["entries"]
            if e.get("editable")]


def _constraints(entry):
    """The validator constraints of an editable entry (what the apply helper's
    allow-list carries): type plus whichever of min/max/choices/maxlen/pattern/
    under apply."""
    spec = {"type": entry["type"]}
    for limit in ("min", "max", "choices", "maxlen", "pattern", "under"):
        if limit in entry:
            spec[limit] = entry[limit]
    return spec


# "table.key" -> validator spec, the allow-list the form submits against. The
# root helper's CONFIG_SETTABLE must equal this (asserted by the drift test).
EDITABLE = {
    "%s.%s" % (e["table"], e["key"]): _constraints(e) for e in editable_entries()
}


def find(table, key):
    """The catalog entry for ``table``/``key``, or None. Used by the web handler
    to coerce and validate a submitted value against its declared type."""
    for section in SECTIONS:
        for entry in section["entries"]:
            if entry["table"] == table and entry["key"] == key:
                return entry
    return None
