from pathlib import Path

from tuomin_gateway.benchmark import compare_core_scope_report, run_benchmark

FIXTURE_DIR = Path(__file__).parent / "fixtures"
SAMPLES = FIXTURE_DIR / "redaction_benchmark.jsonl"
DICTIONARY = FIXTURE_DIR / "redaction_benchmark_dictionary.json"


def test_rules_dictionary_core_scope_excludes_semantic_and_quasi_denominators():
    result = run_benchmark(SAMPLES, DICTIONARY, use_ner=False)

    core_scope = result["core_scope"]

    assert core_scope["gold_total"] == 240
    assert core_scope["semantic_policy_count"] == 43
    assert core_scope["quasi_generalization_count"] == 62

    assert (core_scope["overall"]["total"], core_scope["overall"]["fully"]) == (240, 186)
    assert core_scope["overall"]["partial"] == 0
    assert core_scope["overall"]["missed"] == 54
    assert round(core_scope["overall"]["leakage"], 3) == 54.000
    assert round(core_scope["overall"]["leakage_rate"], 3) == 0.225
    assert round(core_scope["overall"]["risk_reduction"], 3) == 0.775

    groups = core_scope["groups"]
    assert groups["personal"]["total"] == 51
    assert groups["enterprise"]["total"] == 73
    assert groups["project"]["total"] == 24
    assert groups["locator"]["total"] == 92


def test_core_scope_comparison_reports_l1b_delta_without_counting_policy_spans():
    l1a = run_benchmark(SAMPLES, DICTIONARY, use_ner=False)
    l1b = run_benchmark(SAMPLES, DICTIONARY, use_ner=False)
    l1b["core_scope"]["overall"]["leakage"] = 40.0
    l1b["core_scope"]["overall"]["leakage_rate"] = 40.0 / l1b["core_scope"]["gold_total"]
    l1b["core_scope"]["overall"]["risk_reduction"] = 1 - l1b["core_scope"]["overall"]["leakage_rate"]

    report = compare_core_scope_report(l1a, l1b)

    assert report["gold_total"] == 240
    assert report["semantic_policy_count"] == 43
    assert report["quasi_generalization_count"] == 62
    assert report["levels"]["L1a"]["leakage"] == l1a["core_scope"]["overall"]["leakage"]
    assert report["levels"]["L1b"]["leakage"] == 40.0
    assert report["deltas"]["L1b_vs_L1a"]["leakage_delta"] < 0
    assert report["deltas"]["L1b_vs_L1a"]["relative_risk_reduction_delta"] > 0
