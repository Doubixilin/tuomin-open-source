"""Run an optional comparison engine on three synthetic contract sections."""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path


from synthetic_contract_fixture import DICTIONARY, FIXTURE, reference_metrics, run_metadata


def _select_non_overlapping(spans: list[dict]) -> list[dict]:
    chosen: list[dict] = []
    for span in sorted(spans, key=lambda item: (-(item["end"] - item["start"]), item["start"])):
        if any(span["start"] < old["end"] and old["start"] < span["end"] for old in chosen):
            continue
        chosen.append(span)
    return sorted(chosen, key=lambda item: (item["start"], item["end"]))


def _mask(text: str, spans: list[dict]) -> tuple[str, list[dict]]:
    spans = _select_non_overlapping(spans)
    counters: dict[str, int] = {}
    replacements = []
    for span in spans:
        label = re.sub(r"[^A-Z0-9]+", "_", span["label"].upper()).strip("_") or "PII"
        counters[label] = counters.get(label, 0) + 1
        replacements.append((span["start"], span["end"], f"<{label}_{counters[label]:03d}>"))
    masked = text
    for start, end, replacement in reversed(replacements):
        masked = masked[:start] + replacement + masked[end:]
    return masked, spans


def _presidio_detector(*, dictionary: bool):
    from presidio_analyzer import AnalyzerEngine, PatternRecognizer
    from presidio_analyzer.nlp_engine import NoOpNlpEngine

    nlp = NoOpNlpEngine(models=[{"lang_code": "en", "model_name": "no_op"}])
    nlp.load()
    analyzer = AnalyzerEngine(nlp_engine=nlp)
    if dictionary:
        entries = json.loads(DICTIONARY.read_text(encoding="utf-8"))
        grouped: dict[str, list[str]] = {}
        for entry in entries:
            grouped.setdefault(entry["label"], []).append(entry["canonical_value"])
        for label, values in sorted(grouped.items()):
            analyzer.registry.add_recognizer(
                PatternRecognizer(
                    supported_entity=f"SYNTHETIC_{label}",
                    deny_list=values,
                    supported_language="en",
                )
            )

    def detect(text: str) -> list[dict]:
        return [
            {
                "label": item.entity_type,
                "start": item.start,
                "end": item.end,
                "score": round(float(item.score), 4),
                "source": "presidio",
            }
            for item in analyzer.analyze(text=text, language="en")
        ]

    return detect


def _oneaifw_detector(assets: Path):
    sys.path.insert(0, str(assets / "aifw/py-origin"))
    from services.app.analyzer import AnalyzerWrapper

    analyzer = AnalyzerWrapper()

    def detect(text: str) -> list[dict]:
        return [
            {
                "label": item.entity_type,
                "start": item.start,
                "end": item.end,
                "score": round(float(item.score), 4),
                "source": "oneaifw-py-origin",
            }
            for item in analyzer.analyze(text, language="zh")
        ]

    return detect


def _opf_detector(assets: Path):
    sys.path.insert(0, str(assets / "privacy-filter"))
    from opf import OPF

    detector = OPF(
        model=assets / "opf-model/original",
        device="cpu",
        output_mode="typed",
    )

    def detect(text: str) -> list[dict]:
        result = detector.redact(text)
        return [
            {
                "label": item.label,
                "start": item.start,
                "end": item.end,
                "score": None,
                "source": "opf-official",
            }
            for item in result.detected_spans
        ]

    return detect


def _strings_in(value: object):
    if isinstance(value, str):
        yield value
    elif isinstance(value, list):
        for item in value:
            yield from _strings_in(item)
    elif isinstance(value, dict):
        for item in value.values():
            yield from _strings_in(item)


def _parse_first_json(text: str) -> dict | None:
    start = text.find("{")
    if start < 0:
        return None
    try:
        value, _ = json.JSONDecoder().raw_decode(text[start:])
    except json.JSONDecodeError:
        return None
    return value if isinstance(value, dict) else None


