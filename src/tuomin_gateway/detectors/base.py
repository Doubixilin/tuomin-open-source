from __future__ import annotations

from abc import ABC, abstractmethod

from tuomin_gateway.schemas import DetectionSpan, hash_text

# Re-exported so existing callers (benchmark/redactor/session) keep importing
# from here; the canonical home is ``schemas`` (see hash_text there).
__all__ = ["BaseDetector", "hash_text"]


class BaseDetector(ABC):
    name: str
    version: str

    @abstractmethod
    def detect(self, text: str) -> list[DetectionSpan]:
        raise NotImplementedError

    def make_span(
        self,
        text: str,
        start: int,
        end: int,
        label: str,
        confidence: float,
        risk_level: str = "unknown",
        metadata: dict | None = None,
    ) -> DetectionSpan:
        return DetectionSpan(
            start=start,
            end=end,
            label=label,
            confidence=confidence,
            source=self.name,
            detector_version=self.version,
            text_hash=hash_text(text[start:end]),
            risk_level=risk_level,
            metadata=metadata or {},
        )
