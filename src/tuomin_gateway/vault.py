"""Opaque mapping grants and safe refill audit persistence."""
from __future__ import annotations

import hashlib
import json
import secrets
import time
from dataclasses import dataclass
from pathlib import Path

from tuomin_gateway.audit import AUDIT_STREAM_REFILL, AuditLog
from tuomin_gateway.schemas import MappingEntry
from tuomin_gateway.store import MappingStore


@dataclass(frozen=True)
class MappingGrant:
    handle: str
    app_id: str
    scope: str
    entries: list[MappingEntry]
    expected_counts: dict[str, int]
    created_at: int


class MappingGrantMismatch(PermissionError):
    """The handle exists but is not bound to the requesting app."""


class MappingVault:
    def __init__(self, store: MappingStore, audit_directory: str | Path | None = None) -> None:
        self.store = store
        self.audit_directory = Path(audit_directory or store.directory / "audit")
        self.audit_log = AuditLog(self.audit_directory)

    @staticmethod
    def _key(handle: str) -> str:
        return f"mapping-grant:{handle}"

    def create(
        self,
        *,
        app_id: str,
        scope: str,
        entries: list[MappingEntry],
        expected_counts: dict[str, int],
    ) -> str:
        handle = f"mh_{secrets.token_urlsafe(32)}"
        payload = {
            "version": 1,
            "app_id": app_id,
            "scope": scope,
            "created_at": int(time.time()),
            "expected_counts": expected_counts,
            "entries": [entry.to_dict() for entry in entries],
        }
        self.store.save_payload(
            self._key(handle), json.dumps(payload, ensure_ascii=False).encode("utf-8")
        )
        return handle

    def load(self, handle: str, *, app_id: str) -> MappingGrant:
        if not isinstance(handle, str) or not handle.startswith("mh_"):
            raise FileNotFoundError("mapping handle")
        payload = json.loads(self.store.load_payload(self._key(handle)).decode("utf-8"))
        if payload.get("app_id") != app_id:
            raise MappingGrantMismatch("mapping handle is bound to another app")
        return MappingGrant(
            handle=handle,
            app_id=app_id,
            scope=str(payload["scope"]),
            entries=[MappingEntry.from_dict(item) for item in payload["entries"]],
            expected_counts={str(k): int(v) for k, v in payload["expected_counts"].items()},
            created_at=int(payload["created_at"]),
        )

    def write_refill_audit(
        self,
        *,
        app_id: str,
        handle: str,
        contract: str,
        status: str,
        error_types: list[str],
        restored_count: int,
    ) -> None:
        try:
            self.audit_log.write(
                AUDIT_STREAM_REFILL,
                {
                    "event": "trusted_refill",
                    "app_id": app_id,
                    "mapping_handle_hash": "sha256:"
                    + hashlib.sha256(handle.encode()).hexdigest(),
                    "contract": contract,
                    "status": status,
                    "error_types": list(error_types),
                    "restored_count": restored_count,
                    "raw_values_included": False,
                },
            )
        except OSError:
            pass
