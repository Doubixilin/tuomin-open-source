#!/usr/bin/env python3
"""Safe macOS development readiness report for Tuomin.

The report contains versions, paths, and capability status only. It never reads
or prints provider keys, mapping values, real documents, or private dictionaries.
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
LOCK_FILE = ROOT / "requirements-macos-arm64.lock"
EXPECTED_PYTHON = (3, 12, 10)
EXPECTED_NODE = "22.23.1"
KEY_PACKAGES = ("cryptography", "fastapi", "httpx", "pytest", "torch", "transformers", "uvicorn")
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
    current = {"system": platform.system(), "machine": platform.machine()}
    if sys.platform != "darwin":
        return _item("platform", "error", expected="macOS arm64", **current)
    if platform.machine() != "arm64":
        return _item("platform", "error", expected="arm64 without Rosetta", **current)
    return _item("platform", "ok", **current)


def _check_python() -> dict[str, Any]:
    actual = tuple(sys.version_info[:3])
    status = "ok" if actual == EXPECTED_PYTHON else "error"
    return _item(
        "python",
        status,
        expected=".".join(map(str, EXPECTED_PYTHON)),
        actual=".".join(map(str, actual)),
        executable=sys.executable,
    )


def _check_venv() -> dict[str, Any]:
    expected = (ROOT / ".venv").resolve()
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
        if version != wanted:
            mismatches[name] = {"expected": wanted, "actual": version}
    return _item(
        "packages",
        "error" if mismatches else "ok",
        versions=actual,
        mismatches=mismatches,
        lock_file=str(LOCK_FILE),
    )


def _check_node() -> dict[str, Any]:
    node = shutil.which("node")
    if not node:
        return _item("node", "error", expected=EXPECTED_NODE, actual=None)
    result = subprocess.run([node, "--version"], capture_output=True, text=True, check=False)
    actual = result.stdout.strip().removeprefix("v")
    return _item(
        "node",
        "ok" if actual == EXPECTED_NODE else "error",
        expected=EXPECTED_NODE,
        actual=actual,
        executable=node,
    )


def _check_model(load_ner: bool) -> dict[str, Any]:
    try:
        from huggingface_hub import snapshot_download
        from tuomin_gateway.detectors.ner import MODEL_NAME, MODEL_REVISION

        directory = os.environ.get("TUOMIN_NER_MODEL_DIR")
        snapshot = Path(directory).expanduser() if directory else Path(
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
    try:
        from tuomin_gateway.schemas import MappingEntry
        from tuomin_gateway.store import MappingStore

        with tempfile.TemporaryDirectory(prefix="tuomin_macos_doctor_") as tmp:
            store = MappingStore(tmp, ttl_seconds=60)
            path = store.save(
                "synthetic_doctor",
                [MappingEntry("<ORG_001>", "ORG", "Synthetic Doctor Entity", "sha256:doctor")],
            )
            magic = path.read_bytes().split(b"\n", 1)[0].decode("ascii", "replace")
            mode = store.safe_status()["platform_mode"]
    except Exception as exc:
        return _item(
            "mapping_store",
            "error",
            error_type=type(exc).__name__,
            message=str(exc).splitlines()[0][:240],
        )
    if sys.platform == "darwin" and magic == "PLAIN1":
        return _item(
            "mapping_store",
            "warning",
            platform_mode=mode,
            marker=magic,
            production_ready=False,
            next_step="unset TUOMIN_ALLOW_PLAINTEXT_STORE for protected macOS persistence",
        )
    return _item(
        "mapping_store",
        "ok",
        platform_mode=mode,
        marker=magic,
        encrypted_at_rest=store.safe_status()["encrypted_at_rest"],
        key_provider=store.safe_status()["key_provider"],
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
            example=str(ROOT / "tuomin_apps.macos.example.json"),
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
        claude_available=shutil.which("claude") is not None,
        powershell_available=shutil.which("pwsh") is not None or shutil.which("powershell") is not None,
    )


def build_report(*, load_ner: bool = False) -> dict[str, Any]:
    checks = [
        _check_platform(),
        _check_python(),
        _check_venv(),
        _check_packages(),
        _check_node(),
        _check_model(load_ner),
        _check_mapping_store(),
        _check_config(),
        _check_optional_commands(),
    ]
    statuses = {check["status"] for check in checks}
    status = "error" if "error" in statuses else "warning" if "warning" in statuses else "ok"
    return {
        "status": status,
        "platform_target": "macOS-arm64-development",
        "production_ready": status == "ok" and all(
            check.get("production_ready", True) for check in checks
        ),
        "checks": checks,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Check the Tuomin macOS development baseline.")
    parser.add_argument("--load-ner", action="store_true", help="load the pinned NER model and run a synthetic detection")
    args = parser.parse_args(argv)
    report = build_report(load_ner=args.load_ner)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 1 if report["status"] == "error" else 0


if __name__ == "__main__":
    raise SystemExit(main())
