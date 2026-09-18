#!/usr/bin/env python3
"""Catalog/scene reference audit for AnomalyGuessr (ticket #1624).

Walks the curated catalog (``ag_catalog.CATALOG``) and, on request, a sample
of recent scenes from the queue's ``state.json``; fetches every reference URL
and runs :func:`ag_references.check_reference`. Prints the share of
references that support their claim, the failure modes, and the entries that
need a better source or a softened sentence.

Read-only, no model calls. Network access is required (one request per
reference); the audit is the measuring tool behind the #1624 baseline and the
after-number.

CLI::

    ag_reference_audit.py [--scenes N] [--data DIR] [--json] [--timeout S]
"""

import argparse
import json
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import ag_catalog  # noqa: E402
import ag_references  # noqa: E402


def catalog_items() -> list:
    """(name, explanation, references) for every curated catalog entry."""
    return [(e["label"], e["explanation"], e["references"])
            for e in ag_catalog.CATALOG]


def scene_items(data_dir: Path, limit: int) -> list:
    """The ``limit`` most recently added scenes as audit items."""
    path = data_dir / "state.json"
    if not path.exists():
        return []
    with open(path, encoding="utf-8") as f:
        scenes = json.load(f).get("scenes", {})
    ordered = sorted(scenes.values(), key=lambda s: str(s.get("added") or ""))
    out = []
    for scene in ordered[-limit:]:
        refs = scene.get("references") or []
        if not refs:
            continue
        out.append((scene.get("id") or scene.get("title") or "?",
                    scene.get("explanation") or "", refs))
    return out


def audit(items, fetch, timeout: int) -> dict:
    """Run the check over ``items`` and aggregate by status and coverage."""
    rows = []
    status_count = {}
    uncovered_count = {}
    total = 0
    for name, explanation, refs in items:
        finding = ag_references.reference_finding(
            explanation, refs,
            fetch=lambda url, _f=fetch: _f(url, timeout))
        for c in finding["references"]:
            status_count[c["status"]] = status_count.get(c["status"], 0) + 1
            total += 1
        for token in finding["uncovered"]:
            uncovered_count[token] = uncovered_count.get(token, 0) + 1
        rows.append({"name": name, "explanation": explanation,
                     "references": finding["references"],
                     "uncovered": finding["uncovered"],
                     "failed": finding["failed"]})
    bad_items = [r for r in rows if r["failed"]]
    return {"items": len(items), "references": total,
            "failing_items": len(bad_items), "status_count": status_count,
            "uncovered_count": uncovered_count, "rows": rows}


def print_report(report: dict, label: str) -> None:
    print(f"# {label}: {report['items']} items, {report['references']} "
          f"references, {report['failing_items']} items failing")
    for status, n in sorted(report["status_count"].items()):
        print(f"  {status}: {n}")
    if report["uncovered_count"]:
        print("  uncovered claims: " + ", ".join(
            f"{t} x{n}" for t, n in sorted(report["uncovered_count"].items())))
    print()
    for row in report["rows"]:
        if not row["failed"]:
            continue
        print(f"- {row['name']}")
        for c in row["references"]:
            if c["status"] != "ok":
                print(f"    {c['status']}: {c['url']}")
        if row["uncovered"]:
            print("    uncovered: " + ", ".join(row["uncovered"]))


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--scenes", type=int, default=0,
                   help="also audit the N most recently added scenes")
    p.add_argument("--data", default="", help="queue data dir (default: repo data/)")
    p.add_argument("--json", action="store_true")
    p.add_argument("--timeout", type=int,
                   default=ag_references.FETCH_TIMEOUT)
    args = p.parse_args(argv)

    items = list(catalog_items())
    if args.scenes:
        import ag_queue
        data_dir = Path(args.data) if args.data else ag_queue.default_data_dir()
        items += scene_items(data_dir, args.scenes)

    report = audit(items, lambda url, t: ag_references.fetch_page(url, t),
                   args.timeout)
    if args.json:
        print(json.dumps(report, indent=2))
    else:
        print_report(report, "AnomalyGuessr reference audit")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
