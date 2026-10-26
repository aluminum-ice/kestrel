"""updates - tell the operator when a newer kestrel release is out, and
(from the web dashboard) apply an in-place *app-layer* update on demand.

This plugin never updates anything on its own. While enabled it periodically
asks the release server (the project's Forgejo instance) for the latest
release, compares it to the version stamped into the
image (``/etc/kestrel/version``), and surfaces the result as a dashboard
card plus a small shared state file. The actual upgrade is a deliberate,
operator-initiated action: a button on the plugin's web page (``[web] auth``
on) that, after a confirmation step, runs the root ``kestrel update
apply`` helper through the same privileged socket the capture/plugin controls
use. There is **no** auto-update.

Releases come in two channels, told apart by the tag: a stable ``X.Y.Z`` and a
beta ``X.Y.Z-beta.N`` (which the Release workflow also flags as a Forgejo
pre-release). Only stable releases are offered unless the ``beta`` option is
on; versions compare by SemVer precedence, so a beta device moves on to the
final release when it is out.

Only the *application layer* can be updated in place - the ``kestrel_pi``
package, the reference plugins, the helper scripts, the systemd units and
additive config keys, i.e. everything built from ``builder/extra/files``. A
release that also changed the base OS (new apt packages, kernel or boot config)
is flagged ``needs reflash`` from its update manifest, and the upgrade button is
withheld rather than half-applying something it cannot. See
docs/developer/updates-setup.md.

Because it reaches the network it declares the ``network`` capability (so
enabling it takes the authorized-use acknowledgment, like the other networked
plugins) and ships disabled.

It speaks the Forgejo (Gitea-compatible) release API on ``host``. By default
it talks to the server unauthenticated, so it only sees **public** releases. To
update from a private repo (or a private fork), set the optional ``token``
option to a Forgejo access token: it is stored only in this device's
config.toml (never in the image or the git repo) and authenticates both the
release check here and the ``update apply`` clone. It is only ever sent to
``host``. See docs/developer/updates-setup.md.
"""

from __future__ import annotations

import json
import os
import threading
import time

from kestrel_pi import Plugin

# Where the build stamps the running image's version (see builder/config.yml).
VERSION_FILE = "/etc/kestrel/version"
# Shared update state: the plugin writes it, the web dashboard reads it to
# decide whether to show the upgrade button, and the root apply helper updates
# it after a run. Pi-owned (created by the playbook) so the daemon can write it.
STATE_DIR = "/var/lib/kestrel/update"
STATE_FILE = os.path.join(STATE_DIR, "state.json")
# Name of the "check now" trigger file the CLI (`kestrel update check`)
# and the web "Check" button drop in STATE_DIR to force an immediate check,
# bypassing the interval throttle. Resolved against STATE_DIR at call time (not
# frozen into a constant) so tests that repoint STATE_DIR pick it up too.
CHECK_TRIGGER_NAME = ".check-now"
# The canonical public repo and the Forgejo server that hosts it; override
# `repo` (and `host`) for a fork.
DEFAULT_REPO = "aluminum-ice/kestrel"
DEFAULT_HOST = "https://git.3ntropy.io"
# Forgejo caps a page of results at 50 (its max_response_items); the release
# list reads up to this many pages, i.e. the newest 100 releases.
RELEASES_PAGE = 50
RELEASES_MAX_PAGES = 2
# The release asset the Release workflow attaches, describing whether a release
# is app-layer-updatable or needs a reflash, and the commit to check out.
MANIFEST_ASSET = "update-manifest.json"
# A check that failed for a passing reason is retried this soon, not a whole
# check interval later.
RETRY_SECONDS = 900
# Response size caps: a release list page carries up to 50 release notes; a
# manifest is a few hundred bytes.
MAX_RESPONSE_BYTES = 4 * 1024 * 1024
MAX_MANIFEST_BYTES = 256 * 1024


