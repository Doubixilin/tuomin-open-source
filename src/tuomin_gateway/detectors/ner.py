"""CLUENER-based Chinese NER detector for the redaction layer.

Recalls entities the exact-match dictionary misses — counterparties, people,
addresses — that leak under rules+dictionary alone. Uses a char-level Chinese
model so CJK entity boundaries are correct (GLiNER multilingual fails at this).

Heavy + optional: the model (~400MB) is loaded lazily and cached per process,
only when a profile requests NER. Source label is "model" so fusion gives it
the lowest priority — the exact dictionary and rules override it on overlap.
Requires the optional ``[ner]`` extra (transformers + torch).

Long documents are chunked into overlapping windows before inference (the
RoBERTa base caps at 512 positions; feeding more is undefined — a crash or a
silently degraded tail). Span offsets are restored to the original text and
exact duplicates from the overlap zone are dropped; anything left overlapping
is reconciled downstream by fusion, where NER is the lowest-priority source.
"""
from __future__ import annotations

from dataclasses import replace
import re
import threading

from tuomin_gateway.detectors.base import BaseDetector
from tuomin_gateway.schemas import DetectionSpan, hash_text

MODEL_NAME = "uer/roberta-base-finetuned-cluener2020-chinese"
MODEL_REVISION = "cddd8fc233e373855a8c0a7f4b7eb83acb686a2b"


def _model_source(model_name: str, model_revision: str | None) -> tuple[str, dict]:
    """Resolve where the NER model is loaded from.

    Packaged builds pin an explicit local model directory via the
    ``TUOMIN_NER_MODEL_DIR`` env var (no HF cache lookup, no network, no
    revision); development keeps the pinned HF id + revision, always with
    ``local_files_only=True`` — implicit downloads are never allowed.
    """
    import os

    model_dir = os.environ.get("TUOMIN_NER_MODEL_DIR")
    if model_dir:
        return model_dir, {}
    return model_name, ({"revision": model_revision} if model_revision else {})

# Inference window in CHARACTERS. The char-level Chinese model uses ~1 token
# per char; 384 leaves ample headroom under the 512 position cap even with
# [CLS]/[SEP] and ASCII subword expansion.
MAX_WINDOW_CHARS = 384
# Overlap between consecutive windows: at least as long as common org/address
# entity lengths, so an entity straddling a cut appears COMPLETE inside the
# neighboring window and is detected there.
OVERLAP_CHARS = 64
# Sentence-ish boundaries preferred when cutting a window (newline handled
# separately and preferred over these).
_CHUNK_BREAK_CHARS = "。；;！!？?"
# Windows per batched pipeline call. Batching does not change total compute
# (same windows, same tokens) — it only cuts per-call Python/scheduling
# overhead; the cap bounds peak activation memory to a small constant on CPU.
INFER_BATCH_SIZE = 8

# CLUENER type -> our redaction label. Types not listed (book/game/movie/scene/
# position) are dropped as noise / non-identity.
LABEL_MAP = {
    "company": "ORG",
    "organization": "ORG",
    "government": "ORG",
    "name": "PERSON",
    "address": "ADDRESS",
}


# NER often clips an ORG/ADDRESS name just before a structural tail, leaking the
# rest as plaintext (e.g. "<ORG>理股权投资项目"). We conservatively extend a
# detected ORG/ADDRESS span rightward over a contiguous name-like run ONLY when
# that run ends at one of these tails within a short window — never across a
# connector/stopword or punctuation, so we don't over-redact.
# Structural tails are label-specific. ORG tails deliberately EXCLUDE the
# single-char building units (号/栋/室/座/层): for an org name they are usually
# the *next* token (e.g. "A座") and swallowing them over-redacts. ADDRESS keeps
# those units because a house/building number genuinely belongs to the address
# (e.g. "测试大道100号"). The contiguous-run scan still stops at any connector or
# punctuation, so neither set can cross into an unrelated token.
_ORG_SUFFIXES = (
    "股份有限公司", "有限公司", "有限责任公司", "集团", "公司", "研究院", "设计院",
    "工程局", "事务所", "管理处", "管理局", "委员会", "办公室", "中心", "基金",
    "银行", "证券", "保险", "股权投资项目", "投资项目",
)
_ADDRESS_SUFFIXES = (
    "大厦", "大楼", "广场", "大道", "街道", "社区", "村委会", "基地", "号", "栋", "室", "座", "层",
)
_SUFFIXES_BY_LABEL = {"ORG": _ORG_SUFFIXES, "ADDRESS": _ADDRESS_SUFFIXES}
_BOUNDARY_STOP = set("的了是在与和及或为由对把被让向从到等将该其此之，。、；：！？,.;:!?\n\r\t （）()【】[]\"'《》")
_EXTEND_WINDOW = 12

