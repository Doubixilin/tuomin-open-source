"""Admin routes behind the 工具接入 page: one-click MCP + Skill for AI clients.

Every route sits under ``/admin`` (admin token + host check in the app
middleware). Client configs receive only the STDIO command; the ``agent`` app's
tokens never leave Tuomin's data directory. Demo mode never touches real
client files.
"""
from __future__ import annotations

import base64
from typing import Any, Callable

from fastapi import Body, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

from tuomin_gateway import agent_clients
from tuomin_gateway.agent_setup import (
    AGENT_APP_ID,
    DEFAULT_SOURCE_APP,
    agent_entry,
    default_data_dir,
    load_agent_tokens,
    new_tokens,
    rotate_agent_tokens,
    token_hashes,
    tokens_path,
    _write_private,
)


def register_agent_admin(app: FastAPI, registry, audit: Callable[..., None]) -> None:
    def _error(code: str, message: str, status: int = 400) -> JSONResponse:
        return JSONResponse({"error": {"code": code, "message": message}}, status_code=status)

    def _no_demo() -> None:
        if getattr(app.state, "demo", False):
            raise HTTPException(status_code=403, detail="演示模式不会写入任何 AI 工具的配置")

    def _app_state() -> dict:
        entry = registry.get_app(AGENT_APP_ID)
        tokens = load_agent_tokens(default_data_dir())
        configured = bool(entry and tokens and entry.get("capability_tokens", {}) == {k: v for k, v in token_hashes(tokens).items() if k in entry.get("capabilities", [])})
        dictionary = (entry or {}).get("dictionary")
        # The clipboard app mirrors another app's file; name the real owner.
        owners = [name for name in registry.known_apps() if name not in {AGENT_APP_ID, "clipboard"} and (registry.get_app(name) or {}).get("dictionary") == dictionary] if dictionary else []
        source = DEFAULT_SOURCE_APP if DEFAULT_SOURCE_APP in owners else (owners[0] if owners else None)
        return {"app_id": AGENT_APP_ID, "configured": configured, "registered": entry is not None,
                "dictionary_source": source, "has_dictionary": bool(dictionary)}

    def _ensure_agent(source_app: str | None = None) -> None:
        """Make the live registry and the private token file agree."""
        data_dir = default_data_dir()
        tokens = load_agent_tokens(data_dir)
        if tokens is None:
            tokens = new_tokens()
            _write_private(tokens_path(data_dir), tokens)
        existing = registry.get_app(AGENT_APP_ID)
        source = source_app or DEFAULT_SOURCE_APP
        dictionary = (registry.get_app(source) or {}).get("dictionary") if source else None
        if existing is None:
            entry = agent_entry(tokens, dictionary)
        else:
            entry = dict(existing)
            capabilities = [c for c in sorted(set(entry.get("capabilities") or []) | {"redact", "namespace"}) if c != "trusted_refill"]
            entry["capabilities"] = capabilities
            entry["capability_tokens"] = {**{k: v for k, v in (entry.get("capability_tokens") or {}).items() if k in capabilities}, **token_hashes(tokens)}
            entry["mapping_scopes"] = sorted(set(entry.get("mapping_scopes") or ["document"]) | {"namespace"})
            if source_app is not None:
                if dictionary:
                    entry["dictionary"] = dictionary
                else:
                    entry.pop("dictionary", None)
        if entry != existing:
            try:
                registry.upsert_app(AGENT_APP_ID, entry)
            except RuntimeError as exc:
                raise HTTPException(status_code=409, detail="应用注册表不可写，无法配置智能体应用") from exc
            audit("agent_app_upsert", app_id=AGENT_APP_ID, created=existing is None, dictionary_source=source if dictionary else None)

    def _command_view() -> dict:
        command, args, env = agent_clients.mcp_command()
        return {"command": command, "args": args, "env_keys": sorted(env), "warning": agent_clients.command_warning(command)}

    @app.get("/admin/agents")
    def agents_overview() -> dict:
        return {"app": _app_state(), "mcp": _command_view(), "clients": agent_clients.all_status(),
                "sources": [{"app_id": name, "name": (registry.get_app(name) or {}).get("name") or name}
                            for name in registry.known_apps() if name not in {AGENT_APP_ID, "clipboard"} and (registry.get_app(name) or {}).get("dictionary")]}

    @app.post("/admin/agents/setup")
    def agents_setup(payload: dict = Body(default={})) -> dict:
        _no_demo()
        source = payload.get("dictionary_source")
        if source is not None and (not isinstance(source, str) or (source and registry.get_app(source) is None)):
            raise HTTPException(status_code=400, detail="词典来源不存在")
        _ensure_agent(source if source is not None else None)
        return {"app": _app_state()}

    @app.post("/admin/agents/rotate")
    def agents_rotate() -> dict:
        _no_demo()
        _ensure_agent()
        hashes = rotate_agent_tokens(default_data_dir())
        entry = registry.get_app(AGENT_APP_ID) or {}
        entry["capability_tokens"] = {**(entry.get("capability_tokens") or {}), **hashes}
        registry.upsert_app(AGENT_APP_ID, entry)
        audit("agent_tokens_rotated", app_id=AGENT_APP_ID)
        return {"app": _app_state(), "note": "已更换智能体凭证；已接入的客户端无需改动，下次调用自动使用新凭证。"}

    @app.get("/admin/agents/skill.zip")
    def agents_skill_zip() -> dict:
        try:
            data = agent_clients.skill_zip()
        except agent_clients.ClientError as exc:
            return _error(exc.code, exc.message, 404)
        return {"name": "tuomin-guard-skill.zip", "b64": base64.b64encode(data).decode()}

    @app.post("/admin/agents/selftest")
    def agents_selftest(request: Request) -> dict:
        from tuomin_gateway.agent_selftest import run_selftest

        _ensure_agent()
        command, args, env = agent_clients.mcp_command()
        result = run_selftest(command, args, env, gateway_url=str(request.base_url).rstrip("/"))
        audit("agent_selftest", ok=result["ok"], steps=len(result["steps"]))
        return result

    @app.get("/admin/agents/{client_id}/snippet")
    def agents_snippet(client_id: str) -> Any:
        try:
            return {"client": client_id, "snippet": agent_clients.snippet(client_id)}
        except agent_clients.ClientError as exc:
            return _error(exc.code, exc.message, 404)

    @app.post("/admin/agents/{client_id}/install")
    def agents_install(client_id: str, payload: dict = Body(default={})) -> Any:
        _no_demo()
        _ensure_agent()
        try:
            result = agent_clients.install(client_id, with_skill=payload.get("with_skill", True) is not False)
        except agent_clients.ClientError as exc:
            return _error(exc.code, exc.message, 404 if exc.code == "unknown_client" else 400)
        except OSError as exc:
            return _error("config_write_failed", f"写入失败：{exc.strerror or exc}", 500)
        audit("agent_client_install", client=client_id, method=result.get("method"), skill=bool(result.get("skill_written")))
        return result

    @app.post("/admin/agents/{client_id}/uninstall")
    def agents_uninstall(client_id: str) -> Any:
        _no_demo()
        try:
            result = agent_clients.uninstall(client_id)
        except agent_clients.ClientError as exc:
            return _error(exc.code, exc.message, 404 if exc.code == "unknown_client" else 400)
        except OSError as exc:
            return _error("config_write_failed", f"移除失败：{exc.strerror or exc}", 500)
        audit("agent_client_uninstall", client=client_id, removed=result["removed"])
        return result
