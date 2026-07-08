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
import threading
import time
import unittest

from fastapi import HTTPException, UploadFile

import fixtures as fx
import main
from main import (RenderRequest, Property, do_render, compose_config,
                  _safe_id, sweep_uploads, capabilities, health,
                  put_property, get_property, list_properties, delete_property,
                  list_sheets, reopen_sheet, delete_sheet, get_sheet_svg,
                  get_sheet_png, get_sheet_thumb, parse as parse_endpoint,
                  _apply_custom_fonts, upload_plan_pdf,
                  DownloadRequest, download_sheets)


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

    def _cache_planimg(self, doc_id="imgdoc1"):
        """Seed an image-plan upload the same way /plan-pdf would, without
        going through PDF rasterization — a plain cropped plate PNG."""
        with open(os.path.join(main.UP_DIR, f"{doc_id}.planimg.png"), "wb") as f:
            f.write(fx.plate_png())
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

    def test_paint_image_bakes_into_export(self):
        """paint_image on the request is embedded in the rendered svg, and the
        hide_watermark metadata flag suppresses the ghost mark."""
        doc = self._cache_prims()
        uri = "data:image/png;base64,PAINTBYTES"
        out = do_render(RenderRequest(doc_id=doc,
                                      metadata={"title": "T", "watermark": "800",
                                                "hide_watermark": True},
                                      paint_image=uri))
        self.assertIn(uri, out["svg"])
        self.assertNotIn('fill-opacity="0.07"', out["svg"])   # no text watermark


class ImagePlanRenderEndpointTest(_TempDataDirs):
    """/render on a PDF-plan doc_id (no prims.json, only planimg.png) must
    route to render_image_plan() instead of 404ing on missing geometry."""

    def test_renders_from_cached_planimg(self):
        doc = self._cache_planimg()
        out = do_render(RenderRequest(doc_id=doc, metadata={"title": "2 BED"},
                                      want_png=True))
        self.assertTrue(out["svg"].startswith("<svg"))
        self.assertIn("<image", out["svg"])
        self.assertIsNotNone(out["png_b64"])
        self.assertIsNone(out["sheet_id"])

    def test_neither_prims_nor_planimg_is_404(self):
        with self.assertRaises(HTTPException) as ctx:
            do_render(RenderRequest(doc_id="gone"))
        self.assertEqual(ctx.exception.status_code, 404)


