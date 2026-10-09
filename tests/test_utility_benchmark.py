import json
from pathlib import Path

from tuomin_gateway.benchmark import load_utility_benchmark, run_benchmark, run_utility_benchmark


FIXTURE_DIR = Path(__file__).parent / "fixtures"
SAMPLES = FIXTURE_DIR / "redaction_benchmark.jsonl"
DICTIONARY = FIXTURE_DIR / "redaction_benchmark_dictionary.json"
UTILITY = FIXTURE_DIR / "utility_decoy_benchmark.jsonl"


def test_utility_fixture_loads_and_is_synthetic_only():
    samples = load_utility_benchmark(UTILITY)

    assert len(samples) == 6
    assert all(sample["metadata"]["source"] == "synthetic" for sample in samples)
    assert sum(len(sample["utility_spans"]) for sample in samples) >= 20
    for sample in samples:
        for span in sample["utility_spans"]:
            assert sample["text"][span["start"] : span["end"]] == span["text"]


def test_utility_report_is_hash_only_and_has_stable_metrics():
    samples = load_utility_benchmark(UTILITY)
    report = run_utility_benchmark(UTILITY, DICTIONARY)
    payload = json.dumps(report, ensure_ascii=False)

    assert report["source"] == "synthetic"
    assert report["safe"] is True
    assert report["raw_text_included"] is False
    assert set(report["metrics"]) >= {
        "core_leakage_rate",
        "decoy_hit_rate",
        "semantic_preserve_rate",
        "quasi_preserve_or_generalize_rate",
    }
    assert "over_redaction_by_label" in report
    assert "over_redaction_by_scenario" in report
    assert "utility_protection_summary" in report
    for sample in samples:
        assert sample["text"] not in payload
        for span in sample["utility_spans"]:
            assert span["text"] not in payload


def test_semantic_and_quasi_policy_boundaries_are_reported():
    report = run_utility_benchmark(UTILITY, DICTIONARY)

    assert report["metrics"]["semantic_preserve_rate"] == 1.0
    assert report["metrics"]["quasi_preserve_or_generalize_rate"] == 1.0
    assert report["review_queue"]["summary"]["by_group"]["semantic_policy"] == 3
    assert report["strategy_summary"]["standard"]["by_label"]["RISK_JUDGMENT"]["policy_action"] == "pass"
    assert report["strategy_summary"]["review"]["by_label"]["RISK_JUDGMENT"]["policy_action"] == "warn"
    assert report["strategy_summary"]["strict"]["by_label"]["MILESTONE"]["policy_action"] == "warn"
    assert report["strategy_summary"]["strict"]["by_label"]["AMOUNT"]["policy_action"] == "generalize"


def test_litigation_and_contract_review_preserve_legal_analysis_values():
    report = run_utility_benchmark(UTILITY, DICTIONARY)

    for strategy in ("litigation", "contract_review"):
        by_label = report["strategy_summary"][strategy]["by_label"]
        assert by_label["COURT"]["policy_action"] == "pass"
        assert by_label["STATUTE"]["policy_action"] == "pass"
        assert by_label["AMOUNT"]["policy_action"] == "pass"
        assert by_label["DATE"]["policy_action"] == "pass"


def test_standard_benchmark_includes_utility_protection_without_changing_core_counts():
    result = run_benchmark(SAMPLES, DICTIONARY, use_ner=False)

    assert result["core_scope"]["gold_total"] == 240
    assert result["core_scope"]["semantic_policy_count"] == 43
    assert result["core_scope"]["quasi_generalization_count"] == 62
    assert result["utility_protection"]["metrics"]["decoy_hit_rate"] == 0.0
    assert result["utility_protection"]["metrics"]["semantic_preserve_rate"] == 1.0
