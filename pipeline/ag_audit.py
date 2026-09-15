#!/usr/bin/env python3
"""Read-only moderation audit for AnomalyGuessr (ticket #1485).

Answers three questions from the recorded queue, no model calls:

1. **What does Evan actually reject?** Classifies the free-text moderation
   comments into eight classes and reports the distribution over every
   rejection, not only the latest batch.
2. **Does the rubric separate?** Compares the stored nine-requirement checker
   verdict with Evan's accept/reject decision: agreement, and the two error
   directions (a clean checker verdict on a rejected scene is a "false
   green"; a flagged verdict on an accepted scene is a "false red").
3. **Do the mechanical checks help?** Recomputes the agreement with the
   deterministic tone (and, with ``--presence``, the vision presence) check
   added: a scene counts as clean only when the checker and every mechanical
   check that ran found it clean.
4. **Does the soft-point count separate?** (#1502) The best points threshold
   against Evan's verdicts, the "all soft criteria met" agreement, and the
   constant-answer baselines on the same subset.
5. **How are the "lustig" labels doing?** (#1502) How many scenes carry the
   tag, the base rate, and whether a blind vision test has earned it a place
   as a point yet.
6. **Is the source pool spread?** (#1533) The repository and decade shares
   of the pool and of its unused part, so a Gallery that keeps showing the
   same archive and the same twenty years is visible in the audit, not only
   in the gallery.

The presence check needs an API key and is opt-in (``--presence N``); the
tone check runs offline on the library images. Neither writes to the queue.

CLI::

    ag_audit.py [--data DIR] [--json] [--presence N] [--env .env]

``--json`` prints the raw report; the default is a scannable text table.
"""

import argparse
import json
import re
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import ag_checks  # noqa: E402
import ag_queue  # noqa: E402
import ag_sources  # noqa: E402

# The pipeline's own moderation notes ("Click area re-located onto the
# anomaly (worker, ticket #1285)") are audit trail, not Evan's reason; they
# must not enter the class distribution.
WORKER_NOTE_RE = re.compile(r"\(worker, ticket #", re.I)
# The eight classes the ticket names.
REJECTION_CLASSES = ("element_absent", "click_area", "tone", "size",
                     "broken_render", "scene_fit", "text_mismatch", "other")
CLASS_LABELS = {
    "click_area": "click area off / position wrong",
    "element_absent": "element not in image",
    "tone": "color / tone (incl. b/w photo)",
    "size": "too big / too small / too prominent",
    "broken_render": "broken render / artifact",
    "scene_fit": "does not fit the scene",
    "text_mismatch": "text does not match the image",
    "other": "other",
}
# Ordered patterns: the first hit wins. "color on black and white photo"
# must stay tone, "scaling is way off" is a size finding (the "off" belongs
# to the scale, not the click), and "makes no sense" is a scene-fit finding,
# so size and scene_fit sit before click_area and the absence patterns.
CLASS_PATTERNS = (
    ("tone", re.compile(
        r"black and white|black & white|grayscale|greyscale|\bgrey\b|\bgray\b|"
        r"colou?r|sepia|\btone\b", re.I)),
    ("size", re.compile(
        r"too (big|small|large|prominent|obvious|subtle|tiny)|not that big|"
        r"\bbig\b|\bhuge\b|giant|enormous|scale|scaling|size|"
        r"barely visible|way too", re.I)),
    ("scene_fit", re.compile(
        r"makes no sense|make no sense|doesn'?t (fit|make sense|belong|work)|"
        r"no sense|out of place|unnecessary|does not fit|doesn'?t belong|"
        r"not blending|blending in|irrelevant|not suitable|suitable", re.I)),
    ("click_area", re.compile(
        r"click|position|positioning|placed in the|crop|not cent(er|re)d|"
        r"cent(er|re)d|where i|wrong spot|\boff\b", re.I)),
    ("broken_render", re.compile(
        r"third arm|distort|artifact|weird|broken|cut off|glow|pasted|"
        r"blurry|low quality|uncanny|badly|wrong perspective|mangled|"
        r"isn'?t a (photo|photograph)|doesn'?t look right|"
        r"extra (arm|leg|hand)|two (bicycles|people)|second bicycle", re.I)),
    ("element_absent", re.compile(
        r"\bno\b(?!\s*t\b|\s+sense)|there is no|isn'?t|missing|"
        r"not in (the|this)|can'?t (see|actually see)|cannot see|"
        r"don'?t see|hard to (see|find)|not visible|absent|nowhere|"
        r"very hard to spot", re.I)),
    ("text_mismatch", re.compile(
        r"descri|title|\btext\b|caption|label|explanation|\bsays\b|named|"
        r"date is|no year", re.I)),
)


