"""One-time: copy local properties (and their saved sheets) into the Vercel Blob
store, so the deployed app shows the work you already have on disk.

Run it locally with the store's token in the environment:

    cd app/backend
    .venv\\Scripts\\activate
    pip install vercel_blob                 # only needed to run this seed
    # get the token: in the repo root run `vercel env pull` (writes .env.local
    # with BLOB_READ_WRITE_TOKEN), or copy it from the Vercel dashboard:
    #   Storage -> floorplan-data -> ".env.local" / Quickstart
    # then, with the token exported:
    python seed_blob.py                     # uploads properties
    python seed_blob.py --sheets            # also upload the saved-sheet library

It uploads to the SAME keys the app reads (properties/<id>.json, sheets/...), so
the deployed app picks them up immediately. Safe to re-run (it overwrites).
"""
import os
import sys
import glob


def _load_dotenv():
    """Load .env.local (written by `vercel env pull`) so the token is available
    without a manual export. Checks the repo root and the current directory."""
    here = os.path.dirname(os.path.abspath(__file__))
    for cand in (os.path.normpath(os.path.join(here, "..", "..", ".env.local")),
                 os.path.normpath(os.path.join(os.getcwd(), ".env.local"))):
        if os.path.isfile(cand):
            for line in open(cand, encoding="utf-8"):
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, _, v = line.partition("=")
                    os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))
            return cand
    return None


_load_dotenv()
if not os.environ.get("BLOB_READ_WRITE_TOKEN"):
    sys.exit("Set BLOB_READ_WRITE_TOKEN first — see the docstring at the top of this file.")

import storage  # reads the token at import -> Blob backend

BASE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(BASE, "data")
storage.ROOT = DATA

if not storage.USING_BLOB:
    sys.exit("BLOB_READ_WRITE_TOKEN not picked up — storage is in filesystem mode.")


def _upload(path):
    with open(path, "rb") as f:
        storage.write_bytes(path, f.read())
    print("  ->", os.path.relpath(path, DATA).replace(os.sep, "/"))


def main():
    also_sheets = "--sheets" in sys.argv
    n = 0
    print("properties:")
    for path in sorted(glob.glob(os.path.join(DATA, "properties", "*.json"))):
        _upload(path)
        n += 1
    if also_sheets:
        print("sheets:")
        for path in glob.glob(os.path.join(DATA, "sheets", "**", "*"), recursive=True):
            if os.path.isfile(path):
                _upload(path)
                n += 1
    print(f"done: {n} files uploaded to Blob")


if __name__ == "__main__":
    main()
