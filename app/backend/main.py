"""
Floor Plan Sheet Generator — backend service.

  POST /parse                         DXF/DWG upload -> geometry cached + labels
  POST /plate                         floor-plate image upload (key plans)
  POST /extract-brand                 brand PDF/image -> auto palette + font hints
  POST /render                        config (+ optional key plan) -> SVG/PNG
  GET  /properties                    list configured properties
  GET/PUT/DELETE /properties/{id}     property CRUD (brand + layer map)
  GET  /sheets                        unified library: all sheets, all properties
  GET  /sheets/{prop}                 saved sheets for one property
  GET  /sheets/{prop}/{id}.svg|.png   download a saved sheet
  PATCH /sheets/{prop}/{id}           rename a saved sheet (library label)
  POST /sheets/{prop}/{id}/reopen     re-register geometry to keep editing
  DELETE /sheets/{prop}/{id}          remove a saved sheet
  GET  /capabilities                  feature/runtime flags

State lives on disk under data/. The uploads cache is swept automatically.
"""

import base64
import glob
import io
import json
import os
import re
import shutil
import time
import uuid

from fastapi import FastAPI, UploadFile, File, Form, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response
from pydantic import BaseModel
from typing import Optional, List, Dict, Any

from engine import (parse_dxf, ParseError, DEFAULT_LAYER_MAP, render,
                    render_keyplan_sheet, trace_plate, colorize_trace,
                    dwg_to_dxf, converter_available,
                    ConversionError, extract_brand, BrandError)

BASE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(BASE, "data")
PROP_DIR = os.path.join(DATA, "properties")
UP_DIR = os.path.join(DATA, "uploads")
SHEET_DIR = os.path.join(DATA, "sheets")
for d in (PROP_DIR, UP_DIR, SHEET_DIR):
    os.makedirs(d, exist_ok=True)

MAX_UPLOAD_MB = 60
UPLOAD_TTL_HOURS = 24   # working files in uploads/ older than this get swept

app = FastAPI(title="Floor Plan Sheet Generator")
app.add_middleware(CORSMiddleware, allow_origins=["*"],
                   allow_methods=["*"], allow_headers=["*"])


# --------------------------------------------------------------------------- #
# uploads cache sweep
# --------------------------------------------------------------------------- #
def sweep_uploads(max_age_hours: float = UPLOAD_TTL_HOURS) -> int:
    """Delete working files in uploads/ older than max_age_hours. Returns count."""
    cutoff = time.time() - max_age_hours * 3600
    removed = 0
    for fn in glob.glob(os.path.join(UP_DIR, "*")):
        try:
            if os.path.isfile(fn) and os.path.getmtime(fn) < cutoff:
                os.remove(fn)
                removed += 1
        except OSError:
            pass
    return removed


@app.on_event("startup")
def _on_startup():
    sweep_uploads()


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
_ID_RE = re.compile(r"^[A-Za-z0-9_-]+$")


def _safe_id(value, what="id"):
    """Reject ids that could escape the data dirs via path separators. All ids
    (property/sheet/plate/doc) are generated as uuid hex or slugs, so a strict
    allow-list is safe — and on Windows it also blocks the backslash-segment
    traversal that the default URL path converter would otherwise let through."""
    if not isinstance(value, str) or not _ID_RE.match(value):
        raise HTTPException(status_code=400, detail=f"Invalid {what}.")
    return value


def _read_json(path, default=None):
    """Read a JSON file, returning `default` if it doesn't exist."""
    if not os.path.isfile(path):
        return default
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _write_json(path, data, **dump_kw):
    """Write JSON atomically: dump to a sibling temp file, then os.replace it
    into place — so a reader (or a crash mid-write) never sees a truncated file.
    The replace is atomic on a single filesystem, which all of data/ is."""
    tmp = f"{path}.{uuid.uuid4().hex}.tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, **dump_kw)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


def load_property(prop_id):
    return _read_json(os.path.join(PROP_DIR, f"{prop_id}.json"))


def save_property(prop):
    _write_json(os.path.join(PROP_DIR, f"{prop['id']}.json"), prop,
                indent=2, ensure_ascii=False)


