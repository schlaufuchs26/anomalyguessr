#!/usr/bin/env python3
"""AnomalyGuessr deterministic scene generator (ticket #1169).

Replaces the agent-mode generation loop of `cron/daily-anomalyguessr.yaml`
with a script: pick N random UNUSED sources from the source dataset
(`pipeline/ag_sources.py`, ticket #1170), run each through a fixed sequence of
model calls, and add the verified scenes to the queue (`pipeline/ag_queue.py`).

Evan's contract (#1169): "randomly select 10 unused source images from the
dataset and run them through a sequence of model calls." Everything the old
cron prompt left to LLM judgment is now code:

1. **Source choice** - with `--top-up`, `ag_sources.top_up()` grows the pool
   first (#1174); then `ag_sources.list_sources(unused=True)`, shuffled with
   a seeded RNG; a source is marked used only after a scene from it landed
   in the queue, so a failed attempt leaves it reusable.
2. **Candidate filter** - `pipeline/ag_catalog.py` (machine-readable mirror of
   `wiki/entries/anomalyguessr-anomalies.md`) is the hard pre-filter:
   setting fit, density rule, era rule (`min_year > photo year`), variety
   caps (one anomaly per label per set, same family max 2x), plus the
   cross-day novelty tier (ticket #1328): labels used in the last
   `NOVELTY_WINDOW_DAYS` drop out when enough fresh candidates remain, and
   source images that can host a fresh anomaly are picked first. The rules
   stay authoritative: the model can never plant a plastic bottle in 1905 or
   a person in an empty street.
3. **Anomaly choice (ticket #1211)** - with `--picker llm` (default) one
   `deepseek/deepseek-v4.1-flash` vision call over the source image + the
   candidate list picks which candidate fits THIS photo best and returns a
   one-line reason. An off-list label, empty content or API error falls back
   to the rule pick (`--picker rules`), so the picker can never lose a scene
   the rules could have filled. Reasoning is explicitly disabled
   (`reasoning: {enabled: false}`) with a tight `max_tokens`: with thinking
   on, V4.1-Flash spends the whole budget on `reasoning_content` and returns
   `content: null` (measured 2026-09-11). Picks, tokens and cost per pick are
   logged for the A/B (`pipeline/ag_picker_ab.py`, ticket #1217: image vs
   blank image vs text-only over a source sample). Temperature 0 (ticket
   #1313) makes a repeat of the same photo+prompt reproduce the pick;
   `--picker-draws N` (default 1) additionally asks N times and keeps the
   majority label.
4. **Prompt** - deterministic template per anomaly type + placement recipe
   (`ag_catalog.RECIPES`): one dominant placement instruction, one hard
   numeric scale cap, tone/blend rules (see engineering-practices.md).
5. **Image edit** - the same OpenRouter call the `image_generate` tool makes
   (`google/gemini-3.1-flash-image`, source image as a data URL, fresh
   int32 seed per call, `aspect_ratio` from the source dimensions).
6. **Verify** - `ag_verify.verify()` (full-image vision bounding box as the
   primary answer; the box must cover the WHOLE anomaly and the stored
   answer radius follows the box's half-diagonal, so every part of it is
   clickable, ticket #1328; pixel-diff as sanity gate + fallback, #1165;
   `--dedup` guards against byte-identical re-serves, #1124). Each added
   scene reports its measured rendered size (`scale_measured`,
   `scale_over_budget`) as tuning data, not as a rejection gate (#1328).
7. **Entry text** - deterministic from the source metadata + the catalog
   entry: no free-form LLM text. `place`/`year` come from the catalog
   metadata with heuristics; `explanation`/`references` from the catalog;
   `hints` from the placement recipe + the verified answer position.

Model-call sequence per scene: the anomaly picker vision call, the image
edit, then the verification vision pass (inside `ag_verify`).

Retries (run-guards #1122/#1124): at most `--max-attempts` generations per
source, each with a MATERIALLY different anomaly (never the same prompt), and
every previous output is passed to `ag_verify --dedup`. Budget guard:
`--max-generations` caps the image calls per run.

CLI::

    ag_generate.py [--data DIR] [--count 10] [--seed N] [--dry-run] [--top-up]
        [--env .env] [--dm-channel ID] [--image-model M] [--vision-model M]
        [--picker rules|llm] [--picker-model M] [--picker-temperature T]
        [--picker-draws N] [--picker-provider-default]
        [--max-attempts 2] [--max-generations N] [--date YYYY-MM-DD]
        [--out-dir DIR] [--no-vision]

`--dry-run` prints the plan (source -> anomaly -> prompt) without any API
call or queue write; use it to inspect a day's plan for free. A dry run
never calls the LLM picker (it would not be free), so it shows the rule
picks.

**Run lock (ticket #1210).** Every non-dry run takes an exclusive
`flock` on `data/anomalyguessr/generate.lock` for its whole duration, so
the daily cron and a manual "generate more" run cannot overlap. The flock
is held by the process and released by the kernel on exit, so a killed run
cannot leave a stale lock. `ag_queue.state_add` writes atomically per scene
(tmp file + rename) but is still a read-modify-write without a lock, so two
overlapping generators could lose each other's updates; the run lock is
what serializes them.

**Progress status (ticket #1210).** Unless the run is a dry run, the script
keeps `data/anomalyguessr/generate-status.json` up to date (state, planned,
added, failed, imageCalls, startedAt/finishedAt/error), written atomically
after planning and after every scene. The dashboard's server reads it to
show "generating 3 / 10" while the run is in flight.

Exit code 0 = ran (even with fewer than `--count` scenes; the JSON report
lists what failed); 1 = no scene was added or a hard error occurred. When
`--dm-channel` is set, a short failure message is posted to that Discord
channel (the old cron's "bei echten Problemen kurze DM" rule, now that no
LLM session is involved).
"""

import argparse
import base64
import collections
import datetime
import errno
import fcntl
import hashlib
import json
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
import ag_queue  # noqa: E402
import ag_sources  # noqa: E402
import ag_verify  # noqa: E402

IMAGE_MODEL = "google/gemini-3.1-flash-image"
DEFAULT_BASE_URL = "https://openrouter.ai/api/v1"
DEFAULT_IMAGE_SIZE = "1K"
# Landscape aspect ratios accepted by the image model, nearest-match against
# the source dimensions (the queue rejects non-landscape scene images).
ASPECTS = (("16:9", 16 / 9), ("3:2", 1.5), ("4:3", 4 / 3), ("5:4", 1.25),
           ("21:9", 21 / 9), ("2:1", 2.0), ("1:1", 1.0))
