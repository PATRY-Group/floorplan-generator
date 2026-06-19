"""
Auto-trace a raw Revit top-down plate screenshot into a clean filled
floor-footprint silhouette — the "basic key plan" look. numpy + PIL only.

`trace_plate()` returns a palette-independent grayscale mask PNG at a downscaled
resolution, so it can be cached once per (plate, seal). The mask carries three
levels: 0 outside the footprint, 255 the footprint interior, and 128 the wall
ink traced from the photo *within* the footprint (interior room divisions —
clipped to the footprint so the surrounding context/furniture outside it is
dropped). `colorize()` turns that mask + a brand palette into an RGBA PNG
(footprint filled in `mid`, perimeter outline in `dark`, the traced interior
walls in black, transparent elsewhere) at render time — cheap, so palette
changes don't invalidate the cached trace.

The seal kernel is image-dependent: it must be wider than the building's
doorway/opening gaps to close the perimeter so the interior fills. It is exposed
to the user as a "seal strength" slider, with a one-click fallback to the
dimmed-screenshot mode when a plate won't trace cleanly. Schematic by design.

Pipeline: resample to a working resolution -> drop the colored section gizmo
(high-saturation pixels) -> threshold dark walls -> morphological close (seal
gaps) -> fill interior holes -> keep the largest connected component (drops
surrounding context). The interior walls are additionally solidified (the two
drawn faces are closed into one band), de-speckled, and contour-smoothed, and
`colorize()` anti-aliases on the way out — so the black wall lines read as clean
edges rather than jagged raster stair-steps when the key plan is zoomed.
"""
import io
from collections import deque

import numpy as np
from PIL import Image, ImageFilter

# Kernels below are calibrated at BASE_W px and scaled to the working TARGET_W,
# so raising the resolution (for smoother edges) doesn't change how aggressively
# anything seals/cleans. TARGET_W is finer than strictly needed for the footprint
# because the interior wall lines are what the eye zooms into — coarse pixels
# there read as "hairy" stair-steps.
BASE_W = 700         # kernel calibration reference
TARGET_W = 1000      # working trace resolution (finer == smoother wall edges)
SAT_THRESH = 60      # saturation above this == coloured gizmo, not wall ink
DARK_THRESH = 110    # grayscale below this == wall ink
WALL_SOLID = 5       # close kernel to fill the cavity between a wall's two faces
WALL_SMOOTH = 3      # open kernel to shave hairs/spurs off the traced walls
WALL_SPECKLE = 40    # drop traced-wall components smaller than this (px) — speckle
SMOOTH_FOOT = 1.1    # gaussian blur (px @ BASE_W) to round the footprint contour
SMOOTH_WALL = 0.9    # gaussian blur (px @ BASE_W) to round the wall contours
DISPLAY_W = 900      # colorize output width — LANCZOS downscale anti-aliases edges


def _odd(k):
    k = max(3, int(k))
    return k if k % 2 else k + 1


def _morph(mask, k, grow):
    """Dilate (grow=True) or erode (grow=False) a boolean mask. Uses an integral
    image so cost is independent of kernel size — large seal kernels at high
    resolution would otherwise dominate the trace. Matches a PIL Max/MinFilter
    of the same odd kernel."""
    k = _odd(k)
    r = k // 2
    h, w = mask.shape
    ii = np.zeros((h + 1, w + 1), np.int64)
    ii[1:, 1:] = mask.astype(np.int64).cumsum(0).cumsum(1)
    y0 = np.clip(np.arange(h) - r, 0, h)
    y1 = np.clip(np.arange(h) + r + 1, 0, h)
    x0 = np.clip(np.arange(w) - r, 0, w)
    x1 = np.clip(np.arange(w) + r + 1, 0, w)
    Y0, X0 = np.meshgrid(y0, x0, indexing="ij")
    Y1, X1 = np.meshgrid(y1, x1, indexing="ij")
    s = ii[Y1, X1] - ii[Y0, X1] - ii[Y1, X0] + ii[Y0, X0]
    if grow:
        return s > 0                       # any True in window == dilate
    return s >= (Y1 - Y0) * (X1 - X0)       # all True in window == erode


def _smooth(mask, blur):
    """Round off stair-steps: gaussian-blur the binary mask, re-threshold at 0.5.
    Turns a jagged raster contour into a smooth one without an external lib."""
    if blur <= 0:
        return mask
    im = Image.fromarray((mask * 255).astype(np.uint8))
    return np.asarray(im.filter(ImageFilter.GaussianBlur(blur))) > 127


def _to_gray_sat(rgb):
    r, g, b = (rgb[..., i].astype(int) for i in range(3))
    gray = 0.299 * r + 0.587 * g + 0.114 * b
    mx = np.maximum(np.maximum(r, g), b)
    mn = np.minimum(np.minimum(r, g), b)
    sat = np.where(mx > 0, (mx - mn) / np.maximum(mx, 1) * 255, 0)
    return gray, sat


def _fill_holes(mask):
    """Flood-fill background inward from the border; unreached background is
    interior, so OR it back in to make the footprint solid."""
    h, w = mask.shape
    bg = ~mask
    reached = np.zeros_like(mask)
    dq = deque()
    for x in range(w):
        for y in (0, h - 1):
            if bg[y, x] and not reached[y, x]:
                reached[y, x] = True
                dq.append((y, x))
    for y in range(h):
        for x in (0, w - 1):
            if bg[y, x] and not reached[y, x]:
                reached[y, x] = True
                dq.append((y, x))
    while dq:
        y, x = dq.popleft()
        for dy, dx in ((1, 0), (-1, 0), (0, 1), (0, -1)):
            ny, nx = y + dy, x + dx
            if 0 <= ny < h and 0 <= nx < w and bg[ny, nx] and not reached[ny, nx]:
                reached[ny, nx] = True
                dq.append((ny, nx))
    return mask | (bg & ~reached)


