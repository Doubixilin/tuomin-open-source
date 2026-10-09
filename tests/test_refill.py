from tuomin_gateway.mapping import MappingEntry
from tuomin_gateway.placeholders import substitute
from tuomin_gateway.refill import refill_text, refill_text_contract


def mapping_entries():
    return [
        MappingEntry(
            placeholder="<PROJECT_001>",
            label="PROJECT",
            original_value="测试绿洲项目",
            text_hash="sha256:project",
        ),
        MappingEntry(
            placeholder="<ORG_001>",
            label="ORG",
            original_value="示例建设单位A",
            text_hash="sha256:org",
        ),
    ]


def test_refill_restores_only_when_placeholders_are_intact():
    result = refill_text("请复核 <ORG_001> 的 <PROJECT_001>。", mapping_entries())

    assert result.status == "ok"
    assert result.text == "请复核 示例建设单位A 的 测试绿洲项目。"


def test_refill_blocks_unknown_placeholder():
    result = refill_text("请复核 <ORG_001> 和 <PERSON_001> 的 <PROJECT_001>。", mapping_entries())

    assert result.status == "blocked"
    assert "unknown_placeholder" in result.error_types


def test_refill_blocks_altered_placeholder():
    result = refill_text("请复核 <ORG-001> 的 <PROJECT_001>。", mapping_entries())

    assert result.status == "blocked"
    assert "altered_placeholder" in result.error_types


# Mangled angle-bracketed forms the strict patterns used to miss: lowercase,
# inner whitespace, missing separator, or a line break inside the brackets.
ALTERED_VARIANTS = [
    "<org_001>",
    "< ORG_001>",
    "<ORG_ 001>",
    "<ORG_001 >",
    "<ORG001>",
    "<ORG_\n001>",
]


def test_refill_blocks_lenient_altered_variants():
    for variant in ALTERED_VARIANTS:
        result = refill_text(f"请复核 {variant} 的 <PROJECT_001>。", mapping_entries())
        assert result.status == "blocked", variant
        assert "altered_placeholder" in result.error_types, variant
        assert variant in result.altered_placeholders, variant


def test_trusted_display_blocks_lenient_altered_variants():
    for variant in ALTERED_VARIANTS:
        result = refill_text_contract(
            f"请复核 {variant}。", mapping_entries(), contract="trusted_display"
        )
        assert result.status == "blocked", variant
        assert "altered_placeholder" in result.error_types, variant


def test_wellformed_placeholders_and_plain_prose_are_not_altered():
    result = refill_text("请复核 <ORG_001> 的 <PROJECT_001>。", mapping_entries())
    assert result.status == "ok"
    # No angle-bracket token at all -> nothing to flag.
    result = refill_text("请复核这份没有占位符的纪要。", mapping_entries())
    assert "altered_placeholder" not in result.error_types


def test_refill_reversible_past_999_entities():
    # The 4-digit placeholder must round-trip (the old \d{3} regex silently failed).
    entries = [MappingEntry(placeholder="<DEPT_1000>", label="DEPARTMENT",
                            original_value="第一千号部门", text_hash="sha256:x")]
    result = refill_text("归属 <DEPT_1000> 处理。", entries)
    assert result.status == "ok"
    assert result.text == "归属 第一千号部门 处理。"


def test_substitute_does_not_recurse_into_restored_values():
    # An original value containing another placeholder's literal text must not be
    # re-substituted — single-pass guarantees this.
    mapping = {"<A_001>": "see <B_001> later", "<B_001>": "SECRET"}
    out, count = substitute("ref <A_001> and <B_001>", mapping)
    assert out == "ref see <B_001> later and SECRET"
    assert count == 2


def test_refill_blocked_safe_dict_does_not_include_original_values():
    result = refill_text("仅保留 <ORG_001>。", mapping_entries())

    payload = result.to_safe_dict()
    original_values = {entry.original_value for entry in mapping_entries()}

    assert result.status == "blocked"
    assert "missing_placeholder" in payload["error_types"]
    assert payload["text"] is None
    assert original_values == {"示例建设单位A", "测试绿洲项目"}
    for original_value in original_values:
        assert original_value not in str(payload)
