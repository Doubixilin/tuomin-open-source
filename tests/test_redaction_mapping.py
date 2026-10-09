from pathlib import Path

from tuomin_gateway.detectors.dictionary import DictionaryDetector
from tuomin_gateway.detectors.rules import RuleDetector
from tuomin_gateway.fusion import fuse_detections
from tuomin_gateway.redactor import redact_text
from tuomin_gateway.refill import refill_text
from tuomin_gateway.schemas import DetectionSpan


FIXTURE = Path(__file__).parent / "fixtures" / "synthetic_dictionary.json"


def test_repeated_entity_uses_same_placeholder():
    text = "示建A负责测试绿洲项目。示建A再次确认测试绿洲项目。"
    detections = DictionaryDetector.from_json(FIXTURE).detect(text)

    result = redact_text(text, detections, task_id="task_repeat")

    assert result.redacted_text.count("<ORG_001>") == 2
    assert result.redacted_text.count("<PROJECT_001>") == 2
    assert len({entry.placeholder for entry in result.mapping if entry.label == "ORG"}) == 1


def test_document_alias_refill_preserves_each_surface_form():
    text = "示建A与示例甲方共同确认。"
    detections = DictionaryDetector.from_json(FIXTURE).detect(text)

    result = redact_text(text, detections, task_id="task_alias_surface")
    restored = refill_text(result.redacted_text, result.mapping)

    assert result.redacted_text == "<ORG_001>与<ORG_002>共同确认。"
    assert restored.status == "ok"
    assert restored.text == text


def test_fusion_prefers_dictionary_span_over_overlapping_rule_span():
    text = "合同编号：HT-2026-TM-0001属于示建A。"
    rule_spans = RuleDetector().detect(text)
    dictionary_spans = DictionaryDetector.from_entries(
        [
            {
                "canonical_value": "合成合同别名",
                "aliases": ["HT-2026-TM-0001"],
                "label": "PROJECT",
                "risk_level": "high",
                "version": "test",
            }
        ]
    ).detect(text)

    fused = fuse_detections(rule_spans + dictionary_spans, text)

    assert any(span.label == "PROJECT" for span in fused)
    assert not any(span.label == "CONTRACT_ID" and text[span.start : span.end] == "HT-2026-TM-0001" for span in fused)


def test_synthetic_end_to_end_redaction_contains_no_original_values():
    text = (
        "示例建设单位A与合成供应商乙就测试绿洲项目签署合同，"
        "合同编号：HT-2026-TM-0001，金额为人民币123,456.78元。"
    )
    detections = fuse_detections(
        RuleDetector().detect(text) + DictionaryDetector.from_json(FIXTURE).detect(text), text
    )

    result = redact_text(text, detections, task_id="task_e2e")

    assert "<ORG_001>" in result.redacted_text
    assert "<SUPPLIER_001>" in result.redacted_text
    assert "<PROJECT_001>" in result.redacted_text
    assert "<CONTRACT_ID_001>" in result.redacted_text
    assert "示例建设单位A" not in result.redacted_text
    assert "HT-2026-TM-0001" not in result.redacted_text


def test_redactor_handles_empty_detections_without_mapping():
    result = redact_text("仅合成公开文本", [], task_id="task_empty")

    assert result.redacted_text == "仅合成公开文本"
    assert result.mapping == []
    assert result.risk_summary["placeholder_count"] == 0


def test_redactor_deduplicates_repeated_identical_span():
    detection = DetectionSpan(
        start=0,
        end=4,
        label="ORG",
        confidence=0.99,
        source="dictionary",
        detector_version="test",
        text_hash="sha256:org",
    )

    result = redact_text("合成甲方确认", [detection, detection], task_id="task_duplicate_span")

    assert result.redacted_text == "<ORG_001>确认"
    assert [entry.placeholder for entry in result.mapping] == ["<ORG_001>"]


def test_redactor_preserves_adjacent_span_order():
    detections = [
        DetectionSpan(
            start=0,
            end=4,
            label="ORG",
            confidence=0.99,
            source="dictionary",
            detector_version="test",
            text_hash="sha256:org",
        ),
        DetectionSpan(
            start=4,
            end=7,
            label="PROJECT",
            confidence=0.99,
            source="dictionary",
            detector_version="test",
            text_hash="sha256:project",
        ),
    ]

    result = redact_text("合成甲方项目A", detections, task_id="task_adjacent")

    assert result.redacted_text == "<ORG_001><PROJECT_001>"
