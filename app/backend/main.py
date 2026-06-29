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
# import glob      # now via storage.glob (filesystem or Blob)
import io
import json
import logging
import os
import re
# import shutil    # now via storage.copy / storage.rmtree
import time
import uuid
import zipfile

from fastapi import FastAPI, UploadFile, File, Form, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response
from pydantic import BaseModel
from typing import Optional, List, Dict, Any

from ezdxf.filemanagement import readfile

from engine import (parse_dxf, ParseError, DEFAULT_LAYER_MAP, infer_layer_map,
                    render, render_png, SHEET_PNG_W, render_keyplan_sheet, autocrop_plate,
                    dwg_to_dxf, converter_available,
                    ConversionError, extract_brand, BrandError)
import storage   # filesystem (local/Docker) or Vercel Blob, chosen by env token

BASE = os.path.dirname(os.path.abspath(__file__))
# DATA_DIR override lets a serverless/container deployment point storage at a
# writable path (e.g. /tmp on Vercel, or a mounted volume); defaults to ./data.
DATA = os.environ.get("DATA_DIR") or os.path.join(BASE, "data")
PROP_DIR = os.path.join(DATA, "properties")
UP_DIR = os.path.join(DATA, "uploads")
SHEET_DIR = os.path.join(DATA, "sheets")
for d in (PROP_DIR, UP_DIR, SHEET_DIR):
    try:
        os.makedirs(d, exist_ok=True)
    except OSError:
        pass  # read-only FS (serverless) — storage routes to Blob instead
storage.ROOT = DATA   # paths under DATA map to blob keys relative to this root

MAX_UPLOAD_MB = 60
UPLOAD_TTL_HOURS = 168  # working files in uploads/ older than this get swept (1 week)

logger = logging.getLogger(__name__)

# Allowed CORS origins default to the local Vite dev server (which fronts this
# API via its /api proxy). Override in deployment with the ALLOWED_ORIGINS env
# var — a comma-separated list of origins (same pattern as UPLOAD_TTL_HOURS).
ALLOWED_ORIGINS = [o.strip() for o in os.environ.get(
    "ALLOWED_ORIGINS",
    "http://localhost:5173,http://127.0.0.1:5173").split(",") if o.strip()]

app = FastAPI(title="Floor Plan Sheet Generator")
app.add_middleware(CORSMiddleware, allow_origins=ALLOWED_ORIGINS,
                   allow_methods=["*"], allow_headers=["*"])


# --------------------------------------------------------------------------- #
# uploads cache sweep
# --------------------------------------------------------------------------- #
def sweep_uploads(max_age_hours: float = UPLOAD_TTL_HOURS) -> int:
    """Delete working files in uploads/ older than max_age_hours. Returns count."""
    cutoff = time.time() - max_age_hours * 3600
    removed = 0
    for fn in storage.glob(os.path.join(UP_DIR, "*")):
        try:
            if storage.getmtime(fn) < cutoff:
                storage.remove(fn)
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
    """Read a JSON file (or blob), returning `default` if it doesn't exist."""
    return storage.read_json(path, default)


def _write_json(path, data, **dump_kw):
    """Write JSON through the storage backend. The filesystem backend writes
    atomically (temp + replace) so a reader never sees a truncated file."""
    storage.write_json(path, data, **dump_kw)


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
    for fn in storage.glob(os.path.join(UP_DIR, f"{plate_id}_plate*")):
        return storage.read_bytes(fn)
    return None


def _css_family(fam):
    return (fam or "").replace("\\", "").replace("'", "")


def _png_width(meta):
    """Match the resvg re-render width to whatever the cairosvg path used: the
    default sheet is SHEET_PNG_W, but the plan_only export renders wider (see
    engine.render), so derive it from meta to keep branded/unbranded PNGs equal."""
    return (min(2400, max(1000, round(meta["page"]["w"] * 2)))
            if meta.get("plan_only") else SHEET_PNG_W)


