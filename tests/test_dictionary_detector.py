from pathlib import Path
import json
import subprocess
import sys

from tuomin_gateway.detectors.dictionary import DictionaryDetector
from tuomin_gateway.profiles import get_profile, with_overrides
from tuomin_gateway.session import SessionRedactor


FIXTURE = Path(__file__).parent / "fixtures" / "synthetic_dictionary.json"


def test_dictionary_detector_matches_aliases_with_canonical_metadata():
    detector = DictionaryDetector.from_json(FIXTURE)

    spans = detector.detect("示建A参与绿洲MVP，由供应商乙方配合。")

    by_label = {span.label: span for span in spans}
    assert by_label["ORG"].metadata["canonical_value"] == "示例建设单位A"
    assert by_label["PROJECT"].metadata["canonical_value"] == "测试绿洲项目"
    assert by_label["SUPPLIER"].metadata["risk_level"] == "high"


def test_dictionary_detector_ignores_inactive_entries_and_duplicate_aliases():
    detector = DictionaryDetector.from_entries(
        [
            {
                "entry_id": "active_org",
                "canonical_value": "合成建设单位A",
                "aliases": ["合成甲方", "合成甲方"],
                "label": "ORG",
                "risk_level": "high",
                "status": "active",
            },
            {
                "entry_id": "inactive_project",
                "canonical_value": "停用合成项目",
                "aliases": ["停用项目"],
                "label": "PROJECT",
                "risk_level": "high",
                "status": "inactive",
            },
        ]
    )

    spans = detector.detect("合成甲方与停用项目仅用于测试。")

    assert [span.label for span in spans] == ["ORG"]


def test_dictionary_detector_ignores_empty_alias_without_hanging(tmp_path):
    entries_path = tmp_path / "dictionary.json"
    entries_path.write_text(
        json.dumps(
            [
                {
                    "entry_id": "empty_alias",
                    "canonical_value": "不会出现的合成名称",
                    "aliases": ["", "   "],
                    "label": "ORG",
                    "risk_level": "high",
                    "status": "active",
                }
            ],
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    script = (
        "from pathlib import Path; import sys; "
        "sys.path.insert(0, str(Path.cwd() / 'src')); "
        "from tuomin_gateway.detectors.dictionary import DictionaryDetector; "
        f"spans = DictionaryDetector.from_json({str(entries_path)!r}).detect('无匹配合成文本'); "
        "print(len(spans))"
    )

    completed = subprocess.run(
        [sys.executable, "-c", script],
        cwd=Path(__file__).parents[1],
        text=True,
        capture_output=True,
        timeout=2,
        check=True,
    )

    assert completed.stdout.strip() == "0"


def test_canonical_aliases_share_one_session_placeholder():
    detector = DictionaryDetector.from_entries(
        [
            {
                "entry_id": "org-1",
                "canonical_value": "示例建设第八工程局有限公司",
                "aliases": ["示例建设八局"],
                "label": "ORG",
                "risk_level": "high",
                "status": "active",
            }
        ]
    )
    profile = with_overrides(get_profile("kb"), use_ner=False)
    redactor = SessionRedactor([detector], profile, identity_mode="canonical")

    canonical = redactor.mask("示例建设第八工程局有限公司")
    alias = redactor.mask("示例建设八局")

    assert canonical == alias == "<ORG_001>"
    assert redactor.mapping["<ORG_001>"] == "示例建设第八工程局有限公司"


def test_dictionary_repairs_unique_leading_character_ocr_truncation():
    detector = DictionaryDetector.from_entries(
        [
            {
                "entry_id": "org-1",
                "canonical_value": "上海示例甲置业有限公司",
                "aliases": ["示例甲公司"],
                "label": "ORG",
                "risk_level": "high",
                "status": "active",
                "allow_leading_ocr_truncation": True,
            }
        ]
    )

    spans = detector.detect("例甲公司延续上一页内容")

    assert len(spans) == 1
    assert spans[0].metadata["canonical_value"] == "上海示例甲置业有限公司"
    assert spans[0].metadata["boundary_repair"] == "leading_character_truncation"


def test_dictionary_skips_ambiguous_leading_character_repair():
    detector = DictionaryDetector.from_entries(
        [
            {
                "entry_id": "org-1",
                "canonical_value": "甲例甲公司",
                "aliases": [],
                "label": "ORG",
                "status": "active",
                "allow_leading_ocr_truncation": True,
            },
            {
                "entry_id": "org-2",
                "canonical_value": "乙例甲公司",
                "aliases": [],
                "label": "ORG",
                "status": "active",
                "allow_leading_ocr_truncation": True,
            },
        ]
    )

    assert detector.detect("例甲公司延续上一页内容") == []


def test_dictionary_does_not_fuzzy_match_without_explicit_ocr_opt_in():
    detector = DictionaryDetector.from_entries(
        [
            {
                "entry_id": "org-1",
                "canonical_value": "上海示例甲置业有限公司",
                "aliases": ["示例甲公司"],
                "label": "ORG",
                "status": "active",
            }
        ]
    )

    assert detector.detect("例甲公司是另一普通表述") == []
