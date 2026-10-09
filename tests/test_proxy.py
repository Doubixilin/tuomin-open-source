"""Phase 1: unified reverse proxy e2e (fake upstream, no network).

Proves the masking-forward-refill round trip end to end:
- outbound request body carries ONLY placeholders (no raw values),
- non-streaming response is refilled,
- SSE response is refilled even when a placeholder is split across chunks,
- fail-closed on blocked labels when a profile opts into blocking.

Uses ``profile=kb`` (use_ner=False) so the test needs no optional model/network.
"""
import json
import socket

import pytest

from fastapi import FastAPI
from fastapi.testclient import TestClient

from tuomin_gateway.service.proxy import ForwardResult, register_proxy
from tuomin_gateway.service.registry import AppRegistry
from tuomin_gateway.store import MappingStore
from tuomin_gateway.vault import MappingVault

PHONE = "13900001111"
BANK = "6222020202020202020"
UPSTREAM = "http://fake-upstream.local/v1/messages"
SECRET = "sk-proj-AbCdEfGhIjKlMnOpQrSt"

APPS = {
    "blocky": {
        "profile": {
            "base": "strict",
            "name": "blocky",
            "use_ner": False,
            "action": {"CONTACT": "block"},
            "block_min_severity": "critical",
        }
    },
    "strict_proxy": {
        "profile": {
            "base": "strict",
            "name": "strict_proxy",
            "use_ner": False,
            "ner_required": False,
        }
    },
    # Guard-opted-in (block_min_severity) without input-side CONTACT blocking;
    # lenient refill so the chunked streaming path is exercised too.
    "guardblock": {
        "profile": {
            "base": "strict",
            "name": "guardblock",
            "use_ner": False,
            "ner_required": False,
            "refill_strict": False,
            "block_min_severity": "critical",
        }
    },
}


class _Capture:
    def __init__(self, response: ForwardResult):
        self.response = response
        self.body: bytes | None = None
        self.headers: dict | None = None

    async def __call__(self, url, headers, body, *, stream):
        self.body = body
        self.headers = headers
        return self.response


def _client(forwarder):
    app = FastAPI()
    register_proxy(
        app, AppRegistry(APPS), forwarder=forwarder, legacy_auto_refill=True
    )
    return TestClient(app)


def _headers(profile="kb"):
    return {"x-tuomin-upstream": UPSTREAM, "x-tuomin-profile": profile}


def test_outbound_is_masked_and_nonstream_response_refilled():
    # Model "sees" a placeholder and echoes it; we must refill it back.
    canned = {
        "id": "msg_1",
        "content": [{"type": "text", "text": "请拨打 <CONTACT_001> 联系。"}],
    }
    cap = _Capture(ForwardResult(200, {"content-type": "application/json"},
                                 body=json.dumps(canned, ensure_ascii=False).encode("utf-8")))
    client = _client(cap)

    resp = client.post(
        "/v1/messages",
        headers=_headers(),
        json={"model": "x", "messages": [{"role": "user", "content": f"我的电话是{PHONE}。"}]},
    )
    assert resp.status_code == 200

    # 1) outbound body masked: no raw phone, placeholder present
    outbound = cap.body.decode("utf-8")
    assert PHONE not in outbound
    assert "<CONTACT_001>" in outbound

    # 2) response refilled back to the original value
    data = resp.json()
    assert PHONE in data["content"][0]["text"]
    assert "<CONTACT_001>" not in data["content"][0]["text"]
    assert resp.headers["x-tuomin-refill-restored-count"] == "1"
    assert resp.headers["x-tuomin-policy-schema"] == "policy-dimensions-v1"


def test_outbound_masks_tool_prose_and_object_arguments_only():
    cap = _Capture(
        ForwardResult(
            200,
            {"content-type": "application/json"},
            body=json.dumps({"content": []}).encode(),
        )
    )
    client = _client(cap)

    response = client.post(
        "/v1/messages",
        headers=_headers(),
        json={
            "model": "deepseek-v4-pro",
            "messages": [
                {
                    "role": "assistant",
                    "content": "准备调用工具",
                    "tool_calls": [
                        {
                            "function": {
                                "name": "lookup_contact",
                                "arguments": {"contact": PHONE, "count": 2},
                            }
                        }
                    ],
                }
            ],
            "tools": [
                {
                    "name": "lookup_contact",
                    "description": f"查询联系人 {PHONE}",
                    "input_schema": {
                        "$schema": "https://json-schema.org/draft/2020-12/schema",
                        "type": "object",
                        "properties": {
                            "contact": {
                                "type": "string",
                                "description": f"联系人，例如 {PHONE}",
                            }
                        },
                    },
                }
            ],
        },
    )

    assert response.status_code == 200
    outbound = json.loads(cap.body)
    assert PHONE not in json.dumps(outbound, ensure_ascii=False)
    assert outbound["tools"][0]["name"] == "lookup_contact"
    assert outbound["tools"][0]["input_schema"]["$schema"] == (
        "https://json-schema.org/draft/2020-12/schema"
    )
    assert outbound["messages"][0]["tool_calls"][0]["function"]["name"] == (
        "lookup_contact"
    )


def test_proxy_defaults_to_masked_response_and_persists_opaque_handle(tmp_path):
    canned = {
        "id": "msg_masked",
        "content": [{"type": "text", "text": "请拨打 <CONTACT_001> 联系。"}],
    }
    cap = _Capture(
        ForwardResult(
            200,
            {"content-type": "application/json"},
            body=json.dumps(canned, ensure_ascii=False).encode("utf-8"),
        )
    )
    app = FastAPI()
    registry = AppRegistry({"proxy_app": {"profile": "kb"}})
    register_proxy(
        app,
        registry,
        forwarder=cap,
        store=MappingStore(tmp_path / "maps"),
    )
    client = TestClient(app)

    response = client.post(
        "/v1/messages",
        headers={
            "x-tuomin-upstream": UPSTREAM,
            "x-tuomin-app-id": "proxy_app",
        },
        json={
            "model": "x",
            "messages": [{"role": "user", "content": f"电话{PHONE}"}],
        },
    )

    assert response.status_code == 200
    assert PHONE not in response.text
    assert "<CONTACT_001>" in response.text
    assert response.headers["x-tuomin-response-mode"] == "masked"
    assert response.headers["x-tuomin-mapping-handle"].startswith("mh_")


