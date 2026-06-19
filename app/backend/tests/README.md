# Backend test suite

Validates the **output** of the Floor Plan Sheet Generator backend — the
authoritative, server-side artifacts (parsed geometry, rendered SVG/PNG, key
plans, brand palettes) and the HTTP/storage contracts that wrap them.

The suite is **hermetic**: every DXF and image is built in-memory (ezdxf / PIL),
and the API tests redirect `main`'s data directories to a temp tree, so it never
depends on the transient `data/uploads/` cache or touches real saved properties
and sheets. No fixtures on disk, no network, no extra dependencies — it runs on
the backend's existing `.venv` with the stdlib `unittest` runner.

## Running

From `app/backend/` (with the venv active):

```bash
python -m unittest discover -s tests -p "test_*.py"      # quiet
python -m unittest discover -s tests -p "test_*.py" -v   # per-test
```

(`pytest tests` also works if you prefer it — these are plain `unittest`
TestCases.)

## What each module covers (no overlap by design)

| Module | Validates |
|---|---|
| `test_parse.py` | `prims` shape contract, furniture/drop-layer exclusion, room-vs-title-vs-equipment classification, title/suite/sf suggestions, dimension estimation **and its deliberate refusals** (unitless / span-too-wide / extreme-aspect), large-entity downsampling, sheet/empty-file rejection |
| `test_render.py` | SVG well-formedness + page size, real PNG, the **SVG↔DXF transform contract** the drag editor relies on, label override vs auto-placement, palette defaults/overrides, watermark text-scaling vs image, XML escaping, the bare `plan_only` export |
| `test_brand.py` | palette role assignment by luminance/chroma, accent-gating against near-black chromatics, swatch list contract, PDF font-name cleaning, error handling |
| `test_keyplan.py` | unit-cell box→frame coordinate mapping, standalone sheet branding + NOT-TO-SCALE marker, plate-required guard, deterministic trace mask, brand-coloured silhouette |
| `test_convert.py` | graceful degradation when the ODA converter is absent (forced, machine-independent) |
| `test_api.py` | id traversal safety, uploads sweep, config composition, render-from-cache + expired-doc 404, property CRUD, save→reopen→delete sheet lifecycle, font-embed hook, upload guards (`.rvt`/unsupported/oversize/happy-path) |

The engine is tested directly for rich SVG/meta assertions; the API tests assert
only what the HTTP layer *adds* (loading, persistence, status codes), to avoid
re-checking the same output twice.

## Regression guard: polyline geometry

`test_parse.py::PolylineGeometryTest` guards a fixed defect:

- `parse.py` lists `("LWPOLYLINE", "POLYLINE")` as supported and originally
  called `entity.flattening(FLATTEN_DIST)` on them.
- In the installed **ezdxf 1.4.4**, `LWPolyline`/`Polyline2d` have **no usable
  `.flattening()` method**. The resulting `AttributeError` was swallowed by the
  bare `except Exception: pass` in `_collect_entities`, so **all polyline
  geometry was silently dropped** — a Revit export whose walls are polylines
  lost them with no error (and could be misreported as an empty "sheet" export).
- `ARC`/`CIRCLE`/`ELLIPSE`/`SPLINE` were never affected (those types do have
  `.flattening()`).
- **Fix (applied):** polylines now flatten via the already-imported `ezpath` —
  `ezpath.make_path(entity).flattening(FLATTEN_DIST)`, which also honours arc
  bulges. These tests assert polyline walls survive into `prims`; keep them so
  a future ezdxf bump or refactor can't silently reintroduce the drop.
