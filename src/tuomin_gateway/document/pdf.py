"""PDF native-text extraction with PyMuPDF or pdfminer.six."""
from __future__ import annotations

from contextlib import closing
import statistics
import unicodedata
from pathlib import Path
from typing import Any, Callable, Iterable

from .vendor.document_ir import DocumentBlock
from .vendor.pdf_reading_order import pdf_page_lines_in_reading_order


class PDFDependencyUnavailable(RuntimeError):
    """The optional PDF parser is not installed in this environment."""


class _TextPage:
    """Character coordinates shared by both PDF parsers and the reading-order pass."""

    def __init__(self, chars: Iterable[dict[str, Any]], width: float) -> None:
        self.chars = list(chars)
        self.width = float(width)

    @classmethod
    def from_page(cls, page: Any) -> "_TextPage":
        raw = page.get_text("rawdict")
        chars: list[dict[str, Any]] = []
        for block in raw.get("blocks", ()):
            if block.get("type", 0) != 0:
                continue
            for line in block.get("lines", ()):
                for span in line.get("spans", ()):
                    for char in span.get("chars", ()):
                        bbox = char.get("bbox")
                        if not bbox or len(bbox) != 4:
                            continue
                        chars.append(
                            {
                                "text": str(char.get("c") or ""),
                                "x0": float(bbox[0]),
                                "x1": float(bbox[2]),
                                "top": float(bbox[1]),
                                "bottom": float(bbox[3]),
                                "object_type": "char",
                            }
                        )
        return cls(chars, float(page.rect.width))

    def extract_text(self) -> str:
        return "\n".join(
            record["text"]
            for record in self.extract_text_lines(strip=True, return_chars=False)
            if record["text"]
        )

    def extract_text_lines(
        self, *, strip: bool = True, return_chars: bool = False
    ) -> list[dict[str, Any]]:
        records: list[dict[str, Any]] = []
        for line_chars in _group_lines(self.chars):
            text = _join_line_chars(line_chars)
            if strip:
                text = text.strip()
            record: dict[str, Any] = {"text": text}
            if return_chars:
                record["chars"] = list(line_chars)
            records.append(record)
        return records

    def filter(
        self, predicate: Callable[[dict[str, Any]], bool]
    ) -> "_TextPage":
        return _TextPage(
            (char for char in self.chars if predicate(char)), self.width
        )


def _pdf_pages(path):
    try:
        import pymupdf
    except ImportError:
        try:
            from pdfminer.high_level import extract_pages
            from pdfminer.layout import LTChar, LTContainer, LTImage
        except ImportError as exc:
            raise PDFDependencyUnavailable(
                "Install tuomin-gateway[pdf-text] for PDF text extraction, "
                "or review THIRD_PARTY_NOTICES.md before installing tuomin-gateway[pdf]."
            ) from exc
        for page in extract_pages(path):
            chars = []
            has_images = False
            pending = list(reversed(list(page)))
            while pending:
                item = pending.pop()
                if isinstance(item, LTChar):
                    text = item.get_text()
                    if text.startswith("(cid:") and text.endswith(")"):
                        text = "\ufffd"
                    chars.append({"text": text, "x0": item.x0, "x1": item.x1,
                                  "top": page.y1 - item.y1, "bottom": page.y1 - item.y0,
                                  "object_type": "char"})
                elif isinstance(item, LTImage):
                    has_images = True
                elif isinstance(item, LTContainer):
                    pending.extend(reversed(list(item)))
            yield _TextPage(chars, page.width), has_images, "pdfminer-native"
        return

    with pymupdf.open(stream=Path(path).read_bytes(), filetype="pdf") as document:
        for page in document:
            yield _TextPage.from_page(page), bool(page.get_images()), "pymupdf-native"


