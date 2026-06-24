# Bundled fallback fonts

These ship inside the deployment so `resvg` can rasterize text **even on hosts
with no system fonts installed** (notably Vercel's Python Lambda, which has
none). Without them, PNG exports come out with a blank header/footer and missing
room labels — the SVG looks fine in the browser because the browser supplies the
fonts, but the server-side raster has nothing to draw with.

They are chosen to be **metric-compatible** with the design's CSS font stacks so
layout/typography barely shifts vs. a machine that *does* have the named fonts:

| File | Family | Substitutes for | License |
|------|--------|-----------------|---------|
| `Arimo-Regular.ttf`, `Arimo-Bold.ttf` | Arimo | Helvetica / Arial (the sans stack) | see `Arimo-LICENSE.txt` |
| `Gelasio-Regular.ttf`, `Gelasio-Bold.ttf` | Gelasio | Georgia (the serif stack — header lockup, suite title) | see `Gelasio-LICENSE.txt` |

Static Regular/Bold weights were instanced (via fontTools) from the upstream
Google Fonts variable fonts (`Arimo[wght]`, `Gelasio[wght]`).

`render.py` loads these by absolute path and maps the generic `serif` /
`sans-serif` families to them (`render_png`), so the `Georgia,…,serif` and
`Helvetica,…,sans-serif` stacks resolve here when the named faces are absent.
On a machine that *has* Georgia/Arial (e.g. local Windows dev), resvg matches
those by name first and these are never used — so dev output is unchanged.
