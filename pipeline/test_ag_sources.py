"""Tests for pipeline/ag_sources.py (ticket #1170).

Run with: python3 scripts/test_ag_sources.py

No live network is used: the Commons/LOC adapters are tested against
synthetic records, and the download/save path against a local file:// URL or
a monkeypatched fetcher. The only live-network gate is behind
AG_SOURCES_NETWORK=1 (engineering-practices.md ticket #1084).
"""

import io
import json
import shutil
import subprocess
import tempfile
import unittest
import urllib.error
from pathlib import Path

import ag_sources as s

SCRIPTS_DIR = Path(__file__).resolve().parent


def make_img(path: Path, w=1200, h=800, color="gray"):
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["convert", "-size", f"{w}x{h}", f"xc:{color}", str(path)],
        check=True, capture_output=True,
    )


def commons_raw(title="Market Street 1900", width=2000, height=1500,
                license="Public domain", desc="A busy market street.",
                categories="Market|Streets", date_original="1900",
                coords=None):
    raw = {
        "title": f"File:{title}.jpg",
        "pageid": 123,
        "imageinfo": [{
            "url": f"https://upload.example/{title}.jpg",
            "descriptionurl": f"https://commons.wikimedia.org/wiki/File:{title}.jpg",
            "width": width, "height": height,
            "extmetadata": {
                "LicenseShortName": {"value": license},
                "LicenseUrl": {"value": "https://example.org/lic"},
                "ObjectName": {"value": title},
                "DateTimeOriginal": {"value": date_original},
                "ImageDescription": {"value": desc},
                "Categories": {"value": categories},
            },
        }],
    }
    if coords:
        raw["coordinates"] = coords
    return raw


class TempDirMixin:
    def setUp(self):
        self._tmp = Path(tempfile.mkdtemp())
        self.data_dir = self._tmp / "anomalyguessr"
        self.data_dir.mkdir(parents=True)

    def tearDown(self):
        shutil.rmtree(self._tmp, ignore_errors=True)


class LicenseTest(unittest.TestCase):
    def test_allow_public_domain(self):
        self.assertTrue(s.license_ok("Public domain"))
        self.assertTrue(s.license_ok("Public Domain"))
        self.assertTrue(s.license_ok("No known restrictions"))

    def test_allow_cc_by_family(self):
        self.assertTrue(s.license_ok("CC BY 4.0"))
        self.assertTrue(s.license_ok("CC BY-SA 3.0"))
        self.assertTrue(s.license_ok("CC0"))

    def test_deny_noncommercial_and_nd(self):
        self.assertFalse(s.license_ok("CC BY-NC 4.0"))
        self.assertFalse(s.license_ok("CC BY-ND 4.0"))
        self.assertFalse(s.license_ok("All rights reserved"))

    def test_deny_empty(self):
        self.assertFalse(s.license_ok(""))
        self.assertFalse(s.license_ok(None))


class NormalizeIdTest(unittest.TestCase):
    def test_deterministic_and_stable(self):
        a = s.normalize_id("commons", "Market Street", "1900", "http://u/1")
        b = s.normalize_id("commons", "Market Street", "1900", "http://u/1")
        self.assertEqual(a, b)

    def test_different_file_url_differs(self):
        a = s.normalize_id("commons", "Market Street", "1900", "http://u/1")
        b = s.normalize_id("commons", "Market Street", "1900", "http://u/2")
        self.assertNotEqual(a, b)

    def test_slug_shape(self):
        sid = s.normalize_id("commons", "Market, Street!", "1900", "http://u/1")
        self.assertRegex(sid, r"^commons-market-street-\d{4}-[0-9a-f]{6}$")


