"""kestrel plugin host daemon and plugin framework (Phase 3).

The public plugin API is re-exported here so plugins can write either

    from kestrel_pi import Plugin

or the fully-qualified

    from kestrel_pi.plugins import Plugin

Both are the same class. ``HOOK_API_VERSION`` is the daemon's current hook-API
version; a plugin declares the version it targets with ``__target_api__`` and
the loader compares the two (improvement 4).
"""

# The installed package's version (kept in step with the release by the
# Release workflow for the image and PyPI); a source checkout that was never
# installed reports a dev placeholder.
try:
    from importlib.metadata import PackageNotFoundError as _NotFound
    from importlib.metadata import version as _dist_version
    try:
        __version__ = _dist_version("kestrel-pi")
    except _NotFound:
        __version__ = "0.0.0+dev"
except ImportError:  # pragma: no cover - importlib.metadata is stdlib on 3.8+
    __version__ = "0.0.0+dev"

# The device's release version: stamped by the image build and rewritten by an
# in-place update ("master" on a build with no release version). This, not the
# package version, is what the device reports as its version.
VERSION_FILE = "/etc/kestrel/version"


def device_version():
    """The device's release version from :data:`VERSION_FILE`, or "" when it
    can't be read (a checkout or test host). A read-only device fact."""
    try:
        with open(VERSION_FILE, "r", encoding="utf-8") as handle:
            return handle.read().strip()[:64]
    except OSError:
        return ""

# Bump this on any breaking change to a dispatched hook's signature. Phase 5
# gives ``on_ui_update`` its real second argument - a read-only device/agent
# state snapshot (:class:`~kestrel_pi.ui.UIState`) - now that a physical
# e-ink renderer exists, so this is 2. A plugin written for the new signature
# declares ``__target_api__ = 2``; one still pinned to 1 loads with a warning.
HOOK_API_VERSION = 2

from .plugins import CAPABILITIES, Plugin, load_helper, load_library  # noqa: E402  (re-export after constants)

__all__ = ["Plugin", "CAPABILITIES", "HOOK_API_VERSION", "load_helper", "load_library",
           "__version__", "device_version"]
