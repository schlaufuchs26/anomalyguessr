#!/usr/bin/env python3
"""AnomalyGuessr scene generator: a four-step LLM flow (ticket #1372).

Sourcing and generation are two separate problems (Evan, 2026-09-12).
Sourcing (`pipeline/ag_sources.py`) fills the pool from Commons "Quality
images" with key filters only; this script turns one pooled photo into one
queued scene.

Per scene:

1. **Propose** (`propose_anomaly`): one `deepseek/deepseek-v4.1-flash` vision
   call over the source photo. The model judges the photo's apparent era and
   invents ONE subtle time-travel anomaly for THIS image: a real later-era
   object for a historical photo, a fictional-future element for a modern
   one (Evan: modern photos are fine, "dann nutzen wir fictional futures").
   It answers with label, kind, apparent era, figure flag, placement,
   explanation and references. `ag_catalog.INSPIRATION` supplies few-shot
   shape examples; recently used labels are passed in to avoid repeats.
2. **Apply** (`image_edit`): one `google/gemini-3.1-flash-image` call that
   adds the anomaly. The generation prompt keeps the hard constraints (one
   dominant placement instruction, ONE numeric scale cap with a
   same-distance anchor, keep everything else, tone match, grain, no glow);
   the long proscriptive rule list moved into the checker.
3. **Locate** (`locate_anomaly`): one vision call over the edited image for
   the click target (x/y/r, figure flag), with the proposal and the edit
   prompt as context. The answer is widened when the deterministic diff
   hotspot falls outside it, so a wildly wrong circle cannot ship alone.
4. **Check** (`check_scene`): one vision call against the requirements list
   (time-travel framing, subtlety, scale, tone, grain, keep-the-rest,
   identifiability). A not-OK verdict with a fix prompt triggers ONE
   correction edit, then the deterministic gates run again and the checker
   gets the last word: a scene the re-check still rejects is dropped.

Deterministic gates stay deterministic: a byte-identical re-serve is refused
(`ag_verify.identical_output_check`), a scene that is not a localized edit is
refused (`ag_verify.locate_hotspot`), and the queue validator still checks
the entry schema.

Each scene gets a trace sidecar (`data/anomalyguessr/traces/<id>.json`, ticket
#1373) with every pipeline step in order: model, full prompt, answer,
reasoning content, usage (tokens + cost), duration and timestamp, with the
image named but never embedded. Steps are flushed to
`traces/pending/<source>.json` as the pipeline runs, so a crash keeps a
partial record; a finished scene shows the whole flow, a scene from before
the trace existed shows none.

The run lock, the progress status file, the DM-on-failure path and the queue
add are unchanged from #1210/#1169: the daily cron and the dashboard's
"generate more" both go through this script.

CLI::

    ag_generate.py [--data DIR] [--count 10] [--seed N] [--dry-run] [--top-up]
        [--env .env] [--dm-channel ID] [--model M] [--image-model M]
        [--max-attempts 2] [--max-generations N] [--date YYYY-MM-DD]
        [--out-dir DIR] [--report FILE] [--no-check] [--no-preflight]

Exit code 0 = ran (fewer than `--count` scenes is possible; the JSON report
lists what failed); 1 = no scene was added or a hard error occurred.
"""

import argparse
import datetime
import errno
import fcntl
import hashlib
import json
import math
import os
import random
import re
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import ag_catalog  # noqa: E402
import ag_llm  # noqa: E402
import ag_queue  # noqa: E402
import ag_sources  # noqa: E402
import ag_verify  # noqa: E402

# The three text/vision calls (proposal, coordinates, check) share one cheap
# multimodal model; the image edit is the expensive call.
MODEL = "deepseek/deepseek-v4.1-flash"
IMAGE_MODEL = "google/gemini-3.1-flash-image"
DEFAULT_BASE_URL = "https://openrouter.ai/api/v1"
DEFAULT_IMAGE_SIZE = "1K"
DEFAULT_MODEL_MAX_TOKENS = 900
DEFAULT_MODEL_TIMEOUT = 120
# A single pick without a temperature is near a coin flip (#1217); 0 makes a
# repeat of the same photo+prompt reproduce the proposal (#1313).
DEFAULT_TEMPERATURE = 0.0
# Landscape aspect ratios accepted by the image model, nearest-match against
# the source dimensions (the queue rejects non-landscape scene images).
ASPECTS = (("16:9", 16 / 9), ("3:2", 1.5), ("4:3", 4 / 3), ("5:4", 1.25),
           ("21:9", 21 / 9), ("2:1", 2.0), ("1:1", 1.0))
SCENE_ID_RE = re.compile(r"[^a-z0-9]+")

# How recently used anomaly labels feed the proposal prompt (the free-form
# replacement for the old catalog label/family novelty tier, #1328).
REPEAT_WINDOW_DAYS = 7
REPEAT_LABEL_LIMIT = 15

# Run lock + progress status (ticket #1210). Both live in the data dir next
# to state.json/feedback.json; they are runtime artifacts, not committed.
LOCK_NAME = "generate.lock"
STATUS_NAME = "generate-status.json"
TRACE_DIRNAME = "traces"


class GenerationError(RuntimeError):
    """A model call or a deterministic gate failed for one attempt."""


class RunLockedError(RuntimeError):
    """Another generation run holds the run lock (ticket #1210)."""

    def __init__(self, holder: str):
        self.holder = holder
        super().__init__(
            f"another generation run is in progress (pid {holder})")


# ── Prompts ────────────────────────────────────────────────────────────────

# Tone/blend rules that stay hard constraints of the *generation* call: the
# model has to aim at them directly. The rest of the old proscriptive list
# (scale verification, placement of risky objects, framing) moved into the
# checker (ticket #1372).
KEEP = ("Keep every other part of the photograph EXACTLY as it is: same "
        "composition, people, goods and background; do not redraw, move, "
        "add or recolor anything else.")
BLEND = ("Match the ORIGINAL photo's actual tone and coloration EXACTLY, "
         "whatever it is (many old photographs are true black-and-white/"
         "grayscale: the added element must then also be grayscale); never "
         "add sepia, never add any warm or color cast, never add a filter; "
         "film grain lies OVER the object; soft edges, consistent lighting "
         "and shadow direction, realistic perspective; it must look "
         "photographed, not pasted. No glow, low contrast, not a focal "
         "point.")
DEFAULT_OBJECT_SCALE = ("about 2 percent of the image height (roughly 20-30 "
                        "pixels on a 1200-pixel-tall image), never more than "
                        "3 percent")
DEFAULT_FIGURE_SCALE = "roughly 8-15 percent of the image height"
SIZE_ANCHOR = ("judge it against something at the same distance in the "
               "photo (a crate, a wheel or a person's shoe) so its "
               "perspective matches the scene")

# The checker's requirements list: the hard-won rules of the agent era, moved
# here from the generation prompt. A violation should be caught and corrected
# instead of being pre-empted by an ever-longer recipe.
REQUIREMENTS = (
    "1. Time travel only: a real element from a LATER era than the "
    "photograph, or a clearly futuristic one. Pure fantasy (flying saucers, "
    "dragons, unicorns, ghosts, magic) is a failure.\n"
    "2. Subtle: one small element, not centered, not the largest thing in "
    "the frame, not a focal point; partly hidden or at the edge is best.\n"
    "3. Scale: realistic for its position, judged against something at the "
    "same distance. A small object must stay under about 3 percent of the "
    "image height; a person roughly 8-15 percent, never a giant.\n"
    "4. Tone: exactly the photograph's tone and coloration (a grayscale "
    "photo stays grayscale); no sepia, no color cast, no filter.\n"
    "5. Grain lies over the added element; soft edges; consistent lighting "
    "and shadow direction; realistic perspective; no glow; it must look "
    "photographed, not pasted.\n"
    "6. Everything else unchanged: same composition, people, goods and "
    "background; nothing else redrawn, moved or recolored.\n"
    "7. The added element must be identifiable as the anachronism (not so "
    "tiny or so blended that a player cannot find it)."
)


