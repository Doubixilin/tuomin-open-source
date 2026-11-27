"""Shared fixtures and measurements for the synthetic contract benchmark."""
from __future__ import annotations

from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
import sys


FIXTURES = Path(__file__).resolve().parents[3] / "tests" / "fixtures"
FIXTURE = FIXTURES / "synthetic_contract_benchmark.json"
DICTIONARY = FIXTURES / "synthetic_contract_benchmark_dictionary.json"


def run_metadata() -> dict:
    peak_rss_mib = None
    if sys.platform in ("linux", "darwin"):
        import resource

        rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        peak_rss_mib = round(rss / (1024**2 if sys.platform == "darwin" else 1024), 1)
    return {
        "schema_version": "synthetic-contract-result.v1",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source": "synthetic",
        "fixture_sha256": sha256(FIXTURE.read_bytes()).hexdigest(),
        "dictionary_sha256": sha256(DICTIONARY.read_bytes()).hexdigest(),
        "peak_rss_mib": peak_rss_mib,
    }


def reference_metrics(case: dict, masked: str) -> dict:
    sensitive = case["sensitive_references"]
    utility = case["utility_references"]
    handled = [item for item in sensitive if item["value"] not in masked]
    preserved = [value for value in utility if value in masked]
    return {
        "sensitive_total": len(sensitive),
        "sensitive_handled": len(handled),
        "sensitive_rate": len(handled) / len(sensitive) if sensitive else 1.0,
        "utility_total": len(utility),
        "utility_preserved": len(preserved),
        "utility_rate": len(preserved) / len(utility) if utility else 1.0,
    }
