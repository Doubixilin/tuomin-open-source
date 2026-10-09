from __future__ import annotations

import json

from fastapi import FastAPI
from fastapi.testclient import TestClient

from tuomin_gateway.detectors.base import BaseDetector
from tuomin_gateway.inspection import InspectionVault
from tuomin_gateway.profiles import Profile
from tuomin_gateway.service.proxy import ForwardResult, register_proxy
from tuomin_gateway.service.registry import AppRegistry, hash_capability_token
from tuomin_gateway.service.v1 import register_v1_api
from tuomin_gateway.store import MappingStore, PlaintextMappingCipher


TOKENS = {
    "proxy_session": "proxy-session-secret",
    "mapping_inspect": "mapping-inspect-secret",
}


class _PlaceholderFragmentDetector(BaseDetector):
    name = "placeholder-fragment-test"
    version = "test-v1"

    def detect(self, text: str):
        start = text.index("009>")
        return [
            self.make_span(
                text=text,
                start=start,
                end=start + len("009>"),
                label="ORG",
                confidence=0.99,
                risk_level="high",
            )
        ]


def test_inspection_coverage_ignores_detections_inside_placeholder_tokens(
    tmp_path,
):
    vault = InspectionVault(
        MappingStore(
            tmp_path / "inspections",
            cipher=PlaintextMappingCipher(),
            ttl_seconds=3600,
        )
    )

    summary = vault.create(
        app_id="research_client",
        project_id="project-1",
        run_id="run-1",
        scope="prelim",
        terminal_status="restored",
        entries=[],
        trace=[],
        coverage_items=[{"id": "block-1", "text": "已替换<ORG_009>"}],
        detectors=[_PlaceholderFragmentDetector()],
        profile=Profile(name="inspection-test", use_ner=False),
    )

    assert summary["coverage_finding_count"] == 0


def _registry(
    *,
    openai_upstream: str | None = None,
    proxy_targets: dict[str, dict[str, str]] | None = None,
) -> AppRegistry:
    entry = {
        "profile": {
            "base": "strict",
            "use_ner": False,
            "ner_required": False,
        },
        "capabilities": ["proxy_session", "mapping_inspect"],
        "capability_tokens": {
            name: hash_capability_token(value)
            for name, value in TOKENS.items()
        },
        "proxy_routes": ["openai"],
    }
    if openai_upstream is not None:
        entry["proxy_upstreams"] = {"openai": openai_upstream}
    if proxy_targets is not None:
        entry["proxy_targets"] = proxy_targets
    return AppRegistry(
        {
            "research_client": entry
        }
    )


