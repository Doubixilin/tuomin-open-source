from __future__ import annotations

import hashlib
import json

from fastapi.testclient import TestClient

from tuomin_gateway.service.app import create_app
from tuomin_gateway.service.registry import AppRegistry, hash_capability_token
from tuomin_gateway.store import MappingStore


TOKENS = {
    "detect": "t2-detect-token",
    "redact": "t2-redact-token",
    "trusted_refill": "t2-refill-token",
    "namespace": "t2-namespace-token",
}
OTHER_TOKENS = {
    "trusted_refill": "t2-other-refill-token",
    "namespace": "t2-other-namespace-token",
}


def _hashes(tokens):
    return {
        capability: hash_capability_token(token)
        for capability, token in tokens.items()
    }


def _entry(dictionary, *, tokens=TOKENS, profile="kb", labels=None):
    return {
        "profile": profile,
        "dictionary": str(dictionary),
        "capabilities": list(tokens),
        "capability_tokens": _hashes(tokens),
        "mapping_scopes": ["document", "namespace"],
        "refill_contracts": ["trusted_display", "exact_transform"],
        "structured_value_labels": labels
        or ["PROJECT_NAME", "DOCUMENT_NAME"],
    }


def _dictionary_payload(canonical="示例甲公司"):
    return [
        {
            "canonical_value": canonical,
            "aliases": [],
            "label": "ORG",
            "risk_level": "high",
            "status": "active",
        }
    ]


def _fixture(tmp_path):
    dictionary = tmp_path / "t2-dictionary.json"
    dictionary.write_text(
        json.dumps(_dictionary_payload(), ensure_ascii=False), encoding="utf-8"
    )
    registry = AppRegistry(
        {
            "t2_app": _entry(dictionary),
            "other_app": _entry(dictionary, tokens=OTHER_TOKENS),
        }
    )
    client = TestClient(
        create_app(registry=registry, store=MappingStore(tmp_path / "maps"))
    )
    return client, dictionary


def _auth(token):
    return {"x-tuomin-capability-token": token}


def _create_namespace(client):
    return client.post(
        "/api/v1/namespaces",
        headers=_auth(TOKENS["namespace"]),
        json={"app_id": "t2_app"},
    ).json()["namespace_id"]


def _redact_values(client, namespace_id):
    return client.post(
        "/api/v1/redact/values",
        headers=_auth(TOKENS["namespace"]),
        json={
            "app_id": "t2_app",
            "namespace_id": namespace_id,
            "items": [
                {
                    "id": "project",
                    "label": "PROJECT_NAME",
                    "value": "合成项目甲",
                },
                {
                    "id": "document",
                    "label": "DOCUMENT_NAME",
                    "value": "合成协议乙.pdf",
                },
            ],
        },
    ).json()


def _structured_refill(client, handle, value, contract="trusted_display"):
    return client.post(
        "/api/v1/refill/structured",
        headers=_auth(TOKENS["trusted_refill"]),
        json={
            "app_id": "t2_app",
            "mapping_handle": handle,
            "contract": contract,
            "value": value,
        },
    )