def classify_comment(text: str) -> str:
    """The rejection class of one moderation comment (#1485)."""
    s = str(text or "").strip()
    if not s:
        return "other"
    for name, pattern in CLASS_PATTERNS:
        if pattern.search(s):
            return name
    return "other"


def classify_rejections(feedback: dict, state: dict | None = None) -> dict:
    """Distribution of rejection reasons over every rejected scene (#1485).

    A scene with several comments contributes a class per comment; a
    rejection without a (human) comment lands in ``uncommented`` so the
    counts add up to the rejection total. The pipeline's own "worker, ticket
    #" notes are filtered out: they record what the pipeline did, not why
    Evan rejected the scene.
    """
    rejected = set(feedback.get("rejected", {})) | set(
        feedback.get("excluded", {}))
    comments = feedback.get("comments", {})
    counts = {name: 0 for name in REJECTION_CLASSES}
    scene_counts = {name: 0 for name in REJECTION_CLASSES}
    per_scene, uncommented = {}, 0
    for eid in sorted(rejected):
        texts = [c.get("text", "") for c in comments.get(eid, [])
                 if not WORKER_NOTE_RE.search(str(c.get("text", "")))]
        if not texts:
            uncommented += 1
            per_scene[eid] = "uncommented"
            continue
        classes = [classify_comment(t) for t in texts]
        for cls in classes:
            counts[cls] += 1
        scene_counts[classes[0]] += 1
        per_scene[eid] = classes[0]
    return {"rejected_scenes": len(rejected),
            "commented_scenes": len(rejected) - uncommented,
            "uncommented": uncommented,
            "comments_classified": sum(counts.values()),
            "counts": counts,
            "scene_counts": scene_counts,
            "per_scene": per_scene}


def _checker_verdict(scene: dict) -> int | None:
    """The stored checker score, None when the scene has no verdict."""
    checker = scene.get("checker") or {}
    score = checker.get("score")
    return score if isinstance(score, int) else None


def rubric_agreement(state: dict, feedback: dict) -> dict:
    """Agreement between the checker verdict and Evan's decision (#1485).

    Only scenes with a stored checker verdict count; the pre-#1436 scenes
    carry none, so this is a subset of the 159 judgements. "clean" means the
    checker failed no requirement.
    """
    accepted = set(feedback.get("accepted", {}))
    rejected = set(feedback.get("rejected", {})) | set(
        feedback.get("excluded", {}))
    rows, agree, false_green, false_red = [], 0, [], []
    for eid, scene in (state.get("scenes") or {}).items():
        score = _checker_verdict(scene)
        if score is None:
            continue
        failed = (scene.get("checker") or {}).get("failed") or []
        clean = not failed
        evan_ok = eid in accepted
        if eid not in accepted and eid not in rejected:
            continue
        rows.append({"id": eid, "score": score, "clean": clean,
                     "evan": "accepted" if evan_ok else "rejected"})
        if clean == evan_ok:
            agree += 1
        elif clean and not evan_ok:
            false_green.append(eid)
        else:
            false_red.append(eid)
    total = len(rows)
    accepted_n = sum(1 for r in rows if r["evan"] == "accepted")
    rejected_n = total - accepted_n
    return {"judged": total, "agree": agree,
            "agreement": round(agree / total, 3) if total else None,
            # The constant-answer baselines on the same subset: a rubric that
            # scores below "always reject" separates nothing (rework note
            # 2026-09-14). The pool is rejection-heavy, so these are high.
            "baseline_always_accept": (round(accepted_n / total, 3)
                                       if total else None),
            "baseline_always_reject": (round(rejected_n / total, 3)
                                       if total else None),
            "accepted": accepted_n, "rejected": rejected_n,
            "false_green": len(false_green), "false_green_ids": false_green,
            "false_red": len(false_red), "false_red_ids": false_red,
            "rows": rows}


