import pytest
from fastapi.testclient import TestClient

from tuomin_gateway.client import TuominApiError, TuominClient, TuominEgressDenied
from tuomin_gateway.service.app import create_app
from tuomin_gateway.service.registry import AppRegistry, hash_capability_token
from tuomin_gateway.store import MappingStore


TOKENS = {
    "detect": "detect-token",
    "redact": "redact-token",
    "trusted_refill": "refill-token",
    "namespace": "namespace-token",
}


def _client(tmp_path, tokens=TOKENS):
    registry = AppRegistry(
        {
            "synthetic_client": {
                "profile": "kb",
                "capabilities": list(TOKENS),
                "capability_tokens": {
                    capability: hash_capability_token(token)
                    for capability, token in TOKENS.items()
                },
                "mapping_scopes": ["document", "namespace"],
                "refill_contracts": ["trusted_display", "exact_transform"],
                "structured_value_labels": ["PROJECT_NAME", "DOCUMENT_NAME"],
            }
        }
    )
    http = TestClient(create_app(registry=registry, store=MappingStore(tmp_path / "maps")))
    return TuominClient(
        app_id="synthetic_client", capability_tokens=tokens, http_client=http
    )


def test_client_document_redact_and_trusted_refill(tmp_path):
    client = _client(tmp_path)

    redacted = client.redact("电话13900001111")
    restored = client.refill(
        redacted["mapping_handle"], redacted["masked_text"], contract="trusted_display"
    )

    assert "13900001111" not in redacted["masked_text"]
    assert restored["text"] == "电话13900001111"


def test_client_namespace_lifecycle(tmp_path):
    client = _client(tmp_path)

    created = client.create_namespace()
    namespace_id = created["namespace_id"]
    masked = client.namespace_redact(namespace_id, "电话13900001111")
    status = client.namespace_status(namespace_id)
    archived = client.archive_namespace(namespace_id)
    deleted = client.delete_namespace(namespace_id)

    assert "<CONTACT_001>" in masked["masked_text"]
    assert status["placeholder_count"] == 1
    assert archived["namespace_status"] == "archived"
    assert deleted["deleted"] is True


def test_client_document_and_namespace_batch(tmp_path):
    client = _client(tmp_path)

    documents = client.redact_batch(
        [
            {"id": "a", "text": "电话13900001111"},
            {"id": "b", "text": "电话13800002222"},
        ]
    )
    namespace_id = client.create_namespace()["namespace_id"]
    namespace = client.redact_batch(
        [
            {"id": "a", "text": "电话13900001111"},
            {"id": "b", "text": "再次联系13900001111"},
        ],
        namespace_id=namespace_id,
    )

    assert documents["scope"] == "document"
    assert documents["items"][0]["mapping_handle"] != documents["items"][1]["mapping_handle"]
    assert namespace["scope"] == "namespace"
    assert namespace["items"][0]["masked_text"] == "电话<CONTACT_001>"
    assert namespace["items"][1]["masked_text"] == "再次联系<CONTACT_001>"


def test_client_raises_safe_error_without_token_value(tmp_path):
    client = _client(tmp_path, tokens={**TOKENS, "redact": "wrong-secret"})

    try:
        client.redact("电话13900001111")
    except TuominApiError as exc:
        assert exc.code == "capability_denied"
        assert "wrong-secret" not in str(exc)
    else:  # pragma: no cover
        raise AssertionError("invalid token was accepted")


def test_client_redact_values_and_assert_egress(tmp_path):
    client = _client(tmp_path)
    namespace_id = client.create_namespace()["namespace_id"]

    payload = client.redact_values(
        namespace_id,
        [
            {"id": "project", "label": "PROJECT_NAME", "value": "示例项目甲"},
            {
                "id": "document",
                "label": "DOCUMENT_NAME",
                "value": "合成协议.pdf",
            },
        ],
    )

    assert client.assert_egress_allowed(payload) is payload
    assert payload["items"] == [
        {"id": "project", "masked_value": "<PROJECT_NAME_001>"},
        {"id": "document", "masked_value": "<DOCUMENT_NAME_001>"},
    ]

    readiness = client.app_readiness()
    restored = client.refill_structured(
        payload["mapping_handle"],
        {
            "project": "<PROJECT_NAME_001>",
            "document": "<DOCUMENT_NAME_001>",
        },
    )
    assert readiness["contracts"]["structured_refill"] == "v1"
    assert restored["status"] == "ok"
    assert restored["value"] == {
        "project": "示例项目甲",
        "document": "合成协议.pdf",
    }


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"status": "ok", "blocked_labels": [], "egress_allowed": "true"},
        {"status": "ok", "blocked_labels": ["LEGAL_STRATEGY"], "egress_allowed": True},
        {"status": "error", "blocked_labels": [], "egress_allowed": True},
        {"status": "ok", "blocked_labels": "", "egress_allowed": True},
        {"status": "ok", "blocked_labels": ["sensitive raw"], "egress_allowed": True},
    ],
)
def test_client_assert_egress_fails_closed_on_invalid_or_denied_payload(
    tmp_path, payload
):
    client = _client(tmp_path)

    with pytest.raises(TuominEgressDenied) as raised:
        client.assert_egress_allowed(payload)

    assert raised.value.code == "egress_denied"
    assert "mapping_handle" not in str(raised.value)
    assert "sensitive raw" not in str(raised.value)
