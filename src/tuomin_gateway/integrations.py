"""Provider-neutral adapters for Tuomin's primary local consumers.

These adapters intentionally stop at text boundaries. They do not know about a
specific LLM request schema, vector database, or UI framework: consuming apps
send only masked text to those systems and keep trusted refill in their local
display process.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

from tuomin_gateway.client import TuominClient


@dataclass
class _NamespaceAdapter:
    client: TuominClient
    namespace_id: str

    workflow: ClassVar[str] = "namespace"

    @classmethod
    def start(cls, client: TuominClient):
        created = client.create_namespace()
        return cls(client=client, namespace_id=created["namespace_id"])

    @classmethod
    def resume(cls, client: TuominClient, namespace_id: str):
        client.namespace_status(namespace_id)
        return cls(client=client, namespace_id=namespace_id)

    def mask_text(self, text: str) -> dict:
        return self.client.namespace_redact(self.namespace_id, text)

    def mask_batch(self, items: list[dict[str, str]]) -> dict:
        return self.client.redact_batch(items, namespace_id=self.namespace_id)

    def restore_for_display(self, mapping_handle: str, text: str) -> dict:
        return self.client.refill(
            mapping_handle,
            text,
            contract="trusted_display",
        )

    def status(self) -> dict:
        return self.client.namespace_status(self.namespace_id)

    def archive(self) -> dict:
        return self.client.archive_namespace(self.namespace_id)


class ContractReviewAdapter(_NamespaceAdapter):
    """One persistent namespace per local contract review run."""

    workflow = "contract_review"


class InvestmentReviewAdapter(_NamespaceAdapter):
    """One resumable namespace per investment project or agreement package."""

    workflow = "investment_review"


class KnowledgeBaseAdapter(_NamespaceAdapter):
    """One persistent namespace per corpus, shared by ingestion and querying."""

    workflow = "knowledge_base"

    @classmethod
    def create(cls, client: TuominClient):
        return cls.start(client)

    def mask_documents(self, items: list[dict[str, str]]) -> dict:
        return self.mask_batch(items)

    def mask_query(self, text: str) -> dict:
        return self.mask_text(text)

    def restore_answer(self, mapping_handle: str, text: str) -> dict:
        return self.restore_for_display(mapping_handle, text)


__all__ = [
    "ContractReviewAdapter",
    "InvestmentReviewAdapter",
    "KnowledgeBaseAdapter",
]
