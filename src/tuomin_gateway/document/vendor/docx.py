from __future__ import annotations

import hashlib
import mimetypes
import re
import struct
import zipfile
from pathlib import Path
from typing import Any, Iterable, Iterator
from xml.etree import ElementTree as ET

from .document_ir import DocumentBlock


_W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
_XML = "http://www.w3.org/XML/1998/namespace"


def _qn(local_name: str) -> str:
    return f"{{{_W}}}{local_name}"


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _natural_part_key(name: str) -> tuple[str, int, str]:
    match = re.search(r"(\d+)(?=\.xml$)", name)
    return (name[: match.start()] if match else name, int(match.group(1)) if match else 0, name)


def _column_name(index: int) -> str:
    """Return a one-based spreadsheet-style column name."""
    value = index
    result = ""
    while value:
        value, remainder = divmod(value - 1, 26)
        result = chr(65 + remainder) + result
    return result or "A"


def _clean_text(value: str) -> str:
    lines = []
    for line in value.replace("\u00a0", " ").splitlines():
        compact = re.sub(r"[ \t]+", " ", line).strip()
        if compact:
            lines.append(compact)
    return "\n".join(lines)


class _Warnings:
    def __init__(self) -> None:
        self.items: list[dict[str, Any]] = []
        self._keys: set[tuple[str, str]] = set()

    def add(
        self,
        code: str,
        message: str,
        *,
        severity: str = "warning",
        requires_manual_review: bool = True,
        capability_blocking: bool = False,
        details: dict[str, Any] | None = None,
    ) -> None:
        key = (code, message)
        if key in self._keys:
            return
        self._keys.add(key)
        item: dict[str, Any] = {
            "code": code,
            "severity": severity,
            "message": message,
            "requires_manual_review": requires_manual_review,
        }
        if capability_blocking:
            item["capability_blocking"] = True
        if details:
            item["details"] = details
        self.items.append(item)


class _BlockBuilder:
    def __init__(self, source_id: str, source_uid: str) -> None:
        self.source_id = source_id
        self.source_uid = source_uid
        self.blocks: list[DocumentBlock] = []

    def add(
        self,
        *,
        block_id: str,
        block_type: str,
        text: str,
        legacy_locators: Iterable[str],
        metadata: dict[str, Any] | None = None,
        quality: dict[str, Any] | None = None,
    ) -> DocumentBlock | None:
        normalized = _clean_text(text)
        if not normalized and block_type != "image":
            return None
        block = DocumentBlock(
            source_id=self.source_id,
            source_uid=self.source_uid,
            block_id=block_id,
            order=len(self.blocks) + 1,
            block_type=block_type,
            text=normalized,
            extraction_method="ooxml-zip",
            legacy_locators=list(legacy_locators),
            metadata=dict(metadata or {}),
            quality=dict(quality or {}),
        )
        self.blocks.append(block)
        return block


def _read_xml(package: zipfile.ZipFile, name: str) -> ET.Element:
    try:
        raw = package.read(name)
    except KeyError as exc:
        raise ValueError(f"DOCX package is missing {name}") from exc
    try:
        return ET.fromstring(raw)
    except ET.ParseError as exc:
        raise ValueError(f"DOCX part is invalid XML: {name}") from exc


def _read_optional_xml(
    package: zipfile.ZipFile,
    name: str,
    warnings: _Warnings,
) -> ET.Element | None:
    if name not in package.namelist():
        return None
    try:
        return _read_xml(package, name)
    except ValueError as exc:
        warnings.add(
            "DOCX_OPTIONAL_PART_INVALID",
            str(exc),
            details={"part": name},
        )
        return None


def _iter_block_elements(parent: ET.Element) -> Iterator[ET.Element]:
    """Yield paragraph/table elements in package order.

    Content controls and revision/custom-XML wrappers may contain body blocks;
    recursing through those wrappers preserves their OOXML order.  A yielded
    table is treated as one top-level body item, so its internal paragraphs do
    not leak into the body paragraph sequence.
    """
    for child in list(parent):
        local = _local_name(child.tag)
        if local in {"p", "tbl"} and child.tag.startswith(f"{{{_W}}}"):
            yield child
        elif local not in {"txbxContent", "hdr", "ftr"}:
            yield from _iter_block_elements(child)


