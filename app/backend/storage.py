"""Pluggable storage backend: local filesystem or Vercel Blob.

The backend is chosen once, at import, by the presence of BLOB_READ_WRITE_TOKEN:
present -> Vercel Blob (serverless, persistent, shared); absent -> the local
filesystem exactly as before. So local dev, the test suite, and the Docker image
keep using files unchanged; only a Vercel deployment (with a connected Blob store)
uses Blob.

Every function takes the SAME absolute paths the app already builds under the data
directory. In Blob mode a path is mapped to a blob key — its path relative to
ROOT, with forward slashes — so main.py's path-building is untouched; only the I/O
primitives route through here.

Blob reads/deletes resolve a key to its public URL via a prefix `list` (one extra
HTTP call), which keeps the code independent of how Blob formats public hostnames.
Fine for this low-traffic internal tool.
"""
import os
import json
import glob as _globmod
import shutil
import fnmatch
import uuid
from datetime import datetime
from typing import Any

ROOT = ""  # the data dir these paths live under; set by main.py at import
_TOKEN = os.environ.get("BLOB_READ_WRITE_TOKEN")
USING_BLOB = bool(_TOKEN)

# Fail loud instead of losing data silently. On a serverless host the only
# writable disk is ephemeral (/tmp), wiped on every cold start — so a Vercel
# deploy with no Blob token would *look* fine while quietly dropping every saved
# property and sheet between requests. Detect that one case and refuse to start
# with guidance, rather than degrading to invisible data loss. Local dev, the
# test suite, and Docker (no VERCEL env, legitimately tokenless) are untouched;
# set ALLOW_EPHEMERAL_STORAGE=1 to opt into throwaway storage on purpose.
_ON_SERVERLESS = bool(os.environ.get("VERCEL") or os.environ.get("AWS_LAMBDA_FUNCTION_NAME"))
if _ON_SERVERLESS and not USING_BLOB and not os.environ.get("ALLOW_EPHEMERAL_STORAGE"):
    raise RuntimeError(
        "Serverless host detected but BLOB_READ_WRITE_TOKEN is not set. Storage "
        "would fall back to the ephemeral filesystem and silently lose all saved "
        "properties and sheets on the next cold start. Connect a Vercel Blob store "
        "(it injects BLOB_READ_WRITE_TOKEN), or set ALLOW_EPHEMERAL_STORAGE=1 to "
        "intentionally use throwaway storage."
    )

# Human-readable backend, for /health and logs.
MODE = "blob" if USING_BLOB else ("ephemeral-fs" if _ON_SERVERLESS else "filesystem")


def _key(path):
    """Absolute path under ROOT -> blob key (slash-separated, ROOT-relative)."""
    return os.path.relpath(path, ROOT).replace(os.sep, "/")


# --------------------------------------------------------------------------- #
# filesystem backend (dev / Docker) — behaves exactly like the original code
# --------------------------------------------------------------------------- #
def _fs_read_bytes(path):
    try:
        with open(path, "rb") as f:
            return f.read()
    except (FileNotFoundError, NotADirectoryError):
        return None


def _fs_write_bytes(path, data):
    # Atomic write (temp + replace) so a reader never sees a truncated file —
    # the guarantee the original _write_json provided.
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.{uuid.uuid4().hex}.tmp"
    try:
        with open(tmp, "wb") as f:
            f.write(data)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


# --------------------------------------------------------------------------- #
# Vercel Blob backend (serverless) — lazy-imports the SDK so FS mode never needs it
# --------------------------------------------------------------------------- #
def _blob():
    import vercel_blob  # type: ignore[import-not-found]  # Vercel-only dep; not installed locally
    return vercel_blob


def _blob_list_all(prefix):
    """Every blob under `prefix`, following Vercel Blob's pagination cursor.
    list() caps each page (~1000 blobs); without walking the cursor, a property
    with enough saved sheets — or a busy uploads cache — would silently lose
    everything past the first page: exists() false-negatives, glob/listdir
    missing files, rmtree leaving orphans. One listing call per page."""
    blobs = []
    cursor = None
    while True:
        opts = {"prefix": prefix}
        if cursor:
            opts["cursor"] = cursor
        res = _blob().list(opts)
        blobs.extend(res.get("blobs", []))
        # Tolerate either camelCase or snake_case paging fields — the vercel_blob
        # Python SDK's exact return shape isn't pinned here, and guessing wrong
        # would silently stop after page one (the bug we're fixing).
        if not (res.get("hasMore") or res.get("has_more")):
            break
        cursor = res.get("cursor")
        if not cursor:      # has-more but no cursor -> stop, don't loop forever
            break
    return blobs


