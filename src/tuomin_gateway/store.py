"""Platform-protected, TTL'd mapping store for the central service.

Windows uses current-user DPAPI. macOS uses an AES-256-GCM master key held in
the login Keychain. Linux retains the explicit development plaintext fallback.
The mapping file name is a SHA-256 digest of the logical key and is also bound
to the authenticated ciphertext so files cannot be swapped between handles.
"""
from __future__ import annotations

import getpass
import hashlib
import json
import os
import secrets
import sys
import threading
import time
from pathlib import Path
from typing import Protocol

from tuomin_gateway.platform_security import restrict_permissions
from tuomin_gateway.schemas import MappingEntry

_DPAPI_MAGIC = b"DPAPI1\n"
_MACOS_MAGIC = b"MACAES1\n"
_PLAIN_MAGIC = b"PLAIN1\n"
_IS_WINDOWS = sys.platform == "win32"
_IS_MACOS = sys.platform == "darwin"
# Packaged builds MUST override this via TUOMIN_KEYCHAIN_SERVICE: the Keychain
# binds an item to the creating binary's code-signing identity, so a frozen app
# reading an item created by a different binary (e.g. a dev venv) blocks in
# securityd waiting for a consent prompt.
_MACOS_KEYCHAIN_SERVICE = os.environ.get(
    "TUOMIN_KEYCHAIN_SERVICE", "com.tuomin.gateway.mapping-store.v1"
)
_MACOS_AAD_PREFIX = b"tuomin-mapping-store-v1\0"
_MACOS_SECURITY_FRAMEWORK = "/System/Library/Frameworks/Security.framework/Security"


class StoreEncryptionError(RuntimeError):
    """The platform encryption boundary failed and persistence was refused."""


class MappingCipher(Protocol):
    marker: bytes
    platform_mode: str
    encrypted_at_rest: bool
    key_provider_name: str | None

    def encrypt(self, payload: bytes, *, context: bytes) -> bytes: ...

    def decrypt(self, blob: bytes, *, context: bytes) -> bytes: ...


class MappingKeyProvider(Protocol):
    name: str

    def get_or_create_key(self) -> bytes: ...


class MacOSKeychainBackend(Protocol):
    def find(self, *, service: str, account: str) -> bytes | None: ...

    def add(self, *, service: str, account: str, secret: bytes) -> bool: ...


class PlaintextMappingCipher:
    marker = _PLAIN_MAGIC
    encrypted_at_rest = False
    key_provider_name = None

    def __init__(self, platform_mode: str = "plaintext-dev-fallback") -> None:
        self.platform_mode = platform_mode

    def encrypt(self, payload: bytes, *, context: bytes) -> bytes:
        return self.marker + payload

    def decrypt(self, blob: bytes, *, context: bytes) -> bytes:
        if not blob.startswith(self.marker):
            raise ValueError("unrecognized mapping file format")
        return blob[len(self.marker) :]


class WindowsDpapiMappingCipher:
    marker = _DPAPI_MAGIC
    platform_mode = "windows-dpapi"
    encrypted_at_rest = True
    key_provider_name = "windows-current-user-dpapi"

    def encrypt(self, payload: bytes, *, context: bytes) -> bytes:
        try:
            return self.marker + _dpapi(True, payload)
        except OSError as exc:
            raise StoreEncryptionError(
                f"DPAPI encryption failed; refusing plaintext at rest: {exc}"
            ) from exc

    def decrypt(self, blob: bytes, *, context: bytes) -> bytes:
        if blob.startswith(_PLAIN_MAGIC):
            raise StoreEncryptionError("refusing to read a plaintext mapping on Windows")
        if not blob.startswith(self.marker):
            raise ValueError("unrecognized mapping file format")
        try:
            return _dpapi(False, blob[len(self.marker) :])
        except OSError as exc:
            raise StoreEncryptionError("DPAPI decryption failed") from exc


