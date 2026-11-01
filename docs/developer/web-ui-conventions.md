# Web UI conventions

How every built-in web page in kestrel is built. It applies to the daemon
status and management pages, to any plugin web view (`on_webhook`), and to the
e-ink web rendering. Every built-in page follows this so the pages stay
consistent, mobile-friendly, and dependency-free on the device.

This is a style guide, not a versioned contract: it has no `contract_version`.
When a rule here stops making sense, change the rule and the pages together.

## Core decision: server-rendered, dependency-free

**The pages are plain HTML rendered by the daemon in Python, styled with one
inline stylesheet. No JavaScript framework, no SPA, no client-side build step.**
A small amount of hand-written inline `<script>` is allowed for a specific job
(e.g. polling `/status.json` after a settings change); a framework is not.

Why not React / Vue / Ant Design (antd) / any component library:

- **No JS toolchain exists in the build.** The image is built by Packer +
  Ansible + Python (see [`CLAUDE.md`](../../CLAUDE.md)). There is no Node, npm,
  webpack or Vite anywhere in the pipeline, and adding one to compile a bundle
  is a large, new moving part for one status page.
- **Nothing under `builder/extra/` may be generated.** Every file there is a
  rebuild input copied verbatim into the image. A bundler's output (minified
  JS/CSS) would be committed build artifacts, which the repo forbids.
- **The device and link are small.** The page is served over the management
  link — often a Bluetooth PAN — to a phone, from a Pi Zero 2 W (512 MB). A
  framework runtime plus a component library is hundreds of KB of JS for a page
  that is a table and a couple of forms. Server-rendered HTML is a few KB.
- **Offline.** The device has no internet guarantee and the page must never
  depend on a CDN. Everything is inline; there are no external `<script>`,
  `<link>`, `@import`, font, or image requests.

A modern, clean, mobile-friendly look is reached with CSS alone — see below.
That is the bar for "beautiful"; a component library is not needed to clear it.

## Mobile-first and responsive

The page is read on a phone over the management link, so design for a narrow
screen first and let it widen.

- **Viewport.** Every page sets
  `<meta name="viewport" content="width=device-width,initial-scale=1">`.
  `_page()` does this for all pages.
- **Fluid container.** The body is a centered column with a sane `max-width`
  (≈60em) and a side gutter; it never forces horizontal scrolling of the page.
- **Touch targets.** Inputs and buttons are comfortably tappable (≈44px tall),
  full-width up to a readable `max-width`, with 1rem text so iOS does not zoom
  on focus.
- **Tables reflow to cards.** A multi-column table (the plugin list) must not
  make a phone scroll sideways. Below a breakpoint (currently `40em`) the table
  becomes a stack of cards: `thead` is visually hidden, each `tr` is a bordered
  card, each `td` is a block whose header label is shown from a `data-label`
  attribute via `::before`. Build the markup with `<thead>`/`<tbody>` and give
  each data cell a `data-label` so the reflow has its labels.
- **No fixed pixel widths** on layout; use `%`, `rem`, `max-width`, and flexbox
  so the page adapts from a phone to a laptop.

## Theming: CSS variables + dark mode

- Define colours as CSS custom properties on `:root`, and redefine them under
  `@media (prefers-color-scheme: dark)` so light and dark share one ruleset.
- Give `body` an explicit `color` and `background`; never rely on the UA
  default, which breaks in dark mode.
- **Status is never colour alone** (accessibility). Where colour carries
  meaning — e.g. the plugin name cell shaded green/gray/red for
  loaded/disabled/failed — also give it text: a legend, and a `title` (and/or
  visible word) so a screen reader and a colour-blind reader get the state too.

## How a page is assembled

