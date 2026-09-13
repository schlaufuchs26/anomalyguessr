#!/usr/bin/env python3
"""Live A/B run for ticket #1444: cumulative fix-edit vs source-based re-add.

One source, one shared initial draw, then two correction chains fed by the
SAME checker instructions:

- **cumulative** (what #1436 ships): each round edits the current image.
- **readd** (this ticket): each round re-adds the element to the SOURCE with
  the accumulated corrections (``ag_readd.readd_prompt``).

Both final images go through the same checker (``ag_generate.check_scene``)
and the same deterministic drift metric (``ag_readd.frame_drift``), so the
comparison is "same instructions, same base photo, different place the fix is
applied". The fixes come from the checkpoint chain (a checker is a model
call, so letting each branch generate its own fixes would confound the
comparison); branch B's own checks are recorded for the record.

This is a measurement, not a pipeline component: it never touches the queue,
``state.json`` or the used-markers. Images land in ``--out-dir``; the JSON
report goes to stdout (and ``--report``).

CLI::

    ag_readd_1444.py [--sources ID,ID,...] [--count N] [--data DIR]
        [--out-dir DIR] [--report FILE] [--env /home/exedev/fuchs/.env]
        [--rounds 2] [--max-image-calls 40] [--model M] [--image-model M]

Exit code 0 when at least one scene produced a comparable pair; 1 otherwise
(the report still explains what failed)."""

import argparse
import json
import os
import sys
import tempfile
import time
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
PIPELINE_DIR = SCRIPT_DIR.parent
if str(PIPELINE_DIR) not in sys.path:
    sys.path.insert(0, str(PIPELINE_DIR))

import ag_generate as g  # noqa: E402
import ag_llm  # noqa: E402
import ag_readd  # noqa: E402
import ag_sources  # noqa: E402
import ag_verify  # noqa: E402


def make_args(ns) -> argparse.Namespace:
    """The subset of ag_generate's args the measured code paths read."""
    return argparse.Namespace(
        model=ns.model,
        base_url=g.DEFAULT_BASE_URL,
        model_max_tokens=g.DEFAULT_MODEL_MAX_TOKENS,
        model_timeout=g.DEFAULT_MODEL_TIMEOUT,
        temperature=g.DEFAULT_TEMPERATURE,
        image_model=ns.image_model,
        image_size=g.DEFAULT_IMAGE_SIZE,
        image_timeout=180,
        max_generations=ns.max_image_calls,
    )


def load_sources(data_dir: Path, ids) -> list:
    index = ag_sources.load_index(data_dir)["sources"]
    out = []
    for sid in ids:
        entry = index.get(sid)
        if entry is None:
            print(f"skip {sid}: not in the source index", file=sys.stderr)
            continue
        image = entry.get("image")
        if not image or not (ag_sources.sources_dir(data_dir) / image).exists():
            print(f"skip {sid}: source image missing", file=sys.stderr)
            continue
        out.append(entry)
    return out


def trace_source_ids(data_dir: Path) -> list:
    """Source ids of the stored scenes that ran correction rounds (#1436)."""
    ids = []
    for path in sorted((data_dir / g.TRACE_DIRNAME).glob("*.json")):
        try:
            trace = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        calls = trace.get("calls") or []
        if not any(str(c.get("stage", "")).startswith("fix-edit r")
                   for c in calls):
            continue
        for call in calls:
            if call.get("stage") == "edit r0" and call.get("image"):
                name = str(call["image"])
                ids.append(name[:-4] if name.endswith(".jpg") else name)
                break
    # Newest traces first: the most recent run's scenes are the interesting
    # ones, and its sources are most likely still in the pool.
    return list(dict.fromkeys(ids))


