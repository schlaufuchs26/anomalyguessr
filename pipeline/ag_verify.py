#!/usr/bin/env python3
"""AnomalyGuessr scene auto-verification (ticket #1107, localization rework #1165).

Fully unattended verification of a generated scene. Since ticket #1165
(2026-09-11) the PRIMARY answer source is a vision-model localization pass
on the FULL edited image: a second image-model call returns the anomaly's
normalized bounding box, and the scene answer (center + radius) is derived
from that box. The aligned pixel-diff hotspot is DEMOTED to a fallback +
sanity gate:

1. **Localization pass (primary):** send the full edited image to the
   fuchs vision model with a structured prompt ("return the bounding box of
   the anomaly as x1,y1,x2,y2 normalized 0..1"). Also asks for present =
   true/false, a short note, and an object-size sanity hint. The answer
   center + radius come from this box. The full image is used (not a zoom
   crop), because the crop removes the depth/scale context and the whole
   point of #1165 is that the vision model localizes against the real
   scene.
2. **Pixel-diff sanity gate (fallback):** the aligned diff (original
   resized to output size, 96x96 grid, BFS cluster) still runs and rejects
   scenes where the generator did not do a localized edit at all:
   whole-frame repaint / no change / identical output (run-guards #1122,
   #1124). When the vision pass returns no parseable box, present=false or
   no API key is configured, the diff hotspot + a tight zoom-crop vision
   confirm (the pre-#1165 recipe) is the fallback for the answer.
3. **Cross-check:** when BOTH a vision box and a diff hotspot exist and
   they disagree by more than POSITION_CONFLICT_DELTA, the diff hotspot is
   likely a re-encode artifact, not the object (copenhagen-poster +
   nyc-docks, 2026-09-10). The answer follows the vision box anyway (which
   it already does in the primary path) and the verdict carries a
   `position_conflict` + `warn` so the pipeline re-checks at full image
   instead of silently trusting anything.

Vision is used only to LOCATE/describe (Evan's calibration note: its blend
ratings are biased), so a pasted-but-recognizable object passes the gate;
blending is controlled by the generation prompt, not by this gate.

Output: a JSON verdict on stdout, e.g.::

    {"ok": true, "id": "...", "answer": {"x": 0.31, "y": 0.6, "r": 0.05},
     "localization": "vision", "box": [0.2, 0.5, 0.42, 0.7],
     "hotspot": {"cx": ..., "cy": ..., "x1": ..., "y1": ..., "x2": ..., "y2": ...,
                 "area": 0.012, "peak": 96},
     "vision": {"present": true, "note": "...", "box": [...]}}

CLI::

    ag_verify.py --edited E.jpg --original O.jpg --anomaly "Digital watch" \
        [--crop-out DIR] [--no-vision] [--env /path/.env] \
        [--vision-model google/gemini-2.5-flash] [--out verdict.json] \
        [--dedup PREV1.jpg [--dedup PREV2.jpg ...]] [--no-diff]

Exit code 0 with ok=true = verified; exit code 0 with ok=false = rejected
(the JSON explains why); non-zero = tooling/network error (caller may retry).
"""

import argparse
import hashlib
import json
import math
import os
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