def _node_text(element: ET.Element, *, skip_textboxes: bool = True) -> str:
    pieces: list[str] = []

    def walk(node: ET.Element) -> None:
        local = _local_name(node.tag)
        if skip_textboxes and local == "txbxContent":
            return
        if local in {"del", "moveFrom"}:
            inner = _clean_text(
                "".join(
                    _node_text(child, skip_textboxes=skip_textboxes)
                    for child in list(node)
                )
            )
            if inner:
                pieces.append(f"[deleted: {inner}]")
            return
        if local in {"ins", "moveTo"}:
            inner = _clean_text(
                "".join(
                    _node_text(child, skip_textboxes=skip_textboxes)
                    for child in list(node)
                )
            )
            if inner:
                pieces.append(f"[inserted: {inner}]")
            return
        if local in {"t", "delText"}:
            pieces.append(node.text or "")
            return
        if local == "tab":
            pieces.append("\t")
            return
        if local in {"br", "cr"}:
            pieces.append("\n")
            return
        if local == "noBreakHyphen":
            pieces.append("-")
            return
        if local == "softHyphen":
            pieces.append("\u00ad")
            return
        if local == "footnoteReference":
            pieces.append(f"[footnote:{node.get(_qn('id'), '')}]")
            return
        if local == "endnoteReference":
            pieces.append(f"[endnote:{node.get(_qn('id'), '')}]")
            return
        if local == "commentReference":
            pieces.append(f"[comment:{node.get(_qn('id'), '')}]")
            return
        for child in list(node):
            walk(child)

    walk(element)
    return _clean_text("".join(pieces))


def _paragraph_metadata(paragraph: ET.Element) -> dict[str, Any]:
    metadata: dict[str, Any] = {}
    properties = paragraph.find(_qn("pPr"))
    if properties is not None:
        style = properties.find(_qn("pStyle"))
        if style is not None and style.get(_qn("val")):
            metadata["style_id"] = style.get(_qn("val"))
        outline = properties.find(_qn("outlineLvl"))
        if outline is not None and outline.get(_qn("val")) is not None:
            metadata["outline_level"] = outline.get(_qn("val"))
        numbering = properties.find(_qn("numPr"))
        if numbering is not None:
            number_id = numbering.find(_qn("numId"))
            level = numbering.find(_qn("ilvl"))
            if number_id is not None and number_id.get(_qn("val")) is not None:
                metadata["numbering_id"] = number_id.get(_qn("val"))
            if level is not None and level.get(_qn("val")) is not None:
                metadata["numbering_level"] = level.get(_qn("val"))
    for kind in ("footnoteReference", "endnoteReference", "commentReference"):
        values = [
            node.get(_qn("id"), "")
            for node in paragraph.iter(_qn(kind))
            if node.get(_qn("id"), "")
        ]
        if values:
            metadata[f"{kind}_ids"] = values
    metadata["contains_page_break_marker"] = any(
        node.get(_qn("type")) == "page" for node in paragraph.iter(_qn("br"))
    ) or any(True for _ in paragraph.iter(_qn("lastRenderedPageBreak")))
    return metadata


def _table_rows(table: ET.Element) -> list[list[dict[str, Any]]]:
    rows: list[list[dict[str, Any]]] = []
    for row_number, row in enumerate(table.findall(f"./{_qn('tr')}"), 1):
        cells: list[dict[str, Any]] = []
        column = 1
        for cell in row.findall(f"./{_qn('tc')}"):
            properties = cell.find(_qn("tcPr"))
            grid_span = 1
            vertical_merge: str | None = None
            if properties is not None:
                span = properties.find(_qn("gridSpan"))
                if span is not None:
                    try:
                        grid_span = max(1, int(span.get(_qn("val"), "1")))
                    except ValueError:
                        grid_span = 1
                merge = properties.find(_qn("vMerge"))
                if merge is not None:
                    vertical_merge = merge.get(_qn("val"), "continue")
            paragraphs = [
                _node_text(paragraph)
                for paragraph in cell.iter(_qn("p"))
            ]
            text = _clean_text("\n".join(value for value in paragraphs if value))
            cells.append(
                {
                    "row": row_number,
                    "column": column,
                    "coordinate": f"{_column_name(column)}{row_number}",
                    "text": text,
                    "grid_span": grid_span,
                    "vertical_merge": vertical_merge,
                    "nested_table_count": max(0, len(list(cell.iter(_qn("tbl"))))),
                }
            )
            column += grid_span
        rows.append(cells)
    return rows


