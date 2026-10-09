import json
from pathlib import Path

from tuomin_gateway.benchmark import (
    compare_risk_reduction_report,
    load_benchmark,
    measure,
    risk_reduction_report,
    run_benchmark,
)
from tuomin_gateway.detectors.base import BaseDetector

FIXTURE_DIR = Path(__file__).parent / "fixtures"
SAMPLES = FIXTURE_DIR / "redaction_benchmark.jsonl"
DICTIONARY = FIXTURE_DIR / "redaction_benchmark_dictionary.json"


class FakeNerDetector(BaseDetector):
    name = "model"
    version = "fake-ner"

    def __init__(self, values: list[str], *, fail: bool = False):
        self.values = values
        self.fail = fail

    def detect(self, text: str):
        if self.fail:
            raise RuntimeError(f"synthetic detector unavailable for {text}")
        spans = []
        for value in self.values:
            start = text.find(value)
            if start >= 0:
                spans.append(
                    self.make_span(
                        text=text,
                        start=start,
                        end=start + len(value),
                        label="ORG",
                        confidence=0.91,
                        risk_level="high",
                    )
                )
        return spans


def _span(text: str, value: str, category: str) -> dict[str, object]:
    start = text.index(value)
    return {
        "start": start,
        "end": start + len(value),
        "label": "ORG",
        "text": value,
        "category": category,
    }


def _small_samples() -> list[dict[str, object]]:
    gold_text = "合成客户甲在测试城推进离线评估。"
    decoy_text = "示例公司只是通用占位，不应默认脱敏。"
    return [
        {
            "sample_id": "synthetic-gold-1",
            "kind": "gold",
            "text": gold_text,
            "metadata": {"source": "synthetic", "scenario": "meeting_note", "doc_type": "note"},
            "gold_spans": [_span(gold_text, "合成客户甲", "unknown")],
        },
        {
            "sample_id": "synthetic-decoy-1",
            "kind": "decoy",
            "text": decoy_text,
            "metadata": {"source": "synthetic", "scenario": "meeting_note", "doc_type": "note"},
            "decoys": [
                {
                    "start": decoy_text.index("示例公司"),
                    "end": decoy_text.index("示例公司") + len("示例公司"),
                    "text": "示例公司",
                }
            ],
        },
    ]


def test_risk_reduction_reports_l0_l1_and_stratified_coverage():
    samples = load_benchmark(SAMPLES)
    baseline = run_benchmark(SAMPLES, DICTIONARY, use_ner=False)

    report = risk_reduction_report(samples, baseline)

    assert report["source"] == "synthetic"
    assert report["levels"]["L0"]["status"] == "baseline"
    assert report["levels"]["L0"]["leakage"] == baseline["gold_total"]
    assert report["levels"]["L0"]["leakage_rate"] == 1.0
    assert report["levels"]["L1"]["status"] == "implemented"
    assert report["levels"]["L1"]["leakage"] == baseline["overall"]["leakage"]
    assert baseline["overall"]["missed"] < report["levels"]["L1"]["leakage"] < baseline["overall"]["missed"] + baseline["overall"]["partial"]
    assert report["levels"]["L1"]["leakage_rate"] == report["levels"]["L1"]["leakage"] / baseline["gold_total"]
    assert report["levels"]["L1"]["relative_risk_reduction"] == 1 - report["levels"]["L1"]["leakage_rate"]
    assert report["levels"]["L2"]["status"].startswith("placeholder")
    assert report["levels"]["L3"]["status"] in {"not_implemented", "generalization_limited"}

    assert {"formatted", "known", "unknown", "quasi_identifier", "semantic_sensitive"}.issubset(
        report["coverage_by_category"]
    )
    assert {"contract", "pre_investment", "meeting_note", "legal_opinion", "risk_summary"}.issubset(
        report["coverage_by_scenario"]
    )
    for block in report["coverage_by_category"].values():
        assert {"total", "fully", "partial", "missed", "coverage_rate", "leakage_rate"}.issubset(block)


def test_risk_reduction_is_json_serializable_and_keeps_synthetic_boundary():
    report = risk_reduction_report(load_benchmark(SAMPLES), run_benchmark(SAMPLES, DICTIONARY))
    payload = json.dumps(report, ensure_ascii=False)

    assert "synthetic" in payload
    assert "sk-live" not in payload
    assert "AKIA" not in payload


def test_compare_report_calculates_fake_ner_unknown_gain_and_added_predictions():
    samples = _small_samples()
    l1a = measure(samples, detectors=[])
    l1b = measure(samples, detectors=[], ner=FakeNerDetector(["合成客户甲"]))

    report = compare_risk_reduction_report(samples, l1a, l1b)

    assert report["source"] == "synthetic"
    assert set(report["levels"]) == {"L0", "L1a", "L1b"}
    assert report["levels"]["L1a"]["leakage"] == 1.0
    assert report["levels"]["L1b"]["leakage"] == 0.0
    assert report["deltas"]["L1b_vs_L1a"]["relative_risk_reduction_delta"] == 1.0
    assert report["ner_effect"]["unknown_recall_lift"] == 1.0
    assert report["ner_effect"]["added_prediction_count"] == 1
    assert report["ner_effect"]["new_decoy_hits"] == 0
    assert "latency_summary" in report


def test_compare_report_surfaces_fake_ner_decoy_over_redaction():
    samples = _small_samples()
    l1a = measure(samples, detectors=[])
    l1b = measure(samples, detectors=[], ner=FakeNerDetector(["示例公司"]))

    report = compare_risk_reduction_report(samples, l1a, l1b)

    assert report["levels"]["L1b"]["decoy_protection"]["hit"] == 1
    assert report["levels"]["L1b"]["over_redaction"]["decoy_hit_rate"] == 1.0
    assert report["ner_effect"]["new_decoy_hits"] == 1


def test_ner_unavailable_degrades_without_leaking_sensitive_text():
    samples = _small_samples()

    result = measure(samples, detectors=[], ner=FakeNerDetector(["合成客户甲"], fail=True))
    payload = json.dumps(result, ensure_ascii=False)

    assert result["ner"]["status"] == "unavailable"
    assert result["overall"]["leakage"] == 1.0
    assert "合成客户甲" not in payload
    assert "RuntimeError" not in payload
