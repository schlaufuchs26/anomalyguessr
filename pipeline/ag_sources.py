#!/usr/bin/env python3
"""AnomalyGuessr source dataset: Commons "Quality images" pool (ticket #1372).

Rebuilt from the query-driven #1170 dataset. Evan's spec (2026-09-12):
sourcing and generation are two separate problems. Sourcing should get good
source images and nothing else:

- **Wikimedia Commons only for now**, as a consistent baseline.
- **No string heuristics.** Candidates are filtered on structured keys only:
  license, quality rating, MIME/format, pixel dimensions, file size.
- **No year limit.** Any era of photograph is usable; the generator's first
  model call judges the apparent era (a modern photo becomes a fictional
  future, not a rejection).
- **"Quality images" only**, enumerated from the Commons assessment category
  until the pool runs out. `used` marks a consumed source, so "running out"
  is real.

What this module owns: the pool itself (the index + downloaded images), the
key filters, the used flag, and the category walk that fills the pool. It
does NOT generate scenes; that is `pipeline/ag_generate.py`.

Layout (mirrors ``data/anomalyguessr/``; gitignored via the repo's ``data/``
rule, backed up by scripts/backup.sh)::

    data/anomalyguessr/sources/
      index.json                 # {version, sources: {id: SourceEntry},
                                 #  topup: {quality: {continue: ...}}}
      images/<id>.<ext>          # downloaded source photos

Commands::

    ag_sources.py --data DIR top-up [--target 30] [--batch 20] [--max-calls 8]
    ag_sources.py --data DIR status
    ag_sources.py --data DIR list [--repo REPO] [--unused]
    ag_sources.py --data DIR seed --backend manual --dir PATH
    ag_sources.py --data DIR prune
    ag_sources.py --data DIR mark-used ID [ID...]
    ag_sources.py --data DIR mark-unused ID [ID...]

``top-up`` is the repeatable way to fill the pool: it walks
``Category:Quality images`` from a persisted ``cmcontinue`` cursor, keeps the
files that pass the key filters, downloads them and merges them by
deterministic id (never duplicating an entry, never resetting ``used``). The
daily generator calls it before picking sources.

The **born-digital signal** (a file whose EXIF capture date is modern) is
recorded on every entry as ``born_digital``; it is NOT a rejection. Evan's
call (2026-09-12): modern photos are fine, the anomaly then becomes a
fictional-future element. Measured on the first 200 Quality images:
198/200 carry a modern ``DateTimeOriginal``, so treating the signal as a
filter would empty the pool. ``EXCLUDE_BORN_DIGITAL`` flips that to a
rejection if it is ever wanted.

Pure logic lives in module functions so tests can import them; only the
Commons adapter dials the live API and those tests are gated behind
``AG_SOURCES_NETWORK=1`` (engineering-practices.md ticket #1084).
"""

import argparse
import datetime
import json
import os as _os
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
# CC BY-SA need only attribution (the game's credit field covers that).
# Noncommercial / no-derivatives / unknown / "all rights reserved" are
# excluded because a game cannot reuse them without restriction.
LICENSE_ALLOW = (
    "public domain", "publicdomain", "no known restriction", "no restrictions",
    "cc0", "cc by", "cc-by", "creative commons attribution",
)
# Families that must NOT pass even if a token from LICENSE_ALLOW sneaks in.
LICENSE_DENY = ("nc", "-nd", "noncommercial", "no derivatives", "all rights reserved")

# Key filters (Evan's "filter only on keys"): MIME/format, pixel dimensions,
# download size. No year, place or subject filtering.
MIN_WIDTH = 1000
MAX_BYTES = 30 * 1024 * 1024
MIME_EXT = {
    "image/jpeg": "jpg", "image/png": "png", "image/webp": "webp",
    "image/tiff": "tif",
}

# Born-digital signal: a file whose EXIF capture date is at/after this year.
# Recorded, not enforced (see the module docstring).
BORN_DIGITAL_YEAR = 2000
EXCLUDE_BORN_DIGITAL = False

