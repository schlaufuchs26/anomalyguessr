#!/usr/bin/env python3
"""A/B: does the source photo change the anomaly pick? (ticket #1217)

``ag_generate.py --picker llm`` (#1211) sends the source photo, a compact
candidate list and the photo facts to ``deepseek/deepseek-v4.1-flash``. Spot
checks suggested the image adds ~360 prompt tokens but no decision weight:
the pick looked driven by the candidate list + facts. This harness measures
that on a sample::

    pipeline/ag_picker_ab.py --env ~/fuchs/.env --count 20 --report /tmp/ab.json

Per source it asks the SAME prompt over the SAME candidate list three ways,
each ``--repeats`` times (3 by default):

- ``image``: the real source photo (production behavior),
- ``blank``: a uniform gray PNG with the source's dimensions (is any image
  attached at all enough?),
- ``text``: no image part at all.

The summary reports how often each variant's majority pick differs from the
``image`` majority, how stable each variant is across its own repeats (the
sampling-noise control), how often a pick differs from the rule pick, and the
prompt/completion tokens, cost and latency each variant pays. That says
whether the photo earns its tokens or whether a text-only picker would do.

Stability has two extra measures (ticket #1313): ``pair_agreement`` is the
share of same-source repeat pairs that picked the same label (the raw 38%
number of #1217), and ``split_majority_agreement`` splits a source's repeats
into two halves, takes each half's majority and asks whether the two votes
agree. The latter predicts what a k-draw majority picker (``--repeats 6`` =
two 3-draw votes) would reproduce. ``--temperature`` passes a sampling
temperature to the picker: omit it to keep the provider default, pass 0.0 to
make repeats converge.

The candidate list per source is built independently (variety caps within
one list, no cross-source bookkeeping), so all variants see the identical
list. Production accumulates the caps across a run, which would make the
lists diverge right after the first differing pick and invalidate the A/B.

Sources are independent, so ``run()`` fans them out over a thread pool
(``--jobs``, 8 by default): the 40x5 run of ticket #1217 (600 calls, ~20
minutes serial) finishes in ~2 minutes, which makes wider samples (100+
sources) practical. The row order stays the sampled order.

Live network + API spend by design (variants x repeats calls per source,
~$0.00025 each): this is a measurement tool, never a cron job. Its tests
inject the picker and spend nothing.
"""

import argparse
import base64
import json
import os
import random
import statistics
import struct
import sys
import time
import zlib
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import ag_catalog  # noqa: E402
import ag_generate as g  # noqa: E402
import ag_sources  # noqa: E402
import ag_verify  # noqa: E402

VARIANTS = ("image", "blank", "text")
DEFAULT_SEED = 1217
DEFAULT_REPEATS = 3
DEFAULT_JOBS = 8
BLANK_GRAY = 128


# ── Pure helpers ───────────────────────────────────────────────────────────

def blank_png(width: int, height: int, gray: int = BLANK_GRAY) -> bytes:
    """A uniform gray PNG of the given size, stdlib only (no Pillow).

    The blank control should cost the provider a comparable image-patch
    footprint, so the caller passes the source's own dimensions.
    """
    width, height = max(1, int(width)), max(1, int(height))
    row = b"\x00" + bytes([gray, gray, gray]) * width
    raw = row * height

    def chunk(tag: bytes, data: bytes) -> bytes:
        body = tag + data
        return (struct.pack(">I", len(data)) + body
                + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF))

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr)
            + chunk(b"IDAT", zlib.compress(raw, 6)) + chunk(b"IEND", b""))


def blank_data_url(width: int, height: int) -> str:
    """The blank control as a data URL, same shape the picker sends."""
    return ("data:image/png;base64,"
            + base64.b64encode(blank_png(width, height)).decode())


def image_url_for(variant: str, source: dict, data_dir: Path):
    """The image data URL for a variant, or None for the text variant."""
    if variant == "text":
        return None
    if variant == "blank":
        return blank_data_url(source.get("width") or 1200,
                              source.get("height") or 800)
    image = ag_sources.sources_dir(data_dir) / source["image"]
    if not image.exists():
        return None
    return g._data_url(image)


