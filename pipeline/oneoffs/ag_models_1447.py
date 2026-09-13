#!/usr/bin/env python3
"""Live model comparison for ticket #1447 (cheaper image-edit models).

Evan's ask (13.09.): Nano Banana 2 is expensive and slow; is there a cheaper
model? The ticket measures candidates on OUR task, same prompt, before the
default is touched.

The run per source:

1. One REAL proposal call (``ag_generate.propose_anomaly``, deepseek-v4.1-flash)
   gives the anomaly and placement; the real ``edit_prompt`` is built from it.
2. That SAME prompt goes to every candidate model (``ag_generate._edit_attempts``,
   so the #1439 purpose preamble and the one post-refusal retry behave exactly
   as in the pipeline).
3. The returned image is written to ``--out-dir`` and judged by the real
   checker (``ag_generate.check_scene``) and the deterministic gates
   (``candidate_gate``: landscape, byte-identical re-serve, whole-frame
   repaint) plus ``ag_readd.frame_drift`` for "did the rest of the frame
   survive".

Cost comes from the response's ``usage`` payload (``ag_llm.usage_of``), never
from the price list. A candidate entry can be ``model@size`` (e.g.
``...@0.5K``) to measure the resolution lever; the default size is 1K.

Sources are offline files (the library's ``-original.jpg``), resized to the
pipeline's 2000 px cap so every model sees identical bytes.

CLI::

    ag_models_1447.py [--sources KEY,KEY] [--models M,M,...]
        [--data DIR] [--out-dir DIR] [--report FILE] [--env /home/exedev/fuchs/.env]
        [--image-size 1K] [--model M]

The JSON report goes to ``--report`` (default stdout); the script exits 0
when every (source, model) pair produced a judged image, else 1.
"""

import argparse
import json
import os
import random
import subprocess
import sys
import time
import zlib
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
PIPELINE_DIR = SCRIPT_DIR.parent
if str(PIPELINE_DIR) not in sys.path:
    sys.path.insert(0, str(PIPELINE_DIR))

import ag_generate as g  # noqa: E402
import ag_llm  # noqa: E402
import ag_queue  # noqa: E402
import ag_readd  # noqa: E402
import ag_sources  # noqa: E402
import ag_verify  # noqa: E402

# The pairs measured for #1447: the current default plus the three cheaper
# candidates named in the ticket.
DEFAULT_MODELS = [
    "google/gemini-3.1-flash-image",
    "google/gemini-3.1-flash-lite-image",
    "openai/gpt-5-image-mini",
    "google/gemini-2.5-flash-image",
]

# The three tone/era classes the ticket names, as stored scene originals in
# the data dir's library (a used source's own index entry is gone; the
# library keeps the untouched original). ``key`` is the scene-id prefix,
# ``year`` the catalogue fact fed to the proposal/checker.
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
    """One measurement source: the original resized to the pipeline cap.

    The pipeline feeds the pool's capped copy (Gallica serves at 2000 px and
    Commons sources are normalized on download); a measurement that fed the
    raw 5600 px library original would compare models on bytes the pipeline
    never sends.
    """
    original = library_original(data_dir, spec["key"])
    dest = out_dir / "sources" / f"{spec['key']}.jpg"
    dest.parent.mkdir(parents=True, exist_ok=True)
    if not dest.exists():
        subprocess.run(["convert", str(original), "-resize",
                        f"{RESIZE_WIDTH}x>", "-quality", "92", str(dest)],
                       check=True, capture_output=True)
    w, h = ag_verify.image_dims(dest)
    return {
        "id": spec["key"], "class": spec["class"], "image": dest.name,
        "image_path": dest, "width": w, "height": h, "place": spec["place"],
        "repository": spec["repository"], "originalTitle": spec["title"],
        "description": spec["title"], "year": spec["year"],
        "year_field": "catalog", "year_source": spec["year_source"],
        "year_raw": spec["year_raw"], "original_file": original.name,
    }


def model_slug(model: str) -> str:
    return model.replace("/", "_").replace(".", "")


def make_args(model: str, image_model: str, image_size: str,
              base_url: str = g.DEFAULT_BASE_URL) -> argparse.Namespace:
    return argparse.Namespace(
        model=model, base_url=base_url,
        model_max_tokens=g.DEFAULT_MODEL_MAX_TOKENS,
        model_timeout=g.DEFAULT_MODEL_TIMEOUT, temperature=g.DEFAULT_TEMPERATURE,
        image_model=image_model, image_size=image_size, image_timeout=180,
        max_generations=6,
    )


def judge(path: Path, source_image: Path, proposal: dict, st: dict,
          model: str, api_key: str, totals: dict, args) -> dict:
    """Deterministic gates + the real checker for one returned image."""
    reason, hotspot = g.candidate_gate(path, source_image, None)
    try:
        check = g.check_scene(path, proposal, api_key, model, args.base_url,
                              args.model_max_tokens, args.model_timeout,
                              args.temperature, scene=st)
    except (ag_llm.LLMError, OSError) as e:
        return {"gate": reason, "hotspot": hotspot, "error": str(e)}
    ag_llm.add_usage(totals, check["call"].get("usage"))
    w, h = ag_verify.image_dims(path)
    return {
        "gate": reason, "hotspot": hotspot, "size": f"{w}x{h}",
        "score": check["score"], "failed": check["failed"],
        "reason": check["reason"], "fix_prompt": check["fix_prompt"] or "",
        "drift": ag_readd.frame_drift(path, source_image),
    }


