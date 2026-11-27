"""Synthetic checks for an explicitly selected local validation gateway."""
from __future__ import annotations

import argparse
import json
import os
import urllib.error
import urllib.parse
import urllib.request


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", required=True)
    args = parser.parse_args()
    parsed = urllib.parse.urlsplit(args.base_url)
    if (parsed.scheme != "http" or parsed.hostname != "127.0.0.1" or not parsed.port
            or parsed.username or parsed.password or parsed.path not in {"", "/"} or parsed.query or parsed.fragment):
        parser.error("base-url must be an explicit http://127.0.0.1:port validation endpoint")
    token = os.environ.get("TUOMIN_ADMIN_TOKEN")
    if not token:
        parser.error("validation admin credential must be provided via environment")
    base = args.base_url.rstrip("/")
    results = []

    def call(method, route, payload=None, *, raw=None, auth=False):
        body = raw if raw is not None else (json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None)
        request = urllib.request.Request(base + route, data=body, method=method)
        request.add_header("Content-Type", "text/plain" if raw is not None else "application/json")
        if auth:
            request.add_header("x-tuomin-admin-token", token)
            request.add_header("X-Requested-With", "tuomin-ui")
        try:
            with urllib.request.urlopen(request, timeout=90) as response:
                status, data = response.status, response.read()
        except urllib.error.HTTPError as exc:
            status, data = exc.code, exc.read()
        except OSError:
            return -1, {}
        try:
            return status, json.loads(data)
        except ValueError:
            return status, {"html": data.decode("utf-8", "replace")}

    def check(name, passed):
        results.append(bool(passed))
        print(f"{'PASS' if passed else 'FAIL'} {name}", flush=True)

    status, value = call("GET", "/healthz")
    check("health", status == 200 and value.get("status") == "ok")
    status, value = call("GET", "/ui/")
    check("ui", status == 200 and "f_app_notice" in value.get("html", ""))
    original = "联系人沈测试就职于星河测试公司，电话13800138000。"
    status, value = call("POST", "/redact", {"text": original, "app_id": "production"})
    masked, task = value.get("redacted_text", ""), value.get("task_id", "")
    check("redact", status == 200 and bool(task) and bool(masked) and all(x not in masked for x in ("沈测试", "星河测试公司", "13800138000")))
    status, value = call("POST", "/refill", {"text": masked, "task_id": task})
    check("exact_refill", status == 200 and value.get("status") == "ok" and value.get("text") == original)
    status, value = call("POST", "/refill", {"text": masked.replace("<", "< "), "task_id": task})
    check("altered_placeholder_blocked", status in (400, 409, 422) or (status == 200 and value.get("status") == "blocked"))
    status, value = call("POST", "/redact", {"text": original, "task_id": "synthetic-client-selected"})
    check("client_task_id_rejected", status in (400, 422))
    status, value = call("GET", "/api/v1/documents/jobs")
    check("admin_requires_token", status == 401)
    status, value = call("GET", "/api/v1/documents/jobs", auth=True)
    check("admin_valid_token", status == 200)
    aws = "AKIA" + "IOSFODNN7EXAMPLE"
    status, value = call("POST", "/redact", {"text": "测试密钥" + aws, "app_id": "production"})
    output = value.get("redacted_text", "")
    check("bare_key_masked", status == 200 and bool(output) and aws not in output and "<CREDENTIAL_" in output)
    pem = "-----BEGIN RSA PRIVATE KEY-----\nMIIBSYNTHETICPRIVATEBODY\n-----END RSA PRIVATE KEY-----"
    status, value = call("POST", "/api/v1/documents/parse?filename=synthetic.txt&app_id=production", raw=pem.encode(), auth=True)
    output = value.get("redacted_markdown", "")
    check("whole_pem_safe_egress", status == 200 and bool(output) and "PRIVATE" not in output and "<CREDENTIAL_" in output and value.get("egress", {}).get("egress_allowed") is True)
    for route, auth in (("/redact", False), ("/api/v1/documents/parse?filename=synthetic.txt&app_id=production", True)):
        status, value = call("POST", route, None if auth else {"text": pem.split("-----END")[0]}, raw=pem.split("-----END")[0].encode() if auth else None, auth=auth)
        check("incomplete_pem_blocked_" + ("document" if auth else "legacy"), status in (400, 409, 422) and "MIIBSYNTHETIC" not in json.dumps(value))
    print(f"{sum(results)}/{len(results)} checks passed")
    return 0 if all(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
