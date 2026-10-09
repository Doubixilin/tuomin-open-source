"""Long-document NER chunking (Phase 5).

The RoBERTa base caps at 512 positions, so ``CluenerNerDetector`` chunks long
text into overlapping windows before inference. These tests pin the window
math, global offset restoration, overlap dedup, and inference serialization —
with a fake pipeline, no model download.
"""
from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor

from tuomin_gateway.detectors.ner import (
    MAX_WINDOW_CHARS,
    OVERLAP_CHARS,
    CluenerNerDetector,
    _chunk_bounds,
)


def _fake_pipe(calls, entities):
    """A pipeline-like callable emitting HF-style dicts for every registered
    ``(word, entity_group)`` found in each window. Mirrors the batched call
    shape: a list of windows in, a list of per-window entity lists out."""

    def pipe(texts, batch_size=None):
        calls.extend(texts)
        batch = []
        for chunk in texts:
            out = []
            for word, group in entities:
                start = chunk.find(word)
                while start != -1:
                    out.append(
                        {
                            "entity_group": group,
                            "score": 0.91,
                            "start": start,
                            "end": start + len(word),
                            "word": word,
                        }
                    )
                    start = chunk.find(word, start + 1)
            batch.append(out)
        return batch

    return pipe


def _detector(calls, entities=()):
    det = CluenerNerDetector()
    det._pipe = _fake_pipe(calls, entities)
    return det


def _filler(length: int) -> str:
    return "的" * length


# --- window math ------------------------------------------------------------

def test_chunk_bounds_short_text_single_window():
    assert _chunk_bounds("short text") == [(0, 10)]


def test_chunk_bounds_hard_cut_stride_without_punctuation():
    text = _filler(1000)
    bounds = _chunk_bounds(text)
    assert bounds == [(0, 384), (320, 704), (640, 1000)]
    assert all(end - start <= MAX_WINDOW_CHARS for start, end in bounds)


def test_chunk_bounds_prefers_newline_cut_in_back_half():
    text = _filler(379) + "\n" + _filler(600)
    bounds = _chunk_bounds(text)
    assert bounds[0] == (0, 380)  # cut right after the newline


def test_chunk_bounds_prefers_sentence_punctuation_over_hard_cut():
    text = _filler(370) + "。" + _filler(600)
    bounds = _chunk_bounds(text)
    assert bounds[0] == (0, 371)  # cut right after 。


# --- inference with global offsets ------------------------------------------

def test_short_text_single_call_unchanged_behavior():
    calls: list[str] = []
    det = _detector(calls, [("绿洲建设集团", "company")])
    spans = det.detect("甲方为绿洲建设集团，签字。")

    assert calls == ["甲方为绿洲建设集团，签字。"]  # one pass, full text
    assert [text for text in (s.text_hash and "甲方为绿洲建设集团，签字。"[s.start:s.end] for s in spans)] == ["绿洲建设集团"]


def test_tail_entity_in_long_document_detected_with_global_offset():
    calls: list[str] = []
    det = _detector(calls, [("绿洲建设集团", "company")])
    text = _filler(2500) + "乙方是绿洲建设集团。"
    spans = det.detect(text)

    assert len(calls) > 1  # chunked
    hits = [(s.start, s.end) for s in spans if text[s.start:s.end] == "绿洲建设集团"]
    assert hits == [(2503, 2509)]


def test_overlap_duplicate_entity_detected_once():
    calls: list[str] = []
    det = _detector(calls, [("绿洲建设集团", "company")])
    # Entity at 330-336 sits inside the overlap zone (320-384) of window 1 and
    # is re-covered by window 2 under a hard cut — the fake finds it twice.
    text = _filler(330) + "绿洲建设集团" + _filler(500)
    spans = det.detect(text)

    hits = [(s.start, s.end) for s in spans if text[s.start:s.end] == "绿洲建设集团"]
    assert hits == [(330, 336)]


def test_extend_right_completes_name_across_window_cut():
    calls: list[str] = []
    # The fake sees only "绿洲建设" (e.g. the window cut hides the tail); the
    # full text continues with 集团 — extension must recover it globally.
    det = _detector(calls, [("绿洲建设", "company")])
    text = _filler(340) + "绿洲建设集团。" + _filler(200)
    spans = det.detect(text)

    texts = {text[s.start:s.end] for s in spans}
    assert "绿洲建设集团" in texts
    assert all(t != "绿洲建设" for t in texts)