def _apply_custom_fonts(svg, png, font_faces, png_width=SHEET_PNG_W):
    """Make uploaded brand fonts render everywhere. cairosvg ignores embedded
    fonts, so when a property carries font faces we (1) inline an @font-face so
    the SVG renders the font in any browser, and (2) re-render the PNG with
    resvg, which honours fonts loaded from files. Falls back to the cairosvg PNG
    if resvg is unavailable — the SVG still carries the font either way.

    png_width is the raster pixel width the cairosvg path used (engine.render's
    output_width), threaded in by the caller so the resvg re-render comes out at
    the same resolution — otherwise a branded PNG would differ in size from an
    unbranded one (notably the plan_only export, which renders wider than the
    default 900px sheet)."""
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
        for f in faces:
            head, _, b64 = f["data"].partition(",")
            ext = ".otf" if ("otf" in head or "opentype" in head) else ".ttf"
            fd, path = tempfile.mkstemp(suffix=ext)
            os.write(fd, base64.b64decode(b64))
            os.close(fd)
            tmp.append(path)
        # uploaded brand faces take precedence; render_png appends the bundled
        # Arimo/Gelasio fallbacks so any text the brand font doesn't cover (and the
        # generic serif/sans stacks) still render on a no-system-font host.
        png2 = render_png(svg2, png_width, extra_font_files=tmp)
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
        # fontTools types subtables as the abstract DefaultTable, so Pylance can't
        # see the concrete `name` table's getDebugName — narrow to Any (runtime fine).
        nm: Any = f["name"]
        family = (nm.getDebugName(16) or nm.getDebugName(1)
                  or os.path.splitext(os.path.basename(file.filename or "Font"))[0])
    except Exception as exc:
        raise HTTPException(status_code=422, detail=f"Couldn't read that font: {exc}")
    mime = "font/otf" if ext == ".otf" else "font/ttf"
    data = f"data:{mime};base64," + base64.b64encode(raw).decode("ascii")
    return {"family": family, "data": data,
            "format": "opentype" if ext == ".otf" else "truetype"}


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
async def parse(file: UploadFile = File(...), property_id: Optional[str] = Form(None),
                layer_map: Optional[str] = Form(None)):
    sweep_uploads()
    if property_id:
        _safe_id(property_id, "property id")
    override_map = None
    if isinstance(layer_map, str) and layer_map.strip():
        try:
            override_map = json.loads(layer_map)
            if not isinstance(override_map, dict):
                raise ValueError("layer_map must be a JSON object")
        except (ValueError, TypeError) as exc:
            raise HTTPException(status_code=422, detail=f"Bad layer_map: {exc}")
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
    # The source upload is written to the LOCAL filesystem (not storage/Blob) on
    # purpose: ezdxf and the ODA converter need a real file path, and it's only
    # read within this same request. On serverless this lands in /tmp (writable).
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
    # Precedence: an explicit override (the user corrected the map) wins; then a
    # saved property's map; then the Revit-scheme default. The default/property
    # path stays byte-identical to before — inference only steps in on failure.
    used_map = override_map or (prop or {}).get("layer_map") or DEFAULT_LAYER_MAP
    layer_report = None
    layer_inferred = False
    try:
        result = parse_dxf(dxf_path, layer_map=used_map)
    except ParseError as exc:
        # No wall geometry under the chosen map. If the user explicitly chose it,
        # respect that and surface the error. Otherwise the file likely uses a
        # non-Revit layer scheme — auto-detect the roles and try once more.
        if override_map is not None:
            raise HTTPException(status_code=422, detail=str(exc))
        try:
            doc = readfile(dxf_path)
            inferred, layer_report = infer_layer_map(doc)
            result = parse_dxf(dxf_path, layer_map=inferred)
            used_map, layer_inferred = inferred, True
        except ParseError:
            raise HTTPException(status_code=422, detail=str(exc))   # original guidance
        except Exception:
            logger.exception("Layer auto-detection failed for doc_id=%s", doc_id)
            raise HTTPException(status_code=422, detail=str(exc))
    # prims.json must persist for a later /render (possibly a different instance),
    # so it goes through storage (Blob in serverless).
    storage.write_json(os.path.join(UP_DIR, f"{doc_id}.prims.json"),
                       {"prims": result["prims"], "extents": result["extents"]})
    return {"doc_id": doc_id, "labels": result["labels"],
            "ignored_text": result["ignored_text"], "suggestions": result["suggestions"],
            "warnings": result.get("warnings", []), "extents": result["extents"],
            "prim_count": len(result["prims"]),
            "layer_map_used": used_map, "layer_report": layer_report,
            "layer_inferred": layer_inferred}


