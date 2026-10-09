from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from tuomin_gateway.detectors.adapters import ModelOutputAdapter


class OpenAIPrivacyFilterUnavailable(RuntimeError):
    """Raised only when an explicitly configured future adapter cannot run."""


class OpenAIPrivacyFilterAdapter(ModelOutputAdapter):
    """Lazy boundary for a future OpenAI Privacy Filter detector.

    This first version deliberately does not import transformers, download
    models, or load external artifacts. Unconfigured instances return no spans.
    """

    def __init__(
        self,
        model_path: str | None = None,
        enabled: bool = False,
        version: str = "openai-privacy-filter-adapter-v1",
    ) -> None:
        super().__init__(source="openai_privacy_filter", version=version)
        self.model_path = model_path
        self.enabled = enabled

    def raw_outputs(self, text: str) -> Iterable[dict[str, Any]]:
        if not self.enabled or not self.model_path:
            return []
        raise OpenAIPrivacyFilterUnavailable(
            "OpenAI Privacy Filter adapter is declared but no local model runtime is wired in this build."
        )
