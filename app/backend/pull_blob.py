"""Copy the Vercel Blob store back down to the local `data/` tree —
the inverse of seed_blob.py. Use it to pull work saved on the deployed app
(properties, saved sheets) onto your machine.

Run it locally with the store's token in the environment:

    cd app/backend
    .venv\\Scripts\\activate
    pip install vercel_blob                 # only needed to run this pull
    # get the token: in the repo root run `vercel env pull` (writes .env.local
    # with BLOB_READ_WRITE_TOKEN), or copy it from the Vercel dashboard:
    #   Storage -> floorplan-data -> ".env.local" / Quickstart
    # then:
    python pull_blob.py                     # pull properties only
    python pull_blob.py --sheets            # also pull the saved-sheet library
    python pull_blob.py --all               # pull EVERY key in the store

It downloads each blob to the SAME relative path under data/ that the app reads,
so local dev sees exactly what the deployment has. It OVERWRITES local files with
the same name; it never deletes local-only files. Safe to re-run.
"""
import os
import sys


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


def _download(key):
    """Fetch one blob key and write it to its local path under data/."""
    path = os.path.join(DATA, key.replace("/", os.sep))
    data = storage.read_bytes(path)   # storage maps path -> key and reads the blob
    if data is None:
        print("  !! missing:", key)
        return False
    storage._fs_write_bytes(path, data)  # atomic local write, makes dirs
    print("  ->", key)
    return True


def main():
    if "--all" in sys.argv:
        prefixes = [""]
    else:
        prefixes = ["properties/"]
        if "--sheets" in sys.argv:
            prefixes.append("sheets/")

    n = 0
    for prefix in prefixes:
        blobs = storage._blob_list_all(prefix)
        print(f"{prefix or '(all)'}: {len(blobs)} blob(s)")
        for b in blobs:
            key = b.get("pathname")
            if key and _download(key):
                n += 1
    print(f"done: {n} files pulled from Blob into {DATA}")


if __name__ == "__main__":
    main()