YEAR_RE = re.compile(r"\b(1[89]\d{2}|20[0-2]\d)\b")
SCENE_ID_RE = re.compile(r"[^a-z0-9]+")
# Variety caps (catalog selection rule 4, tightened by ticket #1211): one
# anomaly per LABEL per generation set (Evan: never the same anomaly twice),
# the same family at most twice (a bottle and a can are different anomalies
# but one family). The prompt-key rule (label|recipe must not repeat) is
# kept as a second guard.
MAX_PER_LABEL = 1
MAX_PER_FAMILY = 2

# Anomaly picker (ticket #1211): one vision call per source over the
# pre-filtered candidate list. V4.1-Flash reasoning must be OFF: with
# thinking on it spends the whole max_tokens on reasoning_content and
# returns content: null (measured 2026-09-11, see the module docstring).
DEFAULT_PICKER_MODEL = "deepseek/deepseek-v4.1-flash"
DEFAULT_PICKER_MAX_TOKENS = 200
DEFAULT_PICKER_TIMEOUT = 120
# Reproducibility (ticket #1313): a single call without a temperature is
# near a coin flip (same photo+prompt agreed in only 38% of pairs, #1217).
# Temperature 0 lifts that to ~88% (measured, 40 sources x 6 repeats); a
# `draws` majority adds only ~2 points more (3-draw: ~90%), so the default
# stays 1 draw and the flag is there for a scene that must not flip.
DEFAULT_PICKER_TEMPERATURE = 0.0
DEFAULT_PICKER_DRAWS = 1

# Run lock + progress status (ticket #1210). Both live in the data dir next
# to state.json/feedback.json; they are runtime artifacts, not committed.
LOCK_NAME = "generate.lock"
STATUS_NAME = "generate-status.json"

SETTING_KEYWORDS = {
    "market": ("market", "bazaar", "stall", "souk", "fair", "vendor",
               "produce", "shop", "store"),
    "street": ("street", "road", "avenue", "square", "sidewalk", "plaza",
               "town", "parade", "alley", "lane", "bridge"),
    "station": ("station", "railway", "railroad", "train", "platform",
                "depot", "tram"),
    "harbor": ("harbor", "harbour", "dock", "port", "quay", "wharf", "boat",
               "ship", "fish market", "pier"),
}
# Density rule: person/robot anomalies only where people already exist.
CROWD_KEYWORDS = ("crowd", "people", "pedestrian", "parade", "procession",
                  "festival", "ceremony", "onlookers", "shoppers", "busy",
                  "market", "stall", "bazaar", "fair")


class GenerationError(RuntimeError):
    """A single image-edit call failed (retryable with another anomaly)."""


class RunLockedError(RuntimeError):
    """Another generation run holds the run lock (ticket #1210)."""

    def __init__(self, holder: str):
        self.holder = holder
        super().__init__(
            f"another generation run is in progress (pid {holder})")