def _table_summary(rows: list[list[dict[str, Any]]]) -> str:
    rendered: list[str] = []
    for row in rows:
        rendered.append(
            " | ".join(
                f"{cell['coordinate']}={cell['text'] or '[EMPTY CELL]'}"
                for cell in row
            )
        )
    return "\n".join(rendered)


def _structural_quality(*, manual_review: bool = False) -> dict[str, Any]:
    return {
        "level": "manual_review" if manual_review else "structural",
        "requires_manual_review": manual_review,
        "rendered_page_verified": False,
    }


def _add_table_blocks(
    builder: _BlockBuilder,
    table: ET.Element,
    *,
    table_number: int,
    block_prefix: str = "body",
    locator_prefix: str | None = None,
) -> dict[str, int]:
    rows = _table_rows(table)
    table_locator = (
        f"{builder.source_id}#table{table_number:03d}"
        if locator_prefix is None
        else locator_prefix
    )
    table_id = f"{block_prefix}-t{table_number:03d}"
    builder.add(
        block_id=table_id,
        block_type="table",
        text=_table_summary(rows) or "[EMPTY TABLE]",
        legacy_locators=[table_locator],
        metadata={
            "table": table_number,
            "row_count": len(rows),
            "cell_count": sum(len(row) for row in rows),
            "body_order_scope": block_prefix,
        },
        quality=_structural_quality(),
    )
    cell_count = 0
    nested_tables = 0
    for row_number, cells in enumerate(rows, 1):
        row_locator = f"{table_locator}/row{row_number:04d}"
        builder.add(
            block_id=f"{table_id}-r{row_number:04d}",
            block_type="table_row",
            text=" | ".join(
                f"{cell['coordinate']}={cell['text'] or '[EMPTY CELL]'}"
                for cell in cells
            ) or "[EMPTY ROW]",
            legacy_locators=[row_locator],
            metadata={"table": table_number, "row": row_number},
            quality=_structural_quality(),
        )
        for cell in cells:
            cell_count += 1
            nested_tables += int(cell["nested_table_count"])
            coordinate = str(cell["coordinate"])
            builder.add(
                block_id=f"{table_id}-r{row_number:04d}-c{coordinate}",
                block_type="table_cell",
                text=cell["text"] or "[EMPTY CELL]",
                legacy_locators=[f"{row_locator}/cell{coordinate}"],
                metadata={
                    "table": table_number,
                    "row": row_number,
                    "column": cell["column"],
                    "coordinate": coordinate,
                    "grid_span": cell["grid_span"],
                    "vertical_merge": cell["vertical_merge"],
                    "empty": not bool(cell["text"]),
                },
                quality=_structural_quality(),
            )
    return {
        "rows": len(rows),
        "cells": cell_count,
        "nested_tables": nested_tables,
    }


