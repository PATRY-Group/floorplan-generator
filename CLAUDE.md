# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

An **internal web app** that turns a single-unit CAD floor plan (DXF, or DWG via converter) into a branded marketing sheet (SVG + PNG), with auto-placed room labels and a drag-to-fix editor. A non-technical coordinator uploads a file, picks a property, and exports a finished sheet — no coordinate entry.

Read `APP_BUILD_SPEC.md` (the engineering spec) and `FLOORPLAN_WORKFLOW.md` (the manual process this automates) before making non-trivial changes — they encode the *why* behind most design decisions. The shipping app lives entirely under `app/`.

The two top-level scripts (`build_floorplan_sheets.py`, `build_floorplan_sheets_with_keyplan.py`) are the **original prototype engine** the app was refactored from. `app/backend/engine/render.py` is intended to produce byte-for-byte identical output to these — they are the reference, not dead code, but the app does not import them.

## Commands

Two processes. Run from the indicated directory.

**Backend** (FastAPI, port 8000) — `app/backend/`:
```bash
.venv\Scripts\activate          # venv already exists at app/backend/.venv
pip install -r requirements.txt # only when deps change
uvicorn main:app --reload --port 8000
```

**Frontend** (Vite + React, port 5173) — `app/frontend/`:
```bash
npm install                     # first run only
npm run dev                     # dev server; proxies /api/* -> :8000
npm run build                   # production bundle
```

Open http://localhost:5173. The Vite dev server proxies `/api/*` to the backend (see `vite.config.js`), so the frontend calls same-origin `/api` and there is no CORS config to manage. Point at a different backend with `VITE_API_BASE`.

There is **no test suite** and no linter configured. The repo is under git; only the prototype scripts and the `app/` tree are tracked source.

### Environment dependencies
- **PNG output** uses `resvg-py` (not `cairosvg`) so uploaded brand fonts embed in the raster — `cairosvg` can't embed arbitrary fonts. Cairo/GTK is still a transitive concern: `render.py::_register_cairo_dll_dir()` auto-registers the GTK `bin` dir on `PATH`/`CAIROCFFI_DLL_DIRECTORIES` at import. If PNG fails with a DLL error, install the GTK3 runtime.
- **Fonts** uploaded with a property are embedded in the PNG; `fonttools` reads the family name at upload. Brand-file *font names* surfaced by extraction (PDF only, via `PyMuPDF`) are hints to copy — never auto-wired into the serif/sans stacks, since they aren't CSS stacks and aren't installed server-side (the PNG would silently fall back).
- **DWG support** is optional and requires the **ODA File Converter** CLI. Set the `ODA_CONVERTER` env var to its path (or have it on `PATH`). Without it, only DXF is accepted; `/capabilities` reports this and the UI hides DWG.

## Architecture

```
Browser (React, app/frontend)        Backend (FastAPI, app/backend/main.py)
─────────────────────────            ──────────────────────────────────────
upload + property picker  ─POST────► /parse    DXF/DWG -> geometry cache + seeded labels
live SVG preview          ◄────────  /render   prims + config -> SVG + PNG (+ keyplan)
drag-to-fix label handles ─POST────► /plate    floor-plate image upload (key plans)
metadata + key-plan form             /properties, /sheets/*   CRUD + library
```

**The rendering engine is authoritative and server-side.** The frontend's job is to assemble a **config object** (metadata, room list with optional position overrides, palette ref, key-plan opts) and POST it; all layer treatment, label placement, halo, scaling, and page layout stay in `engine/render.py`. The preview SVG *is* the final artifact — there is no separate preview render path that could drift.

### Backend layout (`app/backend/`)
- `main.py` — all HTTP endpoints, file-based storage, uploads-cache sweep. No framework magic; read top to bottom.
- `engine/parse.py` — DXF → `prims` (flat geometry) + auto-seeded `labels` + `ignored_text` + metadata `suggestions`. Raises `ParseError` for sheet exports / empty geometry.
- `engine/render.py` — `render(prims, config) -> (svg, png, meta)`. The core.
- `engine/keyplan.py` — schematic "where's my unit" plate: `keyplan_group()` (footer mini-plate) and `render_keyplan_sheet()` (standalone page).
- `engine/keyplan_trace.py` — auto-trace a raw plate screenshot into a clean filled footprint silhouette (numpy + PIL): `trace_plate()` caches a palette-independent mask; `colorize()` applies the brand palette at render time. "Seal strength" closes doorway gaps; falls back to dimmed-screenshot mode when a plate won't trace cleanly.
- `engine/brand.py` — `extract_brand()` pulls a color palette (and PDF-embedded font names) from an uploaded brand file to auto-fill the property-setup form. `dark`/`light` are dependable; `accent`/`mid` are guesses, so all dominant swatches are returned for the user to re-pick.
- `engine/convert.py` — DWG→DXF via ODA CLI; degrades gracefully when absent.
- `data/properties/*.json` — one file per property (brand + layer map); `800-princess.json` is the worked example. `data/uploads/` — transient parse/plate cache. `data/sheets/<prop>/` — saved sheet library.

