#!/usr/bin/env python3
"""Live branch re-measurement for ticket #1466.

The proposal prompt carried two era branches with the condition "If the
photograph clearly predates {year}", where ``{year}`` is the photo's own
catalogue year; the condition can never be true, so the model had to guess
whether the scene is historical (real later-era object) or modern
(fictional-future element). The fix states the branch that fits the year
(``ag_generate.anomaly_branch_lines``, boundary ``MODERN_SCENE_YEAR = 2000``).

This harness measures the change: for every source it runs the REAL proposal
call (``ag_generate.propose_anomaly``, deepseek-v4.1-flash) twice, once with
the current prompt and once with the pre-fix wording reconstructed here, and
reports the element each run picked, its ``kind``/``exists_from``/``not_today``,
whether the branch matches the year, and the cost. The 2026 entry is a
synthetic catalogue year on a modern-looking source.

CLI::

    ag_branch_1466.py [--sources KEY,...] [--data DIR] [--report FILE]
        [--env /home/exedev/fuchs/.env] [--model M]

The JSON report goes to ``--report`` (default stdout); exit 0 when every
proposal call returned, else 1.
"""

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from unittest import mock

SCRIPT_DIR = Path(__file__).resolve().parent
PIPELINE_DIR = SCRIPT_DIR.parent
if str(PIPELINE_DIR) not in sys.path:
    sys.path.insert(0, str(PIPELINE_DIR))

import ag_generate as g  # noqa: E402
import ag_llm  # noqa: E402
import ag_sources  # noqa: E402
import ag_verify  # noqa: E402

# The measured sources (the #1447 set: the library keeps their originals).
# ``synthetic`` marks a catalog year that does not belong to the photograph;
# it only puts the contemporary reading in front of the model.
SOURCES = [
    {"key": "fortepan-market-1938", "class": "colour historical",
     "year": 1938, "repository": "FORTEPAN",
     "title": "Market stalls with vegetables and eggs", "place": "Hungary",
     "year_source": "FORTEPAN catalogue", "year_raw": "1938"},
    {"key": "commons-a-justi-a-sculpture-leopoldo-de-almeida-1982",
     "class": "colour historical", "year": 1982,
     "repository": "Wikimedia Commons",
     "title": "\"A Justica\" sculpture (Leopoldo de Almeida, 1982), "
              "Palacio da Justica, Lisbon, Portugal",
     "place": "Lisbon, Portugal", "year_source": "Commons file page",
     "year_raw": "1982"},
    {"key": "commons-alter-l-wen-in-m-rstetten-tg-2021-05-09-e518c1",
     "class": "modern", "year": 2021, "repository": "Wikimedia Commons",
     "title": "\"Alter Loewen\" in Maerstetten TG", "place": "Maerstetten, "
     "Switzerland", "year_source": "Commons file page", "year_raw": "2021"},
    {"key": "gallica-tunis-le-port-photographie-de-presse-agence-rol-13cb37",
     "class": "grayscale historical", "year": 1912,
     "repository": "Bibliotheque nationale de France (Gallica)",
     "title": "Tunis, le port", "place": "Tunis, Tunisia",
     "year_source": "Gallica dc:date", "year_raw": "1912"},
    {"key": "gallica-gobron-vue-d-un-moteur-photographie-de-presse-ag-4bc7be",
     "class": "grayscale historical", "year": 1912,
     "repository": "Bibliotheque nationale de France (Gallica)",
     "title": "Stationary two-cylinder engine on test stand", "place": "France",
     "year_source": "Gallica dc:date", "year_raw": "1912"},
    {"key": "commons-bolshoy-tkhach-2023-11-04-c116fb", "class": "modern",
     "year": 2023, "repository": "Wikimedia Commons",
     "title": "Bolshoy Tkhach, Western Caucasus", "place": "Adygea, Russia",
     "year_source": "Commons file page", "year_raw": "2023"},
    {"key": "commons-bolshoy-tkhach-2023-11-04-c116fb",
     "class": "modern (synthetic 2026 catalogue year)", "year": 2026,
     "synthetic": True, "repository": "Wikimedia Commons",
     "title": "Bolshoy Tkhach, Western Caucasus", "place": "Adygea, Russia",
     "year_source": "synthetic", "year_raw": "2026"},
]

RESIZE_WIDTH = ag_sources.GALLICA_IMAGE_WIDTH  # the pool's 2000 px cap


def library_original(data_dir: Path, key: str) -> Path:
    """The stored ``-original.jpg`` of the scene whose id starts with key."""
    matches = [p for p in (data_dir / "library").iterdir()
               if p.is_dir() and p.name.startswith(key)]
    if len(matches) != 1:
        raise FileNotFoundError(f"{key}: {len(matches)} library dirs match")
    files = list(matches[0].glob("*-original.jpg"))
    if len(files) != 1:
        raise FileNotFoundError(f"{matches[0]}: no single -original.jpg")
    return files[0]


def prepared_source(data_dir: Path, spec: dict, out_dir: Path) -> dict:
    """One measurement source: the original resized to the pipeline cap."""
    original = library_original(data_dir, spec["key"])
    stem = spec["key"] + ("-s2026" if spec.get("synthetic") else "")
    dest = out_dir / "sources" / f"{stem}.jpg"
    dest.parent.mkdir(parents=True, exist_ok=True)
    if not dest.exists():
        subprocess.run(["convert", str(original), "-resize",
                        f"{RESIZE_WIDTH}x>", "-quality", "92", str(dest)],
                       check=True, capture_output=True)
    w, h = ag_verify.image_dims(dest)
    return {
        "id": stem, "class": spec["class"], "image": dest.name,
        "image_path": dest, "width": w, "height": h, "place": spec["place"],
        "repository": spec["repository"], "originalTitle": spec["title"],
        "description": spec["title"], "year": spec["year"],
        "year_field": "catalog", "year_source": spec["year_source"],
        "year_raw": spec["year_raw"],
    }


