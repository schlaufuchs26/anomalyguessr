#!/usr/bin/env python3
"""Mechanical post-render checks for AnomalyGuessr (ticket #1485).

Evan's rejection reasons are concrete ("no smartphone in this image", "color
on black and white photo", "too prominent"), and the nine-requirement checker
rubric catches almost none of them: rejections average a checker score of 6.5,
acceptances 7.25, and nearly every new scene scores 8/9 with ``needs_review``
whether Evan keeps it or not.

This module turns the mechanically checkable classes into deterministic
checks, so they no longer depend on the language model's judgement:

- **presence** (:func:`presence_finding`): does the vision localization see
  the named element, and does it lie inside the drawn answer area? The vision
  call itself lives in ``ag_generate.presence_check`` (it shares the call
  plumbing); this function is the pure decision on its answer.
- **tone** (:func:`tone_finding`): a grayscale source must stay grayscale.
  Measured from the files, no model: the added element's area is the tight
  localization box when we have one, else the answer ellipse.
- **size** (:func:`size_finding`): the rendered element height (the
  localization box) against the band the edit prompt asks for (about 2-3 %
  for an object, 8-15 % for a person), plus a relative measure against the
  scene's own scale (:func:`relative_size`). Since #1487 this is a *report
  line only*: absolute element height does not separate Evan's size
  rejections from the elements he keeps, so it never flags a scene.

Presence and tone are flags: a failure flags the scene for moderation and a
presence failure can drive one repair attempt. Size never flags. Nothing
drops the scene, so the run's image-call budget is untouched.

Since #1504 the presence failure is split: an element that is merely
*outside* the drawn click area (:func:`recomputed_answer`) is fixed by
re-deriving the ellipse from the box the vision call already reported, no
image call; only an *absent* element needs a fresh draw. The scene then
ships playable instead of spending a render on a defect that is ours.

The thresholds are measured, not guessed: see
``wiki/entries/anomalyguessr-checks.md`` for the class distribution and the
calibration on the 2026-09-14 pool.
"""

import subprocess
from pathlib import Path

import ag_verify

# A source whose mean HSL saturation is at or below this counts as grayscale.
# Measured on the pool: true grayscale originals sit at 0.000-0.030, sepia /
# colored ones at 0.10+.
GRAYSCALE_MAX_SATURATION = 0.03
# A pixel counts as "colored" above this saturation; JPEG chroma noise on a
# grayscale scan stays well below it.
COLOR_SATURATION_LEVEL = 0.20
# More than this fraction of the element area carrying color is a tone
# failure. Measured backgrounds (grayscale scans, JPEG) stay under 0.005.
COLOR_FRACTION_MAX = 0.02
# The size band the edit prompt asks for (DEFAULT_OBJECT_SCALE /
# DEFAULT_FIGURE_SCALE in ag_generate), as a fraction of the image height.
# These are the two values the report carries: the 2 % floor from #1473 and
# the 3 % ceiling #1485 added.
OBJECT_SIZE_BAND = (0.02, 0.03)
FIGURE_SIZE_BAND = (0.08, 0.15)
# The band the reported ``verdict`` classifies against. The localization box
# is the element silhouette (measured 2026-09-14, #1487), but it does not
# separate the classes: accepted scenes carry boxes up to 0.21 (a daypack, a
# robot, both tight), the size rejections sit at 0.13-0.24. The verdict is
# therefore classification for the report, not a gate; a future measure
# flips ``flag`` to True only once it separates the two verdict sets.
OBJECT_GATE_BAND = (0.015, 0.15)
FIGURE_GATE_BAND = (0.05, 0.30)


def _convert_text(*args) -> str:
    r = subprocess.run(["convert", *args], capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"convert ... failed: {r.stderr[:300]}")
    return r.stdout.strip()


def mean_saturation(path: Path) -> float:
    """Mean HSL saturation of an image, 0..1 (deterministic, #1485)."""
    out = _convert_text(str(path), "-colorspace", "HSL", "-channel", "G",
                        "-separate", "-format", "%[fx:mean]", "info:")
    return float(out or 0.0)


def element_region(answer: dict, box: dict | None = None) -> tuple:
    """Normalized (x1, y1, x2, y2) of the element's area.

    The tight vision box when the presence call returned one, else the
    bounding square of the answer ellipse. Clamped to the frame.
    """
    if box:
        return (max(0.0, float(box["x1"])), max(0.0, float(box["y1"])),
                min(1.0, float(box["x2"])), min(1.0, float(box["y2"])))
    x, y, r = float(answer["x"]), float(answer["y"]), float(answer["r"])
    return (max(0.0, x - r), max(0.0, y - r), min(1.0, x + r), min(1.0, y + r))


