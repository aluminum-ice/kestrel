#!/usr/bin/env python3
"""Build a release's update-manifest.json (the Release workflow's "Build the
update manifest" step). See docs/developer/updates-setup.md.

Usage: update_manifest.py VERSION PRERELEASE UPDATE_KIND COMMIT > update-manifest.json

VERSION is the release being cut (X.Y.Z or X.Y.Z-beta.N), PRERELEASE "true" for
a beta, UPDATE_KIND the workflow input (auto, app-layer or reflash) and COMMIT
the commit it is built from. The release list is read from the Forgejo API
named by $RELEASES_API with $TOKEN, every page of it.

In "auto" mode a release is reflash-only when the diff since the previous
release touched a build-layer input (BUILD_INPUTS); otherwise it is app-layer.
The previous release is picked by version: a beta is compared with the release
just below it, beta or stable; a stable release with the previous *stable* one,
so the betas in between can't hide a build change from a device that never ran
them. When that makes a stable release reflash, it can still be applied in place
on the newest betas of its own cycle: if the diff from every beta from some beta
B up to the newest one touches no build-layer input, the release is app-layer
with min_update_from = B. A device on B or later has B's build layer (it was
flashed with B, or reached it by in-place updates that changed no build input),
so the in-place update is safe there; the updates plugin and update-apply
--verify send every older device to reflash.
"""

import json
import os
import re
import subprocess
import sys
import urllib.request

# A diff touching one of these changes the build layer (the base image, the
# playbook, the build, or the Release workflow that runs the build), which an
# in-place update can't carry onto a device. Other workflows (lint, tests,
# mirroring releases) never change the image, so they don't count.
BUILD_INPUTS = re.compile(
    r"^(builder/config\.(json|yml)|Makefile|\.(forgejo|github)/workflows/main\.yml)$")
PAGE = 50
MAX_PAGES = 40


def key(tag):
    """SemVer-ordered sort key for X.Y.Z / X.Y.Z-beta.N, or None."""
    m = re.fullmatch(r"(\d+)\.(\d+)\.(\d+)(?:-beta\.(\d+))?", tag or "")
    if not m:
        return None
    nums = tuple(int(g) for g in m.groups()[:3])
    return nums + ((1, 0) if m.group(4) is None else (0, int(m.group(4))))


def _published(releases, here):
    """(key, tag, is_beta) of every published release below ``here``."""
    out = []
    for rel in releases:
        if not isinstance(rel, dict) or rel.get("draft"):
            continue
        k = key(rel.get("tag_name"))
        if k is None or k >= here:
            continue
        out.append((k, rel["tag_name"], bool(rel.get("prerelease")) or k[3] == 0))
    return out


def previous(releases, version, stable):
    """The highest published release below ``version`` (stable ones only when
    ``stable``), or ""."""
    here = key(version)
    best = max(((k, tag) for k, tag, beta in _published(releases, here)
                if not (stable and beta)), default=None)
    return best[1] if best else ""


def cycle_betas(releases, prev, version):
    """The published betas strictly between ``prev`` and ``version``, newest
    first."""
    here, low = key(version), key(prev)
    return [tag for k, tag, beta in sorted(_published(releases, here), reverse=True)
            if beta and (low is None or k > low)]


def build_changed(since, ref):
    """True when the diff from ``since`` to ``ref`` touches a build-layer input,
    or when it can't be computed (a missing tag counts as a change)."""
    try:
        out = subprocess.run(["git", "diff", "--name-only", since, ref],
                             capture_output=True, text=True, check=True).stdout
    except (OSError, subprocess.CalledProcessError):
        return True
    return any(BUILD_INPUTS.match(line) for line in out.splitlines())


def decide(version, prerelease, update_kind, ref, releases, changed=build_changed):
    """(kind, reason, min_update_from) for the release."""
    if update_kind == "reflash":
        return "reflash", "release marked reflash-only by the maintainer", ""
    if update_kind == "app-layer":
        return "app-layer", "", ""
    prev = previous(releases, version, stable=not prerelease)
    if not prev:
        return "reflash", "first release; flash the image", ""
    if not changed(prev, ref):
        return "app-layer", "", ""
    reason = "the base image or build changed since %s; flash the image" % prev
    if prerelease:
        return "reflash", reason, ""
    floor = ""
    for beta in cycle_betas(releases, prev, version):
        if changed(beta, ref):
            break
        floor = beta
    if floor:
        return "app-layer", "", floor
    return "reflash", reason, ""


def manifest(version, kind, commit, reason, min_from):
    data = {
        "schema": 1,
        "version": version,
        "kind": kind,
        "commit": commit,
        "requires_reflash_reason": reason if kind == "reflash" else "",
    }
    if min_from:
        data["min_update_from"] = min_from
    return data


def fetch_releases(api, token):
    """Every release on the Forgejo API at ``api``; [] when the list can't be
    read (the release is then treated as the first, so it is reflash)."""
    releases = []
    for page in range(1, MAX_PAGES + 1):
        req = urllib.request.Request("%s/releases?limit=%d&page=%d&draft=false"
                                     % (api, PAGE, page))
        if token:
            req.add_header("Authorization", "token " + token)
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                batch = json.load(resp)
        except (OSError, ValueError) as exc:
            sys.stderr.write("could not read the release list: %s\n" % exc)
            return []
        if not isinstance(batch, list):
            return []
        releases += batch
        if len(batch) < PAGE:
            return releases
    sys.stderr.write("the release list has more than %d pages\n" % MAX_PAGES)
    return []


def main(argv):
    version, prerelease, update_kind, commit = argv[1:5]
    releases = fetch_releases(os.environ.get("RELEASES_API", ""),
                              os.environ.get("TOKEN", ""))
    kind, reason, min_from = decide(version, prerelease == "true", update_kind,
                                    commit, releases)
    data = manifest(version, kind, commit, reason, min_from)
    sys.stdout.write(json.dumps(data, indent=2) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
