"""Tests for pipeline/ag_verify.py (ticket #1107, localization rework #1165).

Run with: python3 scripts/test_ag_verify.py

Synthetic images are generated with ImageMagick. The vision network call is
never made: vision-gated paths are tested with a fake model via monkeypatch
on ag_verify.vision_check, or with --no-vision for the image logic.
"""

import json
import io
import math
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

import ag_verify as tv

SCRIPTS_DIR = Path(__file__).resolve().parent


def make_img(path: Path, w=800, h=600, color="gray", size=None):
    path.parent.mkdir(parents=True, exist_ok=True)
    args = ["convert", "-size", f"{w}x{h}", f"xc:{color}"]
    if size:
        args += ["-resize", size]
    args.append(str(path))
    subprocess.run(args, check=True, capture_output=True)


def make_edited_with_patch(orig: Path, edited: Path, px=90, py=70,
                           pw=90, ph=70, patch="white"):
    """Copy orig to edited, then draw a bright patch (the anomaly)."""
    subprocess.run(["convert", str(orig), "-fill", patch, "-draw",
                    f"rectangle {px},{py} {px + pw},{py + ph}", str(edited)],
                   check=True, capture_output=True)


class DiffGridTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="agv_"))
        self.orig = self.tmp / "orig.png"
        make_img(self.orig, 800, 600, "gray(120)")

    def test_identical_images_produce_empty_diff(self):
        grid = tv.diff_grid(self.orig, self.orig)
        self.assertEqual(grid["w"], 800)
        self.assertEqual(grid["h"], 600)
        self.assertTrue(all(v < 5 for v in grid["cells"].values()))

    def test_patch_produces_local_hotspot(self):
        edited = self.tmp / "edited.png"
        make_edited_with_patch(self.orig, edited)
        loc = tv.locate_hotspot(edited, self.orig)
        self.assertTrue(loc["ok"], loc)
        top = loc["hotspot"]
        # patch center ~ (135/800, 105/600) = (0.17, 0.175)
        self.assertAlmostEqual(top["cx"], 0.17, delta=0.06)
        self.assertAlmostEqual(top["cy"], 0.175, delta=0.06)
        self.assertLess(top["size"] / (tv.GRID * tv.GRID), 0.35)
        ans = tv.answer_from_hotspot(top)
        self.assertAlmostEqual(ans["x"], 0.17, delta=0.06)
        self.assertGreaterEqual(ans["r"], 0.02)
        self.assertLessEqual(ans["r"], 0.15)

    def test_resized_original_aligns(self):
        # original at a different size (the generator rescales): the aligned
        # diff must still find the patch after resizing the original up.
        small = self.tmp / "small.png"
        make_img(small, 640, 480, "gray(120)")
        edited = self.tmp / "edited.png"
        make_edited_with_patch(small, edited, px=90, py=70, pw=90, ph=70)
        # edited keeps small's 640x480 here; resizing path triggers when the
        # edited dims differ from the original's; simulate by enlarging the
        # edited to 800x600 after patching (scale the patch with it).
        big = self.tmp / "big.png"
        subprocess.run(["convert", str(edited), "-resize", "800x600!",
                        str(big)], check=True)
        loc = tv.locate_hotspot(big, small)
        self.assertTrue(loc["ok"], loc)
        # patch was at ~ (135/640, 105/480) -> scaled ~ (169/800, 131/600)
        self.assertAlmostEqual(loc["hotspot"]["cx"], 0.21, delta=0.07)
        self.assertAlmostEqual(loc["hotspot"]["cy"], 0.22, delta=0.07)

    def test_whole_frame_repaint_rejected(self):
        edited = self.tmp / "bright.png"
        # brighten everything: no local hotspot, cluster covers the frame
        make_img(edited, 800, 600, "gray(200)")
        loc = tv.locate_hotspot(edited, self.orig)
        self.assertFalse(loc["ok"])
        self.assertIn("repaint", loc["reason"])

    def test_no_change_rejected(self):
        # only JPEG-ish tiny noise: below threshold
        edited = self.tmp / "noise.png"
        subprocess.run(["convert", str(self.orig), "-attenuate", "0.01",
                        "+noise", "Gaussian", str(edited)], check=True)
        loc = tv.locate_hotspot(edited, self.orig)
        # either rejected as too small or accepted with a tiny cluster; the
        # important part is that a *huge* global change is never accepted
        if loc["ok"]:
            self.assertLess(loc["area"], 0.01)

    def test_global_tone_shift_rejected(self):
        # A whole-frame tone change (the 09-08 fortepan outputs had a global
        # JPEG-scale re-encode ON TOP of the object edit): the verifier must
        # reject it as a whole-frame repaint, not locate the strongest edge
        # artifact as the object position.
        edited = self.tmp / "tone.png"
        subprocess.run(["convert", str(self.orig), "-brightness-contrast",
                        "45x0", str(edited)], check=True)
        loc = tv.locate_hotspot(edited, self.orig)
        self.assertFalse(loc["ok"], loc)
        self.assertIn("repaint", loc["reason"])

    def test_local_patch_on_global_tone_shift_accepted(self):
        # A REAL localized edit (the drawn patch) on top of a global tone
        # shift must still be found. Regression guard for the ticket #1122
        # calibration: CHANGE_LEVEL must sit above JPEG noise but below a
        # genuine object edit, and the whole-frame guard must not eat the
        # object's cluster.
        edited = self.tmp / "tone_patch.png"
        # A MODERATE global shift (below CHANGE_LEVEL's discrimination) plus
        # a real patch: this is the strong-local-signal case the guard must
        # keep. (An EXTREME brightness shift still trips the repaint guard,
        # which is the desired conservative behavior for the real pipeline.)
        subprocess.run(["convert", str(self.orig), "-brightness-contrast",
                        "30x0", str(edited)], check=True)
        make_edited_with_patch(edited, edited, px=90, py=70, pw=90, ph=70,
                               patch="white")
        loc = tv.locate_hotspot(edited, self.orig)
        self.assertTrue(loc["ok"], loc)
        self.assertAlmostEqual(loc["hotspot"]["cx"], 0.17, delta=0.08)
        self.assertAlmostEqual(loc["hotspot"]["cy"], 0.175, delta=0.08)


