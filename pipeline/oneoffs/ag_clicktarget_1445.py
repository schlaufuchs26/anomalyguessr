#!/usr/bin/env python3
"""Live A/B run for ticket #1445: does the click-target pass earn its call?

The #1436 pass showed the model the drawn answer (``x, y, r`` written out)
and asked it to repeat or correct those numbers. In the live 5-scene run
every verdict echoed the drawn numbers: five calls, five overlays, zero
corrections. This harness replays both designs on stored scenes whose
shipped image, answer and proposal are on disk and reports, per variant:

- **echo** (what #1436 ships): the model returns ``covers`` plus the drawn
  or corrected ``x/y/r``.
- **box** (this ticket): the model returns only the anomaly's bounding box;
  the pipeline derives the correction deterministically
  (``ag_generate.answer_covers_box`` / ``covering_answer``).

The two variants judge the SAME rendered overlay, so the only difference is
the question. A scene counts as corrected when the pipeline's answer ends up
different from the drawn one; for the box variant that is a deterministic
comparison against the model's box, for the echo variant it needs the model
to hand back different numbers.

This is a measurement, not a pipeline component: it never touches the queue,
``state.json`` or the used-markers. The JSON report goes to stdout (and
``--report``).

CLI::

    ag_clicktarget_1445.py [--count N] [--data DIR] [--out-dir DIR]
        [--report FILE] [--env /home/exedev/fuchs/.env] [--model M]
        [--scene ID ...]

Exit code 0 when at least one scene produced both verdicts; 1 otherwise.
"""

import argparse
import json
import sys
import time
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
PIPELINE_DIR = SCRIPT_DIR.parent
if str(PIPELINE_DIR) not in sys.path:
    sys.path.insert(0, str(PIPELINE_DIR))

import ag_generate as g  # noqa: E402
import ag_llm  # noqa: E402
import ag_queue  # noqa: E402
import ag_verify  # noqa: E402


def make_args(ns) -> argparse.Namespace:
    """The subset of ag_generate's args the measured code paths read."""
    return argparse.Namespace(
        model=ns.model,
        base_url=g.DEFAULT_BASE_URL,
        model_max_tokens=g.DEFAULT_MODEL_MAX_TOKENS,
        model_timeout=g.DEFAULT_MODEL_TIMEOUT,
        temperature=g.DEFAULT_TEMPERATURE,
    )


def echo_prompt(proposal: dict, answer: dict) -> str:
    """The #1436 wording this ticket replaces (kept here as the baseline)."""
    return "\n".join([
        "You are checking the answer area of a spot-the-anachronism game.",
        "The image you see is the scene with the candidate answer drawn on "
        "it: an ellipse and a small crosshair at its centre.",
        f"The element that does not belong to the photograph's time is: "
        f"{proposal['anomaly']} ({proposal['placement']}).",
        f"The drawn area is x={answer['x']:.3f}, y={answer['y']:.3f}, "
        f"r={answer['r']:.3f} (normalized: x across the width, y down the "
        "height, r the radius as a fraction of the image height).",
        "Does the drawn area fully encompass that whole element?",
        'Answer as strict JSON only: {"covers": true|false, "x": <0..1>, '
        '"y": <0..1>, "r": <0..1>, "figure": true|false, "reason": "<one '
        'line>"}. Repeat the drawn numbers when they cover the whole element; '
        "return corrected numbers when they do not (centre and radius as "
        "above; for a person head to feet, shoes included).",
    ])