def draw(image: Path, prompt: str, retry_prompt: str, proposal: dict, args,
         api_key: str, source: dict, totals: dict, out_dir: Path,
         stem: str) -> tuple:
    """One image-producing edit, mechanical gate included; (path|None, note).

    Mirrors ``ag_generate._draw_scene_image`` without the trace/status
    plumbing: a refused or broken draw inside ``_edit_attempts`` already spent
    its retry, so a terminal failure returns None and the caller records it.
    """
    previous = []
    for attempt in range(1, g.MECHANICAL_RETRIES + 2):
        if g.image_budget_left(args, totals) <= 0:
            return None, {"error": "image budget exhausted"}
        attempts = g._edit_attempts(image, prompt, retry_prompt, proposal,
                                    args, api_key, source, totals)
        terminal = attempts[-1]
        record = terminal["record"]
        if terminal["data"] is None:
            return None, {"error": str(record.get("error")),
                          "refused": terminal["refused"],
                          "usage": record.get("usage"),
                          "duration_s": record.get("duration_s")}
        path = g._write_bytes(terminal["data"], out_dir / f"{stem}-d{attempt}.png")
        reason, _hotspot = g.candidate_gate(path, image, previous or None)
        previous.append(path)
        if reason:
            continue
        return path, {"seed": record.get("seed"),
                      "usage": record.get("usage"),
                      "duration_s": record.get("duration_s")}
    return None, {"error": "no draw passed the mechanical gates"}


def checked(image: Path, proposal: dict, scene: dict | None, args, api_key: str,
            totals: dict) -> dict:
    """One checker call, usage folded into ``totals``; errors are recorded."""
    try:
        res = g.check_scene(image, proposal, api_key, args.model, args.base_url,
                            args.model_max_tokens, args.model_timeout,
                            args.temperature, scene=scene)
    except ag_llm.LLMError as e:
        return {"error": str(e), "score": None, "failed": [],
                "reason": "", "fix_prompt": ""}
    ag_llm.add_usage(totals, res["call"].get("usage"))
    return {"score": res["score"], "failed": res["failed"],
            "reason": res["reason"], "fix_prompt": res["fix_prompt"]}


def _summary(check: dict) -> dict:
    return {"score": check.get("score"), "failed": check.get("failed"),
            "reason": check.get("reason"),
            "fix": check.get("fix_prompt")}