def mechanical_agreement(state: dict, feedback: dict, data_dir: Path) -> dict:
    """Agreement with the deterministic tone check added (#1485).

    A scene is clean only when the checker failed nothing *and* the tone
    check found no color on a grayscale source. The vision presence/size
    checks need a localization box, so they run only in the live pass
    (``presence_audit``); this offline number is the lower bound.
    """
    base = rubric_agreement(state, feedback)
    accepted = set(feedback.get("accepted", {}))
    rejected = set(feedback.get("rejected", {})) | set(
        feedback.get("excluded", {}))
    agree, rows, tone_hits = 0, [], []
    for row in base["rows"]:
        eid = row["id"]
        scene = state["scenes"][eid]
        image = data_dir / "library" / eid / f"{eid}.jpg"
        source = data_dir / "library" / eid / f"{eid}-original.jpg"
        tone_failed = False
        if image.exists() and source.exists() and scene.get("answer"):
            try:
                tone_failed = ag_checks.tone_finding(
                    source, image, scene["answer"])["failed"]
            except RuntimeError:
                tone_failed = False
        if tone_failed:
            tone_hits.append(eid)
        clean = bool(row["clean"]) and not tone_failed
        evan_ok = eid in accepted
        if clean == evan_ok:
            agree += 1
        rows.append({**row, "clean_with_checks": clean,
                     "tone_failed": tone_failed})
    total = len(rows)
    return {"judged": total, "agree": agree,
            "agreement": round(agree / total, 3) if total else None,
            "tone_failed": len(tone_hits), "tone_failed_ids": tone_hits,
            "before": base["agreement"],
            "baseline_always_accept": base["baseline_always_accept"],
            "baseline_always_reject": base["baseline_always_reject"],
            "rows": rows}


def _scene_points(scene: dict) -> tuple:
    """The soft-point count of one stored verdict (#1502).

    Reads the stored ``points``/``points_total`` when present; verdicts from
    before #1502 derive it from the failed numbers and the rubric size.
    """
    checker = scene.get("checker") or {}
    points = checker.get("points")
    if isinstance(points, int):
        return points, int(checker.get("points_total") or 0)
    total = checker.get("total")
    total = int(total) if isinstance(total, int) \
        else ag_checks.CURRENT_REQUIREMENTS_TOTAL
    derived = ag_checks.rubric_points(checker.get("failed"), total)
    return derived["points"], derived["points_total"]


def points_agreement(state: dict, feedback: dict) -> dict:
    """Agreement of the soft-point count with Evan's verdicts (#1502).

    A scene passes at a threshold when it has no stored mechanical defect and
    its soft points reach the threshold. The report carries the agreement of
    the best threshold and of "all soft criteria met", each against the
    constant-answer baselines on the same subset, so a count that separates
    worse than "always reject" shows up as the number it is.
    """
    accepted = set(feedback.get("accepted", {}))
    rejected = set(feedback.get("rejected", {})) | set(
        feedback.get("excluded", {}))
    rows = []
    for eid, scene in (state.get("scenes") or {}).items():
        if eid not in accepted and eid not in rejected:
            continue
        checker = scene.get("checker") or {}
        if not isinstance(checker.get("score"), int) \
                and not isinstance(checker.get("points"), int):
            continue
        points, total = _scene_points(scene)
        mech = scene.get("mechanical") or {}
        defect = any(bool(v) for v in mech.values())
        rows.append({"id": eid, "points": points, "total": total,
                     "defect": defect, "evan": eid in accepted})
    judged = len(rows)
    accepted_n = sum(1 for r in rows if r["evan"])
    rejected_n = judged - accepted_n
    if not judged:
        return {"judged": 0, "rows": [], "best_threshold": None,
                "agreement": None, "all_points_agreement": None,
                "baseline_always_accept": None,
                "baseline_always_reject": None,
                "points_total": None, "defects": 0}
    max_total = max((r["total"] for r in rows), default=0)
    best = None
    for threshold in range(0, max_total + 1):
        agree = sum(1 for r in rows
                    if ((not r["defect"]) and r["points"] >= threshold)
                    == r["evan"])
        if best is None or agree > best["agree"]:
            best = {"threshold": threshold, "agree": agree}
    all_in = sum(1 for r in rows
                 if ((not r["defect"]) and r["points"] >= r["total"])
                 == r["evan"])
    return {
        "judged": judged, "accepted": accepted_n, "rejected": rejected_n,
        "points_total": max_total,
        "defects": sum(1 for r in rows if r["defect"]),
        "best_threshold": best["threshold"],
        "agreement": round(best["agree"] / judged, 3),
        "all_points_agreement": round(all_in / judged, 3),
        "baseline_always_accept": round(accepted_n / judged, 3),
        "baseline_always_reject": round(rejected_n / judged, 3),
        "rows": rows,
    }


