"""FastAPI application for the local desensitization service (127.0.0.1).

One neutral engine; each app declares a profile + dictionary via the registry.
Stateless redact/refill persist an encrypted mapping keyed by task_id; session
endpoints keep placeholders stable across many calls for agent-style consumers.
"""
from __future__ import annotations

import os
import secrets
import time
import uuid

from fastapi import Body, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

from tuomin_gateway import __version__
from tuomin_gateway.audit import build_audit_event
from tuomin_gateway.placeholders import reserved_placeholder_conflict
from tuomin_gateway.policy import (
    POLICY_SCHEMA_VERSION,
    PROFILE_COMPATIBILITY,
    policy_compatibility_metadata,
)
from tuomin_gateway.profiles import Profile
from tuomin_gateway.redactor import redact_text
from tuomin_gateway.refill import refill_text
from tuomin_gateway.service.registry import (
    AppRegistry,
    PROXY_UPSTREAM_ENV,
    ProfileOverrideDenied,
    RegistryConfigurationError,
)
from tuomin_gateway.service.limits import text_over_limit
from tuomin_gateway.session import (
    RequiredDetectorUnavailable,
    SessionRedactor,
    build_detectors,
    probe_ner_runtime,
    run_detection,
)
from tuomin_gateway.store import MappingStore


class DetectorDowngradeDenied(ValueError):
    """Request data attempted to disable an app-authorized detector."""

    code = "detector_downgrade_denied"


def _ner_available() -> bool:
    # find_spec only checks installability — it does NOT import transformers,
    # so /healthz stays fast (a real import can take 5-15s the first time).
    import importlib.util

    return importlib.util.find_spec("transformers") is not None


def _allowed_admin_hosts() -> set[str]:
    hosts = {"127.0.0.1", "localhost", "::1"}
    extra = os.environ.get("TUOMIN_ALLOWED_HOSTS", "")
    hosts.update(h.strip().lower() for h in extra.split(",") if h.strip())
    return hosts


def _license_block_message(state) -> str:
    from tuomin_gateway.licensing import LicenseState

    if state is LicenseState.EXPIRED:
        return "授权已过期且宽限期已结束：请联系发行方续期。已有数据的回填与导出不受影响。"
    return "未激活有效授权：请先在 WebUI「授权」栏粘贴授权码激活。"


