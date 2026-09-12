#!/usr/bin/env python3
"""AnomalyGuessr post-process scale correction (ticket #1114).

The image model reliably renders small modern objects (drink cans, phones)
too large even with scale-first prompting. This script is the deterministic
backstop: when a generated scene's anomaly is localized but clearly too big
for its real size, it shrinks the object region to a concrete pixel budget
and re-composites it onto the restored original background.

Mechanics (all ImageMagick, no LLM):

1. **Localize**: reuse ag_verify's aligned pixel-diff hotspot (the change
   cluster). Only localized edits are correctable; whole-frame repaints are
   rejected.
2. **Mask**: inside an inflated window around the hotspot, threshold the
   full-resolution difference vs. the original, close holes, erode the
   fringe. The white region = object (+ its shadow/halo).
3. **Restore**: composite the *original* back over the mask area (dilated +
   feathered) so the too-big object and its shadow disappear and the true
   background returns.
4. **Shrink**: cut the object out of the edited image (mask as alpha), resize
   it so its height matches the requested fraction of the image height, and
   paste it back anchored at the same bottom (ground-contact) point.
5. **Re-measure**: diff the corrected image against the original again and
   report the new object height fraction; ok=false when the correction did
   not land near the target (e.g. a shadow ghost remained).

Why this is safe for the AnomalyGuessr pool: the generator is instructed to keep
everything else pixel-faithful, so the original is the ground truth for the
background under the object. The pasted object is small (default target 2% of
image height), which hides residual edge artifacts; Evan's eye remains the
final judge, and scenes are still run through ag_verify afterwards.

Known limitation: the object's contact shadow is removed together with the
object (the original has no shadow there) and not re-drawn at the smaller
size; at a few percent of the frame on grainy historical photos this is
usually unnoticeable, but it is why blend quality is judged after the fact.

CLI::

    ag_scale.py --edited E.jpg --original O.jpg [--target-frac 0.02] \\
        [--min-px 10] [--out corrected.png] [--json out.json]

Exit code 0 always (JSON explains); non-zero = tooling error (retryable).
"""

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import ag_verify as tv  # image_dims + locate_hotspot + constants

# 8-bit threshold bounds for the full-res change mask.
T_MIN = 8       # below this: JPEG noise
T_MAX = 60      # above this: only the object core (risk: losing soft edges)
T_FRAC = 0.25   # adaptive: fraction of the window's max diff
# A coarse hotspot broader than this (fraction of the frame) cannot be a
# single localized object; scale-correction needs a localized edit.
MAX_MASK_AREA = 0.25  # eroded change mask > 25% of the frame = too broad
# Skip when the object is already within this factor of the target height.
NOOP_FACTOR = 0.9
# Never shrink below this factor in one pass (guard against absurd targets).
MIN_FACTOR = 0.06


def convert(args, text=False):
    r = subprocess.run(["convert"] + args, capture_output=True, text=text)
    if r.returncode != 0:
        raise RuntimeError(f"convert ... failed: {r.stderr[:300]}")
    return r


def window_masks(win_ed: Path, win_rs: Path, tmp: Path):
    """Change mask inside a pre-cropped window (both images same size).

    Returns {"eroded", "restore", "bbox"} where eroded/restore are window-
    sized mask paths and bbox = (x1, y1, x2, y2) of the eroded mask, relative
    to the window (offset 0). Returns None when nothing meaningful changed.
    """
    diff8 = tmp / "diff8.png"
    convert([str(win_ed), str(win_rs), "-compose", "difference", "-composite",
             "-colorspace", "Gray", "-depth", "8", str(diff8)])
    mx = float(convert([str(diff8), "-format", "%[fx:maxima]", "info:"],
                       text=True).stdout.strip() or 0)
    if mx <= 0:
        return None
    t = max(T_MIN, min(T_MAX, T_FRAC * mx * 255))
    raw = tmp / "mask_raw.png"
    convert([str(diff8), "-threshold", f"{t / 255 * 100:.1f}%", str(raw)])
    closed = tmp / "mask_closed.png"
    convert([str(raw), "-morphology", "Close", "Diamond:2", str(closed)])
    eroded = tmp / "mask_eroded.png"
    convert([str(closed), "-morphology", "Erode", "Diamond:1", str(eroded)])
    # Tight bbox of the eroded mask via -trim (1px black border for safety).
    # identify output: "NAME PNG <pageWxH> <canvasWxH+offX+offY> depth..."
    bordered = tmp / "mask_bordered.png"
    convert([str(eroded), "-bordercolor", "black", "-border", "1",
             str(bordered)])
    geom = convert([str(bordered), "-trim", "info:"], text=True).stdout
    try:
        tok = geom.split()
        bw, bh = map(int, tok[2].split("x"))
        _, _, bx, by = map(int, tok[3].replace("x", "+").split("+"))
    except (ValueError, IndexError):
        return None
    bx -= 1
    by -= 1
    if bw < 2 or bh < 2:
        return None
    restore = tmp / "mask_restore.png"
    convert([str(closed), "-morphology", "Dilate", "Diamond:2", "-blur",
             "0x1", str(restore)])
    mean = float(convert([str(eroded), "-format", "%[fx:mean]", "info:"],
                         text=True).stdout.strip() or 0)
    return {"eroded": eroded, "restore": restore,
            "bbox": (bx, by, bx + bw, by + bh), "mean": mean}


