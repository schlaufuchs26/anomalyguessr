#!/usr/bin/env python3
"""AnomalyGuessr source dataset (ticket #1170).

The first step of making the daily generator deterministic (ticket #1169):
a persistent, aggregated pool of historical source photos that the future
generator will draw from. This module owns the dataset itself (the seed/import
step + used-tracking). It does NOT generate scenes or touch the cron; that is
the generator's job later.

The generator's deterministic contract (Evan, #1169/#1170): "randomly select
10 UNUSED source images from the dataset and run them through a sequence of
model calls." This module provides the pool + the ``used`` flag; it marks a
source consumed only when a caller says so after a SUCCESSFUL scene add, so a
failed generation attempt leaves the source reusable.

Layout (mirrors ``data/anomalyguessr/``; lives on disk, gitignored via the
repo's ``data/`` rule, backed up by scripts/backup.sh)::

    data/anomalyguessr/sources/
      index.json                 # {version, sources: {id: SourceEntry}}
      images/<id>.<ext>          # downloaded source photos

Each SourceEntry is the normalized core already required by the scene schema
(repository/fileUrl/originalTitle/date/place/license/description) plus
``licenseUrl``, image size, ``used`` and the per-repository ``raw`` metadata.
Provenance is preserved per image through ``repository`` and ``raw``.

Design (Evan's scope note on #1170): the dataset is AGGREGATED, able to hold
and merge photos from many repositories (Wikimedia Commons, Library of
Congress, Europeana, National Archives, DPLA, ...). Seeding goes through
backend adapters, one per repository, registered by name; each adapter knows
its repository's API + metadata shape and returns the same normalized
SourceEntry, so a new backend is just a new adapter (plus its tests). Licensing
is verified per source within each adapter (the allowlist lives here so it is
one place to audit), never assumed from the photo's age (no era cutoff; all
time periods valid, #1136).

Commands::

    ag_sources.py --data DIR seed --backend commons --query "market street" [--limit N]
    ag_sources.py --data DIR seed --backend manual --dir PATH
    ag_sources.py --data DIR top-up [--target 30] [--max-calls 6]
    ag_sources.py --data DIR status
    ag_sources.py --data DIR list [--repo REPO] [--unused]
    ag_sources.py --data DIR prune-era
    ag_sources.py --data DIR mark-used ID [ID...]
    ag_sources.py --data DIR mark-unused ID [ID...]

``top-up`` (ticket #1174) is the repeatable way to keep the pool stocked: it
walks a built-in list of Commons queries, paging deeper into each query's
result set on every run (per-query ``gsroffset`` kept in the index), until the
pool holds ``--target`` unused sources. It merges by deterministic id, so it
never duplicates an entry and never resets ``used``; the daily generator calls
it before picking sources so the pool cannot run dry silently.

Pure logic lives in module functions so tests can import them; only the
network adapters (Commons) dial live services, and those tests are gated
behind ``AG_SOURCES_NETWORK=1`` (see engineering-practices.md ticket #1084).
"""

import argparse
import datetime
import json
import re
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

INDEX_VERSION = 1

# Core normalized keys every SourceEntry must carry (subset mirrors the scene
# schema's SOURCE_KEYS in ag_queue.py; licenseUrl/width/height/used/raw added).
CORE_KEYS = (
    "id", "repository", "fileUrl", "originalTitle", "date", "place",
    "license", "licenseUrl", "description", "image", "width", "height",
    "used",
)

# Allowlisted license families. PD + CC0 are unambiguously reusable; CC BY /
# CC BY-SA need only attribution (the game's credit field covers that); the
# cron prompt already names "CC0/PD/CC-BY-mit-Attribution/institutionelle
# Freigabe". Noncommercial / no-derivatives / unknown / "all rights reserved"
# are excluded because a game cannot reuse them without restriction.
LICENSE_ALLOW = (
    "public domain", "publicdomain", "no known restriction", "no restrictions",
    "cc0", "cc by", "cc-by", "creative commons attribution",
)
# Families that must NOT pass even if a token from LICENSE_ALLOW sneaks in.
LICENSE_DENY = ("nc", "-nd", "noncommercial", "no derivatives", "all rights reserved")

MIN_WIDTH = 1000

# Wikimedia rate-limits bursts from one IP ("your bot is making too many
# requests", HTTP 429). Space the API calls apart and back off on 429/503,
# because top-up runs several queries + downloads in a row (ticket #1174).
COMMONS_MIN_INTERVAL = 1.0
COMMONS_MAX_RETRIES = 3
COMMONS_BACKOFF = (5.0, 15.0, 45.0)

# Top-up defaults. The pool should comfortably outlast the generator's
# 10 scenes/day even if a run is skipped.
DEFAULT_TOPUP_TARGET = 30
DEFAULT_TOPUP_LIMIT = 20
DEFAULT_TOPUP_CALLS = 6

# Built-in top-up queries. Each fits the scenes the game can use (busy
# street/market/station/harbor photographs; the generator's setting + crowd
# heuristics consume these), and "1900" biases the search toward dated
# historical files so the era rule has a year to work with.
TOPUP_QUERIES = (
    "busy street scene 1900",
    "market street 1900",
    "market place 1900",
    "town square 1900",
    "railway station 1900",
    "train station platform 1900",
    "harbour boats 1900",
    "city street 1900",
    "village fair 1900",
    "street vendors 1900",
    "parade 1900",
    "horse drawn carriages street 1900",
    "historical photograph market 1910",
    "historical photograph street 1890",
)


