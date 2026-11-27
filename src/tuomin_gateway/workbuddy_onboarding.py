"""WorkBuddy onboarding: detect the live gateway, generate entries, write them.

This is the reusable core behind three front-ends:

- the CLI (``scripts/gen_workbuddy_models.py``),
- the local admin API / WebUI wizard (``service/admin.py`` + ``/ui``),
- any future MCP tool.

Flow: ``plan`` (dry-run, shows what would be written) → ``apply`` (backup +
retarget stale ports + merge) → ``verify`` (local consistency check).

Security: the upstream API key is the user's own. It is written only into
WorkBuddy's local ``models.json`` (which already stores it in plaintext) and is
**never** logged, audited, or returned by these functions. ``plan`` never needs
the key at all.
"""
from __future__ import annotations

import json
import shutil
import time
from pathlib import Path
from urllib.parse import urlsplit

from tuomin_gateway.gateway_discovery import (
    DEFAULT_HOST,
    DEFAULT_PROBE_TIMEOUT,
    DEFAULT_SCAN_RANGE,
    parse_scan_range,
    resolve_gateway,
)
from tuomin_gateway.workbuddy_models import (
    MODES,
    PLACEHOLDER_API_KEY,
    build_entries,
    entries_for_app,
    entry_format_errors,
    load_models,
    merge_entries,
    mode_from_url,
    prune_app_entries,
    retarget_entries,
)

DEFAULT_MODES = ("masked", "demo", "auto")

# The WorkBuddy app registry template the wizard creates when a project app does
# not exist yet. Kept server-side so the wizard never accepts arbitrary app keys.
WORKBUDDY_APP_TEMPLATE = {
    "profile": {"base": "strict", "name": "workbuddy", "refill_strict": False},
    "proxy_upstreams": {"openai": "https://api.deepseek.com/v1/chat/completions"},
    "allow_auto_refill": True,
    "upstream_model": "deepseek-v4-flash",
}
DEFAULT_UPSTREAM_URL = "https://api.deepseek.com/v1/chat/completions"
DEFAULT_UPSTREAM_MODEL = "deepseek-v4-flash"


