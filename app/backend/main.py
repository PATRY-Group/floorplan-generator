"""
Floor Plan Sheet Generator — backend service.

  POST /parse                         DXF/DWG upload -> geometry cached + labels
  POST /plan-pdf                      finished floor-plan PDF -> cropped image cached
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
import hashlib
import io
import json
import logging
import os
import re
# import shutil    # now via storage.copy / storage.rmtree
import threading
import time
import uuid
import zipfile
from collections import OrderedDict
from contextlib import asynccontextmanager

from fastapi import FastAPI, UploadFile, File, Form, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response
from pydantic import BaseModel
from typing import Optional, List, Dict, Any

from ezdxf.filemanagement import readfile

from engine import (parse_dxf, ParseError, DEFAULT_LAYER_MAP, infer_layer_map,
                    render, render_image_plan, render_png, SHEET_PNG_W,
                    render_keyplan_sheet, autocrop_plate, pdf_to_png, PdfPlanError,
                    rotate_plate, rotate_box,
                    dwg_to_dxf, converter_available,
                    ConversionError, extract_brand, BrandError)
from engine.render import PAGE_W as _PAGE_W, PAGE_H as _PAGE_H
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

# Saved-sheet artifacts and plate images are content-stable for a given URL:
# the library cache-busts them with ?v={updated} on every re-save (Library.jsx),
# and a plate_id's cropped bytes never change. So they're safe to cache forever
# — this makes the library grid instant on revisit instead of re-fetching MBs.
IMMUTABLE_CACHE = "public, max-age=31536000, immutable"

# Bounded in-process memo for repeated /render calls with identical inputs (e.g.
# toggling a UI option back and forth, or re-opening the last-previewed state).
# Keyed by geometry (doc_id — stable once parsed) + the full config that drives
# the SVG. Preview/download only; saves are skipped (unique paint_image + disk
# side effects). Lost on serverless cold start and capped — a latency win, never
# a correctness dependency. Complements render.py's _POCHE_CACHE.
_RENDER_CACHE: "OrderedDict[str, tuple]" = OrderedDict()
_RENDER_CACHE_MAX = 32
# FastAPI runs sync endpoints in a threadpool, so guard the OrderedDict: a
# concurrent get/move_to_end racing an insert/popitem can KeyError (a 500).
_RENDER_CACHE_LOCK = threading.Lock()

# Client-supplied API base that gets interpolated into an SVG href — restrict it
# to a URL-path charset so it can't break out of the attribute (XSS guard).
_SAFE_BASE_RE = re.compile(r"^[A-Za-z0-9:/._-]{1,200}$")


def _render_cache_key(doc_id, config):
    """Stable hash of (doc_id, config). config may carry bytes (keyplan
    plate_bytes) and other non-JSON values — hash bytes by content, everything
    else by repr, so equal inputs collapse to the same key."""
    blob = json.dumps(config, sort_keys=True, default=lambda o:
                      hashlib.sha256(o).hexdigest() if isinstance(o, (bytes, bytearray))
                      else repr(o))
    return doc_id + ":" + hashlib.sha256(blob.encode("utf-8")).hexdigest()

logger = logging.getLogger(__name__)

# Allowed CORS origins default to the local Vite dev server (which fronts this
# API via its /api proxy). Override in deployment with the ALLOWED_ORIGINS env
# var — a comma-separated list of origins.
ALLOWED_ORIGINS = [o.strip() for o in os.environ.get(
    "ALLOWED_ORIGINS",
    "http://localhost:5173,http://127.0.0.1:5173").split(",") if o.strip()]

@asynccontextmanager
async def _lifespan(app):
    # Sweep stale uploads on boot (sweep_uploads is defined below; resolved at
    # call time). Replaces the deprecated @app.on_event("startup") hook.
    sweep_uploads()
    yield


app = FastAPI(title="Floor Plan Sheet Generator", lifespan=_lifespan)
app.add_middleware(CORSMiddleware, allow_origins=ALLOWED_ORIGINS,
                   allow_methods=["*"], allow_headers=["*"])


# --------------------------------------------------------------------------- #
# uploads cache sweep
# --------------------------------------------------------------------------- #
def sweep_uploads(max_age_hours: float = UPLOAD_TTL_HOURS) -> int:
    """Delete working files in uploads/ older than max_age_hours. Returns count."""
    cutoff = time.time() - max_age_hours * 3600
    removed = 0
    # Best-effort cleanup: it runs at startup and at the head of /parse and
    # /plate, so it must never break those. In Blob mode storage.* raises the
    # SDK's own errors (not OSError), so catch broadly — a listing hiccup should
    # skip the sweep, not 500 the upload.
    try:
        entries = storage.glob(os.path.join(UP_DIR, "*"))
    except Exception:
        logger.exception("Uploads sweep: listing failed; skipping")
        return 0
    for fn in entries:
        try:
            if storage.getmtime(fn) < cutoff:
                storage.remove(fn)
                removed += 1
        except Exception:
            pass
    return removed


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
    """Write JSON through the storage backend. The *filesystem* backend writes
    atomically (temp + replace) so a reader never sees a truncated file. The Blob
    backend (serverless) has no such guarantee and is eventually consistent — a
    read immediately after a write can briefly miss or stale it; fine for this
    low-traffic tool, but don't assume read-after-write here."""
    storage.write_json(path, data, **dump_kw)


