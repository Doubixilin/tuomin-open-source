from tuomin_gateway.detectors.rules import PublicContextRuleDetector, RuleDetector


def labels_for(text: str) -> set[str]:
    return {span.label for span in RuleDetector().detect(text)}


def test_rules_detect_high_precision_synthetic_entities():
    text = (
        "合同编号：HT-2026-TM-0001；收款账号：6222020202020202020；"
        "金额为人民币123,456.78元；日期为2026年06月12日；"
        "联系电话：13800138000；邮箱：agent@example.test；"
        "统一社会信用代码：91A1234567BCDEFGH1；"
        "系统地址：https://intranet.example.local/case；"
        "token=sk-test_local_1234567890abcdef。"
    )

    assert {
        "CONTRACT_ID",
        "BANK_ACCOUNT",
        "AMOUNT",
        "DATE",
        "CONTACT",
        "ORG_CODE",
        "SYSTEM_URL",
        "CREDENTIAL",
    }.issubset(labels_for(text))


def test_rules_do_not_treat_short_plain_numbers_as_bank_accounts():
    text = "本段仅引用第12页、第3条和普通编号123456。"

    assert "BANK_ACCOUNT" not in labels_for(text)


def test_rules_distinguish_public_region_and_authority_from_exact_locator():
    text = "上海市宝山区规划和自然资源局核对地铁15号线南大路站TOD地块。"
    spans = PublicContextRuleDetector().detect(text)
    values = {
        (span.label, text[span.start : span.end])
        for span in spans
        if span.label in {"PUBLIC_REGION", "PUBLIC_AUTHORITY"}
    }

    assert ("PUBLIC_REGION", "上海市宝山区") in values
    assert ("PUBLIC_AUTHORITY", "上海市宝山区规划和自然资源局") in values
    assert all("南大路站" not in value for _label, value in values)


def test_rules_treat_standalone_named_public_authority_as_public_context():
    text = "材料已报自然资源和规划局核对。"
    spans = PublicContextRuleDetector().detect(text)

    assert any(
        span.label == "PUBLIC_AUTHORITY"
        and text[span.start : span.end] == "自然资源和规划局"
        for span in spans
    )


def _supplier_spans(text: str):
    return [s for s in RuleDetector().detect(text) if s.label == "SUPPLIER"]


def test_org_by_cue_catches_cue_introduced_supplier():
    spans = _supplier_spans("供应商为远大测试建材公司，报价1500万元。")
    assert spans and spans[0].metadata.get("rule_id") == "org_by_cue"
    text = "供应商为远大测试建材公司，报价1500万元。"
    assert text[spans[0].start : spans[0].end] == "远大测试建材公司"  # full boundary, no comma


def test_org_by_cue_handles_no_suffix_business_words():
    # supplier without 公司 suffix, ending in a business-type word
    assert _supplier_spans("对方为恒通示例物流，报价为600万元。")
    assert _supplier_spans("中标单位西部示例建工集团，金额为3200万元。")


def test_org_by_cue_does_not_fire_on_generic_terms():
    # 该公司 / 本公司 are generic decoys — must NOT be redacted (precision guard)
    assert not _supplier_spans("本项目第二标段由该公司承接，占比15%。")
    assert not _supplier_spans("本公司将另行通知，详见第3章。")


def test_enterprise_cue_rules_cover_synthetic_residual_entities():
    examples = [
        ("示例建设集团与合成供应商丙就测试星河项目签约。", "合成供应商丙"),
        ("罗建华与鼎盛示例机电协商。", "鼎盛示例机电"),
        ("会议纪要：未登记机构澄明样本评估到场。", "澄明样本评估"),
        ("法律意见：未登记相对方景澜样本建设涉及争议。", "景澜样本建设"),
        ("HR画像：候选人来自未登记机构禾木样本咨询。", "禾木样本咨询"),
    ]

    for text, expected in examples:
        spans = _supplier_spans(text)
        assert spans, f"expected enterprise cue span in {text!r}"
        assert any(text[span.start : span.end] == expected for span in spans)


