"""
Key plans (spec §6): a schematic "where is my unit in the building" diagram.

Approach for the app: the user exports a finished key-plan image (the unit
already marked on it) and uploads it. We trim the surrounding whitespace on
intake and embed it as reference — always marked SCHEMATIC / NOT TO SCALE.
Two outputs:

  - footer mini-plate  -> keyplan_group(), embedded in the main sheet footer
  - standalone sheet   -> render_keyplan_sheet(), its own branded page

The image is the finished artifact, so we don't draw a unit box, trace a
footprint, or add a north arrow — we just crop and frame what the user gives us.
"""

import base64
import io
import math
import re
from PIL import Image, ImageChops

_PDF_PLAN_MAX_DIM = 2200   # longest edge, in px, for a print-quality plan rasterization

# Palette colours reach keyplan_group's stroke=/fill= attributes raw (via the
# render.py callers, which pass the whole palette dict). Gate them to a #hex
# colour here too — render.py has its own _safe_color, but re-importing it would
# be a circular import (render imports this module), and a fragment injected via
# dangerouslySetInnerHTML must never carry an unvalidated attribute value.
_HEX_COLOR_RE = re.compile(r"^#([0-9a-fA-F]{3,4}|[0-9a-fA-F]{6}|[0-9a-fA-F]{8})$")


def _safe_color(value, default):
    v = (value or "").strip()
    return v if _HEX_COLOR_RE.match(v) else default


class PdfPlanError(ValueError):
    """Raised when a PDF can't be turned into a floor-plan image."""


def pdf_to_png(raw_bytes, target_max_dim=_PDF_PLAN_MAX_DIM):
    """Rasterize a single-page PDF's page to a print-quality PNG.

    Print quality needs a much larger raster than brand.py's _pdf_first_page
    (which only samples colors from a thumbnail), so this uses its own zoom
    tuned to target_max_dim instead of sharing that function. Rejects
    multi-page PDFs rather than silently guessing page 1 — a submittal PDF
    with a cover page first would otherwise crop the wrong page with no error.
    """
    try:
        import fitz  # PyMuPDF
    except ImportError as exc:
        raise PdfPlanError(
            "PDF support needs PyMuPDF (pip install PyMuPDF).") from exc
    try:
        doc = fitz.open(stream=raw_bytes, filetype="pdf")
    except Exception as exc:
        raise PdfPlanError("Couldn't open that PDF.") from exc
    try:
        if doc.page_count < 1:
            raise PdfPlanError("That PDF has no pages.")
        if doc.page_count > 1:
            raise PdfPlanError(
                "That PDF has more than one page. Export or save just the "
                "floor-plan page, then re-upload.")
        page = doc.load_page(0)
        longest = max(page.rect.width, page.rect.height)
        if longest <= 0:
            raise PdfPlanError("That PDF page has no visible content.")
        zoom = target_max_dim / longest
        pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), alpha=False)
        buf = io.BytesIO()
        Image.frombytes("RGB", (pix.width, pix.height), pix.samples).save(buf, "PNG")
        return buf.getvalue()
    finally:
        doc.close()


def img_size(plate_bytes):
    try:
        im = Image.open(io.BytesIO(plate_bytes))
        return im.size
    except Exception:
        return (4, 3)


def autocrop(plate_bytes, tol=12):
    """Trim surrounding whitespace from an exported key-plan image so it sits
    tight in the frame, then re-encode as PNG.

    Handles both a transparent background (crop to the opaque region) and a
    (near-)white one (crop to the region that differs from white by more than
    `tol`, so faint anti-aliased margins go too). A few px of padding keeps the
    plan off the frame edge. Returns the original bytes unchanged if the image
    can't be opened or is effectively blank (nothing to crop)."""
    try:
        im = Image.open(io.BytesIO(plate_bytes)).convert("RGBA")
    except Exception:
        return plate_bytes
    alpha = im.getchannel("A")
    if alpha.getextrema()[0] < 255:
        bbox = alpha.getbbox()                       # real transparency -> opaque region
    else:
        rgb = im.convert("RGB")
        bg = Image.new("RGB", rgb.size, (255, 255, 255))
        diff = ImageChops.difference(rgb, bg).convert("L")
        bbox = diff.point(lambda p: 255 if p > tol else 0).getbbox()
    if not bbox:
        return plate_bytes                           # all blank -> leave as-is
    pad = max(6, round(0.015 * max(im.size)))
    l, t, r, b = bbox
    box = (max(0, l - pad), max(0, t - pad),
           min(im.width, r + pad), min(im.height, b + pad))
    buf = io.BytesIO()
    im.crop(box).save(buf, "PNG")
    return buf.getvalue()