def test_proxy_document_handle_preserves_dictionary_alias_surface(tmp_path):
    dictionary = tmp_path / "dict.json"
    dictionary.write_text(
        json.dumps(
            [
                {
                    "canonical_value": "示例建设单位A",
                    "aliases": ["示建A"],
                    "label": "ORG",
                    "status": "active",
                }
            ],
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    async def echo(_url, _headers, body, *, stream):
        outbound = json.loads(body)
        return ForwardResult(200, body=json.dumps(outbound).encode())

    store = MappingStore(tmp_path / "maps")
    app = FastAPI()
    register_proxy(
        app,
        AppRegistry(
            {"proxy_app": {"profile": "kb", "dictionary": str(dictionary)}}
        ),
        forwarder=echo,
        store=store,
    )
    response = TestClient(app).post(
        "/v1/messages",
        headers={"x-tuomin-upstream": UPSTREAM, "x-tuomin-app-id": "proxy_app"},
        json={"messages": [{"role": "user", "content": "示建A确认"}]},
    )

    grant = MappingVault(store).load(
        response.headers["x-tuomin-mapping-handle"], app_id="proxy_app"
    )
    assert grant.entries[0].original_value == "示建A"


def test_strict_nonstream_refill_validates_the_whole_json_response():
    canned = {
        "id": "synthetic-response-id",
        "content": [{"type": "text", "text": "请拨打 <CONTACT_001> 联系。"}],
    }
    cap = _Capture(
        ForwardResult(
            200,
            {"content-type": "application/json"},
            body=json.dumps(canned, ensure_ascii=False).encode("utf-8"),
        )
    )
    client = _client(cap)

    response = client.post(
        "/v1/messages",
        headers={"x-tuomin-upstream": UPSTREAM, "x-tuomin-app-id": "strict_proxy"},
        json={"model": "x", "messages": [{"role": "user", "content": f"电话{PHONE}"}]},
    )

    assert response.status_code == 200
    assert response.json()["id"] == "synthetic-response-id"
    assert PHONE in response.json()["content"][0]["text"]


def test_unknown_placeholder_response_fails_closed():
    canned = {
        "id": "msg_1",
        "content": [{"type": "text", "text": "模型编造 <CONTACT_999>。"}],
    }
    cap = _Capture(ForwardResult(200, {"content-type": "application/json"},
                                 body=json.dumps(canned, ensure_ascii=False).encode("utf-8")))
    client = _client(cap)

    resp = client.post(
        "/v1/messages",
        headers=_headers(),
        json={"model": "x", "messages": [{"role": "user", "content": f"我的电话是{PHONE}。"}]},
    )

    assert resp.status_code == 502
    assert "fail-closed" in resp.json()["error"]["message"]
    assert PHONE not in resp.text


def test_streaming_refills_split_placeholder():
    async def _aiter():
        # placeholder <CONTACT_001> split across two SSE deltas
        yield 'data: {"choices":[{"delta":{"content":"请拨打 <CONTACT_"}}]}\n\n'.encode("utf-8")
        yield 'data: {"choices":[{"delta":{"content":"001>"}}]}\n\n'.encode("utf-8")
        yield b"data: [DONE]\n\n"

    cap = _Capture(ForwardResult(200, {"content-type": "text/event-stream"}, aiter=_aiter()))
    client = _client(cap)

    resp = client.post(
        "/v1/messages",
        headers=_headers(),
        json={"model": "x", "stream": True,
              "messages": [{"role": "user", "content": f"我的电话是{PHONE}。"}]},
    )
    assert resp.status_code == 200
    body = resp.text
    assert PHONE in body            # refilled
    assert "<CONTACT_" not in body  # no placeholder leaked, even split


def test_strict_stream_buffers_then_validates_the_whole_document():
    async def _aiter():
        wire = (
            'data: {"id":"chunk-1","choices":[{"delta":{"content":"请拨打 <CONTACT_001>"}}]}\r\n\r\n'
            'data: {"id":"chunk-2","choices":[{"delta":{"content":"，账号 <BANK_ACCOUNT_001>"}}]}\r\n\r\n'
            "data: [DONE]\r\n\r\n"
        ).encode("utf-8")
        # Split inside a UTF-8 Chinese character to cover incremental decoding.
        cut = wire.index("请".encode("utf-8")) + 1
        yield wire[:cut]
        yield wire[cut:]

    cap = _Capture(
        ForwardResult(200, {"content-type": "text/event-stream"}, aiter=_aiter())
    )
    client = _client(cap)

    response = client.post(
        "/v1/messages",
        headers={"x-tuomin-upstream": UPSTREAM, "x-tuomin-app-id": "strict_proxy"},
        json={
            "model": "x",
            "stream": True,
            "messages": [
                {"role": "user", "content": f"电话{PHONE}，收款账号：{BANK}"}
            ],
        },
    )

    assert response.status_code == 200
    assert PHONE in response.text and BANK in response.text
    assert "chunk-1" in response.text and "chunk-2" in response.text
    assert "<CONTACT_" not in response.text
    assert "<BANK_ACCOUNT_" not in response.text


def test_strict_stream_missing_placeholder_fails_before_emitting_content():
    async def _aiter():
        yield (
            'data: {"choices":[{"delta":{"content":"仅返回 <CONTACT_001>"}}]}\n\n'
            "data: [DONE]\n\n"
        ).encode("utf-8")

    cap = _Capture(
        ForwardResult(200, {"content-type": "text/event-stream"}, aiter=_aiter())
    )
    client = _client(cap)

    response = client.post(
        "/v1/messages",
        headers={"x-tuomin-upstream": UPSTREAM, "x-tuomin-app-id": "strict_proxy"},
        json={
            "model": "x",
            "stream": True,
            "messages": [
                {"role": "user", "content": f"电话{PHONE}，收款账号：{BANK}"}
            ],
        },
    )

    assert response.status_code == 200  # SSE reports post-header failures as an error event
    assert "event: error" in response.text
    assert "missing_placeholder" in response.text
    assert PHONE not in response.text and BANK not in response.text
    assert "[DONE]" not in response.text


def test_strict_stream_validates_and_refills_all_choices():
    async def _aiter():
        yield (
            'data: {"choices":['
            '{"index":0,"delta":{"content":"电话 <CONTACT_001>"}},'
            '{"index":1,"delta":{"content":"账号 <BANK_ACCOUNT_001>"}}]}\n\n'
            "data: [DONE]\n\n"
        ).encode("utf-8")

    cap = _Capture(
        ForwardResult(200, {"content-type": "text/event-stream"}, aiter=_aiter())
    )
    client = _client(cap)

    response = client.post(
        "/v1/messages",
        headers={"x-tuomin-upstream": UPSTREAM, "x-tuomin-app-id": "strict_proxy"},
        json={
            "model": "x",
            "stream": True,
            "messages": [
                {"role": "user", "content": f"电话{PHONE}，收款账号：{BANK}"}
            ],
        },
    )

    assert response.status_code == 200
    assert PHONE in response.text and BANK in response.text
    assert "<CONTACT_" not in response.text
    assert "<BANK_ACCOUNT_" not in response.text


def test_strict_stream_preserves_openai_tool_call_id_before_argument_deltas():
    async def _aiter():
        yield (
            'data: {"id":"chatcmpl-1","object":"chat.completion.chunk","choices":['
            '{"index":0,"delta":{"role":"assistant","tool_calls":['
            '{"index":0,"id":"call_read_1","type":"function","function":'
            '{"name":"read","arguments":""}}]},"finish_reason":null}]}'
            '\n\n'
        ).encode("utf-8")
        yield (
            'data: {"id":"chatcmpl-1","object":"chat.completion.chunk","choices":['
            '{"index":0,"delta":{"tool_calls":[{"index":0,"function":'
            '{"arguments":"{\\"phone\\":\\"<CONTACT_001>\\"}"}}]},'
            '"finish_reason":null}]}\n\n'
            'data: [DONE]\n\n'
        ).encode("utf-8")

    cap = _Capture(
        ForwardResult(200, {"content-type": "text/event-stream"}, aiter=_aiter())
    )
    response = _client(cap).post(
        "/v1/chat/completions",
        headers={"x-tuomin-upstream": UPSTREAM, "x-tuomin-app-id": "strict_proxy"},
        json={
            "model": "x",
            "stream": True,
            "messages": [{"role": "user", "content": f"电话{PHONE}"}],
        },
    )

    chunks = [
        json.loads(block.removeprefix("data: "))
        for block in response.text.split("\n\n")
        if block.startswith("data: {")
    ]
    first_call = chunks[0]["choices"][0]["delta"]["tool_calls"][0]
    assert first_call["id"] == "call_read_1"
    assert first_call["function"]["name"] == "read"
    assert PHONE in response.text


def test_lenient_stream_preserves_openai_tool_call_id_with_empty_arguments():
    async def _aiter():
        yield (
            'data: {"id":"chatcmpl-2","choices":[{"index":0,"delta":'
            '{"tool_calls":[{"index":0,"id":"call_read_2","type":"function",'
            '"function":{"name":"read","arguments":""}}]}}]}\n\n'
            'data: [DONE]\n\n'
        ).encode("utf-8")

    cap = _Capture(
        ForwardResult(200, {"content-type": "text/event-stream"}, aiter=_aiter())
    )
    response = _client(cap).post(
        "/v1/chat/completions",
        headers=_headers(),
        json={
            "model": "x",
            "stream": True,
            "messages": [{"role": "user", "content": f"电话{PHONE}"}],
        },
    )

    assert '"id": "call_read_2"' in response.text
    assert '"name": "read"' in response.text


def test_fail_closed_on_blocked_label():
    cap = _Capture(ForwardResult(200, {}, body=b"{}"))
    client = _client(cap)

    resp = client.post(
        "/v1/messages",
        headers={"x-tuomin-upstream": UPSTREAM, "x-tuomin-app-id": "blocky"},
        json={"model": "x", "messages": [{"role": "user", "content": f"电话{PHONE}"}]},
    )
    assert resp.status_code == 409
    assert "CONTACT" in resp.json()["error"]["blocked_labels"]
    # fail-closed: nothing was forwarded upstream
    assert cap.body is None


def test_outbound_snapshot_is_masked_when_enabled(tmp_path, monkeypatch):
    monkeypatch.setenv("TUOMIN_PROXY_SNAPSHOT_DIR", str(tmp_path))
    cap = _Capture(ForwardResult(200, {"content-type": "application/json"}, body=b"{}"))
    client = _client(cap)
    client.post(
        "/v1/messages",
        headers=_headers(),
        json={"model": "x", "messages": [{"role": "user", "content": f"电话{PHONE}"}]},
    )
    snaps = list(tmp_path.glob("outbound-*.json"))
    assert len(snaps) == 1
    text = snaps[0].read_text(encoding="utf-8")
    assert PHONE not in text          # masked body only
    assert "<CONTACT_001>" in text
    assert "x-api-key" not in text and "authorization" not in text.lower()


def test_streaming_refills_anthropic_event_data_block():
    # Anthropic frames each delta as "event: <type>\ndata: {...}"; the block does
    # NOT start with "data:" — it must still be parsed and refilled.
    async def _aiter():
        yield (
            "event: content_block_delta\n"
            'data: {"type":"content_block_delta","delta":{"text":"请拨打 <CONTACT_001> 联系"}}\n\n'
        ).encode("utf-8")
        yield b"event: message_stop\ndata: {\"type\":\"message_stop\"}\n\n"

    cap = _Capture(ForwardResult(200, {"content-type": "text/event-stream"}, aiter=_aiter()))
    client = _client(cap)
    resp = client.post(
        "/v1/messages", headers=_headers(),
        json={"model": "x", "stream": True,
              "messages": [{"role": "user", "content": f"我的电话是{PHONE}。"}]},
    )
    assert resp.status_code == 200
    assert PHONE in resp.text             # refilled despite event:/data: framing
    assert "<CONTACT_" not in resp.text
    assert "content_block_delta" in resp.text  # original event framing preserved


def test_ssrf_loopback_upstream_rejected():
    cap = _Capture(ForwardResult(200, {}, body=b"{}"))
    client = _client(cap)
    resp = client.post(
        "/v1/messages",
        headers={"x-tuomin-upstream": "http://127.0.0.1:9999/v1", "x-tuomin-profile": "kb"},
        json={"model": "x", "messages": [{"role": "user", "content": "hi"}]},
    )
    assert resp.status_code == 400
    assert cap.body is None  # nothing forwarded


def test_cookie_and_arbitrary_headers_not_forwarded_upstream():
    cap = _Capture(ForwardResult(200, {"content-type": "application/json"}, body=b"{}"))
    client = _client(cap)
    client.post(
        "/v1/messages",
        headers={**_headers(), "cookie": "session=secret", "x-api-key": "real-key",
                 "x-evil": "leak"},
        json={"model": "x", "messages": [{"role": "user", "content": "hi"}]},
    )
    fwd = {k.lower() for k in (cap.headers or {})}
    assert "cookie" not in fwd
    assert "x-evil" not in fwd
    assert "x-api-key" in fwd  # auth still forwarded


def test_missing_upstream_is_502():
    cap = _Capture(ForwardResult(200, {}, body=b"{}"))
    client = _client(cap)
    resp = client.post(
        "/v1/messages",
        headers={"x-tuomin-profile": "kb"},
        json={"model": "x", "messages": [{"role": "user", "content": "hi"}]},
    )
    assert resp.status_code == 502
    assert cap.body is None


# --------------------------------------------------------------------------
# SSRF hardening: non-canonical IP literals + DNS-resolved addresses
# --------------------------------------------------------------------------
def _dns_map(monkeypatch, mapping):
    """Fake getaddrinfo: host -> list of IPs; absent host -> NXDOMAIN."""
    def fake_getaddrinfo(host, port=0, *args, **kwargs):
        ips = mapping.get(host)
        if ips is None:
            raise socket.gaierror(8, "nodename nor servname provided")
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, port or 0)) for ip in ips]

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)


