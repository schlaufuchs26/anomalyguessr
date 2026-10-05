#!/usr/bin/env python3
"""Photo tags for the source pool, and the element-fit measurement (ticket #2344).

The generator picks a source photo and an element without knowing what the two
do to each other, so an element that works on a busy street can land on an
empty landscape. Evan's hypothesis (2026-10-05): crowded photos with many
people are much more forgiving of an ostentatious time traveller.

This module does two things, measurement first:

1. ``tag`` gives every pool entry one cheap vision call that reads six fixed
   features off the photograph and caches them on the entry (``tags``). The
   call reuses ``pipeline/ag_llm.py``; an entry that already carries a
   complete tag set is skipped, and ``--limit`` lets a long backfill run in
   steps.
2. ``report`` joins those tags with the verdicts that already exist
   (``feedback.json``) and answers the hypothesis with numbers: acceptance and
   ``great`` rate per element family against crowd level, clutter and medium,
   with the sample size behind every cell. ``render_report`` turns it into the
   markdown written to the wiki page.

The join runs from a scene to its pool source twice, trace first then file
URL: a scene carries no pool id, but its generation trace names the source
(``traces/<scene id>.json`` -> ``source``) and its ``sourceUrl`` equals the
pool entry's ``fileUrl``.

The features are deliberately coarse and fixed. They are a fitting hint, not a
correctness fact: the year and place rules never read them. Since ticket #2348
the two weak signals the measurement supports do steer the generator: the
proposal prompt names the fitting kind of element for a busy or sepia photo
(``ag_generate.photo_tag_lines``) and the source picker prefers busy,
populated, non-sepia photographs (``ag_generate.source_tag_rank``).

CLI::

    ag_tags.py --data DIR tag [--limit N] [--dry-run]
    ag_tags.py --data DIR status
    ag_tags.py --data DIR report [--out PATH]
"""

import argparse
import datetime
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import ag_catalog  # noqa: E402  (sibling module, resolved via sys.path)
import ag_llm  # noqa: E402
import ag_queue  # noqa: E402
import ag_sources  # noqa: E402

DEFAULT_MODEL = "deepseek/deepseek-v4.1-flash"
DEFAULT_BASE_URL = ag_llm.DEFAULT_BASE_URL
TAG_MAX_TOKENS = 400
TAG_TIMEOUT = 90
SAVE_EVERY = 20

# The fixed feature set. Every value is one of these; anything else is an
# unusable answer, so a model that invents a category cannot poison the cache.
PEOPLE = ("none", "few", "crowd")
SCENE_TYPES = ("street", "market", "interior", "portrait", "landscape",
               "industry", "domestic")
MEDIUMS = ("colour", "grayscale", "sepia")
CLUTTER = ("clean", "busy")
ENUM_FIELDS = (("people", PEOPLE), ("scene", SCENE_TYPES),
               ("medium", MEDIUMS), ("clutter", CLUTTER))
BOOL_FIELDS = ("host", "text")
TAG_KEYS = tuple(k for k, _ in ENUM_FIELDS) + BOOL_FIELDS
# The features the report splits on (the two booleans are recorded but not
# split on; a binary "text" flag is not what Evan's hypothesis is about).
REPORT_FEATURES = ("people", "clutter", "medium")


class TagError(RuntimeError):
    """The tagging call failed for one source entry."""


def _now_iso() -> str:
    return datetime.datetime.now().astimezone().isoformat(timespec="seconds")


# ── The prompt and the parser ──────────────────────────────────────────────


