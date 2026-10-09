from tuomin_gateway.profiles import (
    GENERALIZE,
    apply_profile,
    get_profile,
    is_fully_reversible,
    profile_for_scenario,
    resolve_named_profile,
    with_overrides,
)
from tuomin_gateway.schemas import DetectionSpan


def _span(label: str, risk: str = "high", start: int = 0, end: int = 3) -> DetectionSpan:
    return DetectionSpan(
        start=start, end=end, label=label, confidence=0.9, source="rule",
        detector_version="t", text_hash="sha256:x", risk_level=risk,
    )


def test_contract_review_passes_amount_and_date_redacts_identity():
    cr = get_profile("contract_review")
    assert cr.decide(_span("AMOUNT")) == "pass"
    assert cr.decide(_span("DATE")) == "pass"
    assert cr.decide(_span("ORG")) == "redact"


def test_apply_profile_filters_passed_labels():
    cr = get_profile("contract_review")
    spans = [_span("ORG", start=0, end=3), _span("AMOUNT", start=4, end=8)]
    kept, blocked = apply_profile(spans, cr)
    assert [s.label for s in kept] == ["ORG"]
    assert blocked == []


def test_block_label_is_both_redacted_and_flagged():
    p = with_overrides(get_profile("strict"), action={"CREDENTIAL": "block"})
    kept, blocked = apply_profile([_span("CREDENTIAL")], p)
    assert [s.label for s in kept] == ["CREDENTIAL"]  # still redacted, never left clear
    assert blocked == ["CREDENTIAL"]


def test_min_risk_threshold_passes_low_risk():
    p = with_overrides(get_profile("strict"), min_risk="high")
    kept, _ = apply_profile([_span("DATE", risk="low"), _span("ORG", risk="high")], p)
    assert [s.label for s in kept] == ["ORG"]


def test_guard_defaults_are_warn_only_no_hard_block():
    # The cross-pillar guard fields default to "scan and warn, never hard-block"
    # so adding the guard layer cannot break an existing app.
    for name in ("kb", "contract_review", "agent", "strict"):
        p = get_profile(name)
        assert p.alert_min_severity == "warn"
        assert p.block_min_severity is None
        assert p.scan_injection is True
        assert p.block_on_hallucinated_placeholder is False


def test_block_min_severity_is_opt_in_per_profile():
    p = with_overrides(get_profile("strict"), block_min_severity="critical")
    assert p.block_min_severity == "critical"
    # opting in must not disturb the masking-side decisions
    assert p.decide(_span("ORG")) == "redact"


def test_generalize_decision_and_apply_tags_span():
    p = with_overrides(get_profile("strict"), generalize={"AMOUNT": "amount_bucket"})
    assert p.decide(_span("AMOUNT")) == GENERALIZE
    assert p.decide(_span("ORG")) == "redact"  # untouched labels still redact
    kept, blocked = apply_profile([_span("AMOUNT")], p)
    assert len(kept) == 1 and blocked == []
    assert kept[0].metadata["action"] == GENERALIZE
    assert kept[0].metadata["generalizer"] == "amount_bucket"


def test_generalize_wins_over_deny_but_not_explicit_action():
    # generalize is an intentional middle ground -> it beats deny_labels...
    p = with_overrides(
        get_profile("strict"),
        deny_labels=frozenset({"AMOUNT"}),
        generalize={"AMOUNT": "amount_bucket"},
    )
    assert p.decide(_span("AMOUNT")) == GENERALIZE
    # ...but an explicit per-label action still wins over generalization.
    p2 = with_overrides(p, action={"AMOUNT": "block"})
    assert p2.decide(_span("AMOUNT")) == "block"


def test_contract_scenario_field_policy():
    p = profile_for_scenario("contract")
    assert p.decide(_span("AMOUNT")) == "pass"           # passes for payment-term analysis
    assert p.decide(_span("DATE")) == "pass"
    assert p.decide(_span("ADDRESS")) == GENERALIZE       # coarsened to province
    assert p.decide(_span("LEGAL_STRATEGY")) == "block"   # never reaches cloud
    assert p.decide(_span("NEGOTIATION_POSITION")) == "block"
    assert p.decide(_span("ORG")) == "redact"             # identity (role-aware later)
    assert p.decide(_span("CONTRACT_ID")) == "redact"


