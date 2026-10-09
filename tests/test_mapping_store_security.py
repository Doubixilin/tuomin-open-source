from __future__ import annotations

import json
import os
import time

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from tuomin_gateway.mapping import load_mapping, save_mapping  # noqa: E402
from tuomin_gateway.schemas import MappingEntry  # noqa: E402
from tuomin_gateway.service.app import create_app  # noqa: E402
from tuomin_gateway.service.registry import AppRegistry  # noqa: E402
from tuomin_gateway.store import (  # noqa: E402
    MacOSAesGcmMappingCipher,
    MacOSKeychainKeyProvider,
    MappingStore,
    PlaintextMappingCipher,
    StoreEncryptionError,
)


def _entries(value: str = "Synthetic Entity Alpha") -> list[MappingEntry]:
    return [
        MappingEntry("<ORG_001>", "ORG", value, "sha256:synthetic")
    ]


class _StaticKeyProvider:
    name = "synthetic-static-key"

    def __init__(self, key: bytes = b"K" * 32) -> None:
        self.key = key

    def get_or_create_key(self) -> bytes:
        return self.key


def test_mapping_store_filename_does_not_contain_task_id(tmp_path):
    store = MappingStore(tmp_path, ttl_seconds=3600)
    path = store.save("synthetic_case_alpha", _entries())

    assert "synthetic_case_alpha" not in path.name
    assert path.name.endswith(".mapping.enc")


def test_mapping_store_safe_status_excludes_mapping_values(tmp_path):
    store = MappingStore(tmp_path, ttl_seconds=123)
    store.save("synthetic_case_alpha", _entries())

    status = store.safe_status()
    encoded = json.dumps(status, ensure_ascii=False)

    assert status["ttl_seconds"] == 123
    assert status["directory"] == str(tmp_path)
    assert "platform_mode" in status
    assert "dpapi_available" in status
    assert "Synthetic Entity Alpha" not in encoded
    assert "<ORG_001>" not in encoded


def test_macos_aesgcm_round_trip_is_encrypted_and_reported_safely(tmp_path):
    cipher = MacOSAesGcmMappingCipher(_StaticKeyProvider())
    store = MappingStore(tmp_path, ttl_seconds=123, cipher=cipher)

    path = store.save("synthetic_case_alpha", _entries())
    raw = path.read_bytes()

    assert raw.startswith(b"MACAES1\n")
    assert b"Synthetic Entity Alpha" not in raw
    assert store.load("synthetic_case_alpha")[0].original_value == "Synthetic Entity Alpha"
    status = store.safe_status()
    assert status["directory"] == str(tmp_path)
    assert status["ttl_seconds"] == 123
    assert status["platform_mode"] == "macos-keychain-aesgcm"
    assert status["encrypted_at_rest"] is True
    assert status["format_marker"] == "MACAES1"
    assert status["key_provider"] == "synthetic-static-key"


def test_macos_aesgcm_wrong_key_and_file_swap_fail_authentication(tmp_path):
    first = MappingStore(
        tmp_path,
        cipher=MacOSAesGcmMappingCipher(_StaticKeyProvider(b"A" * 32)),
    )
    first.save("first", _entries("Synthetic First"))
    first.save("second", _entries("Synthetic Second"))

    wrong_key = MappingStore(
        tmp_path,
        cipher=MacOSAesGcmMappingCipher(_StaticKeyProvider(b"B" * 32)),
    )
    with pytest.raises(StoreEncryptionError, match="authentication failed"):
        wrong_key.load("first")

    first_path = first._path("first")
    second_path = first._path("second")
    first_blob, second_blob = first_path.read_bytes(), second_path.read_bytes()
    first_path.write_bytes(second_blob)
    second_path.write_bytes(first_blob)
    with pytest.raises(StoreEncryptionError, match="authentication failed"):
        first.load("first")


def test_macos_default_is_secure_unless_plaintext_is_explicit(monkeypatch):
    import tuomin_gateway.store as store_module

    monkeypatch.setattr(store_module, "_IS_WINDOWS", False)
    monkeypatch.setattr(store_module, "_IS_MACOS", True)
    monkeypatch.delenv("TUOMIN_ALLOW_PLAINTEXT_STORE", raising=False)

    secure = store_module._default_cipher()
    assert isinstance(secure, MacOSAesGcmMappingCipher)

    monkeypatch.setenv("TUOMIN_ALLOW_PLAINTEXT_STORE", "1")
    explicit_dev = store_module._default_cipher()
    assert isinstance(explicit_dev, PlaintextMappingCipher)
    assert explicit_dev.platform_mode == "plaintext-dev-explicit"


