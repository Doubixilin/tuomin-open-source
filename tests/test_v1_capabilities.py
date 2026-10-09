from __future__ import annotations

import json
import os
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient

from tuomin_gateway.service.app import create_app
from tuomin_gateway.service.registry import AppRegistry, hash_capability_token
from tuomin_gateway.store import MappingStore


REDACT_TOKEN = "synthetic-redact-token"
REFILL_TOKEN = "synthetic-refill-token"
OTHER_REFILL_TOKEN = "synthetic-other-refill-token"
NAMESPACE_TOKEN = "synthetic-namespace-token"


def _entry(redact=REDACT_TOKEN, refill=REFILL_TOKEN):
    return {
        "profile": "kb",
        "capabilities": ["detect", "redact", "trusted_refill", "namespace"],
        "capability_tokens": {
            "detect": hash_capability_token(redact),
            "redact": hash_capability_token(redact),
            "trusted_refill": hash_capability_token(refill),
            "namespace": hash_capability_token(NAMESPACE_TOKEN),
        },
        "mapping_scopes": ["document", "namespace"],
        "refill_contracts": ["none", "trusted_display", "exact_transform"],
    }


def _client(tmp_path):
    dictionary = tmp_path / "namespace-dictionary.json"
    dictionary.write_text(
        json.dumps(
            [
                {
                    "canonical_value": "示例建设单位A",
                    "aliases": ["示建A", "示例甲方"],
                    "label": "ORG",
                    "risk_level": "high",
                    "status": "active",
                },
                {
                    "canonical_value": "合成供应商乙",
                    "aliases": ["供应商乙方"],
                    "label": "SUPPLIER",
                    "risk_level": "high",
                    "status": "active",
                },
            ],
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    review = _entry()
    review["dictionary"] = str(dictionary)
    other = _entry("other-redact", OTHER_REFILL_TOKEN)
    other["dictionary"] = str(dictionary)
    registry = AppRegistry(
        {
            "review_app": review,
            "other_app": other,
        }
    )
    return TestClient(
        create_app(registry=registry, store=MappingStore(tmp_path / "maps"))
    )


def _auth(token):
    return {"x-tuomin-capability-token": token}


def _redact(client, text="电话13900001111，备用电话13900001111"):
    return client.post(
        "/api/v1/redact",
        headers=_auth(REDACT_TOKEN),
        json={"app_id": "review_app", "text": text},
    )


def test_v1_redact_requires_independent_capability_token(tmp_path):
    client = _client(tmp_path)

    missing = client.post(
        "/api/v1/redact", json={"app_id": "review_app", "text": "电话13900001111"}
    )
    wrong_capability = client.post(
        "/api/v1/redact",
        headers=_auth(REFILL_TOKEN),
        json={"app_id": "review_app", "text": "电话13900001111"},
    )

    assert missing.status_code == 403
    assert wrong_capability.status_code == 403
    assert missing.json()["error"]["code"] == "capability_denied"


def test_v1_redact_returns_opaque_handle_and_no_mapping(tmp_path):
    response = _redact(_client(tmp_path), "电话13900001111")

    assert response.status_code == 200
    payload = response.json()
    assert payload["mapping_handle"].startswith("mh_")
    assert len(payload["mapping_handle"]) > 40
    assert "13900001111" not in json.dumps(payload, ensure_ascii=False)
    assert "mapping" not in payload
    assert payload["profile"]["schema_version"] == "policy-dimensions-v1"


def test_v1_redact_rejects_reserved_placeholder_syntax(tmp_path):
    client = _client(tmp_path)
    response = _redact(
        client,
        "合同原文错误地包含 <ORG_001>，另有示建A。",
    )
    namespace_id = _create_namespace(client).json()["namespace_id"]
    namespace_response = _namespace_redact(
        client,
        namespace_id,
        "知识库原文错误地包含 <ORG_001>。",
    )

    assert response.status_code == 409
    payload = response.json()
    assert payload["error"]["code"] == "reserved_placeholder_conflict"
    assert "<ORG_001>" not in json.dumps(payload, ensure_ascii=False)
    assert namespace_response.status_code == 409
    assert namespace_response.json()["error"]["code"] == "reserved_placeholder_conflict"


def test_v1_document_alias_exact_refill_preserves_surface_forms(tmp_path):
    client = _client(tmp_path)
    redacted = _redact(client, "示建A与示例甲方确认").json()

    refill = client.post(
        "/api/v1/refill",
        headers=_auth(REFILL_TOKEN),
        json={
            "app_id": "review_app",
            "mapping_handle": redacted["mapping_handle"],
            "contract": "exact_transform",
            "text": redacted["masked_text"],
        },
    ).json()

    assert redacted["masked_text"] == "<ORG_001>与<ORG_002>确认"
    assert refill["status"] == "ok"
    assert refill["text"] == "示建A与示例甲方确认"


def test_v1_document_batch_returns_independent_handles(tmp_path):
    client = _client(tmp_path)
    response = client.post(
        "/api/v1/redact/batch",
        headers=_auth(REDACT_TOKEN),
        json={
            "app_id": "review_app",
            "items": [
                {"id": "clause-1", "text": "示建A确认"},
                {"id": "clause-2", "text": "供应商乙方确认"},
            ],
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["scope"] == "document"
    assert [item["id"] for item in payload["items"]] == ["clause-1", "clause-2"]
    assert payload["items"][0]["mapping_handle"] != payload["items"][1]["mapping_handle"]
    assert payload["items"][0]["masked_text"] == "<ORG_001>确认"
    assert payload["items"][1]["masked_text"] == "<SUPPLIER_001>确认"


def test_v1_namespace_batch_keeps_identity_stable_and_rejects_invalid_batch_atomically(tmp_path):
    client = _client(tmp_path)
    namespace_id = _create_namespace(client).json()["namespace_id"]

    invalid = client.post(
        "/api/v1/redact/batch",
        headers=_auth(NAMESPACE_TOKEN),
        json={
            "app_id": "review_app",
            "namespace_id": namespace_id,
            "items": [
                {"id": "doc-1", "text": "示建A确认"},
                {"id": "doc-2", "text": ""},
            ],
        },
    )
    status_after_invalid = client.get(
        f"/api/v1/namespaces/{namespace_id}",
        headers=_auth(NAMESPACE_TOKEN),
        params={"app_id": "review_app"},
    ).json()

    valid = client.post(
        "/api/v1/redact/batch",
        headers=_auth(NAMESPACE_TOKEN),
        json={
            "app_id": "review_app",
            "namespace_id": namespace_id,
            "items": [
                {"id": "doc-1", "text": "示建A确认"},
                {"id": "doc-2", "text": "示例甲方与供应商乙方确认"},
            ],
        },
    )

    assert invalid.status_code == 400
    assert invalid.json()["error"]["code"] == "invalid_batch_item"
    assert status_after_invalid["placeholder_count"] == 0
    assert valid.status_code == 200
    payload = valid.json()
    assert payload["scope"] == "namespace"
    assert payload["namespace_id"] == namespace_id
    assert payload["items"][0]["masked_text"] == "<ORG_001>确认"
    assert payload["items"][1]["masked_text"] == "<ORG_001>与<SUPPLIER_001>确认"


def test_trusted_display_allows_subset_but_blocks_unknown(tmp_path):
    client = _client(tmp_path)
    redacted = _redact(client).json()
    handle = redacted["mapping_handle"]

    subset = client.post(
        "/api/v1/refill",
        headers=_auth(REFILL_TOKEN),
        json={
            "app_id": "review_app",
            "mapping_handle": handle,
            "contract": "trusted_display",
            "text": "仅展示 <CONTACT_001>",
        },
    )
    unknown = client.post(
        "/api/v1/refill",
        headers=_auth(REFILL_TOKEN),
        json={
            "app_id": "review_app",
            "mapping_handle": handle,
            "contract": "trusted_display",
            "text": "编造 <PERSON_999>",
        },
    )

    assert subset.status_code == 200
    assert subset.json()["status"] == "ok"
    assert "13900001111" in subset.json()["text"]
    assert unknown.status_code == 200
    assert unknown.json()["status"] == "blocked"
    assert unknown.json()["text"] is None
    assert "unknown_placeholder" in unknown.json()["error_types"]


def test_exact_transform_enforces_placeholder_occurrence_counts(tmp_path):
    client = _client(tmp_path)
    redacted = _redact(client).json()

    response = client.post(
        "/api/v1/refill",
        headers=_auth(REFILL_TOKEN),
        json={
            "app_id": "review_app",
            "mapping_handle": redacted["mapping_handle"],
            "contract": "exact_transform",
            "text": "只保留一次 <CONTACT_001>",
        },
    )

    assert response.json()["status"] == "blocked"
    assert "placeholder_count_mismatch" in response.json()["error_types"]


def test_mapping_handle_is_app_bound_and_handle_is_not_authorization(tmp_path):
    client = _client(tmp_path)
    handle = _redact(client, "电话13900001111").json()["mapping_handle"]

    no_token = client.post(
        "/api/v1/refill",
        json={
            "app_id": "review_app",
            "mapping_handle": handle,
            "contract": "trusted_display",
            "text": "<CONTACT_001>",
        },
    )
    cross_app = client.post(
        "/api/v1/refill",
        headers=_auth(OTHER_REFILL_TOKEN),
        json={
            "app_id": "other_app",
            "mapping_handle": handle,
            "contract": "trusted_display",
            "text": "<CONTACT_001>",
        },
    )

    assert no_token.status_code == 403
    assert cross_app.status_code == 404


def test_refill_none_contract_is_denied_and_audit_contains_no_raw_value(tmp_path):
    client = _client(tmp_path)
    handle = _redact(client, "电话13900001111").json()["mapping_handle"]

    denied = client.post(
        "/api/v1/refill",
        headers=_auth(REFILL_TOKEN),
        json={
            "app_id": "review_app",
            "mapping_handle": handle,
            "contract": "none",
            "text": "<CONTACT_001>",
        },
    )
    allowed = client.post(
        "/api/v1/refill",
        headers=_auth(REFILL_TOKEN),
        json={
            "app_id": "review_app",
            "mapping_handle": handle,
            "contract": "trusted_display",
            "text": "<CONTACT_001>",
        },
    )

    assert denied.status_code == 403
    assert allowed.status_code == 200
    audit = (tmp_path / "maps" / "audit" / "trusted-refill.jsonl").read_text()
    assert "13900001111" not in audit
    assert handle not in audit
    assert '"raw_values_included": false' in audit


def _create_namespace(client, app_id="review_app"):
    return client.post(
        "/api/v1/namespaces",
        headers=_auth(NAMESPACE_TOKEN),
        json={"app_id": app_id},
    )


def _namespace_redact(client, namespace_id, text, app_id="review_app"):
    return client.post(
        f"/api/v1/namespaces/{namespace_id}/redact",
        headers=_auth(NAMESPACE_TOKEN),
        json={"app_id": app_id, "text": text},
    )


def test_namespace_persists_identity_and_resumes_counters_across_app_restart(tmp_path):
    first_client = _client(tmp_path)
    namespace_id = _create_namespace(first_client).json()["namespace_id"]

    first = _namespace_redact(first_client, namespace_id, "示建A确认").json()
    second_client = _client(tmp_path)  # reconstruct service/store over same directory
    second = _namespace_redact(
        second_client, namespace_id, "示例甲方与供应商乙方确认"
    ).json()

    assert "<ORG_001>" in first["masked_text"]
    assert "<ORG_001>" in second["masked_text"]  # alias -> same canonical identity
    assert "<SUPPLIER_001>" in second["masked_text"]
    status = second_client.get(
        f"/api/v1/namespaces/{namespace_id}",
        headers=_auth(NAMESPACE_TOKEN),
        params={"app_id": "review_app"},
    ).json()
    assert status["placeholder_count"] == 2
    assert status["identity_contract"] == "canonical"


def test_namespace_refill_explicitly_uses_canonical_identity(tmp_path):
    client = _client(tmp_path)
    namespace_id = _create_namespace(client).json()["namespace_id"]
    redacted = _namespace_redact(client, namespace_id, "示建A确认").json()

    refill = client.post(
        "/api/v1/refill",
        headers=_auth(REFILL_TOKEN),
        json={
            "app_id": "review_app",
            "mapping_handle": redacted["mapping_handle"],
            "contract": "trusted_display",
            "text": "主体为 <ORG_001>",
        },
    ).json()

    assert refill["text"] == "主体为 示例建设单位A"


def test_namespace_exact_transform_only_requires_current_grant_placeholders(tmp_path):
    client = _client(tmp_path)
    namespace_id = _create_namespace(client).json()["namespace_id"]
    _namespace_redact(client, namespace_id, "示建A确认")
    current = _namespace_redact(client, namespace_id, "供应商乙方确认").json()

    refill = client.post(
        "/api/v1/refill",
        headers=_auth(REFILL_TOKEN),
        json={
            "app_id": "review_app",
            "mapping_handle": current["mapping_handle"],
            "contract": "exact_transform",
            "text": "<SUPPLIER_001>确认",
        },
    ).json()

    assert refill["status"] == "ok"
    assert refill["text"] == "合成供应商乙确认"


def test_namespace_is_app_isolated_and_archive_blocks_new_writes(tmp_path):
    client = _client(tmp_path)
    namespace_id = _create_namespace(client).json()["namespace_id"]

    cross_app = _namespace_redact(
        client, namespace_id, "示建A确认", app_id="other_app"
    )
    archived = client.post(
        f"/api/v1/namespaces/{namespace_id}/archive",
        headers=_auth(NAMESPACE_TOKEN),
        json={"app_id": "review_app"},
    )
    after_archive = _namespace_redact(client, namespace_id, "示建A确认")

    assert cross_app.status_code == 404
    assert archived.status_code == 200
    assert archived.json()["namespace_status"] == "archived"
    assert after_archive.status_code == 409


def test_namespace_concurrent_writes_do_not_reuse_or_lose_placeholders(tmp_path):
    client = _client(tmp_path)
    namespace_id = _create_namespace(client).json()["namespace_id"]

    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(
            pool.map(
                lambda text: _namespace_redact(client, namespace_id, text),
                ["示建A确认", "供应商乙方确认"],
            )
        )

    assert all(response.status_code == 200 for response in responses)
    status = client.get(
        f"/api/v1/namespaces/{namespace_id}",
        headers=_auth(NAMESPACE_TOKEN),
        params={"app_id": "review_app"},
    ).json()
    assert status["placeholder_count"] == 2


def test_namespace_delete_revokes_derived_grants(tmp_path):
    """Orphan grants: a self-contained grant must not outlive its namespace."""
    client = _client(tmp_path)
    namespace_id = _create_namespace(client).json()["namespace_id"]
    redacted = _namespace_redact(client, namespace_id, "示建A确认").json()
    handle = redacted["mapping_handle"]

    def refill():
        return client.post(
            "/api/v1/refill",
            headers=_auth(REFILL_TOKEN),
            json={
                "app_id": "review_app",
                "mapping_handle": handle,
                "contract": "trusted_display",
                "text": "主体为 <ORG_001>",
            },
        )

    assert refill().status_code == 200  # grant works while the namespace lives

    deleted = client.delete(
        f"/api/v1/namespaces/{namespace_id}",
        headers=_auth(NAMESPACE_TOKEN),
        params={"app_id": "review_app"},
    )
    assert deleted.status_code == 200

    # The derived grant is revoked together with the namespace.
    assert refill().status_code == 404


def test_health_purges_abandoned_namespace_and_derived_grants(tmp_path):
    dictionary = tmp_path / "namespace-dictionary.json"
    dictionary.write_text(
        json.dumps(
            [
                {
                    "canonical_value": "示例建设单位A",
                    "aliases": ["示建A"],
                    "label": "ORG",
                    "risk_level": "high",
                    "status": "active",
                }
            ],
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    entry = _entry()
    entry["dictionary"] = str(dictionary)
    store = MappingStore(tmp_path / "maps", ttl_seconds=60)
    client = TestClient(
        create_app(registry=AppRegistry({"review_app": entry}), store=store)
    )
    namespace_id = _create_namespace(client).json()["namespace_id"]
    _namespace_redact(client, namespace_id, "示建A确认").raise_for_status()

    grant_files = list(store.directory.glob("*.mapping.enc"))
    namespace_files = list(
        (store.directory / "namespaces").glob("*.mapping.enc")
    )
    assert grant_files and namespace_files
    for path in [*grant_files, *namespace_files]:
        os.utime(path, (0, 0))

    assert client.get("/api/v1/health").status_code == 200
    assert list(store.directory.glob("*.mapping.enc")) == []
    assert list((store.directory / "namespaces").glob("*.mapping.enc")) == []


def test_service_rejects_nonpositive_mapping_ttl(tmp_path):
    for ttl_seconds in (0, -1):
        with pytest.raises(ValueError, match="mapping TTL must be greater than zero"):
            create_app(
                registry=AppRegistry({}),
                store=MappingStore(
                    tmp_path / f"maps-{ttl_seconds}",
                    ttl_seconds=ttl_seconds,
                ),
            )


def test_namespace_archive_keeps_derived_grants_usable(tmp_path):
    """Archive blocks new writes but keeps the mapping (and its grants)."""
    client = _client(tmp_path)
    namespace_id = _create_namespace(client).json()["namespace_id"]
    redacted = _namespace_redact(client, namespace_id, "示建A确认").json()

    archived = client.post(
        f"/api/v1/namespaces/{namespace_id}/archive",
        headers=_auth(NAMESPACE_TOKEN),
        json={"app_id": "review_app"},
    )
    assert archived.status_code == 200

    refill = client.post(
        "/api/v1/refill",
        headers=_auth(REFILL_TOKEN),
        json={
            "app_id": "review_app",
            "mapping_handle": redacted["mapping_handle"],
            "contract": "trusted_display",
            "text": "主体为 <ORG_001>",
        },
    )
    assert refill.status_code == 200
    assert refill.json()["text"] == "主体为 示例建设单位A"


def test_v1_redact_records_safe_call_ledger(tmp_path):
    client = _client(tmp_path)
    response = _redact(client)

    assert response.status_code == 200
    calls = client.app.state.job_store.list_calls(entry="api")
    assert len(calls) == 1
    call = calls[0]
    assert call["app_id"] == "review_app"
    assert call["char_count"] > 0
    assert call["blocked"] is False
    assert call["label_counts_json"]  # red line: counts only, never text
    assert "13900001111" not in json.dumps(call, ensure_ascii=False)
