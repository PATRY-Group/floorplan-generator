# Floor Plan Sheet Generator — App Build Spec (Engineering Handoff)

## 0. Purpose & audience

Turn the existing Python floor-plan pipeline into an **internal web app** a non-technical user
(leasing/marketing coordinator) can run: upload a CAD file, pick a brand, get a branded marketing
floor plan sheet (SVG + PNG) out — with light visual tweaks if needed, but no code and no reading of
CAD coordinates.

This is an internal tool, not a product. Optimize for "works reliably for our team across our
portfolio," not multi-tenant scale or polish.

Companion docs:
- `FLOORPLAN_WORKFLOW.md` — the manual process this app automates. Read it first; the app is that
  workflow with a UI.
- `build_floorplan_sheets.py` / `build_floorplan_sheets_with_keyplan.py` — the working rendering
  engine. **Reuse this as the backend core**; do not rewrite the rendering from scratch.

---

## 1. The one hard problem, and why it's now solvable

Everything in the pipeline is automated except one thing that historically needed a human: deciding
**where each room label goes inside the plan**. In the manual workflow a person reads room-interior
coordinates off the DXF and types them in as "search rectangles." A non-technical user cannot do that.

**Key finding that makes the app viable:** the marketing DXFs already carry the room names *with
their insertion points* on a text layer (observed: layer `G-ANNO-TEXT`, e.g. `BEDROOM` at a known
XY, `KITCHEN`, `WASHROOM`, `W.I.C`, `PANTRY`, etc.). So the app can **auto-seed every label from the
CAD** — name and starting position both come from the file. No coordinate entry.

The label *placement* still benefits from the existing collision-avoidance logic (find clear space,
halo, stay-in-room), but it now starts from a real seed point per room instead of a hand-typed
rectangle. Users only correct the occasional miss, visually.

> Two different "coordinate" jobs, so we're precise about it:
> - **Label placement inside the plan** — important; drives whether the sheet looks clean. Now
>   auto-seeded from `G-ANNO-TEXT`, with a drag-to-fix fallback (§4).
> - **Key-plan unit shading** — purely cosmetic ("which rectangle is lit up"). Approximate is fine;
>   handled by a simple picker (§6). Key plans are opt-in.

---

## 2. User flow (what the coordinator sees)

1. **Upload** a unit floor plan file (DXF, or DWG — app converts; see §5).
2. **Pick a property** from a saved list (each property carries its brand palette + CAD layer map).
   Or add a new property once via a setup screen (§7).
3. App parses the file and shows a **live preview** of the sheet: walls, fixtures, auto-placed room
   labels, branded header/footer.
4. **Fill metadata**: unit title, suite number, square footage. (Some may auto-fill from the DXF —
   e.g. "1 BED - 1A" and "202" appear as text in the file; offer them as suggestions.)
5. **Review & nudge**: any label that landed badly can be dragged; dimensions can be edited inline.
   Toggle a room's dimension on/off.
6. *(Optional)* **Add a key plan**: upload a floor-plate screenshot, click the unit's rough location
   on it, choose standalone or footer placement.
7. **Export**: download SVG + PNG. Saved to the property's library.

Target: a clean, single-unit DXF → finished sheet in **under 2 minutes**, no tweaks needed in the
common case.

---

## 3. Architecture

```
Browser (React)                    Backend (Python service)
─────────────────                  ────────────────────────
Upload + property picker  ──────►  /parse   : DXF → geometry JSON + seeded room labels
Live SVG preview          ◄──────             (reuses build_floorplan_sheets.py internals)
Drag-to-fix labels        ──────►  /render  : config + overrides → SVG + PNG
Metadata form                      /convert : DWG → DXF (ODA File Converter CLI)
Key-plan picker (opt)              /properties : CRUD for property brand + layer map
Export                             storage  : per-property library of finished sheets
```

- **Frontend:** React. The preview is just the rendered SVG inlined in the page (the engine already
  emits SVG), so "preview" and "final" are the same artifact — no separate rendering path to drift.
- **Backend:** wrap the existing Python engine as a small service (FastAPI is the natural fit). The
  engine already does parse → place → render; expose those as endpoints rather than reimplementing.
- **No database needed initially** — properties and finished sheets can live as files/JSON on disk
  or a bucket. Add a DB only if the library grows enough to need search.

Keep the rendering engine authoritative. The frontend sends a **config object** (the same per-unit
config from the workflow doc: metadata, room list with positions, palette ref, key-plan opts) and
gets back SVG/PNG. All the layer treatment, halo, scaling logic stays server-side and untouched.

---

## 4. Label handling (the core UX)

This is where to spend the engineering care; everything else is plumbing.

**Auto-seed (server, on /parse):**
- Read text entities on the room-label layer (`G-ANNO-TEXT` for current properties; configurable per
  property in the layer map). Each gives a room **name** and a **seed point**.
