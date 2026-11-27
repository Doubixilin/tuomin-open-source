"""Shared span-geometry helpers (overlap test + overlap resolution).

The single place overlapping detections are reconciled. Previously fusion and the
redactor each had their own "drop a span on any overlap" rule with *different*
tie-breaks, and both leaked: when a higher-priority span only *partially* overlaps
a lower one, dropping the loser outright left the uncovered remainder in clear
text. ``resolve_overlaps`` instead keeps the highest-priority span on every
character and CLIPS the loser to the uncovered remainder (recomputing its hash),
so no flagged character is ever left unredacted and the result is guaranteed
non-overlapping — which the placeholder replacement step requires.
"""
from __future__ import annotations

from bisect import bisect_left
from dataclasses import replace

from tuomin_gateway.schemas import DetectionSpan, hash_text


def overlaps(left: DetectionSpan, right: DetectionSpan) -> bool:
    return max(left.start, right.start) < min(left.end, right.end)


def _subtract(start: int, end: int, occupied: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """Return the sub-intervals of ``[start, end)`` not covered by ``occupied``."""
    free: list[tuple[int, int]] = []
    cursor = start
    for occ_start, occ_end in sorted(occupied):
        if occ_end <= cursor or occ_start >= end:
            continue
        if occ_start > cursor:
            free.append((cursor, min(occ_start, end)))
        cursor = max(cursor, occ_end)
        if cursor >= end:
            break
    if cursor < end:
        free.append((cursor, end))
    return free


class _Occupied:
    """Disjoint claimed intervals kept sorted by start.

    ``resolve_overlaps`` used to re-sort every claimed interval for every span
    (quadratic: tens of seconds on a large document, with no cancellation
    point). Claimed pieces never overlap each other, so the ones intersecting a
    query form one contiguous run that bisect finds directly."""

    def __init__(self) -> None:
        self.starts: list[int] = []
        self.ends: list[int] = []

    def overlapping(self, start: int, end: int) -> list[tuple[int, int]]:
        index = bisect_left(self.starts, end)  # intervals starting before `end`
        found: list[tuple[int, int]] = []
        while index > 0 and self.ends[index - 1] > start:
            index -= 1
            found.append((self.starts[index], self.ends[index]))
        return found

    def add(self, start: int, end: int) -> None:
        index = bisect_left(self.starts, start)
        self.starts.insert(index, start)
        self.ends.insert(index, end)


def resolve_overlaps(
    ranked: list[DetectionSpan], text: str
) -> list[DetectionSpan]:
    """Reconcile (already priority-ranked) spans into non-overlapping spans.

    ``ranked`` must be sorted best-first; the caller owns the priority policy. For
    each span we redact only the characters not already claimed by a
    higher-priority span, clipping the loser to that remainder (recomputing its
    hash, and rewriting a ``canonical_value`` metadata to the clipped surface so
    a fragment can never be expanded back into the full canonical name).
    """
    from tuomin_gateway.cancellation import check as check_cancelled

    selected: list[DetectionSpan] = []
    occupied = _Occupied()
    for position, span in enumerate(ranked):
        if position % 1024 == 0:
            check_cancelled()
        for piece_start, piece_end in _subtract(span.start, span.end, occupied.overlapping(span.start, span.end)):
            while piece_start < piece_end and text[piece_start].isspace():
                piece_start += 1
            while piece_end > piece_start and text[piece_end - 1].isspace():
                piece_end -= 1
            if piece_end <= piece_start:
                continue
            if piece_start == span.start and piece_end == span.end:
                selected.append(span)
            else:
                selected.append(
                    replace(
                        span,
                        start=piece_start,
                        end=piece_end,
                        text_hash=hash_text(text[piece_start:piece_end]),
                        metadata=_clipped_metadata(span.metadata, text[piece_start:piece_end]),
                    )
                )
            occupied.add(piece_start, piece_end)
    return sorted(selected, key=lambda item: (item.start, item.end, item.label))


def _clipped_metadata(metadata: dict, piece_text: str) -> dict:
    """Keep the ``text[start:end] == canonical_value`` invariant on a clipped
    fragment: the full canonical value belongs to the unclipped span, and
    carrying it over would let a canonical-identity refill EXPAND the fragment
    back into the full name. A fresh dict is returned so the winner span's
    metadata is never aliased."""
    clipped = {**metadata, "fusion_clipped": True}
    if "canonical_value" in metadata:
        clipped["canonical_value"] = piece_text
    return clipped
