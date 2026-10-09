"""Redaction-rate and relative-risk-reduction benchmark over synthetic fixtures.

This module is deliberately separate from ``evaluation.py``: that one scores
label-aware, boundary-exact precision/recall; this one scores conservative leak
coverage: was the sensitive VALUE covered by any detection span, and how much
residual leakage remains versus an L0 raw-text baseline.
"""
from __future__ import annotations

import copy
import json
import math
import time
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from tuomin_gateway.detectors.base import BaseDetector, hash_text
from tuomin_gateway.fusion import fuse_detections
from tuomin_gateway.schemas import DetectionSpan

PREFERRED_CATEGORIES = (
    "formatted",
    "known",
    "unknown",
    "quasi_identifier",
    "semantic_sensitive",
)

CORE_SCOPE_GROUP_BY_LABEL = {
    "PERSON": "personal",
    "ID_CARD": "personal",
    "EMPLOYEE_ID": "personal",
    "ORG": "enterprise",
    "SUPPLIER": "enterprise",
    "DEPARTMENT": "enterprise",
    "PROJECT": "project",
    "LAND_PARCEL": "project",
    "CONTACT": "locator",
    "CONTRACT_ID": "locator",
    "BID_ID": "locator",
    "BANK_ACCOUNT": "locator",
    "CREDENTIAL": "locator",
    "SYSTEM_URL": "locator",
    "ORG_CODE": "locator",
    "ADDRESS": "locator",
    "LOCATION": "locator",
}

PREFERRED_CORE_GROUPS = ("personal", "enterprise", "project", "locator")

SEMANTIC_POLICY_LABELS = frozenset(
    {
        "INTERNAL_OPINION",
        "LEGAL_STRATEGY",
        "RISK_JUDGMENT",
        "NEGOTIATION_FLOOR",
        "NEGOTIATION_STRATEGY",
        "NEGOTIATION_POSITION",
        "INTERNAL_PROCESS",
    }
)

QUASI_GENERALIZATION_LABELS = frozenset(
    {
        "AMOUNT",
        "DATE",
        "SCALE",
        "TIMELINE",
        "MILESTONE",
        "LOCATION_CONTEXT",
        "DISCOUNT_RATE",
    }
)

UTILITY_POLICY_STRATEGIES = (
    "standard",
    "strict",
    "review",
    "contract_review",
    "litigation",
)
UTILITY_PRESERVE_ACTIONS = {"pass", "generalize", "warn"}
UTILITY_PLACEHOLDER_ACTIONS = {"redact", "block"}


EMPTY_COVERAGE = {
    "total": 0,
    "fully": 0,
    "partial": 0,
    "missed": 0,
    "rate": 0.0,
    "coverage_rate": 0.0,
    "leakage": 0.0,
    "leakage_rate": 0.0,
    "risk_reduction": 0.0,
    "ci": [0.0, 0.0],
}


def load_benchmark(samples_path: str | Path) -> list[dict[str, Any]]:
    """Load + boundary-validate the benchmark fixture (gold + decoy samples)."""
    samples: list[dict[str, Any]] = []
    for line_number, line in enumerate(
        Path(samples_path).read_text(encoding="utf-8").splitlines(), start=1
    ):
        if not line.strip():
            continue
        sample = json.loads(line)
        _validate_sample(sample, line_number)
        samples.append(sample)
    return samples


def load_utility_benchmark(samples_path: str | Path) -> list[dict[str, Any]]:
    """Load synthetic utility/near-miss samples and materialize safe span bounds."""
    samples: list[dict[str, Any]] = []
    for line_number, line in enumerate(
        Path(samples_path).read_text(encoding="utf-8").splitlines(), start=1
    ):
        if not line.strip():
            continue
        sample = json.loads(line)
        _validate_utility_sample(sample, line_number)
        samples.append(sample)
    return samples


def run_utility_benchmark(
    samples_path: str | Path,
    dictionary_path: str | Path,
    core_leakage_rate: float = 0.0,
) -> dict[str, Any]:
    """Evaluate synthetic near-miss and utility-preservation spans safely."""
    return measure_utility(
        load_utility_benchmark(samples_path),
        _default_detectors(dictionary_path),
        core_leakage_rate=core_leakage_rate,
    )


