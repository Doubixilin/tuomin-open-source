from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from tuomin_gateway.detectors.base import BaseDetector
from tuomin_gateway.schemas import DetectionSpan


class ModelOutputAdapter(BaseDetector):
    """Normalize model-style offset outputs into DetectionSpan objects.

    Expected records look like:
    {"start": 0, "end": 3, "label": "PERSON", "score": 0.91}
    """

    name = "model"

    def __init__(self, source: str = "model", version: str = "model-adapter-v1") -> None:
        self.name = source
        self.version = version

    def detect(self, text: str) -> list[DetectionSpan]:
        return self.normalize(text, self.raw_outputs(text))

    def raw_outputs(self, text: str) -> Iterable[dict[str, Any]]:
        raise NotImplementedError

    def normalize(self, text: str, outputs: Iterable[dict[str, Any]]) -> list[DetectionSpan]:
        spans: list[DetectionSpan] = []
        for output in outputs:
            span = self._normalize_one(text, output)
            if span is not None:
                spans.append(span)
        return sorted(spans, key=lambda item: (item.start, item.end, item.label, item.source))

    def _normalize_one(self, text: str, output: dict[str, Any]) -> DetectionSpan | None:
        try:
            start = int(output["start"])
            end = int(output["end"])
            label = str(output["label"])
            confidence = float(output.get("score", output.get("confidence", 0.0)))
        except (KeyError, TypeError, ValueError):
            return None
        if start < 0 or end > len(text) or start >= end or not label.strip():
            return None
        confidence = max(0.0, min(1.0, confidence))
        return self.make_span(
            text=text,
            start=start,
            end=end,
            label=label,
            confidence=confidence,
            risk_level=str(output.get("risk_level", "unknown")),
            metadata=_safe_metadata(output),
        )


class FakeModelDetectorAdapter(ModelOutputAdapter):
    """Synthetic-only adapter for protocol tests; never calls external models."""

    def __init__(
        self,
        outputs: Iterable[dict[str, Any]],
        source: str = "model",
        version: str = "fake-model-adapter-v1",
    ) -> None:
        super().__init__(source=source, version=version)
        self._outputs = list(outputs)

    def raw_outputs(self, text: str) -> Iterable[dict[str, Any]]:
        return list(self._outputs)


def _safe_metadata(output: dict[str, Any]) -> dict[str, Any]:
    blocked = {"text", "value", "raw", "span", "original", "original_value"}
    return {str(key): value for key, value in output.items() if str(key) not in blocked}
