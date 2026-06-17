# Branded Floor Plan Sheet Pipeline

How to turn a Revit/CAD floor plan into a branded marketing floor plan sheet for a property
website. Works for any property and any brand — nothing here is tied to one building.

## What you're producing

A single-page sheet per unit: a clean black-line 2D floor plan with room labels and a few key
dimensions, set on a branded page with a dark header (property lockup) and footer (unit name,
suite number, square footage). See `EXAMPLE_one-bed-sheet.png` for the target. Output is **SVG**
(the web file — scales to any size, ~70 KB) plus a **PNG** preview of the same sheet.

Optionally, a **key plan** can be added: a small diagram of the whole floor with the unit shaded,
showing where in the building it sits. Generated only when asked (see Part 5).

The pipeline is a Python script (`build_floorplan_sheets.py`) that reads the CAD geometry and
renders the SVG. You feed it one config block per unit; it does the rest. There is also a variant
script (`build_floorplan_sheets_with_keyplan.py`) that embeds a key plan in the footer.

## The four inputs you need per unit

1. **A DXF floor plan** of the unit (how to get one: Part 1).
2. **Unit metadata**: title (e.g. "ONE BED"), suite number, square footage, plus the property
   name + location for the header/footer.
3. **A brand palette** — colors + any logo lockup (Part 0).
4. **A room list** — the only fiddly per-unit input (Part 4).

---

## Part 0 — Brand palette (an input, supplied once per property)

Colors are not hardcoded. Supply the brand as a PDF, screenshot, image, or text block; the agent
reads the hex values and assigns them to five roles:

| Role | Used for |
|---|---|
| **Dark / primary** | header & footer bands, wall lines, primary text |
| **Accent** | logo lockup, watermark, key-plan highlight, underlines |
| **Mid / secondary** | secondary text on the dark bands |
| **Light / background** | page background, and label "halos" (Part 4) |
| **(optional) extra** | reserved accent |

Reference only — replace per property. 800 Princess "Stone & Ember":
`Charcoal #2B1F14 · Ember #C17F3A · Limestone #E8D9C0 · Chalk #F7F3ED · Patina #5C7A6E`.

If the brand shows a display typeface or logo lockup, match its feel (serif vs sans).

---

## Part 1 — Get a usable DXF (the one step needing Revit access)

The script reads **DXF** files. Two rules:

**Convert to DXF.** If you have DWG, convert first — Revit's `DXFOUT`, the free **ODA File
Converter** (batch), or an online tool like https://sharecad.org/. Raw `.rvt` and `.dwg` cannot be
read without Autodesk's engine, so the conversion has to happen before Claude sees the file.

**Export the VIEW, not the SHEET.** In Revit, export the floor plan *view*, not a *sheet*. A sheet
export wraps the plan in an external reference that doesn't travel with the file — you end up with
just a titleblock and no actual geometry. (Tell-tale sign: the DXF opens but contains a single
empty block named `X1`.) A view export bakes all the linework into the file where it can be read.
One view per file; leave the layers on.

> A "view" in Revit is a single drawing (one floor plan). A "sheet" is a presentation page that
> *references* views inside a titleblock border. We need the raw view.

---

## Part 2 — Capture floor plates (only if a key plan is wanted)

A key plan needs a top-down image of the whole floor to trace. If the Revit file only has a 3D
view, make a top-down yourself in **Autodesk Viewer** (https://viewer.autodesk.com/, free, no
license — just upload the `.rvt`). Do this once per *distinct* floor, not per unit:

1. **ViewCube** (top-right) → click the **TOP** face. Confirm it reads "TOP" — an orbited/tilted
   angle is distorted and can't be traced.
2. **Model browser** → click eye icons to hide everything you don't want: furniture, fixtures,
   windows, pipes, stairs, ramps, curtain panels/mullions, roofs, grids, stray lines. Keep walls,
   floors, structural columns.
3. Bottom toolbar → **Section** → **Z Plane**. Drag the vertical arrow up/down to slice the
   building horizontally at the target floor; everything above the cut vanishes, leaving the plate.
4. **Fit**, then **Screenshot** (top-right). Save.
5. Read the **compass** (top-right of the screenshot) to know which way is N/S/E/W — needed to
   place the unit on the plate later.

