#!/usr/bin/env python3
"""AnomalyGuessr scene generator: a six-step LLM flow (tickets #1372, #1436).

Sourcing and generation are two separate problems (Evan, 2026-09-12).
Sourcing (`pipeline/ag_sources.py`) fills the pool from Commons "Quality
images" with key filters only; this script turns one pooled photo into one
queued scene.

Per scene:

1. **Propose** (`propose_anomaly`): one `deepseek/deepseek-v4.1-flash` vision
   call over the source photo. The model invents ONE subtle time-travel
   anomaly for THIS image: a real later-era object for a historical photo, a
   fictional-future element for a modern one (Evan: modern photos are fine,
   "dann nutzen wir fictional futures"). The bar is *impossibility*, not
   improbability (ticket #1403): the element must not exist in the scene's
   year. The year is a FACT from the source's catalogue record, stated in the
   prompt with its field and raw value (ticket #1430); the model never judges
   or restates the era. The model returns label, kind, the year the element
   exists from (`exists_from`), for a fictional-future element the reason it
   cannot exist in 2026 (`not_today`), a figure flag, placement, the
   impossibility reason and references. `ag_catalog.INSPIRATION` supplies
   few-shot shape examples; recently used labels are passed in to avoid
   repeats.
2. **Apply** (`image_edit`): one `google/gemini-3.1-flash-image` call that
   adds the anomaly. The generation prompt keeps the hard constraints (one
   dominant placement instruction, ONE numeric scale cap with a
   same-distance anchor, keep everything else, tone match, grain, no glow)
   and opens with a short, neutral purpose preamble (ticket #1439: this is a
   quiz game, the photo is public-domain, the addition is fictional); the
   long proscriptive rule list moved into the checker. A refused edit (the
   provider returns no image) is classified from its raw response (a safety
   block vs an empty/broken reply), retried once with a rephrase that drops
   the modify/cover wording, and, if it refuses again, treated as a
   mechanical failure while the element goes on a persisted refusal-prone
   list so the next proposal avoids it.
3. **Check** (`check_scene`): one vision call against the eight requirements
   (time-travel framing, subtlety, scale, tone, grain, keep-the-rest,
   identifiability, impossibility at the scene's time). The checker does not
   vote the scene out: it returns the numbers of the requirements it fails
   plus a repair instruction. Requirement 8 tests the element's introduction
   year against the scene's year, so an "improbable but possible" element (an
   e-scooter in 2017) fails.
4. **Correct** (`fix-edit`, at most `CORRECTION_ROUNDS` = 2): a checker that
   reports a failing requirement and a fix prompt triggers one edit built
   from that instruction, then a check again; at most one more round. A
   check with no findings ends the chain immediately (Evan 2026-09-13), and
   the last fix-edit is followed by a final check for the record only, which
   never triggers another edit.
5. **Locate** (`locate_anomaly`): one vision call over the shipped image for
   the click target (x/y/r, figure flag), with the proposal and the edit
   prompt as context. The answer is widened when the deterministic diff
   hotspot falls outside it, so a wildly wrong circle cannot ship alone.
6. **Click-target check** (at most `CLICK_TARGET_PASSES` = 2, only when the
   checker failed requirement 7, Identifiable; ticket #1445): the answer
   circle plus a centre crosshair is drawn onto the edited image, and the
   checker model returns the anomaly's bounding box in that image. The
   pipeline compares the box against the drawn ellipse deterministically; a
   corner outside it grows the answer to the smallest circle covering both,
   and a second rendered pass verifies the result. The last rendered overlay
   is kept next to the trace so moderation can see what was judged. A pass
   that never ran is recorded as `skipped` in the summary and the trace.

One source yields exactly one scene, drawn as exactly one image: `--count 5`
lands 5 scenes as long as the mechanical steps (image call, download,
landscape shape) work. A mechanically broken draw is replaced by another one
(up to `MECHANICAL_RETRIES` extra image calls per scene, every attempt in the
trace); only a source whose image call fails or whose draws all break the
pixel gates is reported as failed, never silently skipped.

Deterministic gates stay deterministic: a byte-identical re-serve is refused
(`ag_verify.identical_output_check`), a scene that is not a localized edit is
refused (`ag_verify.locate_hotspot`), an output that is not landscape is
refused before it can reach the queue gate, and the queue validator still
checks the entry schema.

Each scene gets a trace sidecar (`data/anomalyguessr/traces/<id>.json`, ticket
#1373) with every pipeline step in order: model, full prompt, answer,
reasoning content, usage (tokens + cost), duration and timestamp, with the
image named but never embedded. The stages carry the round (`edit r0`,
`check r0`, `fix-edit r1`, `check r1`, `fix-edit r2`, `check r2`,
`coordinates`, `click-target 1`, `click-target 2`), so the panel shows the
chain a scene went through; every mechanical retry is a row too (idea #1435
was that a break makes the draw numbers jump). The last checker verdict
(score, failed requirement numbers, reason) is stored with the scene as
`checker` and in the trace, so moderation and the gallery lightbox show
"checker 5/7, failed 3, 7" before any image is opened. Steps are flushed to
`traces/pending/<source>.json` as the pipeline runs, so a crash keeps a
partial record; a finished scene shows the whole flow, a scene from before
the trace existed shows none. Candidate images are not kept (only the
shipped image and the last click-target overlay live on). The scene-time
entry (`scene_time`: the catalogue year, its provenance class, the repository
field and its raw value) is in the trace too, so a moderator can check the
date without opening the repository page (tickets #1403, #1430). A source
without a catalogue year is refused before the model calls and consumed, so
it is not drawn again every day; the pool gate already keeps such sources
out. `pipeline/ag_era_audit.py` reruns the impossibility test over the whole
queue.

The run lock, the progress status file, the DM-on-failure path and the queue
add are unchanged from #1210/#1169: the daily cron and the dashboard's
"generate more" both go through this script.

CLI::

    ag_generate.py [--data DIR] [--count 10] [--seed N]
        [--dry-run] [--top-up] [--env .env] [--dm-channel ID] [--model M]
        [--image-model M] [--max-attempts 2] [--max-generations N]
        [--date YYYY-MM-DD] [--out-dir DIR] [--report FILE] [--no-check]
        [--no-preflight] [--retext]

The caption (title, place, description) is derived from the proposal and the
source's structured keys, never from raw metadata: the title drops the
uploader's timestamps and years, a coordinate pair is no place, and the era
lives in its own field instead of a "circa <year>" glued into the
description (ticket #1402). ``--retext`` re-derives the captions of the
scenes already in the queue, the repair path for the scenes the old builder
damaged.

Exit code 0 = ran (a scene that only breaks mechanically can still be
missing; the JSON report lists what failed); 1 = no scene was added or a
hard error occurred.
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
import shutil
import subprocess
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
# the source dimensions (the queue rejects non-landscape scene images). The
# square 1:1 entry was removed in #1381: a near-square source (1000x997) then
# asked for a square output, and the queue gate killed the scene late
# ("edited image must be landscape (w>h), got 1024x1024"). Every entry is
# landscape now, so 5:4 is the widest the model is ever asked for.
ASPECTS = (("16:9", 16 / 9), ("3:2", 1.5), ("4:3", 4 / 3), ("5:4", 1.25),
           ("21:9", 21 / 9), ("2:1", 2.0))
SCENE_ID_RE = re.compile(r"[^a-z0-9]+")

# One image per scene (ticket #1436, Evan): the checker no longer ranks
# candidates, it names what is wrong and the pipeline fixes that at most
# CORRECTION_ROUNDS times. MECHANICAL_RETRIES stays: a wrong aspect ratio or
# a byte-identical re-serve is not a quality question, so a broken draw is
# replaced by a new draw instead of shrinking the planned scene count.
MECHANICAL_RETRIES = 2
CORRECTION_ROUNDS = 2
# The click-target pass only runs when the checker failed requirement 7
# (Identifiable; index 6 in REQUIREMENTS), the one finding that actually
# questions whether the answer area can be found (ticket #1445). On 37
# stored scenes a per-scene pass corrected nothing while every drawn answer
# was consistent with the model's own localization, so the bare call was
# dropped. When it does run it is box-based: the model localizes the
# element, the pipeline grows the drawn area deterministically.
CLICK_TARGET_PASSES = 2
CLICK_TARGET_REQUIREMENT = 7
# The overlay that is handed to the model: a stroke this wide relative to the
# image height is unambiguous but still leaves the photo readable.
CLICK_TARGET_STROKE_FRACTION = 0.004

# How recently used anomaly labels feed the proposal prompt (the free-form
# replacement for the old catalog label/family novelty tier, #1328).
REPEAT_WINDOW_DAYS = 7
REPEAT_LABEL_LIMIT = 15

# Run lock + progress status (ticket #1210). Both live in the data dir next
# to state.json/feedback.json; they are runtime artifacts, not committed.
LOCK_NAME = "generate.lock"
STATUS_NAME = "generate-status.json"
TRACE_DIRNAME = "traces"
# Element labels the image provider refused to edit (ticket #1439): persisted
# so the next proposals avoid them for a while instead of re-proposing the
# element the provider just blocked.
REFUSED_ELEMENTS_NAME = "refused_elements.json"


class GenerationError(RuntimeError):
    """A model call or a deterministic gate failed for one attempt."""


class ImageCallFailed(GenerationError):
    """A failed image attempt whose cost still belongs to the run (#1435).

    Carries the attempt's trace record (prompt, seed, model, duration,
    usage), so the caller writes a ``calls`` row for it instead of letting
    the attempt vanish into ``call_errors``: a refused edit ("no images
    returned") is billed but produces no image, and without a row the retry
    made the draw numbers jump and the run total understate its spend.
    """

    def __init__(self, message: str, record: dict):
        super().__init__(message)
        self.record = record


class ImageRefused(GenerationError):
    """The provider answered but returned no image (ticket #1439).

    A refusal is not the same as a mechanical failure: the classification
    (``safety`` for a provider block, ``empty``/``broken`` for a reply that
    simply carried no image) decides whether the element gets one framed
    retry and is marked refusal-prone, or the draw is just replaced.
    """

    def __init__(self, detail: dict):
        self.detail = detail or {}
        self.kind = str(self.detail.get("kind") or "empty")
        super().__init__(f"no images returned ({self.kind})")


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

# The purpose preamble that every image-edit call now carries (ticket #1439).
# Evan measured 12 refusals in 85 edits ("gemini seems to refuse to edit stuff
# a lot"); one neutral sentence about the game and the public-domain source
# states what the picture is for. No pleading and no "this is not for
# nefarious purposes": that phrasing reads as suspicious.
PURPOSE = ("This is an image edit for a spot-the-anachronism quiz game: the "
           "photograph is a public-domain historical picture from a public "
           "archive, and the small addition is a fictional element for the "
           "puzzle; no real, identifiable person is changed or demeaned, and "
           "the result is shown only as a game image.")
# The one retry wording after a refusal (ticket #1439): most refused edits
# changed an existing object, so the retry drops the modify/cover framing and
# asks for a small separate object placed in the scene.
REFUSAL_RETRY = ("Add the new element as a small separate object that sits "
                 "in the scene; do not modify, cover, replace or remove "
                 "anything that is already in the photograph.")

# Fictional-future allowed list (ticket #1430, Evan 2026-09-13): the path
# stays, but only for elements that are impossible in the scene's year *and*
# today. Everything on the second list exists in 2026, so it can never carry
# a modern scene however futuristic it looks.
FUTURE_ALLOWED = (
    "a spacecraft, or an unmistakably alien being or artefact",
    "a humanoid robot in everyday life (only one that cannot exist in 2026; "
    "household and industrial robots already do)",
    "a device you can argue coherently does not exist in 2026",
)
TODAY_TRAPS = (
    "delivery drones and quadcopters", "e-scooters", "smartphones and tablets",
    "QR codes", "LED and solar technology", "modern clothing", "e-bikes",
)

# The checker's requirement list: the hard-won rules of the agent era, moved
# here from the generation prompt. A violation is caught and corrected
# instead of being pre-empted by an ever-longer recipe. One tuple entry per
# requirement, because the checker's score IS the number of requirements a
# candidate satisfies (ticket #1381): a fixed list keeps every candidate and
# every run on the same scale.
REQUIREMENTS = (
    ("Time travel only",
     "a real element from a LATER era than the photograph, or a clearly "
     "futuristic one. Pure fantasy (flying saucers, dragons, unicorns, "
     "ghosts, magic) is a failure."),
    ("Subtle",
     "one small element, not centered, not the largest thing in the frame, "
     "not a focal point; partly hidden or at the edge is best."),
    ("Scale",
     "realistic for its position, judged against something at the same "
     "distance. A small object must stay under about 3 percent of the image "
     "height; a person roughly 8-15 percent, never a giant."),
    ("Tone",
     "exactly the photograph's tone and coloration (a grayscale photo stays "
     "grayscale); no sepia, no color cast, no filter."),
    ("Grain and light",
     "grain lies over the added element; soft edges; consistent lighting and "
     "shadow direction; realistic perspective; no glow; it must look "
     "photographed, not pasted."),
    ("Everything else unchanged",
     "same composition, people, goods and background; nothing else redrawn, "
     "moved or recolored."),
    ("Identifiable",
     "the added element must be findable as the anachronism, not so tiny or "
     "so blended that a player cannot find it."),
    ("Impossible at the scene's time",
     "the element must be IMPOSSIBLE, not merely unusual, in the "
     "photograph's year (stated at the top). Its introduction must be "
     "strictly later than that year (real later era), or it must be "
     "explicitly futuristic, i.e. it does not exist even today. An object "
     "that existed in that year but was only rare, or whose introduction is "
     "the same year as the photograph, is a failure: a player can just say "
     "it belongs. Something that exists in 2026 is never an anomaly, "
     "whatever the photograph's year: for edge cases (drones, robots, AI "
     "devices, electronics) say why the element does not exist in 2026, and "
     "fail it when you are not certain."),
)
REQUIREMENTS_TOTAL = len(REQUIREMENTS)


def requirements_text() -> str:
    return "\n".join(f"{i}. {name}: {detail}"
                     for i, (name, detail) in enumerate(REQUIREMENTS, 1))


def proposal_prompt(source: dict, recent=()) -> str:
    """Call 1's prompt: one anomaly, impossible in the catalogue year.

    Ticket #1430: the year is a fact the prompt *states*, never one the model
    judges (the old "Judge the photo's apparent era yourself" line made the
    catalogue year decoration). The fictional-future path stays, but only for
    elements that are impossible in the scene's year *and* today.
    """
    year, field = ag_sources.source_year(source)
    _y, _f, year_source, year_raw = ag_sources.year_provenance(source)
    where = year_source or field or "the item's catalogue record"
    lines = [
        "You are the content designer for a spot-the-anachronism game: "
        "players get a real photograph and must find the ONE thing that does "
        "not belong to its time.",
        "This photograph comes from "
        f"{source.get('repository') or 'a public archive'}.",
        f"This photograph was taken in {year}. The year is a fact from the "
        f"item's catalogue date ({where}: {year_raw or year}). Do not judge "
        "or restate the era.",
        f"The ONE anomaly you invent must be IMPOSSIBLE in {year}, not merely "
        "unusual or rare.",
        "- A clear plastic bottle in a 1900 photograph is IMPOSSIBLE: the "
        "bottle did not exist yet, so no player can explain it away (valid).",
        "- An e-scooter in a 2017 photograph is only unusual, not impossible: "
        "scooters existed then, so a player can just say it belongs "
        "(invalid; pick something that did not exist yet).",
        "Invent ONE anomaly to hide in THIS photograph:",
        f"- If the photograph clearly predates {year}, the anomaly is a real "
        f"object, garment or vehicle from a LATER era (strictly after {year}).",
        "- If the photograph is modern, the anomaly is a fictional-future "
        "element that does not exist even today. Allowed: "
        + "; ".join(FUTURE_ALLOWED) + ".",
        "  These exist in 2026 and can NEVER carry a modern scene: "
        + ", ".join(TODAY_TRAPS) + ". A photo from 2019 with a delivery drone "
        "is possible, so it is invalid.",
        "- It must be ONE small, concrete thing that could plausibly sit in "
        "this scene: an object, or one extra person whose only modern or "
        "futuristic tell is a small detail (for a person, the year their "
        "modern tell became available).",
        "- Say how the element enters the scene (placement_kind): "
        '"standalone" when it is its own object standing or lying in the '
        'scene, "modification" when it changes an object that is already '
        'there. A modification is welcome: describe it as adding a small '
        'object that sits on that surface ("a small sticker that sits on '
        'the sign"), never as modifying, covering or replacing the object.',
        "- Pure fantasy is out: no flying saucers, dragons, unicorns, ghosts "
        "or magic. Everything must read as a thing from another time.",
        "- Keep it subtle: findable, but not obvious.",
        "Name the scene as well: one short, factual title for THIS photo "
        "(what a caption in a museum would say; no year, no file name, no "
        "archive or uploader metadata).",
        "Style examples (do not copy them; they only show the shape):",
    ]
    lines += [f"- {line}" for line in ag_catalog.inspiration_lines()]
    if recent:
        lines.append("Avoid these anomalies used by recent scenes: "
                     + "; ".join(str(r) for r in recent) + ".")
    lines.append(
        'Answer as strict JSON only, no prose: {"anomaly": "<short label>", '
        '"kind": "later-era"|"fictional-future", "exists_from": "<the year or '
        'era from which the element exists; for a futuristic element say '
        '\\"not real yet, a fictional future\\">", "not_today": "<for '
        'fictional-future only: why this element cannot exist in 2026; empty '
        'string otherwise>", "title": "<short human title of the scene, at '
        'most 8 words>", "figure": true|false, "placement_kind": '
        '"standalone"|"modification", "placement": "<one sentence: '
        'where in THIS photo it sits, how it is partly hidden, and how large '
        'it should look next to things at the same distance>", "explanation": '
        '"<one sentence: why it cannot exist in the scene\'s year>", '
        '"references": [{"label": "<source name>", "url": "https://..."}]}')
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


def edit_prompt(proposal: dict, retry: bool = False) -> str:
    """The image-edit instruction; ``retry`` is the post-refusal rephrase.

    Ticket #1439: every edit call carries the purpose preamble. The retry
    variant keeps it and adds the one wording change (a small separate
    object) for the case the model refused the framed first attempt.
    """
    head = (f"Edit this historical photograph: add ONE "
            f"{proposal['anomaly']}, {proposal['placement']}.")
    parts = [PURPOSE, head]
    if retry:
        parts.append(REFUSAL_RETRY)
    parts += [scale_rule(proposal), KEEP, BLEND]
    return " ".join(parts)


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


def click_target_prompt(proposal: dict) -> str:
    """The click-target check's prompt (tickets #1436, #1445): where is it?

    The image handed in is the shipped scene with the candidate answer drawn
    on it. The model only *localizes* the element: it returns a bounding box,
    the pipeline does the geometry. Ticket #1445: the earlier wording asked
    the model to repeat or correct the drawn x/y/r and it echoed the numbers
    (0 of 5 corrections), while a box is a task it can actually answer.
    """
    return "\n".join([
        "You are checking the answer area of a spot-the-anachronism game.",
        "The image you see is the scene with the candidate answer drawn on "
        "it: an ellipse outlines the answer area and a small crosshair marks "
        "its centre.",
        f"The element that does not belong to the photograph's time is: "
        f"{proposal['anomaly']} ({proposal['placement']}).",
        "Return the bounding box of that element as it appears in THIS "
        "image, generous enough to include all of it (for a person: head to "
        "feet, shoes included).",
        'Answer as strict JSON only: {"x1": <0..1>, "y1": <0..1>, '
        '"x2": <0..1>, "y2": <0..1>, "figure": true|false, "reason": "<one '
        'line>"}, where x1,y1 is the top-left corner and x2,y2 the '
        "bottom-right corner of the box (normalized: x across the width, y "
        "down the height).",
    ])


def scene_time_text(scene: dict | None) -> str:
    """The scene's year for the checker, from the catalogue fact.

    Ticket #1430: there is exactly one year source (the repository
    catalogue), so this never falls back to a model judgement. An empty
    string means no year, which the pool gate prevents.
    """
    if not scene or scene.get("year") is None:
        return ""
    where = scene.get("source") or scene.get("field") or "the item's catalogue"
    return (f"The photograph was taken in {scene['year']} (catalogue year, "
            f"from {where}).")


def check_prompt(proposal: dict, scene: dict | None = None) -> str:
    lines = [
        "You are the quality checker for a spot-the-anachronism game.",
    ]
    time_text = scene_time_text(scene)
    if time_text:
        lines.append(time_text)
    lines.append(
        "The image you see should be the original photograph with ONE "
        f"element added: {proposal['anomaly']} ({proposal['placement']}).")
    lines.append(
        f"The proposal says the element exists from "
        f"{proposal.get('exists_from') or 'an unknown year'} and claims: "
        f"{proposal.get('explanation')}")
    lines.append("Judge it against these requirements, each one on its own:")
    lines.append(requirements_text())
    lines.append(
        "Requirement 8 uses the photograph's year stated above: check the "
        "introduction date, not whether the element merely looks out of "
        "place.")
    if proposal.get("not_today"):
        lines.append("The proposal's reason it cannot exist in 2026: "
                     + str(proposal["not_today"]))
    lines.append(
        "Requirement 8 also means: an element that exists in 2026 is never "
        "an anomaly. For drones, robots, AI devices and electronics, check "
        "that reason: fail it when the element is on the market today.")
    lines.append(
        "You are scoring, not voting: never reject the whole image, "
        "just say which numbered requirements it fails.")
    lines.append(
        'Answer as strict JSON only: {"failed": [<numbers of the '
        'requirements it violates, in rising order, [] when it meets '
        'all of them>], "reason": "<one line: the decisive reason for '
        'the score>", "fix_prompt": "<one self-contained instruction '
        'that would fix the failed requirements, or empty when none '
        'failed>"}')
    return "\n".join(lines)


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
    """Why a proposal cannot be rendered into a scene, [] when it can.

    The scene title is optional: when the model leaves it out (or sends
    something that is not a string), the cleaned Commons name is the
    fallback, so a missing title must not cost an attempt (ticket #1402).
    """
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
    if not isinstance(proposal.get("exists_from"), str) or \
            not proposal["exists_from"].strip():
        errs.append("exists_from must be a non-empty string")
    # Ticket #1430: a fictional-future element must justify why it cannot
    # exist today, because everything that exists in 2026 is possible in a
    # modern photo's year and therefore not an anomaly.
    if proposal.get("kind") == "fictional-future" and (
            not isinstance(proposal.get("not_today"), str)
            or not proposal["not_today"].strip()):
        errs.append("not_today must say why the element cannot exist in "
                    "2026 (required for kind=fictional-future)")
    if not isinstance(proposal.get("placement"), str) or \
            not proposal["placement"].strip():
        errs.append("placement must be a non-empty string")
    if not isinstance(proposal.get("explanation"), str) or \
            not proposal["explanation"].strip():
        errs.append("explanation must be a non-empty string")
    return errs


def placement_kind(proposal: dict) -> str:
    """The proposal's placement kind, "standalone" when it is missing.

    Ticket #1439: the refusal rate gets measured split by placement kind
    (a standalone object in the scene vs a change to an existing object).
    """
    kind = str((proposal or {}).get("placement_kind") or "").strip().lower()
    return kind if kind in ("standalone", "modification") else "standalone"


def normalize_proposal(proposal: dict) -> dict:
    """Whitespace-collapse and cap the proposal's free-text fields."""
    out = dict(proposal)
    for key, limit in (("anomaly", 80), ("exists_from", 60),
                       ("not_today", 400),
                       ("placement", 400), ("explanation", 400)):
        out[key] = ag_llm.clean_text(out.get(key), limit)
    out["title"] = clean_caption_title(out.get("title"))[:80]
    out["figure"] = bool(out.get("figure"))
    out["placement_kind"] = placement_kind(out)
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


def check_click_target(image: Path, proposal: dict, api_key: str,
                       model: str, base_url: str, max_tokens: int, timeout: int,
                       temperature: float | None) -> dict:
    """The click-target check (tickets #1436, #1445): where is the element?

    ``image`` is the rendered overlay. ``box`` is the anomaly's bounding box
    when the model returned usable numbers, else None; ``reason`` is its
    one-line note. The coverage decision is deterministic and lives in the
    caller (ticket #1445).
    """
    prompt = click_target_prompt(proposal)
    result = _run_call(prompt, image, api_key, model, base_url, max_tokens,
                       timeout, temperature)
    parsed = result["parsed"] or {}
    return {"box": valid_box(parsed),
            "reason": ag_llm.clean_text(parsed.get("reason"), 300),
            "call": result}


def render_click_target(image: Path, answer: dict, out_path: Path) -> Path:
    """Draw the answer area onto a copy of the image (ticket #1436).

    The game's answer is a circle in *normalized* coordinates (r is a
    fraction of the image height), so in pixel space it is an ellipse with
    radii r*W and r*H; the centre crosshair marks the exact point. The stroke
    is opaque and scaled to the image height, so the model can see the area
    without the photo becoming unreadable.
    """
    w, h = ag_verify.image_dims(image)
    cx, cy = answer["x"] * w, answer["y"] * h
    rx, ry = max(1.0, answer["r"] * w), max(1.0, answer["r"] * h)
    stroke = max(3, round(h * CLICK_TARGET_STROKE_FRACTION))
    arm = max(6.0, 0.25 * min(rx, ry))
    out_path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["convert", str(image),
         "-stroke", "rgba(255,0,0,1)", "-strokewidth", str(stroke),
         "-fill", "none",
         "-draw", f"ellipse {cx:.1f},{cy:.1f} {rx:.1f},{ry:.1f} 0,360",
         "-stroke", "rgba(0,255,255,1)",
         "-draw", (f"line {cx:.1f},{cy - arm:.1f} {cx:.1f},{cy + arm:.1f} "
                   f"line {cx - arm:.1f},{cy:.1f} {cx + arm:.1f},{cy:.1f}"),
         str(out_path)],
        check=True, capture_output=True)
    return out_path


def failed_requirements(raw) -> list:
    """Requirement numbers the checker flagged, sorted and de-duplicated.

    Tolerates ints and numeric strings (models return both); drops anything
    that is not one of the seven requirement numbers, so a hallucinated
    number cannot inflate the failure count.
    """
    if not isinstance(raw, list):
        return []
    out = set()
    for item in raw:
        try:
            n = int(str(item).strip())
        except (TypeError, ValueError):
            continue
        if 1 <= n <= REQUIREMENTS_TOTAL:
            out.add(n)
    return sorted(out)


def score_from_failed(failed: list) -> int:
    """The comparable score: requirements met, 0..7 (ticket #1381)."""
    return max(0, REQUIREMENTS_TOTAL - len(failed))


def check_scene(image: Path, proposal: dict, api_key: str, model: str,
                base_url: str, max_tokens: int, timeout: int,
                temperature: float | None, scene: dict | None = None) -> dict:
    """Call 4: which requirements the candidate fails, and how to fix them.

    The score is ``REQUIREMENTS_TOTAL - len(failed)``, computed in code from
    the requirement numbers, not from a model-arithmetic field: that is the
    comparable scale for best-of-k selection (ticket #1381). ``scene`` is the
    ``scene_time`` anchor, so requirement 8 can test the element's
    introduction year against the photograph's year (ticket #1403).
    """
    prompt = check_prompt(proposal, scene)
    result = _run_call(prompt, image, api_key, model, base_url, max_tokens,
                       timeout, temperature)
    parsed = result["parsed"] or {}
    failed = failed_requirements(parsed.get("failed"))
    usable = isinstance(parsed.get("failed"), list)
    return {"ok": (not failed) if usable else None,
            "score": score_from_failed(failed) if usable else None,
            "failed": failed,
            "reason": ag_llm.clean_text(parsed.get("reason"), 300),
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


def valid_box(raw) -> dict | None:
    """A usable anomaly bounding box from the click-target call (ticket #1445).

    Normalized corners with x1<x2 and y1<y2, each within the frame and
    between the localization gates ag_verify uses elsewhere (a single point
    or a near-full-frame box is a mis-parse, not an object). Anything
    malformed is None, which leaves the drawn answer untouched.
    """
    if not isinstance(raw, dict):
        return None
    try:
        x1, y1 = float(raw["x1"]), float(raw["y1"])
        x2, y2 = float(raw["x2"]), float(raw["y2"])
    except (KeyError, TypeError, ValueError):
        return None
    if not all(0.0 <= v <= 1.0 for v in (x1, y1, x2, y2)):
        return None
    w, h = x2 - x1, y2 - y1
    if w < ag_verify.MIN_BOX_EXTENT or h < ag_verify.MIN_BOX_EXTENT:
        return None
    if max(w, h) > ag_verify.MAX_BOX_EXTENT:
        return None
    return {"x1": x1, "y1": y1, "x2": x2, "y2": y2,
            "figure": bool(raw.get("figure"))}


def box_answer(box: dict) -> dict:
    """The answer circle covering an anomaly box (ticket #1445).

    Reuses the coordinate path's half-diagonal rule
    (``ag_verify.box_to_answer``), so a box-based correction has the same
    shape as every other answer.
    """
    answer = ag_verify.box_to_answer(
        [box["x1"], box["y1"], box["x2"], box["y2"]],
        person=bool(box.get("figure")))
    answer["figure"] = bool(box.get("figure"))
    return answer


def answer_covers_box(answer: dict, box: dict) -> bool:
    """Whether the drawn ellipse contains every corner of the box (#1445).

    The answer is a circle in normalized coordinates (r is a fraction of the
    image height), so the test is Euclidean there; a corner just outside the
    ellipse counts as not covered.
    """
    r = float(answer["r"])
    if r <= 0:
        return False
    return all(
        ((x - answer["x"]) / r) ** 2 + ((y - answer["y"]) / r) ** 2 <= 1.0
        for x in (box["x1"], box["x2"]) for y in (box["y1"], box["y2"]))


def covering_answer(a: dict, b: dict) -> dict:
    """Smallest answer circle containing both a and b (ticket #1445).

    A correction never drops the region the coordinate call already claimed;
    it only adds the box's area. When neither circle contains the other, the
    minimal covering circle sits on the line between the two centres.
    """
    dx, dy = b["x"] - a["x"], b["y"] - a["y"]
    d = math.hypot(dx, dy)
    figure = bool(a.get("figure") or b.get("figure"))
    if d + b["r"] <= a["r"]:
        return dict(a)
    if d + a["r"] <= b["r"]:
        return dict(b)
    r = (d + a["r"] + b["r"]) / 2
    if d == 0:
        return {"x": a["x"], "y": a["y"], "r": r, "figure": figure}
    t = (r - a["r"]) / d
    return {"x": a["x"] + dx * t, "y": a["y"] + dy * t, "r": r,
            "figure": figure}


def clamp_answer(coords: dict) -> dict:
    """The stored answer shape: figure floor + the queue's radius window.

    Ticket #1308 floors a person's circle at ``PERSON_MIN_RADIUS`` so head and
    shoes stay clickable; the maximum is the queue validator's cap. Every
    answer path goes through this, including a click-target correction
    (ticket #1436).
    """
    x, y, r = coords["x"], coords["y"], coords["r"]
    if coords.get("figure"):
        r = max(r, ag_verify.PERSON_MIN_RADIUS)
    r = max(0.02, min(r, ag_verify.MAX_ANSWER_RADIUS))
    return {"x": round(x, 4), "y": round(y, 4), "r": round(r, 4)}


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


def candidate_gate(edited: Path, original: Path, dedup) -> tuple:
    """(reason, hotspot) for one candidate: shape first, then pixels.

    Ticket #1381: a non-landscape output is caught HERE instead of late in
    `ag_queue.copy_images`, so a broken draw costs one candidate slot and
    never a whole scene. Returns (None, hotspot) for a shippable candidate.
    """
    try:
        w, h = ag_queue.identify_size(edited)
    except (OSError, subprocess.SubprocessError) as e:  # pragma: no cover
        return f"unreadable output ({type(e).__name__})", None
    if w <= h:
        return f"non-landscape output ({w}x{h})", None
    return deterministic_gate(edited, original, dedup)


# ── Image edit ─────────────────────────────────────────────────────────────

# Finish reasons that mean the provider's own filter stopped the reply
# (ticket #1439). Anything else that still produced no image is an empty
# reply, not a safety block.
REFUSAL_FINISH_REASONS = {"content_filter", "safety", "recitation",
                          "blocked", "prohibited_content", "image_safety"}
_SAFETY_KEYS = ("safety", "safety_ratings", "safetyRatings",
                "content_filter", "contentFilter", "blocked", "policy",
                "refusal")


def refusal_detail(body: dict, choices: list) -> dict:
    """Classify an edit that came back without an image (ticket #1439).

    ``kind`` is ``safety`` when the provider's own signal says it blocked the
    edit (a ``refusal`` field, safety metadata or a filtering finish reason),
    ``broken`` when the response carries no choices at all, and ``empty`` for
    a normal-looking reply that simply has no image. The raw provider bits
    (finish reason, refusal text, safety metadata, answer text) ride along so
    the trace and the run report can show what actually came back.
    """
    choice = choices[0] if choices and isinstance(choices[0], dict) else {}
    msg = choice.get("message") if isinstance(choice.get("message"), dict) \
        else {}
    finish = choice.get("finish_reason") or choice.get("native_finish_reason")
    safety = {}
    for src in (msg, body):
        for key in _SAFETY_KEYS:
            if key in src and src[key]:
                safety[key] = src[key]
    if not choices:
        kind = "broken"
    elif safety or str(finish or "").lower() in REFUSAL_FINISH_REASONS:
        kind = "safety"
    else:
        kind = "empty"
    return {"kind": kind, "finish_reason": finish,
            "native_finish_reason": choice.get("native_finish_reason"),
            "text": ag_llm.content_of(body)[:500],
            "refusal": str(msg.get("refusal") or "")[:500],
            "safety": safety}


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
        raise ImageRefused(refusal_detail(body, choices))
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


# ── Caption text (ticket #1402) ────────────────────────────────────────────
#
# The Commons object name is uploader metadata, not a scene title: it carries
# the artist's bracketed year ("(2017)"), the uploader's duplicate stamp
# ("2025-08-25 02"), Wikidata bookkeeping ("label QS:Len,\"…\"") and archive
# suffixes. All of it leaked into the reported scene's caption, together with
# a coordinate pair as the place and a second "circa 2010" in the
# description, so one screen showed three years. These cleaners strip the
# noise; ag_queue.caption_problems refuses an entry that still carries it.

# A trailing upload stamp in either shape ("2025-08-25 02", "20210223").
_UPLOAD_STAMP_RE = re.compile(
    r"\b\d{4}-\d{2}-\d{2}(?:[ T]\d{1,2}(?::\d{2}(?::\d{2})?)?)?\b"
    r"|\b(?:19|20)\d{6}\b")
# An era glued to the title: "(2017)", "(c. 1905)", "(1890-1900)", "c 1900".
_PAREN_YEAR_RE = re.compile(
    r"\s*\(\s*(?:c\.?|ca\.?|circa\s+)?(?:\d{3,4}s?|\d{3,4}\s*[-–]\s*\d{3,4})"
    r"\s*\)")
_ERA_PREFIX_YEAR_RE = re.compile(
    r"\b(?:c\.?|ca\.?|circa|about|around)\s+"
    r"(?:\d{3,4}s?|\d{3,4}\s*[-–]\s*\d{3,4})\b", re.I)
_YEAR_RE = re.compile(r"(?<!\d)(?:1[0-9]\d{2}|20\d{2})(?:s\b)?(?!\d)")
# Wikidata glue in an ObjectName: "… title QS:P1476,it:\"Mercato di
# Firenze\" label QS:Lit,\"…\" label QS:Len,\"Market of Florence\"". The
# label block names the file in other languages and always closes the name,
# so it and everything after it goes.
_QS_TAIL_RE = re.compile(r"\s*\b(?:label|title|description)\s+QS:.*$",
                         re.I | re.S)
_QS_ID_RE = re.compile(r"\s*\bQS:[^\s,]*")
# Archive/gallery bookkeeping suffixes: " - DPLA - <hash>", " - Flickr - …".
_ARCHIVE_SUFFIX_RE = re.compile(
    r"\s*-\s*(?:DPLA|LOC|NARA|Flickr)\s*-\s*.+$", re.I)


def _balance_quotes(t: str) -> str:
    """No half quote survives: a wrapping pair goes, an odd one is dropped.

    The old cleaner stripped quotes at the string's ends, which is exactly
    how the reported title lost the opening quote of the artwork's name and
    kept its closing one.
    """
    t = t.strip()
    if t.count('"') == 2 and t.startswith('"') and t.endswith('"'):
        t = t[1:-1]
    while t.count('"') % 2:
        i = t.find('"')
        t = t[:i] + " " + t[i + 1:]
    return t


def clean_caption_title(text) -> str:
    """The scene title from a raw name; "" when nothing readable is left."""
    t = strip_html(str(text or ""))
    t = re.sub(r"^File:", "", t).strip()
    t = re.sub(r"\.(jpe?g|png|gif|webp|tiff?)$", "", t, flags=re.I)
    t = t.replace("_", " ")
    t = _QS_TAIL_RE.sub("", t)
    t = _QS_ID_RE.sub(" ", t)
    t = _UPLOAD_STAMP_RE.sub(" ", t)
    t = _PAREN_YEAR_RE.sub(" ", t)
    t = _ERA_PREFIX_YEAR_RE.sub(" ", t)
    # Any remaining four-digit year, including a decade ("1900s"): the era
    # has its own field and must not be a second year in the caption.
    t = _YEAR_RE.sub(" ", t)
    t = _ARCHIVE_SUFFIX_RE.sub("", t)
    t = re.sub(r"\s*-\s*[0-9a-f]{16,}\s*$", "", t, flags=re.I)
    t = re.sub(r"\s*-\s*\d{6,}\s*$", "", t)
    t = _balance_quotes(t)
    # Separators left dangling by the removals.
    t = re.sub(r"\s*[,;]\s*\)", ")", t)
    t = re.sub(r"\(\s*\)|\[\s*\]", "", t)
    t = re.sub(r"\s+([,;·|])", r"\1", t)
    t = re.sub(r"\s*,\s*(?:,\s*)+", ", ", t)
    t = re.sub(r"\s*[,;·|]\s*$", "", t)
    t = re.sub(r"\s+", " ", t).strip(" ,;:-–")
    return t


def clean_title(source: dict) -> str:
    """The scene title shown when the proposal names none."""
    return clean_caption_title(source.get("originalTitle")) or "Photograph"


def scene_title(source: dict, proposal: dict) -> str:
    """The displayed title: the proposal's short human title, cleaned.

    The proposal call sees the photo, so it can name the scene; the Commons
    object name is only the fallback (ticket #1402).
    """
    return clean_caption_title(proposal.get("title")) or clean_title(source)


def scene_place(source: dict) -> str:
    """Place from the source's structured keys, "" when they carry none.

    An empty string is the honest shape for "the keys say nothing": a
    placeholder like the old "Unidentified location" reads as a fact and had
    to be hidden again by every reader (tickets #1372, #1378). A coordinate
    pair is dropped the same way instead of reading as a place (#1402).
    """
    return ag_queue.clean_place(source.get("place"))


def _year_int(text) -> int | None:
    """The four-digit year in a text, or None (ticket #1403)."""
    m = re.search(r"(?<!\d)(1[0-9]\d{2}|20\d{2})(?!\d)", str(text or ""))
    return int(m.group(1)) if m else None


def scene_time(source: dict) -> dict:
    """The scene's year: the source's catalogue date, nothing else (#1430).

    Returns the year plus its provenance for the trace: the class
    (``catalog``), the repository field and its raw value, so a moderator can
    check the date without opening the repository page. ``year`` is None only
    for a source that slipped past the pool gate; the caller refuses it
    (``_generate_one``), so no model judgement can stand in.
    """
    year, field, source_field, raw = ag_sources.year_provenance(source)
    if year is None:
        return {"year": None, "display": "", "origin": "", "field": field,
                "source": source_field, "raw": raw}
    return {"year": year, "display": str(year), "origin": "catalog",
            "field": field, "source": source_field, "raw": raw}


def caption_description(title: str, place: str, repository: str) -> str:
    """One clean caption line from the scene's own fields (ticket #1402).

    The era is deliberately absent: it has its own field and the frontend
    renders it next to the place, so gluing "circa <year>" in here showed a
    second year. Parts are joined with "·", not run together with commas,
    and a part that already sits in the title is dropped.
    """
    bits = [title.rstrip(".")]
    head = place.split(",")[0].strip().lower()
    if place and head and head not in title.lower():
        bits.append(place)
    if repository and repository.lower() not in title.lower():
        bits.append(repository)
    return " · ".join(b for b in bits if b) + "."


def build_description(source: dict, title: str, place: str) -> str:
    return caption_description(title, place,
                               source.get("repository") or "an archive")


def build_credit(source: dict) -> str:
    raw = source.get("raw") or {}
    artist = strip_html(str(raw.get("artist") or ""))
    repo = source.get("repository") or "unknown repository"
    return f"{artist} via {repo}" if artist else repo


def build_entry(source: dict, proposal: dict, answer: dict, date: str,
                scene: dict | None = None, checker: dict | None = None) -> dict:
    eid = scene_id(source["id"], proposal["anomaly"])
    st = scene if scene is not None else scene_time(source)
    year = st["display"]
    place = scene_place(source)
    entry = ag_catalog.entry_for_label(proposal["anomaly"])
    if entry is not None:
        explanation = entry["explanation"]
        refs = [dict(r) for r in entry["references"]]
    else:
        explanation = proposal["explanation"]
        refs, _ = resolve_references(proposal, source)
    family = ag_catalog.family_of(proposal["anomaly"]) or "other"
    title = scene_title(source, proposal)
    source_block = {k: source.get(k, "") for k in (
        "repository", "fileUrl", "originalTitle", "date", "place", "license",
        "description")}
    out = {
        "id": eid,
        "title": title,
        "place": place,
        "year": year,
        "description": build_description(source, title, place),
        "anomaly": proposal["anomaly"],
        "family": family,
        "answer": answer,
        "explanation": explanation,
        "references": refs,
        "source": source_block,
        "credit": build_credit(source),
        "sourceUrl": source.get("fileUrl", ""),
    }
    # The last checker verdict travels with the scene (ticket #1436), so
    # moderation and the gallery lightbox can show "checker 5/7, failed 3, 7"
    # without opening any image. A run without the checker (--no-check, or a
    # failed check call) stores no field.
    if checker is not None and checker.get("score") is not None:
        out["checker"] = {"score": checker["score"],
                          "failed": list(checker.get("failed") or []),
                          "reason": checker.get("reason") or ""}
    return out


# ── Caption repair path (ticket #1402) ─────────────────────────────────────


def retext_entry(scene: dict) -> tuple:
    """Re-derive the damaged parts of one queued scene's caption.

    Returns ``(fixed entry, changed keys)``. A clean scene comes back
    untouched: the repair must not overwrite a narrative description (many
    older queued scenes carry one) just because it could be rebuilt. The era
    field is left alone too; an era range like "1880-1900" is one displayed
    era, not the metadata leak the ticket reported. The title cleanup starts
    from the entry's own title, so a model-written title survives a repair
    run, with the raw Commons name as the fallback.
    """
    if not isinstance(scene, dict):
        return scene, []
    if not ag_queue.caption_problems(scene):
        return dict(scene), []
    out = dict(scene)
    source = (scene.get("source")
              if isinstance(scene.get("source"), dict) else {})
    changed = []
    title = clean_caption_title(scene.get("title")) or clean_title(source)
    if title != scene.get("title"):
        out["title"] = title
        changed.append("title")
    place = ag_queue.clean_place(scene.get("place"))
    if place != scene.get("place"):
        out["place"] = place
        changed.append("place")
    # The old builder glued title, place, era and repository into one comma
    # run. When the description is what still leaks, it is re-derived from
    # the fixed parts; a clean caption never reaches this point.
    if ag_queue.caption_problems(out):
        description = build_description(source, out["title"], out["place"])
        if description != out.get("description"):
            out["description"] = description
            changed.append("description")
    return out, changed


def retext_state(data_dir: Path, dry_run: bool = False) -> dict:
    """Rewrite the damaged captions of the queued scenes (#1402 repair path).

    Counts what the reported bug left behind and re-derives each caption,
    the same way ``write_manifest`` re-normalises old places at ship time
    (#1378). A scene whose caption still has a problem after the rewrite is
    listed as unfixable instead of being written silently.
    """
    state = ag_queue.load_state(data_dir)
    scenes = state.get("scenes") or {}
    changed, unfixable = [], []
    damaged = 0
    for eid, scene in scenes.items():
        if not ag_queue.caption_problems(scene):
            continue
        damaged += 1
        fixed, keys = retext_entry(scene)
        problems = ag_queue.caption_problems(fixed)
        if problems:
            unfixable.append({"id": eid, "problems": problems})
        if not keys:
            continue
        changed.append({"id": eid, "changed": keys,
                        "before": {k: scene.get(k) for k in keys},
                        "after": {k: fixed[k] for k in keys}})
        if not dry_run:
            scene.update(fixed)
    if changed and not dry_run:
        ag_queue.save_state(data_dir, state)
    return {"data": str(data_dir), "scenes": len(scenes),
            "damaged": damaged, "changed": len(changed),
            "unfixable": unfixable, "dry_run": dry_run,
            "details": changed}


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


def refused_elements_path(data_dir: Path) -> Path:
    return Path(data_dir) / REFUSED_ELEMENTS_NAME


def load_refused_elements(data_dir: Path) -> dict:
    """The persisted refusal-prone elements; {} when the file is missing.

    Ticket #1439: the image provider refused edits to a few elements over and
    over (a QR sticker 4 of 5 times, a roof panel 3 of 5). The pipeline keeps
    the elements so the proposal avoids them for a while instead of
    re-proposing the element the provider just blocked.
    """
    try:
        data = json.loads(refused_elements_path(data_dir).read_text())
    except (OSError, ValueError):
        return {}
    elements = data.get("elements") if isinstance(data, dict) else None
    return elements if isinstance(elements, dict) else {}


def record_refused_element(data_dir: Path, label: str, kind: str,
                           detail: dict | None = None, now=None) -> dict:
    """Mark one element refusal-prone and persist it (ticket #1439)."""
    label = str(label or "").strip()
    if not label:
        return {}
    now = now or datetime.datetime.now().astimezone()
    elements = load_refused_elements(data_dir)
    entry = dict(elements.get(label) or {})
    entry["count"] = int(entry.get("count") or 0) + 1
    entry["kind"] = str(kind or "empty")
    entry["last_refused"] = now.date().isoformat()
    if detail:
        entry["detail"] = detail
    elements[label] = entry
    try:
        _atomic_json(refused_elements_path(data_dir),
                     {"updatedAt": now.isoformat(timespec="seconds"),
                      "elements": elements})
    except OSError:
        pass  # observability only: a failed write must not fail the run
    return elements


def refused_labels(data_dir: Path, today=None,
                   days: int = REPEAT_WINDOW_DAYS) -> list:
    """Refusal-prone element labels from the recent window, newest first."""
    day = today or datetime.date.today()
    cutoff = (day - datetime.timedelta(days=days)).isoformat()
    rows = [(label, e) for label, e in load_refused_elements(data_dir).items()
            if str(e.get("last_refused") or "") >= cutoff]
    rows.sort(key=lambda r: str(r[1].get("last_refused") or ""), reverse=True)
    return [label for label, _ in rows][:REPEAT_LABEL_LIMIT]


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
              "model": args.model, "image_model": args.image_model,
              "max_generations": args.max_generations}
    totals = ag_llm.zero_usage()

    def emit(state: str = "running", **extra) -> None:
        """Best-effort progress for the dashboard (see module docstring).

        Ticket #1381: the status carries the current phase, the scene being
        worked on, the round/draw and the running cost, so the dev button
        shows real progress instead of a frozen "added 0".
        """
        if lock is None:
            return
        extra.setdefault("scenesTotal", report["planned"])
        try:
            write_status(data_dir, state=state, pid=os.getpid(),
                         count=args.count, planned=report["planned"],
                         added=len(report["added"]),
                         failed=len(report["failed"]),
                         imageCalls=int(totals.get("image_calls", 0)),
                         cost=round(totals.get("cost", 0.0), 6),
                         elapsedS=round(time.time() - started, 1),
                         startedAt=started_iso, **extra)
        except OSError:
            pass  # status is observability, never a reason to fail the run

    emit(phase="starting")

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
        emit(state="error", phase="error", error=report["error"],
             finishedAt=_now_iso())
        return report
    if len(picked) < args.count:
        report["warning"] = (f"source pool low: {len(picked)} unused sources, "
                             f"wanted {args.count} scenes "
                             "(run pipeline/ag_sources.py top-up)")
    report["planned"] = len(picked)
    recent = recent_labels(data_dir, datetime.date.fromisoformat(date))
    # Ticket #1439: elements the provider refused keep out of the proposal
    # pool for the repeat window, so a refusal is not re-proposed next run.
    for label in refused_labels(data_dir, datetime.date.fromisoformat(date)):
        if label not in recent:
            recent.append(label)
    stats = _blank_stats()

    if args.dry_run:
        report["dry_run"] = True
        report["plan"] = [
            {"source": s["id"], "title": clean_title(s),
             "place": scene_place(s),
             "proposal_prompt": proposal_prompt(s, recent),
             "edit_prompt_template": edit_prompt({"anomaly": "<anomaly>",
                                                  "placement": "<placement>",
                                                  "figure": False})}
            for s in picked
        ]
        report["recent_labels"] = recent
        return report

    api_key = os.environ.get("OPENROUTER_API_KEY", "")
    if not api_key:
        report["error"] = "OPENROUTER_API_KEY not set (pass --env)"
        emit(state="error", phase="error", error=report["error"],
             finishedAt=_now_iso())
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
            emit(state="error", phase="error", error=report["error"],
                 finishedAt=_now_iso())
            return report

    out_dir = Path(args.out_dir) if args.out_dir else Path(
        tempfile.mkdtemp(prefix="ag-gen-"))
    used_prompts = set()
    for index, source in enumerate(picked, 1):
        if image_budget_left(args, totals) <= 0:
            report["failed"].append({"id": source["id"], "stage": "budget",
                                     "reason": "generation budget"})
            emit(phase="budget", scene=source["id"])
            continue
        scene, failed = _generate_one(source, args, data_dir, date, out_dir,
                                      recent + sorted(used_prompts), totals,
                                      emit, index, len(picked), stats=stats)
        if scene is not None:
            report["added"].append(scene["report"])
            used_prompts.add(scene["label"])
        elif failed is not None:
            report["failed"].append(failed)
        emit(phase="scene-done", scene=source["id"])

    report["duration_s"] = round(time.time() - started, 1)
    report["image_calls"] = int(totals.get("image_calls", 0))
    report["model_usage"] = {k: v for k, v in totals.items()
                             if k not in ("image_calls", "image_cost")}
    report["cost_total"] = round(totals["cost"], 6)
    report["image_cost_total"] = round(totals.get("image_cost", 0.0), 6)
    # Ticket #1439: the refusal rate overall and split by element and by
    # placement kind, with the raw provider messages of each refusal, so the
    # framing's effect is visible without opening a trace.
    report["refusal"] = refusal_summary(stats, totals)
    report["refused_elements"] = load_refused_elements(data_dir)
    # The new flow's shape (ticket #1436): how many correction rounds the
    # checker asked for and what the click-target pass changed, so a run is
    # comparable without opening a trace.
    clicks = [s.get("click_target") or {} for s in report["added"]]
    report["correction_rounds"] = [s.get("correction_rounds")
                                   for s in report["added"]]
    report["click_target_corrected"] = sum(1 for c in clicks
                                           if c.get("corrected"))
    shifts = [c["shift"] for c in clicks if c.get("shift") is not None]
    report["click_target_shift_mean"] = (
        round(sum(shifts) / len(shifts), 4) if shifts else None)
    report["click_target_cost"] = round(
        sum(float(c.get("cost") or 0.0) for c in clicks), 6)
    report["checker_scores"] = [s["checker"].get("score")
                                for s in report["added"]
                                if (s.get("checker") or {}).get("score")
                                is not None]
    if report["added"]:
        report["cost_per_scene"] = round(totals["cost"] / len(report["added"]), 6)
        report["image_calls_per_scene"] = round(
            report["image_calls"] / len(report["added"]), 2)
        report["seconds_per_scene"] = round(
            report["duration_s"] / len(report["added"]), 1)
    report["moderation"] = moderation_rate(data_dir)
    if report.get("error") or not report["added"]:
        message = report.get("error") or (
            f"no scenes added ({len(report['failed'])} failed)")
        emit(state="error", phase="error", error=message,
             finishedAt=_now_iso())
    else:
        emit(state="done", phase="done", finishedAt=_now_iso(),
             durationS=report["duration_s"])
    return report


def _blank_stats() -> dict:
    """Run-level refusal accounting for one generation run (ticket #1439)."""
    return {"attempts": [], "refusals": []}


def _note_attempt(stats: dict | None, proposal: dict, refused: bool,
                  kind: str | None = None, detail: dict | None = None,
                  usage: dict | None = None,
                  duration_s: float | None = None) -> None:
    """Record one image-edit attempt for the run's refusal report."""
    if stats is None:
        return
    element = str((proposal or {}).get("anomaly") or "")
    placement = placement_kind(proposal or {})
    stats["attempts"].append({"element": element, "placement_kind": placement,
                              "refused": bool(refused)})
    if refused:
        stats["refusals"].append({
            "element": element, "placement_kind": placement,
            "kind": kind, "detail": detail, "usage": usage,
            "duration_s": duration_s})


def refusal_summary(stats: dict | None, totals: dict) -> dict:
    """The run's refusal numbers, overall and split (ticket #1439).

    ``rate`` is the share of image-edit attempts the provider refused;
    ``by_element`` and ``by_placement_kind`` carry the same split, so the
    hypothesis that modifications get refused more often than standalone
    objects can be read straight off one run.
    """
    stats = stats or {}
    attempts = stats.get("attempts") or []
    refusals = stats.get("refusals") or []

    def group(key):
        out = {}
        for a in attempts:
            row = out.setdefault(a.get(key) or "", {"attempts": 0,
                                                    "refusals": 0})
            row["attempts"] += 1
            if a.get("refused"):
                row["refusals"] += 1
        for row in out.values():
            row["rate"] = (round(row["refusals"] / row["attempts"], 3)
                           if row["attempts"] else None)
        return out

    refused_cost = sum(float((r.get("usage") or {}).get("cost") or 0.0)
                       for r in refusals)
    refused_seconds = sum(float(r.get("duration_s") or 0.0)
                          for r in refusals)
    return {
        "attempts": len(attempts),
        "refusals": len(refusals),
        "rate": round(len(refusals) / len(attempts), 3) if attempts else None,
        "by_element": group("element"),
        "by_placement_kind": group("placement_kind"),
        "refused_cost": round(refused_cost, 6),
        "refused_seconds": round(refused_seconds, 1),
        "messages": refusals,
    }


def image_budget_left(args, totals: dict) -> int:
    """Image calls the run may still spend (ticket #1436).

    ``--max-generations`` stays a run-level runaway guard, not a per-scene
    quota: the default covers every planned source at one draw plus its
    mechanical retries and its two correction rounds, and a scene that meets
    the cap is reported instead of starving the sources queued behind it.
    """
    return max(0, int(args.max_generations) - int(totals.get("image_calls", 0)))


def _draw_scene_image(source_image: Path, prompt: str, retry_prompt: str,
                      proposal: dict, attempt: int, args, api_key: str,
                      source: dict, data_dir: Path, out_dir: Path, trace: dict,
                      totals: dict, progress, previous_outputs: list,
                      stats: dict | None = None) -> tuple:
    """The ONE image for this scene (tickets #1436, #1439).

    Draws image edits until one passes the mechanical gate (landscape shape,
    byte-identical re-serve, whole-frame repaint), allowing
    ``MECHANICAL_RETRIES`` extra image calls to replace broken draws. Every
    draw lands in the trace, kept or rejected, with its reason (idea #1435:
    a retry used to leave no row, so the draw numbers jumped). A refused edit
    gets one retry with the rephrased prompt first; an element the provider
    refuses twice ends the attempt (``refused``) instead of burning more
    draws on the same wording. Returns
    ``(draw|None, failure_reasons, budget_hit, refused|None)`` where ``draw``
    carries ``path``, ``seed``, ``hotspot`` and its trace ``record``.
    """
    draws_allowed = 1 + MECHANICAL_RETRIES
    failures, draw, budget_hit, refused = [], 0, False, None
    while draw < draws_allowed:
        if image_budget_left(args, totals) <= 0:
            # Not a draw failure: the gate reasons above stay the report's
            # "why", so a budget stop cannot mask a mechanical rejection.
            budget_hit = True
            break
        draw += 1
        progress("editing", round=0, draw=draw)
        attempts = _edit_attempts(source_image, prompt, retry_prompt,
                                  proposal, args, api_key, source, totals)
        for a in attempts[:-1]:
            rec = a["record"]
            _note_attempt(stats, proposal, refused=bool(a["refused"]),
                          kind=a["refused"], detail=rec.get("provider"),
                          usage=rec.get("usage"),
                          duration_s=rec.get("duration_s"))
            record_call(data_dir, trace,
                        {"stage": "edit r0", "attempt": attempt, "draw": draw,
                         **rec})
        terminal = attempts[-1]
        rec = terminal["record"]
        _note_attempt(stats, proposal, refused=bool(terminal["refused"]),
                      kind=terminal["refused"], detail=rec.get("provider"),
                      usage=rec.get("usage"),
                      duration_s=rec.get("duration_s"))
        row = {"stage": "edit r0", "attempt": attempt, "draw": draw, **rec}
        if terminal["data"] is None:
            record_call(data_dir, trace, row)
            if terminal["refused"]:
                # Refused twice, even after the rephrase: the element is the
                # problem, so report it (the caller marks it refusal-prone).
                failures.append(str(rec["error"]))
                refused = {"kind": terminal["refused"],
                           "detail": rec.get("provider"), "draw": draw}
                break
            failures.append(str(rec["error"]))
            continue
        out_path = _write_bytes(
            terminal["data"],
            out_dir / f"{source['id']}-a{attempt}-r0-d{draw}.png")
        reason, hotspot = candidate_gate(out_path, source_image,
                                         list(previous_outputs) or None)
        previous_outputs.append(out_path)
        if reason:
            row["rejected"] = reason
            trace.setdefault("gate_failures", []).append(
                {"stage": "edit r0", "attempt": attempt, "draw": draw,
                 "reason": reason})
            failures.append(reason)
            record_call(data_dir, trace, row)
            continue
        record_call(data_dir, trace, row)
        return {"path": out_path, "seed": rec.get("seed"),
                "hotspot": hotspot, "record": row}, failures, budget_hit, None
    return None, failures, budget_hit, refused


def _check_round(image: Path, proposal: dict, scene: dict | None, round_no: int,
                 attempt: int, args, api_key: str, data_dir: Path,
                 trace: dict, totals: dict, progress) -> dict | None:
    """One checker call, recorded as ``check r<round>``; None on call error.

    The score is the number of the seven requirements the image meets,
    computed in code from the requirement numbers (ticket #1381). ``scene``
    is the ``scene_time`` anchor, so requirement 8 can test the element's
    introduction year against the photograph's year (ticket #1403).
    """
    progress("checking", round=round_no)
    try:
        check = check_scene(image, proposal, api_key, args.model,
                            args.base_url, args.model_max_tokens,
                            args.model_timeout, args.temperature, scene=scene)
    except ag_llm.LLMError as e:
        trace.setdefault("call_errors", []).append(
            {"stage": f"check r{round_no}", "attempt": attempt,
             "error": str(e)})
        return None
    ag_llm.add_usage(totals, check["call"].get("usage"))
    record_call(data_dir, trace,
                {"stage": f"check r{round_no}", "attempt": attempt,
                 "score": check["score"], "failed": check["failed"],
                 **_call_trace(check["call"])})
    return check


def _fix_edit(current: Path, proposal: dict, check: dict, round_no: int,
              attempt: int, args, api_key: str, source: dict, data_dir: Path,
              out_dir: Path, trace: dict, totals: dict, progress,
              previous_outputs: list, stats: dict | None = None) -> tuple:
    """One correction round (ticket #1436): the checker's repair instruction.

    Edits the CURRENT image, the one that failed the check, so each round
    refines the last result instead of re-adding the anomaly from scratch.
    A refused correction gets the same one framed retry as a refused draw
    (ticket #1439). Returns ``(result|None, refused|None)``; None means the
    round is skipped, refused or its output fails the mechanical gates, and
    the image before the round then ships.
    """
    if image_budget_left(args, totals) <= 0:
        trace.setdefault("corrections", []).append(
            {"round": round_no, "skipped": "generation budget"})
        return None, None
    fix_head = _fix_prompt(proposal, check["fix_prompt"])
    retry_prompt = _fix_prompt(proposal, check["fix_prompt"], retry=True)
    progress("correcting", round=round_no)
    attempts = _edit_attempts(current, fix_head, retry_prompt, proposal,
                              args, api_key, source, totals)
    for a in attempts[:-1]:
        rec = a["record"]
        _note_attempt(stats, proposal, refused=bool(a["refused"]),
                      kind=a["refused"], detail=rec.get("provider"),
                      usage=rec.get("usage"), duration_s=rec.get("duration_s"))
        record_call(data_dir, trace,
                    {"stage": f"fix-edit r{round_no}", "attempt": attempt,
                     **rec})
    terminal = attempts[-1]
    rec = terminal["record"]
    _note_attempt(stats, proposal, refused=bool(terminal["refused"]),
                  kind=terminal["refused"], detail=rec.get("provider"),
                  usage=rec.get("usage"), duration_s=rec.get("duration_s"))
    row = {"stage": f"fix-edit r{round_no}", "attempt": attempt, **rec}
    if terminal["data"] is None:
        # Same as a refused draw: the failed correction is a `calls` row
        # (idea #1435), not only a line in call_errors.
        record_call(data_dir, trace, row)
        trace.setdefault("corrections", []).append(
            {"round": round_no, "failed": str(rec["error"])})
        return None, terminal["refused"]
    fixed_path = _write_bytes(
        terminal["data"],
        out_dir / f"{source['id']}-a{attempt}-r{round_no}-fix.png")
    reason, hotspot = candidate_gate(fixed_path, current,
                                     list(previous_outputs) or None)
    previous_outputs.append(fixed_path)
    if reason:
        row["rejected"] = reason
        trace.setdefault("corrections", []).append(
            {"round": round_no, "rejected": reason})
        trace.setdefault("gate_failures", []).append(
            {"stage": f"fix-edit r{round_no}", "attempt": attempt,
             "reason": reason})
        record_call(data_dir, trace, row)
        return None, None
    record_call(data_dir, trace, row)
    return {"path": fixed_path, "hotspot": hotspot}, None


def _coords_differ(a: dict, b: dict, eps: float = 0.005) -> bool:
    """Whether a corrected answer differs from the judged one (ticket #1436).

    A correction below the tolerance is agreement, not a change, and must
    not spend a second pass.
    """
    return (abs(a["x"] - b["x"]) > eps or abs(a["y"] - b["y"]) > eps
            or abs(a["r"] - b["r"]) > eps)


def _save_click_target_overlay(data_dir: Path, eid: str, src) -> str:
    """Copy the last rendered click-target overlay next to the trace.

    The overlay is what the model judged (ticket #1436); keeping it lets a
    moderator see the drawn area without rerunning anything. Best-effort: a
    missing overlay or an I/O error must not fail a landed scene.
    """
    if not src:
        return ""
    try:
        dst = data_dir / TRACE_DIRNAME / f"{eid}-click-target.png"
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, dst)
        return dst.name
    except OSError:
        return ""