def default_data_dir() -> Path:
    env = os_environ("AG_DATA_DIR")
    if env:
        return Path(env)
    return Path(__file__).resolve().parent.parent / "data" / "anomalyguessr"


def os_environ(key: str) -> str:
    import os
    return os.environ.get(key, "")


def sources_dir(data_dir: Path) -> Path:
    return data_dir / "sources"


def images_dir(data_dir: Path) -> Path:
    return sources_dir(data_dir) / "images"


def load_index(data_dir: Path) -> dict:
    p = sources_dir(data_dir) / "index.json"
    if not p.exists():
        return {"version": INDEX_VERSION, "sources": {}}
    with open(p, encoding="utf-8") as f:
        return json.load(f)


def save_index(data_dir: Path, index: dict) -> None:
    d = sources_dir(data_dir)
    d.mkdir(parents=True, exist_ok=True)
    p = d / "index.json"
    fd, tmp = tempfile.mkstemp(dir=str(d), prefix=".index-", suffix=".tmp")
    try:
        with os_fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(index, f, indent=2, ensure_ascii=False)
            f.write("\n")
        os_replace(tmp, p)
    finally:
        if os_path_exists(tmp):
            os_unlink(tmp)


# thin os aliases so tests can monkeypatch without importing os everywhere
import os as _os
os_fdopen = _os.fdopen
os_replace = _os.replace
os_path_exists = _os.path.exists
os_unlink = _os.unlink


def license_ok(license_text: str) -> bool:
    """True if a license string is safe to reuse for the game.

    Verified per source, never inferred from the photo's age. A missing/empty
    license is refused (cannot verify).
    """
    if not license_text:
        return False
    low = license_text.lower()
    if any(d in low for d in LICENSE_DENY):
        return False
    return any(a in low for a in LICENSE_ALLOW)


def normalize_id(repo_tag: str, title: str, date: str, file_url: str) -> str:
    """Deterministic slug for a source entry.

    ``repo_tag`` is a short stable tag per repository (commons/loc/...).
    Collisions (same title+date from one repo) are disambiguated with a short
    hash of the file URL so re-seeding the same file yields the same id.
    """
    base = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
    base = re.sub(r"-{2,}", "-", base)
    date_part = re.sub(r"[^0-9]+", "-", (date or "")).strip("-")
    slug = f"{repo_tag}-{base}"
    if date_part:
        slug = f"{slug}-{date_part}"
    slug = slug[:90].rstrip("-")
    h = _short_hash(file_url)
    return f"{slug}-{h}"


def _short_hash(s: str, n: int = 6) -> str:
    # hashlib (not builtin hash()): str hash is salted per process, which
    # would make ids non-deterministic across runs. sha1 hex is stable.
    import hashlib
    return hashlib.sha1(s.encode("utf-8")).hexdigest()[:n]


def mark_used(data_dir: Path, ids, used: bool) -> list:
    """Set ``used`` on the given source ids. Returns ids actually updated.

    Used-tracking is the generator's hook: the future deterministic generator
    marks a source used ONLY after a successful scene was added from it, so a
    failed attempt leaves the source reusable. Unknown ids are ignored (so a
    stale id in a caller does not crash the run).
    """
    index = load_index(data_dir)
    updated = []
    for sid in ids:
        src = index["sources"].get(sid)
        if src is None:
            continue
        if src.get("used") != used:
            src["used"] = used
            updated.append(sid)
    if updated:
        save_index(data_dir, index)
    return updated


def add_sources(data_dir: Path, entries: list, date: str,
                refresh: bool = True) -> dict:
    """Merge entries into the index; idempotent per id. Returns counts.

    An entry is skipped when its id already exists (re-seeding the same source
    does not duplicate it) or its image file is already on disk. When
    ``refresh`` is set (re-seed of an existing id), the normalized metadata
    fields are updated in place while ``id``/``used``/``added``/``image`` are
    preserved: a top-up re-visit may improve a stale ``date``/``place`` from
    #1170's first pass, but must never reset used-tracking (#1174).

    Files are copied in by the caller via ``download`` before this is called;
    here we only record the entry.
    """
    index = load_index(data_dir)
    added, skipped, refreshed = 0, 0, 0
    for e in entries:
        sid = e["id"]
        if sid in index["sources"]:
            skipped += 1
            if refresh and _refresh_entry(index["sources"][sid], e):
                refreshed += 1
            continue
        e["used"] = e.get("used", False)
        e["added"] = date
        index["sources"][sid] = e
        added += 1
    if added or refreshed:
        save_index(data_dir, index)
    return {"added": added, "skipped": skipped, "refreshed": refreshed}


# Metadata fields a refresh may overwrite; identity + bookkeeping stay.
_REFRESH_KEYS = ("repository", "fileUrl", "originalTitle", "date", "place",
                 "license", "licenseUrl", "description", "width", "height",
                 "raw")


def _refresh_entry(existing: dict, new: dict) -> bool:
    changed = False
    for k in _REFRESH_KEYS:
        if k in new and existing.get(k) != new.get(k):
            existing[k] = new[k]
            changed = True
    return changed


# ── Adapter interface ──────────────────────────────────────────────────────

class SourceAdapter:
    """One per repository. Subclasses implement search + normalize + download.

    ``repo``      human-readable repository name (goes in the ``repository``
                  field and the scene's provenance).
    ``repo_tag``  short stable id prefix (commons/loc/...), used in source ids.

    ``search(query, limit)`` returns raw candidate records (repository-shaped).
    ``normalize(raw)`` returns a normalized dict of the CORE_KEYS (minus id/
    image/used/added) or None if the candidate is unusable (wrong orientation,
    too small, bad license).
    ``download_url(raw)`` returns the direct image URL for a candidate.
    """

    repo = "unknown"
    repo_tag = "x"

    def search(self, query: str, limit: int, offset: int = 0):
        raise NotImplementedError

    def normalize(self, raw) -> dict | None:
        raise NotImplementedError

    def download_url(self, raw) -> str:
        raise NotImplementedError