def test_registry_resolves_scenario():
    from tuomin_gateway.service.registry import AppRegistry

    reg = AppRegistry({"capp": {"scenario": "contract"}})
    p = reg.resolve_profile("capp")
    assert p.name == "contract"
    assert p.decide(_span("ADDRESS")) == GENERALIZE


def test_pre_investment_scenario_field_policy():
    p = profile_for_scenario("pre_investment")
    assert p.decide(_span("AMOUNT")) == GENERALIZE        # 投资体量 → 10亿档
    assert p.generalize["AMOUNT"] == "amount_band_10yi"
    assert p.decide(_span("DATE")) == "redact"            # 推进信号 → 占位脱敏
    assert p.decide(_span("ADDRESS")) == "redact"         # 防反推 → 占位（强于合同的泛化）
    assert p.decide(_span("PROJECT")) == "redact"
    assert p.decide(_span("LEGAL_STRATEGY")) == "block"
    # Investment roles are inferred rather than declared, so the production
    # scenario keeps pure identity placeholders stable across documents.
    assert p.roles == ""


def test_legal_review_policy_keeps_public_context_but_masks_exact_locator():
    from tuomin_gateway.detectors.rules import PublicContextRuleDetector
    from tuomin_gateway.fusion import fuse_detections
    from tuomin_gateway.schemas import hash_text

    policy = with_overrides(
        profile_for_scenario("pre_investment"),
        deny_labels=frozenset(
            {"AMOUNT", "DATE", "COURT", "ARBITRATION", "STATUTE",
             "PUBLIC_REGION", "PUBLIC_AUTHORITY"}
        ),
    )
    text = "上海市宝山区南大路站TOD地块"
    model = DetectionSpan(
        start=0,
        end=len(text),
        label="ADDRESS",
        confidence=0.9,
        source="model",
        detector_version="test",
        text_hash=hash_text(text),
    )
    fused = fuse_detections([*PublicContextRuleDetector().detect(text), model], text)
    kept, _blocked = apply_profile(fused, policy)

    assert [text[item.start:item.end] for item in kept] == ["南大路站TOD地块"]

    authority = "上海市宝山区规划和自然资源局"
    authority_model = DetectionSpan(
        start=0,
        end=len(authority),
        label="ORG",
        confidence=0.9,
        source="model",
        detector_version="test",
        text_hash=hash_text(authority),
    )
    authority_fused = fuse_detections(
        [*PublicContextRuleDetector().detect(authority), authority_model], authority
    )
    authority_kept, _blocked = apply_profile(authority_fused, policy)
    assert authority_kept == []


def test_litigation_scenario_field_policy():
    p = profile_for_scenario("litigation")
    assert p.decide(_span("AMOUNT")) == "pass"            # 精确法律计算（逾期/违约金）
    assert p.decide(_span("DATE")) == "pass"
    assert p.decide(_span("ADDRESS")) == GENERALIZE        # 当事人地址 → 省
    assert p.decide(_span("COURT")) == "pass"              # 保留原名：LLM 需识别具体法院
    assert p.decide(_span("ARBITRATION")) == "pass"
    assert p.decide(_span("STATUTE")) == "pass"
    assert p.decide(_span("ORG")) == "redact"              # 当事人身份脱敏
    assert p.decide(_span("PERSON")) == "redact"
    assert p.roles == ""                                   # 放弃角色化（设计 §7）


def test_litigation_protect_dictionary_keeps_court_but_masks_party():
    """法院词典命中(优先级 80) → COURT pass → 保留原名；当事人 ORG → redact 脱敏。"""
    from tuomin_gateway.redactor import redact_text
    from tuomin_gateway.session import build_detectors, collect_detections

    entries = [
        {"canonical_value": "北京市第三中级人民法院", "label": "COURT",
         "risk_level": "high", "status": "active"},
        {"canonical_value": "示例建设集团有限公司", "label": "ORG",
         "risk_level": "high", "status": "active"},
    ]
    p = with_overrides(
        profile_for_scenario("litigation"), use_ner=False, ner_required=False
    )
    text = "本案由北京市第三中级人民法院审理，被告示例建设集团有限公司。"
    kept, _ = collect_detections(text, build_detectors(entries), p)
    masked = redact_text(text, kept).redacted_text
    assert "北京市第三中级人民法院" in masked       # 法院保留原名
    assert "示例建设集团有限公司" not in masked      # 当事人被脱敏
    assert "<ORG_" in masked


