#!/usr/bin/env python3
"""Source-based re-add for the correction round (ticket #1444, measurement).

#1436's ``fix-edit`` edits the CURRENT image: each checker round refines the
last render, so the pixels accumulate model re-encodes and the anomaly can
drift, lose its blend or drag the rest of the frame with it. The alternative
measured here is to RE-ADD from the source: build the original add
instruction again with the checker's corrections folded in, and let the model
redraw the element on a clean base.

This module carries the two pieces worth testing without an API key:

- ``readd_prompt``: the source-based instruction (original add plus the
  accumulated fixes). It reuses ag_generate's prompt blocks, so the hard
  constraints (one scale cap, keep-the-rest, tone/blend) stay identical to
  the normal edit call.
- ``frame_drift``: the deterministic "did the rest of the frame change?"
  metric, reusing ag_verify's 96x96 diff grid: the fraction of cells at or
  above ``ag_verify.CHANGE_LEVEL`` (the whole-frame-repaint metric of ticket
  #1122) and the same numbers for the cells outside the largest change
  cluster, i.e. the photo the correction must leave alone.

The live A/B run lives in ``pipeline/oneoffs/ag_readd_1444.py``. If the
measurement says the re-add wins, ``readd_prompt`` is what ``_fix_edit``
would call instead of building an edit of the current image.
"""

from pathlib import Path

import ag_generate
import ag_verify


def readd_prompt(proposal: dict, fixes) -> str:
    """The source-based correction instruction (ticket #1444).

    The original add instruction is issued again, with the fixes a previous
    check asked for inserted as explicit corrections before the hard
    constraints. The element identity and its placement come from the
    proposal; a re-add therefore redraws the same anomaly, it does not invent
    a new one.
    """
    head = (f"Edit this historical photograph: add ONE "
            f"{proposal['anomaly']}, {proposal['placement']}.")
    fixes = [str(f).strip() for f in (fixes or []) if str(f).strip()]
    parts = [ag_generate.PURPOSE, head]
    if fixes:
        parts.append("Apply these corrections from an earlier attempt while "
                     "you add it:")
        parts.extend(f"- {f}" for f in fixes)
    parts += [ag_generate.scale_rule(proposal), ag_generate.KEEP,
              ag_generate.BLEND]
    return " ".join(parts)


def frame_drift(image: Path, source: Path, level: int | None = None) -> dict:
    """Deterministic drift of ``image`` against ``source`` (ticket #1444).

    Compares the 96x96 diff grid (ag_verify.diff_grid) and reports:

    - ``changed_frac`` / ``mean``: whole-frame change, the metric the
      whole-frame-repaint guard of ticket #1122 uses.
    - ``rest_changed_frac`` / ``rest_mean``: the same two numbers for the
      cells OUTSIDE the largest change cluster (the anomaly), i.e. the part
      of the photograph the correction is not supposed to touch. A rising
      ``rest_mean`` between two rounds is the "repeated edits degrade the
      rest of the frame" effect the ticket asks about.

    ``rest_cells`` is 0 when the grid is empty; the caller distinguishes that
    case from a genuinely clean frame.
    """
    level = ag_verify.CHANGE_LEVEL if level is None else level
    grid = ag_verify.diff_grid(Path(image), Path(source))
    cells = grid["cells"]
    if not cells:
        return {"cells": 0, "changed_frac": 0.0, "mean": 0.0,
                "rest_cells": 0, "rest_changed_frac": 0.0, "rest_mean": 0.0}
    vals = list(cells.values())
    n = len(vals)
    changed = sum(1 for v in vals if v >= level)
    # Same adaptive threshold ag_verify.locate_hotspot uses, so the
    # "anomaly region" means the same thing in both modules.
    ordered = sorted(vals)
    p99 = ordered[min(int(n * 0.995), n - 1)]
    threshold = max(8.0, p99 * 0.7)
    components = ag_verify.clusters(cells, threshold)
    rest = []
    if components:
        top = components[0]
        for (x, y), v in cells.items():
            cx = (x + 0.5) / ag_verify.GRID
            cy = (y + 0.5) / ag_verify.GRID
            if not (top["x1"] <= cx <= top["x2"]
                    and top["y1"] <= cy <= top["y2"]):
                rest.append(v)
    else:
        rest = vals
    rest_changed = sum(1 for v in rest if v >= level)

    def frac(count: int, total: int) -> float:
        return round(count / total, 4) if total else 0.0

    return {
        "cells": n,
        "changed_frac": frac(changed, n),
        "mean": round(sum(vals) / n, 2),
        "rest_cells": len(rest),
        "rest_changed_frac": frac(rest_changed, len(rest)),
        "rest_mean": round(sum(rest) / len(rest), 2) if rest else 0.0,
    }


def compare_branches(scenes) -> dict:
    """Aggregate the A/B verdicts of the two correction branches (#1444).

    ``scenes`` is the driver's per-scene report list; every entry with
    ``rounds > 0`` and no ``stopped`` counts. Higher checker score wins a
    scene; the drift means are reported per branch so the "does the rest of
    the frame degrade?" half of the ticket has its number too.
    """
    usable = [s for s in scenes if s.get("rounds") and not s.get("stopped")]
    wins = {"readd": 0, "cumulative": 0, "tie": 0}
    scores = {"readd": [], "cumulative": []}
    changed = {"readd": [], "cumulative": []}
    rest = {"readd": [], "cumulative": []}
    for scene in usable:
        a = scene["cumulative"]["check"]["score"]
        b = scene["readd"]["check"]["score"]
        if a is None or b is None:
            continue
        scores["cumulative"].append(a)
        scores["readd"].append(b)
        wins["readd" if b > a else "cumulative" if a > b else "tie"] += 1
        for branch in ("cumulative", "readd"):
            drift = scene[branch]["drift_vs_source"]
            changed[branch].append(drift["changed_frac"])
            rest[branch].append(drift["rest_mean"])

    def mean(xs):
        return round(sum(xs) / len(xs), 4) if xs else None

    return {
        "compared": len(usable),
        "final_score": {"readd": mean(scores["readd"]),
                        "cumulative": mean(scores["cumulative"])},
        "wins": wins,
        "drift_vs_source_changed_frac": {
            "readd": mean(changed["readd"]),
            "cumulative": mean(changed["cumulative"])},
        "drift_vs_source_rest_mean": {
            "readd": mean(rest["readd"]),
            "cumulative": mean(rest["cumulative"])},
    }