def create_app(
    registry: AppRegistry | None = None,
    store: MappingStore | None = None,
    session_ttl: int | None = None,
    admin_token: str | None = None,
) -> FastAPI:
    registry = registry or AppRegistry.load()
    store = store or MappingStore(
        os.environ.get("TUOMIN_MAPPING_DIR", "tuomin_mappings"),
        ttl_seconds=int(os.environ.get("TUOMIN_MAPPING_TTL", str(24 * 3600))),
    )
    if store.ttl_seconds <= 0:
        raise ValueError("service mapping TTL must be greater than zero")
    session_ttl = session_ttl if session_ttl is not None else int(os.environ.get("TUOMIN_SESSION_TTL", "3600"))

    app = FastAPI(title="Tuomin Desensitization Gateway", version=__version__)
    sessions: dict[str, dict] = {}

    # Admin auth: a token required on the state-changing admin surface. It lives in
    # a CUSTOM header, which a cross-origin page cannot send without a CORS
    # preflight we never approve — so this also defeats CSRF and DNS-rebinding
    # against the no-auth-by-default localhost trust model. A Host-header check is
    # layered on for defense in depth.
    admin_token = admin_token or os.environ.get("TUOMIN_ADMIN_TOKEN") or secrets.token_urlsafe(24)
    app.state.admin_token = admin_token
    allowed_hosts = _allowed_admin_hosts()

    # --- 离线授权（licensing）---
    # 分发构建由 launcher 置 TUOMIN_LICENSE_REQUIRED=1 启用执行；开发/测试
    # 默认关闭（provider 为 None 时 enforce_new_task 恒放行，现有行为零变化）。
    from datetime import datetime, timezone
    from pathlib import Path as _Path

    from tuomin_gateway import licensing
    from tuomin_gateway.licensing import ClockTracker, LicenseStore

    license_required = os.environ.get("TUOMIN_LICENSE_REQUIRED", "") == "1"
    license_data_dir = _Path(
        os.environ.get("TUOMIN_DATA_DIR", str(_Path(store.directory).parent))
    )
    license_store = LicenseStore(license_data_dir)
    license_clock = ClockTracker(license_store)

    def _license_snapshot() -> tuple:
        """(state, payload, effective_now)；effective_now 含防回拨 max_seen。"""
        now = datetime.now(timezone.utc)
        max_seen = license_clock.observe(now)
        payload = license_store.load()
        effective_now = max(now, max_seen)
        state = licensing.compute_state(payload, now=now, max_seen=max_seen)
        return state, payload, effective_now

    def _license_state() -> licensing.LicenseState:
        return _license_snapshot()[0]

    # 每次 create_app 都重置全局 provider：单进程多 app（测试）互不泄漏。
    licensing.configure_enforcement(_license_state if license_required else None)
    app.state.license_required = license_required

    def _delivery_info() -> dict | None:
        """构建期烤入的交付标识（A 机制），仅冻结产物存在。"""
        import sys as _sys

        meipass = getattr(_sys, "_MEIPASS", None)
        if not meipass:
            return None
        import json as _json

        try:
            info = _json.loads((_Path(meipass) / "delivery.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return info if isinstance(info, dict) else None

    app.state.delivery_info = _delivery_info()

    from tuomin_gateway.detectors.rules import UnsafeCredentialInput

    @app.exception_handler(UnsafeCredentialInput)
    async def _unsafe_credential_input(request, exc):
        return JSONResponse({"status": "blocked", "error": {
            "code": "invalid_private_key_block",
            "message": "Private key block is incomplete or mismatched; no output was produced.",
        }}, status_code=400)

    @app.exception_handler(RequiredDetectorUnavailable)
    async def _required_detector_unavailable(request, exc: RequiredDetectorUnavailable):
        payload = {
            "error": {
                "code": "required_detector_unavailable",
                "message": "required detector unavailable",
            },
            "detectors": exc.readiness.to_safe_dict(),
        }
        if request.url.path.startswith("/api/v1/"):
            payload = {
                "status": "error",
                "version": __version__,
                "egress_allowed": False,
                **payload,
            }
        return JSONResponse(payload, status_code=503)

    @app.exception_handler(RegistryConfigurationError)
    async def _registry_configuration_error(request, exc: RegistryConfigurationError):
        payload = {"error": {"code": exc.code, "message": str(exc)}}
        if request.url.path.startswith("/api/v1/"):
            payload = {
                "status": "error",
                "version": __version__,
                "egress_allowed": False,
                **payload,
            }
        return JSONResponse(payload, status_code=503)

    @app.exception_handler(ProfileOverrideDenied)
    async def _profile_override_denied(_request, exc: ProfileOverrideDenied):
        return JSONResponse(
            {"error": {"code": exc.code, "message": "request profile override denied"}},
            status_code=400,
        )

    @app.exception_handler(DetectorDowngradeDenied)
    async def _detector_downgrade_denied(_request, exc: DetectorDowngradeDenied):
        return JSONResponse(
            {"error": {"code": exc.code, "message": "request detector downgrade denied"}},
            status_code=400,
        )

    @app.exception_handler(licensing.LicenseBlockedError)
    async def _license_blocked(_request, exc: licensing.LicenseBlockedError):
        # 业务入口原生后手（redactor 等编译模块）抛出的授权拦截。
        return JSONResponse(
            {"error": {"code": f"license_{exc.state.value}", "message": _license_block_message(exc.state)}},
            status_code=403,
        )

    @app.middleware("http")
    async def _license_gate(request: Request, call_next):
        # 授权第一道（HTTP 分类拦截）；恢复/导出/管理面永不拦截。
        if license_required and licensing.is_new_task_request(request.method, request.url.path):
            state = _license_state()
            if not licensing.new_tasks_allowed(state):
                return JSONResponse(
                    {"error": {"code": f"license_{state.value}", "message": _license_block_message(state)}},
                    status_code=403,
                )
        return await call_next(request)

    @app.middleware("http")
    async def _ui_no_cache(request: Request, call_next):
        # WebUI 静态页禁止缓存：应用升级后浏览器旧缓存会让用户对着过期 UI
        # 操作（真实踩坑：新功能按钮不出现，反复报旧错误）。no-cache 仍允许
        # 条件请求缓存，代价可忽略。
        response = await call_next(request)
        if request.url.path.startswith("/ui"):
            response.headers["Cache-Control"] = "no-cache"
        return response

    @app.middleware("http")
    async def _admin_guard(request: Request, call_next):
        path = request.url.path
        if (
            path.startswith("/admin")
            or path == "/dict/reload"
            or path.startswith("/api/v1/license")
            or path.startswith("/api/v1/documents")
        ):
            host = request.headers.get("host", "").rsplit(":", 1)[0].lower().strip("[]")
            if host and host not in allowed_hosts:
                return JSONResponse({"error": {"message": "tuomin: host not allowed"}}, status_code=403)
            if not secrets.compare_digest(request.headers.get("x-tuomin-admin-token", ""), admin_token):
                return JSONResponse({"error": {"message": "tuomin: admin token required"}}, status_code=401)
        response = await call_next(request)
        if path in {"/redact", "/refill", "/redact_snippet"} or path.startswith(
            "/session/"
        ):
            response.headers["deprecation"] = "true"
            response.headers["x-tuomin-legacy-api"] = "true"
            response.headers["link"] = '</api/v1/health>; rel="successor-version"'
        return response

    # --- helpers ---
    def _resolve(payload: dict) -> tuple[Profile, list[dict] | None]:
        app_id = payload.get("app_id")
        try:
            profile = registry.resolve_profile(app_id, payload.get("profile"))
        except ProfileOverrideDenied:
            raise  # handled app-wide as 400 profile_override_denied
        except (KeyError, TypeError, ValueError):
            # RegistryConfigurationError (a server-side bad declaration) is NOT
            # caught here — it propagates to the app-wide 503 handler.
            raise HTTPException(status_code=400, detail="unknown or invalid profile")
        if payload.get("use_ner") is not None:  # per-request override
            from tuomin_gateway.profiles import with_overrides

            requested = bool(payload["use_ner"])
            if profile.use_ner and not requested:
                raise DetectorDowngradeDenied("request cannot disable app-authorized NER")
            if requested and not profile.use_ner:
                profile = with_overrides(profile, use_ner=True)
        return profile, registry.resolve_dictionary(app_id)

    def _evict_expired_sessions() -> None:
        if session_ttl <= 0:
            return
        now = time.time()
        for sid in [s for s, v in sessions.items() if now - v["ts"] > session_ttl]:
            sessions.pop(sid, None)

    def _get_session(sid: str) -> SessionRedactor:
        _evict_expired_sessions()
        entry = sessions.get(sid)
        if entry is None:
            raise HTTPException(status_code=404, detail="session 不存在或已过期")
        entry["ts"] = time.time()
        return entry["redactor"]

    # --- endpoints ---
    @app.get("/healthz")
    def healthz() -> dict:
        store.purge_expired()
        return {"status": "ok", "version": __version__, "ner_available": _ner_available()}

    @app.get("/readiness")
    def readiness() -> JSONResponse:
        # Unknown/unregistered traffic falls back to strict, so the process is
        # ready only when the strict detector floor can actually be loaded.
        ner_required = registry.resolve_profile(None).ner_required
        dictionary_errors: list[dict[str, str]] = []
        proxy_upstream_errors: list[dict[str, str]] = []
        for app_id in registry.known_apps():
            profile = registry.resolve_profile(app_id)
            ner_required = ner_required or profile.ner_required
            try:
                registry.resolve_dictionary(app_id)
            except RegistryConfigurationError as exc:
                dictionary_errors.append({"app_id": app_id, "code": exc.code})
            entry = registry.get_app(app_id) or {}
            capabilities = entry.get("capabilities", [])
            if isinstance(capabilities, list) and "proxy_session" in capabilities:
                try:
                    routes = registry.allowed_proxy_routes(app_id)
                    targets = registry.proxy_targets(app_id)
                    if targets:
                        target_routes = {spec["route"] for spec in targets.values()}
                        if not target_routes.issubset(routes):
                            proxy_upstream_errors.append(
                                {
                                    "app_id": app_id,
                                    "code": "proxy_target_policy_unavailable",
                                }
                            )
                        for route in sorted(routes - target_routes):
                            proxy_upstream_errors.append(
                                {
                                    "app_id": app_id,
                                    "route": route,
                                    "code": "proxy_target_unavailable",
                                }
                            )
                    else:
                        for route in sorted(routes):
                            if os.environ.get(PROXY_UPSTREAM_ENV[route]):
                                continue
                            if registry.resolve_proxy_upstream(app_id, route) is None:
                                proxy_upstream_errors.append(
                                    {
                                        "app_id": app_id,
                                        "route": route,
                                        "code": "proxy_upstream_unavailable",
                                    }
                                )
                except RegistryConfigurationError as exc:
                    proxy_upstream_errors.append(
                        {"app_id": app_id, "code": exc.code}
                    )

        ner = probe_ner_runtime(required=ner_required)
        ready = (
            (not ner_required or bool(ner["loadable"]))
            and not dictionary_errors
            and not proxy_upstream_errors
        )
        payload = {
            "status": "ready" if ready else "not_ready",
            "version": __version__,
            "detectors": {"ner": ner},
            "apps": {
                "configured": len(registry.known_apps()),
                "dictionary_errors": dictionary_errors,
                "proxy_upstream_errors": proxy_upstream_errors,
            },
        }
        return JSONResponse(payload, status_code=200 if ready else 503)

    @app.post("/redact")
    def redact(payload: dict = Body(default={})) -> dict:
        text = payload.get("text") or ""
        if not text.strip():
            raise HTTPException(status_code=400, detail="text 不能为空")
        if (limit := text_over_limit(text)) is not None:
            raise HTTPException(status_code=413, detail=f"text 超过 {limit} 字符上限")
        if reserved_placeholder_conflict(text):
            raise HTTPException(
                status_code=409, detail="input contains reserved placeholder syntax"
            )
        if payload.get("task_id") is not None:
            # Client-chosen task_ids allowed cross-tenant mapping OVERWRITE (and
            # trivially-guessable refill keys like "doc1"). The server always
            # mints the id; callers use the one in the response.
            raise HTTPException(
                status_code=400,
                detail="不再接受客户端 task_id；请使用响应中返回的 task_id",
            )
        profile, entries = _resolve(payload)
        run = run_detection(text, build_detectors(entries), profile)
        task_id = f"task_{uuid.uuid4().hex[:12]}"
        result = redact_text(text, run.kept, task_id=task_id)
        store.save(result.task_id, result.mapping)
        audit = build_audit_event(result.task_id, run.kept, result.mapping)
        return result.to_safe_dict() | {
            "blocked_labels": run.blocked_labels,
            "profile": profile.name,
            "audit": audit.to_safe_dict(),
            "detectors": run.readiness.to_safe_dict(),
            "policy": policy_compatibility_metadata(profile),
        }

    @app.post("/refill")
    def refill(payload: dict = Body(default={})) -> dict:
        text = payload.get("text")
        task_id = payload.get("task_id")
        if text is None or not task_id:
            raise HTTPException(status_code=400, detail="需要 text 和 task_id")
        try:
            mapping = store.load(task_id)
        except FileNotFoundError:
            raise HTTPException(status_code=404, detail="mapping 不存在（task_id 错误或已过期）")
        return refill_text(text, mapping).to_safe_dict()

    @app.post("/redact_snippet")
    def redact_snippet(payload: dict = Body(default={})) -> dict:
        text = payload.get("text") or ""
        if not text:
            return {"masked": text}
        if (limit := text_over_limit(text)) is not None:
            raise HTTPException(status_code=413, detail=f"text 超过 {limit} 字符上限")
        if reserved_placeholder_conflict(text):
            raise HTTPException(
                status_code=409, detail="input contains reserved placeholder syntax"
            )
        profile, entries = _resolve(payload)
        run = run_detection(text, build_detectors(entries), profile)
        result = redact_text(text, run.kept)  # in-memory only; nothing is persisted here
        return {
            "masked": result.redacted_text,
            "risk_summary": result.risk_summary,
            "blocked_labels": run.blocked_labels,
            "profile": profile.name,
            "detectors": run.readiness.to_safe_dict(),
            "policy": policy_compatibility_metadata(profile),
        }

    @app.post("/session/open")
    def session_open(payload: dict = Body(default={})) -> dict:
        profile, entries = _resolve(payload)
        sid = f"sess_{uuid.uuid4().hex[:16]}"
        sessions[sid] = {
            "redactor": SessionRedactor(build_detectors(entries), profile),
            "ts": time.time(),
        }
        return {
            "session_id": sid,
            "profile": profile.name,
            "policy": policy_compatibility_metadata(profile),
        }

    @app.post("/session/{sid}/mask")
    def session_mask(sid: str, payload: dict = Body(default={})) -> dict:
        redactor = _get_session(sid)
        text = payload.get("text") or ""
        if (limit := text_over_limit(text)) is not None:
            raise HTTPException(status_code=413, detail=f"text 超过 {limit} 字符上限")
        if reserved_placeholder_conflict(text):
            raise HTTPException(
                status_code=409, detail="input contains reserved placeholder syntax"
            )
        masked = redactor.mask(text)
        response = {"masked": masked, "blocked_labels": sorted(redactor.blocked_labels)}
        if redactor.last_readiness is not None:
            response["detectors"] = redactor.last_readiness.to_safe_dict()
        return response

    @app.post("/session/{sid}/refill")
    def session_refill(sid: str, payload: dict = Body(default={})) -> dict:
        redactor = _get_session(sid)
        return redactor.refill(payload.get("text") or "")

    @app.post("/session/{sid}/unmask")
    def session_unmask(sid: str, payload: dict = Body(default={})) -> dict:
        redactor = _get_session(sid)
        return {"text": redactor.unmask(payload.get("text") or "")}

    @app.delete("/session/{sid}")
    def session_close(sid: str) -> dict:
        existed = sessions.pop(sid, None) is not None
        return {"closed": existed}

    @app.get("/profiles")
    def profiles() -> dict:
        return {
            "profiles": registry.known_profiles(),
            "apps": registry.known_apps(),
            "policy_schema_version": POLICY_SCHEMA_VERSION,
            "profile_compatibility": PROFILE_COMPATIBILITY,
        }

    @app.post("/dict/reload")
    def dict_reload() -> dict:
        registry.invalidate_dictionary()
        return {"reloaded": True}

    # --- 离线授权端点（admin token 保护，见 _admin_guard）---
    def _license_status_payload() -> dict:
        delivery = app.state.delivery_info
        payload_out: dict = {
            "enforcement": "enabled" if license_required else "disabled",
            "delivery_customer": (delivery or {}).get("customer"),
        }
        payload = license_store.load()
        payload_out["customer"] = payload.customer if payload else None
        payload_out["license_id"] = payload.license_id if payload else None
        payload_out["expires_at"] = payload.expires_at.isoformat() if payload else None
        if license_required:
            state, _, effective_now = _license_snapshot()
            payload_out["state"] = state.value
            payload_out["grace_days"] = licensing.GRACE_DAYS
            if payload is not None:
                remaining = (payload.expires_at - effective_now).days
                payload_out["days_left"] = max(remaining, 0)
        return payload_out

    @app.get("/api/v1/license/status")
    def license_status() -> dict:
        return _license_status_payload()

    @app.post("/api/v1/license/activate")
    def license_activate(payload: dict = Body(...)) -> JSONResponse:
        code = str(payload.get("code", ""))
        if not code.strip():
            return JSONResponse(
                {"error": {"code": "license_invalid", "message": "授权码不能为空"}},
                status_code=400,
            )
        try:
            license_store.activate(code)
        except licensing.LicenseError as exc:
            return JSONResponse(
                {"error": {"code": "license_invalid", "message": str(exc)}},
                status_code=400,
            )
        return JSONResponse(_license_status_payload())

    # Capability-protected, versioned product API. Legacy endpoints above stay
    # available during migration but do not act as authorization credentials.
    from pathlib import Path

    from tuomin_gateway.inspection import InspectionVault
    from tuomin_gateway.service.v1 import register_v1_api

    inspection_vault = InspectionVault(
        store.child(
            "inspections",
            ttl_seconds=int(
                os.environ.get(
                    "TUOMIN_INSPECTION_TTL", str(180 * 24 * 3600)
                )
            ),
        )
    )

    vault = register_v1_api(
        app, registry, store, inspection_vault=inspection_vault
    )

    # One durable audit log shared by every security-event writer (guard
    # alerts, trusted refill, namespace lifecycle, admin mutations).
    from tuomin_gateway.audit import AuditLog

    audit_log = AuditLog(Path(store.directory) / "audit") if store is not None else None

    # Local file workbench: document parse/redact, recovery packages, refill.
    from tuomin_gateway.jobs import JobStore
    from tuomin_gateway.service.v1_documents import register_documents_api

    job_store = JobStore(store.directory)
    app.state.job_store = job_store
    register_documents_api(app, store, vault, audit_log, job_store, registry)

    # Pillar 1: unified reverse proxy (/v1/messages, /v1/chat/completions).
    from tuomin_gateway.service.proxy import register_proxy

    register_proxy(
        app,
        registry,
        store=store,
        audit_log=audit_log,
        inspection_vault=inspection_vault,
        job_store=job_store,
    )

    # Pillar 4: admin CRUD API + local WebUI (build-free static page at /ui).
    from tuomin_gateway.service.admin import register_admin

    register_admin(app, registry, store, session_ttl, audit_log=audit_log)
    static_dir = Path(__file__).parent / "static"
    if static_dir.exists():
        from fastapi.staticfiles import StaticFiles

        app.mount("/ui", StaticFiles(directory=str(static_dir), html=True), name="ui")

    return app
