from __future__ import annotations

from pathlib import Path
from typing import Any

from .document_ir import DocumentBlock


def extract_text(
    path: Path,
    source_id: str,
    source_uid: str,
) -> tuple[list[DocumentBlock], list[dict[str, Any]], dict[str, Any]]:
    raw = path.read_text(encoding="utf-8-sig", errors="replace")
    warnings: list[dict[str, Any]] = []
    replacement_count = raw.count("\ufffd")
    if replacement_count:
        warnings.append(
            {
                "code": "TEXT_DECODE_REPLACEMENT",
                "severity": "warning",
                "message": f"{replacement_count} undecodable characters were replaced",
                "requires_manual_review": True,
            }
        )

    blocks: list[DocumentBlock] = []
    for line_number, line in enumerate(raw.splitlines(), 1):
        text = line.strip()
        if not text:
            continue
        locator = f"{source_id}#line{line_number:04d}"
        blocks.append(
            DocumentBlock(
                source_id=source_id,
                source_uid=source_uid,
                block_id=f"line-{line_number:04d}",
                order=len(blocks) + 1,
                block_type="line",
                text=text,
                extraction_method="utf8-text",
                legacy_locators=[locator],
                metadata={"line": line_number},
                quality={
                    "level": "manual_review" if replacement_count else "high",
                    "requires_manual_review": bool(replacement_count),
                },
            )
        )
    if not blocks:
        warnings.append(
            {
                "code": "NO_TEXT_EXTRACTED",
                "severity": "blocking",
                "message": "No non-empty text could be extracted",
                "requires_manual_review": True,
                "capability_blocking": True,
            }
        )
    return blocks, warnings, {
        "characters": sum(len(block.text) for block in blocks),
        "line_count": len(blocks),
        "replacement_characters": replacement_count,
    }
