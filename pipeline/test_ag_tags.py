"""Tests for pipeline/ag_tags.py (ticket #2344).

No network and no API spend: the tagging call is injected, so the parser, the
cache/no-retag path and the report's joins over a canned verdict set are all
covered deterministically.
"""

import json
import shutil
import tempfile
import unittest
from pathlib import Path

import ag_sources
import ag_tags


def _tags(people="few", scene="street", medium="grayscale", clutter="busy",
          host=True, text=False):
    return {"people": people, "scene": scene, "medium": medium,
            "clutter": clutter, "host": host, "text": text}


class ParseTagsTest(unittest.TestCase):
    def test_plain_json(self):
        self.assertEqual(
            ag_tags.parse_tags('{"people": "crowd", "scene": "market", '
                               '"medium": "colour", "clutter": "busy", '
                               '"host": true, "text": false}'),
            _tags(people="crowd", scene="market", medium="colour"))

    def test_fenced_and_wrapped(self):
        answer = ('Sure!\n```json\n{"people":"none","scene":"landscape",'
                  '"medium":"sepia","clutter":"clean","host":false,'
                  '"text":true}\n```')
        self.assertEqual(
            ag_tags.parse_tags(answer),
            _tags(people="none", scene="landscape", medium="sepia",
                  clutter="clean", host=False, text=True))

    def test_case_and_boolean_strings(self):
        tags = ag_tags.parse_tags({"people": "Crowd", "scene": "STREET",
                                   "medium": "colour", "clutter": "busy",
                                   "host": "yes", "text": "no"})
        self.assertTrue(tags["host"])
        self.assertFalse(tags["text"])
        self.assertEqual(tags["people"], "crowd")

    def test_unknown_enum_is_unusable(self):
        self.assertIsNone(ag_tags.parse_tags(
            {"people": "many", "scene": "street", "medium": "colour",
             "clutter": "busy", "host": True, "text": False}))

    def test_missing_field_is_unusable(self):
        self.assertIsNone(ag_tags.parse_tags(
            {"people": "few", "scene": "street", "medium": "colour",
             "clutter": "busy", "host": True}))

    def test_non_boolean_flag_is_unusable(self):
        self.assertIsNone(ag_tags.parse_tags(
            {"people": "few", "scene": "street", "medium": "colour",
             "clutter": "busy", "host": "maybe", "text": False}))

    def test_non_object_is_unusable(self):
        self.assertIsNone(ag_tags.parse_tags("no json here"))
        self.assertIsNone(ag_tags.parse_tags("[1, 2, 3]"))


class CacheTest(unittest.TestCase):
    def test_missing_tags_needs_tagging(self):
        self.assertTrue(ag_tags.needs_tags({"id": "x"}))

    def test_complete_tags_are_kept(self):
        self.assertFalse(ag_tags.needs_tags({"id": "x", "tags": _tags()}))

    def test_partial_or_broken_tags_are_retagged(self):
        self.assertTrue(ag_tags.needs_tags({"tags": {"people": "few"}}))
        self.assertTrue(ag_tags.needs_tags({"tags": {"people": "many"}}))