def run_scene(source: dict, args, api_key: str, data_dir: Path, out_dir: Path,
              totals: dict, rounds: int) -> dict:
    """One source through both branches; returns the scene's report."""
    sid = source["id"]
    source_image = ag_sources.sources_dir(data_dir) / source["image"]
    st = g.scene_time(source)
    if st["year"] is None:
        return {"source": sid, "error": "source has no catalogue year"}

    proposal_call = g.propose_anomaly(source_image, source, [], api_key,
                                      args.model, args.base_url,
                                      args.model_max_tokens, args.model_timeout,
                                      args.temperature)
    ag_llm.add_usage(totals, proposal_call["call"].get("usage"))
    if proposal_call["errors"] or not proposal_call["proposal"]:
        return {"source": sid, "error": "proposal failed",
                "detail": proposal_call["errors"]}
    proposal = proposal_call["proposal"]

    base_prompt = g.edit_prompt(proposal)
    I0, note = draw(source_image, base_prompt, g.edit_prompt(proposal, retry=True),
                    proposal, args, api_key, source, totals, out_dir,
                    f"{sid}-r0")
    if I0 is None:
        return {"source": sid, "error": "initial draw failed", "detail": note}
    check0 = checked(I0, proposal, st, args, api_key, totals)

    chain_a, checks_a = [I0], [check0]
    chain_b, checks_b = [I0], [check0]   # check0 is shared: same I0, same fix
    fixes, round_no, stopped = [], 0, None
    while (round_no < rounds and checks_a[-1].get("failed")
           and checks_a[-1].get("fix_prompt")):
        round_no += 1
        fixes.append(checks_a[-1]["fix_prompt"])
        # A: refine the current image (what #1436 ships).
        a_next, a_note = draw(
            chain_a[-1], g._fix_prompt(proposal, fixes[-1]),
            g._fix_prompt(proposal, fixes[-1], retry=True), proposal, args,
            api_key, source, totals, out_dir, f"{sid}-A-r{round_no}")
        if a_next is None:
            stopped = f"cumulative r{round_no}: {a_note.get('error')}"
            break
        chain_a.append(a_next)
        checks_a.append(checked(a_next, proposal, st, args, api_key, totals))
        # B: re-add from the source with every correction so far.
        b_prompt = ag_readd.readd_prompt(proposal, fixes)
        b_next, b_note = draw(
            source_image, b_prompt, b_prompt + " " + g.REFUSAL_RETRY,
            proposal, args, api_key, source, totals, out_dir,
            f"{sid}-B-r{round_no}")
        if b_next is None:
            stopped = f"readd r{round_no}: {b_note.get('error')}"
            break
        chain_b.append(b_next)
        checks_b.append(checked(b_next, proposal, st, args, api_key, totals))

    a_final, a_check = chain_a[-1], checks_a[-1]
    b_final, b_check = chain_b[-1], checks_b[-1]
    return {
        "source": sid,
        "anomaly": proposal.get("anomaly"),
        "kind": proposal.get("kind"),
        "placement_kind": proposal.get("placement_kind"),
        "scene_time": st,
        "rounds": len(chain_a) - 1,
        "rounds_allowed": rounds,
        "stopped": stopped,
        "fixes": fixes[:len(chain_a) - 1],
        "initial": {"image": I0.name, "check": _summary(check0),
                    "drift": ag_readd.frame_drift(I0, source_image),
                    "note": note},
        "cumulative": {
            "image": a_final.name, "check": _summary(a_check),
            "steps": [_summary(c) for c in checks_a],
            "drift_vs_source": ag_readd.frame_drift(a_final, source_image),
            "drift_vs_initial": ag_readd.frame_drift(a_final, I0),
        },
        "readd": {
            "image": b_final.name, "check": _summary(b_check),
            "steps": [_summary(c) for c in checks_b],
            "drift_vs_source": ag_readd.frame_drift(b_final, source_image),
            "drift_vs_initial": ag_readd.frame_drift(b_final, I0),
        },
    }


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="ag_readd_1444.py")
    ap.add_argument("--sources", default=None,
                    help="comma-separated source ids; default: the sources "
                         "of the stored fix-edit traces (newest first)")
    ap.add_argument("--count", type=int, default=5)
    ap.add_argument("--data", default=None)
    ap.add_argument("--out-dir", default=None)
    ap.add_argument("--report", default=None)
    ap.add_argument("--env", default="/home/exedev/fuchs/.env")
    ap.add_argument("--rounds", type=int, default=g.CORRECTION_ROUNDS)
    ap.add_argument("--max-image-calls", type=int, default=40)
    ap.add_argument("--model", default=g.MODEL)
    ap.add_argument("--image-model", default=g.IMAGE_MODEL)
    ns = ap.parse_args(argv)

    if ns.env:
        ag_verify.load_env(Path(ns.env))
    api_key = os.environ.get("OPENROUTER_API_KEY", "")
    if not api_key:
        print("OPENROUTER_API_KEY not set (pass --env)", file=sys.stderr)
        return 1

    data_dir = Path(ns.data) if ns.data else ag_sources.default_data_dir()
    if ns.sources:
        ids = [s.strip() for s in ns.sources.split(",") if s.strip()]
    else:
        ids = trace_source_ids(data_dir)
    sources = load_sources(data_dir, ids)[:max(0, ns.count)]
    if not sources:
        print("no usable sources", file=sys.stderr)
        return 1

    out_dir = Path(ns.out_dir) if ns.out_dir else Path(
        tempfile.mkdtemp(prefix="ag-readd-"))
    args = make_args(ns)
    totals = ag_llm.zero_usage()
    started = time.time()
    scenes, failed = [], []
    for index, source in enumerate(sources, 1):
        if g.image_budget_left(args, totals) <= 0:
            failed.append({"source": source["id"], "error": "image budget"})
            continue
        print(f"[{index}/{len(sources)}] {source['id']}", file=sys.stderr)
        try:
            scene = run_scene(source, args, api_key, data_dir, out_dir, totals,
                              ns.rounds)
        except (g.GenerationError, ag_llm.LLMError, OSError) as e:
            scene = {"source": source["id"], "error": f"{type(e).__name__}: {e}"}
        if scene.get("error"):
            failed.append(scene)
        else:
            scenes.append(scene)

    report = {
        "ticket": 1444,
        "data": str(data_dir),
        "out_dir": str(out_dir),
        "model": ns.model,
        "image_model": ns.image_model,
        "rounds_allowed": ns.rounds,
        "duration_s": round(time.time() - started, 1),
        "image_calls": int(totals.get("image_calls", 0)),
        "cost_total": round(float(totals.get("cost", 0.0)), 6),
        "scenes": scenes,
        "failed": failed,
        "summary": ag_readd.compare_branches(scenes),
    }
    text = json.dumps(report, indent=2)
    if ns.report:
        Path(ns.report).write_text(text + "\n")
    print(text)
    return 0 if report["summary"].get("compared") else 1


if __name__ == "__main__":
    raise SystemExit(main())