GRID = 96
# A diff grid cell whose gray-difference value (0-100, JPEG-rescaled) sits at
# or above this level counts as "changed" for the whole-frame-overlap checks.
# Chosen so that JPEG encoding noise (~5) and mild tone shifts (~15-25) stay
# below it while a real object edit (drawn patch or model-placed object) lands
# comfortably above it.
CHANGE_LEVEL = 35
# A cluster covering more than this fraction of the grid means the whole
# frame was repainted (generator ignored the "keep everything else" part).
MAX_CLUSTER_AREA = 0.35
# A hotspot smaller than this fraction of the frame is likely noise/JPEG
# artifacts rather than an inserted object.
MIN_CLUSTER_AREA = 0.0005
# A scene whose changed cells cover at least this fraction of the frame
# cannot be a localized object edit: the generator re-encoded the whole
# photo (JPEG-style global change). The edge-case becomes: resize the
# original to the OUTPUT dimensions before differencing so the alignment is
# neutral to the generator's re-encode.
GLOBAL_CHANGE_FRAC = 0.15
# Vision crop: half-extent around the hotspot center as a fraction of the
# full image (clamped), and the min crop dimension for a readable zoom.
CROP_MARGIN = 0.18
MIN_CROP_PX = 512
# Cross-check tolerance: when the vision model's located anomaly center
# disagrees with the diff hotspot center by more than this (fraction of the
# full image), the hotspot is likely a re-encode artifact, not the object
# (2026-09-10: copenhagen poster + nyc-docks UFO both pointed at a JPEG
# artifact while the real anomaly sat elsewhere).
POSITION_CONFLICT_DELTA = 0.1
# Minimum width/height (normalized) for a localization box to be usable.
# A smaller box is likely a mis-parse (single point), not an object.
MIN_BOX_EXTENT = 0.005
# Max box extent: a box covering nearly the whole frame means the model did
# not isolate the anomaly (it "found" the whole scene).
MAX_BOX_EXTENT = 0.95


def md5_file(path: Path) -> str:
    """Hex md5 of a file's bytes (streamed; images can be several MB)."""
    h = hashlib.md5()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def identical_output_check(edited: Path, original: Path, dedup=None):
    """Early-abort verdict when the edited image is a byte-identical re-serve.

    Ticket #1124 (measurement #1122, 2026-09-08): repeated image_generate
    calls with the same source returned byte-identical outputs (same md5)
    even when the prompt changed - a deterministic server-side re-serve /
    silent refusal. Such an output must be rejected immediately: running the
    diff/vision gate on it would find only re-encode edge artifacts and can
    confabulate an answer position for them.

    Compares the edited file's md5 against the source crop (--original) and
    against every caller-supplied previous attempt (--dedup). Returns a
    rejection verdict dict on a match, else None to continue verification.
    A --dedup path that does not exist raises ValueError: the guard must not
    silently weaken when the caller mis-globs its attempt list.
    """
    h = md5_file(edited)
    if md5_file(original) == h:
        return {"ok": False, "reason": "identical output: edited image is "
                "byte-identical (md5 " + h + ") to the source crop "
                "(generator returned the input unchanged)"}
    for f in (dedup or []):
        p = Path(f)
        if not p.exists():
            raise ValueError(f"--dedup file {p} does not exist")
        if p.resolve() == edited.resolve():
            continue
        if md5_file(p) == h:
            return {"ok": False, "reason": "identical output: byte-identical "
                    "(md5 " + h + ") to previous attempt " + str(p) +
                    " (deterministic re-serve / cache hit)"}
    return None


def image_dims(path: Path) -> tuple:
    r = subprocess.run(
        ["identify", "-format", "%w %h", str(path)],
        capture_output=True, text=True, check=True,
    )
    w, h = r.stdout.split()
    return int(w), int(h)


def diff_grid(edited: Path, original: Path) -> dict:
    """Aligned pixel-diff downscaled to a GRIDxGRID map of change values.

    Returns {"w": out_w, "h": out_h, "cells": {(x, y): value_0_100}}.
    """
    w, h = image_dims(edited)
    ow, oh = image_dims(original)
    if (w, h) == (ow, oh):
        resized = str(original)
    else:
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tf:
            tmp = tf.name
        try:
            subprocess.run(
                ["convert", str(original), "-resize", f"{w}x{h}!", tmp],
                check=True, capture_output=True,
            )
            resized = tmp
        except Exception:
            os.unlink(tmp)
            raise
    try:
        txt = subprocess.run(
            ["convert", str(edited), resized, "-compose", "difference",
             "-composite", "-colorspace", "Gray", "-resize",
             f"{GRID}x{GRID}!", "txt:-"],
            capture_output=True, text=True, check=True,
        ).stdout
    finally:
        if resized != str(original):
            os.unlink(resized)
    cells = {}
    for line in txt.splitlines()[1:]:
        try:
            coord, rest = line.split(":", 1)
            x, y = map(int, coord.split(","))
        except ValueError:
            continue
        # parse "gray(12)" or "gray(12.3%)" or srgb(...)
        m = rest.split("gray(")
        if len(m) < 2:
            continue
        val = m[1].split(")")[0].rstrip("%")
        try:
            cells[(x, y)] = float(val) if "%" in m[1] else float(val) / 255 * 100
        except ValueError:
            continue
    return {"w": w, "h": h, "cells": cells}


