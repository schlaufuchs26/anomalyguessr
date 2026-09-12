"""Tests for pipeline/ag_era_audit.py (ticket #1338).

No network: a temp data dir with a hand-written state + source index covers
every finding the audit reports.
"""

import json
import shutil
import tempfile
import unittest
from pathlib import Path

import ag_era_audit as audit
import ag_sources


def scene(sid, year="1900", place="Springfield", anomaly="Wheeled suitcase",
          title="Market street", source=None):
    return {
        "id": sid, "title": title, "place": place, "year": year,
        "credit": "PD", "sourceUrl": "https://example.org/x",
        "anomaly": anomaly, "answer": {"x": 0.5, "y": 0.5, "r": 0.05},
        "source": source or {
            "repository": "Wikimedia Commons",
            "fileUrl": f"https://commons.example/{sid}",
            "originalTitle": "Busy market street, 1900",
            "date": "1900", "place": "Springfield",
            "license": "Public domain", "description": "",
        },
    }


class AuditTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="ag_audit_"))
        self.data = self.tmp / "data"
        (self.data / "sources" / "images").mkdir(parents=True)
        self.state = {"version": 1, "last_shipped": None, "scenes": {}}
        self.sources = {}
        self.write()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def write(self):
        (self.data / "state.json").write_text(
            json.dumps(self.state, indent=2), encoding="utf-8")
        (self.data / "sources" / "index.json").write_text(
            json.dumps({"version": 1, "sources": self.sources}, indent=2),
            encoding="utf-8")

    def source(self, sid, **kw):
        entry = {"id": sid, "originalTitle": "Busy market street, 1900",
                 "date": "1900", "place": "", "used": False,
                 "description": "", "raw": {}}
        entry.update(kw)
        self.sources[sid] = entry
        return entry

    def add(self, sid, **kw):
        entry = self.source(sid, **{k: kw.pop(k) for k in
                                    ("originalTitle", "date", "description",
                                     "place", "raw")
                                    if k in kw})
        sc = scene(sid, source={
            "repository": "Wikimedia Commons",
            "fileUrl": f"https://commons.example/{sid}",
            "originalTitle": entry["originalTitle"], "date": entry["date"],
            "place": entry["place"], "license": "Public domain",
            "description": entry["description"]})
        for k, v in kw.items():
            sc[k] = v
        self.state["scenes"][sid] = sc
        return sc

    def test_modern_source_is_reported(self):
        # The July-2024 MBTA photo: the scene says 1900, the source says 2024.
        self.add("commons-modern", year="1900",
                 originalTitle="South Station platform, July 2024",
                 date="2024-07-17 16:55:48",
                 description="A southbound 1900-series Red Line train.")
        self.write()
        rep = audit.audit(self.data)
        self.assertEqual([r["id"] for r in rep["modern_era"]], ["commons-modern"])
        self.assertEqual(rep["modern_era"][0]["era"], 2024)
        self.assertEqual([r["id"] for r in rep["no_evidence"]], ["commons-modern"])
        self.assertEqual(rep["year_1900"]["scenes"], ["commons-modern"])

    def test_unknown_era_and_place_evidence(self):
        sc = self.add("commons-nodate", originalTitle="Unnamed view",
                      date="2008-11-06 23:29")
        sc["place"] = "Unidentified location"
        self.sources["commons-nodate"]["place"] = "42.3522, -71.0531"
        self.write()
        rep = audit.audit(self.data)
        self.assertEqual([r["id"] for r in rep["unknown_era"]], ["commons-nodate"])
        # the scene says Unidentified while the source has coordinates
        self.assertEqual(
            [r["id"] for r in rep["unidentified_place"]], ["commons-nodate"])

    def test_late_year_mention_and_mismatch(self):
        self.add("commons-mismatch", year="1900", originalTitle="Street, 1907",
                 date="1907")
        self.add("commons-late", year="1910", originalTitle="Street c. 1910",
                 date="1910", description="Photographed for a 1982 book.")
        self.write()
        rep = audit.audit(self.data)
        self.assertEqual([r["id"] for r in rep["era_mismatch"]],
                         ["commons-mismatch"])
        self.assertEqual([r["id"] for r in rep["late_year_mentions"]],
                         ["commons-late"])

    def test_seed_scenes_are_reported_not_flagged(self):
        self.state["scenes"]["pike-place"] = scene("pike-place", year="1907")
        self.write()
        rep = audit.audit(self.data)
        self.assertEqual(rep["seed_scenes"], 1)
        self.assertEqual(rep["with_source_entry"], 0)

    def test_pool_lists_ineligible_entries(self):
        self.source("commons-modern-pool",
                    originalTitle="Platform, July 2024",
                    date="2024-07-17 16:55:48")
        self.write()
        rep = audit.audit(self.data)
        self.assertEqual(rep["pool"]["total"], 1)
        self.assertEqual(rep["pool"]["ineligible"][0]["reason"],
                         "modern era (2024)")

    def test_prune_removes_the_wrong_scenes_and_keeps_shipped_ones(self):
        self.add("commons-gone", originalTitle="Platform, July 2024",
                 date="2024-07-17 16:55:48")
        self.add("commons-kept")
        self.sources["commons-kept"].update(
            originalTitle="Platform, July 2024", date="2024-07-17 16:55:48")
        self.state["scenes"]["commons-kept"]["source"]["originalTitle"] = \
            "Platform, July 2024"
        self.state["last_shipped_ids"] = ["commons-kept"]
        self.write()
        rep = audit.prune(self.data)
        self.assertEqual(rep["ids"], ["commons-gone"])
        self.assertEqual(rep["shipped_kept"], ["commons-kept"])
        scenes = json.loads((self.data / "state.json").read_text())["scenes"]
        self.assertEqual(list(scenes), ["commons-kept"])


class GuardTest(unittest.TestCase):
    """The guards the audit leans on live in ag_sources."""

    def test_series_token_is_not_evidence(self):
        self.assertEqual(ag_sources.years_in_text("1900-series"), [])
        self.assertEqual(ag_sources.years_in_text("1900"), [1900])
