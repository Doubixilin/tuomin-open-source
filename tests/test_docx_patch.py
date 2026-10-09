from __future__ import annotations

import hashlib
import io
import zipfile
from xml.etree import ElementTree as ET
from xml.sax.saxutils import escape

import pytest

from tuomin_gateway.document.docx_patch import (
    DocxUnsupportedError,
    extract_paragraph_texts,
    probe_docx,
    redact_docx,
    refill_docx,
)

W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
CONTENT_TYPES = b'<?xml version="1.0"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"/>'
RELS = b'<?xml version="1.0"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"/>'
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 24


def document(body: str) -> bytes:
    return f'<?xml version="1.0" encoding="UTF-8"?><w:document xmlns:w="{W}"><w:body>{body}</w:body></w:document>'.encode()


def paragraph(*runs: str) -> str:
    return "<w:p>" + "".join(runs) + "</w:p>"


def run(text: str, properties: str = "") -> str:
    return f"<w:r>{properties}<w:t>{escape(text)}</w:t></w:r>"


def package(body: str, **extra: bytes) -> bytes:
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", CONTENT_TYPES)
        archive.writestr("_rels/.rels", RELS)
        archive.writestr("word/document.xml", document(body))
        for name, value in extra.items():
            # kwargs cannot contain "/" or ".": "word__styles_xml" means "word/styles.xml"
            archive.writestr(name.replace("__", "/").replace("_xml", ".xml"), value)
    return stream.getvalue()


def read_part(data: bytes, name: str) -> bytes:
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        return archive.read(name)


def texts(data: bytes, part: str = "word/document.xml") -> list[str]:
    root = ET.fromstring(read_part(data, part))
    return [node.text or "" for node in root.iter(f"{{{W}}}t")]


def test_single_node_replacement_and_untouched_hash() -> None:
    source = package(paragraph(run("甲公司联系电话13800138000")), word__styles_xml=b"<styles/>")
    result = redact_docx(source, [{"part": "word/document.xml", "paragraph": 0, "start": 0, "end": 3, "placeholder": "<ORG_001>"}])
    assert texts(result["data"]) == ["<ORG_001>联系电话13800138000"]
    ET.fromstring(read_part(result["data"], "word/document.xml"))
    expected = hashlib.sha256(read_part(source, "word/styles.xml")).hexdigest()
    assert result["untouched_hashes"]["word/styles.xml"] == expected
    assert read_part(result["data"], "word/styles.xml") == read_part(source, "word/styles.xml")


def test_cross_run_replacement() -> None:
    source = package(paragraph(run("北京甲") + run("公司电话") + run("号码")))
    result = redact_docx(source, [{"part": "word/document.xml", "paragraph": 0, "start": 2, "end": 5, "placeholder": "<ORG_001>"}])
    # span 2..5 = "甲公司": node0 keeps prefix + placeholder, node1 keeps suffix,
    # node2 is outside the span and stays untouched
    assert texts(result["data"]) == ["北京<ORG_001>", "电话", "号码"]
    assert result["ledger"][0]["crossed_nodes"] is True


def test_mixed_run_format_warning() -> None:
    source = package(paragraph(run("甲公", "<w:rPr><w:b/></w:rPr>") + run("司", "<w:rPr><w:i/></w:rPr>")))
    result = redact_docx(source, [{"part": "word/document.xml", "paragraph": 0, "start": 0, "end": 3, "placeholder": "<ORG_001>"}])
    assert result["ledger"][0]["format_mixed"] is True
    assert "format_boundary_crossed" in {warning["code"] for warning in result["warnings"]}


def test_table_and_header_replacements() -> None:
    header = f'<?xml version="1.0"?><w:hdr xmlns:w="{W}">{paragraph(run("甲公司"))}</w:hdr>'.encode()
    source = package(f"<w:tbl><w:tr><w:tc>{paragraph(run('甲公司'))}</w:tc></w:tr></w:tbl>", word__header1_xml=header)
    paragraphs = extract_paragraph_texts(source)
    assert [(item.part, item.text) for item in paragraphs] == [("word/document.xml", "甲公司"), ("word/header1.xml", "甲公司")]
    result = redact_docx(source, [
        {"part": "word/document.xml", "paragraph": 0, "start": 0, "end": 3, "placeholder": "<ORG_001>"},
        {"part": "word/header1.xml", "paragraph": 0, "start": 0, "end": 3, "placeholder": "<ORG_002>"},
    ])
    assert texts(result["data"]) == ["<ORG_001>"]
    assert texts(result["data"], "word/header1.xml") == ["<ORG_002>"]