def measure_utility(
    samples: list[dict[str, Any]],
    detectors: list[BaseDetector],
    core_leakage_rate: float = 0.0,
) -> dict[str, Any]:
    """Return hash-only utility, over-redaction, policy, and review metrics."""
    decoy_total = decoy_hits = 0
    semantic_total = semantic_preserved = 0
    quasi_total = quasi_preserve_or_generalize = 0
    span_count = 0
    over_by_label: Counter[str] = Counter()
    over_by_scenario: Counter[str] = Counter()
    category_counts: Counter[str] = Counter()
    review_items: list[dict[str, Any]] = []
    strategy_summary: dict[str, dict[str, Any]] = {
        strategy: {"by_label": {}, "by_action": Counter()}
        for strategy in UTILITY_POLICY_STRATEGIES
    }

    for sample in samples:
        text = sample["text"]
        scenario = sample["metadata"].get("scenario", "unknown")
        detections = fuse_detections([span for detector in detectors for span in detector.detect(text)], text)
        for detection in detections:
            if detection.source == "model" and detection.confidence < 0.75:
                review_items.append(
                    _review_item(
                        sample_id=sample["sample_id"],
                        label=detection.label,
                        group="low_confidence_ner",
                        policy_action="review",
                        reason="low confidence model span",
                        risk_level=detection.risk_level,
                        text_hash=detection.text_hash,
                    )
                )
        for utility_span in sample["utility_spans"]:
            span_count += 1
            label = utility_span["label"]
            category = utility_span.get("category", "unknown")
            category_counts[category] += 1
            hit = _overlaps_any(utility_span["start"], utility_span["end"], detections)
            standard_action = _utility_policy_action("standard", utility_span)
            placeholder_hit = hit and standard_action in UTILITY_PLACEHOLDER_ACTIONS

            if category == "decoy":
                decoy_total += 1
                if placeholder_hit:
                    decoy_hits += 1
            elif category == "semantic_sensitive":
                semantic_total += 1
                if standard_action in UTILITY_PRESERVE_ACTIONS and not placeholder_hit:
                    semantic_preserved += 1
                review_items.append(
                    _review_item(
                        sample_id=sample["sample_id"],
                        label=label,
                        group="semantic_policy",
                        policy_action="warn",
                        reason="semantic policy review",
                        risk_level=utility_span.get("risk_level", "medium"),
                        text_hash=hash_text(utility_span["text"]),
                    )
                )
            elif category == "quasi_identifier":
                quasi_total += 1
                if standard_action in UTILITY_PRESERVE_ACTIONS and not placeholder_hit:
                    quasi_preserve_or_generalize += 1
                if _utility_policy_action("strict", utility_span) == "warn":
                    review_items.append(
                        _review_item(
                            sample_id=sample["sample_id"],
                            label=label,
                            group="quasi_generalization",
                            policy_action="warn",
                            reason="strict strategy quasi review",
                            risk_level=utility_span.get("risk_level", "medium"),
                            text_hash=hash_text(utility_span["text"]),
                        )
                    )

            if placeholder_hit:
                over_by_label[label] += 1
                over_by_scenario[scenario] += 1

            for strategy in UTILITY_POLICY_STRATEGIES:
                action = _utility_policy_action(strategy, utility_span)
                strategy_block = strategy_summary[strategy]
                strategy_block["by_action"][action] += 1
                label_entry = strategy_block["by_label"].setdefault(
                    label,
                    {"policy_action": action, "count": 0, "categories": []},
                )
                label_entry["count"] += 1
                if category not in label_entry["categories"]:
                    label_entry["categories"].append(category)
                if label_entry["policy_action"] != action:
                    label_entry["policy_action"] = "mixed"

    safe_strategy_summary = {
        strategy: {
            "by_action": _safe_counter(block["by_action"]),
            "by_label": {
                label: {
                    "policy_action": entry["policy_action"],
                    "count": int(entry["count"]),
                    "categories": sorted(entry["categories"]),
                }
                for label, entry in sorted(block["by_label"].items())
            },
        }
        for strategy, block in strategy_summary.items()
    }
    return {
        "source": "synthetic",
        "safe": True,
        "raw_text_included": False,
        "sample_count": len(samples),
        "utility_span_count": span_count,
        "metrics": {
            "core_leakage_rate": core_leakage_rate,
            "decoy_hit_rate": decoy_hits / decoy_total if decoy_total else 0.0,
            "semantic_preserve_rate": semantic_preserved / semantic_total if semantic_total else 0.0,
            "quasi_preserve_or_generalize_rate": quasi_preserve_or_generalize / quasi_total if quasi_total else 0.0,
        },
        "over_redaction_by_label": _safe_counter(over_by_label),
        "over_redaction_by_scenario": _safe_counter(over_by_scenario),
        "strategy_summary": safe_strategy_summary,
        "review_queue": _review_queue_block(review_items),
        "utility_protection_summary": {
            "decoy_total": decoy_total,
            "decoy_hits": decoy_hits,
            "semantic_policy_total": semantic_total,
            "semantic_policy_preserved": semantic_preserved,
            "quasi_total": quasi_total,
            "quasi_preserve_or_generalize": quasi_preserve_or_generalize,
            "category_counts": _safe_counter(category_counts),
            "limitations": [
                "synthetic utility fixture only; not a real-material over-redaction rate",
                "policy actions are report-only for utility diagnostics unless a caller applies the profile layer",
            ],
        },
    }


