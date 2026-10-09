"""Unified document extraction entry: path -> (blocks, warnings, stats)."""
from __future__ import annotations

import hashlib
import platform
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .pdf import PDFDependencyUnavailable, extract_pdf
from .vendor.doc_convert import DocConversionError, convert_doc_to_docx
from .vendor.docx import extract_docx
from .vendor.document_ir import DocumentBlock, source_uid as make_source_uid
from .vendor.spreadsheet import extract_spreadsheet
from .vendor.text import extract_text

_TEXT_EXTENSIONS = frozenset({".txt", ".md", ".csv", ".json", ".yaml", ".yml"})
_SPREADSHEET_EXTENSIONS = frozenset({".xlsx", ".xlsm", ".xls"})
_SUPPORTED_EXTENSIONS = tuple(
    sorted(_TEXT_EXTENSIONS | _SPREADSHEET_EXTENSIONS | {".doc", ".docx", ".pdf"})
)
_HASH_CHUNK_BYTES = 1024 * 1024


@dataclass(frozen=True)
class ExtractionResult:
    blocks: list[DocumentBlock]
    warnings: list[dict[str, Any]]
    stats: dict[str, Any]
    file_format: str
    file_sha256: str
    source_uid: str


class UnsupportedFormatError(ValueError):
    """Raised when no supported extraction path exists for a file format."""


def extract_document(path: str | Path, job_id: str) -> ExtractionResult:
    source = Path(path)
    file_sha256 = _file_sha256(source)
    uid = make_source_uid(file_sha256)
    extension = source.suffix.lower()
    file_format = extension.removeprefix(".")

    if extension in _TEXT_EXTENSIONS:
        blocks, warnings, stats = extract_text(source, job_id, uid)
    elif extension == ".docx":
        blocks, warnings, stats = extract_docx(source, job_id, uid)
    elif extension == ".doc":
        if platform.system() != "Darwin":
            raise UnsupportedFormatError(
                "Legacy .doc extraction requires macOS textutil conversion"
            )
        try:
            with tempfile.TemporaryDirectory(prefix="tuomin-doc-") as temporary:
                converted = convert_doc_to_docx(source, Path(temporary))
                blocks, warnings, stats = extract_docx(converted, job_id, uid)
        except DocConversionError as exc:
            raise UnsupportedFormatError(
                f"Legacy .doc conversion failed: {exc.code}"
            ) from exc
        warnings = [
            *warnings,
            {
                "code": "DOC_CONVERTED",
                "severity": "warning",
                "message": (
                    "Legacy .doc content was converted to a temporary DOCX with "
                    "macOS textutil before extraction"
                ),
                "requires_manual_review": True,
            },
        ]
    elif extension in _SPREADSHEET_EXTENSIONS:
        blocks, warnings, stats = extract_spreadsheet(source, job_id, uid)
        hidden_columns = int(stats.get("hidden_column_count", 0) or 0)
        if hidden_columns:
            warnings = [
                *warnings,
                {
                    "code": "SPREADSHEET_HIDDEN_COLUMNS",
                    "severity": "warning",
                    "message": (
                        f"{hidden_columns} hidden spreadsheet column(s) require review"
                    ),
                    "source_id": job_id,
                    "requires_manual_review": True,
                    "capability": "hidden_content",
                    "capability_blocking": False,
                    "details": {"hidden_column_count": hidden_columns},
                },
            ]
    elif extension == ".pdf":
        try:
            blocks, warnings, stats = extract_pdf(source, job_id, uid)
        except PDFDependencyUnavailable as exc:
            raise UnsupportedFormatError(str(exc)) from exc
    else:
        label = extension or "<none>"
        supported = ", ".join(_SUPPORTED_EXTENSIONS)
        raise UnsupportedFormatError(
            f"Unsupported document format {label!r}; supported extensions: {supported}"
        )

    return ExtractionResult(
        blocks=blocks,
        warnings=warnings,
        stats=stats,
        file_format=file_format,
        file_sha256=file_sha256,
        source_uid=uid,
    )


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(_HASH_CHUNK_BYTES), b""):
            digest.update(chunk)
    return digest.hexdigest()
