"""Tests for the dual-mode workbench refill flow."""
from __future__ import annotations

from tuomin_gateway.document.refill_flow import (
    MODE_EDITED,
    MODE_EXACT,
    classify_mode,
    placeholder_diff,
    redacted_sha256,
    refill_markdown,
)
from tuomin_gateway.schemas import MappingEntry

_ENTRIES = [
    MappingEntry(placeholder="<ORG_001>", label="ORG", original_value="甲方公司", text_hash="sha256:a"),
    MappingEntry(placeholder="<ORG_002>", label="ORG", original_value="乙方公司", text_hash="sha256:b"),
    MappingEntry(placeholder="<CONTACT_001>", label="CONTACT", original_value="13800000000", text_hash="sha256:c"),
]
_COUNTS = {"<ORG_001>": 1, "<ORG_002>": 1, "<CONTACT_001>": 1}
_REDACTED = "# 合同\n\n<ORG_001> 与 <ORG_002> 签订，联系 <CONTACT_001>。\n"
_HASH = redacted_sha256(_REDACTED)


def test_classify_exact_vs_edited():
    assert classify_mode(_REDACTED, recorded_redacted_sha256=_HASH) == MODE_EXACT
    assert classify_mode(_REDACTED + " ", recorded_redacted_sha256=_HASH) == MODE_EDITED
    assert classify_mode(_REDACTED, recorded_redacted_sha256=None) == MODE_EDITED


def test_exact_mode_round_trip():
    result = refill_markdown(_REDACTED, _ENTRIES, _COUNTS, recorded_redacted_sha256=_HASH)
    assert result.status == "ok"
    assert result.mode == MODE_EXACT
    assert "甲方公司" in result.text and "13800000000" in result.text
    assert "<ORG_001>" not in result.text


def test_exact_mode_blocks_on_any_anomaly():
    tampered = _REDACTED.replace("<CONTACT_001>", "<CONTACT_999>")
    # force exact classification by hashing the tampered text as "recorded"
    result = refill_markdown(
        tampered, _ENTRIES, _COUNTS, recorded_redacted_sha256=redacted_sha256(tampered)
    )
    assert result.status == "blocked"
    assert "unknown_placeholder" in result.error_types
    assert "missing_placeholder" in result.error_types
    assert result.text is None


def test_exact_mode_blocks_count_mismatch():
    duplicated = _REDACTED + "\n再次出现 <ORG_001>。\n"
    result = refill_markdown(
        duplicated, _ENTRIES, _COUNTS, recorded_redacted_sha256=redacted_sha256(duplicated)
    )
    assert result.status == "blocked"
    assert "placeholder_count_mismatch" in result.error_types


def test_edited_mode_needs_confirmation_then_refills():
    edited = _REDACTED.replace("签订", "共同签订")  # legitimate human edit
    first = refill_markdown(edited, _ENTRIES, _COUNTS, recorded_redacted_sha256=_HASH)
    assert first.status == "ok"  # diff is clean, no confirmation needed
    assert "共同签订" in first.text

    removed = _REDACTED.replace("，联系 <CONTACT_001>", "")
    second = refill_markdown(removed, _ENTRIES, _COUNTS, recorded_redacted_sha256=_HASH)
    assert second.status == "needs_confirmation"
    assert second.diff.missing == ["<CONTACT_001>"]
    assert second.text is None
    confirmed = refill_markdown(
        removed, _ENTRIES, _COUNTS, recorded_redacted_sha256=_HASH, confirm_edited=True
    )
    assert confirmed.status == "ok"
    assert "甲方公司" in confirmed.text


def test_edited_mode_blocks_unknown_and_altered_despite_confirmation():
    unknown = _REDACTED + "\n<ORG_099>\n"
    result = refill_markdown(
        unknown, _ENTRIES, _COUNTS, recorded_redacted_sha256=_HASH, confirm_edited=True
    )
    assert result.status == "blocked"
    assert "unknown_placeholder" in result.error_types

    altered = _REDACTED.replace("<ORG_001>", "<ORG-001>")
    result = refill_markdown(
        altered, _ENTRIES, _COUNTS, recorded_redacted_sha256=_HASH, confirm_edited=True
    )
    assert result.status == "blocked"
    assert "altered_placeholder" in result.error_types


def test_placeholder_diff_reports_duplicates():
    text = _REDACTED + "\n<ORG_001> 又一次。\n"
    diff = placeholder_diff(text, _ENTRIES, _COUNTS)
    assert diff.duplicated == ["<ORG_001>"]
    assert not diff.clean


def test_no_partial_output_on_block():
    tampered = _REDACTED.replace("<ORG_002>", "<ORG_666>")
    result = refill_markdown(tampered, _ENTRIES, _COUNTS, recorded_redacted_sha256=_HASH)
    assert result.status == "blocked"
    assert result.text is None
