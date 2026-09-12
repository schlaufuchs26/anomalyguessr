"""Tests for pipeline/ag_mask.py (ticket #1157).

Run with: python3 scripts/test_ag_mask.py

Synthetic images are generated with ImageMagick. The Gemini API is never
called: dry-run paths and a monkeypatched call_interactions cover the rest.
"""

import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import ag_mask as am

SCRIPTS_DIR = Path(__file__).resolve().parent


def make_img(path: Path, w=800, h=600, color="gray"):
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["convert", "-size", f"{w}x{h}", f"xc:{color}", str(path)],
        check=True, capture_output=True,
    )


class MaskGeometryTest(unittest.TestCase):
    def test_scale_box_normalized_to_pixels(self):
        p = am.scale_box((0.5, 0.5), (0.1, 0.2), 800, 600)
        self.assertEqual(p["cx"], 400.0)
        self.assertEqual(p["cy"], 300.0)
        self.assertEqual(p["rx"], 80.0)
        self.assertEqual(p["ry"], 120.0)

    def test_scale_box_clamps_center_inside(self):
        # Center near the edge plus a radius that would push the ellipse
        # outside: the center must be clamped so the ellipse fits.
        p = am.scale_box((0.01, 0.5), (0.1, 0.1), 800, 600)
        self.assertEqual(p["cx"], 80.0)  # = rx, ellipse touches the edge
        p2 = am.scale_box((0.99, 0.5), (0.1, 0.1), 800, 600)
        self.assertEqual(p2["cx"], 720.0)  # w - rx
        p3 = am.scale_box((0.5, 0.02), (0.05, 0.2), 800, 600)
        self.assertEqual(p3["cy"], 120.0)  # = ry

    def test_scale_box_rejects_oversized_radii(self):
        with self.assertRaises(ValueError):
            am.scale_box((0.5, 0.5), (0.6, 0.1), 800, 600)

    def test_build_mask_ellipse_geometry(self):
        with tempfile.TemporaryDirectory() as td:
            src = Path(td) / "src.jpg"
            make_img(src, 800, 600)
            mask = Path(td) / "mask.png"
            p = am.build_mask(src, (0.5, 0.5), (0.1, 0.1), mask)
            self.assertTrue(mask.exists())
            w, h = am.image_dims(mask)
            self.assertEqual((w, h), (800, 600))
            # bbox of the white (edit) region should be an ellipse,
            # approximately centered with rx ~80 px. Threshold + trim.
            r = subprocess.run(
                ["convert", str(mask), "-threshold", "50%", "-trim",
                 "-format", "%wx%h%O", "info:-"],
                capture_output=True, text=True, check=True,
            ).stdout.strip()
            import re
            m = re.match(r"^(\d+)x(\d+)([+-]\d+)([+-]\d+)$", r)
            self.assertIsNotNone(m, f"unparsed bbox: {r!r}")
            bw, bh, bx, by = map(int, m.groups())
            self.assertAlmostEqual(bx + bw / 2, 400, delta=6)
            self.assertAlmostEqual(by + bh / 2, 300, delta=6)
            # blur spreads ~half the 64px blur inward; accept 40..90
            self.assertGreaterEqual(bw, 80)
            self.assertLessEqual(bw, 170)
            self.assertGreaterEqual(bh, 80)
            self.assertLessEqual(bh, 170)

    def test_mask_answer_normalized_and_radius(self):
        ans = am.mask_answer({"cx": 400, "cy": 300, "rx": 80, "ry": 120},
                             800, 600)
        self.assertEqual(ans["x"], 0.5)
        self.assertEqual(ans["y"], 0.5)
        # r = smaller half-extent / larger frame side, clamped to >= 0.02
        self.assertAlmostEqual(ans["r"], 80 / 800, places=3)
        ans_small = am.mask_answer({"cx": 0, "cy": 0, "rx": 1, "ry": 1},
                                   800, 600)
        self.assertGreaterEqual(ans_small["r"], 0.02)