def candidates_for(source: dict, seed) -> list:
    """The candidate list one source sees, identical across all variants.

    Independent per source (empty counts): the A/B needs the same list for
    every variant, which production's accumulating variety caps cannot give.
    """
    pool = list(ag_catalog.CATALOG)
    random.Random(f"{seed}:{source['id']}").shuffle(pool)
    return g.pick_entries(pool, {}, {}, g.infer_settings(source),
                          g.parse_year(source), g.has_crowd(source), set())


def majority(labels: list):
    """The most frequent label; ties resolve to the earliest pick.

    Thin alias for the production helper (``ag_generate.majority_label``),
    ticket #1313: the multi-draw picker and this harness must agree on the
    tie-break rule.
    """
    return g.majority_label(labels)


def stable(labels: list) -> bool:
    """True when every repeat succeeded and picked the same label.

    ``False`` when any repeat errored: an unstable variant is a control
    signal, not a verdict on the model's preference.
    """
    clean = [x for x in labels if x]
    return bool(clean) and len(clean) == len(labels) and len(set(clean)) == 1


def pair_agreement(labels: list) -> tuple:
    """``(agreeing_pairs, total_pairs)`` over the repeats that parsed.

    The #1217 stability measure: two repeats of the same photo+prompt agreed
    in 38% of pairs without a temperature. Pairs count once each, in input
    order; ``(0, 0)`` when fewer than two repeats parsed.
    """
    clean = [x for x in labels if x]
    if len(clean) < 2:
        return 0, 0
    total = len(clean) * (len(clean) - 1) // 2
    agree = sum(1 for i, a in enumerate(clean) for b in clean[i + 1:]
                if a == b)
    return agree, total


def split_majority_agreement(labels: list):
    """Do two independent halves of the repeats agree on a majority label?

    Answers the production question behind ``--draws`` (ticket #1313): if
    each half is a k-draw majority vote, how often do two such votes land on
    the same label? ``None`` when either half has no parsed pick.
    """
    clean = [x for x in labels if x]
    half = len(clean) // 2
    if half < 1:
        return None
    first, second = majority(clean[:half]), majority(clean[half:])
    if first is None or second is None:
        return None
    return first == second


def ask_source(source: dict, candidates: list, data_dir: Path, call,
               repeats: int, seed=DEFAULT_SEED) -> dict:
    """Ask one source's picker question per variant x repeat.

    ``call(prompt, image_url) -> response body`` is the real OpenRouter
    call in production and an injected fake in tests; a raising call is
    recorded as an error for that repeat, never propagated.

    One source's calls stay serial here; ``run()`` overlaps whole sources.
    The function is safe to call from several threads: it only reads the
    catalog and writes locals.
    """
    prompt = g.picker_prompt(source, candidates)
    row = {
        "source": source["id"],
        "year": g.parse_year(source),
        "rule_pick": candidates[0]["label"],
        "candidates": [e["label"] for e in candidates],
        "picks": {}, "majority": {}, "stable": {}, "errors": {},
        "usage": {}, "latency_s": {},
    }
    for variant in VARIANTS:
        image_url = image_url_for(variant, source, data_dir)
        if variant != "text" and image_url is None:
            row["picks"][variant] = []
            row["majority"][variant] = None
            row["stable"][variant] = False
            row["errors"][variant] = ["missing image"]
            row["usage"][variant] = _zero_usage()
            row["latency_s"][variant] = []
            continue
        labels, errors, usage, latencies = [], [], _zero_usage(), []
        for _ in range(repeats):
            started = time.monotonic()
            try:
                body = call(prompt, image_url)
            except Exception as e:  # noqa: BLE001 - a failed call is a datum
                errors.append(f"{type(e).__name__}: {e}")
                latencies.append(round(time.monotonic() - started, 2))
                continue
            latencies.append(round(time.monotonic() - started, 2))
            u = body.get("usage") or {}
            usage["prompt_tokens"] += int(u.get("prompt_tokens") or 0)
            usage["completion_tokens"] += int(u.get("completion_tokens") or 0)
            usage["cost"] += float(u.get("cost") or 0)
            content = (body.get("choices") or [{}])[0].get("message", {}).get(
                "content")
            match = g.parse_pick(content, candidates)
            if match is None:
                errors.append("empty model answer" if not content
                              else f"off-list label: {str(content)[:80]}")
                continue
            labels.append(match["label"])
        row["picks"][variant] = labels
        row["majority"][variant] = majority(labels)
        row["stable"][variant] = stable(labels)
        row["errors"][variant] = errors
        row["usage"][variant] = usage
        row["latency_s"][variant] = latencies
    return row


