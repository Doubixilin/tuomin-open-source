"""User-requested cancellation of slow document requests."""
from __future__ import annotations

import threading
import time

import pytest

from test_documents_api import _setup
from tuomin_gateway import cancellation


def test_scope_check_and_cancel_are_context_local():
    with cancellation.begin("req-A") as event:
        cancellation.check()  # not cancelled yet
        assert cancellation.cancel("req-A") is True and event.is_set()
        with pytest.raises(cancellation.Cancelled):
            cancellation.check()
    assert cancellation.cancel("req-A") is False  # scope is gone
    cancellation.check()  # outside any scope: never raises
    assert cancellation.cancel("../etc") is False and cancellation.cancel("") is False
    with cancellation.begin("not valid!") as event:
        assert event is None


def test_parse_request_can_be_cancelled_mid_detection(tmp_path, monkeypatch):
    client = _setup(tmp_path)
    from tuomin_gateway.service import v1_documents

    started = threading.Event()

    def slow_detection(text, detectors, profile):
        started.set()
        for _ in range(200):  # up to ~10 s unless cancelled
            cancellation.check()
            time.sleep(0.05)
        raise AssertionError("detection was not cancelled")

    monkeypatch.setattr(v1_documents, "run_detection", slow_detection)
    result = {}

    def run():
        result["response"] = client.post(
            "/api/v1/documents/parse", params={"filename": "slow.txt"},
            content="联系人手机号：13800138000\n".encode("utf-8"),
            headers={"x-tuomin-request-id": "req-slow-1"},
        )

    worker = threading.Thread(target=run)
    worker.start()
    assert started.wait(5)
    cancelled = client.post("/api/v1/documents/cancel", json={"request_id": "req-slow-1"})
    assert cancelled.status_code == 200 and cancelled.json()["cancelled"] is True
    worker.join(10)
    response = result["response"]
    assert response.status_code == 409, response.text
    assert response.json()["error"]["code"] == "cancelled"
    assert client.get("/api/v1/documents/jobs").json()["jobs"] == []  # nothing half-made
    assert client.post("/api/v1/documents/cancel", json={"request_id": "req-slow-1"}).json()["cancelled"] is False