_HARD_ORG_LINE_SUFFIXES = (
    "股份有限公司", "有限公司", "有限责任公司", "集团", "公司", "研究院", "设计院",
    "工程局", "事务所", "管理处", "管理局", "委员会", "办公室", "中心", "基金",
    "银行", "证券", "保险", "政府", "局", "部",
)
_SOFT_ORG_LINE_SUFFIXES = _HARD_ORG_LINE_SUFFIXES + ("行政主管部门",)
_SOFT_ADDRESS_LINE_SUFFIXES = (
    "省", "市", "区", "县", "路", "站", "街", "镇", "乡", "园", "城",
    *_ADDRESS_SUFFIXES,
)

# Exact model surfaces confirmed as generic nouns/public institution classes,
# not identities.  Keep this deliberately small: application dictionaries own
# project-specific decisions; this filter only removes known NER noise.
_GENERIC_MODEL_VALUES = {
    "ORG": {
        "公司",
        "限公司",
        "有限公司",
        "股份公司",
        "份公司",
        "项目公司",
        "管理委员会",
        "展有限公司",
        "开发有限公司",
        "行政机关",
        "人民政府",
        "国务院",
        "自然资源部",
        "国土资源管理部门",
        "人民政府土地管理部门",
        "城市规划、建设、房产管理部门",
        "行政主管部门",
    },
    "ADDRESS": {"地铁", "市", "区", "县"},
}
# Court/arbitration bodies are public institutions, not private identities —
# masking them harms readability without privacy gain (same rationale as the
# 人民政府/国务院 exemptions above). The dictionary protection chain (COURT /
# ARBITRATION entries + profile PASS) remains the primary path; this suffix
# filter is the NER-layer backstop for names no dictionary covers, which the
# 2026-08 benchmark showed being masked as ORG by CLUENER.
_LITIGATION_PUBLIC_ORG_SUFFIXES = ("人民法院", "仲裁委员会", "仲裁院", "仲裁中心")
_GENERIC_ORG_REFERENCE_FORMS = (
    "行政机关",
    "人民政府",
    "县人民政府土地管理部门",
    "市县人民政府国土资源行政主管部门",
    "人民政府国土资源行政主管部门",
    "直辖市人民政府",
    "国务院",
    "自然资源部",
    "国土资源部",
    "国土资源管理部门",
    "城市规划建设房产管理部门",
    "项目公司",
)
_ORG_NEWLINE_CONTINUATIONS = (
    "股份有限公司",
    "有限责任公司",
    "有限公司",
    "工程局",
    "公司",
    "集团",
)

_MODEL_EDGE_CHARS = " \t“”‘’\"'「」『』【】[]"
_CJK_RE = re.compile(r"[\u3400-\u9fff]")
_ORG_PREDICATE_BOUNDARY_RE = re.compile(
    r"(?:有意|意向|计划|拟)?(?:收购|转让|退出|参与)|"
    r"自行(?:决定|管理|运营)|负责(?:实施|管理|运营)?|按照"
)
_REPEATED_SURFACE_EXPANSION = 16


