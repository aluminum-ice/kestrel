"""Tests for the page renderer (no Ghost, browser or release tree needed):

    python3 -m pytest tools/ghost-guide
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import render  # noqa: E402

MD = """# Kestrel operator guide

Intro with a [link](#plugins-1).

---

## Plugins

Text.

### Check now (don't wait for the timer)

| Setting | Meaning |
| --- | --- |
| `a` | b |

## Command reference

### Plugins

```bash
sudo kestrel plugins list
```
"""

PAGE = {"subtitle": "Sub", "hero_caption": "At a glance."}


def test_slugify_matches_ghost():
    assert render.slugify("Stage the card before first boot (no SSH)") == \
        "stage-the-card-before-first-boot-no-ssh"
    assert render.slugify("Check now (don't wait for the timer)") == \
        "check-now-dont-wait-for-the-timer"
    assert render.slugify('Bluetooth link is up but the dashboard says "offline"') == \
        "bluetooth-link-is-up-but-the-dashboard-says-offline"


def test_body_drops_h1_and_numbers_repeated_headings():
    body, headings = render.render_body(MD)
    assert "operator guide" not in body
    assert [slug for _l, _t, slug in headings] == [
        "plugins", "check-now-dont-wait-for-the-timer", "command-reference", "plugins-1"]
    assert '<h3 id="plugins-1">Plugins</h3>' in body
    assert "<table>" in body and 'class="language-bash"' in body


def test_broken_internal_link_is_an_error():
    with pytest.raises(ValueError, match="nowhere"):
        render.render_body("## A\n\nSee [x](#nowhere).\n")


def test_contents_nests_h3_under_h2():
    _body, headings = render.render_body(MD)
    toc = render.contents_html(headings)
    assert toc.startswith('<h2 id="contents">Contents</h2><ol><li><a href="#plugins">')
    assert '<ul><li><a href="#check-now-dont-wait-for-the-timer">' in toc
    assert toc.count("<li>") == 4 and toc.endswith("</ul></li></ol>")


def _fig(key, place, after=None):
    return {"key": key, "place": place, "after": after,
            "html": render.figure_html("shots/%s.png" % key, 904, 1908, key + " alt")}


def test_page_layout_and_figure_placement():
    notice = render.notice_html("1.2.3", "1 May 2026", "https://example.org/r/1.2.3")
    page, _ = render.build_page(MD, PAGE, [_fig("hero-a", "hero"),
                                           _fig("after-cmd", "after", "command-reference")],
                                notice)
    order = [page.index(s) for s in ("<em>Sub</em>", "Kestrel 1.2.3", "hero-a.png",
                                     "At a glance.", 'id="contents"', 'id="plugins"')]
    assert order == sorted(order)
    assert page.index('id="command-reference"') < page.index("after-cmd.png") \
        < page.index('id="plugins-1"')


def test_figure_after_missing_heading_is_an_error():
    with pytest.raises(ValueError, match="gone"):
        render.build_page(MD, PAGE, [_fig("x", "after", "gone")], "")


def test_blocks_ignore_ghost_markup_differences():
    ours = render.figure_html("shots/phone-plugins.png", 904, 1908, "Plugins page",
                              "The Plugins page.") + "<p>Hello <code>x</code></p>"
    ghosts = ('<figure class="kg-card kg-image-card kg-card-hascaption"><img '
              'src="https://blog.example/content/images/2026/10/phone-plugins-2.png" '
              'class="kg-image" alt="Plugins page" srcset="a 600w"><figcaption><span '
              'style="white-space: pre-wrap;">The Plugins page.</span></figcaption></figure>'
              "<!--kg-card-begin: html--><p>Hello <code>x</code></p><!--kg-card-end: html-->")
    assert render.blocks(ours) == render.blocks(ghosts) == [
        "[figure phone-plugins: Plugins page]", "The Plugins page.", "Hello x"]
    assert render.image_sources(ghosts) == {
        "phone-plugins": "https://blog.example/content/images/2026/10/phone-plugins-2.png"}