def measure(
    samples: list[dict[str, Any]],
    detectors: list[BaseDetector],
    ner: BaseDetector | None = None,
) -> dict[str, Any]:
    """Run injected detectors over samples and return safe coverage metrics."""
    started = time.perf_counter()
    cat: dict[str, dict[str, int]] = defaultdict(lambda: {"fully": 0, "partial": 0, "missed": 0})
    scenario: dict[str, dict[str, int]] = defaultdict(lambda: {"fully": 0, "partial": 0, "missed": 0})
    cat_leakage: dict[str, float] = defaultdict(float)
    scenario_leakage: dict[str, float] = defaultdict(float)
    core_scope_counts: dict[str, dict[str, int]] = defaultdict(
        lambda: {"fully": 0, "partial": 0, "missed": 0}
    )
    core_scope_leakage: dict[str, float] = defaultdict(float)
    core_scope_label_counts: dict[str, Counter[str]] = defaultdict(Counter)
    semantic_policy_count = 0
    quasi_generalization_count = 0
    other_scope_count = 0
    total = {"fully": 0, "partial": 0, "missed": 0}
    total_leakage = 0.0
    gold_samples = 0
    n_pred = 0
    ner_pred = 0
    processed_samples = 0
    decoy_total = decoy_hit = 0
    ner_status = "disabled" if ner is None else "ok"
    ner_failure_stage: str | None = None
    residual_failures: list[dict[str, Any]] = []
    residual_summary: dict[str, Any] = {
        "missed": 0,
        "partial": 0,
        "by_label": Counter(),
        "by_group": Counter(),
        "by_scenario": Counter(),
    }
    raw_ner_predictions: list[dict[str, Any]] = []
    fused_ner_kept: list[dict[str, Any]] = []
    ner_dropped_by_fusion: list[dict[str, Any]] = []
    ner_lift_by_group: Counter[str] = Counter()
    ner_lift_by_label: Counter[str] = Counter()
    ner_partial_or_missed_counts: dict[str, Any] = {
        "missed": 0,
        "partial": 0,
        "by_label": Counter(),
        "by_group": Counter(),
        "by_scenario": Counter(),
    }

    def _detect(text: str) -> tuple[list[DetectionSpan], list[DetectionSpan]]:
        nonlocal ner_status, ner_failure_stage, ner_pred
        spans = [s for d in detectors for s in d.detect(text)]
        raw_ner_spans: list[DetectionSpan] = []
        if ner is not None and ner_status != "unavailable":
            try:
                raw_ner_spans = ner.detect(text)
            except Exception:
                ner_status = "unavailable"
                ner_failure_stage = "detect"
                raw_ner_spans = []
            ner_pred += len(raw_ner_spans)
            raw_ner_predictions.extend(span.to_safe_dict() for span in raw_ner_spans)
            spans.extend(raw_ner_spans)
        fused = fuse_detections(spans, text)
        if raw_ner_spans:
            fused_model_spans = [span for span in fused if span.source == "model"]
            fused_ner_kept.extend(span.to_safe_dict() for span in fused_model_spans)
            ner_dropped_by_fusion.extend(
                span.to_safe_dict()
                for span in raw_ner_spans
                if not _same_span_present(span, fused_model_spans)
            )
        return fused, raw_ner_spans

    for sample in samples:
        processed_samples += 1
        text = sample["text"]
        if sample.get("kind") == "decoy":
            spans, _raw_ner_spans = _detect(text)
            for decoy in sample.get("decoys", []):
                decoy_total += 1
                if any(max(s.start, decoy["start"]) < min(s.end, decoy["end"]) for s in spans):
                    decoy_hit += 1
            continue

        gold_samples += 1
        spans, raw_ner_spans = _detect(text)
        n_pred += len(spans)
        sample_scenario = sample["metadata"].get("scenario", "unknown")
        sample_doc_type = sample["metadata"].get("doc_type", "unknown")
        fused_model_spans = [span for span in spans if span.source == "model"]
        for gold in sample["gold_spans"]:
            result, leakage = _coverage_detail(gold["start"], gold["end"], spans)
            category = gold.get("category", "unknown")
            cat[category][result] += 1
            cat_leakage[category] += leakage
            scenario[sample_scenario][result] += 1
            scenario_leakage[sample_scenario] += leakage
            total[result] += 1
            total_leakage += leakage
            scope, group = _core_scope_for_gold(gold)
            core_scope_label_counts[scope][gold["label"]] += 1
            if scope == "core" and group is not None:
                core_scope_counts[group][result] += 1
                core_scope_leakage[group] += leakage
                if result in {"missed", "partial"}:
                    residual_failures.append(
                        _failure_entry(sample, sample_scenario, sample_doc_type, gold, group, result, leakage)
                    )
                    _increment_residual_summary(residual_summary, gold, group, sample_scenario, result)
                    if ner is not None:
                        _increment_ner_residual(
                            ner_partial_or_missed_counts, gold, group, sample_scenario, result
                        )
                elif _overlaps_any(gold["start"], gold["end"], fused_model_spans):
                    ner_lift_by_group[group] += 1
                    ner_lift_by_label[gold["label"]] += 1
            elif scope == "semantic_policy":
                semantic_policy_count += 1
            elif scope == "quasi_generalization":
                quasi_generalization_count += 1
            else:
                other_scope_count += 1

    elapsed_ms = (time.perf_counter() - started) * 1000
    category_names = _ordered_names(cat.keys(), PREFERRED_CATEGORIES)
    categories = {name: _coverage_block(cat[name], cat_leakage[name]) for name in category_names}
    scenarios = {name: _coverage_block(scenario[name], scenario_leakage[name]) for name in sorted(scenario)}
    overall = _coverage_block(total, total_leakage)
    protection_rate = (decoy_total - decoy_hit) / decoy_total if decoy_total else 0.0
    decoy_hit_rate = decoy_hit / decoy_total if decoy_total else 0.0
    ner_active = ner is not None and ner_status == "ok"
    return {
        "use_ner": ner_active,
        "ner_requested": ner is not None,
        "detectors": "rules+dictionary" + ("+ner" if ner is not None else ""),
        "sample_count": gold_samples,
        "gold_total": overall["total"],
        "n_pred": n_pred,
        "categories": categories,
        "overall": overall,
        "coverage_by_category": categories,
        "coverage_by_scenario": scenarios,
        "core_scope": _core_scope_block(
            core_scope_counts,
            core_scope_leakage,
            core_scope_label_counts,
            semantic_policy_count,
            quasi_generalization_count,
            other_scope_count,
        ),
        "residual_audit": _residual_audit_block(residual_failures, residual_summary),
        "review_queue": _review_queue_block(_residual_review_items(residual_failures)),
        "decoy_protection": {
            "total": decoy_total,
            "hit": decoy_hit,
            "rate": protection_rate,
        },
        "over_redaction": {
            "decoy_total": decoy_total,
            "decoy_hits": decoy_hit,
            "decoy_hit_rate": decoy_hit_rate,
        },
        "ner": {
            "requested": ner is not None,
            "active": ner_active,
            "status": ner_status,
            "failure_stage": ner_failure_stage,
            "predicted_count": ner_pred,
            "diagnostics": _ner_diagnostics_block(
                raw_ner_predictions,
                fused_ner_kept,
                ner_dropped_by_fusion,
                ner_lift_by_group,
                ner_lift_by_label,
                ner_partial_or_missed_counts,
            ),
        },
        "latency_summary": {
            "total_ms": elapsed_ms,
            "sample_count": processed_samples,
            "avg_sample_ms": elapsed_ms / processed_samples if processed_samples else 0.0,
        },
    }


def run_benchmark(
    samples_path: str | Path,
    dictionary_path: str | Path,
    use_ner: bool = False,
) -> dict[str, Any]:
    """Convenience wiring: rules + dictionary (+ optional local NER)."""
    detectors = _default_detectors(dictionary_path)
    ner: BaseDetector | None = None
    ner_load_failed = False
    if use_ner:
        try:
            from tuomin_gateway.detectors.ner import get_ner_detector

            ner = get_ner_detector()
        except Exception:
            ner_load_failed = True
    result = measure(load_benchmark(samples_path), detectors, ner)
    _attach_utility_protection(result, samples_path, dictionary_path)
    if use_ner and ner is None and ner_load_failed:
        result["ner_requested"] = True
        result["ner"] = {
            "requested": True,
            "active": False,
            "status": "unavailable",
            "failure_stage": "load",
            "predicted_count": 0,
            "diagnostics": _empty_ner_diagnostics(),
        }
    return result


