"""Convert legacy binary Office files (.doc / .xls) to OOXML for the workbench.

The binary formats cannot be patched in place safely (text offsets drive the
formatting tables), so original-format redaction converts them first and the
rest of the pipeline treats the result as a normal .docx / .xlsx. The output
is therefore the modern format, and the UI says so.

Converters, in order:
- LibreOffice ``soffice --headless --convert-to`` (macOS app bundle, Homebrew or
  any Linux install): handles both .doc and .xls, so it is the primary and the
  only option on a Linux server.
- macOS ``textutil`` for .doc only (always present on macOS).

Everything fails closed: no converter, a non-zero exit, a timeout or a result
that is not a well-formed package raises :class:`LegacyConversionError`.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import zipfile
from dataclasses import dataclass
from pathlib import Path

CONVERT_TIMEOUT_SECONDS = 180
LEGACY_TARGETS = {"doc": "docx", "xls": "xlsx"}
_SOFFICE_CANDIDATES = (
    "/Applications/LibreOffice.app/Contents/MacOS/soffice",
    "/opt/homebrew/bin/soffice",
    "/usr/local/bin/soffice",
    "/usr/bin/soffice",
    "/usr/bin/libreoffice",
    "/snap/bin/libreoffice",
)


class LegacyConversionError(ValueError):
    def __init__(self, code: str, detail: str = "") -> None:
        self.code = code
        super().__init__(f"{code}: {detail}" if detail else code)


@dataclass(frozen=True)
class ConvertedDocument:
    data: bytes
    file_format: str  # "docx" | "xlsx"
    converter: str  # "soffice" | "textutil"


def find_soffice() -> str | None:
    override = os.environ.get("TUOMIN_SOFFICE")
    if override and Path(override).is_file():
        return override
    found = shutil.which("soffice") or shutil.which("libreoffice")
    if found:
        return found
    if os.name == "nt":
        for variable in ("ProgramW6432", "ProgramFiles", "ProgramFiles(x86)", "LOCALAPPDATA"):
            base = os.environ.get(variable)
            if base:
                candidate = Path(base) / "LibreOffice" / "program" / "soffice.exe"
                if candidate.is_file():
                    return str(candidate)
    for candidate in _SOFFICE_CANDIDATES:
        if Path(candidate).is_file():
            return candidate
    return None


def available_converters(source_format: str) -> list[str]:
    names: list[str] = []
    if find_soffice():
        names.append("soffice")
    if source_format == "doc" and shutil.which("textutil"):
        names.append("textutil")
    return names


def _validate_package(path: Path, target: str) -> bytes:
    try:
        data = path.read_bytes()
        with zipfile.ZipFile(path) as package:
            names = set(package.namelist())
    except (OSError, zipfile.BadZipFile) as exc:
        raise LegacyConversionError("conversion_failed", "转换结果不是有效的 Office 文件") from exc
    main = "word/document.xml" if target == "docx" else "xl/workbook.xml"
    if "[Content_Types].xml" not in names or main not in names or not data:
        raise LegacyConversionError("conversion_failed", "转换结果缺少必要部件")
    return data


def _run(command: list[str], *, env: dict | None = None) -> None:
    try:
        completed = subprocess.run(
            command, capture_output=True, timeout=CONVERT_TIMEOUT_SECONDS, check=False,
            env=env, stdin=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise LegacyConversionError("conversion_failed", "转换程序未能完成") from exc
    if completed.returncode != 0:
        raise LegacyConversionError("conversion_failed", "转换程序返回错误")


def convert_legacy(data: bytes, source_format: str) -> ConvertedDocument:
    """Convert ``data`` (a .doc or .xls) and return the modern-format bytes."""
    target = LEGACY_TARGETS.get(source_format)
    if target is None:
        raise LegacyConversionError("unsupported_legacy_format", "只支持 .doc 和 .xls")
    converters = available_converters(source_format)
    if not converters:
        raise LegacyConversionError(
            "converter_unavailable",
            "本机没有可用的转换程序：请安装 LibreOffice，或先用 Office 另存为 ." + target,
        )
    errors: list[str] = []
    with tempfile.TemporaryDirectory(prefix="tuomin-legacy-") as temporary:
        work = Path(temporary)
        source = work / f"input.{source_format}"
        source.write_bytes(data)
        for converter in converters:
            try:
                if converter == "soffice":
                    profile = work / "lo-profile"  # isolated profile: no first-run dialogs, no user state
                    env = {**os.environ, "HOME": str(work)}
                    _run([
                        find_soffice(), "--headless", "--norestore", "--nologo",
                        f"-env:UserInstallation={profile.as_uri()}",
                        "--convert-to", target, "--outdir", str(work), str(source),
                    ], env=env)
                    produced = work / f"input.{target}"
                else:
                    produced = work / f"converted.{target}"
                    _run([shutil.which("textutil"), "-convert", target, "-output", str(produced), str(source)])
                return ConvertedDocument(_validate_package(produced, target), target, converter)
            except LegacyConversionError as exc:
                errors.append(f"{converter}: {exc.code}")
    raise LegacyConversionError("conversion_failed", "；".join(errors) or "转换失败")