def proposal_prompt(source: dict, recent=()) -> str:
    lines = [
        "You are the content designer for a spot-the-anachronism game: "
        "players get a real photograph and must find the ONE thing that does "
        "not belong to its time.",
        "This photograph comes from "
        f"{source.get('repository') or 'a public archive'}. Judge its "
        "apparent era yourself from what you see.",
        "Invent ONE anomaly to hide in THIS photograph:",
        "- If the photo clearly predates the present, the anomaly is a real "
        "object, garment or vehicle from a LATER era (after the photo).",
        "- If the photo looks modern, the anomaly is a clearly futuristic "
        "element (a fictional-future device or figure), because nothing in "
        "the real present would read as out of place.",
        "- It must be ONE small, concrete thing that could plausibly sit in "
        "this scene: an object, or one extra person whose only modern or "
        "futuristic tell is a small detail.",
        "- Pure fantasy is out: no flying saucers, dragons, unicorns, ghosts "
        "or magic. Everything must read as a thing from another time.",
        "- Keep it subtle: findable, but not obvious.",
        "Style examples (do not copy them; they only show the shape):",
    ]
    lines += [f"- {line}" for line in ag_catalog.inspiration_lines()]
    if recent:
        lines.append("Avoid these anomalies used by recent scenes: "
                     + "; ".join(str(r) for r in recent) + ".")
    lines.append(
        'Answer as strict JSON only, no prose: {"anomaly": "<short label>", '
        '"kind": "later-era"|"fictional-future", "apparent_era": "<the year '
        'or decade that best fits the photo, or \\"modern\\">", '
        '"figure": true|false, "placement": "<one sentence: where in THIS '
        'photo it sits, how it is partly hidden, and how large it should '
        'look next to things at the same distance>", "explanation": "<one '
        'sentence: why it cannot belong to this photograph>", "references": '
        '[{"label": "<source name>", "url": "https://..."}]}')
    return "\n".join(lines)


def scale_rule(proposal: dict) -> str:
    """The ONE hard numeric scale cap for the edit call (ticket #1328)."""
    if proposal.get("figure"):
        return ("CRITICAL SCALE: the figure's rendered height must be "
                f"realistic for its position ({DEFAULT_FIGURE_SCALE}), "
                f"{SIZE_ANCHOR}, never a giant foreground figure.")
    return ("CRITICAL SCALE: its rendered height in the final image must be "
            f"{DEFAULT_OBJECT_SCALE}; {SIZE_ANCHOR}; if in doubt make it "
            "smaller and hide more of it behind the foreground object.")


def edit_prompt(proposal: dict) -> str:
    head = (f"Edit this historical photograph: add ONE "
            f"{proposal['anomaly']}, {proposal['placement']}.")
    return " ".join((head, scale_rule(proposal), KEEP, BLEND))


def coord_prompt(proposal: dict, prompt: str = "") -> str:
    lines = [
        f"You added this to the photograph: {proposal['anomaly']} "
        f"({proposal['placement']}).",
    ]
    if prompt:
        lines.append(f"The edit instruction was: {prompt}")
    lines.append(
        "Return the click target that covers the WHOLE added element in THIS "
        "edited image.")
    lines.append(
        'Answer as strict JSON only: {"x": <0..1>, "y": <0..1>, '
        '"r": <0..1>, "figure": true|false}, where x is the center across '
        "the width, y the center down the height, r the radius as a fraction "
        "of the image height that covers all of it (for a person: head to "
        "feet, shoes included), and figure is true when it is a human-like "
        "figure.")
    return "\n".join(lines)


def check_prompt(proposal: dict) -> str:
    return ("You are the quality checker for a spot-the-anachronism game.\n"
            "The image you see should be the original photograph with ONE "
            f"element added: {proposal['anomaly']} "
            f"({proposal['placement']}).\n"
            "Check it against these requirements:\n"
            f"{REQUIREMENTS}\n"
            'Answer as strict JSON only: {"ok": true|false, "problems": '
            '["<short problem>", ...], "fix_prompt": "<one self-contained '
            'instruction to fix the problems, or empty when ok>"}')


# ── Proposal validation / references ───────────────────────────────────────

def valid_reference(ref) -> bool:
    return (isinstance(ref, dict) and isinstance(ref.get("label"), str)
            and ref["label"].strip()
            and isinstance(ref.get("url"), str)
            and re.match(r"^https?://", ref["url"]))


def resolve_references(proposal: dict, source: dict) -> tuple:
    """(references, origin) for the scene entry: the model's when usable.

    Fallback chain (all recorded in the trace): the same-family catalog
    entry's curated references, then the source file page. The queue schema
    requires at least one {label, url} per factual claim, so a proposal
    without usable URLs cannot become a scene on its own.
    """
    refs = [{"label": str(r.get("label")).strip()[:120],
             "url": str(r.get("url")).strip()}
            for r in (proposal.get("references") or []) if valid_reference(r)]
    if refs:
        return refs[:3], "model"
    family = ag_catalog.family_of(str(proposal.get("anomaly") or ""))
    curated = ag_catalog.references_for_family(family) if family else []
    if curated:
        return curated, "catalog-family"
    entry = ag_catalog.entry_for_label(str(proposal.get("anomaly") or ""))
    if entry:
        return [dict(r) for r in entry["references"]], "catalog-label"
    return [{"label": "Source photograph",
             "url": source.get("fileUrl") or source.get("sourceUrl") or ""}], \
        "source"


def proposal_errors(proposal) -> list:
    """Why a proposal cannot be rendered into a scene, [] when it can."""
    errs = []
    if not isinstance(proposal, dict):
        return ["proposal is not a JSON object"]
    if not isinstance(proposal.get("anomaly"), str) or \
            not proposal["anomaly"].strip():
        errs.append("anomaly must be a non-empty string")
    elif len(proposal["anomaly"]) > 80:
        errs.append("anomaly label is longer than 80 characters")
    if proposal.get("kind") not in ("later-era", "fictional-future"):
        errs.append("kind must be later-era or fictional-future")
    if not isinstance(proposal.get("apparent_era"), str) or \
            not proposal["apparent_era"].strip():
        errs.append("apparent_era must be a non-empty string")
    if not isinstance(proposal.get("placement"), str) or \
            not proposal["placement"].strip():
        errs.append("placement must be a non-empty string")
    if not isinstance(proposal.get("explanation"), str) or \
            not proposal["explanation"].strip():
        errs.append("explanation must be a non-empty string")
    return errs


def normalize_proposal(proposal: dict) -> dict:
    """Whitespace-collapse and cap the proposal's free-text fields."""
    out = dict(proposal)
    for key, limit in (("anomaly", 80), ("apparent_era", 40),
                       ("placement", 400), ("explanation", 400)):
        out[key] = ag_llm.clean_text(out.get(key), limit)
    out["figure"] = bool(out.get("figure"))
    return out


