#!/usr/bin/env python3
"""Create a Windows delivery ZIP, full file manifest, and SHA-256 file."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
from importlib import metadata
import json
import os
from pathlib import Path
import platform
import re
import subprocess
import sys
try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10 development/tests
    import tomli as tomllib
import zipfile


REPO_ROOT = Path(__file__).resolve().parents[1]


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def project_version() -> str:
    with (REPO_ROOT / "pyproject.toml").open("rb") as handle:
        return str(tomllib.load(handle)["project"]["version"])


def git_commit() -> str:
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(f"git rev-parse failed: {result.stderr.strip()}")
    return result.stdout.strip()


def source_provenance() -> dict:
    """Hash only build inputs, including new source files; exclude machine state."""
    result = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
        cwd=REPO_ROOT, capture_output=True, check=True,
    )
    roots = {"src", "scripts", "integrations", "packaging", "demo"}
    root_files = {"LICENSE", "THIRD_PARTY_NOTICES.md", "pyproject.toml", "package.json", "requirements-windows.lock",
                  "requirements-macos-arm64.lock", ".python-version", ".nvmrc", "service_runner.pyw"}
    excluded = ("packaging/native/", "packaging/ner-model/", "packaging/delivery.json")
    files = {}
    for name in sorted(set(result.stdout.decode("utf-8").strip("\0").split("\0"))):
        path = Path(name)
        if not name or (path.parts[0] not in roots and name not in root_files and not name.startswith('docs/manual/')):
            continue
        if name.startswith(excluded) or any(part in {"__pycache__", "node_modules"} or part.endswith(".egg-info") for part in path.parts):
            continue
        source = REPO_ROOT / path
        if source.is_file():
            files[name] = sha256_file(source)
    digest = hashlib.sha256(json.dumps(files, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    dirty = subprocess.run(["git", "status", "--porcelain"], cwd=REPO_ROOT, capture_output=True, check=True)
    return {"git_commit": git_commit(), "dirty": bool(dirty.stdout), "code_sha256": digest, "files": files}


def package_version(distribution: str) -> str | None:
    try:
        return metadata.version(distribution)
    except metadata.PackageNotFoundError:
        return None


def customer_slug(customer: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9._-]+", "_", customer.strip()).strip("._-")
    return slug or "customer"


def bundle_files(bundle: Path) -> list[Path]:
    return sorted(path for path in bundle.rglob("*") if path.is_file())


def build_release(bundle: Path, release_dir: Path, customer: str, *, lite: bool = False) -> dict[str, Path]:
    bundle = bundle.resolve()
    release_dir = release_dir.resolve()
    if not bundle.is_dir():
        raise RuntimeError(f"bundle directory is unavailable: {bundle}")
    executable = bundle / "Tuomin.exe"
    if not executable.is_file():
        raise RuntimeError(f"expected executable is unavailable: {executable}")

    provenance = source_provenance()
    metadata_path = bundle / "_internal" / "delivery.json"
    if metadata_path.is_file():
        recorded = json.loads(metadata_path.read_text(encoding="utf-8")).get("source")
        if recorded and recorded["code_sha256"] != provenance["code_sha256"]:
            raise RuntimeError("source changed after build metadata was recorded; rebuild the candidate")
    version = project_version()
    variant_suffix = "-lite" if lite else ""
    base_name = f"Tuomin-windows-x64{variant_suffix}-{version}-{customer_slug(customer)}"
    public_build = os.environ.get("TUOMIN_PUBLIC_BUILD") == "1"
    if public_build:
        base_name = f"Tuomin-{version}-Windows-x64-{'Lite' if lite else 'Runtime'}"
    release_dir.mkdir(parents=True, exist_ok=True)
    archive = release_dir / f"{base_name}.zip"
    manifest_path = release_dir / f"{base_name}.manifest.json"
    checksum_path = release_dir / f"{base_name}.sha256"
    for target in (archive, manifest_path, checksum_path):
        target.unlink(missing_ok=True)

    files = bundle_files(bundle)
    with zipfile.ZipFile(
        archive,
        "w",
        compression=zipfile.ZIP_DEFLATED,
        compresslevel=6,
        allowZip64=True,
    ) as zipped:
        for path in files:
            relative = path.relative_to(bundle)
            zipped.write(path, (Path(bundle.name) / relative).as_posix())

    manifest = {
        "schema_version": 1,
        "product": "tuomin-workbench",
        "version": version,
        "variant": "lite" if lite else "full",
        "git_commit": git_commit(),
        "source": provenance,
        "built_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "customer": customer,
        "platform": sys.platform,
        "architecture": platform.machine(),
        "python": platform.python_version(),
        "tools": {
            "pyinstaller": package_version("pyinstaller"),
            **({} if public_build else {"nuitka": package_version("nuitka")}),
        },
        "bundle": {
            "root": bundle.name,
            "files": {
                path.relative_to(bundle).as_posix(): {
                    "sha256": sha256_file(path),
                    "bytes": path.stat().st_size,
                }
                for path in files
            },
        },
        "archive": {
            "name": archive.name,
            "sha256": sha256_file(archive),
            "bytes": archive.stat().st_size,
        },
    }
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    checksum_path.write_text(
        f"{manifest['archive']['sha256']}  {archive.name}\n",
        encoding="ascii",
    )
    return {
        "archive": archive,
        "manifest": manifest_path,
        "checksum": checksum_path,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle", required=True, type=Path)
    parser.add_argument("--release-dir", required=True, type=Path)
    parser.add_argument("--customer", required=True)
    parser.add_argument("--lite", action="store_true")
    args = parser.parse_args()
    outputs = build_release(args.bundle, args.release_dir, args.customer, lite=args.lite)
    for name, path in outputs.items():
        print(f"{name}: {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
