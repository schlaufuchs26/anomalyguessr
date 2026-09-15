#!/usr/bin/env python3
"""AnomalyGuessr anomaly-pattern catalogue (ticket #1539).

Every lesson from Evan's verdicts used to live in a chat message, so the
generator re-learned nothing: the proposal prompt had examples but no
statement of *what works and what fails*, and the gates had their checks
spread across the code. This module owns the one readable catalogue,
``pipeline/ag_patterns.json``, and the two readers:

- ``prompt_guidance`` turns the *active* patterns into the short
  "this works / this fails" lines the proposal prompt carries (capped, like
  the recent-label avoid line);
- ``carrier_requirements`` and ``matches_rule`` expose the machine-checkable
  subset (the carrier table, the small-element nouns) to the deterministic
  gates in ``ag_generate.py``.

The catalogue is evidence, not a rulebook: a pattern enters as ``observe``,
``ag_feedback.py`` refreshes the counts of every pattern that carries
``matches`` and *proposes* a status change, and a pattern is retired by
setting its status with a date rather than by deleting the entry.

Pattern membership is derived from the label with each pattern's ``matches``
keywords, never from the model's own family bucket (the catalogue's
``label_map_note``): "other" holds 66 decided scenes and the plastic/drinks
buckets overlap.
"""

import json
from pathlib import Path

PATTERNS_NAME = "ag_patterns.json"
PATTERNS_PATH = Path(__file__).resolve().parent / PATTERNS_NAME

STATUS_ACTIVE = "active"
STATUS_OBSERVE = "observe"
STATUS_RETIRED = "retired"

# The proposal prompt carries at most this many guidance lines and this many
# characters, so a growing catalogue cannot crowd out the prompt's own rules
# (the same "capped, like the avoid line" budget the ticket asks for).
PROMPT_LINE_LIMIT = 4
PROMPT_CHAR_CAP = 800


def load_patterns(path=None) -> list:
    """The catalogue's pattern list, [] when the file is missing or broken."""
    try:
        data = json.loads(Path(path or PATTERNS_PATH).read_text())
    except (OSError, ValueError):
        return []
    patterns = data.get("patterns") if isinstance(data, dict) else None
    if not isinstance(patterns, list):
        return []
    return [p for p in patterns if isinstance(p, dict) and p.get("id")]


def active_patterns(patterns) -> list:
    """The patterns the proposal prompt may read, in catalogue order."""
    return [p for p in patterns or () if p.get("status") == STATUS_ACTIVE]


def pattern_by_id(patterns, pattern_id):
    for pattern in patterns or ():
        if pattern.get("id") == pattern_id:
            return pattern
    return None


def prompt_guidance(patterns, limit: int = PROMPT_LINE_LIMIT,
                    char_cap: int = PROMPT_CHAR_CAP) -> list:
    """The short "this works / this fails" lines for the proposal prompt.

    Only active patterns reach the prompt; the list is capped by line count
    and total characters so the catalogue cannot grow the prompt without
    bound. A pattern without a ``prompt`` line is skipped.
    """
    lines = []
    used = 0
    for pattern in active_patterns(patterns):
        text = str(pattern.get("prompt") or "").strip()
        if not text or len(lines) >= max(0, int(limit)):
            continue
        if used + len(text) > int(char_cap):
            continue
        lines.append(text)
        used += len(text)
    return lines


def carrier_requirements(patterns) -> tuple:
    """(element phrase, carrier words) rows from the catalogue's rules.

    Every active pattern whose rule is a ``carrier`` rule contributes one row
    per element alternative, in the shape ``ag_generate.required_carrier``
    reads: the element phrase's words must all appear in the label.
    """
    rows = []
    for pattern in patterns or ():
        if pattern.get("status") == STATUS_RETIRED:
            continue
        rule = pattern.get("rule") or {}
        if rule.get("type") != "carrier":
            continue
        for req in rule.get("requirements") or ():
            carriers = tuple(str(w) for w in (req.get("carrier_any") or ()))
            if not carriers:
                continue
            for element in req.get("element_any") or ():
                element = str(element).strip()
                if element:
                    rows.append((element, carriers))
    return tuple(rows)


def rule_keywords(patterns, rule_type: str) -> tuple:
    """The keyword list a rule type declares (e.g. the small-element nouns)."""
    words = []
    for pattern in patterns or ():
        if pattern.get("status") == STATUS_RETIRED:
            continue
        rule = pattern.get("rule") or {}
        if rule.get("type") != rule_type:
            continue
        for word in rule.get("keywords") or ():
            word = str(word).strip().lower()
            if word and word not in words:
                words.append(word)
    return tuple(words)


def matches_pattern(pattern, label: str) -> bool:
    """Whether ``label`` belongs to the pattern, read from its own keywords.

    The match is a lowercase substring of the label, so "Paper coffee cup
    with lid" lands in ``small-handheld`` via "coffee cup". Empty ``matches``
    means the pattern is judged by hand and never counted from verdicts.
    """
    text = str(label or "").lower()
    if not text:
        return False
    return any(str(k).lower() in text for k in pattern.get("matches") or ()
               if str(k).strip())


def patterns_for_label(label, patterns) -> list:
    """The ids of every pattern the label belongs to, catalogue order."""
    return [str(p["id"]) for p in patterns or ()
            if matches_pattern(p, label)]


def matches_rule(patterns, rule_type: str, label: str) -> bool:
    """Whether a label falls under a machine-checkable rule type."""
    return matches_pattern(
        {"matches": rule_keywords(patterns, rule_type)}, label)