# ── Wikimedia Commons adapter ──────────────────────────────────────────────

UA = "AnomalyGuessr-source-import/1.0 (contact: hugo@fuchs.science)"
COMMONS_API = "https://commons.wikimedia.org/w/api.php"

# Polite pacing shared by every outbound call: the throttle keeps a minimum
# gap between requests from this process, the retry waits out a 429/503.
_last_request = [0.0]


def _throttle(min_interval: float = COMMONS_MIN_INTERVAL) -> None:
    wait = min_interval - (time.monotonic() - _last_request[0])
    if wait > 0:
        time.sleep(wait)
    _last_request[0] = time.monotonic()


def _with_backoff(fn, retries: int = COMMONS_MAX_RETRIES,
                  backoff=COMMONS_BACKOFF):
    """Run ``fn``; retry rate-limit/server errors with an increasing wait.

    Non-retryable HTTP errors (404, ...) and the last attempt re-raise, so
    the caller sees a real failure instead of a silent empty result.
    """
    for attempt in range(retries + 1):
        try:
            return fn()
        except urllib.error.HTTPError as e:
            if e.code not in (429, 503) or attempt >= retries:
                raise
            e.close()  # a retried response's body is abandoned
        except urllib.error.URLError:
            if attempt >= retries:
                raise
        time.sleep(backoff[min(attempt, len(backoff) - 1)])
    raise AssertionError("unreachable")


def _get_bytes(url: str, timeout: int = 60) -> bytes:
    _throttle()
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    return _with_backoff(lambda: _read_all(req, timeout))


def _read_all(req, timeout: int) -> bytes:
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def _commons_api(params: dict) -> dict:
    _throttle()
    url = COMMONS_API + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    return _with_backoff(lambda: _json_body(req))


def _json_body(req) -> dict:
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.load(r)


class CommonsAdapter(SourceAdapter):
    repo = "Wikimedia Commons"
    repo_tag = "commons"

    def search(self, query: str, limit: int, offset: int = 0):
        # ``filetype:bitmap`` narrows to photographs/bitmaps; the caller may
        # add terms like "market street". gsrnamespace=6 = File namespace.
        # ``offset`` pages deeper into the same query's hits, which is what
        # makes the top-up repeatable (the top hits are seeded after the
        # first run; deeper offsets keep yielding new files).
        params = {
            "action": "query", "format": "json",
            "generator": "search",
            "gsrsearch": f"{query} filetype:bitmap",
            "gsrnamespace": "6", "gsrlimit": str(limit),
            "gsroffset": str(offset),
            "prop": "imageinfo|coordinates",
            "iiprop": "url|size|extmetadata",
            "coprop": "type|name|dim", "colimit": "max",
        }
        data = _commons_api(params)
        return list(data.get("query", {}).get("pages", {}).values())

    def normalize(self, raw) -> dict | None:
        ii = (raw.get("imageinfo") or [None])[0]
        if not ii:
            return None
        w, h = ii.get("width", 0), ii.get("height", 0)
        if w <= h or w < MIN_WIDTH:
            return None
        em = ii.get("extmetadata", {})

        def g(key):
            v = em.get(key, {}).get("value", "")
            return v

        license_text = g("LicenseShortName") or g("License")
        if not license_ok(license_text):
            return None
        # ``_idDate`` is the raw metadata date, kept untouched for the id:
        # the improved ``date`` below must not change existing source ids
        # (a changed id would re-add an already-used photo as a duplicate).
        id_date = _strip_html(g("DateTimeOriginal") or g("DateTime"))
        # ObjectName can carry wiki markup (<div class="fn">…); the stored
        # title feeds the scene title and the source id, so strip it here.
        title = _strip_html(g("ObjectName")) or raw.get("title", "")
        description = _strip_html(g("ImageDescription"))
        return {
            "repository": self.repo,
            "fileUrl": ii.get("descriptionurl") or _commons_file_page(raw.get("title", "")),
            "originalTitle": title,
            "date": _commons_date(g("DateTimeOriginal"), g("DateTime"),
                                  title, description),
            "place": extract_place(title=title, categories=g("Categories"),
                                   gps=raw.get("coordinates")),
            "license": license_text,
            "licenseUrl": g("LicenseUrl"),
            "description": description,
            "width": w,
            "height": h,
            "_idDate": id_date,
            "raw": {
                "title": raw.get("title"),
                "pageid": raw.get("pageid"),
                "licenseShortName": license_text,
                "licenseUrl": g("LicenseUrl"),
                "categories": g("Categories"),
                "artist": g("Artist"),
                "dateTime": g("DateTime"),
                "dateTimeOriginal": g("DateTimeOriginal"),
                "gps": _gps_raw(raw.get("coordinates")),
                "credit": g("Credit"),
                "restrictions": g("Restrictions"),
            },
        }

    def download_url(self, raw) -> str:
        ii = (raw.get("imageinfo") or [None])[0]
        return ii["url"]


def _commons_file_page(title: str) -> str:
    if not title:
        return ""
    return "https://commons.wikimedia.org/wiki/" + urllib.parse.quote(title.replace(" ", "_"))


