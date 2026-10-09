"""Admin CRUD API backing the local WebUI (pillar 4).

Lets a user manage the local dictionary, app registrations, and view settings
through the service instead of hand-editing JSON. All write-back goes through
``AppRegistry`` (which preserves the config file's other keys). The dictionary
entries are the user's OWN curated reference data (org/person/project names), so
returning them is fine — this is NOT the encrypted placeholder mapping, which is
never exposed here.

Trust model: localhost only, guarded by the ``_admin_guard`` middleware in
``app.py`` — a required admin token in a custom header (which a cross-origin
page cannot send without a CORS preflight we never approve, defeating CSRF and
DNS-rebinding) plus a Host-header allowlist for defense in depth. Every
mutation is written to the durable admin audit stream (ids/labels only, never
dictionary values) when an ``AuditLog`` is provided.
"""
from __future__ import annotations

import json
import os
import re
import uuid
from pathlib import Path

from fastapi import Body, FastAPI, HTTPException
from fastapi.responses import JSONResponse

from tuomin_gateway import __version__
from tuomin_gateway.audit import (
    AUDIT_STREAM_ADMIN,
    AUDIT_STREAM_GUARD,
    AUDIT_STREAM_NAMESPACE,
    AUDIT_STREAM_REFILL,
    AuditLog,
)
from tuomin_gateway.dictionary_quality import lint_dictionary_entries
from tuomin_gateway.policy import MAPPING_SCOPE_MODES
from tuomin_gateway.profiles import PRESETS, SCENARIO_PRESETS, SENSITIVITY_PRESETS
from tuomin_gateway.service.registry import (
    AppRegistry,
    _profile_from_spec,
    is_structured_value_label,
)

_ALLOWED_APP_KEYS = {
    "profile", "sensitivity", "scenario", "dictionary", "name",
    "capabilities", "capability_tokens", "refill_contracts", "mapping_scopes",
    "structured_value_labels",
    "proxy_routes", "proxy_targets",
}
_CAPABILITIES = {
    "detect",
    "redact",
    "namespace",
    "trusted_refill",
    "proxy_session",
    "mapping_inspect",
    "admin",
}
_REFILL_CONTRACTS = {"none", "trusted_display", "exact_transform"}
_MAPPING_SCOPES = MAPPING_SCOPE_MODES
_AUDIT_STREAMS = {
    AUDIT_STREAM_GUARD,
    AUDIT_STREAM_REFILL,
    AUDIT_STREAM_NAMESPACE,
    AUDIT_STREAM_ADMIN,
    # Workbench package-export events (v1_documents.py writes this stream).
    "workbench",
}
_AUDIT_TAIL_MAX = 200


def _public_app_entry(entry: dict) -> dict:
    out = {key: value for key, value in entry.items() if key != "capability_tokens"}
    tokens = entry.get("capability_tokens")
    out["configured_capability_tokens"] = sorted(tokens) if isinstance(tokens, dict) else []
    return out


def _safe_data_path(raw: object) -> str:
    """Confine an admin-supplied dictionary path: reject traversal, and require it
    to live under TUOMIN_DATA_DIR when that root is configured. Stops the admin
    API from being coerced into reading/writing arbitrary files on the host."""
    if not isinstance(raw, str) or not raw.strip():
        raise HTTPException(status_code=400, detail="dictionary 路径无效")
    path = Path(raw)
    root = os.environ.get("TUOMIN_DATA_DIR")
    if root:
        root_resolved = Path(root).resolve()
        resolved = (path if path.is_absolute() else root_resolved / path).resolve()
        try:
            resolved.relative_to(root_resolved)
        except ValueError:
            raise HTTPException(status_code=400, detail="dictionary 路径必须在 TUOMIN_DATA_DIR 内")
        return str(resolved)
    if ".." in path.parts:
        raise HTTPException(status_code=400, detail="dictionary 路径不能包含 ..")
    return raw