def compose_config(prop, metadata, rooms, palette_override=None, layer_map_override=None):
    prop = prop or {}
    meta = {
        "property_name": prop.get("name", ""),
        "location": prop.get("location", ""),
        "lockup": prop.get("lockup", ""),
        "watermark": prop.get("watermark", prop.get("lockup", "")),
        "watermark_image": prop.get("watermark_image"),
        "footer_address": prop.get("footer_address", ""),
        "header_right": prop.get("header_right", "FLOOR PLAN"),
        "disclaimer": prop.get("disclaimer"),
    }
    meta.update({k: v for k, v in (metadata or {}).items() if v is not None})
    return {"palette": palette_override or prop.get("palette"),
            "fonts": prop.get("fonts"),
            "font_faces": prop.get("font_faces"),
            "layer_map": layer_map_override or prop.get("layer_map") or DEFAULT_LAYER_MAP,
            "metadata": meta, "rooms": rooms or []}


def _plate_bytes(plate_id):
    if not plate_id:
        return None
    for fn in glob.glob(os.path.join(UP_DIR, f"{plate_id}_plate*")):
        with open(fn, "rb") as f:
            return f.read()
    return None


def _css_family(fam):
    return (fam or "").replace("\\", "").replace("'", "")


def _apply_custom_fonts(svg, png, font_faces):
    """Make uploaded brand fonts render everywhere. cairosvg ignores embedded
    fonts, so when a property carries font faces we (1) inline an @font-face so
    the SVG renders the font in any browser, and (2) re-render the PNG with
    resvg, which honours fonts loaded from files. Falls back to the cairosvg PNG
    if resvg is unavailable — the SVG still carries the font either way."""
    faces = [f for f in (font_faces or []) if f.get("data") and f.get("family")]
    if not faces:
        return svg, png
    style = "<style>" + "".join(
        "@font-face{font-family:'%s';src:url(%s);}" % (_css_family(f["family"]), f["data"])
        for f in faces) + "</style>"
    svg2 = svg.replace(">", ">" + style, 1)   # inject right after the <svg …> tag
    tmp = []
    try:
        import tempfile
        import resvg_py
        for f in faces:
            head, _, b64 = f["data"].partition(",")
            ext = ".otf" if ("otf" in head or "opentype" in head) else ".ttf"
            fd, path = tempfile.mkstemp(suffix=ext)
            os.write(fd, base64.b64decode(b64))
            os.close(fd)
            tmp.append(path)
        png2 = bytes(resvg_py.svg_to_bytes(svg_string=svg2, width=900, font_files=tmp))
        return svg2, png2
    except Exception:
        return svg2, png
    finally:
        for p in tmp:
            try:
                os.remove(p)
            except OSError:
                pass


@app.post("/font-info")
async def font_info(file: UploadFile = File(...)):
    """Read a TTF/OTF font's family name (so the sheet can reference it) and
    return it embedded as a data URI to store on the property."""
    raw = await file.read()
    if len(raw) > 4 * 1024 * 1024:
        raise HTTPException(status_code=413,
                            detail="Font file too large (max 4 MB). Use a single TTF/OTF weight.")
    ext = os.path.splitext((file.filename or "").lower())[1]
    if ext not in (".ttf", ".otf", ".ttc"):
        raise HTTPException(status_code=415, detail=(
            "Use a .ttf or .otf font file (not WOFF) so the PNG export can embed it."))
    try:
        from fontTools.ttLib import TTFont, TTCollection
        f = (TTCollection(io.BytesIO(raw)).fonts[0] if ext == ".ttc"
             else TTFont(io.BytesIO(raw)))
        nm = f["name"]
        family = (nm.getDebugName(16) or nm.getDebugName(1)
                  or os.path.splitext(os.path.basename(file.filename or "Font"))[0])
    except Exception as exc:
        raise HTTPException(status_code=422, detail=f"Couldn't read that font: {exc}")
    mime = "font/otf" if ext == ".otf" else "font/ttf"
    data = f"data:{mime};base64," + base64.b64encode(raw).decode("ascii")
    return {"family": family, "data": data,
            "format": "opentype" if ext == ".otf" else "truetype"}


