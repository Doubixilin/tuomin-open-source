"""Render Document IR blocks to clean, reader-friendly Markdown.

Nested DOCX tables are already flattened by the vendored extractor into
parent-cell text, so their inner row and column structure cannot be recovered.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

from .vendor.document_ir import DocumentBlock

_ANCILLARY_TYPES = frozenset(
    {"header", "footer", "footnote", "endnote", "comment", "textbox", "image"}
)
_TABLE_TYPES = frozenset({"table", "table_row", "table_cell"})
_SPREADSHEET_TYPES = frozenset({"spreadsheet_sheet", "spreadsheet_cell"})
_COORDINATE = re.compile(r"^([A-Za-z]+)([1-9][0-9]*)$")
_HEADING_STYLE = re.compile(r"^(?:heading|标题)[ _-]*([0-9]+)$", re.IGNORECASE)


@dataclass(frozen=True)
class BlockSpan:
    block_id: str
    start_line: int
    end_line: int


@dataclass(frozen=True)
class MarkdownRender:
    text: str
    spans: list[BlockSpan]


@dataclass(frozen=True)
class _Chunk:
    lines: list[str]
    spans: list[BlockSpan]


def render_clean_markdown(
    blocks: list[DocumentBlock], *, include_ancillary: bool = False
) -> MarkdownRender:
    """Render blocks in document order and return source-block line spans."""
    ordered = sorted(
        (block for block in blocks if block.text.strip()),
        key=lambda block: (block.order, block.block_id),
    )
    groups: dict[tuple[str, str, str], list[DocumentBlock]] = {}
    for block in ordered:
        key = _group_key(block)
        if key is not None:
            groups.setdefault(key, []).append(block)

    chunks: list[_Chunk] = []
    rendered_groups: set[tuple[str, str, str]] = set()
    for block in ordered:
        key = _group_key(block)
        if key is not None:
            if key in rendered_groups:
                continue
            rendered_groups.add(key)
            chunk = _table_chunk(groups[key], spreadsheet=key[0] == "sheet")
            if chunk is not None:
                chunks.append(chunk)
            continue

        if block.block_type == "spreadsheet_workbook":
            continue
        if block.block_type in _ANCILLARY_TYPES:
            if include_ancillary:
                chunks.append(_paragraph_chunk(block, ancillary=True))
            continue
        if block.block_type in {"paragraph", "line"}:
            chunks.append(_paragraph_chunk(block))

    output_lines: list[str] = []
    spans: list[BlockSpan] = []
    for chunk in chunks:
        if output_lines:
            output_lines.append("")
        offset = len(output_lines)
        output_lines.extend(chunk.lines)
        spans.extend(
            BlockSpan(
                block_id=span.block_id,
                start_line=offset + span.start_line,
                end_line=offset + span.end_line,
            )
            for span in chunk.spans
        )
    return MarkdownRender(text="\n".join(output_lines) + "\n", spans=spans)


def _group_key(block: DocumentBlock) -> tuple[str, str, str] | None:
    source = block.source_uid or block.source_id
    if block.block_type in _TABLE_TYPES:
        table = block.metadata.get("table")
        if table is not None:
            identity = str(table)
        else:
            identity = re.sub(r"-r[0-9]{4}(?:-c.*)?$", "", block.block_id)
        return ("table", source, identity)
    if block.block_type in _SPREADSHEET_TYPES:
        sheet = block.metadata.get("sheet_index")
        if sheet is None:
            sheet = block.metadata.get("sheet_name", block.block_id)
        return ("sheet", source, str(sheet))
    return None


def _paragraph_chunk(block: DocumentBlock, *, ancillary: bool = False) -> _Chunk:
    lines = _text_lines(block.text)
    if ancillary:
        lines[0] = f"<!-- tuomin:{block.block_type} --> {lines[0]}"
    elif block.block_type == "paragraph":
        level = _heading_level(block.metadata)
        if level is not None:
            lines[0] = f"{'#' * level} {lines[0]}"
    return _Chunk(
        lines=lines,
        spans=[BlockSpan(block.block_id, 1, len(lines))],
    )


def _heading_level(metadata: dict[str, Any]) -> int | None:
    outline = _integer(metadata.get("outline_level"))
    if outline is not None:
        return min(6, max(1, outline + 1))
    explicit = _integer(metadata.get("heading_level"))
    if explicit is not None:
        return min(6, max(1, explicit))
    style = str(metadata.get("style_id", "")).strip()
    match = _HEADING_STYLE.fullmatch(style)
    if match is None:
        return None
    return min(6, max(1, int(match.group(1))))


def _table_chunk(
    members: list[DocumentBlock], *, spreadsheet: bool
) -> _Chunk | None:
    cell_type = "spreadsheet_cell" if spreadsheet else "table_cell"
    cells = [block for block in members if block.block_type == cell_type]
    if not cells:
        if spreadsheet:
            return None
        rows = [block for block in members if block.block_type == "table_row"]
        sources = rows or [block for block in members if block.block_type == "table"]
        if not sources:
            return None
        lines = [_flatten(block.text) for block in sources]
        return _Chunk(
            lines=lines,
            spans=[
                BlockSpan(block.block_id, index, index)
                for index, block in enumerate(sources, 1)
            ],
        )

    positioned: list[tuple[int, int, int, DocumentBlock]] = []
    for fallback_column, block in enumerate(cells, 1):
        row, column = _cell_position(block, fallback_column)
        positioned.append((row, column, block.order, block))
    positioned.sort(key=lambda item: (item[0], item[1], item[2], item[3].block_id))

    row_numbers = sorted({item[0] for item in positioned})
    width = max(
        column + max(1, _integer(block.metadata.get("grid_span")) or 1) - 1
        for _, column, _, block in positioned
    )
    values: dict[tuple[int, int], str] = {}
    for row, column, _, block in positioned:
        values[(row, column)] = _escape_cell(_cell_text(block, spreadsheet=spreadsheet))

    lines: list[str] = []
    row_lines: dict[int, int] = {}
    for index, row in enumerate(row_numbers):
        if index == 1:
            lines.append("| " + " | ".join("---" for _ in range(width)) + " |")
        row_lines[row] = len(lines) + 1
        lines.append(
            "| "
            + " | ".join(values.get((row, column), "") for column in range(1, width + 1))
            + " |"
        )
    if len(row_numbers) == 1:
        lines.append("| " + " | ".join("---" for _ in range(width)) + " |")

    spans = [
        BlockSpan(block.block_id, row_lines[row], row_lines[row])
        for row, _, _, block in positioned
    ]
    return _Chunk(lines=lines, spans=spans)


def _cell_position(block: DocumentBlock, fallback_column: int) -> tuple[int, int]:
    row = _integer(block.metadata.get("row"))
    column = _integer(block.metadata.get("column"))
    coordinate = str(block.metadata.get("coordinate", ""))
    match = _COORDINATE.fullmatch(coordinate)
    if match is not None:
        if row is None:
            row = int(match.group(2))
        if column is None:
            column = _column_number(match.group(1))
    return max(1, row or 1), max(1, column or fallback_column)


def _cell_text(block: DocumentBlock, *, spreadsheet: bool) -> str:
    if not spreadsheet:
        return block.text
    metadata = block.metadata
    value = metadata.get("value") if "value" in metadata else None
    if value is None and metadata.get("formula") is not None:
        value = metadata["formula"]
    if value is None and "cached_value" in metadata:
        value = metadata.get("cached_value")
    if value is None and "value" not in metadata:
        return block.text
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, (dict, list, tuple)):
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    return str(value)


def _text_lines(text: str) -> list[str]:
    normalized = text.strip().replace("\r\n", "\n").replace("\r", "\n")
    return normalized.split("\n")


def _flatten(text: str) -> str:
    return "<br>".join(line.strip() for line in _text_lines(text))


def _escape_cell(text: str) -> str:
    return _flatten(text).replace("|", "\\|")


def _integer(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _column_number(name: str) -> int:
    value = 0
    for character in name.upper():
        value = value * 26 + ord(character) - ord("A") + 1
    return value