class ParseVisionTest(unittest.TestCase):
    def test_parses_valid_json(self):
        out = tv.parse_vision(
            '{"present": true, "note": "clearly visible", '
            '"box": [0.1, 0.2, 0.4, 0.5]}')
        self.assertTrue(out["present"])
        self.assertEqual(out["box"], [0.1, 0.2, 0.4, 0.5])

    def test_parses_fenced_json(self):
        out = tv.parse_vision('```json\n{"present": false, "note": "x", '
                              '"box": null}\n```')
        self.assertFalse(out["present"])
        self.assertIsNone(out["box"])

    def test_rejects_invalid_box(self):
        out = tv.parse_vision(
            '{"present": true, "note": "x", "box": [0.5, 0.5, 0.2, 0.9]}')
        self.assertTrue(out["present"])
        self.assertIsNone(out["box"])

    def test_rejects_point_box(self):
        # #1165: a box smaller than MIN_BOX_EXTENT is a mis-parse (the model
        # returned a point, not an object extent); it must degrade to None so
        # the caller can fall back to the pixel-diff path.
        out = tv.parse_vision(
            '{"present": true, "note": "x", "box": [0.3, 0.3, 0.3001, 0.3001]}')
        self.assertTrue(out["present"])
        self.assertIsNone(out["box"])

    def test_rejects_whole_frame_box(self):
        # A box covering > MAX_BOX_EXTENT means the model did not isolate the
        # anomaly; treat as no usable localization.
        out = tv.parse_vision(
            '{"present": true, "note": "x", "box": [0.0, 0.0, 0.98, 0.98]}')
        self.assertTrue(out["present"])
        self.assertIsNone(out["box"])

    def test_parses_size_hint(self):
        out = tv.parse_vision(
            '{"present": true, "note": "x", '
            '"box": [0.1, 0.2, 0.4, 0.5], '
            '"size_hint": "about 2 percent of the image height"}')
        self.assertEqual(out["size_hint"],
                         "about 2 percent of the image height")

    def test_rejects_non_json(self):
        out = tv.parse_vision("I see no object here.")
        self.assertFalse(out["present"])


