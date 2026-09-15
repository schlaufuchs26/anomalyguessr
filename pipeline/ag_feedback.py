#!/usr/bin/env python3
"""AnomalyGuessr nightly feedback pass (ticket #1538).

The generator already learns from a few narrow signals on its own: the image
provider's refusals (#1439) and the reviewer's rejected labels (#1537). This
pass is the missing general one. It reads the moderation verdicts from
``state.json`` + ``feedback.json``, computes acceptance rates *with sample
sizes* over a window, and turns the countable part into adjustments the
generator reads on its next run. Everything it applies is written to
``adaptation.json`` with the counts that caused it and its before/after
value, so a single change can be reverted from one line.

What it may apply on its own, all bounded (ticket #1538):

- the avoid list of labels (#1537's rule) and of families (a family rejected
  in at least ``FAMILY_MIN_SAMPLE`` verdicts, more than
  ``FAMILY_REJECT_SHARE`` of them);
- the proposal prompt's few-shot examples: an example whose family has a
  poor record is dropped, so the model stops seeing the shape that keeps
  failing;
- at most ``MAX_KNOBS`` numeric knobs per run from ``KNOB_RANGES``, each
  clamped to its documented range.

What it never does: rewrite the prompt, invent rules, or change a
deterministic gate's threshold. Anything in that class is reported as a
``proposals`` entry for Evan instead of applied.

The metric is the acceptance rate over the window with its sample size,
never the checker score ([[anomalyguessr-checks]]): the checker separates
Evan's verdicts worse than always rejecting, so it is not a target.

Usage:

    ag_feedback.py --data DIR [--window 14] [--env .env]
                   [--dm-channel ID] [--dry-run] [--no-dm]
"""

import argparse
import datetime
import json
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import ag_catalog  # noqa: E402  (sibling module, resolved via sys.path)
import ag_generate as g  # noqa: E402
import ag_queue  # noqa: E402
import ag_sources  # noqa: E402
import ag_verify  # noqa: E402

# The generator's own loader owns the file name and the reader (ag_generate
# imports nothing from here, so this module imports it, never the reverse).
DEFAULT_WINDOW_DAYS = 14
# A rate is only trusted once this many verdicts back it. Rows below the
# floor are still reported with their sample size; nothing is applied from
# them.
MIN_SAMPLE = 5
# A family is avoided under the same shape as the #1537 label rule, just at
# family granularity (a family is the wider bucket, so the floor is higher).
FAMILY_MIN_SAMPLE = 5
FAMILY_REJECT_SHARE = 0.7
# A few-shot example is dropped when its own family is over this rejection
# share, so the model stops seeing the shape that keeps failing. The list has
# to keep enough examples to still show the shapes (a later-era object, a
# person, future tech); below this floor the full list stays.
EXAMPLE_REJECT_SHARE = 0.7
MIN_EXAMPLES = 3
# The numeric knobs an unsupervised run may move, with their (min, max)
# range. Two at most per run (ticket #1538).
KNOB_RANGES = {
    "repeat_window_days": (3, 21),
    "example_count": (3, 8),
}
MAX_KNOBS = 2
DEFAULT_EXAMPLE_COUNT = 6
# The window's own rejection share that widens the repeat memory: a day of
# mostly-rejected scenes says the generator is short of fresh material.
REGRESSION_REJECT_SHARE = 0.5
# How many "top movers" the DM digest names.
DIGEST_MOVERS = 3

DIMS = ("labels", "families", "archives", "decades", "placements")
_YEAR_RE = re.compile(r"(?<!\d)(1[89]\d{2}|20\d{2})(?!\d)")


# ── Bucket helpers ─────────────────────────────────────────────────────────

def _first_year(text) -> int | None:
    """The first four-digit year in a display string ("c. 1907" -> 1907)."""
    m = _YEAR_RE.search(str(text or ""))
    return int(m.group(1)) if m else None


def scene_archive(scene: dict) -> str:
    """The source archive a scene came from (its repository string)."""
    repo = ((scene or {}).get("source") or {}).get("repository") \
        or (scene or {}).get("credit")
    return str(repo or "unknown").strip() or "unknown"


def scene_decade(scene: dict) -> str:
    """The scene's catalogue decade ("1900s"), "unknown" without a year."""
    year = _first_year((scene or {}).get("year"))
    return f"{year // 10 * 10}s" if year else "unknown"