def run_ner_comparison(
    samples_path: str | Path,
    dictionary_path: str | Path,
    use_ner: bool = False,
) -> dict[str, Any]:
    """Run L1a plus L1b comparison without loading NER unless explicitly asked."""
    samples = load_benchmark(samples_path)
    detectors = _default_detectors(dictionary_path)
    l1a = measure(samples, detectors)
    if use_ner:
        ner: BaseDetector | None = None
        load_failed = False
        try:
            from tuomin_gateway.detectors.ner import get_ner_detector

            ner = get_ner_detector()
        except Exception:
            load_failed = True
        l1b = measure(samples, detectors, ner)
        if ner is None and load_failed:
            l1b["ner_requested"] = True
            l1b["detectors"] = "rules+dictionary+ner"
            l1b["ner"] = {
                "requested": True,
                "active": False,
                "status": "unavailable",
                "failure_stage": "load",
                "predicted_count": 0,
            }
    else:
        l1b = copy.deepcopy(l1a)
        l1b["detectors"] = "rules+dictionary+ner"
        l1b["ner_requested"] = False
        l1b["ner"] = {
            "requested": False,
            "active": False,
            "status": "not_requested",
            "failure_stage": None,
            "predicted_count": 0,
            "diagnostics": _empty_ner_diagnostics(),
        }
    _attach_utility_protection(l1a, samples_path, dictionary_path)
    _attach_utility_protection(l1b, samples_path, dictionary_path)
    return {
        "status": "ok",
        "source": "synthetic",
        "benchmark_comparison": {"L1a": l1a, "L1b": l1b},
        "risk_reduction": compare_risk_reduction_report(samples, l1a, l1b),
    }


def risk_reduction_report(samples: list[dict[str, Any]], benchmark_result: dict[str, Any]) -> dict[str, Any]:
    """Return an L0-vs-L1 relative risk reduction report."""
    gold_samples = [sample for sample in samples if sample.get("kind") == "gold"]
    source_values = {sample.get("metadata", {}).get("source") for sample in samples}
    source = "synthetic" if source_values == {"synthetic"} else "mixed_or_invalid"
    gold_total = benchmark_result["gold_total"]
    l0_leakage = float(gold_total)
    l1_leakage = float(benchmark_result["overall"].get("leakage", 0.0))
    l1_leakage_rate = l1_leakage / l0_leakage if l0_leakage else 0.0
    l1_reduction = (l0_leakage - l1_leakage) / l0_leakage if l0_leakage else 0.0

    return {
        "status": "ok",
        "source": source,
        "method": "relative_to_l0_raw_text_gold_span_leakage",
        "sample_count": benchmark_result["sample_count"],
        "gold_total": gold_total,
        "detectors": benchmark_result["detectors"],
        "dataset": _dataset_summary(gold_samples),
        "levels": {
            "L0": {
                "status": "baseline",
                "description": "raw text; every gold span is leaked",
                "leakage": l0_leakage,
                "leakage_rate": 1.0 if gold_total else 0.0,
                "relative_risk_reduction": 0.0,
            },
            "L1": {
                "status": "implemented",
                "description": "current rules+dictionary detector coverage; optional NER only when explicitly enabled",
                "leakage": l1_leakage,
                "leakage_rate": l1_leakage_rate,
                "relative_risk_reduction": l1_reduction,
                "ci": benchmark_result["overall"]["ci"],
            },
            "L2": {
                "status": "placeholder consistency only",
                "description": "mapping/refill consistency exists, but no separate risk-reduction tier is claimed here",
                "leakage": None,
                "leakage_rate": None,
                "relative_risk_reduction": None,
            },
            "L3": {
                "status": "generalization_limited",
                "description": "quasi-identifier generalization is not fully wired into this benchmark tier",
                "leakage": None,
                "leakage_rate": None,
                "relative_risk_reduction": None,
            },
        },
        "coverage_by_category": benchmark_result["coverage_by_category"],
        "coverage_by_scenario": benchmark_result["coverage_by_scenario"],
        "decoy_protection": benchmark_result["decoy_protection"],
        "over_redaction": benchmark_result.get("over_redaction", _over_redaction_from_decoy(benchmark_result)),
        "latency_summary": benchmark_result.get("latency_summary"),
        "limitations": _limitations(),
    }


