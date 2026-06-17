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
            labels.append({
                "name": txt.upper(),
                "dims": None,
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
