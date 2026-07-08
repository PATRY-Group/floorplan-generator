"""
render.py — prims + config -> (svg, png, meta). The authoritative artifact.

Validates the rendered *output*: a well-formed SVG of the right page, a real
PNG, the coordinate transform that the drag-to-fix overlay depends on, label
placement (override vs auto-search), palette application, watermark behaviour,
XML escaping, and the bare 'plan_only' export path.
"""
import base64
import io
import os
import re
import unittest
import xml.etree.ElementTree as ET

import numpy as np
from PIL import Image

import fixtures as fx
from engine import render, render_image_plan, DEFAULT_LAYER_MAP
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

    def test_malicious_palette_color_cannot_break_out(self):
        """A non-#hex palette value (an attribute-breakout attempt) is dropped to
        the default rather than emitted raw into a fill=/stroke= attribute."""
        evil = '#fff" onload="alert(1)'
        svg, _, _ = self.render(palette={"light": evil})
        ET.fromstring(svg)                              # still well-formed
        self.assertNotIn("onload", svg)
        self.assertNotIn(evil, svg)
        self.assertIn(DEFAULT_PALETTE["light"], svg)    # fell back to the default

    def test_malicious_image_data_uri_is_dropped(self):
        """paint_image / watermark_image that aren't clean base64 image data URIs
        are omitted, not interpolated into an href where they could add handlers."""
        evil = 'x" onerror="alert(1)'
        svg, _, _ = self.render(metadata={"title": "T", "watermark_image": evil},
                                paint_image=evil)
        ET.fromstring(svg)
        self.assertNotIn("onerror", svg)
        self.assertNotIn(evil, svg)

    def test_valid_image_data_uri_still_embeds(self):
        """The sanitizer must not reject legitimate base64 image data URIs."""
        uri = "data:image/png;base64,AAAA"
        svg, _, _ = self.render(metadata={"title": "T", "watermark_image": uri})
        self.assertIn(uri, svg)


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

    def test_hide_watermark_suppresses_the_mark(self):
        """The per-sheet hide_watermark flag drops the ghost mark (text or
        image) so manual paint doesn't clash with it."""
        text, _, _ = render(self.prims, fx.base_render_config(
            metadata={"title": "T", "watermark": "800", "hide_watermark": True}))
        self.assertIsNone(self._wm_font_size(text))      # no text mark
        img_uri = "data:image/png;base64,AAAA"
        image, _, _ = render(self.prims, fx.base_render_config(
            metadata={"title": "T", "watermark_image": img_uri,
                      "hide_watermark": True}))
        self.assertNotIn(img_uri, image)                 # no image mark either


class LogoInHeaderTest(unittest.TestCase):
    """The uploaded watermark logo can optionally double as the header mark
    (property-level 'logo_in_header' brand choice) — same image, two spots."""

    @classmethod
    def setUpClass(cls):
        cls.prims = parse_unit_prims()
        buf = io.BytesIO()
        Image.new("RGB", (120, 60), (200, 50, 50)).save(buf, "PNG")
        cls.logo_uri = "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()

    def test_default_is_text_lockup(self):
        svg, _, _ = render(self.prims, fx.base_render_config(
            metadata={"title": "T", "lockup": "300", "watermark_image": self.logo_uri}))
        self.assertIn(">300<", svg)

    def test_flag_swaps_header_text_for_the_logo_image(self):
        on, _, _ = render(self.prims, fx.base_render_config(
            metadata={"title": "T", "lockup": "300", "watermark_image": self.logo_uri,
                      "logo_in_header": True}))
        off, _, _ = render(self.prims, fx.base_render_config(
            metadata={"title": "T", "lockup": "300", "watermark_image": self.logo_uri,
                      "logo_in_header": False}))
        self.assertNotIn(">300<", on)          # text lockup replaced
        self.assertIn(">300<", off)            # default path unaffected
        # same uploaded image used in both the header and the ghost watermark
        self.assertEqual(on.count(self.logo_uri), 2)
        ET.fromstring(on)                      # still well-formed

    def test_flag_without_an_image_falls_back_to_text(self):
        """No watermark_image uploaded -> the flag is a no-op, not a crash."""
        svg, _, _ = render(self.prims, fx.base_render_config(
            metadata={"title": "T", "lockup": "300", "logo_in_header": True}))
        self.assertIn(">300<", svg)

    def test_shared_with_render_image_plan(self):
        """render_image_plan() uses the same _brand_chrome() helper, so an
        image-sourced sheet gets identical header-logo behaviour."""
        svg, _, _ = render_image_plan(fx.plate_png(), fx.base_render_config(
            metadata={"title": "T", "lockup": "300", "watermark_image": self.logo_uri,
                      "logo_in_header": True}))
        self.assertNotIn(">300<", svg)
        self.assertEqual(svg.count(self.logo_uri), 2)


class PaintLayerTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.prims = parse_unit_prims()

    def test_paint_image_is_embedded_when_present(self):
        uri = "data:image/png;base64,PAINT"
        with_paint, _, _ = render(self.prims, fx.base_render_config(paint_image=uri))
        without, _, _ = render(self.prims, fx.base_render_config())
        self.assertIn(uri, with_paint)                   # baked in as an <image>
        self.assertNotIn(uri, without)
        ET.fromstring(with_paint)                        # still well-formed

    def test_plan_only_export_includes_paint(self):
        """The plan-only crop shares the page coordinate space, so paint that
        covers quirks in the geometry is baked into the bare export too."""
        uri = "data:image/png;base64,PAINT"
        cfg = fx.base_render_config(paint_image=uri)
        cfg["plan_only"] = True
        svg, _, _ = render(self.prims, cfg)
        self.assertIn(uri, svg)


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


class RenderImagePlanTest(unittest.TestCase):
    """render_image_plan wraps an already-finished raster plan (e.g. a
    rasterized PDF page) in the same branded header/footer frame as render(),
    via the shared _brand_chrome() — no walls, no room labels, one embedded
    image instead."""

    def test_full_sheet_has_chrome_and_embedded_image(self):
        svg, png, meta = render_image_plan(fx.plate_png(), fx.base_render_config())
        root = ET.fromstring(svg)                     # well-formed
        self.assertEqual(root.get("viewBox"), f"0 0 {PAGE_W} {PAGE_H}")
        self.assertEqual(meta["page"], {"w": PAGE_W, "h": PAGE_H})
        self.assertEqual(png[:8], b"\x89PNG\r\n\x1a\n")
        self.assertIn("<image", svg)
        self.assertIn("data:image/png;base64,", svg)
        self.assertIn("2 BED", svg)                    # footer title from base_render_config
        self.assertIn("FOR ILLUSTRATIVE PURPOSES ONLY", svg)
        # the embedded plan area carries no border rect (unlike the footer mini-plate)
        self.assertNotIn('stroke-width="1.1"', svg)

    def test_plan_only_export_is_bare_image_no_chrome(self):
        cfg = fx.base_render_config(plan_only=True)
        svg, png, meta = render_image_plan(fx.plate_png(size=(200, 150)), cfg)
        ET.fromstring(svg)
        self.assertTrue(meta.get("plan_only"))
        self.assertEqual(meta["page"], {"w": 200, "h": 150})
        self.assertEqual(png[:8], b"\x89PNG\r\n\x1a\n")
        self.assertNotIn("FOR ILLUSTRATIVE PURPOSES ONLY", svg)
        self.assertNotIn("2 BED", svg)

    def test_watermark_and_sold_out_shared_with_render(self):
        """The chrome helper is shared with render(), so brand behaviour
        (watermark, sold-out stamp) is identical for an image-sourced plan."""
        svg, _, _ = render_image_plan(fx.plate_png(), fx.base_render_config(
            metadata={"title": "T", "watermark": "800", "sold_out": True}))
        self.assertIn("SOLD OUT", svg)
        self.assertIn('fill-opacity="0.07"', svg)      # text watermark mark

    def test_paint_layer_is_embedded(self):
        uri = "data:image/png;base64,PAINT"
        svg, _, _ = render_image_plan(fx.plate_png(),
                                      fx.base_render_config(paint_image=uri))
        self.assertIn(uri, svg)

    def test_aspect_fit_matches_render_scale_formula(self):
        """A portrait image and a landscape image both land within the same
        PLAN_MAX_W/PLAN_MAX_H box render() uses — this is what keeps an
        image-sourced sheet visually consistent with a DXF-sourced one."""
        from engine.render import PLAN_MAX_W, PLAN_MAX_H
        wide_svg, _, _ = render_image_plan(fx.plate_png(size=(800, 100)),
                                           fx.base_render_config())
        tall_svg, _, _ = render_image_plan(fx.plate_png(size=(100, 800)),
                                           fx.base_render_config())
        for svg in (wide_svg, tall_svg):
            m = re.search(r'<image href="data:image/png[^"]+" x="([\d.]+)" '
                          r'y="([\d.]+)" width="([\d.]+)" height="([\d.]+)"', svg)
            self.assertIsNotNone(m)
            w, h = float(m.group(3)), float(m.group(4))
            self.assertLessEqual(round(w), PLAN_MAX_W)
            self.assertLessEqual(round(h), PLAN_MAX_H)


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