def _click_target_passes(image: Path, proposal: dict, answer: dict,
                         attempt: int, args, api_key: str, data_dir: Path,
                         out_dir: Path, trace: dict, totals: dict,
                         progress) -> tuple:
    """The click-target quality pass (tickets #1436, #1445), at most two calls.

    Renders the answer area onto the shipped image and asks the checker model
    for the anomaly's bounding box. The coverage decision is deterministic:
    when a box corner falls outside the drawn ellipse, the answer grows to
    the smallest circle containing both the drawn area and the box, and a
    second rendered pass verifies that correction. Returns ``(answer,
    summary)``; the summary carries the passes, whether the answer was
    corrected, the centre shift in normalized units and the pass's own cost
    for the run report.
    """
    current, judged = dict(answer), dict(answer)
    passes, cost, corrected, last_overlay = [], 0.0, False, None
    for p in range(1, CLICK_TARGET_PASSES + 1):
        overlay = render_click_target(
            image, current, out_dir / f"{image.stem}-click-{p}.png")
        last_overlay = overlay
        progress("click-target", clickPass=p)
        try:
            verdict = check_click_target(overlay, proposal, api_key,
                                         args.model, args.base_url,
                                         args.model_max_tokens,
                                         args.model_timeout, args.temperature)
        except ag_llm.LLMError as e:
            trace.setdefault("call_errors", []).append(
                {"stage": f"click-target {p}", "attempt": attempt,
                 "error": str(e)})
            passes.append({"pass": p, "overlay": overlay.name,
                           "answer": current, "error": str(e)})
            break
        ag_llm.add_usage(totals, verdict["call"].get("usage"))
        cost += float((verdict["call"].get("usage") or {}).get("cost") or 0.0)
        box = verdict["box"]
        covers = None if box is None else answer_covers_box(current, box)
        nxt = None
        if box is not None and not covers:
            # The person rule follows the proposal, not the check's flag: a
            # stray "figure" on a small object must not inflate its circle.
            person = {"figure": bool(proposal.get("figure"))}
            nxt = clamp_answer(covering_answer(current,
                                               box_answer({**box, **person})))
            if not _coords_differ(nxt, current):
                nxt = None
        record_call(data_dir, trace,
                    {"stage": f"click-target {p}", "attempt": attempt,
                     "judged": current, "box": box, "covers": covers,
                     "corrected": nxt,
                     "verdict_reason": verdict["reason"],
                     **_call_trace(verdict["call"])})
        passes.append({"pass": p, "overlay": overlay.name,
                       "answer": current, "box": box, "covers": covers,
                       "corrected": nxt, "reason": verdict["reason"]})
        if nxt is None:
            break
        current = nxt
        corrected = True
    summary = {"passes": len(passes), "corrected": corrected,
               "answer": current,
               "shift": round(math.hypot(current["x"] - judged["x"],
                                         current["y"] - judged["y"]), 4),
               "cost": round(cost, 6), "verdicts": passes,
               "overlay_path": last_overlay}
    return current, summary


