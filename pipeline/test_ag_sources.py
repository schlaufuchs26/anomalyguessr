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
                "height": 1500, "year": 1900, "year_field": "catalog",
                "year_source": "dc:date", "year_raw": "1900"}
        self.assertEqual(s.entry_reject_reason(good), "")
        self.assertEqual(
            s.entry_reject_reason({**good, "license": "All rights reserved"}),
            "license")
        self.assertIn("format",
                      s.entry_reject_reason({**good, "mime": "image/svg+xml"}))
        self.assertIn("orientation",
                      s.entry_reject_reason({**good, "width": 1500,
                                             "height": 2000}))
        # #1405: a source without a single unambiguous year is inadmissible.
        self.assertIn("year",
                      s.entry_reject_reason({**good, "year": None,
                                             "year_field": ""}))
        # #1430: the year must be a catalogue date. Commons' file-page
        # declared capture date ("exif"/"structured") is not one: measured, it
        # cannot be told apart from a scan/upload stamp (#1418).
        self.assertIn("catalog",
                      s.entry_reject_reason({**good, "year_field": "exif"}))
        self.assertIn("catalog",
                      s.entry_reject_reason({**good, "year_field": "structured"}))


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

    def test_single_year_rejects_ambiguous_forms(self):
        # #1405: the year must be a fact from one structured field, not a
        # guess. Ranges, decades, centuries and uncertainty markers fall out.
        for good, want in (("2013-10-24 15:02:48", 2013),
                           ("1900-01-20", 1900),
                           ("1900", 1900),
                           ("2009-05", 2009)):
            self.assertEqual(s.single_year(good), want, good)
        for bad in ("", None, "1907?", "1900s", "19th century", "circa 1900",
                    "[ca. 1900]", "about 1900", "1890-1900", "1820–50",
                    "1903/19uu", "between 1890 and 1900", "1900 or 1901",
                    "early 1900s",
                    # #1533: the Commons pattypan/Wikidata wrapper is not an
                    # assertion; it used to read as exactly 1900.
                    "1900 date QS:P571,+1900-00-00T00:00:00Z/9"):
            self.assertIsNone(s.single_year(bad), bad)

    def test_source_year_prefers_exif_then_structured(self):
        # The EXIF/template capture date wins; the structured date is the
        # fallback. The upload/file date is never a year source.
        self.assertEqual(
            s.source_year({"raw": {"dateTimeOriginal": "1902-05-01",
                                   "dateTime": "2014-09-13"},
                           "date": "2014-09-13"}),
            (1902, "exif"))
        self.assertEqual(s.source_year({"date": "1899-05-01"}),
                         (1899, "structured"))
        self.assertEqual(
            s.source_year({"raw": {"dateTime": "1899"}, "date": ""}),
            (None, ""))
        self.assertEqual(s.source_year({"date": "1907?"}), (None, ""))

    def test_structured_date_repeating_the_upload_stamp_is_rejected(self):
        # A "structured" date that just repeats the upload timestamp is not a
        # creation date (#1405 trap 2).
        self.assertEqual(
            s.source_year({"date": "2014-09-13",
                           "raw": {"uploadTimestamp": "2014-09-13T06:14:18Z"}}),
            (None, ""))
        self.assertEqual(
            s.source_year({"date": "1900-01-20",
                           "raw": {"uploadTimestamp": "2014-09-13T06:14:18Z"}}),
            (1900, "structured"))


class MetadataDateTest(unittest.TestCase):
    def test_prefers_original(self):
        self.assertEqual(s.metadata_date("2013-10-24 15:02:48", "2014-01-01"),
                         "2013-10-24")

    def test_wikidata_wrapper_is_not_a_date(self):
        # #1533: "1900 date QS:P571,+1900-00-00T00:00:00Z/9" is a pattypan
        # marker whose hidden P571 statement is not the photo's year; it used
        # to normalize to "1900" and anchor the item there.
        self.assertEqual(
            s.metadata_date("1900 date QS:P571,+1900-00-00T00:00:00Z/9", ""),
            "")
        # A real timestamp after a wrapper still wins.
        self.assertEqual(
            s.metadata_date("1900 date QS:P571,+1900-00-00T00:00:00Z/9",
                            "2013-10-24 15:02:48"),
            "2013-10-24")

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
        self.assertEqual(n["year"], 2013)
        self.assertEqual(n["year_field"], "exif")
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


class MeasureTest(unittest.TestCase):
    def test_measure_candidates_reports_survival_and_fields(self):
        adapter = s.CommonsAdapter()
        modern = commons_raw(title="Modern",
                             date_original="2019-06-09 10:00:00")
        old = commons_raw(title="Old")
        old["imageinfo"][0]["extmetadata"].pop("DateTimeOriginal")
        old["imageinfo"][0]["extmetadata"]["DateTime"] = {
            "value": "1900-01-20 00:00:00"}
        # A capture field that is uncertain and a file date that just repeats
        # the upload stamp: no admissible year.
        ambiguous = commons_raw(title="Circa", date_original="circa 1900")
        ambiguous["imageinfo"][0]["extmetadata"]["DateTime"] = {
            "value": "2014-09-13 00:00:00"}
        ambiguous["imageinfo"][0]["timestamp"] = "2014-09-13T00:00:00Z"
        portrait = commons_raw(title="Portrait", width=1500, height=2000)
        rep = s.measure_candidates([modern, old, ambiguous, portrait], adapter)
        self.assertEqual(rep["sample"], 4)
        self.assertEqual(rep["normalize_rejected"], 1)   # portrait
        self.assertEqual(rep["normalized"], 3)
        self.assertEqual(rep["survivors"], 2)
        self.assertEqual(rep["year_rejected"], 1)
        self.assertEqual(rep["exif_anchored"], 1)
        self.assertEqual(rep["structured_anchored"], 1)
        self.assertEqual(rep["structured_equals_upload"], 1)


