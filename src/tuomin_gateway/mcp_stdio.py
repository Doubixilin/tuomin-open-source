"""STDIO MCP adapter: expose the agent-contract-v1 tools to local agents.

A thin, stdlib-only MCP server (newline-delimited JSON-RPC 2.0 over stdio)
that forwards to the running Tuomin gateway's ``/api/v1`` endpoints. It holds
capability tokens from the environment — agents never see them — and exposes
ONLY the three low-privilege tools of agent-contract-v1
(``docs/design/agent-facing-contract.md``): no refill, no mapping, no admin.

Run by an MCP client (WorkBuddy/Codex/Claude Code) as::

    python -m tuomin_gateway.mcp_stdio

Environment:
    TUOMIN_GATEWAY_URL            explicit gateway URL; when unset, auto-discovered
                                  (never trust a hard-coded port — it rots into
                                  ECONNREFUSED; see gateway_discovery)
    TUOMIN_AGENT_REDACT_TOKEN     redact capability token (tuomin_redact_text)
    TUOMIN_AGENT_NAMESPACE_TOKEN  namespace capability token (readiness/values)
    TUOMIN_AGENT_APP              optional default app_id

Protocol notes: logs NEVER go to stdout (stdio is the transport). Unknown
methods return JSON-RPC -32601; tool errors are reported as
``{"isError": true, "content": [...]}`` per MCP, never as transport failures.
"""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from typing import Any, Callable

from tuomin_gateway.gateway_discovery import resolve_gateway_url

CONTRACT = "agent-contract-v1"
PROTOCOL_VERSION = "2025-06-18"

TOOLS = [
    {
        "name": "tuomin_readiness",
        "description": (
            "只读查询 Tuomin app 的策略/词典/检测器版本与可运行状态。"
            "ready=false 时必须停止，不得尝试脱敏。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {"app_id": {"type": "string"}},
            "required": ["app_id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "tuomin_redact_text",
        "description": (
            "脱敏一段文本。仅当 egress_allowed=true 时 masked_text 才可进入云端"
            "模型上下文；job_id 不具备恢复能力，回填只在本机可信 UI 进行。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "app_id": {"type": "string"},
                "text": {"type": "string"},
            },
            "required": ["app_id", "text"],
            "additionalProperties": False,
        },
    },
    {
        "name": "tuomin_redact_values",
        "description": (
            "结构化字段脱敏（label 受 app 白名单约束）。语义同 /api/v1/redact/values。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "app_id": {"type": "string"},
                "namespace_id": {"type": "string"},
                "items": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "id": {"type": "string"},
                            "label": {"type": "string"},
                            "value": {"type": "string"},
                        },
                        "required": ["id", "label", "value"],
                    },
                },
            },
            "required": ["app_id", "namespace_id", "items"],
            "additionalProperties": False,
        },
    },
]

HttpPost = Callable[[str, dict, dict | None, str | None], tuple[int, dict]]


def _default_http_post(
    url: str, headers: dict, payload: dict | None, method: str | None
) -> tuple[int, dict]:
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
    request = urllib.request.Request(url, data=data, method=method or ("POST" if data else "GET"))
    for key, value in headers.items():
        request.add_header(key, value)
    request.add_header("content-type", "application/json")
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as exc:
        try:
            return exc.code, json.loads(exc.read())
        except Exception:
            return exc.code, {"status": "error", "error": {"code": "gateway_error", "message": "gateway error"}}
    except OSError:
        return -1, {"status": "error", "error": {"code": "gateway_unavailable", "message": "tuomin gateway unavailable"}}


class AgentGateway:
    """Thin agent-contract client over the gateway's /api/v1 endpoints."""

    def __init__(
        self,
        gateway_url: str,
        redact_token: str | None,
        namespace_token: str | None,
        http_post: HttpPost = _default_http_post,
    ) -> None:
        self.gateway_url = gateway_url.rstrip("/")
        self.redact_token = redact_token or ""
        self.namespace_token = namespace_token or ""
        self._post = http_post

    def _headers(self, token: str) -> dict:
        return {"x-tuomin-capability-token": token}

    def _get(self, path: str, token: str) -> tuple[int, dict]:
        return self._post(self.gateway_url + path, self._headers(token), None, "GET")

    def _call(self, path: str, token: str, payload: dict) -> tuple[int, dict]:
        return self._post(self.gateway_url + path, self._headers(token), payload, None)

    @staticmethod
    def _wrap(payload: dict) -> dict:
        return {"contract": CONTRACT, **payload}

    def readiness(self, app_id: str) -> dict:
        status, data = self._get(f"/api/v1/readiness?app_id={app_id}", self.namespace_token)
        if status != 200:
            return self._wrap({
                "status": "error",
                "ready": False,
                "error": data.get("error") or {"code": "app_not_ready", "message": "app not ready"},
            })
        return self._wrap({
            "status": "ok",
            "ready": data.get("status") == "ready",
            "app": data.get("app"),
            "detectors": data.get("detectors"),
        })

    def redact_text(self, app_id: str, text: str) -> dict:
        status, data = self._call(
            "/api/v1/redact", self.redact_token, {"app_id": app_id, "text": text}
        )
        if status != 200:
            return self._wrap({
                "status": "error",
                "egress_allowed": False,
                "error": data.get("error") or {"code": "gateway_error", "message": "gateway error"},
            })
        detectors = data.get("detectors") or {}
        missing = sorted(set(detectors.get("required", [])) - set(detectors.get("active", [])))
        return self._wrap({
            "status": "ok",
            "masked_text": data.get("masked_text"),
            "egress_allowed": data.get("egress_allowed"),
            "blocked_labels": data.get("blocked_labels"),
            "label_counts": data.get("label_counts"),
            "job_id": data.get("mapping_handle"),
            "detectors": {
                "degraded": detectors.get("degraded"),
                "missing_required": missing,
            },
        })

    def redact_values(self, app_id: str, namespace_id: str, items: list) -> dict:
        status, data = self._call(
            "/api/v1/redact/values",
            self.namespace_token,
            {"app_id": app_id, "namespace_id": namespace_id, "items": items},
        )
        if status != 200:
            return self._wrap({
                "status": "error",
                "egress_allowed": False,
                "error": data.get("error") or {"code": "gateway_error", "message": "gateway error"},
            })
        return self._wrap({
            "status": "ok",
            "items": data.get("items"),
            "egress_allowed": data.get("egress_allowed"),
            "job_id": data.get("mapping_handle"),
        })