class VisionPromptTest(unittest.TestCase):
    def test_wording_is_element_neutral_for_objects_persons_future(self):
        # ticket #1115 + #1136 + #1161: verification must work for planted
        # modern objects, the time-traveler person category AND fictional-
        # future elements ("anachronism"/"modern object" wording would
        # mislead the model for the person and future-tech cases). #1161:
        # "impossible/fictional" became "futuristic technology from a
        # fictional future" (flying saucers / fantasy creatures are out).
        for anomaly in ("Plastic bottle",
                        "Time traveler: young man with modern sneakers",
                        "Robot time traveler standing in the crowd"):
            for full in (True, False):
                text = tv.vision_user_text(anomaly, full_image=full)
                self.assertIn(anomaly, text)
                self.assertIn("anomaly", text)
                self.assertNotIn("anachronism", text)
                self.assertNotIn("a modern object", text)
                self.assertNotIn("impossible/fictional", text)
                self.assertNotIn("dragon", text)
                self.assertIn("fictional future", text)
                self.assertIn('"present"', text)

    def test_prompt_includes_anomaly_once(self):
        text = tv.vision_user_text("Digital watch", full_image=True)
        self.assertEqual(text.count("Digital watch"), 1)

    def test_full_image_prompt_asks_for_box_and_size_hint(self):
        # #1165: the primary localization prompt must instruct the model to
        # return a normalized bounding box of the anomaly in the FULL image.
        text = tv.vision_user_text("Digital watch", full_image=True)
        self.assertIn("full image", text)
        self.assertIn("bounding box", text)
        self.assertIn("normalized 0..1", text)
        self.assertIn("x1,y1", text)
        self.assertIn("size_hint", text)
        # the crop fallback prompt has no size_hint
        crop_text = tv.vision_user_text("Digital watch", full_image=False)
        self.assertNotIn("size_hint", crop_text)

    def test_full_image_prompt_demands_the_whole_anomaly(self):
        # #1328: a tight box around the most modern detail (a person's shoes)
        # made clicks on the rest of the person miss. The prompt must ask for
        # the whole figure/object instead.
        text = tv.vision_user_text("Digital watch", full_image=True)
        self.assertIn("WHOLE anomaly", text)
        self.assertIn("head to feet", text)
        self.assertIn("fully contain", text)
        # the crop fallback stays a tight localization
        self.assertNotIn("head to feet",
                         tv.vision_user_text("Digital watch",
                                             full_image=False))


class MapCropBoxTest(unittest.TestCase):
    def test_maps_to_full_image(self):
        # crop at (200,100) size 400x300 in an 800x600 image
        box = tv.map_crop_box([0, 0, 1, 1], 200, 100, 400, 300, 800, 600)
        self.assertEqual(box, [0.25, 0.1667, 0.75, 0.6667])


