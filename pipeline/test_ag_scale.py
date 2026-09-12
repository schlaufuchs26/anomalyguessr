"""Tests for pipeline/ag_scale.py (ticket #1114).

Run with: python3 scripts/test_ag_scale.py

Synthetic ImageMagick images only; no network, no LLM. The scenarios mirror
the pipeline reality the script was built for: a generated scene whose
anomaly is localized but rendered far too large (cans/phones), where the
rest of the frame stayed faithful to the original.
"""

import json
import subprocess
import tempfile
import unittest
from pathlib import Path

import ag_scale as ts

SCRIPTS_DIR = Path(__file__).resolve().parent


def make_img(path: Path, w=800, h=600, color="gray(120)"):
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["convert", "-size", f"{w}x{h}", f"xc:{color}",
                    str(path)], check=True, capture_output=True)


def make_bg(path: Path, w=800, h=600):
    """Original with a little structure so the diff is not trivial."""
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["convert", "-size", f"{w}x{h}", "gradient:gray(60)-gray(160)",
         str(path)], check=True, capture_output=True)
    # Draw in a separate step: -draw in the same convert chain adds an alpha
    # channel, which breaks ag_verify's txt diff parsing (graya vs gray).
    subprocess.run(
        ["convert", str(path), "-fill", "gray(90)",
         "-draw", "rectangle 0,520 799,599", "-alpha", "off", str(path)],
        check=True, capture_output=True)


def make_edited_with_object(orig: Path, edited: Path, px, py, pw, ph,
                            patch="red"):
    """Copy orig to edited, then draw a bright patch (the too-big anomaly).

    -alpha off keeps the result free of the alpha channel IM7's -draw adds
    for sRGB fills; an alpha channel breaks ag_verify's txt diff parsing.
    """
    subprocess.run(["convert", str(orig), "-fill", patch, "-draw",
                    f"rectangle {px},{py} {px + pw},{py + ph}", "-alpha",
                    "off", str(edited)], check=True, capture_output=True)


class ScaleCorrectTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="ags_"))
        self.orig = self.tmp / "orig.png"
        make_bg(self.orig)

    def test_shrinks_localized_object_to_target(self):
        # "Can" rendered 200 px tall on a 600 px image (~33%): way too big.
        edited = self.tmp / "edited.png"
        make_edited_with_object(self.orig, edited, px=370, py=280, pw=60,
                                ph=200)
        out = self.tmp / "corrected.png"
        verdict = ts.scale_correct(edited, self.orig, target_frac=0.05,
                                   out_path=out)
        self.assertTrue(verdict["ok"], verdict)
        self.assertTrue(out.exists())
        # target 5% of 600 = 30 px; allow grid/mask tolerance.
        self.assertLessEqual(verdict["after_h_frac"], 0.07)
        self.assertLess(verdict["after_h_frac"], verdict["before_h_frac"])
        # Background restored: the corrected image differs from the original
        # only in a small region (the shrunk object), not the old 60x200 box.
        r = subprocess.run(
            ["convert", str(out), str(self.orig), "-compose", "difference",
             "-composite", "-colorspace", "Gray", "-threshold", "8%",
             "-bordercolor", "black", "-border", "1", "-trim", "info:"],
            capture_output=True, text=True)
        tok = r.stdout.split()
        bw, bh = map(int, tok[2].split("x"))
        self.assertLessEqual(bw, 40)
        self.assertLessEqual(bh, 45)

    def test_already_small_is_noop(self):
        edited = self.tmp / "edited.png"
        # ~13 px tall on a 600 px image is already within the 2% target
        # (and still large enough for the diff hotspot to find it).
        make_edited_with_object(self.orig, edited, px=370, py=280, pw=40,
                                ph=13)
        verdict = ts.scale_correct(edited, self.orig, target_frac=0.02)
        self.assertFalse(verdict["ok"])
        self.assertIn("already near target", verdict["reason"])

    def test_no_change_rejected(self):
        verdict = ts.scale_correct(self.orig, self.orig, target_frac=0.02)
        self.assertFalse(verdict["ok"])

    def test_whole_frame_repaint_rejected(self):
        edited = self.tmp / "bright.png"
        make_img(edited)  # uniform gray(120) -> uniform gray(200) repaint
        subprocess.run(["convert", "-size", "800x600", "xc:gray(200)",
                        str(edited)], check=True)
        verdict = ts.scale_correct(edited, self.orig, target_frac=0.02)
        self.assertFalse(verdict["ok"])
        # The rejected edited file must not be overwritten/removed.
        self.assertTrue(edited.exists())

    def test_resized_original_aligns(self):
        # Edited at a different size than the original (generator rescales).
        small = self.tmp / "small.png"
        make_bg(small, 640, 480)
        edited640 = self.tmp / "edited640.png"
        make_edited_with_object(small, edited640, px=300, py=220, pw=48,
                                ph=160)
        edited = self.tmp / "edited.png"
        subprocess.run(["convert", str(edited640), "-resize", "800x600!",
                        str(edited)], check=True)
        out = self.tmp / "corrected.png"
        verdict = ts.scale_correct(edited, small, target_frac=0.05,
                                   out_path=out)
        self.assertTrue(verdict["ok"], verdict)
        self.assertTrue(out.exists())


class MainCliTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="ags_cli_"))
        self.orig = self.tmp / "orig.png"
        make_bg(self.orig)
        self.edited = self.tmp / "edited.png"
        make_edited_with_object(self.orig, self.edited, px=370, py=280,
                                pw=60, ph=200)

    def test_cli_prints_json(self):
        r = subprocess.run(
            ["python3", str(SCRIPTS_DIR / "ag_scale.py"),
             "--edited", str(self.edited), "--original", str(self.orig),
             "--target-frac", "0.05", "--out", str(self.tmp / "out.png")],
            capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        verdict = json.loads(r.stdout)
        self.assertTrue(verdict["ok"], verdict)
        self.assertIn("before_h_frac", verdict)
        self.assertIn("after_h_frac", verdict)

    def test_cli_missing_files_nonzero(self):
        r = subprocess.run(
            ["python3", str(SCRIPTS_DIR / "ag_scale.py"),
             "--edited", str(self.tmp / "missing.png"),
             "--original", str(self.orig), "--target-frac", "0.05"],
            capture_output=True, text=True)
        self.assertNotEqual(r.returncode, 0)


if __name__ == "__main__":
    unittest.main()