class MacOSNativeKeychainBackend:
    """Minimal ctypes binding to the macOS Security.framework Keychain API."""

    _ITEM_NOT_FOUND = -25300
    _DUPLICATE_ITEM = -25299

    def __init__(self, framework_path: str = _MACOS_SECURITY_FRAMEWORK) -> None:
        import ctypes

        try:
            security = ctypes.CDLL(framework_path)
        except OSError as exc:
            raise StoreEncryptionError("macOS Security.framework is unavailable") from exc

        security.SecKeychainFindGenericPassword.argtypes = [
            ctypes.c_void_p,
            ctypes.c_uint32,
            ctypes.c_char_p,
            ctypes.c_uint32,
            ctypes.c_char_p,
            ctypes.POINTER(ctypes.c_uint32),
            ctypes.POINTER(ctypes.c_void_p),
            ctypes.POINTER(ctypes.c_void_p),
        ]
        security.SecKeychainFindGenericPassword.restype = ctypes.c_int32
        security.SecKeychainAddGenericPassword.argtypes = [
            ctypes.c_void_p,
            ctypes.c_uint32,
            ctypes.c_char_p,
            ctypes.c_uint32,
            ctypes.c_char_p,
            ctypes.c_uint32,
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_void_p),
        ]
        security.SecKeychainAddGenericPassword.restype = ctypes.c_int32
        security.SecKeychainItemFreeContent.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        security.SecKeychainItemFreeContent.restype = ctypes.c_int32
        self._ctypes = ctypes
        self._security = security

    @staticmethod
    def _identity(value: str) -> bytes:
        encoded = value.encode("utf-8")
        if not encoded:
            raise StoreEncryptionError("macOS Keychain service and account must not be empty")
        return encoded

    def find(self, *, service: str, account: str) -> bytes | None:
        ctypes = self._ctypes
        service_raw = self._identity(service)
        account_raw = self._identity(account)
        length = ctypes.c_uint32()
        data = ctypes.c_void_p()
        status = self._security.SecKeychainFindGenericPassword(
            None,
            len(service_raw),
            service_raw,
            len(account_raw),
            account_raw,
            ctypes.byref(length),
            ctypes.byref(data),
            None,
        )
        if status == self._ITEM_NOT_FOUND:
            return None
        if status != 0:
            raise StoreEncryptionError(f"macOS Keychain lookup failed (OSStatus {status})")
        try:
            return ctypes.string_at(data, length.value)
        finally:
            self._security.SecKeychainItemFreeContent(None, data)

    def add(self, *, service: str, account: str, secret: bytes) -> bool:
        ctypes = self._ctypes
        service_raw = self._identity(service)
        account_raw = self._identity(account)
        secret_buffer = ctypes.create_string_buffer(secret, len(secret))
        status = self._security.SecKeychainAddGenericPassword(
            None,
            len(service_raw),
            service_raw,
            len(account_raw),
            account_raw,
            len(secret),
            ctypes.cast(secret_buffer, ctypes.c_void_p),
            None,
        )
        if status == 0:
            return True
        if status == self._DUPLICATE_ITEM:
            return False
        raise StoreEncryptionError(f"macOS Keychain key creation failed (OSStatus {status})")


class MacOSKeychainKeyProvider:
    """Read or create the mapping master key in the current user's Keychain.

    The raw key is passed directly to Security.framework in process. It is
    never placed in command-line arguments, environment variables, logs, or
    mapping files.
    """

    name = "macos-login-keychain"

    def __init__(
        self,
        *,
        service: str = _MACOS_KEYCHAIN_SERVICE,
        account: str | None = None,
        backend: MacOSKeychainBackend | None = None,
    ) -> None:
        self.service = service
        self.account = account or getpass.getuser()
        self._backend = backend

    @staticmethod
    def _validate(key: bytes) -> bytes:
        if len(key) != 32:
            raise StoreEncryptionError("invalid Tuomin key length in macOS Keychain")
        return key

    @property
    def backend(self) -> MacOSKeychainBackend:
        if self._backend is None:
            self._backend = MacOSNativeKeychainBackend()
        return self._backend

    def _lookup(self) -> bytes | None:
        key = self.backend.find(service=self.service, account=self.account)
        if key is None:
            return None
        return self._validate(key)

    def get_or_create_key(self) -> bytes:
        existing = self._lookup()
        if existing is not None:
            return existing

        generated = secrets.token_bytes(32)
        if self.backend.add(service=self.service, account=self.account, secret=generated):
            return generated

        # Overwriting a winner would make existing mappings permanently
        # unreadable, so a duplicate is always resolved by re-reading it.
        raced = self._lookup()
        if raced is not None:
            return raced
        raise StoreEncryptionError(
            "macOS Keychain key creation race failed; refusing plaintext at rest"
        )