def _trace_mask(plate_id, seal):
    """Auto-traced footprint mask for (plate, seal), computed once and cached on
    disk (the morphology + BFS are too slow to run per render). Returns the
    grayscale mask PNG bytes and coverage fraction, or (None, 0.0) if no plate."""
    seal = max(7, min(61, int(seal) | 1))   # clamp + force odd
    cache = os.path.join(UP_DIR, f"{plate_id}_trace{seal}.png")
    if os.path.isfile(cache):
        with open(cache, "rb") as f:
            return f.read(), None
    raw = _plate_bytes(plate_id)
    if not raw:
        return None, 0.0
    mask, cov = trace_plate(raw, seal=seal)
    with open(cache, "wb") as f:
        f.write(mask)
    return mask, cov


# --------------------------------------------------------------------------- #
# capabilities / health
# --------------------------------------------------------------------------- #
@app.get("/capabilities")
def capabilities():
    return {"dwg_conversion": converter_available(),
            "formats_accepted": ["dxf"] + (["dwg"] if converter_available() else []),
            "rejected": {"rvt": "Export the floor plan as a DXF VIEW from Revit first."}}


@app.get("/health")
def health():
    return {"ok": True}


# --------------------------------------------------------------------------- #
# parse
# --------------------------------------------------------------------------- #
@app.post("/parse")
async def parse(file: UploadFile = File(...), property_id: Optional[str] = Form(None)):
    sweep_uploads()
    if property_id:
        _safe_id(property_id, "property id")
    name = (file.filename or "").lower()
    raw = await file.read()
    if len(raw) > MAX_UPLOAD_MB * 1024 * 1024:
        raise HTTPException(status_code=413, detail=(
            f"File is over {MAX_UPLOAD_MB} MB. That usually means a whole-building "
            f"or heavily-detailed export. Upload a single-unit floor plan VIEW as DXF."))
    if name.endswith(".rvt"):
        raise HTTPException(status_code=415, detail=(
            "Can't read .rvt files. Export the floor plan as a DXF VIEW from Revit "
            "first (not a sheet), then upload that. See FLOORPLAN_WORKFLOW.md, Part 1."))
    doc_id = uuid.uuid4().hex[:12]
    src_path = os.path.join(UP_DIR, f"{doc_id}_{os.path.basename(name) or 'upload'}")
    with open(src_path, "wb") as f:
        f.write(raw)
    dxf_path = src_path
    if name.endswith(".dwg"):
        try:
            dxf_path = dwg_to_dxf(src_path)
        except ConversionError as exc:
            raise HTTPException(status_code=422, detail=str(exc))
    elif not name.endswith(".dxf"):
        raise HTTPException(status_code=415, detail=(
            "Unsupported file type. Upload a DXF (or DWG if the server has the "
            "ODA File Converter)."))
    prop = load_property(property_id) if property_id else None
    layer_map = (prop or {}).get("layer_map") or DEFAULT_LAYER_MAP
    try:
        result = parse_dxf(dxf_path, layer_map=layer_map)
    except ParseError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    with open(os.path.join(UP_DIR, f"{doc_id}.prims.json"), "w", encoding="utf-8") as f:
        json.dump({"prims": result["prims"], "extents": result["extents"]}, f)
    return {"doc_id": doc_id, "labels": result["labels"],
            "ignored_text": result["ignored_text"], "suggestions": result["suggestions"],
            "warnings": result.get("warnings", []), "extents": result["extents"],
            "prim_count": len(result["prims"])}


# --------------------------------------------------------------------------- #
# plate upload (key plans)
# --------------------------------------------------------------------------- #
@app.post("/plate")
async def upload_plate(file: UploadFile = File(...)):
    sweep_uploads()
    raw = await file.read()
    if len(raw) > 25 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="Plate image too large (max 25 MB).")
    ext = os.path.splitext(file.filename or "plate.png")[1] or ".png"
    plate_id = uuid.uuid4().hex[:12]
    with open(os.path.join(UP_DIR, f"{plate_id}_plate{ext}"), "wb") as f:
        f.write(raw)
    w = h = None
    try:
        from PIL import Image
        w, h = Image.open(io.BytesIO(raw)).size
    except Exception:
        pass
    return {"plate_id": plate_id, "width": w, "height": h}


class TraceRequest(BaseModel):
    plate_id: str
    seal: int = 35
    palette: Optional[Dict[str, str]] = None


