#!/usr/bin/env python3
"""English scene descriptions from the catalogue record (ticket #1883).

The gallery already shows each scene's ``title`` and ``description``; the
description is the field that still reads as raw metadata. Scenes queued
before this step carry the assembled caption ("<title> · <repository>."),
often in the archive's own language, and some records carry no usable
description at all, so a player who does not read French or Norwegian
learns nothing from the photograph.

This step derives about two English sentences from the record's own text:
the source block the queue stores (repository, fileUrl, originalTitle, date,
place, license, description) plus, when the imported description is empty or
boilerplate, the record's own text fetched from its catalogue page. One text
call translates or condenses that text; it never invents context.

Two guards keep it honest:

* a deterministic check refuses an answer that names a number, a date or a
  proper noun the record's text does not contain, so a translation cannot
  smuggle in outside facts (``unsupported_facts``);
* a record with no text anywhere leaves the scene unchanged, and a failed
  call or a guard hit keeps the existing description instead of shipping
  something worse.

``fileUrl`` is provenance, not prose: it stays out of the model input and
out of the guard's vocabulary, or a digit in the ARK would make any number
look sourced.

CLI::

    ag_describe.py --data DIR status          # catalog counts (no model call)
    ag_describe.py --data DIR apply [--limit N] [--dry-run]
"""

import argparse
import datetime
import json
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import ag_llm
import ag_queue

SOURCE_KEYS = ("repository", "originalTitle", "date", "place", "license",
               "description")
CAPTION_SEP = " \u00b7 "
DEFAULT_MODEL = "deepseek/deepseek-v4.1-flash"
DEFAULT_BASE_URL = ag_llm.DEFAULT_BASE_URL
DESCRIPTION_SOURCE = "catalog"
DESCRIPTION_MAX_TOKENS = 300
MAX_DESCRIPTION_CHARS = 500
SAVE_EVERY = 20
USER_AGENT = "Schlaufuchs-AnomalyGuessr/1.0 (+https://fuchs.science)"

# Catalogue boilerplate that is not a description of the photograph: the
# Gallica bibliographic reference, the National Library of Norway's index
# bookkeeping (its ``originInfo`` is a dict of timestamps, not prose).
_JUNK_RE = re.compile(
    r"(r[ée]f[ée]rence bibliographique"
    r"|appartient [àa] l['’]ensemble documentaire"
    r"|firstindextime|indexlastmodified|firstdigitalcontenttime)",
    re.IGNORECASE)

# Sentence-start detection for the proper-noun guard: a capitalised word at
# the start of a sentence is ordinary English, not a smuggled name.
_SENTENCE_START_RE = re.compile(r"(?:^|[.!?]\s+)$")
_NUMBER_RE = re.compile(r"\d[\d.,:/–-]*\d|\d")
_WORD_RE = re.compile(r"[^\W\d_][\w'’\-]*", re.UNICODE)

# Common English words that are capitalised mid-sentence (titles, months,
# the model's own prose) and must not read as outside facts. The second block
# is institutional vocabulary: a model that expands "NYPL" to "the New York
# Public Library" adds no place, date or number, and flagging "Library" only
# threw away a good description.
_COMMON_WORDS = frozenset("""
a an the and or but of in on at to for from with by as is are was were be
been being this that these those it its he she they them his her their a
photograph photo image picture scene shows showing shown ca circa
january february march april may june july august september october november
december monday tuesday wednesday thursday friday saturday sunday
library museum archive archives collection collections university society
company publishing press institute department ministry government national
public royal state city county
""".split())

_LANG_MARKERS = {
    "fr": (" de ", " la ", " le ", " les ", " des ", " du ", " une ", " et ",
           "photographie", "français", "française", "rue", "société", "é",
           "è", "ê", "à", "ç", "ô", "û", "î", "œ"),
    "de": (" der ", " die ", " das ", " und ", " mit ", "straße", "platz",
           "auf ", "ß", "ü", "ö", "ä"),
    "no": (" og ", " ikke ", " på ", " med ", "gate", "havn", "brygge",
           "torg", "jernbane", "ø", "å", "æ", "fjord"),
    "en": (" the ", " and ", " of ", " with ", "street", "market",
           "photograph", "people", "were", "was ", "from ", "in the"),
}