def _zero_usage() -> dict:
    return {"prompt_tokens": 0, "completion_tokens": 0, "cost": 0.0}


def summarize(rows: list, baseline: str = "image") -> dict:
    """Roll the per-source rows into the A/B rates and cost/latency totals.

    ``changed_vs_baseline`` counts sources whose majority pick differs from
    the baseline's; a variant that failed on every repeat counts as changed
    (it would have planted something else). ``stable`` is the sampling-noise
    control: the share of sources whose repeats all agreed within a variant.
    """
    summary = {
        "sources": len(rows), "baseline": baseline,
        "pick_stable_rate": {}, "pair_agreement": {},
        "split_majority_agreement": {},
        "changed_vs_baseline": {},
        "differs_from_rule": {}, "fallback_sources": {},
        "prompt_tokens": {}, "completion_tokens": {}, "cost_usd": {},
        "latency_s_median": {}, "latency_s_total": {},
    }
    for variant in VARIANTS:
        changed = differs = stable_n = fallback = 0
        pairs_ok = pairs_total = split_sources = split_ok = 0
        prompt = completion = 0
        cost = 0.0
        latencies = []
        for row in rows:
            if row["majority"].get(variant) != row["majority"].get(baseline):
                changed += 1
            if row["majority"].get(variant) is None:
                fallback += 1
            elif row["majority"][variant] != row["rule_pick"]:
                differs += 1
            if row["stable"].get(variant):
                stable_n += 1
            agree, total = pair_agreement(row["picks"].get(variant) or [])
            pairs_ok += agree
            pairs_total += total
            split = split_majority_agreement(row["picks"].get(variant) or [])
            if split is not None:
                split_sources += 1
                split_ok += 1 if split else 0
            u = row["usage"].get(variant) or {}
            prompt += int(u.get("prompt_tokens") or 0)
            completion += int(u.get("completion_tokens") or 0)
            cost += float(u.get("cost") or 0)
            latencies += list(row["latency_s"].get(variant) or [])
        n = len(rows) or 1
        summary["pick_stable_rate"][variant] = round(stable_n / n, 3)
        summary["pair_agreement"][variant] = (
            round(pairs_ok / pairs_total, 3) if pairs_total else None)
        summary["split_majority_agreement"][variant] = (
            round(split_ok / split_sources, 3) if split_sources else None)
        summary["changed_vs_baseline"][variant] = round(changed / n, 3)
        summary["differs_from_rule"][variant] = round(differs / n, 3)
        summary["fallback_sources"][variant] = fallback
        summary["prompt_tokens"][variant] = prompt
        summary["completion_tokens"][variant] = completion
        summary["cost_usd"][variant] = round(cost, 6)
        summary["latency_s_median"][variant] = (
            round(statistics.median(latencies), 2) if latencies else None)
        summary["latency_s_total"][variant] = round(sum(latencies), 1)
    return summary


# ── Run loop ───────────────────────────────────────────────────────────────

def make_call(api_key: str, base_url: str, model: str, max_tokens: int,
              timeout: int, temperature=None):
    """The real picker call, closed over the configured model/API.

    ``temperature`` (ticket #1313) is passed through to the request; ``None``
    keeps the provider default, 0.0 makes repeats converge.
    """
    def call(prompt: str, image_url):
        return g.picker_request(prompt, api_key, base_url, model, max_tokens,
                                timeout, image_url=image_url,
                                temperature=temperature)
    return call


