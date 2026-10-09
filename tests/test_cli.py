import json
from pathlib import Path

from tuomin_gateway.cli import main
from tuomin_gateway.mapping import save_mapping
from tuomin_gateway.schemas import MappingEntry


FIXTURE_DIR = Path(__file__).parent / "fixtures"
SAMPLES = FIXTURE_DIR / "synthetic_samples.jsonl"
DICTIONARY = FIXTURE_DIR / "synthetic_dictionary.json"
BENCHMARK_SAMPLES = FIXTURE_DIR / "redaction_benchmark.jsonl"
BENCHMARK_DICTIONARY = FIXTURE_DIR / "redaction_benchmark_dictionary.json"


def synthetic_mapping():
    return [
        MappingEntry(
            placeholder="<ORG_001>",
            label="ORG",
            original_value="合成建设单位A",
            text_hash="sha256:org",
        ),
        MappingEntry(
            placeholder="<PROJECT_001>",
            label="PROJECT",
            original_value="合成绿洲项目",
            text_hash="sha256:project",
        ),
    ]


def test_cli_refill_blocks_placeholder_errors_without_leaking_original_values(tmp_path, capsys):
    mapping_path = tmp_path / "demo.mapping.json"
    save_mapping(synthetic_mapping(), mapping_path)

    exit_code = main(
        [
            "refill",
            "--text",
            "请复核 <ORG_001>、<PROJECT-001> 与 <PERSON_001>。",
            "--mapping",
            str(mapping_path),
        ]
    )

    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 1
    assert payload["status"] == "blocked"
    assert set(payload["error_types"]) == {
        "unknown_placeholder",
        "missing_placeholder",
        "altered_placeholder",
    }
    assert "合成建设单位A" not in json.dumps(payload, ensure_ascii=False)
    assert "合成绿洲项目" not in json.dumps(payload, ensure_ascii=False)


def test_cli_refill_reports_mapping_read_errors_as_safe_json(tmp_path, capsys):
    missing_path = tmp_path / "missing.mapping.json"

    exit_code = main(
        [
            "refill",
            "--text",
            "请复核 <ORG_001>。",
            "--mapping",
            str(missing_path),
        ]
    )

    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 2
    assert payload == {
        "status": "error",
        "error_type": "mapping_read_error",
        "message": "mapping file could not be read",
    }


def test_cli_redact_rejects_reserved_placeholder(capsys):
    exit_code = main(["redact", "--text", "伪造 <ORG_001> 内容"])

    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 2
    assert payload["status"] == "error"
    assert payload["error_type"] == "reserved_placeholder_conflict"


def test_cli_eval_outputs_safe_json(capsys):
    exit_code = main(
        [
            "eval",
            "--samples",
            str(SAMPLES),
            "--dictionary",
            str(DICTIONARY),
        ]
    )

    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert payload["status"] == "ok"
    assert payload["sample_count"] >= 3
    assert "labels" in payload
    assert "placeholder_consistency" in payload
    assert "示例建设单位A" not in json.dumps(payload, ensure_ascii=False)


def test_cli_eval_reports_read_errors_as_safe_json(tmp_path, capsys):
    exit_code = main(
        [
            "eval",
            "--samples",
            str(tmp_path / "missing.jsonl"),
            "--dictionary",
            str(DICTIONARY),
        ]
    )

    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 2
    assert payload == {
        "status": "error",
        "error_type": "eval_read_error",
        "message": "evaluation inputs could not be read",
    }

def test_cli_benchmark_risk_reduction_outputs_l0_l1_summary(capsys):
    exit_code = main(
        [
            "benchmark",
            "--samples",
            str(BENCHMARK_SAMPLES),
            "--dictionary",
            str(BENCHMARK_DICTIONARY),
            "--risk-reduction",
        ]
    )

    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert payload["status"] == "ok"
    assert payload["source"] == "synthetic"
    assert payload["risk_reduction"]["levels"]["L0"]["leakage_rate"] == 1.0
    assert payload["risk_reduction"]["levels"]["L1"]["status"] == "implemented"
    assert "coverage_by_category" in payload["risk_reduction"]
    assert "coverage_by_scenario" in payload["risk_reduction"]
    assert "sk-live" not in json.dumps(payload, ensure_ascii=False)

def test_cli_benchmark_compare_ner_outputs_synthetic_l1a_l1b_json_without_loading_ner(capsys):
    exit_code = main(
        [
            "benchmark",
            "--samples",
            str(BENCHMARK_SAMPLES),
            "--dictionary",
            str(BENCHMARK_DICTIONARY),
            "--compare-ner",
        ]
    )

    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert payload["status"] == "ok"
    assert payload["source"] == "synthetic"
    assert set(payload["risk_reduction"]["levels"]) == {"L0", "L1a", "L1b"}
    assert payload["benchmark_comparison"]["L1a"]["ner"]["status"] == "disabled"
    assert payload["benchmark_comparison"]["L1b"]["ner"]["status"] == "not_requested"
    assert payload["risk_reduction"]["levels"]["L1b"]["description"].startswith("rules+dictionary+local NER")
    assert payload["risk_reduction"]["limitations"][0].startswith("synthetic fixture only")
    assert "sk-live" not in json.dumps(payload, ensure_ascii=False)

def test_cli_benchmark_explicit_ner_unavailable_returns_safe_json(monkeypatch, capsys):
    from tuomin_gateway.detectors import ner as ner_module

    def unavailable_detector():
        raise RuntimeError("synthetic unavailable: 合成客户甲")

    monkeypatch.setattr(ner_module, "get_ner_detector", unavailable_detector)
    exit_code = main(
        [
            "benchmark",
            "--samples",
            str(BENCHMARK_SAMPLES),
            "--dictionary",
            str(BENCHMARK_DICTIONARY),
            "--ner",
        ]
    )

    payload = json.loads(capsys.readouterr().out)
    dumped = json.dumps(payload, ensure_ascii=False)
    assert exit_code == 0
    assert payload["ner"]["status"] == "unavailable"
    assert payload["ner"]["failure_stage"] == "load"
    assert "合成客户甲" not in dumped
    assert "RuntimeError" not in dumped
