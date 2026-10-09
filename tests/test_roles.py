"""Transaction-role assignment + role-aware placeholders (角色化占位)."""
from tuomin_gateway.detectors.rules import RuleDetector
from tuomin_gateway.profiles import profile_for_scenario, with_overrides
from tuomin_gateway.redactor import redact_text
from tuomin_gateway.roles import assign_roles
from tuomin_gateway.schemas import DetectionSpan
from tuomin_gateway.session import SessionRedactor


def _span(text: str, sub: str, label: str = "ORG", canonical: str | None = None,
          occurrence: int = 0) -> DetectionSpan:
    start = text.index(sub) if occurrence == 0 else text.rindex(sub)
    md = {"canonical_value": canonical} if canonical else {}
    return DetectionSpan(
        start=start, end=start + len(sub), label=label, confidence=0.9,
        source="dictionary", detector_version="t", text_hash="x",
        risk_level="high", metadata=md,
    )


def _roles(text, spans):
    return {text[s.start:s.end]: s.metadata.get("role") for s in assign_roles(text, spans, "contract")}


def test_assign_roles_tags_party_from_left_cue():
    text = "甲方示例集团有限公司与乙方测试建工公司签约"
    spans = [_span(text, "示例集团有限公司"), _span(text, "测试建工公司")]
    roles = _roles(text, spans)
    assert roles["示例集团有限公司"] == "PARTYA"
    assert roles["测试建工公司"] == "PARTYB"


def test_assign_roles_nearest_cue_wins():
    text = "乙方与甲方示例集团有限公司"  # 甲方 is nearer than 乙方
    out = assign_roles(text, [_span(text, "示例集团有限公司")], "contract")
    assert out[0].metadata["role"] == "PARTYA"


def test_assign_roles_consistent_across_mentions_by_value():
    # Second mention has no adjacent cue but must inherit the role of the first.
    text = "甲方示例集团有限公司承建，后续示例集团有限公司负责。"
    spans = [_span(text, "示例集团有限公司", occurrence=0),
             _span(text, "示例集团有限公司", occurrence=1)]
    out = assign_roles(text, spans, "contract")
    assert all(s.metadata.get("role") == "PARTYA" for s in out)


def test_assign_roles_uses_canonical_value():
    text = "甲方为示建A，后文示建A再现。"
    spans = [_span(text, "示建A", canonical="示例集团", occurrence=0),
             _span(text, "示建A", canonical="示例集团", occurrence=1)]
    out = assign_roles(text, spans, "contract")
    assert all(s.metadata.get("role") == "PARTYA" for s in out)


def test_assign_roles_noop_without_cue_or_unknown_set():
    text = "示例集团有限公司参与项目。"
    spans = [_span(text, "示例集团有限公司")]
    assert assign_roles(text, spans, "contract")[0].metadata.get("role") is None
    assert assign_roles(text, spans, "nonexistent") == spans  # unknown set -> unchanged


def test_redact_text_honors_role_prefix():
    text = "甲方示例集团有限公司"
    span = DetectionSpan(
        start=2, end=len(text), label="ORG", confidence=0.9, source="dictionary",
        detector_version="t", text_hash="x", risk_level="high",
        metadata={"role": "PARTYA"},
    )
    result = redact_text(text, [span], task_id="r")
    assert "<PARTYA_001>" in result.redacted_text
    assert result.mapping[0].label == "ORG"          # entity label preserved
    assert result.mapping[0].placeholder == "<PARTYA_001>"


def test_investment_role_set_tags_lead_partner_target():
    text = "牵头方示例资本有限公司，合作方远大测试基金，标的方测试能源有限公司。"
    spans = [
        _span(text, "示例资本有限公司"),
        _span(text, "远大测试基金"),
        _span(text, "测试能源有限公司"),
    ]
    roles = {text[s.start:s.end]: s.metadata.get("role")
             for s in assign_roles(text, spans, "investment")}
    assert roles["示例资本有限公司"] == "LEAD"
    assert roles["远大测试基金"] == "PARTNER"
    assert roles["测试能源有限公司"] == "TARGET"


def test_session_contract_scenario_is_role_aware_and_reversible():
    p = with_overrides(
        profile_for_scenario("contract"), use_ner=False, ner_required=False
    )
    sr = SessionRedactor([RuleDetector()], p)
    masked = sr.mask("甲方为示例科技有限公司，乙方为测试建工有限公司。")

    assert "<PARTYA_001>" in masked and "<PARTYB_001>" in masked
    assert "<ORG" not in masked and "<SUPPLIER" not in masked
    # role-typed placeholders are still reversible
    assert sr.unmask("<PARTYA_001>") == "示例科技有限公司"
