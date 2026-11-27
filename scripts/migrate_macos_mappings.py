#!/usr/bin/env python3
"""Explicitly migrate legacy PLAIN1 service mappings to macOS Keychain AES-GCM."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from tuomin_gateway.store import MappingStore, StoreEncryptionError


def _plain_count(directory: Path) -> int:
    if not directory.exists():
        return 0
    return sum(
        path.read_bytes().startswith(b"PLAIN1\n")
        for path in directory.glob("*.mapping.enc")
    )


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Migrate legacy Tuomin PLAIN1 mappings to MACAES1 in place."
    )
    parser.add_argument("mapping_dir", type=Path)
    parser.add_argument(
        "--execute",
        action="store_true",
        help="perform the migration; without this flag only a safe count is printed",
    )
    args = parser.parse_args()

    if sys.platform != "darwin":
        parser.error("this migration command is only supported on macOS")

    count = _plain_count(args.mapping_dir)
    if not args.execute:
        print(json.dumps({"mode": "dry-run", "plaintext_files": count}))
        return 0

    try:
        store = MappingStore(args.mapping_dir)
        status = store.safe_status()
        if not status["encrypted_at_rest"] or status["format_marker"] != "MACAES1":
            raise StoreEncryptionError("secure macOS mapping cipher is not active")
        migrated = store.migrate_plaintext_files()
    except (OSError, ValueError, StoreEncryptionError) as exc:
        print(json.dumps({"mode": "error", "error": str(exc)}), file=sys.stderr)
        return 1

    print(
        json.dumps(
            {
                "mode": "execute",
                "migrated_files": migrated,
                "remaining_plaintext_files": _plain_count(args.mapping_dir),
                "format_marker": "MACAES1",
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