# Wikimedia rate-limits bursts from one IP ("your bot is making too many
# requests", HTTP 429). Space the API calls apart and back off on 429/503.
COMMONS_MIN_INTERVAL = 1.0
COMMONS_MAX_RETRIES = 3
COMMONS_BACKOFF = (5.0, 15.0, 45.0)

# The pool is the Commons quality-assessment category (ticket #1372):
# 463k files when measured on 2026-09-12, walked with a persisted
# ``cmcontinue`` cursor.
QUALITY_CATEGORY = "Category:Quality images"

# Top-up defaults. The pool should comfortably outlast the generator's
# 10 scenes/day even if a run is skipped.
DEFAULT_TOPUP_TARGET = 30
DEFAULT_TOPUP_BATCH = 20
DEFAULT_TOPUP_CALLS = 8

UA = "AnomalyGuessr-source-import/1.0 (contact: hugo@fuchs.science)"
COMMONS_API = "https://commons.wikimedia.org/w/api.php"


def default_data_dir() -> Path:
    env = _os.environ.get("AG_DATA_DIR")
    if env:
        return Path(env)
    return Path(__file__).resolve().parent.parent / "data" / "anomalyguessr"


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
        with _os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(index, f, indent=2, ensure_ascii=False)
            f.write("\n")
        _os.replace(tmp, p)
    finally:
        if _os.path.exists(tmp):
            _os.unlink(tmp)


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


# ── Key filters ────────────────────────────────────────────────────────────

def mime_ext(mime: str) -> str:
    """Extension for an allowlisted MIME type, "" when not allowed."""
    return MIME_EXT.get((mime or "").lower(), "")


def dimensions_ok(width, height, min_width: int = MIN_WIDTH) -> bool:
    """Landscape and wide enough for the game (the queue rejects w<=h)."""
    try:
        w, h = int(width or 0), int(height or 0)
    except (TypeError, ValueError):
        return False
    return w > h and w >= min_width


def entry_reject_reason(entry: dict) -> str:
    """Why a pool entry fails the key filters, "" when it passes.

    Re-runnable on stored entries, so ``prune`` can self-heal the pool with
    the same rule that guards ingestion.
    """
    if not license_ok(str(entry.get("license") or "")):
        return "license"
    mime = str(entry.get("mime") or "")
    if mime and not mime_ext(mime):
        return f"format ({mime})"
    if not dimensions_ok(entry.get("width"), entry.get("height")):
        return f"orientation/size ({entry.get('width')}x{entry.get('height')})"
    return ""


# ── Born-digital signal (recorded, not enforced) ───────────────────────────

_QS_RE = re.compile(r"QS:.*?([12]\d{3})", re.S)


def year_in_metadata(text) -> int | None:
    """Four-digit year from a structured metadata value, or None.

    Only structured fields (EXIF / Commons' Information template) reach this
    function; it is not a free-text parser. A value can be plain
    ("2013-10-24 15:02:48"), a Wikidata wrapper ("1900 date QS:P571,+1900-00
    -00T00:00:00Z/9") or a bare year.
    """
    s = str(text or "")
    if not s:
        return None
    m = _QS_RE.search(s)
    if m:
        return int(m.group(1))
    m = re.search(r"\b(1[0-9]\d{2}|20\d{2})\b", s)
    return int(m.group(1)) if m else None


def exif_year(entry: dict) -> int | None:
    """The file's EXIF capture year (structured metadata), or None.

    ``raw.dateTimeOriginal`` is the capture date; ``raw.dateTime`` is the
    upload/scan timestamp and only a fallback.
    """
    raw = entry.get("raw") or {}
    for key in ("dateTimeOriginal", "dateTime"):
        y = year_in_metadata(raw.get(key))
        if y is not None:
            return y
    return None


