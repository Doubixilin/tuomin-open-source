"""Canonical placeholder grammar + safe substitution.

One definition of what a placeholder looks like, shared by the redactor, the
stateless refill, and the session redactor — they used to each carry their own
copy and disagreed (e.g. a 3-digit-only regex silently failed past the 999th
entity of a label). Counters are zero-padded to at least 3 digits but the regex
accepts more, so the mapping stays reversible without bound.

``substitute`` does the refill in a SINGLE pass over the text via one combined
alternation, which is both O(n) (not O(n*entries)) and safe: an original value
that happens to contain the literal text of another placeholder is never
re-substituted, because each character is consumed exactly once.
"""
from __future__ import annotations

import re

# A well-formed placeholder: <LABEL_001>, <DEPT_1000>, ...
PLACEHOLDER_RE = re.compile(r"<[A-Z][A-Z0-9_]*_\d{3,}>")
# A mangled angle-bracketed form the model may emit (wrong separator / digits).
ALTERED_ANGLE_RE = re.compile(r"<[A-Z][A-Z0-9_]*[-_]\d{1,}>")
# A bare (unbracketed) placeholder-looking token, e.g. the model dropped the <>.
# Deliberately NOT broadened: it already fires on prose tokens like V2_2024 /
# ISO_9001, and widening it would multiply those false blocks.
BARE_PLACEHOLDER_RE = re.compile(r"(?<!<)\b[A-Z][A-Z0-9_]*_\d{3,}\b(?!>)")
# Lenient last-resort for angle-bracketed mangling the strict patterns miss:
# lowercase (<org_001>), inner whitespace (< ORG_001> / <ORG_ 001> / a line
# break), or a missing separator (<ORG001>). Fail-closed by design — it also
# catches look-alikes such as <h1>, which block the refill rather than leak.
ALTERED_LOOSE_RE = re.compile(r"<\s*[A-Za-z][A-Za-z0-9_]*\s*[-_]?\s*\d{1,}\s*>")


def reserved_placeholder_conflict(text: str) -> bool:
    """Raw inputs may not impersonate Tuomin's reserved mapping grammar.

    A literal ``<ORG_001>`` in user-supplied text is indistinguishable from a
    real placeholder downstream: it would pollute ``expected_counts``, get
    refilled to someone else's original value, or be rewritten by unmask.
    Callers fail closed (409 / error) before any masking or persistence.
    """
    return PLACEHOLDER_RE.search(text) is not None


def find_altered_placeholders(text: str) -> list[str]:
    """Return placeholder-looking tokens that are NOT well-formed.

    A mangled placeholder can never be refilled safely, so callers fail closed
    on any finding. Well-formed placeholders (``PLACEHOLDER_RE`` matches) are
    excluded even though the lenient patterns also match them.
    """
    exact = set(PLACEHOLDER_RE.findall(text))
    malformed = set()
    for pattern in (ALTERED_ANGLE_RE, ALTERED_LOOSE_RE):
        for match in pattern.findall(text):
            if match not in exact:
                malformed.add(match)
    malformed.update(BARE_PLACEHOLDER_RE.findall(text))
    return sorted(malformed)


def substitute(text: str, mapping: dict[str, str]) -> tuple[str, int]:
    """Replace every known placeholder in ``text`` with its original, one pass.

    Returns ``(restored_text, occurrences_replaced)``. Placeholders are matched
    longest-first so no placeholder can be a prefix of another; unknown
    placeholders are left untouched for the caller to report.
    """
    if not mapping:
        return text, 0
    pattern = re.compile("|".join(re.escape(p) for p in sorted(mapping, key=len, reverse=True)))
    count = 0

    def _replace(match: re.Match[str]) -> str:
        nonlocal count
        count += 1
        return mapping[match.group(0)]

    return pattern.sub(_replace, text), count
