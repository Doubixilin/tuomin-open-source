"""Synthetic XLSX/PPTX workbench flows through the real document API."""
from __future__ import annotations

import base64
import io
import json
import zipfile

import pytest

from test_documents_api import _bound_client, _package, _setup, stub_ner
from tuomin_gateway.document.extract import extract_document
from tuomin_gateway.document.office_patch import extract_office_texts
from tuomin_gateway.placeholders import PLACEHOLDER_RE


PHONE = "13800138000"
ORG = "示例建设单位A"


def _source(file_format: str) -> bytes:
    if file_format == "xlsx":
        from openpyxl import Workbook
        from openpyxl.comments import Comment

        workbook = Workbook()
        sheet = workbook.active
        sheet.title = "Summary"
        sheet["A1"] = "单位"
        sheet["B1"] = ORG
        sheet["A2"] = "联系电话"
        sheet["B2"] = int(PHONE)
        sheet["B1"].comment = Comment("联系电话" + PHONE, ORG)
        workbook.properties.creator = ORG
        hidden = workbook.create_sheet("Hidden")
        hidden.sheet_state = "hidden"
        hidden["A1"] = ORG
        output = io.BytesIO()
        workbook.save(output)
        workbook.close()
        return output.getvalue()

    parts = {
        "[Content_Types].xml": '''<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
          <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
          <Default Extension="xml" ContentType="application/xml"/>
          <Override PartName="/ppt/presentation.xml" ContentType="application/vnd.openxmlformats-officedocument.presentationml.presentation.main+xml"/>
          <Override PartName="/ppt/slides/slide1.xml" ContentType="application/vnd.openxmlformats-officedocument.presentationml.slide+xml"/>
        </Types>''',
        "_rels/.rels": '''<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
          <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="ppt/presentation.xml"/>
        </Relationships>''',
        "ppt/presentation.xml": '''<p:presentation xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main"
          xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><p:sldIdLst><p:sldId id="256" r:id="rId1"/></p:sldIdLst></p:presentation>''',
        "ppt/_rels/presentation.xml.rels": '''<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
          <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/slide" Target="slides/slide1.xml"/>
        </Relationships>''',
        "ppt/slides/slide1.xml": f'''<p:sld xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main"
          xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"><p:cSld><p:spTree><p:sp><p:txBody><a:bodyPr/><a:lstStyle/>
          <a:p><a:r><a:t>{ORG}</a:t></a:r></a:p><a:p><a:r><a:t>联系电话{PHONE}</a:t></a:r></a:p>
          </p:txBody></p:sp></p:spTree></p:cSld></p:sld>''',
        "ppt/notesSlides/notesSlide1.xml": f'''<p:notes xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main"
          xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"><p:cSld><a:p><a:r><a:t>{ORG}</a:t></a:r></a:p></p:cSld></p:notes>''',
        "docProps/core.xml": f'''<cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties"
          xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:creator>{ORG}</dc:creator></cp:coreProperties>''',
    }
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as package:
        for name, text in parts.items():
            package.writestr(name, text)
    return output.getvalue()


def _redact(client, file_format, **params):
    return client.post(
        "/api/v1/documents/redact-office",
        params={"filename": "synthetic." + file_format, **params},
        content=_source(file_format),
    )


@pytest.mark.parametrize("file_format", ["xlsx", "pptx"])
def test_office_encrypted_package_round_trip_and_safe_ledger(tmp_path, stub_ner, file_format):
    client = _bound_client(tmp_path, [
        {"canonical_value": ORG, "aliases": [], "label": "ORG", "status": "active"},
    ])
    response = _redact(client, file_format, app_id="wb_docs")
    assert response.status_code == 200, response.text
    parsed = response.json()
    assert parsed["file_format"] == file_format
    assert parsed["egress"]["egress_allowed"]
    data = base64.b64decode(parsed["redacted_office_b64"])
    units = extract_office_texts(data, file_format)
    assert all(ORG not in item.text and PHONE not in item.text for item in units)
    package = _package(client, parsed["task_id"])
    assert ORG not in json.dumps(package, ensure_ascii=False)
    assert PHONE not in json.dumps(package, ensure_ascii=False)
    response = client.post("/api/v1/documents/refill-office", json={
        "file_format": file_format, "redacted_office_b64": parsed["redacted_office_b64"],
        "package": package, "passphrase": "correct horse",
    })
    assert response.status_code == 200, response.text
    assert response.json()["mode"] == "exact"
    restored = base64.b64decode(response.json()["restored_office_b64"])
    texts = [item.text for item in extract_office_texts(restored, file_format)]
    assert ORG in "\n".join(texts) and PHONE in "\n".join(texts)
    if file_format == "xlsx":
        from openpyxl import load_workbook
        workbook = load_workbook(io.BytesIO(restored))
        assert workbook.active["B1"].value == ORG
        assert str(workbook.active["B2"].value) == PHONE
        assert workbook["Hidden"].sheet_state == "hidden"
        assert workbook.active["B1"].comment.author == ORG
        workbook.close()
    jobs = client.get("/api/v1/documents/jobs").json()
    audit = client.get("/admin/audit/trusted-refill").json()
    assert ORG not in json.dumps([jobs, audit], ensure_ascii=False)
    assert PHONE not in json.dumps([jobs, audit], ensure_ascii=False)