def _extract_body(
    root: ET.Element,
    builder: _BlockBuilder,
    warnings: _Warnings,
) -> dict[str, int]:
    body = root.find(_qn("body"))
    if body is None:
        raise ValueError("DOCX word/document.xml has no w:body")
    paragraph_number = 0
    table_number = 0
    paragraph_blocks = 0
    table_rows = 0
    table_cells = 0
    nested_tables = 0
    for element in _iter_block_elements(body):
        if element.tag == _qn("p"):
            paragraph_number += 1
            block = builder.add(
                block_id=f"body-p{paragraph_number:04d}",
                block_type="paragraph",
                text=_node_text(element),
                legacy_locators=[
                    f"{builder.source_id}#paragraph{paragraph_number:04d}"
                ],
                metadata={
                    "paragraph": paragraph_number,
                    "body_order_preserved": True,
                    **_paragraph_metadata(element),
                },
                quality=_structural_quality(),
            )
            paragraph_blocks += int(block is not None)
        elif element.tag == _qn("tbl"):
            table_number += 1
            counts = _add_table_blocks(
                builder,
                element,
                table_number=table_number,
            )
            table_rows += counts["rows"]
            table_cells += counts["cells"]
            nested_tables += counts["nested_tables"]
    if nested_tables:
        warnings.add(
            "DOCX_NESTED_TABLE_FLATTENED",
            f"{nested_tables} nested table occurrence(s) were flattened into parent-cell text",
        )
    alt_chunks = len(list(body.iter(_qn("altChunk"))))
    if alt_chunks:
        warnings.add(
            "DOCX_ALTCHUNK_NOT_EXTRACTED",
            f"{alt_chunks} altChunk object(s) were not expanded",
            capability_blocking=True,
        )
    return {
        "paragraph_count": paragraph_blocks,
        "paragraph_slots": paragraph_number,
        "table_count": table_number,
        "table_row_count": table_rows,
        "table_cell_count": table_cells,
        "nested_table_count": nested_tables,
        "altchunk_count": alt_chunks,
    }


def _part_names(package: zipfile.ZipFile, prefix: str) -> list[str]:
    pattern = re.compile(rf"^word/{re.escape(prefix)}\d+\.xml$")
    return sorted(
        (name for name in package.namelist() if pattern.fullmatch(name)),
        key=_natural_part_key,
    )


def _extract_header_footer_parts(
    package: zipfile.ZipFile,
    builder: _BlockBuilder,
    warnings: _Warnings,
    *,
    kind: str,
) -> tuple[int, list[tuple[str, ET.Element]]]:
    block_count = 0
    roots: list[tuple[str, ET.Element]] = []
    for part_number, name in enumerate(_part_names(package, kind), 1):
        root = _read_optional_xml(package, name, warnings)
        if root is None:
            continue
        roots.append((name, root))
        content_number = 0
        for element in _iter_block_elements(root):
            content_number += 1
            if element.tag == _qn("p"):
                text = _node_text(element)
                metadata = _paragraph_metadata(element)
                content_kind = "paragraph"
            else:
                text = _table_summary(_table_rows(element))
                metadata = {"content_kind": "table"}
                content_kind = "table"
            block = builder.add(
                block_id=f"{kind}-{part_number:03d}-p{content_number:04d}",
                block_type=kind,
                text=text,
                legacy_locators=[
                    f"{builder.source_id}#{kind}{part_number:03d}/paragraph{content_number:04d}"
                ],
                metadata={
                    "part": name,
                    "part_number": part_number,
                    "content_number": content_number,
                    "content_kind": content_kind,
                    **metadata,
                },
                quality=_structural_quality(),
            )
            block_count += int(block is not None)
    return block_count, roots


def _extract_notes(
    root: ET.Element | None,
    builder: _BlockBuilder,
    *,
    kind: str,
    part_name: str,
) -> tuple[int, int]:
    if root is None:
        return 0, 0
    block_count = 0
    note_number = 0
    singular = kind[:-1] if kind.endswith("s") else kind
    for note in list(root):
        if _local_name(note.tag) != singular:
            continue
        note_type = note.get(_qn("type"), "")
        raw_id = note.get(_qn("id"), "")
        if note_type in {"separator", "continuationSeparator"}:
            continue
        try:
            native_id = int(raw_id)
            if native_id < 1:
                continue
        except ValueError:
            native_id = note_number + 1
        note_number += 1
        locator_number = native_id if native_id <= 999 else note_number
        content_number = 0
        for element in _iter_block_elements(note):
            content_number += 1
            text = (
                _node_text(element)
                if element.tag == _qn("p")
                else _table_summary(_table_rows(element))
            )
            block = builder.add(
                block_id=f"{singular}-{locator_number:03d}-p{content_number:04d}",
                block_type=singular,
                text=text,
                legacy_locators=[
                    f"{builder.source_id}#{singular}{locator_number:03d}/paragraph{content_number:04d}"
                ],
                metadata={
                    "part": part_name,
                    "note_number": note_number,
                    "locator_note_number": locator_number,
                    "ooxml_note_id": raw_id,
                    "content_number": content_number,
                    "content_kind": "paragraph" if element.tag == _qn("p") else "table",
                },
                quality=_structural_quality(),
            )
            block_count += int(block is not None)
    return note_number, block_count


