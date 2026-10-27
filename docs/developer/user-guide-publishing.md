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

It runs on a desktop, not in CI, and needs nothing from the device. Set it up
once (Linux shown):

```bash
python3 -m venv ~/.venvs/kestrel-guide
~/.venvs/kestrel-guide/bin/pip install -r tools/ghost-guide/requirements.txt
~/.venvs/kestrel-guide/bin/playwright install webkit
sudo ~/.venvs/kestrel-guide/bin/playwright install-deps webkit   # Linux system libraries
```

`--browser chromium` (after `playwright install chromium`) works where WebKit
can't run, but it is not Safari's engine, so publish with WebKit.

Publishing needs a Ghost **Admin API key** from a dedicated Custom Integration
(Ghost Admin, Settings, Integrations, *Add custom integration*, for example
"Kestrel guide publisher"), so it can be revoked on its own. The key has full
admin rights: keep it in your environment only, never in the repo. For example,
in a `chmod 600` file outside the repo:

```bash
# ~/.config/kestrel-ghost.env
GHOST_URL=https://www.3ntropy.io
GHOST_ADMIN_API_KEY=<id>:<secret>
```

```bash
set -a; . ~/.config/kestrel-ghost.env; set +a
```

The commands (each fetches release tags first; `--no-fetch` skips that):

```bash
PY=~/.venvs/kestrel-guide/bin/python
$PY tools/ghost-guide/ghost_guide.py status            # live page's release vs the latest stable
$PY tools/ghost-guide/ghost_guide.py diff              # text/figure changes publishing would make
$PY tools/ghost-guide/ghost_guide.py build             # page + screenshots into tools/ghost-guide/out/<tag>/
$PY tools/ghost-guide/ghost_guide.py push --preview    # publish to a draft "<slug>-preview" page
$PY tools/ghost-guide/ghost_guide.py push              # publish the live page
```

`--tag X.Y.Z` targets a specific stable release instead of the latest; a beta
tag is refused, as is a tag without a published, non-draft, non-pre-release
release. `build` and `diff --shots` write `out/<tag>/preview.html` (a rough
local preview, not the Ghost theme) and the screenshots; `out/` is git-ignored.

### After a stable release

1. `status` — confirm the page is behind the new release.
2. `diff` — read the changes; they should be exactly the release's `GUIDE_MD`
   changes. Open `out/<tag>/preview.html` and the screenshots in
   `out/<tag>/shots/`.
3. `push`.

### The first publish

The page predates the marker, so `push` refuses until you adopt it once. Run
`push --preview` and check the draft preview in Ghost (layout, the notice
callout, the tables, the contents links), then `push --force`. After that,
plain `push` is enough.

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
