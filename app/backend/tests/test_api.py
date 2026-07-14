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
from typing import Any

from fastapi import HTTPException, UploadFile

import fixtures as fx
import main
from main import (RenderRequest, Property, do_render, compose_config,
                  _safe_id, sweep_uploads, capabilities, health,
                  put_property, get_property, list_properties, delete_property,
                  list_sheets, reopen_sheet, delete_sheet, get_sheet_svg,
                  get_sheet_png, get_sheet_pdf, get_sheet_thumb, parse as parse_endpoint,
                  _apply_custom_fonts, upload_plan_pdf,
                  DownloadRequest, _DownloadItem, download_sheets)


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

    def test_storage_listing_error_does_not_propagate(self):
        """The sweep runs at the head of /parse and /plate, so a storage listing
        failure (in Blob mode the SDK raises its own error, not OSError) must be
        swallowed — it should skip the sweep, never 500 the upload."""
        orig = main.storage.glob
        main.storage.glob = lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("blob down"))
        try:
            self.assertEqual(sweep_uploads(), 0)
        finally:
            main.storage.glob = orig


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
        self.assertFalse(reopened["paint_stale"])   # saved at the current page aspect

    def test_stale_aspect_paint_is_dropped_and_flagged_on_reopen(self):
        """A sheet saved against a DIFFERENT page (the page was later resized) has
        full-page paint for the old aspect that would land wrong on the new page.
        Reopen must drop it and set paint_stale so the UI can tell the user to
        re-paint — not silently misplace it."""
        doc = self._cache_prims()
        put_property("acme", Property(id="acme", name="ACME"))
        uri = "data:image/png;base64,PAINTBYTES"
        out = do_render(RenderRequest(doc_id=doc, property_id="acme",
                                      metadata={"title": "2 BED"},
                                      paint_image=uri, save=True))
        sid = out["sheet_id"]
        # Rewrite the saved SVG to an old-aspect page (1000x1080, pre-resize).
        svg_path = os.path.join(main.SHEET_DIR, "acme", f"{sid}.svg")
        main.storage.write_text(
            svg_path, '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1000 1080"></svg>')
        reopened = reopen_sheet("acme", sid)
        self.assertTrue(reopened["paint_stale"])
        self.assertIsNone(reopened["paint_image"])

    def test_current_landscape_paint_is_kept_on_reopen(self):
        """A current-aspect LANDSCAPE sheet (viewBox 1294x1000) must NOT be flagged
        stale — the guard's landscape page string has to match real output, or good
        paint gets dropped on reopen."""
        doc = self._cache_prims()
        put_property("acme", Property(id="acme", name="ACME"))
        uri = "data:image/png;base64,PAINTBYTES"
        out = do_render(RenderRequest(doc_id=doc, property_id="acme",
                                      metadata={"title": "2 BED", "orientation": "landscape"},
                                      paint_image=uri, save=True))
        reopened = reopen_sheet("acme", out["sheet_id"])
        self.assertFalse(reopened["paint_stale"])
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
            items=[_DownloadItem(property_id="acme", sheet_id=sid)],
            formats=["png"], plan_only=True))
        zf = zipfile.ZipFile(io.BytesIO(resp.body))
        self.assertEqual(len(zf.namelist()), 1)

    def test_plan_only_zip_includes_dxf_sourced_sheet(self):
        import zipfile
        sid = self._save(self._cache_prims(), "acme2", "DXF UNIT")
        resp = download_sheets(DownloadRequest(
            items=[_DownloadItem(property_id="acme2", sheet_id=sid)],
            formats=["png"], plan_only=True))
        zf = zipfile.ZipFile(io.BytesIO(resp.body))
        self.assertEqual(len(zf.namelist()), 1)

    def test_saved_sheet_pdf_route_wraps_the_png(self):
        sid = self._save(self._cache_prims(), "acme3", "DXF UNIT")
        resp = get_sheet_pdf("acme3", sid)
        self.assertEqual(resp.media_type, "application/pdf")
        self.assertTrue(bytes(resp.body).startswith(b"%PDF-"))

    def test_pdf_route_404_for_unknown_sheet(self):
        with self.assertRaises(HTTPException) as ctx:
            get_sheet_pdf("acme3", "nope")
        self.assertEqual(ctx.exception.status_code, 404)

    def test_batch_zip_pdf_synthesized_from_saved_png(self):
        import zipfile
        sid = self._save(self._cache_prims(), "acme4", "DXF UNIT")
        resp = download_sheets(DownloadRequest(
            items=[_DownloadItem(property_id="acme4", sheet_id=sid)],
            formats=["pdf"]))  # branded (not plan_only): reads saved .png, wraps it
        zf = zipfile.ZipFile(io.BytesIO(resp.body))
        names = zf.namelist()
        self.assertEqual(len(names), 1)
        self.assertTrue(names[0].endswith(".pdf"))
        self.assertTrue(zf.read(names[0]).startswith(b"%PDF-"))


