"""Tests for pipeline/ag_legibility_probe.py (ticket #1476).

No network and no API spend: the one vision call per scene is monkeypatched.
"""

import json
import tempfile
import unittest
from pathlib import Path

import ag_catalog
import ag_legibility_probe as p
import ag_sources


def make_img(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"not-a-real-image")


def scene(anomaly="Plastic bottle (clear PET)", year="1905", family="drinks"):
    return {"anomaly": anomaly, "year": year, "family": family}


class PromptTest(unittest.TestCase):
    def test_probe_prompt_states_only_the_year(self):
        text = p.probe_prompt(1905)
        self.assertIn("1905", text)
        self.assertIn("does not belong", text)
        self.assertIn('"element"', text)
        # no hint about the planted element
        self.assertNotIn("bottle", text.lower())

    def test_probe_prompt_tolerates_an_unknown_year(self):
        self.assertIn("unknown year", p.probe_prompt(None))


class SceneYearTest(unittest.TestCase):
    def test_parses_the_displayed_year(self):
        self.assertEqual(p.scene_year({"year": "c. 1907"}), 1907)
        self.assertEqual(p.scene_year({"year": "2021"}), 2021)
        self.assertEqual(p.scene_year({"year": 1911}), 1911)
        self.assertIsNone(p.scene_year({"year": "no year"}))
        self.assertIsNone(p.scene_year({}))


class NamesElementTest(unittest.TestCase):
    def test_synonyms_and_family_match(self):
        self.assertTrue(p.names_element(
            "bottle of water", "Plastic bottle (clear PET)"))
        self.assertTrue(p.names_element(
            "a smartphone", "Smartphone"))

    def test_a_material_only_match_is_not_a_hit(self):
        # The matcher must not count a shared material as naming the element:
        # a plastic bag is not the planted plastic bottle.
        self.assertFalse(p.names_element("plastic bag", "Plastic bottle"))
        self.assertFalse(p.names_element("", "Plastic bottle"))
        self.assertFalse(p.names_element("a wooden cart", "Smartphone"))


class SelectionTest(unittest.TestCase):
    def setUp(self):
        self._tmp = Path(tempfile.mkdtemp())
        self._old = ag_sources.default_data_dir
        ag_sources.default_data_dir = lambda: self._tmp

    def tearDown(self):
        ag_sources.default_data_dir = self._old

    def test_round_robin_spans_families_and_skips_missing_images(self):
        scenes = {}
        for i in range(3):
            sid = f"drink-{i}"
            scenes[sid] = scene(family="drinks")
            make_img(self._tmp / "library" / sid / f"{sid}.jpg")
        for i in range(2):
            sid = f"car-{i}"
            scenes[sid] = scene(family="vehicles")
            make_img(self._tmp / "library" / sid / f"{sid}.jpg")
        scenes["no-image"] = scene(family="drinks")
        picked = p.select_scenes(scenes, 4)
        self.assertEqual(len(picked), 4)
        # The first two picks are one from each family (round-robin), and
        # the scene without a library image never appears.
        self.assertEqual({"drink-0", "car-0"}, set(picked[:2]))
        self.assertNotIn("no-image", picked)


class ReportTest(unittest.TestCase):
    def test_family_table_counts_and_rates(self):
        rows = [
            {"family": "drinks", "verdict": "found", "answer": "bottle"},
            {"family": "drinks", "verdict": "other", "answer": "crate"},
            {"family": "vehicles", "verdict": "unreadable", "answer": ""},
        ]
        table = p.family_table(rows)
        self.assertEqual(table["drinks"]["scenes"], 2)
        self.assertEqual(table["drinks"]["found"], 1)
        self.assertEqual(table["drinks"]["find_rate"], 0.5)
        self.assertEqual(table["drinks"]["wrong"], ["crate"])
        self.assertEqual(table["vehicles"]["find_rate"], 0.0)


class ProbeTest(unittest.TestCase):
    def setUp(self):
        self._tmp = Path(tempfile.mkdtemp())
        self.data_dir = self._tmp
        (self.data_dir / "library").mkdir(parents=True)
        self._old_scene = p.probe_scene
        self._old_default = ag_sources.default_data_dir
        ag_sources.default_data_dir = lambda: self.data_dir

    def tearDown(self):
        p.probe_scene = self._old_scene
        ag_sources.default_data_dir = self._old_default

    def test_probe_rolls_up_verdicts_and_families(self):
        sid = "drink-1"
        make_img(self.data_dir / "library" / sid / f"{sid}.jpg")
        state = {"version": 1, "scenes": {sid: scene()}, "next_short": 1}
        (self.data_dir / "state.json").write_text(
            json.dumps(state), encoding="utf-8")
        p.probe_scene = lambda image, scene, *a, **kw: {
            "id": scene["id"], "anomaly": scene["anomaly"],
            "family": scene.get("family"), "year": 1905, "answer": "bottle",
            "reason": "", "verdict": "found",
            "call": {"usage": {"prompt_tokens": 1, "completion_tokens": 1,
                               "reasoning_tokens": 0, "cost": 0.0001}}}
        report = p.probe(self.data_dir, "key", limit=5)
        self.assertEqual(report["probed"], 1)
        self.assertEqual(report["verdicts"]["found"], 1)
        self.assertEqual(report["families"]["drinks"]["found"], 1)
        self.assertAlmostEqual(report["totals"]["cost"], 0.0001)
        self.assertIn("drinks", p.format_report(report))


if __name__ == "__main__":
    unittest.main()
