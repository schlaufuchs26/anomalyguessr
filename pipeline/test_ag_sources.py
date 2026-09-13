"""Tests for pipeline/ag_sources.py after the #1372 sourcing rebuild.

Run with: python3 -m unittest discover -s pipeline -t pipeline

No live network: the Commons adapter is tested against synthetic records and
the top-up walk against a stub adapter with a monkeypatched downloader. The
only live-network gate is the Commons smoke test behind
AG_SOURCES_NETWORK=1 (engineering-practices.md ticket #1084).
"""

import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path

import ag_sources as s


def commons_raw(title="Market Street", width=2000, height=1500,
                license="CC BY-SA 4.0", desc="A busy market street.",
                categories="Market|Streets", date_original="2013-10-24 15:02:48",
                assessments="quality", mime="image/jpeg", coords=None):
    raw = {
        "title": f"File:{title}.jpg",
        "pageid": 123,
        "imageinfo": [{
            "url": f"https://upload.example/{title}.jpg",
            "descriptionurl": f"https://commons.wikimedia.org/wiki/File:{title}.jpg",
            "width": width, "height": height, "mime": mime,
            "extmetadata": {
                "LicenseShortName": {"value": license},
                "LicenseUrl": {"value": "https://example.org/lic"},
                "ObjectName": {"value": title},
                "DateTimeOriginal": {"value": date_original},
                "DateTime": {"value": "2014-09-13 00:26:23"},
                "ImageDescription": {"value": desc},
                "Categories": {"value": categories},
                "Assessments": {"value": assessments},
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
        self.assertTrue(s.license_ok("No known restrictions"))
        self.assertTrue(s.license_ok("CC0"))

    def test_allow_cc_by_family(self):
        self.assertTrue(s.license_ok("CC BY 4.0"))
        self.assertTrue(s.license_ok("CC BY-SA 3.0"))

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


class KeyFilterTest(unittest.TestCase):
    def test_mime_allowlist(self):
        self.assertEqual(s.mime_ext("image/jpeg"), "jpg")
        self.assertEqual(s.mime_ext("IMAGE/PNG"), "png")
        self.assertEqual(s.mime_ext("application/pdf"), "")
        self.assertEqual(s.mime_ext("image/svg+xml"), "")

    def test_dimensions(self):
        self.assertTrue(s.dimensions_ok(2000, 1500))
        self.assertFalse(s.dimensions_ok(1500, 2000))   # portrait
        self.assertFalse(s.dimensions_ok(800, 600))     # too small
        self.assertFalse(s.dimensions_ok(None, None))

    def test_entry_reject_reason(self):
        good = {"license": "CC0", "mime": "image/jpeg", "width": 2000,
                "height": 1500}
        self.assertEqual(s.entry_reject_reason(good), "")
        self.assertEqual(
            s.entry_reject_reason({**good, "license": "All rights reserved"}),
            "license")
        self.assertIn("format",
                      s.entry_reject_reason({**good, "mime": "image/svg+xml"}))
        self.assertIn("orientation",
                      s.entry_reject_reason({**good, "width": 1500,
                                             "height": 2000}))


class BornDigitalTest(unittest.TestCase):
    def test_year_from_various_metadata_shapes(self):
        self.assertEqual(s.year_in_metadata("2013-10-24 15:02:48"), 2013)
        self.assertEqual(s.year_in_metadata("1900 date QS:P571,+1900-00-00T00:00:00Z/9"), 1900)
        self.assertEqual(s.year_in_metadata("1898"), 1898)
        self.assertIsNone(s.year_in_metadata(""))
        self.assertIsNone(s.year_in_metadata(None))

    def test_exif_year_prefers_capture_date(self):
        entry = {"raw": {"dateTimeOriginal": "1902", "dateTime": "2014-09-13"}}
        self.assertEqual(s.exif_year(entry), 1902)

    def test_born_digital_flag(self):
        self.assertTrue(s.is_born_digital(
            {"raw": {"dateTimeOriginal": "2019-06-09"}}))
        self.assertFalse(s.is_born_digital(
            {"raw": {"dateTimeOriginal": "1902"}}))
        self.assertFalse(s.is_born_digital({"raw": {}}))


class MetadataDateTest(unittest.TestCase):
    def test_prefers_original(self):
        self.assertEqual(s.metadata_date("2013-10-24 15:02:48", "2014-01-01"),
                         "2013-10-24")

    def test_wikidata_wrapper_reduced_to_year(self):
        self.assertEqual(
            s.metadata_date("1900 date QS:P571,+1900-00-00T00:00:00Z/9", ""),
            "1900")

    def test_empty_when_nothing(self):
        self.assertEqual(s.metadata_date("", None), "")


class PlaceTest(unittest.TestCase):
    def test_coordinates_are_not_a_place(self):
        # Ticket #1402: the raw GPS pair read as a place in the caption but
        # names nothing, so it stays provenance in raw.gps only.
        coords = [{"lat": 52.5159, "lon": 13.3793}]
        self.assertEqual(s.extract_place(categories="1900 in Berlin"), "Berlin")
        self.assertEqual(s.extract_place(categories=""), "")
        self.assertEqual(
            s.CommonsAdapter().normalize(commons_raw(coords=coords))["place"],
            "")

    def test_category_fallback(self):
        self.assertEqual(s.extract_place(categories="1900 in Hagåtña, Guam"),
                         "Hagåtña, Guam")
        self.assertEqual(s.extract_place(categories="Historical images of Paris"),
                         "Paris")
        # "August 2025 in Toronto": the month-prefixed location category.
        self.assertEqual(s.extract_place(categories="August 2025 in Toronto"),
                         "Toronto")

    def test_no_free_text_parsing(self):
        # A place mentioned only in a title is NOT used (the #1338 bug).
        self.assertEqual(s.extract_place(categories=""), "")

    def test_denied_categories(self):
        self.assertEqual(s.extract_place(categories="Unidentified locations in India"), "")


class CommonsNormalizeTest(unittest.TestCase):
    def setUp(self):
        self.adapter = s.CommonsAdapter()

    def test_accepts_quality_landscape(self):
        n = self.adapter.normalize(commons_raw())
        self.assertIsNotNone(n)
        self.assertEqual(n["repository"], "Wikimedia Commons")
        self.assertEqual(n["width"], 2000)
        self.assertEqual(n["mime"], "image/jpeg")
        self.assertEqual(n["quality"], "quality")
        self.assertTrue(s.quality_ok(n))
        self.assertEqual(n["date"], "2013-10-24")
        self.assertTrue(s.is_born_digital(n))
        self.assertEqual(n["raw"]["dateTimeOriginal"],
                         "2013-10-24 15:02:48")

    def test_coordinates_stay_provenance_not_place(self):
        n = self.adapter.normalize(commons_raw(
            coords=[{"lat": 1.5, "lon": 2.5, "type": "camera"}]))
        self.assertEqual(n["place"], "")
        self.assertEqual(n["raw"]["gps"]["lat"], 1.5)

    def test_rejects_portrait_small_license_mime(self):
        self.assertIsNone(self.adapter.normalize(
            commons_raw(width=1500, height=2000)))
        self.assertIsNone(self.adapter.normalize(
            commons_raw(width=800, height=600)))
        self.assertIsNone(self.adapter.normalize(
            commons_raw(license="All rights reserved")))
        self.assertIsNone(self.adapter.normalize(
            commons_raw(mime="image/svg+xml")))

    def test_quality_ok_false_without_assessment(self):
        n = self.adapter.normalize(commons_raw(assessments=""))
        self.assertFalse(s.quality_ok(n))

    def test_strips_html_from_title_and_description(self):
        raw = commons_raw()
        raw["imageinfo"][0]["extmetadata"]["ObjectName"] = {
            "value": '<div class="fn">Castle</div>'}
        n = self.adapter.normalize(raw)
        self.assertEqual(n["originalTitle"], "Castle")


class IndexTest(TempDirMixin, unittest.TestCase):
    def entry(self, sid="commons-x-abc123", used=False, license="CC0"):
        return {"id": sid, "repository": "Wikimedia Commons",
                "fileUrl": "https://c/1", "originalTitle": "X",
                "date": "2013-10-24", "place": "", "license": license,
                "licenseUrl": "", "description": "", "width": 2000,
                "height": 1500, "mime": "image/jpeg", "used": used,
                "image": f"images/{sid}.jpg"}

    def test_add_is_idempotent_and_preserves_used(self):
        s.add_sources(self.data_dir, [self.entry()], "2026-09-12")
        s.mark_used(self.data_dir, ["commons-x-abc123"], True)
        res = s.add_sources(self.data_dir, [self.entry()], "2026-09-12")
        self.assertEqual(res["added"], 0)
        self.assertEqual(res["skipped"], 1)
        index = s.load_index(self.data_dir)
        self.assertTrue(index["sources"]["commons-x-abc123"]["used"])

    def test_status_counts_born_digital(self):
        e = self.entry()
        e["raw"] = {"dateTimeOriginal": "2019-01-01"}
        s.add_sources(self.data_dir, [e], "2026-09-12")
        st = s.status(self.data_dir)
        self.assertEqual(st["total"], 1)
        self.assertEqual(st["unused"], 1)
        self.assertEqual(st["born_digital"], 1)

    def test_list_unused_and_mark_unused(self):
        s.add_sources(self.data_dir, [self.entry()], "2026-09-12")
        self.assertEqual(len(s.list_sources(self.data_dir, unused=True)), 1)
        s.mark_used(self.data_dir, ["commons-x-abc123"], True)
        self.assertEqual(len(s.list_sources(self.data_dir, unused=True)), 0)
        s.mark_used(self.data_dir, ["nope"], True)  # unknown id: ignored

    def test_prune_invalid_drops_and_removes_image(self):
        e = self.entry()
        e["license"] = "All rights reserved"
        img = s.images_dir(self.data_dir) / f"{e['id']}.jpg"
        img.parent.mkdir(parents=True, exist_ok=True)
        img.write_bytes(b"x")
        s.add_sources(self.data_dir, [e], "2026-09-12")
        res = s.prune_invalid(self.data_dir)
        self.assertEqual(len(res["removed"]), 1)
        self.assertEqual(res["images"], 1)
        self.assertFalse(img.exists())
        self.assertEqual(s.status(self.data_dir)["total"], 0)

    def test_paths_default_to_repo_data(self):
        self.assertTrue(str(s.default_data_dir()).endswith("data/anomalyguessr"))
        self.assertEqual(s.sources_dir(self.data_dir),
                         self.data_dir / "sources")


def stub_raw(title, born=1902, license="CC0"):
    return {"title": f"File:{title}.jpg", "pageid": 1,
            "imageinfo": [{"url": f"https://upload.example/{title}.jpg",
                           "descriptionurl": f"https://commons.example/{title}",
                           "width": 2000, "height": 1500,
                           "mime": "image/jpeg",
                           "extmetadata": {
                               "LicenseShortName": {"value": license},
                               "ObjectName": {"value": title},
                               "Assessments": {"value": "quality"},
                               "Categories": {"value": f"Quality images|{title}"},
                               "DateTimeOriginal": {"value": born}}}]}


class StubCommons(s.CommonsAdapter):
    """A CommonsAdapter whose pages come from memory (no network)."""

    pages = []
    calls = 0

    def quality_batch(self, limit, cursor=None):
        StubCommons.calls += 1
        start = int(cursor or 0)
        chunk = self.pages[start:start + limit]
        nxt = str(start + limit) if start + limit < len(self.pages) else ""
        return chunk, nxt


class SelectCandidatesTest(unittest.TestCase):
    def test_filters_quality_and_counts_born_digital(self):
        adapter = s.CommonsAdapter()
        raws = [commons_raw(title="Good", assessments="quality"),
                commons_raw(title="Bad", assessments="featured|potd")]
        res = s.select_candidates(raws, adapter)
        self.assertEqual(len(res["pairs"]), 1)
        self.assertEqual(res["quality_rejected"], 1)
        self.assertEqual(res["born_digital"], 1)

    def test_exclude_born_digital_flag(self):
        adapter = s.CommonsAdapter()
        old = s.EXCLUDE_BORN_DIGITAL
        s.EXCLUDE_BORN_DIGITAL = True
        try:
            res = s.select_candidates([commons_raw()], adapter)
            self.assertEqual(len(res["pairs"]), 0)
            self.assertEqual(res["born_digital"], 1)
        finally:
            s.EXCLUDE_BORN_DIGITAL = old


class TopUpTest(TempDirMixin, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self._old_cls = s.CommonsAdapter
        StubCommons.calls = 0
        StubCommons.pages = [stub_raw(f"Photo {i}") for i in range(6)]
        s.CommonsAdapter = StubCommons
        # Downloads write a placeholder file instead of hitting the network.
        self._old_fetch = s._fetch_and_save
        s._fetch_and_save = lambda url, target: (
            target.parent.mkdir(parents=True, exist_ok=True),
            target.write_bytes(b"img"), 3)[-1]

    def tearDown(self):
        s.CommonsAdapter = self._old_cls
        s._fetch_and_save = self._old_fetch
        super().tearDown()

    def test_walks_category_and_reaches_target(self):
        rep = s.top_up(self.data_dir, target=4, batch=2, max_calls=5)
        self.assertEqual(rep["stopped"], "target reached")
        self.assertEqual(rep["after"]["unused"], 4)
        self.assertEqual(rep["examples"], ["Photo 0", "Photo 1", "Photo 2"])
        self.assertTrue(rep["added"] >= 4)

    def test_resumes_from_persisted_cursor(self):
        s.top_up(self.data_dir, target=2, batch=2, max_calls=1)
        first = s.status(self.data_dir)["total"]
        self.assertEqual(first, 2)
        rep = s.top_up(self.data_dir, target=6, batch=2, max_calls=5)
        # The second run continues at page 2; nothing is duplicated.
        self.assertEqual(rep["skipped"], 0)
        self.assertEqual(rep["after"]["total"],
                         len(StubCommons.pages))

    def test_reports_cursor_and_stops_when_exhausted(self):
        rep = s.top_up(self.data_dir, target=99, batch=2, max_calls=10)
        self.assertEqual(rep["stopped"], "category exhausted")
        self.assertEqual(rep["shortfall"], 99 - len(StubCommons.pages))
        state = s._load_quality_state(self.data_dir)
        self.assertEqual(state.get("continue"), "")

    def test_healthy_pool_short_circuits(self):
        s.top_up(self.data_dir, target=1, batch=2, max_calls=1)
        calls = StubCommons.calls
        rep = s.top_up(self.data_dir, target=1, batch=2, max_calls=1)
        self.assertEqual(rep["stopped"], "pool healthy")
        self.assertEqual(StubCommons.calls, calls)

    def test_rejected_counted_not_downloaded(self):
        StubCommons.pages = [stub_raw("Portrait") , stub_raw("Small")]
        for raw in StubCommons.pages:
            raw["imageinfo"][0]["height"] = 4000  # portrait, rejected
        rep = s.top_up(self.data_dir, target=5, batch=2, max_calls=2)
        self.assertEqual(rep["added"], 0)
        self.assertEqual(rep["rejected"], 2)
        self.assertEqual(rep["downloaded"], 0)


class ManualSeedTest(TempDirMixin, unittest.TestCase):
    def test_manual_import(self):
        src = self._tmp / "manual"
        src.mkdir()
        (src / "photo.jpg").write_bytes(b"img")
        (src / s.MANUAL_META).write_text(json.dumps({
            "photo.jpg": {"license": "CC0", "originalTitle": "Old photo",
                          "width": 1200, "height": 800,
                          "repository": "Manual"}}))
        res = s.seed_backend(self.data_dir, "manual", "", 10, "2026-09-12",
                             {"directory": src})
        self.assertEqual(res["added"], 1)
        self.assertEqual(res["downloaded"], 1)
        self.assertEqual(s.entry_reject_reason(
            s.list_sources(self.data_dir)[0]), "")


@unittest.skipUnless(os.environ.get("AG_SOURCES_NETWORK") == "1",
                     "live Commons smoke test (AG_SOURCES_NETWORK=1)")
class CommonsLiveTest(unittest.TestCase):
    def test_quality_batch_returns_pages(self):
        adapter = s.CommonsAdapter()
        raws, cursor = adapter.quality_batch(3)
        self.assertTrue(raws)
        self.assertTrue(cursor)


if __name__ == "__main__":
    unittest.main()
