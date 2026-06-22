"""
Rendering engine for the Floor Plan Sheet Generator.

Refactor of the original `build_floorplan_sheets.py`: identical layer treatment,
halo logic, placement search and page layout, driven by a `config` object.

    render(prims, config) -> (svg_str, png_bytes, meta)

`config` may include a "keyplan" with placement "footer" to embed a mini-plate
in the footer (the standalone key-plan page is produced separately by
engine.keyplan.render_keyplan_sheet).
"""

import base64
import hashlib
import html
import io
import math
import os


def _register_cairo_dll_dir():
    """On Windows, cairosvg's native libcairo-2.dll ships with the GTK runtime
    but isn't always on PATH for a freshly-launched process. cairocffi resolves
    the library via ctypes.util.find_library (which searches PATH) and also
    honours CAIROCFFI_DLL_DIRECTORIES, so register the GTK bin dir on both before
    the cairosvg import below — making it work regardless of the launching shell."""
    if os.name != "nt":
        return
    candidates = [
        r"C:\Program Files\GTK3-Runtime Win64\bin",
        r"C:\Program Files (x86)\GTK3-Runtime Win64\bin",
        os.path.join(os.environ.get("LOCALAPPDATA", ""),
                     r"Programs\GTK3-Runtime Win64\bin"),
    ]
    for d in candidates:
        if d and os.path.isdir(d):
            os.environ["PATH"] = d + os.pathsep + os.environ.get("PATH", "")
            existing = os.environ.get("CAIROCFFI_DLL_DIRECTORIES", "")
            os.environ["CAIROCFFI_DLL_DIRECTORIES"] = (
                d + (os.pathsep + existing if existing else ""))
            break


_register_cairo_dll_dir()

import cairosvg
import numpy as np
from PIL import Image, ImageDraw

from .keyplan import keyplan_group, img_size
from .keyplan_trace import solidify_walls, _hex

PAGE_W, PAGE_H = 1000, 1080
HEADER_H = 92
FOOTER_H = 140
PLAN_MAX_W, PLAN_MAX_H = 800, 640
SKINNY_WALL_W = 0.8   # wall-outline stroke for the "skinny" (no-fill) wall style

DEFAULT_SERIF = "Georgia, 'Times New Roman', serif"
DEFAULT_SANS = "'Helvetica Neue', Helvetica, Arial, sans-serif"

DEFAULT_PALETTE = {
    "dark": "#2B1F14",
    "accent": "#C17F3A",
    "mid": "#E8D9C0",
    "light": "#F7F3ED",
}


def _pts_attr(pts):
    return " ".join(f"{x:.1f},{y:.1f}" for x, y in pts)


def _poly_path(polys):
    return " ".join("M " + " L ".join(f"{x:.1f} {y:.1f}" for x, y in p) + " Z"
                    for p in polys)


def _text_w(s, size, ls):
    if not s:
        return 0
    return 0.70 * size * len(s) + ls * (len(s) - 1) + 6


def _glyph_width(font_data_uri, s, size, ls):
    """Exact pixel width of `s` from an embedded font's own advance widths, so
    header spacing adapts to whatever font is in use instead of a fixed guess."""
    import base64
    import io as _io
    from fontTools.ttLib import TTFont
    raw = base64.b64decode(font_data_uri.split(",", 1)[1])
    f = TTFont(_io.BytesIO(raw), fontNumber=0)
    upm = f["head"].unitsPerEm or 1000
    cmap = f.getBestCmap()
    hmtx = f["hmtx"]
    total = 0.0
    for ch in s:
        gn = cmap.get(ord(ch))
        total += (hmtx[gn][0] if gn and gn in hmtx.metrics else upm * 0.6)
    return total / upm * size + ls * max(len(s) - 1, 0) + 6


# Synthesized wall poché is colour-independent, so the (slow) morphology is
# cached by geometry+kernel and only the cheap colorize runs per render. The live
# preview re-renders on every palette/label edit but the wall geometry is stable.
_POCHE_CACHE = {}


def _poche_close_k(plan_w, plan_h, override=None):
    """Close-kernel width in px: wide enough to bridge a wall's two faces, far
    narrower than a room. Scales with the plan span so it holds across sheet
    sizes; ~7-9 px for a typical unit. `override` (config["poche_close_px"]) is
    the escape hatch for an unusual wall thickness."""
    if override:
        return max(3, int(override))
    return max(7, math.ceil(0.006 * max(plan_w, plan_h)))