class CommonsAdapterTest(unittest.TestCase):
    def setUp(self):
        self.adapter = s.CommonsAdapter()

    def test_normalize_landscape_pd(self):
        raw = commons_raw()
        n = self.adapter.normalize(raw)
        self.assertIsNotNone(n)
        self.assertEqual(n["repository"], "Wikimedia Commons")
        self.assertEqual(n["license"], "Public domain")
        self.assertEqual(n["width"], 2000)
        self.assertEqual(n["originalTitle"], "Market Street 1900")
        self.assertEqual(n["fileUrl"],
                         "https://commons.wikimedia.org/wiki/File:Market Street 1900.jpg")
        self.assertEqual(n["description"], "A busy market street.")
        self.assertIn("raw", n)

    def test_normalize_rejects_portrait(self):
        raw = commons_raw(width=1500, height=2000)
        self.assertIsNone(self.adapter.normalize(raw))

    def test_normalize_rejects_small(self):
        raw = commons_raw(width=800, height=600)
        self.assertIsNone(self.adapter.normalize(raw))

    def test_normalize_rejects_bad_license(self):
        raw = commons_raw(license="CC BY-NC 4.0")
        self.assertIsNone(self.adapter.normalize(raw))

    def test_normalize_missing_imageinfo(self):
        self.assertIsNone(self.adapter.normalize({"title": "File:X.jpg"}))

    def test_normalize_place_from_categories(self):
        raw = commons_raw(title="Untitled view",
                          categories="1900 in Hagåtña, Guam|PD US NOAA")
        n = self.adapter.normalize(raw)
        self.assertEqual(n["place"], "Hagåtña, Guam")

    def test_normalize_place_from_gps_when_no_text(self):
        raw = commons_raw(title="Untitled view", categories="PD US",
                          coords=[{"lat": 52.51588889, "lon": 13.37925,
                                   "type": "camera"}])
        n = self.adapter.normalize(raw)
        self.assertEqual(n["place"], "52.5159, 13.3793")
        self.assertEqual(n["raw"]["gps"]["lat"], 52.51588889)

    def test_normalize_keeps_raw_metadata_date_for_the_id(self):
        # The raw metadata date feeds the deterministic id; the improved
        # ``date`` must not change it (a changed id would re-add a used photo).
        raw = commons_raw(title="Market Street 1900",
                          date_original="2005-09-30 08:11:46")
        n = self.adapter.normalize(raw)
        self.assertEqual(n["_idDate"], "2005-09-30 08:11:46")
        self.assertEqual(n["date"], "circa 1900")

    def test_normalize_strips_markup_from_the_title(self):
        raw = commons_raw(title="Market Street 1900")
        raw["imageinfo"][0]["extmetadata"]["ObjectName"]["value"] = (
            "<div class=\"fn\">Boer War market</div>")
        n = self.adapter.normalize(raw)
        self.assertEqual(n["originalTitle"], "Boer War market")


class PlaceTest(unittest.TestCase):
    def test_place_from_title_in(self):
        self.assertEqual(s.place_from_text("Street scene in Agana (1899-1900)"),
                         "Agana")

    def test_place_from_title_tail(self):
        self.assertEqual(s.place_from_text("Exposition, Paris, France"),
                         "Paris, France")

    def test_place_from_title_empty_when_unparsable(self):
        self.assertEqual(s.place_from_text("Unnamed view"), "")

    def test_place_from_categories_year_in(self):
        self.assertEqual(
            s.place_from_categories("1900 in Hagåtña, Guam|PD US NOAA"),
            "Hagåtña, Guam")

    def test_place_from_categories_decade(self):
        self.assertEqual(s.place_from_categories("India in the 1900s|Photos"),
                         "India")

    def test_place_from_categories_historical_images(self):
        self.assertEqual(
            s.place_from_categories("Historical images of Boulevard des Capucines"),
            "Boulevard des Capucines")

    def test_place_from_categories_rejects_unidentified_and_lowercase(self):
        self.assertEqual(s.place_from_categories("Unidentified locations in India"),
                         "")
        self.assertEqual(s.place_from_categories("1900 in art"), "")

    def test_extract_place_priority_title_then_category_then_gps(self):
        self.assertEqual(
            s.extract_place(title="Street scene in Agana",
                            categories="1900 in Paris"), "Agana")
        self.assertEqual(
            s.extract_place(title="Untitled view", categories="1900 in Paris"),
            "Paris")
        self.assertEqual(
            s.extract_place(title="Untitled view",
                            gps=[{"lat": 52.51588889, "lon": 13.37925}]),
            "52.5159, 13.3793")

    def test_extract_place_unparsable_is_empty(self):
        self.assertEqual(s.extract_place(title="Untitled view",
                                         categories="PD US NOAA"), "")


