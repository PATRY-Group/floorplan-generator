"""
main.py — the HTTP/storage layer.

These tests drive the endpoint functions directly (the sync ones by call, the
async upload guards via asyncio) with `main`'s data directories redirected to a
throwaway temp dir, so the suite is hermetic and never touches real properties
or saved sheets.

Per the engine tests already covering SVG/meta in depth, here we assert only
what the HTTP layer *adds*: id safety, the uploads sweep, config composition,
prims loading + expiry, the save->reopen->delete lifecycle, the font-embed
hook, and the upload guards.
"""
import asyncio
import io
import json
import os
import shutil
import tempfile
import time
import unittest

from fastapi import HTTPException, UploadFile

import fixtures as fx
import main
from main import (RenderRequest, Property, do_render, compose_config,
                  _safe_id, sweep_uploads, capabilities, health,
                  put_property, get_property, list_properties, delete_property,
                  list_sheets, reopen_sheet, delete_sheet, get_sheet_svg,
                  get_sheet_png, parse as parse_endpoint, _apply_custom_fonts)


class _TempDataDirs(unittest.TestCase):
    """Base case: point main's storage dirs at a fresh temp tree."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="fpsg_test_")
        self._saved = (main.PROP_DIR, main.UP_DIR, main.SHEET_DIR,
                       main.MAX_UPLOAD_MB)
        main.PROP_DIR = os.path.join(self.tmp, "properties")
        main.UP_DIR = os.path.join(self.tmp, "uploads")
        main.SHEET_DIR = os.path.join(self.tmp, "sheets")
        for d in (main.PROP_DIR, main.UP_DIR, main.SHEET_DIR):
            os.makedirs(d)

    def tearDown(self):
        (main.PROP_DIR, main.UP_DIR, main.SHEET_DIR,
         main.MAX_UPLOAD_MB) = self._saved
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _cache_prims(self, doc_id="doc123"):
        path = fx.write_temp_dxf()
        try:
            from engine import parse_dxf
            res = parse_dxf(path)
        finally:
            os.remove(path)
        with open(os.path.join(main.UP_DIR, f"{doc_id}.prims.json"), "w",
                  encoding="utf-8") as f:
            json.dump({"prims": res["prims"], "extents": res["extents"]}, f)
        return doc_id


class SafeIdTest(unittest.TestCase):
    def test_rejects_traversal_and_separators(self):
        for bad in ("../etc", "a/b", "a\\b", "", "a.b", "a b"):
            with self.assertRaises(HTTPException) as ctx:
                _safe_id(bad)
            self.assertEqual(ctx.exception.status_code, 400)

    def test_accepts_uuid_and_slug(self):
        for ok in ("800-princess", "abcd1234ef", "a_b-C9"):
            self.assertEqual(_safe_id(ok), ok)


class SweepTest(_TempDataDirs):
    def test_old_files_swept_fresh_kept(self):
        old = os.path.join(main.UP_DIR, "old.prims.json")
        fresh = os.path.join(main.UP_DIR, "fresh.prims.json")
        for p in (old, fresh):
            open(p, "w").close()
        old_time = time.time() - (main.UPLOAD_TTL_HOURS + 1) * 3600
        os.utime(old, (old_time, old_time))
        removed = sweep_uploads()
        self.assertEqual(removed, 1)
        self.assertFalse(os.path.exists(old))
        self.assertTrue(os.path.exists(fresh))


class ComposeConfigTest(unittest.TestCase):
    def test_property_defaults_and_none_skip_and_default_layer_map(self):
        prop = {"name": "TOWER", "location": "CITY", "lockup": "800",
                "palette": {"dark": "#111"}}
        cfg = compose_config(prop, metadata={"title": "2 BED", "suite": None},
                             rooms=None)
        md = cfg["metadata"]
        self.assertEqual(md["property_name"], "TOWER")    # from property
        self.assertEqual(md["title"], "2 BED")            # from metadata
        # an explicit None in metadata is skipped, never written as a field
        self.assertNotIn("suite", md)
        self.assertEqual(cfg["rooms"], [])
        # no layer_map on the property -> the Revit default is supplied
        self.assertEqual(cfg["layer_map"], main.DEFAULT_LAYER_MAP)

    def test_palette_override_wins(self):
        prop = {"palette": {"dark": "#111"}}
        cfg = compose_config(prop, {}, [], palette_override={"dark": "#999"})
        self.assertEqual(cfg["palette"], {"dark": "#999"})


class RenderEndpointTest(_TempDataDirs):
    def test_renders_from_cached_prims(self):
        doc = self._cache_prims()
        out = do_render(RenderRequest(doc_id=doc, metadata={"title": "2 BED"},
                                      rooms=[{"name": "BEDROOM", "x": 10, "y": 7}],
                                      want_png=True))
        self.assertTrue(out["svg"].startswith("<svg"))
        self.assertIsNotNone(out["png_b64"])
        self.assertIsNone(out["sheet_id"])           # save not requested
        self.assertIn("transform", out["meta"])

    def test_expired_doc_id_is_404(self):
        with self.assertRaises(HTTPException) as ctx:
            do_render(RenderRequest(doc_id="gone"))
        self.assertEqual(ctx.exception.status_code, 404)


class PropertyCrudTest(_TempDataDirs):
    def test_put_get_list_delete_roundtrip(self):
        saved = put_property("acme", Property(id="acme", name="ACME",
                                              lockup="800"))
        self.assertEqual(saved["id"], "acme")
        # empty layer_map is backfilled with the Revit default
        self.assertEqual(saved["layer_map"], main.DEFAULT_LAYER_MAP)

        self.assertEqual(get_property("acme")["name"], "ACME")
        self.assertEqual([p["id"] for p in list_properties()], ["acme"])

        delete_property("acme")
        with self.assertRaises(HTTPException) as ctx:
            get_property("acme")
        self.assertEqual(ctx.exception.status_code, 404)


class SheetLifecycleTest(_TempDataDirs):
    def test_save_list_reopen_delete(self):
        doc = self._cache_prims()
        put_property("acme", Property(id="acme", name="ACME"))
        req = RenderRequest(doc_id=doc, property_id="acme",
                            metadata={"title": "2 BED", "suite": "204"},
                            rooms=[{"name": "BEDROOM", "x": 10, "y": 7}],
                            save=True)
        out = do_render(req)
        sid = out["sheet_id"]
        self.assertIsNotNone(sid)

        # library lists it, exported artifacts exist
        listing = list_sheets("acme")
        self.assertEqual(listing[0]["sheet_id"], sid)
        self.assertEqual(listing[0]["title"], "2 BED")
        self.assertTrue(get_sheet_svg("acme", sid).body)
        self.assertEqual(get_sheet_png("acme", sid).media_type, "image/png")

        # reopen copies geometry back under a fresh doc_id so editing works
        reopened = reopen_sheet("acme", sid)
        assert reopened is not None      # reopen of a just-saved sheet succeeds
        new_doc = reopened["doc_id"]
        self.assertNotEqual(new_doc, doc)
        self.assertTrue(os.path.exists(
            os.path.join(main.UP_DIR, f"{new_doc}.prims.json")))
        self.assertEqual(reopened["metadata"]["title"], "2 BED")

        # delete removes it from the index
        delete_sheet("acme", sid)
        self.assertEqual(list_sheets("acme"), [])

    def test_reopen_without_geometry_is_404(self):
        with self.assertRaises(HTTPException) as ctx:
            reopen_sheet("acme", "nope")
        self.assertEqual(ctx.exception.status_code, 404)


class FontEmbedTest(unittest.TestCase):
    def test_font_face_is_injected_into_svg(self):
        """The HTTP layer inlines an @font-face for uploaded brand fonts so the
        SVG renders them. (PNG re-render via resvg may fail on a fake font; the
        function falls back, but the SVG must still carry the face.)"""
        svg_in = '<svg xmlns="http://www.w3.org/2000/svg"><rect/></svg>'
        faces = [{"family": "BrandSerif",
                  "data": "data:font/ttf;base64,AAAA"}]
        svg_out, png_out = _apply_custom_fonts(svg_in, b"PNGDATA", faces)
        self.assertIn("@font-face", svg_out)
        self.assertIn("BrandSerif", svg_out)

    def test_no_faces_is_a_passthrough(self):
        svg_out, png_out = _apply_custom_fonts("<svg/>", b"X", None)
        self.assertEqual((svg_out, png_out), ("<svg/>", b"X"))


class CapabilitiesTest(unittest.TestCase):
    def setUp(self):
        self._saved = main.converter_available

    def tearDown(self):
        main.converter_available = self._saved

    def test_health(self):
        self.assertEqual(health(), {"ok": True})

    def test_capabilities_track_converter_presence(self):
        main.converter_available = lambda: False
        self.assertEqual(capabilities()["formats_accepted"], ["dxf"])
        main.converter_available = lambda: True
        self.assertIn("dwg", capabilities()["formats_accepted"])


class UploadGuardTest(_TempDataDirs):
    def _post(self, raw, filename):
        uf = UploadFile(file=io.BytesIO(raw), filename=filename)
        return asyncio.run(parse_endpoint(file=uf, property_id=None))

    def test_rvt_rejected_with_guidance(self):
        with self.assertRaises(HTTPException) as ctx:
            self._post(b"binary", "model.rvt")
        self.assertEqual(ctx.exception.status_code, 415)
        self.assertIn("rvt", ctx.exception.detail.lower())

    def test_unsupported_extension_rejected(self):
        with self.assertRaises(HTTPException) as ctx:
            self._post(b"hello", "notes.txt")
        self.assertEqual(ctx.exception.status_code, 415)

    def test_oversized_upload_rejected(self):
        main.MAX_UPLOAD_MB = 0          # any non-empty file now exceeds the cap
        with self.assertRaises(HTTPException) as ctx:
            self._post(b"x", "plan.dxf")
        self.assertEqual(ctx.exception.status_code, 413)

    def test_happy_path_caches_prims_and_returns_labels(self):
        out = self._post(fx.unit_dxf_bytes(), "unit.dxf")
        self.assertEqual(out["labels"][0]["name"], "BEDROOM")
        self.assertEqual(out["suggestions"]["suite"], "204")
        self.assertGreater(out["prim_count"], 0)
        self.assertTrue(os.path.exists(
            os.path.join(main.UP_DIR, f"{out['doc_id']}.prims.json")))


if __name__ == "__main__":
    unittest.main()