class InceptionClaimTest(unittest.TestCase):
    """SDC P571 parsing (#1418): the time value, not a heuristics guess."""

    def test_reads_a_day_precision_claim(self):
        st = {"P571": [{"mainsnak": {"datavalue": {
            "value": {"time": "+2005-09-10T00:00:00Z", "precision": 11},
            "type": "time"}}}]}
        self.assertEqual(s.inception_year(st), (2005, 11))

    def test_reads_a_year_precision_claim(self):
        # Commons normalizes a year-only claim to a zero month/day.
        st = {"P571": [{"mainsnak": {"datavalue": {
            "value": {"time": "+1900-00-00T00:00:00Z", "precision": 9},
            "type": "time"}}}]}
        self.assertEqual(s.inception_year(st), (1900, 9))

    def test_missing_claim_is_none(self):
        self.assertEqual(s.inception_year({}), (None, None))
        self.assertEqual(s.inception_year(None), (None, None))

    def test_unusable_claims_fall_through_to_the_next(self):
        st = {"P571": [
            {"mainsnak": {"snaktype": "somevalue"}},
            {"mainsnak": {"datavalue": {"value": "not a date",
                                        "type": "string"}}},
            {"mainsnak": {"datavalue": {
                "value": {"time": "+2038-01-19T00:00:00Z", "precision": 11},
                "type": "time"}}},
        ]}
        self.assertEqual(s.inception_year(st), (2038, 11))

    def test_other_claims_are_ignored(self):
        st = {"P275": [{"mainsnak": {"datavalue": {
            "value": {"time": "+1900-00-00T00:00:00Z", "precision": 9},
            "type": "time"}}}]}
        self.assertEqual(s.inception_year(st), (None, None))

    def test_circa_qualifier_marks_the_claim_approximate(self):
        # The time value alone reads as an exact year; the uncertainty lives
        # in the P1480 (sourcing circumstances) qualifier.
        st = {"P571": [{"mainsnak": {"datavalue": {
            "value": {"time": "+1900-01-01T00:00:00Z", "precision": 9},
            "type": "time"}},
            "qualifiers": {"P1480": [{"datavalue": {
                "value": {"id": "Q5727902"}, "type": "wikibase-entityid"}}]}}]}
        self.assertEqual(s.inception_year(st), (1900, 9))
        self.assertTrue(s.inception_is_approximate(st))

    def test_before_after_tolerance_marks_the_claim_approximate(self):
        st = {"P571": [{"mainsnak": {"datavalue": {
            "value": {"time": "+1900-00-00T00:00:00Z", "precision": 9,
                      "before": 5, "after": 0},
            "type": "time"}}}]}
        self.assertTrue(s.inception_is_approximate(st))

    def test_plain_claim_is_not_approximate(self):
        st = {"P571": [{"mainsnak": {"datavalue": {
            "value": {"time": "+2013-08-29T00:00:00Z", "precision": 11},
            "type": "time"}}}]}
        self.assertFalse(s.inception_is_approximate(st))
        self.assertFalse(s.inception_is_approximate({}))


class InceptionStatsTest(unittest.TestCase):
    """The #1418 comparison: does P571 add anything over the EXIF year?"""

    def setUp(self):
        self.adapter = s.CommonsAdapter()

    def norm(self, raw):
        entry = self.adapter.normalize(raw)
        self.assertIsNotNone(entry)
        return entry

    def row(self, title, raw, inception, precision=11):
        return {"title": f"File:{title}.jpg", "inception": inception,
                "precision": precision, "entry": self.norm(raw)}

    def test_counts_matches_differences_and_rescues(self):
        modern = commons_raw(title="Modern",
                             date_original="2019-06-09 10:00:00")
        # A scan: the capture date is the digitization stamp, and the claim
        # repeats it instead of naming the historical year.
        scan = commons_raw(title="Scan",
                           date_original="2005-09-30 08:11:46")
        scan["imageinfo"][0]["extmetadata"]["DateTime"] = {
            "value": "2015-01-10 20:04:50"}
        # Where the claim differs, it carries a photo-foreign year.
        mismatched = commons_raw(title="Mismatch",
                                 date_original="2013-08-29 09:00:00")
        # No capture date, and the structured date repeats the upload stamp:
        # the #1405 filter refuses the file, but the claim names a year.
        rescued = commons_raw(title="Rescued", date_original="2011-10-04")
        rescued["imageinfo"][0]["extmetadata"].pop("DateTimeOriginal")
        rescued["imageinfo"][0]["extmetadata"]["DateTime"] = {
            "value": "2014-09-13 00:00:00"}
        rescued["imageinfo"][0]["timestamp"] = "2014-09-13T00:00:00Z"
        rows = [
            self.row("Modern", modern, 2019),
            self.row("Scan", scan, 2005),
            self.row("Mismatch", mismatched, 1304, precision=9),
            self.row("Rescued", rescued, 2011),
        ]
        rows[0]["approximate"] = True
        rep = s.inception_stats(rows)
        self.assertEqual(rep["sample"], 4)
        self.assertEqual(rep["has_p571"], 4)
        self.assertEqual(rep["approximate"], 1)
        self.assertEqual(rep["p571_precision"], {"9": 1, "11": 3})
        self.assertEqual(rep["matches_exif"], 2)
        self.assertEqual(rep["differs_from_exif"], 1)
        self.assertEqual(rep["exif_missing"], 1)
        self.assertEqual(rep["matches_anchor"], 2)
        self.assertEqual(rep["matches_structured"], 2)
        self.assertEqual(rep["matches_upload"], 0)
        self.assertEqual(rep["filter_rejected"], 1)
        self.assertEqual(rep["filter_rejected_with_p571"], 1)
        self.assertEqual(rep["differences"],
                         [{"title": "File:Mismatch.jpg", "inception": 1304,
                           "exif": 2013, "structured": 2013, "upload": None,
                           "anchor": 2013}])

    def test_a_row_without_a_claim_only_feeds_the_filter_counts(self):
        raw = commons_raw(title="NoClaim", date_original="2013-10-24")
        rep = s.inception_stats([{"title": "File:NoClaim.jpg",
                                  "inception": None, "precision": None,
                                  "entry": self.norm(raw)}])
        self.assertEqual(rep["has_p571"], 0)
        self.assertEqual(rep["p571_precision"], {})
        self.assertEqual(rep["filter_rejected"], 0)
        self.assertEqual(rep["differences"], [])


