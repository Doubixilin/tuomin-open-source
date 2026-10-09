"""Versioned capability-protected Tuomin service API."""
from __future__ import annotations

from collections import Counter
from dataclasses import replace
import hashlib
import json
import os
import re
import time
from typing import Any

from fastapi import Body, FastAPI, Request
from fastapi.responses import JSONResponse

from tuomin_gateway import __version__
from tuomin_gateway.audit import AUDIT_STREAM_NAMESPACE, build_audit_event
from tuomin_gateway.placeholders import PLACEHOLDER_RE, reserved_placeholder_conflict
from tuomin_gateway.namespace_vault import NamespaceMismatch, NamespaceVault
from tuomin_gateway.inspection import InspectionMismatch, InspectionVault
from tuomin_gateway.policy import POLICY_SCHEMA_VERSION, dimensions_from_profile
from tuomin_gateway.profiles import BLOCK, REDACT, Profile
from tuomin_gateway.redactor import redact_text
from tuomin_gateway.refill import (
    StructuredRefillInvalid,
    StructuredRefillTooLarge,
    refill_structured_contract,
    refill_text_contract,
)
from tuomin_gateway.schemas import DetectionSpan, hash_text
from tuomin_gateway.service.limits import (
    max_batch_items,
    max_text_chars,
    text_over_limit,
)
from tuomin_gateway.service.registry import (
    AppRegistry,
    CapabilityDenied,
    RegistryConfigurationError,
    is_structured_value_label,
)
from tuomin_gateway.session import (
    RequiredDetectorUnavailable,
    SessionRedactor,
    build_detectors,
    probe_ner_runtime,
    run_detection,
)
from tuomin_gateway.store import MappingStore
from tuomin_gateway.vault import MappingGrantMismatch, MappingVault


_T2_CONTRACTS = {
    "structured_values": "v1",
    "egress_decision": "v1",
    "structured_refill": "v1",
    "protection_receipt": "v1",
}
_SAFE_RECEIPT_VERSION = re.compile(r"(?:sha256:)?[a-zA-Z0-9._:-]{1,200}")


def _record_call(request: Request, *, entry: str, app_id: str, started: float, char_count: int, label_counts: dict, blocked: bool, task_id: str | None = None) -> None:
    """Best-effort write to the safe call ledger (counts/versions only — never
    text or values). Ledger failures must never break the data path."""
    job_store = getattr(request.app.state, "job_store", None)
    if job_store is None:
        return
    from tuomin_gateway.jobs import JobFieldError

    try:
        job_store.record_call(
            entry=entry,
            app_id=app_id,
            task_id=task_id,
            duration_ms=max(0, int((time.monotonic() - started) * 1000)),
            char_count=char_count,
            label_counts=label_counts,
            blocked=blocked,
            policy_version=POLICY_SCHEMA_VERSION,
        )
    except (OSError, JobFieldError):
        pass


