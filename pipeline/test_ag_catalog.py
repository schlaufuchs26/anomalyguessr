"""Tests for pipeline/ag_catalog.py (the machine-readable anomaly catalog).

Run with: python3 -m unittest discover -s pipeline -t pipeline -p 'test_ag_*.py'

The catalog is data the generator trusts blindly, so these tests guard its
shape: every entry complete, labels unique, recipes real, and the numeric
scale budget in step with the prompt wording (ticket #1328).
"""

import re
import unittest

import ag_catalog as c


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
    """Everything the generator needs from the catalog actually works.

    Since #1372 the generator does not build a prompt from a catalog entry:
    a model proposes the anomaly. The catalog's remaining jobs are the
    few-shot inspiration list, the curated text for a matched label and the
    family resolver, so those are what these tests guard.
    """

    def test_every_entry_survives_the_family_resolver(self):
        for e in c.CATALOG:
            self.assertEqual(c.family_of(e["label"]), e["family"])

    def test_entry_lookup_is_exact_and_case_insensitive(self):
        e = next(e for e in c.CATALOG if e["type"] == "person")
        self.assertEqual(c.entry_for_label(e["label"])["label"], e["label"])
        self.assertEqual(c.entry_for_label(e["label"].lower())["label"],
                         e["label"])
        self.assertIsNone(c.entry_for_label("Not a catalog label"))
        self.assertIsNone(c.entry_for_label(""))

    def test_every_family_has_curated_references(self):
        for e in c.CATALOG:
            with self.subTest(label=e["label"]):
                refs = c.references_for_family(e["family"])
                self.assertTrue(refs)
                self.assertRegex(refs[0]["url"], r"^https?://")

    def test_inspiration_lines_are_usable(self):
        lines = c.inspiration_lines()
        self.assertGreaterEqual(len(lines), 3)
        for line in lines:
            self.assertIsInstance(line, str)
            self.assertTrue(line.strip())
        self.assertEqual(c.inspiration_lines(0), [])
        self.assertEqual(len(c.inspiration_lines(2)), 2)


if __name__ == "__main__":
    unittest.main()