# --------------------------------------------------------------------------- #
# plate upload (key plans)
# --------------------------------------------------------------------------- #
@app.post("/plate")
async def upload_plate(file: UploadFile = File(...)):
    sweep_uploads()
    raw = await file.read()
    if len(raw) > 25 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="Plate image too large (max 25 MB).")
    # The uploaded image is the finished key plan. Trim its surrounding
    # whitespace once, on intake, and store the cropped PNG — every consumer
    # (preview, footer, standalone) then sees the same tight image (WYSIWYG).
    cropped = autocrop_plate(raw)
    plate_id = uuid.uuid4().hex[:12]
    storage.write_bytes(os.path.join(UP_DIR, f"{plate_id}_plate.png"), cropped)
    w = h = None
    try:
        from PIL import Image
        w, h = Image.open(io.BytesIO(cropped)).size
    except Exception:
        pass
    return {"plate_id": plate_id, "width": w, "height": h}


@app.get("/plate/{plate_id}")
def get_plate(plate_id: str):
    """Serve a previously uploaded (cropped) plate image. A saved key plan
    persists only the plate_id, so re-opening or restoring a sheet needs this to
    repaint the key-plan preview."""
    _safe_id(plate_id, "plate id")
    for fn in storage.glob(os.path.join(UP_DIR, f"{plate_id}_plate*")):
        media = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".gif": "image/gif",
                 ".webp": "image/webp", ".bmp": "image/bmp"}.get(
                     os.path.splitext(fn)[1].lower(), "image/png")
        data = storage.read_bytes(fn)
        if data is not None:
            return Response(content=data, media_type=media)
    raise HTTPException(status_code=404, detail="Plate not found or expired.")


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
    plan_only: bool = False  # bare line drawing — no header/footer/watermark/keyplan
    paint_image: Optional[str] = None  # PNG data-URI of the manual paint layer, baked into exports only
    live_preview: bool = False  # editor preview: omit the watermark from the SVG (the frontend overlays it above the paint canvas) — exports bake it inline


