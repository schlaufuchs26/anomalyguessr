#!/usr/bin/env python3
"""Blind legibility probe for the AnomalyGuessr queue (ticket #1476).

The game asks a player to find the one thing that does not belong to the
photograph's year, so the closest proxy for a player is a vision model asked
exactly that, with no hint about what was planted:

    You are looking at a photograph from around <year>. Find the ONE thing
    that does not belong to that year.

The probe shows the *shipped* edited image, states only the scene's year and
records whether the model names the planted element. It is a measurement,
not a gate: it never edits or removes a scene, and it is independent of the
checker rubric (which #1432 showed to be weak), so it can say whether a
family is findable at all.

Verdicts per scene:

- ``found``: the answer names the planted element (family match or a shared
  significant token; the matcher is deliberately conservative, it can only
  under-count).
- ``other``: the answer names a different element. The raw label is reported
  so a human can judge whether it is a real second anomaly.
- ``unreadable``: no usable answer (empty content, no JSON element).

Every row also carries its element family (``ag_catalog.family_of``), so the
report is a table by family: find-rate and how often the model names
something else. Cost is the real OpenRouter usage, reported per run.

CLI::

    ag_legibility_probe.py [--data DIR] [--limit 24] [--model M]
        [--env .env] [--json]
"""

import argparse
import json
import re
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import ag_catalog  # noqa: E402
import ag_generate as g  # noqa: E402
import ag_llm  # noqa: E402
import ag_queue  # noqa: E402
import ag_sources  # noqa: E402
import ag_verify  # noqa: E402

PROBE_MODEL = g.MODEL
PROBE_MAX_TOKENS = 300
PROBE_TIMEOUT = 120
# Conservative matcher: the adjectives/materials that a wrong answer shares
# with the planted label must not count as a hit ("plastic bag" is not the
# planted "plastic bottle"). A hit needs a shared head noun or the same
# catalog family.
GENERIC_TOKENS = {
    "plastic", "clear", "modern", "disposable", "paper", "metal", "wooden",
    "solar", "powered", "electric", "electronic", "small", "large", "tiny",
    "new", "old", "real", "fake", "colored", "coloured", "white", "black",
    "red", "blue", "green", "grey", "gray", "type", "model", "kind", "box",
}
_YEAR_RE = re.compile(r"(?<!\d)((?:1[5-9]|20)\d{2})(?!\d)")


def scene_year(scene: dict) -> int | None:
    """The scene's displayed catalogue year as an int, None when absent."""
    year = scene.get("year")
    if isinstance(year, int):
        return year
    m = _YEAR_RE.search(str(year or ""))
    return int(m.group(1)) if m else None


def probe_prompt(year) -> str:
    """The blind question: only the year, no hint about the planted element."""
    return ("You are looking at a photograph from around "
            f"{year if year is not None else 'an unknown year'}. "
            "Exactly one object in it does not belong to that year: it is "
            "from a later time. Find that object. "
            'Answer as strict JSON only: {"element": "<short name of the '
            'object you found>", "reason": "<one line>"}')


def _tokens(text) -> set:
    return {t for t in re.findall(r"[a-z0-9]+", str(text or "").lower())
            if len(t) >= 3 and t not in GENERIC_TOKENS}


def names_element(answer, planted: str) -> bool:
    """Whether the probe's answer names the planted element (conservative).

    A shared catalog family counts, as does a shared non-generic token, so
    "bottle of water" names "Plastic bottle (clear PET)" while "plastic bag"
    does not. The matcher can only under-count: every raw answer is in the
    JSON report for a human check.
    """
    answer = str(answer or "").strip()
    if not answer:
        return False
    fam_a, fam_p = ag_catalog.family_of(answer), ag_catalog.family_of(planted)
    if fam_a and fam_a == fam_p:
        return True
    planted_tokens = _tokens(planted)
    return bool(planted_tokens & _tokens(answer))


def probe_scene(image: Path, scene: dict, api_key: str, model: str,
                base_url: str, temperature=0.0) -> dict:
    """One blind probe call over the shipped image; usage is in ``call``."""
    year = scene_year(scene)
    result = g._run_call(probe_prompt(year), image, api_key, model, base_url,
                         PROBE_MAX_TOKENS, PROBE_TIMEOUT, temperature)
    parsed = result.get("parsed") or {}
    answer = ag_llm.clean_text(parsed.get("element"), 80)
    planted = str(scene.get("anomaly") or "")
    verdict = ("found" if names_element(answer, planted)
               else "other" if answer else "unreadable")
    return {"id": scene.get("id"), "anomaly": planted,
            "family": scene.get("family") or ag_catalog.family_of(planted),
            "year": year, "answer": answer,
            "reason": ag_llm.clean_text(parsed.get("reason"), 200),
            "verdict": verdict, "call": result}


