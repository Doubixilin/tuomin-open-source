from __future__ import annotations

from dataclasses import replace
import json
import os
from pathlib import Path
import runpy
import threading
import time

import pytest
from fastapi.testclient import TestClient

from tuomin_gateway.detectors.rules import RuleDetector, UnsafeCredentialInput
from tuomin_gateway.profiles import Profile, apply_profile
from tuomin_gateway.redactor import redact_text
from tuomin_gateway.refill import refill_text
from tuomin_gateway.session import build_detectors, run_detection

ROOT = Path(__file__).resolve().parents[1]


def pem(kind="RSA ", newline="\n"):
    return f"-----BEGIN {kind}PRIVATE KEY-----{newline}MIIBSYNTHETICBODY{newline}-----END {kind}PRIVATE KEY-----"


@pytest.mark.parametrize("kind", ["", "RSA ", "EC ", "OPENSSH ", "DSA ", "ENCRYPTED "])
@pytest.mark.parametrize("newline", ["\n", "\r\n"])
def test_entire_private_block_roundtrip(kind, newline):
    text = "前文\n" + pem(kind, newline) + "\n后文"
    spans = RuleDetector().detect(text)
    result = redact_text(text, spans)
    assert result.redacted_text == "前文\n<CREDENTIAL_001>\n后文"
    assert refill_text(result.redacted_text, result.mapping).text == text


@pytest.mark.parametrize("text", [
    "-----BEGIN RSA PRIVATE KEY-----\nSYNTHETICBODY",
    "SYNTHETICBODY\n-----END RSA PRIVATE KEY-----",
    "-----BEGIN RSA PRIVATE KEY-----\nBODY\n-----END EC PRIVATE KEY-----",
    "-----BEGIN RSA PRIVATE KEY\nBODY",
    "-----BEGIN RSA PRIVATE\nBODY",
    "-----BEGIN PRIVATE KEY-----\n-----END PRIVATE KEY-----",
    "-----BEGIN PRIVATE KEY-----\n" + pem() + "\n-----END PRIVATE KEY-----",
])
def test_malformed_pem_blocks_even_custom_detector_paths(text):
    with pytest.raises(UnsafeCredentialInput):
        RuleDetector().detect(text)
    with pytest.raises(UnsafeCredentialInput):
        run_detection(text, [], Profile(name="synthetic", use_ner=False))
    with pytest.raises(UnsafeCredentialInput):
        redact_text(text, [])


def test_private_key_floor_survives_dictionary_preserve_and_overlap():
    text = pem() + "\n" + pem("EC ")
    detectors = build_detectors([{"canonical_value": "MIIBSYNTHETICBODY", "label": "PUBLIC_AUTHORITY"}])
    profile = Profile(name="synthetic", use_ner=False, action={"PUBLIC_AUTHORITY": "pass", "CREDENTIAL": "pass"})
    run = run_detection(text, detectors, profile)
    result = redact_text(text, run.kept)
    assert result.redacted_text == "<CREDENTIAL_001>\n<CREDENTIAL_002>"
    blocked = apply_profile(RuleDetector().detect(text), replace(profile, action={"CREDENTIAL": "block"}))[1]
    assert blocked == ["CREDENTIAL"]


@pytest.fixture
def local_client(tmp_path, monkeypatch):
    from tuomin_gateway.detectors import ner
    from tuomin_gateway.service.app import create_app
    from tuomin_gateway.service.registry import AppRegistry
    from tuomin_gateway.store import MappingStore

    class FakeNer:
        version = "synthetic-ner"
        def detect(self, text): return []
        def _pipeline(self): return object()

    monkeypatch.setattr(ner, "get_ner_detector", lambda: FakeNer())
    dictionary = tmp_path / "dictionary.json"
    dictionary.write_text("[]", encoding="utf-8")
    config = tmp_path / "apps.json"
    config.write_text(json.dumps({"apps": {"production": {"profile": "file_workbench", "dictionary": str(dictionary)}}}), encoding="utf-8")
    app = create_app(registry=AppRegistry.load(config), store=MappingStore(tmp_path / "maps"), admin_token="synthetic-admin")
    client = TestClient(app, base_url="http://localhost")
    client.headers["x-tuomin-admin-token"] = "synthetic-admin"
    return client


def test_document_pem_masks_body_and_malformed_never_exports(local_client):
    response = local_client.post("/api/v1/documents/parse?filename=pem.txt&app_id=production", content=pem().encode())
    assert response.status_code == 200
    assert "SYNTHETICBODY" not in json.dumps(response.json())
    assert response.json()["egress"]["egress_allowed"] is True
    assert response.json()["redacted_markdown"].strip() == "<CREDENTIAL_001>"
    malformed = pem().split("-----END")[0]
    for route, body in [
        ("/redact", {"json": {"text": malformed, "app_id": "production"}}),
        ("/api/v1/documents/parse?filename=pem.txt&app_id=production", {"content": malformed.encode()}),
    ]:
        response = local_client.post(route, **body)
        assert response.status_code in (400, 409, 422)
        assert "SYNTHETICBODY" not in response.text