# A property's index.json is a read-modify-write hotspot: save, rename, and
# delete all load the list, mutate it, and write it back. Starlette runs these
# sync endpoints in a threadpool, so two concurrent edits to the SAME property
# can interleave and drop one update (a saved sheet ends up orphaned — its files
# on disk but missing from the library). A per-property lock serialises those
# read-modify-write sequences; callers must re-read the index *inside* the lock.
# (This guards the in-process race, the realistic one for this low-traffic
# internal tool. It does not serialise across separate serverless instances —
# that would need a distributed lock, out of scope here.)
_index_locks: Dict[str, threading.Lock] = {}
_index_locks_guard = threading.Lock()


def _index_lock(prop_id):
    with _index_locks_guard:
        lk = _index_locks.get(prop_id)
        if lk is None:
            lk = _index_locks[prop_id] = threading.Lock()
        return lk


async def _read_capped(file, max_bytes, too_large_detail):
    """Read an upload without buffering an oversized body. The multipart parser
    has already spooled the body to a temp file, but reading the whole thing into
    a `bytes` is the memory spike that can OOM a small serverless function — so
    read at most one byte past the limit and reject if it's exceeded, instead of
    materialising gigabytes only to measure them. (A request-body cap at the
    platform/ASGI layer is the complete defence; this bounds the in-process cost.)"""
    raw = await file.read(max_bytes + 1)
    if len(raw) > max_bytes:
        raise HTTPException(status_code=413, detail=too_large_detail)
    return raw


def _verify_raster(raw, hint="image"):
    """Reject bytes that aren't a decodable raster with a 422. autocrop_plate
    swallows its own decode failure and returns the input unchanged, so without
    this a renamed/corrupt file is accepted and later renders a broken <image>."""
    from PIL import Image
    try:
        Image.open(io.BytesIO(raw)).verify()
    except Exception as exc:
        raise HTTPException(status_code=422, detail=(
            f"Couldn't read that {hint} — upload a valid PNG or JPG. ({exc})"))


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
        "logo_in_header": prop.get("logo_in_header", False),
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
    # Strip characters that could escape the quoted CSS family name or the
    # surrounding <style> element (', ", \, <, >). A no-op for real family names.
    return "".join(c for c in (fam or "") if c not in '"\'\\<>')


# A brand font is stored as a base64 data URI (produced by /font-info). Validate
# the shape before interpolating it into @font-face src:url(...) so a hand-crafted
# property can't break out of the <style> block. Also guarantees b64decode below
# gets clean input.
_FONT_DATA_RE = re.compile(r"^data:[A-Za-z0-9.+/-]+;base64,[A-Za-z0-9+/=\s]+$")


# Library grid thumbnails: ~600px wide stays sharp on retina-scaled cards yet is a
# small fraction of the full 2000px sheet's bytes. Generated by downscaling the
# saved sheet PNG (visually identical, just smaller) and cached as
# {sheet_id}.thumb.png alongside it (swept by delete_sheet's {sheet_id}.* glob).
THUMB_W = 600


def _thumb_png(png_bytes, width=THUMB_W):
    """Downscale a sheet PNG to a small library thumbnail. Returns the input
    unchanged if the source is already narrower than `width` or can't be decoded —
    a thumbnail is an optimization, never worth failing a save or a fetch over."""
    try:
        from PIL import Image
        im = Image.open(io.BytesIO(png_bytes))
        if im.width <= width:
            return png_bytes
        if im.mode not in ("RGB", "RGBA", "L"):
            im = im.convert("RGB")
        h = max(1, round(im.height * width / im.width))
        im = im.resize((width, h), Image.LANCZOS)
        buf = io.BytesIO()
        im.save(buf, format="PNG", optimize=True)
        return buf.getvalue()
    except Exception:
        logger.exception("thumbnail generation failed; falling back to full PNG")
        return png_bytes


# The sheet page is US Letter portrait proportions (see engine.render PAGE_W:PAGE_H
# = 1000:1294), so wrapping it at 8.5in wide yields a true 8.5x11in page. The PDF is
# a raster wrap of the already-correct branded PNG: guaranteed to match the PNG
# export (same fonts/watermark/paint), and no vector-SVG renderer to re-drop text on
# the fontless serverless host. See engine/render.py's resvg notes.
PDF_PAGE_WIDTH_IN = 8.5   # US Letter width; height follows the Letter-proportioned sheet -> 11in


