from __future__ import annotations

import pytest

from tuomin_gateway.detectors.adapters import FakeModelDetectorAdapter
from tuomin_gateway.detectors.openai_privacy_filter import (
    OpenAIPrivacyFilterAdapter,
    OpenAIPrivacyFilterUnavailable,
)
from tuomin_gateway.fusion import fuse_detections


def test_fake_adapter_normalizes_model_output_to_detection_span():
    detector = FakeModelDetectorAdapter(
        outputs=[
            {"start": 0, "end": 3, "label": "PERSON", "score": 0.91},
        ],
        source="openai_privacy_filter",
        version="fake-privacy-filter-test",
    )

    spans = detector.detect("abc synthetic")

    assert len(spans) == 1
    span = spans[0]
    assert span.start == 0
    assert span.end == 3
    assert span.label == "PERSON"
    assert span.confidence == 0.91
    assert span.source == "openai_privacy_filter"
    assert span.detector_version == "fake-privacy-filter-test"
    assert span.text_hash.startswith("sha256:")


def test_adapter_safe_output_contains_hash_without_raw_value():
    detector = FakeModelDetectorAdapter(
        outputs=[
            {"start": 4, "end": 10, "label": "ORG", "score": 0.88},
        ],
    )

    span = detector.detect("safeACMECOtail")[0]
    payload = span.to_safe_dict()

    assert payload["text_hash"].startswith("sha256:")
    assert "text" not in payload
    assert "value" not in payload
    assert "ACMECO" not in repr(payload)


def test_fusion_accepts_adapter_span_and_keeps_rule_priority_stable():
    text = "abc123456789def"
    model_span = FakeModelDetectorAdapter(
        outputs=[{"start": 3, "end": 12, "label": "PERSON", "score": 0.99}],
        source="openai_privacy_filter",
    ).detect(text)[0]
    rule_span = FakeModelDetectorAdapter(
        outputs=[{"start": 5, "end": 10, "label": "ID_CARD", "score": 0.96}],
        source="rule",
    ).detect(text)[0]

    fused = fuse_detections([model_span, rule_span], text)

    assert [(span.start, span.end, span.source) for span in fused] == [
        (3, 5, "openai_privacy_filter"),
        (5, 10, "rule"),
        (10, 12, "openai_privacy_filter"),
    ]


def test_openai_privacy_filter_adapter_returns_empty_when_unconfigured():
    detector = OpenAIPrivacyFilterAdapter()

    assert detector.detect("synthetic text only") == []


def test_openai_privacy_filter_adapter_raises_when_enabled_without_runtime():
    detector = OpenAIPrivacyFilterAdapter(enabled=True, model_path="synthetic-local-placeholder")

    with pytest.raises(OpenAIPrivacyFilterUnavailable):
        detector.detect("synthetic text")
