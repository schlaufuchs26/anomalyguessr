#!/usr/bin/env python3
"""Date-exactness audit for candidate source collections (ticket #1430).

The game's claim is "this could not exist in the photograph's year", so the
year must be a fact the repository asserts about the item. Evan's bar
(2026-09-13): a dataset that "unambiguously establishes the date the photo
depicts with no room for error". This script measures candidate collections
over their own APIs and reports, per collection:

- share of sampled items whose date field names exactly one four-digit year,
- share that is dated but only as circa / range / decade / century,
- share with no date at all,
- the metadata field that supplies the date,
- license usability (public domain / CC-BY / CC0, no NC/ND/unknown),
- image usability (landscape, >= 1000 px, size from the repository's own
  image service).

Collections that cannot be reached from this host are listed with the
observed evidence (HTTP status, API-key requirement, robots.txt) instead of
a fabricated sample.

Run::

    python3 pipeline/ag_dates_audit.py --sample 60
    python3 pipeline/ag_dates_audit.py --sample 60 --source gallica,wellcome
    python3 pipeline/ag_dates_audit.py --list

Live network script: the tests cover ``analyze`` (pure) and are gated behind
``AG_SOURCES_NETWORK=1`` for the probes (engineering-practices.md #1084).
"""

import argparse
import json
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import ag_sources  # noqa: E402

UA = ag_sources.UA
GALLICA_SRU = "https://gallica.bnf.fr/SRU"
WELLCOME_API = "https://api.wellcomecollection.org/catalogue/v2"

# Queries per collection: a broad query over the repository's own catalogue
# of photographs, not a date-filtered query (a date filter would rig the
# exact-year share we are trying to measure).
GALLICA_QUERY = ('dc.type all "image" and dc.rights all "domaine public" '
                 'and gallica all "photographie de presse"')
WELLCOME_QUERY = "photograph"

# Collections the ticket names that this host cannot measure. Each entry
# records the observed evidence, so the table says "not reachable" with a
# reason instead of an invented number.
BLOCKED = (
    {"source": "Library of Congress (FSA/OWI, Detroit Publishing, photochrom)",
     "endpoint": "https://www.loc.gov/search/?q=...&fo=json",
     "observed": "HTTP 403, Cloudflare interstitial ('Just a moment...')",
     "note": "bot challenge blocks this datacenter IP; needs a browser or a "
             "residential node ([[mini-pc-residential-node]], #1155)"},
    {"source": "Deutsche Digitale Bibliothek / Bundesarchiv",
     "endpoint": "https://api.deutsche-digitale-bibliothek.de/search",
     "observed": "HTTP 403 unauthenticated",
     "note": "needs a registered API key"},
    {"source": "Deutsche Fotothek (SLUB Dresden)",
     "endpoint": "https://www.deutschefotothek.de/",
     "observed": "robots.txt: 'User-agent: * / Disallow: /'; search sits "
                 "behind a proof-of-work page",
     "note": "crawling is explicitly disallowed for every agent but "
             "Googlebot; left alone"},
    {"source": "Nationaal Archief (NL)",
     "endpoint": "https://www.nationaalarchief.nl/",
     "observed": "HTTP 403 (datacenter block)",
     "note": "residential node needed"},
    {"source": "Smithsonian Open Access",
     "endpoint": "https://api.si.edu/openaccess/api/v1.0/search",
     "observed": "HTTP 403 without api_key",
     "note": "free API key, but none is configured on this host (data.si.edu)"},
    {"source": "NYPL Digital Collections",
     "endpoint": "https://api.repo.nypl.org/api/v2/items/search",
     "observed": "HTTP 401 without token",
     "note": "free token from the NYPL API portal"},
    {"source": "Europeana (edm:timeSpan)",
     "endpoint": "https://api.europeana.eu/record/v2/search.json",
     "observed": "HTTP 401 without wskey",
     "note": "free API key (pro.europeana.eu)"},
)


def _http_json(url: str, timeout: int = 40) -> dict:
    ag_sources._throttle(0.5)
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def _http_text(url: str, timeout: int = 40) -> str:
    ag_sources._throttle(0.5)
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")


# ── Date classification (pure) ─────────────────────────────────────────────

