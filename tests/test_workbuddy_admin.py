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
        "wb_test": {"profile": {"base": "strict", "name": "wb_test", "refill_strict": False},
                    "proxy_upstreams": {"openai": "https://api.example.test/v1/chat/completions"}},
        # The default file workbench app: no upstream, so the proxy answers 502 for chats.
        "workbench": {"profile": "file_workbench", "name": "本机工作台"},
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
    assert "wb_test" in status["apps"] and "workbench" in status["apps"]
    assert status["chat_apps"] == ["wb_test"]  # the file workbench app cannot be a WorkBuddy model

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
    assert result["workbuddy_loaded"] is None
    assert all(row["format_valid"] for row in result["entries"])


def test_verify_endpoint_reports_invalid_types_instead_of_success(tmp_path, monkeypatch):
    client, out, _, _ = _setup(tmp_path, monkeypatch)
    models = ob.build_entries("wb_test", GATEWAY_URL, "sk-private", ["auto"])
    models[0]["supportsToolCall"] = "True"
    (out / "models.json").write_text(json.dumps(models), encoding="utf-8")
    response = client.post("/admin/integrations/workbuddy/verify", json={"app_id": "wb_test"})
    assert response.status_code == 200
    result = response.json()
    assert not result["ok"] and result["invalid_entries"]
    assert result["stale_entries"] == []
    assert "sk-private" not in response.text


def test_apps_without_upstream_are_refused_with_guidance(tmp_path, monkeypatch):
    client, out, _, _ = _setup(tmp_path, monkeypatch)
    plan = client.get("/admin/integrations/workbuddy", params={"app_id": "workbench"})
    assert plan.status_code == 400 and plan.json()["error"]["code"] == "app_has_no_upstream"
    resp = client.post("/admin/integrations/workbuddy/apply", json={"app_id": "workbench", "api_key": "sk-x"})
    assert resp.status_code == 400
    error = resp.json()["error"]
    assert error["code"] == "app_has_no_upstream" and "一键创建" in error["message"] and "502" in error["message"]
    assert not (out / "models.json").exists()  # nothing was written for an app that cannot work
    verify = client.post("/admin/integrations/workbuddy/verify", json={"app_id": "workbench"}).json()
    assert verify["ok"] is False and verify["upstream_configured"] is False


def test_apply_writes_json_booleans_workbuddy_accepts(tmp_path, monkeypatch):
    client, out, _, _ = _setup(tmp_path, monkeypatch)
    legacy = [{"id": "wb_test·脱敏·全mask", "name": "wb_test·脱敏·全mask", "vendor": "Custom",
               "url": "http://127.0.0.1:8767/apps/wb_test/v1/chat/completions", "apiKey": "sk-old",
               "supportsToolCall": "True", "supportsImages": "False", "supportsReasoning": "True",
               "useCustomProtocol": "False", "reasoning": "{'defaultEffort': 'high'}"}]
    (out / "models.json").write_text(json.dumps(legacy, ensure_ascii=False), encoding="utf-8")
    before = client.post("/admin/integrations/workbuddy/verify", json={"app_id": "wb_test"}).json()
    assert before["ok"] is False and len(before["invalid_entries"]) == 1
    assert client.post("/admin/integrations/workbuddy/apply", json={"app_id": "wb_test", "api_key": "sk-new"}).status_code == 200
    models = json.loads((out / "models.json").read_text(encoding="utf-8"))
    assert all(isinstance(e["supportsToolCall"], bool) and isinstance(e["reasoning"], dict) for e in models)
    after = client.post("/admin/integrations/workbuddy/verify", json={"app_id": "wb_test"}).json()
    assert after["ok"] is True and after["invalid_entries"] == [] and after["upstream_configured"] is True


def test_apply_prunes_entries_of_apps_without_upstream(tmp_path, monkeypatch):
    client, out, _, _ = _setup(tmp_path, monkeypatch)
    stale = [{"id": f"workbench·脱敏·{m}", "name": f"workbench·脱敏·{m}", "vendor": "Custom",
              "url": f"http://127.0.0.1:8765/apps/workbench/{p}v1/chat/completions", "apiKey": "sk-old",
              "supportsToolCall": "True", "supportsImages": "False", "supportsReasoning": "True",
              "useCustomProtocol": "False", "reasoning": "{'defaultEffort': 'high'}"}
             for m, p in (("全mask", ""), ("思维链mask", "demo/"), ("无感回填", "auto/"))]
    other = {"id": "glm", "name": "GLM", "vendor": "X", "url": "https://example.test/v4", "apiKey": "k", "supportsToolCall": True}
    (out / "models.json").write_text(json.dumps(stale + [other], ensure_ascii=False), encoding="utf-8")
    resp = client.post("/admin/integrations/workbuddy/apply", json={"app_id": "wb_test", "api_key": "sk-new"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["pruned_entries"] == 3
    models = json.loads((out / "models.json").read_text(encoding="utf-8"))
    assert not any("/apps/workbench/" in e["url"] for e in models)
    assert any(e["id"] == "glm" for e in models) and len([e for e in models if "/apps/wb_test/" in e["url"]]) == 3
    assert any(p.name.startswith("models.json.bak-") for p in out.iterdir())


def test_wizard_targets_the_answering_gateway_not_port_discovery(tmp_path, monkeypatch):
    client, _, _, _ = _setup(tmp_path, monkeypatch)
    seen = []
    monkeypatch.setattr(ob, "resolve_gateway", lambda *args, **kwargs: seen.append(kwargs.get("base_url")) or _resolution())
    client.get("/admin/integrations/workbuddy", params={"app_id": "wb_test"})
    client.post("/admin/integrations/workbuddy/verify", json={"app_id": "wb_test"})
    client.post("/admin/integrations/workbuddy/verify", json={"app_id": "wb_test", "base_url": "http://127.0.0.1:9999"})
    assert seen == ["http://localhost", "http://localhost", "http://127.0.0.1:9999"]
