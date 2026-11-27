"""Small loopback-only HTTP client with no credential-bearing diagnostics."""
from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request


class LocalClient:
    def __init__(self, base_url: str, *, timeout: float = 10):
        parsed = urllib.parse.urlsplit(base_url)
        if parsed.scheme != "http" or parsed.hostname != "127.0.0.1" or parsed.username or parsed.password:
            raise ValueError("desktop client requires a loopback HTTP address")
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def request(self, method: str, path: str, payload: dict | None = None, *, token: str = "", admin: bool = False) -> tuple[int, dict]:
        if not path.startswith("/") or path.startswith("//"):
            raise ValueError("invalid local API path")
        headers = {"X-Requested-With": "tuomin-shell"}
        if token:
            headers["x-tuomin-admin-token" if admin else "x-tuomin-capability-token"] = token
        body = None if payload is None else json.dumps(payload).encode("utf-8")
        if body is not None:
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(self.base_url + path, data=body, headers=headers, method=method)
        try:
            with self.opener.open(request, timeout=self.timeout) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as error:
            try:
                return error.code, json.loads(error.read())
            except (ValueError, UnicodeError):
                return error.code, {"status": "error", "error": {"code": "invalid_response"}}
        except (OSError, ValueError):
            return 0, {"status": "error", "error": {"code": "gateway_unavailable"}}