def _wall_band_mask(wall_lines, cavity_lines, close_k):
    """Rasterize wall linework (already in page coords) at page resolution and
    solidify the double-line faces into one filled band. Returns a boolean
    (PAGE_H, PAGE_W) mask, cached by geometry+kernel."""
    h = hashlib.md5(str(close_k).encode())
    for tag, lines in ((b"W", wall_lines), (b"C", cavity_lines)):
        for line in lines:
            h.update(tag)
            h.update(np.asarray(line, dtype=np.float32).tobytes())
    key = h.digest()
    band = _POCHE_CACHE.get(key)
    if band is not None:
        return band
    occ = Image.new("1", (PAGE_W, PAGE_H), 0)
    dr = ImageDraw.Draw(occ)
    for line in (*wall_lines, *cavity_lines):
        if len(line) >= 2:
            dr.line([(float(x), float(y)) for x, y in line], fill=1, width=3)
    band = solidify_walls(np.asarray(occ, dtype=bool), close_k)
    if len(_POCHE_CACHE) > 32:
        _POCHE_CACHE.clear()
    _POCHE_CACHE[key] = band
    return band


def _wall_poche_image(wall_lines, cavity_lines, plan_w, plan_h, wall_hex,
                      close_override=None):
    """Solid-wall poché synthesized from wall linework, as a full-page (PAGE_W x
    PAGE_H) base64 PNG <image> tag positioned in page coords — transparent except
    the wall band, so it overlays cleanly under the crisp vector wall strokes in
    either export path. Returns None if there is no wall geometry."""
    if not (wall_lines or cavity_lines):
        return None
    band = _wall_band_mask(wall_lines, cavity_lines,
                           _poche_close_k(plan_w, plan_h, close_override))
    if not band.any():
        return None
    rgba = np.zeros((PAGE_H, PAGE_W, 4), np.uint8)
    rgba[band] = (*_hex(wall_hex), 255)
    buf = io.BytesIO()
    Image.fromarray(rgba, "RGBA").save(buf, "PNG")
    uri = "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode("ascii")
    return (f'<image href="{uri}" x="0" y="0" width="{PAGE_W}" height="{PAGE_H}" '
            f'preserveAspectRatio="none"/>')


def _role_lookup(layer_map):
    out = {}
    for role, layers in layer_map.items():
        for ly in layers:
            out[ly] = role
    return out


