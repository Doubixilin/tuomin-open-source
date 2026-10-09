"""Tests for the workbench document endpoints (TestClient integration)."""
from __future__ import annotations

import copy
import json

from fastapi.testclient import TestClient

from tuomin_gateway.service.app import create_app
from tuomin_gateway.service.registry import AppRegistry
from tuomin_gateway.store import MappingStore


_TOKEN = "test-token"
_HEADERS = {"x-tuomin-admin-token": _TOKEN}
_SECRET = "13800138000"


def _setup(tmp_path):
    config = tmp_path / "apps.json"
    config.write_text(json.dumps({"apps": {}}), encoding="utf-8")
    app = create_app(
        registry=AppRegistry.load(config),
        store=MappingStore(tmp_path / "maps"),
        session_ttl=3600,
        admin_token=_TOKEN,
    )
    client = TestClient(app, base_url="http://localhost")
    client.headers.update(_HEADERS)
    return client


def _parse(client: TestClient, text: str = f"联系人手机号：{_SECRET}\n") -> dict:
    response = client.post(
        "/api/v1/documents/parse",
        params={"filename": "sample.txt"},
        content=text.encode("utf-8"),
    )
    assert response.status_code == 200, response.text
    return response.json()


def _package(client: TestClient, task_id: str, passphrase: str = "correct horse") -> dict:
    response = client.post(
        f"/api/v1/documents/{task_id}/package",
        json={"passphrase": passphrase},
    )
    assert response.status_code == 200, response.text
    return response.json()["package"]


def test_txt_complete_round_trip_and_audit(tmp_path):
    client = _setup(tmp_path)
    parsed = _parse(client)
    serialized = json.dumps(parsed, ensure_ascii=False)
    assert _SECRET not in serialized
    assert "original_value" not in serialized
    assert parsed["file_name"] == "sample.txt"
    assert parsed["file_format"] == "txt"
    assert "<" in parsed["redacted_markdown"]

    package = _package(client, parsed["task_id"])
    refill = client.post(
        "/api/v1/documents/refill",
        json={
            "redacted_markdown": parsed["redacted_markdown"],
            "package": package,
            "passphrase": "correct horse",
        },
    )
    assert refill.status_code == 200
    assert refill.json()["status"] == "ok"
    assert refill.json()["mode"] == "exact"
    assert _SECRET in refill.json()["restored_markdown"]

    audit = client.get("/admin/audit/workbench")
    assert audit.status_code == 200
    assert any(
        event.get("event") == "package_export" and event.get("task_id") == parsed["task_id"]
        for event in audit.json()["events"]
    )


def test_every_document_endpoint_requires_admin_token(tmp_path):
    client = _setup(tmp_path)
    no_token = {"x-tuomin-admin-token": ""}
    assert client.post(
        "/api/v1/documents/parse?filename=a.txt", content=b"hello", headers=no_token
    ).status_code == 401
    assert client.post(
        "/api/v1/documents/unknown/package", json={"passphrase": "x"}, headers=no_token
    ).status_code == 401
    assert client.post(
        "/api/v1/documents/refill",
        json={"redacted_markdown": "x", "package": {}, "passphrase": "x"},
        headers=no_token,
    ).status_code == 401


def test_wrong_passphrase_and_bound_job_tamper_are_rejected(tmp_path):
    client = _setup(tmp_path)
    first = _parse(client)
    package = _package(client, first["task_id"])

    wrong = client.post(
        "/api/v1/documents/refill",
        json={
            "redacted_markdown": first["redacted_markdown"],
            "package": package,
            "passphrase": "wrong",
        },
    )
    assert wrong.status_code == 403

    second = _parse(client, "备用联系人手机号：13900139000\n")
    rebound = copy.deepcopy(package)
    rebound["binding"]["job_id"] = second["task_id"]
    mismatch = client.post(
        "/api/v1/documents/refill",
        json={
            "redacted_markdown": first["redacted_markdown"],
            "package": rebound,
            "passphrase": "correct horse",
        },
    )
    assert mismatch.status_code == 403


