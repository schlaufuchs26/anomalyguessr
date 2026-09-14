#!/usr/bin/env python3
"""Measurement for ticket #1504: how often is the click ellipse off the box?

The presence check (#1485) stores the element box it saw next to the shipped
answer. This harness replays the geometry only (no model, no image call):

- **covered**: the shipped ellipse already contains every box corner.
- **centre-only**: the box centre sits inside, a corner does not. The presence
  decision calls this "ok" (it tests the centre or full coverage), but a click
  on the element's far corner can miss.
- **outside**: the box centre lies outside the drawn ellipse; that is the
  finding #1504's recompute fixes.
- **absent**: no box (the element was not seen).

For every non-covered scene it runs :func:`ag_checks.recomputed_answer` and
reports how many then pass ``answer_covers_box``, plus the radius before and
after (the click floor must not leave a sliver).

Reads only ``data/anomalyguessr/traces/*.json``. The JSON report goes to
stdout and ``--report``. Exit 0 when at least one scene carried a box.

CLI::

    ag_presence_1504.py [--data DIR] [--report FILE]
"""

import argparse
import json
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
PIPELINE_DIR = SCRIPT_DIR.parent
if str(PIPELINE_DIR) not in sys.path:
    sys.path.insert(0, str(PIPELINE_DIR))

import ag_checks  # noqa: E402


def classify(answer: dict, box: dict) -> str:
    if ag_checks.answer_covers_box(answer, box):
        return "covered"
    return "centre-only" if ag_checks.box_center_inside(answer, box) \
        else "outside"


def measure(trace: dict, entry: dict | None, eid: str) -> dict | None:
    if not entry or not isinstance(entry.get("answer"), dict):
        return None
    presence = (trace.get("mechanical_checks") or {}).get("presence") or {}
    box = ((presence.get("vision") or {}).get("box") or presence.get("box"))
    if not box:
        return None
    answer = entry["answer"]
    if not isinstance(answer, dict):
        return None
    row = {"scene": eid, "status": presence.get("status"),
           "answer": {k: answer[k] for k in ("x", "y", "r")},
           "box": box, "covered": classify(answer, box)}
    if row["covered"] != "covered":
        revised = ag_checks.recomputed_answer(box)
        row["recomputed"] = {
            "answer": revised,
            "covers": ag_checks.answer_covers_box(revised, box),
            "r_before": answer["r"],
            "r_after": revised["r"],
            "at_min_radius": revised["r"] == ag_checks.MIN_CLICK_RADIUS,
        }
    return row


def summarize(rows: list) -> dict:
    fixed = [r for r in rows if r.get("recomputed")]
    covered_after = [r for r in fixed if r["recomputed"]["covers"]]
    floors = [r["recomputed"]["r_after"] for r in fixed]
    return {
        "scenes_with_box": len(rows),
        "already_covered": sum(1 for r in rows if r["covered"] == "covered"),
        "centre_only": sum(1 for r in rows if r["covered"] == "centre-only"),
        "outside": sum(1 for r in rows if r["covered"] == "outside"),
        "recomputed": len(fixed),
        "covered_after_recompute": len(covered_after),
        "min_r_after": round(min(floors), 4) if floors else None,
        "at_min_radius": sum(1 for r in fixed
                             if r["recomputed"]["at_min_radius"]),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", type=Path,
                    default=Path(__file__).resolve().parents[2]
                    / "data" / "anomalyguessr")
    ap.add_argument("--report", type=Path, default=None)
    ns = ap.parse_args()
    import ag_queue
    scenes = ag_queue.load_state(ns.data)["scenes"]
    rows = []
    for path in sorted((ns.data / "traces").glob("*.json")):
        try:
            trace = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        row = measure(trace, scenes.get(path.stem), path.stem)
        if row is not None:
            rows.append(row)
    report = {"summary": summarize(rows), "scenes": rows}
    text = json.dumps(report, indent=1)
    if ns.report:
        ns.report.write_text(text)
    print(json.dumps(report["summary"], indent=1))
    return 0 if rows else 1


if __name__ == "__main__":
    raise SystemExit(main())
