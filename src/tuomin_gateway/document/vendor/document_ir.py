from __future__ import annotations

import hashlib
import importlib.metadata
import json
import os
import re
import stat
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping

from .path_guard import ensure_under

DOCUMENT_IR_SCHEMA_VERSION = "1.0"
EXTRACTOR_VERSION = "2.1.0"
# Formula-heavy investment workbooks legitimately produce large, locally
# generated IR even when the source XLSX itself is only a few megabytes. Keep a
# finite read bound, but size it for those real workbooks rather than documents
# alone.
MAX_DOCUMENT_IR_BYTES = 64_000_000

_SOURCE_PREFIX = re.compile(r"^M\d{3}#")
_SOURCE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}")
_PAGE_RANGE = re.compile(r"^page(?P<start>\d{3})(?:-(?P<end>\d{3}))?$")
_PARAGRAPH = re.compile(r"^paragraph\d{4}$")
_TABLE = re.compile(r"^table\d{3}(?:/row\d{4})?(?:/cell[A-Z]+\d+)?$")
_SHEET_CELL = re.compile(r"^sheet\[(?P<sheet>.+)](?:!(?P<cell>[A-Z]+\d+))?$")


@dataclass(frozen=True)
class LocatorResolution:
    locator: str
    matched_block_ids: tuple[str, ...] = ()
    errors: tuple[str, ...] = ()

    @property
    def resolved(self) -> bool:
        return bool(self.matched_block_ids) and not self.errors


@dataclass(frozen=True)
class ManifestBoundFileSnapshot:
    path: Path
    content: bytes
    sha256: str


class ManifestBoundFileError(ValueError):
    """Stable rejection raised before an unsafe manifest target is read."""

    def __init__(self, stable_code: str = "manifest_bound_file_unsafe"):
        self.stable_code = stable_code
        super().__init__(stable_code)


@dataclass
class DocumentBlock:
    source_id: str
    source_uid: str
    block_id: str
    order: int
    block_type: str
    text: str
    extraction_method: str
    legacy_locators: list[str]
    metadata: dict[str, Any] = field(default_factory=dict)
    quality: dict[str, Any] = field(default_factory=dict)

    def canonical_locator(self) -> str:
        return f"evidence://{self.source_uid}/{self.block_id}"

    def text_sha256(self) -> str:
        return hashlib.sha256(self.text.encode("utf-8")).hexdigest()

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": DOCUMENT_IR_SCHEMA_VERSION,
            "source_id": self.source_id,
            "source_uid": self.source_uid,
            "block_id": self.block_id,
            "order": self.order,
            "block_type": self.block_type,
            "text": self.text,
            "text_sha256": self.text_sha256(),
            "canonical_locator": self.canonical_locator(),
            "legacy_locators": list(self.legacy_locators),
            "extraction_method": self.extraction_method,
            "metadata": self.metadata,
            "quality": self.quality,
        }


def source_uid(source_sha256: str) -> str:
    digest = source_sha256.strip().lower()
    if not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise ValueError("source sha256 must be 64 lowercase hex characters")
    return f"sha256:{digest}"


def extraction_cache_key(
    *,
    source_sha256: str,
    extension: str,
    config: Mapping[str, Any],
    extractor_version: str = EXTRACTOR_VERSION,
    dependency_versions: Mapping[str, str] | None = None,
) -> str:
    resolved_dependencies = (
        dict(dependency_versions)
        if dependency_versions is not None
        else extraction_dependency_versions(extension)
    )
    payload = {
        "source_sha256": source_sha256.lower(),
        "extension": extension.lower(),
        "extractor_version": extractor_version,
        "config": config,
        "dependency_versions": resolved_dependencies,
    }
    compact = json.dumps(
        payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True
    )
    return hashlib.sha256(compact.encode("utf-8")).hexdigest()


def extraction_dependency_versions(extension: str) -> dict[str, str]:
    """Versions that can change Extraction v2 output for one format."""
    normalized = extension.lower()
    distributions: list[str] = []
    if normalized == ".pdf":
        distributions = ["PyMuPDF", "PaddleOCR", "paddlepaddle", "onnxruntime"]
    elif normalized in {".xlsx", ".xlsm"}:
        distributions = ["openpyxl"]
    elif normalized == ".xls":
        distributions = ["xlrd"]
    versions = {"python": ".".join(map(str, sys.version_info[:3]))}
    for distribution in distributions:
        try:
            versions[distribution] = importlib.metadata.version(distribution)
        except importlib.metadata.PackageNotFoundError:
            versions[distribution] = "not-installed"
    if normalized == ".doc":
        from .doc_convert import (
            doc_converter_identity,
        )

        versions["doc_converter"] = doc_converter_identity()
    return dict(sorted(versions.items()))


