from __future__ import annotations

import json
import sys
from types import SimpleNamespace

from scripts.doctor_windows import (
    EXPECTED_PYTHON,
    KEY_PACKAGES,
    LOCK_FILE,
    _node_supported,
    build_report,
    parse_lock_versions,
)


def test_windows_lock_contains_every_key_runtime_package():
    versions = parse_lock_versions()

    assert LOCK_FILE.name == "requirements-windows.lock"
    assert all(name in versions for name in KEY_PACKAGES)
    assert versions["torch"] == "2.13.0"
    assert versions["transformers"] == "5.14.1"
    assert versions["cryptography"] == "46.0.7"
    assert versions["openpyxl"] == "3.1.5"
    assert versions["pymupdf"] == "1.28.2"
    assert versions["xlrd"] == "2.0.2"


def test_windows_doctor_enforces_formal_runtime_versions():
    assert EXPECTED_PYTHON == (3, 12, 10)
    assert _node_supported((22, 23, 1))
    assert _node_supported((22, 99, 0))
    assert not _node_supported((22, 23, 0))
    assert not _node_supported((24, 14, 0))
    assert not _node_supported(None)



def test_windows_doctor_report_never_serializes_provider_secrets(monkeypatch):
    marker = "synthetic-windows-doctor-secret-must-not-appear"
    monkeypatch.setenv("DEEPSEEK_API_KEY", marker)
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", marker)

    encoded = json.dumps(build_report(load_ner=False), ensure_ascii=False)

    assert marker not in encoded


def test_windows_doctor_uses_explicit_offline_model_folder(tmp_path, monkeypatch):
    from scripts.doctor_windows import MODEL_FILES, _check_model
    for name in MODEL_FILES:
        (tmp_path / name).write_bytes(b'synthetic model metadata')
    monkeypatch.setenv('TUOMIN_NER_MODEL_DIR', str(tmp_path))
    def reject_cache_lookup(**kwargs):
        raise AssertionError("Explicit model path should take precedence over cache lookup")
    monkeypatch.setitem(sys.modules, "huggingface_hub", SimpleNamespace(snapshot_download=reject_cache_lookup))
    report = _check_model(False)
    assert report['status'] == 'ok' and report['snapshot'] == str(tmp_path)