def _png_to_pdf(png_bytes):
    """Wrap a sheet PNG in a one-page PDF whose page matches the sheet's own aspect
    (PDF_PAGE_WIDTH_IN inches wide), so it fills the page with no bands. resvg
    emits RGBA; flatten onto white first (Pillow's PDF encoder can't write alpha
    and would otherwise error or garble)."""
    from PIL import Image
    im = Image.open(io.BytesIO(png_bytes))
    if im.mode == "RGBA":
        bg = Image.new("RGB", im.size, (255, 255, 255))
        bg.paste(im, mask=im.split()[3])
        im = bg
    elif im.mode != "RGB":
        im = im.convert("RGB")
    # Landscape sheets (wider than tall) map to 11x8.5in; portrait to 8.5x11 — both
    # true US Letter, just rotated. Keyed off the rendered image's aspect.
    width_in = 11.0 if im.width > im.height else PDF_PAGE_WIDTH_IN
    dpi = max(1.0, im.width / width_in)
    buf = io.BytesIO()
    im.save(buf, format="PDF", resolution=dpi)
    return buf.getvalue()


def _png_width(meta):
    """Match the resvg re-render width to the engine's own PNG width so a
    branded (font) re-render comes out the same size as the unbranded one. The
    engine reports its actual raster width as meta["png_w"]; fall back to the old
    derivation for any meta that predates it."""
    w = meta.get("png_w")
    if w:
        return w
    return (min(2400, max(1000, round(meta["page"]["w"] * 2)))
            if meta.get("plan_only") else SHEET_PNG_W)


def _raster_with_faces(svg, png_width, font_faces):
    """resvg-raster `svg` at `png_width`, loading any uploaded brand fonts from
    temp files — resvg only honours fonts from *files*, so the @font-face data
    URI already inlined in the SVG (for browsers) isn't enough for the PNG. With
    no brand faces (or on error) it renders with the bundled fallback fonts that
    render_png always supplies. Shared by the inline save path and the lazy
    rebuild so both produce the identical PNG."""
    faces = [f for f in (font_faces or [])
             if f.get("family") and _FONT_DATA_RE.match((f.get("data") or "").strip())]
    if not faces:
        return render_png(svg, png_width)
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
        return render_png(svg, png_width, extra_font_files=tmp)
    except Exception:
        return render_png(svg, png_width)   # brand faces unusable — fallback fonts
    finally:
        for p in tmp:
            try:
                os.remove(p)
            except OSError:
                pass


def _apply_custom_fonts(svg, png, font_faces, png_width=SHEET_PNG_W):
    """Make uploaded brand fonts render everywhere. When a property carries font
    faces we (1) inline an @font-face so the SVG renders the font in any browser,
    and (2) re-render the PNG with resvg, which honours fonts loaded from files.
    Falls back to the engine's original PNG if the resvg re-render fails — the
    SVG still carries the font either way.

    png_width is the raster pixel width the engine rendered at (engine.render's
    output_width), threaded in by the caller so the resvg re-render comes out at
    the same resolution — otherwise a branded PNG would differ in size from an
    unbranded one (notably the plan_only export, which renders wider than the
    default 900px sheet)."""
    faces = [f for f in (font_faces or [])
             if f.get("family") and _FONT_DATA_RE.match((f.get("data") or "").strip())]
    if not faces:
        return svg, png
    style = "<style>" + "".join(
        "@font-face{font-family:'%s';src:url(%s);}" % (_css_family(f["family"]), f["data"])
        for f in faces) + "</style>"
    svg2 = svg.replace(">", ">" + style, 1)   # inject right after the <svg …> tag
    # A live preview skips the PNG (png is None): the inlined @font-face above is
    # all the on-screen SVG needs, so there's no raster to redo — return early
    # and don't pay for the resvg second pass.
    if png is None:
        return svg2, None
    return svg2, _raster_with_faces(svg2, png_width, faces)


def _sheet_png_bytes(prop_id, sheet_id):
    """The full-resolution sheet PNG, rasterized on demand. A save defers the
    raster (it persists only the SVG — the authoritative artifact), so the PNG is
    built here from the saved SVG on first access and cached back to disk. This is
    byte-identical to the inline save path: the saved SVG *is* the font-injected
    `svg2`, and we re-raster it with the same brand fonts (loaded from the
    property) at the same SHEET_PNG_W. Saved sheets are never plan_only, so the
    width is always SHEET_PNG_W. Returns None only if the sheet has no SVG."""
    d = os.path.join(SHEET_DIR, prop_id)
    png = storage.read_bytes(os.path.join(d, f"{sheet_id}.png"))
    if png is not None:
        return png
    svg = storage.read_text(os.path.join(d, f"{sheet_id}.svg"))
    if not svg:
        return None
    prop = load_property(prop_id)
    png = _raster_with_faces(svg, SHEET_PNG_W, (prop or {}).get("font_faces"))
    storage.write_bytes(os.path.join(d, f"{sheet_id}.png"), png)   # cache for next fetch
    return png


