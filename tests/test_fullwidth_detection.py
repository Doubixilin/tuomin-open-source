"""Full-width / mixed-width detection closure (textnorm half-width view).

Chinese business text mixes full-width ASCII (０-９, Ａ-Ｚ, ：，￥) with
half-width forms; before ``textnorm``, rules and dictionary needles missed the
full-width forms entirely. These tests pin: detection fires on both widths,
spans always slice the ORIGINAL text (exact-surface + canonical invariant),
and value parsers (amount/date generalizers) read the half-width view.
"""
from __future__ import annotations

from tuomin_gateway.detectors.dictionary import DictionaryDetector
from tuomin_gateway.detectors.rules import RuleDetector
from tuomin_gateway.generalizers import amount_band_10yi, amount_bucket, quarter
from tuomin_gateway.redactor import redact_text
from tuomin_gateway.refill import refill_text
from tuomin_gateway.textnorm import to_halfwidth


def _rule_spans(text: str):
    return RuleDetector().detect(text)


def test_to_halfwidth_is_one_to_one_and_preserves_boundary_punctuation():
    # Digits/letters/value-internal punctuation translate; boundary punctuation
    # (，。！？；、（）) deliberately stays full-width to preserve stop-char and
    # lookahead semantics in the patterns.
    assert to_halfwidth("１２３Ａａ：，￥　（）％") == "123Aa:，¥ （）%"
    assert to_halfwidth("，。！？；、（）【】") == "，。！？；、（）【】"
    original = "全角１２３和半角123混合，CJK不动。"
    assert len(to_halfwidth(original)) == len(original)
    assert to_halfwidth("中文标点。不动") == "中文标点。不动"


def test_fullwidth_phone_detected_and_slices_original():
    text = "联系电话１３９００００１１１１。"
    spans = _rule_spans(text)
    hits = [s for s in spans if s.label == "CONTACT"]
    assert len(hits) == 1
    assert text[hits[0].start:hits[0].end] == "１３９００００１１１１"  # original width


def test_mixed_width_phone_detected_as_one_run():
    text = "电话139０００１１１11。"
    hits = [s for s in _rule_spans(text) if s.label == "CONTACT"]
    assert len(hits) == 1
    assert text[hits[0].start:hits[0].end] == "139０００１１１11"


def test_fullwidth_bank_account_id_card_amount_date_detected():
    text = (
        "银行账号：６２２２０２０２０２０２０２０２０；"
        "身份证１１０１０１１９９００１０１０１０Ｘ；"
        "金额￥９９，８００元；日期２０２６年０６月１２日；"
        "日期２０２６－０６－１２；日期２０２６／０６／１３；总额３２亿元。"
    )
    labels = {(s.label, text[s.start:s.end]) for s in _rule_spans(text)}
    assert ("BANK_ACCOUNT", "６２２２０２０２０２０２０２０２０") in labels
    assert ("ID_CARD", "１１０１０１１９９００１０１０１０Ｘ") in labels
    assert ("AMOUNT", "￥９９，８００元") in labels
    assert ("DATE", "２０２６年０６月１２日") in labels
    assert ("DATE", "２０２６－０６－１２") in labels
    assert ("DATE", "２０２６／０６／１３") in labels
    assert ("AMOUNT", "３２亿元") in labels


def test_fullwidth_uscc_and_contract_id_detected():
    text = "统一社会信用代码：９１３３０１０６ＭＡ１Ｋ３５ＸＬ４Ｔ；合同编号：ＨＴ-２０２６-ＴＭ-０００１"
    labels = {(s.label, text[s.start:s.end]) for s in _rule_spans(text)}
    assert ("ORG_CODE", "９１３３０１０６ＭＡ１Ｋ３５ＸＬ４Ｔ") in labels
    assert ("CONTRACT_ID", "ＨＴ-２０２６-ＴＭ-０００１") in labels


def test_dictionary_matches_across_widths_both_ways():
    entries = [
        {"canonical_value": "绿洲建设集团", "aliases": ["绿洲HT-2026"], "label": "ORG", "risk_level": "high", "status": "active"},
        {"canonical_value": "ＨＴ－９９９", "aliases": [], "label": "CONTRACT_ID", "risk_level": "high", "status": "active"},
    ]
    det = DictionaryDetector.from_entries(entries)

    # Half-width entry matches full-width text.
    text_fw = "乙方为绿洲建设集团，编号ＨＴ－９９９。"
    spans_fw = det.detect(text_fw)
    assert any(text_fw[s.start:s.end] == "绿洲建设集团" for s in spans_fw)
    # Full-width entry matches half-width text (needles normalized both sides).
    text_hw = "乙方为绿洲建设集团，编号HT-999。"
    spans_hw = det.detect(text_hw)
    assert any(text_hw[s.start:s.end] == "HT-999" for s in spans_hw)
    # canonical identity stays the entry's canonical value, untouched.
    meta = [s.metadata for s in spans_hw if s.label == "CONTRACT_ID"]
    assert meta and meta[0]["canonical_value"] == "ＨＴ－９９９"


def test_generalizers_parse_fullwidth_values():
    assert amount_band_10yi("３２亿元") == "30亿–40亿元"
    assert amount_bucket("￥９９，８００元") == "100万元以下"
    assert quarter("２０２６年０６月１２日") == "2026年Q2"


def test_end_to_end_fullwidth_redact_and_refill_restores_original_width():
    text = "联系电话１３９００００１１１１，备用13900002222。"
    spans = _rule_spans(text)
    result = redact_text(text, spans, task_id="task_fullwidth")

    assert "１３９００００１１１１" not in result.redacted_text
    assert "13900002222" not in result.redacted_text
    assert "<CONTACT_001>" in result.redacted_text
    assert "<CONTACT_002>" in result.redacted_text  # exact-surface: widths stay distinct

    refilled = refill_text(result.redacted_text, result.mapping)
    assert refilled.status == "ok"
    assert refilled.text == text  # full-width original restored verbatim
