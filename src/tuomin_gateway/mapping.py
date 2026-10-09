from __future__ import annotations

import json
import os
import secrets
from collections import defaultdict
from pathlib import Path

from tuomin_gateway.platform_security import restrict_permissions
from tuomin_gateway.schemas import MappingEntry


PREFIX_BY_LABEL = {
    "DEPARTMENT": "DEPT",
    "LAND_PARCEL": "PARCEL",
    "SYSTEM_URL": "URL",
    "RISK_JUDGMENT": "RISK",
    "INTERNAL_OPINION": "OPINION",
    "NEGOTIATION_POSITION": "POSITION",
}


class PlaceholderFactory:
    def __init__(self) -> None:
        self._counters: defaultdict[str, int] = defaultdict(int)

    def next(self, label: str) -> str:
        prefix = PREFIX_BY_LABEL.get(label, label)
        self._counters[prefix] += 1
        return f"<{prefix}_{self._counters[prefix]:03d}>"

    def reserve(self, prefix: str, number: int) -> None:
        """Advance a prefix counter when hydrating a persistent mapping."""
        self._counters[prefix] = max(self._counters[prefix], number)


def save_mapping(entries: list[MappingEntry], path: str | Path) -> None:
    """Write a placeholder->original mapping as a local CLI artifact.

    NOTE: this is the offline `tuomin redact` CLI path and the file is PLAINTEXT
    JSON containing originals — it is an explicit user artifact, not the running
    service's store. The long-lived service persists mappings via
    ``store.MappingStore`` (DPAPI-encrypted at rest). Permissions are restricted
    to the owner; treat the file as sensitive and delete it when done.
    """
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    payload = [entry.to_dict() for entry in entries]
    serialized = json.dumps(payload, ensure_ascii=False, indent=2)
    temp = destination.with_name(
        f".{destination.name}.{os.getpid()}.{secrets.token_hex(4)}.tmp"
    )
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        restrict_permissions(temp, is_dir=False)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            fd = -1
            handle.write(serialized)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, destination)
        restrict_permissions(destination, is_dir=False)
    finally:
        if fd >= 0:
            os.close(fd)
        temp.unlink(missing_ok=True)


def load_mapping(path: str | Path) -> list[MappingEntry]:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    return [MappingEntry.from_dict(entry) for entry in payload]


__all__ = ["MappingEntry", "PlaceholderFactory", "load_mapping", "save_mapping"]