def date_kind(raw) -> str:
    """'exact' | 'approximate' | 'none' for one repository date value.

    ``exact`` reuses the pool's own rule (``ag_sources.single_year``): one
    unambiguous four-digit year, no range, decade, century or uncertainty
    marker. ``approximate`` is a non-empty value that fails that rule.
    """
    text = str(raw or "").strip()
    if not text:
        return "none"
    return "exact" if ag_sources.single_year(text) is not None \
        else "approximate"


def analyze(rows) -> dict:
    """Date/license/image statistics for one collection's sample (pure).

    Each row: ``{"title", "date", "date_field", "license_ok", "width",
    "height"}``. ``image_ok`` is the pool's own key filter (landscape, at
    least ``ag_sources.MIN_WIDTH`` px). ``eligible`` is the share a sourcing
    run could actually use today: exact year, usable license, usable image.
    """
    stats = {"sample": len(rows), "exact": 0, "approximate": 0, "none": 0,
             "license_ok": 0, "image_ok": 0, "eligible": 0,
             "date_fields": {}, "examples": []}
    fields = {}
    for row in rows:
        kind = date_kind(row.get("date"))
        stats[kind] += 1
        field = str(row.get("date_field") or "?")
        fields[field] = fields.get(field, 0) + 1
        if row.get("license_ok"):
            stats["license_ok"] += 1
        if ag_sources.dimensions_ok(row.get("width"), row.get("height")):
            stats["image_ok"] += 1
        if (kind == "exact" and row.get("license_ok")
                and ag_sources.dimensions_ok(row.get("width"),
                                             row.get("height"))):
            stats["eligible"] += 1
            if len(stats["examples"]) < 5:
                stats["examples"].append(
                    {"title": str(row.get("title") or "")[:90],
                     "date": str(row.get("date") or ""),
                     "width": row.get("width"), "height": row.get("height"),
                     "url": str(row.get("url") or "")})
    stats["date_fields"] = dict(sorted(fields.items()))
    for key in ("exact", "approximate", "none", "license_ok", "image_ok",
                "eligible"):
        stats[f"{key}_share"] = (round(stats[key] / len(rows), 3)
                                 if rows else None)
    return stats


# ── Commons probe (pool baseline) ──────────────────────────────────────────

def pick_commons_fields(raw) -> dict:
    """One Commons candidate -> audit row, from the structured keys only."""
    ii = (raw.get("imageinfo") or [None])[0]
    if not ii:
        return None
    em = ii.get("extmetadata", {})
    g = lambda k: (em.get(k) or {}).get("value", "")  # noqa: E731
    date = g("DateTimeOriginal") or g("DateTime")
    license_text = g("LicenseShortName") or g("License")
    return {"title": g("ObjectName") or raw.get("title", ""),
            "date": date, "date_field": "extmetadata.DateTimeOriginal",
            "license_ok": ag_sources.license_ok(license_text),
            "width": ii.get("width"), "height": ii.get("height"),
            "url": ii.get("descriptionurl") or ""}


def sample_commons(sample: int) -> list:
    adapter = ag_sources.CommonsAdapter()
    cursor, raws = None, []
    while len(raws) < sample:
        page, cursor = adapter.quality_batch(
            max(1, min(50, sample - len(raws))), cursor)
        raws.extend(page)
        if not page or not cursor:
            break
    rows = [pick_commons_fields(r) for r in raws]
    return [r for r in rows if r]


# ── Gallica (BnF) probe ────────────────────────────────────────────────────
#
# Gallica publishes its catalogue through an SRU interface
# (searchRetrieve over Dublin Core). ``dc:date`` is the catalogue's date for
# the item, ``dc:rights`` the rights statement, ``dc:identifier`` the ARK.
# Image dimensions come from the IIIF service's page-1 info.json; the image
# itself from the same service (full/full).

_GALLICA_ARK_RE = re.compile(r"ark:/12148/([a-z0-9]+)")
_GALLICA_PUBLIC_DOMAIN = ("domaine public", "public domain")


