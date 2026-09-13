#!/usr/bin/env python3
"""Offline replay of the score guard over recorded traces (ticket #1461).

The generator now ships the best-scoring correction round's image instead of
always the newest (``ag_generate.best_scoring_round``). This script re-reads
the traces under ``data/anomalyguessr/traces/`` and reports what the guard
would have changed: for every scene with at least two scored check rounds,
the score sequence, the round the guard ships, and the round that shipped
before (the last one).

This is a measurement, not a pipeline component: no model calls, no queue
writes, the traces are only read. It is the replay behind the ticket numbers
(8 scenes, 1 dropped round, 6 ties; mean score 6.25 -> 6.50).

CLI::

    ag_score_guard_1461.py [--data DIR] [--report FILE]

Exit code 0 when at least one scene with two scored rounds is found, 1
otherwise (the report still explains what was found)."""

import argparse
import json
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
PIPELINE_DIR = SCRIPT_DIR.parent
sys.path.insert(0, str(PIPELINE_DIR))

import ag_generate as ag  # noqa: E402 - after sys.path setup
import ag_sources  # noqa: E402


def check_rounds(trace: dict) -> list:
    """The trace's scored check rounds, one candidate per round number.

    Production records exactly one ``check r<n>`` row per round; the first row
    of a round number wins if a trace ever carries more (a retry would repeat
    the same image).
    """
    rounds, seen = [], set()
    for call in trace.get("calls") or []:
        stage = str(call.get("stage", ""))
        if not stage.startswith("check r") or not stage[7:].isdigit():
            continue
        n = int(stage[7:])
        if n in seen:
            continue
        seen.add(n)
        rounds.append(ag.round_candidate(n, Path(f"r{n}.png"), None, call))
    return rounds


def replay(data_dir: Path) -> dict:
    scenes = []
    for path in sorted((data_dir / ag.TRACE_DIRNAME).glob("*.json")):
        try:
            trace = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not trace.get("scene"):
            continue  # a pending/in-progress sidecar has no scene yet
        scored = [r for r in check_rounds(trace) if isinstance(r["score"], int)]
        if len(scored) < 2:
            continue
        best = ag.best_scoring_round(check_rounds(trace))
        last = scored[-1]
        scenes.append({
            "scene": trace["scene"],
            "scores": [r["score"] for r in scored],
            "shipped_before": last["round"],
            "ships_now": best["round"],
            "score_before": last["score"],
            "score_now": best["score"],
            "changed": best["round"] != last["round"],
            "guard": bool(trace.get("score_guard")),
        })
    changed = [s for s in scenes if s["changed"]]
    return {
        "data": str(data_dir),
        "scenes": scenes,
        "counts": {
            "with_two_scored_rounds": len(scenes),
            "guard_changes_the_shipped_round": len(changed),
            "last_round_scored_lower": sum(
                1 for s in changed if s["score_before"] < s["score_now"]),
            "tie_kept_the_earlier_round": sum(
                1 for s in changed if s["score_before"] == s["score_now"]),
        },
        "mean_score": {
            "before": round(sum(s["score_before"] for s in scenes)
                            / len(scenes), 2) if scenes else None,
            "after": round(sum(s["score_now"] for s in scenes)
                           / len(scenes), 2) if scenes else None,
        },
    }


def main(argv=None) -> int:
    p = argparse.ArgumentParser(
        prog="ag_score_guard_1461.py",
        description="Replay the best-scoring-round guard over recorded traces "
                    "(ticket #1461).",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--data", default=None,
                   help="data dir (default: repo data/anomalyguessr)")
    p.add_argument("--report", default=None,
                   help="write the JSON report to this file too")
    args = p.parse_args(argv)
    data_dir = (Path(args.data) if args.data
                else ag_sources.default_data_dir())
    if not (data_dir / ag.TRACE_DIRNAME).is_dir():
        print(f"no traces dir under {data_dir}", file=sys.stderr)
        return 1
    report = replay(data_dir)
    text = json.dumps(report, indent=2, ensure_ascii=False)
    print(text)
    if args.report:
        Path(args.report).write_text(text + "\n", encoding="utf-8")
    return 0 if report["scenes"] else 1


if __name__ == "__main__":
    sys.exit(main())
