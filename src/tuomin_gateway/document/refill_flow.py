"""Refill flows for workbench Markdown files and AI answers.

Wraps the fail-closed core in ``refill.py`` (which is NOT modified) with the
file-workbench policy (开发计划 §4.3) and the AI-answer policy (§9.2):

- **exact mode** — the submitted Markdown is byte-identical to what the job
  produced (SHA-256 match): full ``exact_transform`` validation including
  per-placeholder occurrence counts.
- **edited mode** — any byte difference: the caller must show the user a
  structural placeholder diff (unknown / altered / missing / duplicated) and
  obtain explicit confirmation before ``trusted_display`` refill. Unknown or
  altered (tampered) placeholders always block, confirmation or not.
- **answer mode** — an online-AI answer referencing a subset of the mapping:
  subset use and repeated known placeholders are normal; unknown/altered
  block; zero hits require explicit confirmation.

Nothing here overwrites the source file; callers write the result atomically.
"""
from __future__ import annotations

import hashlib
from collections import Counter
from dataclasses import dataclass, field

from tuomin_gateway.placeholders import PLACEHOLDER_RE, find_altered_placeholders
from tuomin_gateway.refill import refill_text_contract
from tuomin_gateway.schemas import MappingEntry

MODE_EXACT = "exact"
MODE_EDITED = "edited"
MODE_ANSWER = "answer"

STATUS_OK = "ok"
STATUS_BLOCKED = "blocked"
STATUS_NEEDS_CONFIRMATION = "needs_confirmation"


@dataclass(frozen=True)
class PlaceholderDiff:
    """Structural differences between a submitted text and the job's package."""

    unknown: list[str] = field(default_factory=list)     # in text, not in package
    altered: list[str] = field(default_factory=list)     # damaged placeholder-shaped spans
    missing: list[str] = field(default_factory=list)     # in package, absent from text
    duplicated: list[str] = field(default_factory=list)  # occurrence count above expected

    @property
    def clean(self) -> bool:
        return not (self.unknown or self.altered or self.missing or self.duplicated)

    def to_safe_dict(self) -> dict[str, list[str]]:
        return {
            "unknown": self.unknown,
            "altered": self.altered,
            "missing": self.missing,
            "duplicated": self.duplicated,
        }


@dataclass(frozen=True)
class RefillFlowResult:
    status: str  # ok | blocked | needs_confirmation
    mode: str
    text: str | None
    error_types: list[str] = field(default_factory=list)
    diff: PlaceholderDiff = field(default_factory=PlaceholderDiff)


def redacted_sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def classify_mode(text: str, *, recorded_redacted_sha256: str | None) -> str:
    """Byte-level classification: ANY difference lands in edited mode."""
    if recorded_redacted_sha256 and redacted_sha256(text) == recorded_redacted_sha256:
        return MODE_EXACT
    return MODE_EDITED


def placeholder_diff(
    text: str,
    entries: list[MappingEntry],
    expected_counts: dict[str, int],
) -> PlaceholderDiff:
    known = {entry.placeholder for entry in entries}
    found = Counter(PLACEHOLDER_RE.findall(text))
    return PlaceholderDiff(
        unknown=sorted(set(found) - known),
        altered=sorted(set(find_altered_placeholders(text))),
        missing=sorted(p for p in expected_counts if not found[p]),
        duplicated=sorted(p for p, n in expected_counts.items() if found[p] > n),
    )


def refill_markdown(
    text: str,
    entries: list[MappingEntry],
    expected_counts: dict[str, int],
    *,
    recorded_redacted_sha256: str | None,
    confirm_edited: bool = False,
) -> RefillFlowResult:
    """Refill workbench Markdown under the dual-mode policy.

    Edited mode returns ``needs_confirmation`` (with the diff) until the caller
    passes ``confirm_edited=True``; unknown/altered placeholders block in every
    mode. On ``ok`` the restored text is returned; on any other status ``text``
    is None — no partial results ever escape.
    """
    mode = classify_mode(text, recorded_redacted_sha256=recorded_redacted_sha256)
    if mode == MODE_EXACT:
        result = refill_text_contract(
            text, entries, contract="exact_transform", expected_counts=expected_counts
        )
        if result.status != "ok":
            return RefillFlowResult(
                status=STATUS_BLOCKED,
                mode=mode,
                text=None,
                error_types=result.error_types,
                diff=placeholder_diff(text, entries, expected_counts),
            )
        return RefillFlowResult(status=STATUS_OK, mode=mode, text=result.text)

    diff = placeholder_diff(text, entries, expected_counts)
    if diff.unknown or diff.altered:
        return RefillFlowResult(
            status=STATUS_BLOCKED,
            mode=mode,
            text=None,
            error_types=(
                (["unknown_placeholder"] if diff.unknown else [])
                + (["altered_placeholder"] if diff.altered else [])
            ),
            diff=diff,
        )
    if not diff.clean and not confirm_edited:
        return RefillFlowResult(
            status=STATUS_NEEDS_CONFIRMATION, mode=mode, text=None, diff=diff
        )
    result = refill_text_contract(text, entries, contract="trusted_display")
    if result.status != "ok":  # defensive: trusted_display only blocks unknown/altered
        return RefillFlowResult(
            status=STATUS_BLOCKED,
            mode=mode,
            text=None,
            error_types=result.error_types,
            diff=diff,
        )
    return RefillFlowResult(status=STATUS_OK, mode=mode, text=result.text, diff=diff)


def refill_answer(
    text: str,
    entries: list[MappingEntry],
    *,
    confirm_empty: bool = False,
) -> RefillFlowResult:
    """Restore an AI answer under the answer-mode policy (开发计划 §9.2).

    Unlike document refill, the answer legitimately references only a SUBSET of
    the mapping and may repeat a known placeholder: ``missing``/``duplicated``
    never block here. Unknown or altered (tampered) placeholders always block.
    Zero known-placeholder hits return ``needs_confirmation`` until the caller
    passes ``confirm_empty=True``, so an unanswered restore can never masquerade
    as success. On ``ok`` the restored text is returned; on any other status
    ``text`` is None — no partial results ever escape.
    """
    # expected_counts all 1: subset use and repeats are normal in answer mode,
    # so only unknown/altered carry meaning in the diff.
    diff = placeholder_diff(
        text, entries, {entry.placeholder: 1 for entry in entries}
    )
    if diff.unknown or diff.altered:
        return RefillFlowResult(
            status=STATUS_BLOCKED,
            mode=MODE_ANSWER,
            text=None,
            error_types=(
                (["unknown_placeholder"] if diff.unknown else [])
                + (["altered_placeholder"] if diff.altered else [])
            ),
            diff=diff,
        )
    matched = sum(text.count(entry.placeholder) for entry in entries)
    if matched == 0 and not confirm_empty:
        return RefillFlowResult(
            status=STATUS_NEEDS_CONFIRMATION, mode=MODE_ANSWER, text=None, diff=diff
        )
    result = refill_text_contract(text, entries, contract="trusted_display")
    if result.status != "ok":  # defensive: trusted_display only blocks unknown/altered
        return RefillFlowResult(
            status=STATUS_BLOCKED,
            mode=MODE_ANSWER,
            text=None,
            error_types=result.error_types,
            diff=diff,
        )
    return RefillFlowResult(status=STATUS_OK, mode=MODE_ANSWER, text=result.text, diff=diff)