def test_enterprise_cue_rules_keep_existing_decoys_clear():
    assert not _supplier_spans("本项目第二标段由该公司承接，占比15%。")
    assert not _supplier_spans("示例公司只是通用占位，不应默认脱敏。")
    assert not _supplier_spans("张三与示例公司讨论通用模板。")
    assert not _supplier_spans("双方同股同权进行开发。")
    assert not _supplier_spans("分别负责住宅地块与商办地块开发。")
    assert not _supplier_spans("双方共同实施本项目后续开发建设。")
def _spans_of(text: str, label: str):
    return [s for s in RuleDetector().detect(text) if s.label == label]


def _captured(text: str) -> set[str]:
    return {text[s.start : s.end] for s in RuleDetector().detect(text)}


def test_phone_not_matched_inside_alphanumeric_token():
    # An 11-digit phone-shaped run embedded in letters/digits is an id, not a phone.
    assert "CONTACT" not in labels_for("订单号A13800138000B已创建")
    # A genuine phone in Chinese context still matches.
    assert "CONTACT" in labels_for("联系电话13800138000，请查收")


def test_bare_uscc_detected_without_cue():
    spans = _spans_of("营业执照91440300MA5XYZ12AB登记在册", "ORG_CODE")
    assert spans and spans[0].metadata.get("rule_id") == "uscc_bare"
    assert "营业执照91440300MA5XYZ12AB"[spans[0].start : spans[0].end] == "91440300MA5XYZ12AB"


def test_bare_uscc_does_not_match_all_digit_id_card():
    # 18 all-digit run is an ID card, not a USCC — uscc_bare requires a letter.
    assert "ORG_CODE" not in labels_for("证件号110101199003078888 已核验")


def test_bank_account_span_stops_at_the_number():
    text = "收款账号：6222 0000 1111 2222 请尽快转账"
    spans = _spans_of(text, "BANK_ACCOUNT")
    assert spans
    captured = text[spans[0].start : spans[0].end]
    assert captured == "6222 0000 1111 2222"  # no trailing 请尽快 absorbed


def test_landline_detected_with_hyphenated_area_code():
    text = "联系方式：020-22905676，备用（0536）5600239。"
    spans = _spans_of(text, "CONTACT")

    captured = {text[span.start : span.end] for span in spans}
    assert "020-22905676" in captured
    assert "（0536）5600239" in captured


def test_landline_with_extension_and_fullwidth_parens():
    assert "010-1234567转802" in _captured("总机010-1234567转802 工作时间接听")


def test_landline_does_not_fire_on_bare_digit_runs_or_decimals():
    # 无分隔的 0 开头数字串（编号形态）与小数都不是固定电话。
    assert "CONTACT" not in labels_for("批次号02022905676 系数0.22905676 已核对")


def test_bank_account_cue_window_covers_non_adjacent_number():
    text = "户名：某建设集团有限公司 账号：44042701040014089 请按此付款"
    spans = _spans_of(text, "BANK_ACCOUNT")

    assert spans
    assert text[spans[0].start : spans[0].end] == "44042701040014089"


def test_bank_account_cue_window_does_not_cross_sentences():
    text = "账号信息另行提供。本段编号20260101202601012026 与上文无关"
    assert "BANK_ACCOUNT" not in labels_for(text)


def test_date_dot_separated_and_missing_day_suffix():
    text = "签订于2019.03.15，有效期至2025年12月31，原格式2026/06/12 不变。"
    captured = _captured(text)

    assert "2019.03.15" in captured
    assert "2025年12月31" in captured
    assert "2026/06/12" in captured


def test_date_mixed_separators_do_not_match():
    assert "DATE" not in labels_for("版本号2026-08.13 仅内部使用")



def test_bare_wan_yi_amounts_detected_without_currency_cue():
    # 万/亿 amounts with no ￥/人民币 prefix (the leaky case in financial summaries).
    for text, value in [
        ("合同额201亿元", "201亿元"),
        ("潜亏25亿", "25亿"),
        ("利润总额2亿元", "2亿元"),
        ("起诉案件标的额100亿元", "100亿元"),
        ("报价800万元", "800万元"),
    ]:
        spans = _spans_of(text, "AMOUNT")
        assert spans, f"expected AMOUNT in {text!r}"
        assert text[spans[0].start : spans[0].end] == value


def test_area_in_wan_square_meters_is_not_misclassified_as_money():
    for text in [
        "总占地面积18万平方米",
        "总计容面积20.98万平方米",
        "商业面积3万平米",
        "地下空间1.5万㎡",
        "办公面积2万m²",
        "计容建筑面积10.2 万平\n方米",
    ]:
        assert "AMOUNT" not in labels_for(text), text


