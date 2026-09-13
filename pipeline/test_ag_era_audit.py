"""Tests for pipeline/ag_era_audit.py (ticket #1403).

No network: a temp data dir with a hand-written state file covers every
verdict the audit reports.
"""

import shutil
import tempfile
import unittest
from pathlib import Path

import ag_era_audit as audit
import ag_queue


def scene(sid, anomaly, year="1900", date="1900", family="", explanation="",
          shown=None):
    return {
        "id": sid, "title": "Market street", "place": "Springfield",
        "year": year, "anomaly": anomaly, "family": family,
        "explanation": explanation, "answer": {"x": 0.5, "y": 0.5, "r": 0.05},
        "source": {"date": date, "repository": "Wikimedia Commons"},
        "shown": shown,
    }


class AuditTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="ag_imposs_"))
        self.data = self.tmp / "data"
        (self.data / "sources" / "images").mkdir(parents=True)
        self.state = {"version": 1, "last_shipped": None, "scenes": {}}
        self.write()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def write(self):
        ag_queue.save_state(self.data, self.state)

    def add(self, s):
        self.state["scenes"][s["id"]] = s
        self.write()

    def test_an_improbable_but_possible_element_fails(self):
        # The reported case: an e-scooter in a 2017 photo existed then, so a
        # player can explain it away; impossibility is the bar (#1403).
        self.add(scene("scooter-2017", "Folding electric kick scooter",
                       year="modern", date="2017-06-14",
                       explanation="Electric scooters are a 2010s product."))
        report = audit.audit(self.data)
        self.assertEqual(report["verdicts"]["improbable"], 1)
        row = report["failing"][0]
        self.assertEqual(row["year"], 2017)
        self.assertEqual(row["year_origin"], "metadata")
        self.assertEqual(row["intro_year"], 2015)

    def test_a_plastic_bottle_in_1900_is_impossible(self):
        self.add(scene("bottle", "Plastic bottle (clear PET)", year="1900",
                       date="1898",
                       explanation="PET bottles came into use in the 1970s."))
        report = audit.audit(self.data)
        self.assertEqual(report["verdicts"]["impossible"], 1)
        self.assertEqual(report["failing"], [])

    def test_a_futuristic_element_always_passes(self):
        self.add(scene("robot", "Robot time traveler", year="modern",
                       date="2015-01-01", family="robot"))
        report = audit.audit(self.data)
        self.assertEqual(report["verdicts"]["impossible"], 1)

    def test_an_object_introduced_the_same_year_fails(self):
        self.add(scene("same", "E-scooter", year="2015", date="2015-05-05"))
        self.assertEqual(audit.judge(self.state["scenes"]["same"])["verdict"],
                         "same-year")

    def test_the_metadata_anchor_beats_the_displayed_year(self):
        # A modern capture date under a stale displayed era: the anchor is
        # the year the impossibility test uses (#1403).
        self.add(scene("solar", "Solar panel on the roof", year="1960",
                       date="2017-06-14"))
        row = audit.judge(self.state["scenes"]["solar"])
        self.assertEqual(row["year"], 2017)
        self.assertEqual(row["year_origin"], "metadata")
        self.assertEqual(row["verdict"], "improbable")

    def test_without_metadata_the_displayed_year_is_used(self):
        self.add(scene("old", "Plastic bottle", year="c. 1870", date=""))
        row = audit.judge(self.state["scenes"]["old"])
        self.assertEqual(row["year"], 1870)
        self.assertEqual(row["year_origin"], "display")
        self.assertEqual(row["verdict"], "impossible")

    def test_an_unresolvable_label_is_unknown(self):
        self.add(scene("mystery", "Quantum whatsit", year="1900"))
        self.assertEqual(audit.judge(self.state["scenes"]["mystery"])
                         ["verdict"], "unknown")

    def test_the_explanation_year_is_reported(self):
        self.add(scene("bottle", "Plastic bottle", year="1900",
                       explanation="PET bottles only appeared in the 1970s."))
        self.assertEqual(audit.judge(self.state["scenes"]["bottle"])
                         ["reason_year"], 1970)

    def test_an_upload_stamp_anchor_is_flagged_not_pruned(self):
        # Boundary case #3: a scanned 1900 photo whose structured date is the
        # 2005 upload stamp. The anchor is trusted for the test (so the row
        # fails) but never auto-pruned, because the metadata is suspect.
        self.add(scene("agana", "Modern bicycle", year="1899",
                       date="2005-09-30"))
        report = audit.audit(self.data)
        self.assertEqual(report["failing_prunable"], [])
        self.assertEqual(report["disputed"], ["agana"])
        self.assertTrue(report["failing"][0]["anchor_disagrees"])

    def test_prune_drops_failing_unshown_scenes_only(self):
        self.add(scene("bad", "E-scooter", year="2017", date="2017-01-01"))
        self.add(scene("bad-shown", "E-scooter", year="2017",
                       date="2017-01-01", shown="2026-09-10"))
        self.add(scene("good", "Plastic bottle (clear PET)", year="1900",
                       date="1898"))
        report = audit.audit(self.data)
        result = audit.prune(self.data, report)
        self.assertEqual(result["removed"], ["bad"])
        remaining = ag_queue.load_state(self.data)["scenes"]
        self.assertIn("good", remaining)
        self.assertIn("bad-shown", remaining)
        self.assertNotIn("bad", remaining)


if __name__ == "__main__":
    unittest.main()
