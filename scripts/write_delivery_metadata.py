#!/usr/bin/env python3
"""Write non-secret build-time delivery metadata."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import secrets
import sys


def write_delivery_metadata(output: Path, customer: str) -> None:
    output = output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from package_release import source_provenance

    payload = {
        "source": source_provenance(),
        "customer": customer,
        "built_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "product": "tuomin-workbench",
    }
    temporary = output.with_name(
        f".{output.name}.{os.getpid()}.{secrets.token_hex(4)}.tmp"
    )
    try:
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, output)
    finally:
        temporary.unlink(missing_ok=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--customer", required=True)
    args = parser.parse_args()
    customer = args.customer.strip()
    if not customer:
        raise SystemExit("customer must not be empty")
    write_delivery_metadata(args.output, customer)
    print(f"delivery metadata ready: {args.output.resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
