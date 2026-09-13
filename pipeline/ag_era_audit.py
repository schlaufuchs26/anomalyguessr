#!/usr/bin/env python3
"""AnomalyGuessr impossibility audit (ticket #1403).

The game's bar is impossibility, not improbability: the planted element must
not exist in the photograph's year (a real later-era object, or something
explicitly futuristic). This audit cross-checks the queue's existing scenes
against that bar, because the reported e-scooter in a 2017 photograph shows
the old "unexpected for that year" bar admits elements the player can explain
away.

For every scene it resolves:

- the scene year: the source metadata anchor first (``source.date`` via
  ``ag_sources.anchor_year``), the displayed ``year`` field second;
- the element's introduction year: the catalog's ``min_year`` where the label
  matches exactly, a curated keyword table for the historic labels the
  catalog no longer carries, and "futuristic" for the robot/UFO families
  (they do not exist at all, so they pass this test).

Verdicts:

- ``impossible``: introduced strictly later than the scene year, or
  futuristic. Passes.
- ``same-year``: introduced in the scene's own year. Fails: a player can say
  it belongs.
- ``improbable``: existed before the scene year. Fails.
- ``unknown``: no introduction year could be resolved (needs review).

The scene's own ``explanation`` is reported as ``reason_year`` when it names
a year, so a claim that disagrees with the resolver is visible.

``--prune`` removes the failing scenes from the queue state; a scene that was
already shown (``shown`` is set, so its URL is live) is never pruned, only
reported. The default is report-only.

CLI::

    ag_era_audit.py [--data DIR] [--json] [--prune]

Run the tests with ``python3 -m unittest discover -s pipeline -t pipeline``.
"""

import argparse
import json
import re
import sys
from pathlib import Path

import ag_catalog
import ag_queue
import ag_sources

# Families that are not real objects at all: they read as fictional future
# travellers (or the kept-but-deprecated UFOs), so they are impossible in
# every real year by construction (#1161).
FUTURE_FAMILIES = ("robot", "ufo")

# Curated introduction years for labels the catalog no longer carries (the
# catalog is the generation-time reference and is deliberately small). The
# year is the earliest year the element existed somewhere in the world;
# matched against the lowercased anomaly label, first hit wins, so more
# specific phrases must come before their substrings (trail camera before
# solar-powered, paper cup before plastic bottle).
INTRO_RULES = (
    ("drone", 2010), ("quadcopter", 2010),
    ("gopro", 2004),
    ("bluetooth", 2000),
    ("qr code", 1994),
    ("smartphone", 2007),
    ("digital watch", 1972),
    ("trail camera", 2000),
    ("solar-powered", 1990),
    ("solar panel", 1954),
    ("e-scooter", 2015), ("electric scooter", 2015),
    ("electric kick scooter", 2015),
    ("roller skate", 1863),
    ("outboard motor", 1906),
    ("hard hat", 1919), ("safety helmet", 1919),
    ("shipping container", 1956),
    ("tetra pak", 1951), ("carton drink box", 1951),
    ("drink can", 1935),
    ("polystyrene", 1950),
    ("paper coffee cup", 1910), ("disposable coffee cup", 1910),
    ("plastic bottle", 1970), ("plastic water bottle", 1970),
    ("plastic bag", 1960),
    ("plastic bucket", 1950), ("plastic cooler", 1950),
    ("cooler box", 1950),
    ("chip bag", 1950),
    ("stackable plastic crate", 1960),
    ("plastic tarp", 1950),
    ("nylon rope", 1940),
    ("traffic cone", 1940),
    ("umbrella", 1950),
    ("daypack", 1960),
    ("suitcase", 1970),
    ("advertising poster", 1960),
    ("bicycle", 1975),
)

# The person family's modern tells (sneakers, sunglasses, headphones): the
# catalog anchors the time-traveller person at 1960.
PERSON_INTRO = 1960

_YEAR_RE = re.compile(r"(?<!\d)((?:1[5-9]|20)\d{2})(?!\d)")


def scene_anchor(scene: dict) -> tuple[int | None, str]:
    """(year, origin) for a scene: metadata anchor first, display second."""
    year, _field = ag_sources.anchor_year(scene.get("source") or {})
    if year is not None:
        return year, "metadata"
    m = _YEAR_RE.search(str(scene.get("year") or ""))
    if m:
        return int(m.group(1)), "display"
    return None, "unknown"


def resolve_intro(anomaly, family="") -> dict:
    """The element's introduction: {"year", "kind"} (kind = how resolved)."""
    label = str(anomaly or "")
    entry = ag_catalog.entry_for_label(label)
    if entry is not None:
        if entry["type"] == "future":
            return {"year": None, "kind": "futuristic"}
        if entry.get("min_year"):
            return {"year": int(entry["min_year"]), "kind": "catalog"}
    fam = ag_catalog.family_of(label) or str(family or "")
    if fam in FUTURE_FAMILIES:
        return {"year": None, "kind": "futuristic"}
    if fam == "person":
        return {"year": PERSON_INTRO, "kind": "family"}
    low = label.lower()
    for needle, year in INTRO_RULES:
        if needle in low:
            return {"year": year, "kind": "keyword"}
    return {"year": None, "kind": "unknown"}


def reason_year(scene: dict) -> int | None:
    """The year the scene's own explanation cites, None when it cites none."""
    m = _YEAR_RE.search(str(scene.get("explanation") or ""))
    return int(m.group(1)) if m else None


