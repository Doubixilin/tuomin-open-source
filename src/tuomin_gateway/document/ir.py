"""Tuomin-facing Document IR facade (re-exports the vendored IR types)."""
from __future__ import annotations

from .vendor.document_ir import (
    DOCUMENT_IR_SCHEMA_VERSION,
    EXTRACTOR_VERSION,
    MAX_DOCUMENT_IR_BYTES,
    DocumentBlock,
    LocatorResolution,
    ManifestBoundFileError,
    ManifestBoundFileSnapshot,
    dependency_fingerprint,
    extraction_cache_key,
    extraction_dependency_versions,
    load_document_ir,
    load_manifest_document_ir,
    read_manifest_bound_file,
    resolve_archive_locator,
    resolve_locator,
    source_uid,
    validate_cached_extraction,
    write_document_ir,
)

__all__ = [
    "DOCUMENT_IR_SCHEMA_VERSION",
    "EXTRACTOR_VERSION",
    "MAX_DOCUMENT_IR_BYTES",
    "DocumentBlock",
    "LocatorResolution",
    "ManifestBoundFileError",
    "ManifestBoundFileSnapshot",
    "dependency_fingerprint",
    "extraction_cache_key",
    "extraction_dependency_versions",
    "load_document_ir",
    "load_manifest_document_ir",
    "read_manifest_bound_file",
    "resolve_archive_locator",
    "resolve_locator",
    "source_uid",
    "validate_cached_extraction",
    "write_document_ir",
]