def compare_risk_reduction_report(
    samples: list[dict[str, Any]],
    l1a_result: dict[str, Any],
    l1b_result: dict[str, Any],
) -> dict[str, Any]:
    """Return L0/L1a/L1b comparison for rules+dictionary vs local NER fallback."""
    gold_samples = [sample for sample in samples if sample.get("kind") == "gold"]
    source_values = {sample.get("metadata", {}).get("source") for sample in samples}
    source = "synthetic" if source_values == {"synthetic"} else "mixed_or_invalid"
    gold_total = int(l1a_result["gold_total"])
    l0_leakage = float(gold_total)
    l1a_level = _risk_level(
        "implemented",
        "rules+dictionary synthetic detector coverage",
        l1a_result,
        l0_leakage,
    )
    l1b_level = _risk_level(
        "implemented" if l1b_result.get("ner", {}).get("status") == "ok" else l1b_result.get("ner", {}).get("status", "implemented"),
        "rules+dictionary+local NER fallback coverage; local NER runs only when explicitly requested",
        l1b_result,
        l0_leakage,
    )
    unknown_a = _coverage_or_empty(l1a_result, "unknown")
    unknown_b = _coverage_or_empty(l1b_result, "unknown")
    decoy_a = l1a_result.get("decoy_protection", {})
    decoy_b = l1b_result.get("decoy_protection", {})
    reduction_delta = l1b_level["relative_risk_reduction"] - l1a_level["relative_risk_reduction"]
    leakage_delta = l1b_level["leakage"] - l1a_level["leakage"]
    leakage_rate_delta = l1b_level["leakage_rate"] - l1a_level["leakage_rate"]
    new_decoy_hits = int(decoy_b.get("hit", 0)) - int(decoy_a.get("hit", 0))

    return {
        "status": "ok",
        "source": source,
        "method": "relative_to_l0_raw_text_gold_span_leakage",
        "sample_count": l1a_result["sample_count"],
        "gold_total": gold_total,
        "dataset": _dataset_summary(gold_samples),
        "levels": {
            "L0": {
                "status": "baseline",
                "description": "raw text; every gold span is leaked",
                "leakage": l0_leakage,
                "leakage_rate": 1.0 if gold_total else 0.0,
                "relative_risk_reduction": 0.0,
            },
            "L1a": l1a_level,
            "L1b": l1b_level,
        },
        "deltas": {
            "L1b_vs_L1a": {
                "leakage_delta": leakage_delta,
                "leakage_rate_delta": leakage_rate_delta,
                "relative_risk_reduction_delta": reduction_delta,
                "prediction_count_delta": l1b_result.get("n_pred", 0) - l1a_result.get("n_pred", 0),
                "decoy_hit_delta": new_decoy_hits,
            }
        },
        "coverage_by_category": {
            "L1a": l1a_result["coverage_by_category"],
            "L1b": l1b_result["coverage_by_category"],
        },
        "coverage_by_scenario": {
            "L1a": l1a_result["coverage_by_scenario"],
            "L1b": l1b_result["coverage_by_scenario"],
        },
        "core_scope": compare_core_scope_report(l1a_result, l1b_result),
        "decoy_protection": {
            "L1a": l1a_result["decoy_protection"],
            "L1b": l1b_result["decoy_protection"],
        },
        "over_redaction": {
            "L1a": l1a_result.get("over_redaction", _over_redaction_from_decoy(l1a_result)),
            "L1b": l1b_result.get("over_redaction", _over_redaction_from_decoy(l1b_result)),
        },
        "ner_effect": {
            "status": l1b_result.get("ner", {}).get("status", "unknown"),
            "unknown_recall_lift": unknown_b["coverage_rate"] - unknown_a["coverage_rate"],
            "unknown_leakage_reduction": unknown_a["leakage"] - unknown_b["leakage"],
            "added_prediction_count": l1b_result.get("ner", {}).get(
                "predicted_count", l1b_result.get("n_pred", 0) - l1a_result.get("n_pred", 0)
            ),
            "new_decoy_hits": new_decoy_hits,
        },
        "latency_summary": {
            "L1a": l1a_result.get("latency_summary"),
            "L1b": l1b_result.get("latency_summary"),
        },
        "limitations": _limitations(),
    }


def compare_core_scope_report(l1a_result: dict[str, Any], l1b_result: dict[str, Any]) -> dict[str, Any]:
    """Return L0/L1a/L1b risk reduction over the core redaction denominator only."""
    l1a_scope = l1a_result["core_scope"]
    l1b_scope = l1b_result["core_scope"]
    gold_total = int(l1a_scope["gold_total"])
    l0_leakage = float(gold_total)
    l1a_level = _core_scope_level("implemented", l1a_result, l0_leakage)
    l1b_status = l1b_result.get("ner", {}).get("status")
    l1b_level = _core_scope_level(
        "implemented" if l1b_status == "ok" else l1b_status or "implemented",
        l1b_result,
        l0_leakage,
    )
    leakage_delta = l1b_level["leakage"] - l1a_level["leakage"]
    leakage_rate_delta = l1b_level["leakage_rate"] - l1a_level["leakage_rate"]
    reduction_delta = l1b_level["relative_risk_reduction"] - l1a_level["relative_risk_reduction"]

    return {
        "status": "ok",
        "source": "synthetic",
        "method": "core_identity_project_locator_scope_excluding_semantic_policy_and_quasi_generalization",
        "gold_total": gold_total,
        "semantic_policy_count": int(l1a_scope["semantic_policy_count"]),
        "quasi_generalization_count": int(l1a_scope["quasi_generalization_count"]),
        "levels": {
            "L0": {
                "status": "baseline",
                "description": "raw text; every core-scope gold span is leaked",
                "leakage": l0_leakage,
                "leakage_rate": 1.0 if gold_total else 0.0,
                "relative_risk_reduction": 0.0,
            },
            "L1a": l1a_level,
            "L1b": l1b_level,
        },
        "deltas": {
            "L1b_vs_L1a": {
                "leakage_delta": leakage_delta,
                "leakage_rate_delta": leakage_rate_delta,
                "relative_risk_reduction_delta": reduction_delta,
                "prediction_count_delta": l1b_result.get("n_pred", 0) - l1a_result.get("n_pred", 0),
                "decoy_hit_delta": int(l1b_result.get("decoy_protection", {}).get("hit", 0))
                - int(l1a_result.get("decoy_protection", {}).get("hit", 0)),
            }
        },
        "groups": {
            "L1a": l1a_scope["groups"],
            "L1b": l1b_scope["groups"],
        },
        "semantic_policy_labels": l1a_scope["semantic_policy_labels"],
        "quasi_generalization_labels": l1a_scope["quasi_generalization_labels"],
        "decoy_protection": {
            "L1a": l1a_result["decoy_protection"],
            "L1b": l1b_result["decoy_protection"],
        },
        "over_redaction": {
            "L1a": l1a_result.get("over_redaction", _over_redaction_from_decoy(l1a_result)),
            "L1b": l1b_result.get("over_redaction", _over_redaction_from_decoy(l1b_result)),
        },
        "limitations": _limitations()
        + [
            "semantic policy spans are counted only as policy diagnostics, not core leakage",
            "quasi-identifiers are counted as future generalization candidates, not core leakage",
        ],
    }


def _same_span_present(span: DetectionSpan, spans: list[DetectionSpan]) -> bool:
    return any(
        candidate.start == span.start
        and candidate.end == span.end
        and candidate.label == span.label
        and candidate.text_hash == span.text_hash
        and candidate.source == span.source
        for candidate in spans
    )