def _strip_html(s: str) -> str:
    s = re.sub(r"<[^>]+>", " ", s or "")
    return re.sub(r"\s+", " ", s).strip()


_ISO_ZERO_RE = re.compile(r"\b(\d{4})-00-00T\d{2}:\d{2}:\d{2}Z\b")

# ── Photo era (year) ───────────────────────────────────────────────────────
# One rule for the dataset and the generator (ticket #1338): the era of a
# photo comes from structured metadata first, free text second, and free text
# only through token guards. A four-digit token inside a name or model number
# is not a year. The July-2024 MBTA photo shipped as "circa 1900" because its
# Commons description reads "a southbound 1900-series Red Line train":
# 1900-series is a vehicle class. A trailing "s" or "er" ("1900s", "1900er")
# already fails the \b boundary; the tail/head patterns below catch the
# hyphenated and "model 1900" forms.
YEAR_RE = re.compile(r"\b(1[5-9]\d{2}|20\d{2})\b")
_YEAR_NAME_TAIL_RE = re.compile(
    r"\s*[-\u2013]?\s*(?:series|class|model|type|baureihe|nr|no)\.?\b", re.I)
_YEAR_NAME_HEAD_RE = re.compile(
    r"(?:series|class|model|type|baureihe|number|nr|no)\W*$", re.I)

# Era cutoff (ticket #1338). The pool holds historical photographs: the
# searches ask for dated files, every catalog anomaly anchors before 2020, and
# an anachronism claim only makes sense against a scene older than the object.
# A photo at or after this year is a born-digital upload; it is refused
# instead of shipped with an invented era. Raise it deliberately, never by
# accident.
MODERN_YEAR = 2000


def _strip_wikidata_date(s: str) -> str:
    """Plain text from Commons' sometimes-wikitext dates.

    ``DateTime`` can be raw wikitext ("1900 date QS:P571,+1900-00-00T00:00:
    00Z/9"); the year is the only part a human or the generator can use.
    """
    s = _strip_html(s)
    if "QS:" in s:
        m = re.search(r"(\d{4})", s)
        if m:
            return m.group(1)
    return _ISO_ZERO_RE.sub(r"\1", s)


def years_in_text(text: str) -> list[int]:
    """Four-digit tokens in ``text`` that can be a photo year, earliest first.

    Guards (ticket #1338): "1900-series", "class 1900", "model 1900" and
    "no. 1900" are names or numbers, not years. "1900s" and "1900er" never
    match at all: no word boundary sits between the digits and the suffix.
    """
    s = _strip_html(str(text or ""))
    found = set()
    for m in YEAR_RE.finditer(s):
        if _YEAR_NAME_TAIL_RE.match(s, m.end()):
            continue
        if _YEAR_NAME_HEAD_RE.search(s[:m.start()]):
            continue
        found.add(int(m.group(1)))
    return sorted(found)


def first_year(text: str):
    """Earliest plausible year in a text, or None."""
    years = years_in_text(text)
    return years[0] if years else None


def text_photo_year(title: str = "", description: str = ""):
    """Era year from free text: the title's own year, else the description's.

    The title is the file's name and speaks about the photograph; a
    description fragment may talk about something else entirely (a
    "1900-series" train, a collection donated in 1982). The title therefore
    wins whenever it names a year.
    """
    year = first_year(title)
    if year is not None:
        return year
    return first_year(description)


def entry_photo_year(entry: dict):
    """Era year of a normalized source entry, or None when it cannot be told.

    Evidence order (ticket #1338):
    1. the free text: title first, then description (guarded, see
       ``years_in_text``);
    2. the ``date`` field, which the adapters fill from structured metadata
       (EXIF / Commons' Information template).

    A modern year in the free text is the photo's own era: a born-digital
    photo names its year in the title. A modern year that only the ``date``
    field carries is the scan/upload stamp of an undated archive photo
    ("2005-09-30" on a c.1900 glass negative), so the era stays unknown.
    None means "no era"; callers refuse the source instead of guessing.
    """
    text_year = text_photo_year(entry.get("originalTitle") or "",
                                entry.get("description") or "")
    if text_year is not None and text_year < MODERN_YEAR:
        return text_year
    date_year = first_year(str(entry.get("date") or ""))
    if text_year is None:
        if date_year is not None and date_year < MODERN_YEAR:
            return date_year
        return None
    return text_year


def ineligible_reason(entry: dict) -> str:
    """Why a source may not become a scene, "" when it is usable.

    Refusing beats guessing (ticket #1338): a photo whose era cannot be
    established cannot carry an anachronism claim, and a born-digital photo
    is no historical scene.
    """
    year = entry_photo_year(entry)
    if year is None:
        return "era unknown"
    if year >= MODERN_YEAR:
        return f"modern era ({year})"
    return ""


def _commons_date(original: str, fallback: str, title: str = "",
                  description: str = "") -> str:
    """Resolved photo date: structured metadata first, free text second.

    ``DateTimeOriginal`` is the EXIF capture date for a born-digital upload
    and the uploader's Information-template date for a scanned archive photo;
    ``DateTime`` is the scan/upload timestamp. Rule (ticket #1338): a
    metadata value with a pre-modern year is the photo date; otherwise the
    guarded free-text year is (the title's year wins over the description);
    a lone modern stamp is kept as its raw value, because it may be the
    capture date of a modern photo. Either way the caller decides with
    ``entry_photo_year`` whether the era is usable.
    """
    cleaned = []
    for v in (original, fallback):
        c = _strip_wikidata_date(v)
        if c and c not in cleaned:
            cleaned.append(c)
    for c in cleaned:
        year = first_year(c)
        if year is not None and year < MODERN_YEAR:
            return c
    text_year = text_photo_year(title, description)
    if text_year is not None:
        return f"circa {text_year}"
    return cleaned[0] if cleaned else ""