@app.post("/font-info")
async def font_info(file: UploadFile = File(...)):
    """Read a TTF/OTF font's family name (so the sheet can reference it) and
    return it embedded as a data URI to store on the property."""
    raw = await _read_capped(file, 4 * 1024 * 1024,
                             "Font file too large (max 4 MB). Use a single TTF/OTF weight.")
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
    return {"ok": True, "storage": storage.MODE}


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
            # Match /render's pydantic Dict[str, List[str]] shape: a role mapping
            # to a bare string (not a list) otherwise reaches parse_dxf and raises
            # an uncaught TypeError ("A-WALL" + []) -> 500 instead of a clean 422.
            if not isinstance(override_map, dict) or not all(
                    isinstance(v, list) and all(isinstance(x, str) for x in v)
                    for v in override_map.values()):
                raise ValueError(
                    "layer_map must be a JSON object of {role: [layer names]}")
        except (ValueError, TypeError) as exc:
            raise HTTPException(status_code=422, detail=f"Bad layer_map: {exc}")
    name = (file.filename or "").lower()
    raw = await _read_capped(file, MAX_UPLOAD_MB * 1024 * 1024, (
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
    # Sanitize the basename before it becomes a path: a char invalid on the host
    # FS (':' etc. on Windows, or an embedded NUL) would make open() raise -> 500.
    safe_name = re.sub(r"[^A-Za-z0-9._-]", "_", os.path.basename(name)) or "upload"
    src_path = os.path.join(UP_DIR, f"{doc_id}_{safe_name}")
    with open(src_path, "wb") as f:
        f.write(raw)
    dxf_path = src_path
    # Local temp files (raw upload + any converted DXF) are needed only within
    # this request; remove them in `finally`. In Blob mode the uploads sweep only
    # lists blob keys, so these local /tmp files would otherwise accumulate until
    # the disk fills. Only prims.json (below, via storage) must persist.
    cleanup_paths = [src_path]
    try:
        if name.endswith(".dwg"):
            try:
                dxf_path = dwg_to_dxf(src_path)
                cleanup_paths.append(dxf_path)
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
    finally:
        for p in cleanup_paths:
            try:
                os.remove(p)
            except OSError:
                pass


# --------------------------------------------------------------------------- #
# plate upload (key plans)
# --------------------------------------------------------------------------- #
@app.post("/plate")
async def upload_plate(file: UploadFile = File(...)):
    sweep_uploads()
    raw = await _read_capped(file, 25 * 1024 * 1024, "Plate image too large (max 25 MB).")
    _verify_raster(raw, "plate image")
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
            return Response(content=data, media_type=media,
                            headers={"Cache-Control": IMMUTABLE_CACHE})
    raise HTTPException(status_code=404, detail="Plate not found or expired.")


@app.get("/planimg/{doc_id}")
def get_planimg(doc_id: str):
    """Serve the cropped floor-plan raster for an image/PDF plan. Live previews
    reference the plan by this URL instead of re-embedding a multi-MB base64
    blob in every render's SVG (the saved sheet still inlines it, so it stays
    self-contained). Content is stable per doc_id, so cache it hard."""
    _safe_id(doc_id, "doc id")
    data = _load_planimg(doc_id)
    if data is None:
        raise HTTPException(status_code=404, detail="Plan image not found or expired.")
    return Response(content=data, media_type="image/png",
                    headers={"Cache-Control": IMMUTABLE_CACHE})


# --------------------------------------------------------------------------- #
# PDF plan upload (an already-finished floor plan — no vector geometry)
# --------------------------------------------------------------------------- #
@app.post("/plan-pdf")
async def upload_plan_pdf(file: UploadFile = File(...)):
    """Intake for an already-finished floor plan supplied as a PDF **or a raster
    image (PNG/JPG)**: rasterize the PDF's single page (images are used as-is),
    autocrop, and mint a doc_id the same way /parse does — so /render treats it
    uniformly with a DXF-sourced doc from here on (see _load_doc)."""
    sweep_uploads()
    name = (file.filename or "").lower()
    raw = await _read_capped(file, 25 * 1024 * 1024, "File too large (max 25 MB).")
    # Extension-trust only, matching /parse's convention — bad content behind a
    # correct extension surfaces as a 422 here (pdf_to_png / _verify_raster), not
    # at the extension gate (415).
    if name.endswith(".pdf"):
        try:
            png = pdf_to_png(raw)
        except PdfPlanError as exc:
            raise HTTPException(status_code=422, detail=str(exc))
    elif name.endswith((".png", ".jpg", ".jpeg")):
        _verify_raster(raw)     # autocrop can't reject junk, so validate here
        png = raw
    else:
        raise HTTPException(status_code=415,
                            detail="Upload a PDF, PNG, or JPG of the floor plan.")
    cropped = autocrop_plate(png)
    doc_id = uuid.uuid4().hex[:12]
    storage.write_bytes(os.path.join(UP_DIR, f"{doc_id}.planimg.png"), cropped)
    w = h = None
    try:
        from PIL import Image
        w, h = Image.open(io.BytesIO(cropped)).size
    except Exception:
        pass
    return {"doc_id": doc_id, "width": w, "height": h}


# --------------------------------------------------------------------------- #
# brand extraction (property setup auto-fill)
# --------------------------------------------------------------------------- #
@app.post("/extract-brand")
async def extract_brand_file(file: UploadFile = File(...)):
    raw = await _read_capped(file, 25 * 1024 * 1024, "Brand file too large (max 25 MB).")
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
    want_pdf: bool = False   # include base64 single-page PDF (wraps the PNG; for download)
    plan_only: bool = False  # bare line drawing — no header/footer/watermark/keyplan
    paint_image: Optional[str] = None  # PNG data-URI of the manual paint layer, baked into exports only
    live_preview: bool = False  # editor preview: omit the watermark from the SVG (the frontend overlays it above the paint canvas) — exports bake it inline
    asset_base: str = ""  # client's API base (e.g. "/api"); lets a live image-plan preview reference /planimg/{doc_id} by URL instead of inlining the raster
    orientation: str = "portrait"  # "portrait" (8.5x11) or "landscape" (11x8.5); swaps the page W/H


def _load_prims(doc_id):
    """DXF-sourced geometry for doc_id, or None if this doc_id is a PDF-plan
    upload (or expired/unknown — the caller distinguishes those)."""
    data = storage.read_json(os.path.join(UP_DIR, f"{doc_id}.prims.json"))
    return data["prims"] if data is not None else None


def _load_planimg(doc_id):
    """Raster plan image for doc_id, or None if this doc_id is a DXF upload."""
    return storage.read_bytes(os.path.join(UP_DIR, f"{doc_id}.planimg.png"))


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
    image_bytes = None if prims is not None else _load_planimg(req.doc_id)
    if prims is None and image_bytes is None:
        raise HTTPException(status_code=404,
                            detail="Upload expired or not found. Re-upload the file.")
    prop = load_property(req.property_id) if req.property_id else None
    config = compose_config(prop, req.metadata, req.rooms, req.palette, req.layer_map)
    config["plan_only"] = req.plan_only
    config["paint_image"] = req.paint_image
    config["live_preview"] = req.live_preview
    # Orientation may arrive top-level (RenderRequest.orientation) or ride in the
    # metadata (the frontend stores it there so it persists + autosaves with the sheet).
    _orient = req.orientation if req.orientation == "landscape" else (req.metadata or {}).get("orientation")
    config["orientation"] = "landscape" if _orient == "landscape" else "portrait"
    # The PNG is only needed when the client asks for it (download) or on save
    # (persisted to disk). A live preview uses only the SVG, so skip the 2000px
    # resvg raster (and its brand-font second pass) entirely — the big win here.
    # Rasterize the PNG inline only when the client needs it in the response
    # (download of a PNG/PDF). A save no longer forces it: the response returns as
    # soon as the SVG + metadata are persisted, and the PNG/thumbnail are built
    # lazily from the saved SVG on first fetch (_sheet_png_bytes) — byte-identical,
    # but the save round-trip no longer pays for a 2000px resvg raster.
    config["want_png"] = bool(req.want_png or req.want_pdf)
    # Image/PDF plans: on a live preview, point the SVG's <image> at /planimg
    # instead of inlining a multi-MB base64 raster on every keystroke. Saves
    # (self-contained artifacts) and downloads keep the inline embed. asset_base
    # is client-supplied and lands in an href="…" attribute of an inlined SVG, so
    # it must be charset-restricted to prevent breaking out of the attribute.
    if (image_bytes is not None and req.live_preview and not req.save
            and req.asset_base and _SAFE_BASE_RE.match(req.asset_base)):
        config["planimg_href"] = f"{req.asset_base.rstrip('/')}/planimg/{req.doc_id}"
    if req.keyplan and not req.plan_only:
        kp = dict(req.keyplan)
        plate = _plate_bytes(kp.get("plate_id"))
        # Optional user rotation of the key-plan image (0/90/180/270 CW). Rotate the
        # raster AND the highlight box together so the marked cell stays put.
        # This runs before the render try/except, so a junk value must not 500.
        try:
            rot = int(kp.get("rotate") or 0) % 360
        except (TypeError, ValueError):
            rot = 0
        if plate and rot:
            plate = rotate_plate(plate, rot)
            if kp.get("box"):
                kp["box"] = rotate_box(kp["box"], rot)
        kp["plate_bytes"] = plate
        config["keyplan"] = kp
    # Serve an identical prior preview from the memo; saves bypass it (side
    # effects + a unique paint_image would never hit anyway).
    cache_key = None if req.save else _render_cache_key(req.doc_id, config)
    if cache_key:
        with _RENDER_CACHE_LOCK:
            cached = _RENDER_CACHE.get(cache_key)
            if cached is not None:
                _RENDER_CACHE.move_to_end(cache_key)
    else:
        cached = None
    if cached is not None:
        svg, png, meta = cached
    else:
        try:
            svg, png, meta = (render(prims, config) if prims is not None
                              else render_image_plan(image_bytes, config))
        except (ParseError, ValueError, KeyError) as exc:
            # Bad input / config (unknown palette key, malformed geometry, …) —
            # meaningful to the user, so surface it as a 422 with the real message.
            raise HTTPException(status_code=422, detail=str(exc))
        except Exception:
            # Genuinely unexpected server fault: log the full traceback for ops and
            # return a generic message rather than leaking internals to the client.
            logger.exception("Unexpected error rendering doc_id=%s", req.doc_id)
            raise HTTPException(status_code=500, detail="Render failed — see server logs")
        if cache_key:
            with _RENDER_CACHE_LOCK:
                _RENDER_CACHE[cache_key] = (svg, png, meta)
                if len(_RENDER_CACHE) > _RENDER_CACHE_MAX:
                    _RENDER_CACHE.popitem(last=False)   # evict least-recently-used
    # Embed any uploaded brand fonts so they render in both the SVG and the PNG.
    svg, png = _apply_custom_fonts(svg, png, config.get("font_faces"), png_width=_png_width(meta))

    keyplan_svg = None
    if req.keyplan and not req.plan_only and req.keyplan.get("placement") == "standalone":
        try:
            keyplan_svg = render_keyplan_sheet(config)
            # Thread brand fonts into the standalone key plan too, or the saved
            # {sheet_id}-keyplan.svg renders in fallback fonts while the main
            # sheet uses the brand faces.
            keyplan_svg, _ = _apply_custom_fonts(keyplan_svg, None, config.get("font_faces"))
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
        # The PNG/thumbnail are derived, re-buildable artifacts. A plain save
        # defers them (png is None) — they're rasterized from this SVG on first
        # fetch. A save that also requested a PNG/PDF already has it, so persist
        # it (and its thumbnail) now to save the rebuild. On re-save, drop a stale
        # cached PNG/thumb so the next fetch rebuilds from the new SVG.
        png_path = os.path.join(out, f"{sheet_id}.png")
        thumb_path = os.path.join(out, f"{sheet_id}.thumb.png")
        if png is not None:
            storage.write_bytes(png_path, png)
            storage.write_bytes(thumb_path, _thumb_png(png))
        else:
            storage.remove(png_path)     # no-op if absent; prevents serving a stale raster
            storage.remove(thumb_path)
        kp_path = os.path.join(out, f"{sheet_id}-keyplan.svg")
        if keyplan_svg:
            storage.write_text(kp_path, keyplan_svg)
        else:
            storage.remove(kp_path)   # key plan dropped since last save (no-op if absent)
        # persist the editable config + geometry so the sheet can be re-opened
        kind = "dxf" if prims is not None else "image"
        _write_json(os.path.join(out, f"{sheet_id}.config.json"),
                    {"property_id": req.property_id, "metadata": req.metadata,
                     "rooms": req.rooms, "keyplan": req.keyplan,
                     "paint_image": req.paint_image, "kind": kind})
        prims_src = os.path.join(UP_DIR, f"{req.doc_id}.prims.json")
        if storage.exists(prims_src):
            storage.copy(prims_src, os.path.join(out, f"{sheet_id}.prims.json"))
        planimg_src = os.path.join(UP_DIR, f"{req.doc_id}.planimg.png")
        if storage.exists(planimg_src):
            storage.copy(planimg_src, os.path.join(out, f"{sheet_id}.planimg.png"))
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
        # Serialise the index read-modify-write against a concurrent save/rename/
        # delete on the same property, and re-read inside the lock so we extend the
        # live list rather than the stale snapshot read above for id determination.
        with _index_lock(req.property_id):
            sheets = _read_json(index, [])
            existing = next((s for s in sheets if s.get("sheet_id") == sheet_id), None)
            entry = {"sheet_id": sheet_id, "title": req.metadata.get("title", ""),
                     "suite": req.metadata.get("suite", ""),
                     "sf": req.metadata.get("sf", ""),
                     "keyplan": bool(keyplan_svg), "kind": kind,
                     "created": existing["created"] if existing else time.strftime("%Y-%m-%d %H:%M"),
                     "updated": time.strftime("%Y-%m-%d %H:%M:%S")}  # cache-busts the library thumbnail
            if existing:
                sheets[sheets.index(existing)] = entry   # keep its position in the library
            else:
                sheets.insert(0, entry)
            _write_json(index, sheets, indent=2, ensure_ascii=False)

    png_b64 = base64.b64encode(png).decode("ascii") if req.want_png else None
    pdf_b64 = (base64.b64encode(_png_to_pdf(png)).decode("ascii")
               if req.want_pdf and png else None)
    return {"svg": svg, "keyplan_svg": keyplan_svg, "sheet_id": sheet_id,
            "meta": meta, "png_b64": png_b64, "pdf_b64": pdf_b64}


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
    logo_in_header: bool = False   # also show the uploaded watermark image as the header mark
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
    # entries keep surfacing in GET /sheets. Hold the index lock so a concurrent
    # save can't re-create index.json after the rmtree (resurrecting the property
    # with a dangling entry).
    with _index_lock(prop_id):
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
    with _index_lock(prop_id):
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
    # Defense in depth: if this SVG were ever opened as a top-level document, a
    # restrictive CSP keeps any stray markup from executing script (the engine
    # already escapes text and validates colours/images/fonts before embedding).
    # Allows inline styles + data: images so the sheet itself still renders; when
    # embedded via <img> (the library), scripts can't run regardless.
    return Response(text, media_type="image/svg+xml", headers={
        "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; img-src data:; font-src data:",
        "X-Content-Type-Options": "nosniff", "Cache-Control": IMMUTABLE_CACHE})


@app.get("/sheets/{prop_id}/{sheet_id}.thumb.png")
def get_sheet_thumb(prop_id, sheet_id):
    """Small thumbnail for the library grid. MUST be declared before the .png
    route: `{sheet_id}.png` also matches `<id>.thumb.png` (capturing
    sheet_id="<id>.thumb", which then 400s on _safe_id's no-dot rule), and
    Starlette resolves routes in declaration order. Saves that produced a PNG
    pre-build the thumb; otherwise it's built lazily from the full PNG (itself
    rebuilt from the SVG on demand), cached back for next time."""
    _safe_id(prop_id, "property id")
    _safe_id(sheet_id, "sheet id")
    d = os.path.join(SHEET_DIR, prop_id)
    data = storage.read_bytes(os.path.join(d, f"{sheet_id}.thumb.png"))
    if data is None:
        full = _sheet_png_bytes(prop_id, sheet_id)   # rebuilds from SVG if the PNG was deferred
        if full is None:
            raise HTTPException(status_code=404, detail="Sheet not found.")
        data = _thumb_png(full)
        storage.write_bytes(os.path.join(d, f"{sheet_id}.thumb.png"), data)
    return Response(data, media_type="image/png",
                    headers={"Cache-Control": IMMUTABLE_CACHE})


@app.get("/sheets/{prop_id}/{sheet_id}.png")
def get_sheet_png(prop_id, sheet_id):
    _safe_id(prop_id, "property id")
    _safe_id(sheet_id, "sheet id")
    data = _sheet_png_bytes(prop_id, sheet_id)   # rebuilds from SVG if the PNG was deferred
    if data is None:
        raise HTTPException(status_code=404, detail="Sheet not found.")
    return Response(data, media_type="image/png",
                    headers={"Cache-Control": IMMUTABLE_CACHE})


@app.get("/sheets/{prop_id}/{sheet_id}.pdf")
def get_sheet_pdf(prop_id, sheet_id):
    """PDF export of a saved sheet: wrap its stored branded PNG (no separate .pdf
    is persisted — _png_to_pdf is cheap and this stays a raster PDF matching the
    PNG). Cache is immutable like the PNG; a re-save overwrites the same sheet_id,
    so consumers cache-bust with the sheet's `?v={updated}` query (see Library.jsx)."""
    _safe_id(prop_id, "property id")
    _safe_id(sheet_id, "sheet id")
    png = _sheet_png_bytes(prop_id, sheet_id)   # rebuilds from SVG if the PNG was deferred
    if png is None:
        raise HTTPException(status_code=404, detail="Sheet not found.")
    return Response(_png_to_pdf(png), media_type="application/pdf",
                    headers={"Cache-Control": IMMUTABLE_CACHE})


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
    its stored config + geometry/image. Returns {"svg": str, "png": bytes} or
    None when the sheet lacks the saved source needed to re-render (older
    saves, or one whose upload expired before it could be preserved)."""
    d = os.path.join(SHEET_DIR, prop_id)
    cfg = _read_json(os.path.join(d, f"{sheet_id}.config.json"))
    if not isinstance(cfg, dict):
        return None
    config = compose_config(load_property(prop_id), cfg.get("metadata"), cfg.get("rooms"))
    config["plan_only"] = True
    config["paint_image"] = cfg.get("paint_image")   # bake saved paint, as the in-editor plan-only download does
    raw = _read_json(os.path.join(d, f"{sheet_id}.prims.json"))
    if isinstance(raw, dict) and "prims" in raw:
        svg, png, meta = render(raw["prims"], config)
    else:
        image_bytes = storage.read_bytes(os.path.join(d, f"{sheet_id}.planimg.png"))
        if image_bytes is None:
            return None
        svg, png, meta = render_image_plan(image_bytes, config)
    svg, png = _apply_custom_fonts(svg, png, config.get("font_faces"), png_width=_png_width(meta))
    return {"svg": svg, "png": png}


@app.post("/sheets/download")
def download_sheets(req: DownloadRequest):
    """Bundle the chosen format(s) for the selected sheets into one ZIP. Keyplans
    excluded. With plan_only, re-renders each sheet as a bare plan instead of
    pulling the saved branded artifacts."""
    if not req.items:
        raise HTTPException(status_code=400, detail="No sheets selected.")
    exts = [e for e in ("svg", "png", "pdf") if e in req.formats]   # filter + normalize order
    if not exts:
        raise HTTPException(status_code=400, detail="Pick at least one format (PNG, SVG or PDF).")
    buf = io.BytesIO()
    used: Dict[str, int] = {}
    added = 0
    # mirror Library.jsx exportName(): "<prop-slug>-<title-slug>" (+ "-plan").
    # Strip anything but [A-Za-z0-9._-] so a crafted title (".."/"/"/"\") can't
    # produce a traversal-shaped ZIP entry name (zip-slip on a naive extractor).
    slug = lambda s: (re.sub(r"[^A-Za-z0-9._-]+", "-", (s or "").strip())
                      .strip("-.").lower() or "floorplan")
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
                    if ext == "svg":
                        data = rendered["svg"].encode("utf-8")
                    elif ext == "pdf":
                        data = _png_to_pdf(rendered["png"])
                    else:
                        data = rendered["png"]
                    zf.writestr(_zip_arcname(used, f"{name}.{ext}"), data)
                    added += 1
            else:
                for ext in exts:
                    if ext == "svg":
                        data = storage.read_bytes(os.path.join(d, f"{it.sheet_id}.svg"))
                    else:
                        # png/pdf both derive from the (possibly-deferred) sheet PNG,
                        # rebuilt from the SVG on demand. No .pdf is ever persisted.
                        data = _sheet_png_bytes(it.property_id, it.sheet_id)
                        if data is not None and ext == "pdf":
                            data = _png_to_pdf(data)
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
    planimg_path = os.path.join(d, f"{sheet_id}.planimg.png")
    has_prims = storage.exists(prims_path)
    has_planimg = storage.exists(planimg_path)
    if not storage.exists(cfg_path) or not (has_prims or has_planimg):
        raise HTTPException(
            status_code=404,
            detail="This sheet can't be re-opened — its source geometry wasn't "
                   "saved with it. Re-upload the file to edit.")
    cfg = _read_json(cfg_path)
    if not isinstance(cfg, dict):
        raise HTTPException(
            status_code=404,
            detail="This sheet can't be re-opened — its saved config is "
                   "unreadable. Re-upload the file to edit.")
    new_doc = uuid.uuid4().hex[:12]
    if has_prims:
        storage.copy(prims_path, os.path.join(UP_DIR, f"{new_doc}.prims.json"))
        cfg["kind"] = "dxf"
    else:
        storage.copy(planimg_path, os.path.join(UP_DIR, f"{new_doc}.planimg.png"))
        cfg["kind"] = "image"
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
    # Guard stale paint: a DXF sheet's paint is a full-page overlay for the page
    # it was saved against. If the engine's page has since changed (it was
    # resized), that paint would land wrong on the new page — so drop it and flag,
    # rather than silently misplacing it. The coordinator re-paints on the resized
    # page. (Image-plan sheets page off the image's own size, so they're unaffected.)
    cfg["paint_stale"] = False
    if cfg.get("paint_image") and has_prims:
        svg_txt = storage.read_text(os.path.join(d, f"{sheet_id}.svg")) or ""
        current_pages = (f'viewBox="0 0 {_PAGE_W} {_PAGE_H}"',      # portrait
                         f'viewBox="0 0 {_PAGE_H} {_PAGE_W}"')      # landscape
        if svg_txt and not any(v in svg_txt for v in current_pages):
            cfg["paint_image"] = None
            cfg["paint_stale"] = True
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
    with _index_lock(prop_id):
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
