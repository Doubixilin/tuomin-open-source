from __future__ import annotations

import json
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from tuomin_gateway.service.app import create_app  # noqa: E402
from tuomin_gateway.service.registry import AppRegistry  # noqa: E402
from tuomin_gateway.store import MappingStore  # noqa: E402


ROOT = Path(__file__).resolve().parents[1]
EXAMPLES = ROOT / "examples"
DOCS = ROOT / "docs" / "integration"
FORBIDDEN_SNIPPETS = (
    "".join(("真", "实", "客", "户")),
    "".join(("真", "实", "项", "目")),
    "".join(("真", "实", "账", "号")),
    "sk" + "-",
    "AK" + "IA",
    "Bearer" + " ",
)


def _load_example(name: str) -> dict:
    return json.loads((EXAMPLES / name).read_text(encoding="utf-8"))


def test_integration_example_json_files_are_parseable_and_synthetic():
    for name in (
        "dify-redact-request.json",
        "dify-refill-request.json",
        "langgraph-state-example.json",
    ):
        raw = (EXAMPLES / name).read_text(encoding="utf-8")
        payload = json.loads(raw)
        assert isinstance(payload, dict)
        assert not any(snippet in raw for snippet in FORBIDDEN_SNIPPETS)


def test_dify_examples_match_local_redact_refill_contract(tmp_path):
    client = TestClient(
        create_app(
            registry=AppRegistry({"dify_synthetic_demo": {"profile": "kb"}}),
            store=MappingStore(tmp_path / "maps", ttl_seconds=3600),
            session_ttl=3600,
        )
    )
    redact_request = _load_example("dify-redact-request.json")
    refill_request = _load_example("dify-refill-request.json")

    # The redact example no longer carries a client-chosen task_id (rejected
    # with 400); the workflow routes the server-minted id from the response.
    assert "task_id" not in redact_request
    redacted = client.post("/redact", json=redact_request)
    assert redacted.status_code == 200
    redacted_payload = redacted.json()
    assert redacted_payload["task_id"].startswith("task_")
    assert "<CONTACT_001>" in redacted_payload["redacted_text"]
    assert "13900001111" not in redacted_payload["redacted_text"]
    assert {"task_id", "redacted_text", "risk_summary", "audit", "blocked_labels"}.issubset(redacted_payload)

    refill = client.post(
        "/refill", json={**refill_request, "task_id": redacted_payload["task_id"]}
    )
    assert refill.status_code == 200
    refill_payload = refill.json()
    assert refill_payload["status"] == "ok"
    assert "13900001111" in refill_payload["text"]
    assert "<CONTACT_001>" not in refill_payload["text"]


def test_redact_rejects_client_supplied_task_id(tmp_path):
    client = TestClient(
        create_app(
            registry=AppRegistry({"dify_synthetic_demo": {"profile": "kb"}}),
            store=MappingStore(tmp_path / "maps", ttl_seconds=3600),
        )
    )

    response = client.post(
        "/redact",
        json={
            "app_id": "dify_synthetic_demo",
            "task_id": "doc1",
            "text": "联系电话13900001111",
        },
    )

    assert response.status_code == 400
    # And the rejected id must NOT have created a loadable mapping.
    refill = client.post("/refill", json={"task_id": "doc1", "text": "<CONTACT_001>"})
    assert refill.status_code == 404


def test_dify_refill_missing_mapping_contract_is_human_review_branch(tmp_path):
    client = TestClient(create_app(registry=AppRegistry({}), store=MappingStore(tmp_path / "maps")))

    response = client.post("/refill", json={"task_id": "missing_synthetic_task", "text": "<CONTACT_001>"})

    assert response.status_code == 404


def test_integration_docs_reference_local_contract_files_and_no_mapping_upload():
    dify = (DOCS / "dify-local-gateway.md").read_text(encoding="utf-8")
    langgraph = (DOCS / "langgraph-local-gateway.md").read_text(encoding="utf-8")
    combined = dify + langgraph

    assert "http://127.0.0.1:8765/redact" in dify
    assert "http://127.0.0.1:8765/refill" in dify
    assert "human_review" in langgraph
    assert "examples/dify-redact-request.json" in dify
    assert "examples/langgraph-state-example.json" in langgraph
    assert "Do not upload mapping" in combined