def test_proxy_session_masks_every_request_refills_locally_and_snapshots(
    tmp_path, monkeypatch
):
    captured: list[dict] = []

    async def forward(_upstream, _headers, body, *, stream):
        assert stream is False
        payload = json.loads(body)
        captured.append(payload)
        masked = payload["messages"][-1]["content"]
        return ForwardResult(
            200,
            {"content-type": "application/json"},
            json.dumps(
                {"choices": [{"message": {"content": f"已核对：{masked}"}}]},
                ensure_ascii=False,
            ).encode(),
        )

    monkeypatch.delenv("TUOMIN_UPSTREAM_OPENAI", raising=False)
    store = MappingStore(
        tmp_path / "maps", cipher=PlaintextMappingCipher(), ttl_seconds=3600
    )
    inspections = InspectionVault(
        store.child("inspections", ttl_seconds=3600)
    )
    app = FastAPI()
    registry = _registry(
        openai_upstream="https://provider.example/v1/chat/completions"
    )
    register_v1_api(app, registry, store, inspection_vault=inspections)
    register_proxy(
        app,
        registry,
        forwarder=forward,
        store=store,
        inspection_vault=inspections,
    )
    client = TestClient(app)

    created = client.post(
        "/api/v1/proxy-sessions",
        headers={"x-tuomin-capability-token": TOKENS["proxy_session"]},
        json={
            "app_id": "research_client",
            "project_id": "project-1",
            "run_id": "run-1",
            "thread_id": "followup-1",
            "provider_route": "openai",
        },
    )
    assert created.status_code == 200
    session = created.json()
    session_id = session["proxy_session_id"]
    session_token = session["proxy_session_token"]
    active_inspection_id = session["inspection_id"]
    assert session["terminal_status"] == "active"
    assert session["coverage_complete"] is False
    assert session["base_url"].endswith(f"/proxy-sessions/{session_id}/v1")

    request = client.post(
        f"/proxy-sessions/{session_id}/v1/chat/completions",
        headers={"x-tuomin-proxy-session-token": session_token},
        json={
            "model": "test",
            "tools": [
                {
                    "name": "lookup_contact",
                    "description": "查询联系人手机13800138000",
                    "input_schema": {
                        "$schema": "https://json-schema.org/draft/2020-12/schema",
                        "type": "object",
                    },
                }
            ],
            "messages": [
                {"role": "system", "content": "test@example.com"},
                {"role": "user", "content": "请核对联系人手机13800138000"}
            ],
        },
    )
    assert request.status_code == 200
    assert request.headers["x-tuomin-response-mode"] == "trusted-local-transparent"
    assert "13800138000" in request.json()["choices"][0]["message"]["content"]
    assert "13800138000" not in json.dumps(captured, ensure_ascii=False)
    assert "test@example.com" not in json.dumps(captured, ensure_ascii=False)
    assert "<CONTACT_001>" in json.dumps(captured, ensure_ascii=False)

    active = client.post(
        f"/api/v1/inspections/{active_inspection_id}/query",
        headers={"x-tuomin-capability-token": TOKENS["mapping_inspect"]},
        json={
            "app_id": "research_client",
            "category": "entries",
            "offset": 0,
            "limit": 50,
        },
    )
    assert active.status_code == 200
    assert active.json()["terminal_status"] == "active"
    assert active.json()["coverage_complete"] is False
    assert active.json()["total"] == 2

    closed = client.post(
        f"/api/v1/proxy-sessions/{session_id}/close",
        headers={"x-tuomin-proxy-session-token": session_token},
    )
    assert closed.status_code == 200
    inspection_id = closed.json()["inspection_id"]
    assert inspection_id == active_inspection_id
    assert closed.json()["entry_count"] == 2
    assert closed.json()["coverage_finding_count"] == 0
    assert closed.json()["coverage_complete"] is True

    queried = client.post(
        f"/api/v1/inspections/{inspection_id}/query",
        headers={"x-tuomin-capability-token": TOKENS["mapping_inspect"]},
        json={
            "app_id": "research_client",
            "category": "entries",
            "offset": 0,
            "limit": 50,
        },
    )
    assert queried.status_code == 200
    items = {item["original_value"]: item for item in queried.json()["items"]}
    assert set(items) == {"13800138000", "test@example.com"}
    assert items["13800138000"]["placeholder"] == "<CONTACT_001>"
    assert items["13800138000"]["source_ids"] == ["request-0001"]
    assert items["test@example.com"]["source_ids"] == ["request-0001"]
    assert items["13800138000"]["source"] == "rule"
    assert items["13800138000"]["detector_version"] == (
        "rules-2026.09.14-secrets-pem"
    )

    expired = client.post(
        f"/proxy-sessions/{session_id}/v1/chat/completions",
        headers={"x-tuomin-proxy-session-token": session_token},
        json={"model": "test", "messages": []},
    )
    assert expired.status_code == 404


def test_proxy_session_creation_fails_before_launch_when_upstream_missing(
    tmp_path, monkeypatch
):
    monkeypatch.delenv("TUOMIN_UPSTREAM_OPENAI", raising=False)
    app = FastAPI()
    register_proxy(
        app,
        _registry(),
        store=MappingStore(
            tmp_path / "maps", cipher=PlaintextMappingCipher(), ttl_seconds=3600
        ),
    )
    client = TestClient(app)

    response = client.post(
        "/api/v1/proxy-sessions",
        headers={"x-tuomin-capability-token": TOKENS["proxy_session"]},
        json={
            "app_id": "research_client",
            "project_id": "project-1",
            "run_id": "run-1",
            "thread_id": "followup-1",
            "provider_route": "openai",
        },
    )

    assert response.status_code == 503
    assert response.json()["error"] == {
        "code": "proxy_upstream_unavailable",
        "message": "proxy upstream unavailable",
    }