class SheetThumbnailTest(_TempDataDirs):
    def _save_sheet(self):
        doc = self._cache_prims()
        put_property("acme", Property(id="acme", name="ACME"))
        out = do_render(RenderRequest(doc_id=doc, property_id="acme",
                                      metadata={"title": "2 BED"}, save=True))
        return out["sheet_id"]

    def test_thumbnail_built_lazily_and_downscaled(self):
        import io
        from PIL import Image
        sid = self._save_sheet()
        # A plain save defers the raster — neither the PNG nor the thumb exist yet.
        d = os.path.join(main.SHEET_DIR, "acme")
        self.assertFalse(os.path.exists(os.path.join(d, f"{sid}.thumb.png")))
        self.assertFalse(os.path.exists(os.path.join(d, f"{sid}.png")))
        # First fetch rebuilds from the SVG, downscales, and caches back.
        thumb = get_sheet_thumb("acme", sid)
        self.assertEqual(thumb.media_type, "image/png")
        self.assertTrue(os.path.exists(os.path.join(d, f"{sid}.thumb.png")))
        tw, _ = Image.open(io.BytesIO(bytes(thumb.body))).size
        fw, _ = Image.open(io.BytesIO(bytes(get_sheet_png("acme", sid).body))).size
        self.assertLessEqual(tw, main.THUMB_W)   # never wider than the cap
        self.assertLess(tw, fw)                  # and smaller than the full sheet

    def test_thumbnail_regenerated_when_deleted(self):
        sid = self._save_sheet()
        d = os.path.join(main.SHEET_DIR, "acme")
        get_sheet_thumb("acme", sid)                 # build + cache once
        os.remove(os.path.join(d, f"{sid}.thumb.png"))
        thumb = get_sheet_thumb("acme", sid)         # regenerated on demand
        self.assertEqual(thumb.media_type, "image/png")
        self.assertTrue(os.path.exists(os.path.join(d, f"{sid}.thumb.png")))

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

    def test_local_source_upload_is_cleaned_up(self):
        """Only prims.json should persist. The raw local upload (needed only
        during the request) must be removed — in Blob mode the sweep never sees
        local /tmp files, so a leftover would accumulate until the disk fills."""
        out = self._post(fx.unit_dxf_bytes(), "unit.dxf")
        leftovers = [f for f in os.listdir(main.UP_DIR)
                     if f.startswith(f"{out['doc_id']}_")]
        self.assertEqual(leftovers, [])

    def test_hostile_filename_does_not_500(self):
        """A filename with characters invalid on the host FS (e.g. ':' on
        Windows) must not crash open() with an unhandled error (a 500) — the
        name is sanitized before it becomes a temp path."""
        out = self._post(fx.unit_dxf_bytes(), 'a:b*c?"<>|.dxf')
        self.assertGreater(out["prim_count"], 0)


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

    def test_garbage_content_behind_png_extension_is_422(self):
        """The raster branch must validate too: junk bytes behind a .png
        extension used to be accepted (autocrop swallows the decode error) and
        later render a broken <image> — they should fail on intake with 422."""
        with self.assertRaises(HTTPException) as ctx:
            self._post(b"not a png", "plan.png")
        self.assertEqual(ctx.exception.status_code, 422)

    def test_wrong_extension_rejected_415(self):
        with self.assertRaises(HTTPException) as ctx:
            self._post(fx.pdf_bytes(), "plan.dxf")
        self.assertEqual(ctx.exception.status_code, 415)