- **One inline stylesheet, shared.** `_STATUS_CSS` holds the whole stylesheet
  and every page is wrapped by `_page(title, body, head="")`, so all pages
  (status, manage, applied) look the same and dark mode is automatic. A new page
  goes through `_page()`; it does not carry its own `<style>`. When one thing
  needs a different rendering on a phone (the `map` plugin's portrait map), the
  page carries both and marks them `.wide-only` / `.narrow-only`; the shared
  stylesheet shows the one that fits at the 40em breakpoint.
- **Every plugin page opens with the same links back.** The shell puts
  `<p><a href="/plugins">&larr; Plugins</a> &middot; <a href="/">Dashboard</a></p>`
  above the body of every page a plugin returns with `context.page()`, error and
  "not found" pages included; with a token login both links carry `?token=…`.
  The order and wording are fixed: Plugins first, then Dashboard. A plugin never
  writes this line itself (a test fails on any shipped plugin that does, since
  it would show twice); its body starts with its own `<h1>`, and any extra links
  a page needs (Cancel, the JSON view) go below the heading.
- **Assemble by concatenation, not `str.format`, for anything holding CSS or
  JS.** The stylesheet contains literal `{` `}`, which `str.format` reads as
  fields (this once 500'd the page with `KeyError: 'font-family'`). Per-row
  `.format()` is fine as long as the template has only `{}` placeholders and no
  literal braces.
- **Escape everything.** All interpolated text goes through `_esc()`; any value
  embedded in inline `<script>` goes through `_js()`. Plugin-supplied strings
  (names, notes, option values) are untrusted.

## Security conventions (every web page follows these)

The web subsystem is described in
[Architecture](architecture.md); the rules a new page must keep:

- **Bind to the management link only**, by interface address — loopback plus
  the management interfaces that have an IPv4 (`resolve_bind_hosts`), never a
  `0.0.0.0` wildcard and never the capture dongle.
- **Honour `[web] auth`.** `system` (default) verifies the device login against
  `/etc/shadow` through the root helper, so the web login is the SSH login and
  the two never drift; `basic`/`token` use a web-only credential; `none` is open
  and strictly read-only. Pages fail closed on an unknown mode or empty
  credential.
- **State changes need auth + CSRF.** Any form that writes posts the per-run
  CSRF token; a `GET` never changes state. With auth `none` the page shows no
  controls.
- **Response headers on every page** (`_page_headers`): `X-Frame-Options: DENY`,
  `Content-Security-Policy: frame-ancestors 'none'`, `Referrer-Policy:
  no-referrer`, `Cache-Control: no-store`.
- **Never echo a secret** back into a page; a secret field is a password input
  that is blank and, left blank, keeps the stored value.
- **Privileged work goes through the root socket helpers** (`plugins-ctl`),
  which never import plugin code; the daemon runs as `pi`.

## Where the code lives

- `builder/extra/files/kestrel_pi/kestrel_pi/web.py` — the Flask app,
  `_STATUS_CSS`, `_page()` (which appends the Documentation footer), the
  renderers, the small `_render_markdown()` guide renderer, and `WebServer`
  (per-interface bind).
- `builder/extra/files/kestrel_pi/kestrel_pi/userguide.py` — `GUIDE_MD`,
  the operator user guide (Markdown) served at `/guide` and linked from every
  page's footer. **Keep it current as features ship**: when a feature changes
  how an operator configures or uses the device, update `GUIDE_MD` in the same
  change. It must stay free of personal information (it ships in the image).
- `builder/extra/files/kestrel_pi/tests/test_web.py` — renders the pages and
  asserts the markup, the auth/CSRF behaviour, and the headers.

## Checklist for a new web page or view

- [ ] Rendered server-side in Python; no framework, no build step, no external
      resource request.
- [ ] Wrapped by `_page()` (so it gets the viewport meta, the shared stylesheet,
      and dark mode) — not its own `<style>`.
- [ ] A plugin page is returned with `context.page()`, so the shell adds the
      "← Plugins · Dashboard" links; its body starts with its `<h1>` and never
      repeats those links.
- [ ] Reads well at phone width: no sideways scroll; any wide table reflows to
      cards (`data-label` + hidden `thead`); touch-sized controls.
- [ ] Colour meaning is backed by text/label (not colour alone).
- [ ] All interpolated text through `_esc()`; script values through `_js()`.
- [ ] Served on the management link only; honours `[web] auth`; writes carry the
      CSRF token; the security headers are present; no secret is echoed back.
- [ ] Covered by a test in `test_web.py`.
