"""Tests for pipeline/ag_checks.py (ticket #1485).

The three mechanical checks: presence (pure decision), tone (real images via
ImageMagick) and size (pure arithmetic). No network, no model calls.
"""

import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

import ag_checks as c


def make_img(path: Path, w=200, h=200, color="gray"):
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["convert", "-size", f"{w}x{h}", f"xc:{color}", str(path)],
                   check=True, capture_output=True)
    return path


def make_patch(path: Path, w=200, h=200, base="gray", patch="red",
               corners=(80, 80, 120, 120)):
    """A base-colored image with a solid rectangular patch, for the tone
    check. ``corners`` is (x1, y1, x2, y2) in pixels."""
    path.parent.mkdir(parents=True, exist_ok=True)
    x1, y1, x2, y2 = corners
    subprocess.run(["convert", "-size", f"{w}x{h}", f"xc:{base}",
                    "-fill", patch, "-draw",
                    f"rectangle {x1},{y1} {x2},{y2}",
                    str(path)], check=True, capture_output=True)
    return path


class TempDirMixin:
    def setUp(self):
        self._tmp = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self._tmp, ignore_errors=True)


class SaturationTest(TempDirMixin, unittest.TestCase):
    def test_mean_saturation_separates_gray_from_colour(self):
        gray = make_img(self._tmp / "gray.png", color="gray")
        red = make_img(self._tmp / "red.png", color="red")
        self.assertAlmostEqual(c.mean_saturation(gray), 0.0, places=3)
        self.assertAlmostEqual(c.mean_saturation(red), 1.0, places=3)

    def test_region_saturation_counts_colored_pixels(self):
        img = make_patch(self._tmp / "patch.png", patch="red")
        # The red square sits at x 0.4-0.6, y 0.4-0.6 in a 200x200 image.
        hit = c.region_saturation(img, (0.4, 0.4, 0.6, 0.6))
        miss = c.region_saturation(img, (0.0, 0.0, 0.2, 0.2))
        self.assertGreater(hit["colored_fraction"], 0.5)
        self.assertAlmostEqual(miss["colored_fraction"], 0.0, places=3)

    def test_region_saturation_survives_a_degenerate_region(self):
        img = make_img(self._tmp / "gray.png")
        out = c.region_saturation(img, (0.5, 0.5, 0.5001, 0.5001))
        self.assertTrue(out["empty"])
        self.assertEqual(out["colored_fraction"], 0.0)


class ToneTest(TempDirMixin, unittest.TestCase):
    def test_color_on_a_grayscale_source_is_flagged(self):
        source = make_img(self._tmp / "src.png", color="gray")
        edited = make_patch(self._tmp / "edit.png", patch="red")
        box = {"x1": 0.4, "y1": 0.4, "x2": 0.6, "y2": 0.6}
        out = c.tone_finding(source, edited, {"x": 0.5, "y": 0.5, "r": 0.1},
                             box)
        self.assertTrue(out["source_grayscale"])
        self.assertTrue(out["failed"])

    def test_gray_element_on_a_grayscale_source_passes(self):
        source = make_img(self._tmp / "src.png", color="gray")
        edited = make_patch(self._tmp / "edit.png", base="gray",
                            patch="#4d4d4d")
        out = c.tone_finding(source, edited, {"x": 0.5, "y": 0.5, "r": 0.1})
        self.assertTrue(out["source_grayscale"])
        self.assertFalse(out["failed"])

    def test_colored_source_skips_the_measurement(self):
        source = make_img(self._tmp / "src.png", color="red")
        edited = make_img(self._tmp / "edit.png", color="red")
        out = c.tone_finding(source, edited, {"x": 0.5, "y": 0.5, "r": 0.1})
        self.assertFalse(out["source_grayscale"])
        self.assertFalse(out["failed"])
        self.assertIsNone(out["edited"])

    def test_tight_box_prefers_the_element_area_over_the_ellipse(self):
        # A small colored element in a large answer ellipse: the ellipse
        # dilutes the color, the tight box does not.
        source = make_img(self._tmp / "src.png", color="gray")
        edited = make_patch(self._tmp / "edit.png", patch="red",
                            corners=(20, 20, 40, 40))
        answer = {"x": 0.9, "y": 0.9, "r": 0.3}
        without = c.tone_finding(source, edited, answer)
        with_box = c.tone_finding(
            source, edited, answer,
            {"x1": 0.09, "y1": 0.09, "x2": 0.11, "y2": 0.11})
        self.assertFalse(without["failed"])
        self.assertTrue(with_box["failed"])


