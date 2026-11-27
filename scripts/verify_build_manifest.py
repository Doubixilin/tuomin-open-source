#!/usr/bin/env python3
"""Verify a frozen Windows bundle, release manifest, ZIP, and checksum."""

from __future__ import annotations

import argparse
import hashlib
import importlib.machinery
import json
import os
from pathlib import Path
import runpy
import zipfile


REPO_ROOT = Path(__file__).resolve().parents[1]
NATIVE_DEFINITION = runpy.run_path(
    str(REPO_ROOT / "scripts" / "build_native_modules.py")
)
CROWN_MODULES = tuple(NATIVE_DEFINITION["CROWN_MODULES"])
PRIVATE_KEY_MARKER = b"-----BEGIN PRIVATE KEY-----"
FORBIDDEN_PARTS = {
    "local_private",
    "private_data",
    "private_samples",
    "real_samples",
    "private_workspace",
    ".admin_token_local",
    "admin-token",
    "tuomin_apps.json",
    "rd.json",
}


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def expected_native_modules() -> set[str]:
    return {
        ".".join(
            (
                "tuomin_gateway",
                *Path(relative).parent.parts,
                Path(relative).stem,
            )
        )
        for relative in CROWN_MODULES
    }


def bundled_module_name(bundle: Path, path: Path) -> str | None:
    relative = path.relative_to(bundle)
    parts = relative.parts
    try:
        package_index = parts.index("tuomin_gateway")
    except ValueError:
        return None
    package_parts = parts[package_index:]
    if not package_parts:
        return None
    module_stem = package_parts[-1].split(".")[0]
    return ".".join((*package_parts[:-1], module_stem))


def verify_native_modules(bundle: Path) -> None:
    extension_suffixes = tuple(importlib.machinery.EXTENSION_SUFFIXES)
    discovered: dict[str, list[Path]] = {}
    for path in bundle.rglob("*"):
        if not path.is_file() or not path.name.endswith(extension_suffixes):
            continue
        module_name = bundled_module_name(bundle, path)
        if module_name is not None:
            discovered.setdefault(module_name, []).append(path)

    expected = expected_native_modules()
    actual = expected & discovered.keys()
    missing = expected - actual
    duplicates = {
        name: paths for name, paths in discovered.items() if name in expected and len(paths) != 1
    }
    if missing or duplicates:
        raise RuntimeError(
            "native module verification failed: "
            f"missing={sorted(missing)} duplicates={sorted(duplicates)}"
        )

    protected_relatives = {Path(relative) for relative in CROWN_MODULES}
    leaked_sources: list[str] = []
    for path in bundle.rglob("*"):
        if not path.is_file() or path.suffix not in {".py", ".pyc"}:
            continue
        relative = path.relative_to(bundle)
        try:
            package_index = relative.parts.index("tuomin_gateway")
        except ValueError:
            continue
        package_relative = Path(*relative.parts[package_index + 1 :])
        source_relative = package_relative.with_suffix(".py")
        if source_relative in protected_relatives:
            leaked_sources.append(relative.as_posix())
    if leaked_sources:
        raise RuntimeError(f"protected Python sources leaked into bundle: {leaked_sources}")


def file_contains(path: Path, needle: bytes) -> bool:
    overlap = max(0, len(needle) - 1)
    previous = b""
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            combined = previous + chunk
            if needle in combined:
                return True
            previous = combined[-overlap:] if overlap else b""
    return False


