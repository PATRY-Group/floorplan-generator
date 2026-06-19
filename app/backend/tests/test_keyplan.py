"""
keyplan.py / keyplan_trace.py — the "where is my unit" plate.

Validates: the unit-cell box maps from image fractions to frame coordinates,
the standalone sheet is well-formed and carries its NOT-TO-SCALE marker (and
refuses without a plate), and the auto-trace produces a deterministic mask +
brand-coloured silhouette.
"""
import io
import re
import unittest
import xml.etree.ElementTree as ET

import numpy as np
from PIL import Image

import fixtures as fx
from engine import (keyplan_group, render_keyplan_sheet,
                    trace_plate, colorize_trace)


class KeyplanGroupTest(unittest.TestCase):
    def test_box_fraction_maps_to_frame_coords(self):
        """box = [fx, fy, fw, fh] as image fractions -> the shaded accent cell
        at (ox + fx*w, oy + fy*h, fw*w, fh*h)."""
        svg = keyplan_group(fx.plate_png(), [0.25, 0.5, 0.5, 0.25],
                            ox=0, oy=0, w=100, h=80,
                            palette={"dark": "#000", "accent": "#C17F3A"})
        # the accent cell is the rect drawn with fill-opacity 0.55
        m = re.search(r'<rect x="([\d.]+)" y="([\d.]+)" width="([\d.]+)" '
                      r'height="([\d.]+)" fill="#C17F3A" fill-opacity="0.55"', svg)
        self.assertIsNotNone(m)
        self.assertEqual([float(g) for g in m.groups()], [25.0, 40.0, 50.0, 20.0])

    def test_no_box_means_no_cell(self):
        svg = keyplan_group(fx.plate_png(), None, 0, 0, 100, 80,
                            palette={"accent": "#C17F3A"})
        self.assertNotIn("fill-opacity=\"0.55\"", svg)


class KeyplanSheetTest(unittest.TestCase):
    def test_requires_a_plate(self):
        with self.assertRaises(ValueError):
            render_keyplan_sheet({"metadata": {}, "keyplan": {}})

    def test_standalone_sheet_is_branded_and_marked(self):
        cfg = {
            "metadata": {"property_name": "TEST TOWER", "lockup": "800",
                         "title": "2 BED", "location": "CITY"},
            "keyplan": {"plate_bytes": fx.plate_png(),
                        "floor_label": "LEVEL 3", "box": [0.2, 0.2, 0.3, 0.3]},
        }
        svg = render_keyplan_sheet(cfg)
        ET.fromstring(svg)
        self.assertIn("KEY PLAN", svg)
        self.assertIn("SCHEMATIC KEY PLAN — NOT TO SCALE", svg)
        self.assertIn("LEVEL 3", svg)


class TracePlateTest(unittest.TestCase):
    def test_trace_returns_mask_and_coverage(self):
        mask, cov = trace_plate(fx.plate_png(), seal=35)
        self.assertEqual(mask[:8], b"\x89PNG\r\n\x1a\n")
        self.assertTrue(0.0 < cov < 1.0)        # a real footprint, not whole/empty
        # the mask is a single-channel (L) image
        self.assertEqual(Image.open(io.BytesIO(mask)).mode, "L")

    def test_trace_is_deterministic(self):
        plate = fx.plate_png()
        self.assertEqual(trace_plate(plate, seal=35)[0],
                         trace_plate(plate, seal=35)[0])

    def test_colorize_fills_interior_with_mid(self):
        mask, _ = trace_plate(fx.plate_png(), seal=35)
        rgba = colorize_trace(mask, {"mid": "#E8D9C0", "dark": "#2B1F14"})
        img = Image.open(io.BytesIO(rgba))
        self.assertEqual(img.mode, "RGBA")
        arr = np.asarray(img)
        mid = (0xE8, 0xD9, 0xC0, 255)
        # the centre of the footprint should be the 'mid' fill, fully opaque
        cy, cx = arr.shape[0] // 2, arr.shape[1] // 2
        self.assertEqual(tuple(arr[cy, cx]), mid)
        # and somewhere outside is fully transparent
        self.assertEqual(arr[0, 0, 3], 0)


if __name__ == "__main__":
    unittest.main()
