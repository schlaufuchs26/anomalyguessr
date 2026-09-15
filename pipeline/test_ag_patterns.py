"""Tests for pipeline/ag_patterns.py (ticket #1539).

Run with: python3 -m unittest discover -s pipeline -t pipeline

No network and no API spend: the seeded catalogue is read as data and the
readers are exercised on it plus a few synthetic patterns.
"""

import unittest

import ag_patterns as ap

REQUIRED_FIELDS = ("id", "title", "kind", "status", "added", "updated",
                   "summary", "prompt", "matches", "evidence")
KNOWN_STATUS = (ap.STATUS_ACTIVE, ap.STATUS_OBSERVE, ap.STATUS_RETIRED)
KNOWN_RULES = ("carrier", "small", "presentation")


class SeededCatalogueTest(unittest.TestCase):
    """The committed pipeline/ag_patterns.json is valid and complete."""

    def setUp(self):
        self.patterns = ap.load_patterns()

    def test_the_catalogue_loads_and_has_the_seed(self):
        self.assertGreaterEqual(len(self.patterns), 4)

    def test_every_pattern_carries_its_evidence_fields(self):
        for pattern in self.patterns:
            for field in REQUIRED_FIELDS:
                self.assertIn(field, pattern, pattern.get("id"))
            self.assertIn(pattern["kind"], ("positive", "negative"))
            self.assertIn(pattern["status"], KNOWN_STATUS)
            self.assertTrue(pattern["evidence"].get("note"),
                            pattern["id"])
            rule = pattern.get("rule")
            if rule is not None:
                self.assertIn(rule.get("type"), KNOWN_RULES)

    def test_ids_are_unique(self):
        ids = [p["id"] for p in self.patterns]
        self.assertEqual(len(ids), len(set(ids)))

    def test_the_canonical_positive_pattern_names_both_reference_scenes(self):
        positive = ap.pattern_by_id(self.patterns, "integrated-element")
        self.assertEqual(positive["status"], ap.STATUS_ACTIVE)
        accepted = positive["evidence"]["examples"]["accepted"]
        self.assertIn("AG-122", accepted)
        self.assertIn("AG-156", accepted)
        self.assertIn("WD-40", positive["prompt"])
        self.assertIn("jet ski", positive["prompt"])

    def test_the_carrier_pattern_keeps_its_machine_checkable_rule(self):
        pattern = ap.pattern_by_id(self.patterns, "carrier-missing")
        self.assertEqual(pattern["rule"]["type"], "carrier")
        rows = dict(ap.carrier_requirements([pattern]))
        self.assertIn("outboard motor", rows)
        self.assertIn("boat", rows["outboard motor"])


class PromptGuidanceTest(unittest.TestCase):

    def test_only_active_patterns_reach_the_prompt(self):
        patterns = [
            {"id": "a", "status": ap.STATUS_ACTIVE, "prompt": "Works: A."},
            {"id": "o", "status": ap.STATUS_OBSERVE, "prompt": "Works: O."},
            {"id": "r", "status": ap.STATUS_RETIRED, "prompt": "Works: R."},
        ]
        self.assertEqual(ap.prompt_guidance(patterns), ["Works: A."])

    def test_the_guidance_is_capped_by_lines_and_characters(self):
        many = [{"id": f"p{i}", "status": ap.STATUS_ACTIVE,
                 "prompt": "x" * 50} for i in range(10)]
        lines = ap.prompt_guidance(many, limit=3, char_cap=120)
        self.assertEqual(len(lines), 2)  # the third 50-char line crosses 120

    def test_the_seeded_active_patterns_fit_one_prompt(self):
        lines = ap.prompt_guidance(ap.load_patterns())
        self.assertGreaterEqual(len(lines), 3)
        self.assertTrue(any("WD-40" in line for line in lines))


class PatternMatchTest(unittest.TestCase):

    def setUp(self):
        self.patterns = ap.load_patterns()

    def test_the_label_map_reads_the_label_not_the_family(self):
        self.assertEqual(
            ap.patterns_for_label("Modern outboard motor", self.patterns),
            ["carrier-missing"])
        self.assertIn("small-handheld",
                      ap.patterns_for_label("Paper coffee cup", self.patterns))
        self.assertEqual(ap.patterns_for_label("Steel vacuum flask",
                                               self.patterns), [])

    def test_a_small_rule_matches_a_held_object(self):
        self.assertTrue(ap.matches_rule(self.patterns, "small",
                                        "Paper coffee cup"))
        self.assertFalse(ap.matches_rule(self.patterns, "small",
                                         "Traffic cone (orange)"))

    def test_a_retired_pattern_contributes_nothing(self):
        patterns = [{"id": "x", "status": ap.STATUS_RETIRED, "kind": "negative",
                     "matches": ["bottle"], "prompt": "Fails: x.",
                     "rule": {"type": "carrier", "requirements": [
                         {"element_any": ["bottle"], "carrier_any": ["hand"]}]}}]
        self.assertEqual(ap.prompt_guidance(patterns), [])
        self.assertEqual(ap.carrier_requirements(patterns), ())
        self.assertEqual(ap.matches_rule(patterns, "small", "green bottle"),
                         False)


if __name__ == "__main__":
    unittest.main()