class CommonsDateTest(unittest.TestCase):
    def test_modern_exif_stamp_falls_back_to_title_year(self):
        self.assertEqual(
            s._commons_date("2005-09-30 08:11:46", "2005-09-30 08:11:46",
                            "Street scene in Agana (1899-1900)", ""),
            "circa 1899")

    def test_historical_metadata_date_is_kept(self):
        self.assertEqual(
            s._commons_date("1900-01-01 00:00:00", "", "Title"), "1900-01-01 00:00:00")

    def test_wikidata_wrapper_is_unwrapped_to_a_year(self):
        self.assertEqual(
            s._commons_date("1900 date QS:P571,+1900-00-00T00:00:00Z/9", "",
                            "Street scene 1900"), "1900")

    def test_wikidata_century_decade_wrapper(self):
        self.assertEqual(
            s._commons_date("1900s date QS:P,+1900/00/00", "", "Toronto market"),
            "1900")

    def test_no_signal_is_empty(self):
        self.assertEqual(s._commons_date("", "", "", ""), "")


class LocAdapterTest(unittest.TestCase):
    def setUp(self):
        self.adapter = s.LocAdapter()

    def test_normalize_landscape(self):
        raw = {
            "id": "https://www.loc.gov/item/abc",
            "title": "Market Street, 1900",
            "date": "1900",
            "image": ["https://tile.loc.gov/full.jpg"],
            "rights": ["No known restrictions on publication."],
            "description": ["A busy street."],
        }
        n = self.adapter.normalize(raw)
        self.assertIsNotNone(n)
        self.assertEqual(n["repository"], "Library of Congress")
        self.assertEqual(n["license"], "No known restrictions on publication.")
        self.assertNotIn("image", n)  # normalize does not set image/download
        self.assertEqual(self.adapter.download_url(raw),
                         "https://tile.loc.gov/full.jpg")

    def test_normalize_rejects_no_image(self):
        raw = {"id": "x", "title": "t", "image": []}
        self.assertIsNone(self.adapter.normalize(raw))

    def test_normalize_rejects_restricted(self):
        raw = {
            "id": "x", "title": "t", "image": ["http://x.jpg"],
            "rights": ["Restricted: all rights reserved"],
        }
        self.assertIsNone(self.adapter.normalize(raw))