# Place heuristics, shared with the generator's guess_place: a title like
# "Street scene in Agana (1899-1900)" or "..., Paris, France" carries the
# place. Deliberately conservative; no match = "" and the entry later says
# "Unidentified location", never a guess.
_PLACE_IN_RE = re.compile(
    r"\bin ([A-Z][\w'.\-]*(?: [A-Z][\w'.\-]*)*"
    r"(?:,\s*[A-Z][\w'.\-]*(?: [A-Z][\w'.\-]*)*)*)")
_PLACE_TAIL_RE = re.compile(
    r",\s*([A-Z][\w'.\-]*(?: [A-Z][\w'.\-]*)*"
    r"(?:,\s*[A-Z][\w'.\-]*(?: [A-Z][\w'.\-]*)*)?)\s*$")

# Commons location categories: "1900 in Hagåtña, Guam", "India in the
# 1900s", "Historical images of Boulevard des Capucines".
_CAT_YEAR_IN_RE = re.compile(r"^\d{3,4}\s+in\s+(.+)$")
_CAT_IN_THE_RE = re.compile(r"^(.+?)\s+in the \d{3,4}s$")
_CAT_HIST_RE = re.compile(r"^Historical images of (.+)$")
# Categories that look place-like but are not: upload provenance,
# collections, and explicit "unidentified" markers.
_CAT_DENY = (
    "unidentified", "unknown", "unspecified", "flickr", "wikimedia",
    "media contributed", "uploaded", "files ", "images from", "photographs of",
    "exposition universelle", "world's fair", "without wikidata",
)


def place_from_text(text: str) -> str:
    """Place from a title/description ("in Agana", trailing ", Paris")."""
    text = _strip_html(str(text or ""))
    if not text:
        return ""
    m = _PLACE_IN_RE.search(text)
    if m:
        return m.group(1).strip().strip('"')
    m = _PLACE_TAIL_RE.search(text)
    if m:
        return m.group(1).strip()
    return ""


def _looks_like_place(cand: str) -> bool:
    # Proper-noun check: rejects lowercase category leftovers like "art" or
    # "photography" that the year-in patterns would otherwise accept.
    return len(cand) > 1 and any(ch.isupper() for ch in cand)


def place_from_categories(categories) -> str:
    """Place from Commons location categories ("1900 in Hagåtña, Guam")."""
    if isinstance(categories, (list, tuple)):
        cats = categories
    else:
        cats = str(categories or "").split("|")
    for raw in cats:
        c = _strip_html(str(raw)).strip()
        if not c or any(d in c.lower() for d in _CAT_DENY):
            continue
        for rx in (_CAT_YEAR_IN_RE, _CAT_IN_THE_RE, _CAT_HIST_RE):
            m = rx.match(c)
            if m:
                cand = m.group(1).strip().strip('"')
                if _looks_like_place(cand):
                    return cand
    return ""


def _format_latlon(lat, lon) -> str:
    try:
        return f"{float(lat):.4f}, {float(lon):.4f}"
    except (TypeError, ValueError):
        return ""


def _gps_place(coords) -> str:
    """GPS coordinates as an honest fallback place ("52.5159, 13.3793")."""
    if not coords:
        return ""
    c = coords[0] if isinstance(coords, list) else coords
    if not isinstance(c, dict):
        return ""
    return _format_latlon(c.get("lat"), c.get("lon"))


def _gps_raw(coords):
    """The CameraLocation/CameraLocation dict kept in ``raw`` (or None)."""
    if not coords:
        return None
    c = coords[0] if isinstance(coords, list) else coords
    if not isinstance(c, dict):
        return None
    return {"lat": c.get("lat"), "lon": c.get("lon"), "type": c.get("type")}


def extract_place(title: str = "", categories="", gps=None) -> str:
    """Place from real metadata, or "" when it cannot be determined.

    Order (ticket #1174): title text > Commons location category > GPS
    coordinates. Never guesses; an unparsable source keeps "" so the
    generator falls back to "Unidentified location".
    """
    p = place_from_text(title)
    if p:
        return p
    p = place_from_categories(categories)
    if p:
        return p
    return _gps_place(gps)


# ── Library of Congress adapter ────────────────────────────────────────────

LOC_SEARCH = "https://www.loc.gov/search/"


def _loc_search(query: str, limit: int) -> dict:
    params = {"q": query, "fo": "json", "at": "results", "c": str(limit)}
    url = LOC_SEARCH + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


class LocAdapter(SourceAdapter):
    repo = "Library of Congress"
    repo_tag = "loc"

    def search(self, query: str, limit: int, offset: int = 0):
        data = _loc_search(query, limit)
        # LOC JSON: results live under ``search.results``.
        return list(data.get("search", {}).get("results", []))

    def normalize(self, raw) -> dict | None:
        if raw.get("id") is None:
            return None
        img = raw.get("image") or []
        if not img:
            return None
        # ``image`` is a list of URLs; the first is typically the full-size.
        download = img[0]
        # LOC records carry ``rights`` as a list of strings, e.g.
        # ["No known restrictions on publication."].
        rights = " ".join(str(x) for x in (raw.get("rights") or []))
        if not license_ok(rights):
            return None
        title = raw.get("title", "") or ""
        date = str(raw.get("date") or "")
        desc = raw.get("description") or ""
        if isinstance(desc, list):
            desc = " ".join(str(x) for x in desc)
        return {
            "repository": self.repo,
            "fileUrl": raw.get("url") or raw.get("id", ""),
            "originalTitle": title,
            "date": date,
            "place": place_from_text(title),
            "license": rights,
            "licenseUrl": "",
            "description": _strip_html(desc),
            "width": 0, "height": 0,
            "raw": {
                "id": raw.get("id"),
                "number": raw.get("number"),
                "partof": raw.get("partof"),
                "thumbnails": raw.get("thumbnail"),
                "subjects": raw.get("subject"),
            },
        }

    def download_url(self, raw) -> str:
        return (raw.get("image") or [""])[0]