- Filter out non-room text: unit code (`1 BED - 1A`), suite number (`202`), equipment tags (`HWT`,
  `DW`, `FR`, `V1`, etc.). Heuristic: keep all-caps words that match a room vocabulary
  (BEDROOM, LIVING, KITCHEN, BATH, WASHROOM, DEN, W.I.C, PANTRY, FOYER, …); surface the rest as
  "ignored — click to re-add" so nothing is silently lost.
- Derive a starting **search rectangle** around each seed (e.g. expand from the seed until hitting
  wall lines, or a fixed default box) and run the existing clear-pocket placement + halo.
- Compute dimensions from wall geometry where a bounding room can be inferred; otherwise leave blank
  for the user to fill.

**Drag-to-fix (client):**
- Render each label as a draggable element over the SVG preview. Dragging updates that room's
  position in the config; re-render (or move client-side and only re-halo on drop).
- Inline-edit the dimension text; toggle dimension visibility per room.
- "Stay in room" and halo rules still apply on the server render — the drag just changes the seed.

This replaces coordinate entry entirely: the user sees labels already placed and only touches the
ones that look wrong. In clean units they touch nothing.

---

## 5. File ingestion

- Accept **DXF** directly (ezdxf).
- Accept **DWG**: convert server-side with the **ODA File Converter** CLI (free, headless, batch).
  Bundle it in the backend image. (LibreDWG is not a reliable fallback — it OOMs building; don't
  depend on it.)
- Reject **.rvt** with a clear message: "Export the floor plan as a DXF *view* from Revit first" —
  link to the relevant section of `FLOORPLAN_WORKFLOW.md`. The app cannot read .rvt.
- On parse, validate it's a **view, not a sheet**: if the file is essentially one empty `X1` block
  with no wall geometry, show the "this looks like a sheet export, we need a view export" error
  rather than rendering an empty page.

---

## 6. Key plans (optional feature, build second)

- User uploads a floor-plate screenshot (captured via Autodesk Viewer per the workflow doc).
- App shows it; user **clicks the unit's approximate location** (or drags a box). That's the shaded
  cell — cosmetic, approximate by design.
- App overlays a simplified plate trace + accent-shaded unit + north arrow + floor label, marked
  "SCHEMATIC — NOT TO SCALE."
- Output as standalone sheet or footer mini-plate (the engine already supports both).
- Ship the main generator first; key plans are a clean add-on once the core works.

---

## 7. Property setup (brand + layer map)

Each property is configured **once**, then reused for all its units:

- **Brand palette:** five color roles (dark, accent, mid, light, +optional) + display-font feel.
  Let the user paste hex or upload the brand PDF/image and confirm the auto-read values.
- **CAD layer map:** which layer names mean wall / poché / door / glazing / overhead / fixture /
  room-label / drop. Defaults to the known scheme (`A-WALL`, `A-WALL-PATT`, `A-DOOR`, `A-GLAZ`,
  `G-ANNO-TEXT`, …); editable for properties on a different CAD standard.
- **Header/footer text:** property name + location.

Store as a small JSON per property. This is the only "admin" surface and can be a plain form.

---

## 8. Build order (suggested)

1. **Backend service** wrapping the existing engine: `/parse`, `/render`, `/convert`. Prove a DXF in
   → SVG/PNG out over HTTP, matching what the script produces today.
2. **Minimal frontend**: upload → property pick → metadata form → preview → export. No drag yet;
   just auto-placed labels. This alone is usable for clean units.
3. **Drag-to-fix + inline dimension editing** — the UX payoff of §4.
4. **Property setup screen** (§7).
5. **Key plans** (§6).
6. **Library**: list/download past sheets per property.

Each step is independently shippable; stop wherever it's "good enough" for the team.

---

## 9. Explicitly out of scope (for v1)

- Multi-user accounts/permissions — it's an internal tool; trust the team.
- Editing wall geometry — if the CAD is wrong, fix it in CAD, not here.
- Exact-scale key plans — schematic only (see workflow doc).
- The Canva "dollhouse" render — that's a separate manual creative step downstream of this app's
  output, not part of the app.
- Auto-detecting the brand from a logo alone — user confirms colors during property setup.

---

## 10. Known gotchas to carry over (from building the pipeline)

- Export must be a Revit **view**, not a **sheet** (else no geometry; one empty `X1` block).
- Occupancy integral image must be **int64** (uint8 overflows → random label placement).
- Label **search rectangles kept tight** to the room, else labels drift into neighbours. Auto-seed
  helps, but the expand-from-seed step must stop at walls.
- **No .rvt/.dwg parsing in-process** — DWG via ODA CLI only; .rvt rejected with guidance.
- Built-in fixtures (kitchen/bath) stay; **loose furniture dropped** by block-name match.
- Open-plan living/kitchen **dimensions are judgment calls** — make them user-editable, don't trust
  raw wall-to-wall.