class MacOSAesGcmMappingCipher:
    marker = _MACOS_MAGIC
    platform_mode = "macos-keychain-aesgcm"
    encrypted_at_rest = True

    def __init__(self, key_provider: MappingKeyProvider | None = None) -> None:
        self.key_provider = key_provider or MacOSKeychainKeyProvider()
        self.key_provider_name = self.key_provider.name

    @staticmethod
    def _aesgcm(key: bytes):
        try:
            from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        except ImportError as exc:
            raise StoreEncryptionError(
                "cryptography is required for macOS mapping encryption"
            ) from exc
        return AESGCM(key)

    def encrypt(self, payload: bytes, *, context: bytes) -> bytes:
        key = self.key_provider.get_or_create_key()
        nonce = secrets.token_bytes(12)
        try:
            ciphertext = self._aesgcm(key).encrypt(
                nonce,
                payload,
                _MACOS_AAD_PREFIX + context,
            )
        except StoreEncryptionError:
            raise
        except Exception as exc:
            raise StoreEncryptionError("macOS mapping encryption failed") from exc
        return self.marker + nonce + ciphertext

    def decrypt(self, blob: bytes, *, context: bytes) -> bytes:
        if blob.startswith(_PLAIN_MAGIC):
            raise StoreEncryptionError(
                "plaintext mapping requires explicit migration before macOS use"
            )
        if not blob.startswith(self.marker):
            raise ValueError("unrecognized mapping file format")
        encrypted = blob[len(self.marker) :]
        if len(encrypted) < 12 + 16:
            raise StoreEncryptionError("invalid macOS encrypted mapping payload")
        nonce, ciphertext = encrypted[:12], encrypted[12:]
        key = self.key_provider.get_or_create_key()
        try:
            return self._aesgcm(key).decrypt(
                nonce,
                ciphertext,
                _MACOS_AAD_PREFIX + context,
            )
        except StoreEncryptionError:
            raise
        except Exception as exc:
            raise StoreEncryptionError(
                "macOS mapping authentication failed; refusing recovery"
            ) from exc


