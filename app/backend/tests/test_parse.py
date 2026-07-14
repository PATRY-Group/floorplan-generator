"""
parse.py — DXF -> prims + seeded labels + suggestions.

Validates the *parse output* contract: the `prims` shape the renderer depends
on, which entities are kept vs dropped, room-label classification, the
metadata suggestions, dimension estimation (and its deliberate refusals), and
the guards that reject un-usable files.
"""
import os
import unittest

import fixtures as fx
from engine import parse_dxf, ParseError
from engine.parse import (_estimate_dims, _cap_points, _clean_text,
                          _looks_like_room, _area_to_sf, _is_furniture,
                          MAX_PTS_PER_ENTITY)


class ParseGeometryTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.path = fx.write_temp_dxf()
        cls.result = parse_dxf(cls.path)

    @classmethod
    def tearDownClass(cls):
        os.remove(cls.path)

    def test_prims_shape_contract(self):
        """Every prim is [layer, kind, data, block] with the documented data
        shape: line -> list of (x,y) tuples; hatch -> list of polygons."""
        prims = self.result["prims"]
        self.assertTrue(prims)
        for layer, kind, data, block in prims:
            self.assertIsInstance(layer, str)
            self.assertIn(kind, ("line", "hatch"))
            if kind == "line":
                self.assertGreaterEqual(len(data), 2)
                self.assertEqual(len(data[0]), 2)          # (x, y)
            else:
                self.assertTrue(all(len(poly) >= 3 for poly in data))

    def test_furniture_block_is_dropped(self):
        """Loose furniture (block name matches FURNITURE_FRAGMENTS) never
        reaches the geometry, even though its sub-entity is on a wall layer."""
        blocks = {p[3] for p in self.result["prims"]}
        self.assertNotIn("SOFA-2SEAT", blocks)

    def test_drop_layer_excluded(self):
        """Entities on a 'drop' layer (A-AREA-IDEN) are filtered out at parse."""
        layers = {p[0] for p in self.result["prims"]}
        self.assertNotIn("A-AREA-IDEN", layers)
        self.assertIn("A-WALL", layers)

    def test_extents_from_wall_geometry(self):
        self.assertEqual(self.result["extents"],
                         {"minx": 0, "maxx": 20, "miny": 0, "maxy": 15})

    def test_only_real_rooms_are_seeded(self):
        """'BEDROOM' seeds a label; '2 BED' (a unit title) and the suite/sf/
        compass texts do not."""
        names = [l["name"] for l in self.result["labels"]]
        self.assertEqual(names, ["BEDROOM"])

    def test_seeded_label_fields(self):
        label = self.result["labels"][0]
        self.assertEqual(label["font_scale"], 1.0)
        self.assertTrue(label["show_dims"])
        # the search rect is clamped inside the plan extents
        l, r, b, t = label["rect"]
        self.assertGreaterEqual(l, 0)
        self.assertLessEqual(r, 20)
        self.assertGreaterEqual(b, 0)
        self.assertLessEqual(t, 15)

    def test_non_room_text_is_ignored_not_lost(self):
        ignored = {t["text"] for t in self.result["ignored_text"]}
        self.assertEqual(ignored, {"2 BED", "204", "650 SF", "NORTH"})

    def test_metadata_suggestions(self):
        self.assertEqual(self.result["suggestions"],
                         {"title": "2 BED", "suite": "204", "sf": "650 SF"})


