# Floor Plan Sheet Generator

Internal web app that turns a unit CAD file into a branded marketing floor plan
sheet (SVG + PNG). It wraps the existing Python rendering engine behind a small
HTTP API and gives a non-technical coordinator a point-and-click UI: upload a
DXF, pick a property, get a finished sheet — with room labels auto-placed from
the CAD file, no coordinate entry.

This implements **all six steps** of `APP_BUILD_SPEC.md`: backend service,
frontend, drag-to-fix label editing (with arrow-key nudging), the property
setup screen, key plans (footer mini-plate + standalone sheet), and a
searchable per-property sheet library with re-open and delete. Plus quality-of-
life: toast notifications, remembered last-used property, autosave/restore of
the in-progress unit, and automatic cleanup of the uploads cache.

```
app/
  backend/        FastAPI service wrapping the rendering engine
    main.py         /parse, /render, /convert(via DWG), /properties, /sheets
    engine/
      parse.py      DXF -> geometry primitives + auto-seeded room labels
      render.py     primitives + config -> SVG + PNG (refactor of build_floorplan_sheets.py)
      convert.py    DWG -> DXF via ODA File Converter (optional)
    data/
      properties/   one JSON per property (brand palette + CAD layer map)
      uploads/      cached parses (transient)
      sheets/       finished sheet library, per property
    requirements.txt
  frontend/       Vite + React UI (upload -> preview -> export)
```

## Run it

Two processes: the Python backend and the React dev server.

### 1. Backend (port 8000)

```bash
cd app/backend
python3 -m venv .venv && source .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -r requirements.txt
uvicorn main:app --reload --port 8000
```

`cairosvg` needs the Cairo native library. macOS: `brew install cairo`.
Debian/Ubuntu: `sudo apt-get install libcairo2`. Windows: it ships with the
`cairosvg` wheel, or install GTK runtime if you hit a DLL error.

### 2. Frontend (port 5173)

```bash
cd app/frontend
npm install
npm run dev
```

Open http://localhost:5173. The dev server proxies `/api/*` to the backend on
:8000, so nothing else to configure. To point at a different backend, set
`VITE_API_BASE`.

## Using it

1. Pick a property (ships with **800 Princess**, the "Stone & Ember" brand).
2. Upload a unit **DXF** (a Revit *view* export, not a sheet). DWG works only if
   the server has the ODA File Converter installed (set `ODA_CONVERTER`).
3. The sheet renders live. Room labels are seeded from the CAD text layer
   (`G-ANNO-TEXT`) and placed in clear space automatically.
4. Fill/confirm unit title, suite, square footage — suggestions are pulled from
   the file where possible.
5. Edit dimensions per room, toggle a dimension off, rename or remove a room, or
   re-add any text the parser ignored.
6. **Drag any label** on the preview to reposition it; the server re-places and
   re-halos it at the dropped spot. Or **click a label and nudge it with the
   arrow keys** (Shift = 10px) for fine placement. Double-click a handle to
   clear the override and return it to automatic placement. Moved labels show an
   ember handle; auto-placed ones show a hollow handle.
7. *(Optional)* **Add a key plan**: enable the Key plan section, upload (or
   paste) a floor-plate screenshot, and drag a box over the unit. Pick a **look**
   — **Auto-traced plan** (the app extracts a clean filled footprint silhouette
   in the brand palette; a *seal-strength* slider tunes how aggressively wall
   gaps are closed) or **Screenshot** (the raw image embedded lightened, a
   fallback for busy plates that won't trace cleanly). Set a floor label and
   choose **footer mini-plate** (embedded on the unit sheet) or **standalone
   sheet** (its own branded page). It is always marked SCHEMATIC / NOT TO SCALE.
8. **Save to library & export** to write the SVG + PNG into the property’s
   library and get download links.
9. The **library** (bottom of the page) lists every saved sheet for the
   property with thumbnails. Search by title/suite, download SVG/PNG (and the
   key-plan SVG when present), **Re-open** a sheet to keep editing it, or
   **Delete** it. Your last-used property and in-progress unit are remembered
   across reloads.

## Adding / editing a property

Use **+ New property** (or **Edit**) next to the property picker. The form
covers identity (name, location, header lockup, watermark, footer address,
disclaimer), the four-role brand palette with a live header/footer swatch, and
the CAD layer map (which DXF layer names mean wall / poché / door / glazing /
room-label / drop). Defaults match the Revit export scheme. Each property is
saved as one JSON file in `backend/data/properties/` — you can still hand-edit
those if you prefer, modelled on `800-princess.json`.

## Notes / known limits (v1)

- Dimensions are left blank on parse for the user to fill — open-plan sizes are
  judgment calls (spec §10) and wall-to-wall auto-measure is deliberately not
  trusted yet.
- Label search boxes are seeded as a fraction of the plan around each CAD text
  point, then refined by the clear-pocket + halo placement. Occasional misses
  are corrected by dragging the label on the preview.
- `.rvt` is rejected with guidance; export a DXF view from Revit first.
- The rendering output is byte-for-byte the same engine as
  `build_floorplan_sheets.py`; only the inputs are now config-driven.
- **Large files** are guarded: uploads over 60 MB are rejected, geometry is
  capped at 200k primitives (with an on-screen warning if hit), and a single
  oversized polyline/spline is downsampled. These protect against whole-floor
  or fully-detailed exports; the right input is still a single-unit view.

## Uploads cache & cleanup

`/parse` writes the source file and a `*.prims.json` geometry cache into
`backend/data/uploads/`, and `/plate` writes uploaded plate images there too.
These are working files, not deliverables — finished sheets (with their own
config + geometry copy, so they can be re-opened) live in `backend/data/sheets/`.

The uploads cache is **swept automatically**: on server startup and at the start
of every `/parse` and `/plate`, files older than `UPLOAD_TTL_HOURS` (default 24)
are deleted. Re-opening a saved sheet re-registers its geometry with a fresh
timestamp, so the 24h window doesn't break editing. Tune the window via the
`UPLOAD_TTL_HOURS` constant in `main.py`.
```
