"""Operator-tunable request size limits (Phase 5).

Long contracts are legitimate input, but unbounded text lets a single request
tie up the rules scanner, the CPU-bound NER pipeline, and the fusion pass.
These caps fail closed with HTTP 413 instead of truncating (silent truncation
would hide entities in the dropped tail — the exact failure NER chunking
exists to prevent). The CLI is an explicit local tool and is NOT limited here.
"""
from __future__ import annotations

import os

_DEFAULT_MAX_TEXT_CHARS = 200_000      # ≈ hundreds of Chinese pages
_DEFAULT_MAX_BATCH_ITEMS = 64
_DEFAULT_MAX_REQUEST_BYTES = 4_000_000  # proxy bodies carry full chat history


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "") or default)
    except ValueError:
        return default


def max_text_chars() -> int:
    return _env_int("TUOMIN_MAX_TEXT_CHARS", _DEFAULT_MAX_TEXT_CHARS)


def max_batch_items() -> int:
    return _env_int("TUOMIN_MAX_BATCH_ITEMS", _DEFAULT_MAX_BATCH_ITEMS)


def max_request_bytes() -> int:
    return _env_int("TUOMIN_MAX_REQUEST_BYTES", _DEFAULT_MAX_REQUEST_BYTES)


def text_over_limit(text: object) -> int | None:
    """Return the exceeded character limit for an oversized text, else None."""
    if not isinstance(text, str):
        return None
    limit = max_text_chars()
    return limit if len(text) > limit else None