def _profile_version(profile: Profile) -> str:
    canonical = json.dumps(
        dimensions_from_profile(profile).to_safe_dict(),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(canonical).hexdigest()


def _detector_versions(*readiness_values: object) -> dict[str, str]:
    versions: dict[str, str] = {}
    for readiness in readiness_values:
        for state in getattr(readiness, "states", ()):
            if state.active and isinstance(state.version, str) and state.version:
                version = state.version
                if _SAFE_RECEIPT_VERSION.fullmatch(version) is None:
                    version = (
                        "sha256:"
                        + hashlib.sha256(version.encode("utf-8")).hexdigest()
                    )
                versions[state.name] = version
    return dict(sorted(versions.items()))


def _protection_receipt(
    *,
    app_id: str,
    scope: str,
    profile: Profile,
    dictionary_version: str | None,
    readiness_values: tuple[object, ...] = (),
) -> dict[str, object]:
    return {
        "schema_version": "tuomin-protection-receipt-v1",
        "app_id": app_id,
        "scope": scope,
        "profile_name": profile.name,
        "profile_version": _profile_version(profile),
        "dictionary_version": dictionary_version,
        "detector_versions": _detector_versions(*readiness_values),
    }


def _error(code: str, message: str, status: int) -> JSONResponse:
    return JSONResponse(
        {
            "status": "error",
            "version": __version__,
            "egress_allowed": False,
            "error": {"code": code, "message": message},
        },
        status_code=status,
    )


def _token(request: Request) -> str | None:
    value = request.headers.get("x-tuomin-capability-token")
    if value:
        return value
    authorization = request.headers.get("authorization", "")
    if authorization.lower().startswith("bearer "):
        return authorization[7:].strip()
    return None


def register_v1_api(
    app: FastAPI,
    registry: AppRegistry,
    store: MappingStore,
    *,
    vault: MappingVault | None = None,
    namespace_vault: NamespaceVault | None = None,
    inspection_vault: InspectionVault | None = None,
) -> MappingVault:
    vault = vault or MappingVault(store)
    namespace_vault = namespace_vault or NamespaceVault(
        # A crashed consumer cannot run its explicit DELETE.  Keep namespace
        # state on the same finite retention horizon as every derived grant so
        # abandoned session/attempt mappings do not become permanent.
        store.child("namespaces", ttl_seconds=store.ttl_seconds)
    )
    inspection_vault = inspection_vault or InspectionVault(
        store.child(
            "inspections",
            ttl_seconds=int(
                os.environ.get(
                    "TUOMIN_INSPECTION_TTL", str(180 * 24 * 3600)
                )
            ),
        )
    )

    def _audit_namespace(
        event: str,
        app_id: str,
        namespace_id: str,
        **safe_fields: object,
    ) -> None:
        """Persist a namespace security event (ids only, never mapping values).
        Audit failures must not break the data path."""
        try:
            vault.audit_log.write(
                AUDIT_STREAM_NAMESPACE,
                {
                    "event": event,
                    "app_id": app_id,
                    "namespace_id": namespace_id,
                    **safe_fields,
                },
            )
        except OSError:
            pass

    def authorize(request: Request, app_id: object, capability: str) -> str | JSONResponse:
        if not isinstance(app_id, str) or not app_id:
            return _error("app_id_required", "app_id is required", 400)
        try:
            registry.authorize(app_id, capability, _token(request))
        except CapabilityDenied:
            return _error("capability_denied", "capability denied", 403)
        return app_id

    def authorize_namespace(request: Request, app_id: object) -> str | JSONResponse:
        authorized = authorize(request, app_id, "namespace")
        if isinstance(authorized, JSONResponse):
            return authorized
        if "namespace" not in registry.allowed_mapping_scopes(authorized):
            return _error("mapping_scope_denied", "namespace mapping scope denied", 403)
        return authorized

    def resolve(app_id: str):
        profile = registry.resolve_profile(app_id)
        dictionary = registry.resolve_dictionary_state(app_id)
        return profile, dictionary.entries, dictionary.version

    def resolve_structured_value_policy(
        app_id: str,
    ) -> tuple[Profile, frozenset[str], dict[str, str]]:
        profile = registry.resolve_profile(app_id)
        allowed = registry.allowed_structured_value_labels(app_id)
        decisions: dict[str, str] = {}
        for label in allowed:
            decision = profile.decide(
                DetectionSpan(
                    start=0,
                    end=1,
                    label=label,
                    confidence=1.0,
                    source="declared_value",
                    detector_version="structured-values-v1",
                    text_hash=hash_text(""),
                    risk_level="critical",
                )
            )
            if decision not in {REDACT, BLOCK}:
                raise RegistryConfigurationError(
                    "structured value policy unavailable",
                    code="structured_value_policy_unavailable",
                )
            decisions[label] = decision
        return profile, allowed, decisions

    @app.get("/api/v1/health")
    def health() -> dict[str, object]:
        store.purge_expired()
        namespace_vault.store.purge_expired()
        # Inspection snapshots hold original values under encryption; they must
        # stay on the same finite retention horizon as every other grant.
        inspection_vault.store.purge_expired()
        return {"status": "ok", "version": __version__}

    @app.get("/api/v1/readiness")
    def readiness(request: Request, app_id: str | None = None) -> JSONResponse:
        if app_id is None:
            required = registry.resolve_profile(None).ner_required
            dictionary_errors = 0
            for registered_app_id in registry.known_apps():
                app_requires_ner = registry.resolve_profile(
                    registered_app_id
                ).ner_required
                required = required or app_requires_ner
                try:
                    registry.resolve_dictionary(registered_app_id)
                except RegistryConfigurationError:
                    dictionary_errors += 1
            ner = probe_ner_runtime(required=required)
            ready = (
                not required or bool(ner["loadable"])
            ) and dictionary_errors == 0
            return JSONResponse(
                {
                    "status": "ready" if ready else "not_ready",
                    "version": __version__,
                    "detectors": {"ner": ner},
                    "dictionary_error_count": dictionary_errors,
                },
                status_code=200 if ready else 503,
            )

        authorized = authorize_namespace(request, app_id)
        if isinstance(authorized, JSONResponse):
            return authorized
        try:
            profile, allowed_labels, _decisions = resolve_structured_value_policy(
                authorized
            )
            dictionary = registry.resolve_dictionary_state(authorized)
        except RegistryConfigurationError as exc:
            return JSONResponse(
                {
                    "status": "not_ready",
                    "version": __version__,
                    "error": {
                        "code": exc.code,
                        "message": "app protection configuration unavailable",
                    },
                },
                status_code=503,
            )

        ner = (
            probe_ner_runtime(required=profile.ner_required)
            if profile.use_ner
            else {
                "requested": False,
                "required": False,
                "loadable": True,
                "active": False,
                "error_type": None,
            }
        )
        if profile.ner_required and not bool(ner["loadable"]):
            return JSONResponse(
                {
                    "status": "not_ready",
                    "version": __version__,
                    "error": {
                        "code": "required_detector_unavailable",
                        "message": "required detector unavailable",
                    },
                    "detectors": {"ner": ner},
                },
                status_code=503,
            )
        return JSONResponse(
            {
                "status": "ready",
                "version": __version__,
                "contracts": dict(_T2_CONTRACTS),
                "app": {
                    "app_id": authorized,
                    "profile_name": profile.name,
                    "profile_version": _profile_version(profile),
                    "dictionary_configured": dictionary.configured,
                    "dictionary_version": dictionary.version,
                    "structured_value_labels": sorted(allowed_labels),
                },
                "detectors": {"ner": ner},
            }
        )

    @app.post("/api/v1/detect")
    def detect(request: Request, payload: dict = Body(default={})) -> Any:
        app_id = authorize(request, payload.get("app_id"), "detect")
        if isinstance(app_id, JSONResponse):
            return app_id
        text = payload.get("text")
        if not isinstance(text, str) or not text.strip():
            return _error("text_required", "text is required", 400)
        if (limit := text_over_limit(text)) is not None:
            return _error("text_too_large", f"text exceeds the {limit} character limit", 413)
        try:
            profile, entries, dictionary_version = resolve(app_id)
            run = run_detection(
                text,
                build_detectors(
                    entries, dictionary_version=dictionary_version
                ),
                profile,
            )
        except RequiredDetectorUnavailable as exc:
            return JSONResponse(
                {
                    "status": "error",
                    "version": __version__,
                    "error": {"code": "required_detector_unavailable", "message": "required detector unavailable"},
                    "egress_allowed": False,
                    "detectors": exc.readiness.to_safe_dict(),
                }, status_code=503
            )
        return {
            "status": "ok",
            "version": __version__,
            "detections": [span.to_safe_dict() for span in run.kept],
            "blocked_labels": run.blocked_labels,
            "egress_allowed": not run.blocked_labels,
            "profile": dimensions_from_profile(profile).to_safe_dict(),
            "detectors": run.readiness.to_safe_dict(),
        }

    @app.post("/api/v1/redact")
    def redact(request: Request, payload: dict = Body(default={})) -> Any:
        started = time.monotonic()
        app_id = authorize(request, payload.get("app_id"), "redact")
        if isinstance(app_id, JSONResponse):
            return app_id
        if "document" not in registry.allowed_mapping_scopes(app_id):
            return _error("mapping_scope_denied", "document mapping scope denied", 403)
        text = payload.get("text")
        if not isinstance(text, str) or not text.strip():
            return _error("text_required", "text is required", 400)
        if (limit := text_over_limit(text)) is not None:
            return _error("text_too_large", f"text exceeds the {limit} character limit", 413)
        if reserved_placeholder_conflict(text):
            return _error(
                "reserved_placeholder_conflict",
                "input contains reserved placeholder syntax",
                409,
            )
        profile, entries, dictionary_version = resolve(app_id)
        run = run_detection(
            text,
            build_detectors(entries, dictionary_version=dictionary_version),
            profile,
        )
        result = redact_text(text, run.kept)
        expected_counts = dict(Counter(PLACEHOLDER_RE.findall(result.redacted_text)))
        handle = vault.create(
            app_id=app_id,
            scope="document",
            entries=result.mapping,
            expected_counts=expected_counts,
        )
        audit = build_audit_event(result.task_id, run.kept, result.mapping)
        _record_call(
            request,
            entry="api",
            app_id=app_id,
            started=started,
            char_count=len(text),
            label_counts=dict(sorted(Counter(entry.label for entry in result.mapping).items())),
            blocked=bool(run.blocked_labels),
            task_id=result.task_id,
        )
        return {
            "status": "ok",
            "version": __version__,
            "masked_text": result.redacted_text,
            "mapping_handle": handle,
            "profile": dimensions_from_profile(profile).to_safe_dict(),
            "detectors": run.readiness.to_safe_dict(),
            "blocked_labels": run.blocked_labels,
            "label_counts": dict(
                sorted(Counter(entry.label for entry in result.mapping).items())
            ),
            "egress_allowed": not run.blocked_labels,
            "audit": audit.to_safe_dict(),
            "protection_receipt": _protection_receipt(
                app_id=app_id,
                scope="document",
                profile=profile,
                dictionary_version=dictionary_version,
                readiness_values=(run.readiness,),
            ),
        }

    @app.post("/api/v1/redact/batch")
    def redact_batch(request: Request, payload: dict = Body(default={})) -> Any:
        """Provider-neutral batch redaction for documents or one namespace.

        The complete batch is validated before any mapping or namespace state
        is created. A namespace batch is persisted under one lock/save cycle so
        placeholders stay stable across every item in the request.
        """
        namespace_id = payload.get("namespace_id")
        capability = "namespace" if namespace_id is not None else "redact"
        app_id = authorize(request, payload.get("app_id"), capability)
        if isinstance(app_id, JSONResponse):
            return app_id
        scope = "namespace" if namespace_id is not None else "document"
        if scope not in registry.allowed_mapping_scopes(app_id):
            return _error("mapping_scope_denied", f"{scope} mapping scope denied", 403)
        if namespace_id is not None and (
            not isinstance(namespace_id, str) or not namespace_id
        ):
            return _error("invalid_namespace_id", "namespace_id is invalid", 400)

        raw_items = payload.get("items")
        if not isinstance(raw_items, list) or not raw_items:
            return _error("items_required", "items must be a non-empty list", 400)
        if len(raw_items) > max_batch_items():
            return _error(
                "batch_too_large",
                f"items exceed the {max_batch_items()} item limit",
                413,
            )
        items: list[tuple[str, str]] = []
        seen_ids: set[str] = set()
        for raw_item in raw_items:
            if not isinstance(raw_item, dict):
                return _error("invalid_batch_item", "each item must be an object", 400)
            item_id = raw_item.get("id")
            text = raw_item.get("text")
            if (
                not isinstance(item_id, str)
                or not item_id
                or item_id in seen_ids
                or not isinstance(text, str)
                or not text.strip()
            ):
                return _error(
                    "invalid_batch_item",
                    "each item requires a unique non-empty id and text",
                    400,
                )
            if (limit := text_over_limit(text)) is not None:
                return _error(
                    "text_too_large",
                    f"item text exceeds the {limit} character limit",
                    413,
                )
            if reserved_placeholder_conflict(text):
                return _error(
                    "reserved_placeholder_conflict",
                    "input contains reserved placeholder syntax",
                    409,
                )
            seen_ids.add(item_id)
            items.append((item_id, text))

        profile, entries, dictionary_version = resolve(app_id)
        if namespace_id is None:
            detectors = build_detectors(
                entries, dictionary_version=dictionary_version
            )
            response_items: list[dict[str, object]] = []
            batch_readiness: list[object] = []
            for item_id, text in items:
                run = run_detection(text, detectors, profile)
                batch_readiness.append(run.readiness)
                result = redact_text(text, run.kept)
                expected_counts = dict(
                    Counter(PLACEHOLDER_RE.findall(result.redacted_text))
                )
                handle = vault.create(
                    app_id=app_id,
                    scope="document",
                    entries=result.mapping,
                    expected_counts=expected_counts,
                )
                audit = build_audit_event(result.task_id, run.kept, result.mapping)
                response_items.append(
                    {
                        "id": item_id,
                        "status": "ok",
                        "masked_text": result.redacted_text,
                        "mapping_handle": handle,
                        "blocked_labels": run.blocked_labels,
                        "egress_allowed": not run.blocked_labels,
                        "detectors": run.readiness.to_safe_dict(),
                        "audit": audit.to_safe_dict(),
                    }
                )
            blocked_labels = sorted(
                {
                    label
                    for item in response_items
                    for label in item["blocked_labels"]
                }
            )
            return {
                "status": "ok",
                "version": __version__,
                "scope": "document",
                "blocked_labels": blocked_labels,
                "egress_allowed": not blocked_labels,
                "profile": dimensions_from_profile(profile).to_safe_dict(),
                "items": response_items,
                "protection_receipt": _protection_receipt(
                    app_id=app_id,
                    scope="document",
                    profile=profile,
                    dictionary_version=dictionary_version,
                    readiness_values=tuple(batch_readiness),
                ),
            }

        try:
            with namespace_vault.lock(namespace_id):
                state = namespace_vault.load(namespace_id, app_id=app_id)
                if state.status != "active":
                    return _error("namespace_inactive", "namespace is not active", 409)
                redactor = SessionRedactor(
                    build_detectors(
                        entries, dictionary_version=dictionary_version
                    ),
                    profile,
                )
                redactor.hydrate(state.entries)
                masked_items = [
                    (item_id, redactor.mask(text)) for item_id, text in items
                ]
                persisted_entries = redactor.mapping_entries()
                # Mint grants INSIDE the lock (a grant must match the persisted
                # entries) and record every handle so namespace delete can
                # revoke the derived grants (they are self-contained).
                minted_items: list[tuple[str, str, str]] = []
                for item_id, masked_text in masked_items:
                    handle = vault.create(
                        app_id=app_id,
                        scope="namespace",
                        entries=persisted_entries,
                        expected_counts=dict(Counter(PLACEHOLDER_RE.findall(masked_text))),
                    )
                    minted_items.append((item_id, masked_text, handle))
                namespace_vault.save(
                    replace(
                        state,
                        entries=persisted_entries,
                        updated_at=int(time.time()),
                        grant_handles=[
                            *state.grant_handles,
                            *(handle for _, _, handle in minted_items),
                        ],
                        trace=[*state.trace, *redactor.trace_entries()],
                    )
                )
        except (FileNotFoundError, NamespaceMismatch, ValueError, KeyError):
            return _error("namespace_not_found", "namespace unavailable", 404)

        response_items = []
        for item_id, masked_text, handle in minted_items:
            response_items.append(
                {
                    "id": item_id,
                    "status": "ok",
                    "masked_text": masked_text,
                    "mapping_handle": handle,
                }
            )
        return {
            "status": "ok",
            "version": __version__,
            "scope": "namespace",
            "namespace_id": namespace_id,
            "identity_contract": "canonical",
            "profile": dimensions_from_profile(profile).to_safe_dict(),
            "detectors": redactor.last_readiness.to_safe_dict()
            if redactor.last_readiness is not None
            else {},
            "blocked_labels": sorted(redactor.blocked_labels),
            "egress_allowed": not redactor.blocked_labels,
            "items": response_items,
            "protection_receipt": _protection_receipt(
                app_id=app_id,
                scope="namespace",
                profile=profile,
                dictionary_version=dictionary_version,
                readiness_values=(redactor.last_readiness,),
            ),
        }

    @app.post("/api/v1/redact/values")
    def redact_values(request: Request, payload: dict = Body(default={})) -> Any:
        app_id = authorize_namespace(request, payload.get("app_id"))
        if isinstance(app_id, JSONResponse):
            return app_id
        namespace_id = payload.get("namespace_id")
        if (
            not isinstance(namespace_id, str)
            or len(namespace_id) <= 3
            or not namespace_id.startswith("ns_")
        ):
            return _error("invalid_namespace_id", "namespace_id is invalid", 400)

        raw_items = payload.get("items")
        if not isinstance(raw_items, list) or not raw_items:
            return _error("items_required", "items must be a non-empty list", 400)
        if len(raw_items) > max_batch_items():
            return _error(
                "batch_too_large",
                f"items exceed the {max_batch_items()} item limit",
                413,
            )

        profile, allowed_labels, decisions = resolve_structured_value_policy(app_id)
        dictionary_version = registry.dictionary_version(app_id)
        items: list[tuple[str, str, str]] = []
        seen_ids: set[str] = set()
        total_characters = 0
        for raw_item in raw_items:
            if not isinstance(raw_item, dict):
                return _error(
                    "invalid_value_item", "each item must be an object", 400
                )
            item_id = raw_item.get("id")
            label = raw_item.get("label")
            value = raw_item.get("value")
            if (
                not isinstance(item_id, str)
                or not item_id
                or item_id in seen_ids
                or not is_structured_value_label(label)
                or not isinstance(value, str)
                or not value.strip()
            ):
                return _error(
                    "invalid_value_item",
                    "each item requires a unique non-empty id, label, and string value",
                    400,
                )
            if label not in allowed_labels:
                return _error(
                    "structured_value_label_denied",
                    "structured value label denied",
                    403,
                )
            if (limit := text_over_limit(value)) is not None:
                return _error(
                    "text_too_large",
                    f"item value exceeds the {limit} character limit",
                    413,
                )
            total_characters += len(value)
            if total_characters > max_text_chars():
                return _error(
                    "text_too_large",
                    f"item values exceed the {max_text_chars()} character limit",
                    413,
                )
            if reserved_placeholder_conflict(value):
                return _error(
                    "reserved_placeholder_conflict",
                    "input contains reserved placeholder syntax",
                    409,
                )
            seen_ids.add(item_id)
            items.append((item_id, label, value))

        blocked_labels = sorted(
            {label for _, label, _ in items if decisions[label] == BLOCK}
        )
        try:
            with namespace_vault.lock(namespace_id):
                state = namespace_vault.load(namespace_id, app_id=app_id)
                if state.status != "active":
                    return _error("namespace_inactive", "namespace is not active", 409)
                redactor = SessionRedactor([], profile)
                redactor.hydrate(state.entries)
                masked_items = [
                    (item_id, redactor.mask_value(value, label))
                    for item_id, label, value in items
                ]
                persisted_entries = redactor.mapping_entries()
                expected_counts = dict(
                    Counter(
                        placeholder
                        for _, masked_value in masked_items
                        for placeholder in PLACEHOLDER_RE.findall(str(masked_value))
                    )
                )
                handle = vault.create(
                    app_id=app_id,
                    scope="namespace",
                    entries=persisted_entries,
                    expected_counts=expected_counts,
                )
                namespace_vault.save(
                    replace(
                        state,
                        entries=persisted_entries,
                        updated_at=int(time.time()),
                        grant_handles=[*state.grant_handles, handle],
                        trace=[*state.trace, *redactor.trace_entries()],
                    )
                )
        except (FileNotFoundError, NamespaceMismatch, ValueError, KeyError):
            return _error("namespace_not_found", "namespace unavailable", 404)

        audit = {
            "event": "structured_value_redact",
            "item_count": len(items),
            "label_counts": dict(sorted(Counter(label for _, label, _ in items).items())),
            "placeholder_count": len(expected_counts),
            "raw_values_included": False,
        }
        _audit_namespace(
            "structured_value_redact",
            app_id,
            namespace_id,
            item_count=audit["item_count"],
            label_counts=audit["label_counts"],
            placeholder_count=audit["placeholder_count"],
            raw_values_included=False,
        )
        return {
            "status": "ok",
            "version": __version__,
            "scope": "namespace",
            "namespace_id": namespace_id,
            "identity_contract": "declared_exact",
            "egress_allowed": not blocked_labels,
            "blocked_labels": blocked_labels,
            "mapping_handle": handle,
            "items": [
                {"id": item_id, "masked_value": masked_value}
                for item_id, masked_value in masked_items
            ],
            "audit": audit,
            "protection_receipt": _protection_receipt(
                app_id=app_id,
                scope="namespace",
                profile=profile,
                dictionary_version=dictionary_version,
            ),
        }

    @app.post("/api/v1/refill")
    def refill(request: Request, payload: dict = Body(default={})) -> Any:
        app_id = authorize(request, payload.get("app_id"), "trusted_refill")
        if isinstance(app_id, JSONResponse):
            return app_id
        contract = payload.get("contract", "none")
        if contract not in registry.allowed_refill_contracts(app_id) or contract == "none":
            return _error("refill_contract_denied", "refill contract denied", 403)
        text = payload.get("text")
        handle = payload.get("mapping_handle")
        if not isinstance(text, str) or not isinstance(handle, str):
            return _error("invalid_request", "text and mapping_handle are required", 400)
        try:
            grant = vault.load(handle, app_id=app_id)
        except (FileNotFoundError, MappingGrantMismatch, ValueError, KeyError):
            return _error("mapping_handle_not_found", "mapping handle unavailable", 404)
        result = refill_text_contract(
            text,
            grant.entries,
            contract=contract,
            expected_counts=grant.expected_counts,
        )
        restored_count = (
            sum(text.count(entry.placeholder) for entry in grant.entries)
            if result.status == "ok"
            else 0
        )
        vault.write_refill_audit(
            app_id=app_id,
            handle=handle,
            contract=contract,
            status=result.status,
            error_types=result.error_types,
            restored_count=restored_count,
        )
        return {
            "version": __version__,
            **result.to_safe_dict(),
            "contract": contract,
            "audit": {
                "event": "trusted_refill",
                "status": result.status,
                "restored_count": restored_count,
                "raw_values_included": False,
            },
        }

    @app.post("/api/v1/refill/structured")
    def refill_structured(request: Request, payload: dict = Body(default={})) -> Any:
        app_id = authorize(request, payload.get("app_id"), "trusted_refill")
        if isinstance(app_id, JSONResponse):
            return app_id
        contract = payload.get("contract", "none")
        handle = payload.get("mapping_handle")
        if (
            not isinstance(contract, str)
            or not isinstance(handle, str)
            or "value" not in payload
        ):
            return _error(
                "invalid_request",
                "value, contract, and mapping_handle are required",
                400,
            )
        if (
            contract not in registry.allowed_refill_contracts(app_id)
            or contract == "none"
        ):
            return _error("refill_contract_denied", "refill contract denied", 403)
        try:
            grant = vault.load(handle, app_id=app_id)
        except (FileNotFoundError, MappingGrantMismatch, ValueError, KeyError):
            return _error(
                "mapping_handle_not_found", "mapping handle unavailable", 404
            )
        try:
            result = refill_structured_contract(
                payload["value"],
                grant.entries,
                contract=contract,
                expected_counts=grant.expected_counts,
                max_string_chars=max_text_chars(),
            )
        except StructuredRefillInvalid:
            return _error("invalid_request", "value must be JSON-safe", 400)
        except StructuredRefillTooLarge:
            return _error(
                "structure_too_large", "structured value exceeds limits", 413
            )

        vault.write_refill_audit(
            app_id=app_id,
            handle=handle,
            contract=contract,
            status=result.status,
            error_types=result.error_types,
            restored_count=result.restored_count,
        )
        return {
            "version": __version__,
            **result.to_safe_dict(),
            "contract": contract,
            "audit": {
                "event": "trusted_refill",
                "status": result.status,
                "restored_count": result.restored_count,
                "raw_values_included": False,
            },
        }

    @app.post("/api/v1/namespaces")
    def create_namespace(request: Request, payload: dict = Body(default={})) -> Any:
        app_id = authorize_namespace(request, payload.get("app_id"))
        if isinstance(app_id, JSONResponse):
            return app_id
        state = namespace_vault.create(app_id=app_id)
        _audit_namespace("namespace_create", app_id, state.namespace_id)
        return {
            "status": "ok",
            "version": __version__,
            "namespace_id": state.namespace_id,
            "namespace_status": state.status,
            "identity_contract": state.identity_contract,
        }

    @app.get("/api/v1/namespaces/{namespace_id}")
    def namespace_status(namespace_id: str, request: Request, app_id: str) -> Any:
        authorized = authorize_namespace(request, app_id)
        if isinstance(authorized, JSONResponse):
            return authorized
        try:
            state = namespace_vault.load(namespace_id, app_id=authorized)
        except (FileNotFoundError, NamespaceMismatch, ValueError, KeyError):
            return _error("namespace_not_found", "namespace unavailable", 404)
        return {
            "status": "ok",
            "version": __version__,
            "namespace_id": namespace_id,
            "namespace_status": state.status,
            "identity_contract": state.identity_contract,
            "placeholder_count": len(state.entries),
            "created_at": state.created_at,
            "updated_at": state.updated_at,
        }

    @app.post("/api/v1/namespaces/{namespace_id}/redact")
    def namespace_redact(
        namespace_id: str, request: Request, payload: dict = Body(default={})
    ) -> Any:
        app_id = authorize_namespace(request, payload.get("app_id"))
        if isinstance(app_id, JSONResponse):
            return app_id
        text = payload.get("text")
        if not isinstance(text, str) or not text.strip():
            return _error("text_required", "text is required", 400)
        if (limit := text_over_limit(text)) is not None:
            return _error("text_too_large", f"text exceeds the {limit} character limit", 413)
        if reserved_placeholder_conflict(text):
            return _error(
                "reserved_placeholder_conflict",
                "input contains reserved placeholder syntax",
                409,
            )
        try:
            with namespace_vault.lock(namespace_id):
                state = namespace_vault.load(namespace_id, app_id=app_id)
                if state.status != "active":
                    return _error("namespace_inactive", "namespace is not active", 409)
                profile, entries, dictionary_version = resolve(app_id)
                redactor = SessionRedactor(
                    build_detectors(
                        entries, dictionary_version=dictionary_version
                    ),
                    profile,
                )
                redactor.hydrate(state.entries)
                masked = redactor.mask(text)
                persisted_entries = redactor.mapping_entries()
                expected_counts = dict(Counter(PLACEHOLDER_RE.findall(masked)))
                # Mint INSIDE the lock so the grant matches the persisted
                # entries, and record the handle for revoke-on-delete.
                handle = vault.create(
                    app_id=app_id,
                    scope="namespace",
                    entries=persisted_entries,
                    expected_counts=expected_counts,
                )
                updated = replace(
                    state,
                    entries=persisted_entries,
                    updated_at=int(time.time()),
                    grant_handles=[*state.grant_handles, handle],
                    trace=[*state.trace, *redactor.trace_entries()],
                )
                namespace_vault.save(updated)
        except (FileNotFoundError, NamespaceMismatch, ValueError, KeyError):
            return _error("namespace_not_found", "namespace unavailable", 404)
        return {
            "status": "ok",
            "version": __version__,
            "namespace_id": namespace_id,
            "masked_text": masked,
            "mapping_handle": handle,
            "identity_contract": "canonical",
            "profile": dimensions_from_profile(profile).to_safe_dict(),
            "detectors": redactor.last_readiness.to_safe_dict()
            if redactor.last_readiness is not None
            else {},
            "blocked_labels": sorted(redactor.blocked_labels),
            "egress_allowed": not redactor.blocked_labels,
            "protection_receipt": _protection_receipt(
                app_id=app_id,
                scope="namespace",
                profile=profile,
                dictionary_version=dictionary_version,
                readiness_values=(redactor.last_readiness,),
            ),
        }

    @app.post("/api/v1/namespaces/{namespace_id}/archive")
    def archive_namespace(
        namespace_id: str, request: Request, payload: dict = Body(default={})
    ) -> Any:
        app_id = authorize_namespace(request, payload.get("app_id"))
        if isinstance(app_id, JSONResponse):
            return app_id
        try:
            state = namespace_vault.archive(namespace_id, app_id=app_id)
        except (FileNotFoundError, NamespaceMismatch, ValueError, KeyError):
            return _error("namespace_not_found", "namespace unavailable", 404)
        _audit_namespace("namespace_archive", app_id, namespace_id)
        return {
            "status": "ok",
            "version": __version__,
            "namespace_id": namespace_id,
            "namespace_status": state.status,
        }

    @app.delete("/api/v1/namespaces/{namespace_id}")
    def delete_namespace(namespace_id: str, request: Request, app_id: str) -> Any:
        authorized = authorize_namespace(request, app_id)
        if isinstance(authorized, JSONResponse):
            return authorized
        try:
            # Revoke every grant derived from this namespace (grants are
            # self-contained) before removing the namespace itself.
            deleted = namespace_vault.delete(
                namespace_id,
                app_id=authorized,
                revoke_handles=lambda handle: vault.store.delete(
                    MappingVault._key(handle)
                ),
            )
        except (FileNotFoundError, NamespaceMismatch, ValueError, KeyError):
            return _error("namespace_not_found", "namespace unavailable", 404)
        _audit_namespace("namespace_delete", authorized, namespace_id)
        return {"status": "ok", "version": __version__, "deleted": deleted}

    @app.post("/api/v1/namespaces/{namespace_id}/inspection-snapshots")
    def create_inspection_snapshot(
        namespace_id: str,
        request: Request,
        payload: dict = Body(default={}),
    ) -> Any:
        app_id = authorize(request, payload.get("app_id"), "mapping_inspect")
        if isinstance(app_id, JSONResponse):
            return app_id
        coverage = payload.get("coverage_items", [])
        if not isinstance(coverage, list) or len(coverage) > 1000:
            return _error("inspection_invalid", "coverage items are invalid", 400)
        total = 0
        normalized: list[dict[str, str]] = []
        for item in coverage:
            if not isinstance(item, dict):
                return _error("inspection_invalid", "coverage item is invalid", 400)
            item_id, text = item.get("id"), item.get("text")
            if not isinstance(item_id, str) or not isinstance(text, str):
                return _error("inspection_invalid", "coverage item is invalid", 400)
            total += len(text)
            if total > max_text_chars():
                return _error("inspection_too_large", "coverage items are too large", 413)
            normalized.append({"id": item_id, "text": text})
        try:
            state = namespace_vault.load(namespace_id, app_id=app_id)
            profile, entries, dictionary_version = resolve(app_id)
            snapshot = inspection_vault.create(
                app_id=app_id,
                project_id=payload.get("project_id"),
                run_id=payload.get("run_id"),
                scope=payload.get("scope", "prelim"),
                terminal_status=payload.get("terminal_status", "restored"),
                entries=state.entries,
                trace=state.trace,
                coverage_items=normalized,
                detectors=build_detectors(
                    entries, dictionary_version=dictionary_version
                ),
                profile=profile,
            )
        except (FileNotFoundError, NamespaceMismatch):
            return _error("namespace_not_found", "namespace unavailable", 404)
        except RequiredDetectorUnavailable:
            return _error(
                "required_detector_unavailable",
                "required detector unavailable",
                503,
            )
        except (ValueError, TypeError, KeyError):
            return _error("inspection_invalid", "inspection request is invalid", 400)
        _audit_namespace(
            "inspection_snapshot_create",
            app_id,
            namespace_id,
            inspection_id=snapshot["inspection_id"],
            entry_count=snapshot["entry_count"],
            generalization_count=snapshot["generalization_count"],
            coverage_finding_count=snapshot["coverage_finding_count"],
        )
        return {"status": "ok", "version": __version__, **snapshot}

    @app.post("/api/v1/inspections/{inspection_id}/query")
    def query_inspection(
        inspection_id: str,
        request: Request,
        payload: dict = Body(default={}),
    ) -> Any:
        app_id = authorize(request, payload.get("app_id"), "mapping_inspect")
        if isinstance(app_id, JSONResponse):
            return app_id
        try:
            result = inspection_vault.query(
                inspection_id,
                app_id=app_id,
                category=payload.get("category", "entries"),
                offset=payload.get("offset", 0),
                limit=payload.get("limit", 50),
                search=payload.get("search", ""),
            )
        except (FileNotFoundError, InspectionMismatch):
            return _error("inspection_not_found", "inspection unavailable", 404)
        except (ValueError, TypeError, KeyError):
            return _error("inspection_invalid", "inspection query is invalid", 400)
        return {"status": "ok", "version": __version__, **result}

    @app.delete("/api/v1/inspections/{inspection_id}")
    def delete_inspection(
        inspection_id: str,
        request: Request,
        app_id: str,
    ) -> Any:
        authorized = authorize(request, app_id, "mapping_inspect")
        if isinstance(authorized, JSONResponse):
            return authorized
        try:
            deleted = inspection_vault.delete(
                inspection_id, app_id=authorized
            )
        except (FileNotFoundError, InspectionMismatch):
            return _error("inspection_not_found", "inspection unavailable", 404)
        return {"status": "ok", "version": __version__, "deleted": deleted}

    return vault