@app.post("/plate/trace")
def trace_plate_preview(req: TraceRequest):
    """Auto-trace a plate into a footprint silhouette and return a coloured
    preview (data URI) + coverage, so the UI can show the result, let the user
    tune seal strength, and fall back to the raw screenshot if it won't trace."""
    _safe_id(req.plate_id, "plate id")
    mask, cov = _trace_mask(req.plate_id, req.seal)
    if mask is None:
        raise HTTPException(status_code=404, detail="Plate not found or expired.")
    png = colorize_trace(mask, req.palette or {})
    data_uri = "data:image/png;base64," + base64.b64encode(png).decode("ascii")
    return {"preview": data_uri, "coverage": cov}


# --------------------------------------------------------------------------- #
# brand extraction (property setup auto-fill)
# --------------------------------------------------------------------------- #
@app.post("/extract-brand")
async def extract_brand_file(file: UploadFile = File(...)):
    raw = await file.read()
    if len(raw) > 25 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="Brand file too large (max 25 MB).")
    try:
        return extract_brand(raw, file.filename or "")
    except BrandError as exc:
        raise HTTPException(status_code=422, detail=str(exc))


# --------------------------------------------------------------------------- #
# render
# --------------------------------------------------------------------------- #
class RenderRequest(BaseModel):
    doc_id: str
    property_id: Optional[str] = None
    metadata: Dict[str, Any] = {}
    rooms: List[Dict[str, Any]] = []
    palette: Optional[Dict[str, str]] = None
    layer_map: Optional[Dict[str, List[str]]] = None
    keyplan: Optional[Dict[str, Any]] = None
    save: bool = False
    sheet_id: Optional[str] = None   # overwrite this saved sheet instead of minting a new one
    want_png: bool = False   # include base64 PNG in the response (for download)


def _load_prims(doc_id):
    path = os.path.join(UP_DIR, f"{doc_id}.prims.json")
    if not os.path.isfile(path):
        raise HTTPException(status_code=404,
                            detail="Upload expired or not found. Re-upload the file.")
    with open(path, encoding="utf-8") as f:
        return json.load(f)["prims"]


@app.post("/render")
def do_render(req: RenderRequest):
    _safe_id(req.doc_id, "doc id")
    if req.property_id:
        _safe_id(req.property_id, "property id")
    if req.sheet_id:
        _safe_id(req.sheet_id, "sheet id")
    if req.keyplan and req.keyplan.get("plate_id"):
        _safe_id(req.keyplan["plate_id"], "plate id")
    prims = _load_prims(req.doc_id)
    prop = load_property(req.property_id) if req.property_id else None
    config = compose_config(prop, req.metadata, req.rooms, req.palette, req.layer_map)
    if req.keyplan:
        kp = dict(req.keyplan)
        kp["plate_bytes"] = _plate_bytes(kp.get("plate_id"))
        if kp.get("mode") == "traced" and kp.get("plate_id"):
            mask, _ = _trace_mask(kp["plate_id"], kp.get("seal", 35))
            if mask is not None:
                kp["silhouette_bytes"] = colorize_trace(mask, config.get("palette") or {})
        config["keyplan"] = kp
    try:
        svg, png, meta = render(prims, config)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Render failed: {exc}")
    # Embed any uploaded brand fonts so they render in both the SVG and the PNG.
    svg, png = _apply_custom_fonts(svg, png, config.get("font_faces"))

    keyplan_svg = None
    if req.keyplan and req.keyplan.get("placement") == "standalone":
        try:
            keyplan_svg = render_keyplan_sheet(config)
        except Exception as exc:
            raise HTTPException(status_code=500, detail=f"Key plan failed: {exc}")

    sheet_id = None
    if req.save and req.property_id:
        out = os.path.join(SHEET_DIR, req.property_id)
        os.makedirs(out, exist_ok=True)
        index = os.path.join(out, "index.json")
        sheets = _read_json(index, [])

        # Overwrite the existing entry when re-saving a re-opened sheet; otherwise mint a new id.
        existing = next((s for s in sheets if s.get("sheet_id") == req.sheet_id), None) \
            if req.sheet_id else None
        sheet_id = req.sheet_id if existing else uuid.uuid4().hex[:10]

        with open(os.path.join(out, f"{sheet_id}.svg"), "w", encoding="utf-8") as f:
            f.write(svg)
        with open(os.path.join(out, f"{sheet_id}.png"), "wb") as f:
            f.write(png)
        kp_path = os.path.join(out, f"{sheet_id}-keyplan.svg")
        if keyplan_svg:
            with open(kp_path, "w", encoding="utf-8") as f:
                f.write(keyplan_svg)
        elif os.path.isfile(kp_path):
            os.remove(kp_path)   # key plan was dropped since the last save
        # persist the editable config + geometry so the sheet can be re-opened
        _write_json(os.path.join(out, f"{sheet_id}.config.json"),
                    {"property_id": req.property_id, "metadata": req.metadata,
                     "rooms": req.rooms, "keyplan": req.keyplan})
        prims_src = os.path.join(UP_DIR, f"{req.doc_id}.prims.json")
        if os.path.isfile(prims_src):
            shutil.copy(prims_src, os.path.join(out, f"{sheet_id}.prims.json"))
        entry = {"sheet_id": sheet_id, "title": req.metadata.get("title", ""),
                 "suite": req.metadata.get("suite", ""),
                 "sf": req.metadata.get("sf", ""),
                 "keyplan": bool(keyplan_svg),
                 "created": existing["created"] if existing else time.strftime("%Y-%m-%d %H:%M"),
                 "updated": time.strftime("%Y-%m-%d %H:%M:%S")}  # cache-busts the library thumbnail
        if existing:
            sheets[sheets.index(existing)] = entry   # keep its position in the library
        else:
            sheets.insert(0, entry)
        _write_json(index, sheets, indent=2, ensure_ascii=False)

    png_b64 = base64.b64encode(png).decode("ascii") if req.want_png else None
    return {"svg": svg, "keyplan_svg": keyplan_svg, "sheet_id": sheet_id,
            "meta": meta, "png_b64": png_b64}


