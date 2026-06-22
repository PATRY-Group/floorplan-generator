"""Vercel Python serverless entry point (the step-3 spike).

Vercel serves the module-level ASGI `app` as a function. We import the existing
FastAPI app from app/backend/main.py and add a `/spike` probe whose only job is
to answer the make-or-break questions BEFORE the storage rewrite:

  1. Does the function deploy at all? (i.e. do numpy/Pillow/PyMuPDF/ezdxf/resvg
     fit under Vercel's bundle-size limit and import on the Lambda runtime?)
  2. Does rasterization actually work there? (resvg is the real native risk.)

This is a throwaway diagnostic — not the final wiring. The frontend and the
Blob-backed storage come after the spike confirms Vercel is viable.
"""
import os
import sys
import time

# main.py lives in app/backend; put it on the path so `import main` resolves.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app", "backend"))

# Storage has no persistent disk on Vercel; point it at the one writable dir so
# import-time setup + per-request file ops don't blow up during the spike.
os.environ.setdefault("DATA_DIR", "/tmp/data")

from main import app  # noqa: E402  — the FastAPI ASGI app Vercel will serve


@app.get("/spike")
def spike():
    """Prove the heavy deps import and resvg can rasterize on this runtime."""
    info = {}
    try:
        import numpy, PIL, fitz, ezdxf, resvg_py  # noqa: F401
        info["imports"] = "numpy, Pillow, PyMuPDF, ezdxf, resvg_py — all loaded"
    except Exception as e:
        return {"ok": False, "stage": "import", "error": repr(e)}
    try:
        svg = ('<svg xmlns="http://www.w3.org/2000/svg" width="120" height="60">'
               '<rect width="120" height="60" fill="#2B1F14"/>'
               '<text x="12" y="42" font-size="30" fill="#C17F3A" '
               'font-family="Georgia, serif">800</text></svg>')
        t = time.time()
        png = bytes(resvg_py.svg_to_bytes(svg_string=svg, width=240))
        info.update(ok=True, render_ms=round((time.time() - t) * 1000),
                    png_bytes=len(png), png_is_real=png[:8] == b"\x89PNG\r\n\x1a\n")
    except Exception as e:
        info.update(ok=False, stage="render", error=repr(e))
    return info
