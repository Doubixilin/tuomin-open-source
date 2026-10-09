"""Tests for the unified document extraction entry (generated files)."""

from __future__ import annotations

import hashlib
import platform
import zipfile

import pytest

from tuomin_gateway.document.extract import (
    UnsupportedFormatError,
    extract_document,
)
from tuomin_gateway.document.vendor.document_ir import source_uid as vendored_source_uid


def _write_minimal_docx(path) -> None:
    content_types = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="xml" ContentType="application/xml"/>
  <Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>
</Types>
"""
    document = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
  <w:body>
    <w:p><w:r><w:t>First paragraph</w:t></w:r></w:p>
    <w:p><w:r><w:t>Second paragraph</w:t></w:r></w:p>
    <w:sectPr/>
  </w:body>
</w:document>
"""
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as package:
        package.writestr("[Content_Types].xml", content_types)
        package.writestr("word/document.xml", document)


def test_txt_round_trip_identity_and_locators(tmp_path) -> None:
    path = tmp_path / "notes.txt"
    path.write_text("第一行\n\nsecond line\n", encoding="utf-8")

    result = extract_document(path, "job-text")
    expected_hash = hashlib.sha256(path.read_bytes()).hexdigest()

    assert [block.text for block in result.blocks] == ["第一行", "second line"]
    assert all(
        locator.startswith("job-text#")
        for block in result.blocks
        for locator in block.legacy_locators
    )
    assert result.file_format == "txt"
    assert result.file_sha256 == expected_hash
    assert result.source_uid == vendored_source_uid(expected_hash)
    assert "file_sha256" not in result.stats
    assert "file_format" not in result.stats


def test_markdown_dispatches_to_text_extractor(tmp_path) -> None:
    path = tmp_path / "notes.md"
    path.write_text("# Heading\nBody\n", encoding="utf-8")

    result = extract_document(path, "job-md")

    assert result.file_format == "md"
    assert [block.text for block in result.blocks] == ["# Heading", "Body"]
    assert {block.extraction_method for block in result.blocks} == {"utf8-text"}


def test_generated_docx_extracts_paragraphs(tmp_path) -> None:
    path = tmp_path / "contract.docx"
    _write_minimal_docx(path)

    result = extract_document(path, "job-docx")

    paragraphs = [
        block.text for block in result.blocks if block.block_type == "paragraph"
    ]
    assert paragraphs == ["First paragraph", "Second paragraph"]
    assert result.file_format == "docx"
    assert result.stats["paragraph_count"] == 2
    assert all(block.source_id == "job-docx" for block in result.blocks)


def test_generated_xlsx_preserves_sheet_merge_and_hidden_column(tmp_path) -> None:
    openpyxl = pytest.importorskip("openpyxl")
    path = tmp_path / "workbook.xlsx"
    workbook = openpyxl.Workbook()
    sheet = workbook.active
    sheet.title = "Review"
    sheet["A1"] = "Merged heading"
    sheet.merge_cells("A1:B1")
    sheet["C2"] = "Hidden content"
    sheet.column_dimensions["C"].hidden = True
    workbook.save(path)
    workbook.close()

    result = extract_document(path, "job-sheet")

    assert result.file_format == "xlsx"
    assert any(block.block_type == "spreadsheet_sheet" for block in result.blocks)
    hidden_cell = next(
        block for block in result.blocks if block.block_id == "sheet001-cell-C2"
    )
    assert hidden_cell.metadata["hidden_column"] is True
    sheet_block = next(
        block for block in result.blocks if block.block_type == "spreadsheet_sheet"
    )
    assert sheet_block.metadata["merged_ranges"] == ["A1:B1"]
    assert result.stats["hidden_column_count"] == 1
    assert any(
        warning["code"] == "SPREADSHEET_HIDDEN_COLUMNS"
        for warning in result.warnings
    )


@pytest.mark.parametrize("suffix", [".zip"])
def test_unsupported_formats_raise(tmp_path, suffix) -> None:
    path = tmp_path / f"document{suffix}"
    path.write_bytes(b"not a supported document")

    with pytest.raises(UnsupportedFormatError) as captured:
        extract_document(path, "job-unsupported")

    assert "supported extensions" in str(captured.value)
    assert ".docx" in str(captured.value)


def test_missing_file_is_not_masked(tmp_path) -> None:
    with pytest.raises(FileNotFoundError):
        extract_document(tmp_path / "missing.txt", "job-missing")


def test_legacy_doc_is_rejected_or_converted_on_macos(tmp_path) -> None:
    path = tmp_path / "legacy.doc"
    path.write_text("Legacy document text", encoding="utf-8")

    if platform.system() != "Darwin":
        with pytest.raises(UnsupportedFormatError, match="macOS textutil"):
            extract_document(path, "job-doc")
        return

    try:
        result = extract_document(path, "job-doc")
    except UnsupportedFormatError as exc:
        assert "conversion failed" in str(exc)
    else:
        assert result.file_format == "doc"
        assert any(warning["code"] == "DOC_CONVERTED" for warning in result.warnings)
        assert result.source_uid == vendored_source_uid(result.file_sha256)