def clusters(cells: dict, threshold: float) -> list:
    """BFS-connected components of cells >= threshold, largest first."""
    seen = set()
    out = []
    for (x, y), v in cells.items():
        if v < threshold or (x, y) in seen:
            continue
        q = [(x, y)]
        seen.add((x, y))
        comp = []
        mass = 0.0
        while q:
            cx, cy = q.pop()
            comp.append((cx, cy))
            mass += cells.get((cx, cy), 0)
            for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                nx, ny = cx + dx, cy + dy
                if (0 <= nx < GRID and 0 <= ny < GRID and
                        (nx, ny) not in seen and
                        cells.get((nx, ny), 0) >= threshold):
                    seen.add((nx, ny))
                    q.append((nx, ny))
        xs = [p[0] for p in comp]
        ys = [p[1] for p in comp]
        out.append({
            "mass": mass,
            "size": len(comp),
            "cx": sum(xs) / len(xs) / GRID,
            "cy": sum(ys) / len(ys) / GRID,
            "x1": min(xs) / GRID,
            "y1": min(ys) / GRID,
            "x2": (max(xs) + 1) / GRID,
            "y2": (max(ys) + 1) / GRID,
        })
    return sorted(out, key=lambda c: -c["mass"])


def changed_fraction(grid: dict, level: int = CHANGE_LEVEL) -> float:
    """Fraction of diff cells at or above ``level`` (0-100 gray).

    The generator re-encodes the whole output, so JPEG noise + a global tone
    shift are present even when the actual edit is a tiny localized object.
    ``level`` is chosen above those (see CHANGE_LEVEL). A high changed
    fraction means the "edit" is actually a full-frame re-render; the
    ag_verify hotspot then finds edge artifacts, not the object, and the
    answer position is garbage (fortepan 09-08).
    """
    if not grid["cells"]:
        return 0.0
    n = sum(1 for v in grid["cells"].values() if v >= level)
    return n / len(grid["cells"])


def locate_hotspot(edited: Path, original: Path) -> dict:
    """Run the aligned diff and return the hotspot + verdict fields.

    Returns a dict with ok=False + reason when the image-level plausibility
    gate rejects the scene (no change, or a whole-frame repaint).
    """
    grid = diff_grid(edited, original)
    vals = list(grid["cells"].values())
    if not vals:
        return {"ok": False, "reason": "diff produced no cells"}
    # Whole-frame re-encode guard (ticket #1122): when most of the frame
    # changed above the JPEG/tone-shift level, the edit is a full re-render
    # and any hotspot is an alignment/edge artifact. Reject as "repaint"
    # before the cluster logic runs.
    gfrac = changed_fraction(grid)
    if gfrac >= GLOBAL_CHANGE_FRAC:
        return {
            "ok": False, "reason": "whole-frame repaint (changed cells "
            f"cover {gfrac:.0%} of the frame at level {CHANGE_LEVEL})",
            "mean": round(sum(vals) / len(vals), 2),
            "p99": round(vals[min(int(len(vals) * 0.995), len(vals) - 1)], 2),
            "changed_frac": round(gfrac, 4),
            "threshold": 0, "area": 0,
        }
    vals.sort()
    mean = sum(vals) / len(vals)
    p99 = vals[min(int(len(vals) * 0.995), len(vals) - 1)]
    threshold = max(8.0, p99 * 0.7)
    cs = clusters(grid["cells"], threshold)
    if not cs:
        return {
            "ok": False, "reason": "no change hotspot found",
            "mean": round(mean, 2), "p99": round(p99, 2),
            "threshold": round(threshold, 2),
        }
    top = cs[0]
    area = top["size"] / (GRID * GRID)
    if area > MAX_CLUSTER_AREA:
        return {
            "ok": False, "reason": "whole-frame repaint (largest change "
            f"cluster covers {area:.0%} of the frame)",
            "area": round(area, 4), "mean": round(mean, 2),
            "p99": round(p99, 2), "threshold": round(threshold, 2),
            "hotspot": top,
        }
    if area < MIN_CLUSTER_AREA:
        return {
            "ok": False, "reason": "change hotspot too small to be an "
            f"inserted object ({area:.4%} of frame)",
            "area": area, "mean": round(mean, 2),
            "p99": round(p99, 2), "threshold": round(threshold, 2),
            "hotspot": top,
        }
    # A focused cluster while the changed fraction stayed below the
    # whole-frame repaint threshold: the object is real (ticket #1122).
    return {
        "ok": True,
        "hotspot": top,
        "mean": round(mean, 2),
        "p99": round(p99, 2),
        "threshold": round(threshold, 2),
        "area": round(area, 4),
        "changed_frac": round(gfrac, 4),
    }


