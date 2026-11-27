"""Issue a capability secret and its one-way registry verifier."""
import hashlib
import secrets


def generate_capability_token() -> tuple[str, str]:
    token = secrets.token_urlsafe(32)
    return token, "sha256:" + hashlib.sha256(token.encode("utf-8")).hexdigest()
