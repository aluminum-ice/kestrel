"""Non-blocking, isolated hook dispatch (improvement 1).

Legacy pwnagotchi runs hooks on unbounded background threads with no deadline,
so nothing notices a slow hook or sets aside a stuck plugin, and a hung
plugin's work silently piles up. Here the daemon owns a bounded worker pool and
dispatches hooks off the event loop:

* **Non-blocking:** :meth:`Dispatcher.dispatch` returns immediately; a slow or
  hung plugin never holds up capture/connectivity events or its peers.
* **Per-plugin serialization:** a single plugin's hooks never overlap (so a
  plugin never re-enters itself and its own state is safe), while different
  plugins' hooks run concurrently.
* **Isolation:** every hook runs under try/except keyed by plugin name; an
  exception is logged and swallowed, the batch continues, the daemon stays up.
* **Soft deadline + quarantine:** a hook running past ``max_hook_seconds`` is
  logged as an overrun (its slowness is surfaced on the Plugins page), but a
  hook that overruns and then *finishes* is only slow, not stuck, so it is not
  punished. Only a hook still running past the much longer
  ``stuck_after_seconds`` ceiling is treated as stuck: Python can't kill the
  thread, so it would hold its pool worker indefinitely and, with enough of
  them, freeze the other plugins' ``on_ui_update`` (the dashboard would stop
  updating). Such a plugin is quarantined - taken out of dispatch rotation - so
  it can't drain the pool. A well-behaved plugin keeps slow work (network,
  subprocess) on its own thread and returns from the hook at once, so it never
  trips either threshold.
"""

from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor


class Dispatcher:
    def __init__(self, max_workers=4, max_hook_seconds=30, logger=None,
                 stuck_after_seconds=300):
        self._pool = ThreadPoolExecutor(
            max_workers=max(1, int(max_workers)),
            thread_name_prefix="pwn-hook",
        )
        self._max_hook_seconds = float(max_hook_seconds)
        # A hook that overruns the soft deadline but then returns is merely slow
        # and is left alone. Only a hook still running this many seconds after it
        # started has stopped returning: Python can't kill the thread, so it
        # holds its pool worker indefinitely. Such a plugin is quarantined (taken
        # out of dispatch rotation) so a stuck hook can't drain the pool and
        # freeze every other plugin's on_ui_update. 0 disables quarantine
        # (overruns only logged). Only meaningful when >= max_hook_seconds.
        self._stuck_after = float(stuck_after_seconds)
        self._log = logger
        # Per-plugin serialization state, keyed by the plugin record.
        self._lanes = {}          # record -> {"queue": [...], "running": bool}
        self._lanes_lock = threading.Lock()
        self._idle = threading.Event()
        self._idle.set()
        self._inflight = 0
        self._shutdown = False

    # -- public ----------------------------------------------------------
    def dispatch(self, record, hook_name, *args):
        """Queue ``record.instance.<hook_name>(*args)`` to run off the loop.

        Returns immediately. No-op if the plugin does not implement the hook or
        is in a failed state."""
        if self._shutdown or record.failed:
            return
        instance = record.instance
        method = getattr(type(instance), hook_name, None)
        # Skip hooks the plugin didn't override (still the base no-op) to keep
        # the pool free for real work.
        from .plugins import Plugin

        if method is None or method is getattr(Plugin, hook_name, None):
            return

        with self._lanes_lock:
            lane = self._lanes.setdefault(record, {"queue": [], "running": False})
            lane["queue"].append((hook_name, args))
            self._inflight += 1
            self._idle.clear()
            if not lane["running"]:
                lane["running"] = True
                self._pool.submit(self._pump, record)

    def wait_idle(self, timeout=None):
        """Block until every queued hook has finished. Returns True if idle."""
        return self._idle.wait(timeout)

    def shutdown(self, wait=True):
        self._shutdown = True
        self._pool.shutdown(wait=wait)

    # -- internals -------------------------------------------------------
    def _pump(self, record):
        """Run one queued hook for ``record``, then re-arm for the next."""
        with self._lanes_lock:
            lane = self._lanes.get(record)
            if not lane or not lane["queue"]:
                if lane:
                    lane["running"] = False
                return
            hook_name, args = lane["queue"].pop(0)

        self._run_one(record, hook_name, args)

        with self._lanes_lock:
            self._inflight -= 1
            if self._inflight <= 0:
                self._inflight = 0
                self._idle.set()
            lane = self._lanes.get(record)
            if lane and lane["queue"]:
                # More queued for this plugin: keep the single lane going.
                self._pool.submit(self._pump, record)
            elif lane:
                lane["running"] = False

    def _run_one(self, record, hook_name, args):
        # Two timers: the soft deadline logs an overrun (visibility), the much
        # longer stuck ceiling quarantines a hook that never returns. Both are
        # cancelled the instant the hook returns, so _on_stuck fires only while
        # the hook is still running - a slow-but-finishing hook is never
        # quarantined.
        soft = threading.Timer(
            self._max_hook_seconds, self._on_overrun, args=(record, hook_name)
        )
        soft.daemon = True
        soft.start()
        stuck = None
        if self._stuck_after > 0:
            stuck = threading.Timer(
                self._stuck_after, self._on_stuck, args=(record, hook_name)
            )
            stuck.daemon = True
            stuck.start()
        started = time.monotonic()
        try:
            getattr(record.instance, hook_name)(*args)
        except Exception as exc:  # noqa: BLE001 - isolation is the whole point
            record.last_error = "%s: %s" % (hook_name, exc)
            if self._log:
                self._log.exception(
                    "plugin %s raised in %s", record.name, hook_name
                )
        finally:
            soft.cancel()
            if stuck is not None:
                stuck.cancel()
            record.last_hook = hook_name
            record.last_hook_time = time.time()
            elapsed = time.monotonic() - started
            record.last_hook_seconds = elapsed
            if (elapsed >= self._max_hook_seconds and not record.failed
                    and self._log):
                # Overran the soft deadline but returned: slow, not stuck.
                self._log.info(
                    "plugin %s hook %s finished after %.0fs (over the %.0fs "
                    "soft deadline) - slow, not stuck",
                    record.name, hook_name, elapsed, self._max_hook_seconds,
                )

    def _on_overrun(self, record, hook_name):
        # The soft deadline passed and the hook has not returned yet. Record it
        # for the Plugins page, but do NOT quarantine: it may simply be slow and
        # about to finish. Quarantine is the stuck ceiling's job (_on_stuck).
        record.overruns += 1
        record.last_error = "%s overran %ss" % (hook_name, self._max_hook_seconds)
        if self._log:
            self._log.warning(
                "plugin %s hook %s overran %.0fs and is still running",
                record.name, hook_name, self._max_hook_seconds,
            )

    def _on_stuck(self, record, hook_name):
        # Fires only while the hook is still running at the stuck ceiling (the
        # finally clause cancels this timer the moment the hook returns). A hook
        # that has not returned after this long has stopped returning; Python
        # can't kill the thread, so it holds its pool worker indefinitely. Take
        # the plugin out of rotation (dispatch() skips a failed record) so it
        # can't pile up more hung workers and freeze the other plugins'
        # on_ui_update. It now shows as failed on the Plugins page.
        secs = int(self._stuck_after)
        pretty = "%dm" % round(secs / 60) if secs >= 120 else "%ds" % secs
        record.state = "failed"
        record.reason = (
            "%s is stuck (ran past %s) and was quarantined" % (hook_name, pretty)
        )
        if self._log:
            self._log.error(
                "plugin %s quarantined: hook %s still running after %.0fs; no "
                "further hooks will be dispatched to it",
                record.name, hook_name, self._stuck_after,
            )
