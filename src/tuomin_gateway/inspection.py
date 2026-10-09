"""Encrypted, app-scoped redaction inspection snapshots.

Snapshots are deliberately separate from refill grants.  They explain what a
local workflow redacted, generalized, or may have missed without extending the
lifetime of a namespace or granting refill authority.
"""
from __future__ import annotations

from collections import Counter, defaultdict
import hashlib
import json
import re
import secrets
import time
from typing import Any, Iterable

from tuomin_gateway.placeholders import PLACEHOLDER_RE
from tuomin_gateway.profiles import Profile
from tuomin_gateway.schemas import MappingEntry, RedactionTraceEntry
from tuomin_gateway.session import RequiredDetectorUnavailable, run_detection
from tuomin_gateway.store import MappingStore


_IDENTIFIER_RE = re.compile(r"[A-Za-z0-9._:-]{1,200}")
_CATEGORIES = {"entries", "generalizations", "coverage_findings"}


class InspectionMismatch(PermissionError):
    pass


def _identifier(value: object, *, field: str) -> str:
    if not isinstance(value, str) or _IDENTIFIER_RE.fullmatch(value) is None:
        raise ValueError(f"invalid {field}")
    return value


class InspectionVault:
    def __init__(self, store: MappingStore) -> None:
        self.store = store

    @staticmethod
    def _key(inspection_id: str) -> str:
        return f"inspection:{inspection_id}"

    def create(
        self,
        *,
        app_id: str,
        project_id: str,
        run_id: str,
        scope: str,
        terminal_status: str,
        entries: Iterable[MappingEntry],
        trace: Iterable[RedactionTraceEntry],
        coverage_items: list[dict[str, str]],
        detectors: list[Any],
        profile: Profile,
        inspection_id: str | None = None,
        coverage_complete: bool = True,
    ) -> dict[str, Any]:
        app_id = _identifier(app_id, field="app_id")
        project_id = _identifier(project_id, field="project_id")
        run_id = _identifier(run_id, field="run_id")
        scope = _identifier(scope, field="scope")
        terminal_status = _identifier(terminal_status, field="terminal_status")
        existing: dict[str, Any] | None = None
        if inspection_id is not None:
            inspection_id = _identifier(inspection_id, field="inspection_id")
            existing = self.load(inspection_id, app_id=app_id)
            if (
                existing.get("project_id") != project_id
                or existing.get("run_id") != run_id
                or existing.get("scope") != scope
                or existing.get("terminal_status") != "active"
            ):
                raise InspectionMismatch("inspection cannot be updated")

        sources: dict[str, set[str]] = defaultdict(set)
        occurrences: Counter[str] = Counter()
        normalized_coverage: list[tuple[str, str]] = []
        for item in coverage_items:
            item_id = _identifier(item.get("id"), field="coverage item id")
            text = item.get("text")
            if not isinstance(text, str):
                raise ValueError("invalid coverage item text")
            normalized_coverage.append((item_id, text))
            for placeholder in PLACEHOLDER_RE.findall(text):
                sources[placeholder].add(item_id)
                occurrences[placeholder] += text.count(placeholder)

        trace_by_placeholder: dict[str, list[RedactionTraceEntry]] = defaultdict(list)
        trace_items = list(trace)
        for item in trace_items:
            if item.action == "redact" and PLACEHOLDER_RE.fullmatch(item.redacted_value):
                trace_by_placeholder[item.redacted_value].append(item)
        mapping_rows = []
        for entry in entries:
            decisions = trace_by_placeholder.get(entry.placeholder, [])
            decision_sources = sorted({item.source for item in decisions})
            detector_versions = sorted(
                {item.detector_version for item in decisions if item.detector_version}
            )
            mapping_rows.append(
                {
                    **entry.to_dict(),
                    "source_ids": sorted(sources.get(entry.placeholder, set())),
                    "occurrence_count": int(occurrences.get(entry.placeholder, 0)),
                    "source": "+".join(decision_sources) if decision_sources else "unknown",
                    "detector_version": (
                        "+".join(detector_versions) if detector_versions else ""
                    ),
                }
            )

        generalization_rows: list[dict[str, Any]] = []
        seen_generalizations: set[tuple[str, str, str]] = set()
        for item in trace_items:
            if item.action != "generalize":
                continue
            key = (item.label, item.original_value, item.redacted_value)
            if key in seen_generalizations:
                continue
            seen_generalizations.add(key)
            matching_sources = sorted(
                item_id
                for item_id, text in normalized_coverage
                if item.redacted_value in text
            )
            generalization_rows.append(
                {
                    **item.to_dict(),
                    "source_ids": matching_sources,
                    "occurrence_count": sum(
                        text.count(item.redacted_value)
                        for _, text in normalized_coverage
                    ),
                }
            )

        coverage_rows: list[dict[str, Any]] = []
        if coverage_complete:
            seen_findings: set[tuple[str, str, str]] = set()
            for item_id, text in normalized_coverage:
                placeholder_ranges = [
                    match.span() for match in PLACEHOLDER_RE.finditer(text)
                ]
                try:
                    detection = run_detection(text, detectors, profile)
                except RequiredDetectorUnavailable:
                    raise
                for span in detection.kept:
                    candidate = text[span.start : span.end]
                    if (
                        not candidate
                        or PLACEHOLDER_RE.fullmatch(candidate)
                        or any(
                            max(span.start, start) < min(span.end, end)
                            for start, end in placeholder_ranges
                        )
                    ):
                        continue
                    key = (item_id, span.label, candidate)
                    if key in seen_findings:
                        continue
                    seen_findings.add(key)
                    coverage_rows.append(
                        {
                            "source_id": item_id,
                            "label": span.label,
                            "candidate_value": candidate,
                            "source": span.source,
                            "detector_version": span.detector_version,
                            "text_hash": span.text_hash,
                            "reason": "sensitive_candidate_remains_in_provider_view",
                        }
                    )

        inspection_id = inspection_id or f"insp_{secrets.token_urlsafe(24)}"
        now = int(time.time())
        payload = {
            "schema_version": "tuomin-inspection-v1",
            "inspection_id": inspection_id,
            "app_id": app_id,
            "project_id": project_id,
            "run_id": run_id,
            "scope": scope,
            "terminal_status": terminal_status,
            "created_at": existing.get("created_at", now) if existing else now,
            "updated_at": now,
            "revision": int(existing.get("revision", 0)) + 1 if existing else 1,
            "coverage_complete": bool(coverage_complete),
            "entries": mapping_rows,
            "generalizations": generalization_rows,
            "coverage_findings": coverage_rows,
        }
        path = self.store.save_payload(
            self._key(inspection_id),
            json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8"),
        )
        summary = {
            "inspection_id": inspection_id,
            "schema_version": payload["schema_version"],
            "scope": scope,
            "terminal_status": terminal_status,
            "entry_count": len(mapping_rows),
            "generalization_count": len(generalization_rows),
            "coverage_finding_count": len(coverage_rows),
            "coverage_complete": bool(coverage_complete),
            "updated_at": now,
        }
        if terminal_status != "active":
            summary["ciphertext_sha256"] = (
                "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()
            )
        return summary

    def load(self, inspection_id: str, *, app_id: str) -> dict[str, Any]:
        _identifier(inspection_id, field="inspection_id")
        payload = json.loads(
            self.store.load_payload(self._key(inspection_id)).decode("utf-8")
        )
        if payload.get("app_id") != app_id:
            raise InspectionMismatch("inspection belongs to another app")
        if payload.get("schema_version") != "tuomin-inspection-v1":
            raise ValueError("unsupported inspection schema")
        return payload

    def query(
        self,
        inspection_id: str,
        *,
        app_id: str,
        category: str,
        offset: int,
        limit: int,
        search: str = "",
    ) -> dict[str, Any]:
        if category not in _CATEGORIES:
            raise ValueError("invalid inspection category")
        if offset < 0 or not 1 <= limit <= 100:
            raise ValueError("invalid inspection page")
        if len(search) > 200 or "\x00" in search:
            raise ValueError("invalid inspection search")
        payload = self.load(inspection_id, app_id=app_id)
        rows = payload.get(category)
        if not isinstance(rows, list):
            raise ValueError("invalid inspection payload")
        needle = search.casefold().strip()
        if needle:
            rows = [
                row
                for row in rows
                if needle in json.dumps(row, ensure_ascii=False).casefold()
            ]
        return {
            "inspection_id": inspection_id,
            "schema_version": payload["schema_version"],
            "project_id": payload["project_id"],
            "run_id": payload["run_id"],
            "scope": payload["scope"],
            "terminal_status": payload["terminal_status"],
            "coverage_complete": bool(payload.get("coverage_complete", True)),
            "updated_at": int(payload.get("updated_at", payload.get("created_at", 0))),
            "category": category,
            "offset": offset,
            "limit": limit,
            "total": len(rows),
            "items": rows[offset : offset + limit],
        }

    def delete(self, inspection_id: str, *, app_id: str) -> bool:
        self.load(inspection_id, app_id=app_id)
        return self.store.delete(self._key(inspection_id))


__all__ = ["InspectionMismatch", "InspectionVault"]
