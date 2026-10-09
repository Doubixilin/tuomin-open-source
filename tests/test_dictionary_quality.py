import json

from tuomin_gateway.cli import main
from tuomin_gateway.dictionary_quality import (
    DICTIONARY_QUALITY_CONTRACT,
    assert_curated_dictionary,
    lint_dictionary_entries,
)


def _entry(entry_id, canonical, label="ORG", aliases=()):
    return {
        "entry_id": entry_id,
        "canonical_value": canonical,
        "aliases": list(aliases),
        "label": label,
        "risk_level": "high",
        "status": "active",
        "version": "test",
    }


def test_curated_dictionary_accepts_specific_canonical_aliases():
    entries = [
        _entry(
            "org-1",
            "示例建设第八工程局有限公司",
            aliases=["示例建设八局"],
        ),
        _entry("project-1", "星河湾更新项目", label="PROJECT"),
    ]

    report = assert_curated_dictionary(entries)

    assert report == {
        "status": "ok",
        "contract": DICTIONARY_QUALITY_CONTRACT,
        "entry_count": 2,
        "active_entry_count": 2,
        "issue_count": 0,
        "issue_codes": [],
        "issues": [],
    }


def test_quality_gate_blocks_observed_benchmark_pollution():
    entries = [
        _entry("org-1", "经中国建筑股份有限公司"),
        _entry("org-2", "项目公司为上海示例甲置业有限公司"),
        _entry("project-1", "实施本项目", label="PROJECT"),
        _entry("project-2", "投资变更", label="PROJECT"),
    ]

    report = lint_dictionary_entries(entries)

    assert report["status"] == "blocked"
    assert set(report["issue_codes"]) == {"context_prefix", "generic_value"}
    assert {item["value"] for item in report["issues"]} == {
        "经中国建筑股份有限公司",
        "项目公司为上海示例甲置业有限公司",
        "实施本项目",
        "投资变更",
    }


def test_quality_gate_blocks_context_phrase_embedded_in_extracted_sentence():
    report = lint_dictionary_entries(
        [_entry("org-1", "现将本项目提请投融资管理委员会")]
    )

    assert report["status"] == "blocked"
    assert "context_prefix" in report["issue_codes"]


def test_quality_gate_blocks_generic_alias_and_cross_entry_identity_conflict():
    entries = [
        _entry("org-1", "示例建设集团有限公司", aliases=["公司", "示例集团"]),
        _entry("org-2", "另一示例集团有限公司", aliases=["示例集团"]),
    ]

    report = lint_dictionary_entries(entries)

    assert "generic_value" in report["issue_codes"]
    assert "standalone_suffix" in report["issue_codes"]
    assert "identity_conflict" in report["issue_codes"]


def test_quality_gate_blocks_generic_legal_reference_fragment():
    report = lint_dictionary_entries([_entry("org-1", "行政机")])

    assert report["status"] == "blocked"
    assert "generic_fragment" in report["issue_codes"]


def test_quality_gate_blocks_sentence_and_table_row_values():
    report = lint_dictionary_entries(
        [
            _entry("org-1", "根据招标要求，公司拟提供担保"),
            _entry("project-1", "板块 总户数 车位开售时间 开发商", label="PROJECT"),
        ]
    )

    assert report["status"] == "blocked"
    assert "sentence_punctuation" in report["issue_codes"]
    assert "excessive_whitespace" in report["issue_codes"]


def test_quality_gate_blocks_same_surface_under_multiple_labels():
    report = lint_dictionary_entries(
        [
            _entry("org-1", "示例建设集团有限公司", label="ORG"),
            _entry("supplier-1", "示例建设集团有限公司", label="SUPPLIER"),
        ]
    )

    assert report["status"] == "blocked"
    assert "label_conflict" in report["issue_codes"]


def test_quality_gate_rejects_non_boolean_ocr_repair_flag():
    entry = _entry("org-1", "示例建设集团有限公司")
    entry["allow_leading_ocr_truncation"] = "yes"

    report = lint_dictionary_entries([entry])

    assert report["status"] == "blocked"
    assert "ocr_repair_flag_invalid" in report["issue_codes"]


def test_quality_gate_allows_structured_url_punctuation():
    report = lint_dictionary_entries(
        [_entry("url-1", "https://internal.example.test/path", label="SYSTEM_URL")]
    )

    assert report["status"] == "ok"


def test_inactive_polluted_entry_does_not_block_curated_active_set():
    entry = _entry("project-1", "实施本项目", label="PROJECT")
    entry["status"] = "inactive"

    report = lint_dictionary_entries([entry])

    assert report["status"] == "ok"
    assert report["active_entry_count"] == 0


def test_dictionary_lint_cli_reports_values_locally(tmp_path, capsys):
    path = tmp_path / "dictionary.json"
    path.write_text(
        json.dumps([_entry("project-1", "投资变更", label="PROJECT")], ensure_ascii=False),
        encoding="utf-8",
    )

    exit_code = main(["dictionary-lint", "--dictionary", str(path)])
    payload = json.loads(capsys.readouterr().out)

    assert exit_code == 1
    assert payload["status"] == "blocked"
    assert payload["issues"][0]["value"] == "投资变更"
