"""Task-based (passphrase-free) refill for the local trusted UI."""
from __future__ import annotations

import base64
import re
import time

from fastapi.testclient import TestClient

from test_documents_api import _SECRET, _bound_client, _parse, _setup, stub_ner  # noqa: F401
from test_office_documents_api import ORG, PHONE, _source
from tuomin_gateway.document.office_patch import extract_office_texts
from tuomin_gateway.service.app import create_app
from tuomin_gateway.service.registry import AppRegistry
from tuomin_gateway.store import MappingStore


def test_task_refill_answer_and_mapping_need_no_package(tmp_path):
    client = _setup(tmp_path)
    parsed = _parse(client)
    task = parsed["task_id"]
    placeholder = re.search(r"<[A-Z_]+_\d{3}>", parsed["redacted_markdown"]).group()

    refill = client.post(f"/api/v1/documents/{task}/refill", json={"redacted_markdown": parsed["redacted_markdown"]})
    assert refill.status_code == 200, refill.text
    assert refill.json()["mode"] == "exact" and _SECRET in refill.json()["restored_markdown"]

    answer = client.post(f"/api/v1/documents/{task}/refill-answer", json={"answer_markdown": f"请拨打 {placeholder}。"})
    assert answer.status_code == 200, answer.text
    assert _SECRET in answer.json()["restored_markdown"]

    mapping = client.post(f"/api/v1/documents/{task}/mapping", json={})
    assert mapping.status_code == 200
    assert mapping.json()["file_name"] == "sample.txt"
    assert any(e["placeholder"] == placeholder and e["original_value"] == _SECRET for e in mapping.json()["entries"])
    audit = client.get("/admin/audit/workbench").json()["events"]
    assert any(e.get("event") == "mapping_unlock" and e.get("source") == "task" and e.get("task_id") == task for e in audit)
    assert _SECRET not in str(audit)

    assert client.post("/api/v1/documents/job-missing/refill", json={"redacted_markdown": "x"}).status_code == 404
    assert client.delete(f"/api/v1/documents/jobs/{task}").status_code == 200
    assert client.post(f"/api/v1/documents/{task}/mapping", json={}).status_code == 404
    no_token = {"x-tuomin-admin-token": ""}
    assert client.post(f"/api/v1/documents/{task}/mapping", json={}, headers=no_token).status_code == 401


def test_task_refill_office_restores_without_package(tmp_path, stub_ner):
    client = _bound_client(tmp_path, [{"canonical_value": ORG, "aliases": [], "label": "ORG", "status": "active"}])
    redacted = client.post("/api/v1/documents/redact-office", params={"filename": "synthetic.xlsx", "app_id": "wb_docs"}, content=_source("xlsx"))
    assert redacted.status_code == 200, redacted.text
    task = redacted.json()["task_id"]
    refill = client.post(f"/api/v1/documents/{task}/refill-office", json={"redacted_office_b64": redacted.json()["redacted_office_b64"]})
    assert refill.status_code == 200, refill.text
    assert refill.json()["mode"] == "exact"
    texts = "\n".join(unit.text for unit in extract_office_texts(base64.b64decode(refill.json()["restored_office_b64"]), "xlsx"))
    assert ORG in texts and PHONE in texts
    wrong = client.post(f"/api/v1/documents/{task}/refill-office", json={"file_format": "pptx", "redacted_office_b64": redacted.json()["redacted_office_b64"]})
    assert wrong.status_code == 409


def test_task_mappings_outlive_the_short_api_grant_ttl(tmp_path):
    app = create_app(
        registry=AppRegistry.load(tmp_path / "apps.json"),
        store=MappingStore(tmp_path / "maps", ttl_seconds=1),
        session_ttl=3600,
        admin_token="test-token",
    )
    client = TestClient(app, base_url="http://localhost")
    client.headers.update({"x-tuomin-admin-token": "test-token"})
    parsed = _parse(client)
    time.sleep(1.3)
    assert client.get("/healthz").status_code == 200  # purges the 1-second API store
    refill = client.post(f"/api/v1/documents/{parsed['task_id']}/refill", json={"redacted_markdown": parsed["redacted_markdown"]})
    assert refill.status_code == 200, refill.text
    assert _SECRET in refill.json()["restored_markdown"]
    assert (tmp_path / "maps" / "documents").is_dir()
