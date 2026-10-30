"""kestrel plugin host daemon and plugin framework (Phase 3).

The public plugin API is re-exported here so plugins can write either

    from kestrel_pi import Plugin

or the fully-qualified

    from kestrel_pi.plugins import Plugin

Both are the same class. ``HOOK_API_VERSION`` is the daemon's current hook-API
version; a plugin declares the version it targets with ``__target_api__`` and
the loader compares the two (improvement 4).
"""

__version__ = "0.3.0"

# Bump this on any breaking change to a dispatched hook's signature. Phase 5
# gives ``on_ui_update`` its real second argument - a read-only device/agent
# state snapshot (:class:`~kestrel_pi.ui.UIState`) - now that a physical
# e-ink renderer exists, so this is 2. A plugin written for the new signature
# declares ``__target_api__ = 2``; one still pinned to 1 loads with a warning.
HOOK_API_VERSION = 2

from .plugins import CAPABILITIES, Plugin, load_helper, load_library  # noqa: E402  (re-export after constants)

__all__ = ["Plugin", "CAPABILITIES", "HOOK_API_VERSION", "load_helper", "load_library",
           "__version__"]