def scene_placement(scene: dict) -> str:
    """The proposal's placement kind; "unknown" for scenes from before #1538."""
    return str((scene or {}).get("placement_kind") or "unknown")


def _label_pair(scene: dict):
    """(normalized label, reader-facing label) or None when there is none."""
    label = str((scene or {}).get("anomaly") or "").strip()
    norm = g.normalized_label(label)
    return (norm, label) if norm else None


def _family_pair(scene: dict):
    fam = ag_catalog.family_of(str((scene or {}).get("anomaly") or ""))
    return (fam, fam) if fam else None


def _plain_pair(getter):
    def pair(scene):
        value = getter(scene)
        return (value, value)
    return pair


BUCKETS = {
    "labels": _label_pair,
    "families": _family_pair,
    "archives": _plain_pair(scene_archive),
    "decades": _plain_pair(scene_decade),
    "placements": _plain_pair(scene_placement),
}


# ── The tally ──────────────────────────────────────────────────────────────

def _rate(accepted: int, rejected: int):
    total = accepted + rejected
    return round(accepted / total, 4) if total else None


def _new_row(key: str) -> dict:
    return {"key": key, "accepted": 0, "rejected": 0, "commentedRejected": 0,
            "commentedAccepted": 0, "last": ""}


def commented_scene_ids(feedback: dict) -> set:
    """Ids that carry at least one reviewer comment (ticket #1538).

    A commented rejection is a different signal from a silent one: the
    comment usually names a fixable defect, the silence usually "not fun".
    """
    return {str(sid) for sid, comments in (feedback.get("comments") or {}).items()
            if comments}


def _dimension(accepted, rejected, scenes, cutoff, pair_of,
               commented_ids) -> dict:
    rows: dict = {}
    for verdicts, field in ((accepted, "accepted"), (rejected, "rejected")):
        for scene_id, at in (verdicts or {}).items():
            when = str(at or "")[:10]
            if when < cutoff:
                continue
            scene = (scenes or {}).get(scene_id)
            if not scene:
                continue
            pair = pair_of(scene)
            if not pair:
                continue
            key, display = pair
            row = rows.setdefault(key, _new_row(display))
            row[field] += 1
            if str(scene_id) in commented_ids:
                row["commentedRejected" if field == "rejected"
                    else "commentedAccepted"] += 1
            if when > row["last"]:
                row["last"] = when
    for row in rows.values():
        row["total"] = row["accepted"] + row["rejected"]
        row["rate"] = _rate(row["accepted"], row["rejected"])
    return rows


def acceptance_stats(scenes, feedback, window_days: int = DEFAULT_WINDOW_DAYS,
                     today=None) -> dict:
    """Acceptance rates with sample sizes over the window (ticket #1538).

    Every dimension is tallied independently from the same verdicts: per
    normalized element label, family, source archive, catalogue decade and
    placement kind, each row split by whether the verdict carried a comment.
    """
    day = today or datetime.date.today()
    cutoff = (day - datetime.timedelta(days=window_days)).isoformat()
    accepted = (feedback.get("accepted") or {})
    rejected = (feedback.get("rejected") or {})
    commented = commented_scene_ids(feedback)
    out = {"windowDays": int(window_days)}
    for dim in DIMS:
        out[dim] = _dimension(accepted, rejected, scenes, cutoff,
                              BUCKETS[dim], commented)
    kept = {sid: at for sid, at in accepted.items()
            if str(at or "")[:10] >= cutoff}
    dropped = {sid: at for sid, at in rejected.items()
               if str(at or "")[:10] >= cutoff}
    out["sample"] = {
        "total": len(kept) + len(dropped),
        "accepted": len(kept),
        "rejected": len(dropped),
        "commentedRejected": len(set(dropped) & commented),
        "commentedAccepted": len(set(kept) & commented),
        "rate": _rate(len(kept), len(dropped)),
    }
    return out


# ── What the pass applies ──────────────────────────────────────────────────

def _reject_share(row) -> float:
    total = int(row.get("total") or 0)
    return (int(row.get("rejected") or 0) / total) if total else 0.0


