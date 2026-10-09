"""Tests for PDF extraction (generated PDFs via pymupdf)."""
from __future__ import annotations

import hashlib

import pytest

pymupdf = pytest.importorskip("pymupdf")

from tuomin_gateway.document.extract import extract_document
from tuomin_gateway.document.pdf import extract_pdf
from tuomin_gateway.document.vendor.document_ir import source_uid


def _uid(path) -> str:
    return source_uid(hashlib.sha256(path.read_bytes()).hexdigest())


def _warning_codes(warnings) -> set[str]:
    return {warning["code"] for warning in warnings}


def test_single_column_cjk_lines_are_extracted_in_order(tmp_path) -> None:
    path = tmp_path / "single-column.pdf"
    document = pymupdf.open()
    page = document.new_page()
    page.insert_text((72, 90), "第一行中文内容", fontname="china-s")
    page.insert_text((72, 120), "第二行中文内容", fontname="china-s")
    document.save(str(path))
    document.close()

    blocks, warnings, stats = extract_pdf(path, "job-cjk", _uid(path))

    assert [block.text for block in blocks] == ["第一行中文内容", "第二行中文内容"]
    assert [block.order for block in blocks] == [1, 2]
    assert blocks[0].block_id == "page-001-line-0001"
    assert blocks[0].legacy_locators == ["job-cjk#page001"]
    assert blocks[0].extraction_method == "pymupdf-native"
    assert stats == {
        "pages": 1,
        "lines": 2,
        "characters": len("第一行中文内容第二行中文内容"),
        "two_column_pages": 0,
        "ocr_pages": 0,
    }
    assert "PDF_REQUIRES_OCR" not in _warning_codes(warnings)


def test_bilingual_two_column_page_is_reordered(tmp_path) -> None:
    path = tmp_path / "two-column.pdf"
    left_lines = ["这是左栏第一段中文内容", "这是左栏第二段中文内容"]
    right_lines = [
        "This is the first paragraph in the right column.",
        "This is the second paragraph in the right column.",
    ]
    document = pymupdf.open()
    page = document.new_page()
    for index, text in enumerate(left_lines):
        page.insert_text((55, 90 + index * 35), text, fontname="china-s")
    for index, text in enumerate(right_lines):
        page.insert_text((330, 90 + index * 35), text)
    document.save(str(path))
    document.close()

    blocks, warnings, stats = extract_pdf(path, "job-columns", _uid(path))
    texts = [block.text for block in blocks]

    assert texts == left_lines + right_lines
    assert "PDF_TWO_COLUMN_REORDERED" in _warning_codes(warnings)
    assert stats["two_column_pages"] == 1


def test_image_only_page_requires_ocr(tmp_path) -> None:
    path = tmp_path / "scan.pdf"
    document = pymupdf.open()
    page = document.new_page()
    pixmap = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 8, 8), False)
    pixmap.clear_with(220)
    page.insert_image(page.rect, pixmap=pixmap)
    document.save(str(path))
    document.close()

    blocks, warnings, stats = extract_pdf(path, "job-scan", _uid(path))

    assert blocks == []
    assert stats["ocr_pages"] == 1
    assert "PDF_PAGE_NEEDS_OCR" in _warning_codes(warnings)
    blocking = next(
        warning for warning in warnings if warning["code"] == "PDF_REQUIRES_OCR"
    )
    assert blocking["severity"] == "blocking"
    assert blocking["capability_blocking"] is True
    assert blocking["message"] == "扫描件 PDF 需要 OCR，当前不支持"


def test_control_character_heavy_page_is_flagged_as_garbled(tmp_path) -> None:
    path = tmp_path / "garbled.pdf"
    document = pymupdf.open()
    page = document.new_page()
    page.insert_text((72, 90), "A" + "\x01" * 20 + "B")
    document.save(str(path))
    document.close()

    blocks, warnings, _stats = extract_pdf(path, "job-garbled", _uid(path))

    assert blocks
    assert "PDF_GARBLED_TEXT" in _warning_codes(warnings)


def test_extract_document_routes_pdf_end_to_end(tmp_path) -> None:
    path = tmp_path / "routed.pdf"
    document = pymupdf.open()
    page = document.new_page()
    page.insert_text((72, 90), "Native PDF text")
    document.save(str(path))
    document.close()

    result = extract_document(path, "job-x1")

    assert result.file_format == "pdf"
    assert [block.text for block in result.blocks] == ["Native PDF text"]
    assert result.blocks[0].source_id == "job-x1"
    assert result.blocks[0].source_uid == result.source_uid
    assert result.stats["pages"] == 1