def test_mapping_failures_do_not_write_or_replace_original(tmp_path, monkeypatch):
    import tuomin_gateway.mapping as mapping
    from tuomin_gateway.schemas import MappingEntry

    target = tmp_path / "mapping.json"
    target.write_text("old-value", encoding="utf-8")
    entries = [MappingEntry("<ORG_001>", "ORG", "synthetic-private-value", "sha256:test")]
    def fail(*args, **kwargs): raise PermissionError("synthetic failure")
    with monkeypatch.context() as patch:
        patch.setattr(mapping, "restrict_permissions", fail)
        with pytest.raises(PermissionError): mapping.save_mapping(entries, target)
    assert target.read_text() == "old-value"
    with monkeypatch.context() as patch:
        patch.setattr(mapping.os, "replace", fail)
        with pytest.raises(PermissionError): mapping.save_mapping(entries, target)
    assert target.read_text() == "old-value"
    assert list(tmp_path.iterdir()) == [target]


def test_admin_token_reused_but_never_logged(tmp_path, monkeypatch, capsys):
    from tuomin_gateway import runtime
    from tests.permission_assertions import assert_owner_private

    monkeypatch.delenv("TUOMIN_ADMIN_TOKEN", raising=False)
    target = runtime.configure_admin_token(tmp_path)
    first = os.environ["TUOMIN_ADMIN_TOKEN"]
    monkeypatch.delenv("TUOMIN_ADMIN_TOKEN")
    assert runtime.configure_admin_token(tmp_path) == target
    assert os.environ["TUOMIN_ADMIN_TOKEN"] == first
    runtime.report_admin_token_location(target)
    assert first not in capsys.readouterr().out
    assert_owner_private(target, is_dir=False)


def test_admin_token_permission_failure_leaves_no_secret(tmp_path, monkeypatch):
    from tuomin_gateway import runtime
    monkeypatch.delenv("TUOMIN_ADMIN_TOKEN", raising=False)
    original = runtime.restrict_permissions
    def restrict(path, *, is_dir):
        if not is_dir: raise PermissionError("synthetic")
        original(path, is_dir=is_dir)
    monkeypatch.setattr(runtime, "restrict_permissions", restrict)
    with pytest.raises(PermissionError): runtime.configure_admin_token(tmp_path)
    assert not (tmp_path / "admin-token").exists()
    assert "TUOMIN_ADMIN_TOKEN" not in os.environ


def test_preheat_and_readiness_share_load_lock(monkeypatch):
    from tuomin_gateway import runtime, session
    from tuomin_gateway.detectors import ner
    class FakeNer:
        version = "synthetic"
        _lock = threading.Lock()
        loaded = False
        calls = 0
        def _pipeline(self):
            if not self.loaded:
                self.calls += 1
                time.sleep(.05)
                self.loaded = True
        def detect(self, text):
            with self._lock: self._pipeline()
            return []
    detector = FakeNer()
    find_spec = session.importlib.util.find_spec
    monkeypatch.setattr(session.importlib.util, "find_spec",
                        lambda name, *args: object() if name == "transformers" else find_spec(name, *args))
    monkeypatch.setattr(ner, "get_ner_detector", lambda: detector)
    worker = runtime.warm_ner_async()
    assert session.probe_ner_runtime(required=True)["loadable"] is True
    worker.join(timeout=2)
    assert not worker.is_alive()
    assert detector.calls == 1


def test_preheat_disable_and_safe_failure(monkeypatch, capsys):
    from tuomin_gateway import runtime
    from tuomin_gateway.detectors import ner
    monkeypatch.setenv("TUOMIN_WARM_NER", "0")
    assert runtime.warm_ner_async() is None
    monkeypatch.setenv("TUOMIN_WARM_NER", "1")
    def fail(): raise RuntimeError("synthetic-secret-that-must-not-appear")
    monkeypatch.setattr(ner, "get_ner_detector", fail)
    worker = runtime.warm_ner_async()
    worker.join(timeout=2)
    output = capsys.readouterr().out
    assert "unavailable" in output and "warmup done" not in output
    assert "synthetic-secret-that-must-not-appear" not in output


def test_explicit_model_directory_reports_missing_files(tmp_path, monkeypatch):
    from tuomin_gateway.session import _ner_model_cached
    monkeypatch.setenv("TUOMIN_NER_MODEL_DIR", str(tmp_path))
    (tmp_path / "config.json").write_text("{}")
    assert _ner_model_cached() is False


def test_runner_import_has_no_runtime_effects(tmp_path, monkeypatch):
    monkeypatch.setenv("TUOMIN_DATA_DIR", str(tmp_path / "not-created"))
    result = runpy.run_path(str(ROOT / "service_runner.pyw"), run_name="imported_runner")
    assert callable(result["main"])
    assert not (tmp_path / "not-created").exists()