def extract_pdf(
    path: str | Path,
    source_id: str,
    source_uid: str,
) -> tuple[list[DocumentBlock], list[dict[str, Any]], dict[str, Any]]:
    blocks: list[DocumentBlock] = []
    warnings: list[dict[str, Any]] = []
    two_column_pages = 0
    ocr_pages = 0
    page_count = 0
    try:
        with closing(_pdf_pages(path)) as pages:
            for page_number, (adapter, has_images, method) in enumerate(pages, start=1):
                page_count = page_number
                non_space_chars = sum(
                    1
                    for char in adapter.chars
                    if str(char.get("text") or "").strip()
                )
                needs_ocr = non_space_chars < 10 and has_images
                if needs_ocr:
                    ocr_pages += 1
                    warnings.append(
                        {
                            "code": "PDF_PAGE_NEEDS_OCR",
                            "severity": "warning",
                            "message": f"PDF page {page_number} appears to require OCR",
                            "source_id": source_id,
                            "page": page_number,
                            "requires_manual_review": True,
                            "capability": "ocr",
                            "capability_blocking": False,
                            "details": {"page": page_number},
                        }
                    )

                lines, split = pdf_page_lines_in_reading_order(adapter)
                if split is not None:
                    two_column_pages += 1
                    warnings.append(
                        {
                            "code": "PDF_TWO_COLUMN_REORDERED",
                            "severity": "info",
                            "message": (
                                f"PDF page {page_number} was reordered left column first"
                            ),
                            "source_id": source_id,
                            "page": page_number,
                            "requires_manual_review": False,
                            "capability_blocking": False,
                            "details": {"page": page_number, "column_split": split},
                        }
                    )

                page_line_number = 0
                for raw_line in lines:
                    text = str(raw_line).strip()
                    if not text:
                        continue
                    page_line_number += 1
                    blocks.append(
                        DocumentBlock(
                            source_id=source_id,
                            source_uid=source_uid,
                            block_id=(
                                f"page-{page_number:03d}-line-{page_line_number:04d}"
                            ),
                            order=len(blocks) + 1,
                            block_type="line",
                            text=text,
                            extraction_method=method,
                            legacy_locators=[f"{source_id}#page{page_number:03d}"],
                            metadata={"page": page_number},
                            quality={
                                "level": "high",
                                "requires_manual_review": False,
                            },
                        )
                    )
    except PDFDependencyUnavailable:
        raise
    except Exception as exc:
        raise ValueError("invalid or unreadable PDF") from exc

    if not blocks or (page_count > 0 and ocr_pages == page_count):
        warnings.append(
            {
                "code": "PDF_REQUIRES_OCR",
                "severity": "blocking",
                "capability_blocking": True,
                "message": "扫描件 PDF 需要 OCR，当前不支持",
                "source_id": source_id,
                "requires_manual_review": True,
                "capability": "ocr",
            }
        )

    combined_text = "".join(block.text for block in blocks)
    if _is_garbled(combined_text):
        warnings.append(
            {
                "code": "PDF_GARBLED_TEXT",
                "severity": "warning",
                "message": "PDF text layer contains garbled or invalid characters",
                "source_id": source_id,
                "requires_manual_review": True,
                "capability_blocking": False,
            }
        )

    return blocks, warnings, {
        "pages": page_count,
        "lines": len(blocks),
        "characters": sum(len(block.text) for block in blocks),
        "two_column_pages": two_column_pages,
        "ocr_pages": ocr_pages,
    }


def _group_lines(chars: Iterable[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    usable = [
        char
        for char in chars
        if all(key in char for key in ("x0", "x1", "top", "bottom"))
    ]
    if not usable:
        return []
    heights = [
        max(0.0, float(char["bottom"]) - float(char["top"])) for char in usable
    ]
    positive_heights = [height for height in heights if height > 0]
    median_height = statistics.median(positive_heights) if positive_heights else 1.0
    threshold = max(0.1, median_height / 2.0)
    ordered = sorted(usable, key=lambda char: (float(char["top"]), float(char["x0"])))
    grouped: list[dict[str, Any]] = []
    for char in ordered:
        top = float(char["top"])
        if grouped and abs(top - float(grouped[-1]["top"])) < threshold:
            grouped[-1]["chars"].append(char)
            count = len(grouped[-1]["chars"])
            grouped[-1]["top"] = (
                float(grouped[-1]["top"]) * (count - 1) + top
            ) / count
        else:
            grouped.append({"top": top, "chars": [char]})
    return [
        sorted(group["chars"], key=lambda char: (float(char["x0"]), float(char["top"])))
        for group in sorted(grouped, key=lambda group: float(group["top"]))
    ]


def _join_line_chars(chars: list[dict[str, Any]]) -> str:
    if not chars:
        return ""
    widths = [
        max(0.0, float(char["x1"]) - float(char["x0"]))
        for char in chars
        if str(char.get("text") or "").strip()
    ]
    median_width = statistics.median(widths) if widths else 1.0
    parts: list[str] = []
    previous: dict[str, Any] | None = None
    pending_space = False
    for char in chars:
        text = str(char.get("text") or "")
        if not text:
            continue
        if text.isspace():
            pending_space = True
            continue
        if previous is not None:
            previous_text = str(previous.get("text") or "")
            both_cjk = _is_cjk(previous_text[-1:]) and _is_cjk(text[:1])
            gap = float(char["x0"]) - float(previous["x1"])
            geometric_space = gap > max(1.0, median_width * 0.4)
            if not both_cjk and (pending_space or geometric_space):
                parts.append(" ")
        parts.append(text)
        previous = char
        pending_space = False
    return "".join(parts)


def _is_cjk(value: str) -> bool:
    return bool(value) and "\u4e00" <= value <= "\u9fff"


def _is_garbled(text: str) -> bool:
    if not text:
        return False
    if "\ufffd" in text or any("\ue000" <= char <= "\uf8ff" for char in text):
        return True
    control_count = sum(unicodedata.category(char) == "Cc" for char in text)
    return control_count / len(text) > 0.01