class DescriptionError(RuntimeError):
    """The description step failed for one scene."""


def _now_iso() -> str:
    return datetime.datetime.now().astimezone().isoformat(timespec="seconds")


# ── Text classification ────────────────────────────────────────────────────


def is_caption(text) -> bool:
    """True for the assembled caption ("<title> · <repository>.")."""
    return CAPTION_SEP in str(text or "")


def language_guess(text) -> str:
    """A cheap language label: "en", "fr", "de", "no" or "unknown".

    Marker word/character counts, not a real detector: it only has to sort
    the queue's captions ("Christian, boxeur · Bibliothèque nationale de
    France") from English prose, and it stays deterministic and offline.
    """
    t = " " + re.sub(r"\s+", " ", str(text or "")).casefold() + " "
    if len(t) < 8:
        return "unknown"
    scores = {lang: sum(1 for m in marks if m in t)
              for lang, marks in _LANG_MARKERS.items()}
    best = max(scores, key=lambda k: (scores[k], k == "en"))
    if scores[best] == 0:
        return "en" if t.strip().isascii() else "unknown"
    # English wins a tie: its markers ("the", "and", "market") are common
    # enough that any single one is already evidence.
    return best


def needs_description(current) -> bool:
    """Whether a scene's stored description still wants this step."""
    text = str(current or "").strip()
    if not text or is_caption(text):
        return True
    return language_guess(text) not in ("en", "unknown")


# ── Record text and fetch ──────────────────────────────────────────────────


def usable_description(text) -> bool:
    """Whether a record's own ``description`` is prose, not boilerplate."""
    t = str(text or "").strip()
    return bool(t) and not _JUNK_RE.search(t)


def source_block_text(source: dict) -> str:
    """The queue's source block as one text, ``fileUrl`` left out (see above)."""
    source = source if isinstance(source, dict) else {}
    return " ".join(str(source.get(k) or "").strip() for k in SOURCE_KEYS
                    if str(source.get(k) or "").strip())


def _strip_tags(text: str) -> str:
    text = re.sub(r"<[^>]+>", " ", str(text or ""))
    return (text.replace("&nbsp;", " ").replace("&amp;", "&")
            .replace("&quot;", '"').replace("&eacute;", "é"))


def _filter_record_text(text: str) -> str:
    """Collapse whitespace and drop catalogue boilerplate lines."""
    text = _JUNK_RE.sub(" ", str(text or ""))
    return re.sub(r"\s+", " ", text).strip()


def http_fetch(url: str, timeout: int = 30) -> str:
    """Default fetcher: the URL's body as text; raises ``DescriptionError``."""
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read().decode("utf-8", "replace")
    except (urllib.error.URLError, OSError, ValueError) as e:
        raise DescriptionError(f"fetch failed: {url}: {e}") from e


def _record_description(url: str, fetch) -> str:
    """The catalogue page's own text for one source URL, or ""."""
    try:
        return _filter_record_text(fetch(url))
    except (DescriptionError, ValueError, TypeError, KeyError, IndexError):
        return ""


def _fetch_gallica(url: str, fetch) -> str:
    m = re.search(r"/ark:/12148/([a-z0-9]+)", url)
    if not m:
        return ""
    body = fetch("https://gallica.bnf.fr/services/OAIRecord?ark=" + m.group(1))
    return _filter_record_text(_strip_tags(body))


def _fetch_nb(url: str, fetch) -> str:
    m = re.search(r"/items/(.+?)/?$", url)
    if not m:
        return ""
    urn = m.group(1)
    body = fetch("https://api.nb.no/catalog/v1/items?q="
                 + urllib.parse.quote(urn))
    items = ((json.loads(body).get("_embedded") or {}).get("items") or [])
    parts = []
    for item in items:
        meta = item.get("metadata") or {}
        if str((meta.get("identifiers") or {}).get("urn") or "") != urn:
            continue
        for key in ("title", "originInfo", "subject"):
            value = meta.get(key)
            if isinstance(value, str):
                parts.append(value)
            elif isinstance(value, dict):
                parts += [str(v) for v in value.values() if isinstance(v, str)]
    return _filter_record_text(" ".join(parts))


