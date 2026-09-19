"""Tests for pipeline/ag_feedback.py (ticket #1538).

Run with: python3 -m unittest discover -s pipeline -t pipeline

No network and no API spend: everything reads a small fixture of scenes and
reviewer verdicts, the same shape ``state.json`` + ``feedback.json`` have.
"""

import datetime
import json
import shutil
import tempfile
import unittest
from pathlib import Path

import ag_catalog
import ag_feedback as fb
import ag_generate as g
import ag_patterns as ap
import ag_queue

TODAY = datetime.date(2026, 9, 15)
GALLICA = "Bibliothèque nationale de France (Gallica)"


def scene(sid, anomaly, year="c. 1907", repo=GALLICA,
          placement="standalone", added="2026-09-10"):
    """One stored scene in the shape the pass reads."""
    return {"id": sid, "anomaly": anomaly, "year": year,
            "placement_kind": placement, "added": added,
            "source": {"repository": repo, "originalTitle": "A street"}}


def verdicts(accepted=(), rejected=(), comments=()):
    return {
        "version": 1,
        "accepted": {sid: "2026-09-14T10:00:00Z" for sid in accepted},
        "rejected": {sid: "2026-09-14T10:00:00Z" for sid in rejected},
        "comments": {sid: [{"text": "too obvious"}] for sid in comments},
    }


def outboard_fixture():
    """The 11 real scenes (ticket #1537): 2 accepted, 9 rejected."""
    scenes = {f"o{i}": scene(f"o{i}", "Modern outboard motor")
              for i in range(11)}
    return scenes, verdicts(accepted=["o0", "o1"],
                            rejected=[f"o{i}" for i in range(2, 11)],
                            comments=[f"o{i}" for i in range(2, 10)])


class TempDataMixin:
    def setUp(self):
        self._tmp = Path(tempfile.mkdtemp())
        self.data_dir = self._tmp / "anomalyguessr"
        self.data_dir.mkdir(parents=True)

    def tearDown(self):
        shutil.rmtree(self._tmp, ignore_errors=True)

    def write(self, scenes, verdicts_):
        ag_queue.save_state(self.data_dir, {"version": 1, "scenes": scenes})
        (self.data_dir / "feedback.json").write_text(json.dumps(verdicts_))


class StatsTest(unittest.TestCase):
    """Ticket #1538: acceptance rates with sample sizes, split by comment."""

    def test_the_real_outboard_record_is_tallied(self):
        scenes, record = outboard_fixture()
        stats = fb.acceptance_stats(scenes, record, today=TODAY)
        row = stats["labels"]["outboard motor"]
        self.assertEqual((row["accepted"], row["rejected"]), (2, 9))
        self.assertEqual(row["total"], 11)
        self.assertEqual(row["rate"], round(2 / 11, 4))
        # 8 of the 9 rejections carried a comment; a commented rejection is a
        # different signal from a silent one.
        self.assertEqual(row["commentedRejected"], 8)
        self.assertEqual(stats["families"]["vehicle"]["total"], 11)

    def test_every_dimension_is_tallied_from_the_same_verdicts(self):
        scenes = {
            "a": scene("a", "Plastic bottle (clear PET)", year="c. 1907",
                       repo=GALLICA, placement="standalone"),
            "b": scene("b", "Wheeled suitcase", year="1921",
                       repo="Wikimedia Commons", placement="modification"),
            "c": scene("c", "Plastic bottle (clear PET)", year="c. 1907",
                       repo=GALLICA, placement="standalone"),
        }
        stats = fb.acceptance_stats(scenes,
                                    verdicts(accepted=["a", "b"],
                                             rejected=["c"]), today=TODAY)
        self.assertEqual(stats["families"]["drinks"]["total"], 2)
        self.assertEqual(stats["families"]["luggage"]["accepted"], 1)
        self.assertEqual(stats["archives"][GALLICA]["total"], 2)
        self.assertEqual(stats["decades"]["1900s"]["total"], 2)
        self.assertEqual(stats["placements"]["standalone"]["total"], 2)
        self.assertEqual(stats["placements"]["modification"]["total"], 1)
        self.assertEqual(stats["sample"]["total"], 3)

    def test_verdicts_outside_the_window_are_dropped(self):
        scenes, record = outboard_fixture()
        stats = fb.acceptance_stats(scenes, record, window_days=1,
                                    today=datetime.date(2026, 9, 20))
        self.assertEqual(stats["sample"]["total"], 0)
        self.assertEqual(stats["labels"], {})