class IndexTest(TempDirMixin, unittest.TestCase):
    def _entry(self, eid, license="Public domain"):
        return {
            "id": eid,
            "repository": "Test",
            "fileUrl": f"http://example/{eid}",
            "originalTitle": "Scene " + eid,
            "date": "1900",
            "place": "",
            "license": license,
            "licenseUrl": "",
            "description": "desc",
            "image": f"images/{eid}.jpg",
            "width": 1200, "height": 800,
            "raw": {},
        }

    def test_add_and_merge(self):
        res = s.add_sources(self.data_dir, [self._entry("a"), self._entry("b")], "2026-09-11")
        self.assertEqual(res, {"added": 2, "skipped": 0, "refreshed": 0})
        res2 = s.add_sources(self.data_dir, [self._entry("a")], "2026-09-11")
        self.assertEqual(res2, {"added": 0, "skipped": 1, "refreshed": 0})
        idx = s.load_index(self.data_dir)
        self.assertEqual(len(idx["sources"]), 2)
        self.assertEqual(idx["sources"]["a"]["used"], False)
        self.assertEqual(idx["sources"]["a"]["added"], "2026-09-11")

    def test_refresh_updates_metadata_but_keeps_used_and_added(self):
        s.add_sources(self.data_dir, [self._entry("a")], "2026-09-11")
        s.mark_used(self.data_dir, ["a"], True)
        updated = self._entry("a")
        updated["place"] = "Agana"
        updated["date"] = "circa 1899"
        res = s.add_sources(self.data_dir, [updated], "2026-09-12")
        self.assertEqual(res, {"added": 0, "skipped": 1, "refreshed": 1})
        src = s.load_index(self.data_dir)["sources"]["a"]
        self.assertEqual(src["place"], "Agana")
        self.assertEqual(src["date"], "circa 1899")
        self.assertTrue(src["used"])          # used-tracking survives
        self.assertEqual(src["added"], "2026-09-11")

    def test_refresh_off_leaves_stale_metadata(self):
        s.add_sources(self.data_dir, [self._entry("a")], "d")
        updated = self._entry("a")
        updated["place"] = "Agana"
        res = s.add_sources(self.data_dir, [updated], "d", refresh=False)
        self.assertEqual(res, {"added": 0, "skipped": 1, "refreshed": 0})
        self.assertEqual(s.load_index(self.data_dir)["sources"]["a"]["place"], "")

    def test_mark_used(self):
        s.add_sources(self.data_dir, [self._entry("a"), self._entry("b")], "d")
        upd = s.mark_used(self.data_dir, ["a", "missing"], True)
        self.assertEqual(upd, ["a"])
        self.assertTrue(s.load_index(self.data_dir)["sources"]["a"]["used"])
        self.assertFalse(s.load_index(self.data_dir)["sources"]["b"]["used"])
        # mark-unused flips back; idempotent
        self.assertEqual(s.mark_used(self.data_dir, ["a"], False), ["a"])
        self.assertFalse(s.load_index(self.data_dir)["sources"]["a"]["used"])
        self.assertEqual(s.mark_used(self.data_dir, ["a"], False), [])

    def test_status_and_list(self):
        s.add_sources(self.data_dir, [self._entry("a"), self._entry("b")], "d")
        s.mark_used(self.data_dir, ["a"], True)
        st = s.status(self.data_dir)
        self.assertEqual(st["total"], 2)
        self.assertEqual(st["used"], 1)
        self.assertEqual(st["unused"], 1)
        unused = s.list_sources(self.data_dir, unused=True)
        self.assertEqual([x["id"] for x in unused], ["b"])


class SeedTest(TempDirMixin, unittest.TestCase):
    def test_manual_seed(self):
        src_dir = self._tmp / "manual"
        src_dir.mkdir()
        make_img(src_dir / "photo1.jpg", 1200, 800)
        meta = {
            "photo1.jpg": {
                "repository": "Example Archive",
                "originalTitle": "Old Market",
                "date": "1910",
                "license": "Public domain",
                "description": "A market.",
            }
        }
        (src_dir / s.MANUAL_META).write_text(json.dumps(meta))
        res = s.seed_backend(self.data_dir, "manual", "", 10, "2026-09-11",
                             {"directory": src_dir})
        self.assertEqual(res["added"], 1)
        self.assertEqual(res["rejected"], 0)
        idx = s.load_index(self.data_dir)
        src = next(iter(idx["sources"].values()))
        self.assertEqual(src["originalTitle"], "Old Market")
        self.assertEqual(src["width"], 1200)
        # image copied on disk
        img = self.data_dir / "sources" / src["image"]
        self.assertTrue(img.exists())

    def test_manual_rejects_no_license(self):
        src_dir = self._tmp / "manual2"
        src_dir.mkdir()
        make_img(src_dir / "photo1.jpg", 1200, 800)
        # no metadata.json -> normalize returns None -> rejected
        res = s.seed_backend(self.data_dir, "manual", "", 10, "d",
                             {"directory": src_dir})
        self.assertEqual(res["rejected"], 1)
        self.assertEqual(res["added"], 0)


