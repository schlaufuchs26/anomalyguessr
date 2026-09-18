#!/usr/bin/env python3
"""Reference-claim support check for AnomalyGuessr (ticket #1624).

Every factual claim in a scene's ``explanation`` is supposed to carry a
reference that *states* that claim. In practice many references point at a
generic topic page (``.../Sneakers``) that never names the year or era the
sentence rests on, so a reader cannot check the reasoning. Evan's complaint
(2026-09-18) is exactly that: the links under an explanation usually do not
provide an adequate reference.

This module is the deterministic half of the fix:

- :func:`time_tokens` pulls the dated claims out of one explanation sentence
  (years, decades, "mid-20th-century" and spelled-out centuries).
- :func:`topic_tokens` pulls the reference label's subject words.
- :func:`check_reference` fetches the URL and decides one per-reference
  status: ``ok``, ``dead``, ``empty`` or ``wrong_topic`` (the page is not
  about the label's subject), plus the ``stated`` dated tokens the page
  actually carries. The page text is a plain-text rendering of the fetched
  HTML.
- :func:`reference_finding` wraps a whole scene's reference list into the
  finding shape the scene report carries, so moderation sees a failing
  reference instead of a silent pass. It requires *collective coverage*: a
  dated token in the explanation is covered when some live, on-topic
  reference states it, so one reference for the undated half of a sentence
  does not have to repeat the year. It runs at generation time, after the
  entry's references are final; the same function backs the catalog audit
  (``pipeline/ag_reference_audit.py``).

The fetch is injectable (``fetch`` parameter), so the tests run on fixture
pages with no network. Only http(s) URLs are fetched; a request looks like a
normal reader (descriptive User-Agent).

What the check deliberately does NOT do: judge whether a page *contradicts*
the claim (that needs reading). A page that mentions neither the subject nor
the dated token fails; one that mentions both passes.
"""

import html as _html
import re
import urllib.error
import urllib.request

# A descriptive agent, not a browser impersonation: Wikimedia asks for this.
USER_AGENT = ("AnomalyGuessr-reference-check/1.0 "
              "(+https://anomalyguessr.com; contact: evan@fuchs.science)")
FETCH_TIMEOUT = 20


def _norm(text: str) -> str:
    """Lowercase, collapse every non-alphanumeric run to one space.

    ``"mid-20th-century"`` and ``"mid 20th century"`` normalize to the same
    string, so hyphen/spelling variants need no special case in the search.
    """
    return " ".join(re.sub(r"[^a-z0-9]+", " ", str(text).lower()).split())


def _contains(haystack_norm: str, needle_norm: str) -> bool:
    """Whole-token substring test on normalized text (no partial numbers)."""
    if not needle_norm:
        return False
    return f" {needle_norm} " in f" {haystack_norm} "


# Dated claims, from most specific to least. Order matters only for reading.
_YEAR_RE = re.compile(r"\b(1[5-9]\d{2}|20\d{2})\b")
_DECADE_RE = re.compile(r"\b(1[5-9]\d0)s\b")
# "20th century", "mid-20th-century", "early 19th century", "4th century".
_CENTURY_RE = re.compile(
    r"\b(?:(mid|early|late)[\s-]*)?(\d{1,2})(?:st|nd|rd|th)[\s-]*century\b",
    re.I)
# Spelled-out centuries, one per ordinal word we expect to see.
_ORDINAL_WORDS = {
    "fifteenth": 15, "sixteenth": 16, "seventeenth": 17, "eighteenth": 18,
    "nineteenth": 19, "twentieth": 20, "twenty first": 21,
}
_ORDINAL_WORD_RE = re.compile(
    r"\b(?:(mid|early|late)[\s-]*)?(" + "|".join(_ORDINAL_WORDS)
    + r")[\s-]*century\b", re.I)


def _century_token(qualifier: str, number: int) -> str:
    qualifier = (qualifier or "").lower()
    return f"{qualifier} {number}th century".strip()


def time_tokens(explanation: str) -> list:
    """The dated tokens an explanation asserts, canonicalized.

    Returns tokens like ``"1970s"``, ``"1967"``, ``"20th century"`` or
    ``"mid 20th century"``, in the order first seen and without duplicates.
    An explanation with no dated claim (a fictional-future robot) returns an
    empty list, and then the support check has no dated token to require.
    """
    text = str(explanation or "")
    found = []

    def add(token):
        if token not in found:
            found.append(token)

    for m in _YEAR_RE.finditer(text):
        add(m.group(1))
    for m in _DECADE_RE.finditer(text):
        add(f"{m.group(1)}s")
    for m in _CENTURY_RE.finditer(text):
        add(_century_token(m.group(1), int(m.group(2))))
    for m in _ORDINAL_WORD_RE.finditer(text):
        add(_century_token(m.group(1), _ORDINAL_WORDS[_norm(m.group(2))]))
    return found


def _time_variants(token: str) -> list:
    """Search strings that all mean the same dated token."""
    norm = _norm(token)
    variants = {norm}
    m = re.fullmatch(r"(mid|early|late) (\d{1,2})th century", norm)
    if m:
        words = {19: "nineteenth", 20: "twentieth", 21: "twenty first"}
        spelled = words.get(int(m.group(2)))
        if spelled:
            variants.add(f"{m.group(1)} {spelled} century")
    return sorted(variants)


# Words too generic to prove a page is about the reference's subject.
_STOPWORDS = {
    "the", "and", "for", "with", "from", "page", "article", "source", "site",
    "history", "about", "into", "that", "this", "not", "are", "was", "were",
    "its", "his", "her", "they", "have", "has", "had", "old", "new", "see",
    "also", "part", "list", "commons", "wikipedia", "file", "html", "https",
    "http", "www", "date", "year", "made", "used", "use",
}