def _blob_find(key):
    """Return the blob dict whose pathname == key, or None."""
    for b in _blob_list_all(key):
        if b.get("pathname") == key:
            return b
    return None


def _blob_read_bytes(path):
    b = _blob_find(_key(path))
    if not b:
        return None
    import urllib.request
    # Private store: the blob URL requires the token to read. Sending the bearer
    # header is harmless for public stores too, so this works either way.
    req = urllib.request.Request(
        b.get("downloadUrl") or b["url"],
        headers={"authorization": f"Bearer {_TOKEN}"})
    with urllib.request.urlopen(req) as r:
        return r.read()


def _blob_write_bytes(path, data):
    # addRandomSuffix off so the key stays stable and addressable by pathname.
    _blob().put(_key(path), data, {"addRandomSuffix": False, "allowOverwrite": True})


# --------------------------------------------------------------------------- #
# public, path-based API (used by main.py)
# --------------------------------------------------------------------------- #
def read_bytes(path):
    return _blob_read_bytes(path) if USING_BLOB else _fs_read_bytes(path)


def write_bytes(path, data):
    (_blob_write_bytes if USING_BLOB else _fs_write_bytes)(path, data)


def read_text(path):
    data = read_bytes(path)
    return None if data is None else data.decode("utf-8")


def write_text(path, text):
    write_bytes(path, text.encode("utf-8"))


def read_json(path, default=None) -> Any:
    data = read_bytes(path)
    if data is None:
        return default
    return json.loads(data.decode("utf-8"))


def write_json(path, obj, **dump_kw):
    write_bytes(path, json.dumps(obj, **dump_kw).encode("utf-8"))


def exists(path):
    if USING_BLOB:
        return _blob_find(_key(path)) is not None
    return os.path.isfile(path)


def remove(path):
    if USING_BLOB:
        b = _blob_find(_key(path))
        if b:
            _blob().delete(b["url"])
        return
    try:
        os.remove(path)
    except OSError:
        pass


def copy(src, dst):
    if USING_BLOB:
        data = read_bytes(src)
        if data is not None:
            write_bytes(dst, data)
        return
    shutil.copy(src, dst)


def glob(pattern):
    """Like glob.glob for a pattern under the data dir. Blob mode lists by the
    fixed prefix before the first wildcard, then fnmatches the keys."""
    if not USING_BLOB:
        return _globmod.glob(pattern)
    key_pat = _key(pattern)
    prefix = key_pat.split("*", 1)[0]
    out = []
    for b in _blob_list_all(prefix):
        pn = b.get("pathname", "")
        if fnmatch.fnmatch(pn, key_pat):
            out.append(os.path.join(ROOT, pn.replace("/", os.sep)))
    return out


def listdir(path):
    """Names directly under `path` (one level). Blob has no dirs, so derive them
    from the keys sharing the prefix."""
    if not USING_BLOB:
        return os.listdir(path) if os.path.isdir(path) else []
    prefix = _key(path).rstrip("/") + "/"
    names = set()
    for b in _blob_list_all(prefix):
        rest = b.get("pathname", "")[len(prefix):]
        if rest:
            names.add(rest.split("/", 1)[0])
    return sorted(names)


def isdir(path):
    if not USING_BLOB:
        return os.path.isdir(path)
    prefix = _key(path).rstrip("/") + "/"
    return bool(_blob().list({"prefix": prefix, "limit": 1}).get("blobs"))


def getmtime(path):
    """Epoch seconds of last modification (for the uploads TTL sweep)."""
    if not USING_BLOB:
        return os.path.getmtime(path)
    b = _blob_find(_key(path))
    if not b:
        return 0.0
    ts = b.get("uploadedAt")  # ISO-8601, e.g. 2026-06-22T15:00:00.000Z
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
    except (ValueError, AttributeError):
        return 0.0


def rmtree(path):
    """Remove everything under `path`."""
    if not USING_BLOB:
        shutil.rmtree(path, ignore_errors=True)
        return
    prefix = _key(path).rstrip("/") + "/"
    urls = [b["url"] for b in _blob_list_all(prefix)]
    if urls:
        _blob().delete(urls)
