# DXF Ingest Normalizer — Spec

> ## ⚠️ FINDINGS — folder shared & engine confirmed (2026-06-19) — READ FIRST
>
> This spec was written **before anyone could open the files** (see §4, §7: "confirm
> against the engine when the folder is shared"). The folder has since been shared and
> the real engine (`app/backend/engine/parse.py`) was run against all three problem
> DXFs. **The spec's four premises do not hold for this engine — do not implement the
> explode / unit-normalize / 3-tier-seed machinery below; it is redundant or moot.**
>
> | Spec premise | Reality (verified by running `parse.py`) |
> |---|---|
> | Geometry hidden in blocks → renders nothing | ❌ `_collect_entities` **already** recursively explodes `INSERT`s (`virtual_entities`, `MAX_DEPTH=5`). Armstrong → 697 prims, A12 → 2,951 prims. |
> | Deep nesting → one explode isn't enough | ❌ The existing recursion already covers the 2–3 levels present. |
> | Inconsistent units → microscopic vs huge | ❌ Engine reads `$INSUNITS` (`INSUNITS_TO_FEET`) **and renders fit-to-page**, so every sheet auto-scales independently. Mixed units never cause a scale mismatch. The `CANONICAL_UNIT` decision in §4 is therefore moot. |
> | No `G-ANNO-TEXT` → empty/failed sheet | ❌ Labels are optional; a sheet renders from walls alone. Armstrong has 10 clean room labels in the file. |
>
> **Actual root cause:** a **layer-name mismatch**. These files were drafted in plain
> AutoCAD (not Revit), so walls sit on `A_WALL_FULL_N` / `A_WALL_CAVITY` (not
> `A-WALL`/`I-WALL`) and room text on `A_TEXT_BLOWUPS` (not `G-ANNO-TEXT`). The default
> `layer_map` matches nothing → `_wall_extents()` returns `None` → `ParseError`.
> Converting the source DWG ourselves would **not** fix it: layer names are intrinsic to
> the source drawing and conversion (CloudConvert *or* ODA) copies the layer table verbatim.
>
> **The fix that shipped instead:** `app/backend/engine/layers.py :: infer_layer_map()` —
> auto-detects layer roles from layer names + content (room text drives room-label
> detection), surfaced for human review/override and persisted via the existing property
> `layer_map`. See the approved plan and `MULTI_UNIT_SPLIT_TODO.md` (the A12 multi-unit
> split, deliberately deferred).
>
> Everything below is the **original, superseded** spec, kept for context only.

---

**Status:** ~~ready for implementation~~ **SUPERSEDED — see Findings above** · Owner: marketing/dev · Companion docs: `APP_BUILD_SPEC.md`, `FLOORPLAN_WORKFLOW.md`

This is a standalone spec for a new **front stage** in the floor-plan sheet pipeline. It does not replace the rendering engine or change the brand/label logic. It sits in front of everything else and guarantees that whatever DXF comes in, the downstream pipeline receives the clean, predictable shape it was originally built to expect.

---

## 1. Why this exists

The pipeline was built around DXFs that had two properties:

1. Drawable geometry (walls, dimensions, text) lives directly in **modelspace**.
2. Room labels live as text on a known annotation layer (`G-ANNO-TEXT`), each with an XY position — this is what makes auto-seeding labels possible.

Real-world inputs break both assumptions. Three files from an older property (one Armstrong unit, two identical A12 enlarged-unit sheets) were run through **CloudConvert** on the way to DXF, and they fail the current pipeline for reasons that have nothing to do with the drawings being bad — the geometry is intact, it's just in the wrong place and the wrong units. *(Findings: this framing turned out to be wrong — the geometry is found fine; only the layer names differ.)*

## 2. Contract

**Input:** any `.dxf` file. **Output:** a flat-in-modelspace `ezdxf` document in a canonical unit with a non-empty label-seed list. *(Findings: the engine already flattens via `virtual_entities` and already reads `$INSUNITS`; this contract describes work the engine does.)*

## 3. Stages (recursive explode / unit normalization / 3-tier label-seed harvest / hand-off)

*(Findings: §3.1 recursive explode and §3.2 unit normalization duplicate existing engine behavior. §3.3 tier-1/tier-2 label harvest also already exists — `parse._collect_entities` harvests all TEXT/MTEXT and tags `is_label_layer`. Tier-3 geometry-centroid seeding genuinely does not exist but is not needed for these files. The real gap was layer-role mapping, addressed by `infer_layer_map`.)*

## 4. Canonical unit — ~~DECISION PENDING CODE REVIEW~~ MOOT

*(Findings: the engine renders fit-to-page and converts `$INSUNITS`→feet only for dimension estimates. There is no `CANONICAL_UNIT` to set; no normalization needed.)*

## 5. Edge cases & failure behavior

*(Findings: the "unitless / mis-scaled sheet" and "zero seeds → blank sheet" failure modes do not occur — render is fit-to-page and labels are optional. The genuine failure was `ParseError: no readable wall geometry`, caused by the layer mismatch.)*

## 6. Acceptance criteria

*(Findings: superseded by the shipped plan's criteria — `infer_layer_map` must produce a working map for all three real files and must agree with the Revit map on the clean 800 Princess file, byte-identical, no regression.)*

## 7. Open items — RESOLVED

- `CANONICAL_UNIT`: moot (no normalization).
- Engine entry point: `parse_dxf()` in `engine/parse.py`, called from `main.py` `/parse`.
- Annotation-layer name(s): not fixed — detected per file by `infer_layer_map` from text content, not a hardcoded name.