def judge(scene: dict) -> dict:
    """One scene's verdict against the impossibility bar."""
    year, origin = scene_anchor(scene)
    intro = resolve_intro(scene.get("anomaly"), scene.get("family"))
    m = _YEAR_RE.search(str(scene.get("year") or ""))
    apparent = int(m.group(1)) if m else None
    row = {"id": scene.get("id", ""), "anomaly": scene.get("anomaly", ""),
           "family": scene.get("family") or ag_catalog.family_of(
               scene.get("anomaly", "")),
           "year": year, "year_origin": origin,
           "apparent_year": apparent,
           # A metadata anchor that disagrees with the displayed era by more
           # than two years is suspicious: it may be an upload/scan stamp
           # (boundary case #3), and the anchor is trusted for the test per
           # the ticket. Such a row is reported, but never auto-pruned.
           "anchor_disagrees": (origin == "metadata" and apparent is not None
                                and abs(year - apparent) > 2),
           "intro_year": intro["year"], "intro_kind": intro["kind"],
           "reason_year": reason_year(scene),
           "shown": bool(scene.get("shown"))}
    if intro["kind"] == "futuristic":
        row["verdict"] = "impossible"
    elif year is None or intro["year"] is None:
        row["verdict"] = "unknown"
    elif intro["year"] > year:
        row["verdict"] = "impossible"
    elif intro["year"] == year:
        row["verdict"] = "same-year"
    else:
        row["verdict"] = "improbable"
    return row


def audit(data_dir: Path) -> dict:
    """Report every queued scene's verdict; the failing rows are listed."""
    state = ag_queue.load_state(data_dir)
    rows = [judge(s) for s in state["scenes"].values()]
    verdicts = {v: 0 for v in ("impossible", "same-year", "improbable",
                               "unknown")}
    origins = {v: 0 for v in ("metadata", "display", "unknown")}
    for row in rows:
        verdicts[row["verdict"]] += 1
        origins[row["year_origin"]] += 1
    failing = [r for r in rows if r["verdict"] in ("same-year", "improbable")]
    return {"total": len(rows), "verdicts": verdicts, "year_origin": origins,
            "failing": failing,
            "failing_prunable": [r["id"] for r in failing
                                 if not r["shown"] and not r["anchor_disagrees"]],
            "failing_shipped": [r["id"] for r in failing if r["shown"]],
            "disputed": [r["id"] for r in failing if r["anchor_disagrees"]],
            "unknown": [r for r in rows if r["verdict"] == "unknown"]}


def prune(data_dir: Path, report: dict) -> dict:
    """Remove the failing scenes that are unambiguous and not yet shown.

    A shown scene's URL is live, so it is never removed here. A scene whose
    metadata anchor disagrees with its displayed era is skipped too: the
    anchor may be an upload stamp for a scanned photo (boundary case #3), so
    dropping it could throw away a good scene. Both sets are listed in the
    report for a hand review.
    """
    return ag_queue.remove_scenes(data_dir, report["failing_prunable"])


def format_report(report: dict) -> str:
    v = report["verdicts"]
    lines = [
        f"scenes: {report['total']}  "
        f"impossible: {v['impossible']}  same-year: {v['same-year']}  "
        f"improbable: {v['improbable']}  unknown: {v['unknown']}",
        "year source: " + ", ".join(f"{k}={n}" for k, n in
                                    report["year_origin"].items()),
    ]
    if report["failing"]:
        lines.append(f"failing ({len(report['failing'])}):")
        for r in sorted(report["failing"], key=lambda r: (r["year"] or 0)):
            flag = " [anchor disagrees, review]" if r["anchor_disagrees"] else ""
            lines.append(
                f"  {r['id']}: {r['anomaly']!r} in {r['year']}"
                f" ({r['year_origin']}), introduced {r['intro_year']}"
                f" [{r['intro_kind']}] -> {r['verdict']}{flag}")
    if report["failing_shipped"]:
        lines.append("already shown, not pruned: "
                     + ", ".join(report["failing_shipped"]))
    if report["disputed"]:
        lines.append(f"anchor/era disagree ({len(report['disputed'])}), not "
                     "pruned: " + ", ".join(report["disputed"]))
    if report["unknown"]:
        lines.append(f"unknown introduction ({len(report['unknown'])}):")
        for r in report["unknown"]:
            lines.append(f"  {r['id']}: {r['anomaly']!r} in {r['year']}")
    return "\n".join(lines)


def parse_args(argv=None):
    p = argparse.ArgumentParser(
        prog="ag_era_audit.py",
        description="Audit the queued scenes against the impossibility bar "
                    "(ticket #1403).")
    p.add_argument("--data", default=None,
                   help="data dir (default: repo data/anomalyguessr)")
    p.add_argument("--json", action="store_true", help="print JSON")
    p.add_argument("--prune", action="store_true",
                   help="remove the failing, not-yet-shown scenes from the "
                        "queue state")
    return p.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    data_dir = Path(args.data) if args.data \
        else ag_sources.default_data_dir()
    report = audit(data_dir)
    if args.prune:
        report["pruned"] = prune(data_dir, report)
    print(json.dumps(report, indent=2) if args.json else format_report(report))
    return 0


if __name__ == "__main__":
    sys.exit(main())