def box_to_answer(box, radius_from_extent: bool = True) -> dict:
    """Normalized answer (x,y,r) from a normalized [x1,y1,x2,y2] box."""
    x = (box[0] + box[2]) / 2
    y = (box[1] + box[3]) / 2
    if radius_from_extent:
        w = box[2] - box[0]
        hgt = box[3] - box[1]
        r = max(w, hgt) * 0.8
    else:
        r = 0.05
    r = max(0.02, min(r, 0.15))
    return {"x": round(x, 4), "y": round(y, 4), "r": round(r, 4)}


def answer_from_hotspot(top: dict, vision_box=None,
                        box_radius: bool = False) -> dict:
    """Normalized answer position + radius from a diff hotspot.

    Legacy helper (kept for ag_scale.py + the pixel-diff fallback path):
    the radius follows the cluster's extent (a generous hit target), capped
    to the same range the game validator allows. A vision bounding box
    (full-image normalized) overrides the center when present. With
    box_radius=True (position-conflict path, ticket #1153) the radius
    follows the box's own extent instead of the (untrusted) hotspot's.
    """
    if vision_box:
        return box_to_answer(vision_box, radius_from_extent=box_radius)
    x = top["cx"]
    y = top["cy"]
    w = top["x2"] - top["x1"]
    hgt = top["y2"] - top["y1"]
    r = max(w, hgt) * 0.8
    r = max(0.02, min(r, 0.15))
    return {"x": round(x, 4), "y": round(y, 4), "r": round(r, 4)}


def load_env(env_path) -> None:
    """Minimal .env loader (KEY=VALUE lines) for keys not already set."""
    env_path = Path(env_path)
    if not env_path.exists():
        return
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        k = k.strip()
        v = v.strip().strip('"').strip("'")
        if k and v and os.environ.get(k) is None:
            os.environ[k] = v