def test_edited_refill_requires_confirmation(tmp_path):
    client = _setup(tmp_path)
    parsed = _parse(client)
    package = _package(client, parsed["task_id"])
    edited = parsed["redacted_markdown"] + "\n补充说明。\n"

    pending = client.post(
        "/api/v1/documents/refill",
        json={
            "redacted_markdown": edited,
            "package": package,
            "passphrase": "correct horse",
        },
    )
    assert pending.status_code == 200
    assert pending.json()["status"] == "needs_confirmation"
    assert pending.json()["mode"] == "edited"

    confirmed = client.post(
        "/api/v1/documents/refill",
        json={
            "redacted_markdown": edited,
            "package": package,
            "passphrase": "correct horse",
            "confirm_edited": True,
        },
    )
    assert confirmed.status_code == 200
    assert confirmed.json()["status"] == "ok"
    assert confirmed.json()["mode"] == "edited"
    assert _SECRET in confirmed.json()["restored_markdown"]
    assert "补充说明" in confirmed.json()["restored_markdown"]


def test_unknown_placeholder_blocks_refill(tmp_path):
    client = _setup(tmp_path)
    parsed = _parse(client)
    package = _package(client, parsed["task_id"])
    response = client.post(
        "/api/v1/documents/refill",
        json={
            "redacted_markdown": parsed["redacted_markdown"] + "\n<ORG_999>\n",
            "package": package,
            "passphrase": "correct horse",
            "confirm_edited": True,
        },
    )
    assert response.status_code == 422
    assert response.json()["status"] == "blocked"
    assert "unknown_placeholder" in response.json()["error_types"]


def test_unsupported_extension_and_oversize_body(tmp_path, monkeypatch):
    client = _setup(tmp_path)
    unsupported = client.post(
        "/api/v1/documents/parse",
        params={"filename": "sample.zip"},
        content=b"not a real archive",
    )
    assert unsupported.status_code == 415

    garbage_pdf = client.post(
        "/api/v1/documents/parse",
        params={"filename": "sample.pdf"},
        content=b"not a pdf",
    )
    import importlib.util
    expected = 422 if importlib.util.find_spec("pymupdf") is not None else 415
    assert garbage_pdf.status_code == expected

    monkeypatch.setenv("TUOMIN_WORKBENCH_MAX_BYTES", "3")
    oversized = client.post(
        "/api/v1/documents/parse",
        params={"filename": "sample.txt"},
        content=b"four",
    )
    assert oversized.status_code == 413


def test_pdf_without_optional_dependency_is_explicitly_unavailable(tmp_path, monkeypatch):
    import builtins
    original_import = builtins.__import__

    def without_pdf(name, *args, **kwargs):
        if name == "pymupdf":
            raise ImportError("synthetic missing optional dependency")
        return original_import(name, *args, **kwargs)

    client = _setup(tmp_path)
    monkeypatch.setattr(builtins, "__import__", without_pdf)
    response = client.post("/api/v1/documents/parse",
                           params={"filename": "synthetic.pdf"}, content=b"synthetic")
    assert response.status_code == 415
    assert response.json()["error"]["code"] == "unsupported_format"
    assert "THIRD_PARTY_NOTICES.md" in response.json()["error"]["message"]


def test_delete_job_cascades_to_mapping_grant(tmp_path):
    client = _setup(tmp_path)
    parsed = _parse(client)
    task_id = parsed["task_id"]
    job = client.app.state.job_store.get_job(task_id)
    mapping_ref = job["mapping_ref"]

    response = client.delete(f"/api/v1/documents/jobs/{task_id}")
    assert response.status_code == 200, response.text

    # job row gone; package export now impossible; grant file removed
    assert client.app.state.job_store.get_job(task_id) is None
    again = client.post(f"/api/v1/documents/{task_id}/package", json={"passphrase": "x"})
    assert again.status_code == 404
    store = MappingStore(tmp_path / "maps")
    import pytest as _pytest  # local import to keep header tidy
    with _pytest.raises(FileNotFoundError):
        store.load_payload(f"mapping-grant:{mapping_ref}")

    # second delete is a clean 404, and the deletion itself was audited
    assert client.delete(f"/api/v1/documents/jobs/{task_id}").status_code == 404
    audit = client.get("/admin/audit/workbench").json()
    assert any(e.get("event") == "job_delete" and e.get("task_id") == task_id for e in audit["events"])


