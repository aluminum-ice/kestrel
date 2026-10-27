# Publishing the user guide to Ghost

The public HTML user guide is a single Ghost page
(`https://www.3ntropy.io/kestrel-user-guide/`). It is **generated, never edited
in Ghost**: `tools/ghost-guide/ghost_guide.py` rebuilds it from the latest
**stable** release and publishes it through the Ghost Admin API. Betas are never
published.

## Where the words come from

The guide flows one way:

1. `docs/user-guide/` — the detailed, per-topic operator guides.
2. `GUIDE_MD` in `kestrel_pi/userguide.py` — the one-page operator guide the
   device serves at `/guide`, kept in step with `docs/user-guide/` in the same
   change (see CLAUDE.md).
3. The Ghost page — that release's `GUIDE_MD`, plus what only the blog has:
   a subtitle, a notice naming the stable release it matches (with a link to
   its release notes), a numbered contents list, and screenshots.

So a change reaches the blog by landing in `GUIDE_MD` and shipping in a stable
release. Anything that has to appear only on the blog (subtitle, captions, which
screenshots go where) lives in `tools/ghost-guide/guide.toml`. Nothing on the
Ghost page should be edited by hand: the next publish overwrites it, and
`push` refuses to run over hand edits until they are moved into the repo.

## How the page is built

- **Text.** `GUIDE_MD` at the release tag (read with `git archive`, not the
  work tree) is rendered with markdown-it (CommonMark + tables), the renderer
  the page was first made with, minus its H1 (Ghost's page title replaces it).
  Ghost converts the HTML into its own cards on upload (`?source=html`): prose
  becomes native paragraphs, headings, lists and code blocks; each table becomes
  an HTML card; each figure an image card. That is the page's existing layout.
- **Anchors.** Ghost gives each heading an id from its text (lower-cased,
  punctuation to `-`, a repeat suffixed `-1`, `-2`, ...). The renderer slugs the
  same way, so the contents list and `GUIDE_MD`'s own `#anchor` links resolve;
  a `GUIDE_MD` link to a heading that doesn't exist fails the build, and `push`
  warns if Ghost assigned an id the contents list doesn't expect.
- **Screenshots.** Every figure is retaken for each release from that release's
  own code. `fixture.py` runs the release's real `kestrel_pi` daemon and web app
  on loopback, with the bundled plugins loaded (their hardware-touching
  `on_loaded` skipped) and made-up data: fictional networks on documentation MAC
  addresses (`00:00:5E:00:53:xx`), a generic GPS position, a learning agent
  mid-session. Labels, layout and wording therefore always match the release.
  When a release changes something the fixture reaches into, it fails loudly
  rather than drawing a misleading page.
  - Phone shots: Playwright's WebKit (Safari's engine) as an **iPhone 14 Pro
    Max in dark mode**: its user agent, touch and mobile layout, a 430 x 932 pt
    screen at 2x, the page in the 430 x 896 pt area under a 36 pt status strip,
    framed with a bezel and the Dynamic Island: 904 x 1908 px.
  - E-ink: the `eink` plugin's own compositor draws the 264 x 176 panel frame
    for the same demo state (mood, voice, counters), scaled 3x on a light margin
    (824 x 560 px).
- **Images on Ghost.** Each figure's image is named after its `key`. On push, a
  live image whose pixels are unchanged is reused; anything else is uploaded
  (Ghost may add `-N` to the name). Ghost's API can't delete images, so replaced
  ones stay in the media library until removed by hand.
- **Marker.** After publishing, the page's footer code injection gets
  `<!-- kestrel-guide release=X.Y.Z content=<hash> -->`: the release and a hash
  of the page content as Ghost rendered it. `status` reads it from the public
  page; `push` uses it to skip a no-op and to detect edits made in Ghost.

## Running it

It runs on a desktop, not in CI, and needs nothing from the device. The
step-by-step runbook (setup, the Admin API key file, what to run in each
terminal, the first publish, the routine after a stable release, and
troubleshooting) is [`tools/ghost-guide/README.md`](../../tools/ghost-guide/README.md);
keep operational steps there, not here.

The key belongs to a dedicated Ghost Custom Integration so it can be revoked on
its own. It has full admin rights, so it lives only in an env file outside the
repo (`~/.config/kestrel-ghost.env`, mode 600) and is loaded only into the
subshell that runs `push`, never into the operator's shell; the tool never
prints it.

### Adding or moving a screenshot

Add a `[[figure]]` to `guide.toml`: a `key` (the image name), `after` (the
heading slug it follows, or `place = "hero"`), the page `path` on the demo
device, `alt` text and an optional `caption`. `focus` scrolls to a piece of
text first; `scenario = "update-available"` shows the dashboard with an in-place
upgrade on offer. A figure whose heading disappears from a later release fails
the build, so move or drop it then. Demo data lives at the top of
`fixture.py`; keep it fictional (no real SSIDs, MACs, places or names).

## Tests

```bash
python3 -m pytest tools/ghost-guide
```

covers the renderer (slugs, contents, figure placement, comparison). It is not
part of `make test` or CI, which don't install the tool's requirements.