def region_saturation(path: Path, region: tuple,
                      level: float = COLOR_SATURATION_LEVEL) -> dict:
    """Saturation stats inside a normalized region of ``path``.

    ``colored_fraction`` is the share of pixels above ``level``; that is the
    number that survives a mostly-gray element area (a small colored object
    inside a large ellipse barely moves the mean). An empty region reports
    zero instead of raising, so a degenerate answer cannot fail a run.
    """
    x1, y1, x2, y2 = region
    w, h = ag_verify.image_dims(path)
    px1 = int(round(x1 * w))
    py1 = int(round(y1 * h))
    pw = max(1, int(round((x2 - x1) * w)))
    ph = max(1, int(round((y2 - y1) * h)))
    if pw < 4 or ph < 4:
        return {"width": pw, "height": ph, "mean": 0.0,
                "colored_fraction": 0.0, "empty": True}
    crop = [str(path), "-crop", f"{pw}x{ph}+{px1}+{py1}", "+repage",
            "-colorspace", "HSL", "-channel", "G", "-separate"]
    mean = float(_convert_text(*crop, "-format", "%[fx:mean]", "info:") or 0)
    colored = float(_convert_text(*crop, "-threshold",
                                  f"{level * 100:.0f}%",
                                  "-format", "%[fx:mean]", "info:") or 0)
    return {"width": pw, "height": ph, "mean": round(mean, 4),
            "colored_fraction": round(colored, 4), "empty": False}


def tone_finding(source: Path, edited: Path, answer: dict,
                 box: dict | None = None) -> dict:
    """The tone check (#1485): a grayscale source must not gain color.

    Only runs its measurement on a grayscale source; a colored source has no
    tone rule to check here (requirement 4's color-match is a judgement call
    and stays with the checker).
    """
    src = mean_saturation(source)
    out = {"checked": True, "source_grayscale": src <= GRAYSCALE_MAX_SATURATION,
           "source_saturation": round(src, 4), "failed": False,
           "class": "tone", "region": None, "edited": None}
    if not out["source_grayscale"]:
        return out
    region = element_region(answer, box)
    out["region"] = [round(v, 4) for v in region]
    out["edited"] = region_saturation(edited, region)
    out["failed"] = out["edited"]["colored_fraction"] > COLOR_FRACTION_MAX
    return out


def relative_size(height_fraction: float,
                  reference_height_percent) -> dict | None:
    """The element height against the scene's own scale (#1487).

    ``reference_height_percent`` is a typical person's rendered height in the
    same image (the presence call returns it), so ``factor`` answers "how
    many typical people tall is the element". None when there is no usable
    reference (a photo without people, or a malformed answer).

    Reported only: Evan's decision on #1487 is to build this measure and
    report it, and to gate on it only once it separates accepted from
    rejected scenes.
    """
    try:
        ref = float(reference_height_percent) if reference_height_percent \
            is not None else 0.0
    except (TypeError, ValueError):
        ref = 0.0
    if ref <= 0:
        return None
    return {"reference_percent": round(ref, 4),
            "factor": round(height_fraction / (ref / 100.0), 4)}


def size_finding(box: dict | None, figure: bool = False,
                 reference_height_percent=None) -> dict:
    """The size check (#1485, report-only since #1487).

    ``box`` is the normalized localization box from the presence vision call,
    which the 2026-09-14 measurement showed *is* the element's silhouette: on
    eight renders it bounds the element tightly and sometimes undershoots.
    Without a box the check reports ``checked: False``; the answer circle's
    radius is a coverage margin, not an element height, so it is not used.

    ``verdict`` classifies the height against ``gate_band`` for the report.
    It is not a failure and never flags a scene (``failed`` and ``flag`` stay
    False): accepted scenes reach 0.19-0.21 while the size rejections sit at
    0.13-0.24, so the classes overlap. ``relative`` carries the scene-relative
    measure (:func:`relative_size`).
    """
    out = {"checked": False, "reason": "no localization box", "class": "size",
           "failed": False, "flag": False, "verdict": "unmeasured",
           "figure": bool(figure), "relative": None}
    if not box:
        return out
    height = float(box["y2"]) - float(box["y1"])
    band = FIGURE_SIZE_BAND if figure else OBJECT_SIZE_BAND
    gate = FIGURE_GATE_BAND if figure else OBJECT_GATE_BAND
    verdict = ("too_small" if height < gate[0]
               else "too_prominent" if height > gate[1] else "ok")
    out.update({"checked": True, "reason": "", "height_fraction":
                round(height, 4), "band": [round(v, 4) for v in band],
                "gate_band": [round(v, 4) for v in gate], "verdict": verdict,
                "relative": relative_size(height, reference_height_percent)})
    return out


