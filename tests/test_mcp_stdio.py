"""Tests for the STDIO MCP adapter (agent-contract-v1, 阶段 9)."""
from __future__ import annotations

import json
import threading
import time

from fastapi.testclient import TestClient  # noqa: F401  (kept for parity with other suites)

from tuomin_gateway.gateway_discovery import Resolution
from tuomin_gateway.mcp_stdio import (  # noqa: F401
    DEFAULT_GATEWAY_URL,
    AgentGateway,
    TOOLS,
    dispatch,
    main,
    resolve_gateway_from_env,
)
from tuomin_gateway.service.app import create_app
from tuomin_gateway.service.registry import AppRegistry, hash_capability_token
from tuomin_gateway.store import MappingStore

REDACT_TOKEN = "synthetic-redact-token"
NAMESPACE_TOKEN = "synthetic-namespace-token"
ORG = "示例建设单位A"


# --- dispatch-level tests (fake poster, no network) ---------------------------

def _fake_gateway(canned):
    def poster(url, headers, payload, method):
        for path, response in canned.items():
            if url.endswith(path):
                return response
        raise AssertionError(f"unexpected call: {url}")
    return AgentGateway("http://fake", "rt", "nt", http_post=poster)


def _call_tool(gateway, name, arguments):
    response = dispatch(gateway, {
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": name, "arguments": arguments},
    })
    assert response["id"] == 1
    assert "result" in response
    content = response["result"]["content"][0]
    assert content["type"] == "text"
    return json.loads(content["text"]), response["result"].get("isError", False)


def test_initialize_and_tools_list():
    gateway = _fake_gateway({})
    init = dispatch(gateway, {"jsonrpc": "2.0", "id": 1, "method": "initialize"})
    assert init["result"]["protocolVersion"]
    assert init["result"]["serverInfo"]["name"] == "tuomin-mcp"

    tools = dispatch(gateway, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
    names = {tool["name"] for tool in tools["result"]["tools"]}
    assert names == {"tuomin_readiness", "tuomin_redact_text", "tuomin_redact_values"}
    # 红线：不暴露 refill / mapping / admin 工具
    assert not any("refill" in n or "mapping" in n or "admin" in n or "unlock" in n for n in names)


def test_notification_returns_no_response():
    assert dispatch(_fake_gateway({}), {"jsonrpc": "2.0", "method": "notifications/initialized"}) is None


def test_unknown_method_and_tool_errors():
    gateway = _fake_gateway({})
    resp = dispatch(gateway, {"jsonrpc": "2.0", "id": 9, "method": "bogus/method"})
    assert resp["error"]["code"] == -32601
    resp = dispatch(gateway, {"jsonrpc": "2.0", "id": 10, "method": "tools/call",
                              "params": {"name": "tuomin_refill", "arguments": {}}})
    assert resp["error"]["code"] == -32602


def test_redact_text_tool_wraps_contract():
    canned = {
        "/api/v1/redact": (200, {
            "status": "ok", "masked_text": "拨打 <CONTACT_001>",
            "mapping_handle": "mh_x", "egress_allowed": True,
            "blocked_labels": [], "label_counts": {"CONTACT": 1},
            "detectors": {"required": ["rules"], "active": ["rules"], "degraded": False},
        }),
    }
    payload, is_error = _call_tool(_fake_gateway(canned), "tuomin_redact_text",
                                   {"app_id": "a", "text": "拨打 13800138000"})
    assert not is_error
    assert payload["contract"] == "agent-contract-v1"
    assert payload["status"] == "ok"
    assert payload["job_id"] == "mh_x"
    assert payload["detectors"] == {"degraded": False, "missing_required": []}


def test_tool_error_becomes_tool_result_not_transport_error():
    canned = {"/api/v1/redact": (409, {"status": "error", "error": {"code": "reserved_placeholder_conflict", "message": "x"}})}
    payload, is_error = _call_tool(_fake_gateway(canned), "tuomin_redact_text",
                                   {"app_id": "a", "text": "<ORG_001>"})
    assert is_error is True
    assert payload["status"] == "error"
    assert payload["egress_allowed"] is False
    assert payload["error"]["code"] == "reserved_placeholder_conflict"


def test_missing_arguments_are_tool_errors():
    payload, is_error = _call_tool(_fake_gateway({}), "tuomin_redact_text", {"app_id": "a"})
    assert is_error is True
    assert payload["error"]["code"] == "invalid_arguments"


# --- gateway URL resolution (no hard-coded, rotting port) --------------------

def test_resolve_gateway_from_env_prefers_explicit_url():
    calls = []

    def resolver(app, env):
        calls.append(app)
        return Resolution("http://127.0.0.1:8775", None, [], True, "stub")

    url = resolve_gateway_from_env(
        {"TUOMIN_GATEWAY_URL": "http://127.0.0.1:9999"}, resolver=resolver,
    )
    assert url == "http://127.0.0.1:9999"
    assert calls == []  # explicit wins; discovery never runs


def test_resolve_gateway_from_env_uses_discovered_url():
    def resolver(app, env):
        assert app == "wb_test"
        return Resolution("http://127.0.0.1:8775", None, [], True, "stub")

    url = resolve_gateway_from_env({"TUOMIN_AGENT_APP": "wb_test"}, resolver=resolver)
    assert url == "http://127.0.0.1:8775"


def test_resolve_gateway_from_env_falls_back_when_discovery_fails():
    def broken(app, env):
        raise RuntimeError("boom")

    assert resolve_gateway_from_env({}, resolver=broken) == DEFAULT_GATEWAY_URL
    assert resolve_gateway_from_env({}, resolver=lambda app, env: Resolution(None, None, [], False, "none")) == DEFAULT_GATEWAY_URL


# --- live wiring test: real gateway over HTTP ---------------------------------

def test_live_gateway_round_trip(tmp_path, monkeypatch):
    import socket

    import uvicorn

    # conftest 的 no_real_dns fixture 把所有 getaddrinfo 指向文档 IP，会让
    # urllib 连不上本机网关；本测试只把答案换成本机回环，仍不做真实 DNS。
    def localhost_getaddrinfo(host, port=0, *args, **kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", port or 0))]

    monkeypatch.setattr(socket, "getaddrinfo", localhost_getaddrinfo)

    dictionary = tmp_path / "dict.json"
    dictionary.write_text(
        json.dumps([{"canonical_value": ORG, "aliases": [], "label": "ORG", "status": "active"}],
                   ensure_ascii=False),
        encoding="utf-8",
    )
    registry = AppRegistry({
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
        }
    })
    app = create_app(registry=registry, store=MappingStore(tmp_path / "maps"))
    config = uvicorn.Config(app, host="127.0.0.1", port=0, log_level="error")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.time() + 10
    while not server.started and time.time() < deadline:
        time.sleep(0.05)
    assert server.started
    port = server.servers[0].sockets[0].getsockname()[1]

    try:
        gateway = AgentGateway(
            f"http://127.0.0.1:{port}", REDACT_TOKEN, NAMESPACE_TOKEN
        )
        ready = gateway.readiness("agent_app")
        assert ready["ready"] is True
        assert ready["app"]["app_id"] == "agent_app"

        result = gateway.redact_text("agent_app", f"发包方：{ORG}")
        assert result["status"] == "ok"
        assert ORG not in result["masked_text"]
        assert result["egress_allowed"] is True
        assert result["job_id"].startswith("mh_")
        assert ORG not in json.dumps(result, ensure_ascii=False)
    finally:
        server.should_exit = True
        thread.join(timeout=5)