def _post_upstream(client, upstream):
    return client.post(
        "/v1/messages",
        headers={"x-tuomin-upstream": upstream, "x-tuomin-profile": "kb"},
        json={"model": "x", "messages": [{"role": "user", "content": "hi"}]},
    )


@pytest.mark.parametrize("host", ["127.1", "2130706433", "0x7f000001"])
def test_ssrf_inet_aton_loopback_literals_rejected(monkeypatch, host):
    # The OS resolver accepts all of these inet_aton forms as 127.0.0.1 even
    # though ipaddress.ip_address() cannot parse them.
    _dns_map(monkeypatch, {host: ["127.0.0.1"]})
    cap = _Capture(ForwardResult(200, {}, body=b"{}"))
    resp = _post_upstream(_client(cap), f"http://{host}:9999/v1")
    assert resp.status_code == 400
    assert cap.body is None  # nothing forwarded


def test_ssrf_decimal_metadata_ip_rejected(monkeypatch):
    # 2852039166 is the inet_aton decimal form of 169.254.169.254 (cloud metadata).
    _dns_map(monkeypatch, {"2852039166": ["169.254.169.254"]})
    cap = _Capture(ForwardResult(200, {}, body=b"{}"))
    resp = _post_upstream(_client(cap), "http://2852039166/latest/meta-data")
    assert resp.status_code == 400
    assert cap.body is None