def test_named_proxy_target_is_required_pinned_and_content_minimized(
    tmp_path, monkeypatch
):
    forwarded: list[tuple[str, dict[str, str]]] = []

    async def forward(upstream, headers, _body, *, stream):
        assert stream is False
        forwarded.append((upstream, headers))
        return ForwardResult(
            200,
            {"content-type": "application/json"},
            json.dumps({"choices": [{"message": {"content": "ok"}}]}).encode(),
        )

    monkeypatch.setenv(
        "TUOMIN_UPSTREAM_OPENAI",
        "https://wrong-environment.example/v1/chat/completions",
    )
    targets = {
        "deepseek": {
            "route": "openai",
            "upstream": "https://deepseek.example/v1/chat/completions",
        },
        "kimi": {
            "route": "openai",
            "upstream": "https://kimi.example/v1/chat/completions",
        },
    }
    app = FastAPI()
    register_proxy(
        app,
        _registry(proxy_targets=targets),
        forwarder=forward,
        store=MappingStore(
            tmp_path / "maps", cipher=PlaintextMappingCipher(), ttl_seconds=3600
        ),
    )
    client = TestClient(app)

    listed = client.get(
        "/api/v1/proxy-targets",
        params={"app_id": "research_client"},
        headers={"x-tuomin-capability-token": TOKENS["proxy_session"]},
    )
    assert listed.status_code == 200
    assert listed.json()["targets"] == [
        {"upstream_id": "deepseek", "provider_route": "openai"},
        {"upstream_id": "kimi", "provider_route": "openai"},
    ]
    assert "example" not in json.dumps(listed.json())

    base_payload = {
        "app_id": "research_client",
        "project_id": "project-1",
        "run_id": "run-1",
        "thread_id": "followup-1",
        "provider_route": "openai",
    }
    missing = client.post(
        "/api/v1/proxy-sessions",
        headers={"x-tuomin-capability-token": TOKENS["proxy_session"]},
        json=base_payload,
    )
    assert missing.status_code == 400
    assert missing.json()["error"]["code"] == "proxy_target_required"

    created = client.post(
        "/api/v1/proxy-sessions",
        headers={"x-tuomin-capability-token": TOKENS["proxy_session"]},
        json={**base_payload, "upstream_id": "kimi"},
    )
    assert created.status_code == 200
    session = created.json()
    assert session["upstream_id"] == "kimi"

    response = client.post(
        f"/proxy-sessions/{session['proxy_session_id']}/v1/chat/completions",
        headers={
            "x-tuomin-proxy-session-token": session["proxy_session_token"],
            "x-tuomin-upstream": "https://attacker.example/v1/chat/completions",
            "authorization": "Bearer kimi-secret",
        },
        json={"model": "k3", "messages": []},
    )
    assert response.status_code == 200
    assert forwarded == [
        (
            "https://kimi.example/v1/chat/completions",
            {
                "authorization": "Bearer kimi-secret",
                "content-type": "application/json",
            },
        )
    ]


def test_proxy_session_rejects_wrong_capability_and_unregistered_route(tmp_path):
    store = MappingStore(
        tmp_path / "maps", cipher=PlaintextMappingCipher(), ttl_seconds=3600
    )
    app = FastAPI()
    register_proxy(app, _registry(), store=store)
    client = TestClient(app)
    payload = {
        "app_id": "research_client",
        "project_id": "project-1",
        "run_id": "run-1",
        "thread_id": "followup-1",
        "provider_route": "anthropic",
    }
    denied = client.post(
        "/api/v1/proxy-sessions",
        headers={"x-tuomin-capability-token": "wrong"},
        json=payload,
    )
    assert denied.status_code == 403
    route = client.post(
        "/api/v1/proxy-sessions",
        headers={"x-tuomin-capability-token": TOKENS["proxy_session"]},
        json=payload,
    )
    assert route.status_code == 403
    assert route.json()["error"]["code"] == "proxy_route_denied"
