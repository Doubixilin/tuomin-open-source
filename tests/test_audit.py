"""Audit-event construction tests (merged from the former test_alert_event.py).

The non-negotiable invariant: neither an AuditEvent nor an AlertEvent EVER
carries raw sensitive text — only hashes, counts, and metadata. Durable
persistence (AuditLog) lives in test_durable_audit.py.
"""
from pathlib import Path

from tuomin_gateway.audit import build_alert_event, build_audit_event
from tuomin_gateway.detectors.dictionary import DictionaryDetector
from tuomin_gateway.detectors.rules import RuleDetector
from tuomin_gateway.fusion import fuse_detections
from tuomin_gateway.redactor import redact_text
from tuomin_gateway.refill import refill_text
from tuomin_gateway.schemas import SEVERITY_RANK, AlertEvent


FIXTURE = Path(__file__).parent / "fixtures" / "synthetic_dictionary.json"


def test_audit_summary_does_not_include_raw_sensitive_values():
    text = "示例建设单位A签署合同编号：HT-2026-TM-0001，联系人电话：13800138000。"
    detections = fuse_detections(
        RuleDetector().detect(text) + DictionaryDetector.from_json(FIXTURE).detect(text), text
    )
    redaction = redact_text(text, detections, task_id="task_audit")
    refill = refill_text(redaction.redacted_text, redaction.mapping)

    audit = build_audit_event(
        task_id="task_audit",
        detections=detections,
        mapping=redaction.mapping,
        refill_result=refill,
    )
    audit_payload = audit.to_safe_dict()

    assert audit_payload["task_id"] == "task_audit"
    assert audit_payload["detection_count"] >= 3
    assert audit_payload["placeholder_count"] >= 3
    assert "示例建设单位A" not in str(audit_payload)
    assert "HT-2026-TM-0001" not in str(audit_payload)
    assert "13800138000" not in str(audit_payload)


def test_alert_event_carries_no_raw_values():
    secret = "sk-live_SUPERSECRET_0123456789"
    event = build_alert_event(
        alert_type="secret",
        severity="critical",
        direction="input",
        action="warn",
        source="signature",
        detector="builtin-secrets",
        matched_values=[secret, "13900001111"],
        label_counts={"CREDENTIAL": 1, "CONTACT": 1},
        task_id="task_1",
        summary="2 secrets in input",
    )
    safe = event.to_safe_dict()
    blob = repr(safe)

    assert secret not in blob
    assert "13900001111" not in blob
    assert safe["span_count"] == 2
    assert len(safe["matched_hashes"]) == 2
    assert all(len(h) == 16 for h in safe["matched_hashes"])
    assert safe["label_counts"] == {"CONTACT": 1, "CREDENTIAL": 1}
    assert safe["severity"] == "critical"
    assert safe["action"] == "warn"
    assert safe["direction"] == "input"


def test_alert_event_hash_is_stable_and_distinct():
    a = build_alert_event(
        alert_type="pii", severity="warn", direction="output",
        action="warn", source="model", detector="ner", matched_values=["王海洋"],
    )
    b = build_alert_event(
        alert_type="pii", severity="warn", direction="output",
        action="warn", source="model", detector="ner", matched_values=["王海洋"],
    )
    c = build_alert_event(
        alert_type="pii", severity="warn", direction="output",
        action="warn", source="model", detector="ner", matched_values=["李雪梅"],
    )
    assert a.matched_hashes == b.matched_hashes
    assert a.matched_hashes != c.matched_hashes


def test_alert_event_can_omit_hashes_for_low_entropy_values():
    # Low-entropy / guessable matches keep only a count — not even a keyed hash.
    event = build_alert_event(
        alert_type="secret", severity="critical", direction="input",
        action="warn", source="entropy", detector="builtin-secrets",
        matched_values=["password123"], store_hashes=False,
    )
    safe = event.to_safe_dict()
    assert safe["span_count"] == 1
    assert safe["matched_hashes"] == []
    assert "password123" not in repr(safe)


def test_alert_hash_is_keyed_not_bare_sha256():
    import hashlib
    from tuomin_gateway.audit import _hash_matched
    value = "AKIAIOSFODNN7EXAMPLE"
    assert _hash_matched(value) != hashlib.sha256(value.encode()).hexdigest()[:16]


def test_severity_rank_orders_info_warn_critical():
    assert SEVERITY_RANK["info"] < SEVERITY_RANK["warn"] < SEVERITY_RANK["critical"]


def test_alert_event_empty_matches_is_safe():
    event = AlertEvent(
        alert_type="injection", severity="warn", direction="input",
        action="warn", source="signature", detector="builtin-injection",
    )
    safe = event.to_safe_dict()
    assert safe["span_count"] == 0
    assert safe["matched_hashes"] == []
    assert safe["label_counts"] == {}
