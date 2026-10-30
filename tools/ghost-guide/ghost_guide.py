#!/usr/bin/env python3
"""Keep the Ghost user-guide page in step with the latest stable release.

The page is generated, never hand-edited: the release's GUIDE_MD (the guide the
device serves at /guide), wrapped in guide.toml's subtitle, a notice naming the
release, a contents list and screenshots rendered from that release's own code.

    status   which stable release the live page matches, and the latest one
    diff     what publishing would change on the live page (text and figures)
    build    render the page and screenshots locally, into out/<tag>/
    push     publish to Ghost (needs GHOST_URL and GHOST_ADMIN_API_KEY)

See README.md for setup.
"""

import argparse
import datetime
import difflib
import hashlib
import io
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import tomllib

import requests

import render

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "out")
STABLE_TAG = re.compile(r"^\d+\.\d+\.\d+$")
MARKER = re.compile(r"<!-- kestrel-guide release=(\S+) content=(\w+) -->")
# Figure sizes when screenshots are skipped (diff without --shots).
DEFAULT_DIMS = {"eink": (824, 560), "phone": (904, 1908)}


def die(message):
    print("error: " + message, file=sys.stderr)
    sys.exit(1)


def load_config():
    with open(os.path.join(HERE, "guide.toml"), "rb") as handle:
        cfg = tomllib.load(handle)
    for fig in cfg["figure"]:
        fig["place"] = "hero" if fig.get("place") == "hero" else "after"
    return cfg


# -- releases -------------------------------------------------------------------

def repo_root():
    return subprocess.run(["git", "-C", HERE, "rev-parse", "--show-toplevel"],
                          check=True, capture_output=True, text=True).stdout.strip()


def _version_key(tag):
    return tuple(int(part) for part in tag.split("."))


def stable_tags(fetch=True):
    root = repo_root()
    if fetch:
        subprocess.run(["git", "-C", root, "fetch", "--tags", "--quiet"], check=False)
    tags = subprocess.run(["git", "-C", root, "tag", "--list"], check=True,
                          capture_output=True, text=True).stdout.split()
    return sorted((t for t in tags if STABLE_TAG.match(t)), key=_version_key)


def release_info(cfg, tag):
    """The published, non-draft, non-prerelease release for ``tag``."""
    rel = cfg["release"]
    url = "%s/api/v1/repos/%s/releases/tags/%s" % (rel["host"], rel["repo"], tag)
    resp = requests.get(url, timeout=30)
    if resp.status_code == 404:
        die("tag %s has no published release on %s" % (tag, rel["host"]))
    resp.raise_for_status()
    data = resp.json()
    if data.get("draft") or data.get("prerelease"):
        die("release %s is a draft or pre-release; the page follows stable releases only"
            % tag)
    published = ""
    if data.get("published_at"):
        when = datetime.datetime.fromisoformat(data["published_at"].replace("Z", "+00:00"))
        published = when.strftime("%-d %B %Y")
    page = data.get("html_url") or "%s/%s/releases/tag/%s" % (rel["host"], rel["repo"], tag)
    return published, page


def export_tree(tag, dest):
    """The release's builder/extra/files, extracted from git (not the work tree)."""
    if os.path.exists(dest):
        shutil.rmtree(dest)
    os.makedirs(dest)
    archive = subprocess.run(["git", "-C", repo_root(), "archive", "--format=tar", tag,
                              "builder/extra/files"], check=True, capture_output=True).stdout
    with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
        tar.extractall(dest, filter="data")
    return os.path.join(dest, "builder", "extra", "files")


# -- building -------------------------------------------------------------------

