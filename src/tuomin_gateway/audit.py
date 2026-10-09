"""Safe audit construction and durable audit persistence.

Two responsibilities, one safety rule — never persist raw sensitive values:

- ``build_audit_event`` / ``build_alert_event`` construct AuditEvent /
  AlertEvent payloads that carry only counts, labels and keyed (HMAC) hashes.
- ``AuditLog`` is the single durable writer every security-relevant event goes
  through (guard alerts, trusted refill, namespace lifecycle, admin
  mutations): owner-only directory/files, append + fsync, one
  ``<stream>.jsonl`` per stream.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import time
from collections import Counter
from pathlib import Path

from tuomin_gateway.schemas import (
    AlertEvent,
    AuditEvent,
    DetectionSpan,
    MappingEntry,
    RefillResult,
)
from tuomin_gateway.store import _restrict_permissions

# Keyed hash so an alert log never lets an attacker brute-force/rainbow-table a
# matched value back from its hash (an unsalted SHA-256 of a weak password IS the
# password). The key is read from the environment for a stable, deployment-wide
# correlation key, or generated per-process (correlatable within a run, useless
# across runs and not pre-computable). It is never logged.
_HMAC_KEY = (os.environ.get("TUOMIN_ALERT_HMAC_KEY") or secrets.token_hex(32)).encode("utf-8")


def build_audit_event(
    task_id: str,
    detections: list[DetectionSpan],
    mapping: list[MappingEntry],
    refill_result: RefillResult | None = None,
    versions: dict[str, str] | None = None,
) -> AuditEvent:
    return AuditEvent(
        task_id=task_id,
        detection_count=len(detections),
        label_counts=dict(sorted(Counter(span.label for span in detections).items())),
        source_counts=dict(sorted(Counter(span.source for span in detections).items())),
        placeholder_count=len(mapping),
        refill_status=refill_result.status if refill_result else None,
        error_types=refill_result.error_types if refill_result else [],
        versions=versions or _default_versions(),
    )


def _default_versions() -> dict[str, str]:
    # Sourced from the package + detector so audit provenance can't drift from a
    # stale literal.
    from tuomin_gateway import __version__
    from tuomin_gateway.detectors.rules import RuleDetector

    return {"gateway": __version__, "rules": RuleDetector.version, "dictionary": "synthetic"}


def _hash_matched(value: str) -> str:
    """Keyed (HMAC) hash of a matched span — a correlation token only, not a value.
    Unlike a bare hash it cannot be reversed by guessing/rainbow-tabling without
    the per-deployment key."""
    return hmac.new(_HMAC_KEY, value.encode("utf-8"), hashlib.sha256).hexdigest()[:16]


def build_alert_event(
    *,
    alert_type: str,
    severity: str,
    direction: str,
    action: str,
    source: str,
    detector: str,
    matched_values: list[str] | None = None,
    label_counts: dict[str, int] | None = None,
    detector_version: str = "",
    task_id: str | None = None,
    confidence: float | None = None,
    risk_level: str = "unknown",
    summary: str = "",
    store_hashes: bool = True,
) -> AlertEvent:
    """Construct an AlertEvent that carries NO raw sensitive text.

    ``matched_values`` are keyed-hashed (never stored in clear) and only their
    count + per-label counts survive. For low-entropy / guessable matches (e.g.
    the generic password pattern) the caller passes ``store_hashes=False`` so not
    even a keyed hash is kept — only the count.
    """
    values = matched_values or []
    return AlertEvent(
        alert_type=alert_type,
        severity=severity,
        direction=direction,
        action=action,
        source=source,
        detector=detector,
        detector_version=detector_version,
        task_id=task_id,
        span_count=len(values),
        label_counts=dict(sorted((label_counts or {}).items())),
        matched_hashes=[_hash_matched(v) for v in values] if store_hashes else [],
        confidence=confidence,
        risk_level=risk_level,
        summary=summary,
    )


# Durable audit streams (one ``<stream>.jsonl`` file per stream). Every
# security-relevant event the gateway persists goes through ``AuditLog`` so the
# durability rules live in exactly one place.
AUDIT_STREAM_REFILL = "trusted-refill"
AUDIT_STREAM_GUARD = "guard-alerts"
AUDIT_STREAM_NAMESPACE = "namespace"
AUDIT_STREAM_ADMIN = "admin"

_STREAM_NAME_RE = re.compile(r"[a-z][a-z0-9-]*")


class AuditLog:
    """Durable, owner-only JSONL security audit — one file per event stream.

    The ONLY persistence properties guaranteed here: owner-only directory and
    files, append + fsync, and a ``timestamp`` added to every event. Callers
    must pass safe event dicts (handle hashes, counts, event types) — this class
    cannot enforce the no-raw-value rule, it is upheld at every build site
    (``build_alert_event`` / refill audit / lifecycle events). Audit failures
    must never break the data path; callers wrap writes in ``except OSError``.
    """

    def __init__(self, directory: str | Path) -> None:
        self.directory = Path(directory)

    def write(self, stream: str, event: dict) -> None:
        """Append one event to ``<directory>/<stream>.jsonl`` (owner-only)."""
        if not _STREAM_NAME_RE.fullmatch(stream):
            raise ValueError(f"invalid audit stream name: {stream!r}")
        self.directory.mkdir(parents=True, exist_ok=True)
        _restrict_permissions(self.directory, is_dir=True)
        path = self.directory / f"{stream}.jsonl"
        record = {"timestamp": int(time.time()), **event}
        fd = os.open(path, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600)
        try:
            os.write(fd, (json.dumps(record, ensure_ascii=False) + "\n").encode("utf-8"))
            os.fsync(fd)
        finally:
            os.close(fd)
        _restrict_permissions(path, is_dir=False)