class MaskFileTest(unittest.TestCase):
    def test_mask_bbox_parses(self):
        with tempfile.TemporaryDirectory() as td:
            src = Path(td) / "src.jpg"
            make_img(src, 800, 600)
            mask = Path(td) / "m.png"
            am.build_mask(src, (0.5, 0.5), (0.1, 0.05), mask)
            p = am.mask_bbox(mask)
            self.assertAlmostEqual(p["cx"], 400, delta=8)
            self.assertAlmostEqual(p["cy"], 300, delta=8)
            self.assertAlmostEqual(p["rx"], 80, delta=15)
            self.assertAlmostEqual(p["ry"], 30, delta=15)

    def test_maybe_downscale_only_huge(self):
        with tempfile.TemporaryDirectory() as td:
            src = Path(td) / "big.jpg"
            make_img(src, 2000, 1000)
            out = am.maybe_downscale(src, (1300, 900), Path(td))
            self.assertNotEqual(out, src)
            w, h = am.image_dims(out)
            self.assertLessEqual(w, 1300)
            self.assertLessEqual(h, 900)
            small = Path(td) / "small.jpg"
            make_img(small, 800, 600)
            self.assertEqual(am.maybe_downscale(small, (1300, 900), Path(td)),
                             small)


class RunDryRunTest(unittest.TestCase):
    def test_dry_run_writes_mask_and_answer_no_api(self):
        with tempfile.TemporaryDirectory() as td:
            src = Path(td) / "src.jpg"
            make_img(src, 800, 600)
            args = am.parse_args([
                "--source", str(src), "--center", "0.7,0.6",
                "--radius", "0.09", "--prompt", "edit it",
                "--out-dir", td, "--out", "scene1", "--dry-run",
            ])
            rc = am.run(args)
            self.assertEqual(rc, 0, "dry-run must succeed")
            ans = json.loads((Path(td) / "scene1-answer.json").read_text())
            mask = Path(td) / "scene1-mask.png"
            self.assertTrue(mask.exists())
            self.assertAlmostEqual(ans["x"], 0.7, places=2)
            self.assertAlmostEqual(ans["y"], 0.6, places=2)
            self.assertGreater(ans["r"], 0)

    def test_run_with_reused_mask_derives_answer_from_bbox(self):
        with tempfile.TemporaryDirectory() as td:
            src = Path(td) / "src.jpg"
            make_img(src, 800, 600)
            mask = Path(td) / "m.png"
            am.build_mask(src, (0.25, 0.5), (0.1, 0.04), mask)
            args = am.parse_args([
                "--source", str(src), "--mask", str(mask),
                "--prompt", "edit it", "--out-dir", td, "--out", "scene2",
                "--dry-run",
            ])
            rc = am.run(args)
            self.assertEqual(rc, 0)
            ans = json.loads((Path(td) / "scene2-answer.json").read_text())
            self.assertAlmostEqual(ans["x"], 0.25, delta=0.03)
            self.assertAlmostEqual(ans["y"], 0.5, delta=0.05)

    def test_errors(self):
        with tempfile.TemporaryDirectory() as td:
            missing = Path(td) / "nope.jpg"
            with self.assertRaises(ValueError):
                am.run(am.parse_args([
                    "--source", str(missing), "--center", "0.5,0.5",
                    "--radius", "0.1", "--prompt", "edit",
                    "--out-dir", td, "--dry-run"]))
            src = Path(td) / "src.jpg"
            make_img(src, 800, 600)
            with self.assertRaises(ValueError):
                # no center, no mask
                am.run(am.parse_args([
                    "--source", str(src), "--prompt", "edit",
                    "--out-dir", td, "--dry-run"]))
            with self.assertRaises(ValueError):
                # radius > 0.5
                am.run(am.parse_args([
                    "--source", str(src), "--center", "0.5,0.5",
                    "--radius", "0.9", "--prompt", "edit",
                    "--out-dir", td, "--dry-run"]))

    def test_real_call_writes_image(self):
        """Monkeypatched API: run() must write the returned bytes + answer."""
        with tempfile.TemporaryDirectory() as td:
            src = Path(td) / "src.jpg"
            make_img(src, 800, 600)
            original = am.call_interactions

            def fake(key, model, prompt, src_b64, mime, mask_b64, size,
                     timeout):
                return b"\xff\xd8\xff\xe0fakejpegdata"

            am.call_interactions = fake
            try:
                args = am.parse_args([
                    "--source", str(src), "--center", "0.5,0.5",
                    "--radius", "0.1", "--prompt", "edit it",
                    "--out-dir", td, "--out", "scene3",
                ])
                os.environ["GEMINI_API_KEY"] = "test-key"
                rc = am.run(args)
                self.assertEqual(rc, 0)
                edited = Path(td) / "scene3.jpg"
                self.assertTrue(edited.exists())
                self.assertEqual(edited.read_bytes(),
                                 b"\xff\xd8\xff\xe0fakejpegdata")
                ans = json.loads(
                    (Path(td) / "scene3-answer.json").read_text())
                self.assertEqual(ans["x"], 0.5)
            finally:
                am.call_interactions = original


if __name__ == "__main__":
    unittest.main()