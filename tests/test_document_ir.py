"""Tests for the tuomin Document IR facade and source-id generalization."""

from __future__ import annotations

import json

import pytest

from tuomin_gateway.document.ir import (
    DocumentBlock,
    resolve_archive_locator,
    resolve_locator,
    write_document_ir,
)
from tuomin_gateway.document.vendor.document_ir import (
    DocumentBlock as VendoredDocumentBlock,
)
from tuomin_gateway.document.vendor.spreadsheet import extract_spreadsheet

_SOURCE_UID = "sha256:" + "a" * 64


def test_facade_reexports_vendored_document_block() -> None:
    assert DocumentBlock is VendoredDocumentBlock


def _write_workbook(path) -> None:
    openpyxl = pytest.importorskip("openpyxl")
    workbook = openpyxl.Workbook()
    workbook.active["A1"] = "value"
    workbook.save(path)
    workbook.close()


@pytest.mark.parametrize("source_id", ["M001", "job-2026-0821-a1b2"])
def test_extract_spreadsheet_accepts_legacy_and_job_source_ids(
    tmp_path, source_id
) -> None:
    workbook_path = tmp_path / "source.xlsx"
    _write_workbook(workbook_path)

    blocks, warnings, stats = extract_spreadsheet(
        workbook_path, source_id, _SOURCE_UID
    )

    assert blocks
    assert all(block.source_id == source_id for block in blocks)
    assert warnings == []
    assert stats["sheet_count"] == 1


@pytest.mark.parametrize(
    "source_id",
    ["", "has space", "-leading", "a" * 65],
)
def test_extract_spreadsheet_rejects_invalid_source_ids(
    tmp_path, source_id
) -> None:
    workbook_path = tmp_path / "source.xlsx"
    _write_workbook(workbook_path)

    with pytest.raises(ValueError, match="source_id"):
        extract_spreadsheet(workbook_path, source_id, _SOURCE_UID)


@pytest.mark.parametrize("source_id", ["M001", "job-2026-0821-a1b2"])
def test_locator_resolution_preserves_full_source_id(tmp_path, source_id) -> None:
    locator = f"{source_id}#page003"
    block = DocumentBlock(
        source_id=source_id,
        source_uid=_SOURCE_UID,
        block_id="page003",
        order=1,
        block_type="page",
        text="page text",
        extraction_method="test",
        legacy_locators=[locator],
    )
    block_dict = block.to_dict()

    direct = resolve_locator(locator, source_id=source_id, blocks=[block_dict])
    assert direct.resolved
    assert direct.matched_block_ids == ("page003",)

    mismatch = resolve_locator(locator, source_id="other-source", blocks=[block_dict])
    assert mismatch.errors == ("locator source mismatch",)

    archive = tmp_path / source_id
    archive.mkdir()
    write_document_ir(archive / "document.ir.jsonl", [block])
    (archive / "material-extraction-manifest.json").write_text(
        json.dumps(
            {
                "records": [
                    {
                        "source_id": source_id,
                        "document_ir": "document.ir.jsonl",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    archived = resolve_archive_locator(archive, locator)
    assert archived.resolved
    assert archived.matched_block_ids == ("page003",)
