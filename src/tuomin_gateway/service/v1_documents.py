"""Local file workbench endpoints: parse/redact, package export, refill."""
from __future__ import annotations

import base64
from collections import Counter
import hashlib
import os
from pathlib import PurePath
import secrets
import tempfile
import time
from typing import Any

from fastapi import Body, FastAPI, Request
from fastapi.responses import JSONResponse

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
from tuomin_gateway.document.markdown import render_clean_markdown
from tuomin_gateway.document.refill_flow import MODE_EDITED, refill_answer, refill_markdown
from tuomin_gateway.jobs import JobFieldError, JobStore
from tuomin_gateway.mapping import PlaceholderFactory
from tuomin_gateway.mapproc import PackageError, export_manifest, export_package, import_package
from tuomin_gateway.placeholders import PLACEHOLDER_RE, reserved_placeholder_conflict
from tuomin_gateway.policy import POLICY_SCHEMA_VERSION
from tuomin_gateway.profiles import get_profile, is_fully_reversible
from tuomin_gateway.redactor import normalize_detections, redact_text
from tuomin_gateway.schemas import MappingEntry
from tuomin_gateway.service.registry import AppRegistry, RegistryConfigurationError
from tuomin_gateway.service.v1 import _error
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
        {
            "code": str(warning.get("code", "document_warning")),
            "message": str(warning.get("message", "")),
        }
        for warning in warnings
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
        started = time.monotonic()
        if not isinstance(filename, str) or not filename.strip():
            return _error("filename_required", "filename is required", 400)
        file_name = PurePath(filename).name
        if file_name in {"", ".", ".."}:
            return _error("filename_invalid", "filename must contain a bare file name", 400)
        body = await request.body()
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

        profile = get_profile(_PROFILE)
        if not is_fully_reversible(profile):
            return _error(
                "profile_not_reversible",
                "file workbench profile is not fully reversible",
                500,
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

        run = run_detection(text, build_detectors(entries=entries), profile)
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
                profile=_PROFILE,
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
                profile=_PROFILE,
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
        )
        try:
            audit_log.write(
                _AUDIT_STREAM_WORKBENCH,
                {"event": "package_export", "task_id": task_id, "app_id": job_app},
            )
        except OSError:
            pass
        return {"status": "ok", "package": package}

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

        result = refill_markdown(
            redacted_markdown,
            imported.entries,
            imported.binding["expected_counts"],
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
            redacted_markdown.count(entry.placeholder) for entry in imported.entries
        )
        label_counts = dict(sorted(Counter(entry.label for entry in imported.entries).items()))
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

        result = refill_answer(
            answer_markdown, imported.entries, confirm_empty=confirm_empty
        )
        summary = _answer_summary(answer_markdown, imported.entries, result.diff)
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
        """Format-preserving DOCX redaction (plan §9.4).

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
        if not file_name.lower().endswith(".docx"):
            return _error("unsupported_format", "format-preserving redaction supports .docx only", 415)
        body = await request.body()
        if len(body) > _max_bytes():
            return _error("document_too_large", "document exceeds the workbench byte limit", 413)

        try:
            probe_warnings = probe_docx(body, allow_ole_strip=ole_strip)
            paragraphs = extract_paragraph_texts(body, allow_ole_strip=ole_strip)
        except DocxUnsupportedError as exc:
            return _error(exc.code, str(exc), 422)
        except ValueError as exc:
            return _error("document_extraction_failed", str(exc), 422)

        bound_app, entries, dictionary_version, bind_error = _bind_workbench_app(app_id)
        if bind_error is not None:
            return bind_error
        effective_app = bound_app or _APP_ID

        profile = get_profile(_PROFILE)
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

        run = run_detection(full_text, build_detectors(entries=entries), profile)

        factory = PlaceholderFactory()
        key_to_placeholder: dict[tuple[str, str], str] = {}
        mapping_entries: list[MappingEntry] = []
        replacements: list[dict[str, Any]] = []
        skipped_spans = 0
        normalized = normalize_detections(full_text, run.kept)
        span_patched: list[bool] = []
        for span in normalized:
            original = full_text[span.start:span.end]
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

        try:
            patched = redact_docx(body, replacements, allow_ole_strip=ole_strip)
        except DocxUnsupportedError as exc:
            return _error(exc.code, str(exc), 422)
        except ValueError as exc:
            return _error("docx_patch_failed", str(exc), 422)

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
            placeholder = key_to_placeholder[(prefix, original)]
            redacted_text = redacted_text[:span.start] + placeholder + redacted_text[span.end:]

        extra_reasons: list[dict[str, str]] = []
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
                        "message": "出站闸门未通过，未生成脱敏 DOCX",
                    },
                    "egress": egress,
                },
                status_code=422,
            )

        redacted_bytes = patched["data"]
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
            "format": "docx-ledger-v1",
            "parts": sorted({item["part"] for item in replacements}),
            "ledger": patched["ledger"],
            "untouched_hashes": patched["untouched_hashes"],
        }
        label_counts = dict(sorted(Counter(entry.label for entry in mapping_entries).items()))
        job_id = "job-" + secrets.token_urlsafe(12)
        try:
            job_store.create_job(
                task_id=job_id,
                file_name=file_name,
                file_sha256=source_digest,
                file_format="docx",
                profile=_PROFILE,
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
            pass

        return {
            "status": "ok",
            "task_id": job_id,
            "file_name": file_name,
            "file_format": "docx",
            "redacted_docx_b64": base64.b64encode(redacted_bytes).decode("ascii"),
            "redacted_sha256": redacted_digest,
            "label_counts": label_counts,
            "egress": egress,
            "warnings": _safe_warnings(probe_warnings + patched["warnings"]),
        }

    @app.post("/api/v1/documents/refill-docx")
    def refill_docx_endpoint(payload: dict = Body(default={})) -> Any:
        """Format-preserving DOCX refill (plan §9.4).

        Placeholder-integrity driven: a Word re-save always changes bytes and
        may reshuffle runs, so exact-hash classification is meaningless here —
        unknown/altered/split placeholders fail closed instead.
        """
        started = time.monotonic()
        package = payload.get("package")
        passphrase = payload.get("passphrase")
        redacted_b64 = payload.get("redacted_docx_b64")
        if not isinstance(redacted_b64, str):
            return _error("invalid_request", "redacted_docx_b64 must be a string", 400)
        try:
            data = base64.b64decode(redacted_b64.encode("ascii"), validate=True)
        except ValueError:
            return _error("invalid_request", "redacted_docx_b64 is not valid base64", 400)
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

        values = {entry.placeholder: entry.original_value for entry in imported.entries}
        try:
            result = refill_docx(data, values)
        except DocxUnsupportedError as exc:
            return _error(exc.code, str(exc), 422)
        except ValueError as exc:
            return _error("docx_refill_failed", str(exc), 422)
        if result["status"] == "blocked":
            return JSONResponse(
                {
                    "status": "blocked",
                    "error_types": result["error_types"],
                    "diff": result["diff"],
                },
                status_code=422,
            )

        mode = (
            "exact"
            if job and hashlib.sha256(data).hexdigest() == job.get("redacted_sha256")
            else "edited"
        )
        try:
            audit_log.write(
                AUDIT_STREAM_REFILL,
                {
                    "event": "trusted_refill",
                    "app_id": job_app,
                    "task_id": task_id,
                    "contract": f"workbench:docx:{mode}",
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
            "restored_docx_b64": base64.b64encode(result["data"]).decode("ascii"),
        }