def _text(value):
    """A stripped string from a JSON field, or "" for anything else, so a
    malformed reply can't raise in the check."""
    return value.strip() if isinstance(value, str) else ""


def read_json_capped(resp, limit):
    """Parse a streamed ``requests`` response as JSON, refusing a body over
    ``limit`` bytes."""
    buf = bytearray()
    for chunk in resp.iter_content(chunk_size=65536):
        buf += chunk
        if len(buf) > limit:
            raise ValueError("reply larger than %d bytes" % limit)
    return json.loads(bytes(buf).decode("utf-8"))


def read_installed_version(path=None):
    """The version stamped into this image, or "" if unreadable. A local build
    stamps "master" (see builder/config.yml), which parses as a dev build."""
    try:
        with open(path or VERSION_FILE, encoding="utf-8") as handle:
            return handle.read().strip()
    except OSError:
        return ""


def parse_version(text):
    """A comparable tuple from a version or tag string: strips a leading ``v``,
    drops any ``-prerelease`` suffix and keeps the leading dotted-numeric run
    (``"0.4.3"`` -> ``(0, 4, 3)``). Returns ``None`` for a non-numeric / dev
    string like ``"master"`` so it is never mistaken for a real version."""
    if not text:
        return None
    body = text.strip().lstrip("vV").split("-", 1)[0].split("+", 1)[0]
    nums = []
    for part in body.split("."):
        if part.isdigit():
            nums.append(int(part))
        else:
            break
    return tuple(nums) if nums else None


def version_key(text):
    """A sortable key for a release tag or installed version, with SemVer
    pre-release precedence: ``0.7.0-beta.1`` < ``0.7.0-beta.2`` < ``0.7.0``.
    The key is ``(numbers, pre)``, where ``pre`` is ``(1,)`` for a stable
    release and ``(0, N)`` for ``-beta.N``, the only pre-release form the
    Release workflow cuts. Returns ``None`` for a dev string like ``"master"``
    and for any other suffix (``-rc1``, ``-dirty``), so an unrecognised version
    is never offered and never compared."""
    if not text:
        return None
    body = text.strip().lstrip("vV").split("+", 1)[0]
    core, sep, suffix = body.partition("-")
    nums = parse_version(core)
    if nums is None or not core.replace(".", "").isdigit():
        return None
    if not sep:
        return (nums, (1,))
    label, dot, number = suffix.partition(".")
    if label == "beta" and dot and number.isdigit():
        return (nums, (0, int(number)))
    return None


def is_prerelease(tag):
    """True when ``tag`` is a ``-beta.N`` pre-release version."""
    key = version_key(tag)
    return key is not None and key[1][0] == 0


def is_newer(latest, installed):
    """True when release tag ``latest`` is strictly newer than ``installed``,
    by SemVer precedence (a final release is newer than its own betas).
    Unparseable/dev installed version => never newer (we don't nag dev builds)."""
    lv, iv = version_key(latest), version_key(installed)
    if lv is None or iv is None:
        return False
    return lv > iv


def release_is_prerelease(rel):
    """True for a release Forgejo flags as a pre-release, or whose tag is a
    ``-beta.N`` version (so a beta published without the flag still never
    reaches the stable channel)."""
    return bool(rel.get("prerelease")) or is_prerelease(_text(rel.get("tag_name")))


