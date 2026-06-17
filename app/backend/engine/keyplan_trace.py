"""
Auto-trace a raw Revit top-down plate screenshot into a clean filled
floor-footprint silhouette — the "basic key plan" look. numpy + PIL only.

`trace_plate()` returns a palette-independent grayscale mask PNG (255 inside the
footprint, 0 outside) at a downscaled resolution, so it can be cached once per
(plate, seal). `colorize()` turns that mask + a brand palette into an RGBA PNG
(footprint filled in `mid`, outline in `dark`, transparent elsewhere) at render
time — cheap, so palette changes don't invalidate the cached trace.

The seal kernel is image-dependent: it must be wider than the building's
doorway/opening gaps to close the perimeter so the interior fills. It is exposed
to the user as a "seal strength" slider, with a one-click fallback to the
dimmed-screenshot mode when a plate won't trace cleanly. Schematic by design.

Pipeline: downscale -> drop the colored section gizmo (high-saturation pixels)
-> threshold dark walls -> morphological close (seal gaps) -> fill interior
holes -> keep the largest connected component (drops surrounding context).
"""
import io
from collections import deque

import numpy as np
from PIL import Image, ImageFilter

TARGET_W = 700       # downscale width; the trace is schematic, so this is plenty
SAT_THRESH = 60      # saturation above this == coloured gizmo, not wall ink
DARK_THRESH = 110    # grayscale below this == wall ink


def _odd(k):
    k = max(3, int(k))
    return k if k % 2 else k + 1


def _morph(mask, k, grow):
    """Dilate (grow=True) or erode (grow=False) a boolean mask via PIL filters."""
    im = Image.fromarray((mask * 255).astype(np.uint8))
    f = ImageFilter.MaxFilter(_odd(k)) if grow else ImageFilter.MinFilter(_odd(k))
    return np.asarray(im.filter(f)) > 127


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


def trace_plate(plate_bytes, seal=35):
    """Raw plate bytes -> (mask_png_bytes, coverage_fraction).

    mask is an L-mode PNG (255 inside the footprint, 0 outside) at downscaled
    resolution, preserving the input aspect ratio so box fractions still map.
    coverage is the footprint's share of the frame — a sanity signal the UI can
    use (very low == traced walls only; very high == bridged into neighbours)."""
    im = Image.open(io.BytesIO(plate_bytes)).convert("RGB")
    if im.width > TARGET_W:
        im = im.resize((TARGET_W, round(im.height * TARGET_W / im.width)))
    rgb = np.asarray(im)
    gray, sat = _to_gray_sat(rgb)
    walls = (gray < DARK_THRESH) & (sat < SAT_THRESH)
    closed = _morph(_morph(walls, seal, True), seal, False)
    solid = _fill_holes(closed)
    foot = _largest_component(solid)
    foot = _morph(_morph(foot, 5, False), 5, True)   # despeckle the edge
    cov = float(foot.mean())
    buf = io.BytesIO()
    Image.fromarray((foot * 255).astype(np.uint8), "L").save(buf, "PNG")
    return buf.getvalue(), cov


def _hex(s):
    s = s.lstrip("#")
    return tuple(int(s[i:i + 2], 16) for i in (0, 2, 4))


def colorize(mask_png_bytes, palette):
    """Mask PNG + palette -> RGBA PNG: footprint filled in `mid`, outline in
    `dark`, transparent elsewhere. Cheap — run per render, not cached."""
    mask = np.asarray(Image.open(io.BytesIO(mask_png_bytes)).convert("L")) > 127
    edge = mask & ~_morph(mask, 3, False)
    h, w = mask.shape
    rgba = np.zeros((h, w, 4), np.uint8)
    rgba[mask] = (*_hex(palette.get("mid", "#E8D9C0")), 255)
    rgba[edge] = (*_hex(palette.get("dark", "#2B1F14")), 255)
    buf = io.BytesIO()
    Image.fromarray(rgba, "RGBA").save(buf, "PNG")
    return buf.getvalue()
