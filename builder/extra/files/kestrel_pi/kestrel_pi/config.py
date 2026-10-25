"""Loading ``/etc/kestrel/config.toml`` and the daemon/web defaults.

The config file is the same one Phase 2 established, read with the stdlib
``tomllib`` (Python 3.11+). Unknown keys are kept and ignored so later phases
add options without breaking older images; missing sections fall back to the
defaults here. The file is never rewritten by the daemon - the
``kestrel plugins`` tool owns edits.
"""

from __future__ import annotations

import tomllib

DEFAULT_CONFIG_PATH = "/etc/kestrel/config.toml"

DAEMON_DEFAULTS = {
    "enabled": True,
    "epoch_seconds": 60,
    "max_hook_seconds": 30,
    "hook_workers": 4,
    # A hook still running this many seconds after it started has stopped
    # returning (not just slow): Python can't kill the thread, so it holds its
    # pool worker for good. Such a plugin is quarantined (taken out of dispatch
    # rotation) so a stuck hook can't drain the pool and freeze the other
    # plugins' dashboard updates. A hook that overruns max_hook_seconds but then
    # finishes is only slow and is never quarantined. 0 disables quarantine.
    "hook_stuck_seconds": 300,
    # How often the daemon polls the capture store and the management paths.
    "capture_poll_seconds": 5,
    "connectivity_poll_seconds": 15,
    # Consecutive failed connectivity polls before the dashboard reports
    # offline. >1 debounces a flaky/slow management link (e.g. a BT-PAN tether)
    # so a single probe timeout doesn't flap the state; online is still shown
    # on the first reachable poll. 1 = report offline on the first failure.
    "offline_confirmations": 2,
}

WEB_DEFAULTS = {
    "enabled": True,
    "bind": "management",
    "port": 8080,
    # system = the device login account (same credential as SSH); basic/token =
    # a web-only credential; none = open, read-only page.
    "auth": "system",
    "username": "",
    "password": "",
    "token": "",
    # Optional explicit HTTP reachability probe; empty = use NetworkManager's
    # own connectivity check for on_internet_available.
    "probe_url": "",
    # Soft deadline (seconds) for a plugin's on_webhook page handler. Past it the
    # page returns a timeout response instead of the browser hanging on a stuck
    # plugin; the call is left to finish in the background (one leaked thread, no
    # shared pool to starve). 0 disables the timeout.
    "webhook_timeout_seconds": 30,
}


def load_config(path=DEFAULT_CONFIG_PATH, logger=None):
    """Load and return the full config dict (empty on a missing/bad file)."""
    try:
        with open(path, "rb") as handle:
            return tomllib.load(handle)
    except FileNotFoundError:
        if logger:
            logger.warning("config %s not found; using defaults", path)
        return {}
    except (OSError, tomllib.TOMLDecodeError) as exc:
        if logger:
            logger.error("config %s: %s; using defaults", path, exc)
        return {}


def section(config, name, defaults):
    """Return ``config[name]`` merged over ``defaults`` (a fresh dict)."""
    merged = dict(defaults)
    merged.update(config.get(name, {}) or {})
    return merged
