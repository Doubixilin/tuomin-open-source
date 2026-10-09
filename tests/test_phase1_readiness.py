from __future__ import annotations

import json

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from tuomin_gateway.service.app import create_app  # noqa: E402
from tuomin_gateway.service.registry import AppRegistry  # noqa: E402
from tuomin_gateway.store import MappingStore  # noqa: E402


def _client(tmp_path, apps):
    return TestClient(
        create_app(
            registry=AppRegistry(apps),
            store=MappingStore(tmp_path / "maps"),
            session_ttl=3600,
        )
    )


def _break_ner(monkeypatch):
    import tuomin_gateway.detectors.ner as ner_module

    def unavailable():
        raise ner_module.NerUnavailable("synthetic model unavailable")

    monkeypatch.setattr(ner_module, "get_ner_detector", unavailable)


def test_required_ner_failure_is_503_and_never_silently_degrades(monkeypatch, tmp_path):
    _break_ner(monkeypatch)
    client = _client(tmp_path, {"strict_app": {"profile": "strict"}})

    response = client.post(
        "/redact",
        json={"app_id": "strict_app", "text": "联系电话13900001111"},
    )

    assert response.status_code == 503
    payload = response.json()
    assert payload["error"]["code"] == "required_detector_unavailable"
    assert payload["detectors"]["degraded"] is True
    assert payload["detectors"]["active"] == ["rule"]
    assert payload["detectors"]["required"] == ["ner", "rule"]
    assert payload["detectors"]["errors"][0]["detector"] == "ner"
    assert "synthetic model unavailable" not in json.dumps(payload)


def test_optional_ner_failure_reports_degraded_but_keeps_rule_protection(monkeypatch, tmp_path):
    _break_ner(monkeypatch)
    client = _client(tmp_path, {"agent_app": {"profile": "agent"}})

    response = client.post(
        "/redact",
        json={"app_id": "agent_app", "text": "联系电话13900001111"},
    )

    assert response.status_code == 200
    payload = response.json()
    assert "13900001111" not in payload["redacted_text"]
    assert payload["detectors"]["degraded"] is True
    assert payload["detectors"]["requested"] == ["ner", "rule"]
    assert payload["detectors"]["required"] == ["rule"]
    assert payload["detectors"]["active"] == ["rule"]


@pytest.mark.parametrize("content", ["{not-json", json.dumps({"not": "a list"})])
def test_declared_broken_dictionary_is_503(content, tmp_path):
    dictionary = tmp_path / "broken.json"
    dictionary.write_text(content, encoding="utf-8")
    client = _client(
        tmp_path,
        {"dictionary_app": {"profile": "kb", "dictionary": str(dictionary)}},
    )

    response = client.post(
        "/redact",
        json={"app_id": "dictionary_app", "text": "联系电话13900001111"},
    )

    assert response.status_code == 503
    payload = response.json()
    assert payload["error"]["code"] == "dictionary_unavailable"
    assert "broken.json" not in json.dumps(payload)


def test_declared_missing_dictionary_is_503(tmp_path):
    missing = tmp_path / "missing.json"
    client = _client(
        tmp_path,
        {"dictionary_app": {"profile": "kb", "dictionary": str(missing)}},
    )

    response = client.post(
        "/redact",
        json={"app_id": "dictionary_app", "text": "联系电话13900001111"},
    )

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "dictionary_unavailable"