def _overlaps_any(start: int, end: int, spans: list[DetectionSpan]) -> bool:
    return any(max(start, span.start) < min(end, span.end) for span in spans)


def _failure_entry(
    sample: dict[str, Any],
    scenario: str,
    doc_type: str,
    gold: dict[str, Any],
    group: str,
    status: str,
    leakage: float,
) -> dict[str, Any]:
    return {
        "sample_id": sample["sample_id"],
        "scenario": scenario,
        "doc_type": doc_type,
        "label": gold["label"],
        "group": group,
        "category": gold.get("category", "unknown"),
        "status": status,
        "leakage": leakage,
        "gold_hash": hash_text(gold["text"]),
    }


def _increment_residual_summary(
    summary: dict[str, Any], gold: dict[str, Any], group: str, scenario: str, status: str
) -> None:
    summary[status] += 1
    summary["by_label"][gold["label"]] += 1
    summary["by_group"][group] += 1
    summary["by_scenario"][scenario] += 1


def _increment_ner_residual(
    summary: dict[str, Any], gold: dict[str, Any], group: str, scenario: str, status: str
) -> None:
    summary[status] += 1
    summary["by_label"][gold["label"]] += 1
    summary["by_group"][group] += 1
    summary["by_scenario"][scenario] += 1


def _residual_audit_block(failures: list[dict[str, Any]], summary: dict[str, Any]) -> dict[str, Any]:
    return {
        "safe": True,
        "scope": "core_only",
        "raw_text_included": False,
        "core_failures": failures,
        "summary": {
            "missed": int(summary["missed"]),
            "partial": int(summary["partial"]),
            "by_label": _safe_counter(summary["by_label"]),
            "by_group": _safe_counter(summary["by_group"]),
            "by_scenario": _safe_counter(summary["by_scenario"]),
        },
    }


def _review_item(
    *,
    sample_id: str,
    label: str,
    group: str,
    policy_action: str,
    reason: str,
    risk_level: str,
    text_hash: str,
) -> dict[str, Any]:
    return {
        "task_id": sample_id,
        "sample_id": sample_id,
        "label": label,
        "group": group,
        "policy_action": policy_action,
        "reason": reason,
        "risk_level": risk_level,
        "text_hash": text_hash,
    }


def _residual_review_items(failures: list[dict[str, Any]]) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for failure in failures:
        if failure["label"] == "PERSON" or failure["status"] == "partial":
            items.append(
                _review_item(
                    sample_id=failure["sample_id"],
                    label=failure["label"],
                    group="core_residual",
                    policy_action="review",
                    reason=f"core {failure['status']} residual requires local review",
                    risk_level="high",
                    text_hash=failure["gold_hash"],
                )
            )
    return items


def _review_queue_block(items: list[dict[str, Any]]) -> dict[str, Any]:
    by_label = Counter(item["label"] for item in items)
    by_group = Counter(item["group"] for item in items)
    by_action = Counter(item["policy_action"] for item in items)
    return {
        "safe": True,
        "raw_text_included": False,
        "items": items,
        "summary": {
            "total": len(items),
            "by_label": _safe_counter(by_label),
            "by_group": _safe_counter(by_group),
            "by_action": _safe_counter(by_action),
        },
    }


def _safe_counter(counter: Counter[str]) -> dict[str, int]:
    return {key: int(value) for key, value in sorted(counter.items())}


def _ner_diagnostics_block(
    raw_ner_predictions: list[dict[str, Any]],
    fused_ner_kept: list[dict[str, Any]],
    ner_dropped_by_fusion: list[dict[str, Any]],
    ner_lift_by_group: Counter[str],
    ner_lift_by_label: Counter[str],
    ner_partial_or_missed_counts: dict[str, Any],
) -> dict[str, Any]:
    return {
        "safe": True,
        "raw_text_included": False,
        "diagnostic_method": "safe_span_hash_and_overlap_approximation",
        "raw_ner_predictions": raw_ner_predictions,
        "raw_ner_prediction_count": len(raw_ner_predictions),
        "raw_ner_preserved_exact_count": len(raw_ner_predictions) - len(ner_dropped_by_fusion),
        "raw_ner_dropped_or_clipped_count": len(ner_dropped_by_fusion),
        "fused_model_span_count": len(fused_ner_kept),
        "fused_ner_kept": fused_ner_kept,
        "fused_ner_kept_count": len(fused_ner_kept),
        "ner_dropped_by_fusion": ner_dropped_by_fusion,
        "ner_dropped_by_fusion_count": len(ner_dropped_by_fusion),
        "ner_lift_by_group": _safe_counter(ner_lift_by_group),
        "ner_lift_by_label": _safe_counter(ner_lift_by_label),
        "ner_partial_or_missed_counts": {
            "missed": int(ner_partial_or_missed_counts["missed"]),
            "partial": int(ner_partial_or_missed_counts["partial"]),
            "by_label": _safe_counter(ner_partial_or_missed_counts["by_label"]),
            "by_group": _safe_counter(ner_partial_or_missed_counts["by_group"]),
            "by_scenario": _safe_counter(ner_partial_or_missed_counts["by_scenario"]),
        },
        "limitations": [
            "raw NER and fused NER are compared by safe span geometry/hash only",
            "lift is approximate because benchmark coverage is span-overlap based, not detector-causality based",
        ],
    }


def _empty_ner_diagnostics() -> dict[str, Any]:
    return _ner_diagnostics_block([], [], [], Counter(), Counter(), {
        "missed": 0,
        "partial": 0,
        "by_label": Counter(),
        "by_group": Counter(),
        "by_scenario": Counter(),
    })

def _default_detectors(dictionary_path: str | Path) -> list[BaseDetector]:
    from tuomin_gateway.detectors.dictionary import DictionaryDetector
    from tuomin_gateway.detectors.rules import RuleDetector

    return [RuleDetector(), DictionaryDetector.from_json(dictionary_path)]