class SeedBackendUrlTest(TempDirMixin, unittest.TestCase):
    """Seed through a stubbed Commons adapter to test the download/save path
    without a live network (gated real calls behind AG_SOURCES_NETWORK)."""

    def test_download_and_add(self):
        raw = commons_raw()
        # Stub the adapter's search + a fake fetcher by replacing _fetch_and_save.
        class FakeCommons(s.CommonsAdapter):
            def search(self, query, limit, offset=0):
                return [raw]

        orig = s.ADAPTERS["commons"]
        s.ADAPTERS["commons"] = FakeCommons
        orig_fetch = s._fetch_and_save
        downloaded = {}

        def fake_fetch(url, target):
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(b"fakeimage")
            downloaded[url] = str(target)

        s._fetch_and_save = fake_fetch
        try:
            res = s.seed_backend(self.data_dir, "commons", "market street", 5, "d")
            # re-seed is idempotent (skipped, not duplicated)
            res2 = s.seed_backend(self.data_dir, "commons", "market street", 5, "d")
        finally:
            s.ADAPTERS["commons"] = orig
            s._fetch_and_save = orig_fetch
        self.assertEqual(res["added"], 1)
        self.assertEqual(res["rejected"], 0)
        self.assertEqual(res2["added"], 0)
        idx = s.load_index(self.data_dir)
        src = next(iter(idx["sources"].values()))
        self.assertTrue(self.data_dir / "sources" / src["image"] in
                        [Path(v) for v in downloaded.values()])
        self.assertEqual(len(idx["sources"]), 1)


class TopUpTest(TempDirMixin, unittest.TestCase):
    """Top-up paging/dedup against stubbed Commons searches (no network)."""

    def _install(self, search_fn):
        class FakeCommons(s.CommonsAdapter):
            def search(self, query, limit, offset=0):
                return search_fn(query, limit, offset)

        self._orig_adapter = s.ADAPTERS["commons"]
        self._orig_fetch = s._fetch_and_save
        s.ADAPTERS["commons"] = FakeCommons

        def fake_fetch(url, target):
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(b"fakeimage")

        s._fetch_and_save = fake_fetch

    def tearDown(self):
        s.ADAPTERS["commons"] = self._orig_adapter
        s._fetch_and_save = self._orig_fetch
        super().tearDown()

    def test_top_up_fills_pool_and_is_idempotent_preserving_used(self):
        per = {
            "q1": [commons_raw(title="Market Street A"),
                   commons_raw(title="Market Street B")],
            "q2": [commons_raw(title="Harbour C"),
                   commons_raw(title="Harbour D")],
        }
        self._install(lambda q, limit, offset: per.get(q, [])[offset:offset + limit])
        rep = s.top_up(self.data_dir, target=3, limit=10,
                       queries=["q1", "q2"], max_calls=5)
        self.assertEqual(rep["added"], 4)
        self.assertEqual(rep["after"]["unused"], 4)
        self.assertEqual(rep["stopped"], "target reached")

        first = sorted(s.load_index(self.data_dir)["sources"])[0]
        s.mark_used(self.data_dir, [first], True)
        rep2 = s.top_up(self.data_dir, target=4, limit=10,
                        queries=["q1", "q2"], max_calls=5)
        idx = s.load_index(self.data_dir)
        self.assertEqual(len(idx["sources"]), 4)         # no duplicate entries
        self.assertTrue(idx["sources"][first]["used"])   # used survives re-seed
        self.assertEqual(rep2["added"], 0)

    def test_top_up_pages_deeper_on_repeat_runs(self):
        raws = [commons_raw(title=f"Street {i}") for i in range(30)]
        offsets = []

        def search_fn(query, limit, offset):
            offsets.append(offset)
            return raws[offset:offset + limit]

        self._install(search_fn)
        first = s.top_up(self.data_dir, target=100, limit=10,
                         queries=["q"], max_calls=1)
        second = s.top_up(self.data_dir, target=100, limit=10,
                          queries=["q"], max_calls=1)
        self.assertEqual(first["added"], 10)
        self.assertEqual(second["added"], 10)
        self.assertEqual(offsets, [0, 10])
        self.assertEqual(s.status(self.data_dir)["total"], 20)

    def test_top_up_is_a_noop_when_the_pool_is_healthy(self):
        self._install(lambda q, limit, offset: [commons_raw(title="Market Street A")])
        s.top_up(self.data_dir, target=1, limit=10, queries=["q"], max_calls=3)
        rep = s.top_up(self.data_dir, target=1, limit=10, queries=["q"], max_calls=3)
        self.assertEqual(rep["stopped"], "pool healthy")
        self.assertEqual(rep["calls"], 0)

    def test_top_up_records_a_dead_query_and_continues(self):
        err = urllib.error.HTTPError("u", 429, "slow down", {}, io.BytesIO())

        def search_fn(query, limit, offset):
            if query == "bad":
                raise err
            return [commons_raw(title="Market Street X")]

        self._install(search_fn)
        rep = s.top_up(self.data_dir, target=5, limit=10,
                       queries=["bad", "good"], max_calls=2)
        err.close()
        self.assertEqual(len(rep["errors"]), 1)
        self.assertEqual(rep["added"], 1)

    def test_top_up_reports_a_shortfall(self):
        self._install(lambda q, limit, offset: [commons_raw(title="Market Street A")])
        rep = s.top_up(self.data_dir, target=10, limit=10,
                       queries=["q"], max_calls=1)
        self.assertEqual(rep["shortfall"], 9)


