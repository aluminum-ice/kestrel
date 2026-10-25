#!/usr/bin/env python3
"""Generate grouped release notes for a kestrel release.

Reads the release metadata from update-manifest.json (version, kind, commit,
reflash reason), collects the commits unique to this release (reachable from the
release commit but not from any earlier tag), groups them into New /
Improvements / Fixes, and prints Markdown notes to stdout. CI tooling only; not
shipped in the image.

Usage (CI):
    python3 .forgejo/gen-release-notes.py update-manifest.json > release-notes.md

Options (for local testing):
    --ref <rev>            commit/tag to describe (default: HEAD)
    --prev-tags "a b c"    space-separated prior tags (default: every git tag
                           except this release's version)
"""
import argparse
import json
import subprocess
import sys


def git(*args):
    return subprocess.run(
        ["git", *args], capture_output=True, text=True, check=True
    ).stdout


def classify(subject):
    """Bucket a commit subject into new / improvement / fix by its first word."""
    first = subject.split(" ", 1)[0].lower().rstrip(":")
    if first in ("fix", "fixes", "fixed", "correct", "prevent", "stop"):
        return "fix"
    if first in ("add", "introduce", "create", "ship"):
        return "new"
    return "improvement"


def collect_commits(ref, prev_tags):
    args = ["log", "--no-merges", "--pretty=%s", ref]
    args += ["--not", *prev_tags] if prev_tags else []
    out = git(*args)
    seen, subjects = set(), []
    for line in out.splitlines():
        s = line.strip()
        # Drop pure-chore noise that is not interesting in release notes.
        if not s or s in seen:
            continue
        seen.add(s)
        subjects.append(s)
    return subjects


def render(version, kind, commit, reason, subjects):
    groups = {"new": [], "improvement": [], "fix": []}
    for s in subjects:
        groups[classify(s)].append(s)

    lines = []
    sections = [
        ("New", groups["new"]),
        ("Improvements", groups["improvement"]),
        ("Fixes", groups["fix"]),
    ]
    for label, items in sections:
        if not items:
            continue
        lines.append(f"**{label}**")
        lines.append("")
        lines += [f"- {s}" for s in items]
        lines.append("")

    if not any(items for _, items in sections):
        lines.append("Maintenance release.")
        lines.append("")

    if kind == "app-layer":
        install = "_Install: app-layer — apply in place via the updates plugin._"
    elif kind == "reflash":
        install = f"_Install: reflash — {reason}._" if reason else "_Install: reflash — flash the image._"
    else:
        install = ""
    if install:
        lines.append(install)
        lines.append("")

    lines.append("---")
    lines.append(f"_Built from {commit}._")
    return "\n".join(lines) + "\n"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("manifest")
    ap.add_argument("--ref", default="HEAD")
    ap.add_argument("--prev-tags", default=None)
    opts = ap.parse_args()

    with open(opts.manifest) as f:
        m = json.load(f)
    version = m["version"]
    kind = m.get("kind", "")
    commit = m.get("commit", "")
    reason = m.get("requires_reflash_reason", "")

    if opts.prev_tags is not None:
        prev_tags = opts.prev_tags.split()
    else:
        prev_tags = [t for t in git("tag").split() if t and t != version]

    subjects = collect_commits(opts.ref, prev_tags)
    sys.stdout.write(render(version, kind, commit[:7] if commit else "", reason, subjects))


if __name__ == "__main__":
    main()
