"""Synthetic HTTP contracts, not acceptance of the real NER model or pasteboard.

An explicitly empty, ready NER double keeps these tests offline while preserving
the production file_workbench profile. Rules and dictionary detection are real.
"""
from __future__ import annotations

from contextlib import contextmanager
import json
import secrets
import socket
import threading
import time
from urllib.error import HTTPError
from urllib.request import ProxyHandler, Request, build_opener

import pytest

from tuomin_gateway.detectors import ner
from tuomin_gateway.placeholders import PLACEHOLDER_RE
from tuomin_gateway.service.app import create_app
from tuomin_gateway.service.registry import AppRegistry, hash_capability_token
from tuomin_gateway.store import MappingStore


_ORG = "虚构星橙工程研究院"
_SECOND_ORG = "虚构云帆科技有限公司"
_CAPABILITIES = ("detect", "redact", "namespace", "trusted_refill")


class _SyntheticReadyNER:
    version = "synthetic-contract-v1"

    def detect(self, text):
        return []


class _LiveGatewayFixture:
    def __init__(self, *values):
        self._values = values

    def __iter__(self):
        return iter(self._values)

    def __repr__(self):
        return "<synthetic offline HTTP fixture>"


@pytest.fixture
def live_gateway(tmp_path, monkeypatch):
    """Start/restart real uvicorn over a disposable synthetic registry/store."""
    import uvicorn

    def localhost_getaddrinfo(host, port=0, *args, **kwargs):
        assert host in {"127.0.0.1", "localhost"}
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", port or 0))]

    monkeypatch.setattr(socket, "getaddrinfo", localhost_getaddrinfo)
    monkeypatch.setattr(ner, "get_ner_detector", lambda: _SyntheticReadyNER())
    monkeypatch.setenv("TUOMIN_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("TUOMIN_LICENSE_REQUIRED", raising=False)
    config_path = tmp_path / "synthetic-registry.json"
    config_path.write_text('{"apps": {}}', encoding="utf-8")
    dictionary = tmp_path / "synthetic-dictionary.json"
    dictionary.write_text(json.dumps([
        {"canonical_value": name, "aliases": [], "label": "ORG", "status": "active"}
        for name in (_ORG, _SECOND_ORG)
    ], ensure_ascii=False), encoding="utf-8")
    tokens = {name: secrets.token_urlsafe(32) for name in _CAPABILITIES}
    admin_token = secrets.token_urlsafe(32)

    @contextmanager
    def start():
        app = create_app(
            registry=AppRegistry.load(config_path),
            store=MappingStore(tmp_path / "synthetic-mappings"),
            admin_token=admin_token,
        )
        server = uvicorn.Server(uvicorn.Config(
            app, host="127.0.0.1", port=0, log_level="error", access_log=False,
        ))
        thread = threading.Thread(target=server.run, daemon=True)
        thread.start()
        deadline = time.monotonic() + 10
        while not server.started and time.monotonic() < deadline:
            time.sleep(0.02)
        assert server.started
        port = server.servers[0].sockets[0].getsockname()[1]
        opener = build_opener(ProxyHandler({}))

        def request(method, path, payload=None, *, capability="namespace", admin=False, token=None):
            headers = {"Content-Type": "application/json"}
            if admin:
                headers["x-tuomin-admin-token"] = admin_token
            else:
                headers["x-tuomin-capability-token"] = token or tokens[capability]
            data = None if payload is None else json.dumps(payload).encode("utf-8")
            req = Request(f"http://127.0.0.1:{port}{path}", data=data, headers=headers, method=method)
            try:
                with opener.open(req, timeout=10) as response:
                    return response.status, json.load(response)
            except HTTPError as error:
                return error.code, json.load(error)

        try:
            yield request
        finally:
            server.should_exit = True
            thread.join(timeout=5)
            assert not thread.is_alive()
            app.state.job_store.close()
            from tuomin_gateway import licensing

            licensing.configure_enforcement(None)

    entry = {
        "profile": "file_workbench",
        "dictionary": str(dictionary),
        "capabilities": list(_CAPABILITIES),
        "capability_tokens": {key: hash_capability_token(value) for key, value in tokens.items()},
        "refill_contracts": ["trusted_display"],
        "mapping_scopes": ["namespace"],
    }
    return _LiveGatewayFixture(start, entry, tokens, admin_token, config_path)


def _register_and_redact(request, entry):
    status, registration = request("PUT", "/admin/apps/clipboard", entry, admin=True)
    assert status == 200
    assert registration["entry"]["profile"] == "file_workbench"
    status, created = request("POST", "/api/v1/namespaces", {"app_id": "clipboard"})
    assert status == 200
    namespace = created["namespace_id"]
    path = f"/api/v1/namespaces/{namespace}/redact"
    status, first = request("POST", path, {"app_id": "clipboard", "text": f"甲方为{_ORG}。"})
    assert status == 200
    status, second = request("POST", path, {
        "app_id": "clipboard", "text": f"请{_ORG}和{_SECOND_ORG}确认。",
    })
    assert status == 200
    assert first["detectors"]["degraded"] is False
    assert second["egress_allowed"] is True
    first_placeholder = PLACEHOLDER_RE.findall(first["masked_text"])[0]
    assert first_placeholder in PLACEHOLDER_RE.findall(second["masked_text"])
    assert first["label_counts"] == {"ORG": 1}
    assert second["label_counts"] == {"ORG": 2}
    assert second["entity_count"] == 2
    return namespace, first, second


def test_live_clipboard_registration_refill_and_safe_ledger(live_gateway):
    start, entry, tokens, admin_token, config_path = live_gateway
    with start() as request:
        namespace, first, second = _register_and_redact(request, entry)
        placeholder = PLACEHOLDER_RE.findall(first["masked_text"])[0]
        answer = f"请由{placeholder}确认。"
        status, restored = request("POST", "/api/v1/refill", {
            "app_id": "clipboard", "contract": "trusted_display",
            "mapping_handle": second["mapping_handle"], "text": answer,
        }, capability="trusted_refill")
        assert status == 200
        assert restored["status"] == "ok"
        assert bool(restored["text"] == answer.replace(placeholder, _ORG))
        assert restored["audit"]["restored_count"] == 1
        status, blocked = request("POST", "/api/v1/refill", {
            "app_id": "clipboard", "contract": "trusted_display",
            "mapping_handle": second["mapping_handle"],
            "text": answer.replace(placeholder, placeholder.lower()),
        }, capability="trusted_refill")
        assert status == 200
        assert blocked["status"] == "blocked"
        assert blocked["text"] is None
        assert "altered_placeholder" in blocked["error_types"]

        # A capability is independent: namespace authority cannot refill.
        status, denied = request("POST", "/api/v1/refill", {
            "app_id": "clipboard", "contract": "trusted_display",
            "mapping_handle": second["mapping_handle"], "text": answer,
        })
        assert status == 403
        assert denied["error"]["code"] == "capability_denied"
        status, ledger = request("GET", "/api/v1/documents/calls?entry=clipboard", admin=True)
        assert status == 200
        assert len(ledger["calls"]) == 4
        assert all(call["entry"] == "clipboard" for call in ledger["calls"])
        assert all(call["app_id"] == "clipboard" for call in ledger["calls"])
        assert sum(call["blocked"] for call in ledger["calls"]) == 1
        serialized = json.dumps(ledger, ensure_ascii=False)
        for secret in (_ORG, _SECOND_ORG, admin_token, *tokens.values(), first["mapping_handle"], second["mapping_handle"]):
            assert bool(secret not in serialized)
        saved_registry = config_path.read_text(encoding="utf-8")
        assert all(token not in saved_registry for token in tokens.values())


def test_live_clipboard_restart_restores_only_owned_handles(live_gateway):
    start, entry, tokens, _admin_token, _config_path = live_gateway
    with start() as request:
        namespace, first, second = _register_and_redact(request, entry)

    with start() as request:
        path = f"/api/v1/namespaces/{namespace}"
        status, resumed = request("GET", f"{path}?app_id=clipboard")
        assert status == 200
        assert resumed["placeholder_count"] == 2
        assert bool(resumed["mapping_handles"] == [first["mapping_handle"], second["mapping_handle"]])
        assert all(value not in json.dumps(resumed, ensure_ascii=False) for value in (_ORG, _SECOND_ORG))
        status, restored = request("POST", "/api/v1/refill", {
            "app_id": "clipboard", "contract": "trusted_display",
            "mapping_handle": resumed["mapping_handles"][-1], "text": first["masked_text"],
        }, capability="trusted_refill")
        assert status == 200
        assert restored["status"] == "ok"
        assert bool(_ORG in restored["text"])
        status, denied = request("GET", f"{path}?app_id=clipboard", token=secrets.token_urlsafe(32))
        assert status == 403
        assert "mapping_handles" not in denied
        other_token = secrets.token_urlsafe(32)
        other_entry = {**entry, "capability_tokens": {
            **entry["capability_tokens"], "namespace": hash_capability_token(other_token),
        }}
        status, _ = request("PUT", "/admin/apps/synthetic_other", other_entry, admin=True)
        assert status == 200
        status, denied = request("GET", f"{path}?app_id=synthetic_other", token=other_token)
        assert status == 404
        assert "mapping_handles" not in denied
        status, _ = request("POST", f"{path}/archive", {"app_id": "clipboard"})
        assert status == 200
        status, _ = request("DELETE", f"{path}?app_id=clipboard")
        assert status == 200
        status, removed = request("POST", "/api/v1/refill", {
            "app_id": "clipboard", "contract": "trusted_display",
            "mapping_handle": second["mapping_handle"], "text": first["masked_text"],
        }, capability="trusted_refill")
        assert status == 404
        assert removed["error"]["code"] == "mapping_handle_not_found"


def test_live_clipboard_package_requires_admin_and_authenticates_source(live_gateway):
    start, entry, tokens, admin_token, _config_path = live_gateway
    passphrase = secrets.token_urlsafe(32)
    with start() as request:
        namespace, _first, second = _register_and_redact(request, entry)
        path = f"/admin/clipboard/namespaces/{namespace}/package"
        status, denied = request("POST", path, {"passphrase": passphrase})
        assert status == 401
        assert "package" not in denied
        status, _ = request("PUT", "/admin/apps/synthetic_other", entry, admin=True)
        assert status == 200
        status, other = request("POST", "/api/v1/namespaces", {"app_id": "synthetic_other"})
        assert status == 200
        status, denied = request("POST", f"/admin/clipboard/namespaces/{other['namespace_id']}/package", {
            "passphrase": passphrase,
        }, admin=True)
        assert status == 404
        assert "package" not in denied
        status, invalid = request("POST", path, {"passphrase": ""}, admin=True)
        assert status == 400
        assert invalid["error"]["code"] == "passphrase_required"
        status, exported = request("POST", path, {"passphrase": passphrase}, admin=True)
        assert status == 200
        package = exported["package"]
        assert package["binding"]["app_id"] == "clipboard"
        assert package["binding"]["versions"]["source_kind"] == "clipboard_namespace"
        assert package["cipher"]["name"] == "AES-256-GCM"
        serialized = json.dumps(exported, ensure_ascii=False)
        for secret in (_ORG, _SECOND_ORG, passphrase, admin_token, *tokens.values(), second["mapping_handle"]):
            assert bool(secret not in serialized)
        unlock_path = "/api/v1/documents/unlock-mapping"
        status, unlocked = request("POST", unlock_path, {
            "package": package, "passphrase": passphrase,
        }, admin=True)
        assert status == 200
        assert unlocked["app_id"] == "clipboard"
        assert bool({item["original_value"] for item in unlocked["entries"]} == {_ORG, _SECOND_ORG})
        status, wrong_password = request("POST", unlock_path, {
            "package": package, "passphrase": secrets.token_urlsafe(32),
        }, admin=True)
        assert status == 403
        assert "entries" not in wrong_password
        for field, value in (
            ("source_kind", "workbench"),
            ("gateway", "tampered-synthetic-version"),
        ):
            tampered = json.loads(json.dumps(package))
            tampered["binding"]["versions"][field] = value
            status, rejected = request("POST", unlock_path, {
                "package": tampered, "passphrase": passphrase,
            }, admin=True)
            assert status == 403
            assert "entries" not in rejected
        tampered = json.loads(json.dumps(package))
        tampered["binding"]["app_id"] = "workbench"
        status, rejected = request("POST", unlock_path, {
            "package": tampered, "passphrase": passphrase,
        }, admin=True)
        assert status == 403
        assert "entries" not in rejected


def test_live_clipboard_package_export_and_unlock_survive_license_block(live_gateway, monkeypatch):
    start, entry, _tokens, _admin_token, _config_path = live_gateway
    with start() as request:
        namespace, _first, _second = _register_and_redact(request, entry)
    monkeypatch.setenv("TUOMIN_LICENSE_REQUIRED", "1")
    with start() as request:
        status, denied = request("POST", "/api/v1/namespaces", {"app_id": "clipboard"})
        assert status == 403
        assert denied["error"]["code"].startswith("license_")
        passphrase = secrets.token_urlsafe(32)
        status, exported = request("POST", f"/admin/clipboard/namespaces/{namespace}/package", {
            "passphrase": passphrase,
        }, admin=True)
        assert status == 200
        status, unlocked = request("POST", "/api/v1/documents/unlock-mapping", {
            "package": exported["package"], "passphrase": passphrase,
        }, admin=True)
        assert status == 200
        assert len(unlocked["entries"]) == 2


def test_live_clipboard_mapping_route_is_admin_only_and_scoped_to_the_clipboard_app(live_gateway):
    start, entry, tokens, admin_token, _config_path = live_gateway
    with start() as request:
        namespace, _first, _second = _register_and_redact(request, entry)
        path = f"/admin/clipboard/namespaces/{namespace}/mapping"
        status, denied = request("POST", path, {})
        assert status == 401 and "entries" not in denied
        status, shown = request("POST", path, {}, admin=True)
        assert status == 200
        assert any(item["original_value"] == _ORG for item in shown["entries"])
        status, _ = request("PUT", "/admin/apps/synthetic_other", entry, admin=True)
        assert status == 200
        status, other = request("POST", "/api/v1/namespaces", {"app_id": "synthetic_other"})
        assert status == 200
        status, foreign = request("POST", f"/admin/clipboard/namespaces/{other['namespace_id']}/mapping", {}, admin=True)
        assert status == 404 and "entries" not in foreign
