"""
Key plans (spec §6): a schematic "where is my unit in the building" diagram.

Approach for the app: the user uploads a floor-plate screenshot (captured per
FLOORPLAN_WORKFLOW Part 2) and drags a box over their unit. We embed that
screenshot, lightened, with the unit cell shaded in the brand accent, a north
arrow, and a floor label — always marked SCHEMATIC / NOT TO SCALE. Two outputs:

  - footer mini-plate  -> keyplan_group(), embedded in the main sheet footer
  - standalone sheet   -> render_keyplan_sheet(), its own branded page

This is intentionally schematic and approximate (the box is hand-placed); that
is the right fidelity for a building-locator thumbnail.
"""

import base64
import html
import io
from PIL import Image

PAGE_W, PAGE_H = 1000, 1080
HEADER_H = 92
FOOTER_H = 140
PLAN_MAX_W, PLAN_MAX_H = 800, 640

DEFAULT_SERIF = "Georgia, 'Times New Roman', serif"
DEFAULT_SANS = "'Helvetica Neue', Helvetica, Arial, sans-serif"


def img_size(plate_bytes):
    try:
        im = Image.open(io.BytesIO(plate_bytes))
        return im.size
    except Exception:
        return (4, 3)


def _data_uri(plate_bytes):
    head = plate_bytes[:4]
    mime = "image/png"
    if head[:2] == b"\xff\xd8":
        mime = "image/jpeg"
    elif head[:4] == b"RIFF":
        mime = "image/webp"
    b64 = base64.b64encode(plate_bytes).decode("ascii")
    return f"data:{mime};base64,{b64}"


def _north_arrow(cx, cy, r, deg, dark, accent):
    """A small north arrow rotated by `deg` (clockwise from up)."""
    return (
        f'<g transform="translate({cx:.1f},{cy:.1f}) rotate({deg})">'
        f'<circle r="{r:.1f}" fill="#FFFFFF" stroke="{dark}" '
        f'stroke-width="1" stroke-opacity="0.6"/>'
        f'<polygon points="0,{-r*0.78:.1f} {r*0.34:.1f},{r*0.34:.1f} '
        f'0,{r*0.12:.1f} {-r*0.34:.1f},{r*0.34:.1f}" fill="{accent}"/>'
        f'<text x="0" y="{-r*0.92:.1f}" text-anchor="middle" '
        f'font-family="{DEFAULT_SANS}" font-size="{r*0.72:.1f}" '
        f'font-weight="bold" fill="{dark}">N</text></g>'
    )


def keyplan_group(plate_bytes, box, ox, oy, w, h, palette,
                  north_deg=0, with_north=True, with_border=True,
                  silhouette=None):
    """
    SVG fragment: a plate diagram in box (ox,oy,w,h) with the unit cell shaded
    in accent. `box` = [fx, fy, fw, fh] as fractions of the image (None -> no
    shaded cell yet).

    Two looks share the same frame (so box fractions map identically):
      - silhouette given -> the auto-traced footprint (already brand-coloured,
        transparent background) drawn opaque: the clean "basic key plan" look.
      - silhouette None   -> the raw screenshot embedded lightened to 50%.
    """
    dark = palette.get("dark", "#2B1F14")
    accent = palette.get("accent", "#C17F3A")
    parts = []
    if with_border:
        parts.append(f'<rect x="{ox:.1f}" y="{oy:.1f}" width="{w:.1f}" '
                     f'height="{h:.1f}" fill="#FFFFFF" stroke="{dark}" '
                     f'stroke-width="1.1"/>')
    if silhouette is not None:
        parts.append(f'<image href="{_data_uri(silhouette)}" x="{ox:.1f}" '
                     f'y="{oy:.1f}" width="{w:.1f}" height="{h:.1f}" '
                     f'preserveAspectRatio="none"/>')
    else:
        parts.append(f'<image href="{_data_uri(plate_bytes)}" x="{ox:.1f}" '
                     f'y="{oy:.1f}" width="{w:.1f}" height="{h:.1f}" '
                     f'opacity="0.5" preserveAspectRatio="none"/>')
    if box and len(box) == 4:
        fx, fy, fw, fh = box
        rx = ox + fx * w
        ry = oy + fy * h
        rw = fw * w
        rh = fh * h
        parts.append(f'<rect x="{rx:.1f}" y="{ry:.1f}" width="{rw:.1f}" '
                     f'height="{rh:.1f}" fill="{accent}" fill-opacity="0.55" '
                     f'stroke="{accent}" stroke-width="1.5"/>')
    if with_north:
        nr = min(w, h) * 0.09
        parts.append(_north_arrow(ox + w - nr - 6, oy + nr + 6, nr,
                                  north_deg, dark, accent))
    return "\n".join(parts)


def render_keyplan_sheet(config):
    """Standalone branded key-plan page."""
    palette = config.get("palette") or {}
    dark = palette.get("dark", "#2B1F14")
    accent = palette.get("accent", "#C17F3A")
    mid = palette.get("mid", "#E8D9C0")
    light = palette.get("light", "#F7F3ED")
    fonts = config.get("fonts") or {}
    serif = fonts.get("serif", DEFAULT_SERIF)
    sans = fonts.get("sans", DEFAULT_SANS)

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
    group = keyplan_group(plate, kp.get("box"), ox, oy, pw, ph, palette,
                          north_deg=kp.get("north_deg", 0),
                          silhouette=kp.get("silhouette_bytes"))

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