# ── Manual (local files) adapter ───────────────────────────────────────────
# Imports a directory of already-downloaded photos with a sidecar metadata
# JSON, so sources can be added without any live API (e.g. a hand-picked set
# from any repository). This also makes the dataset trivially extendable when
# a repository's API is unreachable from a given network.

MANUAL_META = "metadata.json"


class ManualAdapter(SourceAdapter):
    repo = "Manual"
    repo_tag = "manual"

    def __init__(self, directory: Path):
        self.directory = Path(directory)

    def search(self, query: str, limit: int, offset: int = 0):
        # Manual import has no search; scan the directory.
        items = []
        meta_path = self.directory / MANUAL_META
        meta = {}
        if meta_path.exists():
            with open(meta_path, encoding="utf-8") as f:
                meta = json.load(f)
        for img in sorted(self.directory.iterdir()):
            if img.suffix.lower() not in (".jpg", ".jpeg", ".png", ".gif", ".webp", ".tif", ".tiff"):
                continue
            items.append({"path": img, "meta": meta.get(img.name, {})})
        return items

    def normalize(self, raw) -> dict | None:
        m = raw["meta"]
        if not isinstance(m, dict) or not m:
            return None
        license_text = str(m.get("license") or "")
        if not license_ok(license_text):
            return None
        # Size read by the importer after download; leave for the caller to
        # fill (set in the CLI once the file is on disk).
        return {
            "repository": str(m.get("repository") or "Manual"),
            "fileUrl": str(m.get("fileUrl") or ""),
            "originalTitle": str(m.get("originalTitle") or raw["path"].stem),
            "date": str(m.get("date") or ""),
            "place": str(m.get("place") or ""),
            "license": license_text,
            "licenseUrl": str(m.get("licenseUrl") or ""),
            "description": str(m.get("description") or ""),
            "width": int(m.get("width") or 0),
            "height": int(m.get("height") or 0),
            "raw": {k: v for k, v in m.items() if k not in ("license",)},
        }

    def download_url(self, raw) -> str:
        return ""  # handled as a local file copy, not a URL fetch


# ── Registry ───────────────────────────────────────────────────────────────

ADAPTERS = {
    "commons": CommonsAdapter,
    "loc": LocAdapter,
    "manual": ManualAdapter,
}


def get_adapter(name: str, **kw) -> SourceAdapter:
    try:
        cls = ADAPTERS[name]
    except KeyError:
        raise ValueError(
            f"unknown backend {name!r}; known: {', '.join(sorted(ADAPTERS))}"
        )
    return cls(**kw)


# ── Seed pipeline ──────────────────────────────────────────────────────────

def _fetch_bytes(url: str) -> bytes:
    return _get_bytes(url)


def seed_backend(data_dir: Path, backend: str, query: str, limit: int,
                 date: str, adapter_kw=None, offset: int = 0,
                 refresh: bool = True) -> dict:
    """Search a backend, download + normalize candidates, add to the index.

    Returns {added, skipped, refreshed, rejected, era_rejected, downloaded}.
    ``rejected`` counts candidates that failed the license/orientation/size
    filter (they were search hits but not usable) and ``era_rejected`` those
    whose photo era could not be established or is modern (ticket #1338), so a
    seed run can report how picky the allowlist and the era rule were.
    ``offset`` pages into the backend's result set (top-up); ``downloaded +
    rejected + era_rejected`` is the number of hits the backend returned, so a
    caller can tell whether it reached the end of a query.
    """
    adapter = get_adapter(backend, **(adapter_kw or {}))
    raws = adapter.search(query, limit, offset)
    rejected = 0
    era_rejected = 0
    ids = []
    for raw in raws:
        try:
            norm = adapter.normalize(raw)
        except Exception:
            rejected += 1
            continue
        if norm is None:
            rejected += 1
            continue
        # An unusable era is refused at ingestion (ticket #1338): the pool
        # only ever holds photos whose era is established and pre-modern, so
        # no later step can inherit a guessed year.
        if ineligible_reason(norm):
            era_rejected += 1
            continue
        # The id date is the RAW metadata date, not the improved ``date``:
        # changing the normalized date must not mint a second id for a photo
        # already in the pool (that would reset its used flag, #1174).
        id_date = norm.pop("_idDate", norm.get("date", ""))
        sid = normalize_id(adapter.repo_tag, norm["originalTitle"], id_date,
                           norm["fileUrl"] or adapter.download_url(raw))
        norm["id"] = sid
        norm["repository"] = norm.get("repository") or adapter.repo
        # Manual adapter serves local files, not URLs.
        if backend == "manual":
            src = Path(raw["path"])
            ext = src.suffix.lstrip(".")
            target = images_dir(data_dir) / f"{sid}.{ext}"
            target.parent.mkdir(parents=True, exist_ok=True)
            if not target.exists():
                import shutil
                shutil.copyfile(src, target)
            norm["image"] = f"images/{sid}.{ext}"
            w, h = load_image_size(data_dir, norm)
            norm["width"], norm["height"] = w, h
        else:
            url = adapter.download_url(raw)
            ext = _guess_ext(url, norm["originalTitle"])
            target = images_dir(data_dir) / f"{sid}.{ext}"
            if not target.exists():
                _fetch_and_save(url, target)
            norm["image"] = f"images/{sid}.{ext}"
        ids.append(norm)
    res = add_sources(data_dir, ids, date, refresh=refresh)
    return {
        "added": res["added"],
        "skipped": res["skipped"],
        "refreshed": res["refreshed"],
        "rejected": rejected,
        "era_rejected": era_rejected,
        "downloaded": len(ids),
    }