class SizeTest(unittest.TestCase):
    def box(self, height, y=0.5):
        return {"x1": 0.4, "y1": y, "x2": 0.5, "y2": y + height}

    def test_an_object_in_the_band_passes(self):
        out = c.size_finding(self.box(0.025))
        self.assertTrue(out["checked"])
        self.assertEqual(out["verdict"], "ok")
        self.assertFalse(out["failed"])
        self.assertEqual(out["gate_band"], [0.015, 0.15])

    def test_a_tiny_object_is_flagged(self):
        out = c.size_finding(self.box(0.005))
        self.assertEqual(out["verdict"], "too_small")
        self.assertTrue(out["failed"])

    def test_a_prominent_object_is_flagged(self):
        out = c.size_finding(self.box(0.2))
        self.assertEqual(out["verdict"], "too_prominent")
        self.assertTrue(out["failed"])

    def test_a_generous_box_below_the_gate_is_not_flagged(self):
        # Measured accepted scenes sit at 0.04-0.10: above the prompt's 3 %
        # ceiling, below the calibrated gate.
        out = c.size_finding(self.box(0.10))
        self.assertEqual(out["verdict"], "ok")
        self.assertFalse(out["failed"])

    def test_a_person_uses_the_figure_band(self):
        self.assertEqual(c.size_finding(self.box(0.2), figure=True)["verdict"],
                         "ok")
        self.assertEqual(c.size_finding(self.box(0.02), figure=True)["verdict"],
                         "too_small")

    def test_without_a_box_the_size_is_unmeasured(self):
        out = c.size_finding(None)
        self.assertFalse(out["checked"])
        self.assertEqual(out["verdict"], "unmeasured")
        self.assertFalse(out["failed"])


class GeometryTest(unittest.TestCase):
    def test_answer_covers_every_corner(self):
        answer = {"x": 0.5, "y": 0.5, "r": 0.1}
        self.assertTrue(c.answer_covers_box(answer,
                                            {"x1": 0.49, "y1": 0.49,
                                             "x2": 0.51, "y2": 0.51}))
        self.assertFalse(c.answer_covers_box(answer,
                                             {"x1": 0.6, "y1": 0.6,
                                              "x2": 0.7, "y2": 0.7}))

    def test_box_center_inside_the_ellipse(self):
        answer = {"x": 0.5, "y": 0.5, "r": 0.1}
        self.assertTrue(c.box_center_inside(answer,
                                            {"x1": 0.48, "y1": 0.48,
                                             "x2": 0.52, "y2": 0.52}))
        self.assertFalse(c.box_center_inside(answer,
                                             {"x1": 0.7, "y1": 0.7,
                                              "x2": 0.8, "y2": 0.8}))


class PresenceTest(unittest.TestCase):
    answer = {"x": 0.5, "y": 0.5, "r": 0.1}

    def test_a_box_in_the_answer_area_is_ok(self):
        out = c.presence_finding(
            {"present": True, "box": {"x1": 0.48, "y1": 0.48, "x2": 0.52,
                                      "y2": 0.52}, "note": ""},
            self.answer)
        self.assertEqual(out["status"], "ok")
        self.assertFalse(out["failed"])

    def test_a_box_far_outside_is_flagged(self):
        # The "no such bottle in the click area" class.
        out = c.presence_finding(
            {"present": True, "box": {"x1": 0.8, "y1": 0.8, "x2": 0.9,
                                      "y2": 0.9}, "note": ""},
            self.answer)
        self.assertEqual(out["status"], "outside")
        self.assertTrue(out["failed"])

    def test_an_absent_element_is_flagged(self):
        out = c.presence_finding({"present": False, "box": None,
                                  "note": "no phone in this image"},
                                 self.answer)
        self.assertEqual(out["status"], "absent")
        self.assertTrue(out["failed"])

    def test_present_without_a_box_is_inconclusive(self):
        out = c.presence_finding({"present": True, "box": None, "note": ""},
                                 self.answer)
        self.assertEqual(out["status"], "unlocated")
        self.assertFalse(out["failed"])


class ReviewReasonsTest(unittest.TestCase):
    def test_a_clean_finding_set_has_no_reasons(self):
        findings = {"presence": {"failed": False}, "tone": {"failed": False},
                    "size": {"failed": False}}
        self.assertEqual(c.review_reasons(findings), [])

    def test_each_failed_check_names_itself(self):
        findings = {"presence": {"failed": True, "status": "absent"},
                    "tone": {"failed": True,
                             "edited": {"colored_fraction": 0.31}},
                    "size": {"failed": True, "verdict": "too_prominent",
                             "height_fraction": 0.42}}
        reasons = c.review_reasons(findings)
        self.assertEqual(len(reasons), 3)
        self.assertIn("not visible", reasons[0])
        self.assertIn("grayscale", reasons[1])
        self.assertIn("too_prominent", reasons[2])

    def test_a_presence_box_outside_names_its_own_class(self):
        findings = {"presence": {"failed": True, "status": "outside"}}
        self.assertIn("outside", c.review_reasons(findings)[0])


if __name__ == "__main__":
    unittest.main()