def _load_prims(doc_id):
    data = storage.read_json(os.path.join(UP_DIR, f"{doc_id}.prims.json"))
    if data is None:
        raise HTTPException(status_code=404,
                            detail="Upload expired or not found. Re-upload the file.")
    return data["prims"]


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
    config["plan_only"] = req.plan_only
    config["paint_image"] = req.paint_image
    config["live_preview"] = req.live_preview
    if req.keyplan and not req.plan_only:
        kp = dict(req.keyplan)
        kp["plate_bytes"] = _plate_bytes(kp.get("plate_id"))
        config["keyplan"] = kp
    try:
        svg, png, meta = render(prims, config)
    except (ParseError, ValueError, KeyError) as exc:
        # Bad input / config (unknown palette key, malformed geometry, …) —
        # meaningful to the user, so surface it as a 422 with the real message.
        raise HTTPException(status_code=422, detail=str(exc))
    except Exception:
        # Genuinely unexpected server fault: log the full traceback for ops and
        # return a generic message rather than leaking internals to the client.
        logger.exception("Unexpected error rendering doc_id=%s", req.doc_id)
        raise HTTPException(status_code=500, detail="Render failed — see server logs")
    # Embed any uploaded brand fonts so they render in both the SVG and the PNG.
    svg, png = _apply_custom_fonts(svg, png, config.get("font_faces"), png_width=_png_width(meta))

    keyplan_svg = None
    if req.keyplan and not req.plan_only and req.keyplan.get("placement") == "standalone":
        try:
            keyplan_svg = render_keyplan_sheet(config)
        except (ParseError, ValueError, KeyError) as exc:
            raise HTTPException(status_code=422, detail=str(exc))
        except Exception:
            logger.exception("Unexpected error rendering standalone key plan "
                             "for doc_id=%s", req.doc_id)
            raise HTTPException(status_code=500, detail="Key plan failed — see server logs")

    sheet_id = None
    if req.save and req.property_id and not req.plan_only:
        out = os.path.join(SHEET_DIR, req.property_id)
        index = os.path.join(out, "index.json")
        sheets = _read_json(index, [])

        # Overwrite the existing entry when re-saving a re-opened sheet; otherwise mint a new id.
        existing = next((s for s in sheets if s.get("sheet_id") == req.sheet_id), None) \
            if req.sheet_id else None
        sheet_id = req.sheet_id if existing else uuid.uuid4().hex[:10]

        storage.write_text(os.path.join(out, f"{sheet_id}.svg"), svg)
        storage.write_bytes(os.path.join(out, f"{sheet_id}.png"), png)
        kp_path = os.path.join(out, f"{sheet_id}-keyplan.svg")
        if keyplan_svg:
            storage.write_text(kp_path, keyplan_svg)
        else:
            storage.remove(kp_path)   # key plan dropped since last save (no-op if absent)
        # persist the editable config + geometry so the sheet can be re-opened
        _write_json(os.path.join(out, f"{sheet_id}.config.json"),
                    {"property_id": req.property_id, "metadata": req.metadata,
                     "rooms": req.rooms, "keyplan": req.keyplan,
                     "paint_image": req.paint_image})
        prims_src = os.path.join(UP_DIR, f"{req.doc_id}.prims.json")
        if storage.exists(prims_src):
            storage.copy(prims_src, os.path.join(out, f"{sheet_id}.prims.json"))
        # Preserve the key-plan plate image alongside the sheet. The config keeps
        # only the plate_id, and the plate lives in the sweepable uploads area — so
        # copy it in (mirroring the prims-on-save above) to survive the uploads sweep.
        # The ext is derived from the stored upload filename, not assumed.
        plate_id = (req.keyplan or {}).get("plate_id")
        if plate_id:
            for fn in storage.glob(os.path.join(UP_DIR, f"{plate_id}_plate*")):
                ext = os.path.splitext(fn)[1]
                try:
                    storage.copy(fn, os.path.join(out, f"{sheet_id}-plate{ext}"))
                except OSError:
                    pass   # plate already swept — degrade gracefully, don't fail the save
                break
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
    for fn in sorted(storage.listdir(PROP_DIR)):
        if fn.endswith(".json"):
            prop = storage.read_json(os.path.join(PROP_DIR, fn))
            if prop is not None:
                out.append(prop)
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
    brand_fonts: Optional[List[str]] = None                # font names detected in a brand PDF, kept as hints
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
    storage.remove(os.path.join(PROP_DIR, f"{prop_id}.json"))   # no-op if absent
    # Also drop the property's saved-sheet library; otherwise the orphaned
    # entries keep surfacing in GET /sheets.
    storage.rmtree(os.path.join(SHEET_DIR, prop_id))
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
    for prop_id in sorted(storage.listdir(SHEET_DIR)):
        if not storage.isdir(os.path.join(SHEET_DIR, prop_id)):
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
    if not storage.exists(index):
        raise HTTPException(status_code=404, detail="Sheet not found.")
    sheets = _read_json(index, [])
    entry = next((s for s in sheets if s.get("sheet_id") == sheet_id), None)
    if entry is None:
        raise HTTPException(status_code=404, detail="Sheet not found.")
    entry["title"] = title
    _write_json(index, sheets, indent=2, ensure_ascii=False)
    cfg_path = os.path.join(d, f"{sheet_id}.config.json")
    if storage.exists(cfg_path):
        cfg = _read_json(cfg_path, {})
        cfg.setdefault("metadata", {})["title"] = title
        _write_json(cfg_path, cfg, ensure_ascii=False)
    return {"sheet_id": sheet_id, "title": title}


@app.get("/sheets/{prop_id}/{sheet_id}.svg")
def get_sheet_svg(prop_id, sheet_id):
    _safe_id(prop_id, "property id")
    _safe_id(sheet_id, "sheet id")
    text = storage.read_text(os.path.join(SHEET_DIR, prop_id, f"{sheet_id}.svg"))
    if text is None:
        raise HTTPException(status_code=404, detail="Sheet not found.")
    return Response(text, media_type="image/svg+xml")


@app.get("/sheets/{prop_id}/{sheet_id}.png")
def get_sheet_png(prop_id, sheet_id):
    _safe_id(prop_id, "property id")
    _safe_id(sheet_id, "sheet id")
    data = storage.read_bytes(os.path.join(SHEET_DIR, prop_id, f"{sheet_id}.png"))
    if data is None:
        raise HTTPException(status_code=404, detail="Sheet not found.")
    return Response(data, media_type="image/png")


