"""WorkBuddy onboarding wizard — admin API tests."""
from __future__ import annotations

import json

from fastapi.testclient import TestClient

from tuomin_gateway import workbuddy_onboarding as ob
from tuomin_gateway.gateway_discovery import Gateway, Resolution
from tuomin_gateway.service.app import create_app
from tuomin_gateway.service.registry import AppRegistry
from tuomin_gateway.store import MappingStore

GATEWAY_URL = "http://127.0.0.1:8775"


def _resolution():
    gateway = Gateway(base_url=GATEWAY_URL, port=8775, apps=["wb_test"], apps_known=True,
                      version="0.1.0", source="test")
    return Resolution(GATEWAY_URL, gateway, [gateway], True, "test")


def _setup(tmp_path, monkeypatch):
    monkeypatch.setenv("TUOMIN_WORKBUDDY_MODELS", str(tmp_path / "models.json"))
    monkeypatch.setattr(ob, "resolve_gateway", lambda *args, **kwargs: _resolution())
    cfg = tmp_path / "apps.json"
    cfg.write_text(json.dumps({"apps": {
        "wb_test": {"profile": {"base": "strict", "name": "wb_test", "refill_strict": False}},
    }}, ensure_ascii=False), encoding="utf-8")
    registry = AppRegistry.load(cfg)
    store = MappingStore(tmp_path / "maps")
    app = create_app(registry=registry, store=store, session_ttl=3600, admin_token="test-token")
    client = TestClient(app, base_url="http://localhost")
    client.headers.update({"x-tuomin-admin-token": "test-token"})
    return client, tmp_path, registry, store


def test_wizard_requires_admin_token(tmp_path, monkeypatch):
    client, _, _, _ = _setup(tmp_path, monkeypatch)
    anon = TestClient(client.app, base_url="http://localhost")
    assert anon.get("/admin/integrations/workbuddy").status_code == 401
    assert anon.post("/admin/integrations/workbuddy/apply", json={}).status_code == 401


def test_status_lists_apps_and_plan(tmp_path, monkeypatch):
    client, _, _, _ = _setup(tmp_path, monkeypatch)
    status = client.get("/admin/integrations/workbuddy").json()
    assert "wb_test" in status["apps"]

    plan = client.get("/admin/integrations/workbuddy", params={"app_id": "wb_test"}).json()["plan"]
    assert plan["gateway_url"] == GATEWAY_URL
    assert len(plan["planned_entries"]) == 3


def test_ensure_app_creates_workbuddy_template(tmp_path, monkeypatch):
    client, _, registry, _ = _setup(tmp_path, monkeypatch)
    resp = client.post("/admin/integrations/workbuddy/app",
                       json={"app_id": "new_proj", "upstream_model": "deepseek-v4-flash"})
    assert resp.status_code == 200 and resp.json()["created"] is True
    entry = registry.get_app("new_proj")
    assert entry["allow_auto_refill"] is True
    assert entry["upstream_model"] == "deepseek-v4-flash"
    assert entry["profile"]["refill_strict"] is False
    assert entry["proxy_upstreams"]["openai"].startswith("https://")

    again = client.post("/admin/integrations/workbuddy/app", json={"app_id": "new_proj"})
    assert again.json()["created"] is False  # idempotent


def test_invalid_app_id_rejected(tmp_path, monkeypatch):
    client, _, _, _ = _setup(tmp_path, monkeypatch)
    assert client.post("/admin/integrations/workbuddy/app", json={"app_id": "../etc"}).status_code == 400
    assert client.post("/admin/integrations/workbuddy/apply", json={"app_id": "a b", "api_key": "x"}).status_code == 400


def test_apply_writes_models_and_never_echoes_key(tmp_path, monkeypatch):
    client, out, _, store = _setup(tmp_path, monkeypatch)
    resp = client.post("/admin/integrations/workbuddy/apply",
                       json={"app_id": "wb_test", "api_key": "EXAMPLE_ONLY_NO_SECRET"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["written_entries"] == 3
    assert "EXAMPLE_ONLY_NO_SECRET" not in json.dumps(body, ensure_ascii=False)

    models = json.loads((out / "models.json").read_text(encoding="utf-8"))
    assert len([e for e in models if "/apps/wb_test/" in e["url"]]) == 3
    assert all(e["apiKey"] == "EXAMPLE_ONLY_NO_SECRET" for e in models)

    # audit records the event but must not contain the key
    audit_file = store.directory / "audit" / "admin.jsonl"
    assert audit_file.exists()
    audit_text = audit_file.read_text(encoding="utf-8")
    assert "workbuddy_apply" in audit_text
    assert "EXAMPLE_ONLY_NO_SECRET" not in audit_text


def test_apply_unknown_app_404_and_missing_key_400(tmp_path, monkeypatch):
    client, _, _, _ = _setup(tmp_path, monkeypatch)
    assert client.post("/admin/integrations/workbuddy/apply",
                       json={"app_id": "ghost", "api_key": "x"}).status_code == 404
    missing = client.post("/admin/integrations/workbuddy/apply",
                          json={"app_id": "wb_test", "api_key": ""})
    assert missing.status_code == 400
    assert missing.json()["error"]["code"] == "api_key_required"


def test_verify_endpoint(tmp_path, monkeypatch):
    client, _, _, _ = _setup(tmp_path, monkeypatch)
    client.post("/admin/integrations/workbuddy/apply", json={"app_id": "wb_test", "api_key": "sk-x"})
    result = client.post("/admin/integrations/workbuddy/verify", json={"app_id": "wb_test"}).json()
    assert result["ok"] is True
    assert result["gateway_serves_app"] is True
    assert {row["mode"] for row in result["entries"]} == {"masked", "demo", "auto"}
