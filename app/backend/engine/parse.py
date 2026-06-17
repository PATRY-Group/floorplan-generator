"""
DXF parsing for the Floor Plan Sheet Generator.

Reads a Revit *view* DXF export and produces:
  - `prims`: flat geometry primitives the renderer consumes
            (list of [layer, kind, data, block])
  - `labels`: auto-seeded room labels (name + seed point + search rect)
  - `ignored_text`: non-room text the user can re-add
  - `suggestions`: best-guess unit title / suite / square footage

The renderer expects the same `prims` shape the original
`build_floorplan_sheets.py` consumed from its geom JSON:
    kind == 'line'  -> data is a list of (x, y) points (a polyline)
    kind == 'hatch' -> data is a list of polygons, each a list of (x, y)
    block           -> originating block name (used to drop loose furniture)
"""

import re
import statistics
import ezdxf
from ezdxf import path as ezpath

# --- recursion / flattening tuning ------------------------------------------
MAX_DEPTH = 5          # cap INSERT explosion depth (spec: 4-6)
FLATTEN_DIST = 0.5     # arc/spline flattening tolerance (drawing units)

# --- large-file guards ------------------------------------------------------
# A single-unit view is a few hundred to a few thousand primitives. A whole
# floor or a heavily-detailed export can be orders of magnitude larger and
# would bloat the SVG and slow placement. Cap collection and warn rather than
# hang or OOM.
MAX_PRIMS = 200_000           # stop collecting geometry past this
MAX_PTS_PER_ENTITY = 8_000    # downsample a single huge polyline/spline