@pytest.mark.parametrize("file_format", ["xlsx", "pptx"])
def test_office_no_file_when_unbound_and_auth_required(tmp_path, stub_ner, file_format):
    client = _setup(tmp_path)
    response = _redact(client, file_format)
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "egress_blocked"
    assert "redacted_office_b64" not in response.json()
    assert client.post("/api/v1/documents/redact-office", headers={"x-tuomin-admin-token": ""}).status_code == 401
    assert client.post("/api/v1/documents/refill-office", headers={"x-tuomin-admin-token": ""}).status_code == 401


def test_office_rejects_old_or_macro_formats_and_malformed_archives(tmp_path, monkeypatch):
    client = _setup(tmp_path)
    for ext in ("xlsm", "ppt", "pptm", "docx"):
        response = client.post("/api/v1/documents/redact-office", params={"filename": "sample." + ext}, content=b"not Office")
        assert response.status_code == 415
    response = client.post("/api/v1/documents/redact-office", params={"filename": "sample.pptx"}, content=b"not Office")
    assert response.status_code == 422
    # Legacy .xls is converted first; without a converter it fails closed with a clear code.
    from tuomin_gateway.document.legacy_convert import LegacyConversionError
    from tuomin_gateway.service import v1_documents

    def unavailable(data, source_format):
        raise LegacyConversionError("converter_unavailable", "no converter")

    monkeypatch.setattr(v1_documents, "convert_legacy", unavailable)
    response = client.post("/api/v1/documents/redact-office", params={"filename": "sample.xls"}, content=b"not Office")
    assert response.status_code == 422 and response.json()["error"]["code"] == "converter_unavailable"


def test_office_recovery_rejects_wrong_passphrase_or_format(tmp_path, stub_ner):
    client = _bound_client(tmp_path, [])
    parsed = _redact(client, "pptx", app_id="wb_docs").json()
    package = _package(client, parsed["task_id"])
    payload = {"file_format": "pptx", "redacted_office_b64": parsed["redacted_office_b64"], "package": package, "passphrase": "wrong"}
    assert client.post("/api/v1/documents/refill-office", json=payload).status_code == 403
    payload.update(file_format="xlsx", passphrase="correct horse")
    assert client.post("/api/v1/documents/refill-office", json=payload).status_code == 403


def _rewrite_part(data: bytes, transform) -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(data)) as source, zipfile.ZipFile(output, "w") as target:
        for info in source.infolist():
            target.writestr(info, transform(info.filename, source.read(info)))
    return output.getvalue()


def test_office_wrong_same_format_package_and_edits_need_confirmation(tmp_path, stub_ner):
    client = _bound_client(tmp_path, [])
    first = _redact(client, "pptx", app_id="wb_docs").json()
    # Identical source/masked content from a different task still has distinct
    # recovery provenance, so an accidental package mix-up is not silent.
    second = _redact(client, "pptx", app_id="wb_docs").json()
    assert first["redacted_sha256"] != second["redacted_sha256"]
    package = _package(client, first["task_id"])
    payload = {"file_format": "pptx", "redacted_office_b64": second["redacted_office_b64"], "package": package, "passphrase": "correct horse"}
    pending = client.post("/api/v1/documents/refill-office", json=payload)
    assert pending.status_code == 200
    assert pending.json()["status"] == "needs_confirmation"
    assert "restored_office_b64" not in pending.json()

    edited = _rewrite_part(base64.b64decode(first["redacted_office_b64"]), lambda name, raw: raw.replace("联系电话".encode(), "联系号码".encode()))
    payload["redacted_office_b64"] = base64.b64encode(edited).decode()
    assert client.post("/api/v1/documents/refill-office", json=payload).json()["status"] == "needs_confirmation"
    payload["confirm_edited"] = True
    restored = client.post("/api/v1/documents/refill-office", json=payload)
    assert restored.status_code == 200, restored.text
    assert restored.json()["status"] == "ok"
    assert restored.json()["mode"] == "edited"


def test_office_unknown_placeholder_cannot_be_confirmed(tmp_path, stub_ner):
    client = _bound_client(tmp_path, [])
    parsed = _redact(client, "pptx", app_id="wb_docs").json()
    package = _package(client, parsed["task_id"])
    data = base64.b64decode(parsed["redacted_office_b64"])
    placeholder = next(match.group() for item in extract_office_texts(data, "pptx") for match in PLACEHOLDER_RE.finditer(item.text))
    encoded = placeholder.replace("<", "&lt;").replace(">", "&gt;").encode()
    changed = _rewrite_part(data, lambda name, raw: raw.replace(encoded, b"&lt;CONTACT_999&gt;"))
    assert "<CONTACT_999>" in "\n".join(item.text for item in extract_office_texts(changed, "pptx"))
    response = client.post("/api/v1/documents/refill-office", json={
        "file_format": "pptx", "redacted_office_b64": base64.b64encode(changed).decode(),
        "package": package, "passphrase": "correct horse", "confirm_edited": True,
    })
    assert response.status_code == 422, response.text
    assert "unknown_placeholder" in response.json()["error_types"]
    assert "restored_office_b64" not in response.json()