class InceptionBatchTest(unittest.TestCase):
    """The adapter reads MediaInfo claims 50 titles at a time (#1418)."""

    def test_chunks_titles_and_reads_both_claim_keys(self):
        calls = []

        def fake_api(params):
            calls.append(params)
            ents = {}
            for i, title in enumerate(params["titles"].split("|")):
                key = "statements" if i % 2 else "claims"
                ents[title] = {"title": title, key: {"P571": []}}
            return {"entities": ents}

        titles = [f"File:F{i}.jpg" for i in range(60)]
        original = s._commons_api
        s._commons_api = fake_api
        try:
            claims = s.CommonsAdapter().inception_batch(titles)
        finally:
            s._commons_api = original
        self.assertEqual([len(c["titles"].split("|")) for c in calls],
                         [50, 10])
        self.assertEqual(calls[0]["action"], "wbgetentities")
        self.assertEqual(calls[0]["sites"], "commonswiki")
        self.assertEqual(len(claims), 60)

    def test_file_title_reads_raw_then_url(self):
        self.assertEqual(s.file_title({"raw": {"title": "File:A.jpg"}}),
                         "File:A.jpg")
        self.assertEqual(
            s.file_title({"fileUrl":
                          "https://commons.wikimedia.org/wiki/File:Some_File.jpg"}),
            "File:Some File.jpg")
        self.assertEqual(s.file_title({}), "")


class YearAuditTest(TempDirMixin, unittest.TestCase):
    def test_year_audit_flags_pool_entries_without_a_year(self):
        good = {"id": "commons-good-000000", "repository": "Wikimedia Commons",
                "fileUrl": "", "originalTitle": "Good", "date": "2013-10-24",
                "place": "", "license": "CC0", "licenseUrl": "",
                "description": "", "width": 2000, "height": 1500,
                "used": True}
        bad = {**good, "id": "commons-bad-000000", "originalTitle": "Bad",
               "date": "circa 1900", "used": False}
        s.add_sources(self.data_dir, [good, bad], "2026-09-13")
        rep = s.year_audit(self.data_dir)
        self.assertEqual(rep["total"], 2)
        self.assertEqual(rep["rejected"], ["commons-bad-000000"])
        self.assertEqual(rep["rejected_used"], 0)
        self.assertEqual(rep["structured_anchored"], 1)
        self.assertEqual(rep["decades"], {2010: 1})


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


class StubCatalogAdapter(s.SourceAdapter):
    """Adapter returning one catalog-year entry verbatim (no network)."""

    repo = "Stub"
    repo_tag = "stub"

    def __init__(self, norm=None):
        self.norm = norm

    def normalize(self, raw):
        return self.norm

    def download_url(self, raw):
        return "https://example.org/img.jpg"


def catalog_entry(**over):
    entry = {"license": "Public domain", "mime": "image/jpeg",
             "width": 2000, "height": 1500, "year": 1910,
             "year_field": "catalog", "year_source": "dc:date",
             "year_raw": "1910"}
    entry.update(over)
    return entry