def test_redact_refill_round_trip_text() -> None:
    source = package(paragraph(run("甲公司") + run("电话13800138000")))
    redacted = redact_docx(source, [
        {"part": "word/document.xml", "paragraph": 0, "start": 0, "end": 3, "placeholder": "<ORG_001>"},
        {"part": "word/document.xml", "paragraph": 0, "start": 5, "end": 16, "placeholder": "<PHONE_001>"},
    ])
    restored = refill_docx(redacted["data"], {"<ORG_001>": "甲公司", "<PHONE_001>": "13800138000"})
    assert restored["status"] == "ok"
    assert restored["restored_count"] == 2
    assert extract_paragraph_texts(restored["data"])[0].text == extract_paragraph_texts(source)[0].text


@pytest.mark.parametrize(
    ("body", "values", "error"),
    [
        (paragraph(run("<ORG_999>")), {"<ORG_001>": "甲公司"}, "unknown_placeholder"),
        (paragraph(run("<ORG-1>")), {"<ORG_001>": "甲公司"}, "altered_placeholder"),
        (paragraph(run("<ORG_") + run("001>")), {"<ORG_001>": "甲公司"}, "placeholder_split_across_runs"),
    ],
)
def test_refill_blocks_unsafe_placeholders(body: str, values: dict[str, str], error: str) -> None:
    result = refill_docx(package(body), values)
    assert result["status"] == "blocked"
    assert error in result["error_types"]
    assert "data" not in result


def test_probe_blockers_and_field_warning() -> None:
    encrypted = package(paragraph(run("正文")), EncryptionInfo=b"encrypted")
    with pytest.raises(DocxUnsupportedError) as caught:
        probe_docx(encrypted)
    assert caught.value.code == "encrypted_package"

    comments = f'<?xml version="1.0"?><w:comments xmlns:w="{W}"><w:comment w:id="0">{paragraph(run("批注"))}</w:comment></w:comments>'.encode()
    with pytest.raises(DocxUnsupportedError) as caught:
        probe_docx(package(paragraph(run("正文")), word__comments_xml=comments))
    assert caught.value.code == "comments_present"

    txbx = package('<w:p><w:r><w:drawing><w:txbxContent>' + paragraph(run("文本框")) + '</w:txbxContent></w:drawing></w:r></w:p>')
    warnings = probe_docx(txbx)
    assert [warning["code"] for warning in warnings] == ["textbox_patched"]
    # 空文本框（纯形状）不产生 warning
    empty_txbx = package('<w:p><w:r><w:drawing><w:txbxContent>' + paragraph(run("")) + '</w:txbxContent></w:drawing></w:r></w:p>')
    assert probe_docx(empty_txbx) == []

    warnings = probe_docx(package('<w:p><w:fldSimple w:instr="DATE">' + run("日期") + '</w:fldSimple></w:p>'))
    assert [warning["code"] for warning in warnings] == ["field_codes"]


def test_textbox_paragraph_is_patched_and_refilled() -> None:
    body = (
        paragraph(run("正文"))
        + '<w:p><w:r><w:drawing><w:txbxContent>'
        + paragraph(run("甲公司"))
        + "</w:txbxContent></w:drawing></w:r></w:p>"
    )
    source = package(body)
    paragraphs = extract_paragraph_texts(source)
    # 段落序：正文段、外层空段、文本框内段落（iter 文档序）
    assert any(item.text == "甲公司" for item in paragraphs)
    target = next(item for item in paragraphs if item.text == "甲公司")
    result = redact_docx(source, [{
        "part": target.part, "paragraph": target.index,
        "start": 0, "end": 3, "placeholder": "<ORG_001>",
    }])
    assert "<ORG_001>" in "".join(texts(result["data"]))
    restored = refill_docx(result["data"], {"<ORG_001>": "甲公司"})
    assert restored["status"] == "ok"
    assert "甲公司" in "".join(texts(restored["data"]))


