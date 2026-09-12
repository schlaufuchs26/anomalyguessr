#!/usr/bin/env python3
"""AnomalyGuessr era/place audit (ticket #1338).

Cross-checks the queue state against the source dataset: every scene's
declared ``year``/``place`` must agree with its source metadata under the one
era rule in ``ag_sources``. The audit exists because the July-2024 MBTA photo
shipped as "Unidentified location, 1900" with a "wheeled suitcase" anomaly
whose explanation claimed the object did not exist yet; nothing compared the
scene's era against the source's metadata.

What it reports (counts, each with the concrete ids):

- ``modern_era``: the source resolves to a born-digital photo (>= 2000).
- ``unknown_era``: no year can be established for the source at all.
- ``era_mismatch``: a source entry resolves to a different year than the
  scene declares.
- ``no_evidence``: the declared year has no plain-token evidence in the
  source text, i.e. it came from a token the guards reject ("1900-series").
- ``unidentified_place``: the scene says "Unidentified location" while the
  source carries coordinates or a place.
- ``late_year_mentions``: the scene/source text names a year >= ``LATE_YEAR``
  (the ticket's quick-scan criterion: 1950) while the declared year is
  earlier; kept as candidates, not automatic errors, because a description
  may mention a later year for its own reasons.
- ``year_counts`` + ``year_1900``: how the declared years distribute and how
  often the most common value has real evidence, which answers whether
  "1900" is a default or just what the top-up queries ask for.
- ``pool``: the source dataset's own ineligible entries (the ones
  ``ag_sources.prune_ineligible`` removes).

Scenes without a source-dataset entry are the hand-curated seed set: their
provenance is the scene's own ``source`` block, so the same rule applies, but
they are reported separately (``seed_scenes``): a mismatch there is a review
item for Evan, never an automatic removal.

CLI::

    ag_era_audit.py [--data DIR] [--json] [--prune]

``--prune`` removes the offending scenes from the queue state
(``ag_queue.remove_scenes``), the repair path for what cannot be regenerated:
a scene whose source is a modern photo is dropped, not rewritten. Scenes that
are part of the last shipped set are never pruned (the live site links them).

Run the tests with ``python3 -m unittest discover -s pipeline -t pipeline``.
"""

import argparse
import json
import re
import sys
from pathlib import Path

import ag_queue
import ag_sources

# The ticket's quick-scan threshold: a text that names a year from 1950 on
# while the scene claims an earlier era is worth a look.
LATE_YEAR = 1950
# Unguarded four-digit scan, used only to find *candidates*; the guards in
# ag_sources decide what counts as a year.
RAW_YEAR_RE = re.compile(r"\b(1[5-9]\d{2}|20\d{2})\b")
UNKNOWN_PLACES = ("", "Unidentified location", "Unknown")


def _source_index(data_dir: Path) -> dict:
    """fileUrl and originalTitle -> source entry, for the scene join."""
    index = ag_sources.load_index(data_dir)
    by_url, by_title = {}, {}
    for entry in index.get("sources", {}).values():
        if entry.get("fileUrl"):
            by_url[entry["fileUrl"]] = entry
        if entry.get("originalTitle"):
            by_title[entry["originalTitle"]] = entry
    return {"by_url": by_url, "by_title": by_title,
            "entries": index.get("sources", {})}


def _scene_source(scene: dict) -> dict:
    """The scene's own provenance block as a source-shaped entry."""
    return dict(scene.get("source") or {})


def _match(index: dict, scene: dict):
    block = _scene_source(scene)
    return (index["by_url"].get(block.get("fileUrl"))
            or index["by_title"].get(block.get("originalTitle")))


def _raw_years(text: str) -> list:
    return [int(y) for y in RAW_YEAR_RE.findall(str(text or ""))]


