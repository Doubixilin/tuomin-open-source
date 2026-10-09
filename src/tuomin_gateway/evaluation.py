from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path
from typing import Any

from tuomin_gateway.detectors.dictionary import DictionaryDetector
from tuomin_gateway.detectors.rules import RuleDetector
from tuomin_gateway.fusion import fuse_detections
from tuomin_gateway.redactor import redact_text
from tuomin_gateway.refill import refill_text
from tuomin_gateway.schemas import DetectionSpan


def load_synthetic_samples(path: str | Path) -> list[dict[str, Any]]:
    samples: list[dict[str, Any]] = []
    for line_number, line in enumerate(Path(path).read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        sample = json.loads(line)
        _validate_sample(sample, line_number)
        samples.append(sample)
    return samples


def evaluate_samples(
    samples_path: str | Path,
    dictionary_path: str | Path,
    use_ner: bool = False,
    extra_detectors: list[Any] | None = None,
) -> dict[str, Any]:
    samples = load_synthetic_samples(samples_path)
    dictionary = DictionaryDetector.from_json(dictionary_path)
    rule_detector = RuleDetector()
    ner = _maybe_ner(use_ner)
    extra_detectors = list(extra_detectors or [])

    label_counts: dict[str, dict[str, int]] = defaultdict(lambda: {"tp": 0, "fp": 0, "fn": 0})
    totals = {"tp": 0, "fp": 0, "fn": 0, "exact_matches": 0, "boundary_or_label_mismatch": 0}
    mismatches: list[dict[str, Any]] = []
    refill_ok = 0
    refill_blocked = 0
    placeholder_count = 0

    for sample in samples:
        text = sample["text"]
        gold_spans = sample["gold_spans"]
        spans = rule_detector.detect(text) + dictionary.detect(text)
        for detector in extra_detectors:
            spans = spans + detector.detect(text)
        if ner is not None:
            try:
                spans = spans + ner.detect(text)
            except Exception:
                ner = None  # NER unavailable at runtime -> degrade for the rest of the run
        predicted = fuse_detections(spans, text)
        predicted_records = [_span_record(span, text) for span in predicted]

        # Greedy 1:1 span matching (not set intersection): each gold span is
        # consumed by at most one exact-key prediction, so tp/fp/fn are true
        # span-level counts and stay disjoint (predicted = tp+fp, gold = tp+fn).
        matched_gold = [False] * len(gold_spans)
        tp_spans: list[dict[str, Any]] = []
        fp_spans: list[dict[str, Any]] = []
        for pred in predicted_records:
            key = _match_key(pred)
            idx = next(
                (i for i, gold in enumerate(gold_spans) if not matched_gold[i] and _match_key(gold) == key),
                None,
            )
            if idx is None:
                fp_spans.append(pred)
            else:
                matched_gold[idx] = True
                tp_spans.append(pred)
        fn_spans = [gold for i, gold in enumerate(gold_spans) if not matched_gold[i]]

        for span in tp_spans:
            label_counts[span["label"]]["tp"] += 1
            totals["tp"] += 1
            totals["exact_matches"] += 1
        for span in fp_spans:
            label_counts[span["label"]]["fp"] += 1
            totals["fp"] += 1
        for span in fn_spans:
            label_counts[span["label"]]["fn"] += 1
            totals["fn"] += 1

        # Diagnostic ONLY — a subset view of (fp, fn): near-misses that overlap a
        # gold span but differ in boundary or label. Deliberately NOT added to
        # tp/fp/fn (those stay disjoint and summable).
        mismatch_pairs = _boundary_or_label_mismatches(fp_spans, fn_spans)
        totals["boundary_or_label_mismatch"] += len(mismatch_pairs)
        for predicted_span, gold_span in mismatch_pairs:
            mismatches.append(
                {
                    "sample_id": sample["sample_id"],
                    "type": "boundary_or_label_mismatch",
                    "source": "synthetic",
                    "predicted": _safe_span(predicted_span),
                    "gold": _safe_span(gold_span),
                }
            )

        redaction = redact_text(text, predicted, task_id=sample["sample_id"])
        placeholder_count += len(redaction.mapping)
        refill_result = refill_text(redaction.redacted_text, redaction.mapping)
        if refill_result.status == "ok":
            refill_ok += 1
        else:
            refill_blocked += 1

    return {
        "status": "ok",
        "source": "synthetic",
        "use_ner": use_ner,
        "sample_count": len(samples),
        "labels": _format_label_metrics(label_counts),
        "totals": totals,
        "placeholder_consistency": {
            "refill_ok": refill_ok,
            "refill_blocked": refill_blocked,
            "placeholder_count": placeholder_count,
        },
        "mismatches": mismatches,
    }


def _maybe_ner(use_ner: bool):
    """Return the local NER detector if requested AND importable, else None.

    Lazy + best-effort so the default eval keeps zero optional dependencies.
    """
    if not use_ner:
        return None
    try:
        from tuomin_gateway.detectors.ner import get_ner_detector

        return get_ner_detector()
    except Exception:
        return None


def _validate_sample(sample: dict[str, Any], line_number: int) -> None:
    required = {"sample_id", "text", "gold_spans", "metadata"}
    if not required.issubset(sample):
        raise ValueError(f"sample line {line_number} is missing required fields")
    if sample["metadata"].get("source") != "synthetic":
        raise ValueError(f"sample line {line_number} must be synthetic")
    text = sample["text"]
    for gold in sample["gold_spans"]:
        if not {"start", "end", "label", "text"}.issubset(gold):
            raise ValueError(f"sample line {line_number} has an invalid gold span")
        if text[gold["start"] : gold["end"]] != gold["text"]:
            raise ValueError(f"sample line {line_number} has a gold span boundary mismatch")


def _span_record(span: DetectionSpan, text: str) -> dict[str, Any]:
    return {
        "start": span.start,
        "end": span.end,
        "label": span.label,
        "text": text[span.start : span.end],
    }


def _match_key(span: dict[str, Any]) -> tuple[int, int, str]:
    return (span["start"], span["end"], span["label"])


def _boundary_or_label_mismatches(
    predictions: list[dict[str, Any]], gold_spans: list[dict[str, Any]]
) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    pairs: list[tuple[dict[str, Any], dict[str, Any]]] = []
    used_gold: set[int] = set()
    for prediction in predictions:
        for index, gold in enumerate(gold_spans):
            if index in used_gold:
                continue
            if _overlaps(prediction, gold):
                pairs.append((prediction, gold))
                used_gold.add(index)
                break
    return pairs


def _overlaps(left: dict[str, Any], right: dict[str, Any]) -> bool:
    return max(left["start"], right["start"]) < min(left["end"], right["end"])


def _safe_span(span: dict[str, Any]) -> dict[str, Any]:
    return {
        "start": span["start"],
        "end": span["end"],
        "label": span["label"],
        "text": span["text"],
        "source": "synthetic",
    }


def _format_label_metrics(label_counts: dict[str, dict[str, int]]) -> dict[str, dict[str, float | int]]:
    metrics: dict[str, dict[str, float | int]] = {}
    for label, counts in sorted(label_counts.items()):
        tp = counts["tp"]
        fp = counts["fp"]
        fn = counts["fn"]
        precision = tp / (tp + fp) if tp + fp else 0.0
        recall = tp / (tp + fn) if tp + fn else 0.0
        metrics[label] = {
            "tp": tp,
            "fp": fp,
            "fn": fn,
            "precision": precision,
            "recall": recall,
        }
    return metrics
