import json, html, cairosvg
import numpy as np
from PIL import Image, ImageDraw

# ---------- Stone & Ember palette ----------
CHARCOAL = "#2B1F14"
EMBER    = "#C17F3A"
LIMESTONE= "#E8D9C0"
CHALK    = "#F7F3ED"

SERIF = "Georgia, 'Times New Roman', serif"
SANS  = "'Helvetica Neue', Helvetica, Arial, sans-serif"

PAGE_W, PAGE_H = 1000, 1080
HEADER_H = 92
FOOTER_H = 140
PLAN_MAX_W, PLAN_MAX_H = 800, 640

DROP_LAYERS = {'A-AREA-IDEN','S-COLS-SYMB','S-STRS','S-STRS-MBND'}
WALL_LINE_LAYERS = {'A-WALL','I-WALL'}
DOOR_LAYERS = {'A-DOOR','A-DOOR-FRAM'}
GLAZ = 'A-GLAZ'
DASHED_LAYERS = {'A-DETL-HDLN','A-FLOR-OVHD'}

# rooms: (NAME, dims-or-None, interior search rect (x1,x2,y1,y2) in DXF coords, font scale)
UNITS = {
 '1A': dict(
   geom='/home/claude/geom_1A.json',
   title='ONE BED', sub='SUITE 202', sf='517 SF',
   out='/home/claude/800-princess-one-bed-1A.svg',
   rooms=[
     ('BEDROOM',     "14'4\" x 9'3\"",  (-9620,-9460, -78, 24),  1.0),
     ('LIVING ROOM', "14'9\" x 9'2\"",  (-9620,-9455, -193,-93), 1.0),
     ('KITCHEN',     "12'2\" x 7'8\"",  (-9444,-9308, -163,-80), 1.0),
     ('W.I.C',        None,            (-9448,-9416, -4, 28),   0.9),
     ('WASHROOM',     None,            (-9402,-9310, -64, -4),  0.9),
     ('PANTRY',       None,            (-9336,-9305, -128,-78), 0.8),
   ]),
 'GS': dict(
   geom='/home/claude/geom_GS.json',
   title='GUEST SUITE', sub='SUITE 110', sf='506 SF',
   out='/home/claude/800-princess-guest-suite.svg',
   rooms=[
     ('BEDROOM',     "9'9\" x 12'9\"",  (-9614,-9512,-1282,-1135), 1.0),
     ('LIVING ROOM', "9'3\" x 17'10\"", (-9500,-9392,-1330,-1130), 1.0),
     ('KITCHEN',     "7'0\" x 14'6\"",  (-9372,-9318,-1226,-1136), 0.9),
     ('DEN',         "7'2\" x 8'4\"",   (-9376,-9302,-1330,-1242), 1.0),
     ('W.I.C.',       None,            (-9614,-9518,-1114,-1064), 0.9),
     ('WASHROOM',     None,            (-9492,-9407,-1112,-1062), 0.9),
   ]),
}

def pts_attr(pts):
    return " ".join(f"{x:.1f},{y:.1f}" for x, y in pts)

def poly_path(polys):
    return " ".join("M " + " L ".join(f"{x:.1f} {y:.1f}" for x, y in p) + " Z" for p in polys)

def text_w(s, size, ls):
    """generous width estimate for letter-spaced uppercase sans"""
    return 0.70 * size * len(s) + ls * (len(s) - 1) + 6