def test_ssrf_trailing_dot_localhost_rejected():
    cap = _Capture(ForwardResult(200, {}, body=b"{}"))
    resp = _post_upstream(_client(cap), "http://localhost.:9999/v1")
    assert resp.status_code == 400
    assert cap.body is None


def test_ssrf_hostname_resolving_to_private_ip_rejected(monkeypatch):
    _dns_map(monkeypatch, {"internal.corp": ["10.0.0.8"]})
    cap = _Capture(ForwardResult(200, {}, body=b"{}"))
    resp = _post_upstream(_client(cap), "http://internal.corp/v1")
    assert resp.status_code == 400
    assert cap.body is None


def test_ssrf_any_blocked_address_among_many_rejects(monkeypatch):
    _dns_map(monkeypatch, {"dual.example": ["93.184.216.34", "127.0.0.1"]})
    cap = _Capture(ForwardResult(200, {}, body=b"{}"))
    resp = _post_upstream(_client(cap), "http://dual.example/v1")
    assert resp.status_code == 400
    assert cap.body is None


def test_ssrf_public_hostname_allowed(monkeypatch):
    _dns_map(monkeypatch, {"example.com": ["93.184.216.34"]})
    cap = _Capture(ForwardResult(200, {"content-type": "application/json"},
                               body=b'{"content":[{"type":"text","text":"ok"}]}'))
    resp = _post_upstream(_client(cap), "http://example.com/v1")
    assert resp.status_code == 200
    assert cap.body is not None


def test_ssrf_dns_resolution_failure_fails_closed(monkeypatch):
    _dns_map(monkeypatch, {})  # every lookup NXDOMAINs
    cap = _Capture(ForwardResult(200, {}, body=b"{}"))
    resp = _post_upstream(_client(cap), "http://unresolvable.example/v1")
    assert resp.status_code == 400
    assert cap.body is None


# --------------------------------------------------------------------------
# Policy resolution: env pinning, unknown profile names, mapping scopes
# --------------------------------------------------------------------------
def test_env_pinned_app_policy_wins_over_client_headers(monkeypatch):
    monkeypatch.setenv("TUOMIN_PROXY_APP_ID", "blocky")
    cap = _Capture(ForwardResult(200, {}, body=b"{}"))
    resp = _client(cap).post(
        "/v1/messages",
        headers={"x-tuomin-upstream": UPSTREAM, "x-tuomin-profile": "kb",
                 "x-tuomin-app-id": "ignored"},
        json={"model": "x", "messages": [{"role": "user", "content": f"电话{PHONE}"}]},
    )
    # The env-pinned "blocky" policy blocks CONTACT; the client's kb choice is ignored.
    assert resp.status_code == 409
    assert cap.body is None