def parse_gallica_records(xml: str) -> list:
    """Raw SRU records -> ``[{title, dates, rights, ark}]`` (pure)."""
    out = []
    for rec in re.findall(r"<srw:record>(.*?)</srw:record>", xml, re.S):
        def all_of(tag):
            return [ag_sources._strip_html(x) for x in
                    re.findall(rf"<{tag}>(.*?)</{tag}>", rec, re.S)]
        ark = ""
        for ident in all_of("dc:identifier"):
            m = _GALLICA_ARK_RE.search(ident)
            if m:
                ark = m.group(1)
                break
        out.append({"title": (all_of("dc:title") or [""])[0],
                    "dates": all_of("dc:date"),
                    "rights": all_of("dc:rights"),
                    "ark": ark})
    return out


def gallica_info(ark: str) -> tuple:
    """(width, height) of a Gallica item's first page, (0, 0) on failure."""
    try:
        j = _http_json(f"https://gallica.bnf.fr/iiif/ark:/12148/{ark}"
                       "/f1/info.json")
    except (urllib.error.URLError, ValueError):
        return 0, 0
    return int(j.get("width") or 0), int(j.get("height") or 0)


def gallica_row(rec: dict) -> dict:
    ark = rec.get("ark") or ""
    w, h = gallica_info(ark) if ark else (0, 0)
    rights = " ".join(rec.get("rights") or [])
    return {"title": rec.get("title", ""), "date": (rec.get("dates") or [""])[0],
            "date_field": "dc:date", "license_ok": any(
                p in rights.lower() for p in _GALLICA_PUBLIC_DOMAIN),
            "width": w, "height": h,
            "url": f"https://gallica.bnf.fr/ark:/12148/{ark}" if ark else ""}


def sample_gallica(sample: int) -> list:
    rows, start = [], 1
    while len(rows) < sample:
        params = {"operation": "searchRetrieve", "version": "1.2",
                  "query": GALLICA_QUERY, "maximumRecords": "50",
                  "startRecord": str(start)}
        xml = _http_text(GALLICA_SRU + "?" + urllib.parse.urlencode(params))
        recs = parse_gallica_records(xml)
        if not recs:
            break
        rows.extend(gallica_row(r) for r in recs)
        start += len(recs)
    return rows[:sample]


# ── Wellcome Collection probe ──────────────────────────────────────────────

def wellcome_date(work: dict) -> tuple:
    """(date label, field) from a Wellcome work's production events."""
    labels = [d.get("label") for p in (work.get("production") or [])
              for d in (p.get("dates") or []) if d.get("label")]
    return (labels[0] if labels else "", "production.dates[].label")


def sample_wellcome(sample: int) -> list:
    rows, page = [], 1
    seen = set()
    while len(rows) < sample:
        try:
            data = _http_json(
                f"{WELLCOME_API}/images?query={urllib.parse.quote(WELLCOME_QUERY)}"
                f"&pageSize=100&page={page}")
        except (urllib.error.URLError, ValueError):
            break
        results = data.get("results") or []
        if not results:
            break
        for im in results:
            if len(rows) >= sample:
                break
            src = im.get("source") or {}
            wid = src.get("id")
            if not wid or wid in seen:
                continue
            seen.add(wid)
            loc = (im.get("locations") or [{}])[0]
            lic = (loc.get("license") or {}).get("id") or ""
            try:
                work = _http_json(f"{WELLCOME_API}/works/{wid}")
            except (urllib.error.URLError, ValueError):
                continue
            date, field = wellcome_date(work)
            w, h = (0, 0)
            info = (im.get("thumbnail") or {}).get("url") or ""
            if info:
                try:
                    j = _http_json(info)
                    w, h = int(j.get("width") or 0), int(j.get("height") or 0)
                except (urllib.error.URLError, ValueError):
                    pass
            rows.append({"title": src.get("title", ""), "date": date,
                         "date_field": field,
                         "license_ok": lic in ("pdm", "cc0", "cc-by",
                                               "cc-by-sa"),
                         "width": w, "height": h,
                         "url": f"https://wellcomecollection.org/works/{wid}"})
        page += 1
    return rows


# ── National Library of Norway probe (ticket #1535) ────────────────────────
#
# Sesam REST API; ``metadata.dateCreated`` is the library's catalogue date,
# ``accessInfo`` carries the licence, and the IIIF ``info.json`` the served
# image size. The probe measures the same photograph query the pool's NB walk
# uses (``ag_sources.NB_WALK_QUERIES[0]``).