def blocked_label_rows(stats) -> list:
    """Label rows #1537's rule blocks, newest verdict first."""
    rows = [(row.get("key") or "", row)
            for row in (stats.get("labels") or {}).values()
            if g.label_blocked(row)]
    rows.sort(key=lambda r: str(r[1].get("last") or ""), reverse=True)
    return [row for label, row in rows if label]


def blocked_family_rows(stats, min_sample: int = FAMILY_MIN_SAMPLE,
                        share: float = FAMILY_REJECT_SHARE) -> list:
    """Family rows with enough verdicts and a lopsided rejection record."""
    rows = [row for row in (stats.get("families") or {}).values()
            if int(row.get("total") or 0) >= min_sample
            and _reject_share(row) >= share]
    rows.sort(key=lambda r: _reject_share(r), reverse=True)
    return rows


def example_label(line: str) -> str:
    """The catalog label at the head of a few-shot example line."""
    return str(line).split(";", 1)[0].strip()


def filter_examples(examples, avoid_families, count: int):
    """(kept, dropped): drop the examples whose family is avoided (#1538).

    ``count`` caps the kept list from the head. The caller decides what to do
    when too few survive; this function only reports what would go.
    """
    avoid = {str(f) for f in (avoid_families or ())}
    kept, dropped = [], []
    for line in examples or ():
        family = ag_catalog.family_of(example_label(line))
        if family and family in avoid:
            dropped.append({"example": line, "family": family})
        else:
            kept.append(line)
    cap = max(1, int(count))
    return (kept[:cap] if len(kept) > cap else kept), dropped


def _clamp(name: str, value: int) -> int:
    low, high = KNOB_RANGES[name]
    return max(low, min(high, int(value)))


def choose_knobs(stats, dropped_examples, current=None) -> dict:
    """At most ``MAX_KNOBS`` numeric knobs, clamped and count-backed.

    Two policies, both from the window's own numbers:

    - a window rejected more often than ``REGRESSION_REJECT_SHARE`` widens
      ``repeat_window_days`` (a bad streak says the avoid memory is too
      short);
    - when half or more of the few-shot examples lost their family, the
      kept ``example_count`` drops, so a long list of untrustworthy shapes
      is not shown.
    """
    current = dict(current or {})
    week = stats.get("sample") or {}
    total = int(week.get("total") or 0)
    knobs = {}
    base = int(current.get("repeat_window_days") or g.REPEAT_WINDOW_DAYS)
    if total >= MIN_SAMPLE and (int(week.get("rejected") or 0) / total
                                >= REGRESSION_REJECT_SHARE):
        # Monotone up: a bad window widens the avoid memory and keeps it
        # wide; a healthy window leaves the current value alone.
        base = max(base, 14)
    knobs["repeat_window_days"] = _clamp("repeat_window_days", base)
    examples = 4 if len(dropped_examples) * 2 >= DEFAULT_EXAMPLE_COUNT \
        else DEFAULT_EXAMPLE_COUNT
    base = int(current.get("example_count") or DEFAULT_EXAMPLE_COUNT)
    knobs["example_count"] = _clamp("example_count", min(base, examples))
    return knobs


def _knob_change(name, before, after, reason):
    return {"kind": "knob", "key": name, "before": before, "after": after,
            "reason": reason}


