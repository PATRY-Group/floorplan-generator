"""
Brand extraction — pull a color palette (and, for PDFs, embedded font names)
from an uploaded brand file so the property-setup form (APP_BUILD_SPEC §7) can
be auto-filled and *confirmed*, instead of typing hex by hand.

The palette is the reliable part:
  dark   = darkest dominant color    (bands / walls / text)
  light  = lightest dominant color   (page background + label halos)
  accent = most colorful mid-tone    (lockup / watermark / underlines)
  mid    = a genuine guess           (text on dark bands)
`dark`/`light` are dependable. `accent` works well on a real brand guide where
the brand color is a sizeable swatch; on a *finished* marketing sheet the accent
is a hair of the pixels and gets quantized away, so it degrades to the most
colorful tone present. `mid` rarely matches a brand's secondary tint. Because of
all that we also return *every* dominant swatch and let the user re-pick in the UI.

Fonts (PDF only) are returned as raw embedded names for the user to copy — never
auto-wired into the property's serif/sans stacks. The names aren't CSS font
stacks, and they won't be installed server-side, so the PNG render would silently
fall back anyway (see CLAUDE.md). Surfacing them as hints is the honest contract.
"""

import io
import re

from PIL import Image

# Tunables for color extraction.
_MAX_DIM = 220          # downsample longest edge to this before quantizing
_QUANT_COLORS = 16      # palette size for median-cut quantization
_MIN_FRAC = 0.012       # drop bins below this share (anti-alias / JPEG edge noise)
_PDF_RENDER_DPI = 150   # rasterize the first PDF page at this DPI
_PDF_MAX_DIM = 1600     # but clamp so a huge page doesn't blow up memory


class BrandError(ValueError):
    """Raised when a brand file can't be read (bad/empty/unsupported)."""


def extract_brand(raw: bytes, filename: str = "") -> dict:
    """Extract a palette + font hints from brand-file bytes.

    Returns: {
      "source":   "pdf" | "image",
      "palette":  {"dark","accent","mid","light"} hex strings,
      "swatches": [{"hex","frac","luminance","saturation"}, ...] frac-desc,
      "fonts":    [str, ...] embedded font family names (PDF only; else []),
    }
    """
    if not raw:
        raise BrandError("Empty file.")
    name = (filename or "").lower()
    is_pdf = name.endswith(".pdf") or raw[:5] == b"%PDF-"

    fonts: list[str] = []
    if is_pdf:
        img, fonts = _pdf_first_page(raw)
        source = "pdf"
    else:
        try:
            img = Image.open(io.BytesIO(raw))
            img.load()
        except Exception as exc:
            raise BrandError(
                "Couldn't read that image. Upload a PNG, JPG, or PDF of the brand "
                "sheet.") from exc
        source = "image"

    swatches = _dominant_colors(img)
    if not swatches:
        raise BrandError("No usable colors found in that file.")
    palette = _assign_roles(swatches)
    return {"source": source, "palette": palette,
            "swatches": swatches, "fonts": fonts}


# --------------------------------------------------------------------------- #
# PDF: rasterize page 1 for color, enumerate embedded fonts
# --------------------------------------------------------------------------- #
def _pdf_first_page(raw: bytes):
    try:
        import fitz  # PyMuPDF
    except ImportError as exc:
        raise BrandError(
            "PDF support needs PyMuPDF (pip install PyMuPDF). Upload a PNG/JPG "
            "of the brand sheet instead.") from exc
    try:
        doc = fitz.open(stream=raw, filetype="pdf")
    except Exception as exc:
        raise BrandError("Couldn't open that PDF.") from exc
    try:
        if doc.page_count < 1:
            raise BrandError("That PDF has no pages.")
        page = doc.load_page(0)
        zoom = _PDF_RENDER_DPI / 72.0
        longest = max(page.rect.width, page.rect.height) * zoom
        if longest > _PDF_MAX_DIM:        # clamp absurdly large pages
            zoom *= _PDF_MAX_DIM / longest
        pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), alpha=False)
        img = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
        fonts = _pdf_fonts(doc)
        return img, fonts
    finally:
        doc.close()


def _pdf_fonts(doc) -> list[str]:
    """Collect distinct embedded font family names across all pages."""
    seen, out = set(), []
    for pno in range(doc.page_count):
        for f in doc.get_page_fonts(pno, full=False):
            # tuple: (xref, ext, type, basefont, name, encoding, ...)
            base = f[3] if len(f) > 3 else ""
            fam = _clean_font_name(base)
            if fam and fam.lower() not in seen:
                seen.add(fam.lower())
                out.append(fam)
    return out


def _clean_font_name(base: str) -> str:
    """'ABCDEF+HelveticaNeue-Bold' -> 'HelveticaNeue-Bold'."""
    if not base:
        return ""
    # strip the 6-char subset prefix PDFs prepend to embedded subsets
    base = re.sub(r"^[A-Z]{6}\+", "", base)
    return base.strip()


# --------------------------------------------------------------------------- #
# color extraction
# --------------------------------------------------------------------------- #
def _dominant_colors(img: Image.Image) -> list[dict]:
    """Median-cut quantize to a handful of dominant colors with frequencies."""
    img = img.convert("RGB")
    img.thumbnail((_MAX_DIM, _MAX_DIM))
    quant = img.quantize(colors=_QUANT_COLORS, method=Image.Quantize.MEDIANCUT)
    palette = quant.getpalette()  # flat [r,g,b, r,g,b, ...]
    counts = quant.getcolors() or []  # [(count, index), ...]
    total = sum(c for c, _ in counts) or 1

    bins = []
    for count, idx in counts:
        frac = count / total
        if frac < _MIN_FRAC:
            continue
        r, g, b = palette[idx * 3: idx * 3 + 3]
        bins.append({
            "hex": "#{:02X}{:02X}{:02X}".format(r, g, b),
            "frac": round(frac, 4),
            "luminance": round(_luminance(r, g, b), 4),
            "chroma": round((max(r, g, b) - min(r, g, b)) / 255.0, 4),
        })
    bins.sort(key=lambda d: d["frac"], reverse=True)
    return bins


def _luminance(r, g, b) -> float:
    """Perceived relative luminance, 0..1."""
    return (0.2126 * r + 0.7152 * g + 0.0722 * b) / 255.0


def _assign_roles(swatches: list[dict]) -> dict:
    """Map dominant colors to dark/accent/mid/light by luminance + chroma."""
    by_lum = sorted(swatches, key=lambda d: d["luminance"])
    dark = by_lum[0]["hex"]
    light = by_lum[-1]["hex"]

    # accent: the most colorful color, gated to a usable luminance band so a
    # near-black brown (high HSV "saturation", but visually just dark) can't
    # masquerade as an accent. Relax the gate, then drop it, if nothing fits.
    not_extreme = [s for s in swatches if s["hex"] not in (dark, light)]
    gated = [s for s in not_extreme if 0.18 <= s["luminance"] <= 0.9]
    accent_pool = gated or not_extreme or swatches
    accent = max(accent_pool, key=lambda d: d["chroma"])["hex"]

    # mid: best guess — the most frequent remaining color, else reuse light
    used = {dark, light, accent}
    rest = [s for s in swatches if s["hex"] not in used]
    mid = rest[0]["hex"] if rest else light

    return {"dark": dark, "accent": accent, "mid": mid, "light": light}
