"""Run Tuomin on three synthetic contract sections."""
from __future__ import annotations

import argparse
import json
import time

from tuomin_gateway.detectors.dictionary import DictionaryDetector
from tuomin_gateway.detectors.rules import RuleDetector
from tuomin_gateway.profiles import SENSITIVITY_PRESETS, with_overrides
from tuomin_gateway.redactor import redact_text
from tuomin_gateway.session import run_detection


from synthetic_contract_fixture import DICTIONARY, FIXTURE, reference_metrics, run_metadata


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--mode",
        choices=("rules-cold", "rules-dictionary", "rules-cluener", "dictionary-cluener"),
        required=True,
    )
    args = parser.parse_args()

    fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
    use_dictionary = "dictionary" in args.mode
    use_ner = "cluener" in args.mode
    detectors = [RuleDetector()]
    if use_dictionary:
        detectors.append(DictionaryDetector.from_json(DICTIONARY))
    profile = with_overrides(
        SENSITIVITY_PRESETS["standard"],
        name=f"synthetic-contract-{args.mode}",
        use_ner=use_ner,
        ner_required=use_ner,
    )

    started = time.perf_counter()
    cases = []
    sensitive_total = sensitive_handled = utility_total = utility_preserved = 0
    detector_states = None
    for case in fixture["cases"]:
        run = run_detection(case["text"], detectors, profile)
        result = redact_text(case["text"], run.kept)
        metrics = reference_metrics(case, result.redacted_text)
        sensitive_total += metrics["sensitive_total"]
        sensitive_handled += metrics["sensitive_handled"]
        utility_total += metrics["utility_total"]
        utility_preserved += metrics["utility_preserved"]
        detector_states = run.readiness.to_safe_dict()
        cases.append(
            {
                "case_id": case["id"],
                "section": case["section"],
                "masked_text": result.redacted_text,
                "metrics": metrics,
                "risk_summary": result.risk_summary,
                "detections": [
                    {
                        "label": span.label,
                        "source": span.source,
                        "surface": case["text"][span.start : span.end],
                    }
                    for span in run.kept
                ],
            }
        )

    elapsed = time.perf_counter() - started
    payload = {
        **run_metadata(),
        "engine": "Tuomin",
        "mode": args.mode,
        "case_count": len(cases),
        "metrics": {
            "sensitive_total": sensitive_total,
            "sensitive_handled": sensitive_handled,
            "sensitive_rate": sensitive_handled / sensitive_total,
            "utility_total": utility_total,
            "utility_preserved": utility_preserved,
            "utility_rate": utility_preserved / utility_total,
        },
        "elapsed_seconds": round(elapsed, 3),
        "detectors": detector_states,
        "cases": cases,
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
