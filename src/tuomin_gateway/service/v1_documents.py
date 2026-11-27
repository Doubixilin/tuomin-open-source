"""Local file workbench endpoints: parse/redact, package export, refill."""
from __future__ import annotations

import base64
from collections import Counter
import hashlib
import io
import os
import re
from pathlib import PurePath
import secrets
import tempfile
import time
import zipfile
from typing import Any

from fastapi import Body, FastAPI, Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from tuomin_gateway import __version__, guard
from tuomin_gateway.audit import AUDIT_STREAM_REFILL, AuditLog
from tuomin_gateway.detectors.base import hash_text
from tuomin_gateway.document.docx_patch import (
    DocxUnsupportedError,
    extract_paragraph_texts,
    probe_docx,
    redact_docx,
    refill_docx,
)
from tuomin_gateway.document.extract import UnsupportedFormatError, extract_document
from tuomin_gateway.document.legacy_convert import LEGACY_TARGETS, LegacyConversionError, convert_legacy
from tuomin_gateway.document.office_patch import (
    OfficeUnsupportedError,
    extract_office_texts,
    probe_office,
    redact_office,
    refill_office,
)
from tuomin_gateway.document.markdown import render_clean_markdown
from tuomin_gateway.detectors.table_context import TableCell, TableHeaderRuleDetector, markdown_table_cells
from tuomin_gateway.document.refill_flow import MODE_EDITED, placeholder_diff, refill_answer, refill_markdown
from tuomin_gateway.jobs import JobFieldError, JobStore
from tuomin_gateway.mapping import PlaceholderFactory
from tuomin_gateway.mapproc import PackageError, export_manifest, export_package, import_package
from tuomin_gateway.placeholders import PLACEHOLDER_RE, reserved_placeholder_conflict
from tuomin_gateway.policy import POLICY_SCHEMA_VERSION
from tuomin_gateway.profiles import RULES_WORKBENCH, get_profile, is_fully_reversible
from tuomin_gateway.redactor import normalize_detections, redact_text
from tuomin_gateway.schemas import MappingEntry
from tuomin_gateway.service.registry import AppRegistry, RegistryConfigurationError
from tuomin_gateway.service.v1 import _CLIPBOARD_PACKAGE_SOURCE, _error
from tuomin_gateway.session import build_detectors, run_detection
from tuomin_gateway.store import MappingStore
from tuomin_gateway.vault import MappingGrantMismatch, MappingVault

_APP_ID = "workbench"
_PROFILE = "file_workbench"
_DEFAULT_MAX_BYTES = 20 * 1024 * 1024
_AUDIT_STREAM_WORKBENCH = "workbench"
_EGRESS_DISCLAIMER = (
    "egress_allowed 仅表示当前配置与扫描通过，不构成零漏检承诺"
)


def _file_profile(registry: AppRegistry, bound_app: str | None):
    entry = (registry.get_app(bound_app) or {}) if bound_app else {}
    return get_profile(RULES_WORKBENCH if entry.get("profile") == RULES_WORKBENCH else _PROFILE)


def _max_bytes() -> int:
    try:
        value = int(os.environ.get("TUOMIN_WORKBENCH_MAX_BYTES", "") or _DEFAULT_MAX_BYTES)
    except ValueError:
        return _DEFAULT_MAX_BYTES
    return value if value > 0 else _DEFAULT_MAX_BYTES


def _versions() -> dict[str, str]:
    try:
        from tuomin_gateway.audit import _default_versions

        return _default_versions()
    except ImportError:
        return {"gateway": __version__}


def _safe_warnings(warnings: list[dict[str, Any]]) -> list[dict[str, str]]:
    return [
        {"code": code, "message": message}
        for code, message in dict.fromkeys(
            (str(warning.get("code", "document_warning")), str(warning.get("message", "")))
            for warning in warnings
        )
    ]


def _job(job_store: JobStore, task_id: str) -> dict | None:
    try:
        return job_store.get_job(task_id)
    except JobFieldError:
        return None


def _evaluate_egress(
    *,
    bound_app: str | None,
    dictionary_version: str | None,
    run: Any,
    redacted_text: str,
    profile: Any,
    versions: dict[str, str],
    extra_reasons: list[dict[str, str]] | None = None,
) -> dict[str, Any]:
    """Egress decision: conservative, fail-closed. A degraded OPTIONAL detector
    also blocks egress (but never the local preview) — the gate certifies
    confidence, so lower recall must not pass."""
    readiness = run.readiness.to_safe_dict()
    missing_required = sorted(set(readiness["required"]) - set(readiness["active"]))
    residual = guard.scan_output(redacted_text, profile)
    residual_status = (
        "block" if residual.blocking else ("alert" if residual.events else "clean")
    )
    reasons: list[dict[str, str]] = []
    if bound_app is None:
        reasons.append({
            "code": "app_not_bound",
            "message": "未绑定应用与生产词典，脱敏结果仅限本地预览",
        })
    if missing_required or readiness["degraded"]:
        reasons.append({
            "code": "detector_not_ready",
            "message": "检测器未全部就绪: " + ", ".join(
                missing_required
                or [str(e["detector"]) for e in readiness["errors"]]
            ),
        })
    if run.blocked_labels:
        reasons.append({
            "code": "blocked_labels",
            "message": "存在阻断标签: " + ", ".join(sorted(run.blocked_labels)),
        })
    if residual.blocking:
        reasons.append({
            "code": "residual_scan_block",
            "message": "脱敏结果残留扫描发现阻断级风险",
        })
    elif residual.events:
        reasons.append({
            "code": "residual_scan_alert",
            "message": "脱敏结果残留扫描发现告警级风险",
        })
    reasons.extend(extra_reasons or [])
    return {
        "egress_allowed": not reasons,
        "reasons": reasons,
        "detectors": readiness,
        "blocked_labels": sorted(run.blocked_labels),
        "residual_scan": {
            "status": residual_status,
            "alert_count": len(residual.events),
        },
        "dictionary": {"bound": bound_app is not None, "version": dictionary_version},
        "versions": versions,
        "disclaimer": _EGRESS_DISCLAIMER,
    }


# Read-only Office structures cannot be rewritten. A hit there blocks the export
# only when it is a genuine sensitive value: a rule/dictionary hit inside a
# formula's quoted string literal, a sheet name, link target or attribute.
# Model guesses on structural text, arithmetic inside formulas and low-risk
# labels (dates, amounts, public regions) are ignored with a warning instead.
_STRUCTURE_LABELS = {
    "formula": "公式", "sheet_name": "工作表名", "defined_name": "定义名称", "hyperlink": "超链接地址",
    "chart_cache": "图表数值缓存", "metadata": "文档属性", "text": "文本",
}
_STRUCTURAL_IGNORED_LABELS = {"DATE", "AMOUNT", "DISCOUNT_RATE", "PUBLIC_REGION", "PUBLIC_AUTHORITY"}
_FORMULA_STRING_LITERAL = re.compile(r'"(?:[^"]|"")*"')


def _describe_structure(kind: str) -> str:
    if kind.startswith("attribute:"):
        return f"属性 {kind[len('attribute:'):]}"
    return _STRUCTURE_LABELS.get(kind, kind)