# "Funny" becomes a model-scored point only after enough human labels exist
# and a blind vision test separates them from the base rate (#1502).
FUNNY_MIN_MARKS = 50
FUNNY_MIN_LIFT = 0.20


def funny_labels(feedback: dict) -> dict:
    """The "lustig" label set in feedback.json (#1502).

    ``tagged`` is how many scenes carry the label, ``judged`` how many scenes
    have a moderation verdict at all, and ``base_rate`` the share that is
    tagged: the rate a blind vision model has to beat before "funny" can
    become a point.
    """
    funny = feedback.get("funny") or {}
    judged = (set(feedback.get("accepted", {}))
              | set(feedback.get("rejected", {}))
              | set(feedback.get("excluded", {})))
    tagged = len(funny)
    return {"tagged": tagged, "judged": len(judged),
            "base_rate": round(tagged / len(judged), 3) if judged else None,
            "per_scene": dict(funny)}


def funny_verdict(tagged: int, hit_rate, base_rate,
                  min_marks: int = FUNNY_MIN_MARKS,
                  min_lift: float = FUNNY_MIN_LIFT) -> dict:
    """Whether "funny" has earned a place as a point (#1502).

    Needs the label set to be big enough *and* a blind vision test to beat
    the base rate by ``min_lift``. Until then it stays a moderation tag; the
    returned ``reason`` says which condition is missing.
    """
    if tagged < min_marks:
        return {"is_a_point": False,
                "reason": f"{tagged} of {min_marks} labels"}
    if hit_rate is None or base_rate is None:
        return {"is_a_point": False, "reason": "no blind-test numbers"}
    lift = float(hit_rate) - float(base_rate)
    return {"is_a_point": lift >= min_lift,
            "lift": round(lift, 3),
            "reason": (f"blind lift {lift:.2f}" if lift >= min_lift
                       else f"blind lift {lift:.2f} below {min_lift}")}


def acceptance_by_day(feedback: dict) -> dict:
    """Accepted vs rejected per day, from the moderation timestamps (#1485).
    The re-measurement after a change reads the last day's rate; the earlier
    days are the baseline.
    """
    days: dict = {}
    for eid, stamp in (feedback.get("accepted") or {}).items():
        day = str(stamp)[:10]
        days.setdefault(day, {"accepted": 0, "rejected": 0, "total": 0})
        days[day]["accepted"] += 1
        days[day]["total"] += 1
    for eid, stamp in (feedback.get("rejected") or {}).items():
        day = str(stamp)[:10]
        days.setdefault(day, {"accepted": 0, "rejected": 0, "total": 0})
        days[day]["rejected"] += 1
        days[day]["total"] += 1
    for cell in days.values():
        cell["rate"] = (round(cell["accepted"] / cell["total"], 2)
                        if cell["total"] else None)
    return dict(sorted(days.items()))


def scene_counts(state: dict) -> dict:
    scenes = state.get("scenes") or {}
    with_checker = sum(1 for s in scenes.values()
                       if _checker_verdict(s) is not None)
    return {"scenes": len(scenes), "with_checker": with_checker}


def source_spread(data_dir: Path) -> dict:
    """Repository and decade shares of the source pool (ticket #1533).

    The pool is what the daily run draws from, so a skew here is the cause of
    a skewed gallery: the report carries the shares of the whole pool and of
    the unused part (what selection actually sees) plus whether the unused
    part is spread (no repository or decade over a third).
    """
    entries = ag_sources.list_sources(data_dir)
    unused = [e for e in entries if not e.get("used")]
    report = ag_sources.spread_report(entries)
    report["unused"] = ag_sources.spread_report(unused)
    report["unused_spread_ok"] = ag_sources.pool_spread_ok(unused)
    return report