def tag_prompt() -> str:
    """The one vision prompt: six fixed features as a JSON object."""
    return (
        "You label one historical photograph for a spot-the-anachronism game. "
        "Look at the image and answer with one JSON object and nothing else:\n"
        '{"people": "none"|"few"|"crowd", '
        '"scene": "street"|"market"|"interior"|"portrait"|"landscape"|'
        '"industry"|"domestic", '
        '"medium": "colour"|"grayscale"|"sepia", '
        '"clutter": "clean"|"busy", "host": true|false, "text": true|false}\n'
        "Definitions:\n"
        "- people: how many people are visible. none = nobody, few = one to "
        "five, crowd = six or more or a dense gathering.\n"
        "- scene: the dominant setting. street = a road, square or public "
        "thoroughfare; market = stalls or a trading place; interior = inside "
        "a building; portrait = one person is the subject; landscape = open "
        "countryside, water or sky with no built scene; industry = a factory, "
        "mine, railway or machine; domestic = a home, room or family scene.\n"
        "- medium: the photograph's tone. colour = any colour, grayscale = "
        "true black and white, sepia = brown-toned.\n"
        "- clutter: how busy the composition is. clean = plain surfaces, "
        "empty space or few objects; busy = many objects, goods, stalls or "
        "machinery.\n"
        "- host: true when the photo shows a plausible surface or object an "
        "added modern object could sit on or attach to (a table, stall, wall, "
        "ground, vehicle); false when there is none.\n"
        "- text: true when the photo carries legible text or signage (signs, "
        "posters, labels).\n"
        "Use exactly these words. Judge only what is visible."
    )


def _as_bool(value):
    """A model's boolean as a real bool, or None when it is neither."""
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        low = value.strip().lower()
        if low in ("true", "yes", "1"):
            return True
        if low in ("false", "no", "0"):
            return False
    return None


def parse_tags(answer):
    """Validate a model answer into the six fixed features, or None.

    ``answer`` is the raw assistant text (JSON, possibly fenced or wrapped in
    prose). A missing field, an unknown enum value or a non-boolean flag makes
    the whole answer unusable: a partial tag set would silently shrink the
    report's cells, so the caller stores nothing and retries the entry later.
    """
    if isinstance(answer, dict):
        obj = answer
    else:
        obj = ag_llm.parse_json(answer)
    if not isinstance(obj, dict):
        return None
    out = {}
    for key, allowed in ENUM_FIELDS:
        value = str(obj.get(key) or "").strip().lower()
        if value not in allowed:
            return None
        out[key] = value
    for key in BOOL_FIELDS:
        value = _as_bool(obj.get(key))
        if value is None:
            return None
        out[key] = value
    return out


def needs_tags(entry: dict) -> bool:
    """Whether a pool entry still wants the tagging call.

    The cache lives on the entry as ``tags``; an entry whose stored set is
    complete and valid is skipped, so a re-run is cheap and idempotent. A
    legacy or truncated set (a field missing, an unknown value) is retagged.
    """
    tags = (entry or {}).get("tags")
    if not isinstance(tags, dict):
        return True
    return parse_tags(tags) is None


# ── The model call and the per-entry step ──────────────────────────────────


def tag_text(image: Path, api_key: str, model: str,
             base_url: str = DEFAULT_BASE_URL, max_tokens: int = TAG_MAX_TOKENS,
             timeout: int = TAG_TIMEOUT) -> dict:
    """One vision call; returns the trace-shaped call record."""
    prompt = tag_prompt()
    started = time.time()
    message = {"role": "user",
               "content": [ag_llm.text_part(prompt), ag_llm.image_part(image)]}
    body = ag_llm.chat([message], api_key, model, base_url, max_tokens, 0.0,
                       timeout, reasoning=False)
    answer = ag_llm.content_of(body)
    return {"model": model, "prompt": prompt, "answer": answer,
            "image": image.name, "at": _now_iso(),
            "tags": parse_tags(answer), "usage": ag_llm.usage_of(body),
            "duration_s": round(time.time() - started, 2)}


def image_path(data_dir: Path, entry: dict) -> Path:
    """The pool entry's downloaded photo (``entry["image"]`` is pool-relative)."""
    return ag_sources.sources_dir(data_dir) / str(entry.get("image") or "")