class SelectCandidatesTest(unittest.TestCase):
    def test_commons_candidates_fail_the_catalog_year_gate(self):
        # #1430: Commons' file-page declared capture date is not a catalogue
        # date, so the pool gate refuses every Commons candidate.
        res = s.select_candidates([commons_raw()], s.CommonsAdapter())
        self.assertEqual(len(res["pairs"]), 0)
        self.assertEqual(res["year_rejected"], 1)

    def test_catalog_candidate_survives(self):
        adapter = StubCatalogAdapter(catalog_entry())
        res = s.select_candidates([{"id": "x"}], adapter)
        self.assertEqual(res["pairs"], [({"id": "x"}, catalog_entry())])

    def test_quality_filter_counts_before_the_year_gate(self):
        adapter = s.CommonsAdapter()
        raws = [commons_raw(title="Good", assessments="quality"),
                commons_raw(title="Bad", assessments="featured|potd")]
        res = s.select_candidates(raws, adapter)
        self.assertEqual(res["quality_rejected"], 1)
        self.assertEqual(res["year_rejected"], 1)

    def test_commons_placeholder_date_is_no_year(self):
        # #1533: the pattypan/Wikidata wrapper ("1900 date QS:P571,...") is
        # not an assertion of the photo's year; it used to read as 1900.
        raw = stub_raw("Street scene 1900",
                       born='1900<div style="display: none;">date QS:P571,'
                            '+1900-00-00T00:00:00Z/9</div>')
        entry = s.CommonsAdapter().normalize(raw)
        self.assertIsNone(entry["year"])
        self.assertEqual(entry["date"], "")
        self.assertIn("year", s.entry_reject_reason(entry))
        res = s.select_candidates([raw], s.CommonsAdapter())
        self.assertEqual(res["year_rejected"], 1)

    def test_exclude_born_digital_flag(self):
        adapter = StubCatalogAdapter(catalog_entry(
            raw={"dateTimeOriginal": "2019-06-09"}))
        old = s.EXCLUDE_BORN_DIGITAL
        s.EXCLUDE_BORN_DIGITAL = True
        try:
            res = s.select_candidates([{"id": "x"}], adapter)
            self.assertEqual(len(res["pairs"]), 0)
            self.assertEqual(res["born_digital"], 1)
        finally:
            s.EXCLUDE_BORN_DIGITAL = old


class GallicaTest(unittest.TestCase):
    SRU = """<srw:searchRetrieveResponse xmlns:srw="http://www.loc.gov/zing/srw/"
      xmlns:oai_dc="http://www.openarchives.org/OAI/2.0/oai_dc/"
      xmlns:dc="http://purl.org/dc/elements/1.1/">
      <srw:numberOfRecords>2</srw:numberOfRecords>
      <srw:records>
        <srw:record><srw:recordData><oai_dc:dc>
          <dc:title>Rue Saint-Louis en l'Ile : [photographie] / E. Atget</dc:title>
          <dc:date>1906-1907</dc:date>
          <dc:identifier>https://gallica.bnf.fr/ark:/12148/btv1b10516422n</dc:identifier>
          <dc:rights>domaine public</dc:rights>
          <dc:rights>public domain</dc:rights>
        </oai_dc:dc></srw:recordData></srw:record>
        <srw:record><srw:recordData><oai_dc:dc>
          <dc:title>Aveugles [de guerre] aux Quinze-Vingts</dc:title>
          <dc:date>1916</dc:date>
          <dc:identifier>https://gallica.bnf.fr/ark:/12148/btv1b6945669k</dc:identifier>
          <dc:rights>domaine public</dc:rights>
        </oai_dc:dc></srw:recordData></srw:record>
      </srw:records>
    </srw:searchRetrieveResponse>"""

    def test_parse_sru_records(self):
        recs = s.parse_gallica_records(self.SRU)
        self.assertEqual(len(recs), 2)
        self.assertEqual(recs[0]["ark"], "btv1b10516422n")
        self.assertEqual(recs[0]["dates"], ["1906-1907"])
        self.assertEqual(recs[1]["dates"], ["1916"])
        self.assertIn("public domain", recs[0]["rights"])

    def test_catalog_year_rejects_ranges_and_disagreement(self):
        self.assertEqual(s.gallica_catalog_year(["1916"]), (1916, "1916"))
        # A range is not a year; two disagreeing dates are not either.
        self.assertEqual(s.gallica_catalog_year(["1906-1907"]), (None, ""))
        self.assertEqual(s.gallica_catalog_year(["1910", "1912"]), (None, ""))
        self.assertEqual(s.gallica_catalog_year([]), (None, ""))
        # Two dates that name the same year are one year.
        self.assertEqual(s.gallica_catalog_year(["1910", "1910-05"]),
                         (1910, "1910"))

    def test_normalize_requires_public_domain_and_year(self):
        adapter = s.GallicaAdapter()
        raw = {"title": "Rue", "dates": ["1916"], "rights": ["domaine public"],
               "ark": "btv1b6945669k", "width": 3000, "height": 2000}
        entry = adapter.normalize(raw)
        self.assertEqual(entry["year"], 1916)
        self.assertEqual(entry["year_field"], "catalog")
        self.assertEqual(entry["year_source"], "dc:date")
        self.assertEqual(entry["year_raw"], "1916")
        self.assertEqual(entry["license"], "Public domain")
        # The stored size is the served (capped) copy, not the 3000 px original.
        self.assertEqual(entry["width"], s.GALLICA_IMAGE_WIDTH)
        self.assertLess(entry["height"], 2000)
        self.assertIsNone(adapter.normalize({**raw, "rights": ["in copyright"]}))
        self.assertIsNone(adapter.normalize({**raw, "width": 800,
                                             "height": 600}))
        self.assertIsNone(adapter.normalize({**raw, "ark": ""}))
        # A range or circa value has no single year; the entry carries none
        # and the pool's year gate refuses it.
        ranged = adapter.normalize({**raw, "dates": ["1906-1907"]})
        self.assertIsNone(ranged["year"])
        self.assertEqual(ranged["year_raw"], "1906-1907")
        self.assertIn("year", s.entry_reject_reason(ranged))

    def test_image_url_caps_the_width(self):
        self.assertEqual(s.gallica_image_url("abc", 8000),
                         f"{s.GALLICA_IIIF}/abc/f1/full/2000,/0/native.jpg")
        self.assertEqual(s.gallica_image_url("abc", 900),
                         f"{s.GALLICA_IIIF}/abc/f1/full/900,/0/native.jpg")
        self.assertIn("/full/full/", s.gallica_image_url("abc", 0))

    def test_walk_rotates_through_the_query_list(self):
        # #1533: the walk used to drain the first query (Agence Rol,
        # 1908-1914) forever, which is why the whole pool was one archive and
        # two decades. It now takes one page per query in rotation.
        adapter = s.GallicaAdapter()
        seen = []

        def fake_sru(query, limit, start):
            seen.append((query, start))
            if query == s.GALLICA_WALK_QUERIES[0]:
                return [{"ark": f"a{start + i}", "dates": ["1910"]}
                        for i in range(limit)], 100
            return [], 0

        old_size = s.gallica_size
        adapter._sru = fake_sru
        s.gallica_size = lambda ark: (2000, 1500)  # no network
        try:
            raws, cursor = adapter.walk_batch(2, None)
            self.assertEqual(len(raws), 2)
            # One page from the first query, then the walk moves on, so the
            # next call samples a different collection.
            self.assertEqual(cursor["query"], 1)
            self.assertEqual(cursor["starts"], {"0": 3})
            raws2, _cursor2 = adapter.walk_batch(2, cursor)
            self.assertEqual(len(raws2), 2)
            self.assertIn(s.GALLICA_WALK_QUERIES[1], [q for q, _ in seen])
        finally:
            s.gallica_size = old_size

    def test_walk_parks_queries_and_ends_when_all_are_done(self):
        adapter = s.GallicaAdapter()
        adapter._sru = lambda query, limit, start: ([], 0)
        old_size = s.gallica_size
        s.gallica_size = lambda ark: (2000, 1500)
        try:
            raws, cursor = adapter.walk_batch(2, None)
            self.assertEqual(raws, [])
            self.assertIsNone(cursor)
        finally:
            s.gallica_size = old_size


