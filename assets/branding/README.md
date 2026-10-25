# Kestrel — logo and brand mark

Vector logo assets for the Kestrel identity.

## Concept

The mark is a **windhovering kestrel**. The kestrel is the falcon that holds
itself dead-still in the air — wings working, body locked to a point, eyes fixed
on the ground below — so it can watch and wait. That posture is the device: it
sits in one place and reads the band around it, patient and exact. The primary
mark is that hover seen head-on, wings spread, tail fanned for the hold.

It is drawn as a single solid silhouette on purpose, so it survives every surface
it has to live on — a browser tab, a boot splash, and the 1-bit e-ink display,
where it renders cleanly in plain black or reversed white.

## Files

| File | What it is |
| --- | --- |
| [`kestrel-mark.svg`](kestrel-mark.svg) | Primary mark — hovering kestrel, single colour (`fill="currentColor"`) |
| [`kestrel-mark-stoop.svg`](kestrel-mark-stoop.svg) | Alternate "in motion" mark — the dive (stoop) |
| [`kestrel-mark-twotone.svg`](kestrel-mark-twotone.svg) | Two-tone — slate-blue wings over a rufous body (male-kestrel plumage) |
| [`kestrel-favicon.svg`](kestrel-favicon.svg) | App/tab icon — white bird in a rounded rufous tile, bold down to 16 px |
| [`kestrel-lockup.svg`](kestrel-lockup.svg) | Horizontal lockup — mark + `KESTREL` wordmark |
| [`kestrel-brand-sheet.html`](kestrel-brand-sheet.html) | Standalone brand sheet — the full design presentation (concept, every variant, favicon sizes, palette) in light and dark; open it in a browser |

The single-colour marks use `fill="currentColor"`, so they inherit the surrounding
text colour (`color:` in CSS) — drop them in and they recolour themselves.

## Palette

| Name | Hex | Use |
| --- | --- | --- |
| Rufous (kestrel) | `#E2623C` | primary mark, accents |
| Rufous deep | `#C24A24` | mark on light backgrounds, text accents |
| Slate blue-grey | `#5F7488` | secondary / two-tone wings |
| Ink | `#1B2128` | wordmark on light |
| Bone | `#F3F0EA` | warm light background |

## Wordmark

The lockup sets `KESTREL` in a condensed grotesque (Saira Condensed, with Oswald /
Archivo Narrow / Arial Narrow as fallbacks), 700 weight, wide tracking — a lean,
upright face that echoes the bird's alert vertical hold. In `kestrel-lockup.svg`
the wordmark is live `<text>`, which depends on the font being installed:
**convert it to outlines** before using it anywhere the font is not guaranteed.

## Notes

- These are design assets only — not wired into the image build or the on-device
  web UI. When the rename happens, the favicon and mark can be dropped into the
  dashboard (`kestrel_pi` web pages) and the e-ink renderer.
- Optimise the SVGs with `svgo` (or similar) before shipping if you want them
  minified.
