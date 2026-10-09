from __future__ import annotations

import json
from pathlib import Path

from tuomin_gateway.detectors.adapters import FakeModelDetectorAdapter
from tuomin_gateway.evaluation import evaluate_samples


FIXTURE_DIR = Path(__file__).parent / "fixtures"
DICTIONARY = FIXTURE_DIR / "synthetic_dictionary.json"


def test_evaluate_samples_accepts_extra_detector_without_enabling_by_default(tmp_path):
    sample_path = tmp_path / "synthetic_extra_detector.jsonl"
    sample = {
        "sample_id": "synthetic_extra_detector",
        "text": "Alice joins the synthetic review.",
        "gold_spans": [
            {"start": 0, "end": 5, "label": "PERSON", "text": "Alice"},
        ],
        "metadata": {"source": "synthetic", "doc_type": "unit"},
    }
    sample_path.write_text(json.dumps(sample, ensure_ascii=False) + "\n", encoding="utf-8")
    fake_detector = FakeModelDetectorAdapter(
        outputs=[{"start": 0, "end": 5, "label": "PERSON", "score": 0.91}],
    )

    default_report = evaluate_samples(sample_path, DICTIONARY)
    adapter_report = evaluate_samples(sample_path, DICTIONARY, extra_detectors=[fake_detector])

    assert default_report["totals"]["fn"] == 1
    assert default_report["labels"]["PERSON"]["tp"] == 0
    assert default_report["labels"]["PERSON"]["fn"] == 1
    assert adapter_report["labels"]["PERSON"]["tp"] == 1
    assert adapter_report["totals"]["fn"] == 0