Note: repeating residential floors share one plate (capture once); the ground/amenity floor is
always its own capture. (https://sharecad.org/ also opens DWG/DXF in a browser if useful.)

---

## Part 3 — How the script builds the sheet (automated)

`build_floorplan_sheets.py`, per unit:

1. Reads the DXF and recursively explodes nested blocks into raw lines/arcs/polygons.
2. Keeps structural geometry + built-in fixtures, drops noise and loose furniture (table below).
3. Scales the plan to fit the page and centers it.
4. Draws each layer in its own weight (walls heavy, doors medium, glazing/fixtures light).
5. Places room labels in clear space (Part 4).
6. Writes the **SVG** and a 900 px **PNG** of the same sheet.

**Layer treatment** (these are Revit's default export layer names; a different CAD template may use
different names — map them to the same *roles* once when onboarding a property):

| Layer | Role | Drawn as |
|---|---|---|
| `A-WALL`, `I-WALL` | wall outline | solid dark, 1.6 px |
| `A-WALL-PATT` (hatch) | wall fill (poché) | solid dark fill |
| `A-DOOR`, `A-DOOR-FRAM` | doors | leaf 1.0 px; swing arcs faded |
| `A-GLAZ` | glazing | thin 0.9 px |
| `A-DETL-HDLN`, `A-FLOR-OVHD` | overhead lines | dashed, faint |
| fixtures / casework | bath/kitchen built-ins | thin 0.6 px, faint |
| `A-AREA-IDEN`, `S-COLS-SYMB`, `S-STRS`, `S-STRS-MBND` | tags, column & stair symbols | **dropped** |
| `A-FLOR` (hatch) | floor finish fill | **dropped** (too busy) |
| furniture blocks (Sofa, Bed, Chair, TV…) | loose furniture | **dropped** by name |

Built-in fixtures (toilet, sink, stove, tub) stay — they read as part of the room. Only
free-standing furniture is removed, matching the clean marketing look.

---

## Part 4 — Room labels (the only per-unit tuning, and where care is needed)

Labels aren't placed by hand. For each room you give the script a **search rectangle** — the
room's interior bounds, in the drawing's own coordinates. The script then finds the clearest
empty pocket *inside that rectangle* and drops the label there.

How to get a room's rectangle: open the DXF (or its PNG) and read off the approximate corner
coordinates of the room's interior — the script prints coordinate-gridded debug images to help.
It's a rough box, not exact; it just has to stay within the room's walls.

Rules the script follows, all learned from iterating:

- **Stay in the room.** The label never leaves its rectangle to find cleaner space. If a room has
  no clear pocket, the label centers and accepts some overlap rather than wandering into the
  neighbour. (A too-large rectangle is the #1 mistake — it lets a label drift into the next room.
  Keep rectangles tight to the actual room.)
- **Halo for legibility.** Every label is drawn twice: a fat stroke in the background color
  underneath, then the text on top. The halo "erases" any linework behind the text, so the
  occasional overlap still reads cleanly.
- **Always horizontal.** No sideways/rotated text, even in narrow rooms. Tight rooms get a smaller
  font instead.
- **Name + dimension as a pair**, centered together (name on top, dimension smaller and fainter
  below). Closets/baths/pantries are name-only.
- **Dimensions** are measured from the walls, rounded to the inch, written like `14'4" x 9'3"`.
  Open-plan living/kitchen sizes are a judgment call — confirm with leasing what to advertise.

---

## Part 5 — Key plans (only when requested)

A key plan is generated **on request**, not by default. Two placements:

- **Standalone sheet** — its own branded page.
- **Footer key plan** — a small plate in the floor plan's footer (`build_floorplan_sheets_with_keyplan.py`).

The plate is a **simplified hand-trace** of the Part 2 screenshot — a recognizable outline with the
floor split into a rough grid of units and the target unit shaded in the accent color. It is **not**
to scale and the unit count is approximate; always label it "SCHEMATIC — NOT TO SCALE." That's the
right fidelity for a thumbnail that just answers "where is my unit in the building."

To place the unit correctly: all units in a building share one coordinate origin, so a unit's
position (which edge, which end) can be read from its DXF wall coordinates; use the Part 2 compass
to translate that into N/S/E/W on the plate. For an exact (not schematic) plate, export that floor
as a *view* DXF (Part 1) and trace it instead.

---

## Part 6 — Per-unit checklist & shipping

Per unit, you set only:

- [ ] Unit metadata (title, suite, SF, property name/location)
- [ ] Room list (name, dimension or none, search rectangle, font scale)
- [ ] Key plan? (only if requested — which floor plate, which unit to shade)

Set once per property: the brand palette, and a check that the DXF layer names match the table in
Part 3 (re-map roles if the property uses a different CAD standard).

Before shipping, confirm: SVG **and** PNG exist, every label sits inside its room, no sideways
text, walls are solid and closed, brand colors are right, nothing clips the header/footer.

---

## Part 7 — Agent Prompt (portable, property-agnostic)

Paste into an agent (Claude with code execution + file tools, or Claude Code) with: the unit DXF,
unit metadata, the brand palette (PDF/image/screenshot/text), and any reference images. Nothing
below is tied to a specific property.

```
You are generating a branded marketing floor plan sheet from a CAD file. Produce a clean, web-ready
SVG plus a PNG preview, in the brand identity I provide. Use Python with ezdxf, numpy, Pillow,
cairosvg.

INPUTS (with this message):
- A unit floor plan DXF (a Revit VIEW export — geometry is in model space).
- Unit metadata: title (e.g. "ONE BED"), suite number, square footage, and the property
  name + location for the header/footer.
- A BRAND PALETTE as PDF/image/screenshot/text. Read the hex values and map them to roles:
  dark/primary (bands, walls, primary text); accent (logo lockup, watermark, highlights,
  underlines); mid/secondary (secondary text on dark bands); light/background (page bg + label
  halos). Match the display-font feel (serif vs sans) if a lockup is shown.
- Optionally: reference image(s) for style, and a floor-plate screenshot + unit position for a
  key plan.

Derive ALL colors from the supplied brand material — assume no specific property or palette. If the
brand material is ambiguous, state your interpretation and proceed.

PAGE: 1000x1080 viewBox. Header band 92 px, footer band 140 px, plan area between (max 800x640). A
large faint accent-color watermark (property number/initial, ~430 px display weight, ~0.07 opacity)
sits behind the plan. Display type (lockup, unit name) in the brand display feel; all else sans.

READING THE DXF:
- Never read .rvt or .dwg directly (needs Autodesk's engine). DXF only.
- Recursively explode every INSERT to primitives (cap recursion depth 4-6). Flatten arcs/polylines
  with ezdxf path.flattening.
- Layer roles (map a property's actual layer names to these if its CAD standard differs):
    wall outline (e.g. A-WALL, I-WALL)        -> solid dark 1.6 px
    wall poche hatch (e.g. A-WALL-PATT)        -> solid dark fill, fill-rule nonzero
    doors (e.g. A-DOOR, A-DOOR-FRAM)           -> leaf 1.0 px; polyline with >6 pts = swing arc,
                                                  fade to 0.45 opacity, 0.7 px
    glazing (e.g. A-GLAZ)                      -> dark 0.9 px
    overhead/dashed (A-DETL-HDLN, A-FLOR-OVHD) -> dashed 0.35 opacity
    fixtures/casework/detail                   -> thin 0.6 px, 0.55 opacity
  DROP entirely: area-identity tags, column symbols, stair symbols/boundaries, floor-finish hatch.
  DROP loose furniture blocks by name (Sofa, Chair, Bed, Television, Bedside, etc.). KEEP built-in
  kitchen/bath fixtures.
- Compute extents from wall layers only; scale to fit the plan box; center; flip Y for SVG.

ROOM LABELS (get this exactly right):
- Per room you are given: name, dimension string or None, an interior SEARCH RECTANGLE (room bounds
  in DXF coords), and a font scale (1.0 default; 0.8-0.9 for tight rooms).
- Build an occupancy raster of ALL plan linework (lines width ~3, hatches filled) as a numpy array;
  build an integral image (int64 — uint8 overflows) for fast box-sum overlap queries.
- For each room, search candidate positions ONLY inside its rectangle. Score by
  (overlap>0, overlap_amount, distance_to_rect_center); pick the minimum: zero overlap first, then
  least overlap, then most central.
- HARD RULES:
    * Label stays inside its own rectangle. Never drift into a neighbour for clean space. If no
      clear pocket fits, center it and ALLOW overlap.
    * All text HORIZONTAL — never rotate, even in narrow rooms; shrink the font instead.
    * Halo every label: draw it twice — first fill:none, stroke = brand light/background color,
      stroke-width 8, stroke-linejoin round; then the dark fill on top. The halo knocks out
      linework under the text.
    * Name + dimensions centered as a vertical pair: name on top (~11.5 px x scale, letter-spacing
      ~2.2, 0.78 opacity), dims below (~10 px x scale, letter-spacing ~1.2, 0.5 opacity). Name-only
      rooms get a single centered line.
- Dimensions: measure from wall geometry, round to the inch, format like 14'4" x 9'3".

HEADER (dark band): left = accent display lockup (property number/name), thin accent divider,
property name (letter-spaced light text) over location (secondary). Right = small secondary label
"FLOOR PLAN".

FOOTER (dark band): left = display unit title (light) over an accent underline, then
"SUITE ### . ### SF" (secondary). Right = accent "PROPERTY . LOCATION" and a small secondary
disclaimer "FOR ILLUSTRATIVE PURPOSES ONLY. DIMENSIONS APPROXIMATE."

KEY PLAN (only if a plate + position are provided; otherwise skip):
- Trace a SIMPLIFIED plate outline polygon from the screenshot (recognizable, not vector-exact):
  white plate, dark outline, on the brand background.
- Split into a rough unit grid with thin faded dark lines (~0.4 opacity) for demising walls and the
  corridor; lobby columns as small dots if present. Unit count is approximate.
- Shade ONLY the target unit's cell in the accent color (~0.92 opacity), placed per the given
  edge/end (use the screenshot compass for orientation).
- Add a small north arrow + floor label. Mark "SCHEMATIC KEY PLAN. NOT TO SCALE." Place as a
  standalone sheet, or a ~150 px mini plate in the right of the footer band (shift address left).

OUTPUT:
- Write the SVG to the outputs directory AND a 900 px PNG beside it — both are deliverables.
- Self-check: every label inside its room; no sideways text; walls solid and closed; colors derived
  from the supplied palette; nothing clips header/footer. Report any room where the label had to
  overlap because no clear pocket existed.
```

---

## Part 8 — Known gotchas

- **Sheet vs view export** — sheet DXFs have only a titleblock; the plan is an unbound xref. Export
  the *view*. Symptom: one empty `X1` block, no geometry.
- **Integral-image overflow** — the occupancy integral image must be int64; uint8 overflows and
  label placement goes random.
- **Loose search rectangles** — too big, and a label escapes into the next room. Keep them tight.
- **No .rvt/.dwg parsing in-sandbox** — convert to DXF upstream.
- **Key plans are schematic** — hand-traced, not to scale; label them so. Trace a view DXF if exact.
- **Open-plan dimensions** — living/kitchen sizes are judgment calls; confirm with leasing.
- **New property** — re-check layer names (Part 3) and supply the new palette (Part 0); both are
  inputs, not constants.

---

# Secondary: Canva "dollhouse" 3D-style render (downstream of Path A)

This is an **optional second pass**, used only when you want a warm, furnished, 3D-modelled-looking
render instead of (or alongside) the clean vector sheet. It is **not** a substitute for the pipeline
above — Canva works *from* the SVG/PNG the agent already produced.

Sequence:

1. Run the main pipeline first to get the unit's SVG/PNG (Parts 1–7).
2. In **Canva AI**, feed it: the floor plan PNG from step 1, plus one or more **reference images**
   (e.g. a previously approved dollhouse render in the brand theme). References dramatically improve
   how well it matches the look.
3. Canva stages furniture into the rooms while keeping the structure — it's good at this.
4. Optional cleanup: run an AI denoiser, then crop off the AI-softened header/footer and paste the
   crisp branded bands back in the Canva editor.

Prompt template (attach the reference image(s) and the floor plan to modify):

```
Turn this basic floorplan into a dollhouse-style floorplan. Do not alter the layout or room
labelling. Try to match the theme. Use the actual floorplan to inform your design, not a generic
theme. I want the [BRAND THEME, e.g. brown/ember] theme. Preserve the top-down view. Remove the
labelling, which will be too faint to see once furnished anyway. I have attached a reference image
of [reference unit]. You should modify the [target unit] floorplan.
```

Swap the bracketed parts per property/unit. The labels disappear once furniture is staged, which is
why the prompt tells Canva to drop them.