def _risk_level(status: str, description: str, result: dict[str, Any], l0_leakage: float) -> dict[str, Any]:
    leakage = float(result["overall"].get("leakage", 0.0))
    leakage_rate = leakage / l0_leakage if l0_leakage else 0.0
    return {
        "status": status,
        "description": description,
        "detectors": result.get("detectors"),
        "leakage": leakage,
        "leakage_rate": leakage_rate,
        "relative_risk_reduction": (l0_leakage - leakage) / l0_leakage if l0_leakage else 0.0,
        "overall": result["overall"],
        "coverage_by_category": result["coverage_by_category"],
        "coverage_by_scenario": result["coverage_by_scenario"],
        "decoy_protection": result["decoy_protection"],
        "over_redaction": result.get("over_redaction", _over_redaction_from_decoy(result)),
        "ner": result.get("ner"),
        "latency_summary": result.get("latency_summary"),
    }


def _coverage_or_empty(result: dict[str, Any], category: str) -> dict[str, Any]:
    return result.get("coverage_by_category", {}).get(category, EMPTY_COVERAGE)


def _over_redaction_from_decoy(result: dict[str, Any]) -> dict[str, Any]:
    decoy = result.get("decoy_protection", {})
    total = int(decoy.get("total", 0))
    hits = int(decoy.get("hit", 0))
    return {"decoy_total": total, "decoy_hits": hits, "decoy_hit_rate": hits / total if total else 0.0}


def _limitations() -> list[str]:
    return [
        "synthetic fixture only; not evidence of real-material performance",
        "not a complete redaction or zero-leakage proof",
        "local NER is optional and runs only after an explicit request; unavailable runtime degrades safely",
    ]


def _coverage_detail(g0: int, g1: int, spans: list[DetectionSpan]) -> tuple[str, float]:
    intervals = sorted(
        (max(s.start, g0), min(s.end, g1))
        for s in spans
        if max(s.start, g0) < min(s.end, g1)
    )
    length = max(0, g1 - g0)
    if not intervals or length == 0:
        return ("missed", 1.0 if length else 0.0)

    merged: list[tuple[int, int]] = []
    for start, end in intervals:
        if not merged or start > merged[-1][1]:
            merged.append((start, end))
        else:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
    covered = sum(end - start for start, end in merged)
    leakage = max(0.0, (length - covered) / length)
    return ("fully" if leakage == 0 else "partial", leakage)


def _wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """Wilson 95% CI (better than normal approximation near p=0/1)."""
    if n == 0:
        return (0.0, 0.0)
    p = k / n
    d = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / d
    half = (z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))) / d
    return (max(0.0, centre - half), min(1.0, centre + half))


def _coverage_block(counts: dict[str, int], leakage: float = 0.0) -> dict[str, Any]:
    fully = counts["fully"]
    partial = counts["partial"]
    missed = counts["missed"]
    total = fully + partial + missed
    rate = fully / total if total else 0.0
    leakage_rate = leakage / total if total else 0.0
    lo, hi = _wilson(fully, total)
    return {
        "total": total,
        "fully": fully,
        "partial": partial,
        "missed": missed,
        "rate": rate,
        "coverage_rate": rate,
        "leakage": leakage,
        "leakage_rate": leakage_rate,
        "risk_reduction": 1 - leakage_rate if total else 0.0,
        "ci": [lo, hi],
    }


def _core_scope_for_gold(gold: dict[str, Any]) -> tuple[str, str | None]:
    label = gold["label"]
    category = gold.get("category")
    if category == "semantic_sensitive" or label in SEMANTIC_POLICY_LABELS:
        return ("semantic_policy", None)
    group = CORE_SCOPE_GROUP_BY_LABEL.get(label)
    if group is not None and not (label in {"ADDRESS", "LOCATION"} and category == "quasi_identifier"):
        return ("core", group)
    if category == "quasi_identifier" or label in QUASI_GENERALIZATION_LABELS:
        return ("quasi_generalization", None)
    if group is not None:
        return ("core", group)
    return ("other", None)


def _core_scope_block(
    group_counts: dict[str, dict[str, int]],
    group_leakage: dict[str, float],
    label_counts: dict[str, Counter[str]],
    semantic_policy_count: int,
    quasi_generalization_count: int,
    other_scope_count: int,
) -> dict[str, Any]:
    total_counts = {"fully": 0, "partial": 0, "missed": 0}
    total_leakage = 0.0
    groups: dict[str, dict[str, Any]] = {}
    for group in PREFERRED_CORE_GROUPS:
        counts = group_counts[group]
        groups[group] = _coverage_block(counts, group_leakage[group])
        for key in total_counts:
            total_counts[key] += counts[key]
        total_leakage += group_leakage[group]
    overall = _coverage_block(total_counts, total_leakage)
    return {
        "scope": "personal_enterprise_project_locator",
        "gold_total": overall["total"],
        "overall": overall,
        "groups": groups,
        "semantic_policy_count": semantic_policy_count,
        "quasi_generalization_count": quasi_generalization_count,
        "other_count": other_scope_count,
        "core_labels": dict(sorted(label_counts["core"].items())),
        "semantic_policy_labels": dict(sorted(label_counts["semantic_policy"].items())),
        "quasi_generalization_labels": dict(sorted(label_counts["quasi_generalization"].items())),
    }


def _core_scope_level(status: str, result: dict[str, Any], l0_leakage: float) -> dict[str, Any]:
    scope = result["core_scope"]
    leakage = float(scope["overall"].get("leakage", 0.0))
    leakage_rate = leakage / l0_leakage if l0_leakage else 0.0
    return {
        "status": status,
        "description": result.get("detectors"),
        "detectors": result.get("detectors"),
        "leakage": leakage,
        "leakage_rate": leakage_rate,
        "relative_risk_reduction": (l0_leakage - leakage) / l0_leakage if l0_leakage else 0.0,
        "overall": scope["overall"],
        "groups": scope["groups"],
        "ner": result.get("ner"),
        "latency_summary": result.get("latency_summary"),
    }


