from __future__ import annotations

import json

from fastapi.testclient import TestClient

from tuomin_gateway.service.app import create_app
from tuomin_gateway.service.registry import AppRegistry, hash_capability_token
from tuomin_gateway.store import MappingStore


TOKENS = {
    "detect": "structured-detect-token",
    "redact": "structured-redact-token",
    "trusted_refill": "structured-refill-token",
    "namespace": "structured-namespace-token",
}
OTHER_NAMESPACE_TOKEN = "other-namespace-token"
BAD_POLICY_NAMESPACE_TOKEN = "bad-policy-namespace-token"
BAD_CONFIG_NAMESPACE_TOKEN = "bad-config-namespace-token"


def _token_hashes(tokens):
    return {
        capability: hash_capability_token(token)
        for capability, token in tokens.items()
    }


def _entry(*, profile="kb", labels=None, tokens=TOKENS):
    return {
        "profile": profile,
        "capabilities": list(tokens),
        "capability_tokens": _token_hashes(tokens),
        "mapping_scopes": ["document", "namespace"],
        "refill_contracts": ["trusted_display", "exact_transform"],
        "structured_value_labels": labels
        if labels is not None
        else ["PROJECT_NAME", "DOCUMENT_NAME", "LEGAL_STRATEGY"],
    }