# --------------------------------------------------------------------------- #
# properties CRUD
# --------------------------------------------------------------------------- #
@app.get("/properties")
def list_properties():
    out = []
    for fn in sorted(os.listdir(PROP_DIR)):
        if fn.endswith(".json"):
            with open(os.path.join(PROP_DIR, fn), encoding="utf-8") as f:
                out.append(json.load(f))
    return out


@app.get("/properties/{prop_id}")
def get_property(prop_id):
    _safe_id(prop_id, "property id")
    prop = load_property(prop_id)
    if not prop:
        raise HTTPException(status_code=404, detail="Property not found.")
    return prop


class Property(BaseModel):
    id: str
    name: str = ""
    location: str = ""
    lockup: str = ""
    watermark: str = ""
    watermark_image: Optional[str] = None   # data URI; overrides the text watermark
    footer_address: str = ""
    header_right: str = "FLOOR PLAN"
    disclaimer: Optional[str] = None
    palette: Dict[str, str] = {}
    fonts: Optional[Dict[str, str]] = None
    brand_swatches: Optional[List[Dict[str, Any]]] = None  # detected colors, kept for re-picking
    font_faces: Optional[List[Dict[str, Any]]] = None      # uploaded brand fonts: {family, data, format}
    layer_map: Dict[str, List[str]] = {}


@app.put("/properties/{prop_id}")
def put_property(prop_id, prop: Property):
    _safe_id(prop_id, "property id")
    data = prop.model_dump()
    data["id"] = prop_id
    if not data.get("layer_map"):
        data["layer_map"] = DEFAULT_LAYER_MAP
    save_property(data)
    return data


@app.delete("/properties/{prop_id}")
def delete_property(prop_id):
    _safe_id(prop_id, "property id")
    path = os.path.join(PROP_DIR, f"{prop_id}.json")
    if os.path.isfile(path):
        os.remove(path)
    return {"deleted": prop_id}


# --------------------------------------------------------------------------- #
# sheet library
# --------------------------------------------------------------------------- #
def _read_index(prop_id):
    return _read_json(os.path.join(SHEET_DIR, prop_id, "index.json"), [])


@app.get("/sheets")
def list_all_sheets():
    """Every saved sheet across all properties, each annotated with its
    property id + name, newest first — the unified library."""
    out = []
    for prop_id in sorted(os.listdir(SHEET_DIR)) if os.path.isdir(SHEET_DIR) else []:
        if not os.path.isdir(os.path.join(SHEET_DIR, prop_id)):
            continue
        prop = load_property(prop_id) or {}
        pname = prop.get("name") or prop_id
        for s in _read_index(prop_id):
            out.append({**s, "property_id": prop_id, "property_name": pname})
    out.sort(key=lambda s: s.get("created", ""), reverse=True)
    return out