def box_prompt(proposal: dict, drawn: bool) -> str:
    """The #1445 wording: localize the element, the pipeline compares.

    ``drawn`` selects the overlay variant (the model sees the rendered
    answer and can describe where the element sits relative to it) or the
    clean variant (no ellipse in the image, so a box cannot echo it).
    """
    lines = ["You are checking the answer area of a spot-the-anachronism "
             "game."]
    if drawn:
        lines.append(
            "The image you see is the scene with the candidate answer drawn "
            "on it: an ellipse outlines the answer area and a small "
            "crosshair marks its centre.")
    lines.append(
        f"The element that does not belong to the photograph's time is: "
        f"{proposal['anomaly']} ({proposal['placement']}).")
    lines.append(
        "Return the bounding box of that element as it appears in THIS "
        "image, generous enough to include all of it (for a person: head to "
        "feet, shoes included).")
    lines.append(
        'Answer as strict JSON only: {"x1": <0..1>, "y1": <0..1>, '
        '"x2": <0..1>, "y2": <0..1>, "figure": true|false, "reason": "<one '
        'line>"}, where x1,y1 is the top-left corner and x2,y2 the '
        "bottom-right corner of the box (normalized: x across the width, y "
        "down the height).")
    return "\n".join(lines)


def load_scenes(data_dir: Path, limit: int, wanted) -> list:
    """Stored scenes that carry a shipped image, an answer and a proposal."""
    state = ag_queue.load_state(data_dir)["scenes"]
    out = []
    for path in sorted((data_dir / g.TRACE_DIRNAME).glob("*.json")):
        eid = path.stem
        if wanted and eid not in wanted:
            continue
        scene = state.get(eid)
        if not scene or not isinstance(scene.get("answer"), dict):
            continue
        image = data_dir / "library" / eid / f"{eid}.jpg"
        if not image.exists():
            continue
        try:
            trace = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        proposal = None
        for call in trace.get("calls") or []:
            if call.get("stage") == "proposal":
                proposal = ag_llm.parse_json(call.get("answer") or "")
                break
        if not isinstance(proposal, dict) or not proposal.get("anomaly") \
                or not proposal.get("placement"):
            continue
        out.append({"id": eid, "image": image, "answer": scene["answer"],
                    "proposal": proposal})
        if limit and len(out) >= limit:
            break
    return out


def run_variant(prompt: str, overlay: Path, args, api_key: str) -> dict:
    started = time.time()
    result = g._run_call(prompt, overlay, api_key, args.model, args.base_url,
                         args.model_max_tokens, args.model_timeout,
                         args.temperature)
    return {"call": result, "duration_s": round(time.time() - started, 2)}


def measure(scene: dict, args, api_key: str, out_dir: Path) -> dict:
    """Both verdicts for one scene, on the same rendered overlay."""
    eid, answer = scene["id"], scene["answer"]
    overlay = g.render_click_target(
        scene["image"], answer, out_dir / f"{eid}-overlay.png")
    row = {"scene": eid, "answer": answer,
           "anomaly": scene["proposal"].get("anomaly"),
           "figure": bool(scene["proposal"].get("figure"))}

    echo = run_variant(echo_prompt(scene["proposal"], answer), overlay,
                       args, api_key)
    parsed = echo["call"]["parsed"] or {}
    coords = g.valid_coords(parsed)
    echo_covers = parsed.get("covers")
    row["echo"] = {
        "covers": echo_covers if isinstance(echo_covers, bool) else None,
        "coords": coords,
        "echoed": bool(coords and not g._coords_differ(coords, answer)),
        "corrected": bool(coords and g._coords_differ(coords, answer)),
        "reason": str(parsed.get("reason") or "")[:200],
        "raw": (echo["call"]["answer"] or "")[:400],
        "cost": float((echo["call"].get("usage") or {}).get("cost") or 0.0),
        "duration_s": echo["duration_s"],
    }

    box_call = run_variant(g.click_target_prompt(scene["proposal"]), overlay,
                           args, api_key)
    box = g.valid_box(box_call["call"]["parsed"])
    if box is None:
        row["box"] = {"box": None, "covered": None, "corrected": None,
                      "raw": (box_call["call"]["answer"] or "")[:400],
                      "cost": float((box_call["call"].get("usage") or {})
                                    .get("cost") or 0.0),
                      "duration_s": box_call["duration_s"]}
        return row
    covered = g.answer_covers_box(answer, box)
    corrected = None if covered else g.covering_answer(
        answer, g.box_answer(box))
    if corrected is not None:
        corrected = g.clamp_answer(corrected)
        if not g._coords_differ(corrected, answer):
            corrected = None
    row["box"] = {
        "box": box,
        # a box that is just the drawn ellipse (width and height ~ 2r) is an
        # echo in box clothing, not an independent localization
        "anchored": (abs((box["x2"] - box["x1"]) - 2 * answer["r"]) < 0.02
                     and abs((box["y2"] - box["y1"]) - 2 * answer["r"]) < 0.02),
        "covered": covered,
        "corrected": corrected,
        "shift": (round((corrected["x"] - answer["x"]) ** 2
                        + (corrected["y"] - answer["y"]) ** 2, 6) ** 0.5
                  if corrected else 0.0),
        "r_growth": (round(corrected["r"] / answer["r"], 3)
                     if corrected and answer["r"] else None),
        "reason": str((box_call["call"]["parsed"] or {}).get("reason")
                      or "")[:200],
        "raw": (box_call["call"]["answer"] or "")[:400],
        "cost": float((box_call["call"].get("usage") or {}).get("cost") or 0.0),
        "duration_s": box_call["duration_s"],
    }
    return row


