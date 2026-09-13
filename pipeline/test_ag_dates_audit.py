"""Tests for the #1430 date-exactness audit (pure parts; no network).

Run with: python3 -m unittest discover -s pipeline -t pipeline
"""

import unittest

import ag_dates_audit as a


class DateKindTest(unittest.TestCase):
    def test_classifies_exact_approximate_and_missing(self):
        self.assertEqual(a.date_kind("1912"), "exact")
        self.assertEqual(a.date_kind("1912-05-01"), "exact")
        for bad in ("1906-1907", "c. 1920", "1900s", "19th century", ""):
            self.assertEqual(a.date_kind(bad),
                             "none" if bad == "" else "approximate", bad)


class AnalyzeTest(unittest.TestCase):
    def rows(self):
        return [
            # exact year, usable license, landscape and wide enough
            {"title": "a", "date": "1912", "date_field": "dc:date",
             "license_ok": True, "width": 3000, "height": 2000},
            # exact year but portrait
            {"title": "b", "date": "1912", "date_field": "dc:date",
             "license_ok": True, "width": 1500, "height": 2000},
            # a range, licence fine
            {"title": "c", "date": "1906-1907", "date_field": "dc:date",
             "license_ok": True, "width": 3000, "height": 2000},
            # in copyright, no date
            {"title": "d", "date": "", "date_field": "dc:date",
             "license_ok": False, "width": 3000, "height": 2000},
        ]

    def test_counts_and_shares(self):
        st = a.analyze(self.rows())
        self.assertEqual(st["sample"], 4)
        self.assertEqual(st["exact"], 2)
        self.assertEqual(st["approximate"], 1)
        self.assertEqual(st["none"], 1)
        self.assertEqual(st["license_ok"], 3)
        self.assertEqual(st["image_ok"], 3)
        self.assertEqual(st["eligible"], 1)
        self.assertEqual(st["exact_share"], 0.5)
        self.assertEqual(st["eligible_share"], 0.25)
        self.assertEqual(st["date_fields"], {"dc:date": 4})
        self.assertEqual(st["examples"][0]["title"], "a")

    def test_empty_sample_is_not_a_division_by_zero(self):
        st = a.analyze([])
        self.assertEqual(st["sample"], 0)
        self.assertIsNone(st["exact_share"])

    def test_blocked_collections_are_reported_not_invented(self):
        names = {b["source"] for b in a.BLOCKED}
        self.assertTrue(any("Library of Congress" in n for n in names))
        for b in a.BLOCKED:
            self.assertTrue(b["observed"])


if __name__ == "__main__":
    unittest.main()
