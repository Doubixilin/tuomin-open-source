"""Tests for the WorkBuddy onboarding core (tuomin_gateway.workbuddy_onboarding)."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from tuomin_gateway import workbuddy_onboarding as ob
from tuomin_gateway.gateway_discovery import Gateway, Resolution

GATEWAY_URL = "http://127.0.0.1:8775"


def _resolution(base_url=GATEWAY_URL, verified=True, reason="test"):
    gateway = Gateway(base_url=base_url, port=8775, apps=["wb_test"], apps_known=True,
                      version="0.1.0", source="test") if base_url else None
    gateways = [gateway] if gateway else []
    return Resolution(base_url, gateway, gateways, verified, reason)


@pytest.fixture
def fake_gateway(monkeypatch):
    monkeypatch.setattr(ob, "resolve_gateway", lambda *args, **kwargs: _resolution())


def _existing_models(tmp_path: Path) -> Path:
    path = tmp_path / "models.json"
    path.write_text(json.dumps([
        {"id": "脱敏-无感", "name": "脱敏-无感",
         "url": "http://127.0.0.1:8767/apps/wb_test/auto/v1/chat/completions",
         "apiKey": "sk-old"},
        {"id": "glm", "name": "GLM", "url": "https://open.bigmodel.cn/api/coding/paas/v4",
         "apiKey": "sk-glm"},
    ], ensure_ascii=False), encoding="utf-8")
    return path


def test_plan_reports_gateway_and_planned_entries(tmp_path, fake_gateway):
    plan = ob.plan("wb_test", models_file=tmp_path / "models.json")
    assert plan["gateway_url"] == GATEWAY_URL
    assert plan["verified"] is True
    assert len(plan["planned_entries"]) == 3
    assert all(e["url"].startswith(GATEWAY_URL + "/apps/wb_test/") for e in plan["planned_entries"])
    assert all(e["apiKey"] == ob.PLACEHOLDER_API_KEY for e in plan["planned_entries"])
    assert plan["models_exists"] is False


def test_apply_heals_stale_port_and_preserves_other_apps(tmp_path, fake_gateway):
    models = _existing_models(tmp_path)
    result = ob.apply("wb_test", api_key="sk-real", models_file=models)

    assert result["gateway_url"] == GATEWAY_URL
    assert result["written_entries"] == 3
    assert result["retargeted_entries"] == 1  # the 8767 entry was healed
    assert result["backup"] and Path(result["backup"]).exists()

    data = json.loads(models.read_text(encoding="utf-8"))
    wb = [e for e in data if "/apps/wb_test/" in e["url"]]
    assert len(wb) == 3  # no duplicates
    assert all(e["url"].startswith(GATEWAY_URL) for e in wb)
    assert all(e["apiKey"] == "sk-real" for e in wb)
    glm = [e for e in data if e["id"] == "glm"]
    assert glm and glm[0]["url"].startswith("https://")  # untouched


def test_apply_never_returns_the_api_key(tmp_path, fake_gateway):
    result = ob.apply("wb_test", api_key="EXAMPLE_ONLY_NO_SECRET", models_file=tmp_path / "models.json")
    assert "EXAMPLE_ONLY_NO_SECRET" not in json.dumps(result, ensure_ascii=False)


def test_apply_requires_api_key(tmp_path, fake_gateway):
    with pytest.raises(ob.OnboardingError) as excinfo:
        ob.apply("wb_test", api_key="  ", models_file=tmp_path / "models.json")
    assert excinfo.value.code == "api_key_required"


def test_apply_fails_loudly_when_no_gateway_serves_app(tmp_path, monkeypatch):
    monkeypatch.setattr(ob, "resolve_gateway", lambda *a, **k: _resolution(
        base_url=None, verified=False, reason="no live gateway serves app 'wb_test'"))
    with pytest.raises(ob.OnboardingError) as excinfo:
        ob.apply("wb_test", api_key="sk-x", models_file=tmp_path / "models.json")
    assert excinfo.value.code == "no_gateway_serves_app"


def test_apply_rejects_unknown_mode(tmp_path, fake_gateway):
    with pytest.raises(ob.OnboardingError) as excinfo:
        ob.apply("wb_test", api_key="sk-x", modes=["bogus"], models_file=tmp_path / "models.json")
    assert excinfo.value.code == "unknown_mode"


def test_verify_ok_and_detects_stale(tmp_path, fake_gateway):
    models = _existing_models(tmp_path)
    stale = ob.verify("wb_test", models_file=models)
    assert stale["ok"] is False
    assert stale["gateway_serves_app"] is True
    assert stale["stale_entries"] and stale["stale_entries"][0]["points_at_gateway"] is False

    ob.apply("wb_test", api_key="sk-real", models_file=models)
    healthy = ob.verify("wb_test", models_file=models)
    assert healthy["ok"] is True
    assert healthy["stale_entries"] == []
    assert {row["mode"] for row in healthy["entries"]} == {"masked", "demo", "auto"}
