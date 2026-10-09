"""End-to-end generalization (L3): coarsening is applied in place, is NOT
reversible, fails closed to a placeholder, and never trips the refill check."""
from tuomin_gateway.detectors.base import BaseDetector
from tuomin_gateway.detectors.rules import RuleDetector
from tuomin_gateway.profiles import apply_profile, get_profile, profile_for_scenario, with_overrides
from tuomin_gateway.redactor import redact_text
from tuomin_gateway.refill import refill_text
from tuomin_gateway.schemas import DetectionSpan
from tuomin_gateway.session import SessionRedactor


def _span(text: str, sub: str, label: str = "AMOUNT") -> DetectionSpan:
    start = text.index(sub)
    return DetectionSpan(
        start=start, end=start + len(sub), label=label, confidence=0.9,
        source="rule", detector_version="t", text_hash="x", risk_level="medium",
    )


class _FalseAmountDetector(BaseDetector):
    name = "fake-ner"
    version = "test"

    def detect(self, text: str) -> list[DetectionSpan]:
        spans = []
        for value in ("2.23万", "20.98万平方米", "15,525", "10.2 万"):
            start = text.find(value)
            if start >= 0:
                spans.append(
                    self.make_span(
                        text=text,
                        start=start,
                        end=start + len(value),
                        label="AMOUNT",
                        confidence=0.95,
                        risk_level="medium",
                    )
                )
        return spans


def test_detector_fusion_suppresses_area_quantities_mislabeled_as_money():
    profile = with_overrides(
        profile_for_scenario("pre_investment"),
        use_ner=False,
        ner_required=False,
    )
    redactor = SessionRedactor([_FalseAmountDetector()], profile)
    text = (
        "集中商业2.23万平方米，住宅20.98万平方米，地下15,525平方米，"
        "计容建筑面积10.2 万平\n方米。"
    )

    assert redactor.mask(text) == text
    assert redactor.trace_entries() == []


def test_generalized_value_is_coarsened_and_not_reversible():
    text = "合同额30亿元整。"
    p = with_overrides(get_profile("strict"), generalize={"AMOUNT": "amount_bucket"})
    kept, _ = apply_profile([_span(text, "30亿元")], p)
    result = redact_text(text, kept, task_id="gtest")

    assert "10亿–100亿元" in result.redacted_text
    assert "30亿元" not in result.redacted_text
    assert result.mapping == []  # generalized value is NOT in the reversible mapping
    assert result.risk_summary["generalized_count"] == 1
    assert result.risk_summary["placeholder_count"] == 0

    # Refill sees no placeholder: the coarse value is clean and stays put.
    rr = refill_text(result.redacted_text, result.mapping)
    assert rr.status == "ok"
    assert rr.text == result.redacted_text


def test_generalize_fails_closed_to_placeholder_when_uncoarsenable():
    # An AMOUNT span whose value has no parseable number must NOT leak; it falls
    # back to a reversible placeholder instead of staying in clear.
    text = "金额为若干元。"
    p = with_overrides(get_profile("strict"), generalize={"AMOUNT": "amount_bucket"})
    kept, _ = apply_profile([_span(text, "若干元")], p)
    result = redact_text(text, kept, task_id="gtest2")

    assert "若干元" not in result.redacted_text          # not left in clear
    assert result.risk_summary["placeholder_count"] == 1  # fell back to placeholder
    assert result.risk_summary["generalized_count"] == 0


def test_session_mask_generalizes_without_polluting_mapping():
    p = with_overrides(
        get_profile("agent"), generalize={"AMOUNT": "amount_bucket"}, use_ner=False
    )
    sr = SessionRedactor([RuleDetector()], p)
    masked = sr.mask("合同额30亿元。")

    assert "10亿–100亿元" in masked
    assert masked.count("<AMOUNT") == 0                 # no reversible AMOUNT placeholder
    assert "10亿–100亿元" not in sr.mapping.values()     # coarse value never recorded


def test_ungrouped_amount_masks_whole_under_strict():
    # Regression (amount_cny truncation): the whole value is one span — no
    # cleartext tail and no second fragment placeholder.
    strict = with_overrides(get_profile("strict"), use_ner=False, ner_required=False)
    for text, masked_expected in [
        ("合同总价人民币5000元，一次性付清。", "合同总价<AMOUNT_001>，一次性付清。"),
        ("￥99800元", "<AMOUNT_001>"),
        ("人民币1234万元", "<AMOUNT_001>"),
    ]:
        sr = SessionRedactor([RuleDetector()], strict)
        assert sr.mask(text) == masked_expected


def test_ungrouped_amount_generalizes_to_correct_band_under_pre_investment():
    # Regression: the truncated two-fragment split used to concatenate two wrong
    # bands (人民币1234亿元 -> "10亿元以下10亿元以下").
    pre = with_overrides(profile_for_scenario("pre_investment"), use_ner=False, ner_required=False)
    for text, masked_expected in [
        ("标的人民币1234亿元。", "标的100亿元以上。"),
        ("标的人民币1234万元。", "标的10亿元以下。"),
        ("标的人民币32亿元。", "标的30亿–40亿元。"),
    ]:
        sr = SessionRedactor([RuleDetector()], pre)
        assert sr.mask(text) == masked_expected
