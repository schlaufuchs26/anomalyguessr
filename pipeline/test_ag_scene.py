"""Tests for pipeline/ag_scene.py, the short-handle lookup (ticket #1413).

Run with: python3 -m unittest discover -s pipeline -t pipeline -p 'test_ag_*.py'
"""

import contextlib
import io
import json
import shutil
import tempfile
import unittest
from pathlib import Path

import ag_scene


def scene(eid, short, added="2026-09-08", shown=None):
    return {
        "id": eid,
        "shortId": short,
        "title": f"Title {eid}",
        "place": "Place",
        "year": "1900",
        "anomaly": "Plastic bottle",
        "sourceUrl": f"https://example.test/{eid}",
        "source": {"repository": "Example Archive"},
        "added": added,
        "shown": shown,
    }


class SceneLookupTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="ag_scene_"))
        self.data = self.tmp / "data"
        (self.data / "library" / "long-id-one").mkdir(parents=True)
        (self.data / "traces").mkdir(parents=True)
        (self.data / "library" / "long-id-one" / "long-id-one.jpg").write_bytes(
            b"img")
        (self.data / "traces" / "long-id-one.json").write_text("{}")
        state = {
            "version": 1,
            "last_shipped": None,
            "next_short": 3,
            "scenes": {
                "long-id-one": scene("long-id-one", "AG-1"),
                "long-id-two": scene("long-id-two", "AG-2",
                                     added="2026-09-09"),
            },
        }
        (self.data / "state.json").write_text(json.dumps(state))
        (self.data / "feedback.json").write_text(json.dumps({
            "version": 1,
            "accepted": {"long-id-one": "2026-09-08T08:00:00+02:00"},
            "rejected": {},
            "comments": {"long-id-one": [{"text": "good", "createdAt": "x"}]},
        }))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def run_find(self, handle):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = ag_scene.main(["--data", str(self.data), "find", handle])
        return rc, buf.getvalue()

    def test_find_by_short_handle_reports_scene_trace_and_moderation(self):
        rc, out = self.run_find("AG-1")
        self.assertEqual(rc, 0)
        view = json.loads(out)
        self.assertEqual(view["id"], "long-id-one")
        self.assertEqual(view["shortId"], "AG-1")
        self.assertEqual(view["moderation"], "accepted")
        self.assertEqual(view["comments"], [{"text": "good", "createdAt": "x"}])
        self.assertTrue(view["hasTrace"])
        self.assertTrue(view["libraryImageExists"])
        # The API URL keeps the canonical id, so it works on an old API too.
        self.assertIn("long-id-one", view["traceUrl"])

    def test_find_accepts_the_long_id_and_lower_case(self):
        for handle in ("long-id-one", "ag-1"):
            rc, out = self.run_find(handle)
            self.assertEqual(rc, 0)
            self.assertEqual(json.loads(out)["shortId"], "AG-1")

    def test_find_reports_unmoderated_without_a_verdict(self):
        rc, out = self.run_find("AG-2")
        self.assertEqual(rc, 0)
        view = json.loads(out)
        self.assertEqual(view["moderation"], "unmoderated")
        self.assertFalse(view["hasTrace"])
        self.assertIsNone(view["shown"])

    def test_find_unknown_handle_exits_2(self):
        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            rc = ag_scene.main(["--data", str(self.data), "find", "AG-99"])
        self.assertEqual(rc, 2)
        self.assertIn("unknown scene handle", buf.getvalue())

    def test_list_prints_oldest_first_with_the_next_number(self):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = ag_scene.main(["--data", str(self.data), "list"])
        out = buf.getvalue()
        self.assertEqual(rc, 0)
        self.assertLess(out.index("AG-1"), out.index("AG-2"))
        self.assertIn("next AG-3", out)


if __name__ == "__main__":
    unittest.main()