def box_center_inside(answer: dict, box: dict, margin: float = 1.0) -> bool:
    """Whether the box's centre falls inside the answer ellipse (#1485).

    The answer is a circle in normalized coordinates (r is a fraction of the
    image height), so the test is Euclidean there. ``margin`` widens the
    ellipse slightly for the presence decision: a click area that is close
    enough that the element's centre is inside is "in the click area", the
    class Evan rejects as "no such bottle in the click area" is an element
    that sits elsewhere entirely.
    """
    r = float(answer["r"]) * margin
    if r <= 0:
        return False
    cx = (float(box["x1"]) + float(box["x2"])) / 2
    cy = (float(box["y1"]) + float(box["y2"])) / 2
    return ((cx - answer["x"]) / r) ** 2 + ((cy - answer["y"]) / r) ** 2 <= 1.0


def answer_covers_box(answer: dict, box: dict) -> bool:
    """Whether the drawn ellipse contains every corner of the box (#1445).

    Lives here so the presence decision and the click-target pass share one
    geometry; ``ag_generate.answer_covers_box`` delegates to it.
    """
    r = float(answer["r"])
    if r <= 0:
        return False
    return all(
        ((x - answer["x"]) / r) ** 2 + ((y - answer["y"]) / r) ** 2 <= 1.0
        for x in (box["x1"], box["x2"]) for y in (box["y1"], box["y2"]))


# Floor for the click ellipse recomputed from a presence box (#1504). The
# player aims at the ellipse, so a tiny element box must not leave a sliver
# target. Measured on the 177 stored scenes (2026-09-14): answer radii run
# 0.035-0.22, median 0.117; 0.05 keeps a small element clickable without
# inflating it over a neighbouring object. It sits below the person floor
# (``ag_verify.PERSON_MIN_RADIUS``, 0.12), so a figure keeps its larger area.
MIN_CLICK_RADIUS = 0.05


def recomputed_answer(box: dict, figure: bool = False) -> dict:
    """The click ellipse re-derived from the element box (#1504).

    The "outside" presence finding is our own datum, not a damaged image: the
    vision call reports where the element sits, so the ellipse can be drawn
    around it without spending an image call. The box goes through the same
    coordinate path as a click-target correction (``ag_verify.box_to_answer``:
    half-diagonal times the cover margin, clamped to the answer-radius window,
    person floor for a figure), then the click floor
    (:data:`MIN_CLICK_RADIUS`) applies and the centre is clamped into the
    frame.

    The centre is the box centre, already inside [0, 1] because the vision box
    is validated that way; the clamp is a float-safety net. The whole ellipse
    is deliberately NOT forced inside the frame: pushing an edge element's
    centre inward would undo the coverage the margin bought, and a circle that
    reaches past the frame edge costs the player nothing.
    """
    answer = ag_verify.box_to_answer(
        [box["x1"], box["y1"], box["x2"], box["y2"]], person=figure)
    r = min(ag_verify.MAX_ANSWER_RADIUS, max(answer["r"], MIN_CLICK_RADIUS))
    return {"x": round(min(1.0, max(0.0, answer["x"])), 4),
            "y": round(min(1.0, max(0.0, answer["y"])), 4),
            "r": round(r, 4)}


def presence_finding(vision: dict, answer: dict) -> dict:
    """The presence decision on a vision localization answer (#1485).

    ``vision`` is ``{"present": bool, "box": {...}|None, "note": str}``.
    Status is one of:

    - ``ok``: the model sees the element and it lies in the answer area.
    - ``outside``: the model sees the element, but its centre lies outside
      the answer ellipse; the player would be told "wrong" for clicking the
      right thing. This is the "no such bottle in the click area" class.
    - ``absent``: the model does not see the named element at all.
    - ``unlocated``: present, but no usable box; nothing to fail on, so this
      is reported as inconclusive rather than a defect.
    """
    note = str(vision.get("note") or "")
    box = vision.get("box")
    if not vision.get("present"):
        return {"status": "absent", "failed": True, "box": box, "note": note,
                "class": "presence"}
    if not box:
        return {"status": "unlocated", "failed": False, "box": None,
                "note": note, "class": "presence"}
    inside = box_center_inside(answer, box) or answer_covers_box(answer, box)
    return {"status": "ok" if inside else "outside", "failed": not inside,
            "box": box, "note": note, "class": "presence"}