# ── Model calls ────────────────────────────────────────────────────────────

def _chat_with_image(prompt: str, image: Path, api_key: str, model: str,
                     base_url: str, max_tokens: int, timeout: int,
                     temperature: float | None):
    message = {"role": "user",
               "content": [ag_llm.text_part(prompt), ag_llm.image_part(image)]}
    return ag_llm.chat([message], api_key, model, base_url, max_tokens,
                       temperature, timeout)


def image_ref(image: Path | None) -> str:
    """A stable, host-free id for the image a call saw (ticket #1373).

    The trace is publishable text, so never store the absolute path: the
    file name identifies the source photo or the attempt's edited output
    without leaking where the pipeline runs.
    """
    return image.name if image is not None else ""


def _run_call(prompt: str, image: Path | None, api_key: str, model: str,
              base_url: str, max_tokens: int, timeout: int,
              temperature: float | None) -> dict:
    """One logged vision call: {prompt, answer, parsed, usage, model, latency}."""
    started = time.time()
    at = datetime.datetime.now().astimezone().isoformat(timespec="seconds")
    if image is None:
        body = ag_llm.chat([{"role": "user", "content": [ag_llm.text_part(prompt)]}],
                           api_key, model, base_url, max_tokens, temperature,
                           timeout)
    else:
        body = _chat_with_image(prompt, image, api_key, model, base_url,
                                max_tokens, timeout, temperature)
    content = ag_llm.content_of(body)
    return {"model": model, "prompt": prompt, "answer": content,
            "reasoning": ag_llm.reasoning_of(body),
            "image": image_ref(image), "at": at,
            "parsed": ag_llm.parse_json(content),
            "usage": ag_llm.usage_of(body),
            "duration_s": round(time.time() - started, 2)}


def propose_anomaly(image: Path, source: dict, recent, api_key: str,
                    model: str, base_url: str, max_tokens: int, timeout: int,
                    temperature: float | None) -> dict:
    """Call 1: the creative proposal (era judgement + anomaly + placement)."""
    prompt = proposal_prompt(source, recent)
    result = _run_call(prompt, image, api_key, model, base_url, max_tokens,
                       timeout, temperature)
    proposal = result["parsed"]
    errors = proposal_errors(proposal)
    if errors:
        parsed = None
    else:
        parsed = normalize_proposal(proposal)
    return {"proposal": parsed, "errors": errors, "call": result}


def locate_anomaly(image: Path, proposal: dict, prompt: str, api_key: str,
                   model: str, base_url: str, max_tokens: int, timeout: int,
                   temperature: float | None) -> dict:
    """Call 3: the click target in the edited image (context of 1 and 2)."""
    text = coord_prompt(proposal, prompt)
    result = _run_call(text, image, api_key, model, base_url, max_tokens,
                       timeout, temperature)
    coords = valid_coords(result["parsed"])
    return {"coords": coords, "call": result}


def check_scene(image: Path, proposal: dict, api_key: str, model: str,
                base_url: str, max_tokens: int, timeout: int,
                temperature: float | None) -> dict:
    """Call 4: does the scene meet the requirements, and how to fix it."""
    prompt = check_prompt(proposal)
    result = _run_call(prompt, image, api_key, model, base_url, max_tokens,
                       timeout, temperature)
    parsed = result["parsed"] or {}
    ok = parsed.get("ok")
    problems = parsed.get("problems")
    if not isinstance(problems, list):
        problems = []
    problems = [ag_llm.clean_text(p, 200) for p in problems if p]
    return {"ok": ok if isinstance(ok, bool) else None,
            "problems": problems,
            "fix_prompt": ag_llm.clean_text(parsed.get("fix_prompt"), 600),
            "call": result}


def valid_coords(raw) -> dict | None:
    """A usable click target from the coordinate call, None when malformed."""
    if not isinstance(raw, dict):
        return None
    try:
        x, y, r = float(raw["x"]), float(raw["y"]), float(raw["r"])
    except (KeyError, TypeError, ValueError):
        return None
    if not (0.0 <= x <= 1.0 and 0.0 <= y <= 1.0):
        return None
    if not (0.0 < r <= 0.5):
        return None
    return {"x": x, "y": y, "r": r, "figure": bool(raw.get("figure"))}


def finalize_answer(coords: dict, hotspot) -> tuple:
    """The stored answer + a conflict report against the diff hotspot.

    The LLM's target is the answer (ticket #1372). The deterministic hotspot
    is the cheap sanity check: when its center falls outside the LLM circle,
    the circle is widened to cover the causal edit region (capped at the
    queue's maximum), so a wildly wrong box cannot ship alone, and the
    disagreement is reported (idea #1343).
    """
    x, y, r = coords["x"], coords["y"], coords["r"]
    if coords.get("figure"):
        r = max(r, ag_verify.PERSON_MIN_RADIUS)
    r = max(0.02, min(r, ag_verify.MAX_ANSWER_RADIUS))
    conflict = None
    if hotspot:
        cx, cy = float(hotspot["cx"]), float(hotspot["cy"])
        delta = math.hypot(cx - x, cy - y)
        if delta > r:
            conflict = {"delta": round(delta, 4),
                        "llm_center": [round(x, 4), round(y, 4)],
                        "hotspot_center": [round(cx, 4), round(cy, 4)]}
            r = min(ag_verify.MAX_ANSWER_RADIUS, delta * 1.1)
    return {"x": round(x, 4), "y": round(y, 4), "r": round(r, 4)}, conflict


def deterministic_gate(edited: Path, original: Path, dedup):
    """(reason, hotspot): the pixel-evidence gates, never skipped.

    A byte-identical re-serve and a whole-frame repaint are refused here, so
    the model calls only ever judge a genuinely localized edit.
    """
    dup = ag_verify.identical_output_check(edited, original, dedup)
    if dup is not None:
        return dup.get("reason") or "identical output", None
    loc = ag_verify.locate_hotspot(edited, original)
    if not loc["ok"]:
        return loc.get("reason") or "no localized edit", None
    return None, loc["hotspot"]


# ── Image edit ─────────────────────────────────────────────────────────────

def aspect_ratio_for(width: int, height: int) -> str:
    if not width or not height:
        return "3:2"
    ratio = width / height
    return min(ASPECTS, key=lambda a: abs(a[1] - ratio))[0]