def audit(data_dir: Path) -> dict:
    state = ag_queue.load_state(data_dir)
    feedback = ag_queue.load_feedback(data_dir)
    funny = funny_labels(feedback)
    return {"data": str(data_dir), "scene_counts": scene_counts(state),
            "sources": source_spread(data_dir),
            "classes": classify_rejections(feedback, state),
            "rubric": rubric_agreement(state, feedback),
            "with_tone": mechanical_agreement(state, feedback, data_dir),
            "points": points_agreement(state, feedback),
            "funny": {**funny,
                      "verdict": funny_verdict(funny["tagged"], None, None)},
            "by_day": acceptance_by_day(feedback)}


def format_report(report: dict) -> str:
    classes = report["classes"]
    lines = [f"scenes: {report['scene_counts']['scenes']} "
             f"({report['scene_counts']['with_checker']} with a stored "
             "checker verdict)",
             f"rejections: {classes['rejected_scenes']} "
             f"({classes['commented_scenes']} commented, "
             f"{classes['uncommented']} without a comment)",
             "class                  comments  scenes  share"]
    total = classes["comments_classified"] or 1
    scenes_total = sum(classes["scene_counts"].values()) or 1
    for name in REJECTION_CLASSES:
        n = classes["counts"][name]
        sn = classes["scene_counts"][name]
        lines.append(f"{CLASS_LABELS[name]:<22} {n:>7} {sn:>7} "
                     f"{sn / scenes_total:>6.1%}")
    rubric, tone = report["rubric"], report["with_tone"]
    points = report["points"]
    funny = report["funny"]
    src = report["sources"]
    src_repos = ", ".join(f"{k} {v['count']}" for k, v
                          in list(src["repositories"].items())[:4])
    src_decades = ", ".join(f"{k} {v['count']}" for k, v
                            in list(src["decades"].items())[:6])
    lines += [
        f"sources (#1533): {src['total']} in the pool ({src['unused']['total']}"
        f" unused), repositories: {src_repos}",
        f"  decades: {src_decades}; unused spread ok: "
        f"{src['unused_spread_ok']}",
        f"rubric: judged {rubric['judged']}, agreement "
        f"{rubric['agreement']}, false green {rubric['false_green']} "
        f"(clean verdict, rejected), false red {rubric['false_red']} "
        f"(flagged, accepted)",
        f"  baseline on the same {rubric['judged']} scenes "
        f"({rubric['accepted']} accepted / {rubric['rejected']} rejected): "
        f"always accept {rubric['baseline_always_accept']}, "
        f"always reject {rubric['baseline_always_reject']}",
        f"with the tone check: agreement {tone['agreement']} "
        f"(before {tone['before']}), tone failures {tone['tone_failed']}",
        f"soft points (#1502): judged {points['judged']}, best threshold "
        f"{points['best_threshold']}/{points['points_total']} agreement "
        f"{points['agreement']}, all-criteria agreement "
        f"{points['all_points_agreement']}, baselines "
        f"always accept {points['baseline_always_accept']} / always reject "
        f"{points['baseline_always_reject']}, stored defects "
        f"{points['defects']}",
        f"funny labels (#1502): {funny['tagged']} tagged of "
        f"{funny['judged']} judged (base rate {funny['base_rate']}), "
        f"{funny['verdict']['reason']}; "
        f"is a point: {funny['verdict']['is_a_point']}",
        "acceptance by day:",
    ]
    for day, cell in report["by_day"].items():
        lines.append(f"  {day}: {cell['accepted']}/{cell['total']} "
                     f"({cell['rate']:.0%})")
    return "\n".join(lines)


def parse_args(argv=None):
    p = argparse.ArgumentParser(
        prog="ag_audit.py",
        description="Read-only moderation audit (#1485).")
    p.add_argument("--data", default=None)
    p.add_argument("--json", action="store_true")
    return p.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    data_dir = Path(args.data) if args.data \
        else ag_queue.default_data_dir()
    report = audit(data_dir)
    print(json.dumps(report, indent=2) if args.json
          else format_report(report))
    return 0


if __name__ == "__main__":
    sys.exit(main())
