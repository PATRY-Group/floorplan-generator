"""
Layer-role auto-detection for DXFs that don't follow the Revit layer scheme.

The pipeline's default layer_map (DEFAULT_LAYER_MAP in parse.py) matches Revit's
National CAD Standard names — A-WALL, A-GLAZ, G-ANNO-TEXT, … . Files exported
from Revit (e.g. 800 Princess) line up and parse cleanly. Files drafted in plain
AutoCAD or by an outside firm carry their own house naming — A_WALL_FULL_N,
A_WALL_CAVITY, A_TEXT_BLOWUPS — so the default map matches nothing, `_wall_extents`
returns None, and `parse_dxf` rejects the file as "no readable wall geometry"
even though the walls and room labels are right there.

`infer_layer_map` walks the drawing, tallies what each layer carries, and guesses
a layer_map from the layer names *and their content*. Room-label layers are
detected from the text they hold (does it read like a room name?), which is the
strongest, most portable signal — it's what lets Armstrong's `A_TEXT_BLOWUPS`
seed labels without anyone naming it by hand.

Returns `(layer_map, report)`. The report lists every layer with the role it was
assigned and a confidence tag, so the UI can show the guess and let a human
correct it before rendering — auto-detect with manual override, never a silent
mis-map. See DXF_INGEST_NORMALIZER_SPEC.md (Findings block) for why this, and not
the original explode/unit/seed plan, is the fix.
"""

import re

from . import parse as _parse

# Roles we emit, in the same order the frontend's PropertySetup lists them.
ROLES = ["wall_line", "wall_fill", "door", "glazing", "dashed",
         "room_label", "drop", "floor_hatch"]


def _norm(name):
    """Lowercase a layer name and unify separators (_ - / and whitespace) to
    single spaces, so 'A_WALL_FULL_N' and 'A-WALL' compare on equal footing."""
    return re.sub(r"[\s/_-]+", " ", str(name).strip().lower())


def _has(norm, *needles):
    return any(n in norm for n in needles)


def _blank_tally():
    return {"line": 0, "hatch": 0, "text": 0, "room_text": 0, "samples": []}


def _walk(entity, depth, tallies):
    """Recursively tally entities per layer, exploding INSERTs exactly the way
    parse._collect_entities does (same furniture skip + MAX_DEPTH cap) but
    *counting only* — no geometry is built, so this stays cheap on big files."""
    dxftype = entity.dxftype()
    if dxftype == "INSERT":
        if depth >= _parse.MAX_DEPTH or _parse._is_furniture(entity.dxf.name):
            return
        try:
            for sub in entity.virtual_entities():
                _walk(sub, depth + 1, tallies)
        except Exception:
            pass
        return

    layer = getattr(entity.dxf, "layer", "0")
    t = tallies.setdefault(layer, _blank_tally())

    if dxftype in ("TEXT", "MTEXT"):
        t["text"] += 1
        try:
            raw = entity.text if dxftype == "MTEXT" else entity.dxf.text
            txt = _parse._clean_text(raw)
            if txt and _parse._looks_like_room(txt):
                t["room_text"] += 1
            if txt and len(t["samples"]) < 5:
                t["samples"].append(txt)
        except Exception:
            pass
    elif dxftype in ("LINE", "LWPOLYLINE", "POLYLINE", "ARC",
                     "CIRCLE", "ELLIPSE", "SPLINE"):
        t["line"] += 1
    elif dxftype == "HATCH":
        t["hatch"] += 1


def _classify(norm, t):
    """Best-guess (role, confidence) for one layer, or (None, 'unused').

    Priority matters: room-label (by content) and walls are resolved before the
    'drop' net so a text layer named e.g. 'A_TEXT_BLOWUPS' is kept as labels and
    not dropped. wall_fill is checked before wall_line so poché doesn't pose as
    the outline; dashed before floor so 'A-FLOR-OVHD' reads as dashed overhead."""
    # 1) Room labels — the text content is the strongest, most portable signal.
    if t["room_text"] > 0:
        return "room_label", "content"
    if t["text"] > 0 and _has(norm, "anno text", "room name", "roomname",
                              "rmname", "label", "text blowup"):
        return "room_label", "name"
    # 2) Walls.
    if _has(norm, "wall"):
        if _has(norm, "cavity", "patt", "poch", "hatch", "fill"):
            return "wall_fill", "name"
        return "wall_line", "name"
    # 3) Openings.
    if _has(norm, "door"):
        return "door", "name"
    if _has(norm, "glaz", "window"):
        return "glazing", "name"
    # 4) Overhead / dashed (before floor, so 'flor ovhd' lands here not floor).
    if _has(norm, "ovhd", "overhead", "hidden", "hdln", "dash"):
        return "dashed", "name"
    if _has(norm, "flor", "floor"):
        return "floor_hatch", "name"
    # 5) Tags / structure / annotation we don't render.
    if _has(norm, "area iden", "iden", "cols", "column", "stair", "strs",
            "grid", "defpoint", "title", "revision", "dim", "mech", "symb"):
        return "drop", "name"
    return None, "unused"


def infer_layer_map(doc):
    """Infer a layer_map for `doc` (an ezdxf document).

    Returns (layer_map, report):
      layer_map — {role: [layer, …]} for every ROLE (empty list when none match).
      report    — one dict per content-bearing layer:
                  {layer, line_count, text_count, room_text_count,
                   role, confidence, samples}.
                  confidence is 'content' | 'name' | 'fallback' | 'unused'.
    """
    msp = doc.modelspace()
    tallies = {}
    for ent in msp:
        _walk(ent, 0, tallies)

    layer_map = {r: [] for r in ROLES}
    report = []
    for layer, t in sorted(tallies.items()):
        role, conf = _classify(_norm(layer), t)
        if role:
            layer_map[role].append(layer)
        report.append({
            "layer": layer,
            "line_count": t["line"] + t["hatch"],
            "text_count": t["text"],
            "room_text_count": t["room_text"],
            "role": role,
            "confidence": conf,
            "samples": t["samples"],
        })

    # Fallback: nothing read as a wall means the file uses names we don't know.
    # Promote the most line-heavy non-text candidate to wall_line so the plan
    # still renders and gets extents, flagged low-confidence for human review.
    if not layer_map["wall_line"] and not layer_map["wall_fill"]:
        candidates = [r for r in report
                      if r["role"] in (None, "floor_hatch", "dashed")
                      and r["line_count"] > 0 and r["text_count"] == 0]
        if candidates:
            best = max(candidates, key=lambda r: r["line_count"])
            layer_map["wall_line"].append(best["layer"])
            best["role"] = "wall_line"
            best["confidence"] = "fallback"

    return layer_map, report