def _structural_hit(span: Any, owner: Any, start: int, end: int) -> bool:
    """True when a hit in a read-only unit is structural text, not a leak."""
    if span.source == "model" or span.label in _STRUCTURAL_IGNORED_LABELS:
        return True
    if getattr(owner, "kind", "text") == "formula":
        inside_literal = any(m.start() <= start and end <= m.end() for m in _FORMULA_STRING_LITERAL.finditer(owner.text))
        return not inside_literal
    return False


def register_documents_api(
    app: FastAPI,
    store: MappingStore,
    vault: MappingVault,
    audit_log: AuditLog,
    job_store: JobStore,
    registry: AppRegistry,
) -> None:
    @app.delete("/api/v1/documents/jobs/{task_id}")
    def delete_document_job(task_id: str) -> Any:
        """Delete a workbench job and cascade to its stored mapping grant.

        The call-ledger rows are kept on purpose: they carry counts/versions
        only (no values) and are the durable record that the job existed.
        """
        try:
            job = job_store.delete_job(task_id)
        except JobFieldError:
            job = None
        if job is None:
            return _error("job_not_found", "document job not found", 404)
        mapping_ref = job.get("mapping_ref")
        if isinstance(mapping_ref, str) and mapping_ref:
            try:
                store.delete(f"mapping-grant:{mapping_ref}")
            except OSError:
                pass
        try:
            audit_log.write(
                _AUDIT_STREAM_WORKBENCH,
                {"event": "job_delete", "task_id": task_id, "app_id": _APP_ID},
            )
        except OSError:
            pass
        return {"status": "ok", "deleted": task_id}

    @app.get("/api/v1/documents/jobs")
    def list_document_jobs(limit: int = 50, offset: int = 0) -> Any:
        try:
            jobs = job_store.list_jobs(limit=limit, offset=offset)
        except JobFieldError:
            return _error("invalid_query", "limit/offset must be non-negative integers", 400)
        return {"status": "ok", "jobs": jobs}

    @app.get("/api/v1/documents/calls")
    def list_workbench_calls(limit: int = 100, entry: str | None = None) -> Any:
        try:
            calls = job_store.list_calls(limit=limit, entry=entry)
        except JobFieldError as exc:
            return _error("invalid_query", str(exc), 400)
        return {"status": "ok", "calls": calls}

    @app.post("/api/v1/documents/parse")
    async def parse_document(
        request: Request, filename: str | None = None, app_id: str | None = None
    ) -> Any:
        # Detection and extraction are CPU-heavy and synchronous. Running them
        # on the event loop froze the whole gateway for the duration of a
        # large file (cancel, quit pre-check, clipboard, agents all queued).
        body = await request.body()
        return await run_in_threadpool(_parse_document, body, filename, app_id)

    def _parse_document(body: bytes, filename: str | None, app_id: str | None) -> Any:
        started = time.monotonic()
        if not isinstance(filename, str) or not filename.strip():
            return _error("filename_required", "filename is required", 400)
        file_name = PurePath(filename).name
        if file_name in {"", ".", ".."}:
            return _error("filename_invalid", "filename must contain a bare file name", 400)
        if len(body) > _max_bytes():
            return _error("document_too_large", "document exceeds the workbench byte limit", 413)

        job_id = "job-" + secrets.token_urlsafe(12)
        try:
            with tempfile.TemporaryDirectory(prefix="tuomin-workbench-") as temporary:
                path = PurePath(temporary) / file_name
                with open(path, "wb") as handle:
                    handle.write(body)
                extracted = extract_document(path, job_id)
        except UnsupportedFormatError as exc:
            return _error("unsupported_format", str(exc), 415)
        except ValueError as exc:
            return _error("document_extraction_failed", str(exc), 422)

        rendered = render_clean_markdown(extracted.blocks)
        text = rendered.text
        if not text.strip():
            return _error("empty_document", "document contains no renderable text", 422)
        if reserved_placeholder_conflict(text):
            return _error(
                "reserved_placeholder_conflict",
                "input contains reserved placeholder syntax",
                409,
            )

        # Egress gate (plan 阶段 8 §9.1): the workbench may bind a registered
        # app to use its production dictionary; unbound parses stay preview-only
        # (egress blocked with app_not_bound). Unknown apps fail closed — never
        # fall back to an empty dictionary silently.
        bound_app: str | None = None
        dictionary_version: str | None = None
        entries: list[dict[str, Any]] = []
        if app_id is not None:
            bound = app_id.strip()
            if not bound:
                return _error("invalid_query", "app_id must be non-empty", 400)
            if bound not in registry.known_apps():
                return _error("unknown_app", "unknown app in workbench binding", 404)
            try:
                dict_state = registry.resolve_dictionary_state(bound)
            except RegistryConfigurationError as exc:
                return _error(exc.code, str(exc), 503)
            bound_app = bound
            entries = dict_state.entries or []
            dictionary_version = dict_state.version
        effective_app = bound_app or _APP_ID

        profile = _file_profile(registry, bound_app)
        if not is_fully_reversible(profile):
            return _error(
                "profile_not_reversible",
                "file workbench profile is not fully reversible",
                500,
            )

        detectors = build_detectors(entries=entries)
        table_cells = markdown_table_cells(text)
        if table_cells:
            detectors.append(TableHeaderRuleDetector(table_cells))
        run = run_detection(text, detectors, profile)
        result = redact_text(text, run.kept, task_id=job_id)
        task_id = result.task_id or job_id
        redacted_markdown = result.redacted_text
        expected_counts = dict(Counter(PLACEHOLDER_RE.findall(redacted_markdown)))
        mapping_ref = vault.create(
            app_id=effective_app,
            scope="document",
            entries=result.mapping,
            expected_counts=expected_counts,
        )
        versions = _versions()
        redacted_digest = hashlib.sha256(redacted_markdown.encode("utf-8")).hexdigest()
        label_counts = dict(sorted(Counter(entry.label for entry in result.mapping).items()))

        egress = _evaluate_egress(
            bound_app=bound_app,
            dictionary_version=dictionary_version,
            run=run,
            redacted_text=redacted_markdown,
            profile=profile,
            versions=versions,
        )

        try:
            job_store.create_job(
                task_id=task_id,
                file_name=file_name,
                file_sha256=extracted.file_sha256,
                file_format=extracted.file_format,
                profile=profile.name,
                versions=versions,
                mapping_ref=mapping_ref,
                app_id=effective_app,
            )
            job_store.complete_job(
                task_id,
                label_counts=label_counts,
                redacted_sha256=redacted_digest,
                artifacts=[],
            )
            job_store.record_call(
                entry="workbench",
                app_id=effective_app,
                task_id=task_id,
                duration_ms=max(0, int((time.monotonic() - started) * 1000)),
                char_count=len(redacted_markdown),
                label_counts=label_counts,
                blocked=bool(run.blocked_labels),
                policy_version=POLICY_SCHEMA_VERSION,
            )
        except (OSError, JobFieldError):
            pass

        return {
            "status": "ok",
            "task_id": task_id,
            "file_name": file_name,
            "file_format": extracted.file_format,
            "redacted_markdown": redacted_markdown,
            "redacted_sha256": redacted_digest,
            "manifest": export_manifest(
                result.mapping,
                job_id=task_id,
                app_id=_APP_ID,
                profile=profile.name,
                versions=versions,
            ),
            "warnings": _safe_warnings(extracted.warnings),
            "label_counts": label_counts,
            "egress": egress,
        }

    @app.post("/api/v1/documents/{task_id}/package")
    def package_document(task_id: str, payload: dict = Body(default={})) -> Any:
        job = _job(job_store, task_id)
        if job is None:
            return _error("job_not_found", "document job not found", 404)
        if job.get("status") != "completed":
            return _error("job_not_completed", "document job is not completed", 409)
        passphrase = payload.get("passphrase")
        if not isinstance(passphrase, str) or not passphrase:
            return _error("passphrase_required", "passphrase must be non-empty", 400)
        mapping_ref = job.get("mapping_ref")
        if not isinstance(mapping_ref, str) or not mapping_ref:
            return _error("mapping_unavailable", "document mapping is unavailable", 404)
        # Jobs created before app binding existed carry no app_id; fall back to
        # the historical workbench identity so their packages stay refillable.
        job_app = job.get("app_id") or _APP_ID
        try:
            grant = vault.load(mapping_ref, app_id=job_app)
        except (FileNotFoundError, MappingGrantMismatch, ValueError, KeyError):
            return _error("mapping_unavailable", "document mapping is unavailable", 404)
        package = export_package(
            grant.entries,
            passphrase=passphrase,
            job_id=task_id,
            app_id=job_app,
            profile=job["profile"],
            versions=job["versions_json"],
            source_sha256=job["file_sha256"],
            redacted_sha256=job["redacted_sha256"],
            expected_counts=grant.expected_counts,
            docx_ledger=job.get("docx_ledger_json"),
        )
        try:
            audit_log.write(
                _AUDIT_STREAM_WORKBENCH,
                {"event": "package_export", "task_id": task_id, "app_id": job_app},
            )
        except OSError:
            pass
        return {"status": "ok", "package": package}

    # --- shared refill tails (package-based and task-based paths) ----------------
    def _finish_refill_markdown(started, task_id, job, job_app, entries, expected_counts, redacted_markdown, confirm_edited):
        result = refill_markdown(
            redacted_markdown,
            entries,
            expected_counts,
            recorded_redacted_sha256=job["redacted_sha256"] if job else None,
            confirm_edited=confirm_edited,
        )
        safe_diff = result.diff.to_safe_dict()
        if result.status == "blocked":
            return JSONResponse(
                {"status": "blocked", "error_types": result.error_types, "diff": safe_diff},
                status_code=422,
            )
        if result.status == "needs_confirmation" or (
            result.mode == MODE_EDITED and not confirm_edited
        ):
            return {"status": "needs_confirmation", "mode": result.mode, "diff": safe_diff}

        restored_count = sum(
            redacted_markdown.count(entry.placeholder) for entry in entries
        )
        label_counts = dict(sorted(Counter(entry.label for entry in entries).items()))
        try:
            audit_log.write(
                AUDIT_STREAM_REFILL,
                {
                    "event": "trusted_refill",
                    "app_id": job_app,
                    "task_id": task_id,
                    "contract": f"workbench:{result.mode}",
                    "status": "ok",
                    "error_types": [],
                    "restored_count": restored_count,
                    "raw_values_included": False,
                },
            )
        except OSError:
            pass
        try:
            job_store.record_call(
                entry="workbench",
                app_id=job_app,
                task_id=task_id,
                duration_ms=max(0, int((time.monotonic() - started) * 1000)),
                char_count=len(redacted_markdown),
                label_counts=label_counts,
                blocked=False,
                policy_version=POLICY_SCHEMA_VERSION,
            )
        except (OSError, JobFieldError):
            pass
        return {"status": "ok", "mode": result.mode, "restored_markdown": result.text}

    def _finish_refill_answer(started, task_id, job_app, entries, answer_markdown, confirm_empty):
        result = refill_answer(
            answer_markdown, entries, confirm_empty=confirm_empty
        )
        summary = _answer_summary(answer_markdown, entries, result.diff)
        if result.status == "blocked":
            return JSONResponse(
                {
                    "status": "blocked",
                    "error_types": result.error_types,
                    "diff": result.diff.to_safe_dict(),
                },
                status_code=422,
            )
        if result.status == "needs_confirmation":
            return {
                "status": "needs_confirmation",
                "mode": result.mode,
                "reason": "no_placeholder_hit",
                "summary": summary,
            }

        try:
            audit_log.write(
                AUDIT_STREAM_REFILL,
                {
                    "event": "trusted_refill",
                    "app_id": job_app,
                    "task_id": task_id,
                    "contract": "workbench:answer",
                    "status": "ok",
                    "error_types": [],
                    "restored_count": summary["restored_count"],
                    "raw_values_included": False,
                },
            )
        except OSError:
            pass
        try:
            job_store.record_call(
                entry="workbench",
                app_id=job_app,
                task_id=task_id,
                duration_ms=max(0, int((time.monotonic() - started) * 1000)),
                char_count=len(answer_markdown),
                label_counts={},
                blocked=False,
                policy_version=POLICY_SCHEMA_VERSION,
            )
        except (OSError, JobFieldError):
            pass
        return {
            "status": "ok",
            "mode": result.mode,
            "restored_markdown": result.text,
            "summary": summary,
        }

    def _finish_refill_office(started, task_id, job, job_app, entries, ledger, recorded_redacted_sha256, expected_counts, data, file_format, confirm_edited):
        if file_format != "docx" and (
            not ledger or ledger.get("format") != file_format + "-ledger-v1"
        ):
            return _error("package_rejected", "recovery package does not match the Office format", 403)
        values = {entry.placeholder: entry.original_value for entry in entries}
        try:
            result = refill_docx(data, values) if file_format == "docx" else refill_office(data, values, file_format)
        except (DocxUnsupportedError, OfficeUnsupportedError) as exc:
            return _error(exc.code, str(exc), 422)
        except ValueError as exc:
            return _error(file_format + "_refill_failed", str(exc), 422)
        if result["status"] == "blocked":
            return JSONResponse(
                {
                    "status": "blocked",
                    "error_types": result["error_types"],
                    "diff": result["diff"],
                },
                status_code=422,
            )

        recorded_digest = (job or {}).get("redacted_sha256") if file_format == "docx" else recorded_redacted_sha256
        mode = "exact" if hashlib.sha256(data).hexdigest() == recorded_digest else "edited"
        if file_format != "docx" and mode == "edited" and not confirm_edited:
            text = "\n\n".join(unit.text for unit in extract_office_texts(data, file_format))
            diff = placeholder_diff(text, entries, expected_counts or {})
            return {
                "status": "needs_confirmation", "mode": mode,
                "diff": diff.to_safe_dict(),
                "message": "文件已编辑或与恢复包不对应；请确认属于同一任务，错误恢复包可能还原成错误原值",
            }
        try:
            audit_log.write(
                AUDIT_STREAM_REFILL,
                {
                    "event": "trusted_refill",
                    "app_id": job_app,
                    "task_id": task_id,
                    "contract": f"workbench:{file_format}:{mode}",
                    "status": "ok",
                    "error_types": [],
                    "restored_count": result["restored_count"],
                    "raw_values_included": False,
                },
            )
        except OSError:
            pass
        try:
            job_store.record_call(
                entry="workbench",
                app_id=job_app,
                task_id=task_id,
                duration_ms=max(0, int((time.monotonic() - started) * 1000)),
                char_count=len(data),
                label_counts={},
                blocked=False,
                policy_version=POLICY_SCHEMA_VERSION,
            )
        except (OSError, JobFieldError):
            pass
        return {
            "status": "ok",
            "mode": mode,
            "restored_count": result["restored_count"],
            ("restored_docx_b64" if file_format == "docx" else "restored_office_b64"):
                base64.b64encode(result["data"]).decode("ascii"),
        }

    # --- task-based refill: the local trusted UI needs no passphrase or package ----
    def _task_grant(task_id: str):
        job = _job(job_store, task_id)
        if job is None:
            return None, None, None, _error("job_not_found", "document job not found", 404)
        if job.get("status") != "completed":
            return None, None, None, _error("job_not_completed", "document job is not completed", 409)
        mapping_ref = job.get("mapping_ref")
        job_app = job.get("app_id") or _APP_ID
        if not isinstance(mapping_ref, str) or not mapping_ref:
            return None, None, None, _error("mapping_unavailable", "该任务的映射已不在本机；如有恢复包可改用恢复包回填", 404)
        try:
            grant = vault.load(mapping_ref, app_id=job_app)
        except (FileNotFoundError, MappingGrantMismatch, ValueError, KeyError):
            return None, None, None, _error("mapping_unavailable", "该任务的映射已不在本机（可能已过期或被删除）；如有恢复包可改用恢复包回填", 404)
        return job, job_app, grant, None

    @app.post("/api/v1/documents/{task_id}/refill")
    def refill_document_by_task(task_id: str, payload: dict = Body(default={})) -> Any:
        started = time.monotonic()
        redacted_markdown = payload.get("redacted_markdown")
        confirm_edited = payload.get("confirm_edited", False)
        if not isinstance(redacted_markdown, str):
            return _error("invalid_request", "redacted_markdown must be a string", 400)
        if len(redacted_markdown.encode("utf-8")) > _max_bytes():
            return _error("document_too_large", "redacted_markdown exceeds the workbench byte limit", 413)
        if not isinstance(confirm_edited, bool):
            return _error("invalid_request", "confirm_edited must be a boolean", 400)
        job, job_app, grant, error = _task_grant(task_id)
        if error is not None:
            return error
        return _finish_refill_markdown(started, task_id, job, job_app, grant.entries, grant.expected_counts, redacted_markdown, confirm_edited)

    @app.post("/api/v1/documents/{task_id}/refill-answer")
    def refill_answer_by_task(task_id: str, payload: dict = Body(default={})) -> Any:
        started = time.monotonic()
        answer_markdown = payload.get("answer_markdown")
        confirm_empty = payload.get("confirm_empty", False)
        if not isinstance(answer_markdown, str):
            return _error("invalid_request", "answer_markdown must be a string", 400)
        if len(answer_markdown.encode("utf-8")) > _max_bytes():
            return _error("document_too_large", "answer exceeds the workbench byte limit", 413)
        if not isinstance(confirm_empty, bool):
            return _error("invalid_request", "confirm_empty must be a boolean", 400)
        job, job_app, grant, error = _task_grant(task_id)
        if error is not None:
            return error
        return _finish_refill_answer(started, task_id, job_app, grant.entries, answer_markdown, confirm_empty)

    # --- agent sessions: refill an AI tool's answer in the trusted UI ---------
    # MCP clients redact inside one namespace per session (consistent
    # placeholders across calls). Agents hold only redact/namespace
    # capabilities; restoring happens here, behind the admin guard.
    def _agent_namespace(namespace_id: str):
        from tuomin_gateway.agent_setup import AGENT_APP_ID
        from tuomin_gateway.namespace_vault import NamespaceMismatch

        namespace_vault = getattr(app.state, "namespace_vault", None)
        if namespace_vault is None or not isinstance(namespace_id, str) or not namespace_id.startswith("ns_"):
            return None, _error("namespace_not_found", "智能体会话不存在或已过期", 404)
        try:
            return namespace_vault.load(namespace_id, app_id=AGENT_APP_ID), None
        except (FileNotFoundError, NamespaceMismatch, ValueError, KeyError):
            return None, _error("namespace_not_found", "智能体会话不存在或已过期", 404)

    @app.get("/api/v1/documents/dictionary-candidates")
    def dictionary_candidates(task_id: str, app_id: str) -> Any:
        """Entities detected in a task / agent session / clipboard session that
        the target dictionary does not cover yet (从脱敏结果学习)."""
        from tuomin_gateway.dictionary_import import candidates

        if registry.get_app(app_id) is None:
            return _error("unknown_app", "目标词典所属应用不存在", 404)
        if task_id.startswith("agent:"):
            state, error = _agent_namespace(task_id[6:])
            if error is not None:
                return error
            entries = state.entries
        elif task_id.startswith("clipboard:"):
            from tuomin_gateway.namespace_vault import NamespaceMismatch

            try:
                entries = app.state.namespace_vault.load(task_id[10:], app_id="clipboard").entries
            except (AttributeError, FileNotFoundError, NamespaceMismatch, ValueError, KeyError):
                return _error("namespace_not_found", "剪贴板会话不存在或已过期", 404)
        else:
            _job, _job_app, grant, error = _task_grant(task_id)
            if error is not None:
                return error
            entries = grant.entries
        found = candidates(entries, registry.load_entries(app_id))
        try:
            audit_log.write(_AUDIT_STREAM_WORKBENCH, {"event": "dictionary_candidates", "task_id": task_id.split(":")[0] if ":" in task_id else task_id, "app_id": app_id, "candidate_count": len(found)})
        except OSError:
            pass
        return {"status": "ok", "app_id": app_id, "candidates": found}

    @app.get("/api/v1/documents/agent-sessions")
    def agent_sessions(limit: int = 20) -> Any:
        """Recent agent namespaces from the counts-only ledger, newest first."""
        limit = max(1, min(int(limit), 100))
        try:
            calls = job_store.list_calls(limit=1000, entry="agent")
        except (OSError, JobFieldError):
            calls = []
        sessions: dict[str, dict] = {}
        for call in calls:
            namespace_id = call.get("task_id")
            if not isinstance(namespace_id, str) or not namespace_id:
                continue
            row = sessions.setdefault(namespace_id, {"namespace_id": namespace_id, "last_ts": call.get("ts", 0), "first_ts": call.get("ts", 0), "calls": 0, "label_counts": {}})
            row["calls"] += 1
            row["first_ts"] = min(row["first_ts"], call.get("ts", 0))
            for label, count in (call.get("label_counts_json") or {}).items():
                if isinstance(count, int):
                    row["label_counts"][label] = row["label_counts"].get(label, 0) + count
        result = []
        for row in sorted(sessions.values(), key=lambda item: item["last_ts"], reverse=True):
            state, error = _agent_namespace(row["namespace_id"])
            if error is not None:
                continue
            row["placeholder_count"] = len(state.entries)
            row["namespace_status"] = state.status
            result.append(row)
            if len(result) >= limit:
                break
        return {"status": "ok", "sessions": result}

    @app.post("/api/v1/documents/agent-sessions/{namespace_id}/mapping")
    def agent_session_mapping(namespace_id: str, payload: dict = Body(default={})) -> Any:
        state, error = _agent_namespace(namespace_id)
        if error is not None:
            return error
        try:
            audit_log.write(_AUDIT_STREAM_WORKBENCH, {"event": "mapping_unlock", "task_id": namespace_id, "app_id": "agent", "entry_count": len(state.entries), "source": "agent_session"})
        except OSError:
            pass
        return {
            "status": "ok", "app_id": "agent", "task_id": namespace_id,
            "entries": [{"placeholder": e.placeholder, "label": e.label, "original_value": e.original_value} for e in state.entries],
        }

    @app.post("/api/v1/documents/agent-sessions/{namespace_id}/refill-answer")
    def agent_session_refill_answer(namespace_id: str, payload: dict = Body(default={})) -> Any:
        started = time.monotonic()
        answer_markdown = payload.get("answer_markdown")
        confirm_empty = payload.get("confirm_empty", False)
        if not isinstance(answer_markdown, str):
            return _error("invalid_request", "answer_markdown must be a string", 400)
        if len(answer_markdown.encode("utf-8")) > _max_bytes():
            return _error("document_too_large", "answer exceeds the workbench byte limit", 413)
        if not isinstance(confirm_empty, bool):
            return _error("invalid_request", "confirm_empty must be a boolean", 400)
        state, error = _agent_namespace(namespace_id)
        if error is not None:
            return error
        return _finish_refill_answer(started, namespace_id, "agent", list(state.entries), answer_markdown, confirm_empty)

    @app.post("/api/v1/documents/{task_id}/refill-office")
    def refill_office_by_task(task_id: str, payload: dict = Body(default={})) -> Any:
        started = time.monotonic()
        confirm_edited = payload.get("confirm_edited", False)
        if not isinstance(confirm_edited, bool):
            return _error("invalid_request", "confirm_edited must be a boolean", 400)
        job, job_app, grant, error = _task_grant(task_id)
        if error is not None:
            return error
        file_format = payload.get("file_format") or job.get("file_format")
        if not isinstance(file_format, str) or file_format not in {"docx", "xlsx", "pptx"}:
            return _error("unsupported_format", "task refill supports docx, xlsx and pptx", 415)
        if job.get("file_format") != file_format:
            return _error("format_mismatch", "所选任务不是这种格式的文件", 409)
        redacted_b64 = payload.get("redacted_docx_b64" if file_format == "docx" else "redacted_office_b64")
        if redacted_b64 is None:
            redacted_b64 = payload.get("redacted_office_b64") if file_format == "docx" else payload.get("redacted_docx_b64")
        if not isinstance(redacted_b64, str):
            return _error("invalid_request", "redacted file content must be a base64 string", 400)
        if len(redacted_b64) > ((_max_bytes() + 2) // 3) * 4:
            return _error("document_too_large", "document exceeds the workbench byte limit", 413)
        try:
            data = base64.b64decode(redacted_b64.encode("ascii"), validate=True)
        except ValueError:
            return _error("invalid_request", "redacted file content is not valid base64", 400)
        if len(data) > _max_bytes():
            return _error("document_too_large", "document exceeds the workbench byte limit", 413)
        return _finish_refill_office(started, task_id, job, job_app, grant.entries, job.get("docx_ledger_json"), job.get("redacted_sha256"), grant.expected_counts, data, file_format, confirm_edited)

    @app.post("/api/v1/documents/{task_id}/mapping")
    def task_mapping(task_id: str, payload: dict = Body(default={})) -> Any:
        """Trusted display of a stored task's placeholder↔value mapping (local UI)."""
        job, job_app, grant, error = _task_grant(task_id)
        if error is not None:
            return error
        try:
            audit_log.write(
                _AUDIT_STREAM_WORKBENCH,
                {"event": "mapping_unlock", "task_id": task_id, "app_id": job_app, "entry_count": len(grant.entries), "source": "task"},
            )
        except OSError:
            pass
        return {
            "status": "ok",
            "app_id": job_app,
            "task_id": task_id,
            "file_name": job.get("file_name"),
            "entries": [
                {"placeholder": entry.placeholder, "label": entry.label, "original_value": entry.original_value}
                for entry in grant.entries
            ],
        }

    @app.post("/api/v1/documents/refill")
    def refill_document(payload: dict = Body(default={})) -> Any:
        started = time.monotonic()
        redacted_markdown = payload.get("redacted_markdown")
        package = payload.get("package")
        passphrase = payload.get("passphrase")
        confirm_edited = payload.get("confirm_edited", False)
        if not isinstance(redacted_markdown, str):
            return _error("invalid_request", "redacted_markdown must be a string", 400)
        if len(redacted_markdown.encode("utf-8")) > _max_bytes():
            return _error("document_too_large", "redacted_markdown exceeds the workbench byte limit", 413)
        if not isinstance(package, dict):
            return _error("invalid_package", "package must be an object", 400)
        binding = package.get("binding")
        if not isinstance(binding, dict):
            return _error("invalid_package", "package binding is required", 400)
        task_id = binding.get("job_id")
        if not isinstance(task_id, str) or not task_id:
            return _error("invalid_package", "package binding job_id is required", 400)
        if not isinstance(passphrase, str):
            return _error("invalid_request", "passphrase must be a string", 400)
        if not isinstance(confirm_edited, bool):
            return _error("invalid_request", "confirm_edited must be a boolean", 400)

        job = _job(job_store, task_id)
        job_app = (job.get("app_id") if job else None) or _APP_ID
        try:
            imported = import_package(
                package,
                passphrase=passphrase,
                job_id=task_id,
                app_id=job_app,
                source_sha256=job["file_sha256"] if job else None,
            )
        except PackageError as exc:
            return _error("package_rejected", str(exc), 403)

        return _finish_refill_markdown(started, task_id, job, job_app, imported.entries, imported.binding["expected_counts"], redacted_markdown, confirm_edited)

    def _answer_summary(
        text: str, entries: list, diff: Any
    ) -> dict[str, Any]:
        """Safe answer-refill summary: placeholder names, labels and counts only
        — never original values or answer content."""
        counts = Counter(PLACEHOLDER_RE.findall(text))
        known = {entry.placeholder: entry.label for entry in entries}
        matched = [
            {"placeholder": name, "label": known[name], "count": counts[name]}
            for name in sorted(known)
            if counts[name]
        ]
        return {
            "matched": matched,
            "unused": sorted(name for name in known if not counts[name]),
            "unknown": diff.unknown,
            "altered": diff.altered,
            "restored_count": sum(item["count"] for item in matched),
        }

    @app.post("/api/v1/documents/refill-answer")
    def refill_answer_endpoint(payload: dict = Body(default={})) -> Any:
        """Local refill of an online-AI answer (answer mode, plan §9.2).

        Subset references and repeated known placeholders are normal; unknown
        or altered placeholders block; zero hits require explicit confirmation.
        """
        started = time.monotonic()
        answer_markdown = payload.get("answer_markdown")
        package = payload.get("package")
        passphrase = payload.get("passphrase")
        confirm_empty = payload.get("confirm_empty", False)
        if not isinstance(answer_markdown, str):
            return _error("invalid_request", "answer_markdown must be a string", 400)
        if len(answer_markdown.encode("utf-8")) > _max_bytes():
            return _error("document_too_large", "answer exceeds the workbench byte limit", 413)
        if not isinstance(package, dict):
            return _error("invalid_package", "package must be an object", 400)
        binding = package.get("binding")
        if not isinstance(binding, dict):
            return _error("invalid_package", "package binding is required", 400)
        task_id = binding.get("job_id")
        if not isinstance(task_id, str) or not task_id:
            return _error("invalid_package", "package binding job_id is required", 400)
        if not isinstance(passphrase, str):
            return _error("invalid_request", "passphrase must be a string", 400)
        if not isinstance(confirm_empty, bool):
            return _error("invalid_request", "confirm_empty must be a boolean", 400)

        job = _job(job_store, task_id)
        job_app = (job.get("app_id") if job else None) or _APP_ID
        try:
            imported = import_package(
                package,
                passphrase=passphrase,
                job_id=task_id,
                app_id=job_app,
                source_sha256=job["file_sha256"] if job else None,
            )
        except PackageError as exc:
            return _error("package_rejected", str(exc), 403)

        return _finish_refill_answer(started, task_id, job_app, imported.entries, answer_markdown, confirm_empty)

    @app.post("/api/v1/documents/unlock-mapping")
    def unlock_mapping(payload: dict = Body(default={})) -> Any:
        """Trusted display of a recovery package's placeholder↔value mapping.

        Local-only surface (admin token + custom-header guard): returns the
        original values for the UI's auto-locking unlock zone. The audit row
        carries counts only — never values.
        """
        package = payload.get("package")
        passphrase = payload.get("passphrase")
        if not isinstance(package, dict):
            return _error("invalid_package", "package must be an object", 400)
        binding = package.get("binding")
        if not isinstance(binding, dict):
            return _error("invalid_package", "package binding is required", 400)
        task_id = binding.get("job_id")
        if not isinstance(task_id, str) or not task_id:
            return _error("invalid_package", "package binding job_id is required", 400)
        if not isinstance(passphrase, str):
            return _error("invalid_request", "passphrase must be a string", 400)

        job = _job(job_store, task_id)
        job_app = (job.get("app_id") if job else None) or _APP_ID
        versions = binding.get("versions")
        if (
            job is None
            and task_id.startswith("ns_")
            and binding.get("app_id") == "clipboard"
            and isinstance(versions, dict)
            and versions.get("source_kind") == _CLIPBOARD_PACKAGE_SOURCE
        ):
            # Namespace snapshots are not file jobs. This narrow, trusted-UI
            # path keeps their original app binding; import_package below
            # still authenticates the entire binding (including source_kind)
            # with the user's passphrase before exposing any mapping values.
            job_app = "clipboard"
        try:
            imported = import_package(
                package,
                passphrase=passphrase,
                job_id=task_id,
                app_id=job_app,
                source_sha256=job["file_sha256"] if job else None,
            )
        except PackageError as exc:
            return _error("package_rejected", str(exc), 403)

        try:
            audit_log.write(
                _AUDIT_STREAM_WORKBENCH,
                {
                    "event": "mapping_unlock",
                    "task_id": task_id,
                    "app_id": job_app,
                    "entry_count": len(imported.entries),
                },
            )
        except OSError:
            pass
        return {
            "status": "ok",
            "app_id": job_app,
            "task_id": task_id,
            "entries": [
                {
                    "placeholder": entry.placeholder,
                    "label": entry.label,
                    "original_value": entry.original_value,
                }
                for entry in imported.entries
            ],
        }

    def _bind_workbench_app(app_id: str | None):
        """Resolve the optional app binding shared by workbench endpoints."""
        if app_id is None:
            return None, [], None, None
        bound = app_id.strip()
        if not bound:
            return None, None, None, _error("invalid_query", "app_id must be non-empty", 400)
        if bound not in registry.known_apps():
            return None, None, None, _error("unknown_app", "unknown app in workbench binding", 404)
        try:
            dict_state = registry.resolve_dictionary_state(bound)
        except RegistryConfigurationError as exc:
            return None, None, None, _error(exc.code, str(exc), 503)
        return bound, dict_state.entries or [], dict_state.version, None

    @app.post("/api/v1/documents/redact-docx")
    async def redact_docx_endpoint(
        request: Request, filename: str | None = None, app_id: str | None = None,
        ole_strip: bool = False,
    ) -> Any:
        return await _redact_office(request, filename, app_id, "docx", ole_strip)

    @app.post("/api/v1/documents/redact-office")
    async def redact_office_endpoint(
        request: Request, filename: str | None = None, app_id: str | None = None,
    ) -> Any:
        file_format = PurePath(filename or "").suffix.lower().lstrip(".")
        file_format = LEGACY_TARGETS.get(file_format, file_format)  # .xls is converted to .xlsx first
        if file_format not in {"xlsx", "pptx"}:
            return _error("unsupported_format", "Office redaction supports .xlsx, .xls and .pptx", 415)
        return await _redact_office(request, filename, app_id, file_format)

    async def _redact_office(
        request: Request, filename: str | None, app_id: str | None,
        file_format: str, ole_strip: bool = False,
    ) -> Any:
        body = await request.body()
        return await run_in_threadpool(_redact_office_work, body, filename, app_id, file_format, ole_strip)

    def _redact_office_work(
        body: bytes, filename: str | None, app_id: str | None,
        file_format: str, ole_strip: bool = False,
    ) -> Any:
        """Format-preserving Office redaction using the same protected mapping.

        Patches OOXML text nodes at coordinates and returns the redacted
        package as base64 JSON. The artifact is only produced when the egress
        gate passes — a blocked export returns 422 WITHOUT the file, because
        for this endpoint the file IS the outbound artifact (unlike the
        markdown parse preview, which stays local).

        ole_strip=true 时把 OLE 嵌入对象（WPS 公式等二进制黑盒）整体移除后
        继续脱敏（安全降级：默认仍 ole_object_present fail-closed）。
        """
        started = time.monotonic()
        if not isinstance(filename, str) or not filename.strip():
            return _error("filename_required", "filename is required", 400)
        file_name = PurePath(filename).name
        if file_name in {"", ".", ".."}:
            return _error("filename_invalid", "filename must contain a bare file name", 400)
        legacy_source = PurePath(file_name).suffix.lower().lstrip(".")
        converted_note = None
        if LEGACY_TARGETS.get(legacy_source) == file_format:
            # .doc / .xls: convert first, then continue exactly like a .docx / .xlsx.
            if len(body) > _max_bytes():
                return _error("document_too_large", "document exceeds the workbench byte limit", 413)
            try:
                converted = convert_legacy(body, legacy_source)
            except LegacyConversionError as exc:
                return _error(exc.code, str(exc), 422)
            body = converted.data
            file_name = file_name[: -len(legacy_source)] + file_format
            converted_note = {
                "code": "legacy_converted",
                "message": f"原文件是 .{legacy_source}，已用 {converted.converter} 转换为 .{file_format} 后脱敏；脱敏文件和回填结果都是 .{file_format}，复杂版式可能有差异，请核对",
            }
        else:
            if not file_name.lower().endswith("." + file_format):
                return _error("unsupported_format", "file extension does not match the requested format", 415)
            if len(body) > _max_bytes():
                return _error("document_too_large", "document exceeds the workbench byte limit", 413)

        try:
            if file_format == "docx":
                probe_warnings = probe_docx(body, allow_ole_strip=ole_strip)
                paragraphs = extract_paragraph_texts(body, allow_ole_strip=ole_strip)
            else:
                probe_warnings = probe_office(body, file_format)
                paragraphs = extract_office_texts(body, file_format)
        except (DocxUnsupportedError, OfficeUnsupportedError) as exc:
            return _error(exc.code, str(exc), 422)
        except ValueError as exc:
            return _error("document_extraction_failed", str(exc), 422)
        if converted_note is not None:
            probe_warnings = [converted_note, *probe_warnings]

        bound_app, entries, dictionary_version, bind_error = _bind_workbench_app(app_id)
        if bind_error is not None:
            return bind_error
        effective_app = bound_app or _APP_ID

        profile = _file_profile(registry, bound_app)
        if not is_fully_reversible(profile):
            return _error(
                "profile_not_reversible",
                "file workbench profile is not fully reversible",
                500,
            )

        # Concatenate paragraph texts for one detection pass, tracking the
        # global→paragraph offset of each non-empty paragraph.
        nonempty = [item for item in paragraphs if item.text.strip()]
        ranges: list[tuple[int, Any]] = []
        pieces: list[str] = []
        cursor = 0
        for item in nonempty:
            ranges.append((cursor, item))
            pieces.append(item.text)
            cursor += len(item.text) + 2  # "\n\n" separator
        full_text = "\n\n".join(pieces)
        if not full_text.strip():
            return _error("empty_document", "document contains no patchable text", 422)
        if reserved_placeholder_conflict(full_text):
            return _error(
                "reserved_placeholder_conflict",
                "input contains reserved placeholder syntax",
                409,
            )

        detectors = build_detectors(entries=entries)
        header_cells = [
            TableCell(global_start, global_start + len(item.text), tuple(item.context))
            for global_start, item in ranges
            if getattr(item, "context", ())
        ]
        if header_cells:
            detectors.append(TableHeaderRuleDetector(header_cells))
        run = run_detection(full_text, detectors, profile)

        factory = PlaceholderFactory()
        key_to_placeholder: dict[tuple[str, str], str] = {}
        mapping_entries: list[MappingEntry] = []
        replacements: list[dict[str, Any]] = []
        skipped_spans = 0
        normalized = normalize_detections(full_text, run.kept)
        span_patched: list[bool] = []
        # Read-only structures (formulas, link targets, unknown attributes) cannot
        # be rewritten. A model guess there is almost always a false positive on
        # structural text and is dropped with a warning; a rule or dictionary hit
        # is real and blocks the export, naming the location.
        readonly_model_hits = 0
        readonly_blockers: list[str] = []
        readonly_dropped: list[Any] = []
        for span in normalized:
            original = full_text[span.start:span.end]
            owner_start, owner = next(((global_start, item) for global_start, item in ranges if global_start <= span.start and span.end <= global_start + len(item.text)), (0, None))
            if owner is not None and getattr(owner, "readonly", False):
                if _structural_hit(span, owner, span.start - owner_start, span.end - owner_start):
                    readonly_model_hits += 1
                    readonly_dropped.append(span)
                    span_patched.append(True)  # nothing to patch; excluded from residual view below
                    continue
                readonly_blockers.append(f"{owner.part} 的{_describe_structure(getattr(owner, 'kind', 'text'))}（{span.label}）")
                span_patched.append(False)
                continue
            prefix = span.metadata.get("role") or span.label
            key = (prefix, original)
            placeholder = key_to_placeholder.get(key)
            if placeholder is None:
                placeholder = factory.next(prefix)
                key_to_placeholder[key] = placeholder
                mapping_entries.append(MappingEntry(
                    placeholder=placeholder,
                    label=span.label,
                    original_value=original,
                    text_hash=hash_text(original),
                ))
            located = None
            for global_start, item in ranges:
                if global_start <= span.start and span.end <= global_start + len(item.text):
                    located = {
                        "part": item.part,
                        "paragraph": item.index,
                        "start": span.start - global_start,
                        "end": span.end - global_start,
                        "placeholder": placeholder,
                    }
                    break
            if located is None:
                # A cross-paragraph span cannot be patched at coordinates, and
                # skipping it would silently leak — so the export is blocked.
                skipped_spans += 1
                span_patched.append(False)
                continue
            span_patched.append(True)
            replacements.append(located)

        # Every detected value is replaced wherever else it appears in writable
        # text (chart caches, shape names, embedded sheets, other slides) with the
        # same placeholder, so the exported file cannot keep a copy the detector
        # missed in a different context. Mirrors the clipboard session's
        # known-value propagation; occurrences overlapping existing spans are left.
        taken: dict[tuple[str, int], list[tuple[int, int]]] = {}
        for located in replacements:
            taken.setdefault((located["part"], located["paragraph"]), []).append((located["start"], located["end"]))
        known_values = sorted(((entry.original_value, entry.placeholder) for entry in mapping_entries if len(entry.original_value) >= 2), key=lambda pair: -len(pair[0]))
        propagated = 0
        for item in nonempty:
            if getattr(item, "readonly", False):
                continue
            spans_here = taken.setdefault((item.part, item.index), [])
            for value, placeholder in known_values:
                position = 0
                while True:
                    found = item.text.find(value, position)
                    if found < 0:
                        break
                    end = found + len(value)
                    if not any(start < end and found < stop for start, stop in spans_here):
                        replacements.append({"part": item.part, "paragraph": item.index, "start": found, "end": end, "placeholder": placeholder})
                        spans_here.append((found, end))
                        propagated += 1
                    position = end
        if readonly_blockers:
            shown = "；".join(dict.fromkeys(readonly_blockers[:3]))
            more = f" 等 {len(readonly_blockers)} 处" if len(readonly_blockers) > 3 else ""
            return _error("readonly_text_replacement", f"readonly_text_replacement: 需要替换的文字位于只读结构，无法安全改写：{shown}{more}。请在 Office 中把该处改为普通文字后重试，或改用「解析并脱敏」得到文本版", 422)
        try:
            if file_format == "docx":
                patched = redact_docx(body, replacements, allow_ole_strip=ole_strip)
            else:
                patched = redact_office(body, replacements, file_format)
        except (DocxUnsupportedError, OfficeUnsupportedError) as exc:
            return _error(exc.code, str(exc), 422)
        except ValueError as exc:
            return _error(file_format + "_patch_failed", str(exc), 422)
        if propagated:
            patched.setdefault("warnings", []).append({
                "code": "known_values_propagated",
                "message": f"已识别的敏感值在其他 {propagated} 处（图表缓存、形状名、内嵌工作簿等）同样替换为相同占位符",
            })
        if readonly_model_hits:
            patched.setdefault("warnings", []).append({
                "code": "readonly_model_hits_ignored",
                "message": f"{readonly_model_hits} 处只读结构（公式运算式、工作表名、链接地址或未知属性）中的疑似命中（模型猜测、日期或金额）无法改写，已按结构文本忽略；如需核对，请用「解析并脱敏」查看文本版",
            })

        # Textual view of the redacted package for the residual scan — mirrors
        # the real artifact: unpatched (skipped) spans keep their original text.
        redacted_text = full_text
        for span, was_patched in sorted(
            zip(normalized, span_patched), key=lambda pair: pair[0].start, reverse=True
        ):
            if not was_patched:
                continue
            original = full_text[span.start:span.end]
            prefix = span.metadata.get("role") or span.label
            placeholder = key_to_placeholder.get((prefix, original))
            if placeholder is None:
                continue  # ignored structural hit: no placeholder was assigned
            redacted_text = redacted_text[:span.start] + placeholder + redacted_text[span.end:]

        extra_reasons: list[dict[str, str]] = []
        if file_format != "docx":
            # Reopen the actual output: a synthetic replacement string cannot
            # establish that every supported XML location was really patched.
            try:
                output_units = extract_office_texts(patched["data"], file_format)
            except ValueError:
                return _error("office_verification_failed", "Office output could not be verified", 422)
            # Read-only structures were never patch targets (strong hits there
            # already blocked above), so verify the writable text only.
            redacted_text = "\n\n".join(item.text for item in output_units if not item.readonly)
            clear_text = PLACEHOLDER_RE.sub("", redacted_text)
            if any(entry.original_value in clear_text for entry in mapping_entries):
                extra_reasons.append({
                    "code": "detected_value_residual",
                    "message": "输出文件中仍有本次已识别的敏感值，已停止导出",
                })
        if skipped_spans:
            extra_reasons.append({
                "code": "cross_paragraph_span",
                "message": f"{skipped_spans} 个跨段落实体无法坐标化补丁",
            })
        versions = _versions()
        egress = _evaluate_egress(
            bound_app=bound_app,
            dictionary_version=dictionary_version,
            run=run,
            redacted_text=redacted_text,
            profile=profile,
            versions=versions,
            extra_reasons=extra_reasons,
        )
        if not egress["egress_allowed"]:
            return JSONResponse(
                {
                    "status": "error",
                    "egress_allowed": False,
                    "error": {
                        "code": "egress_blocked",
                        "message": "出站闸门未通过，未生成脱敏 " + file_format.upper(),
                    },
                    "egress": egress,
                },
                status_code=422,
            )

        job_id = "job-" + secrets.token_urlsafe(12)
        redacted_bytes = patched["data"]
        if file_format != "docx":
            # Otherwise two documents differing only in sensitive values may
            # produce identical masked bytes and accept each other's package.
            # An opaque ZIP comment makes the recovery hash task-specific;
            # Office re-saves which remove it require edited-file confirmation.
            tagged = io.BytesIO(redacted_bytes)
            with zipfile.ZipFile(tagged, "a") as archive:
                archive.comment = ("tuomin:" + job_id).encode("ascii")
            redacted_bytes = tagged.getvalue()
        expected_counts = dict(Counter(item["placeholder"] for item in replacements))
        mapping_ref = vault.create(
            app_id=effective_app,
            scope="document",
            entries=mapping_entries,
            expected_counts=expected_counts,
        )
        source_digest = hashlib.sha256(body).hexdigest()
        redacted_digest = hashlib.sha256(redacted_bytes).hexdigest()
        ledger_section = {
            "format": file_format + "-ledger-v1",
            "parts": sorted({item["part"] for item in replacements}),
            "ledger": patched["ledger"],
            "untouched_hashes": patched["untouched_hashes"],
        }
        label_counts = dict(sorted(Counter(entry.label for entry in mapping_entries).items()))
        try:
            job_store.create_job(
                task_id=job_id,
                file_name=file_name,
                file_sha256=source_digest,
                file_format=file_format,
                profile=profile.name,
                versions=versions,
                mapping_ref=mapping_ref,
                app_id=effective_app,
                docx_ledger=ledger_section,
            )
            job_store.complete_job(
                job_id,
                label_counts=label_counts,
                redacted_sha256=redacted_digest,
                artifacts=[],
            )
            job_store.record_call(
                entry="workbench",
                app_id=effective_app,
                task_id=job_id,
                duration_ms=max(0, int((time.monotonic() - started) * 1000)),
                char_count=len(full_text),
                label_counts=label_counts,
                blocked=bool(run.blocked_labels),
                policy_version=POLICY_SCHEMA_VERSION,
            )
        except (OSError, JobFieldError):
            if file_format != "docx":
                return _error("job_persistence_failed", "无法保存恢复信息，未返回脱敏文件，请重试", 503)

        return {
            "status": "ok",
            "task_id": job_id,
            "file_name": file_name,
            "file_format": file_format,
            ("redacted_docx_b64" if file_format == "docx" else "redacted_office_b64"):
                base64.b64encode(redacted_bytes).decode("ascii"),
            "redacted_sha256": redacted_digest,
            "label_counts": label_counts,
            "egress": egress,
            "warnings": _safe_warnings(probe_warnings + patched["warnings"]),
        }

    @app.post("/api/v1/documents/refill-docx")
    def refill_docx_endpoint(payload: dict = Body(default={})) -> Any:
        return _refill_office(payload, "docx")

    @app.post("/api/v1/documents/refill-office")
    def refill_office_endpoint(payload: dict = Body(default={})) -> Any:
        file_format = payload.get("file_format")
        if not isinstance(file_format, str) or file_format not in {"xlsx", "pptx"}:
            return _error("unsupported_format", "Office refill requires file_format xlsx or pptx", 415)
        return _refill_office(payload, file_format)

    def _refill_office(payload: dict, file_format: str) -> Any:
        """Format-preserving Office refill using an encrypted recovery package.

        Placeholder integrity is always checked. For XLSX/PPTX the recorded
        hash additionally distinguishes direct refill from an edited or
        mismatched file that requires explicit confirmation. DOCX retains its
        existing contract, including rejection of split placeholders.
        """
        started = time.monotonic()
        confirm_edited = payload.get("confirm_edited", False)
        if file_format != "docx" and not isinstance(confirm_edited, bool):
            return _error("invalid_request", "confirm_edited must be a boolean", 400)
        package = payload.get("package")
        passphrase = payload.get("passphrase")
        content_key = "redacted_docx_b64" if file_format == "docx" else "redacted_office_b64"
        redacted_b64 = payload.get(content_key)
        if not isinstance(redacted_b64, str):
            return _error("invalid_request", content_key + " must be a string", 400)
        if len(redacted_b64) > ((_max_bytes() + 2) // 3) * 4:
            return _error("document_too_large", "document exceeds the workbench byte limit", 413)
        try:
            data = base64.b64decode(redacted_b64.encode("ascii"), validate=True)
        except ValueError:
            return _error("invalid_request", content_key + " is not valid base64", 400)
        if len(data) > _max_bytes():
            return _error("document_too_large", "document exceeds the workbench byte limit", 413)
        if not isinstance(package, dict):
            return _error("invalid_package", "package must be an object", 400)
        binding = package.get("binding")
        if not isinstance(binding, dict):
            return _error("invalid_package", "package binding is required", 400)
        task_id = binding.get("job_id")
        if not isinstance(task_id, str) or not task_id:
            return _error("invalid_package", "package binding job_id is required", 400)
        if not isinstance(passphrase, str):
            return _error("invalid_request", "passphrase must be a string", 400)

        job = _job(job_store, task_id)
        job_app = (job.get("app_id") if job else None) or _APP_ID
        try:
            imported = import_package(
                package,
                passphrase=passphrase,
                job_id=task_id,
                app_id=job_app,
                source_sha256=job["file_sha256"] if job else None,
            )
        except PackageError as exc:
            return _error("package_rejected", str(exc), 403)

        return _finish_refill_office(started, task_id, job, job_app, imported.entries, imported.docx, imported.binding.get("redacted_sha256"), imported.binding.get("expected_counts", {}), data, file_format, confirm_edited)