### Frontend layout (`app/frontend/src/`)
- `App.jsx` — single stateful component orchestrating the whole flow; debounced auto-preview on any input change; localStorage autosave/restore of the in-progress unit and last-used property.
- `LabelOverlay.jsx` — renders the sheet SVG and draggable label handles on top.
- `PropertySetup.jsx`, `KeyPlanPanel.jsx`, `Library.jsx`, `Toasts.jsx` — property CRUD form, key-plan picker, saved-sheet list, notifications.
- `api.js` — thin `fetch` wrapper over the backend; all network calls go through here.

## Cross-cutting contracts (the things that span files)

**`prims` shape** — the data contract between parse and render. A flat list of `[layer, kind, data, block]`:
- `kind == "line"` → `data` is a list of `(x, y)` points (a polyline).
- `kind == "hatch"` → `data` is a list of polygons, each a list of `(x, y)`.
- `block` is the originating block name (used to drop loose furniture).

Both endpoints interpret layers through the property's **layer map** (`DEFAULT_LAYER_MAP` in `parse.py` is the Revit-export default), which maps CAD layer names to roles (`wall_line`, `wall_fill`, `door`, `glazing`, `room_label`, `drop`, …). Changing role semantics means touching both parse and render.

**Coordinate transform** — the contract between `render.py` and `LabelOverlay.jsx`. `render` returns `meta.transform = {tx, ty, s}`; SVG/viewBox ↔ DXF coords convert as `svgX = tx + dxfX*s`, `dxfY = (ty - svgY)/s`. The overlay uses this to translate a dropped/nudged handle position back into the room's DXF `x`/`y` override sent on the next `/render`. A room with explicit `x`/`y` skips auto-placement; clearing them (double-click a handle) returns it to automatic placement.

**Sheet re-open** — saving a sheet persists its editable config *and* a copy of its `prims.json` into `data/sheets/<prop>/`. `/reopen` copies that geometry back into `uploads/` under a fresh `doc_id`, so the 24h uploads sweep doesn't break editing.

**Uploads cache sweep** — files in `data/uploads/` older than `UPLOAD_TTL_HOURS` (default 24, constant in `main.py`) are deleted on startup and at the start of every `/parse` and `/plate`. A `/render` against an expired `doc_id` returns 404 "Upload expired"; the frontend detects this and clears the session.

## Gotchas to preserve (hard-won; see APP_BUILD_SPEC.md §10)

- The CAD input must be a Revit **view export, not a sheet** — a sheet has no wall geometry. `parse_dxf` raises `ParseError` with guidance when geometry is absent.
- The occupancy integral image must be **int64** (`render.py` casts via `.astype(np.int64)`); `uint8` overflows and produces random label placement.
- Label **search rectangles are kept tight** to the room (`seed_box_frac` in `parse.py`) so labels don't drift into neighbours.
- **No `.rvt` or in-process `.dwg` parsing** — `.rvt` is rejected with guidance; `.dwg` only via the ODA CLI.
- **Loose furniture is dropped** by block-name match (`FURNITURE_FRAGMENTS` in `parse.py`); built-in kitchen/bath fixtures stay.
- Room **dimensions are left blank on parse** for the user to fill — open-plan sizes are judgment calls; auto wall-to-wall measure is deliberately not trusted.
- **Large-file guards** (`parse.py`): uploads > `MAX_UPLOAD_MB` (60) rejected, geometry capped at `MAX_PRIMS` (200k) with a UI warning, single oversized polyline/spline downsampled. The right input is a single-unit view, not a whole floor.

## Out of scope for v1 (don't add unprompted)
Multi-user accounts, editing wall geometry (fix it in CAD), exact-scale key plans (schematic only), the downstream Canva "dollhouse" render, and auto-detecting brand colors from a logo.