def test_security_battery_never_passes_network_failures(monkeypatch, capsys):
    import urllib.request
    module = runpy.run_path(str(ROOT / "scripts/security_battery_runtime.py"))
    monkeypatch.setenv("TUOMIN_ADMIN_TOKEN", "synthetic")
    monkeypatch.setattr("sys.argv", ["battery", "--base-url", "http://127.0.0.1:18765"])
    def fail(*args, **kwargs): raise OSError("synthetic connection failure")
    monkeypatch.setattr(urllib.request, "urlopen", fail)
    assert module["main"]() == 1
    assert "PASS " not in capsys.readouterr().out


def test_windows_db_open_retry_is_bounded(tmp_path, monkeypatch):
    from tuomin_gateway import jobs
    monkeypatch.setattr(jobs.sys, "platform", "win32")
    monkeypatch.setattr(jobs.time, "sleep", lambda _: None)
    attempts = []
    original = jobs.os.open
    def delayed(*args):
        attempts.append(1)
        if len(attempts) < 3: raise PermissionError("synthetic lock")
        return original(*args)
    with monkeypatch.context() as patch:
        patch.setattr(jobs.os, "open", delayed)
        descriptor = jobs._open_db_file(tmp_path / "jobs.db")
        os.close(descriptor)
    assert len(attempts) == 3
    attempts.clear()
    def denied(*args):
        attempts.append(1)
        raise PermissionError("synthetic persistent denial")
    with monkeypatch.context() as patch:
        patch.setattr(jobs.os, "open", denied)
        with pytest.raises(PermissionError): jobs._open_db_file(tmp_path / "jobs.db")
    assert len(attempts) == 5
def test_launcher_reuses_legacy_token_file_without_logging(tmp_path, monkeypatch, capsys):
    from tests.permission_assertions import assert_owner_private

    monkeypatch.delenv("TUOMIN_ADMIN_TOKEN", raising=False)
    monkeypatch.setenv("TUOMIN_DATA_DIR", str(tmp_path))
    target = tmp_path / "admin-token"
    legacy = "# Paste the token below into the Tuomin WebUI.\nsynthetic-legacy-token\n"
    target.write_text(legacy, encoding="utf-8")
    launcher = runpy.run_path(str(ROOT / "packaging/launcher.py"))
    assert launcher["_data_dir"]() == tmp_path
    launcher["_configure_admin_token"](tmp_path)
    assert os.environ["TUOMIN_ADMIN_TOKEN"] == "synthetic-legacy-token"
    assert target.read_text(encoding="utf-8") == legacy
    assert "synthetic-legacy-token" not in capsys.readouterr().out
    assert_owner_private(target, is_dir=False)
    assert_owner_private(tmp_path, is_dir=True)


def test_explicit_admin_token_remains_authoritative(tmp_path, monkeypatch, capsys):
    from tuomin_gateway import runtime

    monkeypatch.setenv("TUOMIN_ADMIN_TOKEN", "synthetic-configured-token")
    runtime.report_admin_token_location(runtime.configure_admin_token(tmp_path))
    assert os.environ["TUOMIN_ADMIN_TOKEN"] == "synthetic-configured-token"
    assert not (tmp_path / "admin-token").exists()
    assert "synthetic-configured-token" not in capsys.readouterr().out


def test_cli_serve_reuses_token_and_starts_warmup(tmp_path, monkeypatch, capsys):
    from tuomin_gateway import cli
    from tuomin_gateway.service import app as service_app
    import uvicorn

    monkeypatch.setenv("TUOMIN_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("TUOMIN_ADMIN_TOKEN", raising=False)
    configured_tokens = []
    warmups = []
    monkeypatch.setattr(cli, "_pick_port", lambda preferred, host: preferred)
    monkeypatch.setattr(service_app, "create_app", lambda: configured_tokens.append(os.environ["TUOMIN_ADMIN_TOKEN"]))
    monkeypatch.setattr(cli, "_warm_ner_async", lambda: warmups.append(True))
    monkeypatch.setattr(uvicorn, "run", lambda *args, **kwargs: None)
    assert cli.main(["serve", "--port", "18765"]) == 0
    monkeypatch.delenv("TUOMIN_ADMIN_TOKEN")
    assert cli.main(["serve", "--port", "18765"]) == 0
    assert len(configured_tokens) == 2
    assert configured_tokens[0] == configured_tokens[1]
    assert warmups == [True, True]
    output = capsys.readouterr().out
    assert configured_tokens[0] not in output
    assert str(tmp_path / "admin-token") in output


def test_encrypted_store_permission_failure_does_not_persist(tmp_path, monkeypatch):
    from tuomin_gateway import store

    def fail(*args, **kwargs):
        raise PermissionError("synthetic permission denial")

    monkeypatch.setattr(store, "restrict_permissions", fail)
    target = store.MappingStore(tmp_path / "maps", cipher=store.PlaintextMappingCipher())
    with pytest.raises(PermissionError):
        target.save_payload("synthetic-task", b"synthetic-private-value")
    assert not list(tmp_path.rglob("*.mapping.enc"))