def _fetch_commons(url: str, fetch) -> str:
    if "/wiki/" not in url:
        return ""
    title = urllib.parse.unquote(url.split("/wiki/", 1)[1])
    body = fetch("https://commons.wikimedia.org/w/api.php?action=query"
                 "&format=json&prop=imageinfo&iiprop=extmetadata&titles="
                 + urllib.parse.quote(title))
    pages = ((json.loads(body).get("query") or {}).get("pages") or {})
    parts = []
    for page in pages.values():
        for info in page.get("imageinfo") or []:
            meta = info.get("extmetadata") or {}
            for key in ("ImageDescription", "Categories", "ObjectName"):
                value = (meta.get(key) or {}).get("value")
                if value:
                    parts.append(_strip_tags(str(value)))
    return _filter_record_text(" ".join(parts))


def fetch_record_text(source: dict, fetch=None) -> str:
    """The record's own text from its catalogue page; "" when unavailable.

    ``fetch(url) -> str`` is injectable so tests never dial a live service.
    """
    if fetch is None:
        return ""
    source = source if isinstance(source, dict) else {}
    url = str(source.get("fileUrl") or "")
    repository = str(source.get("repository") or "").casefold()
    if "gallica" in repository or "gallica.bnf.fr" in url:
        return _record_description(url, lambda u: _fetch_gallica(u, fetch))
    if "norway" in repository or "nb.no" in url:
        return _record_description(url, lambda u: _fetch_nb(u, fetch))
    if "commons" in repository or "commons.wikimedia.org" in url:
        return _record_description(url, lambda u: _fetch_commons(u, fetch))
    return ""


def record_text(source: dict, fetch=None) -> tuple:
    """``(text, fetched)``: what the model may read, and whether we fetched.

    The source block is always handed over; the catalogue page is only
    fetched when the imported description is empty or boilerplate.
    """
    base = source_block_text(source)
    fetched = ""
    if not usable_description((source or {}).get("description")):
        fetched = fetch_record_text(source, fetch)
    text = " ".join(p for p in (base, fetched) if p.strip())
    return text, bool(fetched)


# ── Prompt and guard ───────────────────────────────────────────────────────


def describe_prompt(record: str, avoid=()) -> str:
    base = (
        "You write the short description under a historical photograph in a "
        "spot-the-anachronism quiz.\n\n"
        "Catalogue record of the photograph:\n"
        f"{record}\n\n"
        "Write about two sentences in English describing the photograph "
        "itself: what it shows, and where, when and by whom it was taken, "
        "whenever the record states it. Translate any non-English record "
        "text into English. Use ONLY facts stated in the record above; do "
        "not add names, places, dates, numbers, events or interpretations "
        "that are not in it. Answer with the description alone, without "
        "quotes or a label."
    )
    if avoid:
        base += (
            "\n\nThe previous attempt used words the record does not "
            "contain: " + ", ".join(sorted(set(avoid))) + ". Write the "
            "description again without them; keep place and personal names "
            "exactly as the record spells them, and leave out anything the "
            "record does not state."
        )
    return base


def unsupported_facts(text: str, vocabulary: str) -> list:
    """Numbers and proper nouns in ``text`` absent from ``vocabulary``.

    A number or date must occur verbatim in the record text; a capitalised
    word must occur case-insensitively. Sentence-initial words and a small
    list of ordinary English words are not treated as names, so the guard
    does not fire on "The photograph shows...".
    """
    vocab = str(vocabulary or "").casefold()
    facts = []
    for num in _NUMBER_RE.findall(str(text or "")):
        if num.casefold() not in vocab:
            facts.append(num)
    for m in _WORD_RE.finditer(str(text or "")):
        token = m.group(0)
        if not token[:1].isupper():
            continue
        if _SENTENCE_START_RE.search(str(text or "")[:m.start()]):
            continue
        low = token.casefold()
        if low in _COMMON_WORDS:
            continue
        if low not in vocab:
            facts.append(token)
    seen = set()
    return [f for f in facts if not (f in seen or seen.add(f))]


