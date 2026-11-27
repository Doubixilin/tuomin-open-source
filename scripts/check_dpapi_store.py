#!/usr/bin/env python3
"""Verify the MappingStore at-rest marker on the current platform.

Windows should write DPAPI1 and macOS should write Keychain-backed MACAES1.
Linux retains the documented development PLAIN1 fallback. The output is a safe
status summary only; it never prints mapping contents or key material.
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from tuomin_gateway.schemas import MappingEntry  # noqa: E402
from tuomin_gateway.store import MappingStore, StoreEncryptionError  # noqa: E402


def _expected_magic() -> str:
    if sys.platform == "win32":
        return "DPAPI1"
    if sys.platform == "darwin":
        return "MACAES1"
    return "PLAIN1"


def main() -> int:
    try:
        with tempfile.TemporaryDirectory(prefix="tuomin_dpapi_check_") as tmp:
            store = MappingStore(tmp, ttl_seconds=3600)
            status = store.safe_status()
            store.save(
                "dpapi_check",
                [MappingEntry("<ORG_001>", "ORG", "Synthetic DPAPI Entity", "sha256:self-check")],
            )
            path = next(Path(tmp).glob("*.mapping.enc"))
            raw = path.read_bytes()
    except StoreEncryptionError as exc:
        payload = {
            "status": "error",
            "platform": sys.platform,
            "expected_magic": _expected_magic(),
            "actual_magic": None,
            "mapping_store": {"platform_mode": "unavailable"},
            "message": str(exc).split(":", 1)[0],
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 1

    magic = raw.split(b"\n", 1)[0].decode("ascii", errors="replace")
    expected = _expected_magic()
    payload = {
        "status": "ok" if magic == expected else "error",
        "platform": sys.platform,
        "expected_magic": expected,
        "actual_magic": magic,
        "mapping_store": status,
        "note": "DPAPI is expected on Windows, Keychain AES-GCM on macOS, and PLAIN1 only on Linux development hosts.",
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0 if magic == expected else 1


if __name__ == "__main__":
    raise SystemExit(main())