def old_branch_lines(year) -> list:
    """The pre-fix prompt block, reconstructed verbatim (ticket #1466).

    The first branch's condition compares the photo against its own
    catalogue year, so it can never be true; this is what the fix removes.
    """
    return [
        f"- If the photograph clearly predates {year}, the anomaly is a real "
        f"object, garment or vehicle from a LATER era (strictly after {year}).",
        "- If the photograph is modern, the anomaly is a fictional-future "
        "element that does not exist even today. Allowed: "
        + "; ".join(g.FUTURE_ALLOWED) + ".",
        "  These exist in 2026 and can NEVER carry a modern scene: "
        + ", ".join(g.TODAY_TRAPS) + ". A photo from 2019 with a delivery "
        "drone is possible, so it is invalid.",
    ]


def make_args(model: str, base_url: str) -> argparse.Namespace:
    return argparse.Namespace(
        model=model, base_url=base_url,
        model_max_tokens=g.DEFAULT_MODEL_MAX_TOKENS,
        model_timeout=g.DEFAULT_MODEL_TIMEOUT, temperature=g.DEFAULT_TEMPERATURE,
    )


def run_once(image: Path, source: dict, args, api_key: str, old: bool,
             totals: dict) -> dict:
    """One real proposal call with the new prompt or the pre-fix wording."""
    patched = old_branch_lines if old else g.anomaly_branch_lines
    with mock.patch.object(g, "anomaly_branch_lines", patched):
        call = g.propose_anomaly(image, source, [], api_key, args.model,
                                 args.base_url, args.model_max_tokens,
                                 args.model_timeout, args.temperature)
    ag_llm.add_usage(totals, call["call"].get("usage"))
    proposal = call["proposal"] or {}
    return {
        "errors": call["errors"],
        "kind": proposal.get("kind"),
        "anomaly": proposal.get("anomaly"),
        "exists_from": proposal.get("exists_from"),
        "not_today": proposal.get("not_today"),
        "placement_kind": proposal.get("placement_kind"),
        "usage": call["call"].get("usage"),
    }


def measure(spec: dict, args, api_key: str, out_dir: Path, data_dir: Path,
            totals: dict) -> dict:
    source = prepared_source(data_dir, spec, out_dir)
    st = g.scene_time(source)
    row = {"source": spec["key"], "class": spec["class"],
           "synthetic": bool(spec.get("synthetic")), "year": st["year"],
           "contemporary": g.is_contemporary(st["year"])}
    if st["year"] is None:
        row["error"] = "source has no catalogue year"
        return row
    modern = st["year"] >= g.MODERN_SCENE_YEAR
    expected = "fictional-future" if modern else "later-era"
    row["branch"] = "modern" if modern else "historical"
    row["expected_kind"] = expected
    row["new"] = run_once(source["image_path"], source, args, api_key, False,
                          totals)
    row["old"] = run_once(source["image_path"], source, args, api_key, True,
                          totals)
    row["new"]["matches_branch"] = row["new"]["kind"] == expected
    row["old"]["matches_branch"] = row["old"]["kind"] == expected
    row["kind_changed"] = row["new"]["kind"] != row["old"]["kind"]
    return row


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="ag_branch_1466.py")
    ap.add_argument("--sources", default=None,
                    help="comma-separated source keys (see SOURCES)")
    ap.add_argument("--data", default=None)
    ap.add_argument("--out-dir", default=None)
    ap.add_argument("--report", default=None)
    ap.add_argument("--env", default="/home/exedev/fuchs/.env")
    ap.add_argument("--model", default=g.MODEL)
    ap.add_argument("--base-url",
                    default=os.environ.get("OPENAI_BASE_URL")
                    or g.DEFAULT_BASE_URL)
    ns = ap.parse_args(argv)

    if ns.env:
        ag_verify.load_env(Path(ns.env))
    api_key = os.environ.get("OPENROUTER_API_KEY", "")
    if not api_key:
        print("OPENROUTER_API_KEY not set (pass --env)", file=sys.stderr)
        return 1

    data_dir = Path(ns.data) if ns.data else ag_sources.default_data_dir()
    specs = SOURCES
    if ns.sources:
        wanted = {s.strip() for s in ns.sources.split(",") if s.strip()}
        specs = [s for s in SOURCES if s["key"] in wanted]
        if not specs:
            print("no source keys matched", file=sys.stderr)
            return 1
    out_dir = Path(ns.out_dir) if ns.out_dir else Path("/home/exedev/tmp/ag1466")
    out_dir.mkdir(parents=True, exist_ok=True)
    args = make_args(ns.model, ns.base_url)

    started = time.time()
    totals = ag_llm.zero_usage()
    rows = [measure(spec, args, api_key, out_dir, data_dir, totals)
            for spec in specs]
    report = {
        "ticket": 1466,
        "model": ns.model,
        "sources": rows,
        "totals": {k: (round(v, 6) if isinstance(v, float) else v)
                   for k, v in totals.items()},
        "duration_s": round(time.time() - started, 1),
    }
    text = json.dumps(report, indent=1, ensure_ascii=False)
    if ns.report:
        Path(ns.report).write_text(text + "\n")
        print(f"report -> {ns.report}")
    else:
        print(text)
    failed = [r for r in rows if r.get("error") or r.get("new", {}).get(
        "errors") or r.get("old", {}).get("errors")]
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