def clean_description(text, limit: int = MAX_DESCRIPTION_CHARS) -> str:
    """One plain paragraph: fences/quotes off, whitespace collapsed, capped."""
    s = ag_llm.strip_fences(str(text or ""))
    s = s.strip().strip('"').strip("'").strip()
    s = re.sub(r"\s+", " ", s)
    if len(s) > limit:
        s = s[:limit].rstrip()
        cut = max(s.rfind(". "), s.rfind("! "), s.rfind("? "))
        if cut > limit // 2:
            s = s[:cut + 1]
        else:
            s = s[:s.rfind(" ")].rstrip() + "."
    return s


# ── The model call and the per-scene step ──────────────────────────────────


def describe_text(record: str, api_key: str, model: str,
                  base_url: str = DEFAULT_BASE_URL,
                  max_tokens: int = DESCRIPTION_MAX_TOKENS,
                  timeout: int = 60, avoid=()) -> dict:
    """One text call; returns the trace-shaped call record."""
    prompt = describe_prompt(record, avoid)
    started = time.time()
    body = ag_llm.chat([{"role": "user",
                         "content": [ag_llm.text_part(prompt)]}],
                       api_key, model, base_url, max_tokens, 0.0, timeout,
                       reasoning=False)
    return {"model": model, "prompt": prompt, "answer": ag_llm.content_of(body),
            "reasoning": ag_llm.reasoning_of(body), "image": "",
            "at": _now_iso(), "usage": ag_llm.usage_of(body),
            "duration_s": round(time.time() - started, 2)}


def describe_entry(entry: dict, api_key: str, model: str = DEFAULT_MODEL,
                   base_url: str = DEFAULT_BASE_URL,
                   max_tokens: int = DESCRIPTION_MAX_TOKENS,
                   timeout: int = 60, fetch=None) -> tuple:
    """Give one entry an English description; ``(entry, info)``.

    ``info["status"]`` is ``described`` (written), ``kept`` (already English),
    ``no_record`` (no text anywhere), ``empty`` (the model said nothing),
    ``guarded`` (the facts guard fired; the entry is unchanged) or ``error``.
    A guarded answer never reaches the entry.
    """
    if not needs_description(entry.get("description")):
        return entry, {"status": "kept"}
    source = entry.get("source") if isinstance(entry.get("source"), dict) \
        else {}
    record, fetched = record_text(source, fetch)
    if not record.strip():
        return entry, {"status": "no_record"}
    # One broken call must never kill a pass over the whole queue: the entry
    # keeps what it had and the report names the scene.
    calls = []

    def _ask(avoid=()):
        try:
            call = describe_text(record, api_key, model, base_url, max_tokens,
                                 timeout, avoid)
        except Exception as e:  # noqa: BLE001
            raise DescriptionError(f"{type(e).__name__}: {e}") from e
        calls.append(call)
        text = clean_description(call.get("answer"))
        return text, (unsupported_facts(text, record) if text else [])

    try:
        text, facts = _ask()
    except DescriptionError as e:
        return entry, {"status": "error", "error": str(e), "calls": calls}
    # A translation of the record is allowed to render a French place name in
    # English, but then the guard cannot tell it from a fabricated one. One
    # re-ask names the offending words and asks for the record's own spelling;
    # a scene that still leaks facts stays unchanged (and fails the pass).
    if text and facts:
        try:
            retry_text, retry_facts = _ask(avoid=facts)
        except DescriptionError:
            retry_text = ""
        if retry_text and len(retry_facts) < len(facts):
            text, facts = retry_text, retry_facts
    info = {"fetched": fetched, "record_chars": len(record), "calls": calls,
            "call": calls[-1] if calls else None}
    if not text:
        info["status"] = "empty"
        return entry, info
    if facts:
        info.update({"status": "guarded", "facts": facts})
        return entry, info
    info["status"] = "described"
    out = dict(entry)
    out["description"] = text
    out["description_source"] = DESCRIPTION_SOURCE
    info["description"] = text
    return out, info


# ── Catalog measurement and the pass ───────────────────────────────────────


def measure_state(state: dict) -> dict:
    """Count the catalogue's descriptions: empty, caption, English, foreign."""
    scenes = state.get("scenes") or {}
    empty = caption = english = non_english = described = 0
    by_language = {}
    for scene in scenes.values():
        if scene.get("description_source") == DESCRIPTION_SOURCE:
            described += 1
        current = str(scene.get("description") or "").strip()
        if not current:
            empty += 1
        elif is_caption(current):
            caption += 1
        elif language_guess(current) == "en":
            english += 1
        else:
            non_english += 1
            lang = language_guess(current)
            by_language[lang] = by_language.get(lang, 0) + 1
    total = len(scenes)
    return {"scenes": total, "empty": empty, "caption": caption,
            "english": english, "non_english": non_english,
            "by_language": dict(sorted(by_language.items())),
            "already_described": described,
            "without_description": empty + caption,
            "needs_english": empty + caption + non_english}


