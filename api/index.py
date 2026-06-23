"""Vercel Python serverless entry point.

Vercel serves the module-level ASGI `app` as a function. We import the existing
FastAPI app from app/backend/main.py; it handles /api/* and serves the pre-built
frontend (app/frontend/dist/) as static files, so one function covers the whole
origin (routing is in vercel.json).
"""
import os
import sys

# main.py lives in app/backend; put it on the path so `import main` resolves.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app", "backend"))

# Serverless has no persistent disk; point the data root at the one writable dir
# so import-time setup doesn't blow up. Persistent I/O routes through Vercel Blob
# (see app/backend/storage.py), keyed relative to this root.
os.environ.setdefault("DATA_DIR", "/tmp/data")

from main import app  # noqa: E402  — the FastAPI ASGI app Vercel will serve