@app.get("/sheets/{prop_id}")
def list_sheets(prop_id):
    _safe_id(prop_id, "property id")
    return _read_index(prop_id)


class RenameRequest(BaseModel):
    title: str


@app.patch("/sheets/{prop_id}/{sheet_id}")
def rename_sheet(prop_id, sheet_id, req: RenameRequest):
    """Relabel a saved sheet in the library (and in its config, so a re-open
    carries the new title). The already-exported SVG/PNG are left untouched —
    the printed title updates only on the next re-open + re-save."""
    _safe_id(prop_id, "property id")
    _safe_id(sheet_id, "sheet id")
    d = os.path.join(SHEET_DIR, prop_id)
    title = req.title.strip()
    index = os.path.join(d, "index.json")
    if not os.path.isfile(index):
        raise HTTPException(status_code=404, detail="Sheet not found.")
    sheets = _read_json(index, [])
    entry = next((s for s in sheets if s.get("sheet_id") == sheet_id), None)
    if entry is None:
        raise HTTPException(status_code=404, detail="Sheet not found.")
    entry["title"] = title
    _write_json(index, sheets, indent=2, ensure_ascii=False)
    cfg_path = os.path.join(d, f"{sheet_id}.config.json")
    if os.path.isfile(cfg_path):
        cfg = _read_json(cfg_path, {})
        cfg.setdefault("metadata", {})["title"] = title
        _write_json(cfg_path, cfg, ensure_ascii=False)
    return {"sheet_id": sheet_id, "title": title}


@app.get("/sheets/{prop_id}/{sheet_id}.svg")
def get_sheet_svg(prop_id, sheet_id):
    _safe_id(prop_id, "property id")
    _safe_id(sheet_id, "sheet id")
    path = os.path.join(SHEET_DIR, prop_id, f"{sheet_id}.svg")
    if not os.path.isfile(path):
        raise HTTPException(status_code=404, detail="Sheet not found.")
    with open(path, encoding="utf-8") as f:
        return Response(f.read(), media_type="image/svg+xml")


@app.get("/sheets/{prop_id}/{sheet_id}.png")
def get_sheet_png(prop_id, sheet_id):
    _safe_id(prop_id, "property id")
    _safe_id(sheet_id, "sheet id")
    path = os.path.join(SHEET_DIR, prop_id, f"{sheet_id}.png")
    if not os.path.isfile(path):
        raise HTTPException(status_code=404, detail="Sheet not found.")
    with open(path, "rb") as f:
        return Response(f.read(), media_type="image/png")


@app.post("/sheets/{prop_id}/{sheet_id}/reopen")
def reopen_sheet(prop_id, sheet_id):
    _safe_id(prop_id, "property id")
    _safe_id(sheet_id, "sheet id")
    d = os.path.join(SHEET_DIR, prop_id)
    cfg_path = os.path.join(d, f"{sheet_id}.config.json")
    prims_path = os.path.join(d, f"{sheet_id}.prims.json")
    if not os.path.isfile(cfg_path) or not os.path.isfile(prims_path):
        raise HTTPException(
            status_code=404,
            detail="This sheet can't be re-opened — its source geometry wasn't "
                   "saved with it. Re-upload the DXF to edit.")
    cfg = _read_json(cfg_path)
    new_doc = uuid.uuid4().hex[:12]
    shutil.copy(prims_path, os.path.join(UP_DIR, f"{new_doc}.prims.json"))
    cfg["doc_id"] = new_doc
    return cfg


@app.delete("/sheets/{prop_id}/{sheet_id}")
def delete_sheet(prop_id, sheet_id):
    _safe_id(prop_id, "property id")
    _safe_id(sheet_id, "sheet id")
    d = os.path.join(SHEET_DIR, prop_id)
    for fn in glob.glob(os.path.join(d, f"{sheet_id}.*")) + \
            glob.glob(os.path.join(d, f"{sheet_id}-keyplan.*")):
        try:
            os.remove(fn)
        except OSError:
            pass
    index = os.path.join(d, "index.json")
    if os.path.isfile(index):
        sheets = [s for s in _read_json(index, []) if s.get("sheet_id") != sheet_id]
        _write_json(index, sheets, indent=2, ensure_ascii=False)
    return {"deleted": sheet_id}