def tag_entry(entry: dict, data_dir: Path, api_key: str,
              model: str = DEFAULT_MODEL, base_url: str = DEFAULT_BASE_URL,
              max_tokens: int = TAG_MAX_TOKENS, timeout: int = TAG_TIMEOUT) -> tuple:
    """Tag one pool entry; ``(entry, info)``.

    ``info["status"]`` is ``tagged`` (a valid set was stored), ``kept``
    (already tagged), ``missing_image`` (the download is gone), ``unusable``
    (the model answered something that is not the fixed set) or ``error``.
    """
    if not needs_tags(entry):
        return entry, {"status": "kept"}
    path = image_path(data_dir, entry)
    if not path.exists():
        return entry, {"status": "missing_image", "image": str(path.name)}
    try:
        call = tag_text(path, api_key, model, base_url, max_tokens, timeout)
    except Exception as e:  # noqa: BLE001 - one entry must not stop the pass
        return entry, {"status": "error", "error": f"{type(e).__name__}: {e}"}
    tags = call.get("tags")
    if not tags:
        return entry, {"status": "unusable", "answer": call.get("answer", "")[:200],
                       "call": call}
    stored = dict(tags)
    stored["model"] = model
    stored["at"] = call["at"]
    out = dict(entry)
    out["tags"] = stored
    return out, {"status": "tagged", "tags": tags, "call": call}


def backfill(data_dir: Path, api_key: str = "", model: str = DEFAULT_MODEL,
             base_url: str = DEFAULT_BASE_URL, limit: int | None = None,
             dry_run: bool = False, log=None, tag_fn=None) -> dict:
    """Tag every pool entry that still lacks a tag set.

    ``tag_fn(entry) -> (entry, info)`` is injectable so tests never dial a
    live model. Entries are walked newest-added last, oldest first, so a
    limited run makes steady progress through the pool.
    """
    index = ag_sources.load_index(data_dir)
    sources = index.get("sources") or {}
    report = {"sources": len(sources),
              "tagged": sum(1 for e in sources.values() if not needs_tags(e)),
              "targets": 0, "dry_run": bool(dry_run), "model": model}
    if tag_fn is None:
        tag_fn = lambda e: tag_entry(e, data_dir, api_key, model, base_url)  # noqa: E731
    targets = [e for e in sources.values() if needs_tags(e)]
    targets.sort(key=lambda e: (str(e.get("added") or ""), str(e.get("id") or "")))
    if limit is not None:
        targets = targets[:max(0, int(limit))]
    report["targets"] = len(targets)
    done = changed = unusable = missing = errors = 0
    totals = ag_llm.zero_usage()
    for done, entry in enumerate(targets, 1):
        if dry_run:
            continue
        tagged, info = tag_fn(entry)
        call = info.get("call")
        if call:
            ag_llm.add_usage(totals, call.get("usage"))
        status = info.get("status")
        if status == "tagged":
            sources[entry["id"]] = tagged
            changed += 1
        elif status == "unusable":
            unusable += 1
        elif status == "missing_image":
            missing += 1
        elif status == "error":
            errors += 1
        if log:
            log(f"{entry.get('id')}: {status}")
        if changed and done % SAVE_EVERY == 0:
            ag_sources.save_index(data_dir, index)
    if changed and not dry_run:
        ag_sources.save_index(data_dir, index)
    report.update({"done": done, "changed": changed, "unusable": unusable,
                   "missing_image": missing, "errors": errors,
                   "usage": {k: (round(v, 6) if isinstance(v, float) else v)
                             for k, v in totals.items()}})
    report["cost_per_entry"] = (round(totals["cost"] / changed, 6)
                                if changed else None)
    return report


def measure_pool(data_dir: Path) -> dict:
    """Tag coverage of the pool, no model call."""
    sources = (ag_sources.load_index(data_dir).get("sources") or {})
    tagged = [e for e in sources.values() if not needs_tags(e)]
    by_people = {}
    by_scene = {}
    by_medium = {}
    for entry in tagged:
        tags = entry.get("tags") or {}
        by_people[tags.get("people")] = by_people.get(tags.get("people"), 0) + 1
        by_scene[tags.get("scene")] = by_scene.get(tags.get("scene"), 0) + 1
        by_medium[tags.get("medium")] = by_medium.get(tags.get("medium"), 0) + 1
    return {"sources": len(sources), "tagged": len(tagged),
            "untagged": len(sources) - len(tagged),
            "by_people": dict(sorted(by_people.items())),
            "by_scene": dict(sorted(by_scene.items())),
            "by_medium": dict(sorted(by_medium.items()))}


# ── The join: scene -> pool source ─────────────────────────────────────────