def test_env_pinned_app_ignores_bogus_client_profile_and_marks_response(monkeypatch):
    monkeypatch.setenv("TUOMIN_PROXY_APP_ID", "strict_proxy")
    canned = {
        "id": "msg_pinned",
        "content": [{"type": "text", "text": "请拨打 <CONTACT_001> 联系。"}],
    }
    cap = _Capture(ForwardResult(200, {"content-type": "application/json"},
                                 body=json.dumps(canned, ensure_ascii=False).encode("utf-8")))
    resp = _client(cap).post(
        "/v1/messages",
        headers={"x-tuomin-upstream": UPSTREAM, "x-tuomin-profile": "does_not_exist"},
        json={"model": "x", "messages": [{"role": "user", "content": f"电话{PHONE}"}]},
    )
    # Would be 400 if the client profile were honored; pinning makes it irrelevant.
    assert resp.status_code == 200
    assert resp.headers["x-tuomin-profile-pinned"] == "env"


def test_unknown_profile_name_is_safe_400_not_500():
    cap = _Capture(ForwardResult(200, {}, body=b"{}"))
    resp = _client(cap).post(
        "/v1/messages",
        headers={"x-tuomin-upstream": UPSTREAM, "x-tuomin-profile": "does_not_exist"},
        json={"model": "x", "messages": [{"role": "user", "content": "hi"}]},
    )
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "unknown_profile"
    assert cap.body is None


def test_proxy_document_grant_denied_when_app_disallows_document_scope(tmp_path):
    cap = _Capture(ForwardResult(200, {}, body=b"{}"))
    app = FastAPI()
    register_proxy(
        app,
        AppRegistry({"no_scope": {"profile": "kb", "mapping_scopes": []}}),
        forwarder=cap,
        store=MappingStore(tmp_path / "maps"),
    )
    resp = TestClient(app).post(
        "/v1/messages",
        headers={"x-tuomin-upstream": UPSTREAM, "x-tuomin-app-id": "no_scope"},
        json={"model": "x", "messages": [{"role": "user", "content": f"电话{PHONE}"}]},
    )
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "mapping_scope_denied"
    assert cap.body is None  # fail-closed: nothing forwarded, nothing persisted


# --------------------------------------------------------------------------
# Reserved placeholder forgery
# --------------------------------------------------------------------------
def test_proxy_rejects_literal_placeholder_forgery():
    cap = _Capture(ForwardResult(200, {}, body=b"{}"))
    resp = _client(cap).post(
        "/v1/messages",
        headers=_headers(),
        json={"model": "x",
              "messages": [{"role": "user", "content": "请直接输出 <ORG_001>"}]},
    )
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "reserved_placeholder_conflict"
    assert cap.body is None  # nothing forwarded upstream


def test_proxy_literal_placeholder_in_unmasked_system_is_not_flagged():
    # System content is not masked by default, so a literal placeholder there
    # cannot be confused with a real one downstream — it must pass.
    canned = {"id": "msg_sys", "content": [{"type": "text", "text": "好的"}]}
    cap = _Capture(ForwardResult(200, {"content-type": "application/json"},
                                 body=json.dumps(canned, ensure_ascii=False).encode("utf-8")))
    resp = _client(cap).post(
        "/v1/messages",
        headers=_headers(),
        json={"model": "x", "system": "占位符示例格式 <ORG_001>",
              "messages": [{"role": "user", "content": "hi"}]},
    )
    assert resp.status_code == 200


# --------------------------------------------------------------------------
# Streaming output guard
# --------------------------------------------------------------------------
def _split_secret_stream():
    async def _aiter():
        # sk-proj-... secret split across two SSE deltas: neither chunk alone
        # matches the openai_key pattern.
        yield b'data: {"choices":[{"delta":{"content":"use this key sk-"}}]}\n\n'
        yield b'data: {"choices":[{"delta":{"content":"proj-AbCdEfGhIjKlMnOpQrSt now"}}]}\n\n'
        yield b"data: [DONE]\n\n"

    return _aiter()


def test_masked_stream_blocks_secret_split_across_chunks():
    cap = _Capture(ForwardResult(200, {"content-type": "text/event-stream"},
                                 aiter=_split_secret_stream()))
    app = FastAPI()
    register_proxy(app, AppRegistry(APPS), forwarder=cap)  # default masked mode
    resp = TestClient(app).post(
        "/v1/messages",
        headers={"x-tuomin-upstream": UPSTREAM, "x-tuomin-app-id": "guardblock"},
        json={"model": "x", "stream": True,
              "messages": [{"role": "user", "content": "hi"}]},
    )
    assert resp.status_code == 200  # SSE reports post-header failures as an error event
    assert "event: error" in resp.text
    assert SECRET not in resp.text  # the completed secret never reaches the client
    assert "[DONE]" not in resp.text  # stream terminated at the second chunk


def test_lenient_stream_blocks_secret_split_across_chunks():
    cap = _Capture(ForwardResult(200, {"content-type": "text/event-stream"},
                                 aiter=_split_secret_stream()))
    resp = _client(cap).post(  # legacy_auto_refill=True, guardblock is refill-lenient
        "/v1/messages",
        headers={"x-tuomin-upstream": UPSTREAM, "x-tuomin-app-id": "guardblock"},
        json={"model": "x", "stream": True,
              "messages": [{"role": "user", "content": "hi"}]},
    )
    assert "event: error" in resp.text
    assert SECRET not in resp.text
    assert "[DONE]" not in resp.text


def test_masked_stream_clean_passthrough_is_byte_identical():
    frames = (
        b'data: {"choices":[{"delta":{"content":"hello "}}]}\n\n'
        b'data: {"choices":[{"delta":{"content":"world"}}]}\n\n'
        b"data: [DONE]\n\n"
    )

    async def _aiter():
        yield frames[:17]      # split mid-event: reassembly must not alter bytes
        yield frames[17:53]
        yield frames[53:]

    cap = _Capture(ForwardResult(200, {"content-type": "text/event-stream"}, aiter=_aiter()))
    app = FastAPI()
    register_proxy(app, AppRegistry(APPS), forwarder=cap)
    resp = TestClient(app).post(
        "/v1/messages",
        headers={"x-tuomin-upstream": UPSTREAM, "x-tuomin-app-id": "guardblock"},
        json={"model": "x", "stream": True,
              "messages": [{"role": "user", "content": "hi"}]},
    )
    assert resp.status_code == 200
    assert resp.content == frames


