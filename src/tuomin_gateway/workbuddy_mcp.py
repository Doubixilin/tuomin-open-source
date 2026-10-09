"""Write Tuomin's STDIO MCP adapter into WorkBuddy's local ``mcp.json``.

WorkBuddy 5.5.6 exposes a **连接器** (connectors) surface backed by
``~/.workbuddy/mcp.json`` — a plain ``{"mcpServers": {...}}`` map supporting
STDIO servers (``command``/``args``/``env``). Unlike the (UI-hidden) plugin
system, this file is the supported way to give the agent Tuomin tools.

Entries are written with **absolute paths** (no ``${CODEBUDDY_PLUGIN_ROOT}``):
WorkBuddy does not resolve plugin variables in the user MCP file, which is
exactly how a plugin-provided MCP server silently breaks.

The MCP adapter holds capability tokens from its environment (agents never see
them). Those tokens are stored in ``mcp.json`` in plaintext, like any MCP env —
treat the file as a secret.
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import time
from pathlib import Path

DEFAULT_SERVER_NAME = "tuomin"
PACKAGE_DIR = Path(__file__).resolve().parent          # .../tuomin_gateway
SOURCE_ROOT = PACKAGE_DIR.parent                        # .../src (or site-packages)


def default_mcp_path() -> Path:
    override = os.environ.get("TUOMIN_WORKBUDDY_MCP")
    if override:
        return Path(override).expanduser()
    return Path.home() / ".workbuddy" / "mcp.json"


def default_python() -> str:
    """Prefer the interpreter running us (usually the project venv)."""
    return sys.executable or "python3"


def build_server_entry(
    *,
    python: str | None = None,
    gateway_url: str,
    redact_token: str = "",
    namespace_token: str = "",
    app_id: str = "",
) -> dict:
    env = {
        "PYTHONPATH": str(SOURCE_ROOT),
        "TUOMIN_GATEWAY_URL": gateway_url,
    }
    if redact_token:
        env["TUOMIN_AGENT_REDACT_TOKEN"] = redact_token
    if namespace_token:
        env["TUOMIN_AGENT_NAMESPACE_TOKEN"] = namespace_token
    if app_id:
        env["TUOMIN_AGENT_APP"] = app_id
    return {
        # WorkBuddy's MCP loader expects an explicit transport type (its plugin
        # stdio servers all carry "type":"stdio"); an entry with only `command`
        # is listed in --mcp-config but never spawned.
        "type": "stdio",
        "command": python or default_python(),
        "args": ["-m", "tuomin_gateway.mcp_stdio"],
        "env": env,
        "disabled": False,
    }


def _load(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def install_mcp_server(
    *,
    gateway_url: str,
    mcp_file: Path | str | None = None,
    server_name: str = DEFAULT_SERVER_NAME,
    python: str | None = None,
    redact_token: str = "",
    namespace_token: str = "",
    app_id: str = "",
) -> dict:
    """Merge ``mcpServers[server_name]`` into WorkBuddy's mcp.json (backup first)."""
    if not gateway_url:
        raise ValueError("gateway_url is required")
    path = Path(mcp_file).expanduser() if mcp_file else default_mcp_path()
    data = _load(path)
    servers = data.setdefault("mcpServers", {})
    if not isinstance(servers, dict):
        servers = {}
        data["mcpServers"] = servers

    backup = None
    if path.exists():
        backup = path.with_suffix(f".json.bak-{int(time.time())}")
        shutil.copy2(path, backup)

    servers[server_name] = build_server_entry(
        python=python, gateway_url=gateway_url,
        redact_token=redact_token, namespace_token=namespace_token, app_id=app_id,
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return {
        "mcp_file": str(path),
        "server_name": server_name,
        "gateway_url": gateway_url,
        "backup": str(backup) if backup else None,
        "servers_total": len(servers),
        "has_redact_token": bool(redact_token),
        "has_namespace_token": bool(namespace_token),
    }


def uninstall_mcp_server(
    *,
    mcp_file: Path | str | None = None,
    server_name: str = DEFAULT_SERVER_NAME,
) -> dict:
    path = Path(mcp_file).expanduser() if mcp_file else default_mcp_path()
    data = _load(path)
    servers = data.get("mcpServers")
    removed = False
    if isinstance(servers, dict) and server_name in servers:
        del servers[server_name]
        removed = True
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return {"mcp_file": str(path), "server_name": server_name, "removed": removed}