def _guess_ext(url: str, title: str) -> str:
    m = re.search(r"\.(jpg|jpeg|png|gif|webp|tif|tiff)(\?|$)", url, re.I)
    if m:
        return m.group(1).lower() if m.group(1) != "jpeg" else "jpg"
    return "jpg"


def _fetch_and_save(url: str, target: Path) -> None:
    data = _fetch_bytes(url)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(target.parent), prefix=".dl-", suffix=".tmp")
    try:
        with os_fdopen(fd, "wb") as f:
            f.write(data)
        os_replace(tmp, target)
    finally:
        if os_path_exists(tmp):
            os_unlink(tmp)


def load_image_size(data_dir: Path, entry: dict) -> tuple:
    """Read width/height of a stored image (used for manual imports that did
    not know the size at seed time). Returns (0,0) if the file is missing."""
    p = sources_dir(data_dir) / entry["image"]
    if not p.exists():
        return 0, 0
    try:
        import subprocess
        r = subprocess.run(
            ["identify", "-format", "%w %h", str(p)],
            capture_output=True, text=True, check=True,
        )
        w, h = r.stdout.split()
        return int(w), int(h)
    except Exception:
        return 0, 0


# ── Status / listing ───────────────────────────────────────────────────────

def status(data_dir: Path) -> dict:
    index = load_index(data_dir)
    sources = index["sources"]
    used = sum(1 for s in sources.values() if s.get("used"))
    return {
        "total": len(sources),
        "used": used,
        "unused": len(sources) - used,
        "repositories": sorted({s.get("repository", "?") for s in sources.values()}),
    }


def list_sources(data_dir: Path, repo: str | None = None, unused: bool = False) -> list:
    index = load_index(data_dir)
    out = []
    for s in index["sources"].values():
        if repo and s.get("repository") != repo:
            continue
        if unused and s.get("used"):
            continue
        out.append(s)
    out.sort(key=lambda s: s["id"])
    return out


# ── Top-up / re-seed ───────────────────────────────────────────────────────

# Where the per-query result offset + cursor live inside index.json. Keeping
# it in the index (the dataset is the state, no side files) means the pool
# and its paging travel together in backups.
TOPUP_STATE_KEY = "topup"


def _load_topup_state(data_dir: Path) -> dict:
    state = (load_index(data_dir).get(TOPUP_STATE_KEY) or {})
    return {"cursor": int(state.get("cursor") or 0),
            "offsets": dict(state.get("offsets") or {})}


def _save_topup_state(data_dir: Path, state: dict) -> None:
    index = load_index(data_dir)
    index[TOPUP_STATE_KEY] = state
    save_index(data_dir, index)


def prune_ineligible(data_dir: Path) -> dict:
    """Drop pool entries without a usable era; returns what went.

    The counterweight to the ingestion guard (ticket #1338): entries that
    entered the pool before the era rule existed (or through an older
    heuristic) are removed together with their downloaded image, so
    "unused sources" always means usable sources and a refused candidate
    cannot be re-picked every run. Runs inside ``top_up`` before the pool is
    counted, and standalone via ``ag_sources.py prune-era``.
    """
    index = load_index(data_dir)
    removed, images = [], 0
    for sid in sorted(index["sources"]):
        reason = ineligible_reason(index["sources"][sid])
        if not reason:
            continue
        entry = index["sources"].pop(sid)
        removed.append({"id": sid, "reason": reason,
                        "title": entry.get("originalTitle", "")})
        rel = entry.get("image") or ""
        p = sources_dir(data_dir) / rel if rel else None
        if p is not None and p.exists():
            os_unlink(p)
            images += 1
    if removed:
        save_index(data_dir, index)
    return {"removed": removed, "images": images}


