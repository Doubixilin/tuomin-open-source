#!/usr/bin/env python3
"""Generate one capability token and the SHA-256 verifier stored in config."""
from __future__ import annotations

import json
import secrets

from tuomin_gateway.service.registry import hash_capability_token


def main() -> None:
    token = secrets.token_urlsafe(32)
    print(
        json.dumps(
            {
                "token": token,
                "stored_hash": hash_capability_token(token),
                "warning": "Store the token in the consuming app secret store; put only stored_hash in tuomin config.",
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