class BlockedLabelTest(unittest.TestCase):
    """Ticket #1538: the label part reuses #1537's rule."""

    def test_eleven_outboard_verdicts_block_the_label(self):
        scenes, record = outboard_fixture()
        stats = fb.acceptance_stats(scenes, record, today=TODAY)
        rows = fb.blocked_label_rows(stats)
        self.assertEqual([r["key"] for r in rows], ["Modern outboard motor"])

    def test_an_acceptance_reopens_a_blocked_label(self):
        scenes = {"a": scene("a", "Modern outboard motor"),
                  "b": scene("b", "Modern outboard motor")}
        stats = fb.acceptance_stats(scenes, verdicts(rejected=["a", "b"]),
                                    today=TODAY)
        self.assertEqual([r["key"] for r in fb.blocked_label_rows(stats)],
                         ["Modern outboard motor"])
        stats = fb.acceptance_stats(scenes, verdicts(accepted=["a"],
                                                     rejected=["b"]),
                                    today=TODAY)
        self.assertEqual(fb.blocked_label_rows(stats), [])


class FamilyExampleTest(unittest.TestCase):
    """Ticket #1538: a family with a poor record loses its example weight."""

    def family_fixture(self):
        # Ten drinks scenes, seven rejected: a 70 % rejection record.
        scenes = {f"d{i}": scene(f"d{i}", "Plastic bottle (clear PET)")
                  for i in range(10)}
        return scenes, verdicts(accepted=["d7", "d8", "d9"],
                                rejected=[f"d{i}" for i in range(7)])

    def test_a_seventy_percent_family_is_avoided(self):
        scenes, record = self.family_fixture()
        stats = fb.acceptance_stats(scenes, record, today=TODAY)
        rows = fb.blocked_family_rows(stats)
        self.assertEqual([r["key"] for r in rows], ["drinks"])

    def test_its_example_is_dropped_from_the_prompt_list(self):
        scenes, record = self.family_fixture()
        applied = fb.adapt(fb.acceptance_stats(scenes, record, today=TODAY))
        self.assertIn("drinks", applied["avoidFamilies"])
        dropped = [d["family"] for d in applied["droppedExamples"]]
        self.assertEqual(dropped, ["drinks", "drinks"])
        self.assertNotIn("Plastic bottle (clear PET)",
                         [fb.example_label(e) for e in applied["examples"]])

    def test_a_starved_list_stays_whole_and_becomes_a_finding(self):
        # Three bad families would leave two of the six examples; the range
        # of shapes must survive, so nothing drops. Five scenes per family
        # clear the family sample floor.
        scenes = {}
        labels = ["Plastic bottle (clear PET)", "Wheeled suitcase",
                  "Portable transistor radio"]
        for i in range(15):
            label = labels[i % len(labels)]
            scenes[f"s{i}"] = scene(f"s{i}", label)
        applied = fb.adapt(fb.acceptance_stats(scenes, verdicts(
            rejected=list(scenes)), today=TODAY))
        self.assertEqual(len(applied["examples"]),
                         len(ag_catalog.inspiration_lines()))
        self.assertEqual(applied["droppedExamples"], [])
        self.assertEqual(applied["proposals"][0]["kind"], "examples")