class _DownloadItem(BaseModel):
    property_id: str
    sheet_id: str


class DownloadRequest(BaseModel):
    items: List[_DownloadItem]
    formats: List[str] = ["png"]   # any of "png", "svg"
    plan_only: bool = False        # re-render a bare plan (no branding) instead of the saved sheet


def _zip_arcname(used, name):
    """Disambiguate identical export names (same property+title) inside the zip."""
    if name not in used:
        used[name] = 0
        return name
    used[name] += 1
    stem, ext = os.path.splitext(name)
    return f"{stem}-{used[name]}{ext}"


def _render_plan_only(prop_id, sheet_id):
    """Re-render a saved sheet as a bare plan (no header/footer/watermark) from
    its stored config + geometry. Returns {"svg": str, "png": bytes} or None when
    the sheet lacks the saved config/prims needed to re-render (older saves)."""
    d = os.path.join(SHEET_DIR, prop_id)
    cfg = _read_json(os.path.join(d, f"{sheet_id}.config.json"))
    raw = _read_json(os.path.join(d, f"{sheet_id}.prims.json"))
    if not isinstance(cfg, dict) or not isinstance(raw, dict) or "prims" not in raw:
        return None
    config = compose_config(load_property(prop_id), cfg.get("metadata"), cfg.get("rooms"))
    config["plan_only"] = True
    svg, png, meta = render(raw["prims"], config)
    svg, png = _apply_custom_fonts(svg, png, config.get("font_faces"), png_width=_png_width(meta))
    return {"svg": svg, "png": png}


@app.post("/sheets/download")
def download_sheets(req: DownloadRequest):
    """Bundle the chosen format(s) for the selected sheets into one ZIP. Keyplans
    excluded. With plan_only, re-renders each sheet as a bare plan instead of
    pulling the saved branded artifacts."""
    if not req.items:
        raise HTTPException(status_code=400, detail="No sheets selected.")
    exts = [e for e in ("svg", "png") if e in req.formats]   # filter + normalize order
    if not exts:
        raise HTTPException(status_code=400, detail="Pick at least one format (PNG or SVG).")
    buf = io.BytesIO()
    used: Dict[str, int] = {}
    added = 0
    # mirror Library.jsx exportName(): "<prop-slug>-<title-slug>" (+ "-plan")
    slug = lambda s: (s or "floorplan").strip().replace(" ", "-").lower()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for it in req.items:
            _safe_id(it.property_id, "property id")
            _safe_id(it.sheet_id, "sheet id")
            d = os.path.join(SHEET_DIR, it.property_id)
            entry = next((s for s in _read_index(it.property_id)
                          if s.get("sheet_id") == it.sheet_id), None)
            title = (entry or {}).get("title", "")
            name = f"{slug(it.property_id)}-{slug(title)}" + ("-plan" if req.plan_only else "")
            if req.plan_only:
                try:
                    rendered = _render_plan_only(it.property_id, it.sheet_id)
                except Exception:
                    logger.exception("Plan-only re-render failed for %s/%s",
                                     it.property_id, it.sheet_id)
                    rendered = None
                if not rendered:
                    continue
                for ext in exts:
                    data = rendered["svg"].encode("utf-8") if ext == "svg" else rendered["png"]
                    zf.writestr(_zip_arcname(used, f"{name}.{ext}"), data)
                    added += 1
            else:
                for ext in exts:
                    data = storage.read_bytes(os.path.join(d, f"{it.sheet_id}.{ext}"))
                    if data is not None:
                        zf.writestr(_zip_arcname(used, f"{name}.{ext}"), data)
                        added += 1
    if not added:
        detail = ("Couldn't re-render plan-only versions — the selected sheets were "
                  "saved without their source geometry." if req.plan_only
                  else "None of the selected sheets had files.")
        raise HTTPException(status_code=404, detail=detail)
    buf.seek(0)
    return Response(buf.getvalue(), media_type="application/zip",
                    headers={"Content-Disposition": 'attachment; filename="floorplans.zip"'})