def test_request_cannot_replace_registered_strict_profile_with_light(tmp_path):
    client = _client(tmp_path, {"strict_app": {"profile": "strict"}})

    response = client.post(
        "/redact",
        json={
            "app_id": "strict_app",
            "profile": "light",
            "text": "联系电话13900001111",
        },
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "profile_override_denied"


def test_request_cannot_disable_ner_required_by_registered_app(tmp_path):
    client = _client(tmp_path, {"strict_app": {"profile": "strict"}})

    response = client.post(
        "/redact",
        json={
            "app_id": "strict_app",
            "use_ner": False,
            "text": "联系电话13900001111",
        },
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "detector_downgrade_denied"


def test_readiness_distinguishes_liveness_from_required_detector_failure(
    monkeypatch, tmp_path
):
    _break_ner(monkeypatch)
    client = _client(tmp_path, {"strict_app": {"profile": "strict"}})

    assert client.get("/healthz").status_code == 200
    readiness = client.get("/readiness")

    assert readiness.status_code == 503
    payload = readiness.json()
    assert payload["status"] == "not_ready"
    assert isinstance(payload["detectors"]["ner"]["installed"], bool)
    assert payload["detectors"]["ner"]["loadable"] is False
    assert payload["detectors"]["ner"]["required"] is True


def test_readiness_reports_ready_only_after_required_ner_loads(monkeypatch, tmp_path):
    import tuomin_gateway.detectors.ner as ner_module
    import tuomin_gateway.session as session_module

    class FakeDetector:
        version = "synthetic-ner-v1"

        def _pipeline(self):
            return object()

    monkeypatch.setattr(session_module.importlib.util, "find_spec", lambda _name: object())
    monkeypatch.setattr(ner_module, "get_ner_detector", lambda: FakeDetector())
    client = _client(tmp_path, {"strict_app": {"profile": "strict"}})

    response = client.get("/readiness")

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "ready"
    assert payload["detectors"]["ner"]["model_cached"] is True
    assert payload["detectors"]["ner"]["active"] is True


def test_readiness_fails_when_declared_proxy_session_route_has_no_upstream(
    monkeypatch, tmp_path
):
    monkeypatch.delenv("TUOMIN_UPSTREAM_OPENAI", raising=False)
    client = _client(
        tmp_path,
        {
            "review_app": {
                "profile": {
                    "base": "agent",
                    "use_ner": False,
                    "ner_required": False,
                },
                "capabilities": ["proxy_session"],
                "proxy_routes": ["openai"],
            }
        },
    )

    response = client.get("/readiness")

    assert response.status_code == 503
    assert response.json()["apps"]["proxy_upstream_errors"] == [
        {
            "app_id": "review_app",
            "route": "openai",
            "code": "proxy_upstream_unavailable",
        }
    ]


def test_readiness_accepts_persisted_proxy_upstream(monkeypatch, tmp_path):
    # This test isolates proxy configuration; the global strict NER floor
    # is validated separately with explicit unavailable/loadable detectors.
    import tuomin_gateway.service.app as app_module
    monkeypatch.setattr(app_module, "probe_ner_runtime",
                        lambda *, required: {"required": required, "loadable": True})
    monkeypatch.delenv("TUOMIN_UPSTREAM_OPENAI", raising=False)
    client = _client(
        tmp_path,
        {
            "review_app": {
                "profile": {
                    "base": "agent",
                    "use_ner": False,
                    "ner_required": False,
                },
                "capabilities": ["proxy_session"],
                "proxy_routes": ["openai"],
                "proxy_upstreams": {
                    "openai": "https://provider.example/v1/chat/completions"
                },
            }
        },
    )

    response = client.get("/readiness")

    assert response.status_code == 200
    assert response.json()["apps"]["proxy_upstream_errors"] == []


def test_readiness_accepts_named_proxy_targets(monkeypatch, tmp_path):
    # This test isolates proxy configuration; the global strict NER floor
    # is validated separately with explicit unavailable/loadable detectors.
    import tuomin_gateway.service.app as app_module
    monkeypatch.setattr(app_module, "probe_ner_runtime",
                        lambda *, required: {"required": required, "loadable": True})
    monkeypatch.delenv("TUOMIN_UPSTREAM_OPENAI", raising=False)
    client = _client(
        tmp_path,
        {
            "review_app": {
                "profile": {
                    "base": "agent",
                    "use_ner": False,
                    "ner_required": False,
                },
                "capabilities": ["proxy_session"],
                "proxy_routes": ["openai"],
                "proxy_targets": {
                    "deepseek": {
                        "route": "openai",
                        "upstream": "https://provider.example/v1/chat/completions",
                    }
                },
            }
        },
    )

    response = client.get("/readiness")

    assert response.status_code == 200
    assert response.json()["apps"]["proxy_upstream_errors"] == []