class OnboardingError(Exception):
    """Actionable onboarding failure (``code`` is stable for the UI)."""

    def __init__(self, code: str, message: str, detail: dict | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.detail = detail or {}


def chat_ready(entry: dict | None) -> bool:
    """True when the gateway can actually forward this app's chats upstream.

    The proxy resolves the upstream from the registry entry
    (``proxy_upstreams.openai``) or the ``TUOMIN_UPSTREAM_OPENAI`` environment;
    without either, every WorkBuddy request is answered with
    ``502 proxy_upstream_unavailable`` — the "connection failed" a user sees
    after pointing WorkBuddy at the default file/clipboard workbench app."""
    import os

    if os.environ.get("TUOMIN_UPSTREAM_OPENAI"):
        return True
    upstreams = entry.get("proxy_upstreams") if isinstance(entry, dict) else None
    return isinstance(upstreams, dict) and bool(upstreams.get("openai"))


def require_chat_app(app: str, entry: dict | None) -> None:
    if not chat_ready(entry):
        raise OnboardingError(
            "app_has_no_upstream",
            f"应用 {app!r} 没有配置上游模型地址，不能接入 WorkBuddy：网关会用 502 proxy_upstream_unavailable 拒绝它的每次对话，"
            "WorkBuddy 侧表现为「连接失败」。它是文件 / 剪贴板工作台使用的应用；请用下方「一键创建」新建一个项目应用"
            "（填写上游地址与模型），或在「应用」页给它补上上游地址后再写入。",
            {"app": app, "profile": (entry or {}).get("profile")},
        )


def default_models_path() -> Path:
    import os

    override = os.environ.get("TUOMIN_WORKBUDDY_MODELS")
    if override:
        return Path(override).expanduser()
    return Path.home() / ".workbuddy" / "models.json"


def _scan_ports(scan_ports, allow_scan: bool) -> list[int]:
    if not allow_scan:
        return []
    if scan_ports is None:
        return parse_scan_range(DEFAULT_SCAN_RANGE)
    return list(scan_ports)


def _gateway_dict(gateway) -> dict:
    return {
        "base_url": gateway.base_url,
        "port": gateway.port,
        "apps": list(gateway.apps),
        "apps_known": gateway.apps_known,
        "version": gateway.version,
        "source": gateway.source,
    }


def _validate_modes(modes) -> list[str]:
    chosen = list(modes) if modes else list(DEFAULT_MODES)
    unknown = [m for m in chosen if m not in MODES]
    if unknown:
        raise OnboardingError("unknown_mode", f"未知模式: {unknown}", {"allowed": sorted(MODES)})
    return chosen


def _resolve(app, *, base_url, port, timeout, scan_ports, allow_scan, host):
    return resolve_gateway(
        app,
        base_url=base_url,
        port=port,
        scan_ports=_scan_ports(scan_ports, allow_scan),
        allow_scan=allow_scan,
        timeout=timeout,
        host=host,
    )


def plan(
    app: str,
    *,
    base_url: str | None = None,
    port: int | None = None,
    models_file: Path | str | None = None,
    timeout: float = DEFAULT_PROBE_TIMEOUT,
    scan_ports=None,
    allow_scan: bool = True,
    host: str = DEFAULT_HOST,
) -> dict:
    """Dry-run: which gateway would be used and what would be written."""
    if not app or not str(app).strip():
        raise OnboardingError("app_required", "app_id 不能为空")
    path = Path(models_file).expanduser() if models_file else default_models_path()
    resolution = _resolve(
        app, base_url=base_url, port=port, timeout=timeout,
        scan_ports=scan_ports, allow_scan=allow_scan, host=host,
    )
    existing = load_models(path)
    existing_app = entries_for_app(existing, app)
    planned = build_entries(app, resolution.base_url, PLACEHOLDER_API_KEY, list(DEFAULT_MODES)) \
        if resolution.base_url else []
    return {
        "app": app,
        "gateway_url": resolution.base_url,
        "verified": resolution.verified,
        "reason": resolution.reason,
        "gateways": [_gateway_dict(g) for g in resolution.gateways],
        "models_file": str(path),
        "models_exists": path.exists(),
        "models_total": len(existing),
        "existing_entries": [
            {"id": entry.get("id"), "mode": mode_from_url(entry.get("url"), app), "url": entry.get("url")}
            for entry in existing_app
        ],
        "planned_entries": planned,
        "model_names": [e["name"] for e in planned],
    }


def apply(
    app: str,
    *,
    api_key: str,
    modes=None,
    base_url: str | None = None,
    port: int | None = None,
    models_file: Path | str | None = None,
    timeout: float = DEFAULT_PROBE_TIMEOUT,
    scan_ports=None,
    allow_scan: bool = True,
    host: str = DEFAULT_HOST,
    prune_apps=None,
) -> dict:
    """Write/refresh this app's WorkBuddy entries. Never logs or returns ``api_key``.

    ``prune_apps``: apps without an upstream whose stale entries are removed."""
    if not app or not str(app).strip():
        raise OnboardingError("app_required", "app_id 不能为空")
    if not api_key or not str(api_key).strip():
        raise OnboardingError("api_key_required", "请填写上游 API key")
    chosen_modes = _validate_modes(modes)

    resolution = _resolve(
        app, base_url=base_url, port=port, timeout=timeout,
        scan_ports=scan_ports, allow_scan=allow_scan, host=host,
    )
    if resolution.base_url is None:
        raise OnboardingError(
            "no_gateway_serves_app",
            f"未找到挂载 app {app!r} 的在线 Tuomin 网关",
            {"gateways": [_gateway_dict(g) for g in resolution.gateways]},
        )

    path = Path(models_file).expanduser() if models_file else default_models_path()
    existing = load_models(path)
    backup = None
    if path.exists():
        backup = path.with_suffix(f".json.bak-{int(time.time())}")
        shutil.copy2(path, backup)

    entries = build_entries(app, resolution.base_url, str(api_key).strip(), chosen_modes)
    retargeted = retarget_entries(existing, app, resolution.base_url)
    merged = merge_entries(existing, entries, app=app)
    merged, pruned = prune_app_entries(merged, [a for a in (prune_apps or []) if a != app])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(merged, ensure_ascii=False, indent=2), encoding="utf-8")

    return {
        "app": app,
        "gateway_url": resolution.base_url,
        "gateway_verified": resolution.verified,
        "models_file": str(path),
        "backup": str(backup) if backup else None,
        "written_entries": len(entries),
        "retargeted_entries": len(retargeted),
        "pruned_entries": len(pruned),
        "models_total": len(merged),
        "model_names": [e["name"] for e in entries],
        "workbuddy_loaded": None,
        "note": "配置已写入；请在 WorkBuddy 的模型列表中确认，未出现时重新打开会话或重启 WorkBuddy。",
    }