def test_explicit_plaintext_migration_reencrypts_in_place(tmp_path):
    plaintext = MappingStore(
        tmp_path,
        cipher=PlaintextMappingCipher(platform_mode="synthetic-source"),
    )
    path = plaintext.save("synthetic_case_alpha", _entries())
    encrypted = MappingStore(
        tmp_path,
        cipher=MacOSAesGcmMappingCipher(_StaticKeyProvider()),
    )

    assert path.read_bytes().startswith(b"PLAIN1\n")
    assert encrypted.migrate_plaintext_files() == 1
    assert path.read_bytes().startswith(b"MACAES1\n")
    assert b"Synthetic Entity Alpha" not in path.read_bytes()
    assert encrypted.load("synthetic_case_alpha")[0].original_value == "Synthetic Entity Alpha"
    assert encrypted.migrate_plaintext_files() == 0


def test_keychain_creation_uses_native_backend_and_never_overwrites(monkeypatch):
    import tuomin_gateway.store as store_module

    generated = b"G" * 32
    calls: list[tuple[str, str, bytes]] = []

    class Backend:
        def find(self, *, service, account):
            return None

        def add(self, *, service, account, secret):
            calls.append((service, account, secret))
            return True

    monkeypatch.setattr(store_module.secrets, "token_bytes", lambda size: generated)
    provider = MacOSKeychainKeyProvider(
        service="synthetic.test",
        account="tester",
        backend=Backend(),
    )

    assert provider.get_or_create_key() == generated
    assert calls == [("synthetic.test", "tester", generated)]


def test_keychain_creation_race_reloads_winner_without_overwrite(monkeypatch):
    import tuomin_gateway.store as store_module

    winner = b"W" * 32
    lookup_count = 0

    class Backend:
        def find(self, *, service, account):
            nonlocal lookup_count
            lookup_count += 1
            if lookup_count == 1:
                return None
            return winner

        def add(self, *, service, account, secret):
            return False

    provider = MacOSKeychainKeyProvider(
        service="synthetic.race",
        account="tester",
        backend=Backend(),
    )

    assert provider.get_or_create_key() == winner


def test_admin_settings_uses_safe_mapping_store_status_without_values(tmp_path):
    store = MappingStore(tmp_path / "maps", ttl_seconds=3600)
    store.save("synthetic_case_alpha", _entries())
    app = create_app(registry=AppRegistry({}), store=store, admin_token="test-token")
    client = TestClient(app, base_url="http://localhost")

    response = client.get("/admin/settings", headers={"x-tuomin-admin-token": "test-token"})

    payload = response.json()
    encoded = json.dumps(payload, ensure_ascii=False)
    assert response.status_code == 200
    assert payload["mapping_store"]["ttl_seconds"] == 3600
    assert "platform_mode" in payload["mapping_store"]
    assert "Synthetic Entity Alpha" not in encoded
    assert "<ORG_001>" not in encoded


def test_windows_plaintext_mapping_is_refused(monkeypatch, tmp_path):
    import tuomin_gateway.store as store_module

    monkeypatch.setattr(store_module, "_IS_WINDOWS", True)
    store = MappingStore(tmp_path, ttl_seconds=3600)
    store.directory.mkdir(exist_ok=True)
    payload = json.dumps([entry.to_dict() for entry in _entries()]).encode("utf-8")
    store._path("synthetic_case_alpha").write_bytes(store_module._PLAIN_MAGIC + payload)

    with pytest.raises(StoreEncryptionError):
        store.load("synthetic_case_alpha")


def test_windows_dpapi_encrypt_failure_refuses_plaintext(monkeypatch, tmp_path):
    import tuomin_gateway.store as store_module

    def fail_dpapi(protect: bool, data: bytes) -> bytes:
        raise OSError("synthetic dpapi failure")

    monkeypatch.setattr(store_module, "_IS_WINDOWS", True)
    monkeypatch.setattr(store_module, "_dpapi", fail_dpapi)
    store = MappingStore(tmp_path, ttl_seconds=3600)

    with pytest.raises(StoreEncryptionError):
        store.save("synthetic_case_alpha", _entries())

    assert list(tmp_path.glob("*.mapping.enc")) == []


def test_cli_save_mapping_is_plaintext_local_artifact(tmp_path):
    path = tmp_path / "cli.mapping.json"
    save_mapping(_entries(), path)

    raw = path.read_text(encoding="utf-8")

    assert "Synthetic Entity Alpha" in raw
    assert getattr(load_mapping(path)[0], "original" + "_value") == "Synthetic Entity Alpha"


def test_purge_expired_removes_old_mapping_file(tmp_path):
    store = MappingStore(tmp_path, ttl_seconds=10)
    old_path = store.save("old_synthetic_case", _entries())
    keep_path = store.save("new_synthetic_case", _entries("Synthetic Entity Beta"))
    old = time.time() - 100
    old_path.touch()
    keep_path.touch()
    old_path.chmod(0o600)
    keep_path.chmod(0o600)
    os.utime(old_path, (old, old))

    assert store.purge_expired() == 1
    assert not old_path.exists()
    assert keep_path.exists()
