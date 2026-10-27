"""Render the operator guide (``GUIDE_MD``) into the Ghost page's HTML.

The Ghost page is a build output: the release's ``GUIDE_MD`` rendered with
markdown-it (CommonMark + tables, the same renderer that produced the original
page), wrapped in the Ghost-only parts from ``guide.toml`` (subtitle, release
notice, hero figures, contents list) with the screenshots inserted after their
headings. Ghost converts this HTML into its own cards on upload (``?source=html``):
prose becomes native paragraphs/headings/code, each table an HTML card and each
``kg-image-card`` figure an image card, which is the page's existing layout.
"""

import html
import importlib.util
import re

from markdown_it import MarkdownIt

# Ghost generates heading ids from the heading text, lower-cased with runs of
# anything but letters and digits turned into "-", quotes dropped, and a
# repeated id suffixed -1, -2, ... in document order. Matching it keeps the
# contents list and GUIDE_MD's own #anchor links working on the page.
_QUOTES = re.compile(r"['‘’\"“”]")
_NON_ALNUM = re.compile(r"[^a-z0-9]+")


def slugify(text):
    text = _QUOTES.sub("", text.lower())
    return _NON_ALNUM.sub("-", text).strip("-")


def load_guide_md(userguide_path):
    """``GUIDE_MD`` from a release's ``userguide.py``, without importing the
    rest of the device package."""
    spec = importlib.util.spec_from_file_location("kestrel_userguide", userguide_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.GUIDE_MD


def _heading_text(inline):
    return "".join(child.content for child in inline.children or ()
                   if child.type in ("text", "code_inline"))


def render_body(md_text, reserved=("contents",)):
    """Render ``md_text`` minus its H1 (the Ghost page title replaces it).

    Returns ``(body_html, headings)`` where ``headings`` is a list of
    ``(level, text, slug)`` for every h2/h3, slugged the way Ghost slugs them.
    ``reserved`` are ids already used on the page above the body."""
    md = MarkdownIt("commonmark").enable("table")
    tokens = md.parse(md_text)
    if tokens and tokens[0].type == "heading_open" and tokens[0].tag == "h1":
        tokens = tokens[3:]

    seen = {slug: 1 for slug in reserved}
    headings = []
    for i, token in enumerate(tokens):
        if token.type != "heading_open":
            continue
        text = _heading_text(tokens[i + 1])
        base = slugify(text)
        count = seen.get(base, 0)
        slug = base if count == 0 else "%s-%d" % (base, count)
        seen[base] = count + 1
        token.attrSet("id", slug)
        if token.tag in ("h2", "h3"):
            headings.append((int(token.tag[1]), text, slug))

    body = md.renderer.render(tokens, md.options, {})
    ids = {slug for _level, _text, slug in headings}
    broken = sorted(set(re.findall(r'href="#([^"]+)"', body)) - ids)
    if broken:
        raise ValueError("GUIDE_MD links to anchors no heading has: %s"
                         % ", ".join(broken))
    return body, headings


def contents_html(headings):
    """The page's numbered contents list: each h2 with its h3s nested under it."""
    out = ['<h2 id="contents">Contents</h2><ol>']
    open_item = False
    open_sub = False
    for level, text, slug in headings:
        link = '<a href="#%s">%s</a>' % (slug, html.escape(text))
        if level == 2:
            if open_sub:
                out.append("</ul>")
                open_sub = False
            if open_item:
                out.append("</li>")
            out.append("<li>" + link)
            open_item = True
        else:
            if not open_sub:
                out.append("<ul>")
                open_sub = True
            out.append("<li>" + link + "</li>")
    if open_sub:
        out.append("</ul>")
    if open_item:
        out.append("</li>")
    out.append("</ol>")
    return "".join(out)


def figure_html(src, width, height, alt, caption=""):
    """An image card in the markup Ghost both renders and parses back."""
    cls = "kg-card kg-image-card" + (" kg-card-hascaption" if caption else "")
    img = ('<img src="%s" class="kg-image" alt="%s" loading="lazy" width="%d" '
           'height="%d">' % (html.escape(src), html.escape(alt), width, height))
    cap = "<figcaption>%s</figcaption>" % html.escape(caption) if caption else ""
    return '<figure class="%s">%s%s</figure>' % (cls, img, cap)


def notice_html(tag, published, release_url):
    """The callout naming the stable release the page matches."""
    when = (" published %s" % html.escape(published)) if published else ""
    return (
        '<div class="kg-card kg-callout-card kg-callout-card-blue">'
        '<div class="kg-callout-emoji">\U0001f4cc</div>'
        '<div class="kg-callout-text">This guide matches <b>Kestrel %s</b>, a '
        'stable release%s. Beta releases may differ. '
        '<a href="%s">Release notes</a></div></div>'
        % (html.escape(tag), when, html.escape(release_url)))


def build_page(md_text, page_cfg, figures, notice):
    """Assemble the page HTML.

    ``figures`` is a list of dicts with ``place`` (``"hero"`` or ``"after"``),
    ``after`` (a heading slug, for ``"after"``) and ``html`` (the figure
    markup). A figure whose heading no longer exists is an error, so a renamed
    section can't silently drop its screenshot."""
    body, headings = render_body(md_text)
    for fig in figures:
        if fig["place"] != "after":
            continue
        match = re.search(r'<h([23]) id="%s">.*?</h\1>' % re.escape(fig["after"]), body)
        if match is None:
            raise ValueError("figure %s: no heading with id %r in this release's "
                             "guide" % (fig["key"], fig["after"]))
        body = body[:match.end()] + fig["html"] + body[match.end():]

    parts = ["<p><em>%s</em></p>" % html.escape(page_cfg["subtitle"]), notice]
    parts += [fig["html"] for fig in figures if fig["place"] == "hero"]
    if page_cfg.get("hero_caption"):
        parts.append("<p>%s</p>" % html.escape(page_cfg["hero_caption"]))
    parts.append(contents_html(headings))
    parts.append(body)
    return "".join(parts), headings


# -- comparison ---------------------------------------------------------------

_BLOCK_SPLIT = re.compile(
    r"</?(?:p|h[1-6]|li|pre|tr|th|td|hr|ul|ol|table|thead|tbody|blockquote|div"
    r"|figcaption)\b[^>]*>")
_FIGURE = re.compile(r"<figure\b.*?</figure>", re.S)
_IMG_SRC = re.compile(r'<img\b[^>]*\bsrc="([^"]+)"')
_IMG_ALT = re.compile(r'<img\b[^>]*\balt="([^"]*)"')


def _clean(fragment):
    text = html.unescape(re.sub(r"<[^>]+>", "", fragment))
    return re.sub(r"\s+", " ", text).strip()


def figure_stem(src):
    """``phone-dashboard`` from ``.../phone-dashboard-3.png``: Ghost adds -N to
    an upload whose name is taken, so the stem identifies the figure."""
    name = src.rsplit("/", 1)[-1].rsplit(".", 1)[0]
    return re.sub(r"-\d+$", "", name)


def blocks(page_html):
    """The page as a list of comparable lines: one per block of text, and one
    ``[figure stem: alt]`` per image. Ghost's re-rendering (srcset, spans,
    card comments) doesn't change it, so it diffs and hashes a page stably."""
    page_html = re.sub(r"<!--.*?-->", "", page_html, flags=re.S)
    out = []
    pos = 0
    for match in _FIGURE.finditer(page_html):
        out += _text_blocks(page_html[pos:match.start()])
        fig = match.group(0)
        src = _IMG_SRC.search(fig)
        alt = _IMG_ALT.search(fig)
        out.append("[figure %s: %s]" % (figure_stem(src.group(1)) if src else "?",
                                        html.unescape(alt.group(1)) if alt else ""))
        out += _text_blocks(fig)
        pos = match.end()
    out += _text_blocks(page_html[pos:])
    return out


def _text_blocks(fragment):
    return [text for text in (_clean(p) for p in _BLOCK_SPLIT.split(fragment)) if text]


def image_sources(page_html):
    """``{stem: src}`` for every image on the page."""
    return {figure_stem(src): src for src in _IMG_SRC.findall(page_html)}


def heading_ids(page_html):
    return set(re.findall(r'<h[1-6][^>]*\bid="([^"]+)"', page_html))