NB_ITEMS = "https://api.nb.no/catalog/v1/items"


def nb_row(item: dict) -> dict:
    """One Sesam search item -> audit row."""
    meta = item.get("metadata") or {}
    access = item.get("accessInfo") or {}
    urn = (meta.get("identifiers") or {}).get("urn") or ""
    w, h = ag_sources.nb_size(urn) if urn else (0, 0)
    return {"title": meta.get("title") or "",
            # The compact YYYYMMDD/YYYYMM form is expanded so the shared
            # single-year rule reads the same shape for every item.
            "date": ag_sources.nb_date_text(meta.get("dateCreated")),
            "date_field": "metadata.dateCreated",
            "license_ok": bool(access.get("isPublicDomain")) and
            ag_sources.license_ok(str(access.get("license") or "")),
            "width": w, "height": h,
            "url": f"{ag_sources.NB_ITEM}/{urn}" if urn else ""}


def sample_nb(sample: int) -> list:
    rows, page = [], 0
    while len(rows) < sample:
        params = {"q": ag_sources.NB_WALK_QUERIES[0],
                  "size": str(min(100, max(1, sample - len(rows)))),
                  "page": str(page), "filter": ag_sources.NB_MEDIA_FILTER}
        data = _http_json(NB_ITEMS + "?" + urllib.parse.urlencode(params))
        items = (data.get("_embedded") or {}).get("items") or []
        if not items:
            break
        rows.extend(nb_row(i) for i in items)
        page += 1
    return rows[:sample]


PROBES = {
    "commons": sample_commons,
    "gallica": sample_gallica,
    "nb": sample_nb,
    "wellcome": sample_wellcome,
}


def measure(sample: int, sources) -> dict:
    """Run the probes and return ``{source: analyze(rows)}`` plus blocked."""
    out = {}
    for name in sources:
        probe = PROBES.get(name)
        if probe is None:
            continue
        try:
            out[name] = analyze(probe(sample))
        except (urllib.error.URLError, ValueError) as e:  # pragma: no cover
            out[name] = {"error": f"{type(e).__name__}: {e}"}
    return {"sample": sample, "sources": out, "blocked": list(BLOCKED)}


def markdown_table(report: dict) -> str:
    """The summary table: one row per collection (ticket #1430, part 1)."""
    head = ("| collection | sample | exact year | dated, not exact | no date "
            "| license ok | image ok | eligible (exact+license+image) | date "
            "field |\n|---|---|---|---|---|---|---|---|---|")
    lines = [head]
    for name, st in report["sources"].items():
        if "error" in st:
            lines.append(f"| {name} | - | - | - | - | - | - | - | probe "
                         f"failed: {st['error']} |")
            continue
        lines.append(
            f"| {name} | {st['sample']} | {st['exact']} "
            f"({st['exact_share']:.0%}) | {st['approximate']} | {st['none']} "
            f"| {st['license_ok']} | {st['image_ok']} | {st['eligible']} "
            f"({(st['eligible_share'] or 0):.0%}) "
            f"| {', '.join(st['date_fields'])} |")
    for b in report["blocked"]:
        lines.append(f"| {b['source']} | not reachable from this host: "
                     f"{b['observed']} | | | | | | | {b['endpoint']} |")
    return "\n".join(lines)


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--sample", type=int, default=60,
                   help="items to sample per collection")
    p.add_argument("--source", default=",".join(PROBES),
                   help=f"comma-separated probes (default all: "
                        f"{', '.join(PROBES)})")
    p.add_argument("--json", type=Path, default=None,
                   help="write the raw report here")
    p.add_argument("--list", action="store_true",
                   help="list the probes and the blocked collections")
    args = p.parse_args(argv)
    if args.list:
        print("probes: " + ", ".join(sorted(PROBES)))
        for b in BLOCKED:
            print(f"blocked: {b['source']}: {b['observed']}")
        return 0
    sources = [s.strip() for s in args.source.split(",") if s.strip()]
    report = measure(args.sample, sources)
    text = markdown_table(report)
    print(text)
    if args.json:
        args.json.write_text(json.dumps(report, indent=2, ensure_ascii=False)
                             + "\n", encoding="utf-8")
        print(f"\nwrote {args.json}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
