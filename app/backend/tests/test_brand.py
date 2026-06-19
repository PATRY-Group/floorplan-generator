"""
brand.py — extract a palette (+ PDF font hints) from a brand file.

Validates the *extraction output*: role assignment by luminance/chroma, the
swatch list contract, the accent-gating that keeps a near-black chromatic tone
from masquerading as the accent, font-name cleaning, and error handling.
"""
import unittest

import fixtures as fx
from engine import extract_brand, BrandError
from engine.brand import _clean_font_name, _assign_roles


class PaletteRoleTest(unittest.TestCase):
    def test_roles_by_luminance_and_chroma(self):
        png = fx.brand_image_png([("#101010", 60), ("#F5F0E8", 60),
                                  ("#C8421A", 50), ("#888080", 40)])
        out = extract_brand(png, "brand.png")
        self.assertEqual(out["source"], "image")
        self.assertEqual(out["palette"]["dark"], "#101010")    # darkest
        self.assertEqual(out["palette"]["light"], "#F5F0E8")   # lightest
        self.assertEqual(out["palette"]["accent"], "#C8421A")  # most chroma
        self.assertEqual(out["fonts"], [])                     # images: no fonts

    def test_swatches_are_sorted_and_well_formed(self):
        png = fx.brand_image_png([("#101010", 60), ("#F5F0E8", 40),
                                  ("#C8421A", 20)])
        sw = extract_brand(png, "b.png")["swatches"]
        fracs = [s["frac"] for s in sw]
        self.assertEqual(fracs, sorted(fracs, reverse=True))
        for s in sw:
            self.assertRegex(s["hex"], r"^#[0-9A-F]{6}$")
            self.assertTrue(0 <= s["luminance"] <= 1)
            self.assertTrue(0 <= s["chroma"] <= 1)

    def test_accent_gating_skips_near_black_chromatic(self):
        """A very dark but chromatic tone (high HSV saturation, low luminance)
        must not be chosen as the accent when a usable mid-tone exists."""
        swatches = [
            {"hex": "#101010", "frac": 0.4, "luminance": 0.06, "chroma": 0.0},
            {"hex": "#F5F0E8", "frac": 0.3, "luminance": 0.95, "chroma": 0.04},
            {"hex": "#2A0A00", "frac": 0.2, "luminance": 0.06, "chroma": 0.16},
            {"hex": "#C8421A", "frac": 0.1, "luminance": 0.36, "chroma": 0.69},
        ]
        roles = _assign_roles(swatches)
        self.assertEqual(roles["accent"], "#C8421A")
        self.assertNotEqual(roles["accent"], "#2A0A00")


class FontNameTest(unittest.TestCase):
    def test_strips_pdf_subset_prefix(self):
        self.assertEqual(_clean_font_name("ABCDEF+HelveticaNeue-Bold"),
                         "HelveticaNeue-Bold")
        self.assertEqual(_clean_font_name("Georgia"), "Georgia")
        self.assertEqual(_clean_font_name(""), "")


class BrandErrorTest(unittest.TestCase):
    def test_empty_file_rejected(self):
        with self.assertRaises(BrandError):
            extract_brand(b"", "x.png")

    def test_garbage_image_rejected(self):
        with self.assertRaises(BrandError):
            extract_brand(b"not an image", "x.png")


if __name__ == "__main__":
    unittest.main()
