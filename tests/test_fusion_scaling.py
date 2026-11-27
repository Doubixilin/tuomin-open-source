"""Windowed fusion must match the original all-pairs semantics exactly, scale
to large documents, and stop promptly when a cancel is requested."""
from __future__ import annotations

import random
import time
from dataclasses import replace

import pytest

from tuomin_gateway import cancellation
from tuomin_gateway.fusion import _drop_contained_model_alternatives, _selection_key, fuse_detections
from tuomin_gateway.schemas import DetectionSpan, hash_text
from tuomin_gateway.spans import _clipped_metadata, _subtract, overlaps, resolve_overlaps


# --- reference: the original quadratic implementations -----------------------

def reference_drop(ranked):
    cross = {id(s) for s in ranked for l in ranked if s is not l and s.source == l.source == "model"
             and s.label != l.label and l.start <= s.start and l.end >= s.end and l.length() >= s.length() + 2}
    kept = []
    for span in ranked:
        if id(span) in cross:
            continue
        if not any(span.source == e.source == "model" and span.label == e.label and overlaps(span, e)
                   and ((span.start <= e.start and span.end >= e.end) or (e.start <= span.start and e.end >= span.end))
                   for e in kept):
            kept.append(span)
    return kept


def reference_resolve(ranked, text):
    selected, occupied = [], []
    for span in ranked:
        for a, b in _subtract(span.start, span.end, occupied):
            while a < b and text[a].isspace():
                a += 1
            while b > a and text[b - 1].isspace():
                b -= 1
            if b <= a:
                continue
            selected.append(span if (a, b) == (span.start, span.end) else replace(
                span, start=a, end=b, text_hash=hash_text(text[a:b]), metadata=_clipped_metadata(span.metadata, text[a:b])))
            occupied.append((a, b))
    return sorted(selected, key=lambda item: (item.start, item.end, item.label))


def random_spans(rng, text, count):
    spans = []
    for _ in range(count):
        start = rng.randrange(0, len(text) - 1)
        end = min(len(text), start + rng.randint(1, 14))
        spans.append(DetectionSpan(start=start, end=end, label=rng.choice(["ORG", "PERSON", "ADDRESS", "CONTACT"]),
                                   confidence=rng.choice([0.6, 0.8, 0.95, 1.0]), source=rng.choice(["model", "model", "rule", "dictionary"]), detector_version="t",
                                   text_hash=hash_text(text[start:end])))
    return spans


@pytest.mark.parametrize("seed", range(40))
def test_windowed_fusion_equals_reference(seed):
    rng = random.Random(seed)
    text = "".join(rng.choice("示例建设集团有限公司张三 联系电话1380\n") for _ in range(400))
    spans = random_spans(rng, text, rng.randint(5, 160))
    ranked = sorted(spans, key=_selection_key)
    assert _drop_contained_model_alternatives(ranked) == reference_drop(ranked)
    dropped = reference_drop(ranked)
    assert resolve_overlaps(dropped, text) == reference_resolve(dropped, text)


def test_large_document_fusion_is_fast():
    rng = random.Random(7)
    text = "联系人张三，电话13800138000，单位示例建设集团有限公司。" * 4000
    spans = random_spans(rng, text, 40000)
    started = time.monotonic()
    fuse_detections(spans, text)
    assert time.monotonic() - started < 10  # the all-pairs version needed minutes here


def test_fusion_honours_cancellation():
    rng = random.Random(3)
    text = "单位示例建设集团有限公司。" * 2000
    spans = random_spans(rng, text, 5000)
    with cancellation.begin("fusion-cancel") as event:
        event.set()
        with pytest.raises(cancellation.Cancelled):
            fuse_detections(spans, text)
