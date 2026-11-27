#!/usr/bin/env python3
"""Safe Windows development readiness report for Tuomin.

The report contains versions, paths, and capability status only. It never
prints provider keys, mapping values, real documents, or private dictionaries.
"""
from __future__ import annotations

import argparse
import importlib.metadata
import json
import os
import platform
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
LOCK_FILE = ROOT / "requirements-windows.lock"
EXPECTED_PYTHON = (3, 12, 10)
SUPPORTED_NODE = ">=22.23.1 <23"
KEY_PACKAGES = (
    "cryptography",
    "fastapi",
    "httpx",
    "openpyxl",
    "pymupdf",
    "pytest",
    "torch",
    "transformers",
    "uvicorn",
    "xlrd",
    "pywebview",
    "pythonnet",
    "pystray",
    "pillow",
)
MODEL_FILES = (
    "config.json",
    "pytorch_model.bin",
    "special_tokens_map.json",
    "tokenizer_config.json",
    "vocab.txt",
)


def parse_lock_versions(path: Path = LOCK_FILE) -> dict[str, str]:
    versions: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "==" not in line:
            continue
        name, version = line.split("==", 1)
        versions[name.lower().replace("_", "-")] = version
    return versions


def _item(name: str, status: str, **details: Any) -> dict[str, Any]:
    return {"name": name, "status": status, **details}


def _check_platform() -> dict[str, Any]:
    machine = platform.machine() or ("AMD64" if sys.maxsize > 2**32 else "x86")
    current = {"system": platform.system(), "machine": machine}
    status = "ok" if sys.platform == "win32" and sys.maxsize > 2**32 else "error"
    return _item("platform", status, expected="Windows x64", **current)


def _check_python() -> dict[str, Any]:
    actual = tuple(sys.version_info[:3])
    return _item(
        "python",
        "ok" if actual == EXPECTED_PYTHON else "error",
        expected=".".join(map(str, EXPECTED_PYTHON)),
        actual=platform.python_version(),
        executable=sys.executable,
    )


def _check_venv() -> dict[str, Any]:
    venv_dir = os.environ.get("TUOMIN_VENV_DIR", ".venv")
    expected = (ROOT / venv_dir).resolve()
    actual = Path(sys.prefix).resolve()
    return _item(
        "venv",
        "ok" if actual == expected else "error",
        expected=str(expected),
        actual=str(actual),
    )


def _check_packages() -> dict[str, Any]:
    expected = parse_lock_versions()
    actual: dict[str, str | None] = {}
    mismatches: dict[str, dict[str, str | None]] = {}
    for name in KEY_PACKAGES:
        try:
            version = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            version = None
        actual[name] = version
        wanted = expected.get(name.lower().replace("_", "-"))
        if name == "pymupdf" and version is None:
            continue  # Installed only when PDF support is requested.
        if version is None or version.split("+", 1)[0] != wanted:
            mismatches[name] = {"expected": wanted, "actual": version}
    return _item(
        "packages",
        "error" if mismatches else "ok",
        versions=actual,
        mismatches=mismatches,
        lock_file=str(LOCK_FILE),
    )


def _check_webview_runtime() -> dict[str, Any]:
    """Read prerequisites only; never install WebView2 or modify the registry."""
    if os.name != "nt":
        return _item("webview2", "skipped", reason="Windows only")
    import winreg

    framework = None
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                            r"SOFTWARE\Microsoft\NET Framework Setup\NDP\v4\Full") as key:
            framework = winreg.QueryValueEx(key, "Release")[0]
    except OSError:
        pass
    version = None
    client = r"Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}"
    for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        for prefix in ("SOFTWARE\\", "SOFTWARE\\WOW6432Node\\"):
            try:
                with winreg.OpenKey(hive, prefix + client) as key:
                    candidate = str(winreg.QueryValueEx(key, "pv")[0])
                if int(candidate.split(".")[0]) >= 86:
                    version = candidate
            except (OSError, TypeError, ValueError):
                continue
    ready = version is not None and isinstance(framework, int) and framework >= 394802
    return _item("webview2", "ok" if ready else "error", version=version,
                 dotnet_release=framework, required="Edge WebView2 Runtime and .NET Framework >=4.6.2")


