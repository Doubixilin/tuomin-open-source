"""Persistent app-scoped namespace mappings with process/file locking."""
from __future__ import annotations

import hashlib
import json
import os
import secrets
import threading
import time
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

from tuomin_gateway.schemas import MappingEntry
from tuomin_gateway.schemas import RedactionTraceEntry
from tuomin_gateway.store import MappingStore, _restrict_permissions


@dataclass(frozen=True)
class NamespaceState:
    namespace_id: str
    app_id: str
    status: str
    entries: list[MappingEntry]
    created_at: int
    updated_at: int
    identity_contract: str = "canonical"
    # Handles of every mapping grant minted from this namespace. Grants are
    # SELF-CONTAINED (they carry the mapping entries), so namespace deletion
    # must revoke them or the deleted namespace's values stay recoverable
    # until store TTL. The list grows with each namespace redact and is only
    # consumed at delete time; grants already killed by TTL are no-op revokes.
    grant_handles: list[str] = field(default_factory=list)
    trace: list[RedactionTraceEntry] = field(default_factory=list)


class NamespaceMismatch(PermissionError):
    pass


class NamespaceVault:
    def __init__(self, store: MappingStore) -> None:
        self.store = store
        self._thread_locks: dict[str, threading.RLock] = {}
        self._lock_guard = threading.Lock()

    @staticmethod
    def _key(namespace_id: str) -> str:
        return f"namespace:{namespace_id}"

    def _lock_path(self, namespace_id: str) -> Path:
        digest = hashlib.sha256(namespace_id.encode()).hexdigest()
        return self.store.directory / "locks" / f"{digest}.lock"

    @contextmanager
    def lock(self, namespace_id: str):
        with self._lock_guard:
            thread_lock = self._thread_locks.setdefault(namespace_id, threading.RLock())
        with thread_lock:
            path = self._lock_path(namespace_id)
            path.parent.mkdir(parents=True, exist_ok=True)
            _restrict_permissions(path.parent, is_dir=True)
            with path.open("a+b") as handle:
                _restrict_permissions(path, is_dir=False)
                if os.name == "nt":  # pragma: no cover - Windows CI only
                    import msvcrt

                    handle.seek(0)
                    if handle.read(1) == b"":
                        handle.write(b"0")
                        handle.flush()
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
                else:
                    import fcntl

                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
                try:
                    yield
                finally:
                    if os.name == "nt":  # pragma: no cover - Windows CI only
                        handle.seek(0)
                        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                    else:
                        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def create(self, *, app_id: str) -> NamespaceState:
        namespace_id = f"ns_{secrets.token_urlsafe(24)}"
        now = int(time.time())
        state = NamespaceState(namespace_id, app_id, "active", [], now, now)
        self.save(state)
        return state

    def save(self, state: NamespaceState) -> None:
        payload = {
            "version": 1,
            "namespace_id": state.namespace_id,
            "app_id": state.app_id,
            "status": state.status,
            "created_at": state.created_at,
            "updated_at": state.updated_at,
            "identity_contract": state.identity_contract,
            "grant_handles": list(state.grant_handles),
            "entries": [entry.to_dict() for entry in state.entries],
            "trace": [entry.to_dict() for entry in state.trace],
        }
        self.store.save_payload(
            self._key(state.namespace_id),
            json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        )

    def load(self, namespace_id: str, *, app_id: str) -> NamespaceState:
        if not isinstance(namespace_id, str) or not namespace_id.startswith("ns_"):
            raise FileNotFoundError("namespace")
        payload = json.loads(
            self.store.load_payload(self._key(namespace_id)).decode("utf-8")
        )
        if payload.get("app_id") != app_id:
            raise NamespaceMismatch("namespace belongs to another app")
        return NamespaceState(
            namespace_id=namespace_id,
            app_id=app_id,
            status=str(payload["status"]),
            entries=[MappingEntry.from_dict(item) for item in payload["entries"]],
            created_at=int(payload["created_at"]),
            updated_at=int(payload["updated_at"]),
            identity_contract=str(payload.get("identity_contract", "canonical")),
            grant_handles=[str(h) for h in payload.get("grant_handles", [])],
            trace=[
                RedactionTraceEntry.from_dict(item)
                for item in payload.get("trace", [])
            ],
        )

    def archive(self, namespace_id: str, *, app_id: str) -> NamespaceState:
        with self.lock(namespace_id):
            state = self.load(namespace_id, app_id=app_id)
            archived = NamespaceState(
                **{**state.__dict__, "status": "archived", "updated_at": int(time.time())}
            )
            self.save(archived)
            return archived

    def delete(
        self,
        namespace_id: str,
        *,
        app_id: str,
        revoke_handles: Callable[[str], None] | None = None,
    ) -> bool:
        """Delete the namespace, revoking its derived grants FIRST.

        Grants minted from this namespace are self-contained (they carry the
        mapping entries), so without revocation they would keep a deleted
        namespace's values recoverable until store TTL — the orphan-grant gap.
        Revocation happens before the namespace key is removed: a crash after
        it leaves a harmless namespace pointing at dead grants, never a deleted
        namespace with live grants. Archiving does NOT revoke (an archived
        namespace keeps its entries, so refill against it stays meaningful).
        """
        with self.lock(namespace_id):
            state = self.load(namespace_id, app_id=app_id)
            if revoke_handles is not None:
                for handle in state.grant_handles:
                    revoke_handles(handle)
            return self.store.delete(self._key(namespace_id))