def summarize(rows: list) -> dict:
    boxes = [r["box"] for r in rows if r["box"].get("box")]
    echo_correct = sum(1 for r in rows if r["echo"]["corrected"])
    box_correct = sum(1 for b in boxes if b["corrected"])
    shifts = [b["shift"] for b in boxes if b["corrected"]]
    growth = [b["r_growth"] for b in boxes if b["r_growth"]]
    return {
        "scenes": len(rows),
        "echo": {
            "covers_true": sum(1 for r in rows
                               if r["echo"]["covers"] is True),
            "covers_false": sum(1 for r in rows
                                if r["echo"]["covers"] is False),
            "unparsed": sum(1 for r in rows if r["echo"]["covers"] is None),
            "echoed_the_numbers": sum(1 for r in rows if r["echo"]["echoed"]),
            "corrected": echo_correct,
        },
        "box": {
            "boxes": len(boxes),
            "unparsed": len(rows) - len(boxes),
            "covered": sum(1 for b in boxes if b["covered"]),
            "corrected": box_correct,
            "anchored_on_the_ellipse": sum(1 for b in boxes if b["anchored"]),
            "shift_mean": round(sum(shifts) / len(shifts), 4) if shifts else 0.0,
            "r_growth_mean": round(sum(growth) / len(growth), 3)
            if growth else None,
        },
        "cost": round(sum(r["echo"]["cost"] + r["box"]["cost"]
                          for r in rows), 6),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--count", type=int, default=20)
    ap.add_argument("--data", type=Path,
                    default=Path(__file__).resolve().parents[2]
                    / "data" / "anomalyguessr")
    ap.add_argument("--out-dir", type=Path,
                    default=Path("/tmp/ag-clicktarget-1445"))
    ap.add_argument("--report", type=Path, default=None)
    ap.add_argument("--env", type=Path, default=Path("/home/exedev/fuchs/.env"))
    ap.add_argument("--model", default=g.MODEL)
    ap.add_argument("--scene", action="append", default=[])
    ns = ap.parse_args()
    ag_verify.load_env(ns.env)
    import os
    api_key = os.environ.get("OPENROUTER_API_KEY", "")
    if not api_key:
        print("OPENROUTER_API_KEY not set (pass --env)", file=sys.stderr)
        return 1
    args = make_args(ns)
    scenes = load_scenes(ns.data, ns.count, set(ns.scene))
    if not scenes:
        print("no stored scene with image, answer and proposal", file=sys.stderr)
        return 1
    ns.out_dir.mkdir(parents=True, exist_ok=True)
    rows = []
    for scene in scenes:
        try:
            rows.append(measure(scene, args, api_key, ns.out_dir))
        except (g.GenerationError, ag_llm.LLMError) as e:
            print(f"skip {scene['id']}: {e}", file=sys.stderr)
    report = {"model": args.model, "summary": summarize(rows),
              "scenes": rows}
    text = json.dumps(report, indent=1)
    if ns.report:
        ns.report.write_text(text)
    print(json.dumps(report["summary"], indent=1))
    return 0 if rows else 1


if __name__ == "__main__":
    raise SystemExit(main())