class Build:
    """A rendered page for one release, with its screenshots (if taken)."""

    def __init__(self, cfg, tag, shots, browser):
        self.cfg = cfg
        self.tag = tag
        tags = stable_tags(fetch=False)
        if tag not in tags:
            die("%s is not a local stable tag (have: %s)" % (tag, ", ".join(tags[-5:])))
        previous = ([t for t in tags if _version_key(t) < _version_key(tag)] or [tag])[-1]
        self.published, self.release_url = release_info(cfg, tag)
        self.dir = os.path.join(OUT, tag)
        files = export_tree(tag, os.path.join(self.dir, "tree"))
        self.md = render.load_guide_md(
            os.path.join(files, "kestrel_pi", "kestrel_pi", "userguide.py"))
        self.shots = {}
        if shots:
            import fixture
            import shots as shots_mod

            with tempfile.TemporaryDirectory() as work:
                demo = fixture.Demo(files, work, tag, previous, self.release_url)
                base, stop = demo.serve()
                try:
                    self.shots = shots_mod.capture(demo, base, cfg["figure"],
                                                   os.path.join(self.dir, "shots"),
                                                   browser=browser)
                finally:
                    stop()

    def page_html(self, srcs=None):
        """The page, with each figure's ``src`` from ``srcs`` (``{key: url}``),
        else the local screenshot path."""
        figs = []
        for fig in self.cfg["figure"]:
            kind = "eink" if fig.get("source") == "eink" else "phone"
            _path, width, height = self.shots.get(fig["key"], (None,) + DEFAULT_DIMS[kind])
            src = (srcs or {}).get(fig["key"], "shots/%s.png" % fig["key"])
            figs.append(dict(fig, html=render.figure_html(
                src, width, height, fig["alt"], fig.get("caption", ""))))
        notice = render.notice_html(self.tag, self.published, self.release_url)
        page, _headings = render.build_page(self.md, self.cfg["page"], figs, notice)
        return page

    def write_local(self):
        page = self.page_html()
        with open(os.path.join(self.dir, "page.html"), "w", encoding="utf-8") as handle:
            handle.write(page)
        preview = ('<!doctype html><meta charset="utf-8"><meta name="viewport" '
                   'content="width=device-width,initial-scale=1"><title>Guide preview '
                   + self.tag + '</title><style>body{max-width:720px;margin:2em auto;'
                   'padding:0 16px;font:16px/1.6 system-ui,sans-serif}img{max-width:100%;'
                   'height:auto}pre{overflow:auto;background:#f3f4f6;padding:.8em}'
                   'table{border-collapse:collapse}td,th{border:1px solid #ccc;padding:'
                   '.3em}.kg-callout-card{display:flex;gap:.6em;background:#e8f0fe;'
                   'padding:1em;border-radius:6px}figcaption{text-align:center;'
                   'color:#666}</style><h1>Kestrel user guide</h1>' + page)
        path = os.path.join(self.dir, "preview.html")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(preview)
        return path


# -- the live page ----------------------------------------------------------------

def public_content(cfg):
    """The live page's content HTML and its code-injection marker, read from
    the public page (no key needed)."""
    resp = requests.get(cfg["ghost"]["public_url"], timeout=30)
    resp.raise_for_status()
    page = resp.text
    start = re.search(r'<div class="[^"]*\bgh-content\b[^"]*"[^>]*>', page)
    if start is None:
        die("can't find the content block on the public page")
    depth, pos = 1, start.end()
    for tag in re.finditer(r"<(/?)div\b[^>]*>", page[pos:]):
        depth += -1 if tag.group(1) else 1
        if depth == 0:
            content = page[pos:pos + tag.start()]
            break
    else:
        die("unbalanced content block on the public page")
    marker = MARKER.search(page)
    return content, (marker.groups() if marker else None)


def content_hash(page_html):
    return hashlib.sha256("\n".join(render.blocks(page_html)).encode()).hexdigest()[:16]


def show_diff(old_html, new_html, old_name, new_name):
    old, new = render.blocks(old_html), render.blocks(new_html)
    lines = list(difflib.unified_diff(old, new, old_name, new_name, n=1, lineterm=""))
    for line in lines:
        print(line)
    figs_old = [b for b in old if b.startswith("[figure ")]
    figs_new = [b for b in new if b.startswith("[figure ")]
    changed = sum(1 for line in lines if line[:1] in "+-" and line[:3] not in ("+++", "---"))
    print("\n%d line(s) differ; figures %d -> %d" % (changed, len(figs_old), len(figs_new)))
    return changed


# -- commands -------------------------------------------------------------------

def cmd_status(args, cfg):
    tags = stable_tags(fetch=not args.no_fetch)
    if not tags:
        die("no stable release tags found")
    latest = tags[-1]
    content, marker = public_content(cfg)
    if marker is None:
        print("live page: not published by this tool yet (no marker)")
    else:
        release, digest = marker
        edited = digest != content_hash(content)
        print("live page: matches %s%s" % (release, " (edited in Ghost since)" if edited else ""))
    print("latest stable release: %s" % latest)
    if marker is None or marker[0] != latest:
        print("-> run: ghost_guide.py diff, then push")


def _tag(args):
    return args.tag or stable_tags(fetch=not args.no_fetch)[-1]


def cmd_build(args, cfg):
    build = Build(cfg, _tag(args), shots=not args.no_shots, browser=args.browser)
    path = build.write_local()
    print("wrote %s" % path)
    for key, (shot, width, height) in sorted(build.shots.items()):
        print("  %s  %dx%d" % (shot, width, height))


def cmd_diff(args, cfg):
    build = Build(cfg, _tag(args), shots=args.shots, browser=args.browser)
    build.write_local()
    content, _marker = public_content(cfg)
    show_diff(content, build.page_html(), "live page", "release " + build.tag)


