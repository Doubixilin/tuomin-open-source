"""Portable recovery packages (.tuominmap) and placeholder manifests.

Two export products for the local file workbench (安全评审见
docs/plans/2026-08-20-local-app-file-redaction-and-distribution-plan.md §5.2):

- **manifest**: placeholder/label/count/hash only — NO original values. Safe to
  share for audit; cannot refill anything.
- **recovery package** (.tuominmap): the real mapping, encrypted with a
  passphrase-derived key (scrypt + AES-256-GCM). All binding metadata (job,
  app, profile, detector versions, source/redacted file hashes, expected
  placeholder counts) is authenticated as GCM AAD, so a swapped, tampered or
  wrong-file package fails closed.

This module is pure: no file IO, no audit writes (callers handle both).
"""
from __future__ import annotations

import base64
import hashlib
import json
import secrets
import time
from dataclasses import dataclass
from typing import Any

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from tuomin_gateway.schemas import MappingEntry

PACKAGE_FORMAT = "tuominmap"
PACKAGE_VERSION = 1
MANIFEST_FORMAT = "tuominmap-manifest"

_SCRYPT_N = 2**15
_SCRYPT_R = 8
_SCRYPT_P = 1
_KEY_LEN = 32
_NONCE_LEN = 12
_SALT_LEN = 16


class PackageError(ValueError):
    """Any recovery-package validation, binding or decryption failure."""


@dataclass(frozen=True)
class ImportedPackage:
    """A successfully unlocked recovery package."""

    entries: list[MappingEntry]
    binding: dict[str, Any]
    docx: dict[str, Any] | None = None