def _data_uri(plate_bytes):
    head = plate_bytes[:4]
    mime = "image/png"
    if head[:2] == b"\xff\xd8":
        mime = "image/jpeg"
    elif head[:4] == b"RIFF":
        mime = "image/webp"
    b64 = base64.b64encode(plate_bytes).decode("ascii")
    return f"data:{mime};base64,{b64}"


def rotate_plate(plate_bytes, angle):
    """Rotate a key-plan raster by `angle` degrees CLOCKWISE (0/90/180/270).
    Lossless 90° transposes (no resampling); returns the input unchanged for angle
    0 or anything that isn't a right-angle multiple. Aspect swaps for 90/270, so
    callers that fit a box to img_size() get the rotated dimensions naturally."""
    try:
        a = int(angle or 0) % 360
        if a == 0 or a % 90 != 0:
            return plate_bytes
        im = Image.open(io.BytesIO(plate_bytes))
        # PIL's ROTATE_n is counter-clockwise; map clockwise degrees onto it.
        tmap = {90: Image.Transpose.ROTATE_270,
                180: Image.Transpose.ROTATE_180,
                270: Image.Transpose.ROTATE_90}
        im = im.transpose(tmap[a])
        buf = io.BytesIO()
        im.save(buf, "PNG")
        return buf.getvalue()
    except Exception:
        return plate_bytes


def rotate_box(box, angle):
    """Rotate a highlight box `[fx, fy, fw, fh]` (fractions of the image, top-left
    origin) to match a clockwise `rotate_plate(..., angle)` so the shaded unit cell
    still lands on the right spot after the raster is turned."""
    if not box or len(box) != 4:
        return box
    a = int(angle or 0) % 360
    try:
        fx, fy, fw, fh = (float(v) for v in box)
    except (TypeError, ValueError):
        return box
    if a == 90:
        return [1 - fy - fh, fx, fh, fw]
    if a == 180:
        return [1 - fx - fw, 1 - fy - fh, fw, fh]
    if a == 270:
        return [fy, 1 - fx - fw, fh, fw]
    return box


def keyplan_group(plate_bytes, ox, oy, w, h, palette, with_border=True, box=None,
                  img_href=None):
    """SVG fragment: the (pre-cropped) key-plan image framed in box (ox,oy,w,h)
    and embedded at full opacity.

    Two intake modes share this helper (spec §6):
      - "upload"    -> the image is the finished key plan (unit already marked);
                       `box` is None and we just frame and place it.
      - "highlight" -> the image is a plain floor-plate and the user drew a
                       rectangle over their unit in the app; `box` =
                       [fx, fy, fw, fh] as fractions of the image, drawn here as
                       a shaded accent cell.

    The caller fits (ox,oy,w,h) to the image's aspect ratio, so the embed
    preserves aspect (`xMidYMid meet`) with no letterbox — which is why the
    fractional `box` maps linearly onto (ox,oy,w,h). `box` is a TRAILING
    optional param so the finished-PDF/image floor-plan callers (render.py) that
    reuse this helper never get a unit box.

    `img_href`, when given, replaces the inlined base64 data URI with a plain URL
    (e.g. /planimg/{doc_id}) — used by live previews to avoid re-shipping a
    multi-MB raster on every render. Saved artifacts pass None so they stay
    self-contained.
    """
    dark = _safe_color(palette.get("dark"), "#2B1F14")
    accent = _safe_color(palette.get("accent"), "#C17F3A")
    parts = []
    if with_border:
        parts.append(f'<rect x="{ox:.1f}" y="{oy:.1f}" width="{w:.1f}" '
                     f'height="{h:.1f}" fill="#FFFFFF" stroke="{dark}" '
                     f'stroke-width="1.1"/>')
    parts.append(f'<image href="{img_href or _data_uri(plate_bytes)}" x="{ox:.1f}" '
                 f'y="{oy:.1f}" width="{w:.1f}" height="{h:.1f}" '
                 f'preserveAspectRatio="xMidYMid meet"/>')
    if box and len(box) == 4:
        try:
            fx, fy, fw, fh = (float(v) for v in box)
        except (TypeError, ValueError):
            fx = fy = fw = fh = 0.0                   # malformed -> skip, don't throw
        # Reject non-finite (inf would emit width="inf", invalid SVG; nan is
        # already falsy against > 0).
        if all(math.isfinite(v) for v in (fx, fy, fw, fh)) and fw > 0 and fh > 0:
            parts.append(
                f'<rect x="{ox + fx * w:.1f}" y="{oy + fy * h:.1f}" '
                f'width="{fw * w:.1f}" height="{fh * h:.1f}" '
                f'fill="{accent}" fill-opacity="0.55" '
                f'stroke="{accent}" stroke-width="1.5"/>')
    return "\n".join(parts)
