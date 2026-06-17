"""
Floor Plan Sheet Generator — backend service.

  POST /parse                         DXF/DWG upload -> geometry cached + labels
  POST /plate                         floor-plate image upload (key plans)
  POST /extract-brand                 brand PDF/image -> auto palette + font hints
  POST /render                        config (+ optional key plan) -> SVG/PNG
  GET  /properties                    list configured properties
  GET/PUT/DELETE /properties/{id}     property CRUD (brand + layer map)
  GET  /sheets/{prop}                 saved sheets for a property
  GET  /sheets/{prop}/{id}.svg|.png   download a saved sheet
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
import shutil
import time
import uuid

from fastapi import FastAPI, UploadFile, File, Form, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response
from pydantic import BaseModel
from typing import Optional, List, Dict, Any

from engine import (parse_dxf, ParseError, DEFAULT_LAYER_MAP, render,
                    render_keyplan_sheet, dwg_to_dxf, converter_available,
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
def load_property(prop_id):
    path = os.path.join(PROP_DIR, f"{prop_id}.json")
    if not os.path.isfile(path):
        return None
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def save_property(prop):
    with open(os.path.join(PROP_DIR, f"{prop['id']}.json"), "w", encoding="utf-8") as f:
        json.dump(prop, f, indent=2, ensure_ascii=False)


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
            "layer_map": layer_map_override or prop.get("layer_map") or DEFAULT_LAYER_MAP,
            "metadata": meta, "rooms": rooms or []}


def _plate_bytes(plate_id):
    if not plate_id:
        return None
    for fn in glob.glob(os.path.join(UP_DIR, f"{plate_id}_plate*")):
        with open(fn, "rb") as f:
            return f.read()
    return None


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
    prims = _load_prims(req.doc_id)
    prop = load_property(req.property_id) if req.property_id else None
    config = compose_config(prop, req.metadata, req.rooms, req.palette, req.layer_map)
    if req.keyplan:
        kp = dict(req.keyplan)
        kp["plate_bytes"] = _plate_bytes(kp.get("plate_id"))
        config["keyplan"] = kp
    try:
        svg, png, meta = render(prims, config)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Render failed: {exc}")

    keyplan_svg = None
    if req.keyplan and req.keyplan.get("placement") == "standalone":
        try:
            keyplan_svg = render_keyplan_sheet(config)
        except Exception as exc:
            raise HTTPException(status_code=500, detail=f"Key plan failed: {exc}")

    sheet_id = None
    if req.save and req.property_id:
        sheet_id = uuid.uuid4().hex[:10]
        out = os.path.join(SHEET_DIR, req.property_id)
        os.makedirs(out, exist_ok=True)
        with open(os.path.join(out, f"{sheet_id}.svg"), "w", encoding="utf-8") as f:
            f.write(svg)
        with open(os.path.join(out, f"{sheet_id}.png"), "wb") as f:
            f.write(png)
        if keyplan_svg:
            with open(os.path.join(out, f"{sheet_id}-keyplan.svg"), "w", encoding="utf-8") as f:
                f.write(keyplan_svg)
        # persist the editable config + geometry so the sheet can be re-opened
        json.dump({"property_id": req.property_id, "metadata": req.metadata,
                   "rooms": req.rooms, "keyplan": req.keyplan},
                  open(os.path.join(out, f"{sheet_id}.config.json"), "w", encoding="utf-8"))
        prims_src = os.path.join(UP_DIR, f"{req.doc_id}.prims.json")
        if os.path.isfile(prims_src):
            shutil.copy(prims_src, os.path.join(out, f"{sheet_id}.prims.json"))
        index = os.path.join(out, "index.json")
        sheets = json.load(open(index, encoding="utf-8")) if os.path.isfile(index) else []
        sheets.insert(0, {"sheet_id": sheet_id, "title": req.metadata.get("title", ""),
                          "suite": req.metadata.get("suite", ""),
                          "sf": req.metadata.get("sf", ""),
                          "keyplan": bool(keyplan_svg),
                          "created": time.strftime("%Y-%m-%d %H:%M")})
        json.dump(sheets, open(index, "w", encoding="utf-8"), indent=2, ensure_ascii=False)

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
    layer_map: Dict[str, List[str]] = {}


@app.put("/properties/{prop_id}")
def put_property(prop_id, prop: Property):
    data = prop.model_dump()
    data["id"] = prop_id
    if not data.get("layer_map"):
        data["layer_map"] = DEFAULT_LAYER_MAP
    save_property(data)
    return data


@app.delete("/properties/{prop_id}")
def delete_property(prop_id):
    path = os.path.join(PROP_DIR, f"{prop_id}.json")
    if os.path.isfile(path):
        os.remove(path)
    return {"deleted": prop_id}


# --------------------------------------------------------------------------- #
# sheet library
# --------------------------------------------------------------------------- #
@app.get("/sheets/{prop_id}")
def list_sheets(prop_id):
    index = os.path.join(SHEET_DIR, prop_id, "index.json")
    if not os.path.isfile(index):
        return []
    with open(index, encoding="utf-8") as f:
        return json.load(f)


@app.get("/sheets/{prop_id}/{sheet_id}.svg")
def get_sheet_svg(prop_id, sheet_id):
    path = os.path.join(SHEET_DIR, prop_id, f"{sheet_id}.svg")
    if not os.path.isfile(path):
        raise HTTPException(status_code=404, detail="Sheet not found.")
    return Response(open(path, encoding="utf-8").read(), media_type="image/svg+xml")


@app.get("/sheets/{prop_id}/{sheet_id}.png")
def get_sheet_png(prop_id, sheet_id):
    path = os.path.join(SHEET_DIR, prop_id, f"{sheet_id}.png")
    if not os.path.isfile(path):
        raise HTTPException(status_code=404, detail="Sheet not found.")
    return Response(open(path, "rb").read(), media_type="image/png")


@app.post("/sheets/{prop_id}/{sheet_id}/reopen")
def reopen_sheet(prop_id, sheet_id):
    d = os.path.join(SHEET_DIR, prop_id)
    cfg_path = os.path.join(d, f"{sheet_id}.config.json")
    prims_path = os.path.join(d, f"{sheet_id}.prims.json")
    if not os.path.isfile(cfg_path) or not os.path.isfile(prims_path):
        raise HTTPException(
            status_code=404,
            detail="This sheet can't be re-opened — its source geometry wasn't "
                   "saved with it. Re-upload the DXF to edit.")
    cfg = json.load(open(cfg_path, encoding="utf-8"))
    new_doc = uuid.uuid4().hex[:12]
    shutil.copy(prims_path, os.path.join(UP_DIR, f"{new_doc}.prims.json"))
    cfg["doc_id"] = new_doc
    return cfg


@app.delete("/sheets/{prop_id}/{sheet_id}")
def delete_sheet(prop_id, sheet_id):
    d = os.path.join(SHEET_DIR, prop_id)
    for fn in glob.glob(os.path.join(d, f"{sheet_id}.*")) + \
            glob.glob(os.path.join(d, f"{sheet_id}-keyplan.*")):
        try:
            os.remove(fn)
        except OSError:
            pass
    index = os.path.join(d, "index.json")
    if os.path.isfile(index):
        sheets = [s for s in json.load(open(index, encoding="utf-8")) if s.get("sheet_id") != sheet_id]
        json.dump(sheets, open(index, "w", encoding="utf-8"), indent=2, ensure_ascii=False)
    return {"deleted": sheet_id}
