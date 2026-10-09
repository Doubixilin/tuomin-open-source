import json
from pathlib import Path

from tuomin_gateway.benchmark import measure, run_benchmark, run_ner_comparison
from tuomin_gateway.detectors.base import BaseDetector

FIXTURE_DIR = Path(__file__).parent / "fixtures"
SAMPLES = FIXTURE_DIR / "redaction_benchmark.jsonl"
DICTIONARY = FIXTURE_DIR / "redaction_benchmark_dictionary.json"


class ExactDetector(BaseDetector):
    name = "model"
    version = "fake-local-ner"

    def __init__(self, values: list[str], label: str = "ORG"):
        self.values = values
        self.label = label

    def detect(self, text: str):
        spans = []
        for value in self.values:
            start = text.find(value)
            if start >= 0:
                spans.append(
                    self.make_span(
                        text=text,
                        start=start,
                        end=start + len(value),
                        label=self.label,
                        confidence=0.93,
                        risk_level="high",
                    )
                )
        return spans


def _gold_sample(text: str, value: str, label: str = "ORG") -> dict:
    start = text.index(value)
    return {
        "sample_id": "synthetic-audit-1",
        "kind": "gold",
        "text": text,
        "metadata": {"source": "synthetic", "scenario": "contract", "doc_type": "contract"},
        "gold_spans": [
            {
                "start": start,
                "end": start + len(value),
                "label": label,
                "text": value,
                "category": "unknown",
            }
        ],
    }


def test_core_failure_audit_keeps_hashes_not_raw_sensitive_values():
    text = "Counterparty Synthetic Alpha Supplier appears in the memo."
    sensitive = "Synthetic Alpha Supplier"

    result = measure([_gold_sample(text, sensitive)], detectors=[])
    payload = json.dumps(result["residual_audit"], ensure_ascii=False)

    failure = result["residual_audit"]["core_failures"][0]
    assert failure["sample_id"] == "synthetic-audit-1"
    assert failure["status"] == "missed"
    assert failure["group"] == "enterprise"
    assert failure["gold_hash"].startswith("sha256:")
    assert "text" not in failure
    assert sensitive not in payload


def test_residual_audit_aggregates_missed_partial_label_group_and_scenario():
    missed_text = "Vendor Synthetic Alpha Supplier."
    partial_text = "Vendor Synthetic Beta Company."
    missed_value = "Synthetic Alpha Supplier"
    partial_value = "Synthetic Beta Company"
    samples = [
        _gold_sample(missed_text, missed_value),
        _gold_sample(partial_text, partial_value),
    ]

    result = measure(samples, detectors=[], ner=ExactDetector(["Synthetic Beta"]))
    audit = result["residual_audit"]

    assert audit["summary"]["missed"] == 1
    assert audit["summary"]["partial"] == 1
    assert audit["summary"]["by_label"]["ORG"] == 2
    assert audit["summary"]["by_group"]["enterprise"] == 2
    assert audit["summary"]["by_scenario"]["contract"] == 2


def test_enterprise_residuals_are_queryable_on_fixture():
    result = run_benchmark(SAMPLES, DICTIONARY, use_ner=False)
    audit = result["residual_audit"]

    assert audit["summary"]["by_group"]["enterprise"] == 2
    assert audit["summary"]["by_label"].get("ORG", 0) + audit["summary"]["by_label"].get("SUPPLIER", 0) > 0
    assert set(audit["summary"]["by_scenario"]).issuperset({"contract", "meeting_note"})


def test_compare_ner_safe_mode_reports_audit_without_loading_real_ner():
    report = run_ner_comparison(SAMPLES, DICTIONARY, use_ner=False)

    l1b = report["benchmark_comparison"]["L1b"]
    assert l1b["ner"]["status"] == "not_requested"
    assert l1b["ner"]["diagnostics"]["raw_ner_predictions"] == []
    assert l1b["residual_audit"]["summary"]["missed"] == 54


def test_fake_local_ner_diagnostics_shape_is_stable_and_safe():
    text = "Counterparty Synthetic Alpha Supplier works with Synthetic Beta Company."
    samples = [_gold_sample(text, "Synthetic Alpha Supplier")]

    result = measure(samples, detectors=[], ner=ExactDetector(["Synthetic Alpha"]))
    diagnostics = result["ner"]["diagnostics"]
    payload = json.dumps(diagnostics, ensure_ascii=False)

    assert diagnostics["raw_ner_prediction_count"] == 1
    assert diagnostics["fused_ner_kept_count"] == 1
    assert diagnostics["ner_dropped_by_fusion_count"] == 0
    assert diagnostics["ner_partial_or_missed_counts"]["partial"] == 1
    assert diagnostics["ner_partial_or_missed_counts"]["by_group"]["enterprise"] == 1
    assert "Synthetic Alpha" not in payload
    assert diagnostics["raw_ner_predictions"][0]["text_hash"].startswith("sha256:")


def test_semantic_policy_stays_out_of_core_denominator_and_decoy_stats_remain():
    result = run_benchmark(SAMPLES, DICTIONARY, use_ner=False)

    assert result["gold_total"] == 345
    assert result["core_scope"]["gold_total"] == 240
    assert result["core_scope"]["semantic_policy_count"] == 43
    assert result["decoy_protection"]["total"] == 21
    assert result["decoy_protection"]["hit"] == 0


def test_rules_dictionary_fixed_locator_and_project_residuals_are_regression_locked():
    result = run_benchmark(SAMPLES, DICTIONARY, use_ner=False)
    audit = result["residual_audit"]
    by_label = audit["summary"]["by_label"]

    assert result["core_scope"]["overall"]["missed"] == 54
    assert round(result["core_scope"]["overall"]["leakage"], 3) == 54.000
    assert by_label.get("CONTRACT_ID", 0) == 0
    assert by_label.get("BANK_ACCOUNT", 0) == 0
    assert by_label.get("CREDENTIAL", 0) == 0
    assert by_label.get("PROJECT", 0) == 0
    assert result["decoy_protection"]["hit"] == 0


def test_ner_diagnostics_new_count_fields_are_stable_and_hash_only():
    text = "Counterparty Synthetic Alpha Supplier works with Synthetic Beta Company."
    samples = [_gold_sample(text, "Synthetic Alpha Supplier")]

    result = measure(samples, detectors=[], ner=ExactDetector(["Synthetic Alpha"]))
    diagnostics = result["ner"]["diagnostics"]
    payload = json.dumps(diagnostics, ensure_ascii=False)

    assert diagnostics["raw_ner_prediction_count"] == 1
    assert diagnostics["raw_ner_preserved_exact_count"] == 1
    assert diagnostics["raw_ner_dropped_or_clipped_count"] == 0
    assert diagnostics["fused_model_span_count"] == 1
    assert "Synthetic Alpha" not in payload
    assert diagnostics["raw_ner_predictions"][0]["text_hash"].startswith("sha256:")