def top_up(data_dir: Path, target: int = DEFAULT_TOPUP_TARGET,
           limit: int = DEFAULT_TOPUP_LIMIT, date: str | None = None,
           backend: str = "commons", adapter_kw=None, queries=None,
           max_calls: int = DEFAULT_TOPUP_CALLS, log=None) -> dict:
    """Grow the pool until it holds ``target`` unused sources (ticket #1174).

    Repeatable and idempotent: each call resumes at the persisted per-query
    result offset so successive runs page deeper instead of re-scanning the
    same top hits, and merging is by deterministic id, so nothing duplicates
    and ``used`` is never reset. Stops early when the pool is healthy or after
    ``max_calls`` backend searches. Returns a JSON-able report; the caller
    decides whether a remaining shortfall is a problem.
    """
    say = log or (lambda *a, **k: None)
    date = date or _date_today()
    queries = list(queries or TOPUP_QUERIES)
    if not queries:
        raise ValueError("top_up needs at least one query")
    # Self-heal first (ticket #1338): entries without a usable era must not
    # count as pool stock, or the run would stop early on a phantom pool.
    pruned = prune_ineligible(data_dir)
    if pruned["removed"]:
        say(f"top-up: pruned {len(pruned['removed'])} source(s) "
            f"without a usable era")
    before = status(data_dir)
    report = {"backend": backend, "target": target, "before": before,
              "added": 0, "skipped": 0, "refreshed": 0, "rejected": 0,
              "era_rejected": 0, "pruned": pruned["removed"],
              "downloaded": 0, "calls": 0, "queries": [], "errors": [],
              "stopped": ""}
    if before["unused"] >= target:
        report["stopped"] = "pool healthy"
        report["after"] = before
        report["shortfall"] = 0
        return report

    state = _load_topup_state(data_dir)
    cursor = state["cursor"] % len(queries)
    for _ in range(max_calls):
        if status(data_dir)["unused"] >= target:
            report["stopped"] = "target reached"
            break
        q = queries[cursor % len(queries)]
        offset = int(state["offsets"].get(q, 0))
        say(f"top-up: {q!r} offset {offset}")
        try:
            res = seed_backend(data_dir, backend, q, limit, date, adapter_kw,
                               offset=offset)
        except Exception as e:  # noqa: BLE001 - one dead query must not stop the run
            report["errors"].append({"query": q, "offset": offset,
                                     "error": str(e)})
            state["offsets"][q] = 0
            cursor += 1
            state["cursor"] = cursor % len(queries)
            _save_topup_state(data_dir, state)
            continue
        report["calls"] += 1
        for k in ("added", "skipped", "refreshed", "rejected", "era_rejected",
                  "downloaded"):
            report[k] += res.get(k, 0)
        report["queries"].append({"query": q, "offset": offset, **res})
        hits = (res["downloaded"] + res["rejected"]
                + res.get("era_rejected", 0))
        state["offsets"][q] = offset + limit if hits >= limit else 0
        cursor += 1
        state["cursor"] = cursor % len(queries)
        _save_topup_state(data_dir, state)
    else:
        report["stopped"] = "max calls reached"

    after = status(data_dir)
    report["after"] = after
    report["shortfall"] = max(0, target - after["unused"])
    if after["unused"] >= target:
        report["stopped"] = "target reached"
    return report


# ── CLI ────────────────────────────────────────────────────────────────────

def _date_today() -> str:
    return datetime.date.today().isoformat()


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--data", type=Path, default=None, help="data dir (default: repo data/anomalyguessr)")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("seed", help="search a backend and add sources")
    s.add_argument("--backend", required=True)
    s.add_argument("--query", default="")
    s.add_argument("--limit", type=int, default=20)
    s.add_argument("--dir", type=Path, default=None, help="manual backend source dir")
    s.add_argument("--date", default=None)

    sub.add_parser("status")
    sub.add_parser("prune-era",
                   help="drop pool entries whose photo era is unusable")
    ls = sub.add_parser("list")
    ls.add_argument("--repo", default=None)
    ls.add_argument("--unused", action="store_true")

    tu = sub.add_parser("top-up", help="grow the pool from the built-in queries")
    tu.add_argument("--backend", default="commons")
    tu.add_argument("--target", type=int, default=DEFAULT_TOPUP_TARGET,
                    help="unused sources to aim for")
    tu.add_argument("--limit", type=int, default=DEFAULT_TOPUP_LIMIT,
                    help="hits per backend query")
    tu.add_argument("--max-calls", type=int, default=DEFAULT_TOPUP_CALLS,
                    help="backend searches per run")
    tu.add_argument("--query", action="append", default=None,
                    help="override the built-in query list (repeatable)")
    tu.add_argument("--dir", type=Path, default=None, help="manual backend source dir")
    tu.add_argument("--date", default=None)

    mu = sub.add_parser("mark-used")
    mu.add_argument("ids", nargs="+")
    mnu = sub.add_parser("mark-unused")
    mnu.add_argument("ids", nargs="+")

    args = p.parse_args(argv)
    data_dir = args.data or default_data_dir()
    date = getattr(args, "date", None) or _date_today()

    if args.cmd == "seed":
        kw = {}
        if args.backend == "manual":
            if args.dir is None:
                p.error("--backend manual requires --dir")
            kw = {"directory": args.dir}
        res = seed_backend(data_dir, args.backend, args.query, args.limit, date, kw)
        print(json.dumps(res, indent=2))
        return 0
    if args.cmd == "top-up":
        kw = {}
        if args.backend == "manual":
            if args.dir is None:
                p.error("--backend manual requires --dir")
            kw = {"directory": args.dir}
        rep = top_up(data_dir, target=args.target, limit=args.limit, date=date,
                     backend=args.backend, adapter_kw=kw, queries=args.query,
                     max_calls=args.max_calls,
                     log=lambda m: print(m, file=sys.stderr))
        print(json.dumps(rep, indent=2))
        return 0
    if args.cmd == "status":
        print(json.dumps(status(data_dir), indent=2))
        return 0
    if args.cmd == "prune-era":
        print(json.dumps(prune_ineligible(data_dir), indent=2))
        return 0
    if args.cmd == "list":
        for s in list_sources(data_dir, args.repo, args.unused):
            print(f"{s['id']}\t{('used' if s.get('used') else 'unused')}\t{s['repository']}\t{s['originalTitle']}")
        return 0
    if args.cmd == "mark-used":
        print("updated:", mark_used(data_dir, args.ids, True))
        return 0
    if args.cmd == "mark-unused":
        print("updated:", mark_used(data_dir, args.ids, False))
        return 0
    return 2


if __name__ == "__main__":
    sys.exit(main())
