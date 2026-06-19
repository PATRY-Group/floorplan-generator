"""
layers.py — layer-role auto-detection (infer_layer_map).

The pipeline's default layer_map matches the Revit export scheme. Files drafted
in plain AutoCAD use their own layer names, so the default map finds no walls and
parse_dxf rejects them. infer_layer_map guesses the roles from layer names + the
text content. This validates:

  - the *auto-produced* map actually parses the three real problem files (the
    real acceptance criterion — a hand-authored map proving nothing);
  - on a clean Revit-scheme file, inference agrees with the defaults (no
    regression — same labels out);
  - role priority: a text layer carrying room words is kept as room_label, never
    swept into 'drop'.

The real-file tests skip cleanly when the sample DXFs aren't present, so the
suite stays hermetic on machines/CI that don't have them. The synthetic test is
self-contained (builds its own DXF in-memory) and always runs.
"""
import asyncio
import io
import json
import os
import shutil
import tempfile
import unittest

import ezdxf
from fastapi import HTTPException, UploadFile

import main
from main import parse as parse_endpoint
from engine import parse_dxf
from engine.parse import DEFAULT_LAYER_MAP
from engine.layers import infer_layer_map, _classify, _norm


# Repo layout: this file is .../program/app/backend/tests/test_layers.py and the
# sample DXFs live at <repo>/dxf/... (four levels up, outside the git root).
_DXF_DIR = os.path.normpath(os.path.join(
    os.path.dirname(__file__), "..", "..", "..", "..", "dxf"))
ARMSTRONG = os.path.join(_DXF_DIR, "539 armstrong", "Armstrong - Unit 2E.dxf")
A12 = os.path.join(_DXF_DIR, "539 armstrong", "A12_ENLARGED UNITS.dxf")
PRINCESS_1A = os.path.join(
    _DXF_DIR, "800 princess",
    "800 Princess Street - Phase II (6 storey proposal)+rear extension - "
    "Floor Plan - Unit 1A - Marketing.dxf")
PRINCESS_SHEET = os.path.join(
    _DXF_DIR, "800 princess",
    "800 Princess Street - Phase II (6 storey proposal)+rear extension - "
    "A100 PLANS - Sheet - A101B - GROUND FLOOR PLAN.dxf")


# --------------------------------------------------------------------------- #
# Real-file inference (the feature's true acceptance test)
# --------------------------------------------------------------------------- #
class RealFileInferenceTest(unittest.TestCase):
    """Feed infer_layer_map the raw doc, then parse with the *inferred* map."""

    @unittest.skipUnless(os.path.exists(ARMSTRONG), "Armstrong sample DXF absent")
    def test_armstrong_autodetects_and_labels(self):
        doc = ezdxf.readfile(ARMSTRONG)
        lm, report = infer_layer_map(doc)
        # walls/text detected by name + content, not the Revit defaults
        self.assertIn("A_WALL_FULL_N", lm["wall_line"])
        self.assertEqual(lm["room_label"], ["A_TEXT_BLOWUPS"])
        # the auto map turns a previously-rejected file into a labeled sheet
        res = parse_dxf(ARMSTRONG, layer_map=lm)
        self.assertTrue(res["extents"])
        self.assertGreaterEqual(len(res["labels"]), 8)

    @unittest.skipUnless(os.path.exists(A12), "A12 sample DXF absent")
    def test_a12_multiunit_parses(self):
        # A12 is the deferred multi-unit case (see MULTI_UNIT_SPLIT_TODO.md): it
        # must at least parse to a combined sheet, not raise.
        doc = ezdxf.readfile(A12)
        lm, _ = infer_layer_map(doc)
        res = parse_dxf(A12, layer_map=lm)
        self.assertGreater(len(res["prims"]), 100)

    @unittest.skipUnless(os.path.exists(PRINCESS_1A), "800 Princess sample DXF absent")
    def test_revit_file_inference_agrees_with_defaults(self):
        """On a clean Revit-scheme file, inference must reproduce the default
        roles and the same labels — the no-regression guard."""
        doc = ezdxf.readfile(PRINCESS_1A)
        lm, _ = infer_layer_map(doc)
        self.assertIn("A-WALL", lm["wall_line"])
        self.assertIn("I-WALL", lm["wall_line"])
        self.assertEqual(lm["room_label"], ["G-ANNO-TEXT"])
        # same room labels whether parsed with the inferred map or the defaults
        names_inferred = [l["name"] for l in parse_dxf(PRINCESS_1A, layer_map=lm)["labels"]]
        names_default = [l["name"] for l in
                         parse_dxf(PRINCESS_1A, layer_map=DEFAULT_LAYER_MAP)["labels"]]
        self.assertEqual(names_inferred, names_default)

    @unittest.skipUnless(os.path.exists(PRINCESS_SHEET), "800 Princess sheet export absent")
    def test_sheet_export_still_rejected_after_inference(self):
        """A real SHEET export must stay rejected even through inference — the
        fallback wall-layer guess must not turn a titleblock into a fake plan
        (CLAUDE.md's documented sheet-rejection guarantee). Real sheets keep
        their content in paperspace, so modelspace has no wall geometry."""
        from engine import ParseError
        doc = ezdxf.readfile(PRINCESS_SHEET)
        lm, _ = infer_layer_map(doc)
        with self.assertRaises(ParseError):
            parse_dxf(PRINCESS_SHEET, layer_map=lm)