# --- Windows DPAPI via ctypes ----------------------------------------------
def _dpapi(protect: bool, data: bytes) -> bytes:
    import ctypes
    from ctypes import wintypes

    class DATA_BLOB(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

    def _to_blob(raw: bytes) -> DATA_BLOB:
        buf = ctypes.create_string_buffer(raw, len(raw))
        return DATA_BLOB(len(raw), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))

    src = _to_blob(data)
    out = DATA_BLOB()
    fn = (
        ctypes.windll.crypt32.CryptProtectData
        if protect
        else ctypes.windll.crypt32.CryptUnprotectData
    )
    ok = fn(ctypes.byref(src), None, None, None, None, 0, ctypes.byref(out))
    if not ok:
        raise OSError("DPAPI operation failed")
    try:
        return ctypes.string_at(out.pbData, out.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(out.pbData)


def _allow_plaintext_store() -> bool:
    return os.environ.get("TUOMIN_ALLOW_PLAINTEXT_STORE", "").strip().lower() in {
        "1",
        "true",
        "yes",
    }


def _default_cipher() -> MappingCipher:
    if _IS_WINDOWS:
        return WindowsDpapiMappingCipher()
    if _IS_MACOS:
        if _allow_plaintext_store():
            return PlaintextMappingCipher(platform_mode="plaintext-dev-explicit")
        return MacOSAesGcmMappingCipher()
    return PlaintextMappingCipher()


def _restrict_permissions(path: Path, *, is_dir: bool) -> None:
    restrict_permissions(path, is_dir=is_dir)


def _atomic_write(path: Path, blob: bytes) -> None:
    temp = path.with_name(f".{path.name}.{os.getpid()}.{secrets.token_hex(4)}.tmp")
    try:
        with temp.open("xb") as handle:
            _restrict_permissions(temp, is_dir=False)
            handle.write(blob)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
        _restrict_permissions(path, is_dir=False)
    finally:
        temp.unlink(missing_ok=True)


def _deserialize_entries(payload: bytes) -> list[MappingEntry]:
    data = json.loads(payload.decode("utf-8"))
    return [MappingEntry.from_dict(item) for item in data]


class MappingStore:
    """Per-key protected mapping persistence with TTL expiry.

    Mutations and TTL check-then-act sequences (load's expired-then-unlink,
    purge's expired-then-delete) run under one in-process lock: without it a
    purge/load could stat an expired file and then delete a FRESH mapping that
    an interleaved ``save_payload`` just atomically replaced onto the same
    path. The deployment boundary is one owning process per mapping directory
    (the namespace vault file-locks its own lifecycle separately); concurrent
    WRITERS across processes sharing one directory are unsupported.
    """

    def __init__(
        self,
        directory: str | Path,
        ttl_seconds: int = 24 * 3600,
        *,
        cipher: MappingCipher | None = None,
    ) -> None:
        self.directory = Path(directory)
        self.ttl_seconds = ttl_seconds
        self._cipher = cipher or _default_cipher()
        self._lock = threading.RLock()

    def child(self, relative_directory: str | Path, *, ttl_seconds: int) -> "MappingStore":
        """Create a store with the same platform cipher and a separate lifecycle."""
        return MappingStore(
            self.directory / relative_directory,
            ttl_seconds=ttl_seconds,
            cipher=self._cipher,
        )

    def _path(self, key: str) -> Path:
        digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
        return self.directory / f"{digest}.mapping.enc"

    @staticmethod
    def _context(path: Path) -> bytes:
        return path.name.encode("ascii")

    def save(self, key: str, entries: list[MappingEntry]) -> Path:
        payload = json.dumps([e.to_dict() for e in entries], ensure_ascii=False).encode("utf-8")
        return self.save_payload(key, payload)

    def save_payload(self, key: str, payload: bytes) -> Path:
        """Persist an authenticated opaque payload using the platform cipher."""
        with self._lock:
            self.directory.mkdir(parents=True, exist_ok=True)
            _restrict_permissions(self.directory, is_dir=True)
            path = self._path(key)
            blob = self._cipher.encrypt(payload, context=self._context(path))
            _atomic_write(path, blob)
            return path

    def safe_status(self) -> dict[str, object]:
        return {
            "directory": str(self.directory),
            "ttl_seconds": self.ttl_seconds,
            "dpapi_available": _IS_WINDOWS,
            "keychain_available": _IS_MACOS and Path(_MACOS_SECURITY_FRAMEWORK).exists(),
            "platform_mode": self._cipher.platform_mode,
            "encrypted_at_rest": self._cipher.encrypted_at_rest,
            "format_marker": self._cipher.marker.rstrip(b"\n").decode("ascii"),
            "key_provider": self._cipher.key_provider_name,
        }

    def load(self, key: str) -> list[MappingEntry]:
        return _deserialize_entries(self.load_payload(key))

    def load_payload(self, key: str) -> bytes:
        """Load and authenticate an opaque payload."""
        with self._lock:
            path = self._path(key)
            if not path.exists():
                raise FileNotFoundError(key)
            if self._expired(path):
                path.unlink(missing_ok=True)
                raise FileNotFoundError(key)
            return self._cipher.decrypt(path.read_bytes(), context=self._context(path))

    def delete(self, key: str) -> bool:
        with self._lock:
            path = self._path(key)
            existed = path.exists()
            path.unlink(missing_ok=True)
            return existed

    def migrate_plaintext_files(self) -> int:
        """Explicitly re-encrypt valid PLAIN1 files using the active cipher."""
        if not self._cipher.encrypted_at_rest:
            raise StoreEncryptionError("plaintext migration requires an encrypted mapping cipher")
        with self._lock:
            if not self.directory.exists():
                return 0
            migrated = 0
            for path in self.directory.glob("*.mapping.enc"):
                raw = path.read_bytes()
                if not raw.startswith(_PLAIN_MAGIC):
                    continue
                payload = raw[len(_PLAIN_MAGIC) :]
                _deserialize_entries(payload)  # validate before replacing the source
                encrypted = self._cipher.encrypt(payload, context=self._context(path))
                _atomic_write(path, encrypted)
                migrated += 1
            return migrated

    def _expired(self, path: Path) -> bool:
        if self.ttl_seconds <= 0:
            return False
        return (time.time() - path.stat().st_mtime) > self.ttl_seconds

    def purge_expired(self) -> int:
        with self._lock:
            if self.ttl_seconds <= 0 or not self.directory.exists():
                return 0
            removed = 0
            for path in self.directory.glob("*.mapping.enc"):
                if self._expired(path):
                    path.unlink(missing_ok=True)
                    removed += 1
            return removed