def _split_on_newlines(
    text: str, start: int, end: int, label: str = "ORG"
) -> list[tuple[int, int]]:
    """Split [start, end) into per-line sub-spans, dropping the line breaks and
    trimming surrounding whitespace.

    A char-level NER model with ``aggregation_strategy="simple"`` can merge
    entities sitting on consecutive lines (a list of company names, one per row)
    into a SINGLE span that swallows the ``\\n`` between them. Redacting that one
    span then eats the line breaks (4 rows collapse into 1) and fuses distinct
    entities into one placeholder. Most newlines therefore remain hard entity
    boundaries. A narrow two-line heuristic rejoins confirmed PDF soft-wrap
    shapes such as ``国土资\\n源部`` and ``上海\\n市``; blank lines and lists remain
    split. The no-newline case returns the original span unchanged.
    """
    segments: list[tuple[int, int]] = []
    i = start
    while i < end:
        while i < end and text[i] in "\n\r\t ":  # skip leading whitespace/breaks
            i += 1
        if i >= end:
            break
        j = i
        while j < end and text[j] not in "\n\r":  # run to the next line break
            j += 1
        k = j
        while k > i and text[k - 1] in "\t ":  # trim trailing whitespace
            k -= 1
        if k > i:
            segments.append((i, k))
        i = j
    if _should_join_soft_line(text, segments, label):
        return [(segments[0][0], segments[-1][1])]
    return segments


def _should_join_soft_line(
    text: str, segments: list[tuple[int, int]], label: str
) -> bool:
    if len(segments) != 2:
        return False
    left_start, left_end = segments[0]
    right_start, right_end = segments[1]
    separator = text[left_end:right_start].replace("\r\n", "\n").replace("\r", "\n")
    if separator.count("\n") != 1:
        return False
    left = text[left_start:left_end]
    right = text[right_start:right_end]
    combined = left + right
    if label == "ADDRESS":
        return len(right) <= 2 and combined.endswith(_SOFT_ADDRESS_LINE_SUFFIXES)
    if label != "ORG" or left.endswith(_HARD_ORG_LINE_SUFFIXES):
        return False
    if not combined.endswith(_SOFT_ORG_LINE_SUFFIXES):
        return False
    # A very short continuation is a typical mid-word PDF wrap (国土资/源部).
    # The authority-name marker covers 上海市宝/山区规划和自然资源局 without
    # joining ordinary two-row company lists such as 中国建筑/中建八局.
    return len(right) <= 2 or "规划和自然资源" in combined


def _is_extendable(ch: str) -> bool:
    return ("㐀" <= ch <= "鿿") or ch.isdigit()


def _extend_right(text: str, start: int, end: int, label: str = "ORG") -> int:
    """Return a (possibly larger) end if a structural tail completes the name."""
    suffixes = _SUFFIXES_BY_LABEL.get(label, _ORG_SUFFIXES)
    i = end
    limit = min(len(text), end + _EXTEND_WINDOW)
    best = end
    while i < limit:
        ch = text[i]
        if ch in _BOUNDARY_STOP or not _is_extendable(ch):
            break
        i += 1
        if any(text[start:i].endswith(suf) for suf in suffixes):
            best = i  # commit through the suffix; keep scanning for a longer one
    if best == end:
        if label == "ORG":
            return _extend_across_newline_suffix(text, start, end)
        if label == "ADDRESS":
            return _extend_address_across_newline_suffix(text, start, end)
    return best


def _extend_across_newline_suffix(text: str, start: int, end: int) -> int:
    """Complete ``上海示例甲置业\n有限公司`` without crossing a list row."""
    if text[start:end].endswith(_HARD_ORG_LINE_SUFFIXES) or end >= len(text):
        return end
    cursor = end
    if text.startswith("\r\n", cursor):
        cursor += 2
    elif text[cursor] in "\r\n":
        cursor += 1
    else:
        return end
    while cursor < len(text) and text[cursor] in " \t":
        cursor += 1
    for suffix in _ORG_NEWLINE_CONTINUATIONS:
        if text.startswith(suffix, cursor):
            return cursor + len(suffix)
    return end