def _has_batch(texts: list[str], assets: Path) -> tuple[list[list[dict]], list[str]]:
    import torch
    from transformers import AutoModelForCausalLM, AutoTokenizer

    entity_types = [
        "Person", "Organization", "Phone Number", "Email Address", "Contact Information",
        "Supplier", "Project", "Contract ID", "Amount", "Date", "Location", "Bank Account",
        "System URL", "Credential", "Department", "Organization Code", "Address", "Employee ID",
        "ID Card",
    ]
    model_path = assets / "has-full"
    tokenizer = AutoTokenizer.from_pretrained(model_path, local_files_only=True)
    tokenizer.padding_side = "left"
    if tokenizer.pad_token is None:
        tokenizer.pad_token = tokenizer.eos_token
    if tokenizer.chat_template is None:
        # The pinned HaS tokenizer declares Qwen ChatML tokens but not the
        # template itself. Transformers 5 no longer supplies an implicit
        # class default, so make the repository's expected one-turn envelope
        # explicit and version it with this adapter.
        tokenizer.chat_template = (
            "{% for message in messages %}"
            "{{ '<|im_start|>' + message['role'] + '\\n' + message['content'] + '<|im_end|>\\n' }}"
            "{% endfor %}"
            "{% if add_generation_prompt %}{{ '<|im_start|>assistant\\n' }}{% endif %}"
        )
    model = AutoModelForCausalLM.from_pretrained(model_path, local_files_only=True, dtype=torch.bfloat16)
    prompts = []
    for text in texts:
        prompt = (
            "Recognize the following entity types in the text.\n"
            f"Specified types:{json.dumps(entity_types, ensure_ascii=False, separators=(',', ':'))}\n"
            f"<text>{text}</text>"
        )
        prompts.append(
            tokenizer.apply_chat_template(
                [{"role": "user", "content": prompt}],
                tokenize=False,
                add_generation_prompt=True,
            )
        )
    inputs = tokenizer(prompts, return_tensors="pt", padding=True)
    prompt_length = inputs.input_ids.shape[1]
    generated = model.generate(**inputs, max_new_tokens=300, do_sample=False)
    outputs = [tokenizer.decode(row[prompt_length:], skip_special_tokens=True) for row in generated]
    predictions = []
    for text, output in zip(texts, outputs):
        parsed = _parse_first_json(output)
        spans = []
        if parsed is not None:
            for value in _strings_in(parsed):
                value = value.strip()
                if not value:
                    continue
                offset = 0
                while True:
                    start = text.find(value, offset)
                    if start < 0:
                        break
                    spans.append(
                        {
                            "label": "PII",
                            "start": start,
                            "end": start + len(value),
                            "score": None,
                            "source": "has-text-0.6b",
                        }
                    )
                    offset = start + max(1, len(value))
        predictions.append(spans)
    return predictions, outputs


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--engine",
        choices=("presidio-default", "presidio-dictionary", "oneaifw", "opf", "has"),
        required=True,
    )
    parser.add_argument(
        "--assets-dir", type=Path, help="Local directory containing aifw/py-origin, privacy-filter and opf-model/original, or has-full."
    )
    parser.add_argument("--revision", help="Version or commit of the locally installed engine and model.")
    args = parser.parse_args()
    if args.engine in {"oneaifw", "opf", "has"} and not args.assets_dir:
        parser.error("--assets-dir is required for this engine")
    fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
    texts = [case["text"] for case in fixture["cases"]]

    started = time.perf_counter()
    raw_model_outputs = None
    if args.engine == "presidio-default":
        detector = _presidio_detector(dictionary=False)
        predictions = [detector(text) for text in texts]
        engine_name = "Microsoft Presidio"
    elif args.engine == "presidio-dictionary":
        detector = _presidio_detector(dictionary=True)
        predictions = [detector(text) for text in texts]
        engine_name = "Microsoft Presidio"
    elif args.engine == "oneaifw":
        detector = _oneaifw_detector(args.assets_dir)
        predictions = [detector(text) for text in texts]
        engine_name = "OneAIFW"
    elif args.engine == "opf":
        detector = _opf_detector(args.assets_dir)
        predictions = [detector(text) for text in texts]
        engine_name = "OpenAI Privacy Filter"
    else:
        predictions, raw_model_outputs = _has_batch(texts, args.assets_dir)
        engine_name = "HaS Text 0.6B"

    cases = []
    sensitive_total = sensitive_handled = utility_total = utility_preserved = 0
    for index, (case, spans) in enumerate(zip(fixture["cases"], predictions)):
        masked, selected = _mask(case["text"], spans)
        metrics = reference_metrics(case, masked)
        sensitive_total += metrics["sensitive_total"]
        sensitive_handled += metrics["sensitive_handled"]
        utility_total += metrics["utility_total"]
        utility_preserved += metrics["utility_preserved"]
        item = {
            "case_id": case["id"],
            "section": case["section"],
            "masked_text": masked,
            "metrics": metrics,
            "detection_count": len(selected),
            "detections": [
                {
                    "label": span["label"],
                    "source": span["source"],
                    "surface": case["text"][span["start"] : span["end"]],
                    "score": span["score"],
                }
                for span in selected
            ],
        }
        if raw_model_outputs is not None:
            item["raw_model_output"] = raw_model_outputs[index]
            item["parse_ok"] = _parse_first_json(raw_model_outputs[index]) is not None
        cases.append(item)

    elapsed = time.perf_counter() - started
    payload = {
        **run_metadata(),
        "engine": engine_name,
        "mode": args.engine,
        "revision": args.revision,
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
        "cases": cases,
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
