"""Secrets detection (pillar 6) — provider-specific patterns + entropy gate.

Zero-dependency: high-precision regexes for well-known credential formats, plus
a generic ``key = <high-entropy value>`` catch gated on Shannon entropy to keep
false positives down. Used in BOTH directions: warn when a secret is present in
the agent's input, and when the model echoes/emits one in its response.

Returns ``(pattern_id, matched_value)``; the caller hashes the value into an
AlertEvent (raw secret never stored).
"""
from __future__ import annotations

import math
import re
from collections import Counter

# (id, pattern, capture_group) — group 0 unless a sub-group isolates the value.
# Quantifiers are upper-bounded (generously) rather than open-ended: an open `{n,}`
# on attacker-controlled base64-heavy gateway traffic invites quadratic backtracking
# (ReDoS). The caps are far above real token lengths, so detection is unaffected.
_PATTERNS: tuple[tuple[str, re.Pattern[str], int], ...] = (
    ("aws_access_key", re.compile(r"\bAKIA[0-9A-Z]{16}\b"), 0),
    ("github_token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,255}\b"), 0),
    ("slack_token", re.compile(r"\bxox[baprs]-[0-9A-Za-z-]{10,255}\b"), 0),
    ("stripe_key", re.compile(r"\b[sr]k_live_[0-9A-Za-z]{16,255}\b"), 0),
    ("openai_key", re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9_-]{20,255}\b"), 0),
    ("google_api_key", re.compile(r"\bAIza[0-9A-Za-z_\-]{35}\b"), 0),
    ("private_key_block", re.compile(r"-----(?:BEGIN|END) (?:RSA |EC |OPENSSH |DSA |ENCRYPTED )?PRIVATE KEY-----"), 0),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_-]{8,1024}\.[A-Za-z0-9_-]{8,1024}\.[A-Za-z0-9_-]{8,1024}\b"), 0),
)

# Generic "secret-ish assignment" — only flagged if the value looks high-entropy.
_GENERIC = re.compile(
    r"(?:api[_-]?key|secret|token|password|passwd|pwd|access[_-]?key|auth[_-]?token)"
    r"\s*[:=]\s*[\"']?([A-Za-z0-9_\-./+=]{12,4096})",
    re.I,
)
_ENTROPY_THRESHOLD = 3.0


def _shannon_entropy(value: str) -> float:
    if not value:
        return 0.0
    counts = Counter(value)
    n = len(value)
    return -sum((c / n) * math.log2(c / n) for c in counts.values())


def scan(text: str) -> list[tuple[str, str]]:
    if not text:
        return []
    findings: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for pattern_id, pattern, group in _PATTERNS:
        for match in pattern.finditer(text):
            value = match.group(group)
            key = (pattern_id, value)
            if value and key not in seen:
                seen.add(key)
                findings.append((pattern_id, value))
    for match in _GENERIC.finditer(text):
        value = match.group(1)
        key = ("generic_secret", value)
        if value and key not in seen and _shannon_entropy(value) >= _ENTROPY_THRESHOLD:
            seen.add(key)
            findings.append(("generic_secret", value))
    return findings
