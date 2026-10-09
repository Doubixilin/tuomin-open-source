from tuomin_gateway.fusion import fuse_detections
from tuomin_gateway.schemas import DetectionSpan


def span(
    start: int,
    end: int,
    label: str,
    source: str = "dictionary",
    confidence: float = 0.9,
) -> DetectionSpan:
    return DetectionSpan(
        start=start,
        end=end,
        label=label,
        confidence=confidence,
        source=source,
        detector_version="test",
        text_hash=f"sha256:{label}",
    )


TEXT = "abcdefghij"


def test_fusion_same_start_tie_break_is_independent_of_input_order():
    left = span(0, 4, "ORG")
    right = span(0, 4, "PROJECT")

    assert fuse_detections([left, right], TEXT)[0].label == "ORG"
    assert fuse_detections([right, left], TEXT)[0].label == "ORG"


def test_fusion_prefers_longer_span_for_same_start_overlap():
    shorter = span(0, 4, "ORG")
    longer = span(0, 8, "ORG")

    fused = fuse_detections([shorter, longer], TEXT)

    assert fused == [longer]


def test_fusion_clips_loser_instead_of_dropping_partial_overlap():
    # High-priority dictionary span [2,5) overlaps a lower-priority model span
    # [3,9): the model span must survive CLIPPED to [5,9), not be dropped (which
    # would leak chars 5-9 as plaintext).
    dict_span = span(2, 5, "ORG", source="dictionary")
    model_span = span(3, 9, "ORG", source="model")

    fused = fuse_detections([dict_span, model_span], TEXT)

    spans = sorted((s.start, s.end) for s in fused)
    assert spans == [(2, 5), (5, 9)]


def test_fusion_drops_containing_lower_confidence_model_alternative():
    winner = span(0, 4, "ORG", source="model", confidence=0.95)
    containing_alternative = span(0, 5, "ORG", source="model", confidence=0.8)

    assert fuse_detections([containing_alternative, winner], TEXT) == [winner]


def test_fusion_drops_shifted_contained_model_alternative():
    winner = span(0, 10, "ORG", source="model", confidence=0.95)
    shifted_alternative = span(1, 10, "ORG", source="model", confidence=0.8)

    assert fuse_detections([shifted_alternative, winner], TEXT) == [winner]


def test_fusion_prefers_longer_model_surface_over_contained_cross_label_noise():
    text = "苏州万和置业有限公司"
    company = span(0, len(text), "ORG", source="model", confidence=0.90)
    city_noise = span(0, len("苏州"), "ADDRESS", source="model", confidence=0.99)

    assert fuse_detections([city_noise, company], text) == [company]


def test_fusion_keeps_dictionary_and_rule_ahead_of_model_for_same_span():
    model_span = span(0, 4, "ORG", source="model")
    dictionary_span = span(0, 4, "ORG", source="dictionary")
    rule_span = span(0, 4, "ORG", source="rule")

    assert fuse_detections([model_span, dictionary_span], TEXT) == [dictionary_span]
    assert fuse_detections([model_span, rule_span], TEXT) == [rule_span]


def _span_with_metadata(
    start: int, end: int, label: str, source: str, metadata: dict
) -> DetectionSpan:
    return DetectionSpan(
        start=start,
        end=end,
        label=label,
        confidence=0.9,
        source=source,
        detector_version="test",
        text_hash=f"sha256:{label}",
        metadata=metadata,
    )


def test_fusion_clipped_fragment_does_not_keep_full_canonical_value():
    # A canonical-identity refill trusts metadata["canonical_value"]; a clipped
    # fragment must NOT inherit the unclipped span's full canonical name (which
    # would expand the fragment back into the whole name on refill).
    winner = _span_with_metadata(3, 5, "ORG", "manual", {"canonical_value": "de"})
    loser = _span_with_metadata(0, 9, "ORG", "dictionary", {"canonical_value": "某示例全长名"})

    fused = fuse_detections([winner, loser], TEXT)

    fragments = sorted((s.start, s.end) for s in fused)
    assert fragments == [(0, 3), (3, 5), (5, 9)]
    for s in fused:
        if (s.start, s.end) == (3, 5):
            assert s.metadata["canonical_value"] == "de"  # untouched winner
        else:
            assert s.metadata["canonical_value"] == TEXT[s.start : s.end]
    # The loser's own metadata dict must not have been mutated in place.
    assert loser.metadata["canonical_value"] == "某示例全长名"


def test_fusion_drops_generic_org_suffix_created_by_clipping():
    text = "上海示例甲置业有限公司"
    winner_end = len("上海示例甲置业")
    winner = span(0, winner_end, "ORG", source="dictionary", confidence=1.0)
    alternative = span(0, len(text), "ORG", source="model", confidence=0.9)

    fused = fuse_detections([alternative, winner], text)

    assert [(text[item.start:item.end], item.source) for item in fused] == [
        ("上海示例甲置业", "dictionary")
    ]


def test_fusion_keeps_meaningful_remainder_created_by_cross_source_overlap():
    text = "上海乾溪置业"
    winner = span(0, len("上海"), "ORG", source="dictionary", confidence=1.0)
    alternative = span(0, len(text), "ORG", source="model", confidence=0.9)

    fused = fuse_detections([alternative, winner], text)

    assert [text[item.start:item.end] for item in fused] == ["上海", "乾溪置业"]


def test_fusion_trims_whitespace_from_a_clipped_remainder():
    text = "甲公司\n乙公司"
    winner = span(0, len("甲公司"), "ORG", source="dictionary", confidence=1.0)
    alternative = _span_with_metadata(
        0,
        len(text),
        "ORG",
        "model",
        {"canonical_value": text},
    )

    fused = fuse_detections([alternative, winner], text)

    assert [(text[item.start:item.end], item.metadata.get("canonical_value")) for item in fused] == [
        ("甲公司", None),
        ("乙公司", "乙公司"),
    ]