def test_structured_refill_restores_nested_json_atomically_and_audits_safely(
    tmp_path,
):
    client, _dictionary = _fixture(tmp_path)
    namespace_id = _create_namespace(client)
    redacted = _redact_values(client, namespace_id)
    value = {
        "project": "<PROJECT_NAME_001>",
        "sources": [
            {
                "name": "<DOCUMENT_NAME_001>",
                "page": 3,
                "confirmed": True,
            }
        ],
        "note": None,
    }

    response = _structured_refill(
        client, redacted["mapping_handle"], value
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "ok"
    assert payload["value"] == {
        "project": "合成项目甲",
        "sources": [
            {"name": "合成协议乙.pdf", "page": 3, "confirmed": True}
        ],
        "note": None,
    }
    assert payload["audit"] == {
        "event": "trusted_refill",
        "status": "ok",
        "restored_count": 2,
        "raw_values_included": False,
    }
    response_text = json.dumps(payload, ensure_ascii=False)
    assert redacted["mapping_handle"] not in response_text

    audit_text = (
        tmp_path / "maps" / "audit" / "trusted-refill.jsonl"
    ).read_text()
    assert "合成项目甲" not in audit_text
    assert "合成协议乙.pdf" not in audit_text


def test_structured_refill_audit_failure_does_not_break_local_data_path(
    tmp_path, monkeypatch
):
    client, _dictionary = _fixture(tmp_path)
    redacted = _redact_values(client, _create_namespace(client))

    def audit_unavailable(*_args, **_kwargs):
        raise OSError("synthetic audit storage unavailable")

    monkeypatch.setattr(
        "tuomin_gateway.audit.AuditLog.write", audit_unavailable
    )
    response = _structured_refill(
        client,
        redacted["mapping_handle"],
        {"project": "<PROJECT_NAME_001>"},
    )

    assert response.status_code == 200
    assert response.json()["status"] == "ok"
    assert response.json()["value"] == {"project": "合成项目甲"}


def test_structured_refill_blocks_unknown_altered_and_exact_count_mismatch(
    tmp_path,
):
    client, _dictionary = _fixture(tmp_path)
    redacted = _redact_values(client, _create_namespace(client))
    handle = redacted["mapping_handle"]
    cases = [
        (
            {"project": "<PROJECT_NAME_001>", "bad": "<PERSON_999>"},
            "trusted_display",
            "unknown_placeholder",
        ),
        (
            {"project": "<PROJECT_NAME-001>"},
            "trusted_display",
            "altered_placeholder",
        ),
        (
            {"project": "<PROJECT_NAME_001>"},
            "exact_transform",
            "missing_placeholder",
        ),
        (
            {
                "project": "<PROJECT_NAME_001><PROJECT_NAME_001>",
                "document": "<DOCUMENT_NAME_001>",
            },
            "exact_transform",
            "placeholder_count_mismatch",
        ),
    ]

    for value, contract, expected_error in cases:
        response = _structured_refill(client, handle, value, contract)
        assert response.status_code == 200
        payload = response.json()
        assert payload["status"] == "blocked"
        assert payload["value"] is None
        assert payload["audit"]["restored_count"] == 0
        assert expected_error in payload["error_types"]
        assert "合成项目甲" not in json.dumps(payload, ensure_ascii=False)
        assert "合成协议乙.pdf" not in json.dumps(payload, ensure_ascii=False)


def test_structured_refill_enforces_depth_node_and_string_limits(
    tmp_path, monkeypatch
):
    client, _dictionary = _fixture(tmp_path)
    redacted = _redact_values(client, _create_namespace(client))
    handle = redacted["mapping_handle"]
    depth_value = "leaf"
    for _ in range(32):
        depth_value = [depth_value]

    depth = _structured_refill(client, handle, depth_value)
    nodes = _structured_refill(client, handle, [None] * 10_000)
    monkeypatch.setenv("TUOMIN_MAX_TEXT_CHARS", "3")
    characters = _structured_refill(client, handle, {"text": "four"})

    for response in (depth, nodes, characters):
        assert response.status_code == 413
        payload = response.json()
        assert payload["error"]["code"] == "structure_too_large"
        assert payload["egress_allowed"] is False
        assert "leaf" not in json.dumps(payload)


def test_structured_refill_is_capability_contract_and_app_bound(tmp_path):
    client, _dictionary = _fixture(tmp_path)
    redacted = _redact_values(client, _create_namespace(client))
    request = {
        "app_id": "t2_app",
        "mapping_handle": redacted["mapping_handle"],
        "contract": "trusted_display",
        "value": {"project": "<PROJECT_NAME_001>"},
    }

    missing = client.post("/api/v1/refill/structured", json=request)
    denied = client.post(
        "/api/v1/refill/structured",
        headers=_auth(TOKENS["trusted_refill"]),
        json={**request, "contract": "none"},
    )
    cross_app = client.post(
        "/api/v1/refill/structured",
        headers=_auth(OTHER_TOKENS["trusted_refill"]),
        json={**request, "app_id": "other_app"},
    )
    non_json_number = client.post(
        "/api/v1/refill/structured",
        headers={
            **_auth(TOKENS["trusted_refill"]),
            "content-type": "application/json",
        },
        content=json.dumps(
            {**request, "value": {"invalid": float("nan")}},
            allow_nan=True,
        ),
    )

    assert missing.status_code == 403
    assert missing.json()["error"]["code"] == "capability_denied"
    assert denied.status_code == 403
    assert denied.json()["error"]["code"] == "refill_contract_denied"
    assert cross_app.status_code == 404
    assert cross_app.json()["error"]["code"] == "mapping_handle_not_found"
    assert non_json_number.status_code == 400
    assert non_json_number.json()["error"]["code"] == "invalid_request"


def test_dictionary_hot_refresh_updates_version_and_damage_fails_closed(tmp_path):
    client, dictionary = _fixture(tmp_path)

    first = client.post(
        "/api/v1/redact",
        headers=_auth(TOKENS["redact"]),
        json={"app_id": "t2_app", "text": "示例甲公司"},
    ).json()
    replacement = dictionary.with_suffix(".replacement")
    replacement.write_text(
        json.dumps(_dictionary_payload("示例乙公司"), ensure_ascii=False),
        encoding="utf-8",
    )
    replacement.replace(dictionary)
    second = client.post(
        "/api/v1/redact",
        headers=_auth(TOKENS["redact"]),
        json={"app_id": "t2_app", "text": "示例乙公司"},
    ).json()

    assert first["masked_text"] == "<ORG_001>"
    assert second["masked_text"] == "<ORG_001>"
    first_version = first["protection_receipt"]["dictionary_version"]
    second_version = second["protection_receipt"]["dictionary_version"]
    assert first_version.startswith("sha256:")
    assert second_version.startswith("sha256:")
    assert first_version != second_version
    assert second["protection_receipt"]["detector_versions"]["dictionary"] == (
        second_version
    )

    damaged = dictionary.with_suffix(".damaged")
    damaged.write_text("{damaged", encoding="utf-8")
    damaged.replace(dictionary)
    readiness = client.get(
        "/api/v1/readiness",
        headers=_auth(TOKENS["namespace"]),
        params={"app_id": "t2_app"},
    )
    processing = client.post(
        "/api/v1/redact",
        headers=_auth(TOKENS["redact"]),
        json={"app_id": "t2_app", "text": "示例乙公司"},
    )

    assert readiness.status_code == 503
    assert readiness.json()["status"] == "not_ready"
    assert readiness.json()["error"]["code"] == "dictionary_unavailable"
    assert processing.status_code == 503
    assert processing.json()["egress_allowed"] is False
    assert processing.json()["error"]["code"] == "dictionary_unavailable"
    assert "示例乙公司" not in processing.text


def test_app_readiness_is_authorized_safe_and_advertises_only_real_contracts(
    tmp_path,
):
    client, dictionary = _fixture(tmp_path)

    missing = client.get(
        "/api/v1/readiness", params={"app_id": "t2_app"}
    )
    response = client.get(
        "/api/v1/readiness",
        headers=_auth(TOKENS["namespace"]),
        params={"app_id": "t2_app"},
    )

    assert missing.status_code == 403
    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "ready"
    assert payload["contracts"] == {
        "structured_values": "v1",
        "egress_decision": "v1",
        "structured_refill": "v1",
        "protection_receipt": "v1",
    }
    assert payload["app"]["app_id"] == "t2_app"
    assert payload["app"]["profile_name"] == "kb"
    assert payload["app"]["profile_version"].startswith("sha256:")
    assert payload["app"]["dictionary_version"].startswith("sha256:")
    assert payload["app"]["structured_value_labels"] == [
        "DOCUMENT_NAME",
        "PROJECT_NAME",
    ]
    safe_text = json.dumps(payload, ensure_ascii=False)
    assert str(dictionary) not in safe_text
    assert TOKENS["namespace"] not in safe_text


def test_required_ner_failure_marks_app_not_ready(tmp_path, monkeypatch):
    client, dictionary = _fixture(tmp_path)
    strict_token = "strict-namespace-token"
    registry = AppRegistry(
        {
            "strict_app": _entry(
                dictionary,
                tokens={"namespace": strict_token},
                profile="strict",
            )
        }
    )
    strict_client = TestClient(
        create_app(registry=registry, store=MappingStore(tmp_path / "strict-maps"))
    )
    monkeypatch.setattr(
        "tuomin_gateway.service.v1.probe_ner_runtime",
        lambda required: {
            "requested": True,
            "required": required,
            "loadable": False,
            "active": False,
            "error_type": "SyntheticUnavailable",
        },
    )

    response = strict_client.get(
        "/api/v1/readiness",
        headers=_auth(strict_token),
        params={"app_id": "strict_app"},
    )

    assert response.status_code == 503
    assert response.json()["status"] == "not_ready"
    assert response.json()["error"]["code"] == "required_detector_unavailable"


def test_app_readiness_rejects_invalid_or_profile_conflicting_value_policy(
    tmp_path,
):
    dictionary = tmp_path / "readiness-policy-dictionary.json"
    dictionary.write_text(
        json.dumps(_dictionary_payload(), ensure_ascii=False), encoding="utf-8"
    )
    bad_label_token = "bad-label-readiness-token"
    bad_policy_token = "bad-policy-readiness-token"
    registry = AppRegistry(
        {
            "bad_label": _entry(
                dictionary,
                tokens={"namespace": bad_label_token},
                labels=["project-name"],
            ),
            "bad_policy": _entry(
                dictionary,
                tokens={"namespace": bad_policy_token},
                profile={
                    "base": "kb",
                    "name": "bad-readiness-policy",
                    "action": {"PROJECT_NAME": "pass"},
                },
                labels=["PROJECT_NAME"],
            ),
        }
    )
    client = TestClient(
        create_app(registry=registry, store=MappingStore(tmp_path / "bad-maps"))
    )

    for app_id, token in (
        ("bad_label", bad_label_token),
        ("bad_policy", bad_policy_token),
    ):
        response = client.get(
            "/api/v1/readiness",
            headers=_auth(token),
            params={"app_id": app_id},
        )
        assert response.status_code == 503
        assert response.json()["status"] == "not_ready"
        assert response.json()["error"]["code"] == (
            "structured_value_policy_unavailable"
        )


def test_all_redact_shapes_include_safe_protection_receipts(tmp_path):
    client, _dictionary = _fixture(tmp_path)
    namespace_id = _create_namespace(client)
    responses = [
        client.post(
            "/api/v1/redact",
            headers=_auth(TOKENS["redact"]),
            json={"app_id": "t2_app", "text": "示例甲公司"},
        ).json(),
        client.post(
            "/api/v1/redact/batch",
            headers=_auth(TOKENS["redact"]),
            json={
                "app_id": "t2_app",
                "items": [{"id": "one", "text": "示例甲公司"}],
            },
        ).json(),
        client.post(
            "/api/v1/redact/batch",
            headers=_auth(TOKENS["namespace"]),
            json={
                "app_id": "t2_app",
                "namespace_id": namespace_id,
                "items": [{"id": "one", "text": "示例甲公司"}],
            },
        ).json(),
        client.post(
            f"/api/v1/namespaces/{namespace_id}/redact",
            headers=_auth(TOKENS["namespace"]),
            json={"app_id": "t2_app", "text": "示例甲公司"},
        ).json(),
        _redact_values(client, namespace_id),
    ]

    assert [payload["protection_receipt"]["scope"] for payload in responses] == [
        "document",
        "document",
        "namespace",
        "namespace",
        "namespace",
    ]
    for payload in responses:
        receipt = payload["protection_receipt"]
        assert receipt["schema_version"] == "tuomin-protection-receipt-v1"
        assert receipt["app_id"] == "t2_app"
        assert receipt["profile_name"] == "kb"
        assert receipt["profile_version"].startswith("sha256:")
        assert receipt["dictionary_version"].startswith("sha256:")
        assert "mapping" not in receipt
        assert TOKENS["redact"] not in json.dumps(receipt)


def test_protection_receipt_normalizes_real_ner_version_identifier():
    from tuomin_gateway.detectors.ner import MODEL_NAME, MODEL_REVISION
    from tuomin_gateway.service.v1 import _detector_versions
    from tuomin_gateway.session import DetectorReadiness, DetectorState

    raw_version = f"ner-cluener:{MODEL_NAME}@{MODEL_REVISION}"
    receipt_versions = _detector_versions(
        DetectorReadiness(
            states=(
                DetectorState(
                    name="ner",
                    required=True,
                    active=True,
                    version=raw_version,
                ),
            )
        )
    )

    assert "/" in raw_version
    assert "@" in raw_version
    assert receipt_versions == {
        "ner": "sha256:" + hashlib.sha256(raw_version.encode("utf-8")).hexdigest()
    }
