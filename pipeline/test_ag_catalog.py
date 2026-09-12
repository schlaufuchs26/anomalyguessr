"""Tests for pipeline/ag_catalog.py (the machine-readable anomaly catalog).

Run with: python3 -m unittest discover -s pipeline -t pipeline -p 'test_ag_*.py'

The catalog is data the generator trusts blindly, so these tests guard its
shape: every entry complete, labels unique, recipes real, and the numeric
scale budget in step with the prompt wording (ticket #1328).
"""

import re
import unittest

import ag_catalog as c
import ag_generate as g


class CatalogShapeTests(unittest.TestCase):
    def test_labels_are_unique_and_non_empty(self):
        labels = c.labels()
        self.assertEqual(len(labels), len(set(labels)))
        self.assertNotIn("", labels)

    def test_entries_are_complete(self):
        for e in c.CATALOG:
            with self.subTest(label=e.get("label")):
                self.assertIn(e["type"], ("object", "person", "future"))
                self.assertTrue(e["family"])
                self.assertIsInstance(e["settings"], tuple)
                self.assertIn(e["recipe"], c.RECIPES)
                self.assertTrue(e["size"])
                self.assertIn(e["risk"], ("low", "medium", "high"))
                self.assertTrue(e["explanation"])
                refs = e["references"]
                self.assertTrue(refs)
                for r in refs:
                    self.assertTrue(re.match(r"^https?://", r["url"]))
                    self.assertTrue(r["label"])
                if e["type"] != "object":
                    self.assertTrue(e["noun"])
                    self.assertTrue(e["tells"])
                if e["min_year"] is not None:
                    self.assertIsInstance(e["min_year"], int)

    def test_variety_spans_many_families_not_just_bottles(self):
        # #1328: the catalog was thin outside drinks/luggage, so those won
        # every run. Keep the shelf broad enough for a varied daily set.
        families = {e["family"] for e in c.CATALOG}
        self.assertGreaterEqual(len(c.CATALOG), 30)
        self.assertGreaterEqual(len(families), 12)


class ScaleMaxTests(unittest.TestCase):
    """The numeric gate twin must not drift from the prompt budget."""

    def test_scale_max_matches_the_wording(self):
        for e in c.CATALOG:
            if "scale" not in e:
                continue
            with self.subTest(label=e["label"]):
                percents = [int(n) for n in
                            re.findall(r"(\d+)\s*percent", e["scale"])]
                self.assertTrue(percents)
                self.assertAlmostEqual(c.scale_max(e), max(percents) / 100)

    def test_defaults_by_type(self):
        bottle = next(e for e in c.CATALOG
                      if e["label"] == "Plastic bottle (clear PET)")
        person = next(e for e in c.CATALOG if e["type"] == "person")
        cone = next(e for e in c.CATALOG
                    if e["label"] == "Traffic cone (orange)")
        self.assertEqual(c.scale_max(bottle), c.DEFAULT_OBJECT_SCALE_MAX)
        self.assertEqual(c.scale_max(person), c.DEFAULT_PERSON_SCALE_MAX)
        self.assertEqual(c.scale_max(cone), 0.04)


class GeneratorCompatibilityTests(unittest.TestCase):
    """Everything the generator needs from an entry actually works."""

    def test_every_entry_builds_a_prompt(self):
        for e in c.CATALOG:
            with self.subTest(label=e["label"]):
                prompt = g.build_prompt(e)
                self.assertIn("CRITICAL SCALE", prompt)
                self.assertIn("Keep every other part of the photograph", prompt)

    def test_every_entry_survives_the_family_resolver(self):
        for e in c.CATALOG:
            self.assertEqual(c.family_of(e["label"]), e["family"])


if __name__ == "__main__":
    unittest.main()