def _dataset_summary(gold_samples: list[dict[str, Any]]) -> dict[str, Any]:
    scenario_counts = Counter(sample["metadata"].get("scenario", "unknown") for sample in gold_samples)
    category_counts = Counter(
        span.get("category", "unknown") for sample in gold_samples for span in sample["gold_spans"]
    )
    doc_type_counts = Counter(sample["metadata"].get("doc_type", "unknown") for sample in gold_samples)
    return {
        "source": "synthetic",
        "scenario_distribution": dict(sorted(scenario_counts.items())),
        "category_distribution": dict(sorted(category_counts.items())),
        "doc_type_distribution": dict(sorted(doc_type_counts.items())),
    }


def _ordered_names(names: Any, preferred: tuple[str, ...]) -> list[str]:
    seen = set(names)
    return [name for name in preferred if name in seen] + sorted(seen - set(preferred))


def _attach_utility_protection(
    result: dict[str, Any], samples_path: str | Path, dictionary_path: str | Path
) -> None:
    utility_path = Path(samples_path).with_name("utility_decoy_benchmark.jsonl")
    if not utility_path.exists():
        return
    result["utility_protection"] = run_utility_benchmark(
        utility_path,
        dictionary_path,
        core_leakage_rate=float(result.get("core_scope", {}).get("overall", {}).get("leakage_rate", 0.0)),
    )


def _utility_policy_action(strategy: str, span: dict[str, Any]) -> str:
    label = span["label"]
    category = span.get("category")
    if category == "decoy":
        return "pass"
    if strategy == "standard":
        if category in {"semantic_sensitive", "quasi_identifier", "legal_analysis"}:
            return "pass"
        return "redact" if label in CORE_SCOPE_GROUP_BY_LABEL else "pass"
    if strategy == "review":
        if category == "semantic_sensitive":
            return "warn"
        if category in {"quasi_identifier", "legal_analysis"}:
            return "pass"
        return "redact" if label in CORE_SCOPE_GROUP_BY_LABEL else "pass"
    if strategy == "strict":
        if category == "semantic_sensitive":
            return "warn"
        if category == "quasi_identifier":
            if label in {"AMOUNT", "DATE", "LOCATION", "LOCATION_CONTEXT"}:
                return "generalize"
            return "warn"
        if category == "legal_analysis":
            return "pass"
        return "redact" if label in CORE_SCOPE_GROUP_BY_LABEL else "pass"
    if strategy in {"contract_review", "litigation"}:
        from tuomin_gateway.profiles import resolve_named_profile

        profile = resolve_named_profile(strategy)
        decision_span = DetectionSpan(
            start=span["start"],
            end=span["end"],
            label=label,
            confidence=float(span.get("confidence", 0.9)),
            source="utility-gold",
            detector_version="utility-fixture",
            text_hash=hash_text(span["text"]),
            risk_level=span.get("risk_level", "high"),
        )
        if category in {"semantic_sensitive", "quasi_identifier", "legal_analysis"}:
            decision = profile.decide(decision_span)
            if category in {"quasi_identifier", "legal_analysis"} and decision == "redact":
                return "pass"
            return decision
        return profile.decide(decision_span)
    return "pass"


def _validate_utility_sample(sample: dict[str, Any], line_number: int) -> None:
    required = {"sample_id", "kind", "text", "metadata", "utility_spans"}
    if not required.issubset(sample):
        raise ValueError(f"utility benchmark line {line_number} is missing required fields")
    if sample["kind"] != "utility":
        raise ValueError(f"utility benchmark line {line_number} must have kind=utility")
    metadata = sample["metadata"]
    if metadata.get("source") != "synthetic":
        raise ValueError(f"utility benchmark line {line_number} must be synthetic")
    if not metadata.get("scenario"):
        raise ValueError(f"utility benchmark line {line_number} requires metadata.scenario")
    if not metadata.get("doc_type"):
        raise ValueError(f"utility benchmark line {line_number} requires metadata.doc_type")
    text = sample["text"]
    for span in sample["utility_spans"]:
        if not {"label", "text", "category", "reason"}.issubset(span):
            raise ValueError(f"utility benchmark line {line_number} has an invalid utility span")
        start = span.get("start")
        end = span.get("end")
        if start is None or end is None:
            start = text.find(span["text"])
            if start < 0:
                raise ValueError(f"utility benchmark line {line_number} has an unplaceable utility span")
            end = start + len(span["text"])
            span["start"] = start
            span["end"] = end
        if text[start:end] != span["text"]:
            raise ValueError(f"utility benchmark line {line_number} has a utility span boundary mismatch")


def _validate_sample(sample: dict[str, Any], line_number: int) -> None:
    required = {"sample_id", "kind", "text", "metadata"}
    if not required.issubset(sample):
        raise ValueError(f"benchmark line {line_number} is missing required fields")
    metadata = sample["metadata"]
    if metadata.get("source") != "synthetic":
        raise ValueError(f"benchmark line {line_number} must be synthetic")
    if not metadata.get("scenario"):
        raise ValueError(f"benchmark line {line_number} requires metadata.scenario")
    if not metadata.get("doc_type"):
        raise ValueError(f"benchmark line {line_number} requires metadata.doc_type")
    text = sample["text"]
    for gold in sample.get("gold_spans", []):
        if not {"start", "end", "label", "text", "category"}.issubset(gold):
            raise ValueError(f"benchmark line {line_number} has an invalid gold span")
        if text[gold["start"] : gold["end"]] != gold["text"]:
            raise ValueError(f"benchmark line {line_number} has a gold span boundary mismatch")
    for decoy in sample.get("decoys", []):
        if not {"start", "end", "text"}.issubset(decoy):
            raise ValueError(f"benchmark line {line_number} has an invalid decoy span")
        if text[decoy["start"] : decoy["end"]] != decoy["text"]:
            raise ValueError(f"benchmark line {line_number} has a decoy boundary mismatch")
