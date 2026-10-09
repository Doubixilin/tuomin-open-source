"""Job directory and safe call ledger (SQLite, stdlib only).

Persistence for the local workbench: one row per file-redaction job, one row
per gateway call. Red-line rule: NO raw text, original values, absolute paths
or tokens are ever stored here — only names, counts, hashes, ids and versions.
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import threading
import time
from pathlib import Path

from tuomin_gateway.platform_security import restrict_permissions

JOB_STATUS = frozenset({"active", "completed", "failed"})
ENTRY_KINDS = frozenset({"workbench", "api", "proxy", "cli"})
_FORBIDDEN_KEYS = frozenset(
    {
        "text",
        "original",
        "original_value",
        "content",
        "path",
        "file_path",
        "absolute_path",
        "token",
        "secret",
        "plaintext",
        "body",
        "raw",
    }
)
_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_ARTIFACT_KEYS = frozenset({"type", "name", "sha256"})


class JobFieldError(ValueError):
    """Raised when a job or call field violates the safe-store contract."""


def _require_string(value: object, field: str) -> str:
    if not isinstance(value, str):
        raise JobFieldError(f"{field} must be a string")
    return value


def _validate_file_name(value: object) -> str:
    name = _require_string(value, "file_name")
    if name in {".", ".."} or "/" in name or "\\" in name:
        raise JobFieldError("file_name must be a bare name")
    return name


def _validate_sha256(value: object, field: str) -> str:
    digest = _require_string(value, field)
    if _SHA256_RE.fullmatch(digest) is None:
        raise JobFieldError(f"{field} must be 64 lowercase hexadecimal characters")
    return digest


def _validate_safe_keys(values: dict[str, object], field: str) -> None:
    for key in values:
        if key.casefold() in _FORBIDDEN_KEYS:
            raise JobFieldError(f"{field} contains a forbidden key")


def _validate_versions(value: object) -> dict[str, str]:
    if not isinstance(value, dict):
        raise JobFieldError("versions must be a dict")
    if any(not isinstance(key, str) or not isinstance(item, str) for key, item in value.items()):
        raise JobFieldError("versions keys and values must be strings")
    _validate_safe_keys(value, "versions")
    return value


def _validate_label_counts(value: object) -> dict[str, int]:
    if not isinstance(value, dict):
        raise JobFieldError("label_counts must be a dict")
    if any(
        not isinstance(key, str) or not isinstance(count, int) or isinstance(count, bool)
        for key, count in value.items()
    ):
        raise JobFieldError("label_counts keys must be strings and values must be integers")
    _validate_safe_keys(value, "label_counts")
    return value


def _validate_artifacts(value: object) -> list[dict[str, str]]:
    if not isinstance(value, list):
        raise JobFieldError("artifacts must be a list")
    for artifact in value:
        if not isinstance(artifact, dict):
            raise JobFieldError("each artifact must be a dict")
        if any(not isinstance(key, str) for key in artifact):
            raise JobFieldError("artifact keys must be strings")
        _validate_safe_keys(artifact, "artifact")
        if not set(artifact).issubset(_ARTIFACT_KEYS):
            raise JobFieldError("artifact contains an unsupported key")
        if any(not isinstance(item, str) for item in artifact.values()):
            raise JobFieldError("artifact values must be strings")
    return value


def _validate_integer(value: object, field: str, *, minimum: int = 0) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < minimum:
        raise JobFieldError(f"{field} must be an integer of at least {minimum}")
    return value


def _json_dump(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


class JobStore:
    """Owner-only SQLite job directory and value-free call ledger."""

    def __init__(self, directory: str | Path) -> None:
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        restrict_permissions(self.directory, is_dir=True)
        self.path = self.directory / "jobs.db"
        fd = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o600)
        os.close(fd)
        restrict_permissions(self.path, is_dir=False)
        self._lock = threading.Lock()
        self._connection = sqlite3.connect(self.path, check_same_thread=False)
        self._connection.row_factory = sqlite3.Row
        with self._connection:
            self._connection.execute("PRAGMA journal_mode=WAL")
            self._connection.execute(
                """
                CREATE TABLE IF NOT EXISTS jobs (
                    task_id TEXT PRIMARY KEY,
                    created_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL,
                    status TEXT NOT NULL CHECK (status IN ('active','completed','failed')),
                    file_name TEXT NOT NULL,
                    file_sha256 TEXT NOT NULL,
                    file_format TEXT NOT NULL,
                    profile TEXT NOT NULL,
                    versions_json TEXT NOT NULL,
                    label_counts_json TEXT NOT NULL DEFAULT '{}',
                    mapping_ref TEXT,
                    redacted_sha256 TEXT,
                    artifacts_json TEXT NOT NULL DEFAULT '[]',
                    error TEXT
                )
                """
            )
            # Backward-compatible migration: jobs gain the bound app id
            # (egress-gate app binding). Pre-existing rows keep working under
            # the historical default "workbench".
            columns = {
                row[1] for row in self._connection.execute("PRAGMA table_info(jobs)")
            }
            if "app_id" not in columns:
                self._connection.execute(
                    "ALTER TABLE jobs ADD COLUMN app_id TEXT NOT NULL DEFAULT 'workbench'"
                )
            # DOCX format-preserving jobs store their position ledger here
            # (positions/hashes only — never values).
            if "docx_ledger_json" not in columns:
                self._connection.execute(
                    "ALTER TABLE jobs ADD COLUMN docx_ledger_json TEXT"
                )
            self._connection.execute(
                """
                CREATE TABLE IF NOT EXISTS calls (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts INTEGER NOT NULL,
                    entry TEXT NOT NULL,
                    app_id TEXT NOT NULL,
                    task_id TEXT,
                    duration_ms INTEGER NOT NULL,
                    char_count INTEGER NOT NULL,
                    label_counts_json TEXT NOT NULL,
                    blocked INTEGER NOT NULL,
                    policy_version TEXT NOT NULL
                )
                """
            )
        for sqlite_path in (
            self.path,
            Path(f"{self.path}-wal"),
            Path(f"{self.path}-shm"),
        ):
            if sqlite_path.exists():
                restrict_permissions(sqlite_path, is_dir=False)

    def create_job(
        self,
        *,
        task_id: str,
        file_name: str,
        file_sha256: str,
        file_format: str,
        profile: str,
        versions: dict[str, str],
        mapping_ref: str | None = None,
        app_id: str = "workbench",
        docx_ledger: dict | None = None,
    ) -> None:
        task_id = _require_string(task_id, "task_id")
        file_name = _validate_file_name(file_name)
        file_sha256 = _validate_sha256(file_sha256, "file_sha256")
        file_format = _require_string(file_format, "file_format")
        profile = _require_string(profile, "profile")
        versions = _validate_versions(versions)
        app_id = _require_string(app_id, "app_id")
        if mapping_ref is not None:
            mapping_ref = _require_string(mapping_ref, "mapping_ref")
        docx_ledger_json: str | None = None
        if docx_ledger is not None:
            if not isinstance(docx_ledger, dict):
                raise JobFieldError("docx_ledger must be a dict")
            _validate_safe_keys(docx_ledger, "docx_ledger")
            docx_ledger_json = _json_dump(docx_ledger)
        now = int(time.time())
        try:
            with self._lock, self._connection:
                self._connection.execute(
                    """
                    INSERT INTO jobs (
                        task_id, created_at, updated_at, status, file_name,
                        file_sha256, file_format, profile, versions_json,
                        mapping_ref, app_id, docx_ledger_json
                    ) VALUES (?, ?, ?, 'active', ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        task_id,
                        now,
                        now,
                        file_name,
                        file_sha256,
                        file_format,
                        profile,
                        _json_dump(versions),
                        mapping_ref,
                        app_id,
                        docx_ledger_json,
                    ),
                )
        except sqlite3.IntegrityError as exc:
            raise JobFieldError("task_id already exists") from exc

    def complete_job(
        self,
        task_id: str,
        *,
        label_counts: dict[str, int],
        redacted_sha256: str,
        artifacts: list[dict],
    ) -> None:
        task_id = _require_string(task_id, "task_id")
        label_counts = _validate_label_counts(label_counts)
        redacted_sha256 = _validate_sha256(redacted_sha256, "redacted_sha256")
        artifacts = _validate_artifacts(artifacts)
        with self._lock, self._connection:
            cursor = self._connection.execute(
                """
                UPDATE jobs
                SET status = 'completed', label_counts_json = ?,
                    redacted_sha256 = ?, artifacts_json = ?, error = NULL,
                    updated_at = ?
                WHERE task_id = ?
                """,
                (
                    _json_dump(label_counts),
                    redacted_sha256,
                    _json_dump(artifacts),
                    int(time.time()),
                    task_id,
                ),
            )
            if cursor.rowcount == 0:
                raise JobFieldError("unknown task_id")

    def fail_job(self, task_id: str, *, error: str) -> None:
        task_id = _require_string(task_id, "task_id")
        error = _require_string(error, "error")
        with self._lock, self._connection:
            cursor = self._connection.execute(
                """
                UPDATE jobs
                SET status = 'failed', error = ?, updated_at = ?
                WHERE task_id = ?
                """,
                (error, int(time.time()), task_id),
            )
            if cursor.rowcount == 0:
                raise JobFieldError("unknown task_id")

    def get_job(self, task_id: str) -> dict | None:
        task_id = _require_string(task_id, "task_id")
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM jobs WHERE task_id = ?", (task_id,)
            ).fetchone()
            return self._decode_job(row) if row is not None else None

    def list_jobs(self, *, limit: int = 50, offset: int = 0) -> list[dict]:
        limit = _validate_integer(limit, "limit")
        offset = _validate_integer(offset, "offset")
        with self._lock:
            rows = self._connection.execute(
                "SELECT * FROM jobs ORDER BY created_at DESC, rowid DESC LIMIT ? OFFSET ?",
                (limit, offset),
            ).fetchall()
            return [self._decode_job(row) for row in rows]

    def delete_job(self, task_id: str) -> dict | None:
        task_id = _require_string(task_id, "task_id")
        with self._lock, self._connection:
            row = self._connection.execute(
                "SELECT * FROM jobs WHERE task_id = ?", (task_id,)
            ).fetchone()
            if row is None:
                return None
            self._connection.execute("DELETE FROM jobs WHERE task_id = ?", (task_id,))
            return self._decode_job(row)

    def record_call(
        self,
        *,
        entry: str,
        app_id: str,
        duration_ms: int,
        char_count: int,
        label_counts: dict[str, int],
        blocked: bool,
        policy_version: str,
        task_id: str | None = None,
    ) -> None:
        if entry not in ENTRY_KINDS:
            raise JobFieldError("invalid entry kind")
        app_id = _require_string(app_id, "app_id")
        duration_ms = _validate_integer(duration_ms, "duration_ms")
        char_count = _validate_integer(char_count, "char_count")
        label_counts = _validate_label_counts(label_counts)
        if not isinstance(blocked, bool):
            raise JobFieldError("blocked must be a bool")
        policy_version = _require_string(policy_version, "policy_version")
        if task_id is not None:
            task_id = _require_string(task_id, "task_id")
        with self._lock, self._connection:
            self._connection.execute(
                """
                INSERT INTO calls (
                    ts, entry, app_id, task_id, duration_ms, char_count,
                    label_counts_json, blocked, policy_version
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    int(time.time()),
                    entry,
                    app_id,
                    task_id,
                    duration_ms,
                    char_count,
                    _json_dump(label_counts),
                    int(blocked),
                    policy_version,
                ),
            )

    def list_calls(self, *, limit: int = 100, entry: str | None = None) -> list[dict]:
        limit = _validate_integer(limit, "limit")
        if entry is not None and entry not in ENTRY_KINDS:
            raise JobFieldError("invalid entry kind")
        with self._lock:
            if entry is None:
                rows = self._connection.execute(
                    "SELECT * FROM calls ORDER BY ts DESC, id DESC LIMIT ?", (limit,)
                ).fetchall()
            else:
                rows = self._connection.execute(
                    "SELECT * FROM calls WHERE entry = ? ORDER BY ts DESC, id DESC LIMIT ?",
                    (entry, limit),
                ).fetchall()
            return [self._decode_call(row) for row in rows]

    def close(self) -> None:
        """Close the underlying SQLite connection."""
        with self._lock:
            self._connection.close()

    @staticmethod
    def _decode_job(row: sqlite3.Row) -> dict:
        result = dict(row)
        for field in ("versions_json", "label_counts_json", "artifacts_json"):
            result[field] = json.loads(result[field])
        if result.get("docx_ledger_json"):
            result["docx_ledger_json"] = json.loads(result["docx_ledger_json"])
        return result

    @staticmethod
    def _decode_call(row: sqlite3.Row) -> dict:
        result = dict(row)
        result["label_counts_json"] = json.loads(result["label_counts_json"])
        result["blocked"] = bool(result["blocked"])
        return result
