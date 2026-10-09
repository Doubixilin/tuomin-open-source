"""Tests for tuomin_gateway.mapproc (recovery packages and manifests)."""
from __future__ import annotations

import copy
import json

import pytest

from tuomin_gateway.mapproc import (
    PackageError,
    export_manifest,
    export_package,
    import_package,
)
from tuomin_gateway.schemas import MappingEntry

_ENTRIES = [
    MappingEntry(placeholder="<ORG_001>", label="ORG", original_value="甲方公司", text_hash="sha256:a"),
    MappingEntry(placeholder="<CONTACT_001>", label="CONTACT", original_value="13800000000", text_hash="sha256:b"),
]
_BINDING = dict(
    job_id="job-2026-0821-a1b2",
    app_id="workbench",
    profile="file_workbench",
    versions={"gateway": "0.1.0", "rules": "rules-2026.09.14-secrets-pem"},
    source_sha256="a" * 64,
    redacted_sha256="b" * 64,
    expected_counts={"<ORG_001>": 1, "<CONTACT_001>": 1},
)


def _package(passphrase: str = "correct horse", **overrides) -> dict:
    kwargs = {**_BINDING, **overrides}
    return export_package(_ENTRIES, passphrase=passphrase, **kwargs)


def test_manifest_contains_no_original_values():
    manifest = export_manifest(
        _ENTRIES,
        job_id=_BINDING["job_id"],
        app_id="workbench",
        profile="file_workbench",
        versions={"gateway": "0.1.0"},
    )
    raw = json.dumps(manifest, ensure_ascii=False)
    assert "甲方公司" not in raw
    assert "13800000000" not in raw
    assert "original_value" not in raw
    assert manifest["placeholder_count"] == 2
    assert {e["placeholder"] for e in manifest["entries"]} == {"<ORG_001>", "<CONTACT_001>"}


def test_round_trip_restores_entries():
    package = _package()
    imported = import_package(
        package,
        passphrase="correct horse",
        job_id=_BINDING["job_id"],
        app_id="workbench",
        source_sha256=_BINDING["source_sha256"],
        redacted_sha256=_BINDING["redacted_sha256"],
    )
    assert imported.entries == _ENTRIES
    assert imported.binding["expected_counts"] == _BINDING["expected_counts"]


def test_wrong_passphrase_refused():
    with pytest.raises(PackageError):
        import_package(
            _package(), passphrase="wrong", job_id=_BINDING["job_id"], app_id="workbench"
        )


def test_wrong_job_or_app_refused():
    package = _package()
    with pytest.raises(PackageError, match="job_id"):
        import_package(package, passphrase="correct horse", job_id="job-other", app_id="workbench")
    with pytest.raises(PackageError, match="app_id"):
        import_package(
            package, passphrase="correct horse", job_id=_BINDING["job_id"], app_id="other-app"
        )


def test_wrong_file_hashes_refused():
    package = _package()
    with pytest.raises(PackageError, match="source_sha256"):
        import_package(
            package,
            passphrase="correct horse",
            job_id=_BINDING["job_id"],
            app_id="workbench",
            source_sha256="c" * 64,
        )
    with pytest.raises(PackageError, match="redacted_sha256"):
        import_package(
            package,
            passphrase="correct horse",
            job_id=_BINDING["job_id"],
            app_id="workbench",
            redacted_sha256="d" * 64,
        )


def test_tampered_payload_refused():
    package = _package()
    raw = bytearray(__import__("base64").b64decode(package["payload"]))
    raw[0] ^= 1
    package["payload"] = __import__("base64").b64encode(bytes(raw)).decode("ascii")
    with pytest.raises(PackageError, match="decryption"):
        import_package(package, passphrase="correct horse", job_id=_BINDING["job_id"], app_id="workbench")


def test_tampered_binding_refused_even_when_check_skipped():
    # An attacker rewrites the bound job id to the attacker's own job; the
    # caller checks pass but GCM AAD must still fail closed.
    package = _package()
    package["binding"]["job_id"] = "job-attacker"
    with pytest.raises(PackageError):
        import_package(package, passphrase="correct horse", job_id="job-attacker", app_id="workbench")


def test_expired_package_refused():
    package = _package(created_at=1000, expires_at=2000)
    with pytest.raises(PackageError, match="expired"):
        import_package(
            package, passphrase="correct horse", job_id=_BINDING["job_id"], app_id="workbench", now=2001
        )
    imported = import_package(
        package, passphrase="correct horse", job_id=_BINDING["job_id"], app_id="workbench", now=1999
    )
    assert imported.entries == _ENTRIES


def test_malformed_packages_refused():
    base = _package()
    for mutate in (
        lambda p: p.update(format="other"),
        lambda p: p.update(version=2),
        lambda p: p.update(binding={}),
        lambda p: p.update(kdf={"name": "pbkdf2"}),
        lambda p: p.update(payload="!!!not-base64"),
        lambda p: p.pop("cipher"),
    ):
        package = copy.deepcopy(base)
        mutate(package)
        with pytest.raises(PackageError):
            import_package(
                package, passphrase="correct horse", job_id=_BINDING["job_id"], app_id="workbench"
            )