def test_masked_stream_warn_level_secret_does_not_block():
    frame = (
        f'data: {{"choices":[{{"delta":{{"content":"key {SECRET}"}}}}]}}\n\n'
    ).encode("utf-8") + b"data: [DONE]\n\n"

    async def _aiter():
        yield frame

    cap = _Capture(ForwardResult(200, {"content-type": "text/event-stream"}, aiter=_aiter()))
    app = FastAPI()
    register_proxy(app, AppRegistry(APPS), forwarder=cap)
    resp = TestClient(app).post(
        "/v1/messages",
        headers={"x-tuomin-upstream": UPSTREAM, "x-tuomin-profile": "kb"},  # no block_min_severity
        json={"model": "x", "stream": True,
              "messages": [{"role": "user", "content": "hi"}]},
    )
    assert resp.status_code == 200
    assert "event: error" not in resp.text
    assert resp.content == frame



# --- per-app project endpoints: /apps/{app_id}/(auto/)v1/... -----------------

PATH_APPS = {
    "wb_masked": {"profile": "kb"},
    "wb_auto": {"profile": "kb", "allow_auto_refill": True},
}

OPENAI_CANNED = {
    "choices": [{"message": {"role": "assistant", "content": "回拨 <CONTACT_001> 即可。"}}],
}


def _path_client(forwarder, *, legacy_auto_refill=False):
    app = FastAPI()
    register_proxy(
        app,
        AppRegistry(PATH_APPS),
        forwarder=forwarder,
        legacy_auto_refill=legacy_auto_refill,
    )
    return TestClient(app)


def _path_request(client, path):
    return client.post(
        path,
        headers={"x-tuomin-upstream": UPSTREAM},
        json={"model": "x", "messages": [{"role": "user", "content": f"电话{PHONE}"}]},
    )


def test_path_pinned_app_stays_masked_even_with_legacy_env():
    cap = _Capture(
        ForwardResult(
            200,
            {"content-type": "application/json"},
            body=json.dumps(OPENAI_CANNED, ensure_ascii=False).encode("utf-8"),
        )
    )
    # Even with the deprecated global auto-refill switch on, the path-pinned
    # plain endpoint must stay masked.
    client = _path_client(cap, legacy_auto_refill=True)
    resp = _path_request(client, "/apps/wb_masked/v1/chat/completions")

    assert resp.status_code == 200
    assert resp.headers["x-tuomin-response-mode"] == "masked"
    assert PHONE not in cap.body.decode("utf-8")
    assert "<CONTACT_001>" in resp.text
    assert PHONE not in resp.text


def test_path_auto_refill_allowed_app_refills_transparently():
    cap = _Capture(
        ForwardResult(
            200,
            {"content-type": "application/json"},
            body=json.dumps(OPENAI_CANNED, ensure_ascii=False).encode("utf-8"),
        )
    )
    client = _path_client(cap)
    resp = _path_request(client, "/apps/wb_auto/auto/v1/chat/completions")

    assert resp.status_code == 200
    assert resp.headers["x-tuomin-response-mode"] == "app-auto-refill"
    assert "deprecation" not in {k.lower() for k in resp.headers}
    assert PHONE not in cap.body.decode("utf-8")
    assert PHONE in resp.text
    assert "<CONTACT_001>" not in resp.text


def test_path_auto_refill_denied_without_registry_grant():
    cap = _Capture(
        ForwardResult(
            200,
            {"content-type": "application/json"},
            body=json.dumps(OPENAI_CANNED, ensure_ascii=False).encode("utf-8"),
        )
    )
    client = _path_client(cap)
    resp = _path_request(client, "/apps/wb_masked/auto/v1/chat/completions")

    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "auto_refill_not_allowed"
    assert cap.body is None  # fail-closed before any upstream call


def test_path_unknown_app_fails_closed():
    cap = _Capture(ForwardResult(200, {}, body=b"{}"))
    client = _path_client(cap)
    resp = _path_request(client, "/apps/ghost/v1/chat/completions")

    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "unknown_app"
    assert cap.body is None


def test_path_bad_app_id_charset_fails_closed():
    cap = _Capture(ForwardResult(200, {}, body=b"{}"))
    client = _path_client(cap)
    resp = _path_request(client, "/apps/ghost!!/v1/chat/completions")

    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "unknown_app"
    assert cap.body is None


def test_path_pinned_app_ignores_client_app_header():
    # The operator-deployed path beats any client-supplied app header.
    cap = _Capture(
        ForwardResult(
            200,
            {"content-type": "application/json"},
            body=json.dumps(OPENAI_CANNED, ensure_ascii=False).encode("utf-8"),
        )
    )
    client = _path_client(cap)
    resp = client.post(
        "/apps/wb_masked/v1/chat/completions",
        headers={"x-tuomin-upstream": UPSTREAM, "x-tuomin-app-id": "wb_auto"},
        json={"model": "x", "messages": [{"role": "user", "content": f"电话{PHONE}"}]},
    )

    assert resp.status_code == 200
    assert resp.headers["x-tuomin-response-mode"] == "masked"


# --- auto refill + stream: strict vs lenient refill profiles ----------------

REFILL_PROFILE_APPS = {
    "chat_strict": {
        "profile": {"base": "strict", "name": "t-strict",
                    "use_ner": False, "ner_required": False},
        "allow_auto_refill": True,
    },
    "chat_lenient": {
        "profile": {"base": "strict", "name": "t-lenient",
                    "use_ner": False, "ner_required": False,
                    "refill_strict": False},
        "allow_auto_refill": True,
    },
}