def dependency_fingerprint(versions: Mapping[str, str]) -> str:
    compact = json.dumps(
        dict(versions), ensure_ascii=False, separators=(",", ":"), sort_keys=True
    )
    return hashlib.sha256(compact.encode("utf-8")).hexdigest()


def write_document_ir(path: str | Path, blocks: Iterable[DocumentBlock]) -> dict[str, Any]:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    serialized: list[str] = []
    seen_ids: set[str] = set()
    previous_order = 0
    source_ids: set[str] = set()
    source_uids: set[str] = set()
    for block in blocks:
        if block.block_id in seen_ids:
            raise ValueError(f"duplicate document block id: {block.block_id}")
        if block.order <= previous_order:
            raise ValueError("document block order must be strictly increasing")
        if not block.text.strip() and block.block_type not in {"image", "separator"}:
            raise ValueError(f"document block {block.block_id} has empty text")
        seen_ids.add(block.block_id)
        previous_order = block.order
        source_ids.add(block.source_id)
        source_uids.add(block.source_uid)
        serialized.append(
            json.dumps(block.to_dict(), ensure_ascii=False, sort_keys=True)
        )
    if len(source_ids) != 1 or len(source_uids) != 1:
        raise ValueError("one document IR file must contain exactly one source")
    content = "\n".join(serialized) + ("\n" if serialized else "")
    encoded = content.encode("utf-8")
    target.write_bytes(encoded)
    return {
        "block_count": len(serialized),
        "sha256": hashlib.sha256(encoded).hexdigest(),
        "bytes": len(encoded),
    }


def load_document_ir(path: str | Path) -> list[dict[str, Any]]:
    source = Path(path)
    return _parse_document_ir_text(source.read_text(encoding="utf-8"))


def load_manifest_document_ir(
    archive_path: str | Path,
    relative_path: str | Path,
    *,
    expected_sha256: str = "",
) -> list[dict[str, Any]]:
    """Load one manifest-selected IR through component-wise no-follow checks."""

    snapshot = read_manifest_bound_file(archive_path, relative_path)
    if expected_sha256 and snapshot.sha256 != expected_sha256:
        raise ValueError("source document IR hash mismatch")
    try:
        text = snapshot.content.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise ValueError("document IR is invalid UTF-8") from exc
    if "\x00" in text:
        raise ValueError("document IR contains NUL")
    return _parse_document_ir_text(text)