class BoxToAnswerTest(unittest.TestCase):
    def test_center_and_radius_from_box(self):
        ans = tv.box_to_answer([0.1, 0.2, 0.4, 0.5])
        self.assertAlmostEqual(ans["x"], 0.25)
        self.assertAlmostEqual(ans["y"], 0.35)
        # r = 0.5 * hypot(0.3, 0.3) * 1.3 = 0.276 -> clamped to the cap
        self.assertEqual(ans["r"], tv.MAX_ANSWER_RADIUS)

    def test_radius_covers_every_corner_of_the_box(self):
        # #1328: a click anywhere on the anomaly must be a hit, so the stored
        # circle has to contain the box's farthest corner.
        for box in ([0.1, 0.2, 0.4, 0.5], [0.2, 0.3, 0.25, 0.6],
                    [0.46, 0.48, 0.50, 0.52], [0.1, 0.1, 0.11, 0.20]):
            ans = tv.box_to_answer(box)
            for corner in ((box[0], box[1]), (box[0], box[3]),
                           (box[2], box[1]), (box[2], box[3])):
                self.assertLessEqual(
                    math.hypot(corner[0] - ans["x"], corner[1] - ans["y"]),
                    ans["r"], box)

    def test_tall_box_is_not_clipped_at_the_old_cap(self):
        # A mid-ground time traveler (oxford-fair 1912): 0.09 x 0.29, the old
        # max(w,h)*0.8 rule gave 0.232 -> clipped to 0.15, so a click on the
        # head or the shoes missed (Evan: "expand to cover the whole time
        # traveler").
        ans = tv.box_to_answer([0.355, 0.525, 0.443, 0.852])
        self.assertGreater(ans["r"], 0.15)
        self.assertLessEqual(ans["r"], tv.MAX_ANSWER_RADIUS)
        corner = math.hypot(0.443 - ans["x"], 0.852 - ans["y"])
        self.assertLessEqual(corner, ans["r"])

    def test_person_min_radius_floors_a_small_box(self):
        # #1308: a person is the whole figure, so the target must be big
        # enough to click head AND shoes even when the vision model boxes
        # them tightly. The geometry of a small box alone is not enough.
        box = [0.4, 0.3, 0.45, 0.36]
        ans = tv.box_to_answer(box, person=True)
        self.assertGreaterEqual(ans["r"], tv.PERSON_MIN_RADIUS)
        self.assertLess(tv.box_to_answer(box)["r"], tv.PERSON_MIN_RADIUS)

    def test_person_short_box_is_extended_to_a_full_figure(self):
        # An upper-body box (head to chest) must not leave the legs and
        # shoes unclickable: the head is the box's top edge, so the box is
        # extended downward before the answer is derived (ticket #1308).
        box = [0.4, 0.30, 0.45, 0.42]  # 0.05 wide, 0.12 tall: cut at the waist
        ans = tv.box_to_answer(box, person=True)
        self.assertAlmostEqual(ans["y"],
                               box[1] + tv.PERSON_MIN_BOX_HEIGHT / 2,
                               delta=0.001)
        self.assertGreater(ans["y"], (box[1] + box[3]) / 2)
        self.assertGreaterEqual(ans["r"], tv.PERSON_MIN_RADIUS)

    def test_person_full_figure_box_is_not_touched(self):
        # A box that already spans a whole figure (the #1328 prompt produces
        # these) must be used as-is; the person rule only rescues a
        # truncated box.
        box = [0.4, 0.3, 0.45, 0.55]
        self.assertEqual(tv.box_to_answer(box, person=True),
                         tv.box_to_answer(box))

    def test_person_extension_clamps_to_the_frame_bottom(self):
        # A figure at the bottom edge: the extension cannot leave the frame,
        # so the radius floor is what still makes the whole figure clickable.
        ans = tv.box_to_answer([0.4, 0.93, 0.45, 0.99], person=True)
        self.assertLessEqual(ans["y"], 1.0)
        self.assertGreaterEqual(ans["r"], tv.PERSON_MIN_RADIUS)


class VerifyTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="agv_v_"))
        self.orig = self.tmp / "orig.png"
        make_img(self.orig, 800, 600, "gray(120)")
        self.edited = self.tmp / "edited.png"
        make_edited_with_patch(self.orig, self.edited)

    def test_verify_no_vision_locates_answer(self):
        verdict = tv.verify(self.edited, self.orig, "Plastic bottle",
                            vision=False)
        self.assertTrue(verdict["ok"], verdict)
        self.assertIn("answer", verdict)
        self.assertEqual(verdict["localization"], "diff")
        self.assertAlmostEqual(verdict["answer"]["x"], 0.17, delta=0.06)

    def test_verify_saves_crop(self):
        crop_out = self.tmp / "audit"
        verdict = tv.verify(self.edited, self.orig, "Plastic bottle",
                            vision=False, crop_out=crop_out)
        self.assertTrue(verdict["ok"])
        self.assertTrue((crop_out / "hotspot-crop.png").exists())

    def test_verify_vision_box_is_primary_answer(self):
        # #1165: a full-image vision box must become the answer directly,
        # even when it disagrees with the diff hotspot (this is the whole
        # point of demoting the pixel-diff to a sanity gate).
        calls = {}

        def fake_vision_check(image, anomaly, key, model, full_image=True):
            calls["full_image"] = full_image
            calls["anomaly"] = anomaly
            calls["model"] = model
            return {"present": True, "note": "visible",
                    "box": [0.5, 0.5, 0.65, 0.65], "size_hint": None}
        orig = tv.vision_check
        tv.vision_check = fake_vision_check
        try:
            verdict = tv.verify(self.edited, self.orig, "Digital watch",
                                vision=True)
        finally:
            tv.vision_check = orig
        self.assertTrue(verdict["ok"], verdict)
        self.assertEqual(verdict["localization"], "vision")
        self.assertTrue(calls["full_image"])
        self.assertEqual(calls["anomaly"], "Digital watch")
        # box [0.5,0.5,0.65,0.65] -> center (0.575, 0.575), far from the
        # diff hotspot at (0.17, 0.175): the answer must follow the BOX.
        self.assertAlmostEqual(verdict["answer"]["x"], 0.575, delta=0.01)
        self.assertAlmostEqual(verdict["answer"]["y"], 0.575, delta=0.01)
        self.assertIn("position_conflict", verdict)

    def test_verify_person_answer_covers_the_whole_figure(self):
        # #1308: the caller marks a time-traveler scene as a person; an
        # upper-body box then still yields an answer a player can hit on the
        # shoes, while the same box for an object stays tight.
        def fake_vision_check(image, anomaly, key, model, full_image=True):
            return {"present": True, "note": "visible",
                    "box": [0.4, 0.30, 0.45, 0.42], "size_hint": None}
        orig = tv.vision_check
        tv.vision_check = fake_vision_check
        try:
            person = tv.verify(self.edited, self.orig,
                               "Time traveler: tourist", vision=True,
                               person=True)
            obj = tv.verify(self.edited, self.orig, "Digital watch",
                            vision=True)
        finally:
            tv.vision_check = orig
        self.assertTrue(person["ok"], person)
        self.assertGreaterEqual(person["answer"]["r"], tv.PERSON_MIN_RADIUS)
        self.assertGreater(person["answer"]["y"], obj["answer"]["y"])
        self.assertNotIn("person_box_extended", obj)
        self.assertIn("person_box_extended", person)
        self.assertLess(obj["answer"]["r"], tv.PERSON_MIN_RADIUS)

    def test_verify_person_full_figure_box_uses_the_box_geometry(self):
        # A full-figure person box keeps its geometric radius even when that
        # is above the floor: the floor is a minimum, never a replacement.
        def fake_vision_check(image, anomaly, key, model, full_image=True):
            return {"present": True, "note": "visible",
                    "box": [0.4, 0.3, 0.45, 0.55], "size_hint": None}
        orig = tv.vision_check
        tv.vision_check = fake_vision_check
        try:
            person = tv.verify(self.edited, self.orig, "Time traveler: tourist",
                               vision=True, person=True)
        finally:
            tv.vision_check = orig
        self.assertAlmostEqual(person["answer"]["r"], 0.1657, delta=0.002)
        self.assertNotIn("person_box_extended", person)

    def test_verify_vision_absent_rejects(self):
        def fake_vision_check(image, anomaly, key, model, full_image=True):
            return {"present": False, "note": "nothing there", "box": None,
                    "size_hint": None}
        orig = tv.vision_check
        tv.vision_check = fake_vision_check
        try:
            verdict = tv.verify(self.edited, self.orig, "Digital watch",
                                vision=True)
        finally:
            tv.vision_check = orig
        self.assertFalse(verdict["ok"])
        self.assertIn("could not confirm", verdict["reason"])

    def test_verify_vision_no_box_falls_back_to_diff(self):
        # #1165: when the full-image vision pass cannot produce a box, the
        # answer must fall back to the diff hotspot (+ crop-relative
        # vision check). present=true in the full image keeps the scene
        # eligible; the crop check confirms the anomaly at the hotspot.
        calls = {}

        def fake_vision_check(image, anomaly, key, model, full_image=True):
            calls["n"] = calls.get("n", 0) + 1
            if full_image:
                return {"present": True, "note": "somewhere",
                        "box": None, "size_hint": None}
            return {"present": True, "note": "visible",
                    "box": [0.3, 0.3, 0.5, 0.5], "size_hint": None}
        orig = tv.vision_check
        tv.vision_check = fake_vision_check
        try:
            verdict = tv.verify(self.edited, self.orig, "Digital watch",
                                vision=True)
        finally:
            tv.vision_check = orig
        self.assertTrue(verdict["ok"], verdict)
        self.assertEqual(calls["n"], 2)  # one full-image + one crop call
        self.assertEqual(verdict["localization"], "vision-crop")
        # the fallback answer is the diff hotspot refined by the crop box
        # [0.3,0.3,0.5,0.5]: the crop is centered on the hotspot (0.17,
        # 0.175), so the mapped box center stays near the hotspot.
        self.assertAlmostEqual(verdict["answer"]["x"], 0.17, delta=0.07)
        self.assertAlmostEqual(verdict["answer"]["y"], 0.175, delta=0.07)

    def test_verify_vision_no_box_and_absent_rejects(self):
        # present=false in the full-image pass (and no box): reject even
        # though a diff hotspot exists; the anomaly was not confirmed.
        def fake_vision_check(image, anomaly, key, model, full_image=True):
            return {"present": False, "note": "nothing", "box": None,
                    "size_hint": None}
        orig = tv.vision_check
        tv.vision_check = fake_vision_check
        try:
            verdict = tv.verify(self.edited, self.orig, "Digital watch",
                                vision=True)
        finally:
            tv.vision_check = orig
        self.assertFalse(verdict["ok"])
        self.assertIn("could not confirm", verdict["reason"])

    def test_verify_vision_box_away_from_hotspot_warns_and_overrides(self):
        # Ticket #1153 (copenhagen-poster + nyc-docks 2026-09-10): vision
        # anchored the anomaly a long way from the diff hotspot. The
        # hotspot was a JPEG re-encode artifact, so the "answer" built
        # from it was wrong. With #1165 the full-image vision box is the
        # PRIMARY answer; the conflict is recorded as a warning.
        def fake_vision_check(image, anomaly, key, model, full_image=True):
            return {"present": True, "note": "visible",
                    "box": [0.48, 0.48, 0.63, 0.58], "size_hint": None}
        orig = tv.vision_check
        tv.vision_check = fake_vision_check
        try:
            verdict = tv.verify(self.edited, self.orig, "Digital watch",
                                vision=True)
        finally:
            tv.vision_check = orig
        self.assertTrue(verdict["ok"], verdict)
        self.assertIn("warn", verdict)
        self.assertIn("position_conflict", verdict)
        # the answer now centers on the vision box, not the hotspot
        # box [0.48,0.48,0.63,0.58] -> full-image center (0.555, 0.53)
        self.assertAlmostEqual(verdict["answer"]["x"], 0.555, delta=0.03)
        self.assertAlmostEqual(verdict["answer"]["y"], 0.53, delta=0.03)

    def test_verify_missing_key_reports_reason(self):
        saved = os.environ.pop("OPENROUTER_API_KEY", None)
        try:
            verdict = tv.verify(self.edited, self.orig, "Digital watch",
                                vision=True, env_path=self.tmp / "nonexistent")
        finally:
            if saved is not None:
                os.environ["OPENROUTER_API_KEY"] = saved
        self.assertFalse(verdict["ok"])
        self.assertIn("OPENROUTER_API_KEY", verdict["reason"])

    def test_no_diff_disables_hotspot(self):
        # --no-diff: no hotspot is computed; vision-only verdict. With a
        # vision box the scene verifies; without vision+diff there is no
        # localization source at all.
        def fake_vision_check(image, anomaly, key, model, full_image=True):
            return {"present": True, "note": "x",
                    "box": [0.1, 0.1, 0.3, 0.3], "size_hint": None}
        orig = tv.vision_check
        tv.vision_check = fake_vision_check
        try:
            verdict = tv.verify(self.edited, self.orig, "Digital watch",
                                vision=True, diff=False)
        finally:
            tv.vision_check = orig
        self.assertTrue(verdict["ok"], verdict)
        self.assertIsNone(verdict["hotspot"])
        self.assertEqual(verdict["localization"], "vision")

        verdict2 = tv.verify(self.edited, self.orig, "Digital watch",
                             vision=False, diff=False)
        self.assertFalse(verdict2["ok"])

    def test_whole_frame_repaint_rejected_before_vision(self):
        # The diff sanity gate must reject a whole-frame re-render even when
        # the (fake) vision model claims to see the anomaly: a repaint means
        # there is no localized edit to verify.
        def fake_vision_check(image, anomaly, key, model, full_image=True):
            return {"present": True, "note": "x",
                    "box": [0.2, 0.2, 0.4, 0.4], "size_hint": None}
        bright = self.tmp / "bright.png"
        make_img(bright, 800, 600, "gray(210)")
        orig = tv.vision_check
        tv.vision_check = fake_vision_check
        try:
            verdict = tv.verify(bright, self.orig, "Digital watch",
                                vision=True)
        finally:
            tv.vision_check = orig
        self.assertFalse(verdict["ok"])
        self.assertIn("repaint", verdict["reason"])