class FontlessHostPngTest(unittest.TestCase):
    """The most-cited prod footgun (CLAUDE.md): on a fontless serverless host
    resvg draws no text unless the bundled fallback fonts are supplied. We
    simulate that host with skip_system_fonts=True (on dev, system fonts would
    otherwise mask the bug — see memory 'resvg-font-test-technique') and assert
    the bundled fonts make text actually rasterize, while their absence is blank.

    render_png() always supplies the bundled fonts, so this asserts the
    load-bearing fallback rather than calling render_png directly."""

    SVG = ('<svg xmlns="http://www.w3.org/2000/svg" width="240" height="80" '
           'viewBox="0 0 240 80"><rect width="240" height="80" fill="#ffffff"/>'
           '<text x="12" y="56" font-family="Helvetica, Arial, sans-serif" '
           'font-size="40" fill="#000000">ABCDEF</text></svg>')

    def _dark_px(self, png_bytes):
        arr = np.asarray(Image.open(io.BytesIO(png_bytes)).convert("L"))
        return int((arr < 128).sum())

    def _render(self, with_fonts):
        import resvg_py
        from engine.render import (_BUNDLED_FONT_FILES, _BUNDLED_SERIF_FAMILY,
                                    _BUNDLED_SANS_FAMILY)
        fonts = [f for f in _BUNDLED_FONT_FILES if os.path.exists(f)] if with_fonts else None
        return bytes(resvg_py.svg_to_bytes(
            svg_string=self.SVG, width=240, skip_system_fonts=True, font_files=fonts,
            serif_family=_BUNDLED_SERIF_FAMILY, sans_serif_family=_BUNDLED_SANS_FAMILY,
            font_family=_BUNDLED_SANS_FAMILY))

    def test_bundled_fonts_render_text_with_no_system_fonts(self):
        self.assertGreater(self._dark_px(self._render(with_fonts=True)), 500)

    def test_without_any_fonts_text_is_blank(self):
        # confirms the bundled fonts are what's load-bearing, not a system fallback
        self.assertLess(self._dark_px(self._render(with_fonts=False)), 50)


class PlanExtentsTest(unittest.TestCase):
    """A3: geometry that extends past the wall envelope (a door swing, balcony)
    must not be clipped by the plan_only crop. Scale/centering stay wall-based, so
    the main sheet's extents remain wall-only."""

    # walls in a 0..10 box; a door line reaching x=13 — 3 units past the wall
    PRIMS = [["A-WALL", "line", [(0, 0), (10, 0), (10, 10), (0, 10), (0, 0)], ""],
             ["A-DOOR", "line", [(10, 5), (13, 5)], ""]]

    def test_plan_only_extents_cover_geometry_beyond_walls(self):
        _, _, meta = render(self.PRIMS, fx.base_render_config(
            layer_map=DEFAULT_LAYER_MAP, plan_only=True))
        self.assertGreaterEqual(meta["extents"]["maxx"], 13)   # door swing included

    def test_main_sheet_extents_stay_wall_only(self):
        _, _, meta = render(self.PRIMS, fx.base_render_config(
            layer_map=DEFAULT_LAYER_MAP, metadata={"title": "T"}))
        # the full sheet still scales/anchors to the walls (byte-identical output)
        self.assertAlmostEqual(meta["extents"]["maxx"], 10, places=3)


if __name__ == "__main__":
    unittest.main()