class PreviewOptimizationTest(_TempDataDirs):
    """The perf changes: live previews skip the PNG raster, saves defer it (the
    PNG is rebuilt lazily from the saved SVG, byte-identically), and only an
    explicit want_png/want_pdf rasters inline."""

    def test_live_preview_skips_png_but_save_produces_it(self):
        doc = self._cache_prims()
        base: dict[str, Any] = dict(doc_id=doc, metadata={"title": "2 BED"})
        # Live preview: no PNG in the response, no raster done.
        prev = do_render(RenderRequest(live_preview=True, **base))
        self.assertIsNone(prev["png_b64"])
        # want_png: PNG present.
        full = do_render(RenderRequest(want_png=True, **base))
        self.assertIsNotNone(full["png_b64"])
        # The SVG (the authoritative artifact) is identical either way.
        self.assertEqual(prev["svg"], full["svg"])

    def test_want_pdf_page_matches_sheet_aspect(self):
        import base64, fitz
        doc = self._cache_prims()
        out = do_render(RenderRequest(doc_id=doc, metadata={"title": "2 BED"},
                                      want_pdf=True))
        self.assertIsNotNone(out["pdf_b64"])
        pdf = base64.b64decode(out["pdf_b64"])
        self.assertTrue(pdf.startswith(b"%PDF-"))
        d = fitz.open(stream=pdf, filetype="pdf")
        self.assertEqual(d.page_count, 1)
        r = d[0].rect
        # Page sized to the sheet's own shape: PDF_PAGE_WIDTH_IN wide, height
        # follows the branded sheet aspect (PAGE_H > PAGE_W, portrait), no bands.
        self.assertAlmostEqual(r.width / 72, main.PDF_PAGE_WIDTH_IN, places=1)
        self.assertGreater(r.height, r.width)

    def test_pdf_not_produced_without_want_pdf(self):
        doc = self._cache_prims()
        out = do_render(RenderRequest(doc_id=doc, metadata={"title": "2 BED"}))
        self.assertIsNone(out["pdf_b64"])

    def test_save_defers_png_but_serves_it_rebuilt(self):
        doc = self._cache_prims()
        put_property("acme", Property(id="acme", name="ACME"))
        out = do_render(RenderRequest(doc_id=doc, property_id="acme",
                                      metadata={"title": "2 BED"}, save=True))
        sid = out["sheet_id"]
        self.assertIsNone(out["png_b64"])           # no raster in the save response
        d = os.path.join(main.SHEET_DIR, "acme")
        # SVG persisted immediately; PNG + thumbnail deferred.
        self.assertTrue(os.path.exists(os.path.join(d, f"{sid}.svg")))
        self.assertFalse(os.path.exists(os.path.join(d, f"{sid}.png")))
        self.assertFalse(os.path.exists(os.path.join(d, f"{sid}.thumb.png")))
        # Fetching the PNG rebuilds it from the SVG and caches it back.
        resp = get_sheet_png("acme", sid)
        self.assertEqual(resp.media_type, "image/png")
        self.assertTrue(os.path.exists(os.path.join(d, f"{sid}.png")))

    def test_lazy_rebuilt_png_is_byte_identical_to_inline(self):
        """The deferred rebuild must reproduce exactly what the inline save would
        have written — same font-injected SVG, same fonts, same width."""
        doc = self._cache_prims()
        put_property("acme", Property(id="acme", name="ACME"))
        inline = do_render(RenderRequest(doc_id=doc, property_id="acme",
                                         metadata={"title": "T"}, save=True,
                                         want_png=True))          # rasters inline
        inline_png = bytes(get_sheet_png("acme", inline["sheet_id"]).body)
        deferred = do_render(RenderRequest(doc_id=doc, property_id="acme",
                                           metadata={"title": "T"}, save=True))  # deferred
        self.assertFalse(os.path.exists(
            os.path.join(main.SHEET_DIR, "acme", f"{deferred['sheet_id']}.png")))
        rebuilt_png = bytes(get_sheet_png("acme", deferred["sheet_id"]).body)   # rebuilt
        self.assertEqual(inline_png, rebuilt_png)

    def test_lazy_rebuild_threads_property_brand_fonts(self):
        """Load-bearing: the rebuild must raster with the property's uploaded
        brand fonts, or a brand-font sheet loses all text on the fontless
        serverless host (local system fonts would mask it). Spy on the shared
        rasterizer to prove the property's font_faces reach it."""
        doc = self._cache_prims()
        faces = [{"family": "BrandFace", "data": "data:font/ttf;base64,AAAA"}]
        put_property("acme", Property(id="acme", name="ACME", font_faces=faces))
        sid = do_render(RenderRequest(doc_id=doc, property_id="acme",
                                      metadata={"title": "T"}, save=True))["sheet_id"]
        captured = {}
        orig = main._raster_with_faces
        main._raster_with_faces = lambda svg, w, ff: (captured.setdefault("faces", ff),
                                                       orig(svg, w, ff))[1]
        try:
            get_sheet_png("acme", sid)   # deferred -> rebuild path
        finally:
            main._raster_with_faces = orig
        self.assertEqual(captured.get("faces"), faces)