def is_born_digital(entry: dict) -> bool:
    """True when the file's own EXIF capture date is modern (>= 2000)."""
    y = exif_year(entry)
    return y is not None and y >= BORN_DIGITAL_YEAR


def metadata_date(original: str, fallback: str) -> str:
    """Plain date string from structured metadata values (no free text).

    Wikidata-wrapped values (``1900 date QS:P571,+1900-00-00T00:00:00Z/9``)
    are reduced to their year; a full EXIF timestamp keeps its date part.
    """
    for v in (original, fallback):
        s = _strip_html(str(v or ""))
        if not s:
            continue
        m = _QS_RE.search(s)
        if m:
            return m.group(1)
        m = re.match(r"(\d{4}-\d{2}-\d{2})", s)
        if m:
            return m.group(1)
        if re.match(r"^\d{4}$", s):
            return s
    return ""


# ── Place from structured keys (coordinates / categories) ──────────────────

# Commons location categories: "1900 in Hagåtña, Guam", "August 2025 in
# Toronto", "India in the 1900s", "Historical images of Boulevard des
# Capucines".
_CAT_YEAR_IN_RE = re.compile(r"^(?:\w+\s+)?\d{3,4}\s+in\s+(.+)$")
_CAT_IN_THE_RE = re.compile(r"^(.+?)\s+in the \d{3,4}s$")
_CAT_HIST_RE = re.compile(r"^Historical images of (.+)$")
# Categories that look place-like but are not: upload provenance,
# collections, and explicit "unidentified" markers.
_CAT_DENY = (
    "unidentified", "unknown", "unspecified", "flickr", "wikimedia",
    "media contributed", "uploaded", "files ", "images from", "photographs of",
    "exposition universelle", "world's fair", "without wikidata",
)


def _strip_html(s: str) -> str:
    s = re.sub(r"<[^>]+>", " ", s or "")
    return re.sub(r"\s+", " ", s).strip()


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


def _gps_raw(coords):
    """The CameraLocation dict kept in ``raw`` (or None)."""
    if not coords:
        return None
    c = coords[0] if isinstance(coords, list) else coords
    if not isinstance(c, dict):
        return None
    return {"lat": c.get("lat"), "lon": c.get("lon"), "type": c.get("type")}


def extract_place(categories="") -> str:
    """Place from the source's structured keys, "" when there is none.

    A Commons location category, never a coordinate pair: raw coordinates
    read as a place in the caption but name nothing, so they stay provenance
    in ``raw.gps`` (ticket #1402). No free-text parsing (ticket #1372): a
    title-based place label is what shipped the wrong location, and the
    source could not back it up.
    """
    return place_from_categories(categories)


# ── Adapter interface ──────────────────────────────────────────────────────

class SourceAdapter:
    """One per repository. Subclasses implement search + normalize + download.

    ``repo``      human-readable repository name (goes in the ``repository``
                  field and the scene's provenance).
    ``repo_tag``  short stable id prefix (commons/loc/...), used in source ids.

    ``normalize(raw)`` returns a normalized dict of the CORE_KEYS (minus id/
    image/used/added) or None if the candidate is unusable (wrong orientation,
    too small, bad license).
    ``download_url(raw)`` returns the direct image URL for a candidate.
    """

    repo = "unknown"
    repo_tag = "x"

    def normalize(self, raw) -> dict | None:
        raise NotImplementedError

    def download_url(self, raw) -> str:
        raise NotImplementedError


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


# ── Wikimedia Commons adapter ──────────────────────────────────────────────