class PaintPersistenceTest(_TempDataDirs):
    def test_paint_survives_save_and_reopen(self):
        doc = self._cache_prims()
        put_property("acme", Property(id="acme", name="ACME"))
        uri = "data:image/png;base64,PAINTBYTES"
        out = do_render(RenderRequest(doc_id=doc, property_id="acme",
                                      metadata={"title": "2 BED"},
                                      paint_image=uri, save=True))
        sid = out["sheet_id"]
        # the saved svg artifact has the paint baked in
        self.assertIn(uri, bytes(get_sheet_svg("acme", sid).body).decode("utf-8"))
        # reopen returns the paint_image so the editor can restore the canvas
        reopened = reopen_sheet("acme", sid)
        self.assertEqual(reopened["paint_image"], uri)


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

    def test_concurrent_saves_to_one_property_dont_lose_entries(self):
        """Regression guard for the index.json read-modify-write race: N parallel
        saves to the same property must all land in the library. Without the
        per-property lock, interleaved RMW drops entries (orphaned sheets)."""
        doc = self._cache_prims()
        put_property("acme", Property(id="acme", name="ACME"))
        n = 8
        barrier = threading.Barrier(n)   # release all threads into the RMW at once
        errors = []

        def save(i):
            try:
                barrier.wait()
                do_render(RenderRequest(doc_id=doc, property_id="acme",
                                        metadata={"title": f"UNIT {i}"}, save=True))
            except Exception as e:        # surface worker failures on the main thread
                errors.append(e)

        threads = [threading.Thread(target=save, args=(i,)) for i in range(n)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(errors, [])
        listing = list_sheets("acme")
        self.assertEqual(len(listing), n)                      # none lost
        self.assertEqual(len({s["sheet_id"] for s in listing}), n)  # all distinct


class ImagePlanLifecycleTest(_TempDataDirs):
    """The same save -> list -> reopen -> delete lifecycle as DXF sheets, for
    a PDF-plan (image-kind) doc — this must not be second-class in the library
    just because it has no vector geometry."""

    def test_save_list_reopen_delete(self):
        doc = self._cache_planimg()
        put_property("acme", Property(id="acme", name="ACME"))
        out = do_render(RenderRequest(doc_id=doc, property_id="acme",
                                      metadata={"title": "PDF UNIT"}, save=True))
        sid = out["sheet_id"]
        self.assertIsNotNone(sid)

        listing = list_sheets("acme")
        self.assertEqual(listing[0]["sheet_id"], sid)
        self.assertEqual(listing[0]["kind"], "image")
        self.assertTrue(get_sheet_svg("acme", sid).body)
        self.assertEqual(get_sheet_png("acme", sid).media_type, "image/png")
        self.assertTrue(os.path.exists(
            os.path.join(main.SHEET_DIR, "acme", f"{sid}.planimg.png")))
        self.assertFalse(os.path.exists(
            os.path.join(main.SHEET_DIR, "acme", f"{sid}.prims.json")))

        # reopen copies the image back under a fresh doc_id and reports its kind
        reopened = reopen_sheet("acme", sid)
        self.assertEqual(reopened["kind"], "image")
        new_doc = reopened["doc_id"]
        self.assertNotEqual(new_doc, doc)
        self.assertTrue(os.path.exists(
            os.path.join(main.UP_DIR, f"{new_doc}.planimg.png")))
        # the reopened doc renders again on its new doc_id
        redo = do_render(RenderRequest(doc_id=new_doc, property_id="acme",
                                       metadata=reopened["metadata"]))
        self.assertIn("<image", redo["svg"])

        delete_sheet("acme", sid)
        self.assertEqual(list_sheets("acme"), [])

    def test_dxf_sheet_reports_kind(self):
        """A DXF-sourced save gets 'kind': 'dxf' in its library entry, the
        counterpart of the image-kind assertion above."""
        doc = self._cache_prims()
        put_property("acme", Property(id="acme", name="ACME"))
        do_render(RenderRequest(doc_id=doc, property_id="acme",
                                metadata={"title": "DXF UNIT"}, save=True))
        listing = list_sheets("acme")
        self.assertEqual(listing[0]["kind"], "dxf")


class DownloadPlanOnlyTest(_TempDataDirs):
    """/sheets/download with plan_only re-renders each saved sheet as a bare
    plan — must work whether the sheet's source was a DXF or a PDF plan."""

    def _save(self, doc, prop_id, title):
        put_property(prop_id, Property(id=prop_id, name=prop_id.upper()))
        out = do_render(RenderRequest(doc_id=doc, property_id=prop_id,
                                      metadata={"title": title}, save=True))
        return out["sheet_id"]

    def test_plan_only_zip_includes_image_sourced_sheet(self):
        import zipfile
        sid = self._save(self._cache_planimg(), "acme", "PDF UNIT")
        resp = download_sheets(DownloadRequest(
            items=[{"property_id": "acme", "sheet_id": sid}],
            formats=["png"], plan_only=True))
        zf = zipfile.ZipFile(io.BytesIO(resp.body))
        self.assertEqual(len(zf.namelist()), 1)

    def test_plan_only_zip_includes_dxf_sourced_sheet(self):
        import zipfile
        sid = self._save(self._cache_prims(), "acme2", "DXF UNIT")
        resp = download_sheets(DownloadRequest(
            items=[{"property_id": "acme2", "sheet_id": sid}],
            formats=["png"], plan_only=True))
        zf = zipfile.ZipFile(io.BytesIO(resp.body))
        self.assertEqual(len(zf.namelist()), 1)


class SheetThumbnailTest(_TempDataDirs):
    def _save_sheet(self):
        doc = self._cache_prims()
        put_property("acme", Property(id="acme", name="ACME"))
        out = do_render(RenderRequest(doc_id=doc, property_id="acme",
                                      metadata={"title": "2 BED"}, save=True))
        return out["sheet_id"]

    def test_thumbnail_built_on_save_and_downscaled(self):
        import io
        from PIL import Image
        sid = self._save_sheet()
        # save pre-builds the thumb so the library grid never fetches the full sheet
        thumb_path = os.path.join(main.SHEET_DIR, "acme", f"{sid}.thumb.png")
        self.assertTrue(os.path.exists(thumb_path))

        thumb = get_sheet_thumb("acme", sid)
        self.assertEqual(thumb.media_type, "image/png")
        tw, _ = Image.open(io.BytesIO(bytes(thumb.body))).size
        fw, _ = Image.open(io.BytesIO(bytes(get_sheet_png("acme", sid).body))).size
        self.assertLessEqual(tw, main.THUMB_W)   # never wider than the cap
        self.assertLess(tw, fw)                  # and smaller than the full sheet

    def test_thumbnail_regenerated_lazily_for_legacy_sheet(self):
        sid = self._save_sheet()
        # an older sheet saved before thumbnails existed: only the full PNG is present
        os.remove(os.path.join(main.SHEET_DIR, "acme", f"{sid}.thumb.png"))
        thumb = get_sheet_thumb("acme", sid)        # generated on demand from the full PNG
        self.assertEqual(thumb.media_type, "image/png")
        self.assertTrue(os.path.exists(             # and cached back for next time
            os.path.join(main.SHEET_DIR, "acme", f"{sid}.thumb.png")))

    def test_missing_sheet_thumbnail_is_404(self):
        with self.assertRaises(HTTPException) as ctx:
            get_sheet_thumb("acme", "nope")
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
        res = health()
        self.assertTrue(res["ok"])
        # storage mode is surfaced for observability (filesystem in the test env)
        self.assertEqual(res["storage"], "filesystem")

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


class PlanPdfUploadTest(_TempDataDirs):
    """/plan-pdf: the finished-floor-plan-PDF intake, mirroring /parse's
    upload guards but caching a cropped image instead of geometry."""

    def _post(self, raw, filename):
        uf = UploadFile(file=io.BytesIO(raw), filename=filename)
        return asyncio.run(upload_plan_pdf(file=uf))

    def test_happy_path_caches_planimg_and_returns_dims(self):
        out = self._post(fx.pdf_bytes(size=(400, 300)), "plan.pdf")
        self.assertIsNotNone(out["width"])
        self.assertIsNotNone(out["height"])
        self.assertTrue(os.path.exists(
            os.path.join(main.UP_DIR, f"{out['doc_id']}.planimg.png")))

    def test_multi_page_pdf_rejected_422(self):
        with self.assertRaises(HTTPException) as ctx:
            self._post(fx.pdf_bytes(pages=2), "plan.pdf")
        self.assertEqual(ctx.exception.status_code, 422)

    def test_garbage_content_behind_pdf_extension_is_422(self):
        """Extension-trust matches /parse's convention: a correct extension
        with unreadable content fails inside the parser (422), not at the
        extension gate (415)."""
        with self.assertRaises(HTTPException) as ctx:
            self._post(b"not a pdf", "plan.pdf")
        self.assertEqual(ctx.exception.status_code, 422)

    def test_wrong_extension_rejected_415(self):
        with self.assertRaises(HTTPException) as ctx:
            self._post(fx.pdf_bytes(), "plan.dxf")
        self.assertEqual(ctx.exception.status_code, 415)


if __name__ == "__main__":
    unittest.main()