def scene_sources(data_dir: Path, index: dict, state: dict) -> dict:
    """``{scene_id: pool source id}`` for every scene we can resolve.

    The trace file is authoritative (it names the exact source the generator
    drew); the scene's ``sourceUrl`` against the pool's ``fileUrl`` is the
    fallback for a scene whose trace was pruned.
    """
    sources = (index.get("sources") or {})
    by_url = {}
    for source_id, entry in sources.items():
        url = str(entry.get("fileUrl") or "")
        if url:
            by_url.setdefault(url, source_id)
    out = {}
    for scene_id, scene in (state.get("scenes") or {}).items():
        trace = data_dir / "traces" / f"{scene_id}.json"
        source_id = ""
        if trace.exists():
            try:
                source_id = str(json.loads(trace.read_text()).get("source") or "")
            except (OSError, ValueError):
                source_id = ""
        if source_id not in sources:
            url = str(scene.get("sourceUrl")
                      or (scene.get("source") or {}).get("fileUrl") or "")
            source_id = by_url.get(url, "")
        if source_id:
            out[scene_id] = source_id
    return out


def tags_by_source(index: dict) -> dict:
    """``{source_id: tags}`` for the entries that carry a complete set."""
    out = {}
    for source_id, entry in (index.get("sources") or {}).items():
        tags = entry.get("tags")
        if isinstance(tags, dict) and parse_tags(tags) is not None:
            out[source_id] = tags
    return out


def scene_family(scene: dict) -> str:
    """The scene's element family, "other" when neither field nor catalogue knows.

    Ticket #2348: a scene stores the family its label resolved to at build
    time, and a model-invented label used to fall through to "other" even
    when the keyword table could bucket it later. The stored value wins
    unless it is that fallback, so an old "other" scene re-resolves with the
    wider keyword table instead of keeping the report coarse.
    """
    family = str((scene or {}).get("family") or "").strip()
    if family and family != "other":
        return family
    return (ag_catalog.family_of(str((scene or {}).get("anomaly") or ""))
            or family or "other")


def _empty_row() -> dict:
    return {"accepted": 0, "rejected": 0, "great": 0}


def _finish(row: dict) -> dict:
    decided = row["accepted"] + row["rejected"]
    row["decided"] = decided
    row["rate"] = round(row["accepted"] / decided, 4) if decided else None
    row["greatRate"] = round(row["great"] / decided, 4) if decided else None
    return row


def tally(feedback: dict, state: dict, source_of: dict, tags_of: dict,
          feature: str) -> dict:
    """``{(family, value): row}`` joining verdicts with the tags of the source.

    A scene counts once per verdict kind (accepted, rejected, great); a scene
    without a resolvable source or without tags is left out, so every row's
    ``decided`` is its real sample size.
    """
    accepted = feedback.get("accepted") or {}
    rejected = feedback.get("rejected") or {}
    great = feedback.get("great") or {}
    rows: dict = {}
    for scene_id, scene in (state.get("scenes") or {}).items():
        source_id = source_of.get(scene_id)
        tags = tags_of.get(source_id) if source_id else None
        if not tags:
            continue
        value = tags.get(feature)
        if not value:
            continue
        key = (scene_family(scene), value)
        row = rows.setdefault(key, _empty_row())
        if scene_id in accepted:
            row["accepted"] += 1
        if scene_id in rejected:
            row["rejected"] += 1
        if scene_id in great:
            row["great"] += 1
    return {key: _finish(dict(row)) for key, row in rows.items()}


def by_family(rows: dict) -> dict:
    """``{family: {value: row}}`` from a flat ``(family, value)`` tally."""
    out: dict = {}
    for (family, value), row in rows.items():
        out.setdefault(family, {})[value] = row
    return out


def overall_rows(families: dict) -> dict:
    """``{value: row}`` summing a feature's rows over every family."""
    out: dict = {}
    for rows in families.values():
        for value, row in rows.items():
            agg = out.setdefault(value, _empty_row())
            for key in ("accepted", "rejected", "great"):
                agg[key] += int(row.get(key) or 0)
    return {value: _finish(dict(row)) for value, row in out.items()}