def _validate_app_entry(entry: object) -> dict:
    if not isinstance(entry, dict):
        raise HTTPException(status_code=400, detail="entry 必须是对象")
    unknown = set(entry) - _ALLOWED_APP_KEYS
    if unknown:
        raise HTTPException(status_code=400, detail=f"不支持的字段: {sorted(unknown)}")
    if "profile" in entry:
        try:
            _profile_from_spec(entry["profile"])
        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(status_code=400, detail=f"profile 配置无效: {exc}")
    capabilities = entry.get("capabilities", [])
    if not isinstance(capabilities, list) or not set(capabilities) <= _CAPABILITIES:
        raise HTTPException(status_code=400, detail="capabilities 配置无效")
    token_hashes = entry.get("capability_tokens", {})
    if not isinstance(token_hashes, dict) or not set(token_hashes) <= set(capabilities):
        raise HTTPException(status_code=400, detail="capability_tokens 配置无效")
    if any(
        not isinstance(value, str)
        or not value.startswith("sha256:")
        or len(value) != 71
        for value in token_hashes.values()
    ):
        raise HTTPException(status_code=400, detail="capability token 必须保存为 sha256 hash")
    refill_contracts = entry.get("refill_contracts", ["none"])
    if not isinstance(refill_contracts, list) or not set(refill_contracts) <= _REFILL_CONTRACTS:
        raise HTTPException(status_code=400, detail="refill_contracts 配置无效")
    mapping_scopes = entry.get("mapping_scopes", ["document"])
    if not isinstance(mapping_scopes, list) or not set(mapping_scopes) <= _MAPPING_SCOPES:
        raise HTTPException(status_code=400, detail="mapping_scopes 配置无效")
    structured_labels = entry.get("structured_value_labels", [])
    if not isinstance(structured_labels, list) or any(
        not is_structured_value_label(label) for label in structured_labels
    ):
        raise HTTPException(status_code=400, detail="structured_value_labels 配置无效")
    out = dict(entry)
    if "dictionary" in out:
        out["dictionary"] = _safe_data_path(out["dictionary"])
    return out


def _validate_entry(entry: dict) -> None:
    if not isinstance(entry, dict):
        raise HTTPException(status_code=400, detail="entry 必须是对象")
    canonical = entry.get("canonical_value")
    label = entry.get("label")
    if not isinstance(canonical, str) or not canonical.strip():
        raise HTTPException(status_code=400, detail="canonical_value 不能为空")
    if not isinstance(label, str) or not label.strip():
        raise HTTPException(status_code=400, detail="label 不能为空")


def _normalize_entry(entry: dict) -> dict:
    out = dict(entry)
    out.setdefault("entry_id", f"e_{uuid.uuid4().hex[:10]}")
    out.setdefault("aliases", [])
    out.setdefault("risk_level", "high")
    out.setdefault("status", "active")
    out.setdefault("version", "ui")
    return out


def _validate_dictionary_quality(entries: list[dict]) -> None:
    report = lint_dictionary_entries(entries)
    if report["status"] != "ok":
        codes = ",".join(report["issue_codes"])
        raise HTTPException(
            status_code=400,
            detail=f"dictionary 质量校验失败: {codes}",
        )