def verify_bundle_security(bundle: Path, *, public_build: bool = False) -> None:
    if not (bundle / "Tuomin.exe").is_file():
        raise RuntimeError(f"Tuomin.exe is unavailable in bundle: {bundle}")
    private_key_hits: list[str] = []
    forbidden_paths: list[str] = []
    for path in (item for item in bundle.rglob("*") if item.is_file()):
        relative = path.relative_to(bundle)
        parts = {part.casefold() for part in relative.parts}
        restricted_public = public_build and (
            parts & {"ner-model", "delivery.json", "pymupdf", "fitz"}
            or path.suffix.lower() == ".safetensors"
            or path.name.lower() == "pytorch_model.bin"
        )
        if FORBIDDEN_PARTS & parts or restricted_public:
            forbidden_paths.append(relative.as_posix())
        if path.stat().st_size <= 8 * 1024 * 1024 and file_contains(
            path, PRIVATE_KEY_MARKER
        ):
            private_key_hits.append(relative.as_posix())
    if forbidden_paths:
        raise RuntimeError(f"private runtime paths leaked into bundle: {forbidden_paths}")
    if private_key_hits:
        raise RuntimeError(f"private key material detected in bundle: {private_key_hits}")
    if not public_build:
        verify_native_modules(bundle)


def verify_manifest(bundle: Path, manifest_path: Path) -> dict:
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    expected_files = manifest.get("bundle", {}).get("files", {})
    actual_files = {
        path.relative_to(bundle).as_posix(): path
        for path in bundle.rglob("*")
        if path.is_file()
    }
    if set(expected_files) != set(actual_files):
        raise RuntimeError(
            "bundle file set differs from manifest: "
            f"missing={sorted(set(expected_files) - set(actual_files))} "
            f"unexpected={sorted(set(actual_files) - set(expected_files))}"
        )
    for relative, expected in expected_files.items():
        path = actual_files[relative]
        if path.stat().st_size != int(expected["bytes"]):
            raise RuntimeError(f"bundle size mismatch: {relative}")
        if sha256_file(path) != expected["sha256"]:
            raise RuntimeError(f"bundle hash mismatch: {relative}")
    return manifest


def verify_archive(bundle: Path, archive: Path, manifest: dict) -> None:
    expected_archive = manifest["archive"]
    if archive.name != expected_archive["name"]:
        raise RuntimeError("archive name differs from manifest")
    if archive.stat().st_size != int(expected_archive["bytes"]):
        raise RuntimeError("archive size differs from manifest")
    if sha256_file(archive) != expected_archive["sha256"]:
        raise RuntimeError("archive hash differs from manifest")

    expected_files = manifest["bundle"]["files"]
    expected_members = {
        (Path(bundle.name) / relative).as_posix(): metadata
        for relative, metadata in expected_files.items()
    }
    with zipfile.ZipFile(archive) as zipped:
        members = {info.filename: info for info in zipped.infolist() if not info.is_dir()}
        if set(members) != set(expected_members):
            raise RuntimeError("archive file set differs from bundle manifest")
        for name, expected in expected_members.items():
            digest = hashlib.sha256()
            with zipped.open(members[name]) as handle:
                for chunk in iter(lambda: handle.read(1 << 20), b""):
                    digest.update(chunk)
            if digest.hexdigest() != expected["sha256"]:
                raise RuntimeError(f"archive member hash mismatch: {name}")


def verify_checksum(checksum_path: Path, archive: Path, manifest: dict) -> None:
    expected = f"{manifest['archive']['sha256']}  {archive.name}"
    actual = checksum_path.read_text(encoding="ascii").strip()
    if actual != expected:
        raise RuntimeError("SHA-256 sidecar differs from manifest")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle", required=True, type=Path)
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--archive", type=Path)
    parser.add_argument("--checksum", type=Path)
    parser.add_argument("--public-build", action="store_true",
                        default=os.environ.get("TUOMIN_PUBLIC_BUILD") == "1",
                        help="verify the public source distribution without proprietary assets or native-module requirements")
    args = parser.parse_args()
    bundle = args.bundle.resolve()
    verify_bundle_security(bundle, public_build=args.public_build)
    if args.manifest:
        manifest = verify_manifest(bundle, args.manifest.resolve())
        if not args.archive or not args.checksum:
            raise SystemExit("--archive and --checksum are required with --manifest")
        archive = args.archive.resolve()
        verify_archive(bundle, archive, manifest)
        verify_checksum(args.checksum.resolve(), archive, manifest)
    print(f"build verification passed: {bundle}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