class Updates(Plugin):
    __author__ = "kestrel"
    __version__ = "0.1.0"
    __license__ = "GPL3"
    __description__ = "Alert on new releases; apply an in-place update on demand."

    __target_api__ = 2
    __capabilities__ = ("network", "ui")
    # One row button on the Plugins page: "Check" asks the plugin to check for
    # a new release now, bypassing its interval throttle (on_webhook, a POST).
    __actions__ = ({"label": "Check", "path": "check", "method": "post"},)
    __options__ = {
        # Forgejo "owner/repo" to check. Default is the canonical public repo.
        # NB: pure string literals, not DEFAULT_REPO/DEFAULT_HOST - the root
        # plugins-ctl helper reads __options__ statically with ast and never
        # imports this module, so a Name reference here would not resolve.
        "repo": {"type": "str", "default": "aluminum-ice/kestrel"},
        # The Forgejo (or Gitea) server hosting `repo`, as an https:// URL.
        "host": {"type": "str", "default": "https://git.3ntropy.io"},
        # Optional Forgejo access token, for a PRIVATE repo or fork. Empty (the
        # default) => unauthenticated requests, public repos only. Stored only
        # in this device's config.toml, never in the image or the git repo;
        # marked secret so the CLI and web never echo it. It needs read-only
        # "repository" scope. It also authenticates the `update apply` clone.
        "token": {"type": "str", "default": "", "secret": True},
        # Opt in to beta (-beta.N pre-release) updates as well as stable ones.
        # Off => only stable releases are offered. Turning it off while on a
        # beta keeps the device where it is (no downgrade) until a stable
        # release newer than that beta is out.
        "beta": {"type": "bool", "default": False,
                 "help": "Also offer beta (X.Y.Z-beta.N) releases. Turning it "
                         "off never downgrades a device already on a beta."},
        # Seconds to allow the pre-update backup (the remote mirror the update
        # takes before touching files) before the update gives up on it, so a
        # backup that can't reach the remote cannot hold the daemon -- and the
        # dashboard -- down for many minutes. The engine reads this directly;
        # 0 waits indefinitely (the old behaviour).
        "backup_timeout_seconds": {"type": "int", "default": 180,
                 "help": "Seconds to allow the pre-update backup before the "
                         "update cancels it (0 = wait indefinitely)."},
        # How often, at most, to re-check while online (6h default).
        "check_interval_seconds": {"type": "int", "default": 21600},
        "timeout": {"type": "int", "default": 30},
    }

    def on_loaded(self):
        self.log = self.context.logger
        self._lock = threading.Lock()
        self._run_lock = threading.Lock()   # at most one check at a time
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._last_check = None
        self._state = self._load_state()
        self._withdraw_stale_offer()
        self.log.info("updates loaded; installed version %r",
                      read_installed_version() or "unknown")
        # The release check is several sequential HTTP requests, so it runs on
        # this worker thread, never inline in a hook: a hook that blocked on them
        # would hold a dispatch worker and, past the stuck ceiling, be
        # quarantined. The hooks just wake the worker, which also does an initial
        # check in case we are already online at load.
        self._worker = threading.Thread(
            target=self._run, name="updates-worker", daemon=True)
        self._worker.start()

    def _channel(self):
        return "beta" if self.options.get("beta") is True else "stable"

    def _withdraw_stale_offer(self):
        """Withdraw a stored offer that no longer applies: one the other
        channel found (the beta toggle changed), or one computed for a version
        that is no longer installed (an update ran since). The web Upgrade
        button acts on the shared state file, so such an offer must not linger
        until the next check, which may be hours away while offline. The
        cleared state reads as "checking…"."""
        state = self._state
        if not state.get("update_available"):
            return
        if state.get("channel", "stable") == self._channel() and \
                state.get("installed") == read_installed_version():
            return
        cleared = dict(state)
        cleared.update({"latest": "", "update_available": False,
                        "offerable": False, "commit": "", "reason": "",
                        "notes": [], "notes_url": "", "checked_at": None,
                        "installed": read_installed_version(),
                        "channel": self._channel()})
        self._store(cleared)

    def on_unload(self):
        self._stop.set()
        self._wake.set()

    def _run(self):
        self._tick()
        while not self._stop.is_set():
            self._wake.wait(timeout=self._poll_interval())
            self._wake.clear()
            if self._stop.is_set():
                return
            self._tick()

    def _poll_interval(self):
        # Wake when the next check is due (sooner after a failed one, see
        # _schedule_retry) so it happens even with no connectivity edges or
        # epochs; never busier than 60s.
        interval = self._interval()
        if self._last_check is None:
            return interval
        return max(60, interval - (time.monotonic() - self._last_check))

    def _tick(self):
        if not self.context.connectivity.online:
            return
        if not self._run_lock.acquire(blocking=False):
            return  # a check is already running
        try:
            # A pending "check now" request (dropped by the CLI / web button)
            # forces a check that ignores the interval throttle; otherwise the
            # throttle applies as usual.
            if self._consume_check_request():
                self._force_check()
            else:
                self._maybe_check()
        except Exception as exc:  # noqa: BLE001 - the worker must outlive any bug
            # An exception here would end the worker thread and with it every
            # later check, silently, until the daemon restarts.
            self.log.warning("updates: check failed unexpectedly: %r", exc)
            self._schedule_retry()
        finally:
            self._run_lock.release()

    def on_internet_available(self):
        # A reconnect: let the worker run the check (it consumes any pending
        # "check now" trigger and otherwise honors the interval throttle).
        self._wake.set()

    def on_epoch(self, epoch, info):
        # Nudge the worker each epoch so a pending "check now" trigger is picked
        # up and the periodic check runs; the work is on the worker thread, so
        # this hook returns at once. A trigger dropped while offline stays until
        # the worker next runs online, so it is not lost.
        self._wake.set()

    # -- the "Check" row button -----------------------------------------
    def on_webhook(self, path, request):
        """The ``/plugins/updates/check`` action (the "Check" row button): ask
        for an immediate release check, bypassing the interval throttle. It
        drops the same "check now" trigger the CLI's ``update check`` does, via
        the root apply helper (no confirm - a check is read-only and additive),
        then returns to the Plugins page with a banner. The result shows on the
        Updates dashboard card and in ``update status``."""
        if path != "check":
            return self.context.redirect("No such action.", error=True)
        reply = self.context.apply({"target": "update-check",
                                     "action": "check-now"})
        if reply.get("ok"):
            # Nudge the worker so it picks up the trigger file promptly rather
            # than waiting for the next epoch.
            self._wake.set()
            return self.context.redirect("Update check requested.")
        return self.context.redirect(reply.get("error") or "not applied",
                                     error=True)

    # -- dashboard readout ----------------------------------------------
    def on_ui_setup(self, ui):
        ui.add_element("status", "checking…", label="Status", kind="text")
        ui.add_element("installed", read_installed_version() or "unknown",
                       label="Installed", kind="text")
        ui.add_element("latest", "—", label="Latest", kind="text")

    def on_ui_update(self, ui, state):
        with self._lock:
            state = dict(self._state)
        ui.set("installed", state.get("installed") or "unknown")
        ui.set("latest", state.get("latest") or "—")
        ui.set("status", self._status_text(state))

    @staticmethod
    def _status_text(state):
        if not state or not state.get("checked_at"):
            return "checking…"
        if state.get("installed_is_dev"):
            return "dev build (not checked)"
        if not state.get("update_available"):
            return "up to date"
        if state.get("offerable"):
            return "update available"
        return "update available — needs reflash"

    # -- the check -------------------------------------------------------
    def _interval(self):
        try:
            return max(60, int(self.options.get("check_interval_seconds", 21600)))
        except (TypeError, ValueError):
            return 21600

    def _maybe_check(self):
        now = time.monotonic()
        if self._last_check is not None and now - self._last_check < self._interval():
            return
        self._last_check = now
        if not self._check():
            self._schedule_retry()

    def _force_check(self):
        """Check now, ignoring the interval throttle, and reset the throttle
        window so the next periodic check is measured from here. Used by the
        `kestrel update check` CLI and the web "Check" button, which
        drop a trigger file this plugin consumes on its next hook - the force is
        the point, since the whole reason to ask is not to wait out the interval."""
        self._last_check = time.monotonic()
        if not self._check():
            self._schedule_retry()

    def _schedule_retry(self):
        """After a check that failed for a passing reason (no route, a server
        error), try again in RETRY_SECONDS rather than a whole interval."""
        interval = self._interval()
        if interval > RETRY_SECONDS:
            self._last_check = time.monotonic() - interval + RETRY_SECONDS

    def _consume_check_request(self):
        """True if a "check now" trigger file is present, removing it. The file
        is dropped (as pi) by the CLI / web button in STATE_DIR; this pi daemon
        removes it so a single request forces exactly one check."""
        trigger = os.path.join(STATE_DIR, CHECK_TRIGGER_NAME)
        try:
            if os.path.exists(trigger):
                os.remove(trigger)
                return True
        except OSError:
            pass
        return False

    def _check(self):
        """One release check. Returns False when it failed for a passing reason
        (so it is retried soon), True otherwise - including a definite answer
        stored in the shared state, or a configuration problem that retrying
        will not fix."""
        if not self.context.require_capability("network"):
            return True
        repo = (self.options.get("repo") or DEFAULT_REPO).strip()
        host = self._host()
        if not host:
            self.log.warning("updates: host %r is not an https:// URL; not "
                             "checking", self.options.get("host"))
            return True
        token = (self.options.get("token") or "").strip()
        installed = read_installed_version()
        try:
            timeout = int(self.options.get("timeout", 30))
        except (TypeError, ValueError):
            timeout = 30
        beta = self._channel() == "beta"
        # Both channels read the release list and pick the highest version
        # themselves (Forgejo's /releases/latest goes by date, so a hotfix on an
        # older line published later would win); the stable channel skips
        # pre-releases. The same list feeds the notes and the reflash-gap scan.
        url = "%s/api/v1/repos/%s/releases?limit=%d&page=1" % (host, repo, RELEASES_PAGE)
        try:
            status, body = self._get(url, token, timeout)
        except Exception as exc:  # noqa: BLE001 - never let a check break the loop
            self.log.warning("updates: release check failed: %s", exc)
            return False
        if status == 404:
            # Forgejo also returns 404 for a private repo the caller can't
            # see, so when no token is set say that rather than "no release".
            if token:
                self.log.info("updates: %s not found on %s", repo, host)
            else:
                self.log.info("updates: %s has no public releases (a private "
                              "repo needs a token - see docs)", repo)
            self._store(self._compute_state(installed, None, None))
            return True
        if status in (401, 403):
            self.log.warning("updates: %s HTTP %s - check the token's "
                             "validity and scope", host, status)
            return True
        if status >= 400 or not isinstance(body, list):
            self.log.warning("updates: %s answered HTTP %s%s", host, status,
                             "" if status >= 400 else " with no release list")
            return False

        try:
            releases, complete = self._fetch_releases(repo, timeout, token, first=body)
            if releases is None:
                return False
            release = self._newest_release(releases, include_prereleases=beta)
            if release is None:
                self.log.info("updates: no published release for %s yet", repo)
                self._store(self._compute_state(installed, None, None))
                return True
            tag = _text(release.get("tag_name"))
            available = is_newer(tag, installed)
            manifest = self._fetch_manifest(release, timeout, token) if available else None
            # Which releases sit on the target's update path follows the target,
            # not the channel: the Release workflow builds a stable release's
            # manifest and notes against the previous stable release (they
            # already cover any betas in between), and a beta's against the
            # previous release of either kind.
            path_prereleases = is_prerelease(tag)
            notes = self._notes_from_releases(releases, installed, tag,
                                              path_prereleases) \
                if available else None
            # If the latest is an in-place (app-layer) update, make sure the
            # device did not skip a reflash-only release in between: an
            # app-layer update can only be applied on top of every build-layer
            # release since the installed one. A reflash sitting in the gap
            # (e.g. 0.6.4 reflash, device on 0.6.3, latest 0.6.5 app-layer)
            # means this update needs a flash, not an in-place apply.
            gap_reason = ""
            if available and manifest and \
                    _text(manifest.get("kind")).lower() == "app-layer":
                gap_reason = self._gap_requires_reflash(
                    releases, installed, tag, timeout, token, path_prereleases,
                    complete=complete)
        except Exception as exc:  # noqa: BLE001 - a bad reply must not end checks
            self.log.warning("updates: release check failed: %s", exc)
            return False
        state = self._compute_state(installed, release, manifest, notes,
                                    gap_reason=gap_reason)
        self._store(state)
        if state["update_available"]:
            self.log.info("updates: %s available (installed %s, %s)",
                          tag, installed or "unknown",
                          "in-place" if state["offerable"] else "needs reflash")
        return True

    def _get(self, url, token, timeout, limit=None):
        """GET ``url`` and return (status, parsed JSON or None for an HTTP
        error). The body is streamed and refused past ``limit`` bytes, so a
        broken or hostile server can't exhaust the device's memory. The token
        is only sent to ``host``."""
        import requests

        host = self._host()
        on_host = bool(host) and url.startswith(host + "/")
        resp = requests.get(url, headers=self._headers(token if on_host else ""),
                            timeout=timeout, stream=True)
        try:
            if resp.status_code >= 400:
                return resp.status_code, None
            return resp.status_code, read_json_capped(resp, limit or MAX_RESPONSE_BYTES)
        finally:
            resp.close()

    def _fetch_manifest(self, release, timeout, token=""):
        """Fetch the release's update-manifest.json asset: the manifest dict, or
        None when the release has none (or it is not a JSON object). Raises on
        a network or server error, so a passing failure is retried instead of
        reading as "needs reflash".

        Forgejo serves assets from ``browser_download_url`` (under
        ``releases/download/``), which accepts the same token as the API, so a
        private repo's manifest comes from the same URL with the token added.
        The token is only sent when that URL is on ``host``."""
        asset = None
        for candidate in release.get("assets") or []:
            if isinstance(candidate, dict) and candidate.get("name") == MANIFEST_ASSET:
                asset = candidate
                break
        if not asset:
            return None
        url = _text(asset.get("browser_download_url"))
        if not url:
            return None
        status, body = self._get(url, token, timeout, MAX_MANIFEST_BYTES)
        if status == 404:
            return None
        if status >= 400:
            raise OSError("manifest HTTP %s" % status)
        return body if isinstance(body, dict) else None

    def _fetch_releases(self, repo, timeout, token="", first=None):
        """The repo's published releases (newest first, up to 100) and whether
        that is all of them, as ``(releases, complete)``; ``(None, False)`` when
        a page could not be read. Fetched once per check and reused for the
        "What's new" notes and the reflash-gap scan. Forgejo pages at 50, so this
        reads up to RELEASES_MAX_PAGES pages, stopping at the first short one.
        ``first`` is page 1 when the caller already has it."""
        host = self._host()
        if not host:
            return None, False
        releases = []
        start = 1
        if first is not None:
            releases.extend(r for r in first if isinstance(r, dict))
            if len(first) < RELEASES_PAGE:
                return releases, True
            start = 2
        for page in range(start, RELEASES_MAX_PAGES + 1):
            url = "%s/api/v1/repos/%s/releases?limit=%d&page=%d" % (
                host, repo, RELEASES_PAGE, page)
            try:
                status, batch = self._get(url, token, timeout)
            except Exception as exc:  # noqa: BLE001
                self.log.warning("updates: releases fetch failed: %s", exc)
                return None, False
            if status >= 400 or not isinstance(batch, list):
                return None, False
            releases.extend(r for r in batch if isinstance(r, dict))
            if len(batch) < RELEASES_PAGE:
                return releases, True
        return releases, False

    @staticmethod
    def _newest_release(releases, include_prereleases=False):
        """The highest-versioned published release, or None; betas only when
        ``include_prereleases``. Picked by version, not list order: Forgejo
        lists by creation date, so a hotfix cut after a newer release would
        otherwise outrank it."""
        best, best_key = None, None
        for rel in releases:
            if not isinstance(rel, dict) or rel.get("draft"):
                continue
            if not include_prereleases and release_is_prerelease(rel):
                continue
            key = version_key(_text(rel.get("tag_name")))
            if key is not None and (best_key is None or key > best_key):
                best, best_key = rel, key
        return best

    def _host(self):
        """The release server's base URL without a trailing slash, or "" when
        the configured value is not an https:// URL (a token must never go out
        in clear text)."""
        host = (self.options.get("host") or DEFAULT_HOST).strip().rstrip("/")
        return host if host.startswith("https://") else ""

    @staticmethod
    def _headers(token=""):
        headers = {"Accept": "application/json",
                   "User-Agent": "kestrel-updates"}
        if token:
            headers["Authorization"] = "token %s" % token
        return headers

    def _notes_from_releases(self, releases, installed, latest,
                             include_prereleases=False):
        """Release notes for every published release strictly newer than the
        installed version, up to and including ``latest`` - newest first. Stored
        in the shared state so the dashboard's "What's new" link can show the
        combined changes without a round-trip (and even if the link is flaky when
        it is opened). Beta releases are only included when
        ``include_prereleases`` (a beta target): a stable release's own notes
        already cover the betas before it. Returns [] when there is nothing to
        show."""
        hi = version_key(latest)
        notes = []
        for rel in releases:
            if not isinstance(rel, dict) or rel.get("draft"):
                continue
            if not include_prereleases and release_is_prerelease(rel):
                continue
            tag = _text(rel.get("tag_name"))
            if not is_newer(tag, installed):
                continue
            pv = version_key(tag)
            # Never show anything past the release being offered (defensive; a
            # newer draft/prerelease could otherwise sneak in).
            if hi is not None and pv is not None and pv > hi:
                continue
            notes.append({
                "version": tag,
                "date": _text(rel.get("published_at"))[:10],
                "body": _text(rel.get("body"))[:20000],
            })
        notes.sort(key=lambda n: version_key(n["version"]), reverse=True)
        return notes[:50]

    def _gap_requires_reflash(self, releases, installed, latest, timeout, token="",
                              include_prereleases=False, complete=True):
        """Return a one-line reason when a published release strictly between the
        installed version and ``latest`` is reflash-only (or carries no manifest,
        which is treated as reflash), else "". Such a release in the gap means an
        app-layer ``latest`` cannot be applied in place: the device skipped a
        build-layer change and must be flashed. Drafts are ignored, and so are
        betas unless ``include_prereleases`` (a beta target, whose manifest is
        built only against the release just before it; a stable target's
        manifest already covers the betas since the previous stable). When the
        list is not ``complete`` (the page cap was hit) and does not reach back
        to the installed version, the gap can't be checked, which also reads as
        reflash - the safe direction."""
        hi = version_key(latest)
        lo = version_key(installed)
        gap = []
        keys = []
        for rel in releases:
            if not isinstance(rel, dict) or rel.get("draft"):
                continue
            tag = _text(rel.get("tag_name"))
            pv = version_key(tag)
            if pv is None:
                continue
            keys.append(pv)
            if not include_prereleases and release_is_prerelease(rel):
                continue
            if not is_newer(tag, installed):
                continue
            if hi is not None and pv >= hi:   # at or past latest (incl. latest itself)
                continue
            gap.append((pv, tag, rel))
        if not complete and lo is not None and (not keys or min(keys) > lo):
            return ("the installed %s is too far behind to check every release "
                    "up to %s, so it needs a reflash" % (installed, latest))
        # Newest first, so the reported blocker is the most recent reflash in the
        # gap (the real floor the device must reach).
        gap.sort(key=lambda g: g[0], reverse=True)
        for _pv, tag, rel in gap:
            manifest = self._fetch_manifest(rel, timeout, token)
            if not manifest:
                return ("release %s in between has no update manifest, so it "
                        "needs a reflash and can't be skipped" % tag)
            if _text(manifest.get("kind")).lower() != "app-layer":
                return ("release %s in between needs a reflash, so it can't be "
                        "skipped by an in-place update" % tag)
        return ""

    def _compute_state(self, installed, release, manifest, notes=None,
                       gap_reason=""):
        """Build the shared state dict from the installed version, the latest
        release JSON (or None), its manifest (or None) and the merged release
        notes for the range (or None)."""
        release = release if isinstance(release, dict) else {}
        manifest = manifest if isinstance(manifest, dict) else None
        tag = _text(release.get("tag_name"))
        html_url = _text(release.get("html_url"))
        if not html_url.startswith("https://"):
            html_url = ""     # only ever linked as https (never javascript:)
        installed_is_dev = version_key(installed) is None
        available = is_newer(tag, installed)

        kind = "unknown"
        commit = ""
        reason = ""
        offerable = False
        if available:
            if manifest:
                kind = _text(manifest.get("kind")).lower() or "unknown"
                commit = _text(manifest.get("commit"))
                min_from = manifest.get("min_update_from")
                if kind == "app-layer":
                    if gap_reason:
                        # A reflash-only release sits between installed and
                        # latest: an app-layer update can't be applied on top of
                        # a device that skipped it (computed from the release
                        # chain, so it holds even when the manifest omits
                        # min_update_from).
                        kind = "reflash"
                        reason = gap_reason
                    elif min_from and version_key(str(min_from)) is None:
                        kind = "reflash"
                        reason = ("the release's min_update_from (%s) is not a "
                                  "version" % min_from)
                    elif min_from and is_newer(str(min_from), installed):
                        kind = "reflash"
                        reason = ("installed version predates the oldest this "
                                  "in-place update supports (%s)" % min_from)
                    else:
                        offerable = True
                else:
                    reason = (_text(manifest.get("requires_reflash_reason"))
                              or "release changed the base OS (needs reflash)")
            else:
                reason = "no update manifest on the release (needs reflash)"

        return {
            "installed": installed,
            "installed_is_dev": installed_is_dev,
            "latest": tag,
            "update_available": bool(available),
            "offerable": bool(offerable),
            "kind": kind,
            "commit": commit,
            "reason": reason,
            "notes_url": html_url,
            "notes": notes or [],
            "repo": (self.options.get("repo") or DEFAULT_REPO).strip(),
            # The channel this check ran on; an offer from the other channel is
            # withdrawn at load (see _withdraw_stale_offer).
            "channel": self._channel(),
            "prerelease": is_prerelease(tag),
            # Display only: `kestrel update apply` takes the host, repo and
            # token from config.toml and re-verifies the release itself.
            "host": self._host(),
            "checked_at": time.time(),
        }

    # -- shared state ----------------------------------------------------
    def _store(self, state):
        with self._lock:
            self._state = state
            self.context.health_note = self._health_note(state)
        try:
            os.makedirs(STATE_DIR, exist_ok=True)
            tmp = STATE_FILE + ".tmp"
            with open(tmp, "w", encoding="utf-8") as handle:
                json.dump(state, handle)
            os.replace(tmp, STATE_FILE)
        except OSError as exc:
            self.log.warning("updates: could not write state: %s", exc)

    def _load_state(self):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as handle:
                state = json.load(handle)
        except (OSError, ValueError):
            return {}
        return state if isinstance(state, dict) else {}

    @staticmethod
    def _health_note(state):
        if not state.get("update_available"):
            return ""
        tag = state.get("latest") or "a newer release"
        if state.get("offerable"):
            return ("Update %s is available. Open the Updates page to upgrade "
                    "in place." % tag)
        reason = state.get("reason") or "needs a reflash"
        return ("Update %s is available but needs a reflash, not an in-place "
                "update (%s)." % (tag, reason))