class NbTest(unittest.TestCase):
    """National Library of Norway adapter (ticket #1535)."""

    def test_catalog_date_forms_become_one_year(self):
        # Sesam stores the date compactly: YYYY, YYYYMM or YYYYMMDD.
        self.assertEqual(s.nb_date_text("1899"), "1899")
        self.assertEqual(s.nb_date_text("189312"), "1893-12")
        self.assertEqual(s.nb_date_text("18990101"), "1899-01-01")
        self.assertEqual(s.nb_catalog_year("1899"), (1899, "1899"))
        self.assertEqual(s.nb_catalog_year("189312"), (1893, "1893-12"))
        self.assertEqual(s.nb_catalog_year("18990101"), (1899, "1899-01-01"))
        # Empty, range, decade and circa values are not a single year.
        for value in ("", "1900-1910", "1900s", "ca. 1900", "19th century"):
            self.assertIsNone(s.nb_catalog_year(value)[0])

    def test_normalize_stores_the_catalog_year_and_capped_size(self):
        adapter = s.NbAdapter()
        entry = adapter.normalize(nb_raw(date="18990101", width=8000,
                                         height=5300))
        self.assertEqual(entry["repository"], "National Library of Norway")
        self.assertEqual(entry["year"], 1899)
        self.assertEqual(entry["year_field"], "catalog")
        self.assertEqual(entry["year_source"], "metadata.dateCreated")
        self.assertEqual(entry["year_raw"], "18990101")
        self.assertEqual(entry["date"], "1899-01-01")
        self.assertEqual(entry["license"], "Public domain")
        # The stored size is the served (capped) copy, not the 8000 px original.
        self.assertEqual(entry["width"], s.NB_IMAGE_WIDTH)
        self.assertLess(entry["height"], 5300)
        self.assertEqual(entry["fileUrl"],
                         f"{s.NB_ITEM}/{NAMED_URN}")
        self.assertEqual(s.entry_reject_reason(entry), "")
        # The catalogue's "Ukjent" placeholder is not a maker.
        self.assertEqual(entry["raw"]["creator"], "")

    def test_normalize_rejects_non_photo_non_pd_and_no_year(self):
        adapter = s.NbAdapter()
        # "bilder" also holds maps/books; only the digifoto URN is photos.
        self.assertIsNone(adapter.normalize(
            nb_raw(urn="URN:NBN:no-nb_digibok_20200101_0001")))
        self.assertIsNone(adapter.normalize(nb_raw(urn="")))
        self.assertIsNone(adapter.normalize(nb_raw(pd=False,
                                                   license="ccbysa")))
        self.assertIsNone(adapter.normalize(nb_raw(pd=False,
                                                   license="copyrighted")))
        self.assertIsNone(adapter.normalize(nb_raw(width=800, height=600)))
        # A range or circa value has no single year; the entry carries none
        # and the pool's year gate refuses it.
        ranged = adapter.normalize(nb_raw(date="1900-1910"))
        self.assertIsNone(ranged["year"])
        self.assertEqual(ranged["year_field"], "")
        self.assertIn("year", s.entry_reject_reason(ranged))

    def test_image_url_caps_the_width(self):
        self.assertEqual(s.nb_image_url("URN:x", 8000),
                         f"{s.NB_IMAGE}/URN:x/full/2000,/0/native.jpg")
        self.assertEqual(s.nb_image_url("URN:x", 900),
                         f"{s.NB_IMAGE}/URN:x/full/900,/0/native.jpg")
        self.assertIn("/full/full/", s.nb_image_url("URN:x", 0))

    def test_walk_rotates_through_the_query_list(self):
        adapter = s.NbAdapter()
        seen = []

        def fake_search(query, limit, page):
            seen.append((query, page))
            if query == s.NB_WALK_QUERIES[0]:
                return [nb_raw(title=f"Gate {page}")], 100
            return [], 0

        old_search, old_size = adapter._search_page, s.nb_size
        adapter._search_page = fake_search
        s.nb_size = lambda urn: (2000, 1500)  # no network
        try:
            raws, cursor = adapter.walk_batch(1, None)
            self.assertEqual(len(raws), 1)
            # One page from the first term, then the walk moves on, so the
            # next call samples a different subject.
            self.assertEqual(cursor["query"], 1)
            self.assertEqual(cursor["pages"], {"0": 1})
            raws2, _cursor2 = adapter.walk_batch(1, cursor)
            self.assertEqual(len(raws2), 1)
            self.assertIn(s.NB_WALK_QUERIES[1], [q for q, _ in seen])
        finally:
            adapter._search_page = old_search
            s.nb_size = old_size

    def test_walk_parks_queries_and_ends_when_all_are_done(self):
        adapter = s.NbAdapter()
        old_search = adapter._search_page
        adapter._search_page = staticmethod(lambda query, limit, page: ([], 0))
        try:
            raws, cursor = adapter.walk_batch(2, None)
            self.assertEqual(raws, [])
            self.assertIsNone(cursor)
        finally:
            adapter._search_page = old_search