class BackoffTest(unittest.TestCase):
    def test_rate_limit_is_retried_then_succeeds(self):
        calls = {"n": 0}

        def fn():
            calls["n"] += 1
            if calls["n"] < 3:
                raise urllib.error.HTTPError("u", 429, "slow down", {}, io.BytesIO())
            return "ok"

        self.assertEqual(s._with_backoff(fn, retries=3, backoff=(0, 0, 0)), "ok")
        self.assertEqual(calls["n"], 3)

    def test_non_retryable_error_raises_immediately(self):
        calls = {"n": 0}

        def fn():
            calls["n"] += 1
            raise urllib.error.HTTPError("u", 404, "nope", {}, io.BytesIO())

        with self.assertRaises(urllib.error.HTTPError) as cm:
            s._with_backoff(fn, retries=3, backoff=(0, 0, 0))
        cm.exception.close()
        self.assertEqual(calls["n"], 1)


class LiveNetworkTest(unittest.TestCase):
    """Optional live tests, gated behind AG_SOURCES_NETWORK=1.

    Engineering-practices.md ticket #1084: unit tests must not dial live
    services (flake source); the deterministic path is covered by synthetic
    fixtures above. These exercise the real Commons API once, opt-in.
    """

    @unittest.skipUnless(__import__("os").environ.get("AG_SOURCES_NETWORK"),
                         "set AG_SOURCES_NETWORK=1 to run live network tests")
    def test_commons_seed_live(self):
        import tempfile
        tmp = Path(tempfile.mkdtemp())
        try:
            data_dir = tmp / "anomalyguessr"
            res = s.seed_backend(data_dir, "commons", "market street", 3, "2026-09-11")
            self.assertEqual(res["added"], 3)
            self.assertEqual(res["rejected"], 0)
            idx = s.load_index(data_dir)
            self.assertEqual(len(idx["sources"]), 3)
            for src in idx["sources"].values():
                self.assertTrue((data_dir / "sources" / src["image"]).exists())
                self.assertGreater(src["width"], 0)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
