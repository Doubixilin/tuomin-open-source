from __future__ import annotations

from collections import Counter
from uuid import uuid4

from tuomin_gateway.detectors.base import hash_text
from tuomin_gateway.fusion import fuse_detections
from tuomin_gateway.generalizers import generalize_value
from tuomin_gateway.licensing.core import enforce_new_task
from tuomin_gateway.mapping import PlaceholderFactory
from tuomin_gateway.profiles import GENERALIZE
from tuomin_gateway.schemas import DetectionSpan, MappingEntry, RedactionResult


def redact_text(text: str, detections: list[DetectionSpan], task_id: str | None = None) -> RedactionResult:
    # 授权后手（编译进原生代码）：HTTP 中间件是第一道；即使中间件被 patch，
    # 新建脱敏任务仍会在原生层被拦下。恢复路径（refill/unlock）不经过本函数。
    enforce_new_task()
    task_id = task_id or f"task_{uuid4().hex[:12]}"
    normalized_detections = normalize_detections(text, detections)
    factory = PlaceholderFactory()
    key_to_placeholder: dict[tuple[str, str], str] = {}
    placeholder_to_entry: dict[str, MappingEntry] = {}

    replacements: list[tuple[int, int, str]] = []
    generalized_count = 0
    for span in normalized_detections:
        original = text[span.start : span.end]
        # Generalization (L3): coarsen the value irreversibly and substitute it
        # in place. It is NOT added to the reversible mapping (refill never
        # restores it). Fail-closed: if the generalizer can't coarsen the value,
        # fall through to a reversible placeholder rather than leak the original.
        if span.metadata.get("action") == GENERALIZE:
            coarse = generalize_value(span.metadata.get("generalizer"), original)
            if coarse is not None:
                replacements.append((span.start, span.end, coarse))
                generalized_count += 1
                continue
        # Document mappings prioritize byte-for-byte surface restoration. A
        # persistent namespace deliberately uses canonical identity instead
        # (SessionRedactor); keeping those contracts separate avoids silently
        # expanding an alias into its canonical full name on document refill.
        mapping_value = original
        # Prefix is the transaction role (PARTYA…) when role-aware, else the
        # entity label (角色化占位). The MappingEntry keeps the entity label.
        prefix = span.metadata.get("role") or span.label
        key = (prefix, mapping_value)
        placeholder = key_to_placeholder.get(key)
        if placeholder is None:
            placeholder = factory.next(prefix)
            key_to_placeholder[key] = placeholder
            placeholder_to_entry[placeholder] = MappingEntry(
                placeholder=placeholder,
                label=span.label,
                original_value=mapping_value,
                text_hash=hash_text(mapping_value),
            )
        replacements.append((span.start, span.end, placeholder))

    redacted = text
    for start, end, replacement in sorted(replacements, key=lambda item: item[0], reverse=True):
        redacted = redacted[:start] + replacement + redacted[end:]

    mapping = list(placeholder_to_entry.values())
    return RedactionResult(
        task_id=task_id,
        redacted_text=redacted,
        mapping_id=f"{task_id}.mapping.json",
        detections=normalized_detections,
        mapping=mapping,
        risk_summary=_risk_summary(normalized_detections, mapping, generalized_count),
    )


def normalize_detections(text: str, detections: list[DetectionSpan]) -> list[DetectionSpan]:
    """Validate bounds + drop exact duplicates, then reconcile any overlaps.

    Overlap resolution is delegated to ``fuse_detections`` (the single resolver)
    so the redactor and the fusion stage can never disagree on a contested span.
    Callers normally fuse first; re-fusing here is idempotent on non-overlapping
    input and a safety net if a caller forgot.
    """
    # Enforce the PEM floor even for callers supplying their own detections.
    from tuomin_gateway.detectors.rules import private_key_detections

    detections = [*detections, *private_key_detections(text)]
    seen: set[tuple[int, int, str, str]] = set()
    deduped: list[DetectionSpan] = []
    for span in detections:
        if span.start < 0 or span.end > len(text) or span.start >= span.end:
            raise ValueError("detection span is outside text bounds")
        key = (span.start, span.end, span.label, span.text_hash)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(span)
    return fuse_detections(deduped, text)


def _risk_summary(
    detections: list[DetectionSpan], mapping: list[MappingEntry], generalized_count: int = 0
) -> dict:
    label_counts = Counter(span.label for span in detections)
    source_counts = Counter(span.source for span in detections)
    return {
        "detection_count": len(detections),
        "label_counts": dict(sorted(label_counts.items())),
        "source_counts": dict(sorted(source_counts.items())),
        "placeholder_count": len(mapping),
        "generalized_count": generalized_count,
    }
