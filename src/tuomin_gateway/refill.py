from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
import math
from typing import Any

from tuomin_gateway.placeholders import (
    PLACEHOLDER_RE,
    find_altered_placeholders,
    substitute,
)
from tuomin_gateway.schemas import MappingEntry, RefillResult


STRUCTURED_REFILL_MAX_DEPTH = 32
STRUCTURED_REFILL_MAX_NODES = 10_000


class StructuredRefillInvalid(ValueError):
    """The value is not JSON-safe."""


class StructuredRefillTooLarge(ValueError):
    """The value exceeds a fixed structured-refill resource limit."""


@dataclass(frozen=True)
class StructuredRefillResult:
    status: str
    value: Any | None
    restored_count: int = 0
    error_types: list[str] = field(default_factory=list)
    unknown_placeholders: list[str] = field(default_factory=list)
    missing_placeholders: list[str] = field(default_factory=list)
    altered_placeholders: list[str] = field(default_factory=list)

    def to_safe_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "value": self.value if self.status == "ok" else None,
            "error_types": self.error_types,
            "unknown_placeholders": self.unknown_placeholders,
            "missing_placeholders": self.missing_placeholders,
            "altered_placeholders": self.altered_placeholders,
        }


def refill_text(text: str, mapping: list[MappingEntry]) -> RefillResult:
    """Legacy exact-set refill contract."""
    return refill_text_contract(text, mapping, contract="exact_transform")


def refill_text_contract(
    text: str,
    mapping: list[MappingEntry],
    *,
    contract: str,
    expected_counts: dict[str, int] | None = None,
) -> RefillResult:
    known = {entry.placeholder: entry.original_value for entry in mapping}
    found = set(PLACEHOLDER_RE.findall(text))
    unknown = sorted(found - set(known))
    required_placeholders = (
        set(expected_counts) if expected_counts is not None else set(known)
    )
    missing = (
        sorted(required_placeholders - found)
        if contract == "exact_transform"
        else []
    )
    altered = find_altered_placeholders(text)

    error_types: list[str] = []
    if unknown:
        error_types.append("unknown_placeholder")
    if missing:
        error_types.append("missing_placeholder")
    if altered:
        error_types.append("altered_placeholder")
    if contract == "none":
        error_types.append("refill_not_allowed")
    if contract == "exact_transform" and expected_counts is not None:
        if any(text.count(ph) != count for ph, count in expected_counts.items()):
            error_types.append("placeholder_count_mismatch")

    if error_types:
        return RefillResult(
            status="blocked",
            text=text,
            error_types=error_types,
            unknown_placeholders=unknown,
            missing_placeholders=missing,
            altered_placeholders=altered,
        )

    restored, _ = substitute(text, known)
    return RefillResult(status="ok", text=restored)


def refill_structured_contract(
    value: Any,
    mapping: list[MappingEntry],
    *,
    contract: str,
    expected_counts: dict[str, int] | None = None,
    max_string_chars: int,
    max_depth: int = STRUCTURED_REFILL_MAX_DEPTH,
    max_nodes: int = STRUCTURED_REFILL_MAX_NODES,
) -> StructuredRefillResult:
    """Validate a complete JSON value, then restore every string leaf.

    Validation and placeholder-integrity checks finish before a restored copy
    is built, so blocked responses never contain a partially refilled value.
    Object keys are preserved and deliberately not scanned.
    """
    strings: list[str] = []
    node_count = 0
    string_characters = 0

    def inspect(node: Any, depth: int) -> None:
        nonlocal node_count, string_characters
        if depth > max_depth:
            raise StructuredRefillTooLarge("structured value exceeds depth limit")
        node_count += 1
        if node_count > max_nodes:
            raise StructuredRefillTooLarge("structured value exceeds node limit")
        if isinstance(node, str):
            string_characters += len(node)
            if string_characters > max_string_chars:
                raise StructuredRefillTooLarge(
                    "structured value exceeds string character limit"
                )
            strings.append(node)
            return
        if node is None or isinstance(node, (bool, int)):
            return
        if isinstance(node, float):
            if not math.isfinite(node):
                raise StructuredRefillInvalid("number must be finite")
            return
        if isinstance(node, list):
            for item in node:
                inspect(item, depth + 1)
            return
        if isinstance(node, dict):
            if any(not isinstance(key, str) for key in node):
                raise StructuredRefillInvalid("object keys must be strings")
            for item in node.values():
                inspect(item, depth + 1)
            return
        raise StructuredRefillInvalid("value must be JSON-safe")

    inspect(value, 1)

    known = {entry.placeholder: entry.original_value for entry in mapping}
    found_counts: Counter[str] = Counter()
    altered: set[str] = set()
    for text in strings:
        found_counts.update(PLACEHOLDER_RE.findall(text))
        altered.update(find_altered_placeholders(text))

    unknown = sorted(set(found_counts) - set(known))
    expected = Counter(expected_counts or {})
    missing = (
        sorted(placeholder for placeholder in expected if not found_counts[placeholder])
        if contract == "exact_transform"
        else []
    )
    error_types: list[str] = []
    if unknown:
        error_types.append("unknown_placeholder")
    if missing:
        error_types.append("missing_placeholder")
    if altered:
        error_types.append("altered_placeholder")
    if contract == "exact_transform" and found_counts != expected:
        error_types.append("placeholder_count_mismatch")
    if contract == "none":
        error_types.append("refill_not_allowed")
    if error_types:
        return StructuredRefillResult(
            status="blocked",
            value=None,
            error_types=error_types,
            unknown_placeholders=unknown,
            missing_placeholders=missing,
            altered_placeholders=sorted(altered),
        )

    restored_count = 0

    def restore(node: Any) -> Any:
        nonlocal restored_count
        if isinstance(node, str):
            restored, count = substitute(node, known)
            restored_count += count
            return restored
        if isinstance(node, list):
            return [restore(item) for item in node]
        if isinstance(node, dict):
            return {key: restore(item) for key, item in node.items()}
        return node

    return StructuredRefillResult(
        status="ok",
        value=restore(value),
        restored_count=restored_count,
    )