# --- egress gate: app binding + outbound clearance decision -------------------

import pytest

from tuomin_gateway.jobs import JobStore


def _setup_with_apps(tmp_path, apps: dict):
    config = tmp_path / "apps.json"
    config.write_text(json.dumps({"apps": apps}, ensure_ascii=False), encoding="utf-8")
    app = create_app(
        registry=AppRegistry.load(config),
        store=MappingStore(tmp_path / "maps"),
        session_ttl=3600,
        admin_token=_TOKEN,
    )
    client = TestClient(app, base_url="http://localhost")
    client.headers.update(_HEADERS)
    return client


@pytest.fixture
def stub_ner(monkeypatch):
    """Hermetic active NER so readiness is not degraded regardless of host."""

    class _StubNer:
        name = "ner"
        version = "stub"

        def detect(self, text):
            return []

    monkeypatch.setattr(
        "tuomin_gateway.detectors.ner.get_ner_detector", lambda: _StubNer()
    )


def _bound_client(tmp_path, dictionary_entries):
    dictionary = tmp_path / "dict.json"
    dictionary.write_text(
        json.dumps(dictionary_entries, ensure_ascii=False), encoding="utf-8"
    )
    return _setup_with_apps(
        tmp_path, {"wb_docs": {"profile": "kb", "dictionary": str(dictionary)}}
    )


def test_parse_unbound_is_preview_only(tmp_path):
    client = _setup(tmp_path)
    parsed = _parse(client)

    egress = parsed["egress"]
    assert egress["egress_allowed"] is False
    assert any(r["code"] == "app_not_bound" for r in egress["reasons"])
    assert egress["dictionary"] == {"bound": False, "version": None}
    assert egress["disclaimer"]
    # local preview is NOT blocked by the egress gate
    assert parsed["redacted_markdown"]
    assert _SECRET not in parsed["redacted_markdown"]


def test_parse_unknown_app_fails_closed(tmp_path):
    client = _setup(tmp_path)
    response = client.post(
        "/api/v1/documents/parse",
        params={"filename": "a.txt", "app_id": "ghost"},
        content=b"hello",
    )
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "unknown_app"
    assert response.json()["egress_allowed"] is False


def test_parse_bound_app_uses_production_dictionary(tmp_path, stub_ner):
    client = _bound_client(
        tmp_path,
        [{"canonical_value": "示例建设单位A", "aliases": [],
          "label": "ORG", "status": "active"}],
    )
    response = client.post(
        "/api/v1/documents/parse",
        params={"filename": "a.txt", "app_id": "wb_docs"},
        content="发包方：示例建设单位A\n".encode("utf-8"),
    )
    assert response.status_code == 200, response.text
    parsed = response.json()

    # the app's production dictionary actually participates in redaction
    assert "示例建设单位A" not in parsed["redacted_markdown"]
    assert "<ORG_" in parsed["redacted_markdown"]

    egress = parsed["egress"]
    assert egress["egress_allowed"] is True, egress["reasons"]
    assert egress["dictionary"]["bound"] is True
    assert egress["dictionary"]["version"]
    assert egress["detectors"]["degraded"] is False
    assert egress["residual_scan"]["status"] == "clean"