def test_multiline_merged_span_split_on_global_offsets():
    calls: list[str] = []
    merged = "绿洲建设集团\n海天建设集团"
    det = _detector(calls, [(merged, "company")])
    text = _filler(400) + merged + "。" + _filler(100)
    spans = det.detect(text)

    texts = {text[s.start:s.end] for s in spans}
    assert texts == {"绿洲建设集团", "海天建设集团"}


def test_missing_offsets_falls_back_to_find_within_chunk():
    calls: list[str] = []

    def pipe(texts, batch_size=None):
        calls.extend(texts)
        return [
            [{"entity_group": "company", "score": 0.9, "word": "绿洲建设集团"}]
            if "绿洲建设集团" in chunk
            else []
            for chunk in texts
        ]

    det = CluenerNerDetector()
    det._pipe = pipe
    text = _filler(900) + "联系人绿洲建设集团。"
    spans = det.detect(text)

    hits = [(s.start, s.end) for s in spans if text[s.start:s.end] == "绿洲建设集团"]
    assert hits == [(903, 909)]


def test_empty_and_whitespace_text_skip_inference():
    calls: list[str] = []
    det = _detector(calls)
    assert det.detect("") == []
    assert det.detect("   \n  ") == []
    assert calls == []


def test_threshold_filters_low_score_entities():
    calls: list[str] = []

    def pipe(texts, batch_size=None):
        calls.extend(texts)
        return [
            [{"entity_group": "company", "score": 0.2, "start": 0, "end": 6, "word": "绿洲建设集团"}]
            for _ in texts
        ]

    det = CluenerNerDetector(threshold=0.5)
    det._pipe = pipe
    assert det.detect("绿洲建设集团") == []


def test_postprocess_repairs_university_cross_label_boundary():
    calls: list[str] = []
    det = _detector(calls, [("上海大", "company"), ("学", "address")])
    text = "上海大学"

    spans = det.detect(text)

    assert [(span.label, text[span.start:span.end]) for span in spans] == [
        ("ORG", "上海大学")
    ]


def test_postprocess_completes_university_when_suffix_was_not_detected():
    calls: list[str] = []
    det = _detector(calls, [("上海大", "company")])
    text = "上海大学"

    spans = det.detect(text)

    assert [(span.label, text[span.start:span.end]) for span in spans] == [
        ("ORG", "上海大学")
    ]


def test_postprocess_relabels_full_university_as_org():
    calls: list[str] = []
    det = _detector(calls, [("上海大学", "address")])
    text = "上海大学"

    spans = det.detect(text)

    assert [(span.label, text[span.start:span.end]) for span in spans] == [
        ("ORG", "上海大学")
    ]


def test_postprocess_drops_single_character_org_address_noise():
    calls: list[str] = []
    det = _detector(calls, [("送", "company"), ("出", "address")])

    assert det.detect("送出") == []


def test_postprocess_drops_confirmed_generic_model_surfaces():
    calls: list[str] = []
    det = _detector(calls, [("公司", "company"), ("行政主管部门", "organization")])

    assert det.detect("公司和行政主管部门") == []


def test_postprocess_drops_fragments_of_generic_legal_org_references():
    calls: list[str] = []
    det = _detector(
        calls,
        [("县人", "organization"), ("民政府土地管理部门", "organization")],
    )

    assert det.detect("县人民政府土地管理部门") == []


# --- inference serialization -------------------------------------------------

def test_inference_is_serialized_across_threads():
    state = {"active": 0, "max_active": 0}
    calls: list[str] = []

    def pipe(texts, batch_size=None):
        calls.extend(texts)
        state["active"] += 1
        state["max_active"] = max(state["max_active"], state["active"])
        time.sleep(0.01)
        state["active"] -= 1
        return [[] for _ in texts]

    det = CluenerNerDetector()
    det._pipe = pipe
    text = _filler(1000)

    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(det.detect, [text] * 4))

    assert calls  # inference actually ran
    assert state["max_active"] == 1


def test_overlapping_windows_cover_text_with_overlap():
    text = _filler(1000)
    bounds = _chunk_bounds(text)
    for (start_a, end_a), (start_b, _) in zip(bounds, bounds[1:]):
        assert end_a - start_b == OVERLAP_CHARS  # hard-cut stride keeps overlap