def _extract_comments(
    root: ET.Element | None,
    builder: _BlockBuilder,
) -> tuple[int, int]:
    if root is None:
        return 0, 0
    comment_number = 0
    block_count = 0
    for comment in list(root):
        if _local_name(comment.tag) != "comment":
            continue
        comment_number += 1
        content_number = 0
        for element in _iter_block_elements(comment):
            content_number += 1
            text = (
                _node_text(element)
                if element.tag == _qn("p")
                else _table_summary(_table_rows(element))
            )
            block = builder.add(
                block_id=f"comment-{comment_number:03d}-p{content_number:04d}",
                block_type="comment",
                text=text,
                legacy_locators=[
                    f"{builder.source_id}#comment{comment_number:03d}/paragraph{content_number:04d}"
                ],
                metadata={
                    "part": "word/comments.xml",
                    "comment_number": comment_number,
                    "ooxml_comment_id": comment.get(_qn("id"), ""),
                    "author": comment.get(_qn("author"), ""),
                    "initials": comment.get(_qn("initials"), ""),
                    "date": comment.get(_qn("date"), ""),
                    "content_number": content_number,
                    "content_kind": "paragraph" if element.tag == _qn("p") else "table",
                },
                quality=_structural_quality(),
            )
            block_count += int(block is not None)
    return comment_number, block_count


def _extract_textboxes(
    roots: Iterable[tuple[str, ET.Element]],
    builder: _BlockBuilder,
) -> tuple[int, int]:
    textbox_count = 0
    block_count = 0
    for part_name, root in roots:
        for textbox in root.iter(_qn("txbxContent")):
            textbox_count += 1
            content_number = 0
            for element in _iter_block_elements(textbox):
                content_number += 1
                text = (
                    _node_text(element, skip_textboxes=False)
                    if element.tag == _qn("p")
                    else _table_summary(_table_rows(element))
                )
                block = builder.add(
                    block_id=f"textbox-{textbox_count:03d}-p{content_number:04d}",
                    block_type="textbox",
                    text=text,
                    legacy_locators=[
                        f"{builder.source_id}#textbox{textbox_count:03d}/paragraph{content_number:04d}"
                    ],
                    metadata={
                        "part": part_name,
                        "textbox_number": textbox_count,
                        "content_number": content_number,
                        "content_kind": "paragraph" if element.tag == _qn("p") else "table",
                    },
                    quality=_structural_quality(manual_review=True),
                )
                block_count += int(block is not None)
    return textbox_count, block_count


def _png_dimensions(data: bytes) -> tuple[int, int] | None:
    if len(data) >= 24 and data.startswith(b"\x89PNG\r\n\x1a\n"):
        return struct.unpack(">II", data[16:24])
    return None


def _gif_dimensions(data: bytes) -> tuple[int, int] | None:
    if len(data) >= 10 and data[:6] in {b"GIF87a", b"GIF89a"}:
        return struct.unpack("<HH", data[6:10])
    return None


def _bmp_dimensions(data: bytes) -> tuple[int, int] | None:
    if len(data) >= 26 and data.startswith(b"BM"):
        width, height = struct.unpack("<ii", data[18:26])
        return abs(width), abs(height)
    return None


def _jpeg_dimensions(data: bytes) -> tuple[int, int] | None:
    if len(data) < 4 or not data.startswith(b"\xff\xd8"):
        return None
    offset = 2
    sof_markers = {
        0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7,
        0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF,
    }
    while offset + 4 <= len(data):
        if data[offset] != 0xFF:
            offset += 1
            continue
        while offset < len(data) and data[offset] == 0xFF:
            offset += 1
        if offset >= len(data):
            break
        marker = data[offset]
        offset += 1
        if marker in {0xD8, 0xD9} or 0xD0 <= marker <= 0xD7:
            continue
        if offset + 2 > len(data):
            break
        segment_length = struct.unpack(">H", data[offset:offset + 2])[0]
        if segment_length < 2 or offset + segment_length > len(data):
            break
        if marker in sof_markers and segment_length >= 7:
            height, width = struct.unpack(">HH", data[offset + 3:offset + 7])
            return width, height
        offset += segment_length
    return None