class _DataDir:
    """A throwaway data dir with a pool index, state, traces and feedback."""

    def __init__(self):
        self.dir = Path(tempfile.mkdtemp(prefix="ag-tags-"))
        (self.dir / "sources" / "images").mkdir(parents=True)
        (self.dir / "traces").mkdir()

    def close(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def add_source(self, sid, *, url=None, image=True, tags=None, added="2026-09-01"):
        entry = {"id": sid, "repository": "Test", "fileUrl": url or f"u://{sid}",
                 "image": f"images/{sid}.jpg", "added": added}
        if tags is not None:
            entry["tags"] = tags
        if image:
            (self.dir / "sources" / "images" / f"{sid}.jpg").write_bytes(b"x")
        index = ag_sources.load_index(self.dir)
        index.setdefault("sources", {})[sid] = entry
        ag_sources.save_index(self.dir, index)
        return entry

    def write_state(self, scenes):
        (self.dir / "state.json").write_text(json.dumps({"scenes": scenes}))

    def write_feedback(self, accepted=(), rejected=(), great=()):
        (self.dir / "feedback.json").write_text(json.dumps({
            "accepted": {s: "2026-10-01T00:00:00+02:00" for s in accepted},
            "rejected": {s: "2026-10-01T00:00:00+02:00" for s in rejected},
            "great": {s: "2026-10-01T00:00:00+02:00" for s in great}}))

    def write_trace(self, scene_id, source_id):
        (self.dir / "traces" / f"{scene_id}.json").write_text(
            json.dumps({"source": source_id}))


class BackfillTest(unittest.TestCase):
    def setUp(self):
        self.d = _DataDir()

    def tearDown(self):
        self.d.close()

    def test_tags_untagged_and_skips_cached(self):
        self.d.add_source("s1")
        self.d.add_source("s2", tags=_tags(people="crowd"))
        calls = []

        def tag_fn(entry):
            calls.append(entry["id"])
            return dict(entry, tags=_tags(people="none")), {"status": "tagged"}

        report = ag_tags.backfill(self.d.dir, tag_fn=tag_fn)
        self.assertEqual(calls, ["s1"])
        self.assertEqual(report["targets"], 1)
        self.assertEqual(report["changed"], 1)
        index = ag_sources.load_index(self.d.dir)
        self.assertEqual(index["sources"]["s1"]["tags"]["people"], "none")
        self.assertEqual(index["sources"]["s2"]["tags"]["people"], "crowd")

    def test_limit_bounds_the_run(self):
        for i in range(5):
            self.d.add_source(f"s{i}")
        calls = []

        def tag_fn(entry):
            calls.append(entry["id"])
            return dict(entry, tags=_tags()), {"status": "tagged"}

        report = ag_tags.backfill(self.d.dir, tag_fn=tag_fn, limit=2)
        self.assertEqual(len(calls), 2)
        self.assertEqual(report["targets"], 2)
        self.assertEqual(report["changed"], 2)

    def test_second_run_is_a_no_op(self):
        self.d.add_source("s1")

        def tag_fn(entry):
            return dict(entry, tags=_tags()), {"status": "tagged"}

        ag_tags.backfill(self.d.dir, tag_fn=tag_fn)
        report = ag_tags.backfill(self.d.dir, tag_fn=tag_fn)
        self.assertEqual(report["targets"], 0)
        self.assertEqual(report["changed"], 0)

    def test_missing_image_is_reported_not_stored(self):
        self.d.add_source("s1", image=False)
        report = ag_tags.backfill(
            self.d.dir,
            tag_fn=lambda e: (e, {"status": "missing_image"}))
        self.assertEqual(report["missing_image"], 1)
        self.assertEqual(report["changed"], 0)


class JoinTest(unittest.TestCase):
    def setUp(self):
        self.d = _DataDir()

    def tearDown(self):
        self.d.close()

    def test_trace_wins_and_url_is_the_fallback(self):
        self.d.add_source("s-trace", url="u://trace")
        self.d.add_source("s-url", url="u://url")
        self.d.write_state({
            "a": {"sourceUrl": "u://trace", "anomaly": "Modern bike",
                  "family": "street-furniture"},
            "b": {"sourceUrl": "u://url", "anomaly": "Time traveler: x",
                  "family": "person"},
        })
        self.d.write_trace("a", "s-trace")
        index = ag_sources.load_index(self.d.dir)
        state = json.loads((self.d.dir / "state.json").read_text())
        self.assertEqual(ag_tags.scene_sources(self.d.dir, index, state),
                         {"a": "s-trace", "b": "s-url"})


class ReportTest(unittest.TestCase):
    def setUp(self):
        self.d = _DataDir()

    def tearDown(self):
        self.d.close()

    def _build(self, feedback_args):
        self.d.write_feedback(**feedback_args)
        index = ag_sources.load_index(self.d.dir)
        state = json.loads((self.d.dir / "state.json").read_text())
        source_of = ag_tags.scene_sources(self.d.dir, index, state)
        feedback = json.loads((self.d.dir / "feedback.json").read_text())
        return ag_tags.build_report(index, state, feedback, source_of)

    def test_crowd_vs_empty_person_acceptance(self):
        # Two crowded person scenes (both accepted) and two empty ones (both
        # rejected): the person family should read 100% vs 0%.
        self.d.add_source("crowd1", tags=_tags(people="crowd"))
        self.d.add_source("crowd2", tags=_tags(people="crowd"))
        self.d.add_source("none1", tags=_tags(people="none"))
        self.d.add_source("none2", tags=_tags(people="none"))
        scenes = {}
        for sid in ("crowd1", "crowd2", "none1", "none2"):
            scenes[sid] = {"sourceUrl": f"u://{sid}",
                           "anomaly": "Time traveler: x", "family": "person"}
            self.d.write_trace(sid, sid)
        self.d.write_state(scenes)
        report = self._build({"accepted": ["crowd1", "crowd2"],
                              "rejected": ["none1", "none2"]})
        person = report["features"]["people"]["person"]
        self.assertEqual(person["crowd"]["decided"], 2)
        self.assertEqual(person["crowd"]["rate"], 1.0)
        self.assertEqual(person["none"]["decided"], 2)
        self.assertEqual(person["none"]["rate"], 0.0)

    def test_untagged_scene_is_left_out(self):
        self.d.add_source("tagged", tags=_tags(people="crowd"))
        self.d.add_source("untagged")
        scenes = {"tagged": {"sourceUrl": "u://tagged", "family": "person",
                             "anomaly": "Time traveler: x"},
                  "untagged": {"sourceUrl": "u://untagged", "family": "person",
                               "anomaly": "Time traveler: y"}}
        self.d.write_state(scenes)
        self.d.write_trace("tagged", "tagged")
        self.d.write_trace("untagged", "untagged")
        report = self._build({"accepted": ["tagged", "untagged"]})
        self.assertEqual(report["scenes"]["verdicts_resolved"], 1)

    def test_great_is_a_subset_of_accepted(self):
        self.d.add_source("g1", tags=_tags(people="crowd"))
        self.d.add_source("g2", tags=_tags(people="crowd"))
        scenes = {sid: {"sourceUrl": f"u://{sid}", "family": "drinks",
                        "anomaly": "Plastic bottle"}
                  for sid in ("g1", "g2")}
        self.d.write_state(scenes)
        for sid in ("g1", "g2"):
            self.d.write_trace(sid, sid)
        report = self._build({"accepted": ["g1", "g2"], "great": ["g1"]})
        drinks = report["features"]["people"]["drinks"]["crowd"]
        self.assertEqual(drinks["accepted"], 2)
        self.assertEqual(drinks["great"], 1)
        self.assertEqual(drinks["greatRate"], 0.5)

    def test_markdown_renders_a_table(self):
        self.d.add_source("g1", tags=_tags(people="crowd"))
        self.d.write_state({"g1": {"sourceUrl": "u://g1", "family": "drinks",
                                   "anomaly": "Plastic bottle"}})
        self.d.write_trace("g1", "g1")
        report = self._build({"accepted": ["g1"]})
        md = ag_tags.render_report(report)
        self.assertIn("| family |", md)
        self.assertIn("| drinks |", md)
        self.assertIn("## The hypothesis", md)
        self.assertIn("all families", md)

    def test_overall_rows_sum_across_families(self):
        self.d.add_source("a", tags=_tags(people="crowd"))
        self.d.add_source("b", tags=_tags(people="crowd"))
        scenes = {"a": {"sourceUrl": "u://a", "family": "drinks",
                        "anomaly": "Plastic bottle"},
                  "b": {"sourceUrl": "u://b", "family": "vehicle",
                        "anomaly": "E-scooter"}}
        self.d.write_state(scenes)
        self.d.write_trace("a", "a")
        self.d.write_trace("b", "b")
        report = self._build({"accepted": ["a"], "rejected": ["b"]})
        overall = ag_tags.overall_rows(report["features"]["people"])
        self.assertEqual(overall["crowd"]["decided"], 2)
        self.assertEqual(overall["crowd"]["rate"], 0.5)


if __name__ == "__main__":
    unittest.main()
