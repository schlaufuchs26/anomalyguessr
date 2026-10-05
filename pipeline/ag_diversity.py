#!/usr/bin/env python3
"""Per-week diversity report for the AnomalyGuessr queue (ticket #2343).

Answers one question from the recorded queue, no model calls: how varied are
the anomalies the generator actually ships? The reviewer kept seeing the same
handful of objects, so this report makes the repetition measurable instead of
an impression. Per calendar week it prints the scene count, the distinct-label
count, the share of scenes taken by the ten most common element families and
the exact repeats, so the week before and after a generator change can be
compared on the same numbers.

CLI::

    ag_diversity.py [--data DIR] [--weeks N] [--json]

``--json`` prints the raw report; the default is a scannable text table. The
report is read-only: it never writes to the queue.
"""

import argparse
import datetime
import json
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import ag_catalog
import ag_queue

# How many top families the share is measured over (ticket #2343).
TOP_FAMILIES = 10
# Default number of calendar weeks the report covers, current week included.
DEFAULT_WEEKS = 4


def scene_day(scene) -> datetime.date | None:
    """The date a scene was added, None when the field is missing or bad."""
    raw = str((scene or {}).get("added") or "")[:10]
    try:
        return datetime.date.fromisoformat(raw)
    except ValueError:
        return None


def week_key(day: datetime.date) -> str:
    """The ISO year-week of a date, e.g. ``2026-W40``."""
    year, week, _ = day.isocalendar()
    return f"{year}-W{week:02d}"


def week_start(day: datetime.date) -> datetime.date:
    """The Monday of the week the date falls in."""
    return day - datetime.timedelta(days=day.weekday())


def recent_weeks(today: datetime.date, weeks: int) -> list:
    """The week keys of the last ``weeks`` calendar weeks, oldest first."""
    start = week_start(today)
    keys = []
    for i in range(max(1, int(weeks))):
        keys.append(week_key(start - datetime.timedelta(weeks=i)))
    return list(reversed(keys))


def top_family_share(labels, top: int = TOP_FAMILIES) -> dict:
    """The share of scenes the ``top`` most common families take (#2343).

    Labels the catalog cannot bucket are counted as scenes but never as a
    family, so an unclassifiable element cannot hide a dominant family.
    Returns the families with their counts and the combined share.
    """
    total = len(labels)
    counts = Counter(ag_catalog.family_of(str(label))
                     for label in labels)
    counts.pop("", None)
    rows = counts.most_common(max(1, int(top)))
    covered = sum(n for _, n in rows)
    return {"families": [{"family": fam, "count": n} for fam, n in rows],
            "scenes": covered,
            "share": round(covered / total, 3) if total else 0.0}


def repeat_rows(labels, limit: int = 10) -> list:
    """Exact label repeats, most frequent first (ticket #2343).

    An "exact repeat" is the same anomaly string on more than one scene in
    the week; that is what the reviewer sees as the same object coming back.
    """
    counts = Counter(str(label) for label in labels if str(label))
    rows = [(label, n) for label, n in counts.items() if n >= 2]
    rows.sort(key=lambda r: (-r[1], r[0]))
    return [{"label": label, "count": n} for label, n in rows[:limit]]


def week_report(labels) -> dict:
    """The diversity numbers of one week's labels."""
    labels = [str(label) for label in labels if str(label)]
    share = top_family_share(labels)
    repeats = repeat_rows(labels)
    return {"scenes": len(labels),
            "distinct_labels": len(set(labels)),
            "top_family_share": share["share"],
            "top_families": share["families"],
            "repeats": repeats}


def diversity_report(data_dir: Path, today=None, weeks: int = DEFAULT_WEEKS) -> dict:
    """The per-week diversity report over the recorded queue (ticket #2343)."""
    day = today or datetime.date.today()
    try:
        state = ag_queue.load_state(data_dir)
    except (OSError, ValueError):
        state = {}
    buckets = {key: [] for key in recent_weeks(day, weeks)}
    for scene in state.get("scenes", {}).values():
        added = scene_day(scene)
        if added is None:
            continue
        key = week_key(added)
        if key in buckets:
            buckets[key].append(str(scene.get("anomaly") or ""))
    out = []
    for key in recent_weeks(day, weeks):
        row = week_report(buckets[key])
        row["week"] = key
        out.append(row)
    return {"updatedAt": datetime.datetime.now().astimezone().isoformat(
        timespec="seconds"), "weeks": out}


def render(report) -> str:
    """A scannable text table of the report."""
    lines = ["week       scenes  labels  top-10 share  repeats"]
    for row in report.get("weeks") or []:
        repeats = ", ".join(f"{r['label']} x{r['count']}"
                            for r in row["repeats"]) or "-"
        lines.append(f"{row['week']:<10} {row['scenes']:>6} "
                     f"{row['distinct_labels']:>6} "
                     f"{row['top_family_share'] * 100:>11.1f}%  {repeats}")
    return "\n".join(lines)


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="AnomalyGuessr diversity report")
    p.add_argument("--data", default=None,
                   help="data dir (default: the pipeline's own)")
    p.add_argument("--weeks", type=int, default=DEFAULT_WEEKS,
                   help="calendar weeks to cover, current week included")
    p.add_argument("--json", action="store_true", help="print the raw report")
    args = p.parse_args(argv)
    data_dir = Path(args.data) if args.data else ag_queue.default_data_dir()
    report = diversity_report(data_dir, weeks=args.weeks)
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        print(render(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
