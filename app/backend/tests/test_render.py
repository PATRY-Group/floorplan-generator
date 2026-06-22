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

import numpy as np

import fixtures as fx
from engine import render, DEFAULT_LAYER_MAP
from engine.render import PAGE_W, PAGE_H, DEFAULT_PALETTE
from engine.keyplan_trace import solidify_walls


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
        long_sz, short_sz = self._wm_font_size(long), self._wm_font_size(short)
        assert long_sz is not None and short_sz is not None   # narrow Optional[int]
        self.assertEqual(short_sz, 430)      # min(430, 1500/1)
        self.assertEqual(long_sz, 150)       # 1500/10
        self.assertLess(long_sz, short_sz)

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


class SolidifyWallsTest(unittest.TestCase):
    """The core poché synthesis: two parallel wall faces (linework) become one
    solid filled band, while a wide room gap is left empty."""

    def test_close_bridges_the_gap_between_two_faces(self):
        m = np.zeros((40, 40), bool)
        m[10:30, 10] = True          # left face
        m[10:30, 16] = True          # right face — a 6px cavity between them
        band = solidify_walls(m, close_k=9, speckle=0, smooth=0)
        self.assertTrue(band[20, 13])           # midpoint of the wall is filled
        self.assertTrue(band[20, 10] and band[20, 16])  # faces still solid

    def test_room_sized_gap_is_not_filled(self):
        m = np.zeros((60, 60), bool)
        m[10:50, 10] = True          # two faces 40px apart — a room, not a wall
        m[10:50, 50] = True
        band = solidify_walls(m, close_k=9, speckle=0, smooth=0)
        self.assertFalse(band[30, 30])          # the room interior stays empty


class PocheSynthesisTest(unittest.TestCase):
    """Solid wall poché synthesized from linework when a DXF carries no wall
    HATCH (plain-AutoCAD / CloudConvert exports), gated so hatch files are
    untouched."""

    LAYER_MAP = {"wall_line": ["A_WALL_FULL_N"], "wall_fill": ["A_WALL_CAVITY"]}

    def _linework_prims(self):
        # two parallel faces of a wall ring on line layers — no hatch anywhere
        outer = [(0, 0), (20, 0), (20, 15), (0, 15), (0, 0)]
        inner = [(0.5, 0.5), (19.5, 0.5), (19.5, 14.5), (0.5, 14.5), (0.5, 0.5)]
        return [["A_WALL_FULL_N", "line", outer, ""],
                ["A_WALL_CAVITY", "line", inner, ""]]

    # Poché is now opt-in (skinny is the default), so these pass wall_style solid.
    SOLID = {"title": "2 BED", "wall_style": "solid"}

    def test_solid_linework_walls_get_a_synthesized_poche_image(self):
        cfg = fx.base_render_config(layer_map=self.LAYER_MAP, metadata=self.SOLID)
        svg, png, _ = render(self._linework_prims(), cfg)
        ET.fromstring(svg)                              # well-formed
        self.assertIn("<image", svg)                   # poché overlay emitted
        self.assertIn("data:image/png;base64,", svg)
        self.assertEqual(png[:8], b"\x89PNG\r\n\x1a\n")
        # no hatch -> the vector wall_fills path is empty (fill comes from the image)
        self.assertIn('<path d="" fill=', svg)

    def test_poche_can_be_disabled(self):
        cfg = fx.base_render_config(layer_map=self.LAYER_MAP, metadata=self.SOLID,
                                    synthesize_poche=False)
        svg, _, _ = render(self._linework_prims(), cfg)
        self.assertNotIn("<image", svg)

    def test_solid_hatch_file_is_left_untouched(self):
        """The load-bearing gate: in solid mode a file with a real wall HATCH
        keeps its vector poché and gets NO synthesized raster image."""
        prims = parse_unit_prims()        # build_unit_dxf draws an A-WALL-PATT hatch
        # the default Revit map maps A-WALL-PATT -> wall_fill, so the hatch fills
        svg, _, _ = render(prims, fx.base_render_config(
            layer_map=DEFAULT_LAYER_MAP, metadata=self.SOLID))
        self.assertNotIn("<image", svg)               # synthesis never fired
        self.assertNotIn('<path d="" fill=', svg)     # the hatch rendered as fill

    def test_solid_plan_only_export_also_synthesizes(self):
        cfg = fx.base_render_config(layer_map=self.LAYER_MAP,
                                    metadata=self.SOLID, plan_only=True)
        svg, _, _ = render(self._linework_prims(), cfg)
        ET.fromstring(svg)
        self.assertIn("<image", svg)

    def test_skinny_style_draws_thin_outlines_no_fill(self):
        """metadata.wall_style == 'skinny' -> both wall faces as thin (0.8)
        outlines, no poché image and no solid fill path."""
        cfg = fx.base_render_config(
            layer_map=self.LAYER_MAP,
            metadata={"title": "2 BED", "wall_style": "skinny"})
        svg, _, _ = render(self._linework_prims(), cfg)
        ET.fromstring(svg)
        self.assertNotIn("<image", svg)              # no synthesized fill
        self.assertIn('stroke-width="0.8"', svg)     # skinny outline weight
        self.assertIn('<path d="" fill=', svg)       # wall_fills suppressed

    def test_default_style_is_skinny(self):
        """With no wall_style, the default is now skinny — thin outlines, no
        poché image; solid is opt-in."""
        cfg = fx.base_render_config(
            layer_map=self.LAYER_MAP, metadata={"title": "2 BED"})
        svg, _, _ = render(self._linework_prims(), cfg)
        self.assertNotIn("<image", svg)
        self.assertIn('stroke-width="0.8"', svg)

    def test_skinny_suppresses_a_hatch_fill(self):
        """Skinny on a real-hatch file drops the solid poché too (no fill path)."""
        prims = parse_unit_prims()        # build_unit_dxf draws an A-WALL-PATT hatch
        cfg = fx.base_render_config(
            layer_map=DEFAULT_LAYER_MAP,
            metadata={"title": "2 BED", "wall_style": "skinny"})
        svg, _, _ = render(prims, cfg)
        self.assertNotIn("<image", svg)
        self.assertIn('<path d="" fill=', svg)       # hatch fill suppressed


if __name__ == "__main__":
    unittest.main()