def select_scenes(scenes: dict, limit: int,
                  data_dir: Path | None = None) -> list:
    """Round-robin over families, so the sample spans element families.

    Deterministic: families and ids in sorted order. The probe is a
    measurement, so a reproducible sample matters more than randomness.
    """
    buckets: dict = {}
    for eid, scene in scenes.items():
        image = _image_path(eid, data_dir)
        if not image.exists():
            continue
        fam = scene.get("family") or ag_catalog.family_of(
            str(scene.get("anomaly") or "")) or "other"
        buckets.setdefault(fam, []).append(eid)
    order = sorted(buckets)
    picked, i = [], 0
    while len(picked) < limit:
        added = False
        for fam in order:
            ids = buckets[fam]
            if i < len(ids):
                picked.append(ids[i])
                added = True
                if len(picked) >= limit:
                    break
        if not added:
            break
        i += 1
    return picked


def _image_path(eid: str, data_dir: Path | None = None) -> Path:
    root = data_dir if data_dir is not None else ag_sources.default_data_dir()
    return root / "library" / eid / f"{eid}.jpg"


def probe(data_dir: Path, api_key: str, limit: int = 24, model=PROBE_MODEL,
          base_url=g.DEFAULT_BASE_URL, temperature=0.0) -> dict:
    """Probe one sample of the queue; the report is read-only."""
    state = ag_queue.load_state(data_dir)
    scenes = state.get("scenes") or {}
    picked = select_scenes(scenes, limit, data_dir)
    rows, totals = [], ag_llm.zero_usage()
    for eid in picked:
        scene = dict(scenes[eid])
        scene["id"] = eid
        try:
            row = probe_scene(_image_path(eid, data_dir), scene, api_key,
                              model, base_url, temperature)
        except ag_llm.LLMError as e:
            row = {"id": eid, "anomaly": scene.get("anomaly"), "family":
                   scene.get("family"), "year": scene_year(scene),
                   "answer": "", "reason": str(e), "verdict": "unreadable",
                   "call": {"usage": ag_llm.zero_usage(), "error": str(e)}}
        ag_llm.add_usage(totals, row["call"].get("usage"))
        rows.append(row)
    return {"data": str(data_dir), "model": model, "probed": len(rows),
            "totals": totals, "families": family_table(rows), "rows": rows,
            "verdicts": _verdict_counts(rows)}


def _verdict_counts(rows: list) -> dict:
    out = {"found": 0, "other": 0, "unreadable": 0}
    for row in rows:
        out[row["verdict"]] = out.get(row["verdict"], 0) + 1
    return out


def family_table(rows: list) -> dict:
    """Per-family find-rate and the wrong answers that were named (#1476)."""
    table: dict = {}
    for row in rows:
        fam = str(row.get("family") or "other")
        cell = table.setdefault(fam, {"scenes": 0, "found": 0, "other": 0,
                                      "unreadable": 0, "wrong": []})
        cell["scenes"] += 1
        cell[row["verdict"]] += 1
        if row["verdict"] == "other":
            cell["wrong"].append(row.get("answer") or "")
    for cell in table.values():
        cell["find_rate"] = (round(cell["found"] / cell["scenes"], 2)
                             if cell["scenes"] else 0.0)
    return table


def format_report(report: dict) -> str:
    v = report["verdicts"]
    lines = [f"probed {report['probed']} scenes with {report['model']}: "
             f"found {v['found']}, named another element {v['other']}, "
             f"unreadable {v['unreadable']} "
             f"(${report['totals']['cost']:.4f})",
             "family                 scenes  found  rate  other"]
    for fam, cell in sorted(report["families"].items(),
                            key=lambda kv: (-kv[1]["scenes"], kv[0])):
        lines.append(f"{fam:<22} {cell['scenes']:>6} {cell['found']:>6} "
                     f"{cell['find_rate']:>5.2f} {cell['other']:>6}")
    for fam, cell in sorted(report["families"].items()):
        for wrong in cell["wrong"]:
            lines.append(f"  {fam}: named instead: {wrong!r}")
    return "\n".join(lines)


def parse_args(argv=None):
    p = argparse.ArgumentParser(
        prog="ag_legibility_probe.py",
        description="Blind find-the-anachronism probe over the queue (#1476).")
    p.add_argument("--data", default=None,
                   help="data dir (default: repo data/anomalyguessr)")
    p.add_argument("--limit", type=int, default=24,
                   help="scenes to probe (default 24)")
    p.add_argument("--model", default=PROBE_MODEL)
    p.add_argument("--env", default=None, help=".env path for the API key")
    p.add_argument("--json", action="store_true", help="print JSON")
    return p.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    data_dir = Path(args.data) if args.data \
        else ag_sources.default_data_dir()
    if args.env:
        ag_verify.load_env(Path(args.env))
    import os
    api_key = os.environ.get("OPENROUTER_API_KEY", "")
    if not api_key:
        print("OPENROUTER_API_KEY is not set (use --env)", file=sys.stderr)
        return 2
    report = probe(data_dir, api_key, args.limit, args.model)
    print(json.dumps(report, indent=2) if args.json
          else format_report(report))
    return 0


if __name__ == "__main__":
    sys.exit(main())