def test_parse_bound_app_broken_dictionary_fails_closed(tmp_path, stub_ner):
    broken = tmp_path / "broken.json"
    broken.write_text("{ not json", encoding="utf-8")
    client = _setup_with_apps(
        tmp_path, {"wb_docs": {"profile": "kb", "dictionary": str(broken)}}
    )
    response = client.post(
        "/api/v1/documents/parse",
        params={"filename": "a.txt", "app_id": "wb_docs"},
        content=b"hello",
    )
    assert response.status_code == 503
    assert response.json()["egress_allowed"] is False


def test_parse_aws_key_masked_by_rule_layer(tmp_path, stub_ner):
    # Bare secret rules (2026.09) mask AWS-style keys at the RULE layer now:
    # the key never survives into the redacted preview at all. The key is
    # assembled at runtime so no secret-shaped literal ever sits in the repo.
    fake_key = "AKIA" + "0123456789" + "ABCDEF"
    client = _bound_client(tmp_path, [])
    response = client.post(
        "/api/v1/documents/parse",
        params={"filename": "a.txt", "app_id": "wb_docs"},
        content=f"密钥 {fake_key} 妥善保管\n".encode("utf-8"),
    )
    assert response.status_code == 200, response.text
    parsed = response.json()

    egress = parsed["egress"]
    assert egress["egress_allowed"] is True
    assert egress["residual_scan"]["status"] == "clean"
    assert fake_key not in parsed["redacted_markdown"]
    assert "<CREDENTIAL_001>" in parsed["redacted_markdown"]


def test_parse_residual_secret_blocks_egress(tmp_path, stub_ner):
    # A credential form with NO rule-layer cue (pwd= is not in token_like)
    # survives redaction; the guard's residual scan on the redacted text must
    # block egress. The value is synthetic and high-entropy by construction.
    fake_value = "Kj8mQ2vX9zL4pR"
    client = _bound_client(tmp_path, [])
    response = client.post(
        "/api/v1/documents/parse",
        params={"filename": "a.txt", "app_id": "wb_docs"},
        content=f"pwd={fake_value} 妥善保管\n".encode("utf-8"),
    )
    assert response.status_code == 200, response.text
    parsed = response.json()

    egress = parsed["egress"]
    assert egress["egress_allowed"] is False
    assert egress["residual_scan"]["status"] in {"alert", "block"}
    assert any(
        r["code"] in {"residual_scan_alert", "residual_scan_block"}
        for r in egress["reasons"]
    )
    assert fake_value in parsed["redacted_markdown"]  # preview only


def test_bound_app_package_refill_round_trip(tmp_path, stub_ner):
    client = _bound_client(
        tmp_path,
        [{"canonical_value": "示例建设单位A", "aliases": [],
          "label": "ORG", "status": "active"}],
    )
    parsed = client.post(
        "/api/v1/documents/parse",
        params={"filename": "a.txt", "app_id": "wb_docs"},
        content="发包方：示例建设单位A\n".encode("utf-8"),
    ).json()
    package = _package(client, parsed["task_id"])
    refill = client.post(
        "/api/v1/documents/refill",
        json={
            "redacted_markdown": parsed["redacted_markdown"],
            "package": package,
            "passphrase": "correct horse",
        },
    )
    assert refill.status_code == 200
    assert "示例建设单位A" in refill.json()["restored_markdown"]


def test_jobs_db_legacy_schema_gains_app_id(tmp_path):
    import sqlite3

    db_dir = tmp_path / "maps"
    db_dir.mkdir()
    conn = sqlite3.connect(db_dir / "jobs.db")
    conn.execute(
        """
        CREATE TABLE jobs (
            task_id TEXT PRIMARY KEY, created_at INTEGER NOT NULL,
            updated_at INTEGER NOT NULL, status TEXT NOT NULL,
            file_name TEXT NOT NULL, file_sha256 TEXT NOT NULL,
            file_format TEXT NOT NULL, profile TEXT NOT NULL,
            versions_json TEXT NOT NULL,
            label_counts_json TEXT NOT NULL DEFAULT '{}',
            mapping_ref TEXT, redacted_sha256 TEXT,
            artifacts_json TEXT NOT NULL DEFAULT '[]', error TEXT
        )
        """
    )
    conn.execute(
        "INSERT INTO jobs (task_id, created_at, updated_at, status, file_name,"
        " file_sha256, file_format, profile, versions_json)"
        " VALUES ('job-legacy', 1, 1, 'completed', 'a.txt', ?, 'txt',"
        " 'file_workbench', '{}')",
        ("0" * 64,),
    )
    conn.commit()
    conn.close()

    store = JobStore(db_dir)
    try:
        job = store.get_job("job-legacy")
        assert job is not None
        assert job["app_id"] == "workbench"
    finally:
        store.close()