def _extend_address_across_newline_suffix(
    text: str, start: int, end: int
) -> int:
    """Complete a short address tail such as ``总部基\n地``."""

    if end >= len(text):
        return end
    cursor = end
    if text.startswith("\r\n", cursor):
        cursor += 2
    elif text[cursor] in "\r\n":
        cursor += 1
    else:
        return end
    while cursor < len(text) and text[cursor] in " \t":
        cursor += 1
    for length in (1, 2):
        candidate_end = cursor + length
        if candidate_end > len(text):
            break
        continuation = text[cursor:candidate_end]
        if not continuation or not all(_is_extendable(ch) for ch in continuation):
            break
        combined = text[start:end] + continuation
        if combined.endswith(_ADDRESS_SUFFIXES):
            return candidate_end
    return end


def _chunk_bounds(text: str) -> list[tuple[int, int]]:
    """Split text into (start, end) inference windows with boundary-aware cuts.

    Each window is at most ``MAX_WINDOW_CHARS``; consecutive windows overlap by
    up to ``OVERLAP_CHARS``. A cut is taken at the last newline (preferred) or
    sentence-final punctuation in the back half of the window; without one the
    window is hard-cut and the overlap still gives straddling entities a full
    second chance in the next window.
    """
    if len(text) <= MAX_WINDOW_CHARS:
        return [(0, len(text))]
    bounds: list[tuple[int, int]] = []
    start = 0
    while start < len(text):
        end = min(len(text), start + MAX_WINDOW_CHARS)
        if end < len(text):
            cut = -1
            newline_pos = text.rfind("\n", start + MAX_WINDOW_CHARS // 2, end)
            if newline_pos != -1:
                cut = newline_pos + 1  # keep the break at the end of this window
            else:
                for pos in range(end - 1, start + MAX_WINDOW_CHARS // 2, -1):
                    if text[pos] in _CHUNK_BREAK_CHARS:
                        cut = pos + 1
                        break
            if cut > start:
                end = cut
        bounds.append((start, end))
        if end >= len(text):
            break
        start = max(start + 1, end - OVERLAP_CHARS)
    return bounds


class NerUnavailable(RuntimeError):
    """Raised when transformers / the NER model cannot be loaded."""


class CluenerNerDetector(BaseDetector):
    name = "model"

    def __init__(
        self,
        threshold: float = 0.5,
        model_name: str = MODEL_NAME,
        model_revision: str | None = MODEL_REVISION,
    ):
        self.threshold = threshold
        self.model_name = model_name
        self.model_revision = model_revision
        revision = f"@{model_revision}" if model_revision else ""
        self.version = f"ner-cluener:{model_name}{revision}"
        self._pipe = None
        # The process-wide singleton serves concurrent FastAPI thread-pool
        # requests; HF pipelines are not thread-safe, so loading AND inference
        # serialize on this lock (backpressure = wait, failure modes unchanged).
        self._lock = threading.Lock()

    def _pipeline(self):
        if self._pipe is None:
            try:
                from transformers import AutoModelForTokenClassification, AutoTokenizer, pipeline
            except Exception as exc:  # pragma: no cover - optional dependency
                raise NerUnavailable(f"transformers 未安装（pip install tuomin-gateway[ner]）：{exc}") from exc
            try:
                source, revision_kwargs = _model_source(self.model_name, self.model_revision)
                tokenizer = AutoTokenizer.from_pretrained(
                    source,
                    local_files_only=True,
                    **revision_kwargs,
                )
                model = AutoModelForTokenClassification.from_pretrained(
                    source,
                    local_files_only=True,
                    **revision_kwargs,
                )
                self._pipe = pipeline(
                    "token-classification",
                    model=model,
                    tokenizer=tokenizer,
                    aggregation_strategy="simple",
                )
            except Exception as exc:
                raise NerUnavailable(f"NER 模型未在本地可用或加载失败：{exc}") from exc
        return self._pipe

    def _infer(self, texts: list[str]) -> list:
        """Run inference windows through the pipeline in bounded batches.

        Serialized on ``self._lock`` (HF pipelines are not thread-safe). One
        batched call per ``INFER_BATCH_SIZE`` windows: same total compute as
        sequential calls, but far less per-call overhead — and the lock is
        held for one short stretch instead of many, so concurrent requests
        wait less. Always passes a list so the return shape is uniform.
        """
        with self._lock:
            pipe = self._pipeline()
            results: list = []
            for i in range(0, len(texts), INFER_BATCH_SIZE):
                results.extend(pipe(texts[i : i + INFER_BATCH_SIZE], batch_size=INFER_BATCH_SIZE))
            return results

    def detect(self, text: str):
        if not text or not text.strip():
            return []
        spans = []
        seen: set[tuple[int, int, str]] = set()
        bounds = _chunk_bounds(text)
        windows = [text[start:end] for start, end in bounds]
        for (chunk_start, chunk_end), entities in zip(bounds, self._infer(windows)):
            chunk = text[chunk_start:chunk_end]
            for e in entities:
                label = LABEL_MAP.get(e.get("entity_group"))
                if label is None or float(e.get("score", 0)) < self.threshold:
                    continue
                start, end = e.get("start"), e.get("end")
                if start is None or end is None:  # offsets missing -> best-effort find
                    word = (e.get("word") or "").replace(" ", "")
                    start = text.find(word, chunk_start, chunk_end)
                    end = start + len(word) if start >= 0 else -1
                else:
                    start = int(start) + chunk_start
                    end = int(end) + chunk_start
                start, end = int(start), int(end)
                if start < 0 or end > len(text) or start >= end:
                    continue
                # A merged span may straddle several lines; redact each line on its
                # own so line breaks survive and per-line entities stay distinct.
                for sub_start, sub_end in _split_on_newlines(text, start, end, label):
                    seg_end = sub_end
                    if label in ("ORG", "ADDRESS"):
                        seg_end = _extend_right(text, sub_start, sub_end, label)
                    # The overlap zone re-detects the same entity in the
                    # neighboring window; drop exact duplicates (post-extension).
                    key = (sub_start, seg_end, label)
                    if key in seen:
                        continue
                    seen.add(key)
                    surface = text[sub_start:seg_end]
                    metadata = {"ner_type": e.get("entity_group")}
                    if "\n" in surface or "\r" in surface:
                        metadata["canonical_value"] = surface.replace("\r", "").replace(
                            "\n", ""
                        )
                    spans.append(
                        self.make_span(
                            text=text,
                            start=sub_start,
                            end=seg_end,
                            label=label,
                            confidence=float(e.get("score", 0)),
                            risk_level="high",
                            metadata=metadata,
                        )
                    )
        return _postprocess_model_spans(text, spans)


def _trim_model_span(text: str, span: DetectionSpan) -> DetectionSpan | None:
    """Trim punctuation and obvious predicates accidentally swallowed by NER."""

    start, end = span.start, span.end
    while start < end and text[start] in _MODEL_EDGE_CHARS:
        start += 1
    while end > start and text[end - 1] in _MODEL_EDGE_CHARS:
        end -= 1
    if span.label == "ORG":
        value = text[start:end]
        predicate = _ORG_PREDICATE_BOUNDARY_RE.search(value)
        if predicate is not None and predicate.start() >= 2:
            end = start + predicate.start()
    if end <= start:
        return None
    if start == span.start and end == span.end:
        return span
    return replace(
        span,
        start=start,
        end=end,
        text_hash=hash_text(text[start:end]),
        metadata={**span.metadata, "boundary_repair": "trim_noise"},
    )


def _single_line_break(separator: str) -> bool:
    normalized = separator.replace("\r\n", "\n").replace("\r", "\n")
    return normalized.count("\n") == 1 and not normalized.replace("\n", "").strip()


def _merge_split_line_entities(
    text: str, spans: list[DetectionSpan]
) -> list[DetectionSpan]:
    """Join two model halves only when they form one structural entity tail.

    Complete list rows remain separate because a left side that already ends in
    a structural suffix is never joined.  This targets PDF soft wraps such as
    ``苏州万\n和置业有限公司`` and ``投资发\n展有限公司``.
    """

    merged: list[DetectionSpan] = []
    index = 0
    while index < len(spans):
        current = spans[index]
        following = spans[index + 1] if index + 1 < len(spans) else None
        if following is not None and current.label == following.label:
            separator = text[current.end : following.start]
            left = text[current.start : current.end]
            right = text[following.start : following.end]
            combined = left + right
            if (
                current.label == "ORG"
                and _single_line_break(separator)
                and not left.endswith(_HARD_ORG_LINE_SUFFIXES)
                and combined.endswith(_HARD_ORG_LINE_SUFFIXES)
                and len(right) <= 10
            ) or (
                current.label == "ADDRESS"
                and _single_line_break(separator)
                and not left.endswith(_SOFT_ADDRESS_LINE_SUFFIXES)
                and combined.endswith(_SOFT_ADDRESS_LINE_SUFFIXES)
                and len(right) <= 4
            ):
                merged.append(
                    replace(
                        current,
                        end=following.end,
                        confidence=min(current.confidence, following.confidence),
                        text_hash=hash_text(text[current.start : following.end]),
                        metadata={
                            **current.metadata,
                            "canonical_value": combined,
                            "boundary_repair": "line_wrap_pair",
                        },
                    )
                )
                index += 2
                continue
        merged.append(current)
        index += 1
    return merged


def _span_identity(text: str, span: DetectionSpan) -> str:
    value = span.metadata.get("canonical_value")
    if not isinstance(value, str) or not value:
        value = text[span.start : span.end]
    return "".join(value.split())


def _expanded_surface_match(
    text: str, span: DetectionSpan, candidates: tuple[str, ...]
) -> tuple[int, int, str] | None:
    current = _span_identity(text, span)
    window_start = max(0, span.start - _REPEATED_SURFACE_EXPANSION)
    window_end = min(len(text), span.end + _REPEATED_SURFACE_EXPANSION)
    positions: list[int] = []
    compact_chars: list[str] = []
    for position in range(window_start, window_end):
        if text[position].isspace():
            continue
        positions.append(position)
        compact_chars.append(text[position])
    compact_window = "".join(compact_chars)
    for candidate in candidates:
        if len(candidate) <= len(current) or current not in candidate:
            continue
        search_at = 0
        while True:
            found = compact_window.find(candidate, search_at)
            if found < 0:
                break
            expanded_start = positions[found]
            expanded_end = positions[found + len(candidate) - 1] + 1
            raw_surface = text[expanded_start:expanded_end]
            newline_count = raw_surface.replace("\r\n", "\n").replace("\r", "\n").count("\n")
            if (
                expanded_start <= span.start
                and expanded_end >= span.end
                and span.start - expanded_start <= _REPEATED_SURFACE_EXPANSION
                and expanded_end - span.end <= _REPEATED_SURFACE_EXPANSION
                and newline_count <= 1
            ):
                return expanded_start, expanded_end, candidate
            search_at = found + 1
    return None


def _repair_repeated_surface_boundaries(
    text: str, spans: list[DetectionSpan]
) -> list[DetectionSpan]:
    """Use a repeated complete surface to repair shifted or line-wrapped NER.

    The rule is document-generic: an expansion is accepted only when the exact
    longer surface was independently detected elsewhere and the local source
    text contains that same surface (allowing one soft line break).
    """

    candidates: dict[str, set[str]] = {"ORG": set(), "ADDRESS": set()}
    for span in spans:
        if span.label not in candidates:
            continue
        value = _span_identity(text, span)
        if len(value) >= 3 and value not in _GENERIC_MODEL_VALUES.get(span.label, set()):
            candidates[span.label].add(value)

    ordered_candidates = {
        label: tuple(sorted(values, key=lambda value: (-len(value), value)))
        for label, values in candidates.items()
    }
    repaired: list[DetectionSpan] = []
    for span in spans:
        candidate_match = _expanded_surface_match(
            text, span, ordered_candidates.get(span.label, ())
        )
        if candidate_match is not None:
            start, end, canonical = candidate_match
            span = replace(
                span,
                start=start,
                end=end,
                text_hash=hash_text(text[start:end]),
                metadata={
                    **span.metadata,
                    "canonical_value": canonical,
                    "boundary_repair": "repeated_surface",
                },
            )
        repaired.append(span)

    org_surfaces = {
        _span_identity(text, span) for span in repaired if span.label == "ORG"
    }
    return [
        replace(
            span,
            label="ORG",
            metadata={**span.metadata, "boundary_repair": "cross_label_surface"},
        )
        if span.label == "ADDRESS"
        and _span_identity(text, span) in org_surfaces
        and not _span_identity(text, span).endswith(_SOFT_ADDRESS_LINE_SUFFIXES)
        else span
        for span in repaired
    ]


def _postprocess_model_spans(
    text: str, spans: list[DetectionSpan]
) -> list[DetectionSpan]:
    """Repair confirmed CLUENER boundary noise without domain guesswork."""
    trimmed = [item for span in spans if (item := _trim_model_span(text, span))]
    ordered = sorted(trimmed, key=lambda item: (item.start, item.end, item.label))
    ordered = _merge_split_line_entities(text, ordered)
    ordered = _repair_repeated_surface_boundaries(text, ordered)
    ordered = sorted(ordered, key=lambda item: (item.start, item.end, item.label))
    repaired: list[DetectionSpan] = []
    index = 0
    while index < len(ordered):
        current = ordered[index]
        following = ordered[index + 1] if index + 1 < len(ordered) else None
        current_value = text[current.start : current.end]
        if (
            current.label == "ORG"
            and current_value.endswith("大")
            and text[current.end : current.end + 1] == "学"
        ):
            consumes_following = (
                following is not None
                and following.label == "ADDRESS"
                and current.end == following.start
                and following.end == current.end + 1
            )
            repaired.append(
                replace(
                    current,
                    end=current.end + 1,
                    confidence=(
                        min(current.confidence, following.confidence)
                        if consumes_following
                        else current.confidence
                    ),
                    text_hash=hash_text(text[current.start : current.end + 1]),
                    metadata={
                        **current.metadata,
                        "boundary_repair": "university_suffix",
                    },
                )
            )
            index += 2 if consumes_following else 1
            continue
        if current.label == "ADDRESS" and current_value.endswith("大学"):
            repaired.append(
                replace(
                    current,
                    label="ORG",
                    metadata={
                        **current.metadata,
                        "boundary_repair": "university_label",
                    },
                )
            )
            index += 1
            continue
        repaired.append(current)
        index += 1

    filtered: list[DetectionSpan] = []
    seen: set[tuple[int, int, str]] = set()
    for span in repaired:
        value = text[span.start : span.end]
        compact = "".join(value.split())
        # This detector is a Chinese CLUENER model. Its predictions over pure
        # ASCII/protocol text (tool descriptions, JSON schema prose, model ids)
        # are unsupported and routinely manufacture fragments such as
        # ``tment`` or ``darwin``. Rules/dictionaries still protect structured
        # identifiers and explicitly declared English names; model-only entity
        # spans must contain at least two Han characters to be trusted.
        if len(_CJK_RE.findall(compact)) < 2:
            continue
        if span.label in {"ORG", "ADDRESS"} and len(compact) == 1:
            continue
        if compact in _GENERIC_MODEL_VALUES.get(span.label, set()):
            continue
        if span.label == "ORG" and compact.endswith(_LITIGATION_PUBLIC_ORG_SUFFIXES):
            continue
        if span.label == "ORG" and _is_generic_org_reference(compact):
            continue
        key = (span.start, span.end, span.label)
        if key not in seen:
            seen.add(key)
            filtered.append(span)
    return filtered


def _is_generic_org_reference(value: str) -> bool:
    compact = value.replace("、", "").replace("，", "")
    return len(compact) >= 2 and any(
        compact in generic for generic in _GENERIC_ORG_REFERENCE_FORMS
    )


_NER_SINGLETON: CluenerNerDetector | None = None
_NER_SINGLETON_LOCK = threading.Lock()


def get_ner_detector(threshold: float = 0.5) -> CluenerNerDetector:
    """Process-cached NER detector (model loads once, on first detect)."""
    global _NER_SINGLETON
    with _NER_SINGLETON_LOCK:
        if _NER_SINGLETON is None:
            _NER_SINGLETON = CluenerNerDetector(threshold=threshold)
        return _NER_SINGLETON