def crop_window(img: Path, x1: int, y1: int, x2: int, y2: int, dest: Path):
    convert([str(img), "-crop", f"{x2 - x1}x{y2 - y1}+{x1}+{y1}", "+repage",
             str(dest)])


def scale_correct(edited: Path, original: Path, target_frac: float = 0.02,
                  min_px: int = 10, out_path: Path = None) -> dict:
    """Correct the anomaly's scale. Returns a JSON-able verdict dict."""
    edited = Path(edited)
    original = Path(original)
    W, H = tv.image_dims(edited)
    target_frac = float(target_frac)
    tmpdir = Path(tempfile.mkdtemp(prefix="ags_"))
    try:
        resized = original
        if tv.image_dims(original) != (W, H):
            resized = tmpdir / "original-resized.png"
            subprocess.run(["convert", str(original), "-resize",
                            f"{W}x{H}!", str(resized)], check=True,
                           capture_output=True)

        # 1. Coarse localization (ag_verify grid) bounds the full-res work.
        loc = tv.locate_hotspot(edited, original)
        if not loc["ok"]:
            return {"ok": False, "reason": loc.get("reason",
                    "no change hotspot found")}
        top = loc["hotspot"]
        pad_x = int((top["x2"] - top["x1"]) * W * 0.4) + 4
        pad_y = int((top["y2"] - top["y1"]) * H * 0.4) + 4
        wx1 = max(0, int(top["x1"] * W) - pad_x)
        wy1 = max(0, int(top["y1"] * H) - pad_y)
        wx2 = min(W, int(top["x2"] * W) + pad_x)
        wy2 = min(H, int(top["y2"] * H) + pad_y)

        # 2. Full-res mask inside the window.
        win_ed = tmpdir / "win_ed.png"
        win_rs = tmpdir / "win_rs.png"
        crop_window(edited, wx1, wy1, wx2, wy2, win_ed)
        crop_window(resized, wx1, wy1, wx2, wy2, win_rs)
        m = window_masks(win_ed, win_rs, tmpdir)
        if m is None:
            return {"ok": False, "reason": "no change mask inside the "
                    "hotspot window"}
        mask_area = m["mean"] * (wx2 - wx1) * (wy2 - wy1) / (W * H)
        if mask_area > MAX_MASK_AREA:
            return {"ok": False,
                    "reason": f"change region too broad to isolate one object "
                              f"(change mask covers {mask_area:.0%} of the "
                              f"frame); scale-correction needs a localized "
                              f"edit"}
        bx1, by1, bx2, by2 = m["bbox"]
        # Absolute tight bbox of the object.
        ax1, ay1 = wx1 + bx1, wy1 + by1
        ax2, ay2 = wx1 + bx2, wy1 + by2
        cur_h = ay2 - ay1
        cur_frac = cur_h / H
        target_h = max(target_frac * H, min_px)
        factor = target_h / cur_h
        if factor >= NOOP_FACTOR:
            return {"ok": False,
                    "reason": f"object already near target size "
                              f"({cur_frac:.2%} of image height vs target "
                              f"{target_frac:.2%}); no correction needed",
                    "before_h_frac": round(cur_frac, 4),
                    "target_frac": target_frac}
        factor = max(MIN_FACTOR, factor)

        # 3. Restore the original background where the big object was.
        win_restored = tmpdir / "win_restored.png"
        convert([str(win_ed), str(win_rs), str(m["restore"]), "-composite",
                 str(win_restored)])
        restored_full = tmpdir / "restored_full.png"
        convert([str(edited), str(win_restored), "-geometry",
                 f"+{wx1}+{wy1}", "-composite", str(restored_full)])

        # 4. Cutout: edited window + eroded mask as alpha, cropped to bbox.
        obj = tmpdir / "obj.png"
        convert([str(win_ed), "(", str(m["eroded"]), "-blur", "0x1", ")",
                 "-alpha", "off", "-compose", "CopyOpacity", "-composite",
                 "-crop", f"{bx2 - bx1}x{by2 - by1}+{bx1}+{by1}", "+repage",
                 str(obj)])
        new_w = max(1, round((bx2 - bx1) * factor))
        new_h = max(1, round(cur_h * factor))
        obj_small = tmpdir / "obj_small.png"
        convert([str(obj), "-resize", f"{new_w}x{new_h}!", str(obj_small)])

        # 5. Paste anchored at the same bottom-center (ground contact).
        dx = round((ax1 + ax2) / 2 - new_w / 2)
        dy = ay2 - new_h
        final = Path(out_path) if out_path is not None else \
            edited.with_name(f"{edited.stem}-scaled{edited.suffix or '.png'}")
        convert([str(restored_full), str(obj_small), "-geometry",
                 f"+{dx}+{dy}", "-composite", str(final)])

        # 6. Re-measure: how big is the remaining anomaly now?
        after = None
        if final.suffix.lower() in (".jpg", ".jpeg"):
            # JPEG re-encode shifts pixels; measure before re-saving is not
            # possible, so measure the PNG composite instead: rebuild same
            # geometry into a temp png.
            meas = tmpdir / "measure.png"
            convert([str(restored_full), str(obj_small), "-geometry",
                     f"+{dx}+{dy}", "-composite", str(meas)])
            after = measure_object(meas, resized, tmpdir)
        else:
            after = measure_object(final, resized, tmpdir)
        after_h_frac = None
        if after is not None:
            _, ay1a, _, ay2a = after
            after_h_frac = (ay2a - ay1a) / H
        ok = after_h_frac is not None and after_h_frac <= max(
            target_frac + 0.008, target_frac * 1.3)
        verdict = {
            "ok": ok,
            "out": str(final),
            "factor": round(factor, 3),
            "target_frac": target_frac,
            "before_h_frac": round(cur_frac, 4),
        }
        if after_h_frac is not None:
            verdict["after_h_frac"] = round(after_h_frac, 4)
        if not ok:
            verdict["reason"] = (
                "correction did not land near the target size"
                + (f" (after={after_h_frac:.2%} of image height)"
                   if after_h_frac is not None
                   else "; no remaining change hotspot found"))
        return verdict
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def measure_object(edited: Path, resized: Path, tmp: Path):
    """Tight bbox (ax1, ay1, ax2, ay2) in full-image px of the anomaly.

    Reuses the coarse hotspot to bound the full-res mask work. None when no
    localized change is found.
    """
    loc = tv.locate_hotspot(edited, resized)
    if not loc["ok"]:
        return None
    top = loc["hotspot"]
    W, H = tv.image_dims(edited)
    pad_x = int((top["x2"] - top["x1"]) * W * 0.4) + 4
    pad_y = int((top["y2"] - top["y1"]) * H * 0.4) + 4
    wx1 = max(0, int(top["x1"] * W) - pad_x)
    wy1 = max(0, int(top["y1"] * H) - pad_y)
    wx2 = min(W, int(top["x2"] * W) + pad_x)
    wy2 = min(H, int(top["y2"] * H) + pad_y)
    win_ed = tmp / "meas_ed.png"
    win_rs = tmp / "meas_rs.png"
    crop_window(edited, wx1, wy1, wx2, wy2, win_ed)
    crop_window(resized, wx1, wy1, wx2, wy2, win_rs)
    m = window_masks(win_ed, win_rs, tmp)
    if m is None:
        return None
    bx1, by1, bx2, by2 = m["bbox"]
    return (wx1 + bx1, wy1 + by1, wx1 + bx2, wy1 + by2)


def main(argv) -> int:
    ap = argparse.ArgumentParser(prog="ag_scale.py")
    ap.add_argument("--edited", required=True)
    ap.add_argument("--original", required=True)
    ap.add_argument("--target-frac", type=float, default=0.02)
    ap.add_argument("--min-px", type=int, default=10)
    ap.add_argument("--out", default=None)
    ap.add_argument("--json", dest="json_out", default=None)
    args = ap.parse_args(argv)
    try:
        verdict = scale_correct(args.edited, args.original,
                                target_frac=args.target_frac,
                                min_px=args.min_px, out_path=args.out)
    except (subprocess.CalledProcessError, ValueError, RuntimeError,
            OSError) as e:
        print(json.dumps({"ok": False, "reason": f"{e}"}))
        return 1
    text = json.dumps(verdict, indent=2)
    print(text)
    if args.json_out:
        Path(args.json_out).write_text(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