def _largest_component(mask):
    """Keep only the largest 4-connected True region (drops context structures)."""
    h, w = mask.shape
    lbl = np.zeros((h, w), np.int32)
    cur = best = best_sz = 0
    for sy in range(h):
        for sx in range(w):
            if mask[sy, sx] and lbl[sy, sx] == 0:
                cur += 1
                sz = 0
                dq = deque([(sy, sx)])
                lbl[sy, sx] = cur
                while dq:
                    y, x = dq.popleft()
                    sz += 1
                    for dy, dx in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                        ny, nx = y + dy, x + dx
                        if 0 <= ny < h and 0 <= nx < w and mask[ny, nx] and lbl[ny, nx] == 0:
                            lbl[ny, nx] = cur
                            dq.append((ny, nx))
                if sz > best_sz:
                    best_sz, best = sz, cur
    return (lbl == best) if best else mask


def _drop_small(mask, min_area):
    """Remove 4-connected True components smaller than `min_area` pixels — kills
    isolated speckle (dimension dots, furniture marks) while keeping wall runs."""
    h, w = mask.shape
    seen = np.zeros((h, w), bool)
    out = np.zeros((h, w), bool)
    for sy in range(h):
        for sx in range(w):
            if mask[sy, sx] and not seen[sy, sx]:
                comp = []
                dq = deque([(sy, sx)])
                seen[sy, sx] = True
                while dq:
                    y, x = dq.popleft()
                    comp.append((y, x))
                    for dy, dx in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                        ny, nx = y + dy, x + dx
                        if 0 <= ny < h and 0 <= nx < w and mask[ny, nx] and not seen[ny, nx]:
                            seen[ny, nx] = True
                            dq.append((ny, nx))
                if len(comp) >= min_area:
                    for y, x in comp:
                        out[y, x] = True
    return out


def trace_plate(plate_bytes, seal=35):
    """Raw plate bytes -> (mask_png_bytes, coverage_fraction).

    mask is an L-mode PNG at downscaled resolution, preserving the input aspect
    ratio so box fractions still map. Three levels: 0 outside the footprint, 255
    the footprint interior, 128 the wall ink traced from the photo *inside* the
    footprint. coverage is the footprint's share of the frame — a sanity signal
    the UI can use (very low == traced walls only; very high == bridged into
    neighbours)."""
    im = Image.open(io.BytesIO(plate_bytes)).convert("RGB")
    if im.width != TARGET_W:
        im = im.resize((TARGET_W, max(1, round(im.height * TARGET_W / im.width))))
    f = im.width / BASE_W                       # scale kernels to working resolution
    k = lambda base: max(3, round(base * f))
    rgb = np.asarray(im)
    gray, sat = _to_gray_sat(rgb)
    walls = (gray < DARK_THRESH) & (sat < SAT_THRESH)
    sealk = max(3, round(seal * f))
    closed = _morph(_morph(walls, sealk, True), sealk, False)
    solid = _fill_holes(closed)
    foot = _largest_component(solid)
    foot = _morph(_morph(foot, k(5), False), k(5), True)   # despeckle the edge
    foot = _smooth(foot, SMOOTH_FOOT * f)                  # round the contour
    cov = float(foot.mean())
    # interior wall ink: the dark lines from the photo that fall within the
    # sealed footprint (so exterior context/stairs/hatching are dropped). Walls
    # are drawn as two parallel faces with a hollow gap; close that cavity so
    # each wall reads as one solid band instead of a traced double line. Then
    # clean up the raw ink: open to shave hairs/spurs, drop speckle components,
    # and smooth the contour so edges don't read as jagged stair-steps on zoom.
    inner = walls & foot
    inner = _morph(_morph(inner, k(WALL_SOLID), True), k(WALL_SOLID), False)
    inner = _morph(_morph(inner, k(WALL_SMOOTH), False), k(WALL_SMOOTH), True)
    inner = _drop_small(inner & foot, round(WALL_SPECKLE * f * f))
    inner = _smooth(inner, SMOOTH_WALL * f) & foot
    level = (foot.astype(np.uint8)) * 255
    level[inner] = 128
    buf = io.BytesIO()
    Image.fromarray(level, "L").save(buf, "PNG")
    return buf.getvalue(), cov


def _hex(s):
    s = s.lstrip("#")
    return tuple(int(s[i:i + 2], 16) for i in (0, 2, 4))


def colorize(mask_png_bytes, palette):
    """Mask PNG + palette -> RGBA PNG: footprint filled in `mid`, traced interior
    walls in black, perimeter outline in `dark`, transparent elsewhere. Cheap —
    run per render, not cached."""
    level = np.asarray(Image.open(io.BytesIO(mask_png_bytes)).convert("L"))
    foot = level > 64                       # footprint = interior (255) + walls (128)
    walls = (level > 64) & (level < 192)    # the 128 band: traced interior wall ink
    h, w = level.shape
    edge = foot & ~_morph(foot, max(3, round(3 * w / BASE_W)), False)  # perimeter
    rgba = np.zeros((h, w, 4), np.uint8)
    rgba[foot] = (*_hex(palette.get("mid", "#E8D9C0")), 255)
    rgba[walls] = (0, 0, 0, 255)            # interior room divisions, pure black
    rgba[edge] = (*_hex(palette.get("dark", "#2B1F14")), 255)
    img = Image.fromarray(rgba, "RGBA")
    if w > DISPLAY_W:                       # LANCZOS downscale anti-aliases the edges
        img = img.resize((DISPLAY_W, max(1, round(h * DISPLAY_W / w))), Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()
