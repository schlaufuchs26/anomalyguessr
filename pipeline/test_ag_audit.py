"""Tests for pipeline/ag_audit.py (ticket #1485). No network, no model."""

import unittest

import ag_audit as a


class ClassifyTest(unittest.TestCase):
    def test_click_area_beats_the_absence_word(self):
        # "there is no such bottle in the click area" is a click-area finding.
        self.assertEqual(
            a.classify_comment("there is no such bottle in the click area"),
            "click_area")

    def test_an_absent_element(self):
        self.assertEqual(a.classify_comment("no smartphone in this image"),
                         "element_absent")
        self.assertEqual(a.classify_comment("there is no quadcopter drone "
                                            "in this iamge"),
                         "element_absent")

    def test_color_on_a_black_and_white_photo(self):
        self.assertEqual(a.classify_comment("color on black and white photo"),
                         "tone")

    def test_size(self):
        self.assertEqual(a.classify_comment("cable ties are not that big"),
                         "size")
        self.assertEqual(a.classify_comment("too prominent"), "size")

    def test_broken_render(self):
        self.assertEqual(a.classify_comment("he has a third arm"),
                         "broken_render")

    def test_scene_fit(self):
        self.assertEqual(a.classify_comment("makes no sense in this context"),
                         "scene_fit")

    def test_text_mismatch(self):
        self.assertEqual(a.classify_comment("the description is just wrong"),
                         "text_mismatch")

    def test_unknown_text_is_other(self):
        self.assertEqual(a.classify_comment("hmm"), "other")
        self.assertEqual(a.classify_comment(""), "other")


class DistributionTest(unittest.TestCase):
    def test_every_rejected_scene_is_counted_once(self):
        feedback = {
            "accepted": {"a": "2026-09-14T10:00:00+02:00"},
            "rejected": {"b": "2026-09-14T10:00:00+02:00",
                         "c": "2026-09-14T10:00:00+02:00"},
            "comments": {"b": [{"text": "no smartphone in this image"}],
                         "c": [{"text": "too big"},
                               {"text": "color on black and white photo"}]},
        }
        out = a.classify_rejections(feedback)
        self.assertEqual(out["rejected_scenes"], 2)
        self.assertEqual(out["uncommented"], 0)
        self.assertEqual(out["comments_classified"], 3)
        self.assertEqual(out["counts"]["element_absent"], 1)
        self.assertEqual(out["counts"]["size"], 1)
        self.assertEqual(out["counts"]["tone"], 1)

    def test_a_rejection_without_a_comment_is_reported(self):
        feedback = {"rejected": {"b": "x"}, "comments": {}}
        out = a.classify_rejections(feedback)
        self.assertEqual(out["uncommented"], 1)
        self.assertEqual(out["per_scene"]["b"], "uncommented")


class AgreementTest(unittest.TestCase):
    def state(self, *scenes):
        return {"scenes": {sid: {"checker": checker}
                           for sid, checker in scenes}}

    def test_a_clean_verdict_on_a_rejected_scene_is_a_false_green(self):
        state = self.state(("a", {"score": 9, "failed": []}))
        feedback = {"rejected": {"a": "x"}, "accepted": {}}
        out = a.rubric_agreement(state, feedback)
        self.assertEqual(out["judged"], 1)
        self.assertEqual(out["agreement"], 0.0)
        self.assertEqual(out["false_green"], 1)

    def test_a_flagged_verdict_on_an_accepted_scene_is_a_false_red(self):
        state = self.state(("a", {"score": 6, "failed": [3, 7]}))
        feedback = {"accepted": {"a": "x"}, "rejected": {}}
        out = a.rubric_agreement(state, feedback)
        self.assertEqual(out["false_red"], 1)

    def test_scenes_without_a_verdict_are_skipped(self):
        state = self.state(("a", None), ("b", {"score": 8, "failed": []}))
        state["scenes"]["a"].pop("checker")
        feedback = {"accepted": {"a": "x", "b": "x"}, "rejected": {}}
        out = a.rubric_agreement(state, feedback)
        self.assertEqual(out["judged"], 1)
        self.assertEqual(out["agreement"], 1.0)

    def test_an_unmoderated_scene_is_skipped(self):
        state = self.state(("a", {"score": 9, "failed": []}))
        out = a.rubric_agreement(state, {"accepted": {}, "rejected": {}})
        self.assertEqual(out["judged"], 0)
        self.assertIsNone(out["agreement"])

    def test_constant_answer_baselines_use_the_same_subset(self):
        # 1 accepted, 2 rejected: always-accept is 1/3, always-reject 2/3.
        state = self.state(("a", {"score": 9, "failed": []}),
                           ("b", {"score": 9, "failed": []}),
                           ("c", {"score": 6, "failed": [3]}))
        feedback = {"accepted": {"a": "x"}, "rejected": {"b": "x", "c": "x"}}
        out = a.rubric_agreement(state, feedback)
        self.assertEqual(out["judged"], 3)
        self.assertEqual(out["accepted"], 1)
        self.assertEqual(out["rejected"], 2)
        self.assertEqual(out["baseline_always_accept"], 0.333)
        self.assertEqual(out["baseline_always_reject"], 0.667)
        # The rubric agrees on b and c, not on a: 0.667.
        self.assertEqual(out["agreement"], 0.667)


class DayRateTest(unittest.TestCase):
    def test_the_daily_rate_comes_from_the_timestamps(self):
        feedback = {
            "accepted": {"a": "2026-09-14T10:00:00+02:00"},
            "rejected": {"b": "2026-09-14T10:00:00+02:00",
                         "c": "2026-09-13T10:00:00+02:00",
                         "d": "2026-09-13T10:00:00+02:00"},
        }
        out = a.acceptance_by_day(feedback)
        self.assertEqual(out["2026-09-14"]["rate"], 0.5)
        self.assertEqual(out["2026-09-13"]["rate"], 0.0)
        self.assertEqual(out["2026-09-13"]["rejected"], 2)


if __name__ == "__main__":
    unittest.main()