@app.post("/sheets/{prop_id}/{sheet_id}/reopen")
def reopen_sheet(prop_id, sheet_id):
    _safe_id(prop_id, "property id")
    _safe_id(sheet_id, "sheet id")
    d = os.path.join(SHEET_DIR, prop_id)
    cfg_path = os.path.join(d, f"{sheet_id}.config.json")
    prims_path = os.path.join(d, f"{sheet_id}.prims.json")
    if not storage.exists(cfg_path) or not storage.exists(prims_path):
        raise HTTPException(
            status_code=404,
            detail="This sheet can't be re-opened — its source geometry wasn't "
                   "saved with it. Re-upload the DXF to edit.")
    cfg = _read_json(cfg_path)
    if not isinstance(cfg, dict):
        raise HTTPException(
            status_code=404,
            detail="This sheet can't be re-opened — its saved config is "
                   "unreadable. Re-upload the DXF to edit.")
    new_doc = uuid.uuid4().hex[:12]
    storage.copy(prims_path, os.path.join(UP_DIR, f"{new_doc}.prims.json"))
    cfg["doc_id"] = new_doc
    # Restore the preserved key-plan plate back into uploads under the SAME
    # plate_id the config references, so GET /plate/{plate_id} resolves again and
    # the box-placement picker can repaint. Mirrors the prims copy-back above.
    plate_id = (cfg.get("keyplan") or {}).get("plate_id")
    if plate_id:
        _safe_id(plate_id, "plate id")
        for fn in storage.glob(os.path.join(d, f"{sheet_id}-plate*")):
            ext = os.path.splitext(fn)[1]
            try:
                storage.copy(fn, os.path.join(UP_DIR, f"{plate_id}_plate{ext}"))
            except OSError:
                pass   # preserved plate missing — picker just won't repaint
            break
    return cfg


@app.delete("/sheets/{prop_id}/{sheet_id}")
def delete_sheet(prop_id, sheet_id):
    _safe_id(prop_id, "property id")
    _safe_id(sheet_id, "sheet id")
    d = os.path.join(SHEET_DIR, prop_id)
    for fn in storage.glob(os.path.join(d, f"{sheet_id}.*")) + \
            storage.glob(os.path.join(d, f"{sheet_id}-keyplan.*")) + \
            storage.glob(os.path.join(d, f"{sheet_id}-plate*")):
        storage.remove(fn)
    index = os.path.join(d, "index.json")
    if storage.exists(index):
        sheets = [s for s in _read_json(index, []) if s.get("sheet_id") != sheet_id]
        _write_json(index, sheets, indent=2, ensure_ascii=False)
    return {"deleted": sheet_id}


# --------------------------------------------------------------------------- #
# production: serve the built SPA from this same app (single origin)
# --------------------------------------------------------------------------- #
# The frontend always calls /api/* (frontend/api.js). In dev, Vite's proxy strips
# /api before forwarding (vite.config.js), so this app sees root paths. In any
# built deployment (Vercel function, or the single-service container) the request
# arrives WITH /api, so we strip it here to reach the root-mounted routes above.
# Stripping is always safe: if /api was already removed upstream, there's nothing
# to strip. So the middleware is unconditional; only serving the static SPA is
# gated on a built frontend/dist being present (it isn't in the Vercel function).
class _StripApiPrefix:
    """ASGI middleware: rewrite /api/* -> /* so the SPA's same-origin API calls
    reach the existing root-mounted routes. No-op when /api isn't present."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope.get("type") == "http":
            path = scope.get("path", "")
            if path == "/api" or path.startswith("/api/"):
                scope = dict(scope)
                scope["path"] = path[4:] or "/"
                scope["raw_path"] = scope["path"].encode("utf-8")
        await self.app(scope, receive, send)


app.add_middleware(_StripApiPrefix)

_FRONTEND_DIST = os.path.normpath(
    os.path.join(os.path.dirname(__file__), "..", "frontend", "dist"))
if os.path.isdir(_FRONTEND_DIST):
    from fastapi.staticfiles import StaticFiles
    # Mounted last so every API route above takes precedence; html=True serves
    # index.html at / (the app is a single page with no client-side routing).
    app.mount("/", StaticFiles(directory=_FRONTEND_DIST, html=True), name="spa")
