"""Tests for tuomin_gateway.jobs (job directory + safe call ledger)."""

from __future__ import annotations

import os
import stat
import threading

import pytest

from tuomin_gateway.jobs import JobFieldError, JobStore

_HASH_A = "a" * 64
_HASH_B = "b" * 64


def _create(store: JobStore, task_id: str, file_name: str = "contract.docx") -> None:
    store.create_job(
        task_id=task_id,
        file_name=file_name,
        file_sha256=_HASH_A,
        file_format="docx",
        profile="default",
        versions={"gateway": "1", "rules": "2"},
        mapping_ref="mapping-1",
    )


def test_job_round_trip_complete_fail_list_and_delete(tmp_path) -> None:
    store = JobStore(tmp_path / "jobs")
    _create(store, "task-complete")
    _create(store, "task-failed", "other.pdf")

    active = store.get_job("task-complete")
    assert active is not None
    assert active["status"] == "active"
    assert active["versions_json"] == {"gateway": "1", "rules": "2"}
    assert active["label_counts_json"] == {}
    assert active["artifacts_json"] == []

    artifacts = [{"type": "document", "name": "redacted.docx", "sha256": _HASH_B}]
    store.complete_job(
        "task-complete",
        label_counts={"PERSON": 3},
        redacted_sha256=_HASH_B,
        artifacts=artifacts,
    )
    store.fail_job("task-failed", error="processing failed")

    completed = store.get_job("task-complete")
    assert completed is not None
    assert completed["status"] == "completed"
    assert completed["label_counts_json"] == {"PERSON": 3}
    assert completed["redacted_sha256"] == _HASH_B
    assert completed["artifacts_json"] == artifacts

    failed = store.get_job("task-failed")
    assert failed is not None
    assert failed["status"] == "failed"
    assert failed["error"] == "processing failed"
    assert {row["task_id"] for row in store.list_jobs()} == {
        "task-complete",
        "task-failed",
    }
    assert len(store.list_jobs(limit=1, offset=1)) == 1

    deleted = store.delete_job("task-complete")
    assert deleted == completed
    assert store.get_job("task-complete") is None
    assert store.delete_job("missing") is None
    store.close()


def test_duplicate_and_unknown_task_ids_are_rejected(tmp_path) -> None:
    store = JobStore(tmp_path)
    _create(store, "duplicate")
    with pytest.raises(JobFieldError):
        _create(store, "duplicate")
    with pytest.raises(JobFieldError):
        store.complete_job(
            "missing", label_counts={}, redacted_sha256=_HASH_B, artifacts=[]
        )
    with pytest.raises(JobFieldError):
        store.fail_job("missing", error="safe failure")
    store.close()


@pytest.mark.parametrize("file_name", ["folder/file.docx", "folder\\file.docx", ".", ".."])
def test_file_name_must_be_bare(tmp_path, file_name) -> None:
    store = JobStore(tmp_path)
    with pytest.raises(JobFieldError):
        _create(store, "bad-name", file_name)
    store.close()


def test_red_line_fields_are_rejected(tmp_path) -> None:
    store = JobStore(tmp_path)
    with pytest.raises(JobFieldError):
        store.create_job(
            task_id="bad-hash",
            file_name="file.txt",
            file_sha256="A" * 64,
            file_format="txt",
            profile="default",
            versions={"gateway": "1"},
        )

    _create(store, "valid")
    with pytest.raises(JobFieldError):
        store.complete_job(
            "valid",
            label_counts={},
            redacted_sha256=_HASH_B,
            artifacts=[{"type": "document", "path": "/private/value"}],
        )
    with pytest.raises(JobFieldError):
        store.complete_job(
            "valid",
            label_counts={"PERSON": "one"},
            redacted_sha256=_HASH_B,
            artifacts=[],
        )
    with pytest.raises(JobFieldError):
        store.record_call(
            entry="unknown",
            app_id="app",
            duration_ms=1,
            char_count=2,
            label_counts={},
            blocked=False,
            policy_version="1",
        )
    store.close()


def test_owner_only_permissions(tmp_path) -> None:
    directory = tmp_path / "private-jobs"
    store = JobStore(directory)
    assert stat.S_IMODE(os.stat(directory).st_mode) == 0o700
    assert stat.S_IMODE(os.stat(directory / "jobs.db").st_mode) == 0o600
    store.close()


def test_concurrent_call_recording_and_entry_filter(tmp_path) -> None:
    store = JobStore(tmp_path)
    errors = []

    def record(entry: str) -> None:
        try:
            for index in range(200):
                store.record_call(
                    entry=entry,
                    app_id="local-app",
                    task_id=f"task-{index}",
                    duration_ms=index,
                    char_count=index * 2,
                    label_counts={"PERSON": index % 3},
                    blocked=index % 2 == 0,
                    policy_version="policy-1",
                )
        except Exception as exc:  # pragma: no cover - asserted through the list
            errors.append(exc)

    threads = [
        threading.Thread(target=record, args=("api",)),
        threading.Thread(target=record, args=("cli",)),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []
    calls = store.list_calls(limit=500)
    assert len(calls) == 400
    assert all(isinstance(call["label_counts_json"], dict) for call in calls)
    assert all(isinstance(call["blocked"], bool) for call in calls)
    api_calls = store.list_calls(limit=500, entry="api")
    assert len(api_calls) == 200
    assert {call["entry"] for call in api_calls} == {"api"}
    store.close()