# --------------------------------------------------------------------------- #
# Synthetic, self-contained (always runs)
# --------------------------------------------------------------------------- #
def _build_autocad_scheme_dxf(path):
    """A single unit drawn with AutoCAD-house layer names (not the Revit scheme):
    walls on A_WALL_FULL_N, poché on A_WALL_CAVITY, room text on A_TEXT_BLOWUPS,
    a door on DOOR-line, and dimension text on DIM (must drop, not seed)."""
    doc = ezdxf.new("R2010")
    doc.header["$INSUNITS"] = 1  # inches, like the real A12
    msp = doc.modelspace()
    ring = [(0, 0), (240, 0), (240, 180), (0, 180), (0, 0)]
    for a, b in zip(ring, ring[1:]):
        msp.add_line(a, b, dxfattribs={"layer": "A_WALL_FULL_N"})
    hatch = msp.add_hatch(dxfattribs={"layer": "A_WALL_CAVITY"})
    hatch.paths.add_polyline_path([(0, 0), (6, 0), (6, 180), (0, 180)])
    msp.add_line((60, 0), (96, 0), dxfattribs={"layer": "DOOR-line"})
    msp.add_text("KITCHEN", dxfattribs={"layer": "A_TEXT_BLOWUPS"}).set_placement((120, 90))
    msp.add_text("MASTER BEDROOM", dxfattribs={"layer": "A_TEXT_BLOWUPS"}).set_placement((60, 140))
    msp.add_text("12'-0\"", dxfattribs={"layer": "DIM"}).set_placement((10, 10))
    doc.saveas(path)
    return path


class SyntheticSchemeTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        fd, cls.path = tempfile.mkstemp(suffix=".dxf")
        os.close(fd)
        _build_autocad_scheme_dxf(cls.path)
        cls.doc = ezdxf.readfile(cls.path)
        cls.lm, cls.report = infer_layer_map(cls.doc)

    @classmethod
    def tearDownClass(cls):
        os.remove(cls.path)

    def test_walls_detected_by_name(self):
        self.assertIn("A_WALL_FULL_N", self.lm["wall_line"])
        self.assertIn("A_WALL_CAVITY", self.lm["wall_fill"])

    def test_room_text_layer_detected_by_content(self):
        self.assertEqual(self.lm["room_label"], ["A_TEXT_BLOWUPS"])

    def test_dim_layer_dropped_not_seeded(self):
        self.assertIn("DIM", self.lm["drop"])
        self.assertNotIn("DIM", self.lm["room_label"])

    def test_inferred_map_parses_with_labels(self):
        res = parse_dxf(self.path, layer_map=self.lm)
        names = sorted(l["name"] for l in res["labels"])
        self.assertEqual(names, ["KITCHEN", "MASTER BEDROOM"])