def findings_summary(findings: dict) -> dict:
    """The compact, trace-friendly view of the three checks (#1485).

    The full tone measurement and the presence call record stay in their own
    entries; this is what the scene report and the gallery carry.
    """
    out = {}
    for name in ("presence", "tone", "size", "repair"):
        f = findings.get(name)
        if f is None:
            continue
        out[name] = {k: v for k, v in f.items()
                     if k not in ("call",)}
    return out


# Which checker requirements the deterministic mechanical checks directly
# cover (#1502): Subtle (2) and Scale (3) via the size/prominence measure,
# Tone (4) via the grayscale check, Identifiable (7) via the
# presence/localization check. Those are the hard requirements: a scene that
# trips one cannot be saved by points. The remaining requirements carry the
# soft criteria and accrue points, one per criterion met. The requirement
# numbers are ag_generate.REQUIREMENTS' (stable since #1476 appended
# requirement 9).
MECHANICAL_REQUIREMENTS = (2, 3, 4, 7)
SOFT_REQUIREMENTS = (1, 5, 6, 8, 9)
# The nine-requirement rubric (ag_generate.REQUIREMENTS_TOTAL).
CURRENT_REQUIREMENTS_TOTAL = 9


def rubric_points(failed, total: int = CURRENT_REQUIREMENTS_TOTAL) -> dict:
    """The soft-point count of one checker verdict (#1502).

    ``failed`` holds the requirement numbers the checker flagged; ``total``
    is the rubric size the verdict is out of (nine since #1476, eight
    before). The count covers only the soft requirements of that rubric: a
    defect in a hard requirement is reported separately and never offset
    here.
    """
    failed = set(failed or [])
    soft = [n for n in SOFT_REQUIREMENTS if n <= total]
    return {"points": sum(1 for n in soft if n not in failed),
            "points_total": len(soft)}


# The mechanical checks are hard defects (#1502): a scene that trips one
# cannot be saved by soft points. Size is report-only since #1487, so its
# boolean stays False until a measure separates Evan's verdicts.
DEFECT_NAMES = ("presence", "tone", "size")
DEFECT_LABELS = {
    "presence": "element missing or outside the click area",
    "tone": "color on a grayscale source",
    "size": "size out of band",
}


def defect_flags(findings: dict) -> dict:
    """The hard-defect booleans of one render (#1502).

    Presence and tone are the checks that flag; size reports ``flag`` (always
    False today), so its boolean stays False. These three are the "defects"
    pot: they are never offset by the soft-point count.
    """
    presence = findings.get("presence") or {}
    tone = findings.get("tone") or {}
    size = findings.get("size") or {}
    return {"presence": bool(presence.get("failed")),
            "tone": bool(tone.get("failed")),
            "size": bool(size.get("flag"))}


def has_defect(findings: dict) -> bool:
    """Whether any hard defect fired; the precedence gate (#1502)."""
    return any(defect_flags(findings).values())


def defect_labels(defects: dict) -> list:
    """Human-readable labels of the set defect booleans (#1502)."""
    return [DEFECT_LABELS[name] for name in DEFECT_NAMES
            if defects.get(name)]


def review_reasons(findings: dict) -> list:
    """One moderation reason per failed mechanical check (#1485).

    Empty when every check passed. The reasons name the measured value, so a
    moderator can judge the finding without opening the trace. Size is a
    report line since #1487 (its measured height does not separate Evan's
    verdicts), so it contributes a reason only if a future measure sets
    ``flag``.
    """
    out = []
    presence = findings.get("presence") or {}
    if presence.get("failed"):
        out.append("presence: element not visible" if
                   presence.get("status") == "absent"
                   else "presence: element outside the answer area")
    tone = findings.get("tone") or {}
    if tone.get("failed"):
        out.append("tone: color on a grayscale source (colored fraction "
                   f"{tone['edited']['colored_fraction']})")
    size = findings.get("size") or {}
    if size.get("flag"):
        out.append(f"size: {size['verdict']} "
                   f"({size['height_fraction']} of the image height)")
    return out
