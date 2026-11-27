from __future__ import annotations

from bisect import bisect_left, bisect_right

from tuomin_gateway.schemas import DetectionSpan
from tuomin_gateway.spans import overlaps, resolve_overlaps


SOURCE_PRIORITY = {
    "manual": 100,
    "declared_value": 95,
    "session_mapping": 90,
    "dictionary": 80,
    "rule": 70,
    "public_context_rule": 70,
    "openai_privacy_filter": 55,
    "model": 50,
}

_GENERIC_CLIPPED_ORG_FRAGMENTS = {
    "公司",
    "有限公司",
    "有限责任公司",
    "股份公司",
    "股份有限公司",
    "份公司",
    "展有限公司",
    "开发有限公司",
    "总公司",
    "集团",
}


def fuse_detections(spans: list[DetectionSpan], text: str) -> list[DetectionSpan]:
    """Reconcile detections from every detector into non-overlapping spans.

    Spans are ranked highest-priority-first; on overlap the winner keeps the
    contested characters and the loser is clipped to the remainder. The sole
    exception is a contained alternative from the same NER source: same-label
    variants follow normal ranking, while a materially shorter cross-label
    variant yields to the longer surface. Clipping either would manufacture a
    fragment.
    ``text`` is required so a clipped span's hash can be recomputed.
    """
    ranked = sorted(spans, key=_selection_key)
    resolved = resolve_overlaps(_drop_contained_model_alternatives(ranked), text)
    return [
        span for span in resolved if not _is_generic_clipped_fragment(span, text)
    ]


def _is_generic_clipped_fragment(span: DetectionSpan, text: str) -> bool:
    if span.metadata.get("fusion_clipped") is not True:
        return False
    compact = "".join(text[span.start : span.end].split())
    if span.label in {"ORG", "ADDRESS"} and len(compact) == 1:
        return True
    return span.label == "ORG" and compact in _GENERIC_CLIPPED_ORG_FRAGMENTS


def _drop_contained_model_alternatives(
    ranked: list[DetectionSpan],
) -> list[DetectionSpan]:
    """Drop lower-ranked model alternatives that describe the same entity.

    Overlapping-window NER can return ``上海市`` and ``上海市建`` (or label the
    ``苏州`` prefix inside one longer company as ADDRESS). Clipping a competing
    boundary would manufacture a fragment. Same-label alternatives retain the
    ranked winner; a materially shorter cross-label alternative yields to its
    containing surface. Partial overlaps and every cross-source overlap still
    use the fail-closed clipping rule.

    Only model spans take part, and a containing span must start within one
    maximum model-span length of the contained one, so both passes look at a
    small window of neighbours instead of every pair (the all-pairs version
    took tens of seconds on large documents and could not be cancelled).
    """
    from tuomin_gateway.cancellation import check as check_cancelled

    models = sorted((span for span in ranked if span.source == "model"), key=lambda item: (item.start, item.end))
    model_starts = [span.start for span in models]
    longest = max((span.length() for span in models), default=0)
    cross_label_contained: set[int] = set()
    for position, shorter in enumerate(models):
        if position % 1024 == 0:
            check_cancelled()
        low = bisect_left(model_starts, shorter.start - longest)
        high = bisect_right(model_starts, shorter.start)
        for longer in models[low:high]:
            if (
                longer is not shorter
                and shorter.label != longer.label
                and longer.start <= shorter.start
                and longer.end >= shorter.end
                and longer.length() >= shorter.length() + 2
            ):
                cross_label_contained.add(id(shorter))
                break

    kept: list[DetectionSpan] = []
    kept_starts: list[int] = []
    kept_models: list[DetectionSpan] = []
    for position, span in enumerate(ranked):
        if position % 1024 == 0:
            check_cancelled()
        if id(span) in cross_label_contained:
            continue
        redundant = False
        if span.source == "model":
            # A kept model span overlapping `span` starts before span.end and
            # no earlier than span.start - longest.
            low = bisect_left(kept_starts, span.start - longest)
            high = bisect_left(kept_starts, span.end)
            redundant = any(
                span.label == existing.label
                and overlaps(span, existing)
                and (
                    (span.start <= existing.start and span.end >= existing.end)
                    or (existing.start <= span.start and existing.end >= span.end)
                )
                for existing in kept_models[low:high]
            )
        if not redundant:
            kept.append(span)
            if span.source == "model":
                index = bisect_right(kept_starts, span.start)
                kept_starts.insert(index, span.start)
                kept_models.insert(index, span)
    return kept


def _selection_key(span: DetectionSpan) -> tuple[int, float, int, int, int, str, str]:
    priority, confidence, length = _rank(span)
    return (-priority, -confidence, -length, span.start, span.end, span.label, span.source)


def _rank(span: DetectionSpan) -> tuple[int, float, int]:
    # A whole private key must never be fragmented by a dictionary/pass span.
    if span.metadata.get("whole_private_key") and span.label == "CREDENTIAL":
        return (110, span.confidence, span.length())
    priority = SOURCE_PRIORITY.get(span.source, 40)
    if span.source == "rule" and span.confidence >= 0.95:
        priority += 5
    return (priority, span.confidence, span.length())


# Re-exported for callers that still import it from fusion.
_overlaps = overlaps
