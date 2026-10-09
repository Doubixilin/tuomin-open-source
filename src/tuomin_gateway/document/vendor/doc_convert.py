"""Legacy .doc → .docx conversion for the extraction pipeline.

The authoritative snapshot always keeps the original .doc bytes; conversion
is extraction-time derived data and the converted file never leaves the
temporary directory the caller provides. macOS uses the built-in textutil;
the seam is intentionally narrow (one convert function + one identity
string) so a LibreOffice backend can be added later without touching
callers.
"""

from __future__ import annotations

import platform
import shutil
import subprocess
import zipfile
from pathlib import Path

CONVERT_TIMEOUT_SECONDS = 120


class DocConversionError(RuntimeError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def doc_converter_available() -> bool:
    return shutil.which("textutil") is not None


def doc_converter_identity() -> str:
    """Stable converter identity for extraction dependency fingerprints."""
    if doc_converter_available():
        mac = platform.mac_ver()[0] or "unknown"
        return f"textutil(macOS {mac})"
    return "unavailable"


def convert_doc_to_docx(source: Path, destination_dir: Path) -> Path:
    """Convert one .doc into destination_dir and return the .docx path.

    Fails closed: any converter absence, failure, or structurally invalid
    output raises DocConversionError with a stable code instead of letting
    an empty or partial document reach the review pipeline.
    """
    textutil = shutil.which("textutil")
    if textutil is None:
        raise DocConversionError("doc_converter_unavailable")
    destination = destination_dir / "converted.docx"
    try:
        completed = subprocess.run(
            [
                textutil,
                "-convert",
                "docx",
                "-output",
                str(destination),
                str(source),
            ],
            capture_output=True,
            timeout=CONVERT_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise DocConversionError("doc_conversion_failed") from exc
    if completed.returncode != 0:
        raise DocConversionError("doc_conversion_failed")
    _assert_docx_structure(destination)
    return destination


def _assert_docx_structure(path: Path) -> None:
    try:
        if not path.is_file() or path.stat().st_size < 1:
            raise DocConversionError("doc_conversion_failed")
        with zipfile.ZipFile(path) as package:
            names = set(package.namelist())
    except (zipfile.BadZipFile, OSError) as exc:
        raise DocConversionError("doc_conversion_failed") from exc
    if "[Content_Types].xml" not in names or "word/document.xml" not in names:
        raise DocConversionError("doc_conversion_failed")