def register_admin(
    app: FastAPI,
    registry: AppRegistry,
    store,
    session_ttl: int,
    audit_log: AuditLog | None = None,
) -> None:
    def _audit(event: str, **fields) -> None:
        """Persist an admin-mutation audit event (never entry VALUES — the
        dictionary holds the user's own curated names, which must not land in
        a log). Audit failures must not break the admin data path."""
        if audit_log is None:
            return
        try:
            audit_log.write(AUDIT_STREAM_ADMIN, {"event": event, **fields})
        except OSError:
            pass

    def _require_app(app_id: str) -> dict:
        entry = registry.get_app(app_id)
        if entry is None:
            raise HTTPException(status_code=404, detail=f"app 不存在: {app_id}")
        return entry

    @app.get("/admin/apps")
    def list_apps() -> dict:
        apps = []
        for app_id in registry.known_apps():
            entry = registry.get_app(app_id) or {}
            apps.append({
                "app_id": app_id,
                "profile": entry.get("profile"),
                "sensitivity": entry.get("sensitivity"),
                "scenario": entry.get("scenario"),
                "has_dictionary": bool(entry.get("dictionary")),
            })
        return {"apps": apps}

    @app.get("/admin/apps/{app_id}")
    def get_app(app_id: str) -> dict:
        return {"app_id": app_id, "entry": _public_app_entry(_require_app(app_id))}

    @app.put("/admin/apps/{app_id}")
    def put_app(app_id: str, entry: dict = Body(default={})) -> dict:
        validated = _validate_app_entry(entry)
        registry.upsert_app(app_id, validated)
        _audit("admin_app_upsert", app_id=app_id)
        return {"app_id": app_id, "entry": _public_app_entry(registry.get_app(app_id) or {})}

    @app.delete("/admin/apps/{app_id}")
    def delete_app(app_id: str) -> dict:
        deleted = registry.delete_app(app_id)
        _audit("admin_app_delete", app_id=app_id, deleted=deleted)
        return {"deleted": deleted}

    @app.get("/admin/apps/{app_id}/dictionary")
    def list_entries(app_id: str) -> dict:
        _require_app(app_id)
        return {"app_id": app_id, "entries": registry.load_entries(app_id)}

    @app.post("/admin/apps/{app_id}/dictionary")
    def add_entry(app_id: str, entry: dict = Body(default={})) -> dict:
        _require_app(app_id)
        _validate_entry(entry)
        normalized = _normalize_entry(entry)
        entries = registry.load_entries(app_id)
        entries.append(normalized)
        _validate_dictionary_quality(entries)
        registry.write_entries(app_id, entries)
        _audit(
            "admin_dictionary_add", app_id=app_id,
            entry_id=normalized["entry_id"], label=normalized.get("label", ""),
        )
        return {"entry": normalized}

    @app.put("/admin/apps/{app_id}/dictionary/{entry_id}")
    def update_entry(app_id: str, entry_id: str, entry: dict = Body(default={})) -> dict:
        _require_app(app_id)
        _validate_entry(entry)
        entries = registry.load_entries(app_id)
        for i, existing in enumerate(entries):
            if existing.get("entry_id") == entry_id:
                merged = {**existing, **entry, "entry_id": entry_id}
                entries[i] = merged
                _validate_dictionary_quality(entries)
                registry.write_entries(app_id, entries)
                _audit(
                    "admin_dictionary_update", app_id=app_id,
                    entry_id=entry_id, label=merged.get("label", ""),
                )
                return {"entry": merged}
        raise HTTPException(status_code=404, detail=f"entry 不存在: {entry_id}")

    @app.delete("/admin/apps/{app_id}/dictionary/{entry_id}")
    def delete_entry(app_id: str, entry_id: str) -> dict:
        _require_app(app_id)
        entries = registry.load_entries(app_id)
        kept = [e for e in entries if e.get("entry_id") != entry_id]
        if len(kept) == len(entries):
            raise HTTPException(status_code=404, detail=f"entry 不存在: {entry_id}")
        registry.write_entries(app_id, kept)
        _audit("admin_dictionary_delete", app_id=app_id, entry_id=entry_id)
        return {"deleted": True}

    @app.get("/admin/audit/{stream}")
    def read_audit(stream: str, tail: int = 50) -> dict:
        """Read the tail of one durable audit stream (the read path that makes
        the WARN channel operationally visible). Events are raw-value-free by
        AuditLog construction, so they are safe to return as-is; the admin
        token middleware gates this like every other /admin route."""
        if stream not in _AUDIT_STREAMS:
            raise HTTPException(status_code=404, detail="unknown audit stream")
        if audit_log is None:
            return {"stream": stream, "events": [], "configured": False}
        tail = max(1, min(tail, _AUDIT_TAIL_MAX))
        path = audit_log.directory / f"{stream}.jsonl"
        events: list[dict] = []
        if path.exists():
            for line in path.read_text(encoding="utf-8").splitlines()[-tail:]:
                try:
                    events.append(json.loads(line))
                except json.JSONDecodeError:
                    continue  # tolerate a torn tail line from an in-flight write
        return {"stream": stream, "events": events, "configured": True}

    # --- WorkBuddy 接入向导（探测网关 → 生成条目 → 写 ~/.workbuddy/models.json）---
    # 关键安全约束：api_key 是用户自己的上游 key，只写进 WorkBuddy 本地配置，
    # 绝不进入审计流、日志或响应体。写路径可用 TUOMIN_WORKBUDDY_MODELS 覆盖（测试用）。
    _workbuddy_app_re = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")

    def _require_valid_app_id(app_id: str) -> str:
        if not _workbuddy_app_re.match(app_id or ""):
            raise HTTPException(status_code=400, detail="app_id 只能包含字母/数字/下划线/点/连字符（1-64 位）")
        return app_id

    def _onboarding_error(exc) -> JSONResponse:
        return JSONResponse(
            status_code=400,
            content={"error": {"code": exc.code, "message": exc.message, "detail": exc.detail}},
        )

    @app.get("/admin/integrations/workbuddy")
    def workbuddy_plan(app_id: str = "") -> dict:
        from tuomin_gateway import workbuddy_onboarding as onboarding

        result: dict = {"apps": registry.known_apps()}
        if app_id:
            _require_valid_app_id(app_id)
            try:
                result["plan"] = onboarding.plan(app_id)
            except onboarding.OnboardingError as exc:
                return _onboarding_error(exc)
        return result

    @app.post("/admin/integrations/workbuddy/app")
    def workbuddy_ensure_app(payload: dict = Body(default={})) -> dict:
        """Create the project app from the server-side WorkBuddy template."""
        from tuomin_gateway import workbuddy_onboarding as onboarding

        app_id = _require_valid_app_id(str(payload.get("app_id", "")).strip())
        upstream_url = str(payload.get("upstream_url") or onboarding.DEFAULT_UPSTREAM_URL).strip()
        upstream_model = str(payload.get("upstream_model") or onboarding.DEFAULT_UPSTREAM_MODEL).strip()
        existing = registry.get_app(app_id)
        if existing is not None and not payload.get("overwrite"):
            return {"app_id": app_id, "created": False, "entry": _public_app_entry(existing)}
        entry = {
            "profile": {"base": "strict", "name": app_id, "refill_strict": False},
            "proxy_upstreams": {"openai": upstream_url},
            "allow_auto_refill": True,
            "upstream_model": upstream_model,
        }
        registry.upsert_app(app_id, entry)
        _audit(
            "workbuddy_app_upsert", app_id=app_id,
            upstream_model=upstream_model, created=existing is None,
        )
        return {"app_id": app_id, "created": existing is None, "entry": _public_app_entry(registry.get_app(app_id) or {})}

    @app.post("/admin/integrations/workbuddy/apply")
    def workbuddy_apply(payload: dict = Body(default={})) -> dict:
        from tuomin_gateway import workbuddy_onboarding as onboarding

        app_id = _require_valid_app_id(str(payload.get("app_id", "")).strip())
        if registry.get_app(app_id) is None:
            raise HTTPException(status_code=404, detail=f"app 不存在: {app_id}（请先在向导里创建项目 app）")
        try:
            result = onboarding.apply(
                app_id,
                api_key=str(payload.get("api_key", "")),
                modes=payload.get("modes"),
                base_url=payload.get("base_url") or None,
            )
        except onboarding.OnboardingError as exc:
            return _onboarding_error(exc)
        # Audit intentionally records counts/URL only — never the api_key.
        _audit(
            "workbuddy_apply", app_id=app_id, gateway_url=result.get("gateway_url"),
            written=result.get("written_entries"), retargeted=result.get("retargeted_entries"),
        )
        return result

    @app.post("/admin/integrations/workbuddy/verify")
    def workbuddy_verify(payload: dict = Body(default={})) -> dict:
        from tuomin_gateway import workbuddy_onboarding as onboarding

        app_id = _require_valid_app_id(str(payload.get("app_id", "")).strip())
        try:
            return onboarding.verify(app_id, base_url=payload.get("base_url") or None)
        except onboarding.OnboardingError as exc:
            return _onboarding_error(exc)

    @app.get("/admin/settings")
    def settings() -> dict:
        import importlib.util

        return {
            "version": __version__,
            "mapping_ttl_seconds": getattr(store, "ttl_seconds", None),
            "session_ttl_seconds": session_ttl,
            "mapping_dir": str(getattr(store, "directory", "")),
            "mapping_store": store.safe_status() if hasattr(store, "safe_status") else {},
            "dpapi_available": os.name == "nt",
            "ner_available": importlib.util.find_spec("transformers") is not None,
            "profiles": sorted(PRESETS),
            "sensitivities": sorted(SENSITIVITY_PRESETS),
            "scenarios": sorted(SCENARIO_PRESETS),
            "apps": registry.known_apps(),
        }
