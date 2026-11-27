#!/usr/bin/env python
"""Author the versioned redaction benchmark fixtures from the canonical dataset.

This is an AUTHORING tool, not part of the runtime. It freezes the synthetic
benchmark into
explicit, boundary-validated fixtures that both ``measure_redaction.py`` and the
test suite consume:

- ``tests/fixtures/redaction_benchmark.jsonl``         gold + decoy samples
- ``tests/fixtures/redaction_benchmark_dictionary.json`` the "known" dictionary

The canonical source data lives in ``benchmark_dataset.py`` next to this script.
Offsets are computed once here with the same first-unclaimed-occurrence logic the
measurement used inline, so the emitted fixture reproduces identical numbers.

Run (only when the dataset changes):
  PYTHONPATH=src python docs/competition/eval/build_benchmark_fixture.py
"""
from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from benchmark_dataset import DECOYS, DICT_ENTRIES, S  # noqa: E402

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
FIXTURE_DIR = os.path.join(ROOT, "tests", "fixtures")
SAMPLES_OUT = os.path.join(FIXTURE_DIR, "redaction_benchmark.jsonl")
DICT_OUT = os.path.join(FIXTURE_DIR, "redaction_benchmark_dictionary.json")


def _spans_for(text: str, values: list[str]) -> list[tuple[int, int]]:
    """First unclaimed occurrence of each value (matches the original logic)."""
    claimed: list[tuple[int, int]] = []
    out: list[tuple[int, int]] = []
    for value in values:
        start = -1
        idx = text.find(value)
        while idx != -1:
            cand = (idx, idx + len(value))
            if not any(max(cand[0], c[0]) < min(cand[1], c[1]) for c in claimed):
                start = idx
                break
            idx = text.find(value, idx + 1)
        if start == -1:
            raise SystemExit(f"benchmark build error: {value!r} not placeable in {text!r}")
        claimed.append((start, start + len(value)))
        out.append((start, start + len(value)))
    return out


def build_samples() -> list[dict]:
    samples: list[dict] = []
    for i, row in enumerate(S, start=1):
        text, golds, _ignored_decoys = row[:3]
        scenario = row[3] if len(row) > 3 else "contract"
        doc_type = row[4] if len(row) > 4 else scenario
        offsets = _spans_for(text, [g[0] for g in golds])
        gold_spans = [
            {"start": s, "end": e, "label": label, "text": value, "category": category}
            for (s, e), (value, label, category) in zip(offsets, golds)
        ]
        samples.append(
            {
                "sample_id": f"gold_{i:06d}",
                "kind": "gold",
                "text": text,
                "gold_spans": gold_spans,
                "metadata": {"source": "synthetic", "scenario": scenario, "doc_type": doc_type},
            }
        )
    for i, (text, decoys) in enumerate(DECOYS, start=1):
        offsets = _spans_for(text, decoys)
        decoy_spans = [
            {"start": s, "end": e, "text": value}
            for (s, e), value in zip(offsets, decoys)
        ]
        samples.append(
            {
                "sample_id": f"decoy_{i:06d}",
                "kind": "decoy",
                "text": text,
                "decoys": decoy_spans,
                "metadata": {"source": "synthetic", "scenario": "decoy", "doc_type": "decoy"},
            }
        )
    return samples


def main() -> None:
    samples = build_samples()
    with open(SAMPLES_OUT, "w", encoding="utf-8") as handle:
        for sample in samples:
            handle.write(json.dumps(sample, ensure_ascii=False) + "\n")
    with open(DICT_OUT, "w", encoding="utf-8") as handle:
        json.dump(DICT_ENTRIES, handle, ensure_ascii=False, indent=2)
        handle.write("\n")

    gold = sum(1 for s in samples if s["kind"] == "gold")
    decoy = sum(1 for s in samples if s["kind"] == "decoy")
    gold_values = sum(len(s["gold_spans"]) for s in samples if s["kind"] == "gold")
    decoy_values = sum(len(s["decoys"]) for s in samples if s["kind"] == "decoy")
    print(f"wrote {SAMPLES_OUT}")
    print(f"  gold samples={gold} values={gold_values} | decoy samples={decoy} values={decoy_values}")
    print(f"wrote {DICT_OUT} ({len(DICT_ENTRIES)} entries)")


if __name__ == "__main__":
    main()
