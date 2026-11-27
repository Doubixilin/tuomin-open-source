import json
import os
from pathlib import Path
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests/fixtures/synthetic_contract_benchmark.json"
EVALUATOR = ROOT / "docs/competition/eval/evaluate_synthetic_contract_tuomin.py"


def test_synthetic_contract_references_and_detection():
    fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
    assert fixture["source"] == "synthetic"
    for case in fixture["cases"]:
        for reference in case["sensitive_references"]:
            assert reference["value"] in case["text"]
        for reference in case["utility_references"]:
            assert reference in case["text"]

    env = dict(os.environ, PYTHONPATH=str(ROOT / "src"), PYTHONIOENCODING="utf-8")
    for mode in ("rules-cold", "rules-dictionary"):
        raw = subprocess.check_output(
            [sys.executable, str(EVALUATOR), "--mode", mode], cwd=ROOT, env=env, encoding="utf-8"
        )
        result = json.loads(raw)
        assert result["case_count"] == 3
        assert result["metrics"]["sensitive_total"] == 13
        assert result["metrics"]["utility_preserved"] == result["metrics"]["utility_total"] == 15
        for case, measured in zip(fixture["cases"], result["cases"]):
            assert measured["case_id"] == case["id"]
            for reference in case["sensitive_references"]:
                if mode == "rules-dictionary" or reference["label"] == "CONTACT":
                    assert reference["value"] not in measured["masked_text"]
