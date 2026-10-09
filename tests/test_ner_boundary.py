"""NER right-boundary extension (pure function; no model needed).

Targets the observed leak shape where NER clips an ORG/ADDRESS name before a
structural tail and the rest leaks as plaintext (e.g. "<ORG>理股权投资项目").
"""
from tuomin_gateway.detectors.ner import (
    _extend_right,
    _postprocess_model_spans,
    _split_on_newlines,
)
from tuomin_gateway.schemas import DetectionSpan, hash_text


def test_extends_org_through_project_tail():
    text = "某资管理股权投资项目签约"          # NER clips ORG at "某资管" (0:3)
    assert _extend_right(text, 0, 3) == text.index("项目") + 2  # absorbs 理…项目
    assert text[0:_extend_right(text, 0, 3)] == "某资管理股权投资项目"


def test_extends_address_through_number_and_suffix():
    text = "测试大道100号附近"                  # ADDRESS clipped at "测试大道" (0:4)
    end = _extend_right(text, 0, 4, "ADDRESS")  # address keeps the 号 unit suffix
    assert text[0:end] == "测试大道100号"        # absorbs 100号, stops before 附近


def test_org_extension_does_not_swallow_building_unit():
    # ORG must NOT absorb a trailing building unit like "座" — that is the next
    # token, not part of the company name (the over-redaction H1 guarded against).
    text = "某科技公司座落于此"                  # ORG ends at "某科技公司" (0:5)
    assert _extend_right(text, 0, 5, "ORG") == 5


def test_does_not_extend_across_connector_or_stopword():
    text = "某公司是本市投资中心"                # must not absorb "是本市投资中心"
    assert _extend_right(text, 0, 3) == 3       # stops at the stopword 是


def test_does_not_extend_without_a_known_tail():
    text = "某机构随便一些字"                    # no structural suffix -> no extension
    assert _extend_right(text, 0, 3) == 3


def test_no_extension_at_text_end():
    text = "某公司"
    assert _extend_right(text, 0, 3) == 3


def test_org_extension_completes_company_suffix_across_pdf_soft_wrap():
    text = "上海示例甲置业\n有限公司负责开发"
    end = _extend_right(text, 0, len("上海示例甲置业"), "ORG")

    assert text[:end] == "上海示例甲置业\n有限公司"


def test_address_extension_completes_base_suffix_across_pdf_soft_wrap():
    text = "中建国际总部基\n地块规划"
    end = _extend_right(text, 0, len("中建国际总部基"), "ADDRESS")

    assert text[:end] == "中建国际总部基\n地"


def test_org_extension_does_not_cross_newline_into_another_org():
    text = "中国建筑\n中建八局"

    assert _extend_right(text, 0, len("中国建筑"), "ORG") == len("中国建筑")


def _segments(text: str, start: int, end: int, label: str = "ORG") -> list[str]:
    return [text[s:e] for s, e in _split_on_newlines(text, start, end, label)]


def test_split_keeps_single_line_span_unchanged():
    text = "中国建筑承建"
    assert _split_on_newlines(text, 0, 4) == [(0, 4)]


def test_split_breaks_a_merged_multiline_org_span():
    # The bug: NER merges 4 company names (one per line) into one span that
    # swallows the \n between them. Splitting yields 4 distinct per-line spans.
    text = "中国建筑\n中建八局\n中建八局一公司\n中建八局三公司"
    assert _segments(text, 0, len(text)) == [
        "中国建筑",
        "中建八局",
        "中建八局一公司",
        "中建八局三公司",
    ]


def test_split_trims_surrounding_whitespace_and_blank_lines():
    text = "甲公司 \n\n  乙公司\t"
    assert _segments(text, 0, len(text)) == ["甲公司", "乙公司"]


def test_split_rejoins_short_pdf_soft_wrap_in_public_authority():
    text = "国土资\n源部"
    assert _segments(text, 0, len(text)) == [text]


def test_split_rejoins_pdf_soft_wrap_in_regional_authority():
    text = "上海市宝\n山区规划和自然资源局"
    assert _segments(text, 0, len(text)) == [text]


def test_split_rejoins_short_address_soft_wrap():
    text = "上海\n市"
    assert _segments(text, 0, len(text), "ADDRESS") == [text]


def test_split_does_not_join_two_line_org_list():
    text = "中国建筑\n中建八局"
    assert _segments(text, 0, len(text)) == ["中国建筑", "中建八局"]


def test_split_then_extend_recovers_each_line_tail():
    # Each line is independently right-extended through its structural tail,
    # never across the \n into the next line.
    text = "某资管理股权投资项目\n某科技公司"
    segs = _split_on_newlines(text, 0, len(text))
    assert len(segs) == 2
    s0, _e0 = segs[0]
    assert text[s0 : _extend_right(text, *segs[0], "ORG")] == "某资管理股权投资项目"


def _model_span(text: str, value: str, label: str, *, start: int = 0) -> DetectionSpan:
    begin = text.index(value, start)
    return DetectionSpan(
        start=begin,
        end=begin + len(value),
        label=label,
        confidence=0.95,
        source="model",
        detector_version="test-ner",
        text_hash=hash_text(value),
    )