class ImagePlanPreviewUrlTest(_TempDataDirs):
    """Image/PDF plans reference the raster by /planimg URL on a live preview
    (small payload) but inline the base64 on a save (self-contained artifact)."""

    def test_preview_references_url_save_inlines(self):
        doc = self._cache_planimg()
        prev = do_render(RenderRequest(doc_id=doc, metadata={"title": "T"},
                                       live_preview=True, asset_base="/api"))
        self.assertIn(f"/api/planimg/{doc}", prev["svg"])
        self.assertNotIn("base64,", prev["svg"])   # the multi-MB raster is NOT inlined

        put_property("acme", Property(id="acme", name="ACME"))
        saved = do_render(RenderRequest(doc_id=doc, property_id="acme",
                                        metadata={"title": "T"}, save=True))
        svg = bytes(get_sheet_svg("acme", saved["sheet_id"]).body).decode("utf-8")
        self.assertIn("base64,", svg)               # saved sheet stays self-contained
        self.assertNotIn("/planimg/", svg)

    def test_malicious_asset_base_is_ignored(self):
        doc = self._cache_planimg()
        prev = do_render(RenderRequest(doc_id=doc, metadata={"title": "T"},
                                       live_preview=True,
                                       asset_base='"><script>alert(1)</script>'))
        self.assertNotIn("<script>", prev["svg"])   # rejected -> falls back to inline
        self.assertIn("base64,", prev["svg"])

    def test_planimg_endpoint_serves_bytes_with_cache_header(self):
        doc = self._cache_planimg()
        resp = main.get_planimg(doc)
        self.assertEqual(resp.media_type, "image/png")
        self.assertIn("immutable", resp.headers["cache-control"])
        with self.assertRaises(HTTPException) as ctx:
            main.get_planimg("missingdoc")
        self.assertEqual(ctx.exception.status_code, 404)


class SheetCacheHeaderTest(_TempDataDirs):
    """Saved-sheet artifacts are content-stable per URL (cache-busted with ?v=),
    so they carry an immutable Cache-Control for instant library revisits."""

    def test_svg_png_thumb_are_immutable(self):
        doc = self._cache_prims()
        put_property("acme", Property(id="acme", name="ACME"))
        sid = do_render(RenderRequest(doc_id=doc, property_id="acme",
                                      metadata={"title": "T"}, save=True))["sheet_id"]
        for resp in (get_sheet_svg("acme", sid),
                     get_sheet_png("acme", sid),
                     get_sheet_thumb("acme", sid)):
            self.assertIn("immutable", resp.headers["cache-control"])


class RenderMemoTest(_TempDataDirs):
    """Repeated identical previews hit the in-process memo instead of re-rendering."""

    def test_identical_preview_is_cached_save_is_not(self):
        main._RENDER_CACHE.clear()
        doc = self._cache_prims()
        req: dict[str, Any] = dict(doc_id=doc, metadata={"title": "T"}, live_preview=True)
        do_render(RenderRequest(**req))
        self.assertEqual(len(main._RENDER_CACHE), 1)
        do_render(RenderRequest(**req))                 # identical -> served from memo
        self.assertEqual(len(main._RENDER_CACHE), 1)
        # A save never populates the memo (side effects + unique paint).
        put_property("acme", Property(id="acme", name="ACME"))
        before = len(main._RENDER_CACHE)
        do_render(RenderRequest(doc_id=doc, property_id="acme",
                                metadata={"title": "T"}, save=True))
        self.assertEqual(len(main._RENDER_CACHE), before)


if __name__ == "__main__":
    unittest.main()