def vision_user_text(anomaly: str, full_image: bool) -> str:
    """The vision-check instruction for one anomaly description.

    Wording is deliberately element-neutral ("anomaly"), because the
    pipeline plants modern OBJECTS ("Plastic bottle"), entirely new
    TIME-TRAVELER PERSONS ("Time traveler: young man with modern
    sneakers", ticket #1115) and FICTIONAL-FUTURE elements ("Robot
    time traveler", ticket #1161); "anachronism" would mislead the model
    for the person and future-tech cases. The caller passes whatever to
    look for in ``anomaly``.

    With full_image=True (ticket #1165, the PRIMARY localization path) the
    model sees the WHOLE edited photo and must return the anomaly's
    bounding box in full-image normalized coordinates. With full_image=False
    (fallback) it sees a tight zoom-crop around the diff hotspot.
    """
    if full_image:
        return (
            "This is an edited historical photo. Exactly one anomaly was "
            "edited into the original photo: " + anomaly + ". An anomaly "
            "is anything that could not have been in the original photo: "
            "a later-era object, a time-traveling person, or futuristic "
            "technology a time traveler brought from a fictional future. "
            "Locate it in THIS full image. Answer as strict JSON only, no "
            "prose: "
            '{"present": true|false, "note": "<one short sentence>", '
            '"box": [x1,y1,x2,y2] or null, '
            '"size_hint": "<one short phrase about its rendered size in '
            'this image, e.g. \'about 2 percent of the image height\'>"}'
            ". present=false if the anomaly is not visible or not "
            "recognizable in this image. When present, box is its bounding "
            "box in THIS image, normalized 0..1 (x1,y1 = top-left, x2,y2 = "
            "bottom-right), tight around the anomaly."
        )
    return (
        "This is a tight zoom-crop of an edited historical photo. "
        "Exactly one anomaly was edited into the original photo: "
        + anomaly + ". An anomaly is anything that could not have been "
        "in the original photo: a later-era object, a "
        "time-traveling person, or futuristic technology a time "
        "traveler brought from a fictional future. "
        "Answer as strict JSON only, no prose: "
        '{"present": true|false, "note": "<one short sentence>", '
        '"box": [x1,y1,x2,y2] or null}. present=false if the anomaly '
        "is not visible or not recognizable in this crop. When present, "
        "box is its bounding box in THIS image, normalized 0..1 "
        "(x1,y1 = top-left, x2,y2 = bottom-right)."
    )


