"""
keyplan.py — the "where is my unit" plate.

The user exports a finished key-plan image (the unit already marked on it); the
app trims its whitespace on intake and embeds it as reference. These validate:
autocrop tightens a white-margined image (and leaves a blank one alone), the
embedded image lands in the footer group, and the standalone sheet stays
branded + NOT-TO-SCALE (and refuses without a plate).
"""
import io
import re
import unittest
import xml.etree.ElementTree as ET

from PIL import Image

import fixtures as fx
from engine import keyplan_group, render_keyplan_sheet, autocrop_plate
from engine import pdf_to_png, PdfPlanError


class AutocropTest(unittest.TestCase):
    def test_trims_surrounding_whitespace(self):
        """A plate with a white margin around its content comes back tighter."""
        raw = fx.plate_png(size=(200, 150))            # ring at 20..180 / 20..130
        out = autocrop_plate(raw)
        self.assertEqual(out[:8], b"\x89PNG\r\n\x1a\n")
        ow, oh = Image.open(io.BytesIO(out)).size
        self.assertLess(ow, 200)
        self.assertLess(oh, 150)
        # but it keeps the content — not cropped down to nothing
        self.assertGreater(ow, 100)
        self.assertGreater(oh, 80)

    def test_blank_image_is_left_alone(self):
        """An all-white image has no content to crop -> returned unchanged."""
        buf = io.BytesIO()
        Image.new("RGB", (120, 90), "white").save(buf, "PNG")
        raw = buf.getvalue()
        self.assertEqual(autocrop_plate(raw), raw)

    def test_is_deterministic(self):
        raw = fx.plate_png()
        self.assertEqual(autocrop_plate(raw), autocrop_plate(raw))


class KeyplanGroupTest(unittest.TestCase):
    def test_embeds_image_at_full_opacity(self):
        """The group frames the plate and embeds it opaque, aspect-preserved —
        no unit box, no lightening."""
        svg = keyplan_group(fx.plate_png(), ox=0, oy=0, w=100, h=80,
                            palette={"dark": "#000"})
        self.assertIn("<image", svg)
        self.assertIn("data:image/png;base64,", svg)
        self.assertIn('preserveAspectRatio="xMidYMid meet"', svg)
        self.assertNotIn('opacity="0.5"', svg)        # embedded as reference, not dimmed
        self.assertNotIn('fill-opacity="0.55"', svg)  # no accent unit cell anymore

    def test_border_is_optional(self):
        with_b = keyplan_group(fx.plate_png(), 0, 0, 100, 80, {"dark": "#000"})
        without = keyplan_group(fx.plate_png(), 0, 0, 100, 80, {"dark": "#000"},
                                with_border=False)
        self.assertIn("<rect", with_b)
        self.assertNotIn("<rect", without)


class KeyplanSheetTest(unittest.TestCase):
    def test_requires_a_plate(self):
        with self.assertRaises(ValueError):
            render_keyplan_sheet({"metadata": {}, "keyplan": {}})

    def test_standalone_sheet_is_branded_and_marked(self):
        cfg = {
            "metadata": {"property_name": "TEST TOWER", "lockup": "800",
                         "title": "2 BED", "location": "CITY"},
            "keyplan": {"plate_bytes": fx.plate_png(), "floor_label": "LEVEL 3"},
        }
        svg = render_keyplan_sheet(cfg)
        ET.fromstring(svg)                             # well-formed
        self.assertIn("KEY PLAN", svg)
        self.assertIn("SCHEMATIC KEY PLAN — NOT TO SCALE", svg)
        self.assertIn("LEVEL 3", svg)
        self.assertIn("data:image/png;base64,", svg)   # the plate is embedded


class PdfToPngTest(unittest.TestCase):
    """pdf_to_png rasterizes a single-page PDF for the 'finished floor plan
    PDF' intake — print quality, not the brand.py thumbnail rasterizer."""

    def test_rasterizes_single_page_to_target_size(self):
        png = pdf_to_png(fx.pdf_bytes(pages=1, size=(400, 300)), target_max_dim=800)
        self.assertEqual(png[:8], b"\x89PNG\r\n\x1a\n")
        w, h = Image.open(io.BytesIO(png)).size
        self.assertEqual(max(w, h), 800)             # longest edge hits the target
        self.assertAlmostEqual(w / h, 400 / 300, places=2)   # aspect preserved

    def test_multi_page_pdf_is_rejected(self):
        """A submittal PDF with extra pages must not silently rasterize page 1
        — reject with guidance instead of guessing."""
        with self.assertRaises(PdfPlanError) as ctx:
            pdf_to_png(fx.pdf_bytes(pages=2))
        self.assertIn("one page", str(ctx.exception))

    def test_non_pdf_bytes_raise(self):
        with self.assertRaises(PdfPlanError):
            pdf_to_png(b"not a pdf")

    def test_output_is_deterministic(self):
        raw = fx.pdf_bytes()
        self.assertEqual(pdf_to_png(raw), pdf_to_png(raw))


if __name__ == "__main__":
    unittest.main()