NAMED_URN = "URN:NBN:no-nb_digifoto_20150807_00158_bldsa_PK06252"


def nb_raw(title="Rosenkrantz gate", date="1899", urn=NAMED_URN,
           license="publicdomain", pd=True, width=8000, height=5300,
           creators=("Ukjent",)):
    """One Sesam item shaped like the live API's search response."""
    return {
        "metadata": {
            "title": title,
            "dateCreated": date,
            "creators": list(creators),
            "identifiers": {"urn": urn},
            "geographic": {"placeString": "Norge;Oslo;Oslo;;;"},
            "originInfo": "",
        },
        "accessInfo": {"isPublicDomain": pd, "license": license},
        "width": width, "height": height,
        "query": "gate",
    }


class StubGallica(s.GallicaAdapter):
    """A GallicaAdapter whose walk comes from memory (no network)."""

    pages = []
    calls = 0

    def walk_batch(self, limit, cursor=None):
        StubGallica.calls += 1
        start = int((cursor or {}).get("start") or 1) - 1
        chunk = self.pages[start:start + limit]
        nxt = {"query": 0, "start": start + limit + 1} \
            if start + len(chunk) < len(self.pages) else None
        return chunk, nxt


def gallica_raw(tag=0, dates=("1910",), rights=("domaine public",), width=3000,
                height=2000):
    return {"title": f"Rue {tag} : [photographie de presse] / [Agence Rol]",
            "dates": list(dates), "rights": list(rights), "creator": "",
            "description": "", "ark": "btv1b" + str(tag).lower(), "query": "q",
            "width": width, "height": height}


