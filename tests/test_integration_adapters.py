from fastapi.testclient import TestClient

from tuomin_gateway.client import TuominClient
from tuomin_gateway.integrations import (
    ContractReviewAdapter,
    InvestmentReviewAdapter,
    KnowledgeBaseAdapter,
)
from tuomin_gateway.service.app import create_app
from tuomin_gateway.service.registry import AppRegistry, hash_capability_token
from tuomin_gateway.store import MappingStore


TOKENS = {
    "redact": "synthetic-redact-token",
    "trusted_refill": "synthetic-refill-token",
    "namespace": "synthetic-namespace-token",
}


def _client(tmp_path, app_id):
    registry = AppRegistry(
        {
            app_id: {
                "profile": {"base": "kb", "use_ner": False},
                "capabilities": list(TOKENS),
                "capability_tokens": {
                    capability: hash_capability_token(token)
                    for capability, token in TOKENS.items()
                },
                "mapping_scopes": ["document", "namespace"],
                "refill_contracts": ["trusted_display", "exact_transform"],
            }
        }
    )
    http = TestClient(create_app(registry=registry, store=MappingStore(tmp_path / app_id)))
    return TuominClient(app_id=app_id, capability_tokens=TOKENS, http_client=http)


def test_contract_review_run_masks_batch_and_only_restores_in_trusted_display(tmp_path):
    adapter = ContractReviewAdapter.start(_client(tmp_path, "contract_review"))

    masked = adapter.mask_batch(
        [
            {"id": "clause-1", "text": "联系电话13900001111"},
            {"id": "clause-2", "text": "再次联系13900001111"},
        ]
    )
    shown = adapter.restore_for_display(
        masked["items"][1]["mapping_handle"],
        "审查意见涉及 <CONTACT_001>",
    )

    assert adapter.workflow == "contract_review"
    assert masked["items"][0]["masked_text"] == "联系电话<CONTACT_001>"
    assert masked["items"][1]["masked_text"] == "再次联系<CONTACT_001>"
    assert shown["text"] == "审查意见涉及 13900001111"


def test_investment_review_uses_persistent_namespace_across_batches(tmp_path):
    client = _client(tmp_path, "investment_review")
    first = InvestmentReviewAdapter.start(client)
    namespace_id = first.namespace_id
    first_batch = first.mask_batch([{"id": "memo", "text": "联系电话13900001111"}])

    resumed = InvestmentReviewAdapter.resume(client, namespace_id)
    second_batch = resumed.mask_batch([{"id": "appendix", "text": "联系人13900001111"}])

    assert resumed.workflow == "investment_review"
    assert first_batch["items"][0]["masked_text"] == "联系电话<CONTACT_001>"
    assert second_batch["items"][0]["masked_text"] == "联系人<CONTACT_001>"


def test_knowledge_base_corpus_masks_documents_query_and_restores_answer(tmp_path):
    adapter = KnowledgeBaseAdapter.create(_client(tmp_path, "knowledge_base"))

    documents = adapter.mask_documents(
        [
            {"id": "doc-1", "text": "项目联系人13900001111"},
            {"id": "doc-2", "text": "联系电话13900001111"},
        ]
    )
    query = adapter.mask_query("查询联系人13900001111")
    answer = adapter.restore_answer(
        query["mapping_handle"],
        "命中文档联系人为 <CONTACT_001>",
    )

    assert adapter.workflow == "knowledge_base"
    assert documents["items"][0]["masked_text"] == "项目联系人<CONTACT_001>"
    assert documents["items"][1]["masked_text"] == "联系电话<CONTACT_001>"
    assert query["masked_text"] == "查询联系人<CONTACT_001>"
    assert answer["text"] == "命中文档联系人为 13900001111"
