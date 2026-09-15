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


class PointsAgreementTest(unittest.TestCase):
    def state(self, *scenes):
        return {"scenes": {sid: scene for sid, scene in scenes}}

    def test_best_threshold_and_baselines(self):
        # a: 5/5 points accepted; b: 0/5 rejected. A threshold of 1 cleanly
        # separates; always-reject is also 1/2 here, so the count must beat
        # it, which it does not on two scenes (both 0.5) - the report says so.
        state = self.state(
            ("a", {"checker": {"score": 9, "failed": [], "points": 5,
                               "points_total": 5}}),
            ("b", {"checker": {"score": 4, "failed": [1, 5, 6, 8, 9],
                               "points": 0, "points_total": 5}}),
        )
        feedback = {"accepted": {"a": "x"}, "rejected": {"b": "x"}}
        out = a.points_agreement(state, feedback)
        self.assertEqual(out["judged"], 2)
        self.assertEqual(out["agreement"], 1.0)
        self.assertEqual(out["all_points_agreement"], 1.0)
        self.assertEqual(out["baseline_always_accept"], 0.5)
        self.assertEqual(out["baseline_always_reject"], 0.5)

    def test_a_stored_mechanical_defect_blocks_a_pass(self):
        # A scene with all soft points but a presence defect cannot pass.
        state = self.state(
            ("a", {"checker": {"score": 9, "failed": [], "points": 5,
                               "points_total": 5},
                   "mechanical": {"presence": True, "tone": False,
                                  "size": False}}),
        )
        feedback = {"accepted": {"a": "x"}}
        out = a.points_agreement(state, feedback)
        self.assertEqual(out["defects"], 1)
        # It fails at every threshold: accepted but never counted clean.
        self.assertEqual(out["agreement"], 0.0)

    def test_scenes_without_a_verdict_are_skipped(self):
        state = self.state(("a", {}),)
        out = a.points_agreement(state, {"accepted": {"a": "x"}})
        self.assertEqual(out["judged"], 0)
        self.assertIsNone(out["agreement"])

    def test_points_are_derived_when_not_stored(self):
        # A pre-#1502 verdict has no points field; they come from failed +
        # total (soft = 1,5,6,8,9).
        state = self.state(
            ("a", {"checker": {"score": 8, "failed": [1], "total": 9}}),
        )
        out = a.points_agreement(state, {"accepted": {"a": "x"}})
        self.assertEqual(out["rows"][0]["points"], 4)
        self.assertEqual(out["rows"][0]["total"], 5)


class FunnyLabelsTest(unittest.TestCase):
    def test_tagged_count_and_base_rate(self):
        feedback = {"accepted": {"a": "x", "b": "x"}, "rejected": {"c": "x"},
                    "funny": {"a": "2026-09-14T10:00:00Z"}}
        out = a.funny_labels(feedback)
        self.assertEqual(out["tagged"], 1)
        self.assertEqual(out["judged"], 3)
        self.assertEqual(out["base_rate"], 0.333)

    def test_no_judged_scenes_has_no_base_rate(self):
        out = a.funny_labels({})
        self.assertEqual(out["base_rate"], None)

    def test_funny_needs_enough_labels(self):
        v = a.funny_verdict(10, 0.8, 0.1, min_marks=50)
        self.assertFalse(v["is_a_point"])
        self.assertIn("10 of 50", v["reason"])

    def test_funny_needs_a_blind_lift(self):
        v = a.funny_verdict(60, 0.8, 0.7, min_marks=50, min_lift=0.2)
        self.assertFalse(v["is_a_point"])
        v = a.funny_verdict(60, 0.8, 0.1, min_marks=50, min_lift=0.2)
        self.assertTrue(v["is_a_point"])


class GreatLabelsTest(unittest.TestCase):
    """Ticket #1541: the "great" curation label is reported like "funny"."""

    def test_counts_and_handles(self):
        feedback = {"accepted": {"a": "x", "b": "x"}, "rejected": {"c": "x"},
                    "great": {"a": "2026-09-15T10:00:00Z"}}
        scenes = {"a": {"shortId": "AG-7"}}
        out = a.great_labels(feedback, scenes)
        self.assertEqual(out["tagged"], 1)
        self.assertEqual(out["judged"], 3)
        self.assertEqual(out["handles"], {"a": "AG-7"})

    def test_a_scene_without_a_handle_falls_back_to_its_id(self):
        out = a.tag_labels({"great": {"gone": "x"}}, "great", {})
        self.assertEqual(out["handles"], {"gone": "gone"})

    def test_both_tags_are_tallied_from_their_own_field(self):
        feedback = {"accepted": {"a": "x"},
                    "funny": {"a": "x"}, "great": {"a": "x"}}
        self.assertEqual(a.funny_labels(feedback)["tagged"], 1)
        self.assertEqual(a.great_labels(feedback)["tagged"], 1)
        self.assertEqual(a.great_labels({"funny": {"a": "x"}})["tagged"], 0)

    def test_handle_list_is_capped(self):
        line = a._handle_list({str(i): f"AG-{i}" for i in range(10)})
        self.assertIn("+2", line)
        self.assertEqual(a._handle_list({}), "none")


if __name__ == "__main__":
    unittest.main()