def measure_source(spec: dict, text_args, api_key: str, data_dir: Path,
                   out_dir: Path, models: list) -> dict:
    """One source: proposal once, then every candidate on the same prompt."""
    source = prepared_source(data_dir, spec, out_dir)
    source_image = source["image_path"]
    st = g.scene_time(source)
    if st["year"] is None:
        return {"source": spec["key"], "error": "source has no catalogue year"}

    totals = ag_llm.zero_usage()
    proposal_call = g.propose_anomaly(source_image, source, [], api_key,
                                      text_args.model, text_args.base_url,
                                      text_args.model_max_tokens,
                                      text_args.model_timeout,
                                      text_args.temperature)
    ag_llm.add_usage(totals, proposal_call["call"].get("usage"))
    if proposal_call["errors"] or not proposal_call["proposal"]:
        return {"source": spec["key"], "error": "proposal failed",
                "detail": proposal_call["errors"]}
    proposal = proposal_call["proposal"]
    prompt = g.edit_prompt(proposal)
    retry_prompt = g.edit_prompt(proposal, retry=True)

    rows = []
    for entry in models:
        model, _, size = entry.partition("@")
        size = size or text_args.image_size
        model_args = make_args(text_args.model, model, size, text_args.base_url)
        # Same seed sequence for every model of a source: the draw is not the
        # variable this measurement changes.
        random.seed(zlib.crc32(source["id"].encode()) & 0x7FFFFFFF)
        model_totals = ag_llm.zero_usage()
        model_totals["image_calls"] = 0
        try:
            attempts = g._edit_attempts(source_image, prompt, retry_prompt,
                                        proposal, model_args, api_key, source,
                                        model_totals)
        except (g.GenerationError, ag_llm.LLMError, OSError) as e:
            rows.append({"model": model, "size": size,
                         "error": f"{type(e).__name__}: {e}"})
            continue
        row = {"model": model, "size": size,
               "attempts": [{"variant": a["record"].get("variant"),
                             "refusal": a["refused"],
                             "duration_s": a["record"].get("duration_s"),
                             "usage": a["record"].get("usage"),
                             "seed": a["record"].get("seed"),
                             "error": a["record"].get("error")}
                            for a in attempts],
               "total_cost": round(float(model_totals["cost"]), 6),
               "total_s": round(sum(a["record"].get("duration_s") or 0
                                    for a in attempts), 1)}
        terminal = attempts[-1]
        if terminal["data"] is None:
            row["result"] = "refused"
            row["refusal_kind"] = terminal["refused"]
            rows.append(row)
            continue
        stem = f"{source['id'][:40]}-{model_slug(model)}-{size}"
        out_path = out_dir / "images" / f"{stem}.png"
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_bytes(terminal["data"])
        row["image"] = out_path.name
        row["image_path"] = str(out_path)
        row["judged"] = judge(out_path, source_image, proposal, st, model,
                              api_key, model_totals, model_args)
        row["total_cost"] = round(float(model_totals["cost"]), 6)
        rows.append(row)

    return {
        "source": spec["key"], "class": spec["class"],
        "image": str(source_image),
        "size": f"{source['width']}x{source['height']}", "year": st["year"],
        "proposal": proposal,
        "proposal_usage": proposal_call["call"].get("usage"),
        "prompt": prompt, "rows": rows,
    }


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="ag_models_1447.py")
    ap.add_argument("--sources", default=None,
                    help="comma-separated source keys (see SOURCES)")
    ap.add_argument("--models", default=",".join(DEFAULT_MODELS),
                    help="comma-separated model or model@size entries")
    ap.add_argument("--data", default=None)
    ap.add_argument("--out-dir", default=None)
    ap.add_argument("--report", default=None)
    ap.add_argument("--env", default="/home/exedev/fuchs/.env")
    ap.add_argument("--image-size", default=g.DEFAULT_IMAGE_SIZE)
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
    models = [m.strip() for m in ns.models.split(",") if m.strip()]
    out_dir = Path(ns.out_dir) if ns.out_dir else Path("/home/exedev/tmp/ag1447")
    out_dir.mkdir(parents=True, exist_ok=True)
    report_path = Path(ns.report) if ns.report else None

    started = time.time()
    text_args = make_args(ns.model, "", ns.image_size, ns.base_url)
    scenes, failed = [], []
    for index, spec in enumerate(specs, 1):
        print(f"[{index}/{len(specs)}] {spec['key']}", file=sys.stderr)
        try:
            scene = measure_source(spec, text_args, api_key, data_dir, out_dir,
                                   models)
        except (g.GenerationError, ag_llm.LLMError, OSError,
                FileNotFoundError) as e:
            scene = {"source": spec["key"], "error": f"{type(e).__name__}: {e}"}
        if scene.get("error"):
            failed.append(scene)
        else:
            scenes.append(scene)
        report = {"ticket": 1447, "data": str(data_dir), "out_dir": str(out_dir),
                  "proposal_model": ns.model, "image_size": ns.image_size,
                  "models": models,
                  "duration_s": round(time.time() - started, 1),
                  "scenes": scenes, "failed": failed}
        if report_path:
            report_path.write_text(json.dumps(report, indent=2) + "\n")

    judged = sum(1 for s in scenes for r in s.get("rows", [])
                 if r.get("judged") and not r["judged"].get("error"))
    total_pairs = sum(len(s.get("rows", [])) for s in scenes)
    text = json.dumps(report, indent=2)
    if report_path:
        report_path.write_text(text + "\n")
        print(f"report: {report_path}", file=sys.stderr)
    else:
        print(text)
    print(f"judged {judged}/{total_pairs} (source, model) pairs",
          file=sys.stderr)
    return 0 if judged == total_pairs and total_pairs else 1


if __name__ == "__main__":
    raise SystemExit(main())