def test_postprocess_drops_court_and_arbitration_org_spans():
    text = "本案由广州市中级人民法院管辖，争议提交广州仲裁委员会裁决。"
    spans = [
        _model_span(text, "广州市中级人民法院", "ORG"),
        _model_span(text, "广州仲裁委员会", "ORG"),
    ]

    assert _postprocess_model_spans(text, spans) == []


def test_postprocess_keeps_non_litigation_org_spans():
    text = "某建工集团有限公司承建该项目。"
    spans = [_model_span(text, "某建工集团有限公司", "ORG")]

    repaired = _postprocess_model_spans(text, spans)

    assert len(repaired) == 1


def test_postprocess_joins_two_pdf_line_wrap_halves_of_one_company():
    text = "转让给江苏示例投资发\n展有限公司后交割"
    spans = [
        _model_span(text, "江苏示例投资发", "ORG"),
        _model_span(text, "展有限公司", "ORG"),
    ]

    repaired = _postprocess_model_spans(text, spans)

    assert len(repaired) == 1
    assert text[repaired[0].start : repaired[0].end] == "江苏示例投资发\n展有限公司"
    assert repaired[0].metadata["canonical_value"] == "江苏示例投资发展有限公司"


def test_postprocess_uses_repeated_complete_surface_for_soft_wrap_fragment():
    text = "中建国际确认。后续由中建国\n际自行运营。"
    second = text.rindex("中建国")
    spans = [
        _model_span(text, "中建国际", "ORG"),
        _model_span(text, "中建国", "ORG", start=second),
    ]

    repaired = _postprocess_model_spans(text, spans)
    wrapped = next(item for item in repaired if "\n" in text[item.start : item.end])

    assert text[wrapped.start : wrapped.end] == "中建国\n际"
    assert wrapped.metadata["canonical_value"] == "中建国际"


def test_postprocess_expands_shifted_address_to_repeated_complete_surface():
    text = "中建国际总部基地开工，另见中建国际总部基地规划。"
    second = text.rindex("中建国际总部基地")
    spans = [
        _model_span(text, "中建国际总部基地", "ADDRESS"),
        _model_span(text, "际总部基地", "ADDRESS", start=second),
    ]

    repaired = _postprocess_model_spans(text, spans)

    assert any(
        item.start == second
        and text[item.start : item.end] == "中建国际总部基地"
        for item in repaired
    )


def test_postprocess_trims_org_predicate_and_quote_noise():
    text = "中建国际有意收购，简称“万和置业”"
    spans = [
        _model_span(text, "中建国际有意收购", "ORG"),
        _model_span(text, "万和置业”", "ORG"),
    ]

    values = [text[item.start : item.end] for item in _postprocess_model_spans(text, spans)]

    assert values == ["中建国际", "万和置业"]


def test_postprocess_drops_generic_project_company_reference():
    text = "项目公司负责交割"

    assert _postprocess_model_spans(
        text, [_model_span(text, "项目公司", "ORG")]
    ) == []


def test_postprocess_drops_line_fragment_of_generic_project_company():
    text = "目公司负责交割"

    assert _postprocess_model_spans(
        text, [_model_span(text, "目公司", "ORG")]
    ) == []


def test_postprocess_drops_model_predictions_outside_chinese_training_domain():
    text = "darwin tment 96"
    spans = [
        _model_span(text, "darwin", "ORG"),
        _model_span(text, "tment", "ORG"),
        _model_span(text, "96", "ORG"),
    ]

    assert _postprocess_model_spans(text, spans) == []


def test_postprocess_drops_clipped_company_suffix():
    text = "限公司负责交割"

    assert _postprocess_model_spans(
        text, [_model_span(text, "限公司", "ORG")]
    ) == []

def test_cluener_pipeline_loads_transformers_with_local_files_only(monkeypatch):
    monkeypatch.delenv("TUOMIN_NER_MODEL_DIR", raising=False)
    import sys
    from types import SimpleNamespace

    from tuomin_gateway.detectors.ner import CluenerNerDetector

    calls = []

    class FakeTokenizer:
        @staticmethod
        def from_pretrained(model_name, *, local_files_only=False, revision=None):
            calls.append(("tokenizer", model_name, local_files_only, revision))
            return object()

    class FakeModel:
        @staticmethod
        def from_pretrained(model_name, *, local_files_only=False, revision=None):
            calls.append(("model", model_name, local_files_only, revision))
            return object()

    def fake_pipeline(task, *, model, tokenizer, aggregation_strategy):
        calls.append(("pipeline", task, aggregation_strategy, model is not None, tokenizer is not None))
        return lambda text: []

    monkeypatch.setitem(
        sys.modules,
        "transformers",
        SimpleNamespace(
            AutoModelForTokenClassification=FakeModel,
            AutoTokenizer=FakeTokenizer,
            pipeline=fake_pipeline,
        ),
    )

    detector = CluenerNerDetector(
        model_name="synthetic-local-model",
        model_revision="synthetic-revision",
    )
    detector._pipeline()

    assert calls == [
        ("tokenizer", "synthetic-local-model", True, "synthetic-revision"),
        ("model", "synthetic-local-model", True, "synthetic-revision"),
        ("pipeline", "token-classification", "simple", True, True),
    ]