def _svg_dimensions(data: bytes) -> tuple[int, int] | None:
    try:
        root = ET.fromstring(data)
    except ET.ParseError:
        return None

    def numeric(value: str | None) -> float | None:
        if not value:
            return None
        match = re.match(r"\s*([0-9]+(?:\.[0-9]+)?)", value)
        return float(match.group(1)) if match else None

    width = numeric(root.get("width"))
    height = numeric(root.get("height"))
    if width is None or height is None:
        view_box = root.get("viewBox", "").replace(",", " ").split()
        if len(view_box) == 4:
            try:
                width = float(view_box[2])
                height = float(view_box[3])
            except ValueError:
                return None
    if width is None or height is None:
        return None
    return max(0, round(width)), max(0, round(height))


def _image_dimensions(name: str, data: bytes) -> tuple[int, int] | None:
    suffix = Path(name).suffix.lower()
    if suffix == ".png":
        return _png_dimensions(data)
    if suffix in {".jpg", ".jpeg"}:
        return _jpeg_dimensions(data)
    if suffix == ".gif":
        return _gif_dimensions(data)
    if suffix == ".bmp":
        return _bmp_dimensions(data)
    if suffix == ".svg":
        return _svg_dimensions(data)
    return None


def _extract_media(
    package: zipfile.ZipFile,
    builder: _BlockBuilder,
    warnings: _Warnings,
) -> list[dict[str, Any]]:
    media_names = sorted(
        name
        for name in package.namelist()
        if name.startswith("word/media/") and not name.endswith("/")
    )
    media: list[dict[str, Any]] = []
    for media_number, name in enumerate(media_names, 1):
        data = package.read(name)
        dimensions = _image_dimensions(name, data)
        content_type = mimetypes.guess_type(name)[0] or "application/octet-stream"
        record: dict[str, Any] = {
            "media_number": media_number,
            "package_path": name,
            "filename": Path(name).name,
            "sha256": hashlib.sha256(data).hexdigest(),
            "bytes": len(data),
            "content_type": content_type,
            "width": dimensions[0] if dimensions else None,
            "height": dimensions[1] if dimensions else None,
            "dimension_unit": "pixels" if dimensions else None,
            "ocr_status": "not_performed",
        }
        media.append(record)
        builder.add(
            block_id=f"image-{media_number:03d}",
            block_type="image",
            text=f"[Image not OCRed: {record['filename']}]",
            legacy_locators=[f"{builder.source_id}#image{media_number:03d}"],
            metadata=record,
            quality={
                "level": "manual_review",
                "requires_manual_review": True,
                "ocr_status": "not_performed",
            },
        )
    if media:
        warnings.add(
            "DOCX_MEDIA_NOT_OCR",
            f"{len(media)} embedded media file(s) were indexed but not OCRed",
            details={"media_count": len(media)},
        )
    return media


