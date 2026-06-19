# TODO: Multi-unit DXF splitting (deferred)

**Status:** deferred — not built. Captured so it isn't forgotten.
**Companion:** `DXF_INGEST_NORMALIZER_SPEC.md` (layer auto-detection, which shipped first).

## The case

`dxf/539 armstrong/A12_ENLARGED UNITS.dxf` is **not a single unit** — it's an "enlarged
units" key sheet with **four different unit types drawn side-by-side on one page**:

- `UNIT TYPE 2-E   (1,175.0 SQ.FT. - 2 UNITS)`
- `UNIT TYPE 2-E.1 (1,182.0 SQ.FT. - 3 UNITS)`
- `UNIT TYPE 2-F.1 (1,331.0 SQ.FT. - 3 UNITS)`
- `UNIT TYPE 2-F`

With layer auto-detection (`infer_layer_map`) it now **parses and renders** — but as one
combined sheet showing all four units at once (verified: ~2,900 prims, ~45 room labels
across the four units). That is the **expected interim behavior**, not a bug. The app
targets one marketing sheet per unit, so A12 needs to be split.

## Why this is a separate, harder feature

Layer auto-detection is a naming problem (solved). Splitting is a **geometry-clustering**
problem: the app has to decide where one unit's drawing ends and the next begins, then
crop + render each independently. No single layer or field marks the boundaries.

## Sketch of an approach (not committed)

1. **Detect unit count + anchors** from the title callouts already harvested as text
   (`UNIT TYPE …` strings via `parse`'s `suggestions`/`ignored_text`). Each callout's XY
   is a rough anchor for one unit.
2. **Cluster geometry into units** — two candidate signals:
   - *Spatial gaps:* histogram the wall-geometry X (and/or Y) extents; the large empty
     gutters between unit blocks are the split lines.
   - *Title proximity:* assign each prim to its nearest unit-title anchor.
   Spatial-gap clustering is likely more robust; title proximity is a tie-breaker.
3. **Per-unit crop + render:** for each cluster, compute its sub-extents and run the
   existing render path on just that subset of prims + that unit's labels. Reuse the
   fit-to-page scaling — each unit sheet auto-scales on its own extents.
4. **UI:** one upload of a multi-unit DXF → a picker / batch producing N unit sheets,
   each editable and savable like a normal single-unit sheet.

## Acceptance

- Uploading A12 yields **one sheet per detected unit** (4 here), each correctly cropped
  to its own walls + its own room labels, not the whole sheet.
- A genuine single-unit file (Armstrong, 800 Princess) is detected as 1 unit and behaves
  exactly as today (no spurious splitting).
- Split boundaries are reviewable/adjustable by a human (same auto-detect + override
  philosophy as the layer mapping) rather than silently guessed.

## Open questions

- Is X-only gutter detection enough, or do some key sheets stack units in a grid (needs
  2-D clustering)?
- Do title callouts always exist and sit inside their unit, or can they float in a legend?
- Should split sheets inherit one shared property/brand, or be independently editable
  from the first render?