def test_comma_grouped_wan_yi_amounts_captured_whole():
    # Thousands-grouped amounts must match the WHOLE number, not start after the
    # comma (which left the leading digit in clear and mis-bucketed the range).
    for text, value in [
        ("注册资本1,650万元", "1,650万元"),
        ("出资3,000万元", "3,000万元"),
        ("合资公司注册资本2,500,000万元", "2,500,000万元"),
    ]:
        spans = _spans_of(text, "AMOUNT")
        assert spans, f"expected AMOUNT in {text!r}"
        captured = text[spans[0].start : spans[0].end]
        assert captured == value, f"got {captured!r} in {text!r}"
    # Quantities and ratios that are NOT money must stay clear (over-redaction guard).
    for text in ["共5台设备、200吨钢材，工期为90天。", "质保期24个月", "占比15%", "第585条"]:
        assert "AMOUNT" not in labels_for(text)


def test_ungrouped_long_amounts_captured_whole():
    # Regression: an un-grouped digit run of 4+ digits used to truncate at 3
    # (人民币5000元 -> 人民币500), leaving the tail in clear and splitting the
    # value into two spans with wrong generalization bands.
    for text, value in [
        ("合同总价人民币5000元，一次性付清。", "人民币5000元"),
        ("￥99800元", "￥99800元"),
        ("人民币1234万元", "人民币1234万元"),
        ("人民币1234亿元", "人民币1234亿元"),
    ]:
        spans = _spans_of(text, "AMOUNT")
        assert spans, f"expected AMOUNT in {text!r}"
        captured = [text[span.start : span.end] for span in spans]
        assert value in captured, f"expected whole {value!r} in {text!r}, got {captured!r}"


def test_discount_rate_detected_and_captures_only_the_number():
    text = "下浮率6.4%"
    spans = _spans_of(text, "DISCOUNT_RATE")
    assert spans and spans[0].metadata.get("rule_id") == "discount_rate"
    assert text[spans[0].start : spans[0].end] == "6.4%"  # cue 下浮率 stays in clear


def test_discount_rate_does_not_fire_on_generic_percentages():
    # 担保比例/增值税率/占比/违约金比例 are non-sensitive ratios (benchmark decoys).
    for text in ["担保比例30%", "增值税率9%", "质量保证金占5%", "违约金为合同金额的20%", "占比15%"]:
        assert "DISCOUNT_RATE" not in labels_for(text)


def test_contract_ids_detected_from_narrow_meeting_case_and_legal_cues():
    examples = [
        ("会议纪要：会议号MN-2027-TEST-0021记录发言", "MN-2027-TEST-0021"),
        ("法律意见：供应链有限公司案号LAW-2027-SYN-010，策略意见保留。", "LAW-2027-SYN-010"),
        ("法律意见：律所认为XY-2027-LAW-0007项下可主张解除。", "XY-2027-LAW-0007"),
    ]

    for text, value in examples:
        spans = _spans_of(text, "CONTRACT_ID")
        assert spans, f"expected CONTRACT_ID in {text!r}"
        assert any(text[span.start : span.end] == value for span in spans)


def test_contract_id_cues_do_not_match_generic_hyphenated_tokens():
    assert "CONTRACT_ID" not in labels_for("模板编号 ABC-12 仅表示章节，不是合同定位字段。")
    assert "CONTRACT_ID" not in labels_for("版本号APP-2027-BUILD-010用于公开发布说明。")


def test_bank_card_cue_detects_long_account_number():
    text = "HR画像：银行卡6225888800001111222用于合成薪酬测试。"
    spans = _spans_of(text, "BANK_ACCOUNT")

    assert spans
    assert text[spans[0].start : spans[0].end] == "6225888800001111222"


def test_named_key_credential_detects_hr_key_without_broadening_short_values():
    text = "HR画像：账号hr_key=TESThrKey1234567890abcd仅为合成样本。"
    spans = _spans_of(text, "CREDENTIAL")

    assert spans
    assert text[spans[0].start : spans[0].end] == "TESThrKey1234567890abcd"
    assert "CREDENTIAL" not in labels_for("账号feature_key=short仅为普通配置说明。")