def _parse_version(raw: str) -> tuple[int, int, int] | None:
    try:
        parts = raw.removeprefix("v").split(".")
        if len(parts) < 3:
            return None
        return int(parts[0]), int(parts[1]), int(parts[2])
    except (TypeError, ValueError):
        return None


def _node_supported(version: tuple[int, int, int] | None) -> bool:
    if version is None:
        return False
    return version[0] == 22 and version >= (22, 23, 1)


def _check_node() -> dict[str, Any]:
    configured = os.environ.get("TUOMIN_NODE_BIN")
    node = configured or shutil.which("node")
    if not node:
        return _item("node", "error", expected=SUPPORTED_NODE, actual=None)
    result = subprocess.run(
        [node, "--version"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    actual = result.stdout.strip().removeprefix("v")
    return _item(
        "node",
        "ok" if result.returncode == 0 and _node_supported(_parse_version(actual)) else "error",
        expected=SUPPORTED_NODE,
        actual=actual,
        executable=str(Path(node).resolve()),
    )


def _check_temp_directory() -> dict[str, Any]:
    path = Path(tempfile.gettempdir()).resolve()
    try:
        with tempfile.NamedTemporaryFile(prefix="tuomin-doctor-", dir=path) as handle:
            handle.write(b"ok")
            handle.flush()
    except OSError as exc:
        return _item(
            "temp_directory",
            "error",
            path=str(path),
            error_type=type(exc).__name__,
        )
    return _item("temp_directory", "ok", path=str(path))


def _check_long_paths() -> dict[str, Any]:
    if os.name != "nt":
        return _item("windows_long_paths", "skipped", reason="Windows only")
    try:
        import winreg

        with winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE,
            r"SYSTEM\CurrentControlSet\Control\FileSystem",
        ) as key:
            enabled = int(winreg.QueryValueEx(key, "LongPathsEnabled")[0]) == 1
    except OSError as exc:
        return _item(
            "windows_long_paths",
            "warning",
            enabled=None,
            error_type=type(exc).__name__,
        )
    return _item(
        "windows_long_paths",
        "ok" if enabled else "warning",
        enabled=enabled,
    )


def _check_msvc() -> dict[str, Any]:
    if os.name != "nt":
        return _item("msvc_build_tools", "skipped", reason="Windows only")
    cl = shutil.which("cl.exe")
    if cl:
        return _item("msvc_build_tools", "ok", executable=str(Path(cl).resolve()))

    program_files_x86 = os.environ.get("ProgramFiles(x86)")
    vswhere = (
        Path(program_files_x86) / "Microsoft Visual Studio" / "Installer" / "vswhere.exe"
        if program_files_x86
        else None
    )
    if vswhere and vswhere.is_file():
        result = subprocess.run(
            [
                str(vswhere),
                "-latest",
                "-products",
                "*",
                "-requires",
                "Microsoft.VisualStudio.Component.VC.Tools.x86.x64",
                "-property",
                "installationPath",
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
        installation = result.stdout.strip()
        if result.returncode == 0 and installation:
            return _item("msvc_build_tools", "ok", installation=installation)

    return _item(
        "msvc_build_tools",
        "warning",
        message="Visual Studio 2022 C++ Build Tools are required for Nuitka packaging.",
    )


def _check_model(load_ner: bool) -> dict[str, Any]:
    try:
        from huggingface_hub import snapshot_download
        from tuomin_gateway.detectors.ner import MODEL_NAME, MODEL_REVISION

        configured_model = os.environ.get('TUOMIN_NER_MODEL_DIR')
        snapshot = Path(configured_model).expanduser() if configured_model else Path(
            snapshot_download(
                repo_id=MODEL_NAME,
                revision=MODEL_REVISION,
                local_files_only=True,
                allow_patterns=list(MODEL_FILES),
            )
        )
        missing = [name for name in MODEL_FILES if not (snapshot / name).is_file()]
        if missing:
            return _item(
                "ner_model",
                "error" if load_ner else "warning",
                model=MODEL_NAME,
                revision=MODEL_REVISION,
                missing_files=missing,
            )
        detected: list[str] = []
        if load_ner:
            from tuomin_gateway.detectors.ner import CluenerNerDetector

            spans = CluenerNerDetector().detect("联系人韩语桐就职于北辰测试制造有限公司。")
            detected = sorted({span.label for span in spans})
            if not {"PERSON", "ORG"}.issubset(detected):
                return _item(
                    "ner_model",
                    "error",
                    model=MODEL_NAME,
                    revision=MODEL_REVISION,
                    load_test="unexpected_detection",
                    detected_labels=detected,
                )
        return _item(
            "ner_model",
            "ok",
            model=MODEL_NAME,
            revision=MODEL_REVISION,
            snapshot=str(snapshot),
            load_test="ok" if load_ner else "not_requested",
            detected_labels=detected,
        )
    except Exception as exc:
        return _item(
            "ner_model",
            "error" if load_ner else "warning",
            error_type=type(exc).__name__,
            message=str(exc).splitlines()[0][:240],
        )


def _check_mapping_store() -> dict[str, Any]:
    if os.name != "nt":
        return _item("mapping_store", "skipped", reason="Windows only")
    private_root = ROOT / "local_private"
    private_root.mkdir(parents=True, exist_ok=True)
    try:
        from tuomin_gateway.schemas import MappingEntry
        from tuomin_gateway.store import MappingStore

        with tempfile.TemporaryDirectory(prefix="tuomin_windows_doctor_", dir=private_root) as tmp:
            store = MappingStore(tmp, ttl_seconds=60)
            path = store.save(
                "synthetic_doctor",
                [MappingEntry("<ORG_001>", "ORG", "Synthetic Doctor Entity", "sha256:doctor")],
            )
            magic = path.read_bytes().split(b"\n", 1)[0].decode("ascii", "replace")
            status = store.safe_status()
    except Exception as exc:
        return _item(
            "mapping_store",
            "error",
            error_type=type(exc).__name__,
            message=str(exc).splitlines()[0][:240],
        )
    healthy = magic == "DPAPI1" and status["platform_mode"] == "windows-dpapi"
    return _item(
        "mapping_store",
        "ok" if healthy else "error",
        platform_mode=status["platform_mode"],
        marker=magic,
        encrypted_at_rest=status["encrypted_at_rest"],
        key_provider=status["key_provider"],
    )


def _check_config() -> dict[str, Any]:
    raw = os.environ.get("TUOMIN_CONFIG")
    path = Path(raw).expanduser() if raw else ROOT / "tuomin_apps.json"
    if not path.exists():
        return _item(
            "app_config",
            "warning",
            configured=False,
            expected=str(path),
            example=str(ROOT / "tuomin_apps.example.json"),
        )
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        apps = payload.get("apps")
        if not isinstance(apps, dict):
            raise ValueError("apps must be an object")
    except Exception as exc:
        return _item("app_config", "error", configured=True, error_type=type(exc).__name__)
    return _item("app_config", "ok", configured=True, path=str(path), app_count=len(apps))


def _check_optional_commands() -> dict[str, Any]:
    return _item(
        "optional_commands",
        "ok",
        git_available=shutil.which("git") is not None,
        powershell_available=shutil.which("pwsh") is not None or shutil.which("powershell") is not None,
    )


def build_report(*, load_ner: bool = False) -> dict[str, Any]:
    checks = [
        _check_platform(),
        _check_python(),
        _check_venv(),
        _check_packages(),
        _check_webview_runtime(),
        _check_node(),
        _check_temp_directory(),
        _check_long_paths(),
        _check_msvc(),
        _check_model(load_ner),
        _check_mapping_store(),
        _check_config(),
        _check_optional_commands(),
    ]
    statuses = {check["status"] for check in checks}
    status = "error" if "error" in statuses else "warning" if "warning" in statuses else "ok"
    return {
        "status": status,
        "platform_target": "Windows-x64-development",
        "development_ready": status != "error",
        "packaging_ready": next(
            check for check in checks if check["name"] == "msvc_build_tools"
        )["status"] == "ok",
        "checks": checks,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Check the Tuomin Windows development baseline.")
    parser.add_argument("--load-ner", action="store_true", help="load the pinned NER model and run a synthetic detection")
    args = parser.parse_args(argv)
    report = build_report(load_ner=args.load_ner)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 1 if report["status"] == "error" else 0


if __name__ == "__main__":
    raise SystemExit(main())
