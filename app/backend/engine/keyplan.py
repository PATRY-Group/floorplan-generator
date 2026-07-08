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
import html
import io
from PIL import Image, ImageChops

PAGE_W, PAGE_H = 1000, 1080
HEADER_H = 92
FOOTER_H = 140
PLAN_MAX_W, PLAN_MAX_H = 800, 640

DEFAULT_SERIF = "Georgia, 'Times New Roman', serif"
DEFAULT_SANS = "'Helvetica Neue', Helvetica, Arial, sans-serif"

_PDF_PLAN_MAX_DIM = 2200   # longest edge, in px, for a print-quality plan rasterization


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


def _font_stack(value, generic):
    """Quote a bare brand family name into a valid CSS stack (see the twin in
    render.py for the full rationale): unquoted, a name like "Inter 18pt" is
    invalid CSS and browsers fall back to serif, while the PNG renders fine.
    Values already containing a comma (a stack) pass through untouched."""
    v = (value or "").strip()
    if not v or "," in v:
        return value or ""
    fam = v.replace("\\", "").replace("'", "")
    return f"'{fam}', {generic}"


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


def keyplan_group(plate_bytes, ox, oy, w, h, palette, with_border=True):
    """SVG fragment: the (pre-cropped) key-plan image framed in box (ox,oy,w,h)
    and embedded at full opacity. The image is the finished key plan the user
    exported — the unit is already marked on it — so we just frame and place it.

    The caller fits (ox,oy,w,h) to the image's aspect ratio, so the embed
    preserves aspect (`xMidYMid meet`) and the optional border hugs the image.
    """
    dark = palette.get("dark", "#2B1F14")
    parts = []
    if with_border:
        parts.append(f'<rect x="{ox:.1f}" y="{oy:.1f}" width="{w:.1f}" '
                     f'height="{h:.1f}" fill="#FFFFFF" stroke="{dark}" '
                     f'stroke-width="1.1"/>')
    parts.append(f'<image href="{_data_uri(plate_bytes)}" x="{ox:.1f}" '
                 f'y="{oy:.1f}" width="{w:.1f}" height="{h:.1f}" '
                 f'preserveAspectRatio="xMidYMid meet"/>')
    return "\n".join(parts)


def render_keyplan_sheet(config):
    """Standalone branded key-plan page."""
    palette = config.get("palette") or {}
    dark = palette.get("dark", "#2B1F14")
    accent = palette.get("accent", "#C17F3A")
    mid = palette.get("mid", "#E8D9C0")
    light = palette.get("light", "#F7F3ED")
    fonts = config.get("fonts") or {}
    serif = _font_stack(fonts.get("serif", DEFAULT_SERIF), "serif")
    sans = _font_stack(fonts.get("sans", DEFAULT_SANS), "sans-serif")

    meta = config.get("metadata") or {}
    kp = config.get("keyplan") or {}
    plate = kp.get("plate_bytes")
    if not plate:
        raise ValueError("No plate image provided for the key plan.")

    esc = html.escape
    prop_name = esc((meta.get("property_name") or "").upper())
    location = esc((meta.get("location") or "").upper())
    lockup = esc(meta.get("lockup") or "")
    title = esc((meta.get("title") or "").upper())
    floor_label = esc((kp.get("floor_label") or "").upper())
    footer_addr = esc((meta.get("footer_address") or "").upper())

    iw, ih = img_size(plate)
    s = min(PLAN_MAX_W / max(iw, 1), PLAN_MAX_H / max(ih, 1))
    pw, ph = iw * s, ih * s
    ox = (PAGE_W - pw) / 2
    oy = HEADER_H + (PAGE_H - HEADER_H - FOOTER_H - ph) / 2
    group = keyplan_group(plate, ox, oy, pw, ph, palette)

    return f'''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {PAGE_W} {PAGE_H}" font-family="{sans}">
  <rect width="{PAGE_W}" height="{PAGE_H}" fill="{light}"/>
  <rect width="{PAGE_W}" height="{HEADER_H}" fill="{dark}"/>
  <text x="60" y="62" font-family="{serif}" font-weight="bold" font-size="44" fill="{accent}">{lockup}</text>
  <text x="192" y="50" font-size="21" letter-spacing="7" fill="#FFFFFF">{prop_name}</text>
  <text x="192" y="71" font-size="11" letter-spacing="4" fill="{mid}" fill-opacity="0.85">{location}</text>
  <text x="{PAGE_W-60}" y="56" text-anchor="end" font-size="11" letter-spacing="3.5" fill="{mid}" fill-opacity="0.7">KEY PLAN</text>

  {group}
  <text x="{PAGE_W/2:.0f}" y="{oy - 14:.0f}" text-anchor="middle" font-size="11" letter-spacing="3" fill="{dark}" fill-opacity="0.6">{floor_label}</text>
  <text x="{PAGE_W/2:.0f}" y="{oy + ph + 26:.0f}" text-anchor="middle" font-size="10" letter-spacing="2.5" fill="{dark}" fill-opacity="0.5">SCHEMATIC KEY PLAN — NOT TO SCALE</text>

  <rect y="{PAGE_H-FOOTER_H}" width="{PAGE_W}" height="{FOOTER_H}" fill="{dark}"/>
  <text x="60" y="{PAGE_H-FOOTER_H+62}" font-family="{serif}" font-size="40" fill="#FFFFFF">{title}</text>
  <line x1="62" y1="{PAGE_H-FOOTER_H+80}" x2="122" y2="{PAGE_H-FOOTER_H+80}" stroke="{accent}" stroke-width="2.5"/>
  <text x="60" y="{PAGE_H-FOOTER_H+106}" font-size="12.5" letter-spacing="3" fill="{mid}">{floor_label}</text>
  <text x="{PAGE_W-60}" y="{PAGE_H-FOOTER_H+62}" text-anchor="end" font-size="12" letter-spacing="2.5" fill="{accent}">{footer_addr}</text>
</svg>'''