# --- AI answer refill (answer mode, plan §9.2) --------------------------------

def _answer_fixture(client):
    """Parse a two-entity document and export its recovery package."""
    parsed = _parse(client, f"联系人手机号：{_SECRET}，备用联系人手机号：13900139000\n")
    package = _package(client, parsed["task_id"])
    placeholders = sorted(package["binding"]["expected_counts"])
    return parsed, package, placeholders


def _refill_answer(client, package, answer, passphrase="correct horse", confirm_empty=False):
    return client.post(
        "/api/v1/documents/refill-answer",
        json={
            "answer_markdown": answer,
            "package": package,
            "passphrase": passphrase,
            "confirm_empty": confirm_empty,
        },
    )


def test_answer_refill_subset_and_repeated_placeholders(tmp_path):
    client = _setup(tmp_path)
    _, package, placeholders = _answer_fixture(client)
    first = placeholders[0]

    # subset (only one of two placeholders) + legitimate repetition
    resp = _refill_answer(client, package, f"结论：拨打 {first}，再拨 {first} 确认。")
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["status"] == "ok"
    assert data["mode"] == "answer"
    assert data["restored_markdown"].count(_SECRET) == 2
    summary = data["summary"]
    assert summary["restored_count"] == 2
    assert summary["matched"] == [
        {"placeholder": first, "label": "CONTACT", "count": 2}
    ]
    assert summary["unused"] == placeholders[1:]


def test_answer_refill_unknown_placeholder_blocks(tmp_path):
    client = _setup(tmp_path)
    _, package, _ = _answer_fixture(client)
    resp = _refill_answer(client, package, "请见 <ORG_999> 的说明。")
    assert resp.status_code == 422
    assert "unknown_placeholder" in resp.json()["error_types"]


def test_answer_refill_altered_placeholder_blocks(tmp_path):
    client = _setup(tmp_path)
    _, package, _ = _answer_fixture(client)
    resp = _refill_answer(client, package, "请见 <org_001> 的说明。")  # lowercase = altered
    assert resp.status_code == 422
    assert "altered_placeholder" in resp.json()["error_types"]


def test_answer_refill_zero_hit_requires_confirmation(tmp_path):
    client = _setup(tmp_path)
    _, package, _ = _answer_fixture(client)

    resp = _refill_answer(client, package, "这段回答没有引用任何占位符。")
    assert resp.status_code == 200
    assert resp.json()["status"] == "needs_confirmation"
    assert resp.json()["reason"] == "no_placeholder_hit"
    assert "restored_markdown" not in resp.json()

    confirmed = _refill_answer(
        client, package, "这段回答没有引用任何占位符。", confirm_empty=True
    )
    assert confirmed.status_code == 200
    assert confirmed.json()["status"] == "ok"
    assert confirmed.json()["restored_markdown"] == "这段回答没有引用任何占位符。"


def test_answer_refill_wrong_passphrase_rejected(tmp_path):
    client = _setup(tmp_path)
    _, package, placeholders = _answer_fixture(client)
    resp = _refill_answer(client, package, placeholders[0], passphrase="wrong")
    assert resp.status_code == 403