# --- 批次 4: fail-fast on invalid profile construction -----------------------

def test_profile_roles_fail_fast_on_unknown_role_set():
    import pytest

    from tuomin_gateway.profiles import Profile

    with pytest.raises(ValueError, match="unknown role set"):
        Profile(name="bad", roles="contracts")  # typo of "contract"
    for valid in ("", "contract", "investment"):
        Profile(name=f"ok-{valid}", roles=valid)


def test_profile_scope_frozen_to_legacy_modes():
    import pytest

    from tuomin_gateway.profiles import Profile

    with pytest.raises(ValueError, match="frozen"):
        Profile(name="bad", scope="namespace")
    for valid in ("document", "session"):
        Profile(name=f"ok-{valid}", scope=valid)


def test_registry_invalid_declared_profile_fails_closed():
    import pytest

    from tuomin_gateway.service.registry import AppRegistry, RegistryConfigurationError

    registry = AppRegistry({"bad_app": {"profile": {"name": "x", "roles": "bogus"}}})
    with pytest.raises(RegistryConfigurationError) as exc_info:
        registry.resolve_profile("bad_app")
    assert exc_info.value.code == "profile_unavailable"

    # An unknown scenario name in the registry is a server misconfiguration too.
    registry = AppRegistry({"bad_app2": {"scenario": "litigaton"}})
    with pytest.raises(RegistryConfigurationError):
        registry.resolve_profile("bad_app2")


def test_builtin_presets_all_pass_fail_fast_validation():
    from tuomin_gateway.profiles import PRESETS, SCENARIO_PRESETS, SENSITIVITY_PRESETS

    for table in (PRESETS, SCENARIO_PRESETS, SENSITIVITY_PRESETS):
        for name, profile in table.items():
            assert profile.roles in ("", "contract", "investment"), name
            assert profile.scope in ("document", "session"), name


def test_file_workbench_is_builtin_only_and_keeps_default_policy_fields():
    from tuomin_gateway.profiles import PRESETS, SCENARIO_PRESETS, SENSITIVITY_PRESETS

    p = get_profile("file_workbench")
    assert PRESETS["file_workbench"] is p
    assert "file_workbench" not in SENSITIVITY_PRESETS
    assert "file_workbench" not in SCENARIO_PRESETS
    assert p.name == "file_workbench"
    assert p.scope == "document"
    assert p.use_ner is True
    assert p.ner_required is False
    assert p.refill_strict is True
    assert p.labels is None
    assert p.deny_labels == frozenset()
    assert p.action == {}
    assert p.generalize == {}
    assert p.roles == ""


def test_file_workbench_resolves_and_redacts_every_label_and_risk():
    p = resolve_named_profile("file_workbench")
    assert p is get_profile("file_workbench")
    labels = ("AMOUNT", "DATE", "ADDRESS", "COURT", "CONTACT", "ID_CARD", "OTHER")
    risks = ("low", "medium", "high", "critical", "unknown")
    for label in labels:
        for risk in risks:
            assert p.decide(_span(label, risk=risk)) == "redact"


def test_file_workbench_apply_keeps_mixed_spans_without_generalization():
    p = get_profile("file_workbench")
    spans = [
        _span("AMOUNT", risk="low", start=0, end=3),
        _span("DATE", risk="unknown", start=4, end=7),
        _span("ADDRESS", start=8, end=11),
        _span("COURT", start=12, end=15),
        _span("CONTACT", start=16, end=19),
        _span("ID_CARD", start=20, end=23),
    ]
    kept, blocked = apply_profile(spans, p)
    assert kept == spans
    assert blocked == []
    assert all(span.metadata.get("action") != GENERALIZE for span in kept)


def test_is_fully_reversible_rejects_every_generalize_path():
    assert is_fully_reversible(get_profile("file_workbench")) is True
    assert is_fully_reversible(profile_for_scenario("contract")) is False
    action_generalize = with_overrides(
        get_profile("strict"), action={"X": GENERALIZE}
    )
    assert is_fully_reversible(action_generalize) is False