class KnobTest(unittest.TestCase):
    """Ticket #1538: at most two numeric knobs, clamped, count-backed."""

    def test_a_bad_window_widens_the_repeat_memory(self):
        scenes, record = outboard_fixture()
        applied = fb.adapt(fb.acceptance_stats(scenes, record, today=TODAY))
        self.assertEqual(applied["knobs"]["repeat_window_days"], 14)
        knobs = [c for c in applied["changes"] if c["kind"] == "knob"]
        self.assertEqual((knobs[0]["before"], knobs[0]["after"]), (7, 14))

    def test_a_healthy_window_moves_nothing(self):
        scenes = {f"a{i}": scene(f"a{i}", "Wheeled suitcase")
                  for i in range(9)}
        applied = fb.adapt(fb.acceptance_stats(scenes, verdicts(
            accepted=list(scenes)), today=TODAY))
        self.assertEqual(applied["knobs"]["repeat_window_days"], 7)
        self.assertEqual(applied["changes"], [])

    def test_every_knob_is_clamped_to_its_documented_range(self):
        stats = {"sample": {"total": 10, "rejected": 10, "accepted": 0}}
        knobs = fb.choose_knobs(stats, dropped_examples=[{}] * 6,
                                current={"repeat_window_days": 999,
                                         "example_count": 99})
        for name, value in knobs.items():
            low, high = fb.KNOB_RANGES[name]
            self.assertGreaterEqual(value, low)
            self.assertLessEqual(value, high)
        self.assertLessEqual(len(knobs), fb.MAX_KNOBS)


class ProposalTest(unittest.TestCase):
    """Ticket #1538: findings the pass must not apply itself."""

    def test_an_archive_with_a_bad_record_is_a_finding_not_a_change(self):
        scenes = {f"c{i}": scene(f"c{i}", "Wheeled suitcase",
                                 repo="Wikimedia Commons")
                  for i in range(8)}
        record = verdicts(accepted=["c0"],
                          rejected=[f"c{i}" for i in range(1, 8)])
        applied = fb.adapt(fb.acceptance_stats(scenes, record, today=TODAY))
        kinds = [p["kind"] for p in applied["proposals"]]
        self.assertIn("archives", kinds)
        self.assertNotIn("Wikimedia Commons", applied["avoidFamilies"])


class RunTest(TempDataMixin, unittest.TestCase):
    def test_run_writes_adaptation_and_the_generator_can_read_it(self):
        scenes, record = outboard_fixture()
        self.write(scenes, record)
        fb.run(self.data_dir, window_days=14, now=datetime.datetime(
            2026, 9, 15, 3, 15, tzinfo=datetime.timezone.utc))
        path = self.data_dir / "adaptation.json"
        self.assertTrue(path.exists())
        saved = json.loads(path.read_text())
        self.assertEqual(saved["avoidLabels"], ["Modern outboard motor"])
        self.assertEqual(saved["windowDays"], 14)
        self.assertEqual(saved["changes"][0]["kind"], "avoid_label")
        # The generator reads the same file through its own loader.
        self.assertEqual(g.load_adaptation(self.data_dir)["avoidLabels"],
                         ["Modern outboard motor"])

    def test_dry_run_writes_nothing(self):
        scenes, record = outboard_fixture()
        self.write(scenes, record)
        fb.run(self.data_dir, dry_run=True, now=datetime.datetime(
            2026, 9, 15, 3, 15, tzinfo=datetime.timezone.utc))
        self.assertFalse((self.data_dir / "adaptation.json").exists())

    def test_the_digest_names_the_movers_and_the_applied_changes(self):
        scenes, record = outboard_fixture()
        self.write(scenes, record)
        report = fb.run(self.data_dir, dry_run=True, now=datetime.datetime(
            2026, 9, 15, 3, 15, tzinfo=datetime.timezone.utc))
        text = fb.digest(report)
        self.assertIn("2/11", text)  # the outboard record is a top mover
        self.assertIn("families avoided", text)
        self.assertIn("knob repeat_window_days", text)

    def test_a_missing_adaptation_leaves_the_defaults(self):
        self.assertEqual(g.load_adaptation(self.data_dir), {})


class GeneratorWiringTest(unittest.TestCase):
    """Ticket #1538: the adjustments reach the generator's gate and prompt."""

    def test_the_family_gate_flags_an_avoided_family(self):
        proposal = {"anomaly": "Plastic bottle (clear PET)"}
        self.assertEqual(g.family_conflict(proposal, ["drinks"]), "drinks")
        self.assertIsNone(g.family_conflict(proposal, ["luggage"]))
        findings = g.proposal_conflicts(
            proposal, {"title": "A market", "repository": GALLICA}, [], (),
            ["drinks"])
        self.assertIn("family", findings)
        self.assertIn("family the reviewer keeps rejecting",
                      g.conflict_reason(findings))

    def test_the_prompt_names_the_avoided_family_and_the_kept_examples(self):
        prompt = g.proposal_prompt(
            {"title": "A market", "repository": GALLICA},
            examples=["Only example; a later-era object"],
            avoid_families=["drinks"])
        self.assertIn("Only example; a later-era object", prompt)
        self.assertNotIn("Plastic bottle (clear PET);", prompt)
        self.assertIn("families, the reviewer keeps rejecting", prompt)