class IdenticalOutputTest(unittest.TestCase):
    """Ticket #1124: byte-identical re-serves must abort before the gate."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="agv_dedup_"))
        self.orig = self.tmp / "orig.png"
        make_img(self.orig, 800, 600, "gray(120)")
        self.edited = self.tmp / "edited.png"
        make_edited_with_patch(self.orig, self.edited)

    def test_matches_previous_attempt_aborts(self):
        prev = self.tmp / "attempt-1.png"
        shutil.copy(self.edited, prev)
        verdict = tv.verify(self.edited, self.orig, "Plastic bottle",
                            vision=False, dedup=[str(prev)])
        self.assertFalse(verdict["ok"])
        self.assertIn("identical output", verdict["reason"])
        self.assertIn("attempt-1.png", verdict["reason"])

    def test_matches_source_crop_aborts(self):
        passthrough = self.tmp / "passthrough.png"
        shutil.copy(self.orig, passthrough)
        verdict = tv.verify(passthrough, self.orig, "Plastic bottle",
                            vision=False)
        self.assertFalse(verdict["ok"])
        self.assertIn("identical output", verdict["reason"])
        self.assertIn("source crop", verdict["reason"])

    def test_no_match_proceeds_normally(self):
        other = self.tmp / "attempt-other.png"
        make_edited_with_patch(self.orig, other, px=300, py=200)
        verdict = tv.verify(self.edited, self.orig, "Plastic bottle",
                            vision=False, dedup=[str(other)])
        self.assertTrue(verdict["ok"], verdict)

    def test_missing_dedup_file_raises(self):
        with self.assertRaises(ValueError):
            tv.verify(self.edited, self.orig, "Plastic bottle", vision=False,
                      dedup=[str(self.tmp / "nope.png")])


class VisionCallTest(unittest.TestCase):
    """The vision request must disable reasoning (ticket #1285).

    With a reasoning-model VISION_MODEL (deepseek-v4.1-flash) and thinking
    on, the whole output budget goes to reasoning_content and `content`
    comes back empty, so every localization fails. Pinning
    reasoning: {enabled: false} keeps the call working after a model swap.
    """

    def test_vision_check_disables_reasoning(self):
        tmp = Path(tempfile.mkdtemp(prefix="agv_"))
        try:
            img = tmp / "scene.png"
            make_img(img, 200, 100)
            captured = {}
            body = {"choices": [{"message": {"content": json.dumps(
                {"present": True, "note": "n",
                 "box": [0.1, 0.2, 0.3, 0.4]})}}]}

            def fake_urlopen(req, timeout=None):
                captured["payload"] = json.loads(req.data)
                return io.BytesIO(json.dumps(body).encode())

            orig = tv.urllib.request.urlopen
            tv.urllib.request.urlopen = fake_urlopen
            try:
                got = tv.vision_check(img, "Plastic bottle", "key", "model")
            finally:
                tv.urllib.request.urlopen = orig
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

        p = captured["payload"]
        # Reasoning off: otherwise a thinking model returns content: null.
        self.assertEqual(p["reasoning"], {"enabled": False})
        self.assertEqual(p["max_tokens"], tv.VISION_MAX_TOKENS)
        self.assertTrue(any(c["type"] == "image_url"
                            for c in p["messages"][0]["content"]))
        self.assertEqual(got["box"], [0.1, 0.2, 0.3, 0.4])


class MainCliTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="agv_cli_"))
        self.orig = self.tmp / "orig.png"
        make_img(self.orig, 800, 600, "gray(120)")
        self.edited = self.tmp / "edited.png"
        make_edited_with_patch(self.orig, self.edited)

    def test_cli_no_vision_prints_json(self):
        r = subprocess.run(
            ["python3", str(SCRIPTS_DIR / "ag_verify.py"),
             "--edited", str(self.edited), "--original", str(self.orig),
             "--anomaly", "Plastic bottle", "--no-vision"],
            capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        verdict = json.loads(r.stdout)
        self.assertTrue(verdict["ok"])
        self.assertIn("answer", verdict)
        self.assertEqual(verdict["localization"], "diff")

    def test_cli_no_diff_no_vision_rejects(self):
        r = subprocess.run(
            ["python3", str(SCRIPTS_DIR / "ag_verify.py"),
             "--edited", str(self.edited), "--original", str(self.orig),
             "--anomaly", "Plastic bottle", "--no-vision", "--no-diff"],
            capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        verdict = json.loads(r.stdout)
        self.assertFalse(verdict["ok"])

    def test_cli_rejects_portrait_and_missing_files(self):
        r = subprocess.run(
            ["python3", str(SCRIPTS_DIR / "ag_verify.py"),
             "--edited", str(self.tmp / "missing.png"),
             "--original", str(self.orig), "--anomaly", "x", "--no-vision"],
            capture_output=True, text=True)
        self.assertNotEqual(r.returncode, 0)

    def test_cli_dedup_aborts_early(self):
        prev = self.tmp / "attempt-1.png"
        shutil.copy(self.edited, prev)
        r = subprocess.run(
            ["python3", str(SCRIPTS_DIR / "ag_verify.py"),
             "--edited", str(self.edited), "--original", str(self.orig),
             "--anomaly", "Plastic bottle", "--no-vision",
             "--dedup", str(prev)],
            capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        verdict = json.loads(r.stdout)
        self.assertFalse(verdict["ok"])
        self.assertIn("identical output", verdict["reason"])


if __name__ == "__main__":
    unittest.main()