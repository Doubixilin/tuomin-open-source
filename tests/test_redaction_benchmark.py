"""Regression lock for the redaction-rate benchmark (rules + dictionary, no NER).

These numbers back the honest headline ("能量化漏多少"). They must not drift
silently: any detector/fusion/profile change that moves coverage will fail here
and force the report to be updated deliberately. NER is intentionally excluded
so this test needs zero optional dependencies and no network.
"""
from pathlib import Path

from tuomin_gateway.benchmark import load_benchmark, run_benchmark

FIXTURE_DIR = Path(__file__).parent / "fixtures"
SAMPLES = FIXTURE_DIR / "redaction_benchmark.jsonl"
DICTIONARY = FIXTURE_DIR / "redaction_benchmark_dictionary.json"


def test_benchmark_fixture_loads_and_validates_boundaries():
    samples = load_benchmark(SAMPLES)
    gold = [s for s in samples if s["kind"] == "gold"]
    decoy = [s for s in samples if s["kind"] == "decoy"]
    assert len(gold) >= 100
    assert len(decoy) >= 7
    assert sum(len(s["gold_spans"]) for s in gold) >= 250
    assert sum(len(s["decoys"]) for s in decoy) >= 21

    scenarios = {s["metadata"]["scenario"] for s in gold}
    assert {"contract", "pre_investment", "meeting_note", "legal_opinion", "risk_summary"}.issubset(
        scenarios
    )
    categories = {span["category"] for sample in gold for span in sample["gold_spans"]}
    assert {
        "formatted",
        "known",
        "unknown",
        "quasi_identifier",
        "semantic_sensitive",
    }.issubset(categories)

    for sample in samples:
        metadata = sample["metadata"]
        assert metadata["source"] == "synthetic"
        assert metadata.get("scenario")
        assert metadata.get("doc_type")


def test_benchmark_rules_plus_dictionary_baseline():
    result = run_benchmark(SAMPLES, DICTIONARY, use_ner=False)

    assert result["use_ner"] is False
    assert result["sample_count"] == 110
    assert result["gold_total"] == 345
    # 229 -> 228 when amount_cny stopped truncating un-grouped digit runs;
    # 228 -> 227 when SCALE text such as 18万平方米 stopped being counted as
    # a money prediction. SCALE remains an honest miss without an applicable
    # detector instead of a misleading AMOUNT partial hit.
    assert result["n_pred"] == 227

    formatted = result["categories"]["formatted"]
    assert (formatted["total"], formatted["fully"], formatted["partial"], formatted["missed"]) == (116, 113, 3, 0)

    known = result["categories"]["known"]
    assert (known["total"], known["fully"], known["partial"], known["missed"]) == (59, 59, 0, 0)

    # org_by_cue still gives a small no-NER lift, but the expanded fixture now
    # deliberately includes more hard unknown, quasi-identifier, and semantic spans.
    unknown = result["categories"]["unknown"]
    assert (unknown["total"], unknown["fully"], unknown["partial"], unknown["missed"]) == (93, 39, 0, 54)

    quasi = result["categories"]["quasi_identifier"]
    assert (quasi["total"], quasi["fully"], quasi["partial"], quasi["missed"]) == (34, 2, 3, 29)

    semantic = result["categories"]["semantic_sensitive"]
    assert (semantic["total"], semantic["fully"], semantic["partial"], semantic["missed"]) == (43, 0, 0, 43)

    overall = result["overall"]
    assert (overall["total"], overall["fully"], overall["partial"], overall["missed"]) == (345, 213, 6, 126)
    assert round(overall["rate"], 3) == 0.617
    assert round(overall["leakage"], 3) == 127.901
    assert set(result["coverage_by_scenario"]) == {
        "contract",
        "hr_profile",
        "legal_opinion",
        "meeting_note",
        "pre_investment",
        "risk_summary",
    }

    decoy = result["decoy_protection"]
    assert (decoy["total"], decoy["hit"]) == (21, 0)
    assert decoy["rate"] == 1.0
