"""Request size limits (Phase 5): fail closed with 413, never truncate.

Covers the legacy endpoints, the capability v1 API (incl. batch), and the
reverse proxy body cap. Limits are env-tunable and read per request.
"""
from __future__ import annotations

import json

from fastapi import FastAPI
from fastapi.testclient import TestClient

from tuomin_gateway.service.app import create_app
from tuomin_gateway.service.proxy import ForwardResult, register_proxy
from tuomin_gateway.service.registry import AppRegistry, hash_capability_token
from tuomin_gateway.store import MappingStore


def _legacy_client(tmp_path):
    return TestClient(
        create_app(
            registry=AppRegistry({"demo": {"profile": "kb"}}),
            store=MappingStore(tmp_path / "maps"),
            session_ttl=3600,
            admin_token="t",
        )
    )


def test_legacy_redact_and_snippet_reject_oversized_text(tmp_path, monkeypatch):
    monkeypatch.setenv("TUOMIN_MAX_TEXT_CHARS", "8")
    client = _legacy_client(tmp_path)

    redact = client.post("/redact", json={"app_id": "demo", "text": "联系电话13900001111"})
    snippet = client.post("/redact_snippet", json={"app_id": "demo", "text": "联系电话13900001111"})

    assert redact.status_code == 413
    assert snippet.status_code == 413
    # And nothing was persisted under any task id for the rejected text.
    refill = client.post("/refill", json={"task_id": "anything", "text": "<CONTACT_001>"})
    assert refill.status_code == 404


def test_session_mask_rejects_oversized_text(tmp_path, monkeypatch):
    client = _legacy_client(tmp_path)
    sid = client.post("/session/open", json={"app_id": "demo"}).json()["session_id"]

    monkeypatch.setenv("TUOMIN_MAX_TEXT_CHARS", "8")
    response = client.post(f"/session/{sid}/mask", json={"text": "联系电话13900001111"})
    assert response.status_code == 413

    # The session itself survives; normal-sized text still masks afterwards.
    monkeypatch.delenv("TUOMIN_MAX_TEXT_CHARS")
    ok = client.post(f"/session/{sid}/mask", json={"text": "电话13900001111"})
    assert ok.status_code == 200
    assert "<CONTACT_001>" in ok.json()["masked"]


# --- v1 capability API -------------------------------------------------------

_TOKEN = "synthetic-limits-token"


def _v1_client(tmp_path):
    registry = AppRegistry(
        {
            "limit_app": {
                "profile": "kb",
                "capabilities": ["redact", "namespace"],
                "capability_tokens": {
                    "redact": hash_capability_token(_TOKEN),
                    "namespace": hash_capability_token(_TOKEN),
                },
                "mapping_scopes": ["document", "namespace"],
            }
        }
    )
    client = TestClient(
        create_app(registry=registry, store=MappingStore(tmp_path / "maps"))
    )
    client.headers.update({"x-tuomin-capability-token": _TOKEN})
    return client


def test_v1_redact_rejects_oversized_text_with_safe_envelope(tmp_path, monkeypatch):
    monkeypatch.setenv("TUOMIN_MAX_TEXT_CHARS", "8")
    client = _v1_client(tmp_path)

    response = client.post(
        "/api/v1/redact", json={"app_id": "limit_app", "text": "联系电话13900001111"}
    )

    assert response.status_code == 413
    payload = response.json()
    assert payload["status"] == "error"
    assert payload["error"]["code"] == "text_too_large"
    assert "13900001111" not in json.dumps(payload, ensure_ascii=False)


def test_v1_batch_limits_items_and_item_text(tmp_path, monkeypatch):
    client = _v1_client(tmp_path)

    monkeypatch.setenv("TUOMIN_MAX_BATCH_ITEMS", "2")
    too_many = client.post(
        "/api/v1/redact/batch",
        json={
            "app_id": "limit_app",
            "items": [
                {"id": "a", "text": "电话13900001111"},
                {"id": "b", "text": "电话13900002222"},
                {"id": "c", "text": "电话13900003333"},
            ],
        },
    )
    assert too_many.status_code == 413
    assert too_many.json()["error"]["code"] == "batch_too_large"

    monkeypatch.setenv("TUOMIN_MAX_TEXT_CHARS", "8")
    big_item = client.post(
        "/api/v1/redact/batch",
        json={
            "app_id": "limit_app",
            "items": [{"id": "a", "text": "联系电话13900001111"}],
        },
    )
    assert big_item.status_code == 413
    assert big_item.json()["error"]["code"] == "text_too_large"


def test_v1_namespace_redact_rejects_oversized_text(tmp_path, monkeypatch):
    client = _v1_client(tmp_path)
    namespace_id = client.post(
        "/api/v1/namespaces", json={"app_id": "limit_app"}
    ).json()["namespace_id"]

    monkeypatch.setenv("TUOMIN_MAX_TEXT_CHARS", "8")
    response = client.post(
        f"/api/v1/namespaces/{namespace_id}/redact",
        json={"app_id": "limit_app", "text": "联系电话13900001111"},
    )
    assert response.status_code == 413
    # Namespace is untouched: a normal redact still works after the rejection.
    monkeypatch.delenv("TUOMIN_MAX_TEXT_CHARS")
    ok = client.post(
        f"/api/v1/namespaces/{namespace_id}/redact",
        json={"app_id": "limit_app", "text": "电话13900001111"},
    )
    assert ok.status_code == 200


# --- reverse proxy body cap ---------------------------------------------------

def test_proxy_rejects_oversized_body_before_forwarding(tmp_path, monkeypatch):
    monkeypatch.setenv("TUOMIN_MAX_REQUEST_BYTES", "64")
    called = {"forwarded": False}

    async def forwarder(url, headers, body, *, stream):
        called["forwarded"] = True
        return ForwardResult(200, body=b"{}")

    app = FastAPI()
    register_proxy(
        app,
        AppRegistry({"warnapp": {"profile": "kb"}}),
        forwarder=forwarder,
        store=MappingStore(tmp_path / "maps"),
    )
    client = TestClient(app)

    response = client.post(
        "/v1/messages",
        headers={"x-tuomin-upstream": "http://fake-upstream.local/v1/messages"},
        json={"model": "x", "messages": [{"role": "user", "content": "你好，这是一段稍长的文本，用来超过六十四个字节的请求体上限。"}]},
    )

    assert response.status_code == 413
    assert response.json()["error"]["code"] == "request_too_large"
    assert called["forwarded"] is False  # rejected before any upstream call


def test_default_limits_allow_normal_requests(tmp_path):
    client = _legacy_client(tmp_path)
    response = client.post("/redact", json={"app_id": "demo", "text": "电话13900001111"})
    assert response.status_code == 200