def adapt(stats, examples=None, previous_knobs=None) -> dict:
    """Turn the stats into the bounded changes this run applies (#1538)."""
    examples = list(examples if examples is not None
                    else ag_catalog.inspiration_lines())
    labels = blocked_label_rows(stats)
    families = blocked_family_rows(stats)
    avoid_families = [row["key"] for row in families]
    kept, dropped = filter_examples(examples, avoid_families,
                                    DEFAULT_EXAMPLE_COUNT)
    # The prompt must keep enough examples to hint at the range of shapes (a
    # later-era object, a person, future tech). When dropping would leave
    # fewer than MIN_EXAMPLES, nothing drops: the finding goes to Evan as a
    # proposal instead of starving the prompt, which would be a rule change.
    starved = None
    if dropped and len(kept) < MIN_EXAMPLES:
        starved = {
            "kind": "examples", "key": f"{len(dropped)} of {len(examples)}",
            "reason": f"dropping them would leave {len(kept)} example(s); "
                      "the list stays whole so the prompt still shows the "
                      "range of shapes"}
        kept, dropped = list(examples), []
    knobs = choose_knobs(stats, dropped, previous_knobs)
    changes = []
    for row in labels:
        changes.append({
            "kind": "avoid_label", "key": row["key"],
            "reason": f"{row['accepted']} accepted, {row['rejected']} "
                      f"rejected over the window",
            "before": False, "after": True})
    for row in families:
        changes.append({
            "kind": "avoid_family", "key": row["key"],
            "reason": f"{row['rejected']}/{row['total']} rejected "
                      f"({_pct(_reject_share(row))})",
            "before": False, "after": True})
    for item in dropped:
        changes.append({
            "kind": "drop_example", "key": example_label(item["example"]),
            "reason": f"family {item['family']} is judged out",
            "before": "shown", "after": "dropped"})
    for name, after in knobs.items():
        before = int((previous_knobs or {}).get(name) or 0)
        if not before:
            before = {"repeat_window_days": g.REPEAT_WINDOW_DAYS,
                      "example_count": DEFAULT_EXAMPLE_COUNT}.get(name, after)
        if after != before:
            changes.append(_knob_change(name, before, after, _knob_reason(
                name, stats, dropped, before, after)))
    findings = proposals(stats)
    if starved:
        findings.insert(0, starved)
    return {
        "avoidLabels": [row["key"] for row in labels],
        "avoidFamilies": avoid_families,
        "examples": kept,
        "droppedExamples": dropped,
        "knobs": knobs,
        "changes": changes,
        "proposals": findings,
    }


def _knob_reason(name, stats, dropped, before, after) -> str:
    if name == "repeat_window_days":
        week = stats.get("sample") or {}
        return (f"the window rejected {week.get('rejected')}/"
                f"{week.get('total')} scenes, so the avoid memory widens")
    return (f"{len(dropped)} of {DEFAULT_EXAMPLE_COUNT} examples lost their "
            f"family, so fewer shapes are shown")


def _pct(value) -> str:
    return f"{round(100 * float(value))}%"


def proposals(stats) -> list:
    """Findings the pass must not apply itself (ticket #1538).

    A source archive, a decade or a placement kind with a bad record is a
    real signal, but acting on it means changing what the pipeline sources
    or how it frames, which is a human decision. So is a rule change: they
    all come out here for Evan, never as a live edit.
    """
    out = []
    for dim in ("archives", "decades", "placements"):
        for row in (stats.get(dim) or {}).values():
            if str(row.get("key")) in ("", "unknown"):
                continue
            if int(row.get("total") or 0) >= MIN_SAMPLE \
                    and _reject_share(row) >= FAMILY_REJECT_SHARE:
                out.append({
                    "kind": dim, "key": row["key"],
                    "reason": f"{row['rejected']}/{row['total']} rejected "
                              f"({_pct(_reject_share(row))}); a source or "
                              "framing change, not a bounded adjustment"})
    for row in (stats.get("labels") or {}).values():
        if int(row.get("total") or 0) < g.BLOCK_MIN_VERDICTS \
                and int(row.get("commentedRejected") or 0) >= 2:
            out.append({
                "kind": "labels", "key": row["key"],
                "reason": f"{row['commentedRejected']} commented rejections, "
                          "below the block floor; watch it"})
    return out


# ── Digest + run ───────────────────────────────────────────────────────────

def top_movers(stats, n: int = DIGEST_MOVERS) -> list:
    """The worst label/family records with a trustworthy sample."""
    rows = []
    for dim in ("labels", "families"):
        for row in (stats.get(dim) or {}).values():
            if int(row.get("total") or 0) >= MIN_SAMPLE:
                rows.append((dim, row))
    rows.sort(key=lambda pair: (_reject_share(pair[1]),
                                -int(pair[1].get("total") or 0)), reverse=True)
    return [row for _dim, row in rows[:n]]