def cmd_push(args, cfg):
    url = os.environ.get("GHOST_URL")
    key = os.environ.get("GHOST_ADMIN_API_KEY")
    if not url or not key:
        die("set GHOST_URL and GHOST_ADMIN_API_KEY in the environment (see README.md)")
    from ghostapi import Ghost, GhostError
    import shots as shots_mod

    try:
        ghost = Ghost(url, key)
        slug = cfg["ghost"]["page_slug"]
        live = ghost.page_by_slug(slug)
        if live is None:
            die("no Ghost page with slug %r" % slug)
        marker = MARKER.search(live.get("codeinjection_foot") or "")
        if not args.preview and not args.force:
            if marker is None:
                die("the page has no publish marker yet. Check `diff`, then push once "
                    "with --force to adopt it (or try --preview first).")
            if marker.group(2) != content_hash(live["html"]):
                die("the page was edited in Ghost since %s was published; those edits "
                    "would be overwritten. Move them into the repo, then use --force."
                    % marker.group(1))

        build = Build(cfg, _tag(args), shots=True, browser=args.browser)
        build.write_local()

        # Reuse an image already on Ghost (on the live page, or on the preview
        # page from an earlier --preview) whose pixels are unchanged; upload the rest.
        preview_slug = slug + "-preview"
        preview_page = ghost.page_by_slug(preview_slug)
        candidates = [render.image_sources(live["html"])]
        if preview_page is not None:
            candidates.append(render.image_sources(preview_page["html"]))
        srcs = {}
        with tempfile.TemporaryDirectory() as tmp:
            for fig in cfg["figure"]:
                local, _w, _h = build.shots[fig["key"]]
                for found in candidates:
                    current = found.get(fig["key"])
                    if not current:
                        continue
                    copy = os.path.join(tmp, fig["key"] + ".png")
                    resp = requests.get(current, timeout=60)
                    if not resp.ok:
                        continue
                    with open(copy, "wb") as handle:
                        handle.write(resp.content)
                    if shots_mod.same_pixels(local, copy):
                        srcs[fig["key"]] = current
                        break
                else:
                    srcs[fig["key"]] = ghost.upload_image(local, fig["key"] + ".png")
                    print("uploaded %s -> %s" % (fig["key"], srcs[fig["key"]]))
        page = build.page_html(srcs)

        if args.preview:
            if preview_page is None:
                result = ghost.create_draft_page(live["title"] + " (preview)",
                                                 preview_slug, page)
            else:
                result = ghost.update_page(preview_page, html=page)
            print("draft preview updated: %s/ghost/#/editor/page/%s"
                  % (url.rstrip("/"), result["id"]))
        else:
            if (marker and marker.group(1) == build.tag
                    and render.blocks(page) == render.blocks(live["html"])
                    and render.anchor_links(page) == render.anchor_links(live["html"])):
                print("the page already matches %s; nothing to publish" % build.tag)
                return
            result = ghost.update_page(live, html=page)
            foot = MARKER.sub("", result.get("codeinjection_foot") or "").rstrip()
            stamp = "<!-- kestrel-guide release=%s content=%s -->" % (
                build.tag, content_hash(result["html"]))
            result = ghost.update_page(result, codeinjection_foot=(foot + "\n" + stamp).lstrip())
            print("published %s to %s" % (build.tag, cfg["ghost"]["public_url"]))
    except GhostError as exc:
        die(str(exc))

    missing = sorted(set(re.findall(r'href="#([^"]+)"', page))
                     - render.heading_ids(result["html"]))
    if missing:
        print("warning: Ghost gave some headings different ids; these contents links "
              "are broken: %s" % ", ".join(missing), file=sys.stderr)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("status", "build", "diff", "push"):
        p = sub.add_parser(name)
        p.add_argument("--no-fetch", action="store_true",
                       help="don't fetch release tags from the remote first")
        if name == "status":
            continue
        p.add_argument("--tag", help="a stable release tag (default: the latest)")
        p.add_argument("--browser", default="webkit", choices=("webkit", "chromium"),
                       help="screenshot engine (default webkit, the Safari engine)")
        if name == "build":
            p.add_argument("--no-shots", action="store_true", help="skip the screenshots")
        if name == "diff":
            p.add_argument("--shots", action="store_true",
                           help="also take the screenshots (for the local preview)")
        if name == "push":
            p.add_argument("--preview", action="store_true",
                           help="publish to a draft '<slug>-preview' page instead")
            p.add_argument("--force", action="store_true",
                           help="publish even if the page was edited in Ghost or has "
                                "no marker yet")
    args = parser.parse_args(argv)
    if getattr(args, "tag", None) and not STABLE_TAG.match(args.tag):
        die("--tag must be a stable release (X.Y.Z); the page doesn't follow betas")
    cfg = load_config()
    {"status": cmd_status, "build": cmd_build, "diff": cmd_diff,
     "push": cmd_push}[args.command](args, cfg)


if __name__ == "__main__":
    main()
