"""Phase 4: admin CRUD API backing the WebUI."""
import json

from fastapi.testclient import TestClient

from tuomin_gateway.service.app import create_app
from tuomin_gateway.service.registry import AppRegistry
from tuomin_gateway.store import MappingStore


def _setup(tmp_path):
    dict_path = tmp_path / "demo_dict.json"
    dict_path.write_text(json.dumps(
        [{"entry_id": "e1", "canonical_value": "示例集团", "label": "ORG",
          "aliases": [], "risk_level": "high", "status": "active", "version": "t"}],
        ensure_ascii=False), encoding="utf-8")
    cfg = tmp_path / "apps.json"
    cfg.write_text(json.dumps(
        {"apps": {"demo": {"profile": "strict", "dictionary": str(dict_path)}}},
        ensure_ascii=False), encoding="utf-8")
    registry = AppRegistry.load(cfg)
    app = create_app(registry=registry, store=MappingStore(tmp_path / "maps"),
                     session_ttl=3600, admin_token="test-token")
    client = TestClient(app, base_url="http://localhost")
    client.headers.update({"x-tuomin-admin-token": "test-token"})
    return client, dict_path, cfg


def test_list_and_get_apps(tmp_path):
    client, _, _ = _setup(tmp_path)
    apps = client.get("/admin/apps").json()["apps"]
    assert any(a["app_id"] == "demo" and a["has_dictionary"] for a in apps)
    assert client.get("/admin/apps/demo").json()["entry"]["profile"] == "strict"
    assert client.get("/admin/apps/nope").status_code == 404


def test_dictionary_add_update_delete_persists(tmp_path):
    client, dict_path, _ = _setup(tmp_path)
    assert len(client.get("/admin/apps/demo/dictionary").json()["entries"]) == 1

    added = client.post("/admin/apps/demo/dictionary",
                        json={"canonical_value": "绿洲项目", "label": "PROJECT", "aliases": ["绿洲"]})
    assert added.status_code == 200
    eid = added.json()["entry"]["entry_id"]
    on_file = json.loads(dict_path.read_text(encoding="utf-8"))
    assert any(e["canonical_value"] == "绿洲项目" for e in on_file)  # persisted to disk

    upd = client.put(f"/admin/apps/demo/dictionary/{eid}",
                     json={"canonical_value": "绿洲项目", "label": "PROJECT", "risk_level": "medium"})
    assert upd.json()["entry"]["risk_level"] == "medium"

    assert client.delete(f"/admin/apps/demo/dictionary/{eid}").json()["deleted"] is True
    remaining = json.loads(dict_path.read_text(encoding="utf-8"))
    assert all(e["canonical_value"] != "绿洲项目" for e in remaining)


def test_add_entry_validation(tmp_path):
    client, _, _ = _setup(tmp_path)
    assert client.post("/admin/apps/demo/dictionary", json={"label": "ORG"}).status_code == 400
    polluted = client.post(
        "/admin/apps/demo/dictionary",
        json={"canonical_value": "实施本项目", "label": "PROJECT"},
    )
    assert polluted.status_code == 400
    assert polluted.json()["detail"] == "dictionary 质量校验失败: generic_value"
    assert client.delete("/admin/apps/demo/dictionary/missing").status_code == 404


def test_admin_rejects_alias_conflict_before_write(tmp_path):
    client, dict_path, _ = _setup(tmp_path)
    original = json.loads(dict_path.read_text(encoding="utf-8"))

    conflict = client.post(
        "/admin/apps/demo/dictionary",
        json={
            "canonical_value": "另一示例集团",
            "label": "ORG",
            "aliases": ["示例集团"],
        },
    )

    assert conflict.status_code == 400
    assert conflict.json()["detail"] == "dictionary 质量校验失败: identity_conflict"
    assert json.loads(dict_path.read_text(encoding="utf-8")) == original


def test_app_upsert_and_delete_persist_to_config(tmp_path):
    client, _, cfg = _setup(tmp_path)
    client.put("/admin/apps/newapp", json={"sensitivity": "light"})
    on_file = json.loads(cfg.read_text(encoding="utf-8"))
    assert on_file["apps"]["newapp"]["sensitivity"] == "light"

    assert client.delete("/admin/apps/newapp").json()["deleted"] is True
    assert "newapp" not in json.loads(cfg.read_text(encoding="utf-8"))["apps"]


def test_app_upsert_validates_structured_value_labels(tmp_path):
    client, _, cfg = _setup(tmp_path)

    accepted = client.put(
        "/admin/apps/structured",
        json={
            "profile": "kb",
            "structured_value_labels": ["PROJECT_NAME", "DOCUMENT_NAME"],
        },
    )
    rejected = client.put(
        "/admin/apps/bad-structured",
        json={"profile": "kb", "structured_value_labels": ["project-name"]},
    )

    assert accepted.status_code == 200
    assert rejected.status_code == 400
    saved = json.loads(cfg.read_text(encoding="utf-8"))
    assert saved["apps"]["structured"]["structured_value_labels"] == [
        "PROJECT_NAME",
        "DOCUMENT_NAME",
    ]
    assert "bad-structured" not in saved["apps"]


def test_settings_endpoint(tmp_path):
    client, _, _ = _setup(tmp_path)
    s = client.get("/admin/settings").json()
    assert "strict" in s["profiles"] and "light" in s["sensitivities"]
    assert s["session_ttl_seconds"] == 3600
    assert "demo" in s["apps"]


def test_admin_requires_token(tmp_path):
    client, _, _ = _setup(tmp_path)
    # Same client without the token header is rejected.
    assert client.get("/admin/apps", headers={"x-tuomin-admin-token": ""}).status_code == 401
    assert client.get("/admin/apps", headers={"x-tuomin-admin-token": "wrong"}).status_code == 401


def test_admin_rejects_foreign_host(tmp_path):
    client, _, _ = _setup(tmp_path)
    # DNS-rebinding style request with an attacker Host is refused even with token.
    r = client.get("/admin/apps", headers={"host": "evil.example.com"})
    assert r.status_code == 403