def extract_docx(
    path: Path,
    source_id: str,
    source_uid: str,
) -> tuple[list[DocumentBlock], list[dict[str, Any]], dict[str, Any]]:
    """Extract DOCX content into deterministic, source-addressable IR blocks.

    The extractor intentionally does not invent rendered Word page numbers.
    Body paragraphs and tables follow ``word/document.xml`` order. Ancillary
    content is appended in deterministic part order: headers, footers,
    footnotes, endnotes, comments, textboxes, then package media.
    """
    source_path = Path(path)
    if not source_path.is_file():
        raise FileNotFoundError(f"DOCX source does not exist: {source_path}")

    builder = _BlockBuilder(source_id, source_uid)
    warnings = _Warnings()
    warnings.add(
        "DOCX_RENDERED_PAGES_UNAVAILABLE",
        "DOCX OOXML has no reliable rendered page map; no Word page numbers were inferred",
        severity="warning",
        requires_manual_review=False,
    )

    try:
        package_context = zipfile.ZipFile(source_path)
    except zipfile.BadZipFile as exc:
        raise ValueError("DOCX source is not a valid OOXML ZIP package") from exc

    with package_context as package:
        document_root = _read_xml(package, "word/document.xml")
        body_stats = _extract_body(document_root, builder, warnings)

        header_count, header_roots = _extract_header_footer_parts(
            package, builder, warnings, kind="header"
        )
        footer_count, footer_roots = _extract_header_footer_parts(
            package, builder, warnings, kind="footer"
        )

        footnotes_root = _read_optional_xml(
            package, "word/footnotes.xml", warnings
        )
        endnotes_root = _read_optional_xml(
            package, "word/endnotes.xml", warnings
        )
        comments_root = _read_optional_xml(
            package, "word/comments.xml", warnings
        )
        footnote_count, footnote_block_count = _extract_notes(
            footnotes_root,
            builder,
            kind="footnotes",
            part_name="word/footnotes.xml",
        )
        endnote_count, endnote_block_count = _extract_notes(
            endnotes_root,
            builder,
            kind="endnotes",
            part_name="word/endnotes.xml",
        )
        comment_count, comment_block_count = _extract_comments(
            comments_root, builder
        )

        textbox_roots: list[tuple[str, ET.Element]] = [
            ("word/document.xml", document_root),
            *header_roots,
            *footer_roots,
        ]
        for name, root in (
            ("word/footnotes.xml", footnotes_root),
            ("word/endnotes.xml", endnotes_root),
            ("word/comments.xml", comments_root),
        ):
            if root is not None:
                textbox_roots.append((name, root))
        textbox_count, textbox_block_count = _extract_textboxes(
            textbox_roots, builder
        )

        has_tracked_changes = any(
            any(True for _ in root.iter(_qn(tag)))
            for root in [document_root, *[root for _, root in header_roots], *[root for _, root in footer_roots]]
            for tag in ("ins", "del", "moveFrom", "moveTo")
        )
        if has_tracked_changes:
            warnings.add(
                "DOCX_TRACKED_CHANGES_PRESENT",
                "Tracked changes were preserved with inserted/deleted markers and require review",
            )

        embedded_objects = sorted(
            name
            for name in package.namelist()
            if name.startswith("word/embeddings/") and not name.endswith("/")
        )
        if embedded_objects:
            warnings.add(
                "DOCX_EMBEDDED_OBJECT_NOT_EXTRACTED",
                f"{len(embedded_objects)} embedded object(s) were indexed by count but not decoded",
                capability_blocking=True,
                details={"package_paths": embedded_objects},
            )

        media = _extract_media(package, builder, warnings)

    non_media_blocks = [block for block in builder.blocks if block.block_type != "image"]
    if not any(block.text.strip() for block in non_media_blocks):
        warnings.add(
            "NO_TEXT_EXTRACTED",
            "No non-empty text could be extracted from the DOCX package",
            severity="blocking",
            capability_blocking=True,
        )

    stats: dict[str, Any] = {
        **body_stats,
        "block_count": len(builder.blocks),
        "characters": sum(len(block.text) for block in non_media_blocks),
        "body_order_preserved": True,
        "rendered_page_count": None,
        "rendered_page_numbers_available": False,
        "header_part_count": len(header_roots),
        "header_block_count": header_count,
        "footer_part_count": len(footer_roots),
        "footer_block_count": footer_count,
        "footnote_count": footnote_count,
        "footnote_block_count": footnote_block_count,
        "endnote_count": endnote_count,
        "endnote_block_count": endnote_block_count,
        "comment_count": comment_count,
        "comment_block_count": comment_block_count,
        "textbox_count": textbox_count,
        "textbox_block_count": textbox_block_count,
        "media_count": len(media),
        "media_bytes": sum(int(item["bytes"]) for item in media),
        "media_with_dimensions": sum(
            1 for item in media if item["width"] is not None and item["height"] is not None
        ),
        "media": media,
        "embedded_object_count": len(embedded_objects),
        "tracked_changes_present": has_tracked_changes,
        "warning_count": len(warnings.items),
        "extraction_method": "ooxml-zip",
        "layout_note": (
            "OOXML structural anchors are preserved; rendered Word pages, floating layout, "
            "and visual placement were not reconstructed."
        ),
    }
    return builder.blocks, warnings.items, stats