def describe_state(data_dir: Path, api_key: str = "", model: str = DEFAULT_MODEL,
                   base_url: str = DEFAULT_BASE_URL, limit: int | None = None,
                   fetch=None, dry_run: bool = False, log=None) -> dict:
    """Run the step over every queued scene that still lacks English text."""
    state = ag_queue.load_state(data_dir)
    report = measure_state(state)
    report["dry_run"] = bool(dry_run)
    report["model"] = model
    targets = [s for s in (state.get("scenes") or {}).values()
               if needs_description(s.get("description"))]
    targets.sort(key=lambda s: (str(s.get("added") or ""),
                                str(s.get("id") or "")))
    if limit is not None:
        targets = targets[:max(0, int(limit))]
    report["targets"] = len(targets)
    changed, skipped, guarded = [], [], []
    totals = ag_llm.zero_usage()
    for done, scene in enumerate(targets, 1):
        if dry_run:
            continue
        try:
            described, info = describe_entry(scene, api_key, model, base_url,
                                             fetch=fetch)
        except Exception as e:  # noqa: BLE001 - one scene must not stop the pass
            described, info = scene, {"status": "error",
                                      "error": f"{type(e).__name__}: {e}"}
        for call in info.get("calls") or []:
            ag_llm.add_usage(totals, call.get("usage"))
        status = info.get("status")
        if status == "described":
            scene["description"] = described["description"]
            scene["description_source"] = DESCRIPTION_SOURCE
            changed.append({"id": scene.get("id"), "fetched": info["fetched"],
                            "chars": len(described["description"])})
        elif status == "guarded":
            guarded.append({"id": scene.get("id"), "facts": info["facts"]})
        elif status != "kept":
            skipped.append({"id": scene.get("id"), "status": status})
        if log:
            log(f"{scene.get('id')}: {status}")
        # A long pass over the whole queue must not lose its work: flush
        # every SAVE_EVERY scenes, not only at the end.
        if changed and not dry_run and done % SAVE_EVERY == 0:
            ag_queue.save_state(data_dir, state)
    if changed and not dry_run:
        ag_queue.save_state(data_dir, state)
    report["described"] = len(changed)
    report["guarded"] = guarded
    report["skipped"] = skipped
    report["details"] = changed
    report["usage"] = {k: (round(v, 6) if isinstance(v, float) else v)
                       for k, v in totals.items()}
    report["cost_per_scene"] = (round(totals["cost"] / len(changed), 6)
                                if changed else None)
    return report


def parse_args(argv=None):
    p = argparse.ArgumentParser(prog="ag_describe.py")
    p.add_argument("--data", type=Path, default=ag_queue.default_data_dir())
    p.add_argument("--env", default=None, help=".env path for the API key")
    p.add_argument("--model", default=DEFAULT_MODEL)
    p.add_argument("--base-url", default=DEFAULT_BASE_URL)
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status", help="catalog description counts, no model call")
    p_apply = sub.add_parser("apply", help="write English descriptions")
    p_apply.add_argument("--limit", type=int, default=None)
    p_apply.add_argument("--dry-run", action="store_true")
    return p.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    if args.cmd == "status":
        print(json.dumps(measure_state(ag_queue.load_state(args.data)),
                         indent=2))
        return 0
    if args.env:
        import ag_verify
        ag_verify.load_env(Path(args.env))
    import os
    report = describe_state(
        args.data, os.environ.get("OPENROUTER_API_KEY", ""), args.model,
        args.base_url, limit=args.limit, fetch=http_fetch,
        dry_run=args.dry_run, log=lambda m: print(m, file=sys.stderr))
    print(json.dumps({k: v for k, v in report.items() if k != "details"},
                     indent=2))
    return 2 if report["guarded"] else 0


if __name__ == "__main__":
    sys.exit(main())