def read_manifest_bound_file(
    archive_path: str | Path,
    relative_path: str | Path,
) -> ManifestBoundFileSnapshot:
    """Read one archive-relative regular file without following any link.

    Only the named path is checked. No recursive archive scan is performed.
    """

    raw_archive = Path(archive_path).expanduser()
    relative = Path(relative_path)
    if relative.is_absolute() or not relative.parts or any(
        part in {"", ".", ".."} for part in relative.parts
    ):
        raise ManifestBoundFileError("manifest_bound_path_invalid")
    try:
        archive_initial = raw_archive.lstat()
    except OSError as exc:
        raise ManifestBoundFileError("manifest_bound_archive_unavailable") from exc
    if not _direct_directory(archive_initial):
        raise ManifestBoundFileError("manifest_bound_archive_unsafe")
    try:
        archive = raw_archive.resolve(strict=True)
        archive_final = archive.lstat()
    except OSError as exc:
        raise ManifestBoundFileError("manifest_bound_archive_unavailable") from exc
    if not _same_entry(archive_initial, archive_final):
        raise ManifestBoundFileError("manifest_bound_path_changed")

    component_stats: list[tuple[Path, os.stat_result]] = []
    current = archive
    for index, part in enumerate(relative.parts):
        current = current / part
        try:
            value = current.lstat()
        except OSError as exc:
            raise ManifestBoundFileError("manifest_bound_file_unavailable") from exc
        is_leaf = index == len(relative.parts) - 1
        if (is_leaf and not _direct_regular(value)) or (
            not is_leaf and not _direct_directory(value)
        ):
            raise ManifestBoundFileError("manifest_bound_file_unsafe")
        component_stats.append((current, value))
    leaf, leaf_initial = component_stats[-1]
    if leaf_initial.st_size > MAX_DOCUMENT_IR_BYTES:
        raise ManifestBoundFileError("manifest_bound_file_too_large")
    try:
        resolved = leaf.resolve(strict=True)
        resolved.relative_to(archive)
        resolved_stat = resolved.lstat()
    except (OSError, ValueError) as exc:
        raise ManifestBoundFileError("manifest_bound_path_escape") from exc
    if not _same_entry(leaf_initial, resolved_stat):
        raise ManifestBoundFileError("manifest_bound_path_changed")

    fd: int | None = None
    try:
        fd = os.open(leaf, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        opened = os.fstat(fd)
        if not _direct_regular(opened) or not _same_entry(leaf_initial, opened):
            raise ManifestBoundFileError("manifest_bound_path_changed")
        remaining = MAX_DOCUMENT_IR_BYTES + 1
        chunks: list[bytes] = []
        while remaining:
            chunk = os.read(fd, min(1024 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        content = b"".join(chunks)
    except ManifestBoundFileError:
        raise
    except OSError as exc:
        raise ManifestBoundFileError("manifest_bound_path_changed") from exc
    finally:
        if fd is not None:
            os.close(fd)
    if len(content) > MAX_DOCUMENT_IR_BYTES or len(content) != opened.st_size:
        raise ManifestBoundFileError("manifest_bound_path_changed")
    for path, initial in [(archive, archive_initial), *component_stats]:
        try:
            final = path.lstat()
        except OSError as exc:
            raise ManifestBoundFileError("manifest_bound_path_changed") from exc
        if not _same_entry(initial, final):
            raise ManifestBoundFileError("manifest_bound_path_changed")
    return ManifestBoundFileSnapshot(
        path=resolved,
        content=content,
        sha256=hashlib.sha256(content).hexdigest(),
    )


def _parse_document_ir_text(text_value: str) -> list[dict[str, Any]]:
    blocks: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    previous_order = 0
    # Split on LF only: splitlines() would also break on U+0085/U+2028/U+2029
    # and control separators that legally appear inside cell text values.
    for line_no, line in enumerate(text_value.split("\n"), 1):
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"document IR line {line_no} is invalid JSON") from exc
        if not isinstance(value, dict):
            raise ValueError(f"document IR line {line_no} must be an object")
        if value.get("schema_version") != DOCUMENT_IR_SCHEMA_VERSION:
            raise ValueError(f"document IR line {line_no} has unsupported schema")
        block_id = str(value.get("block_id", ""))
        order = value.get("order")
        text = str(value.get("text", ""))
        if not block_id or block_id in seen_ids:
            raise ValueError(f"document IR line {line_no} has invalid block_id")
        if not isinstance(order, int) or order <= previous_order:
            raise ValueError(f"document IR line {line_no} has invalid order")
        expected_text_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
        if value.get("text_sha256") != expected_text_hash:
            raise ValueError(f"document IR line {line_no} text hash mismatch")
        seen_ids.add(block_id)
        previous_order = order
        blocks.append(value)
    if not blocks:
        raise ValueError("document IR has no blocks")
    return blocks


def _reparse(value: os.stat_result) -> bool:
    return bool(getattr(value, "st_file_attributes", 0) & 0x400)


def _direct_directory(value: os.stat_result) -> bool:
    return bool(
        stat.S_ISDIR(value.st_mode)
        and not stat.S_ISLNK(value.st_mode)
        and not _reparse(value)
    )


def _direct_regular(value: os.stat_result) -> bool:
    return bool(
        stat.S_ISREG(value.st_mode)
        and not stat.S_ISLNK(value.st_mode)
        and not _reparse(value)
    )


def _same_entry(first: os.stat_result, second: os.stat_result) -> bool:
    return bool(
        first.st_dev == second.st_dev
        and first.st_ino == second.st_ino
        and stat.S_IFMT(first.st_mode) == stat.S_IFMT(second.st_mode)
        and first.st_size == second.st_size
    )


def resolve_locator(
    locator: str,
    *,
    source_id: str,
    blocks: Iterable[Mapping[str, Any]],
) -> LocatorResolution:
    raw = locator.strip()
    if not raw:
        return LocatorResolution(locator=locator, errors=("locator is empty",))
    canonical = {
        str(block.get("canonical_locator", "")): str(block.get("block_id", ""))
        for block in blocks
    }
    if raw.startswith("evidence://"):
        block_id = canonical.get(raw)
        if block_id:
            return LocatorResolution(locator=locator, matched_block_ids=(block_id,))
        return LocatorResolution(locator=locator, errors=("canonical locator not found",))

    expected_prefix = f"{source_id}#"
    if not raw.startswith(expected_prefix):
        found = _SOURCE_ID.match(raw)
        if found and found.group(0) != source_id:
            return LocatorResolution(locator=locator, errors=("locator source mismatch",))
        return LocatorResolution(locator=locator, errors=("locator prefix is invalid",))
    anchor_text = raw[len(expected_prefix) :]
    anchors = [item.strip() for item in anchor_text.split(",") if item.strip()]
    if not anchors:
        return LocatorResolution(locator=locator, errors=("locator has no anchor",))

    legacy_map: dict[str, str] = {}
    for block in blocks:
        block_id = str(block.get("block_id", ""))
        for item in block.get("legacy_locators", []):
            value = str(item)
            if value.startswith(expected_prefix):
                legacy_map[value[len(expected_prefix) :]] = block_id

    matched: list[str] = []
    errors: list[str] = []
    for anchor in anchors:
        range_match = _PAGE_RANGE.fullmatch(anchor)
        if range_match and range_match.group("end"):
            start = int(range_match.group("start"))
            end = int(range_match.group("end"))
            if end < start:
                errors.append(f"invalid page range: {anchor}")
                continue
            range_matches = [
                legacy_map[f"page{page:03d}"]
                for page in range(start, end + 1)
                if f"page{page:03d}" in legacy_map
            ]
            if len(range_matches) != end - start + 1:
                errors.append(f"page range is not fully present: {anchor}")
            else:
                matched.extend(range_matches)
            continue
        if not _valid_anchor_syntax(anchor):
            errors.append(f"unsupported locator anchor: {anchor}")
            continue
        block_id = legacy_map.get(anchor)
        if block_id is None:
            errors.append(f"locator anchor not found: {anchor}")
        else:
            matched.append(block_id)
    unique_matches = tuple(dict.fromkeys(matched))
    return LocatorResolution(
        locator=locator,
        matched_block_ids=unique_matches,
        errors=tuple(errors),
    )


def resolve_archive_locator(
    archive_path: str | Path,
    locator: str,
) -> LocatorResolution:
    archive = Path(archive_path).resolve()
    manifest_path = ensure_under(archive, archive / "material-extraction-manifest.json")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return LocatorResolution(locator=locator, errors=(f"extraction manifest invalid: {type(exc).__name__}",))
    match = _SOURCE_ID.match(locator.strip())
    if match is None:
        return LocatorResolution(locator=locator, errors=("locator source id is missing",))
    wanted = match.group(0)
    records = manifest.get("records") if isinstance(manifest, dict) else None
    if not isinstance(records, list):
        return LocatorResolution(locator=locator, errors=("extraction manifest records missing",))
    record = next(
        (item for item in records if isinstance(item, dict) and item.get("source_id") == wanted),
        None,
    )
    if record is None:
        return LocatorResolution(locator=locator, errors=("locator source is unknown",))
    ir_rel = str(record.get("document_ir", ""))
    if not ir_rel:
        return LocatorResolution(locator=locator, errors=("source has no document IR",))
    try:
        blocks = load_manifest_document_ir(archive, ir_rel)
    except (OSError, ValueError) as exc:
        return LocatorResolution(locator=locator, errors=(f"document IR invalid: {type(exc).__name__}",))
    return resolve_locator(locator, source_id=wanted, blocks=blocks)


def validate_cached_extraction(
    archive_path: str | Path,
    snapshot: Mapping[str, Any],
    extraction_manifest: Mapping[str, Any],
    *,
    expected_config: Mapping[str, Any] | None = None,
) -> tuple[bool, list[str]]:
    archive = Path(archive_path).resolve()
    errors: list[str] = []
    if extraction_manifest.get("schema_version") != "2.0":
        errors.append("extraction manifest schema is not 2.0")
    if extraction_manifest.get("extractor_version") != EXTRACTOR_VERSION:
        errors.append("extractor version changed")
    if (
        expected_config is not None
        and extraction_manifest.get("extraction_config") != dict(expected_config)
    ):
        errors.append("extractor configuration changed")
    records = extraction_manifest.get("records")
    files = snapshot.get("files")
    if not isinstance(records, list) or not isinstance(files, list):
        return False, errors + ["snapshot/manifest records are invalid"]
    expected_by_source = {
        str(item.get("source_id")): item
        for item in files
        if isinstance(item, dict) and item.get("source_id")
    }
    if len(expected_by_source) != len(records):
        errors.append("snapshot and extraction record counts differ")
    for record in records:
        if not isinstance(record, dict):
            errors.append("extraction record is not an object")
            continue
        source_id = str(record.get("source_id", ""))
        snapshot_item = expected_by_source.get(source_id)
        if snapshot_item is None:
            errors.append(f"unknown extraction source {source_id}")
            continue
        source_hash = str(record.get("source_sha256", ""))
        if source_hash != snapshot_item.get("sha256"):
            errors.append(f"source hash changed for {source_id}")
        try:
            expected_uid = source_uid(source_hash)
        except ValueError:
            expected_uid = ""
            errors.append(f"source hash is invalid for {source_id}")
        if record.get("source_uid") not in {None, "", expected_uid}:
            errors.append(f"source uid mismatch for {source_id}")
        record_config = record.get("extractor_config")
        if isinstance(record_config, dict):
            record_dependencies = record.get("dependency_versions")
            if not isinstance(record_dependencies, dict):
                record_dependencies = extraction_dependency_versions(
                    str(record.get("extension", ""))
                )
            expected_cache_key = extraction_cache_key(
                source_sha256=source_hash,
                extension=str(record.get("extension", "")),
                config=record_config,
                dependency_versions=record_dependencies,
            )
            if record.get("cache_key") != expected_cache_key:
                errors.append(f"cache key mismatch for {source_id}")
            if isinstance(record.get("dependency_versions"), dict):
                if record.get("dependency_fingerprint") != dependency_fingerprint(
                    record_dependencies
                ):
                    errors.append(f"dependency fingerprint mismatch for {source_id}")
        elif expected_config is not None:
            errors.append(f"extractor config missing for {source_id}")

        paths: dict[str, Path] = {}
        for field, hash_field in (
            ("document_ir", "document_ir_sha256"),
            ("extracted_artifact", "extracted_artifact_sha256"),
            ("quality_artifact", "quality_artifact_sha256"),
        ):
            relative = str(record.get(field, ""))
            expected_hash = str(record.get(hash_field, ""))
            if not relative or not expected_hash:
                errors.append(f"{source_id} missing {field} hash contract")
                continue
            try:
                if field == "document_ir":
                    snapshot_value = read_manifest_bound_file(archive, relative)
                    path = snapshot_value.path
                    actual_hash = snapshot_value.sha256
                else:
                    path = ensure_under(archive, archive / relative)
                    actual_hash = _sha256(path)
                paths[field] = path
            except (OSError, ValueError):
                errors.append(f"{source_id} {field} is missing or unsafe")
                continue
            if actual_hash != expected_hash:
                errors.append(f"{source_id} {field} hash mismatch")
        ir_path = paths.get("document_ir")
        if ir_path is not None:
            try:
                blocks = load_manifest_document_ir(
                    archive,
                    str(record.get("document_ir", "")),
                    expected_sha256=str(record.get("document_ir_sha256", "")),
                )
            except (OSError, ValueError):
                errors.append(f"{source_id} document IR content is invalid")
            else:
                if record.get("document_ir_block_count") not in {
                    None,
                    len(blocks),
                }:
                    errors.append(f"{source_id} document IR count mismatch")
                if any(
                    block.get("source_id") != source_id
                    or block.get("source_uid") != expected_uid
                    for block in blocks
                ):
                    errors.append(f"{source_id} document IR identity mismatch")
    return not errors, errors

def _valid_anchor_syntax(anchor: str) -> bool:
    return bool(
        _PAGE_RANGE.fullmatch(anchor)
        or _PARAGRAPH.fullmatch(anchor)
        or _TABLE.fullmatch(anchor)
        or _SHEET_CELL.fullmatch(anchor)
        or re.fullmatch(r"line\d{4}", anchor)
        or re.fullmatch(r"header\d{3}/paragraph\d{4}", anchor)
        or re.fullmatch(r"footer\d{3}/paragraph\d{4}", anchor)
        or re.fullmatch(r"footnote\d{3}/paragraph\d{4}", anchor)
        or re.fullmatch(r"endnote\d{3}/paragraph\d{4}", anchor)
        or re.fullmatch(r"textbox\d{3}/paragraph\d{4}", anchor)
        or re.fullmatch(r"image\d{3}", anchor)
    )


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
