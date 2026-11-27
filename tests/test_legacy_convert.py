"""Legacy .doc / .xls support: convert to OOXML, then the normal pipeline."""
from __future__ import annotations

import io
import zipfile

import pytest

from test_documents_api import _bound_client, stub_ner  # noqa: F401
from test_office_documents_api import ORG, _source
from tuomin_gateway.document import legacy_convert
from tuomin_gateway.document.legacy_convert import ConvertedDocument, LegacyConversionError, convert_legacy
from tuomin_gateway.service import v1_documents


def _package(target: str) -> bytes:
    main = "word/document.xml" if target == "docx" else "xl/workbook.xml"
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as z:
        z.writestr("[Content_Types].xml", "<Types/>")
        z.writestr(main, "<root/>")
    return out.getvalue()


def _fake_run(fail_for: set[str] = frozenset()):
    def run(command, *, env=None):
        tool = "soffice" if "--headless" in command else "textutil"
        if tool in fail_for:
            raise LegacyConversionError("conversion_failed", tool)
        if tool == "soffice":
            outdir = command[command.index("--outdir") + 1]
            target = command[command.index("--convert-to") + 1]
            (legacy_convert.Path(outdir) / f"input.{target}").write_bytes(_package(target))
        else:
            output = command[command.index("-output") + 1]
            legacy_convert.Path(output).write_bytes(_package("docx"))
    return run


def test_soffice_is_preferred_and_handles_both_formats(monkeypatch):
    monkeypatch.setattr(legacy_convert, "available_converters", lambda fmt: ["soffice", "textutil"] if fmt == "doc" else ["soffice"])
    monkeypatch.setattr(legacy_convert, "find_soffice", lambda: "/opt/fake/soffice")
    monkeypatch.setattr(legacy_convert, "_run", _fake_run())
    doc = convert_legacy(b"binary doc", "doc")
    xls = convert_legacy(b"binary xls", "xls")
    assert (doc.file_format, doc.converter) == ("docx", "soffice")
    assert (xls.file_format, xls.converter) == ("xlsx", "soffice")
    assert zipfile.ZipFile(io.BytesIO(xls.data)).namelist() == ["[Content_Types].xml", "xl/workbook.xml"]


def test_textutil_fallback_for_doc_and_fail_closed_otherwise(monkeypatch):
    monkeypatch.setattr(legacy_convert, "available_converters", lambda fmt: ["soffice", "textutil"] if fmt == "doc" else ["soffice"])
    monkeypatch.setattr(legacy_convert, "find_soffice", lambda: "/opt/fake/soffice")
    monkeypatch.setattr(legacy_convert.shutil, "which", lambda name: "/usr/bin/textutil" if name == "textutil" else None)
    monkeypatch.setattr(legacy_convert, "_run", _fake_run(fail_for={"soffice"}))
    assert convert_legacy(b"binary doc", "doc").converter == "textutil"
    with pytest.raises(LegacyConversionError, match="conversion_failed"):
        convert_legacy(b"binary xls", "xls")  # soffice failed and textutil cannot do xls
    monkeypatch.setattr(legacy_convert, "available_converters", lambda fmt: [])
    with pytest.raises(LegacyConversionError, match="converter_unavailable"):
        convert_legacy(b"binary doc", "doc")
    with pytest.raises(LegacyConversionError, match="unsupported_legacy_format"):
        convert_legacy(b"x", "ppt")


def test_redact_office_accepts_xls_by_converting_to_xlsx(tmp_path, stub_ner, monkeypatch):
    client = _bound_client(tmp_path, [{"canonical_value": ORG, "aliases": [], "label": "ORG", "status": "active"}])
    seen = {}

    def fake_convert(data, source_format):
        seen.update(data=data, source_format=source_format)
        return ConvertedDocument(_source("xlsx"), "xlsx", "soffice")

    monkeypatch.setattr(v1_documents, "convert_legacy", fake_convert)
    response = client.post("/api/v1/documents/redact-office", params={"filename": "台账.xls", "app_id": "wb_docs"}, content=b"legacy xls bytes")
    assert response.status_code == 200, response.text
    body = response.json()
    assert seen == {"data": b"legacy xls bytes", "source_format": "xls"}
    assert body["file_format"] == "xlsx" and body["file_name"] == "台账.xlsx"
    assert any(w["code"] == "legacy_converted" and "soffice" in w["message"] for w in body["warnings"])
    assert body["egress"]["egress_allowed"]

    def no_converter(data, source_format):
        raise LegacyConversionError("converter_unavailable", "请安装 LibreOffice")

    monkeypatch.setattr(v1_documents, "convert_legacy", no_converter)
    blocked = client.post("/api/v1/documents/redact-office", params={"filename": "台账.xls", "app_id": "wb_docs"}, content=b"legacy xls bytes")
    assert blocked.status_code == 422 and blocked.json()["error"]["code"] == "converter_unavailable"
    assert client.post("/api/v1/documents/redact-office", params={"filename": "deck.ppt", "app_id": "wb_docs"}, content=b"x").status_code == 415