class AreaSuggestionTest(unittest.TestCase):
    """Unit area is read by its content signature (a number + an area unit), so
    it works from a tag on ANY layer — including a 'drop' layer (A-AREA-IDEN)
    whose geometry/text is kept off the drawn sheet. Reading the tag for a
    suggestion is deliberately separate from rendering it."""

    def test_metric_tag_on_drop_layer_read_not_rendered(self):
        """A '48.0 m²' tag on the drop layer becomes the SF suggestion (517 SF),
        and never leaks into the drawn/re-addable text."""
        path = fx.write_temp_dxf(area_tag="48.0 m²", include_text=False)
        try:
            r = parse_dxf(path)
        finally:
            os.remove(path)
        self.assertEqual(r["suggestions"]["sf"], "517 SF")
        self.assertEqual(r["ignored_text"], [])    # tag not re-addable
        self.assertEqual(r["labels"], [])           # tag not seeded as a room
        self.assertNotIn("A-AREA-IDEN", {p[0] for p in r["prims"]})

    def test_visible_suite_wins_over_drop_layer_number(self):
        """A bare number on a drop layer (a column-grid / stair tag) must not
        preempt the real on-plan suite — drop text only fills an empty slot."""
        path = fx.write_temp_dxf(area_tag="12")     # stray number on A-AREA-IDEN
        try:
            r = parse_dxf(path)
        finally:
            os.remove(path)
        self.assertEqual(r["suggestions"]["suite"], "204")   # visible, not "12"

    def test_largest_area_wins_over_other_tags(self):
        """With the fixture's '650 SF' text present too, the larger drop-layer
        tag wins (a unit total beats incidental smaller numbers), and the tag
        still never lands in ignored_text."""
        path = fx.write_temp_dxf(area_tag="75.0 m²")   # 75 m² = 807 SF > 650
        try:
            r = parse_dxf(path)
        finally:
            os.remove(path)
        self.assertEqual(r["suggestions"]["sf"], "807 SF")
        ignored = {t["text"] for t in r["ignored_text"]}
        self.assertEqual(ignored, {"2 BED", "204", "650 SF", "NORTH"})


class AreaToSfTest(unittest.TestCase):
    """_area_to_sf is the pure content-detector: metric m² is converted to SF,
    imperial SF/SQFT is taken as-is, and anything without an area unit (a bare
    suite number, a linear metre reading) is refused."""

    def test_recognised_forms(self):
        self.assertEqual(_area_to_sf("48.0 m²"), 517)      # metric, U+00B2
        self.assertEqual(_area_to_sf("48 m2"), 517)             # ascii fallback
        self.assertEqual(_area_to_sf("650 SF"), 650)
        self.assertEqual(_area_to_sf("1,175.0 SQ.FT."), 1175)   # comma + decimal
        self.assertEqual(_area_to_sf("517 S.F."), 517)

    def test_refusals(self):
        self.assertIsNone(_area_to_sf("202"))        # suite, not area
        self.assertIsNone(_area_to_sf("1 BED - 1A")) # title, not area
        self.assertIsNone(_area_to_sf("2.7 m"))      # linear metres, not area
        self.assertIsNone(_area_to_sf("3m2"))        # below plausible floor


class ParseRejectionTest(unittest.TestCase):
    def test_sheet_export_rejected(self):
        path = fx.write_temp_dxf(fx.build_sheet_dxf)
        try:
            with self.assertRaises(ParseError):
                parse_dxf(path)
        finally:
            os.remove(path)

    def test_unreadable_file_rejected(self):
        with self.assertRaises(ParseError):
            parse_dxf(os.path.join(os.path.dirname(__file__), "does-not-exist.dxf"))


class FurnitureMatchTest(unittest.TestCase):
    """The furniture filter must match FURNITURE_FRAGMENTS as delimited tokens,
    not raw substrings — or a block-wrapped unit named '2BED-A_UNIT' (or a
    'BEDROOM' block) gets its whole subtree dropped, losing walls or triggering a
    spurious 'no wall geometry' ParseError."""

    def test_loose_furniture_is_matched(self):
        for name in ("SOFA", "BED", "BED-01", "M_SOFA", "TV-STAND",
                     "SOFA2", "CABINET-LOOSE", "DINING-TABLE"):
            self.assertTrue(_is_furniture(name), name)

    def test_unit_and_room_blocks_are_kept(self):
        for name in ("2BED-A_UNIT", "BEDROOM_2", "BEDROOM", "3BED", "TABLETOP-ROOM"):
            self.assertFalse(_is_furniture(name), name)


