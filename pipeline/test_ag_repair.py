"""Tests for pipeline/ag_repair.py (ticket #2590).

Run with: python3 -m unittest discover -s pipeline -t pipeline

No network and no API spend: the vision pass, the crop confirm and the text
calls are injected, so only the pass's own classification, writing and report
logic is exercised.
"""

import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import ag_repair as rep


def scene(sid, anomaly="Plastic bottle", answer=None, explanation="x"):
    return {"id": sid, "anomaly": anomaly, "year": "1907",
            "explanation": explanation,
            "references": [{"label": "ref", "url": "https://example.test/r"}],
            "answer": answer or {"x": 0.4, "y": 0.5, "r": 0.05}}


class TempDataMixin:
    def setUp(self):
        self._tmp = Path(tempfile.mkdtemp())
        self.data_dir = self._tmp / "anomalyguessr"
        self.data_dir.mkdir(parents=True)

    def tearDown(self):
        shutil.rmtree(self._tmp, ignore_errors=True)

    def write(self, scenes, repairs):
        (self.data_dir / "state.json").write_text(
            json.dumps({"version": 1, "last_shipped": None, "scenes": scenes}))
        (self.data_dir / "feedback.json").write_text(
            json.dumps({"version": 1, "accepted": {}, "rejected": {},
                        "comments": {}, "reasons": {}, "repairs": repairs}))
        for sid in scenes:
            lib = self.data_dir / "library" / sid
            lib.mkdir(parents=True)
            (lib / f"{sid}.jpg").write_bytes(b"edited")
            (lib / f"{sid}-original.jpg").write_bytes(b"original")

    def feedback(self):
        return json.loads((self.data_dir / "feedback.json").read_text())

    def state(self):
        return json.loads((self.data_dir / "state.json").read_text())


def request(reasons, at="2026-10-13T06:00:00+02:00"):
    return {"status": "requested", "reasons": reasons, "requestedAt": at}


class ClassifyTest(unittest.TestCase):
    def test_click_text_and_mixed(self):
        self.assertEqual(rep.classify(["click area too big"]), "click")
        self.assertEqual(rep.classify(["text is inaccurate"]), "text")
        self.assertEqual(
            rep.classify(["click area too big", "text is inaccurate"]),
            "click+text")

    def test_any_unrepairable_reason_wins(self):
        self.assertEqual(
            rep.classify(["click area too big", "too easy"]), "unrepairable")
        self.assertEqual(rep.classify(["too hard"]), "unrepairable")

    def test_unrepairable_reasons_lists_the_blockers(self):
        self.assertEqual(
            rep.unrepairable_reasons(["too easy", "click area too big"]),
            ["too easy"])

    def test_person_label(self):
        self.assertTrue(rep.is_person({"anomaly": "Time traveler: tourist"}))
        self.assertTrue(rep.is_person({"anomaly": "Robot time traveler"}))
        self.assertFalse(rep.is_person({"anomaly": "Plastic bottle"}))


class PendingRequestsTest(unittest.TestCase):
    def test_only_requested_entries(self):
        fb = {"repairs": {
            "a": request(["too easy"]),
            "b": {"status": "repaired", "reasons": ["too easy"]},
            "c": {"status": "requested", "reasons": ["click area too big"]},
        }}
        self.assertEqual(rep.pending_requests(fb),
                         [("a", ["too easy"]), ("c", ["click area too big"])])

    def test_no_repairs_key(self):
        self.assertEqual(rep.pending_requests({}), [])


class ConfirmCropTest(unittest.TestCase):
    def _run(self, present_in):
        def ask(image, prompt, key, model):
            return json.dumps({"present_in": present_in, "note": "n"})

        with mock.patch.object(rep.ag_verify, "crop_window",
                               return_value=((0, 0, 512, 512), (1000, 1000))), \
                mock.patch.object(rep.ag_verify, "save_crop"), \
                mock.patch.object(rep, "_montage"):
            return rep.confirm_crop(Path("/tmp/e.jpg"), Path("/tmp/o.jpg"),
                                    {"x": 0.5, "y": 0.5}, "Plastic bottle",
                                    "k", "m", ask=ask)

    def test_visible_in_the_edited_crop_only_passes(self):
        out = self._run("left")
        self.assertTrue(out["ok"])

    def test_visible_in_both_is_a_reencode_artifact(self):
        out = self._run("both")
        self.assertFalse(out["ok"])
        self.assertIn("re-encode", out["reason"])

    def test_visible_in_neither_fails(self):
        self.assertFalse(self._run("neither")["ok"])

    def test_a_garbled_answer_fails(self):
        def ask(image, prompt, key, model):
            return "not json"

        with mock.patch.object(rep.ag_verify, "crop_window",
                               return_value=((0, 0, 512, 512), (1000, 1000))), \
                mock.patch.object(rep.ag_verify, "save_crop"), \
                mock.patch.object(rep, "_montage"):
            out = rep.confirm_crop(Path("/tmp/e.jpg"), Path("/tmp/o.jpg"),
                                   {"x": 0.5, "y": 0.5}, "x", "k", "m", ask=ask)
        self.assertFalse(out["ok"])