class PatternCountsTest(TempDataMixin, unittest.TestCase):
    """Ticket #1539: the pass refreshes the catalogue's counts."""

    def setUp(self):
        super().setUp()
        self.patterns = ap.load_patterns()

    def test_pattern_counts_tally_label_matched_verdicts(self):
        scenes, record = outboard_fixture()
        counts = fb.pattern_count_rows(scenes, record, self.patterns,
                                       today=TODAY)
        row = counts["carrier-missing"]
        self.assertEqual(row["accepted"], 2)
        self.assertEqual(row["rejected"], 9)
        self.assertEqual(row["decided"], 11)

    def test_a_pattern_without_matches_is_never_counted(self):
        scenes = {"s1": scene("s1", "Jet ski")}
        counts = fb.pattern_count_rows(
            scenes, verdicts(accepted=["s1"]), self.patterns, today=TODAY)
        self.assertNotIn("integrated-element", counts)

    def test_observe_pattern_with_evidence_is_proposed_for_promotion(self):
        patterns = [{"id": "candidate", "kind": "positive",
                     "status": ap.STATUS_OBSERVE, "matches": ["jet ski"]}]
        counts = {"candidate": {"accepted": 5, "rejected": 0, "decided": 5,
                                "rate": 1.0, "last": "2026-09-15"}}
        changes = fb.pattern_change_rows(counts, patterns)
        self.assertEqual(changes[0]["proposal"], "promote to active")

    def test_active_pattern_that_stopped_holding_is_proposed_for_demotion(self):
        patterns = [{"id": "was-good", "kind": "positive",
                     "status": ap.STATUS_ACTIVE, "matches": ["poster"]}]
        counts = {"was-good": {"accepted": 1, "rejected": 5, "decided": 6,
                               "rate": 0.1667, "last": "2026-09-15"}}
        changes = fb.pattern_change_rows(counts, patterns)
        self.assertEqual(changes[0]["proposal"], "demote to observe")

    def test_a_thin_sample_is_not_a_proposal(self):
        patterns = [{"id": "candidate", "kind": "positive",
                     "status": ap.STATUS_OBSERVE, "matches": ["jet ski"]}]
        counts = {"candidate": {"accepted": 1, "rejected": 0, "decided": 1,
                                "rate": 1.0, "last": "2026-09-15"}}
        self.assertEqual(fb.pattern_change_rows(counts, patterns), [])

    def test_great_marks_are_counted_next_to_the_verdicts(self):
        scenes, record = outboard_fixture()
        record["great"] = {"o0": "2026-09-15T08:00:00Z"}
        counts = fb.pattern_count_rows(scenes, record, self.patterns,
                                       today=TODAY)
        row = counts["carrier-missing"]
        self.assertEqual(row["great"], 1)
        self.assertEqual(row["greatScenes"], ["o0"])
        self.assertEqual(row["greatRate"], round(1 / 11, 3))

    def test_a_great_mark_outside_the_window_is_not_counted(self):
        scenes, record = outboard_fixture()
        record["great"] = {"o0": "2026-08-01T08:00:00Z"}
        counts = fb.pattern_count_rows(scenes, record, self.patterns,
                                       today=TODAY)
        self.assertEqual(counts["carrier-missing"]["great"], 0)

    def test_a_great_backed_pattern_is_promoted_on_a_lower_bar(self):
        patterns = [{"id": "candidate", "kind": "positive",
                     "status": ap.STATUS_OBSERVE, "matches": ["jet ski"]}]
        backed = {"candidate": {"accepted": 2, "rejected": 3, "decided": 5,
                                "rate": 0.4, "great": 2, "greatRate": 0.4,
                                "last": "2026-09-15"}}
        changes = fb.pattern_change_rows(backed, patterns)
        self.assertEqual(changes[0]["proposal"], "promote to active")
        self.assertIn("2 great", changes[0]["reason"])

    def test_without_great_backing_the_same_record_is_no_proposal(self):
        patterns = [{"id": "candidate", "kind": "positive",
                     "status": ap.STATUS_OBSERVE, "matches": ["jet ski"]}]
        plain = {"candidate": {"accepted": 2, "rejected": 3, "decided": 5,
                               "rate": 0.4, "great": 0, "greatRate": 0.0,
                               "last": "2026-09-15"}}
        self.assertEqual(fb.pattern_change_rows(plain, patterns), [])

    def test_run_writes_the_pattern_counts_file(self):
        scenes, record = outboard_fixture()
        self.write(scenes, record)
        report = fb.run(self.data_dir, now=datetime.datetime(2026, 9, 15, 6, 0))
        self.assertIn("carrier-missing", report["patternCounts"])
        path = self.data_dir / fb.PATTERN_COUNTS_NAME
        written = json.loads(path.read_text())
        self.assertEqual(written["counts"]["carrier-missing"]["decided"], 11)