def _generate_one(source: dict, args, data_dir: Path, date: str,
                  out_dir: Path, recent: list, totals: dict, emit,
                  scene_index: int = 1, scene_total: int = 1,
                  stats: dict | None = None) -> tuple:
    """One source through the whole flow; returns (scene|None, failure|None).

    Ticket #1436: a source yields exactly one scene, drawn as exactly one
    image. The checker never drops the scene; it reports what is wrong and
    drives up to two correction rounds plus a final click-target pass. Only a
    mechanical failure (the source image missing, the API refusing to produce
    any draw, a coordinate-less scene with no diff hotspot) is reported as
    failed. ``stats`` carries the run's refusal accounting (ticket #1439).
    """
    api_key = os.environ.get("OPENROUTER_API_KEY", "")
    source_image = ag_sources.sources_dir(data_dir) / source["image"]
    if not source_image.exists():
        return None, {"id": source["id"], "reason": f"missing image "
                      f"{source_image}", "stage": "source"}

    def progress(phase: str, **extra) -> None:
        emit(phase=phase, scene=source["id"], sceneIndex=scene_index,
             scenesTotal=scene_total, **extra)

    previous_outputs = []
    # One year source only: the source's catalogue date (ticket #1430). The
    # proposal never supplies or corrects it.
    st = scene_time(source)
    trace = {"source": source["id"], "date": date, "model": args.model,
             "image_model": args.image_model, "scene_time": st,
             "candidate_policy": {"images_per_scene": 1,
                                  "mechanical_retries": MECHANICAL_RETRIES,
                                  "correction_rounds": CORRECTION_ROUNDS,
                                  "click_target_passes": CLICK_TARGET_PASSES}}
    last_error = None
    if st["year"] is None:
        # The pool gate refuses such a source; refusing here too keeps a
        # stale index entry from producing a scene whose year no one can
        # prove. The source is consumed so it is not drawn every day.
        reason = ("source has no catalogue year (provenance field "
                  f"{st['field'] or 'unknown'}); no impossible anomaly can "
                  "be proven")
        last_error = {"id": source["id"], "stage": "year", "reason": reason,
                      "scene_time": st}
        set_trace_error(data_dir, trace, reason)
        ag_sources.mark_used(data_dir, [source["id"]], True)
        return None, last_error
    for attempt in range(1, args.max_attempts + 1):
        progress("proposing")
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
        retry_prompt = edit_prompt(proposal, retry=True)
        draw, failures, budget_hit, refused = _draw_scene_image(
            source_image, prompt, retry_prompt, proposal, attempt, args,
            api_key, source, data_dir, out_dir, trace, totals, progress,
            previous_outputs, stats)
        if draw is None:
            if refused is not None:
                # Ticket #1439: the provider refused this element twice, so it
                # is marked refusal-prone and the next proposal avoids it
                # instead of re-proposing the same element.
                stage, reason = "refusal", f"image edit refused ({refused['kind']})"
                record_refused_element(data_dir, proposal.get("anomaly"),
                                       refused["kind"],
                                       detail=refused.get("detail"))
                if proposal.get("anomaly"):
                    recent.append(proposal["anomaly"])
                last_error = {"id": source["id"], "stage": stage,
                              "reason": reason, "attempt": attempt,
                              "element": proposal.get("anomaly"),
                              "refusal_kind": refused["kind"],
                              "provider": refused.get("detail")}
            elif failures:
                stage, reason = "image", failures[-1]
                last_error = {"id": source["id"], "stage": stage,
                              "reason": reason, "attempt": attempt}
            elif budget_hit:
                stage, reason = "budget", "generation budget"
                last_error = {"id": source["id"], "stage": stage,
                              "reason": reason, "attempt": attempt}
            else:
                stage, reason = "image", "no draw passed the gates"
                last_error = {"id": source["id"], "stage": stage,
                              "reason": reason, "attempt": attempt}
            set_trace_error(data_dir, trace, last_error["reason"])
            continue
        final_path, hotspot = draw["path"], draw["hotspot"]

        # Generate -> check; a clean check ends the chain right there (Evan
        # 2026-09-13). Otherwise the checker's repair instruction drives a
        # fix-edit, at most CORRECTION_ROUNDS rounds, each followed by a
        # check; the check after the last round is a record, never a trigger.
        check, rounds_used = None, 0
        if not args.no_check:
            check = _check_round(final_path, proposal, st, 0, attempt, args,
                                 api_key, data_dir, trace, totals, progress)
            while (check is not None and check["failed"]
                   and check["fix_prompt"]
                   and rounds_used < CORRECTION_ROUNDS):
                rounds_used += 1
                fixed, _refused = _fix_edit(
                    final_path, proposal, check, rounds_used, attempt, args,
                    api_key, source, data_dir, out_dir, trace, totals,
                    progress, previous_outputs, stats)
                if fixed is None:
                    rounds_used -= 1
                    break
                final_path, hotspot = fixed["path"], fixed["hotspot"]
                check = _check_round(final_path, proposal, st, rounds_used,
                                     attempt, args, api_key, data_dir, trace,
                                     totals, progress)

        progress("locating")
        loc = None
        try:
            loc = locate_anomaly(final_path, proposal, prompt, api_key,
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

        # Click-target quality pass (tickets #1436, #1445): only the checker's
        # identifiability finding questions the answer area; then the model
        # localizes the element and the pipeline grows the drawn area.
        if check and CLICK_TARGET_REQUIREMENT in (check.get("failed") or []):
            answer, click = _click_target_passes(
                final_path, proposal, answer, attempt, args, api_key, data_dir,
                out_dir, trace, totals, progress)
        else:
            click = {"passes": 0, "corrected": False, "answer": answer,
                     "shift": 0.0, "cost": 0.0, "verdicts": [],
                     "skipped": "checker did not fail identifiability"}
        if conflict and click["corrected"]:
            # The pass moved the answer, so the old hotspot disagreement no
            # longer describes it.
            conflict = None

        check_summary = {
            "skipped": bool(args.no_check) or check is None,
            "score": check["score"] if check else None,
            "failed": check["failed"] if check else [],
            "reason": check["reason"] if check else "",
            "rounds": rounds_used,
        }
        entry = build_entry(source, proposal, answer, date, scene=st,
                            checker=check_summary)
        errs = ag_queue.validate_entry(entry)
        if errs:
            last_error = {"id": source["id"], "stage": "entry",
                          "reason": "invalid entry: " + "; ".join(errs),
                          "attempt": attempt}
            set_trace_error(data_dir, trace, last_error["reason"])
            continue
        try:
            ag_queue.state_add(data_dir, entry, final_path, source_image,
                               date)
        except Exception as e:  # noqa: BLE001 - queue reject = drop scene
            last_error = {"id": source["id"], "stage": "queue",
                          "reason": f"queue add: {e}", "attempt": attempt}
            break
        ag_sources.mark_used(data_dir, [source["id"]], True)
        overlay = _save_click_target_overlay(data_dir, entry["id"],
                                             click.get("overlay_path"))
        if overlay:
            click["overlay"] = overlay
        click.pop("overlay_path", None)
        trace["click_target"] = click
        write_trace(data_dir, entry["id"], trace)
        scene_report = {
            "scene": entry["id"], "source": source["id"],
            "anomaly": entry["anomaly"], "kind": proposal["kind"],
            "era": entry["year"], "place": entry["place"],
            "scene_time": st,
            "attempt": attempt, "answer": answer,
            "coord_conflict": conflict,
            "checker": check_summary,
            "correction_rounds": rounds_used,
            "click_target": click,
            "mechanical_failures": failures,
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


def _fix_prompt(proposal: dict, fix: str, retry: bool = False) -> str:
    """The correction instruction; ``retry`` is the post-refusal rephrase."""
    head = (f"Edit this photograph again: fix ONLY these problems with the "
            f"added {proposal['anomaly']}: {fix}.")
    parts = [PURPOSE, head]
    if retry:
        parts.append(REFUSAL_RETRY)
    parts += [scale_rule(proposal), KEEP, BLEND]
    return " ".join(parts)


def _settle_image_call(totals: dict | None, usage: dict, record: dict,
                       started: float) -> None:
    """Close out one image attempt: duration and its billed usage (#1435).

    Runs on both outcomes. A refused edit still spent the call, so it counts
    against the run budget (``image_calls``) and its cost is folded into the
    totals; before, a failed attempt was invisible there and the run report
    understated the spend.
    """
    record["duration_s"] = round(time.time() - started, 2)
    if totals is None:
        return
    totals["image_calls"] = totals.get("image_calls", 0) + 1
    ag_llm.add_usage(totals, usage)
    totals["image_cost"] = (totals.get("image_cost", 0.0)
                            + float(usage.get("cost") or 0.0))


def _edit(source_image: Path, prompt: str, args, api_key: str, source: dict,
          totals: dict | None = None):
    """One image edit; returns (bytes, trace record).

    Counts the attempt against the run budget and folds its usage into
    ``totals`` in both outcomes; on a failed call it raises
    ``ImageCallFailed`` carrying the attempt's record, so the caller can put
    it into ``calls`` instead of losing it (idea #1435).
    """
    started = time.time()
    at = datetime.datetime.now().astimezone().isoformat(timespec="seconds")
    seed = random.randint(0, 2**31 - 1)
    usage = ag_llm.zero_usage()
    record = {"prompt": prompt, "seed": seed, "model": args.image_model,
              "image": image_ref(source_image), "at": at, "usage": usage}
    try:
        data = image_edit(source_image, prompt, api_key, args.base_url,
                          args.image_model,
                          aspect_ratio_for(source.get("width"),
                                           source.get("height")),
                          args.image_size, seed=seed,
                          timeout=args.image_timeout, usage_out=usage)
    except ImageRefused as e:
        record["error_kind"] = e.kind
        record["provider"] = e.detail
        _settle_image_call(totals, usage, record, started)
        raise ImageCallFailed(str(e), record) from e
    except GenerationError as e:
        _settle_image_call(totals, usage, record, started)
        raise ImageCallFailed(str(e), record) from e
    _settle_image_call(totals, usage, record, started)
    return data, record


def _edit_attempts(image: Path, prompt: str, retry_prompt: str, proposal: dict,
                   args, api_key: str, source: dict, totals: dict) -> list:
    """One edit plus its one framed retry after a refusal (ticket #1439).

    Returns a list of attempt dicts, each ``{"record", "data", "refused"}``:
    one entry for a normal call, two when the first was refused and the
    rephrase was tried. ``refused`` is the classification (``safety``,
    ``empty`` or ``broken``) for a refusal and None otherwise; a mechanical
    failure stops the list too, because a retry cannot help a transport
    error. A refusal without budget left for the retry stays terminal.
    """
    plans = [(prompt, "purpose")]
    if retry_prompt:
        plans.append((retry_prompt, "retry"))
    out = []
    for text, variant in plans:
        if out and image_budget_left(args, totals) <= 0:
            break
        try:
            data, record = _edit(image, text, args, api_key, source, totals)
        except ImageCallFailed as e:
            record = dict(e.record)
            record["variant"] = variant
            record["error"] = str(e)
            kind = e.record.get("error_kind")
            if kind:
                record["refusal"] = kind
            out.append({"record": record, "data": None, "refused": kind})
            if not kind:
                break
            continue
        record = dict(record)
        record["variant"] = variant
        out.append({"record": record, "data": data, "refused": None})
        break
    return out


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
        description="AnomalyGuessr generator, six-step LLM flow (#1372, #1436).")
    p.add_argument("--data", default=None,
                   help="data dir (default: repo data/anomalyguessr)")
    p.add_argument("--retext", action="store_true",
                   help="repair mode: re-derive the queued scenes' captions "
                        "(title/place/description) instead of generating "
                        "(ticket #1402)")
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
                   help="skip the checker calls (and the correction edits, "
                        "ticket #1436)")
    p.add_argument("--no-preflight", action="store_true",
                   help="skip the one-call vision preflight")
    p.add_argument("--max-attempts", type=int, default=2,
                   help="proposals per source, each a fresh generation "
                        "(run-guard #1122; default 2)")
    p.add_argument("--max-generations", type=int, default=0,
                   help="hard cap on image calls per run "
                        "(0 = count*(1+retries+correction rounds); "
                        "ticket #1436)")
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
        per_scene = 1 + MECHANICAL_RETRIES + CORRECTION_ROUNDS
        args.max_generations = max(1, args.count * per_scene)
    return args


def main(argv=None) -> int:
    args = parse_args(argv)
    if args.retext:
        data_dir = Path(args.data) if args.data \
            else ag_sources.default_data_dir()
        report = retext_state(data_dir)
        print(json.dumps(report, indent=2))
        return 0
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