def confirm_ok(edited, original, answer, anomaly, key, model):
    return {"ok": True, "note": "confirmed"}


def confirm_fail(edited, original, answer, anomaly, key, model):
    return {"ok": False, "reason": "the anomaly is visible in both crops"}


def verify_vision(edited, original, anomaly, **kw):
    return {"ok": True, "localization": "vision",
            "answer": {"x": 0.61, "y": 0.4, "r": 0.07}, "box": [0.5, 0.3, 0.7, 0.5]}


class RunTest(TempDataMixin, unittest.TestCase):
    def test_unrepairable_request_returns_the_scene_to_rejected(self):
        self.write({"a": scene("a")}, {"a": request(["too easy"])})
        report = rep.run(self.data_dir)
        self.assertEqual(report["unrepairable"], 1)
        self.assertEqual(report["unrepairableByReason"], {"too easy": 1})
        fb = self.feedback()
        self.assertIn("a", fb["rejected"])
        self.assertEqual(fb["repairs"]["a"]["status"], "unrepairable")
        self.assertIn("too easy", fb["repairs"]["a"]["detail"])
        self.assertEqual(fb["reasons"]["a"]["reason"], ["too easy"])
        self.assertIn("too easy", rep.digest(report))

    def test_click_repair_writes_the_new_answer_and_keeps_the_old(self):
        self.write({"a": scene("a")},
                   {"a": request(["click area doesn't cover anomaly"])})
        report = rep.run(self.data_dir, verify=verify_vision, confirm=confirm_ok)
        self.assertEqual(report["repaired"], 1)
        self.assertEqual(report["repairedByKind"], {"click": 1})
        st = self.state()
        self.assertEqual(st["scenes"]["a"]["answer"], {"x": 0.61, "y": 0.4, "r": 0.07})
        self.assertEqual(st["scenes"]["a"]["answer_before"],
                         {"x": 0.4, "y": 0.5, "r": 0.05})
        fb = self.feedback()
        self.assertEqual(fb["repairs"]["a"]["status"], "repaired")
        self.assertEqual(fb["repairs"]["a"]["answerAfter"],
                         {"x": 0.61, "y": 0.4, "r": 0.07})
        self.assertNotIn("a", fb["rejected"])

    def test_unconfirmed_click_repair_is_unrepairable(self):
        self.write({"a": scene("a")},
                   {"a": request(["click area too big"])})
        report = rep.run(self.data_dir, verify=verify_vision,
                         confirm=confirm_fail)
        self.assertEqual(report["unrepairable"], 1)
        fb = self.feedback()
        self.assertEqual(fb["repairs"]["a"]["status"], "unrepairable")
        self.assertIn("a", fb["rejected"])

    def test_text_repair_regenerates_and_checks(self):
        self.write({"a": scene("a")}, {"a": request(["text is inaccurate"])})
        calls = []

        def chat(messages, api_key, model, **kw):
            prompt = messages[0]["content"][0]["text"]
            calls.append(prompt)
            if "proposed explanation" in prompt:
                return {"choices": [{"message": {"content": json.dumps(
                    {"matches": True, "reason": "fits the photo"})}}]}
            return {"choices": [{"message": {"content": json.dumps(
                {"explanation": "PET bottles did not exist in 1907.",
                 "references": [{"label": "PET", "url": "https://example.test/pet"}],
                 "matches_image": True, "reason": "visible"})}}]}

        report = rep.run(self.data_dir, chat=chat)
        self.assertEqual(report["repaired"], 1)
        self.assertEqual(report["repairedByKind"], {"text": 1})
        self.assertEqual(len(calls), 2)
        st = self.state()
        self.assertIn("PET bottles", st["scenes"]["a"]["explanation"])
        fb = self.feedback()
        self.assertEqual(fb["repairs"]["a"]["status"], "repaired")
        self.assertIn("PET bottles", fb["repairs"]["a"]["textAfter"])

    def test_a_tooling_error_leaves_the_request_pending(self):
        self.write({"a": scene("a")},
                   {"a": request(["click area doesn't cover anomaly"])})

        def verify_boom(*a, **kw):
            raise RuntimeError("network down")

        report = rep.run(self.data_dir, verify=verify_boom, confirm=confirm_ok)
        self.assertEqual(report["failed"], 1)
        fb = self.feedback()
        self.assertEqual(fb["repairs"]["a"]["status"], "requested")
        self.assertNotIn("a", fb["rejected"])

    def test_dry_run_writes_nothing(self):
        self.write({"a": scene("a")}, {"a": request(["too easy"])})
        before = (self.data_dir / "feedback.json").read_text()
        report = rep.run(self.data_dir, dry_run=True)
        self.assertTrue(report["dryRun"])
        self.assertEqual(report["repaired"], 0)
        self.assertEqual((self.data_dir / "feedback.json").read_text(), before)

    def test_no_requests_is_a_quiet_run(self):
        self.write({"a": scene("a")}, {})
        report = rep.run(self.data_dir)
        self.assertEqual(report["requests"], 0)
        self.assertEqual(rep.digest(report),
                         "AnomalyGuessr repair: no pending requests")


if __name__ == "__main__":
    unittest.main()
