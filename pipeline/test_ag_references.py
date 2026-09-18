"""Tests for pipeline/ag_references.py (ticket #1624).

No network: every fetch is a fixture reader with the same ``(url) -> html``
signature as the real one.
"""

import unittest
import urllib.error

import ag_references as r


FAKE_PAGES = {
    "https://example.org/pet": (
        "<html><body><h1>Polyethylene terephthalate</h1>"
        "<p>PET bottles spread through the 1970s.</p></body></html>"),
    "https://example.org/bicycle": (
        "<html><body><h1>Bicycle</h1><p>A bicycle has two wheels.</p>"
        "</body></html>"),
    "https://example.org/empty": "<html><body><script>x()</script>"
                                 "</body></html>",
}


def fixture_fetch(url):
    if url not in FAKE_PAGES:
        raise urllib.error.URLError("no such fixture")
    return FAKE_PAGES[url]


class TimeTokensTest(unittest.TestCase):
    def test_decade_and_year_and_century(self):
        self.assertEqual(r.time_tokens("spread through the 1970s"),
                         ["1970s"])
        self.assertEqual(r.time_tokens("patented in 1967"), ["1967"])
        self.assertEqual(r.time_tokens("a mid-20th-century product"),
                         ["mid 20th century"])
        self.assertEqual(r.time_tokens("since the twentieth century"),
                         ["20th century"])

    def test_no_dated_claim(self):
        # A fictional-future explanation asserts no date; nothing to require.
        self.assertEqual(r.time_tokens("a robot from a fictional future"),
                         [])


class DeepLinkTest(unittest.TestCase):
    def test_search_and_bare_domain_rejected(self):
        self.assertFalse(r.deep_link("https://en.wikipedia.org/w/index.php"
                                     "?search=pet"))
        self.assertFalse(r.deep_link("https://www.google.com/search?q=pet"))
        self.assertFalse(r.deep_link("https://example.org/"))
        self.assertFalse(r.deep_link("ftp://example.org/page"))

    def test_page_path_accepted(self):
        self.assertTrue(r.deep_link(
            "https://en.wikipedia.org/wiki/Polyethylene_terephthalate"))
        self.assertTrue(r.deep_link("https://example.org/pet#History"))


class CheckReferenceTest(unittest.TestCase):
    def test_pass(self):
        out = r.check_reference(
            "Clear PET plastic bottles only came into common use in the "
            "1970s.", {"label": "Polyethylene terephthalate",
                       "url": "https://example.org/pet"}, fetch=fixture_fetch)
        self.assertEqual(out["status"], "ok")
        self.assertEqual(out["missing"], [])

    def test_wrong_topic(self):
        out = r.check_reference(
            "Clear PET plastic bottles only came into common use in the "
            "1970s.", {"label": "Polyethylene terephthalate",
                       "url": "https://example.org/bicycle"},
            fetch=fixture_fetch)
        self.assertEqual(out["status"], "wrong_topic")

    def test_on_topic_page_reports_stated_tokens(self):
        out = r.check_reference(
            "Clear PET plastic bottles only came into common use in the "
            "1970s.", {"label": "Polyethylene terephthalate",
                       "url": "https://example.org/pet"}, fetch=fixture_fetch)
        self.assertEqual(out["status"], "ok")
        self.assertEqual(out["stated"], ["1970s"])
        self.assertEqual(out["missing"], [])

    def test_dead_link(self):
        out = r.check_reference(
            "Barcodes entered retail in the 1970s.",
            {"label": "Barcode", "url": "https://example.org/gone"},
            fetch=fixture_fetch)
        self.assertEqual(out["status"], "dead")

    def test_empty_page(self):
        out = r.check_reference(
            "PET bottles spread in the 1970s.",
            {"label": "Polyethylene terephthalate",
             "url": "https://example.org/empty"}, fetch=fixture_fetch)
        self.assertEqual(out["status"], "empty")

    def test_topic_match_is_case_and_spelling_insensitive(self):
        out = r.check_reference(
            "PET spread in the 1970s.",
            {"label": "Polyethylene terephthalate",
             "url": "https://example.org/pet"}, fetch=fixture_fetch)
        self.assertEqual(out["status"], "ok")


class ReferenceFindingTest(unittest.TestCase):
    def test_finding_fails_on_uncovered_claim(self):
        # Both pages are live and on topic, but neither states the 1950s.
        finding = r.reference_finding(
            "Aluminium beverage cans only appeared in the 1950s.",
            [{"label": "Polyethylene terephthalate",
              "url": "https://example.org/pet"}], fetch=fixture_fetch)
        self.assertTrue(finding["failed"])
        self.assertEqual(finding["uncovered"], ["1950s"])

    def test_finding_covers_token_across_references(self):
        # One reference can support the dated half of the sentence while a
        # second, unrelated-to-the-date one supports the rest.
        finding = r.reference_finding(
            "Clear PET plastic bottles only came into common use in the "
            "1970s, decades after this photograph was taken.",
            [{"label": "Polyethylene terephthalate",
              "url": "https://example.org/pet"}], fetch=fixture_fetch)
        self.assertFalse(finding["failed"])
        self.assertEqual(finding["uncovered"], [])

    def test_finding_fails_on_wrong_topic(self):
        finding = r.reference_finding(
            "PET bottles spread in the 1970s.",
            [{"label": "Bicycle", "url": "https://example.org/bicycle"}],
            fetch=fixture_fetch)
        self.assertTrue(finding["failed"])

    def test_finding_ok(self):
        finding = r.reference_finding(
            "PET bottles spread in the 1970s.",
            [{"label": "Polyethylene terephthalate",
              "url": "https://example.org/pet"}], fetch=fixture_fetch)
        self.assertFalse(finding["failed"])


if __name__ == "__main__":
    unittest.main()