if __name__ == "__main__":
    unittest.main()


class RejectReasonTest(unittest.TestCase):
    """Ticket #1627: reason-tagged and legacy prose rejections both read."""

    def record(self, **kw):
        base = {"version": 1, "accepted": {}, "rejected": {}, "comments": {}}
        base.update(kw)
        return base

    def test_a_reason_tagged_rejection_is_read(self):
        rec = self.record(
            rejected={"s1": "2026-09-14T10:00:00Z"},
            reasons={"s1": {"reason": "too easy",
                            "at": "2026-09-14T10:00:00Z"}})
        self.assertEqual(fb.reject_reason_entry(rec, "s1"), "too easy")
        self.assertEqual(fb.rejection_kind(rec, "s1"), "reason")

    def test_a_legacy_prose_rejection_is_read(self):
        rec = self.record(rejected={"s1": "2026-09-14T10:00:00Z"},
                          comments={"s1": [{"text": "too obvious"}]})
        self.assertIsNone(fb.reject_reason_entry(rec, "s1"))
        self.assertEqual(fb.rejection_kind(rec, "s1"), "prose")

    def test_a_silent_rejection_has_no_kind(self):
        rec = self.record(rejected={"s1": "2026-09-14T10:00:00Z"})
        self.assertEqual(fb.rejection_kind(rec, "s1"), "none")

    def test_the_window_counts_reasons_and_label_kinds(self):
        rec = self.record(
            rejected={
                "r1": "2026-09-14T10:00:00Z",
                "r2": "2026-09-14T10:00:00Z",
                "p1": "2026-09-14T10:00:00Z",
                "s1": "2026-09-14T10:00:00Z",
                "old": "2026-01-01T10:00:00Z",
            },
            reasons={
                "r1": {"reason": "too easy",
                       "at": "2026-09-14T10:00:00Z"},
                "r2": {"reason": "too easy",
                       "at": "2026-09-14T10:00:00Z"},
                "old": {"reason": "too easy",
                        "at": "2026-01-01T10:00:00Z"},
            },
            comments={"p1": [{"text": "blurry"}]},
        )
        counts = fb.reason_counts(rec, today=TODAY)
        self.assertEqual(counts["counts"]["too easy"], 2)
        self.assertEqual(counts["tagged"], 2)
        self.assertEqual(counts["prose"], 1)
        self.assertEqual(counts["silent"], 1)
        self.assertEqual(counts["rejected"], 4)


class RejectReasonRunTest(TempDataMixin, unittest.TestCase):
    def test_the_report_carries_the_reason_counts(self):
        scenes, _ = outboard_fixture()
        record = verdicts(rejected=["o2", "o3"])
        record["reasons"] = {"o3": {"reason": "too easy",
                                    "at": "2026-09-14T10:00:00Z"}}
        self.write(scenes, record)
        report = fb.run(self.data_dir, dry_run=True, now=datetime.datetime(
            2026, 9, 15, 3, 15, tzinfo=datetime.timezone.utc))
        self.assertEqual(report["rejectReasons"]["counts"]["too easy"], 1)
        self.assertEqual(report["rejectReasons"]["tagged"], 1)
        # o2 carries no comment, so it counts as a silent rejection.
        self.assertEqual(report["rejectReasons"]["silent"], 1)