def render(prims, config):
    palette = {**DEFAULT_PALETTE, **(config.get("palette") or {})}
    DARK, ACCENT, MID, LIGHT = (palette["dark"], palette["accent"],
                                palette["mid"], palette["light"])
    # The drawn floor plan (walls, poché, doors, glazing) renders in its own
    # ink — black by default — independent of the brand "dark" colour, which
    # styles the header/footer bands, watermark and labels. Overridable via
    # palette["wall"] for a property on a different convention.
    WALL = palette.get("wall") or "#000000"
    fonts = config.get("fonts") or {}
    SERIF = fonts.get("serif", DEFAULT_SERIF)
    SANS = fonts.get("sans", DEFAULT_SANS)
    layer_map = config.get("layer_map") or {}
    role_of = _role_lookup(layer_map)
    drop = set(layer_map.get("drop", []))
    floor_hatch = set(layer_map.get("floor_hatch", []))
    wall_fill_layers = set(layer_map.get("wall_fill", []))
    md = config.get("metadata") or {}          # unit metadata (title/suite/…)
    rooms = config.get("rooms") or []

    wall_roles = {"wall_line", "wall_fill"}
    kept, xs, ys = [], [], []
    for layer, kind, data, block in prims:
        if layer in drop:
            continue
        if layer in floor_hatch and kind == "hatch":
            continue
        kept.append((layer, kind, data, block))

    wxs, wys = [], []
    for layer, kind, data, block in kept:
        role = role_of.get(layer)
        target_x, target_y = (wxs, wys) if role in wall_roles else (xs, ys)
        if kind == "line":
            target_x += [p[0] for p in data]
            target_y += [p[1] for p in data]
        else:
            for poly in data:
                target_x += [p[0] for p in poly]
                target_y += [p[1] for p in poly]

    # scale/center from wall geometry when present, else from all geometry
    ext_x = wxs or xs
    ext_y = wys or ys
    minx, maxx = min(ext_x), max(ext_x)
    miny, maxy = min(ext_y), max(ext_y)
    w_in, h_in = max(maxx - minx, 1e-6), max(maxy - miny, 1e-6)
    s = min(PLAN_MAX_W / w_in, PLAN_MAX_H / h_in)
    plan_w, plan_h = w_in * s, h_in * s
    plan_top = HEADER_H + (PAGE_H - HEADER_H - FOOTER_H - plan_h) / 2
    tx = (PAGE_W - plan_w) / 2 - minx * s
    ty = plan_top + maxy * s

    def X(x):
        return tx + x * s

    def Y(y):
        return ty - y * s

    wall_fills, wall_lines, glaz_lines, door_lines, swing_lines = [], [], [], [], []
    thin_lines, dash_lines, cavity_lines = [], [], []
    occ_img = Image.new("1", (PAGE_W, PAGE_H), 0)
    dr = ImageDraw.Draw(occ_img)
    for layer, kind, data, block in kept:
        role = role_of.get(layer)
        if kind == "hatch":
            polys = [[(X(x), Y(y)) for x, y in p] for p in data]
            if layer in wall_fill_layers or role == "wall_fill":
                wall_fills.append(_poly_path(polys))
                for p in polys:
                    dr.polygon(p, fill=1, outline=1)
            else:
                thin_lines += polys
                for p in polys:
                    if len(p) >= 2:
                        dr.line(p + [p[0]], fill=1, width=3)
            continue
        pts = [(X(x), Y(y)) for x, y in data]
        if role == "wall_line":
            wall_lines.append(pts)
        elif role == "wall_fill":
            # A wall_fill layer carrying lines (not a hatch) — the inner cavity
            # faces of a plain-AutoCAD wall. Kept apart so they can feed poché
            # synthesis below instead of disappearing into faint thin_lines.
            cavity_lines.append(pts)
        elif role == "glazing":
            glaz_lines.append(pts)
        elif role == "door":
            (swing_lines if len(pts) > 6 else door_lines).append(pts)
        elif role == "dashed":
            dash_lines.append(pts)
        elif role in ("room_label",):
            continue
        else:
            thin_lines.append(pts)
        if len(pts) >= 2:
            dr.line(pts, fill=1, width=3)

    occ = np.asarray(occ_img, dtype=np.uint8)
    I = occ.astype(np.int64).cumsum(0).cumsum(1)

    def box_sum(x1, y1, x2, y2):
        x1, y1 = max(int(x1), 1), max(int(y1), 1)
        x2, y2 = min(int(x2), PAGE_W - 1), min(int(y2), PAGE_H - 1)
        if x2 <= x1 or y2 <= y1:
            return 1e9
        return int(I[y2, x2] - I[y1 - 1, x2] - I[y2, x1 - 1] + I[y1 - 1, x1 - 1])

    def place(rect, bw, bh):
        x1, x2 = sorted((X(rect[0]), X(rect[1])))
        y1, y2 = sorted((Y(rect[2]), Y(rect[3])))
        cx, cy = (x1 + x2) / 2, (y1 + y2) / 2
        best, best_key = (cx, cy), None
        pad = 3
        lo_x, hi_x = x1 + bw / 2, x2 - bw / 2 + 1
        lo_y, hi_y = y1 + bh / 2, y2 - bh / 2 + 1
        if hi_x <= lo_x:
            lo_x = hi_x = cx
        if hi_y <= lo_y:
            lo_y = hi_y = cy
        for px in np.arange(lo_x, hi_x, 2):
            for py in np.arange(lo_y, hi_y, 2):
                ov = box_sum(px - bw / 2 - pad, py - bh / 2 - pad,
                             px + bw / 2 + pad, py + bh / 2 + pad)
                d = ((px - cx) ** 2 + (py - cy) ** 2) ** 0.5
                key = (ov > 0, ov, d)
                if best_key is None or key < best_key:
                    best_key, best = key, (px, py)
        return best

    esc = html.escape

    def halo_text(x, y, size, ls, opac, content):
        common = (f'x="{x:.0f}" y="{y:.1f}" text-anchor="middle" '
                  f'font-family="{SANS}" font-size="{size:.1f}" '
                  f'letter-spacing="{ls:.1f}"')
        return (f'<text {common} fill="none" stroke="{LIGHT}" '
                f'stroke-width="8" stroke-linejoin="round">{content}</text>\n'
                f'<text {common} fill="{DARK}" '
                f'fill-opacity="{opac}">{content}</text>')

    room_labels, placements = [], []
    for idx, room in enumerate(rooms):
        name = (room.get("name") or "").upper()
        dims = room.get("dims") if room.get("show_dims", True) else None
        k = float(room.get("font_scale", 1.0))
        n_size, n_ls = 11.5 * k, 2.2 * k
        d_size, d_ls = 10 * k, 1.2 * k
        bw = max(_text_w(name, n_size, n_ls),
                 _text_w(dims, d_size, d_ls) if dims else 0)
        bh = (n_size + 4 + d_size) if dims else n_size
        if room.get("x") is not None and room.get("y") is not None:
            px, py = X(float(room["x"])), Y(float(room["y"]))
        else:
            rect = room.get("rect") or [minx, maxx, miny, maxy]
            px, py = place(rect, bw, bh)
        placements.append({"i": idx, "name": name,
                           "px": round(float(px), 1), "py": round(float(py), 1),
                           "bw": round(float(bw), 1), "bh": round(float(bh), 1),
                           "overridden": room.get("x") is not None
                           and room.get("y") is not None})
        if dims:
            room_labels.append(halo_text(px, py - bh / 2 + n_size - 1,
                                         n_size, n_ls, 0.78, esc(name)))
            room_labels.append(halo_text(px, py + bh / 2 - 1,
                                         d_size, d_ls, 0.5, esc(dims)))
        else:
            room_labels.append(halo_text(px, py + n_size * 0.36,
                                         n_size, n_ls, 0.78, esc(name)))

    # Wall style is a per-sheet choice carried in metadata. Default "skinny":
    # both wall faces as thin uniform outlines with no fill (the 539 sheet's
    # original look). Opt into "solid" for the poché fill behaviour below.
    wall_style = (config.get("metadata") or {}).get("wall_style") or "skinny"
    wall_stroke = 1.6
    poche_img = None
    if wall_style == "skinny":
        # Both faces (outer wall_lines + inner cavity_lines) as thin strokes; no
        # fill from any source (drop a hatch poché too, so Revit files go skinny).
        wall_fills = []
        wall_lines = wall_lines + cavity_lines
        wall_stroke = SKINNY_WALL_W
    else:
        # Synthesize solid wall poché when the file carries wall linework but no
        # wall HATCH to fill (plain-AutoCAD / CloudConvert exports). `not
        # wall_fills` is the load-bearing gate: Revit/hatch files have a non-empty
        # wall_fills and never enter here, so their output stays byte-identical.
        if (config.get("synthesize_poche", True) and not wall_fills
                and (wall_lines or cavity_lines)):
            poche_img = _wall_poche_image(wall_lines, cavity_lines, plan_w, plan_h,
                                          WALL, config.get("poche_close_px"))
        if poche_img:
            # The cavity faces are now part of the solid band — stroke them in
            # wall ink too so every band edge stays crisp vector (the soft raster
            # edge hides under it). Otherwise they revert to faint thin lines.
            wall_lines = wall_lines + cavity_lines
        else:
            thin_lines = thin_lines + cavity_lines

    def polyline_group(lines, style):
        return "\n".join(f'<polyline points="{_pts_attr(p)}" {style}/>'
                         for p in lines if len(p) >= 2)

    # ---- "plan only" export: just the line drawing -------------------------
    # The bare floor plan (walls/poché/doors/glazing in WALL ink + room labels),
    # cropped tight with a transparent background — no header, footer, watermark
    # or key-plan chrome. Default output is unaffected; this is a separate path.
    if config.get("plan_only"):
        geom = (
            (poche_img + "\n" if poche_img else "")
            + '<g stroke-linecap="round" stroke-linejoin="round" fill="none">\n'
            f'<path d="{" ".join(wall_fills)}" fill="{WALL}" stroke="none" fill-rule="nonzero"/>\n'
            + polyline_group(wall_lines, f'stroke="{WALL}" stroke-width="{wall_stroke}"') + "\n"
            + polyline_group(glaz_lines, f'stroke="{WALL}" stroke-width="0.9"') + "\n"
            + polyline_group(door_lines, f'stroke="{WALL}" stroke-width="1.0"') + "\n"
            + polyline_group(swing_lines, f'stroke="{WALL}" stroke-width="0.7" stroke-opacity="0.45"') + "\n"
            + polyline_group(thin_lines, f'stroke="{WALL}" stroke-width="0.6" stroke-opacity="0.55"') + "\n"
            + polyline_group(dash_lines, f'stroke="{WALL}" stroke-width="0.6" stroke-opacity="0.35" stroke-dasharray="4 3"') + "\n"
            "</g>"
        )
        labels = "\n".join(room_labels)
        # crop to the plan bounds, expanded to include any labels placed at the edge
        x0, x1 = X(minx), X(maxx)
        y0, y1 = Y(maxy), Y(miny)
        for p in placements:
            x0 = min(x0, p["px"] - p["bw"] / 2); x1 = max(x1, p["px"] + p["bw"] / 2)
            y0 = min(y0, p["py"] - p["bh"] / 2); y1 = max(y1, p["py"] + p["bh"] / 2)
        # generous margin so the exported asset has breathing room on every
        # edge — scales with the plan size, with a sensible floor
        pad = max(64.0, 0.10 * max(x1 - x0, y1 - y0))
        vbx, vby = x0 - pad, y0 - pad
        vbw, vbh = (x1 - x0) + 2 * pad, (y1 - y0) + 2 * pad
        bare = (
            f'<svg xmlns="http://www.w3.org/2000/svg" '
            f'viewBox="{vbx:.1f} {vby:.1f} {vbw:.1f} {vbh:.1f}" font-family="{SANS}">\n'
            f'<rect x="{vbx:.1f}" y="{vby:.1f}" width="{vbw:.1f}" height="{vbh:.1f}" fill="#FFFFFF"/>\n'
            f'{geom}\n{labels}\n</svg>'
        )
        out_w = min(2400, max(1000, round(vbw * 2)))
        png_bytes = cairosvg.svg2png(bytestring=bare.encode("utf-8"), output_width=out_w)
        meta = {
            "transform": {"tx": round(tx, 4), "ty": round(ty, 4), "s": round(s, 6)},
            "page": {"w": round(vbw, 1), "h": round(vbh, 1)},
            "extents": {"minx": minx, "maxx": maxx, "miny": miny, "maxy": maxy},
            "placements": placements,
            "plan_only": True,
        }
        return bare, png_bytes, meta

    title = esc((md.get("title") or "").upper())
    suite = esc(md.get("suite") or "")
    sf = esc(md.get("sf") or "")
    prop_name = esc((md.get("property_name") or "").upper())
    location = esc((md.get("location") or "").upper())
    lockup = esc(md.get("lockup") or "")
    watermark = esc(md.get("watermark") or lockup or "")
    # Centered ghost watermark behind the plan: an uploaded image if provided,
    # otherwise the text mark sized to fit the page width (so a longer mark like
    # "2274" scales down instead of overflowing the fixed 430px size).
    wm_img = md.get("watermark_image")
    wm_cx, wm_cy = PAGE_W / 2, plan_top + plan_h / 2
    if wm_img:
        wm_box = 460.0
        watermark_svg = (
            f'<image href="{wm_img}" x="{wm_cx - wm_box / 2:.0f}" '
            f'y="{wm_cy - wm_box / 2:.0f}" width="{wm_box:.0f}" height="{wm_box:.0f}" '
            f'opacity="0.08" preserveAspectRatio="xMidYMid meet"/>')
    elif watermark:
        wm_size = min(430.0, 1500.0 / max(len(watermark), 1))
        watermark_svg = (
            f'<text x="{wm_cx:.0f}" y="{wm_cy:.0f}" text-anchor="middle" '
            f'dominant-baseline="central" font-family="{SERIF}" font-weight="bold" '
            f'font-size="{wm_size:.0f}" fill="{ACCENT}" fill-opacity="0.07">{watermark}</text>')
    else:
        watermark_svg = ""
    # Optional "SOLD OUT" status stamp: a bold centered diagonal mark laid *on
    # top of* the finished plan and labels (unlike the ghost brand watermark
    # above, which sits behind everything). Per-sheet flag carried in the unit
    # metadata, so it persists on save, restores on re-open, and rides into the
    # PNG export. The bare plan_only export never reaches here, so it stays clean.
    sold_out_svg = ""
    if md.get("sold_out"):
        so_text = "SOLD OUT"
        so_target_w = PAGE_W * 0.74          # how wide the mark should run
        so_size, so_ls = 150.0, 8.0
        tw = _text_w(so_text, so_size, so_ls)
        if tw > so_target_w:                 # shrink to fit, keeping proportions
            scale = so_target_w / tw
            so_size, so_ls, tw = so_size * scale, so_ls * scale, so_target_w
        pad_x, pad_y = so_size * 0.34, so_size * 0.30
        box_w, box_h = tw + pad_x * 2, so_size + pad_y * 2
        bx, by = wm_cx - box_w / 2, wm_cy - box_h / 2
        SOLD = "#C0392B"
        sold_out_svg = (
            f'<g transform="rotate(-18 {wm_cx:.0f} {wm_cy:.0f})" opacity="0.62">'
            f'<rect x="{bx:.0f}" y="{by:.0f}" width="{box_w:.0f}" height="{box_h:.0f}" '
            f'rx="{so_size * 0.12:.0f}" fill="none" stroke="{SOLD}" '
            f'stroke-width="{max(6.0, so_size * 0.06):.0f}"/>'
            f'<text x="{wm_cx:.0f}" y="{wm_cy:.0f}" text-anchor="middle" '
            f'dominant-baseline="central" font-family="{SANS}" font-weight="bold" '
            f'font-size="{so_size:.0f}" letter-spacing="{so_ls:.1f}" fill="{SOLD}">'
            f'{so_text}</text></g>')
    footer_addr = esc((md.get("footer_address") or "").upper())
    header_right = esc((md.get("header_right") or "FLOOR PLAN").upper())
    disclaimer = esc(md.get("disclaimer") or
                     "FOR ILLUSTRATIVE PURPOSES ONLY. DIMENSIONS ARE "
                     "APPROXIMATE AND SUBJECT TO CHANGE.")
    sub_line = suite
    if suite and sf:
        sub_line = f"{suite}&#160;&#160;&#183;&#160;&#160;{sf}"
    elif sf:
        sub_line = sf
    lockup_x = 60
    # Measure the lockup in its actual font when we have the file, so the
    # divider/name spacing adapts to the font instead of a fixed-width guess.
    _disp = next((f for f in (config.get("font_faces") or [])
                  if f.get("data") and (f.get("role") == "serif" or f.get("family") == SERIF)), None)
    if _disp:
        try:
            lockup_w = _glyph_width(_disp["data"], lockup, 44, 0)
        except Exception:
            lockup_w = _text_w(lockup, 44, 0)
    else:
        lockup_w = _text_w(lockup, 44, 0)
    divider_x = lockup_x + 12 + lockup_w
    name_x = divider_x + 20

    # ---- optional footer key-plan mini-plate -------------------------------
    keyplan = config.get("keyplan") or {}
    footer_kp_svg = ""
    addr_x = PAGE_W - 60
    floor_label_svg = ""
    if keyplan.get("plate_bytes") and keyplan.get("placement") == "footer":
        iw, ih = img_size(keyplan["plate_bytes"])
        kp_w = 150.0
        kp_h = min(104.0, kp_w * ih / max(iw, 1))
        kp_ox = PAGE_W - 60 - kp_w
        kp_oy = (PAGE_H - FOOTER_H) + (FOOTER_H - kp_h) / 2 - 8
        footer_kp_svg = keyplan_group(keyplan["plate_bytes"], keyplan.get("box"),
                                      kp_ox, kp_oy, kp_w, kp_h, palette,
                                      north_deg=keyplan.get("north_deg", 0),
                                      silhouette=keyplan.get("silhouette_bytes"))
        addr_x = kp_ox - 24
        fl = esc((keyplan.get("floor_label") or "").upper())
        kp_cx = kp_ox + kp_w / 2
        floor_label_svg = (
            f'<text x="{kp_cx:.0f}" y="{PAGE_H-18}" text-anchor="middle" '
            f'font-size="7" letter-spacing="2" fill="{MID}" '
            f'fill-opacity="0.6">{fl} &#183; SCHEMATIC, NOT TO SCALE</text>')

    svg = f'''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {PAGE_W} {PAGE_H}" font-family="{SANS}">
  <rect width="{PAGE_W}" height="{PAGE_H}" fill="{LIGHT}"/>
  {watermark_svg}
  <rect width="{PAGE_W}" height="{HEADER_H}" fill="{DARK}"/>
  <text x="{lockup_x}" y="62" font-family="{SERIF}" font-weight="bold" font-size="44" fill="{ACCENT}">{lockup}</text>
  <line x1="{divider_x:.0f}" y1="24" x2="{divider_x:.0f}" y2="68" stroke="{ACCENT}" stroke-width="1.2" stroke-opacity="0.7"/>
  <text x="{name_x:.0f}" y="50" font-size="21" letter-spacing="7" fill="#FFFFFF">{prop_name}</text>
  <text x="{name_x:.0f}" y="71" font-size="11" letter-spacing="4" fill="{MID}" fill-opacity="0.85">{location}</text>
  <text x="{PAGE_W-60}" y="56" text-anchor="end" font-size="11" letter-spacing="3.5" fill="{MID}" fill-opacity="0.7">{header_right}</text>
  {poche_img or ''}
  <g stroke-linecap="round" stroke-linejoin="round" fill="none">
    <path d="{' '.join(wall_fills)}" fill="{WALL}" stroke="none" fill-rule="nonzero"/>
{polyline_group(wall_lines, f'stroke="{WALL}" stroke-width="{wall_stroke}"')}
{polyline_group(glaz_lines, f'stroke="{WALL}" stroke-width="0.9"')}
{polyline_group(door_lines, f'stroke="{WALL}" stroke-width="1.0"')}
{polyline_group(swing_lines, f'stroke="{WALL}" stroke-width="0.7" stroke-opacity="0.45"')}
{polyline_group(thin_lines, f'stroke="{WALL}" stroke-width="0.6" stroke-opacity="0.55"')}
{polyline_group(dash_lines, f'stroke="{WALL}" stroke-width="0.6" stroke-opacity="0.35" stroke-dasharray="4 3"')}
  </g>
{chr(10).join(room_labels)}
  {sold_out_svg}
  <rect y="{PAGE_H-FOOTER_H}" width="{PAGE_W}" height="{FOOTER_H}" fill="{DARK}"/>
  <text x="60" y="{PAGE_H-FOOTER_H+62}" font-family="{SERIF}" font-size="40" fill="#FFFFFF">{title}</text>
  <line x1="62" y1="{PAGE_H-FOOTER_H+80}" x2="122" y2="{PAGE_H-FOOTER_H+80}" stroke="{ACCENT}" stroke-width="2.5"/>
  <text x="60" y="{PAGE_H-FOOTER_H+106}" font-size="12.5" letter-spacing="3" fill="{MID}">{sub_line}</text>
  <text x="{addr_x:.0f}" y="{PAGE_H-FOOTER_H+62}" text-anchor="end" font-size="12" letter-spacing="2.5" fill="{ACCENT}">{footer_addr}</text>
  <text x="{addr_x:.0f}" y="{PAGE_H-FOOTER_H+104}" text-anchor="end" font-size="8.5" letter-spacing="1" fill="{MID}" fill-opacity="0.45">{disclaimer}</text>
  {footer_kp_svg}
  {floor_label_svg}
</svg>'''

    png_bytes = cairosvg.svg2png(bytestring=svg.encode("utf-8"), output_width=900)
    meta = {
        "transform": {"tx": round(tx, 4), "ty": round(ty, 4), "s": round(s, 6)},
        "page": {"w": PAGE_W, "h": PAGE_H},
        "extents": {"minx": minx, "maxx": maxx, "miny": miny, "maxy": maxy},
        "placements": placements,
    }
    return svg, png_bytes, meta