def source_row(source: dict, data_dir: Path, call, repeats: int,
               seed=DEFAULT_SEED) -> dict:
    """The row for one sampled source, or a ``skipped`` stub.

    One pool task per source: its variants and repeats go out serially, so
    ``--jobs`` sources overlap at a time.
    """
    candidates = candidates_for(source, seed)
    if not candidates:
        return {"source": source["id"], "skipped": True,
                "reason": "no fitting anomaly"}
    return ask_source(source, candidates, data_dir, call, repeats, seed)


def run(args, call) -> dict:
    """Sample sources, ask every variant, return the JSON-ready report.

    With ``--jobs`` > 1 the sources run concurrently; ``Executor.map``
    returns the rows in sampled order either way.
    """
    data_dir = Path(args.data)
    pool = [s for s in ag_sources.list_sources(data_dir, unused=args.unused)
            if (ag_sources.sources_dir(data_dir) / s["image"]).exists()]
    random.Random(args.seed).shuffle(pool)
    picked = pool[:args.count]
    jobs = args.jobs

    def execute(item):
        index, source = item
        started = time.monotonic()
        row = source_row(source, data_dir, call, args.repeats, args.seed)
        if args.progress:
            print(f"[{index}/{len(picked)}] {source['id']} "
                  f"({time.monotonic() - started:.1f}s)", file=sys.stderr,
                  flush=True)
        return row

    if jobs == 1:
        rows = [execute(item) for item in enumerate(picked, 1)]
    else:
        with ThreadPoolExecutor(max_workers=jobs) as executor:
            rows = list(executor.map(execute, enumerate(picked, 1)))
    answered = [r for r in rows if not r.get("skipped")]
    return {
        "model": args.model,
        "temperature": args.temperature,
        "seed": args.seed,
        "repeats": args.repeats,
        "jobs": jobs,
        "requested": args.count,
        "sampled": len(rows),
        "available": len(pool),
        "variants": list(VARIANTS),
        "summary": summarize(answered),
        "rows": rows,
    }


def positive_int(value: str) -> int:
    n = int(value)
    if n < 1:
        raise argparse.ArgumentTypeError(f"must be >= 1, got {value}")
    return n


def parse_args(argv=None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--data", default=str(ag_sources.default_data_dir()),
                   help="anomalyguessr data dir (default %(default)s)")
    p.add_argument("--count", type=int, default=20,
                   help="sources to sample (default %(default)s)")
    p.add_argument("--seed", type=int, default=DEFAULT_SEED)
    p.add_argument("--repeats", type=int, default=DEFAULT_REPEATS,
                   help="calls per variant per source (default %(default)s)")
    p.add_argument("--jobs", type=positive_int, default=DEFAULT_JOBS,
                   help="sources asked concurrently; 1 = serial "
                        "(default %(default)s)")
    p.add_argument("--unused", action="store_true",
                   help="sample only unused sources")
    p.add_argument("--model", default=g.DEFAULT_PICKER_MODEL)
    p.add_argument("--temperature", type=float, default=None,
                   help="picker temperature; omit (default) for the provider "
                        "default, 0.0 to make repeats converge (#1313)")
    p.add_argument("--base-url", default=g.DEFAULT_BASE_URL)
    p.add_argument("--max-tokens", type=int,
                   default=g.DEFAULT_PICKER_MAX_TOKENS)
    p.add_argument("--timeout", type=int, default=g.DEFAULT_PICKER_TIMEOUT)
    p.add_argument("--env", help="env file with OPENROUTER_API_KEY")
    p.add_argument("--progress", action="store_true",
                   help="print one line per source to stderr while running")
    p.add_argument("--report", help="write the JSON report to this path")
    return p.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    if args.env:
        ag_verify.load_env(Path(args.env))
    api_key = os.environ.get("OPENROUTER_API_KEY", "")
    if not api_key:
        print(json.dumps({"error": "OPENROUTER_API_KEY not set (pass --env)"}))
        return 1
    report = run(args, make_call(api_key, args.base_url, args.model,
                                 args.max_tokens, args.timeout,
                                 args.temperature))
    text = json.dumps(report, indent=2)
    print(text)
    if args.report:
        Path(args.report).write_text(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