def topic_tokens(label: str) -> list:
    """The reference label's subject words, longest first.

    ``"Coffee cup (lid patents from 1967)"`` yields ``["patents", "coffee",
    "cup", "lid"]``. A page must contain at least one of them to count as
    being about the reference's topic.
    """
    words = [w for w in re.split(r"[^a-z0-9]+", str(label or "").lower())
             if len(w) >= 4 and w not in _STOPWORDS and not w.isdigit()]
    seen = []
    for w in sorted(words, key=len, reverse=True):
        if w not in seen:
            seen.append(w)
    return seen


def fetch_page(url: str, timeout: int = FETCH_TIMEOUT) -> str:
    """Fetch one http(s) URL and return the HTML as text.

    Raises :class:`urllib.error.URLError`/:class:`OSError` on any network or
    HTTP failure; :func:`check_reference` turns that into the ``dead``
    status.
    """
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        charset = response.headers.get_content_charset() or "utf-8"
        return response.read().decode(charset, "replace")


def page_text(document: str) -> str:
    """Plain text of an HTML document: scripts/styles/tags stripped."""
    text = re.sub(r"(?is)<(script|style|noscript|sup)[^>]*>.*?</\1>", " ",
                  str(document))
    text = re.sub(r"(?s)<[^>]+>", " ", text)
    return _html.unescape(text)


# A URL shape that cannot be a deep link to a claim: a search page or the
# bare domain root. Kept small on purpose; the token check does the real work.
_SEARCH_URL_RE = re.compile(
    r"(?:/w/index\.php\?|special:search|[?&]q=|/search[/?#]|google\.[a-z]+/?)",
    re.I)


def deep_link(url: str) -> bool:
    """Whether the URL is at least link-shaped like a citable page.

    Rejects search URLs and bare domain roots (``https://example.org/``); a
    path-bearing page passes. This is a cheap pre-filter, not the support
    check itself.
    """
    url = str(url or "").strip()
    if not re.match(r"^https?://", url):
        return False
    if _SEARCH_URL_RE.search(url):
        return False
    rest = re.sub(r"^https?://[^/]+", "", url)
    return bool(rest.strip("/#")) or False


def check_reference(explanation: str, reference: dict, fetch=fetch_page) -> dict:
    """One reference against the claims in the explanation.

    ``fetch`` defaults to the real network fetch; tests inject a fixture
    reader with the same ``(url) -> html`` signature. Per-reference status:

    - ``ok``: the page is live and about the label's subject.
    - ``dead``: the fetch failed (timeout, HTTP error, DNS), or the URL is
      not a citable page (a search URL or a bare domain root).
    - ``empty``: the page fetched but carries no readable text.
    - ``wrong_topic``: the page does not mention any of the label's subject
      words (the link points at something else).

    ``stated`` lists the explanation's dated tokens this page carries;
    coverage across the whole reference list is decided in
    :func:`reference_finding`, because one reference may support only the
    undated half of a two-part sentence.
    """
    label = str((reference or {}).get("label") or "")
    url = str((reference or {}).get("url") or "")
    tokens = time_tokens(explanation)
    out = {"label": label[:120], "url": url, "time_tokens": tokens,
           "topic_tokens": topic_tokens(label), "stated": [], "missing": [],
           "status": "ok", "note": ""}
    if not deep_link(url):
        out.update({"status": "dead", "note": "not a citable page URL"})
        return out
    try:
        document = fetch(url)
    except (urllib.error.URLError, OSError, ValueError) as exc:
        out.update({"status": "dead", "note": f"fetch failed: {exc}"[:200]})
        return out
    text = _norm(page_text(document))
    if not text.strip():
        out.update({"status": "empty", "note": "page has no text"})
        return out
    topics = out["topic_tokens"]
    if topics and not any(_contains(text, _norm(t)) for t in topics):
        out.update({"status": "wrong_topic",
                    "note": "page does not mention the label's subject"})
        return out
    out["stated"] = [t for t in tokens
                     if any(_contains(text, _norm(v))
                            for v in _time_variants(t))]
    out["missing"] = [t for t in tokens if t not in out["stated"]]
    return out


def check_references(explanation: str, references, fetch=fetch_page) -> list:
    """Every reference of one scene, checked in order (#1624)."""
    return [check_reference(explanation, ref, fetch=fetch)
            for ref in (references or [])]


def reference_finding(explanation: str, references, fetch=fetch_page) -> dict:
    """The scene-report finding for a whole reference list (#1624).

    Checks every reference, then decides coverage: each dated token in the
    explanation must be stated by at least one live, on-topic reference.
    ``failed`` is True when any reference is dead, empty or off-topic, or when
    a dated token is left uncovered; the per-reference detail stays in
    ``references`` and the uncovered tokens in ``uncovered`` so a moderator
    sees which link and which claim is the problem. An entry without
    references reports ``checked: False`` (the queue schema refuses such an
    entry anyway).
    """
    refs = check_references(explanation, references, fetch=fetch)
    tokens = time_tokens(explanation)
    stated = set()
    for ref in refs:
        if ref["status"] == "ok":
            stated.update(ref["stated"])
    uncovered = [t for t in tokens if t not in stated]
    bad = [r for r in refs if r["status"] != "ok"]
    notes = [f"{r['label'][:40]}: {r['status']}" for r in bad]
    if uncovered:
        notes.append("uncovered: " + ", ".join(uncovered))
    return {"checked": bool(refs), "class": "reference",
            "failed": bool(bad or uncovered), "references": refs,
            "uncovered": uncovered, "note": "; ".join(notes)}
