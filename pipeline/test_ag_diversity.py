"""Tests for pipeline/ag_diversity.py (the per-week diversity report #2343).

Run with: python3 -m unittest discover -s pipeline -t pipeline -p 'test_ag_*.py'

No network: the report reads a small hand-built queue state and checks the
numbers a reviewer compares the week before and after a change on.
"""

import datetime
import json
import tempfile
import unittest
from pathlib import Path

import ag_diversity as d
import ag_queue


def scene(label, added, sid="s"):
    return {"id": sid, "anomaly": label, "added": added}


class WeekHelperTest(unittest.TestCase):
    def test_week_key_is_the_iso_year_week(self):
        self.assertEqual(d.week_key(datetime.date(2026, 10, 5)), "2026-W41")
        self.assertEqual(d.week_key(datetime.date(2026, 1, 1)), "2026-W01")

    def test_week_start_is_the_monday(self):
        # 2026-10-05 is a Monday.
        self.assertEqual(d.week_start(datetime.date(2026, 10, 5)),
                         datetime.date(2026, 10, 5))
        self.assertEqual(d.week_start(datetime.date(2026, 10, 11)),
                         datetime.date(2026, 10, 5))

    def test_recent_weeks_lists_oldest_first(self):
        weeks = d.recent_weeks(datetime.date(2026, 10, 5), 3)
        self.assertEqual(weeks, ["2026-W39", "2026-W40", "2026-W41"])


class MeasureTest(unittest.TestCase):
    def test_top_family_share_counts_the_ten_biggest(self):
        labels = ["Plastic bottle (clear PET)"] * 3 + ["Wheeled suitcase"] * 1
        share = d.top_family_share(labels, top=10)
        self.assertEqual(share["share"], 1.0)
        self.assertEqual(share["families"][0]["family"], "drinks")

    def test_an_unclassifiable_label_is_a_scene_but_no_family(self):
        share = d.top_family_share(["Hovering transport pod"] * 2, top=10)
        self.assertEqual(share["families"], [])
        self.assertEqual(share["share"], 0.0)

    def test_repeat_rows_are_exact_and_sorted(self):
        rows = d.repeat_rows(["A"] * 2 + ["B"] * 3 + ["C"])
        self.assertEqual(rows, [{"label": "B", "count": 3},
                                {"label": "A", "count": 2}])

    def test_week_report_combines_the_numbers(self):
        row = d.week_report(["Plastic bottle (clear PET)"] * 2
                            + ["Wheeled suitcase"])
        self.assertEqual(row["scenes"], 3)
        self.assertEqual(row["distinct_labels"], 2)
        self.assertEqual(row["repeats"],
                         [{"label": "Plastic bottle (clear PET)", "count": 2}])


class ReportTest(unittest.TestCase):
    def setUp(self):
        self._tmp = Path(tempfile.mkdtemp())
        self.data_dir = self._tmp / "anomalyguessr"

    def _write(self, rows):
        state = {"version": 1, "scenes": {
            f"s{i}": scene(label, added, sid=f"s{i}")
            for i, (label, added) in enumerate(rows)}}
        ag_queue.save_state(self.data_dir, state)

    def test_the_report_buckets_scenes_by_calendar_week(self):
        self._write([("Plastic bottle (clear PET)", "2026-10-05"),
                     ("Wheeled suitcase", "2026-10-05"),
                     ("Plastic bottle (clear PET)", "2026-09-30")])
        report = d.diversity_report(self.data_dir,
                                    today=datetime.date(2026, 10, 5),
                                    weeks=2)
        self.assertEqual([w["week"] for w in report["weeks"]],
                         ["2026-W40", "2026-W41"])
        current = report["weeks"][-1]
        self.assertEqual(current["scenes"], 2)
        self.assertEqual(current["repeats"], [])
        previous = report["weeks"][0]
        self.assertEqual(previous["scenes"], 1)

    def test_scenes_outside_the_window_are_ignored(self):
        self._write([("Plastic bottle (clear PET)", "2026-08-01")])
        report = d.diversity_report(self.data_dir,
                                    today=datetime.date(2026, 10, 5),
                                    weeks=2)
        self.assertEqual([w["scenes"] for w in report["weeks"]], [0, 0])

    def test_a_missing_state_yields_empty_weeks(self):
        report = d.diversity_report(self.data_dir,
                                    today=datetime.date(2026, 10, 5),
                                    weeks=1)
        self.assertEqual(report["weeks"][0]["scenes"], 0)

    def test_render_prints_a_row_per_week(self):
        self._write([("Plastic bottle (clear PET)", "2026-10-05")])
        report = d.diversity_report(self.data_dir,
                                    today=datetime.date(2026, 10, 5),
                                    weeks=1)
        text = d.render(report)
        self.assertIn("2026-W41", text)
        self.assertIn("top-10 share", text)

    def test_the_report_serializes(self):
        report = d.diversity_report(self.data_dir,
                                    today=datetime.date(2026, 10, 5),
                                    weeks=1)
        self.assertIn("weeks", json.loads(json.dumps(report)))


if __name__ == "__main__":
    unittest.main()
