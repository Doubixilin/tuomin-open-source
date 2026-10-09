from __future__ import annotations

from dataclasses import asdict, dataclass, field
from hashlib import sha256
from typing import Any, Literal


def hash_text(value: str) -> str:
    """Canonical sha256 token for a matched value (correlation only).

    Lives in the leaf ``schemas`` module: ``spans`` (span geometry) needs it
    too, and importing it from ``detectors.base`` made ``spans`` pull in the
    whole ``detectors`` package — whose ``dictionary`` module imports
    ``spans`` back (circular import). ``detectors.base`` re-exports this for
    its existing callers.
    """
    return f"sha256:{sha256(value.encode('utf-8')).hexdigest()}"


@dataclass(frozen=True)
class DetectionSpan:
    start: int
    end: int
    label: str
    confidence: float
    source: str
    detector_version: str
    text_hash: str
    risk_level: str = "unknown"
    metadata: dict[str, Any] = field(default_factory=dict)

    def length(self) -> int:
        return self.end - self.start

    def to_safe_dict(self) -> dict[str, Any]:
        return {
            "start": self.start,
            "end": self.end,
            "label": self.label,
            "confidence": self.confidence,
            "source": self.source,
            "detector_version": self.detector_version,
            "text_hash": self.text_hash,
            "risk_level": self.risk_level,
        }


@dataclass(frozen=True)
class MappingEntry:
    placeholder: str
    label: str
    original_value: str
    text_hash: str

    def to_dict(self) -> dict[str, str]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, str]) -> "MappingEntry":
        return cls(
            placeholder=data["placeholder"],
            label=data["label"],
            original_value=data["original_value"],
            text_hash=data["text_hash"],
        )


@dataclass(frozen=True)
class RedactionTraceEntry:
    """Encrypted-only explanation of one redaction decision.

    This object may contain the original value and therefore must never be
    returned by the normal redact/proxy APIs or written to audit logs.  It is
    persisted only inside namespace/proxy-session vault state and copied into
    an authorized inspection snapshot.
    """

    label: str
    original_value: str
    redacted_value: str
    action: str
    source: str
    detector_version: str
    text_hash: str

    def to_dict(self) -> dict[str, str]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, str]) -> "RedactionTraceEntry":
        return cls(
            label=data["label"],
            original_value=data["original_value"],
            redacted_value=data["redacted_value"],
            action=data["action"],
            source=data["source"],
            detector_version=data["detector_version"],
            text_hash=data["text_hash"],
        )


@dataclass
class RedactionResult:
    task_id: str
    redacted_text: str
    mapping_id: str
    detections: list[DetectionSpan]
    mapping: list[MappingEntry]
    risk_summary: dict[str, Any]

    def to_safe_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "redacted_text": self.redacted_text,
            "mapping_id": self.mapping_id,
            "risk_summary": self.risk_summary,
            "placeholder_count": len(self.mapping),
        }


@dataclass
class RefillResult:
    status: Literal["ok", "blocked"]
    text: str
    error_types: list[str] = field(default_factory=list)
    unknown_placeholders: list[str] = field(default_factory=list)
    missing_placeholders: list[str] = field(default_factory=list)
    altered_placeholders: list[str] = field(default_factory=list)

    def to_safe_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "text": self.text if self.status == "ok" else None,
            "error_types": self.error_types,
            "unknown_placeholders": self.unknown_placeholders,
            "missing_placeholders": self.missing_placeholders,
            "altered_placeholders": self.altered_placeholders,
        }


@dataclass
class AuditEvent:
    task_id: str
    detection_count: int
    label_counts: dict[str, int]
    source_counts: dict[str, int]
    placeholder_count: int
    refill_status: str | None = None
    error_types: list[str] = field(default_factory=list)
    versions: dict[str, str] = field(default_factory=dict)

    def to_safe_dict(self) -> dict[str, Any]:
        return asdict(self)


# Severity ladder for guard/alert events (pillars 5 & 6). info < warn < critical.
SEVERITIES = ("info", "warn", "critical")
SEVERITY_RANK = {name: rank for rank, name in enumerate(SEVERITIES)}
RANK_TO_SEVERITY = {rank: name for name, rank in SEVERITY_RANK.items()}


@dataclass(frozen=True)
class AlertEvent:
    """A guard/inspection finding raised on the input or output side.

    Like AuditEvent, this NEVER carries raw sensitive text — only label counts,
    salted-free hashes of the matched spans, and counts. ``to_safe_dict`` is the
    only serialization and is safe to log, header-encode, or show in the UI.
    """

    alert_type: str          # pii | secret | injection | hallucinated_placeholder | reidentified
    severity: str            # info | warn | critical
    direction: str           # input | output
    action: str              # warn | block | redact
    source: str              # signature | model | dictionary | entropy | placeholder
    detector: str
    detector_version: str = ""
    task_id: str | None = None
    span_count: int = 0
    label_counts: dict[str, int] = field(default_factory=dict)
    matched_hashes: list[str] = field(default_factory=list)
    confidence: float | None = None
    risk_level: str = "unknown"
    summary: str = ""

    def to_safe_dict(self) -> dict[str, Any]:
        return {
            "alert_type": self.alert_type,
            "severity": self.severity,
            "direction": self.direction,
            "action": self.action,
            "source": self.source,
            "detector": self.detector,
            "detector_version": self.detector_version,
            "task_id": self.task_id,
            "span_count": self.span_count,
            "label_counts": self.label_counts,
            "matched_hashes": self.matched_hashes,
            "confidence": self.confidence,
            "risk_level": self.risk_level,
            "summary": self.summary,
        }