class CommonsAdapter(SourceAdapter):
    """Wikimedia Commons, pool = the "Quality images" assessment category."""

    repo = "Wikimedia Commons"
    repo_tag = "commons"

    def quality_batch(self, limit: int, cursor: str | None = None):
        """One page of Quality images + imageinfo; returns (raws, cursor).

        ``cursor`` is the ``cmcontinue`` token persisted by ``top_up``; None
        starts at the top of the category. The returned cursor is "" when the
        category is exhausted. Titles are fetched in chunks of 50 (the API's
        ``titles`` limit).
        """
        params = {
            "action": "query", "format": "json",
            "list": "categorymembers",
            "cmtitle": QUALITY_CATEGORY, "cmtype": "file",
            "cmlimit": str(max(1, int(limit))),
        }
        if cursor:
            params["cmcontinue"] = cursor
        data = _commons_api(params)
        members = data.get("query", {}).get("categorymembers", [])
        titles = [m.get("title", "") for m in members if m.get("title")]
        next_cursor = (data.get("continue") or {}).get("cmcontinue") or ""
        raws = []
        for i in range(0, len(titles), 50):
            raws.extend(self._imageinfo(titles[i:i + 50]))
        return raws, next_cursor

    def _imageinfo(self, titles):
        params = {
            "action": "query", "format": "json",
            "titles": "|".join(titles),
            "prop": "imageinfo|coordinates",
            "iiprop": "url|size|extmetadata|mime",
            "coprop": "type|name|dim", "colimit": "max",
        }
        data = _commons_api(params)
        return list(data.get("query", {}).get("pages", {}).values())

    def normalize(self, raw) -> dict | None:
        ii = (raw.get("imageinfo") or [None])[0]
        if not ii:
            return None
        w, h = ii.get("width", 0), ii.get("height", 0)
        if not dimensions_ok(w, h):
            return None
        mime = (ii.get("mime") or "").lower()
        if mime and not mime_ext(mime):
            return None
        em = ii.get("extmetadata", {})

        def g(key):
            return em.get(key, {}).get("value", "")

        license_text = g("LicenseShortName") or g("License")
        if not license_ok(license_text):
            return None
        # ObjectName can carry wiki markup (<div class="fn">…); the stored
        # title feeds the scene title and the source id, so strip it here.
        title = _strip_html(g("ObjectName")) or raw.get("title", "")
        description = _strip_html(g("ImageDescription"))
        categories = g("Categories")
        gps = raw.get("coordinates")
        return {
            "repository": self.repo,
            "fileUrl": ii.get("descriptionurl") or _commons_file_page(raw.get("title", "")),
            "originalTitle": title,
            "date": metadata_date(g("DateTimeOriginal"), g("DateTime")),
            "place": extract_place(categories),
            "license": license_text,
            "licenseUrl": g("LicenseUrl"),
            "description": description,
            "width": w,
            "height": h,
            "mime": mime,
            "quality": _strip_html(g("Assessments")),
            "raw": {
                "title": raw.get("title"),
                "pageid": raw.get("pageid"),
                "licenseShortName": license_text,
                "licenseUrl": g("LicenseUrl"),
                "categories": categories,
                "artist": g("Artist"),
                "dateTime": g("DateTime"),
                "dateTimeOriginal": g("DateTimeOriginal"),
                "assessments": g("Assessments"),
                "gps": _gps_raw(gps),
                "credit": g("Credit"),
                "restrictions": g("Restrictions"),
            },
        }

    def download_url(self, raw) -> str:
        ii = (raw.get("imageinfo") or [None])[0]
        return ii["url"]


def quality_ok(entry: dict) -> bool:
    """True when the file's Commons assessment says it is a Quality image.

    Belt and braces on top of the category walk (ticket #1372: the quality
    rating is one of the key filters Evan named).
    """
    return "quality" in str(entry.get("quality") or "").lower()


def _commons_file_page(title: str) -> str:
    if not title:
        return ""
    return "https://commons.wikimedia.org/wiki/" + urllib.parse.quote(title.replace(" ", "_"))


