"""A long document job must not freeze the gateway (cancel, ledger, quit check)."""
from __future__ import annotations

import json
import socket
import threading
import time
import urllib.error
import urllib.request

import uvicorn

from tuomin_gateway import cancellation
from tuomin_gateway.service import v1_documents
from tuomin_gateway.service.app import create_app
from tuomin_gateway.service.registry import AppRegistry
from tuomin_gateway.store import MappingStore

ADMIN = "admin-token-for-concurrency-tests-0"


def _serve(tmp_path, monkeypatch):
    def localhost(host, port=0, *args, **kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", port or 0))]
    monkeypatch.setattr(socket, "getaddrinfo", localhost)
    (tmp_path / "d.json").write_text("[]", encoding="utf-8")
    config = tmp_path / "apps.json"
    config.write_text(json.dumps({"apps": {"wb": {"profile": "file_workbench", "dictionary": str(tmp_path / "d.json")}}}), encoding="utf-8")
    app = create_app(registry=AppRegistry.load(config), store=MappingStore(tmp_path / "maps"), admin_token=ADMIN)
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=0, log_level="error"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.time() + 10
    while not server.started and time.time() < deadline:
        time.sleep(0.05)
    port = server.servers[0].sockets[0].getsockname()[1]
    return server, thread, f"http://127.0.0.1:{port}"


def _request(base, method, path, body=None, headers=None, timeout=5):
    data = json.dumps(body).encode() if isinstance(body, dict) else body
    req = urllib.request.Request(base + path, data=data, method=method, headers={"x-tuomin-admin-token": ADMIN, "Content-Type": "application/json", **(headers or {})})
    started = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            return response.status, json.loads(response.read() or b"{}"), time.monotonic() - started
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read() or b"{}"), time.monotonic() - started


def test_long_parse_leaves_gateway_responsive_and_cancellable(tmp_path, monkeypatch):
    real = v1_documents.run_detection

    def slow_detection(text, detectors, profile):
        for _ in range(100):  # ~10 s of "work" that cooperatively checks for cancel
            time.sleep(0.1)
            cancellation.check()
        return real(text, detectors, profile)

    monkeypatch.setattr(v1_documents, "run_detection", slow_detection)
    monkeypatch.setattr("tuomin_gateway.detectors.ner.get_ner_detector", lambda: type("N", (), {"name": "ner", "version": "stub", "detect": lambda self, t: []})())
    server, thread, base = _serve(tmp_path, monkeypatch)
    try:
        outcome = {}

        def job():
            outcome["result"] = _request(base, "POST", "/api/v1/documents/parse?filename=a.txt&app_id=wb", "联系人张三，电话13800138000".encode(),
                                         {"Content-Type": "application/octet-stream", "X-Tuomin-Request-Id": "job-cc-1"}, timeout=30)

        worker = threading.Thread(target=job)
        worker.start()
        time.sleep(0.8)
        status, _, elapsed = _request(base, "GET", "/api/v1/documents/calls?limit=10")
        assert status == 200 and elapsed < 1.0, f"ledger blocked for {elapsed:.1f}s"
        status, body, elapsed = _request(base, "POST", "/api/v1/documents/cancel", {"request_id": "job-cc-1"})
        assert status == 200 and body["cancelled"] is True and elapsed < 1.0
        worker.join(timeout=10)
        status, body, elapsed = outcome["result"]
        assert status == 409 and body["status"] == "cancelled" and elapsed < 5.0
    finally:
        server.should_exit = True
        thread.join(timeout=5)