def _b64e(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


def _b64d(value: Any, field: str) -> bytes:
    if not isinstance(value, str):
        raise PackageError(f"{field} must be a base64 string")
    try:
        return base64.b64decode(value.encode("ascii"), validate=True)
    except ValueError as exc:
        raise PackageError(f"{field} is not valid base64") from exc


def _canonical(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _require_str(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise PackageError(f"binding.{field} must be a non-empty string")
    return value


def _validate_binding(binding: Any) -> dict[str, Any]:
    if not isinstance(binding, dict):
        raise PackageError("binding must be an object")
    for field in ("job_id", "app_id", "profile", "source_sha256", "redacted_sha256"):
        _require_str(binding.get(field), field)
    versions = binding.get("versions")
    if not isinstance(versions, dict) or any(
        not isinstance(k, str) or not isinstance(v, str) for k, v in versions.items()
    ):
        raise PackageError("binding.versions must be a string map")
    counts = binding.get("expected_counts")
    if not isinstance(counts, dict) or any(
        not isinstance(k, str) or not isinstance(v, int) or isinstance(v, bool)
        for k, v in counts.items()
    ):
        raise PackageError("binding.expected_counts must be a placeholder->int map")
    created_at = binding.get("created_at")
    if not isinstance(created_at, int) or isinstance(created_at, bool):
        raise PackageError("binding.created_at must be an integer")
    expires_at = binding.get("expires_at")
    if expires_at is not None and (not isinstance(expires_at, int) or isinstance(expires_at, bool)):
        raise PackageError("binding.expires_at must be an integer or null")
    return binding


def _derive_key(passphrase: str, kdf: dict[str, Any]) -> bytes:
    if kdf.get("name") != "scrypt":
        raise PackageError("unsupported KDF")
    try:
        n = int(kdf["n"])
        r = int(kdf["r"])
        p = int(kdf["p"])
        length = int(kdf["length"])
    except (KeyError, TypeError, ValueError) as exc:
        raise PackageError("invalid KDF parameters") from exc
    if (n, r, p, length) != (_SCRYPT_N, _SCRYPT_R, _SCRYPT_P, _KEY_LEN):
        raise PackageError("unsupported KDF parameters")
    salt = _b64d(kdf.get("salt"), "kdf.salt")
    if len(salt) != _SALT_LEN:
        raise PackageError("invalid KDF salt")
    if not passphrase:
        raise PackageError("passphrase must be non-empty")
    return hashlib.scrypt(
        passphrase.encode("utf-8"), salt=salt, n=n, r=r, p=p, dklen=length,
        maxmem=64 * 1024 * 1024,
    )


def export_manifest(
    entries: list[MappingEntry],
    *,
    job_id: str,
    app_id: str,
    profile: str,
    versions: dict[str, str],
) -> dict[str, Any]:
    """Value-free placeholder listing. Contains NO original values."""
    counts: dict[str, int] = {}
    for entry in entries:
        counts[entry.placeholder] = counts.get(entry.placeholder, 0) + 1
    return {
        "format": MANIFEST_FORMAT,
        "version": PACKAGE_VERSION,
        "job_id": job_id,
        "app_id": app_id,
        "profile": profile,
        "versions": dict(sorted(versions.items())),
        "placeholder_count": len(counts),
        "entries": [
            {
                "placeholder": entry.placeholder,
                "label": entry.label,
                "text_hash": entry.text_hash,
                "count": counts[entry.placeholder],
            }
            for entry in entries
        ],
    }


def export_package(
    entries: list[MappingEntry],
    *,
    job_id: str,
    app_id: str,
    profile: str,
    versions: dict[str, str],
    source_sha256: str,
    redacted_sha256: str,
    expected_counts: dict[str, int],
    passphrase: str,
    created_at: int | None = None,
    expires_at: int | None = None,
    docx_ledger: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Encrypt the real mapping into a passphrase-protected recovery package.

    ``docx_ledger`` (format-preserving DOCX jobs) rides inside the encrypted
    payload, so position/format metadata gets the same tamper evidence as the
    mapping itself. The ledger must never contain original values.
    """
    binding = _validate_binding(
        {
            "job_id": job_id,
            "app_id": app_id,
            "profile": profile,
            "versions": dict(sorted(versions.items())),
            "source_sha256": source_sha256,
            "redacted_sha256": redacted_sha256,
            "expected_counts": dict(sorted(expected_counts.items())),
            "created_at": created_at if created_at is not None else int(time.time()),
            "expires_at": expires_at,
        }
    )
    kdf = {
        "name": "scrypt",
        "salt": _b64e(secrets.token_bytes(_SALT_LEN)),
        "n": _SCRYPT_N,
        "r": _SCRYPT_R,
        "p": _SCRYPT_P,
        "length": _KEY_LEN,
    }
    key = _derive_key(passphrase, kdf)
    nonce = secrets.token_bytes(_NONCE_LEN)
    aad = _canonical({"format": PACKAGE_FORMAT, "version": PACKAGE_VERSION, "binding": binding})
    if docx_ledger is None:
        plaintext = _canonical([entry.to_dict() for entry in entries])
    else:
        plaintext = _canonical(
            {
                "entries": [entry.to_dict() for entry in entries],
                "docx": docx_ledger,
            }
        )
    return {
        "format": PACKAGE_FORMAT,
        "version": PACKAGE_VERSION,
        "kdf": kdf,
        "cipher": {"name": "AES-256-GCM", "nonce": _b64e(nonce)},
        "binding": binding,
        "payload": _b64e(AESGCM(key).encrypt(nonce, plaintext, aad)),
    }


def import_package(
    package: Any,
    *,
    passphrase: str,
    job_id: str,
    app_id: str,
    source_sha256: str | None = None,
    redacted_sha256: str | None = None,
    now: int | None = None,
) -> ImportedPackage:
    """Unlock a recovery package, failing closed on any mismatch.

    ``job_id``/``app_id`` are always enforced. ``source_sha256`` and
    ``redacted_sha256`` are enforced when the caller supplies the actual file
    hashes (always do so when refilling a file).
    """
    if not isinstance(package, dict):
        raise PackageError("package must be a JSON object")
    if package.get("format") != PACKAGE_FORMAT:
        raise PackageError("not a tuominmap package")
    if package.get("version") != PACKAGE_VERSION:
        raise PackageError("unsupported package version")
    binding = _validate_binding(package.get("binding"))

    # Clear, explicit binding errors first; GCM authentication below is the
    # second, independent barrier against tampering.
    expected = {"job_id": job_id, "app_id": app_id}
    if source_sha256 is not None:
        expected["source_sha256"] = source_sha256
    if redacted_sha256 is not None:
        expected["redacted_sha256"] = redacted_sha256
    for field, actual in expected.items():
        if binding[field] != actual:
            raise PackageError(f"package is bound to a different {field}")
    expires_at = binding.get("expires_at")
    if expires_at is not None and (now if now is not None else int(time.time())) > expires_at:
        raise PackageError("package has expired")

    cipher = package.get("cipher")
    if not isinstance(cipher, dict) or cipher.get("name") != "AES-256-GCM":
        raise PackageError("unsupported cipher")
    nonce = _b64d(cipher.get("nonce"), "cipher.nonce")
    key = _derive_key(passphrase, package.get("kdf") or {})
    aad = _canonical({"format": PACKAGE_FORMAT, "version": PACKAGE_VERSION, "binding": binding})
    try:
        plaintext = AESGCM(key).decrypt(nonce, _b64d(package.get("payload"), "payload"), aad)
    except Exception as exc:  # wrong passphrase or tampered package — same refusal
        raise PackageError("package decryption failed") from exc
    try:
        payload = json.loads(plaintext.decode("utf-8"))
        if isinstance(payload, list):
            # Legacy shape: bare entry list, no DOCX ledger.
            raw_entries, docx_ledger = payload, None
        else:
            raw_entries = payload["entries"]
            docx_ledger = payload.get("docx")
        entries = [MappingEntry.from_dict(item) for item in raw_entries]
    except (ValueError, KeyError, TypeError, AttributeError) as exc:
        raise PackageError("package payload is invalid") from exc
    if docx_ledger is not None and not isinstance(docx_ledger, dict):
        raise PackageError("package payload is invalid")
    return ImportedPackage(entries=entries, binding=binding, docx=docx_ledger)
