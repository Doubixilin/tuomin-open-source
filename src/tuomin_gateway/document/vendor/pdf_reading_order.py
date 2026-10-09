"""Reading-order helpers for reliably detected bilingual two-column PDF pages."""

from __future__ import annotations

import re
from typing import Any, Callable, Iterable

_RE_CJK = re.compile(r"[一-鿿]")
_RE_LATIN_OR_CYRILLIC = re.compile(r"[A-Za-zÀ-ɏЀ-ӿ]")

_MIN_CJK_CHARS = 16
_MIN_OTHER_LANGUAGE_CHARS = 32
_MIN_LEFT_CJK_RATIO = 0.22
_MIN_RIGHT_OTHER_LANGUAGE_RATIO = 0.75
_MIN_VERTICAL_OVERLAP_RATIO = 0.35
_AUTO_COLUMN_SPLIT = object()


def detect_bilingual_column_split(page: Any) -> float | None:
    """Return the centre-gutter x coordinate for a Chinese/foreign parallel page.

    Detection is deliberately narrow: both sides need enough text, the left side
    must be materially CJK, the right side materially Latin/Cyrillic, and their
    vertical ranges must overlap. Ordinary single-column pages therefore keep
    pdfplumber's existing extraction path.
    """
    chars = [
        char
        for char in getattr(page, "chars", ())
        if str(char.get("text") or "").strip()
        and _has_coordinates(char)
    ]
    if not chars:
        return None

    width = float(getattr(page, "width", 0.0) or 0.0)
    if width <= 0:
        return None

    split, gutter_occupancy = _least_occupied_central_x(chars, width)
    # A real two-column layout has a persistent empty vertical gutter. Without
    # this gate, alternating Chinese/foreign standard lists can look bilingual
    # by script ratio even though every line spans the full page width.
    if gutter_occupancy != 0:
        return None
    left = [char for char in chars if _horizontal_center(char) < split]
    right = [char for char in chars if _horizontal_center(char) >= split]
    left_text = "".join(str(char.get("text") or "") for char in left)
    right_text = "".join(str(char.get("text") or "") for char in right)

    left_cjk = len(_RE_CJK.findall(left_text))
    left_other = len(_RE_LATIN_OR_CYRILLIC.findall(left_text))
    right_cjk = len(_RE_CJK.findall(right_text))
    right_other = len(_RE_LATIN_OR_CYRILLIC.findall(right_text))
    left_ratio = left_cjk / max(left_cjk + left_other, 1)
    right_ratio = right_other / max(right_cjk + right_other, 1)

    if (
        left_cjk < _MIN_CJK_CHARS
        or right_other < _MIN_OTHER_LANGUAGE_CHARS
        or left_ratio < _MIN_LEFT_CJK_RATIO
        or right_ratio < _MIN_RIGHT_OTHER_LANGUAGE_RATIO
    ):
        return None

    if _vertical_overlap_ratio(left, right) < _MIN_VERTICAL_OVERLAP_RATIO:
        return None
    return split


def pdf_page_lines_in_reading_order(
    page: Any, *, column_split: float | None | object = _AUTO_COLUMN_SPLIT
) -> tuple[list[str], float | None]:
    """Extract page lines, ordering a detected parallel page left then right."""
    if column_split is _AUTO_COLUMN_SPLIT:
        split = detect_bilingual_column_split(page)
    elif isinstance(column_split, (int, float)):
        split = float(column_split)
    else:
        split = None
    if split is None:
        page_text = page.extract_text() or ""
        return ([line for line in page_text.split("\n") if line.strip()], None)

    left_page = page.filter(_side_filter(split, left=True))
    right_page = page.filter(_side_filter(split, left=False))
    return (_text_lines(left_page) + _text_lines(right_page), split)


def column_side_filter(split: float, *, left: bool) -> Callable[[dict[str, Any]], bool]:
    """Public side predicate for composing column and table-region filters."""
    return _side_filter(split, left=left)


def text_lines(page: Any) -> list[str]:
    """Extract non-empty text lines from a pdfplumber page-like object."""
    return _text_lines(page)


def _least_occupied_central_x(
    chars: list[dict[str, Any]], width: float
) -> tuple[float, int]:
    centre = width / 2.0
    lower = int(round(width * 0.42))
    upper = int(round(width * 0.58))
    clearance = max(3.0, width * 0.007)

    def score(candidate: float) -> tuple[int, float, float]:
        occupied = sum(
            1
            for char in chars
            if float(char["x0"]) - clearance
            <= candidate
            <= float(char["x1"]) + clearance
        )
        return occupied, abs(candidate - centre), candidate

    best = min(score(float(candidate)) for candidate in range(lower, upper + 1))
    return best[2], best[0]


def _side_filter(split: float, *, left: bool) -> Callable[[dict[str, Any]], bool]:
    def predicate(obj: dict[str, Any]) -> bool:
        if obj.get("object_type") != "char" or not _has_coordinates(obj):
            return True
        is_left = _horizontal_center(obj) < split
        return is_left if left else not is_left

    return predicate


def _text_lines(page: Any) -> list[str]:
    records = page.extract_text_lines(strip=True, return_chars=False)
    return [
        str(record.get("text") or "").strip()
        for record in records
        if str(record.get("text") or "").strip()
    ]


def _vertical_overlap_ratio(
    left: Iterable[dict[str, Any]], right: Iterable[dict[str, Any]]
) -> float:
    left_span = _vertical_span(left)
    right_span = _vertical_span(right)
    if left_span is None or right_span is None:
        return 0.0
    overlap = max(0.0, min(left_span[1], right_span[1]) - max(left_span[0], right_span[0]))
    shortest = max(1.0, min(left_span[1] - left_span[0], right_span[1] - right_span[0]))
    return overlap / shortest


def _vertical_span(chars: Iterable[dict[str, Any]]) -> tuple[float, float] | None:
    letters = [
        char
        for char in chars
        if _RE_CJK.search(str(char.get("text") or ""))
        or _RE_LATIN_OR_CYRILLIC.search(str(char.get("text") or ""))
    ]
    if not letters:
        return None
    return (
        min(float(char["top"]) for char in letters),
        max(float(char["bottom"]) for char in letters),
    )


def _horizontal_center(obj: dict[str, Any]) -> float:
    return (float(obj["x0"]) + float(obj["x1"])) / 2.0


def _has_coordinates(obj: dict[str, Any]) -> bool:
    return all(key in obj for key in ("x0", "x1", "top", "bottom"))