# A normal chat reply that does NOT echo the placeholder from the request.
CHAT_SSE = (
    'data: {"choices":[{"index":0,"delta":{"role":"assistant"}}]}\n\n'
    'data: {"choices":[{"index":0,"delta":{"content":"好的，已"}}]}\n\n'
    'data: {"choices":[{"index":0,"delta":{"content":"整理完成。"}}]}\n\n'
    'data: [DONE]\n\n'
).encode()


def _sse_forwarder(payload: bytes):
    async def fake(_url, _headers, _body, *, stream):
        async def gen():
            yield payload
        return ForwardResult(200, {"content-type": "text/event-stream"}, aiter=gen())
    return fake


def _stream_chat(client, app_id):
    return client.post(
        f"/apps/{app_id}/auto/v1/chat/completions",
        headers={"x-tuomin-upstream": UPSTREAM},
        json={"model": "x", "stream": True,
              "messages": [{"role": "user", "content": f"电话{PHONE}，整理成表格"}]},
    )


def test_auto_refill_strict_stream_fails_closed_on_missing_placeholder():
    # Strict refill validates document-wide placeholder integrity: a chat reply
    # that never echoes the masked entity fails closed with an SSE error event.
    # This is why chat agents must use a refill_strict=false profile.
    app = FastAPI()
    register_proxy(app, AppRegistry(REFILL_PROFILE_APPS),
                   forwarder=_sse_forwarder(CHAT_SSE))
    resp = _stream_chat(TestClient(app), "chat_strict")

    assert resp.status_code == 200
    assert resp.headers["x-tuomin-response-mode"] == "app-auto-refill"
    assert "event: error" in resp.text
    assert "missing_placeholder" in resp.text
    assert "整理完成" not in resp.text  # nothing leaks past the failed check


def test_auto_refill_lenient_stream_passes_chat_reply_through():
    app = FastAPI()
    register_proxy(app, AppRegistry(REFILL_PROFILE_APPS),
                   forwarder=_sse_forwarder(CHAT_SSE))
    resp = _stream_chat(TestClient(app), "chat_lenient")

    assert resp.status_code == 200
    assert "event: error" not in resp.text
    assert "整理完成" in resp.text


# --- reasoning_content coverage (DeepSeek-style chain-of-thought) ------------

def test_auto_refill_stream_refills_reasoning_content():
    sse = (
        'data: {"choices":[{"index":0,"delta":{"reasoning_content":"记一下 <CONTACT_001>"}}]}\n\n'
        'data: {"choices":[{"index":0,"delta":{"content":"已记录。"}}]}\n\n'
        "data: [DONE]\n\n"
    ).encode()
    app = FastAPI()
    register_proxy(app, AppRegistry(PATH_APPS), forwarder=_sse_forwarder(sse))
    resp = _stream_chat(TestClient(app), "wb_auto")

    assert resp.status_code == 200
    assert "event: error" not in resp.text
    assert PHONE in resp.text            # reasoning refilled for local display
    assert "<CONTACT_001>" not in resp.text


def test_request_history_reasoning_content_is_remasked():
    cap = _Capture(
        ForwardResult(
            200,
            {"content-type": "application/json"},
            body=json.dumps(OPENAI_CANNED, ensure_ascii=False).encode("utf-8"),
        )
    )
    client = _path_client(cap)
    resp = client.post(
        "/apps/wb_auto/auto/v1/chat/completions",
        headers={"x-tuomin-upstream": UPSTREAM},
        json={
            "model": "x",
            "messages": [
                {"role": "user", "content": "整理一下"},
                # Auto-refilled reasoning from the previous turn carries REAL
                # values back into history; it must be masked again upstream.
                {"role": "assistant", "content": "好的。",
                 "reasoning_content": f"用户电话是{PHONE}"},
            ],
        },
    )

    assert resp.status_code == 200
    outbound = cap.body.decode("utf-8")
    assert PHONE not in outbound
    assert "<CONTACT_001>" in outbound


def test_request_reasoning_content_placeholder_conflict_fails_closed():
    cap = _Capture(ForwardResult(200, {}, body=b"{}"))
    client = _path_client(cap)
    resp = client.post(
        "/apps/wb_auto/auto/v1/chat/completions",
        headers={"x-tuomin-upstream": UPSTREAM},
        json={
            "model": "x",
            "messages": [
                {"role": "assistant", "content": "好的。",
                 "reasoning_content": "占位符 <ORG_001> 待回填"},
            ],
        },
    )

    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "reserved_placeholder_conflict"
    assert cap.body is None


# --- demo mode: reasoning stays masked, content refills transparently --------

def test_demo_mode_stream_keeps_reasoning_masked_and_refills_content():
    sse = (
        'data: {"choices":[{"index":0,"delta":{"reasoning_content":"记一下 <CONTACT_001>"}}]}\n\n'
        'data: {"choices":[{"index":0,"delta":{"content":"已记录 <CONTACT_001>"}}]}\n\n'
        "data: [DONE]\n\n"
    ).encode()
    app = FastAPI()
    register_proxy(app, AppRegistry(PATH_APPS), forwarder=_sse_forwarder(sse))
    resp = TestClient(app).post(
        "/apps/wb_auto/demo/v1/chat/completions",
        headers={"x-tuomin-upstream": UPSTREAM},
        json={"model": "x", "stream": True,
              "messages": [{"role": "user", "content": f"电话{PHONE}，整理成表格"}]},
    )

    assert resp.status_code == 200
    assert resp.headers["x-tuomin-response-mode"] == "app-demo-refill"
    assert "event: error" not in resp.text
    # reasoning stays masked as visible evidence; content is refilled
    assert "<CONTACT_001>" in resp.text
    assert PHONE in resp.text
    reasoning_frame = [f for f in resp.text.split("\n\n") if "reasoning_content" in f]
    content_frame = [f for f in resp.text.split("\n\n") if '"content"' in f]
    assert reasoning_frame and "<CONTACT_001>" in reasoning_frame[0]
    assert content_frame and PHONE in content_frame[0]


