"""storage.py — the Vercel Blob pagination logic.

The filesystem path is exercised throughout test_api; here we fake the
`vercel_blob` SDK (so the suite needs neither the dependency nor the network) to
verify the list-based helpers walk the pagination cursor instead of silently
stopping after the first page — the latent correctness cliff for large
properties / a busy uploads cache.
"""
import unittest

import storage


class _FakeBlob:
    """Minimal vercel_blob.list() stand-in: pages a fixed set of pathnames,
    honoring `prefix` and the opaque `cursor` we hand back."""
    def __init__(self, pathnames, page_size=2):
        self.pathnames = pathnames
        self.page_size = page_size
        self.calls = 0

    def list(self, opts):
        self.calls += 1
        prefix = opts.get("prefix", "")
        cursor = int(opts.get("cursor", 0))
        matching = [p for p in self.pathnames if p.startswith(prefix)]
        page = matching[cursor:cursor + self.page_size]
        nxt = cursor + self.page_size
        has_more = nxt < len(matching)
        return {"blobs": [{"pathname": p, "url": "u/" + p} for p in page],
                "cursor": str(nxt) if has_more else None,
                "hasMore": has_more}


class BlobPaginationTest(unittest.TestCase):
    def setUp(self):
        self._using, self._blobfn = storage.USING_BLOB, storage._blob
        storage.USING_BLOB = True

    def tearDown(self):
        storage.USING_BLOB, storage._blob = self._using, self._blobfn

    def test_list_all_follows_cursor_across_pages(self):
        names = [f"sheets/p/{i:03}.png" for i in range(5)]   # 5 items @ page 2 -> 3 pages
        fake = _FakeBlob(names, page_size=2)
        storage._blob = lambda: fake
        got = storage._blob_list_all("sheets/p/")
        self.assertEqual(sorted(b["pathname"] for b in got), sorted(names))
        self.assertEqual(fake.calls, 3)            # all three pages walked, none dropped

    def test_find_locates_key_on_a_later_page(self):
        # the exact match lives past the first page — must still be found
        names = [f"u/{i:03}.json" for i in range(7)] + ["u/target.json"]
        storage._blob = lambda: _FakeBlob(names, page_size=3)
        self.assertIsNotNone(storage._blob_find("u/target.json"))
        self.assertIsNone(storage._blob_find("u/missing.json"))

    def test_single_page_does_not_overfetch(self):
        fake = _FakeBlob(["a/1", "a/2"], page_size=10)
        storage._blob = lambda: fake
        storage._blob_list_all("a/")
        self.assertEqual(fake.calls, 1)            # no needless second page request


if __name__ == "__main__":
    unittest.main()
