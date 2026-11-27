"""Provision the local ``agent`` app that MCP clients use, without client tokens.

The desktop launcher registers one app for AI tools (Claude Code, Codex,
Cursor, Claude Desktop, WorkBuddy …). Its capability tokens are generated here,
kept in plaintext only in the private data directory (``agent-tokens.json``,
0600), and stored in the registry as SHA-256 verifiers. ``--mcp-stdio`` reads
the tokens from the same directory, so client configuration files carry only
the command path — never a credential — and rotating the tokens needs no client
change.

The app deliberately lacks ``trusted_refill``: agents can redact (stateless or
within a namespace session) but can never restore values. Pure stdlib.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

from tuomin_gateway.capability_tokens import generate_capability_token

AGENT_APP_ID = "agent"
AGENT_APP_NAME = "AI 智能体"
AGENT_PROFILE = "file_workbench"
AGENT_CAPABILITIES = ("redact", "namespace")
TOKENS_FILE = "agent-tokens.json"
DEFAULT_SOURCE_APP = "workbench"


def tokens_path(data_dir: Path | str) -> Path:
    return Path(data_dir) / TOKENS_FILE


def _write_private(path: Path, payload: dict) -> None:
    from tuomin_gateway.shell.storage import write_private
    write_private(path, payload)


def _valid_tokens(tokens: object) -> bool:
    return (
        isinstance(tokens, dict)
        and set(tokens) == set(AGENT_CAPABILITIES)
        and all(isinstance(value, str) and len(value) >= 32 for value in tokens.values())
    )


def load_agent_tokens(data_dir: Path | str) -> dict[str, str] | None:
    """Plaintext tokens for the MCP adapter, or None when not provisioned."""
    try:
        from tuomin_gateway.shell.storage import read_private
        tokens = read_private(tokens_path(data_dir))
    except (OSError, ValueError):
        return None
    return tokens if _valid_tokens(tokens) else None


def token_hashes(tokens: dict[str, str]) -> dict[str, str]:
    return {name: "sha256:" + hashlib.sha256(value.encode()).hexdigest() for name, value in tokens.items()}


def new_tokens() -> dict[str, str]:
    return {name: generate_capability_token()[0] for name in AGENT_CAPABILITIES}


def agent_entry(tokens: dict[str, str], dictionary: str | None) -> dict:
    entry = {
        "name": AGENT_APP_NAME,
        "profile": AGENT_PROFILE,
        "capabilities": list(AGENT_CAPABILITIES),
        "capability_tokens": token_hashes(tokens),
        "refill_contracts": ["none"],
        "mapping_scopes": ["document", "namespace"],
    }
    if dictionary:
        entry["dictionary"] = dictionary
    return entry


def _source_dictionary(apps: dict, source_app: str) -> str | None:
    source = apps.get(source_app)
    if isinstance(source, dict) and isinstance(source.get("dictionary"), str) and source["dictionary"]:
        return source["dictionary"]
    return None


def ensure_agent_app(config_path: Path | str, data_dir: Path | str, *, source_app: str = DEFAULT_SOURCE_APP) -> bool:
    """Register ``agent`` and its tokens if missing; repair a mismatched token file.

    Returns True when the registry changed. A user-edited ``agent`` entry keeps
    its profile and dictionary; only the capability verifiers are kept in step
    with the private token file so ``--mcp-stdio`` always authenticates.
    """
    config_path = Path(config_path)
    try:
        raw = json.loads(config_path.read_text(encoding="utf-8")) if config_path.exists() else {}
    except (OSError, ValueError):
        return False
    if not isinstance(raw, dict):
        return False
    apps = raw.get("apps")
    if not isinstance(apps, dict):
        apps = {}
        raw["apps"] = apps

    tokens = load_agent_tokens(data_dir)
    if tokens is None:
        tokens = new_tokens()
        _write_private(tokens_path(data_dir), tokens)

    existing = apps.get(AGENT_APP_ID)
    if isinstance(existing, dict):
        wanted = token_hashes(tokens)
        capabilities = sorted(set(existing.get("capabilities") or []) | set(AGENT_CAPABILITIES))
        scopes = sorted(set(existing.get("mapping_scopes") or ["document"]) | {"namespace"})
        if existing.get("capability_tokens") == wanted and sorted(existing.get("capabilities") or []) == capabilities and sorted(existing.get("mapping_scopes") or []) == scopes:
            return False
        existing.update(capabilities=capabilities, capability_tokens={**(existing.get("capability_tokens") or {}), **wanted}, mapping_scopes=scopes)
        # trusted_refill is never granted to agents, even if added by hand.
        existing["capabilities"] = [c for c in existing["capabilities"] if c != "trusted_refill"]
        existing["capability_tokens"] = {k: v for k, v in existing["capability_tokens"].items() if k in existing["capabilities"]}
    else:
        apps[AGENT_APP_ID] = agent_entry(tokens, _source_dictionary(apps, source_app))

    _write_private(config_path, raw)
    return True


def rotate_agent_tokens(data_dir: Path | str) -> dict[str, str]:
    """Write fresh tokens and return their registry verifiers (caller upserts)."""
    tokens = new_tokens()
    _write_private(tokens_path(data_dir), tokens)
    return token_hashes(tokens)


def default_data_dir(*, use_override: bool = True) -> Path:
    """Mirror the desktop launcher's data directory (env override first)."""
    import sys

    configured = os.environ.get("TUOMIN_DATA_DIR") if use_override else None
    if configured:
        return Path(configured).expanduser()
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "Tuomin"
    if os.name == "nt":
        appdata = os.environ.get("APPDATA")
        return (Path(appdata) if appdata else Path.home() / "AppData" / "Roaming") / "Tuomin"
    return Path.home() / ".local" / "share" / "tuomin"