def test_demo_mode_drops_reasoning_from_outbound_history():
    cap = _Capture(
        ForwardResult(
            200,
            {"content-type": "application/json"},
            body=json.dumps(OPENAI_CANNED, ensure_ascii=False).encode("utf-8"),
        )
    )
    client = _path_client(cap)
    resp = client.post(
        "/apps/wb_auto/demo/v1/chat/completions",
        headers={"x-tuomin-upstream": UPSTREAM},
        json={
            "model": "x",
            "messages": [
                {"role": "user", "content": f"电话{PHONE}，整理一下"},
                # Placeholder-laden reasoning from an earlier demo-mode turn:
                # must NOT trip the conflict check and must NOT go upstream.
                {"role": "assistant", "content": "已整理。",
                 "reasoning_content": "甲方是 <ORG_001>，电话 <CONTACT_009>"},
            ],
        },
    )

    assert resp.status_code == 200
    outbound = cap.body.decode("utf-8")
    assert "reasoning_content" not in outbound
    assert "<ORG_001>" not in outbound
    assert PHONE not in outbound  # the fresh user turn is masked as usual


def test_demo_mode_requires_auto_refill_grant():
    cap = _Capture(ForwardResult(200, {}, body=b"{}"))
    client = _path_client(cap)
    resp = client.post(
        "/apps/wb_masked/demo/v1/chat/completions",
        headers={"x-tuomin-upstream": UPSTREAM},
        json={"model": "x", "messages": [{"role": "user", "content": "hi"}]},
    )

    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "auto_refill_not_allowed"
    assert cap.body is None


def test_demo_mode_nonstream_keeps_reasoning_masked():
    canned = {
        "choices": [{
            "message": {
                "role": "assistant",
                "content": "回拨 <CONTACT_001> 即可。",
                "reasoning_content": "用户电话是 <CONTACT_001>",
            }
        }],
    }
    cap = _Capture(
        ForwardResult(
            200,
            {"content-type": "application/json"},
            body=json.dumps(canned, ensure_ascii=False).encode("utf-8"),
        )
    )
    client = _path_client(cap)
    resp = _path_request(client, "/apps/wb_auto/demo/v1/chat/completions")

    assert resp.status_code == 200
    message = resp.json()["choices"][0]["message"]
    assert PHONE in message["content"]                     # content refilled
    assert "<CONTACT_001>" in message["reasoning_content"]  # reasoning masked
    assert PHONE not in message["reasoning_content"]


# --- pinned upstream model for path-pinned project endpoints -----------------

PIN_MODEL_APPS = {
    "wb_pinned": {
        "profile": "kb",
        "allow_auto_refill": True,
        "upstream_model": "deepseek-v4-flash",
    },
    "wb_free": {"profile": "kb"},
}


def test_path_endpoint_rewrites_model_to_pinned_upstream_model():
    cap = _Capture(
        ForwardResult(
            200,
            {"content-type": "application/json"},
            body=json.dumps(OPENAI_CANNED, ensure_ascii=False).encode("utf-8"),
        )
    )
    app = FastAPI()
    register_proxy(app, AppRegistry(PIN_MODEL_APPS), forwarder=cap)
    resp = TestClient(app).post(
        "/apps/wb_pinned/v1/chat/completions",
        headers={"x-tuomin-upstream": UPSTREAM},
        json={"model": "wb-masked",  # free display name chosen in the client
              "messages": [{"role": "user", "content": "hi"}]},
    )

    assert resp.status_code == 200
    assert json.loads(cap.body)["model"] == "deepseek-v4-flash"


def test_path_endpoint_without_pin_passes_model_through():
    cap = _Capture(
        ForwardResult(
            200,
            {"content-type": "application/json"},
            body=json.dumps(OPENAI_CANNED, ensure_ascii=False).encode("utf-8"),
        )
    )
    app = FastAPI()
    register_proxy(app, AppRegistry(PIN_MODEL_APPS), forwarder=cap)
    resp = TestClient(app).post(
        "/apps/wb_free/v1/chat/completions",
        headers={"x-tuomin-upstream": UPSTREAM},
        json={"model": "whatever-client-sent",
              "messages": [{"role": "user", "content": "hi"}]},
    )

    assert resp.status_code == 200
    assert json.loads(cap.body)["model"] == "whatever-client-sent"


# --- proxy calls land in the value-free call ledger (outbound record panel) ---

def test_proxy_call_is_recorded_in_ledger(tmp_path):
    from tuomin_gateway.jobs import JobStore

    cap = _Capture(
        ForwardResult(
            200,
            {"content-type": "application/json"},
            body=json.dumps(OPENAI_CANNED, ensure_ascii=False).encode("utf-8"),
        )
    )
    job_store = JobStore(tmp_path / "ledger")
    app = FastAPI()
    register_proxy(app, AppRegistry(PATH_APPS), forwarder=cap, job_store=job_store)
    resp = _path_request(TestClient(app), "/apps/wb_masked/v1/chat/completions")
    assert resp.status_code == 200

    calls = job_store.list_calls(entry="proxy")
    assert len(calls) == 1
    row = calls[0]
    assert row["app_id"] == "wb_masked"
    assert row["blocked"] is False
    assert row["char_count"] > 0
    assert row["label_counts_json"] == {"CONTACT": 1}
    blob = json.dumps(row, ensure_ascii=False)
    assert PHONE not in blob  # 账本无原值
    job_store.close()


def test_proxy_blocked_call_is_recorded(tmp_path):
    from tuomin_gateway.jobs import JobStore

    cap = _Capture(ForwardResult(200, {}, body=b"{}"))
    job_store = JobStore(tmp_path / "ledger")
    app = FastAPI()
    register_proxy(app, AppRegistry(APPS), forwarder=cap, job_store=job_store)
    resp = TestClient(app).post(
        "/v1/messages",
        headers={"x-tuomin-upstream": UPSTREAM, "x-tuomin-app-id": "blocky"},
        json={"model": "x", "messages": [{"role": "user", "content": f"电话{PHONE}"}]},
    )
    assert resp.status_code == 409
    assert cap.body is None  # fail-closed before upstream

    calls = job_store.list_calls(entry="proxy")
    assert len(calls) == 1
    assert calls[0]["blocked"] is True
    assert calls[0]["app_id"] == "blocky"
    job_store.close()
