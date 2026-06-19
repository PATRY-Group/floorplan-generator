"""
render.py — prims + config -> (svg, png, meta). The authoritative artifact.

Validates the rendered *output*: a well-formed SVG of the right page, a real
PNG, the coordinate transform that the drag-to-fix overlay depends on, label
placement (override vs auto-search), palette application, watermark behaviour,
XML escaping, and the bare 'plan_only' export path.
"""
import re
import unittest
import xml.etree.ElementTree as ET

import fixtures as fx
from engine import render
from engine.render import PAGE_W, PAGE_H, DEFAULT_PALETTE


def parse_unit_prims():
    import os
    path = fx.write_temp_dxf()
    try:
        from engine import parse_dxf
        return parse_dxf(path)["prims"]
    finally:
        os.remove(path)


class RenderOutputTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.prims = parse_unit_prims()

    def render(self, **cfg):
        return render(self.prims, fx.base_render_config(**cfg))

    def test_svg_is_well_formed_full_page(self):
        svg, png, meta = self.render()
        root = ET.fromstring(svg)            # raises if malformed
        self.assertTrue(root.tag.endswith("svg"))
        self.assertEqual(root.get("viewBox"), f"0 0 {PAGE_W} {PAGE_H}")
        self.assertEqual(meta["page"], {"w": PAGE_W, "h": PAGE_H})

    def test_png_is_a_real_png(self):
        _, png, _ = self.render()
        self.assertEqual(png[:8], b"\x89PNG\r\n\x1a\n")

    def test_transform_roundtrip_contract(self):
        """meta.transform is the SVG<->DXF contract LabelOverlay relies on:
        svgX = tx + dxfX*s, svgY = ty - dxfY*s. An overridden room must land
        exactly there."""
        _, _, meta = self.render(rooms=[{"name": "BEDROOM", "x": 10, "y": 7}])
        t = meta["transform"]
        place = meta["placements"][0]
        self.assertAlmostEqual(place["px"], t["tx"] + 10 * t["s"], places=1)
        self.assertAlmostEqual(place["py"], t["ty"] - 7 * t["s"], places=1)
        self.assertTrue(place["overridden"])

    def test_auto_placement_stays_inside_room_rect(self):
        """A room without an x/y override is auto-placed by the occupancy
        search; the result must fall within the room's search rectangle (in
        SVG coords) and be marked not-overridden."""
        rect = [2, 8, 2, 6]   # dxf: x in [2,8], y in [2,6]
        _, _, meta = self.render(rooms=[{"name": "KITCHEN", "rect": rect}])
        t, place = meta["transform"], meta["placements"][0]
        x_lo, x_hi = t["tx"] + 2 * t["s"], t["tx"] + 8 * t["s"]
        y_lo, y_hi = t["ty"] - 6 * t["s"], t["ty"] - 2 * t["s"]
        self.assertTrue(x_lo <= place["px"] <= x_hi)
        self.assertTrue(y_lo <= place["py"] <= y_hi)
        self.assertFalse(place["overridden"])

    def test_room_name_is_uppercased_in_output(self):
        svg, _, _ = self.render(rooms=[{"name": "bedroom", "x": 10, "y": 7}])
        self.assertIn("BEDROOM", svg)
        self.assertNotIn(">bedroom<", svg)

    def test_show_dims_gates_the_dimension_line(self):
        shown, _, _ = self.render(
            rooms=[{"name": "KITCHEN", "x": 10, "y": 7,
                    "dims": "10 x 8", "show_dims": True}])
        hidden, _, _ = self.render(
            rooms=[{"name": "KITCHEN", "x": 10, "y": 7,
                    "dims": "10 x 8", "show_dims": False}])
        self.assertIn("10 x 8", shown)
        self.assertNotIn("10 x 8", hidden)

    def test_default_palette_and_chrome(self):
        """With no palette/header overrides, the page paints in the default
        palette and stamps the default header/disclaimer text."""
        svg, _, _ = self.render(metadata={"title": "T"})
        self.assertIn(DEFAULT_PALETTE["light"], svg)   # page background
        self.assertIn("FLOOR PLAN", svg)               # default header_right
        self.assertIn("FOR ILLUSTRATIVE PURPOSES ONLY", svg)

    def test_custom_palette_is_applied(self):
        svg, _, _ = self.render(palette={"light": "#ABCDEF"})
        self.assertIn("#ABCDEF", svg)

    def test_xml_escaping_of_metadata(self):
        """User text with XML metacharacters must be escaped, and the document
        must remain parseable."""
        svg, _, _ = self.render(metadata={"title": "A & B <C>",
                                          "property_name": "X & Y"})
        ET.fromstring(svg)                  # still well-formed
        self.assertIn("&amp;", svg)
        self.assertIn("&lt;C&gt;", svg)
        self.assertNotIn("<C>", svg)


class WatermarkTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.prims = parse_unit_prims()

    def _wm_font_size(self, svg):
        m = re.search(r'font-size="(\d+)"[^>]*fill-opacity="0\.07"', svg)
        return int(m.group(1)) if m else None

    def test_text_watermark_scales_down_when_long(self):
        """A longer text mark must shrink so it fits the page width instead of
        overflowing the fixed 430px size (the '2274' case in the spec)."""
        short, _, _ = render(self.prims, fx.base_render_config(
            metadata={"title": "T", "watermark": "8"}))
        long, _, _ = render(self.prims, fx.base_render_config(
            metadata={"title": "T", "watermark": "1234567890"}))
        self.assertEqual(self._wm_font_size(short), 430)      # min(430, 1500/1)
        self.assertEqual(self._wm_font_size(long), 150)       # 1500/10
        self.assertLess(self._wm_font_size(long), self._wm_font_size(short))

    def test_watermark_image_replaces_text_mark(self):
        data_uri = "data:image/png;base64,AAAA"
        svg, _, _ = render(self.prims, fx.base_render_config(
            metadata={"title": "T", "watermark": "800",
                      "watermark_image": data_uri}))
        self.assertIn('opacity="0.08"', svg)        # the ghost image
        self.assertIn(data_uri, svg)
        self.assertIsNone(self._wm_font_size(svg))  # no text watermark emitted

    def test_sold_out_stamp_toggles_on_the_flag(self):
        """The SOLD OUT stamp appears only when the per-sheet flag is set, and
        the document stays well-formed with it."""
        off, _, _ = render(self.prims, fx.base_render_config(
            metadata={"title": "T"}))
        on, _, _ = render(self.prims, fx.base_render_config(
            metadata={"title": "T", "sold_out": True}))
        self.assertNotIn("SOLD OUT", off)
        self.assertIn("SOLD OUT", on)
        ET.fromstring(on)                            # still parseable


class PlanOnlyTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.prims = parse_unit_prims()

    def test_bare_export_has_no_page_chrome(self):
        cfg = fx.base_render_config(plan_only=True,
                                    rooms=[{"name": "BEDROOM", "x": 10, "y": 7}])
        svg, png, meta = render(self.prims, cfg)
        ET.fromstring(svg)
        self.assertTrue(meta.get("plan_only"))
        self.assertEqual(png[:8], b"\x89PNG\r\n\x1a\n")
        # cropped viewBox, not the fixed full page
        self.assertNotEqual(ET.fromstring(svg).get("viewBox"),
                            f"0 0 {PAGE_W} {PAGE_H}")
        # no header/footer band text, but the room label survives
        self.assertNotIn("FOR ILLUSTRATIVE PURPOSES ONLY", svg)
        self.assertIn("BEDROOM", svg)


if __name__ == "__main__":
    unittest.main()
