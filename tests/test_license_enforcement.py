"""离线授权的服务层执行：中间件拦截、恢复路径放行、激活端点、原生后手。"""

from __future__ import annotations

import base64
import json
from datetime import datetime, timedelta, timezone

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from fastapi.testclient import TestClient

from tuomin_gateway import licensing
from tuomin_gateway.licensing import LicenseState
from tuomin_gateway.service.app import create_app
from tuomin_gateway.service.registry import AppRegistry
from tuomin_gateway.store import MappingStore

NOW = datetime.now(timezone.utc)


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _make_code(private: Ed25519PrivateKey, *, days: int = 90, issued_at: datetime | None = None) -> str:
    issued = issued_at or NOW
    payload = {
        "v": 1,
        "license_id": "lic_enf01",
        "customer": "执行测试单位",
        "product": "tuomin-workbench",
        "issued_at": issued.isoformat().replace("+00:00", "Z"),
        "expires_at": (issued + timedelta(days=days)).isoformat().replace("+00:00", "Z"),
    }
    payload_bytes = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    signature = private.sign(payload_bytes)
    return f"TM1.{_b64url(payload_bytes)}.{_b64url(signature)}"


@pytest.fixture()
def env(tmp_path, monkeypatch):
    private = Ed25519PrivateKey.generate()
    public_hex = private.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw).hex()
    monkeypatch.setattr(licensing.core, "ISSUER_PUBLIC_KEY_HEX", public_hex)
    monkeypatch.setenv("TUOMIN_LICENSE_REQUIRED", "1")
    monkeypatch.setenv("TUOMIN_DATA_DIR", str(tmp_path / "data"))

    dict_path = tmp_path / "demo_dict.json"
    dict_path.write_text(json.dumps(
        [{"entry_id": "e1", "canonical_value": "示例集团", "label": "ORG",
          "aliases": [], "risk_level": "high", "status": "active", "version": "t"}],
        ensure_ascii=False), encoding="utf-8")
    cfg = tmp_path / "apps.json"
    cfg.write_text(json.dumps(
        {"apps": {"demo": {"profile": "strict", "dictionary": str(dict_path)}}},
        ensure_ascii=False), encoding="utf-8")
    app = create_app(registry=AppRegistry.load(cfg), store=MappingStore(tmp_path / "maps"),
                     session_ttl=3600, admin_token="test-token")
    client = TestClient(app, base_url="http://localhost")
    client.headers.update({"x-tuomin-admin-token": "test-token"})
    yield client, private
    # 测试进程内 create_app 会重置全局 provider；这里再兜底一次。
    licensing.configure_enforcement(None)


# --- 中间件拦截 ---------------------------------------------------------------


def test_unlicensed_blocks_new_tasks(env):
    client, _ = env
    for path in ("/api/v1/redact", "/api/v1/documents/parse", "/redact",
                 "/api/v1/namespaces", "/v1/chat/completions",
                 "/apps/demo/v1/chat/completions"):
        resp = client.post(path, json={})
        assert resp.status_code == 403, path
        assert resp.json()["error"]["code"] == "license_unlicensed", path


def test_unlicensed_allows_recovery_and_management(env):
    client, _ = env
    # 恢复/管理面永不 403-license（下游业务错误如 400/404/422 均可接受）
    for path in ("/api/v1/refill", "/api/v1/documents/refill",
                 "/api/v1/documents/unlock-mapping", "/session/sid-x/refill"):
        resp = client.post(path, json={})
        assert resp.status_code != 403, path
    assert client.get("/healthz").status_code == 200
    assert client.get("/api/v1/license/status").status_code == 200
    assert client.get("/ui/").status_code == 200


def test_status_when_unlicensed(env):
    client, _ = env
    status = client.get("/api/v1/license/status").json()
    assert status["enforcement"] == "enabled"
    assert status["state"] == "unlicensed"
    assert status["customer"] is None


def test_activate_then_allowed(env):
    client, private = env
    code = _make_code(private)
    resp = client.post("/api/v1/license/activate", json={"code": code})
    assert resp.status_code == 200, resp.text
    status = resp.json()
    assert status["state"] == "valid"
    assert status["customer"] == "执行测试单位"
    assert status["days_left"] >= 89

    # 激活后新建任务不再被授权层拦截（业务层可能因 payload 不完整报错，但不是 403）
    resp = client.post("/api/v1/redact", json={})
    assert resp.status_code != 403


def test_activate_rejects_bad_code(env):
    client, _ = env
    resp = client.post("/api/v1/license/activate", json={"code": "TM1.bad.code"})
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "license_invalid"


def test_expired_license_blocks_again(env):
    client, private = env
    expired = _make_code(private, days=1, issued_at=NOW - timedelta(days=licensing.GRACE_DAYS + 10))
    resp = client.post("/api/v1/license/activate", json={"code": expired})
    assert resp.status_code == 200
    assert resp.json()["state"] == "expired"
    resp = client.post("/api/v1/redact", json={})
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "license_expired"


# --- 编译模块原生后手 -----------------------------------------------------------


def test_redactor_backstop_without_provider(monkeypatch, tmp_path):
    """provider 未装配时，编译入口的 enforce 兜底仍按磁盘授权状态执行。"""
    from tuomin_gateway.redactor import redact_text
    from tuomin_gateway.schemas import DetectionSpan

    monkeypatch.setenv("TUOMIN_LICENSE_REQUIRED", "1")
    monkeypatch.setenv("TUOMIN_DATA_DIR", str(tmp_path))
    licensing.configure_enforcement(None)  # 模拟中间件/provider 被移除

    span = DetectionSpan(start=0, end=4, label="ORG", confidence=1.0, source="rules",
                         detector_version="t", text_hash="h")
    with pytest.raises(licensing.LicenseBlockedError) as excinfo:
        redact_text("示例集团", [span])
    assert excinfo.value.state is LicenseState.UNLICENSED


def test_enforce_ignores_provider_override(monkeypatch, tmp_path):
    """逆向评估指出的信任边界：provider 被替换为恒 VALID 时，原生 enforce
    必须仍按磁盘授权状态阻断（执行开启时 provider 不再是信任根）。"""
    from tuomin_gateway.redactor import redact_text
    from tuomin_gateway.schemas import DetectionSpan

    monkeypatch.setenv("TUOMIN_LICENSE_REQUIRED", "1")
    monkeypatch.setenv("TUOMIN_DATA_DIR", str(tmp_path))
    licensing.configure_enforcement(lambda: LicenseState.VALID)  # 恶意 provider

    span = DetectionSpan(start=0, end=4, label="ORG", confidence=1.0, source="rules",
                         detector_version="t", text_hash="h")
    with pytest.raises(licensing.LicenseBlockedError) as excinfo:
        redact_text("示例集团", [span])
    assert excinfo.value.state is LicenseState.UNLICENSED
