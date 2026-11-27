from __future__ import annotations

import json
from pathlib import Path

from scripts.doctor_macos import KEY_PACKAGES, LOCK_FILE, build_report, parse_lock_versions


def test_macos_lock_contains_every_key_runtime_package():
    versions = parse_lock_versions()

    assert LOCK_FILE.name == "requirements-macos-arm64.lock"
    assert all(name in versions for name in KEY_PACKAGES)
    assert versions["torch"] == "2.13.0"
    assert versions["transformers"] == "5.13.1"
    assert versions["cryptography"] == "46.0.7"


def test_macos_example_registry_uses_only_synthetic_fixture_paths():
    root = Path(__file__).resolve().parents[1]
    payload = json.loads((root / "tuomin_apps.macos.example.json").read_text(encoding="utf-8"))

    assert payload["apps"]
    for app in payload["apps"].values():
        assert app["dictionary"] == "tests/fixtures/synthetic_dictionary.json"
        assert "C:/" not in app["dictionary"]
        assert "D:/" not in app["dictionary"]


def test_macos_doctor_report_never_serializes_provider_secrets(monkeypatch):
    marker = "synthetic-doctor-secret-must-not-appear"
    monkeypatch.setenv("DEEPSEEK_API_KEY", marker)
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", marker)

    encoded = json.dumps(build_report(load_ner=False), ensure_ascii=False)

    assert marker not in encoded