# ── Library of Congress adapter (query/manual escape hatch) ────────────────

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
        # LOC records carry ``rights`` as a list of strings, e.g.
        # ["No known restrictions on publication."].
        rights = " ".join(str(x) for x in (raw.get("rights") or []))
        if not license_ok(rights):
            return None
        title = raw.get("title", "") or ""
        desc = raw.get("description") or ""
        if isinstance(desc, list):
            desc = " ".join(str(x) for x in desc)
        return {
            "repository": self.repo,
            "fileUrl": raw.get("url") or raw.get("id", ""),
            "originalTitle": title,
            "date": str(raw.get("date") or ""),
            "place": place_from_categories(raw.get("partof")),
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


# ── Merge / used tracking ──────────────────────────────────────────────────

def mark_used(data_dir: Path, ids, used: bool) -> list:
    """Set ``used`` on the given source ids. Returns ids actually updated.

    Used-tracking is the generator's hook: it marks a source used ONLY after a
    successful scene was added from it, so a failed attempt leaves the source
    reusable. Unknown ids are ignored (so a stale id does not crash a run).
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
    does not duplicate it). When ``refresh`` is set (re-seed of an existing
    id), the normalized metadata fields are updated in place while
    ``id``/``used``/``added``/``image`` are preserved, so a re-visit can
    repair stale fields without resetting used-tracking.
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
                 "mime", "quality", "raw")


def _refresh_entry(existing: dict, new: dict) -> bool:
    changed = False
    for k in _REFRESH_KEYS:
        if k in new and existing.get(k) != new.get(k):
            existing[k] = new[k]
            changed = True
    return changed


# ── Download / staging ─────────────────────────────────────────────────────

def _guess_ext(url: str, title: str) -> str:
    m = re.search(r"\.(jpg|jpeg|png|gif|webp|tif|tiff)(\?|$)", url, re.I)
    if m:
        return m.group(1).lower() if m.group(1) != "jpeg" else "jpg"
    return "jpg"


def _fetch_and_save(url: str, target: Path) -> int:
    """Download ``url`` to ``target``; returns the byte count."""
    data = _get_bytes(url)
    if len(data) > MAX_BYTES:
        raise ValueError(f"file too large ({len(data)} bytes)")
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(target.parent), prefix=".dl-", suffix=".tmp")
    try:
        with _os.fdopen(fd, "wb") as f:
            f.write(data)
        _os.replace(tmp, target)
    finally:
        if _os.path.exists(tmp):
            _os.unlink(tmp)
    return len(data)


def load_image_size(data_dir: Path, entry: dict) -> tuple:
    """Read width/height of a stored image (manual imports without size)."""
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


def _stage(data_dir: Path, adapter: SourceAdapter, pairs: list,
           date: str) -> dict:
    """Download + record normalized entries. ``pairs`` = [(raw, norm), ...].

    Returns {added, skipped, refreshed, downloaded, failed}; ``failed`` holds
    per-file download errors so one dead URL cannot stop a top-up.
    """
    entries, failed = [], []
    for raw, norm in pairs:
        url = adapter.download_url(raw)
        sid = normalize_id(adapter.repo_tag, norm["originalTitle"],
                           norm.get("date", ""), norm["fileUrl"] or url)
        norm["id"] = sid
        norm["repository"] = norm.get("repository") or adapter.repo
        ext = mime_ext(norm.get("mime")) or _guess_ext(url, norm["originalTitle"])
        target = images_dir(data_dir) / f"{sid}.{ext}"
        if not target.exists():
            try:
                if isinstance(adapter, ManualAdapter):
                    import shutil
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(raw["path"], target)
                else:
                    _fetch_and_save(url, target)
            except Exception as e:  # noqa: BLE001 - one dead file must not stop the run
                failed.append({"id": sid, "url": url, "error": str(e)})
                continue
        norm["image"] = f"images/{sid}.{ext}"
        w, h = load_image_size(data_dir, norm)
        if w and h:
            norm["width"], norm["height"] = w, h
        entries.append(norm)
    res = add_sources(data_dir, entries, date)
    res["downloaded"] = len(entries)
    res["failed"] = failed
    return res


# ── Seed (manual / legacy query backends) ──────────────────────────────────

def seed_backend(data_dir: Path, backend: str, query: str, limit: int,
                 date: str, adapter_kw=None, offset: int = 0,
                 refresh: bool = True) -> dict:
    """Search a query backend, download + normalize candidates, add them.

    The commons pool is filled by ``top_up`` (category walk); this path
    remains for the ``manual`` import and the legacy ``loc`` query backend.
    """
    adapter = get_adapter(backend, **(adapter_kw or {}))
    raws = adapter.search(query, limit, offset)
    rejected = 0
    pairs = []
    for raw in raws:
        try:
            norm = adapter.normalize(raw)
        except Exception:
            rejected += 1
            continue
        if norm is None:
            rejected += 1
            continue
        pairs.append((raw, norm))
    res = _stage(data_dir, adapter, pairs, date)
    return {
        "added": res["added"], "skipped": res["skipped"],
        "refreshed": res["refreshed"], "rejected": rejected,
        "downloaded": res["downloaded"], "failed": res["failed"],
    }


# ── Status / listing / prune ───────────────────────────────────────────────

def status(data_dir: Path) -> dict:
    index = load_index(data_dir)
    sources = index["sources"]
    used = sum(1 for s in sources.values() if s.get("used"))
    born = sum(1 for s in sources.values() if is_born_digital(s))
    return {
        "total": len(sources),
        "used": used,
        "unused": len(sources) - used,
        "born_digital": born,
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


def prune_invalid(data_dir: Path) -> dict:
    """Drop pool entries that fail the key filters; returns what went.

    Self-heal for entries that entered the pool before the filter changed
    (or through an older adapter): removed together with their image, so
    "unused sources" always means usable sources.
    """
    index = load_index(data_dir)
    removed, images = [], 0
    for sid in sorted(index["sources"]):
        reason = entry_reject_reason(index["sources"][sid])
        if not reason:
            continue
        entry = index["sources"].pop(sid)
        removed.append({"id": sid, "reason": reason,
                        "title": entry.get("originalTitle", "")})
        rel = entry.get("image") or ""
        p = sources_dir(data_dir) / rel if rel else None
        if p is not None and p.exists():
            _os.unlink(p)
            images += 1
    if removed:
        save_index(data_dir, index)
    return {"removed": removed, "images": images}


# ── Top-up (Quality images walk) ───────────────────────────────────────────

# Where the category cursor lives inside index.json. Keeping it in the index
# (the dataset is the state, no side files) means the pool and its paging
# travel together in backups.
TOPUP_STATE_KEY = "topup"


def _load_quality_state(data_dir: Path) -> dict:
    state = (load_index(data_dir).get(TOPUP_STATE_KEY) or {})
    return dict(state.get("quality") or {})


def _save_quality_state(data_dir: Path, state: dict) -> None:
    index = load_index(data_dir)
    topup = dict(index.get(TOPUP_STATE_KEY) or {})
    topup["quality"] = state
    index[TOPUP_STATE_KEY] = topup
    save_index(data_dir, index)


def select_candidates(raws, adapter) -> dict:
    """Apply the key filters to a page of raw candidates.

    Returns {pairs, rejected, quality_rejected, born_digital} where ``pairs``
    are the (raw, norm) tuples ready to download. The born-digital signal is
    counted, not (by default) a rejection.
    """
    pairs, rejected, quality_rejected, born_digital = [], 0, 0, 0
    for raw in raws:
        try:
            norm = adapter.normalize(raw)
        except Exception:
            rejected += 1
            continue
        if norm is None:
            rejected += 1
            continue
        if isinstance(adapter, CommonsAdapter) and not quality_ok(norm):
            quality_rejected += 1
            continue
        if is_born_digital(norm):
            born_digital += 1
            if EXCLUDE_BORN_DIGITAL:
                continue
        pairs.append((raw, norm))
    return {"pairs": pairs, "rejected": rejected,
            "quality_rejected": quality_rejected,
            "born_digital": born_digital}


def top_up(data_dir: Path, target: int = DEFAULT_TOPUP_TARGET,
           batch: int = DEFAULT_TOPUP_BATCH, date: str | None = None,
           max_calls: int = DEFAULT_TOPUP_CALLS, log=None) -> dict:
    """Grow the pool from Commons "Quality images" until ``target`` unused.

    Repeatable and idempotent: each call resumes at the persisted category
    cursor, merging is by deterministic id, and ``used`` is never reset.
    Stops early when the pool is healthy, when the category is exhausted, or
    after ``max_calls`` pages. Returns a JSON-able report.
    """
    say = log or (lambda *a, **k: None)
    date = date or _date_today()
    pruned = prune_invalid(data_dir)
    if pruned["removed"]:
        say(f"top-up: pruned {len(pruned['removed'])} entries failing the key filters")
    before = status(data_dir)
    report = {"target": target, "before": before, "added": 0, "skipped": 0,
              "refreshed": 0, "rejected": 0, "quality_rejected": 0,
              "born_digital": 0, "downloaded": 0, "calls": 0, "pruned":
              pruned["removed"], "errors": [], "failed": [], "examples": [],
              "stopped": ""}
    if before["unused"] >= target:
        report["stopped"] = "pool healthy"
        report["after"] = before
        report["shortfall"] = 0
        return report

    adapter = CommonsAdapter()
    cursor = _load_quality_state(data_dir).get("continue") or ""
    for _ in range(max_calls):
        if status(data_dir)["unused"] >= target:
            report["stopped"] = "target reached"
            break
        try:
            raws, next_cursor = adapter.quality_batch(batch, cursor or None)
        except Exception as e:  # noqa: BLE001 - a dead page must not stop the run
            report["errors"].append(str(e))
            break
        report["calls"] += 1
        sel = select_candidates(raws, adapter)
        for k in ("rejected", "quality_rejected", "born_digital"):
            report[k] += sel[k]
        if sel["pairs"]:
            res = _stage(data_dir, adapter, sel["pairs"], date)
            for k in ("added", "skipped", "refreshed", "downloaded"):
                report[k] += res.get(k, 0)
            report["failed"] += res.get("failed", [])
            for _, norm in sel["pairs"][:max(0, 3 - len(report["examples"]))]:
                report["examples"].append(norm.get("originalTitle", ""))
        cursor = next_cursor
        _save_quality_state(data_dir, {"continue": cursor})
        if not cursor:
            report["stopped"] = "category exhausted"
            break
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
    p.add_argument("--data", type=Path, default=None,
                   help="data dir (default: repo data/anomalyguessr)")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("seed", help="import from a query backend (manual/loc)")
    s.add_argument("--backend", required=True)
    s.add_argument("--query", default="")
    s.add_argument("--limit", type=int, default=20)
    s.add_argument("--dir", type=Path, default=None, help="manual backend source dir")
    s.add_argument("--date", default=None)

    sub.add_parser("status")
    sub.add_parser("prune",
                   help="drop pool entries that fail the key filters")
    ls = sub.add_parser("list")
    ls.add_argument("--repo", default=None)
    ls.add_argument("--unused", action="store_true")

    tu = sub.add_parser("top-up", help="walk Commons Quality images into the pool")
    tu.add_argument("--target", type=int, default=DEFAULT_TOPUP_TARGET,
                    help="unused sources to aim for")
    tu.add_argument("--batch", type=int, default=DEFAULT_TOPUP_BATCH,
                    help="category members per page")
    tu.add_argument("--max-calls", type=int, default=DEFAULT_TOPUP_CALLS,
                    help="category pages per run")
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
        rep = top_up(data_dir, target=args.target, batch=args.batch, date=date,
                     max_calls=args.max_calls,
                     log=lambda m: print(m, file=sys.stderr))
        print(json.dumps(rep, indent=2))
        return 0
    if args.cmd == "status":
        print(json.dumps(status(data_dir), indent=2))
        return 0
    if args.cmd == "prune":
        print(json.dumps(prune_invalid(data_dir), indent=2))
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