def _cap_points(pts):
    """Stride-downsample an over-long flattened entity, keeping its endpoints."""
    n = len(pts)
    if n <= MAX_PTS_PER_ENTITY:
        return pts
    step = (n // MAX_PTS_PER_ENTITY) + 1
    capped = pts[::step]
    if capped[-1] != pts[-1]:
        capped.append(pts[-1])
    return capped

# --- text classification ----------------------------------------------------
ROOM_VOCAB = {
    "BED", "BEDROOM", "MASTER", "PRIMARY",
    "LIVING", "DINING", "FAMILY", "GREAT",
    "KITCHEN", "PANTRY",
    "BATH", "BATHROOM", "WASHROOM", "ENSUITE", "POWDER", "WC",
    "DEN", "OFFICE", "STUDY", "NOOK",
    "FOYER", "ENTRY", "HALL", "CORRIDOR", "VESTIBULE", "MUDROOM",
    "CLOSET", "WIC", "W.I.C", "W.I.C.", "STORAGE", "LINEN", "UTILITY", "LAUNDRY",
    "BALCONY", "TERRACE", "PATIO", "DECK", "PORCH",
    "GUEST", "SUITE", "STUDIO", "LOFT", "FLEX",
}

EQUIPMENT_TAGS = {
    "HWT", "DW", "FR", "F", "W", "D", "WD", "REF", "MW", "OTR",
    "V1", "V2", "CL", "UP", "DN", "REF.", "F/F",
}

FURNITURE_FRAGMENTS = (
    "SOFA", "COUCH", "CHAIR", "BED", "TABLE", "DESK", "STOOL", "BENCH",
    "TELEVISION", "TV", "BEDSIDE", "NIGHTSTAND", "DRESSER", "WARDROBE",
    "RUG", "PLANT", "LAMP", "ARTWORK", "PICTURE", "OTTOMAN", "SHELF",
    "BOOKCASE", "CABINET-LOOSE",
)

DEFAULT_LAYER_MAP = {
    "wall_line":  ["A-WALL", "I-WALL"],
    "wall_fill":  ["A-WALL-PATT"],
    "door":       ["A-DOOR", "A-DOOR-FRAM"],
    "glazing":    ["A-GLAZ"],
    "dashed":     ["A-DETL-HDLN", "A-FLOR-OVHD"],
    "room_label": ["G-ANNO-TEXT"],
    "drop":       ["A-AREA-IDEN", "S-COLS-SYMB", "S-STRS", "S-STRS-MBND"],
    "floor_hatch": ["A-FLOR"],
}

UNIT_TITLE_RE = re.compile(r"\b(STUDIO|JR\.?\s*\d|\d\s*BED|\d\s*BR|ONE|TWO|THREE)\b", re.I)
SUITE_RE = re.compile(r"^\s*#?\s*(\d{2,4})\s*$")
SF_RE = re.compile(r"(\d{2,5})\s*(?:SF|SQ\.?\s*FT|SQFT|S\.F\.)", re.I)

# DXF $INSUNITS code -> feet per drawing unit. Only codes we can trust to
# convert; anything else (notably 0 = unitless) yields None and we leave
# dimensions blank rather than print a confidently-wrong measurement.
INSUNITS_TO_FEET = {
    1: 1.0 / 12.0,      # inches
    2: 1.0,             # feet
    4: 1.0 / 304.8,     # millimetres
    5: 1.0 / 30.48,     # centimetres
    6: 1.0 / 0.3048,    # metres
}


def _unit_to_feet(doc):
    """Feet per drawing unit from $INSUNITS, or None if not trustworthy."""
    try:
        return INSUNITS_TO_FEET.get(int(doc.header.get("$INSUNITS", 0)))
    except (TypeError, ValueError):
        return None


def _wall_segments(prims, wall_layers):
    """Flatten wall geometry into individual (x1, y1, x2, y2) segments for
    ray casting. Includes wall outlines and poché polygon edges."""
    wl = set(wall_layers)
    segs = []
    for layer, kind, data, _ in prims:
        if layer not in wl:
            continue
        if kind == "line":
            for a, b in zip(data, data[1:]):
                segs.append((a[0], a[1], b[0], b[1]))
        else:  # hatch: close each polygon
            for poly in data:
                for a, b in zip(poly, poly + poly[:1]):
                    segs.append((a[0], a[1], b[0], b[1]))
    return segs


def _ray_hit(segs, sx, sy, axis, sign):
    """Nearest wall distance from (sx, sy) along an axis-aligned ray.

    axis 'x' casts horizontally (ray crosses segments at y == sy); 'y' casts
    vertically. sign is +1 or -1. Returns the distance to the closest crossing
    in that direction, or None if the ray escapes (no wall hit)."""
    best = None
    if axis == "x":
        for x1, y1, x2, y2 in segs:
            if (y1 - sy) * (y2 - sy) > 0 or y1 == y2:
                continue                      # doesn't straddle the ray line
            t = (sy - y1) / (y2 - y1)
            ix = x1 + t * (x2 - x1)
            d = (ix - sx) * sign
            if d > 1e-6 and (best is None or d < best):
                best = d
    else:
        for x1, y1, x2, y2 in segs:
            if (x1 - sx) * (x2 - sx) > 0 or x1 == x2:
                continue
            t = (sx - x1) / (x2 - x1)
            iy = y1 + t * (y2 - y1)
            d = (iy - sy) * sign
            if d > 1e-6 and (best is None or d < best):
                best = d
    return best


def _fmt_ftin(feet):
    """Round to the nearest inch and format as e.g. 14'4\"."""
    total_in = round(feet * 12)
    return f"{total_in // 12}'{total_in % 12}\""


def _span(segs, sx, sy, axis, offsets):
    """Median wall-to-wall span at (sx, sy) along `axis`, sampled from a fan of
    rays offset perpendicular to the cast direction. A single ray that escapes
    through a door opening (and shoots to the far wall) is an outlier the median
    rejects. Returns None if no ray found both bounding walls."""
    spans = []
    for off in offsets:
        ox, oy = (sx, sy + off) if axis == "x" else (sx + off, sy)
        lo = _ray_hit(segs, ox, oy, axis, -1)
        hi = _ray_hit(segs, ox, oy, axis, +1)
        if lo is not None and hi is not None:
            spans.append(lo + hi)
    return statistics.median(spans) if spans else None


def _estimate_dims(segs, sx, sy, unit_to_feet, span_x, span_y):
    """Best-effort interior W x H at a seed point. Returns a 'W'W\" x H'H\"'
    string, or None when units are unknown or walls can't be found both ways
    (so we never show a confident guess)."""
    if not unit_to_feet:
        return None
    # perpendicular fan offsets: small fractions of the plan span, both signs
    ox = [0.0] + [s * span_y for s in (0.015, -0.015, 0.03, -0.03)]
    oy = [0.0] + [s * span_x for s in (0.015, -0.015, 0.03, -0.03)]
    w_u = _span(segs, sx, sy, "x", ox)
    h_u = _span(segs, sx, sy, "y", oy)
    if w_u is None or h_u is None:
        return None
    # Reject confident-but-wrong readings rather than show a guess (spec §10):
    #  - a span covering most of the plan means the ray escaped through a door
    #    and ran to the far exterior wall;
    #  - an extreme aspect ratio is the signature of an open-plan room with no
    #    wall on one side (can't be measured honestly).
    if w_u > 0.85 * span_x or h_u > 0.85 * span_y:
        return None
    if max(w_u, h_u) / max(min(w_u, h_u), 1e-6) > 2.8:
        return None
    w, h = w_u * unit_to_feet, h_u * unit_to_feet
    if w < 2 or h < 2:        # implausibly small -> don't trust it
        return None
    return f"{_fmt_ftin(w)} x {_fmt_ftin(h)}"


class ParseError(Exception):
    """Raised when a DXF cannot be turned into a usable floor plan."""


def _is_furniture(block_name: str) -> bool:
    if not block_name:
        return False
    up = block_name.upper()
    return any(frag in up for frag in FURNITURE_FRAGMENTS)


def _clean_text(raw: str) -> str:
    """Strip MTEXT formatting codes and whitespace."""
    if raw is None:
        return ""
    txt = re.sub(r"\\[A-Za-z][^;\\]*;", "", raw)
    txt = txt.replace("\\P", " ").replace("{", "").replace("}", "")
    txt = re.sub(r"\\~", " ", txt)
    return " ".join(txt.split()).strip()


def _looks_like_room(text: str) -> bool:
    up = text.upper().strip()
    if not up or len(up) > 28:
        return False
    if up in EQUIPMENT_TAGS:
        return False
    # unit code, e.g. "1 BED - 1A" / "2BR-204": digit + hyphen means a code
    if "-" in up and re.search(r"\d", up):
        return False
    # bare unit type, e.g. "1 BED", "2 BR" (a title, not a room)
    if re.match(r"^\d+\s*(BED|BR)$", up):
        return False
    tokens = re.split(r"[\s/]+", up)
    for tok in tokens:
        bare = tok.strip(".")
        if bare in ROOM_VOCAB or tok in ROOM_VOCAB:
            return True
    return False


def _collect_entities(entity, block_name, depth, out_geom, out_text, role_sets):
    """Recursively walk an entity, exploding INSERTs into primitives.

    role_sets is precomputed once by parse_dxf: {drop, floor_hatch, label}.
    """
    if len(out_geom) >= MAX_PRIMS:   # geometry budget exhausted; stop
        return
    dxftype = entity.dxftype()
    layer = getattr(entity.dxf, "layer", "0")

    if dxftype == "INSERT":
        if depth >= MAX_DEPTH:
            return
        if _is_furniture(entity.dxf.name):
            return
        try:
            for sub in entity.virtual_entities():
                _collect_entities(sub, entity.dxf.name, depth + 1,
                                  out_geom, out_text, role_sets)
        except Exception:
            pass
        return

    drop_layers = role_sets["drop"]
    floor_hatch = role_sets["floor_hatch"]
    label_layers = role_sets["label"]

    if dxftype in ("TEXT", "MTEXT"):
        if layer in drop_layers:
            return
        try:
            raw = entity.text if dxftype == "MTEXT" else entity.dxf.text
            ins = entity.dxf.insert
            out_text.append({
                "text": _clean_text(raw),
                "x": float(ins[0]),
                "y": float(ins[1]),
                "layer": layer,
                "is_label_layer": layer in label_layers,
            })
        except Exception:
            pass
        return

    if layer in drop_layers:
        return
    if layer in floor_hatch and dxftype == "HATCH":
        return

    try:
        if dxftype == "LINE":
            s, e = entity.dxf.start, entity.dxf.end
            out_geom.append([layer, "line",
                             [(float(s[0]), float(s[1])),
                              (float(e[0]), float(e[1]))], block_name])

        elif dxftype in ("LWPOLYLINE", "POLYLINE"):
            pts = _cap_points([(float(p[0]), float(p[1]))
                               for p in entity.flattening(FLATTEN_DIST)])
            if len(pts) >= 2:
                out_geom.append([layer, "line", pts, block_name])

        elif dxftype in ("ARC", "CIRCLE", "ELLIPSE", "SPLINE"):
            pts = _cap_points([(float(p[0]), float(p[1]))
                               for p in entity.flattening(FLATTEN_DIST)])
            if len(pts) >= 2:
                out_geom.append([layer, "line", pts, block_name])

        elif dxftype == "HATCH":
            polys = []
            for p in entity.paths:
                try:
                    pp = ezpath.from_hatch_boundary_path(p)
                    poly = _cap_points([(float(v[0]), float(v[1]))
                                        for v in pp.flattening(FLATTEN_DIST)])
                    if len(poly) >= 3:
                        polys.append(poly)
                except Exception:
                    continue
            if polys:
                out_geom.append([layer, "hatch", polys, block_name])
    except Exception:
        pass


def _wall_extents(prims, wall_layers):
    xs, ys = [], []
    wl = set(wall_layers)
    for layer, kind, data, _ in prims:
        if layer not in wl:
            continue
        if kind == "line":
            xs += [p[0] for p in data]
            ys += [p[1] for p in data]
        else:
            for poly in data:
                xs += [p[0] for p in poly]
                ys += [p[1] for p in poly]
    if not xs:
        return None
    return min(xs), max(xs), min(ys), max(ys)


def parse_dxf(filepath, layer_map=None, seed_box_frac=0.13):
    """
    Parse a DXF file into geometry primitives + seeded room labels.

    Returns: { prims, labels, ignored_text, suggestions, extents }
    Raises ParseError for sheet exports / empty geometry.
    """
    layer_map = layer_map or DEFAULT_LAYER_MAP

    try:
        doc = ezdxf.readfile(filepath)
    except (IOError, ezdxf.DXFStructureError) as exc:
        raise ParseError(f"Could not read DXF: {exc}")

    msp = doc.modelspace()

    role_sets = {
        "drop": set(layer_map.get("drop", [])),
        "floor_hatch": set(layer_map.get("floor_hatch", [])),
        "label": set(layer_map.get("room_label", [])),
    }
    prims, raw_text = [], []
    for ent in msp:
        _collect_entities(ent, None, 0, prims, raw_text, role_sets)

    wall_layers = (layer_map.get("wall_line", []) +
                   layer_map.get("wall_fill", []))
    extents = _wall_extents(prims, wall_layers)
    unit_to_feet = _unit_to_feet(doc)
    wall_segs = _wall_segments(prims, wall_layers)

    if extents is None or len(prims) < 5:
        raise ParseError(
            "This file has no readable wall geometry. It looks like a SHEET "
            "export (just a titleblock). In Revit, export the floor plan "
            "VIEW instead of a sheet, then upload that DXF. "
            "See FLOORPLAN_WORKFLOW.md, Part 1."
        )

    minx, maxx, miny, maxy = extents
    span_x = max(maxx - minx, 1.0)
    span_y = max(maxy - miny, 1.0)
    box_w = span_x * seed_box_frac
    box_h = span_y * seed_box_frac

    warnings = []
    if len(prims) >= MAX_PRIMS:
        warnings.append(
            f"This file is very large or highly detailed — only the first "
            f"{MAX_PRIMS:,} geometry pieces were read. If the sheet looks "
            f"incomplete, export a single-unit floor plan VIEW rather than a "
            f"whole-floor or fully-detailed drawing.")

    labels, ignored = [], []
    suggestions = {"title": None, "suite": None, "sf": None}

    for t in raw_text:
        txt = t["text"]
        if not txt:
            continue
        if suggestions["sf"] is None:
            m = SF_RE.search(txt)
            if m:
                suggestions["sf"] = f"{m.group(1)} SF"
        if suggestions["suite"] is None:
            m = SUITE_RE.match(txt)
            if m:
                suggestions["suite"] = m.group(1)
        if suggestions["title"] is None and UNIT_TITLE_RE.search(txt):
            suggestions["title"] = txt.upper()

        if _looks_like_room(txt) and (t["is_label_layer"] or len(raw_text) < 60):
            x, y = t["x"], t["y"]
            x = min(max(x, minx), maxx)
            y = min(max(y, miny), maxy)
            rect = [
                max(x - box_w / 2, minx),
                min(x + box_w / 2, maxx),
                max(y - box_h / 2, miny),
                min(y + box_h / 2, maxy),
            ]
            dims = _estimate_dims(wall_segs, t["x"], t["y"], unit_to_feet,
                                  span_x, span_y)
            labels.append({
                "name": txt.upper(),
                "dims": dims,
                "dims_estimated": dims is not None,
                "seed_x": x,
                "seed_y": y,
                "rect": rect,
                "font_scale": 1.0,
                "show_dims": True,
            })
        else:
            ignored.append({"text": txt, "x": t["x"], "y": t["y"]})

    return {
        "prims": prims,
        "labels": labels,
        "ignored_text": ignored,
        "suggestions": suggestions,
        "warnings": warnings,
        "extents": {"minx": minx, "maxx": maxx, "miny": miny, "maxy": maxy},
    }