def audit(data_dir: Path) -> dict:
    """Cross-check the queue state against the source dataset."""
    state = ag_queue.load_state(data_dir)
    index = _source_index(data_dir)
    report = {
        "scenes": len(state.get("scenes", {})),
        "with_source_entry": 0, "seed_scenes": 0,
        "modern_era": [], "unknown_era": [], "era_mismatch": [],
        "no_evidence": [], "unidentified_place": [], "late_year_mentions": [],
        "shipped_ids": list(state.get("last_shipped_ids") or []),
        "year_counts": {}, "year_1900": {"scenes": [], "no_evidence": []},
        "pool": {"total": len(index["entries"]),
                 "unused": sum(1 for e in index["entries"].values()
                               if not e.get("used")),
                 "ineligible": []},
    }
    for entry in index["entries"].values():
        reason = ag_sources.ineligible_reason(entry)
        if reason:
            report["pool"]["ineligible"].append(
                {"id": entry.get("id"), "reason": reason,
                 "title": entry.get("originalTitle", "")})

    for sid, scene in sorted(state.get("scenes", {}).items()):
        block = _scene_source(scene)
        source = _match(index, scene)
        if source is None:
            report["seed_scenes"] += 1
            source = block
        else:
            report["with_source_entry"] += 1
        declared = ag_sources.first_year(str(scene.get("year") or ""))
        row = {"id": sid, "year": scene.get("year"),
               "era": ag_sources.entry_photo_year(source),
               "anomaly": scene.get("anomaly", ""),
               "title": scene.get("title", ""),
               "source": source.get("originalTitle", "")}
        era = row["era"]
        if era is not None and era >= ag_sources.MODERN_YEAR:
            report["modern_era"].append(row)
        elif era is None:
            report["unknown_era"].append(row)
        elif declared is not None and declared != era:
            report["era_mismatch"].append(row)
        text = " ".join(str(source.get(k) or "") for k in
                        ("originalTitle", "description", "date"))
        if declared is not None and declared not in ag_sources.years_in_text(text):
            report["no_evidence"].append(row)
        if declared == 1900:
            report["year_1900"]["scenes"].append(sid)
            if declared not in ag_sources.years_in_text(text):
                report["year_1900"]["no_evidence"].append(sid)
        if scene.get("place") in UNKNOWN_PLACES:
            evidence = source.get("place") or ((source.get("raw") or {}).get("gps"))
            if evidence:
                report["unidentified_place"].append(
                    {"id": sid, "place": evidence})
        if declared is not None and declared < LATE_YEAR:
            late = [y for y in _raw_years(block.get("originalTitle"))
                    + _raw_years(block.get("description"))
                    + _raw_years(source.get("originalTitle"))
                    + _raw_years(source.get("description")) if y >= LATE_YEAR]
            if late:
                report["late_year_mentions"].append(
                    {"id": sid, "year": scene.get("year"),
                     "mentioned": sorted(set(late))})
        report["year_counts"][str(scene.get("year"))] = (
            report["year_counts"].get(str(scene.get("year")), 0) + 1)
    return report


def prune(data_dir: Path, report: dict | None = None) -> dict:
    """Remove the wrong scenes the audit found; returns the queue result.

    Only scenes the audit marked as ``modern_era`` or ``unknown_era`` are
    dropped: their metadata cannot support any era claim, so there is nothing
    to repair. Scenes in the last shipped set are left alone.
    """
    rep = report or audit(data_dir)
    shipped = set(rep.get("shipped_ids") or [])
    ids = [r["id"] for r in rep["modern_era"] + rep["unknown_era"]
           if r["id"] not in shipped]
    skipped = [r["id"] for r in rep["modern_era"] + rep["unknown_era"]
               if r["id"] in shipped]
    return {"ids": ids, "shipped_kept": skipped,
            "queue": ag_queue.remove_scenes(data_dir, ids)}


def main(argv=None) -> int:
    p = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--data", type=Path, default=None,
                   help="data dir (default: repo data/anomalyguessr)")
    p.add_argument("--json", action="store_true",
                   help="print the full report as JSON")
    p.add_argument("--prune", action="store_true",
                   help="remove modern/unknown-era scenes from the state")
    args = p.parse_args(argv)
    data_dir = args.data or ag_queue.default_data_dir()
    rep = audit(data_dir)
    if args.prune:
        rep["pruned"] = prune(data_dir, rep)
    if args.json:
        print(json.dumps(rep, indent=2, ensure_ascii=False))
        return 0
    print(f"scenes: {rep['scenes']} "
          f"({rep['with_source_entry']} from the source dataset, "
          f"{rep['seed_scenes']} seed scenes)")
    for key, label in (("modern_era", "modern-era source (born-digital)"),
                       ("unknown_era", "era unknown"),
                       ("era_mismatch", "declared year != source era"),
                       ("no_evidence", "declared year has no plain evidence"),
                       ("unidentified_place", "Unidentified place with evidence"),
                       ("late_year_mentions", "names a year >= 1950")):
        rows = rep[key]
        print(f"{len(rows):4d}  {label}")
        for r in rows:
            print(f"        {r['id']}")
            print(f"          scene year {r.get('year')!r}, source era "
                  f"{r.get('era')!r}, anomaly {r.get('anomaly')!r}")
    pool = rep["pool"]
    print(f"pool: {pool['total']} entries, {pool['unused']} unused, "
          f"{len(pool['ineligible'])} ineligible")
    for e in pool["ineligible"]:
        print(f"        {e['id']}: {e['reason']}")
    if "pruned" in rep:
        print("pruned:", json.dumps(rep["pruned"], ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