def vision_check(image: Path, anomaly: str, api_key: str, model: str,
                 full_image: bool = True) -> dict:
    """Ask the fuchs vision model to localize the anomaly in an image.

    Returns {"present": bool, "note": str, "box": [x1,y1,x2,y2]|None,
    "size_hint": str|None} where box is normalized (0..1) in the
    coordinate space of ``image``. The dict is parsed leniently:
    malformed output degrades to present=false/box=None so the caller can
    fall back to the pixel-diff path (ticket #1165).
    """
    import base64

    b64 = base64.b64encode(image.read_bytes()).decode()
    payload = {
        "model": model,
        "messages": [{
            "role": "user",
            "content": [
                {"type": "text", "text": vision_user_text(anomaly,
                                                           full_image)},
                {"type": "image_url",
                 "image_url": {"url": f"data:image/png;base64,{b64}"}},
            ],
        }],
        "max_tokens": 400,
    }
    req = urllib.request.Request(
        "https://openrouter.ai/api/v1/chat/completions",
        data=json.dumps(payload).encode(),
        headers={"Authorization": f"Bearer {api_key}",
                 "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=90) as r:
            body = json.load(r)
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"vision request failed: HTTP {e.code} "
                           f"{e.read()[:300]!r}") from e
    content = (body.get("choices") or [{}])[0].get("message", {}).get(
        "content", "")
    if not content:
        raise RuntimeError("vision request returned no content")
    return parse_vision(content)


def parse_vision(content: str) -> dict:
    """Extract {present, note, box, size_hint} from the model's JSON answer."""
    import re

    s = content.strip()
    # strip markdown fences if present
    s = re.sub(r"^```(?:json)?\s*|\s*```$", "", s)
    m = re.search(r"\{.*\}", s, re.S)
    if not m:
        return {"present": False, "note": content[:200], "box": None,
                "size_hint": None}
    try:
        obj = json.loads(m.group(0))
    except json.JSONDecodeError:
        return {"present": False, "note": content[:200], "box": None,
                "size_hint": None}
    present = bool(obj.get("present"))
    note = str(obj.get("note", ""))[:300]
    size_hint = obj.get("size_hint")
    size_hint = str(size_hint)[:200] if size_hint else None
    box = obj.get("box")
    if (isinstance(box, list) and len(box) == 4 and
            all(isinstance(v, (int, float)) for v in box) and
            all(0 <= v <= 1 for v in box)):
        box = [float(box[0]), float(box[1]), float(box[2]), float(box[3])]
        if box[0] >= box[2] or box[1] >= box[3]:
            box = None
        elif (box[2] - box[0] < MIN_BOX_EXTENT or
              box[3] - box[1] < MIN_BOX_EXTENT or
              max(box[2] - box[0], box[3] - box[1]) > MAX_BOX_EXTENT):
            box = None
    else:
        box = None
    return {"present": present, "note": note, "box": box,
            "size_hint": size_hint}


def map_crop_box(box, crop_x1: int, crop_y1: int, crop_w: int, crop_h: int,
                 img_w: int, img_h: int) -> list:
    """Convert a crop-relative normalized box to full-image normalized."""
    return [
        round((crop_x1 + box[0] * crop_w) / img_w, 4),
        round((crop_y1 + box[1] * crop_h) / img_h, 4),
        round((crop_x1 + box[2] * crop_w) / img_w, 4),
        round((crop_y1 + box[3] * crop_h) / img_h, 4),
    ]


def crop_window(edited: Path, hotspot: dict):
    """Return ((x1, y1, x2, y2), (w, h)) of the crop in full-image pixels."""
    w, h = image_dims(edited)
    cx = hotspot["cx"] * w
    cy = hotspot["cy"] * h
    half = max(w, h) * CROP_MARGIN
    half = max(half, MIN_CROP_PX / 2)
    x1 = max(0, int(cx - half))
    x2 = min(w, int(cx + half))
    y1 = max(0, int(cy - half))
    y2 = min(h, int(cy + half))
    if x2 - x1 < 64 or y2 - y1 < 64:
        raise ValueError("hotspot crop too small")
    return (x1, y1, x2, y2), (w, h)


def save_crop(edited: Path, window: tuple, dest: Path) -> None:
    x1, y1, x2, y2 = window
    subprocess.run(
        ["convert", str(edited), "-crop", f"{x2 - x1}x{y2 - y1}+{x1}+{y1}",
         "+repage", str(dest)],
        check=True, capture_output=True,
    )


def vision_present_else_none(v: dict) -> dict:
    """Normalize a vision response: present=false -> box/None.

    The parse degrades gracefully (malformed JSON, missing fields), so the
    caller can attempt the pixel-diff fallback.
    """
    if not v["present"]:
        return {**v, "box": None}
    return v


def verify(edited: Path, original: Path, anomaly: str, crop_out: Path = None,
           vision: bool = True, env_path: Path = None,
           vision_model: str = None, dedup=None, diff: bool = True) -> dict:
    """Full verification pipeline. Returns the verdict dict.

    #1165: primary answer = full-image vision bounding box. The pixel-diff
    hotspot runs as a sanity gate + fallback answer source (when vision is
    disabled, returns no parseable box, or no API key is set).
    """
    edited = Path(edited)
    original = Path(original)
    dup = identical_output_check(edited, original, dedup)
    if dup is not None:
        return dup

    # --- Pixel-diff sanity gate + fallback source (demoted, #1165) ---
    loc = None
    if diff:
        loc = locate_hotspot(edited, original)
        if not loc["ok"]:
            # Whole-frame repaint / no change: the generator did not make a
            # localized edit at all, so the scene must be rejected REGARDLESS
            # of what the vision pass says (there is no object to localize).
            return loc
    top = loc["hotspot"] if (loc and loc["ok"]) else None

    verdict = {
        "ok": True,
        "answer": None,
        "localization": None,
        "hotspot": top,
    }
    if top is not None:
        verdict.update({
            "area": loc["area"],
            "mean": loc["mean"],
            "p99": loc["p99"],
        })
        verdict["answer"] = answer_from_hotspot(top)
        # Audit evidence (the answer-position crop the queue page shows).
        if crop_out is not None:
            crop_out = Path(crop_out)
            crop_out.mkdir(parents=True, exist_ok=True)
            (x1, y1, x2, y2), _ = crop_window(edited, top)
            save_crop(edited, (x1, y1, x2, y2),
                      crop_out / "hotspot-crop.png")

    # --- Vision: PRIMARY localization pass on the FULL edited image (#1165)
    if vision:
        if env_path is not None:
            load_env(env_path)
        key = os.environ.get("OPENROUTER_API_KEY")
        if not key:
            return {**verdict, "ok": False,
                    "reason": "no OPENROUTER_API_KEY for vision check",
                    "vision": None}
        model = (vision_model or os.environ.get("VISION_MODEL")
                 or "google/gemini-2.5-flash")
        try:
            v = vision_check(edited, anomaly, key, model, full_image=True)
        except RuntimeError:
            # Tooling/network error: retryable, not a scene verdict.
            raise
        vbox = v["box"] if v["present"] else None
        verdict["vision"] = {"present": v["present"], "note": v["note"],
                             "box": v["box"]}
        if v.get("size_hint"):
            verdict["vision"]["size_hint"] = v["size_hint"]

        if vbox is not None:
            # Primary path: answer from the full-image vision box.
            verdict["answer"] = box_to_answer(vbox)
            verdict["localization"] = "vision"
            if top is not None:
                vcx = (vbox[0] + vbox[2]) / 2
                vcy = (vbox[1] + vbox[3]) / 2
                delta = math.hypot(vcx - top["cx"], vcy - top["cy"])
                if delta > POSITION_CONFLICT_DELTA:
                    verdict["position_conflict"] = {
                        "delta": round(delta, 4),
                        "hotspot_center": [round(top["cx"], 4),
                                           round(top["cy"], 4)],
                        "vision_center": [round(vcx, 4), round(vcy, 4)],
                    }
                    verdict["warn"] = (
                        "vision-located anomaly (%.2f, %.2f) disagrees with "
                        "the diff hotspot (%.2f, %.2f) by %.2f > %.2f; the "
                        "hotspot is likely a re-encode artifact and the "
                        "answer follows the vision box"
                        % (vcx, vcy, top["cx"], top["cy"], delta,
                           POSITION_CONFLICT_DELTA))
            return verdict

        # Vision present without a usable full-image box: still require the
        # anomaly to be found at the diff hotspot before falling back.
        if top is None:
            if not v["present"]:
                return {**verdict, "ok": False,
                        "reason": "vision could not confirm the anomaly "
                                  f"({anomaly!r}) in the image",
                        "localization": None}
            return {**verdict, "ok": False,
                    "reason": "no usable bounding box from vision and "
                              "pixel-diff disabled (--no-diff); cannot "
                              "localize the anomaly",
                    "localization": None}
        # Fallback: tight zoom-crop around the diff hotspot + crop-relative
        # vision box (the pre-#1165 recipe), as a second opinion.
        (x1, y1, x2, y2), (w, h) = crop_window(edited, top)
        crop_path = None
        if crop_out is not None:
            crop_out = Path(crop_out)
            crop_out.mkdir(parents=True, exist_ok=True)
            crop_path = crop_out / "hotspot-crop.png"
            save_crop(edited, (x1, y1, x2, y2), crop_path)
        else:
            with tempfile.NamedTemporaryFile(suffix=".png",
                                             delete=False) as tf:
                crop_path = Path(tf.name)
            try:
                save_crop(edited, (x1, y1, x2, y2), crop_path)
            except Exception:
                crop_path.unlink(missing_ok=True)
                raise
        try:
            v2 = vision_check(crop_path, anomaly, key, model,
                              full_image=False)
        finally:
            if crop_out is None and crop_path is not None:
                crop_path.unlink(missing_ok=True)
        vbox2 = None
        if v2["present"] and v2["box"]:
            vbox2 = map_crop_box(v2["box"], x1, y1, x2 - x1, y2 - y1, w, h)
            # Cross-check (ticket #1153): the diff hotspot can sit on a
            # JPEG re-encode artifact while the real anomaly is elsewhere
            # in the crop (copenhagen-poster + nyc-docks, 2026-09-10). The
            # vision box is the independent position signal; when its center
            # disagrees with the hotspot by more than
            # POSITION_CONFLICT_DELTA the hotspot is not the object, so the
            # answer follows the box (with its extent as radius) and the
            # verdict carries a warning so the pipeline re-checks at full
            # image instead of silently trusting the artifact.
            vcx = (vbox2[0] + vbox2[2]) / 2
            vcy = (vbox2[1] + vbox2[3]) / 2
            cx, cy = top["cx"], top["cy"]
            delta = math.hypot(vcx - cx, vcy - cy)
            if delta > POSITION_CONFLICT_DELTA:
                verdict["answer"] = answer_from_hotspot(top, vbox2,
                                                        box_radius=True)
                verdict["localization"] = "vision-crop"
                verdict["position_conflict"] = {
                    "delta": round(delta, 4),
                    "hotspot_center": [round(cx, 4), round(cy, 4)],
                    "vision_center": [round(vcx, 4), round(vcy, 4)],
                }
                verdict["warn"] = (
                    "vision-located anomaly (%.2f, %.2f) disagrees with the "
                    "diff hotspot (%.2f, %.2f) by %.2f > %.2f; the hotspot "
                    "is likely a re-encode artifact and the answer follows "
                    "the vision box"
                    % (vcx, vcy, cx, cy, delta, POSITION_CONFLICT_DELTA))
            else:
                verdict["answer"] = answer_from_hotspot(top, vbox2)
                verdict["localization"] = "vision-crop"
        if not v2["present"]:
            if v["present"]:
                verdict["ok"] = False
                verdict["reason"] = (
                    "vision could not confirm the anomaly "
                    f"({anomaly!r}) in the hotspot crop")
            else:
                verdict["ok"] = False
                verdict["reason"] = ("vision could not confirm the anomaly "
                                     f"({anomaly!r}) in the image")
    else:
        # --no-vision: answer stays on the diff hotspot (when diff ran),
        # or the caller explicitly disabled both localization sources.
        if top is None:
            return {**verdict, "ok": False,
                    "reason": "verification disabled: neither --no-vision "
                              "nor pixel-diff produced a hotspot",
                    "localization": None}
        verdict["localization"] = "diff"
    return verdict


def main(argv) -> int:
    ap = argparse.ArgumentParser(prog="ag_verify.py")
    ap.add_argument("--edited", required=True)
    ap.add_argument("--original", required=True)
    ap.add_argument("--anomaly", required=True)
    ap.add_argument("--crop-out", default=None)
    ap.add_argument("--no-vision", action="store_true")
    ap.add_argument("--no-diff", action="store_true",
                    help="Skip the pixel-diff hotspot entirely (vision-only "
                         "verdict; the diff is the sanity gate + fallback, "
                         "ticket #1165).")
    ap.add_argument("--env", default=None)
    ap.add_argument("--vision-model", default=None)
    ap.add_argument("--out", default=None)
    ap.add_argument("--dedup", action="append", metavar="FILE", default=None,
                    help="Previous generated output for the same source "
                         "crop; repeatable. If the edited image is "
                         "byte-identical (md5) to any of them or to "
                         "--original, verification aborts early with "
                         "ok=false reason 'identical output' (ticket #1124).")
    args = ap.parse_args(argv)
    try:
        verdict = verify(args.edited, args.original, args.anomaly,
                         crop_out=args.crop_out, vision=not args.no_vision,
                         env_path=args.env, vision_model=args.vision_model,
                         dedup=args.dedup, diff=not args.no_diff)
    except (subprocess.CalledProcessError, ValueError, RuntimeError,
            OSError) as e:
        print(json.dumps({"ok": False, "reason": f"{e}"}))
        return 1
    text = json.dumps(verdict, indent=2)
    print(text)
    if args.out:
        Path(args.out).write_text(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))