def build_report(index: dict, state: dict, feedback: dict, source_of: dict,
                 generated_at: str | None = None) -> dict:
    """The measurement: per-family rates against each report feature."""
    tags_of = tags_by_source(index)
    features = {}
    for feature in REPORT_FEATURES:
        features[feature] = by_family(
            tally(feedback, state, source_of, tags_of, feature))
    scenes = state.get("scenes") or {}
    resolved = sum(1 for sid in scenes if source_of.get(sid) in tags_of)
    verdict_ids = (set(feedback.get("accepted") or {})
                   | set(feedback.get("rejected") or {}))
    resolved_verdicts = sum(1 for sid in verdict_ids
                            if source_of.get(sid) in tags_of)
    return {
        "generatedAt": generated_at or _now_iso(),
        "sources": {"total": len(index.get("sources") or {}),
                    "tagged": len(tags_of)},
        "scenes": {"total": len(scenes), "resolved": resolved,
                   "with_verdicts": len(verdict_ids),
                   "verdicts_resolved": resolved_verdicts},
        "features": features,
    }


# ── The markdown ───────────────────────────────────────────────────────────

_VALUE_ORDER = {"people": PEOPLE, "clutter": CLUTTER, "medium": MEDIUMS}
_FEATURE_TITLE = {"people": "Crowd level", "clutter": "Clutter",
                  "medium": "Medium"}


def _fmt_rate(row: dict, field: str) -> str:
    value = row.get(field)
    return "-" if value is None else f"{round(100 * value)}%"


def _overall_table(families: dict, feature: str) -> list:
    """The feature's rate over every family together (the headline split)."""
    rows = overall_rows(families)
    lines = [f"### {_FEATURE_TITLE[feature]}: all families", "",
             "| " + _FEATURE_TITLE[feature].lower() + " | decided | accepted "
             "| rate | great | great rate |",
             "|---|---|---|---|---|---|"]
    for value in _VALUE_ORDER[feature]:
        row = rows.get(value) or {}
        decided = int(row.get("decided") or 0)
        if not decided:
            continue
        lines.append(f"| {value} | {decided} | {row['accepted']} "
                     f"| {_fmt_rate(row, 'rate')} | {row['great']} "
                     f"| {_fmt_rate(row, 'greatRate')} |")
    lines.append("")
    return lines


def _feature_table(families: dict, feature: str) -> list:
    cols = (["family"] + list(_VALUE_ORDER[feature])
            + ["decided", "accepted", "rate", "great", "great rate"])
    lines = [f"### {_FEATURE_TITLE[feature]} x element family", "",
             "| " + " | ".join(cols) + " |",
             "|" + "---|" * len(cols)]
    for family in sorted(families):
        rows = families[family]
        counts = []
        decided = accepted = great = 0
        for value in _VALUE_ORDER[feature]:
            row = rows.get(value) or {}
            decided += int(row.get("decided") or 0)
            accepted += int(row.get("accepted") or 0)
            great += int(row.get("great") or 0)
            counts.append(str(int(row.get("decided") or 0)))
        if not decided:
            continue
        rate = round(100 * accepted / decided) if decided else 0
        grate = round(100 * great / decided) if decided else 0
        lines.append(f"| {family} | " + " | ".join(counts)
                     + f" | {decided} | {accepted} | {rate}% | {great} "
                     f"| {grate}% |")
    lines.append("")
    lines.append("Cells are the number of decided scenes behind that "
                 "family and value; the rate is over the family's row.")
    lines.append("")
    return lines


