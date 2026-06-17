"""
Rendering engine for the Floor Plan Sheet Generator.

Refactor of the original `build_floorplan_sheets.py`: identical layer treatment,
halo logic, placement search and page layout, driven by a `config` object.

    render(prims, config) -> (svg_str, png_bytes, meta)

`config` may include a "keyplan" with placement "footer" to embed a mini-plate
in the footer (the standalone key-plan page is produced separately by
engine.keyplan.render_keyplan_sheet).
"""

import html
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

PAGE_W, PAGE_H = 1000, 1080
HEADER_H = 92
FOOTER_H = 140
PLAN_MAX_W, PLAN_MAX_H = 800, 640

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
    thin_lines, dash_lines = [], []
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

    def polyline_group(lines, style):
        return "\n".join(f'<polyline points="{_pts_attr(p)}" {style}/>'
                         for p in lines if len(p) >= 2)

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
    divider_x = lockup_x + 12 + _text_w(lockup, 44, 0)
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
                                      north_deg=keyplan.get("north_deg", 0))
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
  <g stroke-linecap="round" stroke-linejoin="round" fill="none">
    <path d="{' '.join(wall_fills)}" fill="{WALL}" stroke="none" fill-rule="nonzero"/>
{polyline_group(wall_lines, f'stroke="{WALL}" stroke-width="1.6"')}
{polyline_group(glaz_lines, f'stroke="{WALL}" stroke-width="0.9"')}
{polyline_group(door_lines, f'stroke="{WALL}" stroke-width="1.0"')}
{polyline_group(swing_lines, f'stroke="{WALL}" stroke-width="0.7" stroke-opacity="0.45"')}
{polyline_group(thin_lines, f'stroke="{WALL}" stroke-width="0.6" stroke-opacity="0.55"')}
{polyline_group(dash_lines, f'stroke="{WALL}" stroke-width="0.6" stroke-opacity="0.35" stroke-dasharray="4 3"')}
  </g>
{chr(10).join(room_labels)}
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
