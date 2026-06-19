"""
Shared test fixtures for the Floor Plan Sheet Generator backend suite.

Everything here is **hermetic and synthetic** — DXFs are built in-memory with
ezdxf and images with PIL, so the suite has no dependency on the transient
`data/uploads/` cache (which the 24h sweep can empty at any time) or on a real
Revit export. Each builder encodes *known* geometry, layers, text and units so
tests can assert exact output rather than "it didn't crash".

Layer names follow DEFAULT_LAYER_MAP (the Revit-export default in parse.py).
"""

import io
import os
import sys
import tempfile

# --- make `engine` and `main` importable no matter the cwd ------------------
_BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import ezdxf  # noqa: E402
from PIL import Image, ImageDraw  # noqa: E402


# --------------------------------------------------------------------------- #
# DXF builders
# --------------------------------------------------------------------------- #
def _add_walls(msp, layer, kind, rect):
    """Draw the four edges of `rect` (x0, y0, x1, y1) on `layer` as the given
    entity `kind` ('line' | 'lwpolyline' | 'polyline')."""
    x0, y0, x1, y1 = rect
    ring = [(x0, y0), (x1, y0), (x1, y1), (x0, y1), (x0, y0)]
    if kind == "line":
        for a, b in zip(ring, ring[1:]):
            msp.add_line(a, b, dxfattribs={"layer": layer})
    elif kind == "lwpolyline":
        msp.add_lwpolyline(ring, dxfattribs={"layer": layer})
    elif kind == "polyline":
        msp.add_polyline2d(ring, dxfattribs={"layer": layer})
    else:
        raise ValueError(kind)


def build_unit_dxf(path, *, insunits=2, wall_kind="line",
                   include_text=True, include_furniture=True):
    """A representative single-unit view export.

    20 x 15 (drawing-unit) outer room on A-WALL, poche hatch on A-WALL-PATT,
    a door / glazing / overhead-dashed segment, a 'drop' entity, and (optionally)
    a furniture block that must be dropped and a set of annotation texts:
      - 'BEDROOM'  -> a room label (kept, seeded)
      - '2 BED'    -> a unit-title suggestion (NOT a room label)
      - '204'      -> a suite suggestion
      - '650 SF'   -> a square-footage suggestion
      - 'NORTH'    -> ignored text (not a room, not a suggestion)

    `insunits`: DXF $INSUNITS code (2 = feet, 0 = unitless).
    `wall_kind`: entity type for the walls — use 'lwpolyline'/'polyline' to
    exercise the polyline geometry path.
    """
    doc = ezdxf.new("R2010")
    doc.header["$INSUNITS"] = insunits
    msp = doc.modelspace()

    _add_walls(msp, "A-WALL", wall_kind, (0, 0, 20, 15))
    hatch = msp.add_hatch(dxfattribs={"layer": "A-WALL-PATT"})
    hatch.paths.add_polyline_path([(0, 0), (0.4, 0), (0.4, 15), (0, 15)])

    msp.add_line((5, 0), (7, 0), dxfattribs={"layer": "A-DOOR"})
    msp.add_line((10, 15), (14, 15), dxfattribs={"layer": "A-GLAZ"})
    msp.add_line((2, 2), (4, 4), dxfattribs={"layer": "A-DETL-HDLN"})
    # A-AREA-IDEN is a 'drop' layer -> this segment must never reach prims
    msp.add_line((1, 1), (2, 2), dxfattribs={"layer": "A-AREA-IDEN"})

    if include_text:
        for txt, xy in [("BEDROOM", (10, 7)), ("2 BED", (10, 12)),
                        ("204", (3, 13)), ("650 SF", (15, 13)),
                        ("NORTH", (18, 1))]:
            msp.add_text(txt, dxfattribs={"layer": "G-ANNO-TEXT"}).set_placement(xy)

    if include_furniture:
        blk = doc.blocks.new("SOFA-2SEAT")
        blk.add_line((0, 0), (1, 1), dxfattribs={"layer": "A-WALL"})
        msp.add_blockref("SOFA-2SEAT", (8, 8))

    doc.saveas(path)
    return path


def build_sheet_dxf(path):
    """A 'sheet' export: a titleblock and some text, but NO wall geometry.
    parse_dxf must reject this with a ParseError."""
    doc = ezdxf.new("R2010")
    msp = doc.modelspace()
    # titleblock border lives on a non-wall layer, so it is not wall geometry
    msp.add_line((0, 0), (100, 0), dxfattribs={"layer": "A-ANNO-TTLB"})
    msp.add_text("UNIT 1A", dxfattribs={"layer": "G-ANNO-TEXT"}).set_placement((10, 10))
    doc.saveas(path)
    return path


def write_temp_dxf(builder=build_unit_dxf, **kwargs):
    """Write a builder's DXF to a fresh temp file and return its path."""
    fd, path = tempfile.mkstemp(suffix=".dxf")
    os.close(fd)
    builder(path, **kwargs)
    return path


def unit_dxf_bytes(**kwargs):
    """The bytes of build_unit_dxf — for upload-endpoint tests."""
    path = write_temp_dxf(**kwargs)
    try:
        with open(path, "rb") as f:
            return f.read()
    finally:
        os.remove(path)


# --------------------------------------------------------------------------- #
# Geometry helpers for render / parse internals
# --------------------------------------------------------------------------- #
def box_segments(x0, y0, x1, y1):
    """The four wall segments (x1,y1,x2,y2) of a closed rectangle — the shape
    parse._estimate_dims / _span / _ray_hit consume."""
    corners = [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]
    return [(*corners[i], *corners[(i + 1) % 4]) for i in range(4)]


def base_render_config(**overrides):
    """A minimal valid render config; override any key."""
    cfg = {
        "metadata": {"title": "2 BED", "suite": "204", "sf": "650 SF",
                     "property_name": "TEST TOWER", "location": "CITY, ST",
                     "lockup": "800", "watermark": "800",
                     "footer_address": "1 MAIN ST"},
        "rooms": [],
    }
    cfg.update(overrides)
    return cfg


# --------------------------------------------------------------------------- #
# Synthetic images (brand extraction + key-plan trace)
# --------------------------------------------------------------------------- #
def brand_image_png(bands):
    """A PNG made of horizontal colour bands `[(hex, height_px), ...]`.
    Lets a test control exactly which colours dominate the image."""
    width = 80
    total_h = sum(h for _, h in bands)
    img = Image.new("RGB", (width, total_h))
    draw = ImageDraw.Draw(img)
    y = 0
    for hex_str, h in bands:
        rgb = tuple(int(hex_str.lstrip("#")[i:i + 2], 16) for i in (0, 2, 4))
        draw.rectangle([0, y, width, y + h], fill=rgb)
        y += h
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


def plate_png(size=(200, 150)):
    """A floor-plate screenshot: a thick black rectangular wall ring enclosing
    a white interior on a white background. trace_plate should seal it into a
    solid footprint silhouette."""
    img = Image.new("RGB", size, "white")
    draw = ImageDraw.Draw(img)
    w, h = size
    # outer dark ring (the 'walls'); interior left white
    draw.rectangle([20, 20, w - 20, h - 20], outline="black", width=8)
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()