def digest(report: dict) -> str:
    """A short DM digest: sample, top movers, applied changes."""
    sample = report.get("sample") or {}
    head = (f"AnomalyGuessr feedback {str(report.get('generatedAt'))[:10]} "
            f"({report.get('windowDays')}d window): "
            f"{sample.get('accepted')}/{sample.get('total')} accepted")
    rate = sample.get("rate")
    if rate is not None:
        head += f" ({_pct(rate)})"
    lines = [head]
    movers = top_movers(report.get("rates") or {})
    if movers:
        lines.append("Worst records: " + "; ".join(
            f"{row['key']} {row['rejected']}/{row['total']} "
            f"({_pct(_reject_share(row))})" for row in movers))
    lines.append(_applied_line(report.get("changes") or []))
    props = report.get("proposals") or []
    if props:
        lines.append(f"{len(props)} finding(s) for you: "
                     + ", ".join(f"{p['kind']} {p['key']}" for p in props[:4]))
    return "\n".join(lines)


def _applied_line(changes) -> str:
    """One line naming what was applied, grouped by kind."""
    if not changes:
        return "Applied: nothing; no signal crossed a floor."
    by: dict = {}
    for change in changes:
        by.setdefault(change["kind"], []).append(change)
    parts = []
    for kind, label in (("avoid_label", "labels avoided"),
                        ("avoid_family", "families avoided")):
        items = by.get(kind) or []
        if not items:
            continue
        names = ", ".join(str(c["key"]) for c in items[:4])
        more = "" if len(items) <= 4 else f" +{len(items) - 4}"
        parts.append(f"{len(items)} {label}: {names}{more}")
    for change in by.get("drop_example") or []:
        parts.append(f"example dropped: {change['key']}")
    for change in by.get("knob") or []:
        parts.append(f"knob {change['key']} {change['before']}\u2192"
                     f"{change['after']}")
    return "Applied: " + "; ".join(parts) + "."


def _atomic_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n")
    os.replace(tmp, path)


def run(data_dir, window_days: int = DEFAULT_WINDOW_DAYS, dry_run: bool = False,
        now=None) -> dict:
    """Read the verdicts, adapt, and write ``adaptation.json`` (#1538)."""
    data_dir = Path(data_dir)
    state = ag_queue.load_state(data_dir)
    feedback = ag_queue.load_feedback(data_dir)
    previous = g.load_adaptation(data_dir)
    now = now or datetime.datetime.now().astimezone()
    stats = acceptance_stats(state.get("scenes") or {}, feedback,
                             window_days, today=now.date())
    applied = adapt(stats, previous_knobs=previous.get("knobs"))
    report = {
        "version": 1,
        "generatedAt": now.isoformat(timespec="seconds"),
        "windowDays": int(window_days),
        "metric": "acceptance rate over the window (total = sample size)",
        "sample": stats["sample"],
        "rates": {dim: stats[dim] for dim in DIMS},
        "avoidLabels": applied["avoidLabels"],
        "avoidFamilies": applied["avoidFamilies"],
        "examples": applied["examples"],
        "droppedExamples": applied["droppedExamples"],
        "knobs": applied["knobs"],
        "changes": applied["changes"],
        "proposals": applied["proposals"],
    }
    if not dry_run:
        _atomic_json(data_dir / g.ADAPTATION_NAME, report)
    return report


def parse_args(argv=None):
    p = argparse.ArgumentParser(
        prog="ag_feedback.py",
        description="AnomalyGuessr feedback pass (#1538): turn the review "
                    "verdicts into bounded generator adjustments.")
    p.add_argument("--data", default=None,
                   help="data dir (default: repo data/anomalyguessr)")
    p.add_argument("--window", type=int, default=DEFAULT_WINDOW_DAYS,
                   help=f"verdict window in days (default "
                        f"{DEFAULT_WINDOW_DAYS})")
    p.add_argument("--dry-run", action="store_true",
                   help="compute and print, write nothing and send no DM")
    p.add_argument("--env", default=None, help=".env path for the bot token")
    p.add_argument("--dm-channel", default=None,
                   help="Discord channel for the digest")
    p.add_argument("--no-dm", action="store_true",
                   help="do not send the digest even with --dm-channel")
    return p.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    data_dir = Path(args.data) if args.data else ag_sources.default_data_dir()
    report = run(data_dir, window_days=args.window, dry_run=args.dry_run)
    print(json.dumps(report, indent=2, ensure_ascii=False))
    text = digest(report)
    if args.dm_channel and not args.no_dm and not args.dry_run:
        if args.env:
            ag_verify.load_env(Path(args.env))
        g.notify_discord(args.dm_channel, text,
                         os.environ.get("DISCORD_BOT_TOKEN", ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