def test_answer_refill_response_and_audit_carry_no_values(tmp_path):
    client = _setup(tmp_path)
    _, package, placeholders = _answer_fixture(client)
    resp = _refill_answer(client, package, f"拨打 {placeholders[0]}。")
    assert resp.status_code == 200
    # summary/diff expose placeholder names + labels + counts only: the
    # original value must not appear outside the restored text itself
    summary_blob = json.dumps(resp.json()["summary"], ensure_ascii=False)
    assert _SECRET not in summary_blob

    audit = client.get("/admin/audit/trusted-refill")
    assert audit.status_code == 200
    answer_events = [
        e for e in audit.json()["events"] if e.get("contract") == "workbench:answer"
    ]
    assert answer_events
    blob = json.dumps(answer_events, ensure_ascii=False)
    assert _SECRET not in blob
    assert "拨打" not in blob


# --- trusted mapping unlock (trusted display, plan §9.3) ----------------------

def _unlock(client, package, passphrase="correct horse"):
    return client.post(
        "/api/v1/documents/unlock-mapping",
        json={"package": package, "passphrase": passphrase},
    )


def test_unlock_mapping_returns_entries_with_correct_passphrase(tmp_path):
    client = _setup(tmp_path)
    parsed = _parse(client)
    package = _package(client, parsed["task_id"])

    resp = _unlock(client, package)
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["status"] == "ok"
    assert data["task_id"] == parsed["task_id"]
    entries = data["entries"]
    assert entries and all(
        set(entry) == {"placeholder", "label", "original_value"} for entry in entries
    )
    assert any(entry["original_value"] == _SECRET for entry in entries)


def test_unlock_mapping_wrong_passphrase_rejected(tmp_path):
    client = _setup(tmp_path)
    package = _package(client, _parse(client)["task_id"])
    assert _unlock(client, package, passphrase="wrong").status_code == 403


def test_unlock_mapping_tampered_binding_rejected(tmp_path):
    client = _setup(tmp_path)
    package = _package(client, _parse(client)["task_id"])
    tampered = copy.deepcopy(package)
    tampered["binding"]["job_id"] = "job-does-not-exist"
    assert _unlock(client, tampered).status_code == 403


def test_unlock_mapping_audit_carries_no_values(tmp_path):
    client = _setup(tmp_path)
    package = _package(client, _parse(client)["task_id"])
    assert _unlock(client, package).status_code == 200

    audit = client.get("/admin/audit/workbench")
    assert audit.status_code == 200
    events = [e for e in audit.json()["events"] if e.get("event") == "mapping_unlock"]
    assert events and events[-1]["entry_count"] >= 1
    assert _SECRET not in json.dumps(events, ensure_ascii=False)


# --- format-preserving DOCX redaction / refill (plan §9.4) --------------------

import base64 as _b64
import io as _io
import zipfile as _zipfile
from xml.etree import ElementTree as _ET
from xml.sax.saxutils import escape as _escape

_DOCX_W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
_DOCX_TYPES = b'<?xml version="1.0"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"/>'
_DOCX_RELS = b'<?xml version="1.0"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"/>'
_DOCX_PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 24