def image_edit(source_image: Path, prompt: str, api_key: str,
               base_url: str = DEFAULT_BASE_URL, model: str = IMAGE_MODEL,
               aspect_ratio: str = "3:2", image_size: str = DEFAULT_IMAGE_SIZE,
               seed=None, timeout: int = 180, usage_out: dict | None = None
               ) -> bytes:
    """One OpenRouter image-edit call; mirrors the fuchs image_generate tool.

    A fresh int32 seed per call is the #1124 cache-buster; the explicit
    clamp also works around the #1152 seed-overflow 400. When ``usage_out``
    is given, the response's token/cost usage is folded into it, so the run
    report's cost covers every call (ticket #1372).
    """
    if seed is None:
        seed = random.randint(0, 2**31 - 1)
    seed = int(seed) % (2**31)
    payload = {
        "model": model,
        "messages": [{
            "role": "user",
            "content": [ag_llm.text_part(prompt),
                        ag_llm.image_part(source_image)],
        }],
        "modalities": ["image", "text"],
        "stream": False,
        "seed": seed,
        "image_config": {"aspect_ratio": aspect_ratio,
                         "image_size": image_size},
    }
    req = urllib.request.Request(
        base_url.rstrip("/") + "/chat/completions",
        data=json.dumps(payload).encode(),
        headers={"Authorization": f"Bearer {api_key}",
                 "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = json.load(r)
    except urllib.error.HTTPError as e:
        raise GenerationError(f"image request failed: HTTP {e.code} "
                              f"{e.read()[:300]!r}") from e
    except urllib.error.URLError as e:
        raise GenerationError(f"image request failed: {e}") from e
    choices = body.get("choices") or []
    if usage_out is not None:
        ag_llm.add_usage(usage_out, ag_llm.usage_of(body))
    images = (choices[0].get("message", {}).get("images") or []) if choices \
        else []
    if not images:
        raise GenerationError("no images returned (model refusal?)")
    return ag_llm.decode_data_url(images[0]["image_url"]["url"])


# ── Entry text (deterministic from the proposal + source keys) ─────────────

def slugify(text: str) -> str:
    return SCENE_ID_RE.sub("-", text.lower()).strip("-")


def scene_id(source_id: str, label: str) -> str:
    """Scene id = readable source prefix + the source's short hash + anomaly.

    Raw source ids are long (title slug + date + hash); taking the full id
    plus a label would make unreadable scene ids, and truncating alone can
    collide between near-identical titles. Keeping the id's trailing 6-hex
    hash guarantees uniqueness.
    """
    sid = source_id or ""
    m = re.search(r"-([0-9a-f]{6})$", sid)
    if m:
        tail, prefix = m.group(1), sid[:m.start()]
    else:
        tail = hashlib.sha1(sid.encode()).hexdigest()[:6]
        prefix = sid
    prefix = prefix[:56].rstrip("-")
    return f"{prefix}-{tail}-{slugify(label)[:24].rstrip('-')}"


def strip_html(s: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", s or "")).strip()


def clean_title(source: dict) -> str:
    t = strip_html(str(source.get("originalTitle") or ""))
    t = re.sub(r"^File:", "", t).strip()
    t = re.sub(r"\.(jpe?g|png|gif|webp|tiff?)$", "", t, flags=re.I)
    t = t.replace("_", " ")
    # Drop archive/gallery bookkeeping suffixes (" - DPLA - <hash>",
    # " - <32 hex>", " - <long id>") that carry no scene information.
    t = re.sub(r"\s*-\s*(?:DPLA|LOC|NARA)\s*-\s*[0-9A-Za-z_-]{8,}\s*$", "",
               t, flags=re.I)
    t = re.sub(r"\s*-\s*[0-9a-f]{16,}\s*$", "", t, flags=re.I)
    t = re.sub(r"\s*-\s*\d{6,}\s*$", "", t)
    return re.sub(r"\s+", " ", t).strip(' "') or "Photograph"


def scene_place(source: dict) -> str:
    """Place from the source's structured keys, or the honest unknown marker.

    The queue schema requires a non-empty place string, so the marker stands
    in for "the keys carry none"; the frontend hides it (ticket #1372: no
    invented place label).
    """
    return str(source.get("place") or "").strip() or "Unidentified location"


def scene_year(proposal: dict) -> str:
    """The displayed era: the proposal call's judgement (ticket #1372)."""
    era = ag_llm.clean_text(proposal.get("apparent_era"), 40)
    m = re.search(r"(?<!\d)(1[0-9]\d{2}|20\d{2})(?!\d)", era)
    return m.group(1) if m else (era or "unknown")


def region_phrase(answer: dict) -> str:
    x, y = answer.get("x", 0.5), answer.get("y", 0.5)
    horiz = "left" if x < 0.34 else ("right" if x > 0.66 else "center")
    vert = "upper" if y < 0.34 else ("lower" if y > 0.66 else "middle")
    if horiz == "center":
        return f"{vert} center"
    return f"{vert} {horiz}"


def article(label: str) -> str:
    return "An" if label[:1].upper() in ("A", "E", "I", "O", "U") else "A"


def build_hints(proposal: dict, answer: dict) -> list:
    region = region_phrase(answer)
    where = ag_llm.clean_text(proposal.get("placement"), 120)
    label = proposal["anomaly"]
    if proposal.get("figure"):
        return [
            "Look at the people in the scene; one of them does not belong.",
            f"Check the {region} of the frame: a figure there is out of "
            "time.",
            f"{article(label)} {label} is in the {region}. {where}",
        ]
    return [
        "Scan the whole photograph; a small thing here does not belong to "
        "its time.",
        f"It sits in the {region} of the frame and is partly hidden.",
        f"{article(label)} {label} is in the {region}. {where}",
    ]


def build_description(source: dict, year: str, place: str) -> str:
    title = clean_title(source)
    bits = [title.rstrip(".")]
    if place and place != "Unidentified location" \
            and place.lower() not in title.lower():
        bits.append(place)
    if year and year not in title and year != "unknown":
        bits.append(f"circa {year}")
    repo = source.get("repository") or "an archive"
    bits.append(f"from the {repo} catalogue")
    return ", ".join(bits) + "."


def build_credit(source: dict) -> str:
    raw = source.get("raw") or {}
    artist = strip_html(str(raw.get("artist") or ""))
    repo = source.get("repository") or "unknown repository"
    return f"{artist} via {repo}" if artist else repo


def build_entry(source: dict, proposal: dict, answer: dict, date: str) -> dict:
    eid = scene_id(source["id"], proposal["anomaly"])
    year = scene_year(proposal)
    place = scene_place(source)
    entry = ag_catalog.entry_for_label(proposal["anomaly"])
    if entry is not None:
        explanation = entry["explanation"]
        refs = [dict(r) for r in entry["references"]]
    else:
        explanation = proposal["explanation"]
        refs, _ = resolve_references(proposal, source)
    family = ag_catalog.family_of(proposal["anomaly"]) or "other"
    source_block = {k: source.get(k, "") for k in (
        "repository", "fileUrl", "originalTitle", "date", "place", "license",
        "description")}
    return {
        "id": eid,
        "title": clean_title(source),
        "place": place,
        "year": year,
        "description": build_description(source, year, place),
        "anomaly": proposal["anomaly"],
        "family": family,
        "answer": answer,
        "hints": build_hints(proposal, answer),
        "explanation": explanation,
        "references": refs,
        "source": source_block,
        "credit": build_credit(source),
        "sourceUrl": source.get("fileUrl", ""),
    }


# ── Trace sidecar (ticket #1373) ───────────────────────────────────────────
#
# One JSON file per scene (data/anomalyguessr/traces/<scene-id>.json) with
# every pipeline step in order: model, full prompt, answer, reasoning,
# usage (tokens + cost), duration and timestamp. While a scene is being
# generated the growing trace is flushed to traces/pending/<source>.json
# after each step, so a run that dies mid-scene still leaves a partial
# record; the final write renames it to the scene id and drops the pending
# file. Trace I/O is always best-effort: a full disk or a permission error
# must never fail a scene.

# Anything that looks like an API key or an absolute host path is stripped
# before writing. The trace is publishable text (ticket #1373): it may end
# up in the dev gallery over the tunnel, so it must not carry secrets or
# the box's layout.
_KEY_RE = re.compile(r"\b(?:sk|pk)-[A-Za-z0-9_\-]{8,}\b")


def _scrub(value):
    """Recursively drop key-shaped strings and absolute host paths."""
    if isinstance(value, str):
        cleaned = _KEY_RE.sub("<redacted>", value)
        cleaned = cleaned.replace(str(Path.home()), "~")
        return cleaned
    if isinstance(value, list):
        return [_scrub(v) for v in value]
    if isinstance(value, dict):
        return {k: _scrub(v) for k, v in value.items()}
    return value


def trace_path(data_dir: Path, eid: str) -> Path:
    return data_dir / TRACE_DIRNAME / f"{eid}.json"


def pending_trace_path(data_dir: Path, source_id: str) -> Path:
    slug = re.sub(r"[^a-z0-9._-]", "-", str(source_id).lower())[:120]
    return data_dir / TRACE_DIRNAME / "pending" / f"{slug or 'source'}.json"


def _atomic_json(path: Path, payload: dict) -> None:
    """Write JSON via tmp + os.replace, so readers never see half a file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".trace-",
                               suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2, ensure_ascii=False)
            f.write("\n")
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def flush_trace(data_dir: Path, trace: dict) -> None:
    """Write the in-progress trace to its pending file; never raises."""
    try:
        _atomic_json(pending_trace_path(data_dir, trace.get("source", "")),
                     _scrub(trace))
    except OSError:
        pass  # never block generation on trace I/O


def record_call(data_dir: Path, trace: dict, step: dict) -> None:
    """Append one step to the trace and flush the in-progress file.

    This is the emitter (ticket #1373): the trace is written as the
    pipeline goes, not reconstructed afterwards, so a crash keeps the
    steps that already ran. Never raises.
    """
    trace.setdefault("calls", []).append(step)
    flush_trace(data_dir, trace)


def set_trace_error(data_dir: Path, trace: dict, message: str) -> None:
    """Record the current attempt's failure reason and flush it."""
    trace["error"] = message
    flush_trace(data_dir, trace)


def write_trace(data_dir: Path, eid: str, trace: dict) -> None:
    """Finalize a scene's generation trace; best-effort, never raises."""
    trace = dict(trace)
    trace["scene"] = eid
    trace["recordedAt"] = datetime.datetime.now().astimezone().isoformat(
        timespec="seconds")
    try:
        _atomic_json(trace_path(data_dir, eid), _scrub(trace))
        pending = pending_trace_path(data_dir, trace.get("source", ""))
        if pending.exists():
            pending.unlink()
    except OSError:
        pass  # the scene is already saved; a missing sidecar must not fail it


# ── Environment / status helpers ───────────────────────────────────────────

def ensure_tool_path() -> None:
    """Put the nix profile on PATH: `identify` lives there, and the cron
    terminal env (core/plugins/cron/plugin.go cronPath) does not include it.
    """
    nix_bin = Path.home() / ".nix-profile" / "bin"
    if nix_bin.is_dir():
        parts = os.environ.get("PATH", "").split(":")
        if str(nix_bin) not in parts:
            os.environ["PATH"] = str(nix_bin) + ":" + os.environ.get("PATH", "")


def status_path(data_dir: Path) -> Path:
    return data_dir / STATUS_NAME


def write_status(data_dir: Path, **fields) -> None:
    """Atomically write the progress status the dashboard polls."""
    payload = dict(fields)
    payload["updatedAt"] = datetime.datetime.now().astimezone().isoformat(
        timespec="seconds")
    data_dir.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(data_dir), prefix=".status-",
                               suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2, ensure_ascii=False)
            f.write("\n")
        os.replace(tmp, status_path(data_dir))
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


class RunLock:
    """Exclusive cross-process lock for one whole generation run.

    `ag_queue.state_add` is atomic per scene (tmp file + rename) but is a
    read-modify-write without a lock, so two concurrent generators would lose
    each other's state updates. The kernel drops the flock when the process
    exits (even on SIGKILL), so a crashed run cannot block the next one.
    """

    def __init__(self, data_dir: Path):
        self.path = data_dir / LOCK_NAME
        self._file = None

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        f = open(self.path, "a+", encoding="utf-8")
        try:
            fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as e:
            f.close()
            if e.errno not in (errno.EACCES, errno.EAGAIN):
                raise
            raise RunLockedError(self._holder()) from None
        f.seek(0)
        f.truncate()
        f.write(str(os.getpid()))
        f.flush()
        self._file = f

    def _holder(self) -> str:
        try:
            text = self.path.read_text(encoding="utf-8").strip()
        except OSError:
            return "unknown"
        return text or "unknown"

    def release(self) -> None:
        if self._file is None:
            return
        try:
            fcntl.flock(self._file.fileno(), fcntl.LOCK_UN)
        finally:
            self._file.close()
            self._file = None


# ── Source selection ───────────────────────────────────────────────────────

def recent_labels(data_dir: Path, today=None,
                  days: int = REPEAT_WINDOW_DAYS) -> list:
    """Labels of scenes added in the recent window, newest first.

    The creative proposal call gets them so it can avoid repeats; this
    replaces the old catalog label/family novelty tier, which lost its input
    once the model started inventing labels (ticket #1372).
    """
    try:
        state = ag_queue.load_state(data_dir)
    except (OSError, ValueError):
        return []
    day = today or datetime.date.today()
    cutoff = (day - datetime.timedelta(days=days)).isoformat()
    out = []
    scenes = sorted(state.get("scenes", {}).values(),
                    key=lambda s: str(s.get("added") or ""), reverse=True)
    for s in scenes:
        if str(s.get("added") or "") < cutoff:
            continue
        label = str(s.get("anomaly") or "")
        if label and label not in out:
            out.append(label)
        if len(out) >= REPEAT_LABEL_LIMIT:
            break
    return out


def select_sources(data_dir: Path, count: int, seed=None) -> list:
    """Up to ``count`` random unused sources that have an image on disk."""
    unused = [s for s in ag_sources.list_sources(data_dir, unused=True)
              if s.get("image")
              and (ag_sources.sources_dir(data_dir) / s["image"]).exists()]
    rng = random.Random(seed)
    rng.shuffle(unused)
    return unused[:max(0, count)]


def moderation_rate(data_dir: Path, last: int = 20) -> dict:
    """Acceptance rate of the most recently added scenes (the ground truth).

    The report carries it so the new flow's rate can be compared against the
    pipeline it replaced as soon as Evan moderates a batch (ticket #1372).
    """
    try:
        state = ag_queue.load_state(data_dir)
        fb = ag_queue.load_feedback(data_dir)
    except (OSError, ValueError):
        return {"window": 0, "accepted": 0, "rejected": 0, "unmoderated": 0,
                "acceptance_rate": None}
    scenes = sorted(state.get("scenes", {}).values(),
                    key=lambda s: str(s.get("added") or ""), reverse=True)
    scenes = scenes[:last]
    accepted = set(fb.get("accepted", {}))
    rejected = set(fb.get("rejected", {})) | set(fb.get("excluded", {}))
    acc = sum(1 for s in scenes if s.get("id") in accepted and
              s.get("id") not in rejected)
    rej = sum(1 for s in scenes if s.get("id") in rejected)
    decided = acc + rej
    return {"window": len(scenes), "accepted": acc, "rejected": rej,
            "unmoderated": len(scenes) - decided,
            "acceptance_rate": round(acc / decided, 3) if decided else None}


# ── Run loop ───────────────────────────────────────────────────────────────

def _write_bytes(data: bytes, path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def _now_iso() -> str:
    return datetime.datetime.now().astimezone().isoformat(timespec="seconds")


def run(args) -> dict:
    """Run one generation batch, guarded by the cross-process run lock."""
    ensure_tool_path()
    if args.env:
        ag_verify.load_env(Path(args.env))
    data_dir = Path(args.data) if args.data else ag_sources.default_data_dir()
    if args.dry_run:
        return _run(args, data_dir, lock=None)
    lock = RunLock(data_dir)
    try:
        lock.acquire()
    except RunLockedError as e:
        return {"date": args.date or datetime.date.today().isoformat(),
                "data": str(data_dir), "planned": 0, "added": [],
                "failed": [], "skipped": [], "image_calls": 0,
                "dry_run": False, "locked": True, "error": str(e)}
    try:
        return _run(args, data_dir, lock=lock)
    except BaseException as e:  # noqa: BLE001 - surface the failure to the UI
        write_status(data_dir, state="error",
                     error=f"{type(e).__name__}: {e}",
                     count=args.count,
                     finishedAt=datetime.datetime.now().astimezone().isoformat(
                         timespec="seconds"))
        raise
    finally:
        lock.release()


def _run(args, data_dir: Path, lock) -> dict:
    date = args.date or datetime.date.today().isoformat()
    started = time.time()
    started_iso = _now_iso()
    report = {"date": date, "data": str(data_dir), "planned": 0, "added": [],
              "failed": [], "skipped": [], "image_calls": 0, "dry_run": False,
              "model": args.model, "image_model": args.image_model}

    def emit(state: str = "running", **extra) -> None:
        """Best-effort progress for the dashboard (see module docstring)."""
        if lock is None:
            return
        try:
            write_status(data_dir, state=state, pid=os.getpid(),
                         count=args.count, planned=report["planned"],
                         added=len(report["added"]),
                         failed=len(report["failed"]),
                         imageCalls=report["image_calls"],
                         startedAt=started_iso, **extra)
        except OSError:
            pass  # status is observability, never a reason to fail the run

    emit()

    if args.top_up and not args.dry_run:
        try:
            report["topup"] = ag_sources.top_up(
                data_dir, target=args.topup_target, batch=args.topup_batch,
                max_calls=args.topup_max_calls, date=date,
                log=lambda m: print(m, file=sys.stderr))
        except Exception as e:  # noqa: BLE001 - never block generation
            report["topup"] = {"error": str(e)}

    picked = select_sources(data_dir, args.count, args.seed)
    if not picked:
        report["error"] = ("source dataset has no unused sources; fill it "
                           "with pipeline/ag_sources.py top-up")
        emit(state="error", error=report["error"], finishedAt=_now_iso())
        return report
    if len(picked) < args.count:
        report["warning"] = (f"source pool low: {len(picked)} unused sources, "
                             f"wanted {args.count} scenes "
                             "(run pipeline/ag_sources.py top-up)")
    report["planned"] = len(picked)
    recent = recent_labels(data_dir, datetime.date.fromisoformat(date))

    if args.dry_run:
        report["dry_run"] = True
        report["plan"] = [
            {"source": s["id"], "title": clean_title(s),
             "place": scene_place(s),
             "proposal_prompt": proposal_prompt(s, recent),
             "edit_prompt_template": "add ONE <anomaly>, <placement>. "
                                     + scale_rule({"figure": False}) + " "
                                     + KEEP + " " + BLEND}
            for s in picked
        ]
        report["recent_labels"] = recent
        return report

    api_key = os.environ.get("OPENROUTER_API_KEY", "")
    if not api_key:
        report["error"] = "OPENROUTER_API_KEY not set (pass --env)"
        emit(state="error", error=report["error"], finishedAt=_now_iso())
        return report
    if not args.no_preflight:
        # Preflight (#1307): one real call before the first image call, so a
        # broken vision model fails the run loudly instead of rejecting every
        # scene after every image generation.
        try:
            report["vision_preflight"] = ag_verify.preflight_vision(
                api_key, args.model)
        except (RuntimeError, OSError) as e:
            report["error"] = f"vision preflight failed: {e}"
            emit(state="error", error=report["error"], finishedAt=_now_iso())
            return report

    out_dir = Path(args.out_dir) if args.out_dir else Path(
        tempfile.mkdtemp(prefix="ag-gen-"))
    totals = ag_llm.zero_usage()
    used_prompts = set()
    for source in picked:
        if int(totals.get("image_calls", 0)) >= args.max_generations:
            report["failed"].append({"id": source["id"],
                                     "reason": "generation budget"})
            emit()
            continue
        scene, failed = _generate_one(source, args, data_dir, date, out_dir,
                                      recent + sorted(used_prompts), totals,
                                      emit)
        if scene is not None:
            report["added"].append(scene["report"])
            used_prompts.add(scene["label"])
        elif failed is not None:
            report["failed"].append(failed)
        emit()

    report["duration_s"] = round(time.time() - started, 1)
    report["image_calls"] = int(totals.get("image_calls", 0))
    report["model_usage"] = {k: v for k, v in totals.items()
                             if k != "image_calls"}
    report["cost_total"] = round(totals["cost"], 6)
    if report["added"]:
        report["cost_per_scene"] = round(totals["cost"] / len(report["added"]), 6)
        report["seconds_per_scene"] = round(
            report["duration_s"] / len(report["added"]), 1)
    report["moderation"] = moderation_rate(data_dir)
    if report.get("error") or not report["added"]:
        message = report.get("error") or (
            f"no scenes added ({len(report['failed'])} failed)")
        emit(state="error", error=message, finishedAt=_now_iso())
    else:
        emit(state="done", finishedAt=_now_iso(),
             durationS=report["duration_s"])
    return report


def _generate_one(source: dict, args, data_dir: Path, date: str,
                  out_dir: Path, recent: list, totals: dict, emit) -> tuple:
    """One source through the whole flow; returns (scene|None, failure|None)."""
    api_key = os.environ.get("OPENROUTER_API_KEY", "")
    source_image = ag_sources.sources_dir(data_dir) / source["image"]
    if not source_image.exists():
        return None, {"id": source["id"], "reason": f"missing image "
                      f"{source_image}", "stage": "source"}
    previous_outputs = []
    trace = {"source": source["id"], "date": date, "model": args.model,
             "image_model": args.image_model, "calls": []}
    last_error = None
    for attempt in range(1, args.max_attempts + 1):
        proposal, proposal_call, errors = _attempt_proposal(
            source_image, source, recent, args, api_key)
        record_call(data_dir, trace, {"stage": "proposal", "attempt": attempt,
                                      **_call_trace(proposal_call)})
        ag_llm.add_usage(totals, proposal_call.get("usage"))
        if errors:
            last_error = {"id": source["id"], "stage": "proposal",
                          "reason": "; ".join(errors), "attempt": attempt,
                          "answer": proposal_call.get("answer", "")[:400]}
            set_trace_error(data_dir, trace, last_error["reason"])
            continue
        prompt = edit_prompt(proposal)
        if int(totals.get("image_calls", 0)) >= args.max_generations:
            last_error = last_error or {"id": source["id"],
                                        "stage": "budget",
                                        "reason": "generation budget"}
            break
        try:
            edited, image_call = _edit(source_image, prompt, args, api_key,
                                       source, totals)
        except GenerationError as e:
            last_error = {"id": source["id"], "stage": "image",
                          "reason": str(e), "attempt": attempt}
            break
        totals["image_calls"] = totals.get("image_calls", 0) + 1
        record_call(data_dir, trace, {"stage": "edit", "attempt": attempt,
                                      "prompt": prompt, **image_call})
        out_path = _write_bytes(edited,
                                out_dir / f"{source['id']}-a{attempt}.png")
        reason, hotspot = deterministic_gate(out_path, source_image,
                                             list(previous_outputs) or None)
        if reason:
            last_error = {"id": source["id"], "stage": "gate",
                          "reason": reason, "attempt": attempt}
            previous_outputs.append(out_path)
            trace.setdefault("gate_failures", []).append(
                {"attempt": attempt, "reason": reason})
            continue
        previous_outputs.append(out_path)

        loc = None
        try:
            loc = locate_anomaly(out_path, proposal, prompt, api_key,
                                 args.model, args.base_url,
                                 args.model_max_tokens, args.model_timeout,
                                 args.temperature)
        except ag_llm.LLMError as e:
            last_error = {"id": source["id"], "stage": "coordinates",
                          "reason": str(e), "attempt": attempt}
            trace.setdefault("call_errors", []).append(
                {"stage": "coordinates", "error": str(e)})
        if loc is not None:
            ag_llm.add_usage(totals, loc["call"].get("usage"))
            record_call(data_dir, trace,
                        {"stage": "coordinates", "attempt": attempt,
                         **_call_trace(loc["call"])})
        coords = loc["coords"] if loc is not None else None

        check = None
        corrected = False
        if not args.no_check:
            try:
                check = check_scene(out_path, proposal, api_key, args.model,
                                    args.base_url, args.model_max_tokens,
                                    args.model_timeout, args.temperature)
            except ag_llm.LLMError as e:
                trace.setdefault("call_errors", []).append(
                    {"stage": "check", "error": str(e)})
                check = None
            if check is not None:
                ag_llm.add_usage(totals, check["call"].get("usage"))
                record_call(data_dir, trace,
                            {"stage": "check", "attempt": attempt,
                             **_call_trace(check["call"])})
            if check is not None and check["ok"] is False \
                    and check["fix_prompt"]:
                if int(totals.get("image_calls", 0)) >= args.max_generations:
                    last_error = {"id": source["id"], "stage": "budget",
                                  "reason": "generation budget"}
                    break
                try:
                    fixed, fix_call = _edit(source_image,
                                            _fix_prompt(proposal,
                                                        check["fix_prompt"]),
                                            args, api_key, source, totals)
                except GenerationError as e:
                    last_error = {"id": source["id"], "stage": "fix",
                                  "reason": str(e), "attempt": attempt}
                    break
                totals["image_calls"] = totals.get("image_calls", 0) + 1
                record_call(data_dir, trace,
                            {"stage": "fix-edit", "attempt": attempt,
                             **fix_call})
                fixed_path = _write_bytes(
                    fixed, out_dir / f"{source['id']}-a{attempt}-fix.png")
                reason, hotspot = deterministic_gate(
                    fixed_path, source_image, list(previous_outputs) or None)
                if reason:
                    last_error = {"id": source["id"], "stage": "gate",
                                  "reason": f"after fix: {reason}",
                                  "attempt": attempt}
                    previous_outputs.append(fixed_path)
                    trace.setdefault("gate_failures", []).append(
                        {"attempt": attempt, "reason": f"after fix: {reason}"})
                    continue
                previous_outputs.append(fixed_path)
                out_path = fixed_path
                corrected = True
                try:
                    loc = locate_anomaly(out_path, proposal, prompt, api_key,
                                         args.model, args.base_url,
                                         args.model_max_tokens,
                                         args.model_timeout, args.temperature)
                except ag_llm.LLMError as e:
                    loc = None
                    trace.setdefault("call_errors", []).append(
                        {"stage": "coordinates", "after_fix": True,
                         "error": str(e)})
                if loc is not None:
                    ag_llm.add_usage(totals, loc["call"].get("usage"))
                    record_call(data_dir, trace,
                                {"stage": "coordinates", "attempt": attempt,
                                 "after_fix": True,
                                 **_call_trace(loc["call"])})
                coords = loc["coords"] if loc is not None else None
                # Close the correction loop: the fix is judged by the same
                # checker, so a not-OK verdict really can reject a scene
                # (ticket #1372). One extra cheap call per corrected scene.
                try:
                    recheck = check_scene(out_path, proposal, api_key,
                                          args.model, args.base_url,
                                          args.model_max_tokens,
                                          args.model_timeout, args.temperature)
                except ag_llm.LLMError as e:
                    recheck = None
                    trace.setdefault("call_errors", []).append(
                        {"stage": "recheck", "error": str(e)})
                if recheck is not None:
                    ag_llm.add_usage(totals, recheck["call"].get("usage"))
                    record_call(data_dir, trace,
                                {"stage": "recheck", "attempt": attempt,
                                 **_call_trace(recheck["call"])})
                    check = recheck
                    if check["ok"] is False:
                        last_error = {"id": source["id"], "stage": "check",
                                      "reason": "checker rejected after fix: "
                                                + ("; ".join(check["problems"])
                                                   or "no reason given"),
                                      "attempt": attempt}
                        set_trace_error(data_dir, trace,
                                        last_error["reason"])
                        continue
            elif check is not None and check["ok"] is False:
                last_error = {"id": source["id"], "stage": "check",
                              "reason": "checker rejected: "
                                        + ("; ".join(check["problems"])
                                           or "no fix prompt"),
                              "attempt": attempt}
                set_trace_error(data_dir, trace, last_error["reason"])
                continue

        if coords is None and hotspot is not None:
            # The coordinate call is the answer source; when it is unusable,
            # the diff hotspot still gives a deterministic click target.
            answer = {"x": round(float(hotspot["cx"]), 4),
                      "y": round(float(hotspot["cy"]), 4),
                      "r": 0.05, "fallback": "hotspot"}
            conflict = None
            trace["coordinates_fallback"] = "hotspot"
        elif coords is None:
            last_error = {"id": source["id"], "stage": "coordinates",
                          "reason": "no click target and no diff hotspot",
                          "attempt": attempt}
            continue
        else:
            answer, conflict = finalize_answer(coords, hotspot)
        if check is None:
            check_summary = {"ok": None, "problems": [], "skipped": True,
                             "corrected": corrected}
        else:
            check_summary = {"ok": check["ok"],
                             "problems": check["problems"],
                             "corrected": corrected}

        entry = build_entry(source, proposal, answer, date)
        errs = ag_queue.validate_entry(entry)
        if errs:
            last_error = {"id": source["id"], "stage": "entry",
                          "reason": "invalid entry: " + "; ".join(errs),
                          "attempt": attempt}
            set_trace_error(data_dir, trace, last_error["reason"])
            continue
        try:
            ag_queue.state_add(data_dir, entry, out_path, source_image, date)
        except Exception as e:  # noqa: BLE001 - queue reject = drop scene
            last_error = {"id": source["id"], "stage": "queue",
                          "reason": f"queue add: {e}", "attempt": attempt}
            break
        ag_sources.mark_used(data_dir, [source["id"]], True)
        write_trace(data_dir, entry["id"], trace)
        scene_report = {
            "scene": entry["id"], "source": source["id"],
            "anomaly": entry["anomaly"], "kind": proposal["kind"],
            "era": entry["year"], "place": entry["place"],
            "attempt": attempt, "answer": answer,
            "coord_conflict": conflict,
            "checker": check_summary,
        }
        return {"report": scene_report, "label": entry["anomaly"]}, None
    return None, last_error or {"id": source["id"], "reason": "no attempt ran"}


def _attempt_proposal(source_image: Path, source: dict, recent: list, args,
                      api_key: str):
    """Call 1 with its error handling; returns (proposal, call, errors)."""
    try:
        result = propose_anomaly(source_image, source, recent, api_key,
                                 args.model, args.base_url,
                                 args.model_max_tokens, args.model_timeout,
                                 args.temperature)
    except ag_llm.LLMError as e:
        call = {"model": args.model, "prompt": proposal_prompt(source, recent),
                "answer": "", "usage": ag_llm.zero_usage(), "error": str(e),
                "duration_s": 0.0}
        return None, call, [str(e)]
    return result["proposal"], result["call"], result["errors"]


def _fix_prompt(proposal: dict, fix: str) -> str:
    head = (f"Edit this photograph again: fix ONLY these problems with the "
            f"added {proposal['anomaly']}: {fix}.")
    return " ".join((head, scale_rule(proposal), KEEP, BLEND))


def _edit(source_image: Path, prompt: str, args, api_key: str, source: dict,
          totals: dict | None = None):
    """One image edit; returns (bytes, trace record)."""
    started = time.time()
    at = datetime.datetime.now().astimezone().isoformat(timespec="seconds")
    seed = random.randint(0, 2**31 - 1)
    data = image_edit(source_image, prompt, api_key, args.base_url,
                      args.image_model,
                      aspect_ratio_for(source.get("width"),
                                       source.get("height")),
                      args.image_size, seed=seed, timeout=args.image_timeout,
                      usage_out=totals)
    return data, {"prompt": prompt, "seed": seed,
                  "model": args.image_model,
                  "image": image_ref(source_image), "at": at,
                  "duration_s": round(time.time() - started, 2)}


def _call_trace(call: dict) -> dict:
    """The trace view of one model call: everything a later audit needs.

    The raw call also carries ``parsed`` (redundant with the answer text)
    and, for image edits, the seed; those stay out of the sidecar.
    """
    return {"model": call.get("model"), "prompt": call.get("prompt", ""),
            "answer": call.get("answer", ""),
            "reasoning": call.get("reasoning", ""),
            "image": call.get("image", ""),
            "at": call.get("at"),
            "usage": call.get("usage"),
            "duration_s": call.get("duration_s"),
            "error": call.get("error")}


def failure_message(report: dict) -> str:
    if report.get("error"):
        return f"AnomalyGuessr generator: {report['error']}"
    added, failed = len(report["added"]), len(report["failed"])
    lines = [f"AnomalyGuessr generator: {added} scenes added, "
             f"{failed} failed ({report.get('image_calls', 0)} image calls)."]
    if report.get("warning"):
        lines.append(report["warning"])
    for f in report["failed"][:4]:
        lines.append(f"- {f['id']}: {f.get('reason', '?')}")
    return "\n".join(lines)


def notify_discord(channel_id: str, content: str, token: str,
                   timeout: int = 30) -> bool:
    """Post a plain bot message to a Discord channel (failure alert only)."""
    if not channel_id or not token:
        return False
    payload = json.dumps({"content": content[:1900]}).encode()
    req = urllib.request.Request(
        f"https://discord.com/api/v10/channels/{channel_id}/messages",
        data=payload,
        headers={"Authorization": f"Bot {token}",
                 "Content-Type": "application/json",
                 "User-Agent": "AnomalyGuessr-generator/1.0"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return 200 <= r.status < 300
    except Exception:  # noqa: BLE001 - alerting must never break the run
        return False


def parse_args(argv=None):
    p = argparse.ArgumentParser(
        prog="ag_generate.py",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description="AnomalyGuessr generator, four-step LLM flow (#1372).")
    p.add_argument("--data", default=None,
                   help="data dir (default: repo data/anomalyguessr)")
    p.add_argument("--count", type=int, default=10,
                   help="scenes to generate (default 10)")
    p.add_argument("--seed", type=int, default=None,
                   help="source-sampling seed (default: system random)")
    p.add_argument("--date", default=None, help="queue date (default today)")
    p.add_argument("--dry-run", action="store_true",
                   help="print the selected sources and prompts, call nothing")
    p.add_argument("--env", default=None, help=".env path for API keys")
    p.add_argument("--dm-channel", default=None,
                   help="Discord channel for failure alerts")
    p.add_argument("--model", default=MODEL,
                   help=f"vision/text model for proposal, coordinates and "
                        f"check (default {MODEL})")
    p.add_argument("--image-model", default=IMAGE_MODEL)
    p.add_argument("--model-max-tokens", type=int,
                   default=DEFAULT_MODEL_MAX_TOKENS)
    p.add_argument("--model-timeout", type=int, default=DEFAULT_MODEL_TIMEOUT)
    p.add_argument("--temperature", type=float, default=DEFAULT_TEMPERATURE,
                   help="sampling temperature for the text/vision calls "
                        f"(default {DEFAULT_TEMPERATURE}; omit with "
                        "--provider-default)")
    p.add_argument("--provider-default", action="store_true",
                   help="omit the temperature (use the provider's default)")
    p.add_argument("--base-url", default=os.environ.get("OPENAI_BASE_URL")
                   or DEFAULT_BASE_URL)
    p.add_argument("--image-size", default=DEFAULT_IMAGE_SIZE)
    p.add_argument("--image-timeout", type=int, default=180)
    p.add_argument("--no-check", action="store_true",
                   help="skip the checker call (and its correction edit)")
    p.add_argument("--no-preflight", action="store_true",
                   help="skip the one-call vision preflight")
    p.add_argument("--max-attempts", type=int, default=2,
                   help="proposals per source, each a fresh generation "
                        "(run-guard #1122; default 2)")
    p.add_argument("--max-generations", type=int, default=0,
                   help="hard cap on image calls per run (0 = count*attempts)")
    p.add_argument("--out-dir", default=None,
                   help="working dir for attempts (default: temp dir)")
    p.add_argument("--report", default=None, help="write the JSON report here")
    p.add_argument("--top-up", action="store_true",
                   help="fill the source pool before picking sources")
    p.add_argument("--topup-target", type=int,
                   default=ag_sources.DEFAULT_TOPUP_TARGET,
                   help=f"unused sources the top-up aims for "
                        f"(default {ag_sources.DEFAULT_TOPUP_TARGET})")
    p.add_argument("--topup-batch", type=int,
                   default=ag_sources.DEFAULT_TOPUP_BATCH,
                   help="Quality-images category members per page")
    p.add_argument("--topup-max-calls", type=int,
                   default=ag_sources.DEFAULT_TOPUP_CALLS,
                   help="category pages per top-up")
    args = p.parse_args(argv)
    if not args.max_generations:
        args.max_generations = max(1, args.count * max(1, args.max_attempts))
    return args


def main(argv=None) -> int:
    args = parse_args(argv)
    report = run(args)
    text = json.dumps(report, indent=2)
    print(text)
    if args.report:
        _write_bytes(text.encode(), Path(args.report))

    hard_failure = bool(report.get("error")) or not report["added"]
    if (hard_failure or report.get("warning")) and args.dm_channel:
        if args.env:
            ag_verify.load_env(Path(args.env))
        notify_discord(args.dm_channel, failure_message(report),
                       os.environ.get("DISCORD_BOT_TOKEN", ""))
    return 1 if hard_failure else 0


if __name__ == "__main__":
    sys.exit(main())