# --------------------------------------------------------------------------- #
# _classify role priority (the pure heart)
# --------------------------------------------------------------------------- #
class ClassifyPriorityTest(unittest.TestCase):
    def test_room_text_content_wins_over_name(self):
        # A layer whose name screams 'drop' but which carries room text is kept.
        t = {"line": 0, "hatch": 0, "text": 3, "room_text": 3, "samples": []}
        role, conf = _classify(_norm("A_TEXT_BLOWUPS"), t)
        self.assertEqual((role, conf), ("room_label", "content"))

    def test_dimension_text_layer_is_dropped(self):
        t = {"line": 0, "hatch": 0, "text": 5, "room_text": 0, "samples": []}
        role, _ = _classify(_norm("DIM"), t)
        self.assertEqual(role, "drop")

    def test_wall_fill_before_outline(self):
        t = {"line": 1, "hatch": 0, "text": 0, "room_text": 0, "samples": []}
        self.assertEqual(_classify(_norm("A_WALL_CAVITY"), t)[0], "wall_fill")
        self.assertEqual(_classify(_norm("A_WALL_FULL_N"), t)[0], "wall_line")


# --------------------------------------------------------------------------- #
# /parse wiring: auto-recovery, precedence, override (sandboxed data dirs)
# --------------------------------------------------------------------------- #
def _dxf_bytes(builder):
    fd, path = tempfile.mkstemp(suffix=".dxf")
    os.close(fd)
    try:
        builder(path)
        with open(path, "rb") as f:
            return f.read()
    finally:
        os.remove(path)


class ParseEndpointInferenceTest(unittest.TestCase):
    """The /parse front stage: a non-Revit DXF that the default map can't read
    must auto-detect its layers, recover, and report what it found."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="fpsg_layers_")
        self._saved = (main.PROP_DIR, main.UP_DIR, main.SHEET_DIR)
        main.PROP_DIR = os.path.join(self.tmp, "properties")
        main.UP_DIR = os.path.join(self.tmp, "uploads")
        main.SHEET_DIR = os.path.join(self.tmp, "sheets")
        for d in (main.PROP_DIR, main.UP_DIR, main.SHEET_DIR):
            os.makedirs(d)

    def tearDown(self):
        main.PROP_DIR, main.UP_DIR, main.SHEET_DIR = self._saved
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _post(self, raw, filename="unit.dxf", **kw):
        uf = UploadFile(file=io.BytesIO(raw), filename=filename)
        return asyncio.run(parse_endpoint(file=uf, property_id=None, **kw))

    def test_autocad_scheme_recovers_via_inference(self):
        out = self._post(_dxf_bytes(_build_autocad_scheme_dxf))
        self.assertTrue(out["layer_inferred"])
        self.assertEqual(sorted(l["name"] for l in out["labels"]),
                         ["KITCHEN", "MASTER BEDROOM"])
        # the report explains the guess so the UI can offer correction
        report = out["layer_report"]
        assert report is not None        # inferred run always carries a report
        roles = {r["layer"]: r["role"] for r in report}
        self.assertEqual(roles["A_TEXT_BLOWUPS"], "room_label")

    def test_explicit_override_is_respected_not_second_guessed(self):
        # A user-supplied map that finds no walls must fail loudly, NOT silently
        # fall back to inference — the override is an explicit choice.
        bad = json.dumps({"wall_line": ["NOPE"], "room_label": []})
        with self.assertRaises(HTTPException) as ctx:
            self._post(_dxf_bytes(_build_autocad_scheme_dxf), layer_map=bad)
        self.assertEqual(ctx.exception.status_code, 422)


if __name__ == "__main__":
    unittest.main()