class TextClassificationTest(unittest.TestCase):
    """_looks_like_room / _clean_text are the pure heart of label seeding."""

    def test_room_vocab_matches(self):
        for txt in ("KITCHEN", "PRIMARY BEDROOM", "W.I.C.", "ENSUITE"):
            self.assertTrue(_looks_like_room(txt), txt)

    def test_titles_and_codes_and_equipment_rejected(self):
        for txt in ("2 BED", "1 BED - 1A", "2BR-204", "DW", "HWT", ""):
            self.assertFalse(_looks_like_room(txt), txt)

    def test_clean_text_strips_mtext_codes(self):
        self.assertEqual(_clean_text(r"\fArial|b1;LIVING\PROOM"), "LIVING ROOM")
        self.assertEqual(_clean_text(None), "")


class DimensionEstimateTest(unittest.TestCase):
    """_estimate_dims must produce a measurement only when it is trustworthy,
    and refuse (return None) in every doubtful case — the spec §10 contract."""

    def test_measures_a_bounded_room(self):
        segs = fx.box_segments(0, 0, 10, 8)
        self.assertEqual(_estimate_dims(segs, 5, 4, 1.0, 40, 40), "10'0\" x 8'0\"")

    def test_unitless_returns_none(self):
        segs = fx.box_segments(0, 0, 10, 8)
        self.assertIsNone(_estimate_dims(segs, 5, 4, None, 40, 40))

    def test_span_covering_most_of_plan_rejected(self):
        """A ray that escapes through an opening and runs to the far exterior
        wall (span > 85% of the plan) is not a real room measurement."""
        segs = fx.box_segments(0, 0, 38, 38)
        self.assertIsNone(_estimate_dims(segs, 19, 19, 1.0, 40, 40))

    def test_extreme_aspect_ratio_rejected(self):
        segs = fx.box_segments(0, 0, 30, 5)
        self.assertIsNone(_estimate_dims(segs, 15, 2.5, 1.0, 40, 40))


class CapPointsTest(unittest.TestCase):
    def test_downsamples_and_keeps_endpoints(self):
        pts = list(range(20_000))
        capped = _cap_points(pts)
        self.assertLessEqual(len(capped), MAX_PTS_PER_ENTITY)
        self.assertEqual(capped[0], pts[0])
        self.assertEqual(capped[-1], pts[-1])

    def test_short_run_unchanged(self):
        pts = [1, 2, 3]
        self.assertIs(_cap_points(pts), pts)


class PolylineGeometryTest(unittest.TestCase):
    """The parser lists ('LWPOLYLINE', 'POLYLINE') as supported. Polyline walls
    must survive into prims — a closed 4-segment wall ring flattens to >= 5
    points (the ring is closed).

    Regression guard: LWPolyline/Polyline2d have no usable .flattening() in some
    ezdxf versions (incl. 1.4.4 here); the parser routes them through
    ezpath.make_path(entity).flattening(...) instead. Before that fix the
    AttributeError was swallowed by _collect_entities' bare `except` and the
    geometry was silently dropped (a polyline-only file even looked like an
    empty sheet export). See tests/README.md.
    """

    def _wall_point_count(self, wall_kind):
        path = fx.write_temp_dxf(wall_kind=wall_kind, include_furniture=False)
        try:
            prims = parse_dxf(path)["prims"]
        finally:
            os.remove(path)
        return sum(len(p[2]) for p in prims
                   if p[0] == "A-WALL" and p[1] == "line")

    def test_lwpolyline_walls_preserved(self):
        self.assertGreaterEqual(self._wall_point_count("lwpolyline"), 5)

    def test_polyline_walls_preserved(self):
        self.assertGreaterEqual(self._wall_point_count("polyline"), 5)


if __name__ == "__main__":
    unittest.main()
