import json
from pathlib import Path

from tuomin_gateway.evaluation import evaluate_samples, load_synthetic_samples


FIXTURE_DIR = Path(__file__).parent / "fixtures"
SAMPLES = FIXTURE_DIR / "synthetic_samples.jsonl"
DICTIONARY = FIXTURE_DIR / "synthetic_dictionary.json"


def test_load_synthetic_samples_requires_gold_spans():
    samples = load_synthetic_samples(SAMPLES)

    assert samples
    for sample in samples:
        assert {"sample_id", "text", "gold_spans", "metadata"}.issubset(sample)
        assert sample["metadata"]["source"] == "synthetic"
        for gold in sample["gold_spans"]:
            assert {"start", "end", "label", "text"}.issubset(gold)
            assert sample["text"][gold["start"] : gold["end"]] == gold["text"]


def test_evaluate_samples_reports_per_label_metrics_and_refill_counts():
    report = evaluate_samples(SAMPLES, DICTIONARY)

    assert report["status"] == "ok"
    assert report["sample_count"] >= 3
    assert report["labels"]["ORG"]["tp"] >= 1
    assert report["labels"]["ORG"]["recall"] == 1.0
    assert "precision" in report["labels"]["CONTRACT_ID"]
    assert report["placeholder_consistency"]["refill_ok"] == report["sample_count"]
    assert report["placeholder_consistency"]["refill_blocked"] == 0


def test_evaluate_samples_counts_false_positive_and_false_negative(tmp_path):
    sample_path = tmp_path / "synthetic_eval.jsonl"
    sample = {
        "sample_id": "synthetic_fp_fn",
        "text": "合同编号：HT-2026-TM-0001，漏检合成项。",
        "gold_spans": [
            {"start": 21, "end": 26, "label": "PROJECT", "text": "漏检合成项"},
        ],
        "metadata": {"source": "synthetic", "doc_type": "unit"},
    }
    sample_path.write_text(json.dumps(sample, ensure_ascii=False) + "\n", encoding="utf-8")

    report = evaluate_samples(sample_path, DICTIONARY)

    assert report["labels"]["CONTRACT_ID"]["fp"] == 1
    assert report["labels"]["PROJECT"]["fn"] == 1
    assert report["totals"]["fp"] == 1
    assert report["totals"]["fn"] == 1


def test_evaluate_samples_distinguishes_boundary_or_label_mismatch(tmp_path):
    sample_path = tmp_path / "synthetic_mismatch.jsonl"
    sample = {
        "sample_id": "synthetic_mismatch",
        "text": "示例建设单位A参与。",
        "gold_spans": [
            {"start": 0, "end": 7, "label": "PROJECT", "text": "示例建设单位A"},
        ],
        "metadata": {"source": "synthetic", "doc_type": "unit"},
    }
    sample_path.write_text(json.dumps(sample, ensure_ascii=False) + "\n", encoding="utf-8")

    report = evaluate_samples(sample_path, DICTIONARY)

    assert report["totals"]["boundary_or_label_mismatch"] == 1
    assert report["mismatches"][0]["source"] == "synthetic"
    assert report["mismatches"][0]["type"] == "boundary_or_label_mismatch"