def verify(
    app: str,
    *,
    base_url: str | None = None,
    port: int | None = None,
    models_file: Path | str | None = None,
    timeout: float = DEFAULT_PROBE_TIMEOUT,
    scan_ports=None,
    allow_scan: bool = True,
    host: str = DEFAULT_HOST,
) -> dict:
    """Local consistency check: gateway serves the app, and entries point at it.

    Deliberately does NOT call the upstream model (no token cost): a real
    end-to-end check is the user's first prompt in WorkBuddy.
    """
    if not app or not str(app).strip():
        raise OnboardingError("app_required", "app_id 不能为空")
    path = Path(models_file).expanduser() if models_file else default_models_path()
    resolution = _resolve(
        app, base_url=base_url, port=port, timeout=timeout,
        scan_ports=scan_ports, allow_scan=allow_scan, host=host,
    )
    existing = load_models(path)
    app_entries = entries_for_app(existing, app)

    by_mode: dict[str, dict] = {}
    stale: list[dict] = []
    invalid: list[dict] = []
    for entry in app_entries:
        mode = mode_from_url(entry.get("url"), app)
        origin = urlsplit(entry["url"])
        gateway_origin = urlsplit(resolution.base_url or "")
        errors = entry_format_errors(entry)
        row = {"id": entry.get("id"), "mode": mode, "url": entry.get("url"),
               "points_at_gateway": bool(resolution.base_url and
                   (origin.scheme, origin.netloc) == (gateway_origin.scheme, gateway_origin.netloc)),
               "format_valid": not errors, "format_errors": errors}
        by_mode.setdefault(mode or "unknown", row)
        if not row["points_at_gateway"]:
            stale.append(row)
        if errors:
            invalid.append(row)

    issues = []
    if not resolution.verified:
        issues.append("未找到挂载该应用的在线网关，请先启动网关。")
    if not app_entries:
        issues.append("尚未生成该应用的模型配置，请点击「生成并写入 WorkBuddy」。")
    if invalid:
        issues.append("模型配置格式不兼容，请使用新版重新生成并写入。")
    if stale:
        issues.append("存在指向旧网关的条目，请重新生成并写入以更新地址。")

    return {
        "app": app,
        "gateway_url": resolution.base_url,
        "gateway_serves_app": bool(resolution.verified),
        "models_file": str(path),
        "entries": list(by_mode.values()),
        "stale_entries": stale,
        "invalid_entries": invalid,
        "issues": issues,
        "ok": not issues,
        "workbuddy_loaded": None,
        "note": "仅检查本地配置格式和网关地址，尚未确认 WorkBuddy 已加载。请在 WorkBuddy 中选择模型并发送测试消息。",
    }