def test_office_failed_job_storage_never_returns_unrecoverable_file(tmp_path, stub_ner, monkeypatch):
    client = _bound_client(tmp_path, [])
    def fail(*args, **kwargs):
        raise OSError("synthetic disk failure")
    monkeypatch.setattr("tuomin_gateway.jobs.JobStore.create_job", fail)
    response = _redact(client, "pptx", app_id="wb_docs")
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "job_persistence_failed"
    assert "redacted_office_b64" not in response.json()


def test_pptx_extraction_and_markdown_preview_include_notes_and_properties(tmp_path, stub_ner):
    source = _source("pptx")
    path = tmp_path / "synthetic.pptx"
    path.write_bytes(source)
    result = extract_document(path, "job-pptx")
    assert result.file_format == "pptx"
    assert any("notesSlides" in block.metadata["part"] for block in result.blocks)
    assert any(block.metadata["part"] == "docProps/core.xml" for block in result.blocks)
    client = _bound_client(tmp_path, [{"canonical_value": ORG, "aliases": [], "label": "ORG", "status": "active"}])
    response = client.post("/api/v1/documents/parse", params={"filename": "synthetic.pptx", "app_id": "wb_docs"}, content=source)
    assert response.status_code == 200, response.text
    assert PHONE not in response.json()["redacted_markdown"]
    assert ORG not in response.json()["redacted_markdown"]


def test_detected_values_are_propagated_to_untagged_occurrences(tmp_path, stub_ner, monkeypatch):
    """A value the detector tags in one place is replaced everywhere else too."""
    import dataclasses
    from tuomin_gateway.service import v1_documents

    real = v1_documents.run_detection

    def first_hit_only(text, detectors, profile):
        run = real(text, detectors, profile)
        org_spans = [span for span in run.kept if span.label == "ORG"]
        kept = [span for span in run.kept if span.label != "ORG"] + org_spans[:1]
        return dataclasses.replace(run, kept=kept)

    monkeypatch.setattr(v1_documents, "run_detection", first_hit_only)
    client = _bound_client(tmp_path, [{"canonical_value": ORG, "aliases": [], "label": "ORG", "status": "active"}])
    response = _redact(client, "pptx", app_id="wb_docs")  # ORG appears on the slide and in the notes
    assert response.status_code == 200, response.text
    body = response.json()
    assert "known_values_propagated" in {w["code"] for w in body["warnings"]}
    texts = [u.text for u in extract_office_texts(base64.b64decode(body["redacted_office_b64"]), "pptx")]
    assert all(ORG not in t for t in texts)
    assert sum(t.count("<ORG_001>") for t in texts) >= 2


def _inject_formula(data: bytes, formula: str, sheet_name: str | None = None) -> bytes:
    def transform(name, raw):
        if name == "xl/worksheets/sheet1.xml":
            cell = f'<row r="9"><c r="A9" t="str"><f>{formula}</f><v>1</v></c></row></sheetData>'.encode()
            return raw.replace(b"</sheetData>", cell, 1)
        if name == "xl/workbook.xml" and sheet_name:
            return raw.replace(b'name="Summary"', f'name="{sheet_name}"'.encode(), 1)
        return raw
    return _rewrite_part(data, transform)


def test_structural_hits_in_formulas_and_sheet_names_do_not_block(tmp_path, stub_ner):
    """Arithmetic that looks like a date, and a date in a tab name, are not leaks."""
    client = _bound_client(tmp_path, [{"canonical_value": ORG, "aliases": [], "label": "ORG", "status": "active"}])
    source = _inject_formula(_source("xlsx"), 'SUM(2026-8-26,A1)', sheet_name="2026年8月26日")
    response = client.post("/api/v1/documents/redact-office", params={"filename": "s.xlsx", "app_id": "wb_docs"}, content=source)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["egress"]["egress_allowed"]
    assert "readonly_model_hits_ignored" in {w["code"] for w in body["warnings"]}
    texts = [u.text for u in extract_office_texts(base64.b64decode(body["redacted_office_b64"]), "xlsx")]
    assert all(ORG not in t and PHONE not in t for t in texts if not t.startswith("SUM("))


def test_sensitive_literal_inside_formula_blocks_with_structure_named(tmp_path, stub_ner):
    client = _bound_client(tmp_path, [])
    source = _inject_formula(_source("xlsx"), f'IF(B1="{PHONE}",1,0)')
    response = client.post("/api/v1/documents/redact-office", params={"filename": "s.xlsx", "app_id": "wb_docs"}, content=source)
    assert response.status_code == 422, response.text
    error = response.json()["error"]
    assert error["code"] == "readonly_text_replacement"
    assert "xl/worksheets/sheet1.xml 的公式" in error["message"] and "普通文字" in error["message"]