def build(tag):
    u = UNITS[tag]
    prims = json.load(open(u['geom']))['prims']

    xs, ys, kept = [], [], []
    for layer, kind, data, block in prims:
        if layer in DROP_LAYERS: continue
        if layer == 'A-FLOR' and kind == 'hatch': continue
        kept.append((layer, kind, data, block))
        if kind == 'line':
            xs += [p[0] for p in data]; ys += [p[1] for p in data]
        else:
            for poly in data:
                xs += [p[0] for p in poly]; ys += [p[1] for p in poly]
    minx, maxx, miny, maxy = min(xs), max(xs), min(ys), max(ys)
    w_in, h_in = maxx - minx, maxy - miny
    s = min(PLAN_MAX_W / w_in, PLAN_MAX_H / h_in)
    plan_w, plan_h = w_in * s, h_in * s
    plan_top = HEADER_H + (PAGE_H - HEADER_H - FOOTER_H - plan_h) / 2
    tx = (PAGE_W - plan_w) / 2 - minx * s
    ty = plan_top + maxy * s
    def X(x): return tx + x * s
    def Y(y): return ty - y * s

    # ---- split geometry by style + build occupancy raster ----
    wall_fills, wall_lines, glaz_lines, door_lines, swing_lines = [], [], [], [], []
    thin_lines, dash_lines = [], []
    occ_img = Image.new('1', (PAGE_W, PAGE_H), 0)
    dr = ImageDraw.Draw(occ_img)
    for layer, kind, data, block in kept:
        if kind == 'hatch':
            polys = [[(X(x), Y(y)) for x, y in p] for p in data]
            if layer == 'A-WALL-PATT':
                wall_fills.append(poly_path(polys))
                for p in polys: dr.polygon(p, fill=1, outline=1)
            else:
                thin_lines += polys
                for p in polys: dr.line(p + [p[0]], fill=1, width=3)
            continue
        pts = [(X(x), Y(y)) for x, y in data]
        if layer in WALL_LINE_LAYERS: wall_lines.append(pts)
        elif layer == GLAZ: glaz_lines.append(pts)
        elif layer in DOOR_LAYERS:
            (swing_lines if len(pts) > 6 else door_lines).append(pts)
        elif layer in DASHED_LAYERS: dash_lines.append(pts)
        else: thin_lines.append(pts)
        if len(pts) >= 2: dr.line(pts, fill=1, width=3)
    occ = np.asarray(occ_img, dtype=np.uint8)
    # integral image for fast box sums
    I = occ.astype(np.int64).cumsum(0).cumsum(1)
    def box_sum(x1, y1, x2, y2):
        x1, y1 = max(int(x1), 1), max(int(y1), 1)
        x2, y2 = min(int(x2), PAGE_W - 1), min(int(y2), PAGE_H - 1)
        if x2 <= x1 or y2 <= y1: return 1e9
        return int(I[y2, x2] - I[y1-1, x2] - I[y2, x1-1] + I[y1-1, x1-1])

    def place(rect, bw, bh):
        """centre of label box: clearest spot nearest the room centre"""
        x1, x2 = sorted((X(rect[0]), X(rect[1])))
        y1, y2 = sorted((Y(rect[2]), Y(rect[3])))
        cx, cy = (x1 + x2) / 2, (y1 + y2) / 2
        best, best_key = (cx, cy), None
        pad = 3
        for px in np.arange(x1 + bw/2, x2 - bw/2 + 1, 2):
            for py in np.arange(y1 + bh/2, y2 - bh/2 + 1, 2):
                ov = box_sum(px - bw/2 - pad, py - bh/2 - pad, px + bw/2 + pad, py + bh/2 + pad)
                d = ((px - cx)**2 + (py - cy)**2) ** 0.5
                key = (ov > 0, ov, d)   # prefer zero overlap, then least, then central
                if best_key is None or key < best_key:
                    best_key, best = key, (px, py)
        return best

    esc = html.escape
    room_labels = []
    for name, dims, rect, k in u['rooms']:
        n_size, n_ls = 11.5 * k, 2.2 * k
        d_size, d_ls = 10 * k, 1.2 * k
        bw = max(text_w(name, n_size, n_ls), text_w(dims, d_size, d_ls) if dims else 0)
        bh = (n_size + 4 + d_size) if dims else n_size
        px, py = place(rect, bw, bh)
        def halo_text(x, y, size, ls, opac, content):
            common = (f'x="{x:.0f}" y="{y:.1f}" text-anchor="middle" '
                      f'font-family="{SANS}" font-size="{size:.1f}" letter-spacing="{ls:.1f}"')
            return (f'<text {common} fill="none" stroke="{CHALK}" stroke-width="8" '
                    f'stroke-linejoin="round">{content}</text>\n'
                    f'<text {common} fill="{CHARCOAL}" fill-opacity="{opac}">{content}</text>')
        if dims:
            room_labels.append(halo_text(px, py - bh/2 + n_size - 1, n_size, n_ls, 0.78, esc(name)))
            room_labels.append(halo_text(px, py + bh/2 - 1, d_size, d_ls, 0.5, esc(dims)))
        else:
            room_labels.append(halo_text(px, py + n_size * 0.36, n_size, n_ls, 0.78, esc(name)))

    def polyline_group(lines, style):
        return "\n".join(f'<polyline points="{pts_attr(p)}" {style}/>' for p in lines)

    svg = f'''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {PAGE_W} {PAGE_H}" font-family="{SANS}">
  <rect width="{PAGE_W}" height="{PAGE_H}" fill="{CHALK}"/>

  <!-- watermark -->
  <text x="{PAGE_W/2}" y="{plan_top + plan_h/2 + 150:.0f}" text-anchor="middle"
        font-family="{SERIF}" font-weight="bold" font-size="430"
        fill="{EMBER}" fill-opacity="0.07">800</text>

  <!-- header -->
  <rect width="{PAGE_W}" height="{HEADER_H}" fill="{CHARCOAL}"/>
  <text x="60" y="62" font-family="{SERIF}" font-weight="bold" font-size="44" fill="{EMBER}">800</text>
  <line x1="172" y1="24" x2="172" y2="68" stroke="{EMBER}" stroke-width="1.2" stroke-opacity="0.7"/>
  <text x="192" y="50" font-size="21" letter-spacing="7" fill="#FFFFFF">PRINCESS</text>
  <text x="192" y="71" font-size="11" letter-spacing="4" fill="{LIMESTONE}" fill-opacity="0.85">KINGSTON&#160;&#160;&#183;&#160;&#160;ON</text>
  <text x="{PAGE_W-60}" y="56" text-anchor="end" font-size="11" letter-spacing="3.5" fill="{LIMESTONE}" fill-opacity="0.7">FLOOR PLAN</text>

  <!-- plan -->
  <g stroke-linecap="round" stroke-linejoin="round" fill="none">
    <path d="{' '.join(wall_fills)}" fill="{CHARCOAL}" stroke="none" fill-rule="nonzero"/>
{polyline_group(wall_lines, f'stroke="{CHARCOAL}" stroke-width="1.6"')}
{polyline_group(glaz_lines, f'stroke="{CHARCOAL}" stroke-width="0.9"')}
{polyline_group(door_lines, f'stroke="{CHARCOAL}" stroke-width="1.0"')}
{polyline_group(swing_lines, f'stroke="{CHARCOAL}" stroke-width="0.7" stroke-opacity="0.45"')}
{polyline_group(thin_lines, f'stroke="{CHARCOAL}" stroke-width="0.6" stroke-opacity="0.55"')}
{polyline_group(dash_lines, f'stroke="{CHARCOAL}" stroke-width="0.6" stroke-opacity="0.35" stroke-dasharray="4 3"')}
  </g>

  <!-- room labels -->
{chr(10).join(room_labels)}

  <!-- footer -->
  <rect y="{PAGE_H-FOOTER_H}" width="{PAGE_W}" height="{FOOTER_H}" fill="{CHARCOAL}"/>
  <text x="60" y="{PAGE_H-FOOTER_H+62}" font-family="{SERIF}" font-size="40" fill="#FFFFFF">{esc(u['title'])}</text>
  <line x1="62" y1="{PAGE_H-FOOTER_H+80}" x2="122" y2="{PAGE_H-FOOTER_H+80}" stroke="{EMBER}" stroke-width="2.5"/>
  <text x="60" y="{PAGE_H-FOOTER_H+106}" font-size="12.5" letter-spacing="3" fill="{LIMESTONE}">{esc(u['sub'])}&#160;&#160;&#183;&#160;&#160;{esc(u['sf'])}</text>
  <text x="{PAGE_W-60}" y="{PAGE_H-FOOTER_H+62}" text-anchor="end" font-size="12" letter-spacing="2.5" fill="{EMBER}">800 PRINCESS ST &#183; KINGSTON, ON</text>
  <text x="{PAGE_W-60}" y="{PAGE_H-FOOTER_H+104}" text-anchor="end" font-size="8.5" letter-spacing="1" fill="{LIMESTONE}" fill-opacity="0.45">FOR ILLUSTRATIVE PURPOSES ONLY. DIMENSIONS ARE APPROXIMATE AND SUBJECT TO CHANGE.</text>
</svg>'''
    open(u['out'], 'w').write(svg)
    cairosvg.svg2png(url=u['out'], write_to=u['out'].replace('.svg','.png'), output_width=900)
    print("wrote", u['out'])

for t in UNITS: build(t)