def _docx(body: str, **extra: bytes) -> bytes:
    stream = _io.BytesIO()
    with _zipfile.ZipFile(stream, "w", compression=_zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", _DOCX_TYPES)
        archive.writestr("_rels/.rels", _DOCX_RELS)
        archive.writestr(
            "word/document.xml",
            f'<?xml version="1.0" encoding="UTF-8"?>'
            f'<w:document xmlns:w="{_DOCX_W}"><w:body>{body}</w:body></w:document>'.encode(),
        )
        for name, value in extra.items():
            # kwargs cannot contain "/" or ".": "__" → "/", last "_" → "."
            # ("word__media__logo_png" → "word/media/logo.png")
            path = name.replace("__", "/")
            if "_" in path:
                head, _, ext = path.rpartition("_")
                path = head + "." + ext
            archive.writestr(path, value)
    return stream.getvalue()


def _docx_text(data: bytes, part: str = "word/document.xml") -> str:
    with _zipfile.ZipFile(_io.BytesIO(data)) as archive:
        root = _ET.fromstring(archive.read(part))
    return "".join(node.text or "" for node in root.iter(f"{{{_DOCX_W}}}t"))


def _simple_docx(text: str) -> bytes:
    runs = f"<w:r><w:t>{_escape(text)}</w:t></w:r>"
    return _docx(f"<w:p>{runs}</w:p>", word__media__logo_png=_DOCX_PNG)


def test_docx_redact_refill_round_trip(tmp_path, stub_ner):
    client = _bound_client(
        tmp_path,
        [{"canonical_value": "示例建设单位A", "aliases": [],
          "label": "ORG", "status": "active"}],
    )
    source = _simple_docx("发包方：示例建设单位A，联系电话13800138000。")
    response = client.post(
        "/api/v1/documents/redact-docx",
        params={"filename": "contract.docx", "app_id": "wb_docs"},
        content=source,
    )
    assert response.status_code == 200, response.text
    parsed = response.json()
    assert parsed["egress"]["egress_allowed"] is True

    redacted = _b64.b64decode(parsed["redacted_docx_b64"])
    redacted_text = _docx_text(redacted)
    assert "示例建设单位A" not in redacted_text
    assert "13800138000" not in redacted_text
    assert "<ORG_001>" in redacted_text
    # 未触及 part（图片）字节级不变
    with _zipfile.ZipFile(_io.BytesIO(source)) as archive:
        original_png = archive.read("word/media/logo.png")
    with _zipfile.ZipFile(_io.BytesIO(redacted)) as archive:
        assert archive.read("word/media/logo.png") == original_png

    package = _package(client, parsed["task_id"])
    refill = client.post(
        "/api/v1/documents/refill-docx",
        json={
            "package": package,
            "passphrase": "correct horse",
            "redacted_docx_b64": parsed["redacted_docx_b64"],
        },
    )
    assert refill.status_code == 200, refill.text
    restored = _b64.b64decode(refill.json()["restored_docx_b64"])
    assert _docx_text(restored) == "发包方：示例建设单位A，联系电话13800138000。"
    assert refill.json()["mode"] == "exact"


def test_docx_redact_blocked_without_app_binding(tmp_path, stub_ner):
    client = _setup(tmp_path)
    response = client.post(
        "/api/v1/documents/redact-docx",
        params={"filename": "contract.docx"},
        content=_simple_docx("联系电话13800138000"),
    )
    assert response.status_code == 422
    data = response.json()
    assert data["error"]["code"] == "egress_blocked"
    assert any(r["code"] == "app_not_bound" for r in data["egress"]["reasons"])
    assert "redacted_docx_b64" not in data  # 阻断时不产出文件


def test_docx_redact_rejects_encrypted_package(tmp_path, stub_ner):
    client = _setup(tmp_path)
    body = _docx("<w:p><w:r><w:t>正文</w:t></w:r></w:p>", EncryptionInfo=b"x")
    response = client.post(
        "/api/v1/documents/redact-docx",
        params={"filename": "contract.docx"},
        content=body,
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "encrypted_package"


def test_docx_refill_blocks_unknown_placeholder(tmp_path, stub_ner):
    client = _bound_client(tmp_path, [])
    source = _simple_docx("联系电话13800138000。")
    parsed = client.post(
        "/api/v1/documents/redact-docx",
        params={"filename": "contract.docx", "app_id": "wb_docs"},
        content=source,
    ).json()
    package = _package(client, parsed["task_id"])
    tampered = _simple_docx("联系电话 <CONTACT_999>。")
    refill = client.post(
        "/api/v1/documents/refill-docx",
        json={
            "package": package,
            "passphrase": "correct horse",
            "redacted_docx_b64": _b64.b64encode(tampered).decode("ascii"),
        },
    )
    assert refill.status_code == 422
    assert "unknown_placeholder" in refill.json()["error_types"]