class TopUpTest(TempDirMixin, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self._old_cls = s.ADAPTERS["gallica"]
        StubGallica.calls = 0
        StubGallica.pages = [gallica_raw(i) for i in range(6)]
        s.ADAPTERS["gallica"] = StubGallica
        # Downloads write a placeholder file instead of hitting the network.
        self._old_fetch = s._fetch_and_save
        s._fetch_and_save = lambda url, target: (
            target.parent.mkdir(parents=True, exist_ok=True),
            target.write_bytes(b"img"), 3)[-1]

    def tearDown(self):
        s.ADAPTERS["gallica"] = self._old_cls
        s._fetch_and_save = self._old_fetch
        super().tearDown()

    def test_walks_source_and_reaches_target(self):
        rep = s.top_up(self.data_dir, target=4, batch=2, max_calls=5)
        self.assertEqual(rep["source"], "gallica")
        self.assertEqual(rep["stopped"], "target reached")
        self.assertEqual(rep["after"]["unused"], 4)
        self.assertEqual(rep["examples"], ["Rue 0 : [photographie de presse] "
                                           "/ [Agence Rol]",
                                           "Rue 1 : [photographie de presse] "
                                           "/ [Agence Rol]",
                                           "Rue 2 : [photographie de presse] "
                                           "/ [Agence Rol]"])
        self.assertTrue(rep["added"] >= 4)

    def test_resumes_from_persisted_cursor(self):
        s.top_up(self.data_dir, target=2, batch=2, max_calls=1)
        first = s.status(self.data_dir)["total"]
        self.assertEqual(first, 2)
        rep = s.top_up(self.data_dir, target=6, batch=2, max_calls=5)
        # The second run continues at page 2; nothing is duplicated.
        self.assertEqual(rep["skipped"], 0)
        self.assertEqual(rep["after"]["total"],
                         len(StubGallica.pages))

    def test_reports_cursor_and_stops_when_exhausted(self):
        rep = s.top_up(self.data_dir, target=99, batch=2, max_calls=10)
        self.assertEqual(rep["stopped"], "source exhausted")
        self.assertEqual(rep["shortfall"], 99 - len(StubGallica.pages))
        self.assertIsNone(s._load_walk_state(self.data_dir, "gallica"))

    def test_healthy_pool_short_circuits(self):
        s.top_up(self.data_dir, target=1, batch=2, max_calls=1)
        calls = StubGallica.calls
        rep = s.top_up(self.data_dir, target=1, batch=2, max_calls=1)
        self.assertEqual(rep["stopped"], "pool healthy")
        self.assertEqual(StubGallica.calls, calls)

    def test_rejected_counted_not_downloaded(self):
        StubGallica.pages = [gallica_raw("Portrait", width=2000, height=4000),
                             gallica_raw("Small", width=800, height=600)]
        rep = s.top_up(self.data_dir, target=5, batch=2, max_calls=2)
        self.assertEqual(rep["added"], 0)
        self.assertEqual(rep["rejected"], 2)
        self.assertEqual(rep["downloaded"], 0)

    def test_year_rejected_counted_separately(self):
        StubGallica.pages = [gallica_raw("Dated"),
                             gallica_raw("Range", dates=("circa 1900",))]
        rep = s.top_up(self.data_dir, target=5, batch=2, max_calls=2)
        self.assertEqual(rep["added"], 1)
        self.assertEqual(rep["year_rejected"], 1)
        self.assertEqual(rep["downloaded"], 1)

    def test_non_catalog_source_adds_nothing(self):
        # #1430: the gate refuses a file-page date, so a Commons walk reports
        # every candidate as year-rejected instead of filling the pool.
        s.ADAPTERS["commons"] = StubCommons
        StubCommons.pages = [stub_raw(f"Photo {i}") for i in range(2)]
        try:
            rep = s.top_up(self.data_dir, target=5, batch=2, max_calls=1,
                           source="commons")
        finally:
            del s.ADAPTERS["commons"]
        self.assertEqual(rep["added"], 0)
        self.assertEqual(rep["year_rejected"], 2)


class StubWalkAdapter(s.SourceAdapter):
    """A walk adapter whose pages come from memory (no network)."""

    repo = "Stub"
    repo_tag = "stub"
    entries = []
    calls = 0

    def walk_batch(self, limit, cursor=None):
        StubWalkAdapter.calls += 1
        start = int(cursor or 0)
        chunk = self.entries[start:start + limit]
        nxt = str(start + len(chunk)) \
            if start + len(chunk) < len(self.entries) else None
        return chunk, nxt

    def normalize(self, raw):
        return dict(raw)

    def download_url(self, raw):
        return "https://example.org/img.jpg"


class StubArchiveA(StubWalkAdapter):
    repo = "Archive A"
    repo_tag = "archa"
    entries = []


class StubArchiveB(StubWalkAdapter):
    repo = "Archive B"
    repo_tag = "archb"
    entries = []


class StubArchiveC(StubWalkAdapter):
    repo = "Archive C"
    repo_tag = "archc"
    entries = []


def walk_entry(title, year, repo="Gallica", **over):
    """A normalized pool entry with a catalogue year (refresh fixtures)."""
    entry = catalog_entry(year=year, year_raw=str(year), year_source="dc:date")
    entry.update({"repository": repo, "originalTitle": title,
                  "fileUrl": f"https://example.org/{title}", "date": str(year),
                  "place": "", "description": ""})
    entry["id"] = f"{repo.lower().replace(' ', '-')}-{title}"
    entry.update(over)
    return entry


class SpreadTest(unittest.TestCase):
    def test_cap_is_a_third_of_the_batch(self):
        self.assertEqual(s.spread_cap(30), 10)
        self.assertEqual(s.spread_cap(31), 11)
        self.assertEqual(s.spread_cap(5), 2)
        self.assertGreaterEqual(s.spread_cap(0), 1)

    def test_report_counts_repositories_and_decades(self):
        entries = [walk_entry("a", 1901), walk_entry("b", 1908, repo="Other"),
                   walk_entry("c", 1922, repo="Other")]
        rep = s.spread_report(entries)
        self.assertEqual(rep["total"], 3)
        self.assertEqual(rep["repositories"]["Gallica"]["count"], 1)
        self.assertEqual(rep["repositories"]["Other"]["count"], 2)
        self.assertEqual(rep["decades"][1900]["count"], 2)
        self.assertEqual(rep["distinct_decades"], 2)

    def test_pool_spread_ok_ignores_the_repo_cap_with_one_repository(self):
        # One reachable archive: judging the repository share would starve
        # the pool, so only the decades are held to the cap.
        one_decade = [walk_entry(f"a{i}", 1901) for i in range(6)]
        self.assertFalse(s.pool_spread_ok(one_decade))
        spread = [walk_entry(f"b{i}", 1901 + 10 * i) for i in range(6)]
        self.assertTrue(s.pool_spread_ok(spread))

    def test_pool_spread_ok_enforces_the_repo_cap_with_several(self):
        entries = [walk_entry(f"a{i}", 1901 + 10 * i,
                              repo="A" if i < 4 else "B") for i in range(8)]
        # 4 of 8 from one repository: over a third.
        self.assertFalse(s.pool_spread_ok(entries))

    def test_warning_names_the_thin_spread(self):
        one_repo = s.spread_report([walk_entry("a", 1901),
                                    walk_entry("b", 1911)])
        self.assertIn("one repository", s.spread_warning(one_repo))
        few = s.spread_report([walk_entry("a", 1901, repo="A"),
                               walk_entry("b", 1911, repo="B")])
        self.assertIn("2 decade(s)", s.spread_warning(few))
        good = s.spread_report([walk_entry("a", 1901, repo="A"),
                                walk_entry("b", 1911, repo="B"),
                                walk_entry("c", 1921, repo="C")])
        self.assertEqual(s.spread_warning(good), "")


class RefreshPoolTest(TempDirMixin, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self._old_fetch = s._fetch_and_save
        s._fetch_and_save = lambda url, target: (
            target.parent.mkdir(parents=True, exist_ok=True),
            target.write_bytes(b"img"), 3)[-1]
        self._saved = dict(s.ADAPTERS)
        StubWalkAdapter.calls = 0
        for cls in (StubArchiveA, StubArchiveB, StubArchiveC):
            cls.entries = [walk_entry(f"{cls.repo_tag}-{i}", 1901 + 10 * (i % 4),
                                      repo=cls.repo) for i in range(12)]

    def tearDown(self):
        s.ADAPTERS.clear()
        s.ADAPTERS.update(self._saved)
        s._fetch_and_save = self._old_fetch
        super().tearDown()

    def _seed_skewed_pool(self):
        # 9 of 10 entries one repository and one decade, like the live pool.
        s.add_sources(self.data_dir,
                      [walk_entry(f"old{i}", 1908) for i in range(9)]
                      + [walk_entry("other", 1922, repo="Other Archive")],
                      "2026-09-01")

    def _register(self):
        s.ADAPTERS["a"] = StubArchiveA
        s.ADAPTERS["b"] = StubArchiveB
        s.ADAPTERS["c"] = StubArchiveC

    def test_refresh_spreads_the_added_batch(self):
        # #1533: a skewed pool refills into a batch with at most a third per
        # repository and per decade, instead of a dump of the same archive
        # and the same twenty years.
        self._seed_skewed_pool()
        self._register()
        rep = s.refresh_pool(self.data_dir, target=9, batch=20, max_calls=8,
                             sources=["a", "b", "c"])
        self.assertEqual(rep["cap"], 3)
        self.assertEqual(rep["added"], 9)
        for bucket in ("repositories", "decades"):
            for name, cell in rep["batch_spread"][bucket].items():
                self.assertLessEqual(cell["count"], rep["cap"],
                                     f"{bucket} {name}")
        self.assertGreater(rep["capped"], 0)
        self.assertEqual(rep["spread"]["total"], 19)

    def test_refresh_stops_when_the_pool_is_healthy_and_spread(self):
        self._register()
        s.add_sources(self.data_dir,
                      [walk_entry(f"w{i}", 1901 + 10 * i) for i in range(9)],
                      "2026-09-01")
        rep = s.refresh_pool(self.data_dir, target=9, batch=20, max_calls=8,
                             sources=["a", "b", "c"])
        self.assertEqual(rep["stopped"], "pool healthy and spread")
        self.assertEqual(StubWalkAdapter.calls, 0)

    def test_refresh_reports_an_unwalkable_source(self):
        self._seed_skewed_pool()
        rep = s.refresh_pool(self.data_dir, target=99, batch=2, max_calls=1,
                             sources=["nope"])
        self.assertEqual(rep["stopped"], "no walkable source")
        self.assertTrue(rep["errors"])


class ManualSeedTest(TempDirMixin, unittest.TestCase):
    def test_manual_import(self):
        src = self._tmp / "manual"
        src.mkdir()
        (src / "photo.jpg").write_bytes(b"img")
        (src / s.MANUAL_META).write_text(json.dumps({
            "photo.jpg": {"license": "CC0", "originalTitle": "Old photo",
                          "date": "1900-01-20", "width": 1200, "height": 800,
                          "year": 1900, "year_field": "catalog",
                          "year_source": "catalogue",
                          "repository": "Manual"}}))
        res = s.seed_backend(self.data_dir, "manual", "", 10, "2026-09-12",
                             {"directory": src})
        self.assertEqual(res["added"], 1)
        self.assertEqual(res["downloaded"], 1)
        self.assertEqual(res["year_rejected"], 0)
        entry = s.list_sources(self.data_dir)[0]
        self.assertEqual(entry["year"], 1900)
        self.assertEqual(entry["year_field"], "catalog")
        self.assertEqual(entry["year_source"], "catalogue")
        self.assertEqual(s.entry_reject_reason(entry), "")

    def test_manual_import_with_a_file_page_date_is_rejected(self):
        # #1430: an import that only has a file-page date (no catalogue
        # assertion) does not pass the pool's year gate.
        src = self._tmp / "manual3"
        src.mkdir()
        (src / "photo.jpg").write_bytes(b"img")
        (src / s.MANUAL_META).write_text(json.dumps({
            "photo.jpg": {"license": "CC0", "originalTitle": "Old photo",
                          "date": "1900-01-20", "width": 1200, "height": 800,
                          "repository": "Manual"}}))
        res = s.seed_backend(self.data_dir, "manual", "", 10, "2026-09-12",
                             {"directory": src})
        self.assertEqual(res["added"], 0)
        self.assertEqual(res["year_rejected"], 1)

    def test_manual_import_without_a_year_is_rejected(self):
        # #1405: a manual photo with no single unambiguous year never enters
        # the pool.
        src = self._tmp / "manual2"
        src.mkdir()
        (src / "photo.jpg").write_bytes(b"img")
        (src / s.MANUAL_META).write_text(json.dumps({
            "photo.jpg": {"license": "CC0", "originalTitle": "Undated",
                          "width": 1200, "height": 800,
                          "repository": "Manual"}}))
        res = s.seed_backend(self.data_dir, "manual", "", 10, "2026-09-12",
                             {"directory": src})
        self.assertEqual(res["added"], 0)
        self.assertEqual(res["year_rejected"], 1)
        self.assertEqual(s.status(self.data_dir)["total"], 0)


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