def _client(tmp_path):
    dictionary = tmp_path / "structured-dictionary.json"
    dictionary.write_text(
        json.dumps(
            [
                {
                    "canonical_value": "保留回购触发权",
                    "aliases": [],
                    "label": "LEGAL_STRATEGY",
                    "risk_level": "critical",
                    "status": "active",
                }
            ],
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    blocking_profile = {
        "base": "kb",
        "name": "structured-blocking-test",
        "action": {"LEGAL_STRATEGY": "block"},
    }
    structured = _entry(profile=blocking_profile)
    structured["dictionary"] = str(dictionary)
    other_tokens = {"namespace": OTHER_NAMESPACE_TOKEN}
    bad_policy_tokens = {"namespace": BAD_POLICY_NAMESPACE_TOKEN}
    bad_config_tokens = {"namespace": BAD_CONFIG_NAMESPACE_TOKEN}
    registry = AppRegistry(
        {
            "structured_app": structured,
            "other_app": _entry(
                labels=["PROJECT_NAME"], tokens=other_tokens
            ),
            "bad_policy_app": _entry(
                profile={
                    "base": "kb",
                    "name": "bad-structured-policy",
                    "action": {"PROJECT_NAME": "pass"},
                },
                labels=["PROJECT_NAME"],
                tokens=bad_policy_tokens,
            ),
            "bad_config_app": _entry(
                labels=["project-name"], tokens=bad_config_tokens
            ),
        }
    )
    return TestClient(
        create_app(registry=registry, store=MappingStore(tmp_path / "maps"))
    )


def _auth(token):
    return {"x-tuomin-capability-token": token}


def _create_namespace(client, app_id="structured_app", token=None):
    return client.post(
        "/api/v1/namespaces",
        headers=_auth(token or TOKENS["namespace"]),
        json={"app_id": app_id},
    )


def _redact_values(client, namespace_id, items, *, app_id="structured_app", token=None):
    return client.post(
        "/api/v1/redact/values",
        headers=_auth(token or TOKENS["namespace"]),
        json={"app_id": app_id, "namespace_id": namespace_id, "items": items},
    )


def _namespace_status(client, namespace_id):
    return client.get(
        f"/api/v1/namespaces/{namespace_id}",
        headers=_auth(TOKENS["namespace"]),
        params={"app_id": "structured_app"},
    ).json()


def test_redact_values_is_stable_exact_and_audited_without_raw_values(tmp_path):
    client = _client(tmp_path)
    namespace_id = _create_namespace(client).json()["namespace_id"]
    raw_project = " 示例项目甲 "
    raw_document = "合成供应商乙增资协议.pdf"

    first = _redact_values(
        client,
        namespace_id,
        [
            {"id": "project", "label": "PROJECT_NAME", "value": raw_project},
            {"id": "document", "label": "DOCUMENT_NAME", "value": raw_document},
        ],
    )
    second = _redact_values(
        client,
        namespace_id,
        [{"id": "again", "label": "PROJECT_NAME", "value": raw_project}],
    )

    assert first.status_code == 200
    payload = first.json()
    assert payload["status"] == "ok"
    assert payload["egress_allowed"] is True
    assert payload["blocked_labels"] == []
    assert payload["identity_contract"] == "declared_exact"
    assert payload["items"] == [
        {"id": "project", "masked_value": "<PROJECT_NAME_001>"},
        {"id": "document", "masked_value": "<DOCUMENT_NAME_001>"},
    ]
    assert second.json()["items"][0]["masked_value"] == "<PROJECT_NAME_001>"
    assert payload["mapping_handle"].startswith("mh_")
    assert raw_project not in json.dumps(payload, ensure_ascii=False)
    assert raw_document not in json.dumps(payload, ensure_ascii=False)
    assert _namespace_status(client, namespace_id)["placeholder_count"] == 2

    refill = client.post(
        "/api/v1/refill",
        headers=_auth(TOKENS["trusted_refill"]),
        json={
            "app_id": "structured_app",
            "mapping_handle": payload["mapping_handle"],
            "contract": "exact_transform",
            "text": "<PROJECT_NAME_001>|<DOCUMENT_NAME_001>",
        },
    ).json()
    assert refill["text"] == f"{raw_project}|{raw_document}"

    raw_audit = (tmp_path / "maps" / "audit" / "namespace.jsonl").read_text()
    assert raw_project not in raw_audit
    assert raw_document not in raw_audit
    events = [json.loads(line) for line in raw_audit.splitlines()]
    structured_events = [
        event for event in events if event["event"] == "structured_value_redact"
    ]
    assert structured_events[0]["label_counts"] == {
        "DOCUMENT_NAME": 1,
        "PROJECT_NAME": 1,
    }
    assert structured_events[0]["raw_values_included"] is False


def test_declared_project_name_is_remasked_across_one_pdf_soft_wrap(tmp_path):
    client = _client(tmp_path)
    namespace_id = _create_namespace(client).json()["namespace_id"]
    project_name = "苏州市高新区示例总部基地地块房地产开发项目投资变更"
    declared = _redact_values(
        client,
        namespace_id,
        [{"id": "project", "label": "PROJECT_NAME", "value": project_name}],
    ).json()
    wrapped = project_name.replace("房地产开发", "房地产\n开发")

    response = client.post(
        "/api/v1/redact/batch",
        headers=_auth(TOKENS["namespace"]),
        json={
            "app_id": "structured_app",
            "namespace_id": namespace_id,
            "items": [{"id": "block", "text": f"依据《关于{wrapped}的批复》"}],
        },
    )

    assert response.status_code == 200
    item = response.json()["items"][0]
    masked = item["masked_text"]
    assert masked == "依据《关于<PROJECT_NAME_001>的批复》"
    assert item["mapping_handle"].startswith("mh_")
    assert declared["items"][0]["masked_value"] == "<PROJECT_NAME_001>"


def test_redact_values_validation_is_atomic_and_errors_deny_egress(tmp_path):
    client = _client(tmp_path)
    namespace_id = _create_namespace(client).json()["namespace_id"]
    cases = [
        (
            [{"id": "x", "label": "PERSON", "value": "合成人名"}],
            403,
            "structured_value_label_denied",
        ),
        (
            [
                {"id": "x", "label": "PROJECT_NAME", "value": "示例甲"},
                {"id": "x", "label": "DOCUMENT_NAME", "value": "示例乙"},
            ],
            400,
            "invalid_value_item",
        ),
        (
            [{"id": "x", "label": "project-name", "value": "示例甲"}],
            400,
            "invalid_value_item",
        ),
        (
            [
                {
                    "id": "x",
                    "label": "PROJECT_NAME",
                    "value": "伪造 <ORG_001>",
                }
            ],
            409,
            "reserved_placeholder_conflict",
        ),
    ]

    for items, status_code, code in cases:
        response = _redact_values(client, namespace_id, items)
        assert response.status_code == status_code
        assert response.json()["error"]["code"] == code
        assert response.json()["egress_allowed"] is False

    assert _namespace_status(client, namespace_id)["placeholder_count"] == 0


def test_redact_values_policy_misconfiguration_is_safe_503(tmp_path):
    client = _client(tmp_path)
    cases = [
        ("bad_policy_app", BAD_POLICY_NAMESPACE_TOKEN),
        ("bad_config_app", BAD_CONFIG_NAMESPACE_TOKEN),
    ]

    for app_id, token in cases:
        namespace_id = _create_namespace(
            client, app_id=app_id, token=token
        ).json()["namespace_id"]
        response = _redact_values(
            client,
            namespace_id,
            [{"id": "x", "label": "PROJECT_NAME", "value": "示例项目"}],
            app_id=app_id,
            token=token,
        )
        assert response.status_code == 503
        payload = response.json()
        assert payload["status"] == "error"
        assert payload["egress_allowed"] is False
        assert payload["error"]["code"] == "structured_value_policy_unavailable"
        assert "示例项目" not in json.dumps(payload, ensure_ascii=False)


def test_redact_values_blocks_policy_labels_and_is_app_and_state_scoped(tmp_path):
    client = _client(tmp_path)
    namespace_id = _create_namespace(client).json()["namespace_id"]

    blocked = _redact_values(
        client,
        namespace_id,
        [
            {
                "id": "strategy",
                "label": "LEGAL_STRATEGY",
                "value": "保留回购触发权",
            }
        ],
    )
    cross_app = _redact_values(
        client,
        namespace_id,
        [{"id": "x", "label": "PROJECT_NAME", "value": "示例项目"}],
        app_id="other_app",
        token=OTHER_NAMESPACE_TOKEN,
    )
    client.post(
        f"/api/v1/namespaces/{namespace_id}/archive",
        headers=_auth(TOKENS["namespace"]),
        json={"app_id": "structured_app"},
    )
    archived = _redact_values(
        client,
        namespace_id,
        [{"id": "x", "label": "PROJECT_NAME", "value": "示例项目"}],
    )

    assert blocked.status_code == 200
    assert blocked.json()["items"][0]["masked_value"] == "<LEGAL_STRATEGY_001>"
    assert blocked.json()["blocked_labels"] == ["LEGAL_STRATEGY"]
    assert blocked.json()["egress_allowed"] is False
    assert cross_app.status_code == 404
    assert cross_app.json()["egress_allowed"] is False
    assert archived.status_code == 409
    assert archived.json()["egress_allowed"] is False


def test_redact_values_enforces_aggregate_and_batch_limits(tmp_path, monkeypatch):
    client = _client(tmp_path)
    namespace_id = _create_namespace(client).json()["namespace_id"]
    items = [
        {"id": "a", "label": "PROJECT_NAME", "value": "示例项目甲A"},
        {"id": "b", "label": "DOCUMENT_NAME", "value": "示例文档乙B"},
    ]

    monkeypatch.setenv("TUOMIN_MAX_TEXT_CHARS", "10")
    aggregate = _redact_values(client, namespace_id, items)
    monkeypatch.setenv("TUOMIN_MAX_TEXT_CHARS", "200000")
    monkeypatch.setenv("TUOMIN_MAX_BATCH_ITEMS", "1")
    batch = _redact_values(client, namespace_id, items)

    assert aggregate.status_code == 413
    assert aggregate.json()["error"]["code"] == "text_too_large"
    assert batch.status_code == 413
    assert batch.json()["error"]["code"] == "batch_too_large"
    assert _namespace_status(client, namespace_id)["placeholder_count"] == 0


def test_egress_decision_covers_all_v1_detect_and_redact_shapes(tmp_path):
    client = _client(tmp_path)
    safe_text = "电话13900001111"
    blocked_text = "保留回购触发权"

    detected_safe = client.post(
        "/api/v1/detect",
        headers=_auth(TOKENS["detect"]),
        json={"app_id": "structured_app", "text": safe_text},
    ).json()
    detected_blocked = client.post(
        "/api/v1/detect",
        headers=_auth(TOKENS["detect"]),
        json={"app_id": "structured_app", "text": blocked_text},
    ).json()
    redacted = client.post(
        "/api/v1/redact",
        headers=_auth(TOKENS["redact"]),
        json={"app_id": "structured_app", "text": blocked_text},
    ).json()
    documents = client.post(
        "/api/v1/redact/batch",
        headers=_auth(TOKENS["redact"]),
        json={
            "app_id": "structured_app",
            "items": [
                {"id": "safe", "text": safe_text},
                {"id": "blocked", "text": blocked_text},
            ],
        },
    ).json()
    namespace_id = _create_namespace(client).json()["namespace_id"]
    namespace_batch = client.post(
        "/api/v1/redact/batch",
        headers=_auth(TOKENS["namespace"]),
        json={
            "app_id": "structured_app",
            "namespace_id": namespace_id,
            "items": [{"id": "blocked", "text": blocked_text}],
        },
    ).json()
    namespace_one = client.post(
        f"/api/v1/namespaces/{namespace_id}/redact",
        headers=_auth(TOKENS["namespace"]),
        json={"app_id": "structured_app", "text": blocked_text},
    ).json()

    assert detected_safe["egress_allowed"] is True
    assert detected_blocked["egress_allowed"] is False
    assert redacted["egress_allowed"] is False
    assert documents["egress_allowed"] is False
    assert documents["items"][0]["egress_allowed"] is True
    assert documents["items"][1]["egress_allowed"] is False
    assert namespace_batch["egress_allowed"] is False
    assert namespace_one["egress_allowed"] is False
    assert namespace_one["blocked_labels"] == ["LEGAL_STRATEGY"]


def test_v1_error_envelopes_explicitly_deny_egress(tmp_path):
    client = _client(tmp_path)

    response = client.post(
        "/api/v1/redact",
        json={"app_id": "structured_app", "text": "电话13900001111"},
    )

    assert response.status_code == 403
    assert response.json()["status"] == "error"
    assert response.json()["egress_allowed"] is False


def test_v1_required_detector_error_explicitly_denies_egress(
    tmp_path, monkeypatch
):
    import tuomin_gateway.detectors.ner as ner_module

    def unavailable():
        raise ner_module.NerUnavailable("synthetic model unavailable")

    monkeypatch.setattr(ner_module, "get_ner_detector", unavailable)
    token = "strict-redact-token"
    registry = AppRegistry(
        {
            "strict_app": {
                "profile": "strict",
                "capabilities": ["redact"],
                "capability_tokens": {"redact": hash_capability_token(token)},
                "mapping_scopes": ["document"],
            }
        }
    )
    client = TestClient(
        create_app(registry=registry, store=MappingStore(tmp_path / "strict-maps"))
    )

    response = client.post(
        "/api/v1/redact",
        headers=_auth(token),
        json={"app_id": "strict_app", "text": "电话13900001111"},
    )

    assert response.status_code == 503
    payload = response.json()
    assert payload["status"] == "error"
    assert payload["egress_allowed"] is False
    assert payload["error"]["code"] == "required_detector_unavailable"
    assert "synthetic model unavailable" not in json.dumps(payload)
