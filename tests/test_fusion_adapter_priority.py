from __future__ import annotations

from tuomin_gateway.detectors.adapters import FakeModelDetectorAdapter
from tuomin_gateway.fusion import fuse_detections


def _adapter_span(text: str, start: int, end: int, source: str, score: float = 0.99):
    return FakeModelDetectorAdapter(
        outputs=[{"start": start, "end": end, "label": "PERSON", "score": score}],
        source=source,
        version=f"{source}-test",
    ).detect(text)[0]


def test_fusion_keeps_dictionary_rule_model_conflict_priority_stable():
    text = "abcdefghij"
    dictionary = _adapter_span(text, 2, 8, "dictionary", score=0.99)
    high_conf_rule = _adapter_span(text, 0, 10, "rule", score=0.97)
    model = _adapter_span(text, 1, 9, "openai_privacy_filter", score=0.99)

    fused = fuse_detections([model, high_conf_rule, dictionary], text)

    assert [(span.start, span.end, span.source) for span in fused] == [
        (0, 2, "rule"),
        (2, 8, "dictionary"),
        (8, 10, "rule"),
    ]