def test_nested_textbox_paragraph_not_double_counted() -> None:
    # VML 文本框把完整 w:p 嵌在宿主段落内（真实合同封面结构）：宿主段落的
    # 自身文本不得包含嵌套段落文本，否则两侧都打补丁会互相破坏坐标。
    body = (
        "<w:p><w:r><w:t>附件1</w:t></w:r>"
        "<w:r><w:pict><w:txbxContent>"
        + paragraph(run("湖北省某高速项目"))
        + "</w:txbxContent></w:pict></w:r>"
        "<w:r><w:t>参股立项建议书</w:t></w:r></w:p>"
    )
    source = package(body)
    paragraphs = extract_paragraph_texts(source)
    texts_found = [item.text for item in paragraphs if item.text.strip()]
    assert texts_found == ["附件1参股立项建议书", "湖北省某高速项目"]

    host = next(item for item in paragraphs if item.text == "附件1参股立项建议书")
    nested = next(item for item in paragraphs if item.text == "湖北省某高速项目")
    result = redact_docx(source, [
        {"part": host.part, "paragraph": host.index,
         "start": 3, "end": 8, "placeholder": "<ORG_001>"},
        {"part": nested.part, "paragraph": nested.index,
         "start": 0, "end": 8, "placeholder": "<ADDRESS_001>"},
    ])
    out = "".join(texts(result["data"]))
    assert "<ORG_001>" in out and "<ADDRESS_001>" in out
    assert "湖北省某高速项目" not in out and "参股立项建" not in out
    restored = refill_docx(
        result["data"], {"<ORG_001>": "参股立项建", "<ADDRESS_001>": "湖北省某高速项目"}
    )
    assert restored["status"] == "ok"
    final = "".join(texts(restored["data"]))
    assert final == "附件1湖北省某高速项目参股立项建议书"


# --- OLE 嵌入对象剥离降级（2026-08-25）---------------------------------------

_OLE_NS_O = "urn:schemas-microsoft-com:office:office"
_OLE_NS_R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"


def ole_package(body: str, *, stray: bool = False) -> bytes:
    """构造含 OLE 嵌入对象的 DOCX（w:object + oleObject + .bin + 关系 + 内容类型）。"""
    ct = (
        b'<?xml version="1.0"?>'
        b'<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        b'<Override PartName="/word/embeddings/oleObject1.bin" '
        b'ContentType="application/vnd.openxmlformats-officedocument.oleObject"/>'
        b"</Types>"
    )
    doc_rels = (
        b'<?xml version="1.0"?>'
        b'<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        b'<Relationship Id="rId9" '
        b'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/oleObject" '
        b'Target="embeddings/oleObject1.bin"/>'
        b"</Relationships>"
    )
    if stray:
        ole_markup = (
            f'<w:p><w:r><w:pict><o:OLEObject xmlns:o="{_OLE_NS_O}" '
            f'xmlns:r="{_OLE_NS_R}" r:id="rId9"/></w:pict></w:r></w:p>'
        )
    else:
        ole_markup = (
            f'<w:p><w:r><w:object><o:OLEObject xmlns:o="{_OLE_NS_O}" '
            f'xmlns:r="{_OLE_NS_R}" r:id="rId9"/></w:object></w:r></w:p>'
        )
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", ct)
        archive.writestr("_rels/.rels", RELS)
        archive.writestr("word/document.xml", document(body + ole_markup))
        archive.writestr("word/_rels/document.xml.rels", doc_rels)
        archive.writestr("word/embeddings/oleObject1.bin", b"OLE-COMPOUND-BIN")
    return stream.getvalue()


def test_ole_default_refused() -> None:
    source = ole_package(paragraph(run("甲公司")))
    with pytest.raises(DocxUnsupportedError) as caught:
        probe_docx(source)
    assert caught.value.code == "ole_object_present"


def test_ole_strip_probe_warns() -> None:
    warnings = probe_docx(ole_package(paragraph(run("甲公司"))), allow_ole_strip=True)
    assert [w["code"] for w in warnings] == ["ole_stripped"]


def test_ole_stray_refused_even_with_strip() -> None:
    source = ole_package(paragraph(run("甲公司")), stray=True)
    with pytest.raises(DocxUnsupportedError) as caught:
        probe_docx(source, allow_ole_strip=True)
    assert caught.value.code == "ole_object_present"


def test_ole_strip_redact_removes_package_traces() -> None:
    source = ole_package(paragraph(run("甲公司")))
    result = redact_docx(
        source,
        [{"part": "word/document.xml", "paragraph": 0, "start": 0, "end": 3, "placeholder": "<ORG_001>"}],
        allow_ole_strip=True,
    )
    data = result["data"]
    assert "ole_stripped" in {w["code"] for w in result["warnings"]}
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        names = archive.namelist()
        assert not any(name.startswith("word/embeddings/") for name in names)
        doc = archive.read("word/document.xml").decode("utf-8")
        assert "object" not in doc.replace("oleObject", "")
        rels = archive.read("word/_rels/document.xml.rels").decode("utf-8")
        assert "embeddings/" not in rels
        ct = archive.read("[Content_Types].xml").decode("utf-8")
        assert "embeddings/" not in ct
    assert texts(data)[0] == "<ORG_001>"
    # 剥离后的产物本身不再含 OLE，回填探针默认也能通过
    restored = refill_docx(data, {"<ORG_001>": "甲公司"})
    assert restored["status"] == "ok"
    assert texts(restored["data"])[0] == "甲公司"