def render_report(report: dict) -> str:
    """The report as the markdown body of the wiki page."""
    lines = [
        "# AnomalyGuessr photo tags: which anomalies fit which picture",
        "",
        f"Generated {report['generatedAt']} from "
        f"{report['sources']['tagged']} tagged pool sources and "
        f"{report['scenes']['verdicts_resolved']} resolved verdicts.",
        "",
        "## What this is",
        "",
        "Ticket #2344: every source photo carries a small fixed tag set "
        "(people, scene, medium, clutter, host, text) read by one vision call, "
        "so the pipeline can measure which element families work on which "
        "kind of photograph. Ticket #2348 wires the two weak signals the "
        "measurement supports into the generator: the proposal prompt is told "
        "to prefer a modification on a busy surface, and the source picker "
        "prefers busy, populated, non-sepia photographs.",
        "",
        "## Method",
        "",
        "- Tags come from `pipeline/ag_tags.py tag`, one call per pool entry, "
        "cached on the entry and skipped when present.",
        "- Verdicts are the human accept/reject/great marks in "
        "`feedback.json`. A scene joins its pool source through its generation "
        "trace, then through its `sourceUrl` against the pool's `fileUrl`.",
        "- A cell's sample size is the number of decided scenes behind it; a "
        "rate without a sample size is not read as a finding.",
        "",
        "## Coverage",
        "",
        f"- Pool sources tagged: {report['sources']['tagged']} of "
        f"{report['sources']['total']}.",
        f"- Scenes resolved to a tagged source: {report['scenes']['resolved']} "
        f"of {report['scenes']['total']}.",
        f"- Verdicts resolved to a tagged source: "
        f"{report['scenes']['verdicts_resolved']} of "
        f"{report['scenes']['with_verdicts']}.",
        "",
    ]
    for feature in REPORT_FEATURES:
        lines += _overall_table(report["features"][feature], feature)
        lines += _feature_table(report["features"][feature], feature)
    lines += _hypothesis_section(report)
    return "\n".join(lines) + "\n"


def _hypothesis_section(report: dict) -> list:
    """The crowded-photo answer: the overall split and the person family."""
    people = report["features"].get("people") or {}
    overall = overall_rows(people)
    person = people.get("person") or {}

    def _line(label, row):
        decided = int((row or {}).get("decided") or 0)
        if not decided:
            return f"- {label}: no decided scenes."
        return (f"- {label}: {row['accepted']}/{decided} accepted "
                f"({_fmt_rate(row, 'rate')}), {row['great']} great "
                f"({_fmt_rate(row, 'greatRate')}).")

    lines = ["## The hypothesis", "",
             "Evan's guess: a crowded photo forgives an ostentatious time "
             "traveller more than an empty one.", "",
             "All element families together:", ""]
    for value in PEOPLE:
        lines.append(_line(f"People = {value}", overall.get(value)))
    lines += ["", "The person family alone (the literal time traveller):", ""]
    for value in PEOPLE:
        lines.append(_line(f"People = {value}", person.get(value)))
    lines.append("")
    return lines


# ── CLI ────────────────────────────────────────────────────────────────────


def parse_args(argv=None):
    p = argparse.ArgumentParser(prog="ag_tags.py")
    p.add_argument("--data", type=Path, default=ag_sources.default_data_dir())
    p.add_argument("--env", default=None, help=".env path for the API key")
    p.add_argument("--model", default=DEFAULT_MODEL)
    p.add_argument("--base-url", default=DEFAULT_BASE_URL)
    sub = p.add_subparsers(dest="cmd", required=True)
    p_tag = sub.add_parser("tag", help="tag pool entries with one vision call")
    p_tag.add_argument("--limit", type=int, default=None)
    p_tag.add_argument("--dry-run", action="store_true")
    sub.add_parser("status", help="tag coverage of the pool, no model call")
    p_rep = sub.add_parser("report", help="join tags with verdicts")
    p_rep.add_argument("--out", type=Path, default=None)
    return p.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    if args.cmd == "status":
        print(json.dumps(measure_pool(args.data), indent=2))
        return 0
    if args.cmd == "report":
        index = ag_sources.load_index(args.data)
        state = ag_queue.load_state(args.data)
        feedback = ag_queue.load_feedback(args.data)
        report = build_report(index, state, feedback,
                              scene_sources(args.data, index, state))
        markdown = render_report(report)
        if args.out:
            Path(args.out).write_text(markdown)
            print(json.dumps({k: v for k, v in report.items()
                              if k != "features"}, indent=2))
        else:
            print(markdown)
        return 0
    if args.env:
        import ag_verify
        ag_verify.load_env(Path(args.env))
    report = backfill(args.data, os.environ.get("OPENROUTER_API_KEY", ""),
                      args.model, args.base_url, limit=args.limit,
                      dry_run=args.dry_run,
                      log=lambda m: print(m, file=sys.stderr))
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
