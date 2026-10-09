"""Agent-facing contract tests (agent-contract-v1, docs/design/agent-facing-contract.md).

For every tool in the agent contract, prove the backing /api/v1 endpoint can
supply every required field — the STDIO MCP adapter will be a pure mapping
with no new privileged surface. Also pin the red lines: no original values,
no refill capability, stable error envelopes.
"""
from __future__ import annotations

import json

from fastapi.testclient import TestClient

from tuomin_gateway.service.app import create_app
from tuomin_gateway.service.registry import AppRegistry, hash_capability_token
from tuomin_gateway.store import MappingStore

REDACT_TOKEN = "synthetic-redact-token"
NAMESPACE_TOKEN = "synthetic-namespace-token"
SECRET = "13800138000"
ORG = "示例建设单位A"


def _client(tmp_path):
    dictionary = tmp_path / "dict.json"
    dictionary.write_text(
        json.dumps(
            [{"canonical_value": ORG, "aliases": [], "label": "ORG", "status": "active"}],
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    registry = AppRegistry(
        {
            "agent_app": {
                "profile": "kb",
                "dictionary": str(dictionary),
                "capabilities": ["detect", "redact", "namespace"],
                "capability_tokens": {
                    "detect": hash_capability_token(REDACT_TOKEN),
                    "redact": hash_capability_token(REDACT_TOKEN),
                    "namespace": hash_capability_token(NAMESPACE_TOKEN),
                },
                "mapping_scopes": ["document", "namespace"],
                "structured_value_labels": ["BANK_ACCOUNT", "CONTACT"],
            }
        }
    )
    return TestClient(
        create_app(registry=registry, store=MappingStore(tmp_path / "maps"))
    )


def _auth(token=REDACT_TOKEN):
    return {"x-tuomin-capability-token": token}


# --- tool 1: tuomin_readiness backing ----------------------------------------

def test_readiness_backing_supplies_agent_schema(tmp_path):
    client = _client(tmp_path)
    resp = client.get("/api/v1/readiness", params={"app_id": "agent_app"}, headers=_auth(NAMESPACE_TOKEN))
    assert resp.status_code == 200, resp.text
    data = resp.json()

    assert data["status"] == "ready"
    app = data["app"]
    for field in ("app_id", "profile_name", "profile_version",
                  "dictionary_configured", "dictionary_version",
                  "structured_value_labels"):
        assert field in app, field
    assert app["app_id"] == "agent_app"
    assert app["dictionary_configured"] is True
    assert "ner" in data["detectors"]
    assert ORG not in json.dumps(data, ensure_ascii=False)


def test_readiness_unknown_app_is_stable_error(tmp_path):
    client = _client(tmp_path)
    resp = client.get("/api/v1/readiness", params={"app_id": "ghost"}, headers=_auth(NAMESPACE_TOKEN))
    assert resp.status_code in (401, 403, 404, 503)
    body = resp.json()
    assert body.get("status") in ("error", "not_ready")


# --- tool 2: tuomin_redact_text backing ---------------------------------------

def test_redact_text_backing_supplies_agent_schema(tmp_path):
    client = _client(tmp_path)
    resp = client.post(
        "/api/v1/redact",
        headers=_auth(),
        json={"app_id": "agent_app", "text": f"发包方：{ORG}，联系电话{SECRET}。"},
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()

    # agent-contract-v1 required fields
    assert data["status"] == "ok"
    assert isinstance(data["masked_text"], str)
    assert isinstance(data["egress_allowed"], bool)
    assert isinstance(data["blocked_labels"], list)
    assert isinstance(data["label_counts"], dict)
    assert isinstance(data["mapping_handle"], str) and data["mapping_handle"].startswith("mh_")
    assert "degraded" in data["detectors"]

    # redaction actually happened with the production dictionary
    assert ORG not in data["masked_text"]
    assert SECRET not in data["masked_text"]
    assert data["label_counts"] == {"CONTACT": 1, "ORG": 1}

    # red line: no original values anywhere in the response
    blob = json.dumps(data, ensure_ascii=False)
    assert ORG not in blob
    assert SECRET not in blob


def test_redact_text_reserved_placeholder_is_stable_error(tmp_path):
    client = _client(tmp_path)
    resp = client.post(
        "/api/v1/redact",
        headers=_auth(),
        json={"app_id": "agent_app", "text": "请看 <ORG_001> 的条款"},
    )
    assert resp.status_code == 409
    body = resp.json()
    assert body["status"] == "error"
    assert body["error"]["code"] == "reserved_placeholder_conflict"
    assert body["egress_allowed"] is False


def test_redact_text_requires_capability_token(tmp_path):
    client = _client(tmp_path)
    resp = client.post("/api/v1/redact", json={"app_id": "agent_app", "text": "hello"})
    assert resp.status_code in (401, 403)


# --- tool 3: tuomin_redact_values backing -------------------------------------

def test_redact_values_backing_supplies_agent_schema(tmp_path):
    client = _client(tmp_path)
    ns = client.post(
        "/api/v1/namespaces", headers={"x-tuomin-capability-token": NAMESPACE_TOKEN},
        json={"app_id": "agent_app"},
    )
    assert ns.status_code == 200, ns.text
    namespace_id = ns.json()["namespace_id"]

    resp = client.post(
        "/api/v1/redact/values",
        headers=_auth(NAMESPACE_TOKEN),
        json={
            "app_id": "agent_app",
            "namespace_id": namespace_id,
            "items": [{"id": "c1", "label": "BANK_ACCOUNT", "value": "6222020202020202020"}],
        },
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()

    assert data["status"] == "ok"
    assert data["items"] and set(data["items"][0]) == {"id", "masked_value"}
    assert data["items"][0]["masked_value"].startswith("<BANK_ACCOUNT_")
    assert isinstance(data["egress_allowed"], bool)
    assert isinstance(data["mapping_handle"], str) and data["mapping_handle"].startswith("mh_")
    assert "6222020202020202020" not in json.dumps(data, ensure_ascii=False)


def test_redact_values_label_outside_allowlist_denied(tmp_path):
    client = _client(tmp_path)
    ns = client.post(
        "/api/v1/namespaces", headers={"x-tuomin-capability-token": NAMESPACE_TOKEN},
        json={"app_id": "agent_app"},
    )
    resp = client.post(
        "/api/v1/redact/values",
        headers=_auth(NAMESPACE_TOKEN),
        json={
            "app_id": "agent_app",
            "namespace_id": ns.json()["namespace_id"],
            "items": [{"id": "c1", "label": "PERSON", "value": "张三"}],
        },
    )
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "structured_value_label_denied"