def _result(payload: dict, is_error: bool = False) -> dict:
    return {
        "content": [{"type": "text", "text": json.dumps(payload, ensure_ascii=False)}],
        "isError": is_error,
    }


def dispatch(gateway: AgentGateway, message: dict) -> dict | None:
    """Handle one JSON-RPC message; None for notifications (no response)."""
    method = message.get("method")
    msg_id = message.get("id")

    def respond(result: Any) -> dict:
        return {"jsonrpc": "2.0", "id": msg_id, "result": result}

    def fail(code: int, text: str) -> dict:
        return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": code, "message": text}}

    if method == "initialize":
        return respond({
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "tuomin-mcp", "version": "0.1.0"},
        })
    if method == "notifications/initialized" or method == "notifications/cancelled":
        return None
    if method == "ping":
        return respond({})
    if method == "tools/list":
        return respond({"tools": TOOLS})
    if method == "tools/call":
        params = message.get("params") or {}
        name = params.get("name")
        args = params.get("arguments") or {}
        try:
            if name == "tuomin_readiness":
                app_id = args.get("app_id") or os.environ.get("TUOMIN_AGENT_APP", "")
                if not app_id:
                    return respond(_result({"contract": CONTRACT, "status": "error", "error": {"code": "app_id_required", "message": "app_id is required"}}, True))
                payload = gateway.readiness(str(app_id))
            elif name == "tuomin_redact_text":
                app_id = args.get("app_id") or os.environ.get("TUOMIN_AGENT_APP", "")
                text = args.get("text")
                if not app_id or not isinstance(text, str):
                    return respond(_result({"contract": CONTRACT, "status": "error", "error": {"code": "invalid_arguments", "message": "app_id and text are required"}}, True))
                payload = gateway.redact_text(str(app_id), text)
            elif name == "tuomin_redact_values":
                app_id = args.get("app_id") or os.environ.get("TUOMIN_AGENT_APP", "")
                namespace_id = args.get("namespace_id")
                items = args.get("items")
                if not app_id or not isinstance(namespace_id, str) or not isinstance(items, list):
                    return respond(_result({"contract": CONTRACT, "status": "error", "error": {"code": "invalid_arguments", "message": "app_id, namespace_id and items are required"}}, True))
                payload = gateway.redact_values(str(app_id), namespace_id, items)
            else:
                return fail(-32602, f"unknown tool: {name}")
        except Exception:  # noqa: BLE001 - tool failures become tool errors, never transport failures
            return respond(_result({"contract": CONTRACT, "status": "error", "error": {"code": "internal_error", "message": "internal error"}}, True))
        return respond(_result(payload, payload.get("status") == "error"))
    return fail(-32601, f"method not found: {method}")


DEFAULT_GATEWAY_URL = "http://127.0.0.1:8765"


def resolve_gateway_from_env(env: dict | None = None, *, resolver=None) -> str:
    """Gateway URL for this adapter.

    ``TUOMIN_GATEWAY_URL`` wins when set. Otherwise the live gateway is
    auto-discovered (preferring one that serves ``TUOMIN_AGENT_APP``) so a port
    move — CLI 8765, packaged app dynamic, launchpad e.g. 8775 — does not break
    the adapter. Falls back to the CLI default only when nothing is reachable.
    """
    environment = os.environ if env is None else env
    explicit = environment.get("TUOMIN_GATEWAY_URL")
    if explicit:
        return explicit
    app = (environment.get("TUOMIN_AGENT_APP") or "").strip() or None
    resolve = resolver or resolve_gateway_url
    try:
        resolution = resolve(app, env=environment)
    except Exception:  # noqa: BLE001 - discovery must never block adapter startup
        return DEFAULT_GATEWAY_URL
    return resolution.base_url or DEFAULT_GATEWAY_URL


def main() -> int:
    gateway = AgentGateway(
        gateway_url=resolve_gateway_from_env(),
        redact_token=os.environ.get("TUOMIN_AGENT_REDACT_TOKEN"),
        namespace_token=os.environ.get("TUOMIN_AGENT_NAMESPACE_TOKEN"),
    )
    print("tuomin-mcp: stdio server ready", file=sys.stderr)
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            print(json.dumps({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "parse error"}}), flush=True)
            continue
        response = dispatch(gateway, message)
        if response is not None:
            print(json.dumps(response, ensure_ascii=False), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
