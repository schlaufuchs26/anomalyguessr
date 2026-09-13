"""Tests for pipeline/ag_readd.py (ticket #1444).

Synthetic ImageMagick images only; no network, no LLM. The scenarios mirror
the measurement's two branches: the initial localized edit is the baseline,
a correctable re-add stays localized, and a drifting one repaints the frame.
"""

import subprocess
import tempfile
import unittest
from pathlib import Path

import ag_generate
import ag_readd


def _convert(*args):
    subprocess.run(["convert", *args], check=True, capture_output=True)


def make_bg(path: Path, w=800, h=600):
    """Original with a little structure so the diff is not trivial."""
    path.parent.mkdir(parents=True, exist_ok=True)
    _convert("-size", f"{w}x{h}", "gradient:gray(60)-gray(160)", str(path))
    # -alpha off: IM7's -draw adds an alpha channel, which breaks
    # ag_verify's txt diff parsing.
    _convert(str(path), "-fill", "gray(90)", "-draw",
             "rectangle 0,520 799,599", "-alpha", "off", str(path))


def make_patch(orig: Path, out: Path, px, py, pw, ph, patch="red"):
    _convert(str(orig), "-fill", patch, "-draw",
             f"rectangle {px},{py} {px + pw},{py + ph}", "-alpha", "off",
             str(out))


def make_solid(path: Path, color, w=800, h=600):
    _convert("-size", f"{w}x{h}", f"xc:{color}", str(path))


class ReaddPromptTest(unittest.TestCase):
    def proposal(self):
        return {"anomaly": "Clear plastic water bottle",
                "placement": "at the base of the leftmost tree trunk",
                "figure": False}

    def test_prompt_reissues_the_add_instruction(self):
        p = ag_readd.readd_prompt(self.proposal(), [])
        self.assertIn("add ONE Clear plastic water bottle", p)
        self.assertIn("at the base of the leftmost tree trunk", p)
        self.assertIn(ag_generate.PURPOSE, p)
        self.assertIn("CRITICAL SCALE", p)
        self.assertIn(ag_generate.KEEP, p)
        self.assertIn(ag_generate.BLEND, p)

    def test_prompt_folds_in_every_fix(self):
        p = ag_readd.readd_prompt(self.proposal(),
                                  ["make it grayscale", "hide it behind the trunk"])
        self.assertIn("make it grayscale", p)
        self.assertIn("hide it behind the trunk", p)
        # The corrections come before the hard constraints, so the numeric
        # scale cap and keep-the-rest stay the last word.
        self.assertLess(p.index("make it grayscale"), p.index("CRITICAL SCALE"))
        self.assertLess(p.index("hide it behind the trunk"),
                        p.index(ag_generate.KEEP))

    def test_no_fixes_means_no_correction_block(self):
        p = ag_readd.readd_prompt(self.proposal(), [])
        self.assertNotIn("corrections from an earlier attempt", p)

    def test_blank_fixes_are_dropped(self):
        p = ag_readd.readd_prompt(self.proposal(), ["", "   "])
        self.assertNotIn("corrections from an earlier attempt", p)


class FrameDriftTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="agr_"))
        self.orig = self.tmp / "orig.png"
        make_bg(self.orig)

    def test_identical_images_have_no_drift(self):
        d = ag_readd.frame_drift(self.orig, self.orig)
        self.assertEqual(d["changed_frac"], 0.0)
        self.assertEqual(d["mean"], 0.0)
        self.assertEqual(d["rest_mean"], 0.0)

    def test_localized_patch_keeps_the_rest_clean(self):
        edited = self.tmp / "edited.png"
        make_patch(self.orig, edited, px=370, py=280, pw=40, ph=60)
        d = ag_readd.frame_drift(edited, self.orig)
        self.assertGreater(d["cells"], 0)
        # One small object is a small changed fraction, well under the
        # whole-frame-repaint gate of ticket #1122 (15%).
        self.assertLess(d["changed_frac"], 0.05)
        # The patch is the largest cluster, so the rest of the grid is clean.
        self.assertLess(d["rest_mean"], 3.0)

    def test_full_repaint_is_flagged(self):
        repaint = self.tmp / "repaint.png"
        make_solid(repaint, "gray(200)")
        d = ag_readd.frame_drift(repaint, self.orig)
        self.assertGreater(d["changed_frac"], 0.15)
        self.assertGreater(d["rest_mean"], 30.0)


class CompareBranchesTest(unittest.TestCase):
    def scene(self, a_score, b_score, a_changed=0.02, b_changed=0.01,
              a_rest=1.0, b_rest=0.5, rounds=2, stopped=None):
        def branch(score, changed, rest):
            return {"check": {"score": score},
                    "drift_vs_source": {"changed_frac": changed,
                                        "rest_mean": rest}}
        return {"rounds": rounds, "stopped": stopped,
                "cumulative": branch(a_score, a_changed, a_rest),
                "readd": branch(b_score, b_changed, b_rest)}

    def test_counts_wins_and_means(self):
        scenes = [self.scene(6, 7), self.scene(7, 7), self.scene(8, 6)]
        s = ag_readd.compare_branches(scenes)
        self.assertEqual(s["compared"], 3)
        self.assertEqual(s["wins"], {"readd": 1, "cumulative": 1, "tie": 1})
        self.assertEqual(s["final_score"]["readd"], round(20 / 3, 4))
        self.assertEqual(s["final_score"]["cumulative"], round(21 / 3, 4))
        self.assertEqual(s["drift_vs_source_changed_frac"]["readd"], 0.01)
        self.assertEqual(s["drift_vs_source_rest_mean"]["cumulative"], 1.0)

    def test_scenes_without_rounds_are_ignored(self):
        s = ag_readd.compare_branches([self.scene(6, 7, rounds=0),
                                       self.scene(6, 7, stopped="boom")])
        self.assertEqual(s["compared"], 0)
        self.assertIsNone(s["final_score"]["readd"])

    def test_unscored_checker_is_skipped(self):
        s = ag_readd.compare_branches([self.scene(None, 7)])
        self.assertEqual(s["compared"], 1)
        self.assertEqual(s["wins"], {"readd": 0, "cumulative": 0, "tie": 0})
        self.assertIsNone(s["final_score"]["readd"])


if __name__ == "__main__":
    unittest.main()