class RunLock:
    """Exclusive cross-process lock for one whole generation run.

    `ag_queue.state_add` is atomic per scene (tmp file + rename) but is a
    read-modify-write without a lock, so two concurrent generators would
    lose each other's state updates. The daily cron and the dashboard's
    manual "generate more" both go through this script, so each run holds
    an flock on `data/anomalyguessr/generate.lock` for its whole duration.
    The kernel drops the lock when the process exits (even on SIGKILL), so
    a crashed run cannot block the next one; the lock file itself stays and
    is reused.
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


def status_path(data_dir: Path) -> Path:
    return data_dir / STATUS_NAME


def write_status(data_dir: Path, **fields) -> None:
    """Atomically write the progress status the dashboard polls.

    Best-effort observability: a failure to write it must never break a
    generation run, so callers ignore the error (see the runner's `emit`).
    """
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


# ── Environment / small helpers ────────────────────────────────────────────

def ensure_tool_path() -> None:
    """Put the nix profile on PATH: `identify` lives there, and the cron
    terminal env (core/plugins/cron/plugin.go cronPath) does not include it.
    """
    nix_bin = Path.home() / ".nix-profile" / "bin"
    if nix_bin.is_dir():
        parts = os.environ.get("PATH", "").split(":")
        if str(nix_bin) not in parts:
            os.environ["PATH"] = str(nix_bin) + ":" + os.environ.get("PATH", "")


def slugify(text: str) -> str:
    return SCENE_ID_RE.sub("-", text.lower()).strip("-")


def scene_id(source_id: str, label: str) -> str:
    """Scene id = readable source prefix + the source's short hash + anomaly.

    Raw source ids are ~100 chars (title slug + date + hash); taking the full
    id plus a label would make unreadable scene ids, and truncating alone can
    collide between near-identical titles (the three Paris Exposition
    sources). Keeping the id's trailing 6-hex hash guarantees uniqueness.
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


def source_text(source: dict) -> str:
    raw = source.get("raw") or {}
    extra = " ".join(str(raw.get(k, "")) for k in ("categories", "title",
                                                   "subject", "partof"))
    return " ".join(str(source.get(k, "")) for k in
                    ("originalTitle", "description", "place")) + " " + extra


def parse_year(source: dict):
    """Earliest plausible photo year from the catalog metadata.

    Commons `date` is often the upload timestamp (a c.1900 photo can carry
    `2008-11-06`), so the title is checked first, then the description, then
    `date`. Only 1800-1999 years count: a 2005/2008 stamp is metadata noise,
    not a photo date. None = no year could be determined; the era rule then
    only allows fictional-future elements (no anachronism claim).
    """
    for text in (source.get("originalTitle", ""), source.get("description", ""),
                 source.get("date", "")):
        years = [int(y) for y in YEAR_RE.findall(str(text or ""))
                 if 1800 <= int(y) <= 1999]
        if years:
            return min(years)
    return None


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
    return re.sub(r"\s+", " ", t).strip(' "')


def guess_place(source: dict) -> str:
    """Best-effort place from the title ("... in Agana (1899-1900)").

    The source dataset's own `place` field is authoritative when set (seeded
    from Commons categories/GPS, #1174); otherwise fall back to the title
    heuristic, which lives in ag_sources so the dataset and the generator
    share one parser.
    """
    place = str(source.get("place") or "").strip()
    if place:
        return place
    return ag_sources.place_from_text(clean_title(source))


def _contains(text: str, words) -> bool:
    """Word-prefix match ("streets" matches, "valley" does not match
    "alley"): substring matching produced false positives like valley ->
    street via the "alley" keyword.
    """
    return any(re.search(r"\b" + re.escape(w), text) for w in words)


def infer_settings(source: dict) -> tuple:
    text = source_text(source).lower()
    found = tuple(name for name, words in SETTING_KEYWORDS.items()
                  if _contains(text, words))
    return found


def has_crowd(source: dict) -> bool:
    return _contains(source_text(source).lower(), CROWD_KEYWORDS)


def anomaly_fits(entry: dict, settings: tuple, year, crowd: bool) -> bool:
    """Setting fit + density rule + era rule (catalog selection rules 0-2)."""
    if entry["settings"] and settings:
        if not set(entry["settings"]) & set(settings):
            return False
    if entry["type"] == "person" and not crowd:
        return False
    if entry["recipe"].startswith("person") and not crowd:
        return False
    if entry["min_year"] is None:
        return True
    if year is None:
        return False  # cannot make an anachronism claim without a photo year
    return entry["min_year"] > year


# ── Planning ───────────────────────────────────────────────────────────────

# Cross-day novelty (ticket #1328). The hard pre-filter has no memory between
# runs, so the same few labels (bottles, daypacks, suitcases) won nearly every
# day. These constants make recent use a candidate signal: labels/families
# above the reuse thresholds drop out of a source's candidate list whenever
# enough fresh ones remain, and sources that can host a novel anomaly are
# picked first. Thresholds are counts inside the window (10 scenes/day, so a
# family at 6 has run once a day; a label at 2 has already repeated).
NOVELTY_WINDOW_DAYS = 7
LABEL_REUSE_MAX = 1      # label used more than once in the window = used up
FAMILY_REUSE_MAX = 8     # family used more than ~once a day = used up
FRESH_CHOICE_MIN = 3     # drop used candidates only when >=3 fresh ones remain


def label_usage(state: dict, today=None) -> dict:
    """Recent anomaly usage from the queue state (ticket #1328).

    Returns ``{"families": {family: n}, "labels": {label: n}}`` over the
    scenes added within ``NOVELTY_WINDOW_DAYS``. Retired labels resolve
    through ``ag_catalog.family_of``, so an old "Plastic bottle" still counts
    against the drinks family. An empty state yields empty maps, which makes
    every entry fresh (the pre-#1328 behaviour).
    """
    day = today or datetime.date.today()
    cutoff = (day - datetime.timedelta(days=NOVELTY_WINDOW_DAYS)).isoformat()
    families, labels = collections.Counter(), collections.Counter()
    for scene in (state.get("scenes") or {}).values():
        if not isinstance(scene, dict):
            continue
        if str(scene.get("added") or "") < cutoff:
            continue
        label = str(scene.get("anomaly") or "")
        if label:
            labels[label] += 1
        family = ag_catalog.family_of(label)
        if family:
            families[family] += 1
    return {"families": dict(families), "labels": dict(labels)}


def load_label_usage(data_dir: Path, today=None) -> dict:
    """``label_usage`` for a data dir; an unreadable state means "all fresh"."""
    try:
        state = ag_queue.load_state(data_dir)
    except (OSError, ValueError):
        return {}
    return label_usage(state, today)


def is_fresh(entry: dict, usage) -> bool:
    """True when the entry's label/family was not used up in the window."""
    if not usage:
        return True
    return (usage.get("families", {}).get(entry["family"], 0)
            <= FAMILY_REUSE_MAX
            and usage.get("labels", {}).get(entry["label"], 0)
            <= LABEL_REUSE_MAX)


def fresh_candidate_count(source: dict, usage) -> int:
    """Fitting anomalies this source could host that are not recently used.

    Source selection uses it to favour images that can carry a novel anomaly
    (ticket #1328): a harbor whose only fits are recently used labels ranks
    below a market with fresh candidates.
    """
    settings = infer_settings(source)
    year = parse_year(source)
    crowd = has_crowd(source)
    return sum(1 for e in ag_catalog.CATALOG
               if anomaly_fits(e, settings, year, crowd)
               and is_fresh(e, usage))


def pick_entries(pool: list, counts_label: dict, counts_family: dict,
                 settings: tuple, year, crowd: bool, used_prompts: set,
                 usage=None):
    """Fitting catalog entries, freshest then least-used first, deterministic.

    ``pool`` is the RNG-shuffled catalog; ties between equally-unused entries
    resolve to the shuffled order, so a fixed seed reproduces the plan. This
    is the hard pre-filter the anomaly picker chooses from (ticket #1211):
    every returned entry already satisfies setting fit, density, era and the
    variety caps.

    ``usage`` (from ``label_usage``) adds the cross-day novelty tier (ticket
    #1328): not-recently-used labels sort first, and the recently-used ones
    are dropped entirely when at least ``FRESH_CHOICE_MIN`` fresh candidates
    remain. Without ``usage`` the ordering is the pre-#1328 one.
    """
    cands = [e for e in pool
             if anomaly_fits(e, settings, year, crowd)
             and counts_label.get(e["label"], 0) < MAX_PER_LABEL
             and counts_family.get(e["family"], 0) < MAX_PER_FAMILY
             and build_prompt_key(e) not in used_prompts]
    if usage:
        fresh = [e for e in cands if is_fresh(e, usage)]
        if len(fresh) >= FRESH_CHOICE_MIN:
            cands = fresh
    fam_use = usage.get("families", {}) if usage else {}
    lab_use = usage.get("labels", {}) if usage else {}

    def key(e):
        return (0 if is_fresh(e, usage) else 1,
                fam_use.get(e["family"], 0), lab_use.get(e["label"], 0),
                counts_label.get(e["label"], 0),
                counts_family.get(e["family"], 0))
    cands.sort(key=key)
    return cands


def build_prompt_key(entry: dict) -> str:
    return entry["label"] + "|" + entry["recipe"]


def picker_prompt(source: dict, candidates: list) -> str:
    """The one picker instruction: choose from the given candidate list.

    The rules already guaranteed every candidate is historically valid, so
    the model only judges scene FIT (placement + plausibility); that keeps it
    from "fixing" the rules by picking something anachronistic.
    """
    year = parse_year(source)
    facts = [
        f"year: {year}" if year else "year: unknown",
        f"place: {guess_place(source) or 'unknown'}",
        f"scene: {', '.join(infer_settings(source)) or 'unknown'}",
        f"people present: {'yes' if has_crowd(source) else 'no crowd'}",
    ]
    lines = [f'{i}. "{e["label"]}" - {e["size"]}'
             for i, e in enumerate(candidates, 1)]
    return (
        "This is a real historical photograph. Every candidate below is "
        "historically valid for this scene (era and setting are already "
        "checked). Choose the ONE that would fit THIS particular scene best: "
        "the most natural place to hide it and the most plausible context. "
        "Prefer a candidate whose real-world size and placement suit the "
        "scene over the most anachronistic one.\n"
        f"Photo facts: {'; '.join(facts)}.\n"
        "Candidates:\n" + "\n".join(lines) + "\n"
        'Answer as strict JSON only, no prose: '
        '{"label": "<exact candidate label>", "reason": "<one short sentence>"}. '
        "The label must be copied exactly from the candidate list (the quoted "
        "text only, without the size after the dash)."
    )


def _match_label(raw: str, candidates: list):
    """Map the model's answer to one candidate label, lenient about extras.

    Models tend to echo the size suffix ("Modern daypack (a modern nylon
    daypack, 45-55 cm)") or trailing punctuation; the exact label is then a
    prefix of the answer. The longest matching label wins so a shorter label
    cannot shadow a longer one.
    """
    cleaned = str(raw or "").strip().strip('"').strip("'").strip()
    if not cleaned:
        return None
    low = cleaned.lower()
    exact = next((e for e in candidates if e["label"].lower() == low), None)
    if exact is not None:
        return exact
    prefixed = [e for e in candidates if low.startswith(e["label"].lower())]
    if not prefixed:
        return None
    return max(prefixed, key=lambda e: len(e["label"]))


def parse_pick(content, candidates: list):
    """Parse the picker's answer; None unless it names a listed candidate.

    Lenient about markdown fences/extra prose and about the label's size
    suffix, strict about the label itself: an off-list label is a failed pick
    (the caller falls back to the rule pick), never an excuse to insert an
    anomaly the hard rules excluded.
    """
    if not content:
        return None
    s = re.sub(r"^```(?:json)?\s*|\s*```$", "", str(content).strip())
    m = re.search(r"\{.*\}", s, re.S)
    if not m:
        return None
    try:
        obj = json.loads(m.group(0))
    except json.JSONDecodeError:
        return None
    entry = _match_label(obj.get("label"), candidates)
    if entry is None:
        return None
    return {"label": entry["label"], "reason": str(obj.get("reason") or "")[:200]}


def _choose_entry(source: dict, cands: list, choose):
    """Pick one candidate: model pick when configured, else the rule pick.

    Returns ``(entry, info)``. Any picker problem (missing key, API error,
    empty/off-list answer, a raising injected picker) resolves to ``cands[0]``
    with ``mode="fallback"``, so a failed pick never loses a scene.
    """
    rule = cands[0]
    if choose is None:
        return rule, {"mode": "rules", "picked": rule["label"],
                      "model_pick": None, "reason": "", "usage": None}
    try:
        info = choose(source, cands) or {}
    except Exception as e:  # noqa: BLE001 - picker failure must not lose a scene
        info = {"label": None, "error": f"{type(e).__name__}: {e}"}
    label = info.get("label")
    picked = next((e for e in cands if e["label"] == label), None)
    if picked is None:
        return rule, {"mode": "fallback", "picked": rule["label"],
                      "model_pick": label,
                      "reason": info.get("reason", ""),
                      "error": info.get("error") or "off-list/empty pick",
                      "usage": info.get("usage")}
    return picked, {"mode": "llm", "picked": picked["label"],
                    "model_pick": picked["label"],
                    "reason": info.get("reason", ""),
                    "usage": info.get("usage")}


def plan_day(sources: list, rng: random.Random, choose=None, usage=None):
    """Assign one anomaly per source; returns (plan, skipped, picks).

    The hard rules pre-filter the catalog into candidates per source
    (`pick_entries`). If ``choose`` is given (the LLM picker), it is asked
    which candidate fits the photo best; anything but a valid on-list label
    falls back to the rule pick. Greedy variety keeps one label per set and
    <=2 per family. A source with no fitting candidate is skipped and stays
    unused (e.g. unparseable year, or an empty/unknown setting). ``usage``
    carries the recent-use novelty signal into the candidate order (ticket
    #1328).
    """
    pool = list(ag_catalog.CATALOG)
    rng.shuffle(pool)
    counts_label, counts_family = {}, {}
    used_prompts = set()
    plan, skipped, picks = [], [], []
    for src in sources:
        settings = infer_settings(src)
        year = parse_year(src)
        crowd = has_crowd(src)
        cands = pick_entries(pool, counts_label, counts_family, settings,
                             year, crowd, used_prompts, usage=usage)
        if not cands:
            skipped.append({"id": src["id"], "reason": "no fitting anomaly"})
            continue
        entry, info = _choose_entry(src, cands, choose)
        counts_label[entry["label"]] = counts_label.get(entry["label"], 0) + 1
        counts_family[entry["family"]] = counts_family.get(entry["family"], 0) + 1
        used_prompts.add(build_prompt_key(entry))
        plan.append((src, entry))
        picks.append({**info, "source": src["id"],
                      "candidates": [e["label"] for e in cands]})
    return plan, skipped, picks


def picker_stats(requested: str, dry_run: bool, picks: list,
                 temperature=None, draws=None) -> dict:
    """Report which path ran and the measured picker token/cost footprint."""
    stats = {"mode": requested, "llm": 0, "fallback": 0, "rules": 0,
             "cost": 0.0, "prompt_tokens": 0, "completion_tokens": 0}
    if requested == "llm":
        stats["temperature"] = temperature
        stats["draws"] = draws
    for p in picks:
        mode = p.get("mode")
        if mode in stats:
            stats[mode] += 1
        u = p.get("usage") or {}
        stats["cost"] += float(u.get("cost") or 0)
        stats["prompt_tokens"] += int(u.get("prompt_tokens") or 0)
        stats["completion_tokens"] += int(u.get("completion_tokens") or 0)
    stats["cost"] = round(stats["cost"], 6)
    if dry_run and requested == "llm":
        stats["note"] = "dry run: rule picks only, no picker API call"
    return stats


def picker_temperature(args):
    """The picker temperature to send; None = the provider's default."""
    return None if args.picker_provider_default else args.picker_temperature


def alternative_entry(source: dict, used_prompts: set, rng: random.Random,
                      usage=None):
    """A fitting anomaly for a retry, different from every used prompt.

    Shuffled per call so retries do not always fall back to the same catalog
    entry (run-guard 2: the retry must be materially different).
    """
    pool = list(ag_catalog.CATALOG)
    rng.shuffle(pool)
    cands = pick_entries(pool, {}, {}, infer_settings(source),
                         parse_year(source), has_crowd(source), used_prompts,
                         usage=usage)
    return cands[0] if cands else None


# ── Prompt / entry text (deterministic) ────────────────────────────────────

BLEND = ("Match the ORIGINAL photo's actual tone and coloration EXACTLY, "
         "whatever it is (many old photographs are true black-and-white/"
         "grayscale: the added element must then also be grayscale); never "
         "add sepia, never add any warm or color cast, never add a filter; "
         "film grain lies OVER the object; soft edges, consistent lighting "
         "and shadow direction, realistic perspective; it must look "
         "photographed, not pasted. No glow, low contrast, not a focal "
         "point.")
KEEP = ("Keep every other part of the photograph EXACTLY as it is: same "
        "composition, people, goods and background; do not redraw, move, "
        "add or recolor anything else.")


DEFAULT_OBJECT_SCALE = ("about 2 percent of the image height (roughly 20-30 "
                        "pixels on a 1200-pixel-tall image), never more than "
                        "3 percent")
DEFAULT_PERSON_SCALE = "roughly 8-15 percent of the image height"
# Relative-size anchor (ticket #1328). Evan's "scaling is off" feedback is
# about perspective, not the raw pixel budget: the absolute number alone
# cannot tell the model how large the object should look next to the things
# around it. Every budget sentence therefore names a same-distance reference.
SIZE_ANCHOR = ("judge it against something at the same distance in the "
               "photo (a crate, a wheel or a person's shoe) so its "
               "perspective matches the scene")


def scale_sentence(entry: dict) -> str:
    """The one hard numeric scale cap (engineering-practices: one number).

    Catalog entries override the small-object default where the real-world
    size demands it (traffic cone, tarp, container, bicycle); persons and
    robots get a realistic mid-ground height budget instead. Each sentence
    also carries the SIZE_ANCHOR relative-size rule (ticket #1328).
    """
    if entry["type"] == "person":
        budget = entry.get("scale") or DEFAULT_PERSON_SCALE
        return (f"CRITICAL SCALE: the person's rendered height must be "
                f"realistic for their position ({budget}), {SIZE_ANCHOR}, "
                "never a giant figure.")
    if entry["type"] == "future" and entry["recipe"].startswith("person"):
        budget = entry.get("scale") or DEFAULT_PERSON_SCALE
        return (f"CRITICAL SCALE: its rendered height must be realistic for "
                f"its position ({budget}), {SIZE_ANCHOR}, never a giant "
                "foreground figure.")
    budget = entry.get("scale") or DEFAULT_OBJECT_SCALE
    return (f"CRITICAL SCALE: its rendered height in the final image must be "
            f"{budget}; {SIZE_ANCHOR}; if in doubt make it smaller and hide "
            "more of it behind the foreground object.")


def build_prompt(entry: dict) -> str:
    recipe, _ = ag_catalog.RECIPES[entry["recipe"]]
    if entry["type"] == "object":
        head = (f"Edit this historical photograph: add one small modern "
                f"object, placed naturally {recipe}. The object is "
                f"{entry['size']} ({entry['label']}).")
    elif entry["type"] == "person":
        head = (f"Edit this historical photograph: add ONE entirely new "
                f"person, placed naturally {recipe}. The person is "
                f"{entry['noun']}, whose only modern tells are "
                f"{entry['tells']}. Everything else about the outfit, "
                "posture and appearance matches the photo's era exactly; the "
                "tells are small details, never a full modern outfit.")
    else:  # future / robot
        head = (f"Edit this historical photograph: add ONE clearly "
                f"futuristic {entry['noun']}, placed naturally {recipe}. "
                f"It is {entry['size']} and looks like a time traveler from "
                f"a fictional FUTURE: {entry['tells']}. Do NOT add a flying "
                "saucer, spaceship, dragon, unicorn or any other fantasy "
                "element (time-travel framing only).")
    return " ".join((head, scale_sentence(entry), KEEP, BLEND))


def region_phrase(answer: dict) -> str:
    x, y = answer.get("x", 0.5), answer.get("y", 0.5)
    horiz = "left" if x < 0.34 else ("right" if x > 0.66 else "center")
    vert = "upper" if y < 0.34 else ("lower" if y > 0.66 else "middle")
    if horiz == "center":
        return f"{vert} center"
    return f"{vert} {horiz}"


def article(label: str) -> str:
    return "An" if label[:1].upper() in ("A", "E", "I", "O", "U") else "A"


def build_hints(source: dict, entry: dict, answer: dict) -> list:
    _, where = ag_catalog.RECIPES[entry["recipe"]]
    region = region_phrase(answer)
    if entry["type"] == "object":
        return [
            f"Scan the whole photograph; something small is out of place "
            f"{where}.",
            f"It is a small modern object in the {region} of the frame.",
            f"{article(entry['label'])} {entry['label']} is {where}, in the "
            f"{region}.",
        ]
    if entry["type"] == "person":
        return [
            "Look at the people in the scene; one of them does not belong.",
            f"Check the {where}: one figure is not wearing this era's "
            "clothes or shoes.",
            f"The {entry['noun']} {where}, in the {region}: {entry['tells']}.",
        ]
    return [
        "Look closely; something in this photograph is not from this world "
        "or this century.",
        f"Check the {where}: there is technology that did not exist yet.",
        f"The {entry['noun']} {where}, in the {region}: {entry['tells']}.",
    ]


def build_description(source: dict, year, place: str) -> str:
    title = clean_title(source) or "Historical photograph"
    bits = [title.rstrip(".")]
    if place and place.lower() not in title.lower():
        bits.append(place)
    if year and str(year) not in title:
        bits.append(f"circa {year}")
    repo = source.get("repository") or "an archive"
    bits.append(f"from the {repo} catalogue")
    return ", ".join(bits) + "."


def strip_html(s: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", s or "")).strip()


def build_credit(source: dict) -> str:
    raw = source.get("raw") or {}
    artist = strip_html(str(raw.get("artist") or ""))
    repo = source.get("repository") or "unknown repository"
    return f"{artist} via {repo}" if artist else repo


def build_entry(source: dict, entry: dict, answer: dict, year, place: str,
                date: str) -> dict:
    eid = scene_id(source["id"], entry["label"])
    year_str = str(year) if year else (str(source.get("date") or "").strip()
                                       or "unknown")
    source_block = {k: source.get(k, "") for k in (
        "repository", "fileUrl", "originalTitle", "date", "place", "license",
        "description")}
    return {
        "id": eid,
        "title": clean_title(source) or "Historical photograph",
        "place": place or "Unidentified location",
        "year": year_str,
        "description": build_description(source, year, place),
        "anomaly": entry["label"],
        # Variety bucket (ticket #1232): stored with the scene so the ship
        # step's family preference and its Go mirror read it from the data
        # instead of each keeping a copy of the label->family map.
        "family": entry["family"],
        "answer": answer,
        "hints": build_hints(source, entry, answer),
        "explanation": entry["explanation"],
        "references": [dict(r) for r in entry["references"]],
        "source": source_block,
        "credit": build_credit(source),
        "sourceUrl": source.get("fileUrl", ""),
    }


def verify_label(entry: dict) -> str:
    """`anomaly` string for ag_verify (persons carry their tells)."""
    if entry["type"] == "person":
        return f"{entry['label']}, with {entry['tells']}"
    return entry["label"]


def is_figure(entry: dict) -> bool:
    """True when the anomaly is a human figure (person or humanoid robot).

    Both are whole-body anomalies: the answer must cover head to feet, not
    just the modern tell or the mechanical head (ticket #1308). Passed to
    `ag_verify.verify(person=...)`.
    """
    return entry["type"] == "person" or entry["recipe"].startswith("person")


# ── Scale reporting (ticket #1328) ─────────────────────────────────────────
SIZE_HINT_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(?:%|percent)", re.I)
# Report a scene as over its size budget when the measured rendered size is
# this far above the catalog budget. Reporting only, never a rejection:
# measured on the live queue (2026-09-12), the number does not separate the
# scenes Evan called "scaling is way off" (2.0-2.7x the budget) from scenes
# he praised (up to 7x), so a gate would drop good scenes and keep bad ones.
SCALE_WARN_FACTOR = 1.5


def size_hint_fraction(text):
    """Largest percent number in a vision size hint, as a fraction."""
    if not text:
        return None
    nums = [float(n) for n in SIZE_HINT_RE.findall(str(text))]
    return max(nums) / 100 if nums else None


def measured_scale(verdict: dict):
    """Rendered size of the verified anomaly as a frame-height fraction.

    Whichever is larger: the vision box's longest side or the model's own
    size hint ("about 12 percent of the image height"). None when the
    verdict carries no vision evidence (pixel-diff fallback path).
    """
    vision = verdict.get("vision") or {}
    vals = []
    box = vision.get("box")
    if isinstance(box, list) and len(box) == 4:
        vals.append(max(abs(box[2] - box[0]), abs(box[3] - box[1])))
    hint = size_hint_fraction(vision.get("size_hint"))
    if hint is not None:
        vals.append(hint)
    return max(vals) if vals else None


def scale_report(entry: dict, verdict: dict) -> dict:
    """Measured size + catalog budget for the run report (no gate)."""
    measured = measured_scale(verdict)
    budget = ag_catalog.scale_max(entry)
    if measured is None:
        return {"scale_budget": budget, "scale_measured": None,
                "scale_over_budget": None}
    return {"scale_budget": budget, "scale_measured": round(measured, 4),
            "scale_over_budget": measured > budget * SCALE_WARN_FACTOR}


# ── Model calls ────────────────────────────────────────────────────────────

def aspect_ratio_for(width: int, height: int) -> str:
    if not width or not height:
        return "3:2"
    ratio = width / height
    return min(ASPECTS, key=lambda a: abs(a[1] - ratio))[0]


def _data_url(path: Path) -> str:
    ext = path.suffix.lower().lstrip(".")
    mime = {"jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png",
            "webp": "image/webp", "gif": "image/gif"}.get(ext, "image/jpeg")
    return f"data:{mime};base64,{base64.b64encode(path.read_bytes()).decode()}"


def decode_data_url(url: str) -> bytes:
    if not url.startswith("data:"):
        raise GenerationError(f"unexpected image url (not a data URL): "
                              f"{url[:40]}")
    _, _, b64 = url.partition(",")
    return base64.b64decode(b64)


def image_edit(source_image: Path, prompt: str, api_key: str,
               base_url: str = DEFAULT_BASE_URL, model: str = IMAGE_MODEL,
               aspect_ratio: str = "3:2", image_size: str = DEFAULT_IMAGE_SIZE,
               seed=None, timeout: int = 180) -> bytes:
    """One OpenRouter image-edit call; mirrors the fuchs image_generate tool.

    A fresh int32 seed per call is the #1124 cache-buster; the explicit
    clamp also works around the #1152 seed-overflow 400.
    """
    if seed is None:
        seed = random.randint(0, 2**31 - 1)
    seed = int(seed) % (2**31)
    payload = {
        "model": model,
        "messages": [{
            "role": "user",
            "content": [
                {"type": "text", "text": prompt},
                {"type": "image_url", "image_url": {"url": _data_url(source_image)}},
            ],
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
    images = (choices[0].get("message", {}).get("images") or []) if choices \
        else []
    if not images:
        raise GenerationError("no images returned (model refusal?)")
    return decode_data_url(images[0]["image_url"]["url"])


def picker_content(prompt: str, image_url: str | None) -> list:
    """The picker message parts: the text prompt, plus the image when given.

    ``image_url`` is a data URL (``_data_url(image)``). The A/B harness
    (``ag_picker_ab``, ticket #1217) also calls this with a blank image or
    with ``None`` to measure whether the photo changes the pick at all.
    """
    content = [{"type": "text", "text": prompt}]
    if image_url:
        content.append({"type": "image_url", "image_url": {"url": image_url}})
    return content


def picker_request(prompt: str, api_key: str, base_url: str = DEFAULT_BASE_URL,
                   model: str = DEFAULT_PICKER_MODEL,
                   max_tokens: int = DEFAULT_PICKER_MAX_TOKENS,
                   timeout: int = DEFAULT_PICKER_TIMEOUT,
                   image_url: str | None = None,
                   temperature: float | None = DEFAULT_PICKER_TEMPERATURE
                   ) -> dict:
    """One picker chat call with an optional image part; returns the body.

    Reasoning is explicitly OFF: with thinking on, V4.1-Flash emits only
    ``reasoning_content`` and the ``content`` field is null even with
    generous ``max_tokens`` (measured 2026-09-11, ticket #1211). With
    ``reasoning: {enabled: false}`` a tight output budget is enough.

    ``temperature`` is sent only when not None; 0.0 (the default since
    ticket #1313) makes the pick reproduce, ``None`` keeps the provider's
    sampling default for A/B runs.
    """
    payload = {
        "model": model,
        "messages": [{"role": "user",
                      "content": picker_content(prompt, image_url)}],
        "reasoning": {"enabled": False},
        "max_tokens": max_tokens,
        "stream": False,
    }
    if temperature is not None:
        payload["temperature"] = temperature
    req = urllib.request.Request(
        base_url.rstrip("/") + "/chat/completions",
        data=json.dumps(payload).encode(),
        headers={"Authorization": f"Bearer {api_key}",
                 "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        raise GenerationError(f"picker request failed: HTTP {e.code} "
                              f"{e.read()[:300]!r}") from e
    except urllib.error.URLError as e:
        raise GenerationError(f"picker request failed: {e}") from e


def pick_via_vision(source_image: Path, prompt: str, api_key: str,
                    base_url: str = DEFAULT_BASE_URL,
                    model: str = DEFAULT_PICKER_MODEL,
                    max_tokens: int = DEFAULT_PICKER_MAX_TOKENS,
                    timeout: int = DEFAULT_PICKER_TIMEOUT,
                    temperature: float | None = DEFAULT_PICKER_TEMPERATURE
                    ) -> dict:
    """One vision picker call over the source photo; returns the body."""
    return picker_request(prompt, api_key, base_url, model, max_tokens,
                          timeout, image_url=_data_url(source_image),
                          temperature=temperature)


def majority_label(labels: list):
    """The most frequent label; ties resolve to the earliest pick.

    ``None`` for an all-empty list. Used by the multi-draw picker (ticket
    #1313) and by the A/B harness to summarize repeats.
    """
    clean = [x for x in labels if x]
    if not clean:
        return None
    counts = collections.Counter(clean)
    top = max(counts.values())
    return next(x for x in clean if counts[x] == top)


def _zero_usage() -> dict:
    return {"prompt_tokens": 0, "completion_tokens": 0, "cost": 0.0}


def _add_usage(total: dict, usage) -> dict:
    """Fold one response's usage into a running total (ticket #1313)."""
    usage = usage or {}
    total["prompt_tokens"] += int(usage.get("prompt_tokens") or 0)
    total["completion_tokens"] += int(usage.get("completion_tokens") or 0)
    total["cost"] += float(usage.get("cost") or 0)
    return total


def make_vision_picker(data_dir: Path, api_key: str, base_url: str,
                       model: str = DEFAULT_PICKER_MODEL,
                       max_tokens: int = DEFAULT_PICKER_MAX_TOKENS,
                       timeout: int = DEFAULT_PICKER_TIMEOUT,
                       temperature: float | None = DEFAULT_PICKER_TEMPERATURE,
                       draws: int = DEFAULT_PICKER_DRAWS):
    """Return a ``choose(source, candidates)`` callable for ``plan_day``.

    The callable never raises and never invents a label outside the candidate
    list: a missing image, transport error or unparseable answer returns
    ``label: None`` so ``plan_day`` falls back to the rule pick.

    ``draws`` > 1 asks the same question N times and keeps the majority
    label; usage is summed across the successful draws.
    """
    draws = max(1, int(draws))

    def choose(source: dict, candidates: list) -> dict:
        image = ag_sources.sources_dir(data_dir) / source["image"]
        if not image.exists():
            return {"label": None, "error": f"missing image {image}"}
        prompt = picker_prompt(source, candidates)
        labels, usage, errors, reason = [], _zero_usage(), [], ""
        for _ in range(draws):
            try:
                body = pick_via_vision(image, prompt, api_key, base_url, model,
                                       max_tokens, timeout, temperature)
            except Exception as e:  # noqa: BLE001 - fall back, never lose a scene
                errors.append(f"{type(e).__name__}: {e}")
                continue
            _add_usage(usage, body.get("usage"))
            content = (body.get("choices") or [{}])[0].get("message", {}).get(
                "content")
            match = parse_pick(content, candidates)
            if match is None:
                errors.append("empty model answer" if not content
                              else f"off-list label: {str(content)[:120]}")
                continue
            labels.append(match["label"])
            if not reason:
                reason = match["reason"]
        picked = majority_label(labels)
        if picked is None:
            return {"label": None, "usage": usage,
                    "error": "; ".join(errors) or "no pick"}
        return {"label": picked, "reason": reason, "usage": usage}
    return choose


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


# ── Run loop ───────────────────────────────────────────────────────────────

def _write_bytes(data: bytes, path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def _now_iso() -> str:
    return datetime.datetime.now().astimezone().isoformat(timespec="seconds")


def run(args) -> dict:
    """Run one generation batch, guarded by the cross-process run lock.

    A dry run is free (no lock, no status): it only plans. Every other run
    takes `RunLock` first; if another run (daily cron or a manual click)
    holds it, the report carries a `locked` flag + error and nothing is
    written to the queue or the status file.
    """
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
    started_iso = datetime.datetime.now().astimezone().isoformat(
        timespec="seconds")
    report = {"date": date, "data": str(data_dir), "planned": 0, "added": [],
              "failed": [], "skipped": [], "image_calls": 0, "dry_run": False}

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

    # Top up the source pool first (#1174) so the generator cannot run dry
    # silently. A top-up failure (network, rate limit) must not cost the run
    # its scenes, so it only lands in the report.
    if args.top_up and not args.dry_run:
        try:
            report["topup"] = ag_sources.top_up(
                data_dir, target=args.topup_target,
                max_calls=args.topup_max_calls, date=date,
                log=lambda m: print(m, file=sys.stderr))
        except Exception as e:  # noqa: BLE001 - never block generation
            report["topup"] = {"error": str(e)}

    unused = ag_sources.list_sources(data_dir, unused=True)
    unused = [s for s in unused if s.get("image")]
    if not unused:
        report["error"] = ("source dataset has no unused sources; seed it "
                           "with pipeline/ag_sources.py seed")
        emit(state="error", error=report["error"], finishedAt=_now_iso())
        return report
    if len(unused) < args.count:
        report["warning"] = (f"source pool low: {len(unused)} unused sources, "
                             f"wanted {args.count} scenes "
                             "(run pipeline/ag_sources.py top-up)")

    rng = random.Random(args.seed)
    rng.shuffle(unused)
    # Novelty (ticket #1328): prefer the unused sources that can host an
    # anomaly no recent run has used. The shuffle stays the tie-break, so a
    # fixed seed still reproduces the pick.
    usage = load_label_usage(data_dir, datetime.date.fromisoformat(date))
    if usage:
        unused.sort(key=lambda s: -fresh_candidate_count(s, usage))
        report["novelty"] = {
            "window_days": NOVELTY_WINDOW_DAYS,
            "recent_families": usage.get("families", {}),
            "recent_labels": usage.get("labels", {}),
        }
    picked = unused[:args.count]

    api_key = os.environ.get("OPENROUTER_API_KEY", "")
    choose = None
    if args.picker == "llm" and not args.dry_run:
        if not api_key:
            report["error"] = "OPENROUTER_API_KEY not set (pass --env)"
            emit(state="error", error=report["error"], finishedAt=_now_iso())
            return report
        choose = make_vision_picker(data_dir, api_key, args.base_url,
                                    args.picker_model, args.picker_max_tokens,
                                    args.picker_timeout,
                                    picker_temperature(args),
                                    args.picker_draws)

    plan, skipped, picks = plan_day(picked, rng, choose=choose, usage=usage)
    report["skipped"] = skipped
    report["planned"] = len(plan)
    report["picks"] = picks
    report["picker_stats"] = picker_stats(args.picker, args.dry_run, picks,
                                          picker_temperature(args),
                                          args.picker_draws)
    emit()
    if not plan:
        report["error"] = "no source could be matched to a fitting anomaly"
        emit(state="error", error=report["error"], finishedAt=_now_iso())
        return report

    if args.dry_run:
        report["dry_run"] = True
        report["plan"] = [
            {"source": s["id"], "anomaly": e["label"], "recipe": e["recipe"],
             "year": parse_year(s), "settings": list(infer_settings(s)),
             "picker": picks[i]["mode"], "reason": picks[i].get("reason", ""),
             "candidates": picks[i]["candidates"], "prompt": build_prompt(e)}
            for i, (s, e) in enumerate(plan)
        ]
        return report

    out_dir = Path(args.out_dir) if args.out_dir else Path(
        tempfile.mkdtemp(prefix="ag-gen-"))
    # Planned prompts are "used" from the start, so a retry never repeats a
    # prompt that another scene of the same run already uses.
    used_prompts = {build_prompt_key(e) for _, e in plan}
    for s, entry in plan:
        if args.max_generations and report["image_calls"] >= args.max_generations:
            report["failed"].append({"id": s["id"], "reason": "generation budget"})
            emit()
            continue
        current = entry
        previous_outputs = []
        result = None
        for attempt in range(1, args.max_attempts + 1):
            if args.max_generations and report["image_calls"] >= args.max_generations:
                break
            source_image = ag_sources.sources_dir(data_dir) / s["image"]
            if not source_image.exists():
                result = {"ok": False, "reason": f"missing image {source_image}"}
                break
            prompt = build_prompt(current)
            try:
                report["image_calls"] += 1
                emit()
                img = image_edit(
                    source_image, prompt, api_key, args.base_url,
                    args.image_model, aspect_ratio_for(s.get("width"),
                                                       s.get("height")),
                    args.image_size, timeout=args.image_timeout)
            except GenerationError as e:
                result = {"ok": False, "reason": str(e), "stage": "image",
                          "attempt": attempt}
                continue
            out_path = _write_bytes(img, out_dir / f"{s['id']}-a{attempt}.png")
            try:
                verdict = ag_verify.verify(
                    out_path, source_image, verify_label(current),
                    crop_out=data_dir / "audit" / scene_id(s["id"],
                                                           current["label"]),
                    vision=not args.no_vision, env_path=args.env,
                    vision_model=args.vision_model,
                    dedup=list(previous_outputs) or None,
                    person=is_figure(current))
            except (RuntimeError, OSError, ValueError) as e:
                result = {"ok": False, "reason": f"verify error: {e}",
                          "stage": "verify", "attempt": attempt}
                previous_outputs.append(out_path)
                continue
            previous_outputs.append(out_path)
            if verdict.get("ok"):
                entry_json = build_entry(s, current, verdict["answer"],
                                         parse_year(s), guess_place(s), date)
                try:
                    ag_queue.state_add(data_dir, entry_json, out_path,
                                       source_image, date)
                except Exception as e:  # noqa: BLE001 - queue reject = drop scene
                    result = {"ok": False, "reason": f"queue add: {e}",
                              "stage": "queue", "attempt": attempt}
                    continue
                ag_sources.mark_used(data_dir, [s["id"]], True)
                report["added"].append({
                    "scene": entry_json["id"], "source": s["id"],
                    "anomaly": current["label"],
                    "localization": verdict.get("localization"),
                    "answer": verdict["answer"], "attempt": attempt,
                    **scale_report(current, verdict)})
                result = None
                break
            result = {"ok": False, "reason": verdict.get("reason", "rejected"),
                      "stage": "verify", "attempt": attempt,
                      "localization": verdict.get("localization")}
            # Retry guard (#1122): the next attempt must be materially
            # different, so pick another anomaly instead of resending the
            # same prompt to the same source.
            alt = alternative_entry(s, used_prompts | {build_prompt_key(current)},
                                    rng, usage=usage)
            if alt is None or attempt >= args.max_attempts:
                break
            current = alt
        if result is not None:
            report["failed"].append({"id": s["id"], **result})
        emit()
    report["duration_s"] = round(time.time() - started, 1)
    if report.get("error") or not report["added"]:
        message = report.get("error") or (
            f"no scenes added ({len(report['failed'])} failed)")
        emit(state="error", error=message, finishedAt=_now_iso())
    else:
        emit(state="done", finishedAt=_now_iso(),
             durationS=report["duration_s"])
    return report


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


def parse_args(argv=None):
    p = argparse.ArgumentParser(
        prog="ag_generate.py",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description="Deterministic AnomalyGuessr generator (ticket #1169).")
    p.add_argument("--data", default=None,
                   help="data dir (default: repo data/anomalyguessr)")
    p.add_argument("--count", type=int, default=10,
                   help="scenes to generate (default 10)")
    p.add_argument("--seed", type=int, default=None,
                   help="RNG seed (default: system random); a fixed seed "
                        "reproduces the day's plan")
    p.add_argument("--date", default=None, help="queue date (default today)")
    p.add_argument("--dry-run", action="store_true",
                   help="print the plan, call nothing, write nothing")
    p.add_argument("--env", default=None, help=".env path for API keys")
    p.add_argument("--dm-channel", default=None,
                   help="Discord channel for failure alerts")
    p.add_argument("--image-model", default=IMAGE_MODEL)
    p.add_argument("--vision-model", default=None)
    p.add_argument("--picker", choices=("rules", "llm"), default="llm",
                   help="anomaly picker: a vision call over the fitting "
                        "candidates (llm, default) or the least-used rule "
                        "pick (rules); an llm failure falls back to rules")
    p.add_argument("--picker-model", default=DEFAULT_PICKER_MODEL,
                   help=f"picker vision model (default {DEFAULT_PICKER_MODEL})")
    p.add_argument("--picker-max-tokens", type=int,
                   default=DEFAULT_PICKER_MAX_TOKENS,
                   help="output budget per pick (reasoning is off)")
    p.add_argument("--picker-timeout", type=int,
                   default=DEFAULT_PICKER_TIMEOUT)
    p.add_argument("--picker-temperature", type=float,
                   default=DEFAULT_PICKER_TEMPERATURE,
                   help="picker sampling temperature (default "
                        f"{DEFAULT_PICKER_TEMPERATURE}); with "
                        "--picker-provider-default the request omits it")
    p.add_argument("--picker-provider-default", action="store_true",
                   help="omit temperature from the picker request (use the "
                        "provider's default sampling)")
    p.add_argument("--picker-draws", type=int, default=DEFAULT_PICKER_DRAWS,
                   help="ask the picker N times and keep the majority label "
                        f"(default {DEFAULT_PICKER_DRAWS}; 3 measured only "
                        "~2 points more reproducible than 1, at 3x pick "
                        "cost)")
    p.add_argument("--base-url", default=os.environ.get("OPENAI_BASE_URL")
                   or DEFAULT_BASE_URL)
    p.add_argument("--image-size", default=DEFAULT_IMAGE_SIZE)
    p.add_argument("--image-timeout", type=int, default=180)
    p.add_argument("--no-vision", action="store_true",
                   help="skip the vision localization (diff fallback only)")
    p.add_argument("--max-attempts", type=int, default=2,
                   help="generations per source, each with a different "
                        "anomaly (run-guard #1122; default 2)")
    p.add_argument("--max-generations", type=int, default=0,
                   help="hard cap on image calls per run (0 = count*attempts)")
    p.add_argument("--out-dir", default=None,
                   help="working dir for attempts (default: temp dir)")
    p.add_argument("--report", default=None, help="write the JSON report here")
    p.add_argument("--top-up", action="store_true",
                   help="top up the source pool before picking sources "
                        "(#1174; the daily cron passes this)")
    p.add_argument("--topup-target", type=int,
                   default=ag_sources.DEFAULT_TOPUP_TARGET,
                   help=f"unused sources the top-up aims for "
                        f"(default {ag_sources.DEFAULT_TOPUP_TARGET})")
    p.add_argument("--topup-max-calls", type=int,
                   default=ag_sources.DEFAULT_TOPUP_CALLS,
                   help="backend searches per top-up")
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
