from __future__ import annotations

import hashlib
import io
import warnings
import zipfile
from xml.dom import minidom
from xml.sax.saxutils import escape

import pytest

from tuomin_gateway.document import office_patch as office
from tuomin_gateway.document.office_patch import (
    OfficeUnsupportedError,
    extract_office_texts,
    probe_office,
    redact_office,
    refill_office,
)

S, A, P, C = office.S, office.A, office.P, office.C
CT = '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"/>'
RELS = '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"/>'


def package(kind="xlsx", **parts):
    entries = {
        "[Content_Types].xml": CT,
        "_rels/.rels": RELS,
        "xl/workbook.xml" if kind == "xlsx" else "ppt/presentation.xml": f'<workbook xmlns="{S}"/>' if kind == "xlsx" else f'<p:presentation xmlns:p="{P}"/>',
    }
    entries.update(parts)
    result = io.BytesIO()
    with zipfile.ZipFile(result, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, value in entries.items():
            archive.writestr(name, value)
    return result.getvalue()


def sheet(body):
    return f'<worksheet xmlns="{S}" xmlns:mc="http://schemas.openxmlformats.org/markup-compatibility/2006" xmlns:x14ac="http://schemas.microsoft.com/office/spreadsheetml/2009/9/ac" mc:Ignorable="x14ac"><sheetData><row r="1">{body}</row></sheetData></worksheet>'


def slide(body):
    return f'<p:sld xmlns:p="{P}" xmlns:a="{A}"><p:cSld><p:spTree>{body}</p:spTree></p:cSld></p:sld>'


def para(*texts):
    return '<a:p>' + ''.join(f'<a:r><a:rPr b="1"/><a:t>{escape(t)}</a:t></a:r>' for t in texts) + '</a:p>'


def read(data, part):
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        return archive.read(part)


def all_text(data, kind="xlsx"):
    return [unit.text for unit in extract_office_texts(data, kind)]


def replacements(data, targets, kind="xlsx"):
    return [
        {"part": unit.part, "paragraph": unit.index, "start": unit.text.index(value), "end": unit.text.index(value) + len(value), "placeholder": placeholder}
        for unit in extract_office_texts(data, kind)
        for value, placeholder in targets.items()
        if value in unit.text
    ]


def codes(warnings):
    return {warning["code"] for warning in warnings}


def test_xlsx_rich_shared_inline_cached_and_numeric_roundtrip():
    shared = f'<sst xmlns="{S}"><si><r><rPr><b/></rPr><t>甲公</t></r><r><rPr><i/></rPr><t>司</t></r></si></sst>'
    data = package(**{
        "xl/sharedStrings.xml": shared,
        "xl/worksheets/sheet1.xml": sheet('<c r="A1" s="4" t="s"><v>0</v></c><c r="B1" s="5"><v>13800138000</v></c><c r="C1" t="str"><f>CONCAT(&quot;甲公司&quot;)</f><v>甲公司</v></c><c r="D1" t="inlineStr"><is><t>甲公司</t></is></c><c r="E1"><v>123.45</v></c>'),
        "xl/styles.xml": '<styleSheet xmlns="' + S + '"><fonts count="1"><font><name val="Arial"/></font></fonts></styleSheet>',
    })
    # A sensitive formula cannot be changed safely; it is detected read-only.
    with pytest.raises(OfficeUnsupportedError, match="readonly_text_replacement"):
        redact_office(data, replacements(data, {"甲公司": "<ORG_001>"}), "xlsx")
    data = package(**{
        "xl/sharedStrings.xml": shared,
        "xl/worksheets/sheet1.xml": sheet('<c r="A1" s="4" t="s"><v>0</v></c><c r="B1" s="5"><v>13800138000</v></c><c r="C1" t="str"><v>甲公司</v></c><c r="D1" t="inlineStr"><is><t>甲公司</t></is></c><c r="E1"><v>123.45</v></c>'),
        "xl/styles.xml": '<styleSheet xmlns="' + S + '"><fonts count="1"><font><name val="Arial"/></font></fonts></styleSheet>',
    })
    out = redact_office(data, replacements(data, {"甲公司": "<ORG_001>", "13800138000": "<PHONE_001>"}), "xlsx")
    assert all_text(out["data"]).count("<ORG_001>") == 3
    assert "<PHONE_001>" in all_text(out["data"])
    assert "numeric_cell_converted_to_text" in codes(out["warnings"])
    assert "run_boundary_crossed" in codes(out["warnings"])
    assert read(out["data"], "xl/styles.xml") == read(data, "xl/styles.xml")
    assert out["untouched_hashes"]["xl/styles.xml"] == hashlib.sha256(read(data, "xl/styles.xml")).hexdigest()
    xml = minidom.parseString(read(out["data"], "xl/worksheets/sheet1.xml"))
    cell = next(c for c in xml.getElementsByTagNameNS(S, "c") if c.getAttribute("r") == "B1")
    assert cell.getAttribute("s") == "5"
    assert cell.getAttribute("t") == "inlineStr"
    assert not cell.getElementsByTagNameNS(S, "v")
    assert xml.documentElement.getAttribute("xmlns:x14ac")
    assert xml.documentElement.getAttribute("mc:Ignorable") == "x14ac"
    restored = refill_office(out["data"], {"<ORG_001>": "甲公司", "<PHONE_001>": "13800138000"}, "xlsx")
    assert restored["status"] == "ok"
    assert restored["restored_count"] == 4
    assert all_text(restored["data"]).count("甲公司") == 3
    assert "13800138000" in all_text(restored["data"])
    assert b't="inlineStr"' in read(restored["data"], "xl/worksheets/sheet1.xml")
    assert b'<rPr><b/></rPr>' in read(restored["data"], "xl/sharedStrings.xml")
    assert b'<rPr><i/></rPr>' in read(restored["data"], "xl/sharedStrings.xml")


def test_scientific_numeric_phone_and_untouched_business_numbers():
    data = package(**{"xl/worksheets/sheet1.xml": sheet('<c r="A1"><v>1.3800138E10</v></c><c r="A2"><v>42</v></c><c r="A3"><v>2000.25</v></c>')})
    assert all_text(data) == ["13800138000"]
    out = redact_office(data, replacements(data, {"13800138000": "<PHONE_001>"}), "xlsx")
    assert b'<v>42</v>' in read(out["data"], "xl/worksheets/sheet1.xml")
    assert b'<v>2000.25</v>' in read(out["data"], "xl/worksheets/sheet1.xml")


def test_xlsx_comments_authors_headers_metadata_drawings_and_phonetics():
    data = package(**{
        "xl/comments/comment1.xml": f'<comments xmlns="{S}"><authors><author>张三</author></authors><commentList><comment ref="A1" authorId="0"><text><r><t>甲公</t></r><r><t>司</t></r></text></comment></commentList></comments>',
        "xl/sharedStrings.xml": f'<sst xmlns="{S}"><si><t>甲公司</t><rPh sb="0" eb="1"><t>ZHANGSAN</t></rPh></si></sst>',
        "xl/worksheets/sheet1.xml": f'<worksheet xmlns="{S}"><headerFooter><oddHeader>&amp;L甲公司</oddHeader><oddFooter>13800138000</oddFooter></headerFooter></worksheet>',
        "xl/drawings/drawing1.xml": f'<a:root xmlns:a="{A}">{para("甲公司")}</a:root>',
        "docProps/core.xml": '<cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties" xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:creator>张三</dc:creator><dc:title>甲公司</dc:title></cp:coreProperties>',
        "docProps/custom.xml": '<Properties xmlns="http://schemas.openxmlformats.org/officeDocument/2006/custom-properties" xmlns:vt="http://schemas.openxmlformats.org/officeDocument/2006/docPropsVTypes"><property name="Owner" pid="2"><vt:lpwstr>张三</vt:lpwstr></property></Properties>',
    })
    out = redact_office(data, replacements(data, {"甲公司": "<ORG_001>", "张三": "<PERSON_001>", "13800138000": "<PHONE_001>", "ZHANGSAN": "<PERSON_002>"}), "xlsx")
    text = "\n".join(all_text(out["data"]))
    assert "甲公司" not in text and "张三" not in text and "13800138000" not in text and "ZHANGSAN" not in text
    assert "&L<ORG_001>" in text
    assert all_text(refill_office(out["data"], {"<ORG_001>": "甲公司", "<PERSON_001>": "张三", "<PHONE_001>": "13800138000", "<PERSON_002>": "ZHANGSAN"}, "xlsx")["data"]) == all_text(data)


def test_pptx_slides_notes_masters_layout_smartart_comments_and_chart():
    parts = {
        "ppt/slides/slide1.xml": slide(para("甲公", "司")),
        "ppt/notesSlides/notesSlide1.xml": slide(para("甲公司")),
        "ppt/slideMasters/slideMaster1.xml": slide(para("甲公司")),
        "ppt/slideLayouts/slideLayout1.xml": slide(para("甲公司")),
        "ppt/diagrams/data1.xml": f'<a:root xmlns:a="{A}">{para("甲公司")}</a:root>',
        "ppt/comments/comment1.xml": f'<p:cmLst xmlns:p="{P}"><p:cm authorId="1"><p:text>甲公司</p:text></p:cm></p:cmLst>',
        "ppt/commentAuthors.xml": f'<p:cmAuthorLst xmlns:p="{P}"><p:cmAuthor id="1" name="张三" initials="ZS"/></p:cmAuthorLst>',
        "ppt/charts/chart1.xml": f'<c:chartSpace xmlns:c="{C}"><c:strCache><c:pt idx="0"><c:v>甲公司</c:v></c:pt></c:strCache><c:strLit><c:pt idx="0"><c:v>张三</c:v></c:pt></c:strLit><c:numCache><c:pt idx="0"><c:v>13800138000</c:v></c:pt></c:numCache></c:chartSpace>',
        "ppt/theme/theme1.xml": f'<a:theme xmlns:a="{A}" name="Office"/>',
    }
    data = package("pptx", **parts)
    assert all_text(data, "pptx").count("甲公司") == 7
    assert "13800138000" in all_text(data, "pptx")
    numeric = next(unit for unit in extract_office_texts(data, "pptx") if unit.text == "13800138000")
    assert numeric.readonly
    with pytest.raises(OfficeUnsupportedError, match="readonly_text_replacement"):
        redact_office(data, replacements(data, {"13800138000": "<PHONE_001>"}, "pptx"), "pptx")
    out = redact_office(data, replacements(data, {"甲公司": "<ORG_001>", "张三": "<PERSON_001>"}, "pptx"), "pptx")
    assert "甲公司" not in "".join(all_text(out["data"], "pptx"))
    assert "张三" not in "".join(all_text(out["data"], "pptx"))
    assert read(out["data"], "ppt/theme/theme1.xml") == read(data, "ppt/theme/theme1.xml")
    assert b'<c:v>13800138000</c:v>' in read(out["data"], "ppt/charts/chart1.xml")
    restored = refill_office(out["data"], {"<ORG_001>": "甲公司", "<PERSON_001>": "张三"}, "pptx")
    assert restored["restored_count"] == 9
    assert all_text(restored["data"], "pptx") == all_text(data, "pptx")


def test_units_never_join_cells_paragraphs_or_author_attributes():
    data = package(**{"xl/worksheets/sheet1.xml": sheet('<c t="inlineStr"><is><t>138</t></is></c><c t="inlineStr"><is><t>00138000</t></is></c>')})
    assert all_text(data) == ["138", "00138000"]
    deck = package("pptx", **{"ppt/slides/slide1.xml": slide(para("138") + para("00138000")), "ppt/commentAuthors.xml": f'<p:cmAuthorLst xmlns:p="{P}"><p:cmAuthor name="138" initials="00138000"/></p:cmAuthorLst>'})
    assert all_text(deck, "pptx") == ["138", "00138000", "138", "00138000"]


def test_split_placeholder_refill_and_single_pass_replacement():
    data = package("pptx", **{"ppt/slides/slide1.xml": slide(para("<ORG_", "001>"))})
    restored = refill_office(data, {"<ORG_001>": "A <PERSON_001>", "<PERSON_001>": "张三"}, "pptx")
    assert restored["status"] == "ok"
    assert all_text(restored["data"], "pptx") == ["A <PERSON_001>"]
    assert restored["restored_count"] == 1


@pytest.mark.parametrize("value,expected", [("<ORG_999>", "unknown_placeholder"), ("<ORG_01>", "altered_placeholder"), ("<org_001>", "altered_placeholder"), ("ORG_001", "altered_placeholder")])
def test_bad_placeholder_refill_is_blocked_without_source_fragments(value, expected):
    data = package("pptx", **{"ppt/slides/slide1.xml": slide(para(value))})
    out = refill_office(data, {"<ORG_001>": "SECRET ORIGINAL"}, "pptx")
    assert out["status"] == "blocked"
    assert expected in out["error_types"]
    assert "data" not in out
    assert "SECRET ORIGINAL" not in str(out)
    assert value not in str(out)


def test_readonly_sheet_hyperlink_formula_and_unknown_text():
    cases = [
        {"xl/workbook.xml": f'<workbook xmlns="{S}"><sheets><sheet name="甲公司"/></sheets></workbook>'},
        {"xl/worksheets/sheet1.xml": sheet('<c><f>甲公司</f><v>1</v></c>')},
        {"xl/worksheets/_rels/sheet1.xml.rels": '<Relationships xmlns="' + office.R + '"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/hyperlink" Target="https://example.test/甲公司" TargetMode="External"/></Relationships>'},
    ]
    for parts in cases:
        data = package(**parts)
        found = [unit for unit in extract_office_texts(data, "xlsx") if "甲公司" in unit.text]
        assert len(found) == 1 and found[0].readonly
        with pytest.raises(OfficeUnsupportedError, match="readonly_text_replacement") as error:
            redact_office(data, replacements(data, {"甲公司": "<ORG_001>"}), "xlsx")
        assert found[0].part in str(error.value)  # the error names where the read-only hit is
    # Text of an unknown element cannot break the structure: it is rewritten like any paragraph.
    data = package(**{"xl/unknown.xml": '<unknown><value>甲公司</value></unknown>'})
    found = [unit for unit in extract_office_texts(data, "xlsx") if "甲公司" in unit.text]
    assert len(found) == 1 and not found[0].readonly
    assert all_text(redact_office(data, replacements(data, {"甲公司": "<ORG_001>"}), "xlsx")["data"]) == ["<ORG_001>"]


def test_thumbnail_removed_with_relationship_and_content_type_and_images_warned():
    data = package(**{
        "docProps/thumbnail.jpeg": b"sensitive old preview",
        "xl/media/image1.png": b"not OCRed",
        "_rels/.rels": '<Relationships xmlns="' + office.R + '"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/package/2006/relationships/metadata/thumbnail" Target="docProps/thumbnail.jpeg"/></Relationships>',
        "[Content_Types].xml": '<Types xmlns="' + office.CT + '"><Override PartName="/docProps/thumbnail.jpeg" ContentType="image/jpeg"/></Types>',
    })
    assert {"images_not_inspected", "thumbnail_removed"} <= codes(probe_office(data, "xlsx"))
    out = redact_office(data, [], "xlsx")
    with zipfile.ZipFile(io.BytesIO(out["data"])) as archive:
        assert "docProps/thumbnail.jpeg" not in archive.namelist()
        assert archive.read("xl/media/image1.png") == b"not OCRed"
        assert b"thumbnail" not in archive.read("_rels/.rels")
        assert b"thumbnail" not in archive.read("[Content_Types].xml")


@pytest.mark.parametrize("parts,code", [
    ({"xl/embeddings/embedded.bin": b"private"}, "embedded_object"),
    ({"xl/vbaProject.bin": b"private"}, "macro_present"),
    ({"_xmlsignatures/sig1.xml": "<sig/>"}, "digital_signature"),
    ({"xl/queryTables/queryTable1.xml": "<table/>"}, "unsupported_data_container"),
    ({"xl/worksheets/_rels/sheet1.xml.rels": '<Relationships xmlns="' + office.R + '"><Relationship Target="https://example.test/book.xlsx" TargetMode="External" Type="externalLink"/></Relationships>'}, "external_link"),
    ({"xl/worksheets/sheet1.xml": '<!DOCTYPE r [<!ENTITY x "secret">]><r>&x;</r>'}, "unsafe_xml"),
    ({"xl/worksheets/sheet1.xml": '<r>'}, "invalid_xml"),
    ({"../outside.xml": "<r/>"}, "unsafe_package_path"),
    ({"/absolute.xml": "<r/>"}, "unsafe_package_path"),
])
def test_unsupported_archive_structures_fail_closed(parts, code):
    with pytest.raises(OfficeUnsupportedError) as error:
        probe_office(package(**parts), "xlsx")
    assert error.value.code == code
    assert str(error.value).split(":", 1)[0] == code  # readable detail may follow the code


def test_duplicate_parts_and_package_limits(monkeypatch):
    stream = io.BytesIO()
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        with zipfile.ZipFile(stream, "w") as archive:
            archive.writestr("xl/workbook.xml", "<r/>")
            archive.writestr("xl/workbook.xml", "<r/>")
    with pytest.raises(OfficeUnsupportedError, match="duplicate_package_part"):
        probe_office(stream.getvalue(), "xlsx")
    source = package()
    monkeypatch.setattr(office, "_MAX_PART", 20)
    with pytest.raises(OfficeUnsupportedError, match="package_limit_exceeded"):
        probe_office(source, "xlsx")
    monkeypatch.setattr(office, "_MAX_PART", 10000)
    monkeypatch.setattr(office, "_MAX_ENTRIES", 2)
    with pytest.raises(OfficeUnsupportedError, match="package_limit_exceeded"):
        probe_office(source, "xlsx")
    monkeypatch.setattr(office, "_MAX_ENTRIES", 100)
    monkeypatch.setattr(office, "_MAX_TOTAL", 20)
    with pytest.raises(OfficeUnsupportedError, match="package_limit_exceeded"):
        probe_office(source, "xlsx")


@pytest.mark.parametrize("replacement", [{}, {"part": "x", "paragraph": -1, "start": 0, "end": 1, "placeholder": "<ORG_001>"}, {"part": "x", "paragraph": True, "start": 0, "end": 1, "placeholder": "<ORG_001>"}, {"part": "x", "paragraph": 0, "start": "0", "end": 1, "placeholder": "<ORG_001>"}, {"part": "x", "paragraph": 0, "start": 0, "end": 1, "placeholder": "RAW SECRET"}])
def test_invalid_replacement_fields_have_stable_errors(replacement):
    with pytest.raises(OfficeUnsupportedError, match="invalid_replacement"):
        redact_office(package(), [replacement], "xlsx")


def test_range_overlap_missing_unit_and_invalid_values():
    data = package("pptx", **{"ppt/slides/slide1.xml": slide(para("甲公司"))})
    valid = replacements(data, {"甲公司": "<ORG_001>"}, "pptx")
    for spans in ([dict(valid[0], end=999)], valid + valid, [dict(valid[0], paragraph=999)]):
        with pytest.raises(OfficeUnsupportedError, match="invalid_replacement_range"):
            redact_office(data, spans, "pptx")
    for values in ({"bad": "private"}, {"<ORG_001>": None}, None):
        with pytest.raises(OfficeUnsupportedError, match="invalid_refill_values"):
            refill_office(data, values, "pptx")


def test_formula_numeric_cached_value_cannot_be_turned_into_inline_string():
    data = package(**{"xl/worksheets/sheet1.xml": sheet('<c><f>12345678+1</f><v>13800138000</v></c>')})
    unit = next(unit for unit in extract_office_texts(data, "xlsx") if unit.text == "13800138000")
    assert unit.readonly
    with pytest.raises(OfficeUnsupportedError, match="readonly_text_replacement"):
        redact_office(data, replacements(data, {"13800138000": "<PHONE_001>"}), "xlsx")


def test_formula_string_cache_readonly_and_chart_literal_text_writable():
    book = package(**{"xl/worksheets/sheet1.xml": sheet('<c t="str"><f>CONCAT(A1)</f><v>甲公司</v></c>')})
    assert next(unit for unit in extract_office_texts(book, "xlsx") if unit.text == "甲公司").readonly
    with pytest.raises(OfficeUnsupportedError, match="readonly_text_replacement"):
        redact_office(book, replacements(book, {"甲公司": "<ORG_001>"}), "xlsx")
    deck = package("pptx", **{"ppt/charts/chart1.xml": f'<c:chartSpace xmlns:c="{C}"><c:tx><c:v>甲公司</c:v></c:tx></c:chartSpace>'})
    out = redact_office(deck, replacements(deck, {"甲公司": "<ORG_001>"}, "pptx"), "pptx")
    assert "<ORG_001>" in all_text(out["data"], "pptx")


def test_mixed_content_and_unknown_string_attributes_are_readonly():
    data = package(**{"xl/drawings/vmlDrawing1.vml": '<v:shape xmlns:v="urn:schemas-microsoft-com:vml"><v:textbox><div>联系电话13800138000<span>后缀</span></div></v:textbox></v:shape>', "xl/worksheets/sheet1.xml": f'<worksheet xmlns="{S}"><dataValidations><dataValidation prompt="请联系13800138000"/></dataValidations></worksheet>', "xl/styles.xml": f'<styleSheet xmlns="{S}"><numFmts><numFmt numFmtId="164" formatCode="&quot;甲公司&quot;0"/></numFmts></styleSheet>'})
    units = extract_office_texts(data, "xlsx")
    found = [unit for unit in units if "13800138000" in unit.text or "甲公司" in unit.text]
    assert len(found) == 3
    # Unknown element text (VML textbox) is writable; unknown string attributes
    # (validation prompt, number format) stay read-only and block only when hit.
    readonly = {(unit.part, unit.index) for unit in units if unit.readonly}
    assert [unit.text for unit in found if not unit.readonly] == ["联系电话13800138000"]  # <span> is its own unit
    assert {unit.text for unit in found if unit.readonly} == {"请联系13800138000", '"甲公司"0'}
    writable_only = [r for r in replacements(data, {"13800138000": "<PHONE_001>"}) if (r["part"], r["paragraph"]) not in readonly]
    out = redact_office(data, writable_only, "xlsx")
    assert "联系电话<PHONE_001>" in all_text(out["data"])
    for value in ("13800138000", "甲公司"):
        with pytest.raises(OfficeUnsupportedError, match="readonly_text_replacement") as error:
            redact_office(data, replacements(data, {value: "<X_001>"}), "xlsx")
        assert "sheet1.xml" in str(error.value) or "styles.xml" in str(error.value)


def test_typed_properties_are_readonly_even_for_long_integer():
    data = package(**{"docProps/custom.xml": '<Properties xmlns="http://schemas.openxmlformats.org/officeDocument/2006/custom-properties" xmlns:vt="http://schemas.openxmlformats.org/officeDocument/2006/docPropsVTypes"><property name="Phone" pid="2"><vt:i8>13800138000</vt:i8></property></Properties>'})
    assert next(unit for unit in extract_office_texts(data, "xlsx") if unit.text == "13800138000").readonly
    with pytest.raises(OfficeUnsupportedError, match="readonly_text_replacement"):
        redact_office(data, replacements(data, {"13800138000": "<PHONE_001>"}), "xlsx")


def test_deep_xml_and_modern_ppt_comments_are_explicitly_blocked():
    data = package(**{"xl/worksheets/sheet1.xml": "<n>" * 150 + "secret" + "</n>" * 150})
    with pytest.raises(OfficeUnsupportedError, match="xml_depth_exceeded"):
        probe_office(data, "xlsx")
    deck = package("pptx", **{"ppt/comments/comment1.xml": '<p:cmLst xmlns:p="http://schemas.microsoft.com/office/powerpoint/2018/8/main"><p:cm/></p:cmLst>'})
    with pytest.raises(OfficeUnsupportedError, match="unsupported_comments"):
        probe_office(deck, "pptx")


def test_unknown_numeric_mixed_content_is_detected_in_decimal_form():
    data = package(**{"xl/drawings/vmlDrawing1.vml": '<div>1.3800138E10<span>后缀</span></div>'})
    assert "13800138000" in all_text(data)
    out = redact_office(data, replacements(data, {"13800138000": "<PHONE_001>"}), "xlsx")
    assert "13800138000" not in "".join(all_text(out["data"])) and "<PHONE_001>" in "".join(all_text(out["data"]))


def test_presentation_order_uses_slide_ids_not_filenames():
    data = package("pptx", **{
        "ppt/slides/slide2.xml": slide(para("second")),
        "ppt/slides/slide1.xml": slide(para("first")),
        "ppt/presentation.xml": f'<p:presentation xmlns:p="{P}" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><p:sldIdLst><p:sldId id="256" r:id="rId1"/><p:sldId id="257" r:id="rId2"/></p:sldIdLst></p:presentation>',
        "ppt/_rels/presentation.xml.rels": f'<Relationships xmlns="{office.R}"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/slide" Target="slides/slide1.xml"/><Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/slide" Target="slides/slide2.xml"/></Relationships>',
    })
    assert all_text(data, "pptx")[:2] == ["first", "second"]


def test_invalid_format_and_illegal_xml_refill_values_are_stable():
    for kind in (None, [], "xlsm", "ppt"):
        with pytest.raises(OfficeUnsupportedError, match="unsupported_format"):
            probe_office(package(), kind)
    for value in ("bad\x00value", "bad\ud800value"):
        with pytest.raises(OfficeUnsupportedError, match="invalid_refill_values"):
            refill_office(package(), {"<ORG_001>": value}, "xlsx")


def test_automatic_timestamps_are_removed_instead_of_becoming_invalid_typed_values():
    data = package(**{"docProps/core.xml": '<cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties" xmlns:dcterms="http://purl.org/dc/terms/" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"><dcterms:created xsi:type="dcterms:W3CDTF">2026-10-24T12:00:00Z</dcterms:created><dcterms:modified xsi:type="dcterms:W3CDTF">2026-10-24T12:00:00Z</dcterms:modified><cp:lastPrinted>2026-10-24T12:00:00Z</cp:lastPrinted></cp:coreProperties>'})
    assert not any("2026" in unit.text for unit in extract_office_texts(data, "xlsx"))
    out = redact_office(data, [], "xlsx")
    assert "automatic_timestamps_removed" in codes(out["warnings"])
    assert b"2026" not in read(out["data"], "docProps/core.xml")
    assert b"2026" not in read(refill_office(data, {}, "xlsx")["data"], "docProps/core.xml")


@pytest.mark.parametrize("kind", ["xlsx", "pptx"])
def test_binary_printer_settings_are_removed_with_references_and_page_geometry_kept(kind):
    base = "xl" if kind == "xlsx" else "ppt"
    main_part = "xl/worksheets/sheet1.xml" if kind == "xlsx" else "ppt/presentation.xml"
    rels_part = "xl/worksheets/_rels/sheet1.xml.rels" if kind == "xlsx" else "ppt/_rels/presentation.xml.rels"
    target = "../printerSettings/printerSettings1.bin" if kind == "xlsx" else "printerSettings/printerSettings1.bin"
    main = f'<worksheet xmlns="{S}" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><pageSetup r:id="rId9" orientation="landscape" paperSize="9"/></worksheet>' if kind == "xlsx" else f'<p:presentation xmlns:p="{P}" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><p:sldSz cx="9144000" cy="6858000"/><p:prnPr r:id="rId9"/></p:presentation>'
    data = package(kind, **{
        main_part: main,
        rels_part: f'<Relationships xmlns="{office.R}"><Relationship Id="rId9" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/printerSettings" Target="{target}"/></Relationships>',
        base + "/printerSettings/printerSettings1.bin": b"private printer hostname and owner",
        "[Content_Types].xml": f'<Types xmlns="{office.CT}"><Override PartName="/{base}/printerSettings/printerSettings1.bin" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.printerSettings"/></Types>',
    })
    out = redact_office(data, [], kind)
    assert "printer_settings_removed" in codes(out["warnings"])
    with zipfile.ZipFile(io.BytesIO(out["data"])) as archive:
        assert not any("printerSettings" in name for name in archive.namelist())
    assert b"rId9" not in read(out["data"], main_part)
    assert b"printerSettings" not in read(out["data"], rels_part)
    assert b"printerSettings" not in read(out["data"], "[Content_Types].xml")
    if kind == "xlsx":
        assert b'orientation="landscape"' in read(out["data"], main_part)
    else:
        assert b'cx="9144000"' in read(out["data"], main_part)


RNS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"


def _chart_deck_with_embedded_workbook():
    from openpyxl import Workbook
    workbook = Workbook()
    workbook.active["A1"] = "甲公司"
    workbook.active["B1"] = 850
    buffer = io.BytesIO()
    workbook.save(buffer)
    workbook.close()
    return package("pptx", **{
        "ppt/slides/slide1.xml": slide(para("甲公司")),
        "ppt/charts/chart1.xml": f'<c:chartSpace xmlns:c="{C}" xmlns:r="{RNS}"><c:strCache><c:pt idx="0"><c:v>甲公司</c:v></c:pt></c:strCache><c:externalData r:id="rId1"><c:autoUpdate val="0"/></c:externalData></c:chartSpace>',
        "ppt/charts/_rels/chart1.xml.rels": f'<Relationships xmlns="{office.R}"><Relationship Id="rId1" Type="{RNS}/package" Target="../embeddings/Microsoft_Excel_Worksheet.xlsx"/></Relationships>',
        "ppt/embeddings/Microsoft_Excel_Worksheet.xlsx": buffer.getvalue(),
    })


def test_pptx_chart_embedded_workbook_is_redacted_and_refilled_with_the_deck():
    from openpyxl import load_workbook
    data = _chart_deck_with_embedded_workbook()
    embedded = "ppt/embeddings/Microsoft_Excel_Worksheet.xlsx"
    units = extract_office_texts(data, "pptx")
    assert any(unit.part.startswith(embedded + "!") and unit.text == "甲公司" and not unit.readonly for unit in units)
    assert "embedded_workbook_processed" in codes(probe_office(data, "pptx"))
    out = redact_office(data, replacements(data, {"甲公司": "<ORG_001>"}, "pptx"), "pptx")
    assert "甲公司" not in "".join(all_text(out["data"], "pptx"))
    inner = zipfile.ZipFile(io.BytesIO(read(out["data"], embedded)))
    shared = inner.read("xl/worksheets/sheet1.xml")  # openpyxl writes inline strings
    assert b"&lt;ORG_001&gt;" in shared or b"<ORG_001>" in shared
    assert "甲公司".encode() not in shared
    assert embedded not in out["untouched_hashes"]
    restored = refill_office(out["data"], {"<ORG_001>": "甲公司"}, "pptx")
    assert restored["status"] == "ok"
    # The embedded workbook's automatic timestamps are stripped like the deck's own,
    # so compare content rather than the exact unit list.
    assert all_text(restored["data"], "pptx").count("甲公司") == all_text(data, "pptx").count("甲公司")
    assert "<ORG_001>" not in "".join(all_text(restored["data"], "pptx"))
    restored_book = load_workbook(io.BytesIO(read(restored["data"], embedded)))
    assert restored_book.active["A1"].value == "甲公司" and restored_book.active["B1"].value == 850
    restored_book.close()


def test_pptx_other_embeddings_stay_rejected_and_dangling_chart_data_is_ignored():
    cases = [
        {"ppt/embeddings/oleObject1.bin": b"\x00\x01"},
        {"ppt/embeddings/Book1.xlsm": b"PK"},
    ]
    for parts in cases:
        with pytest.raises(OfficeUnsupportedError, match="embedded_object"):
            extract_office_texts(package("pptx", **parts), "pptx")
    # No such relationship: nothing is hidden behind the reference, only the caches exist.
    dangling = package("pptx", **{"ppt/charts/chart1.xml": f'<c:chartSpace xmlns:c="{C}" xmlns:r="{RNS}"><c:externalData r:id="rId9"/></c:chartSpace>'})
    assert "chart_external_data_ignored" in codes(probe_office(dangling, "pptx"))


def test_schema_uri_attributes_are_never_document_text():
    data = package("pptx", **{"ppt/slides/slide1.xml": f'<p:sld xmlns:p="{P}" xmlns:a="{A}"><p:cSld><p:spTree><p:graphicFrame><a:graphic><a:graphicData uri="http://schemas.openxmlformats.org/drawingml/2006/table"><a:tbl><a:tr><a:tc><a:txBody>{para("甲公司")}</a:txBody></a:tc></a:tr></a:tbl></a:graphicData></a:graphic></p:graphicFrame></p:spTree></p:cSld></p:sld>'})
    assert all_text(data, "pptx") == ["甲公司"]
    assert not any(unit.readonly for unit in extract_office_texts(data, "pptx"))
    out = redact_office(data, replacements(data, {"甲公司": "<ORG_001>"}, "pptx"), "pptx")
    assert all_text(out["data"], "pptx") == ["<ORG_001>"]


def test_unsupported_errors_carry_readable_messages():
    error = OfficeUnsupportedError("embedded_object")
    assert error.code == "embedded_object"
    assert str(error).startswith("embedded_object: ") and "嵌入对象" in str(error)


def test_rejected_containers_name_the_part_and_benign_parts_are_parsed():
    with pytest.raises(OfficeUnsupportedError) as error:
        probe_office(package(**{"xl/queryTables/queryTable1.xml": "<table/>"}), "xlsx")
    assert error.value.code == "unsupported_data_container"
    assert "xl/queryTables/queryTable1.xml" in str(error.value)
    for parts in (
        {"customXml/item1.xml": "<props><company>甲公司</company></props>"},
        {"xl/richData/rdrichvalue.xml": "<rvData><rv><v>甲公司</v></rv></rvData>"},
    ):
        data = package(**parts)
        found = [unit for unit in extract_office_texts(data, "xlsx") if "甲公司" in unit.text]
        assert len(found) == 1 and not found[0].readonly  # element text: rewritten like a paragraph
        assert "甲公司" not in "".join(all_text(redact_office(data, replacements(data, {"甲公司": "<ORG_001>"}), "xlsx")["data"]))


def test_threaded_comments_and_their_authors_round_trip():
    data = package(**{
        "xl/threadedComments/threadedComment1.xml": f'<ThreadedComments xmlns="{office.TC}"><threadedComment ref="A1" personId="{{p1}}"><text>甲公司 请核对</text></threadedComment></ThreadedComments>',
        "xl/persons/person.xml": f'<personList xmlns="{office.TC}"><person displayName="张三" id="{{p1}}" userId="zhangsan@example.test" providerId="AD"/></personList>',
    })
    texts = extract_office_texts(data, "xlsx")
    assert {unit.text for unit in texts if not unit.readonly} >= {"甲公司 请核对", "张三", "zhangsan@example.test"}
    out = redact_office(data, replacements(data, {"甲公司": "<ORG_001>", "张三": "<PERSON_001>", "zhangsan@example.test": "<CONTACT_001>"}), "xlsx")
    joined = "".join(all_text(out["data"], "xlsx"))
    assert "甲公司" not in joined and "张三" not in joined and "zhangsan@example.test" not in joined
    restored = refill_office(out["data"], {"<ORG_001>": "甲公司", "<PERSON_001>": "张三", "<CONTACT_001>": "zhangsan@example.test"}, "xlsx")
    assert restored["status"] == "ok" and all_text(restored["data"], "xlsx") == all_text(data, "xlsx")



def test_pivot_cache_and_table_text_are_redacted_and_refilled_with_the_sheet():
    data = package(**{
        "xl/worksheets/sheet1.xml": sheet('<c t="inlineStr"><is><t>甲公司</t></is></c>'),
        "xl/pivotCache/pivotCacheDefinition1.xml": f'<pivotCacheDefinition xmlns="{S}" refreshOnLoad="1"><cacheSource type="worksheet"><worksheetSource ref="A1:B9" sheet="明细表"/></cacheSource><cacheFields><cacheField name="单位"><sharedItems><s v="甲公司"/><s v="乙公司"/></sharedItems></cacheField><cacheField name="金额"><sharedItems containsNumber="1"><n v="850"/></sharedItems></cacheField></cacheFields></pivotCacheDefinition>',
        "xl/pivotCache/pivotCacheRecords1.xml": f'<pivotCacheRecords xmlns="{S}"><r><x v="0"/><n v="850"/></r><r><s v="甲公司"/><n v="12"/></r></pivotCacheRecords>',
        "xl/pivotTables/pivotTable1.xml": f'<pivotTableDefinition xmlns="{S}" name="甲公司汇总" dataCaption="值"><pivotFields><pivotField name="单位" axis="axisRow"><items><item x="0" n="甲公司（自定义）"/></items></pivotField></pivotFields><dataFields><dataField name="求和项:金额" fld="1"/></dataFields></pivotTableDefinition>',
    })
    assert "pivot_cache_rewritten" in codes(probe_office(data, "xlsx"))
    units = extract_office_texts(data, "xlsx")
    writable = {unit.text for unit in units if not unit.readonly}
    assert {"甲公司", "乙公司", "甲公司汇总", "甲公司（自定义）", "求和项:金额", "单位"} <= writable
    sheet_ref = next(unit for unit in units if unit.text == "明细表")
    assert sheet_ref.readonly  # worksheet names stay structural references
    out = redact_office(data, replacements(data, {"甲公司": "<ORG_001>"}), "xlsx")
    rewritten = "".join(all_text(out["data"], "xlsx"))
    assert "甲公司" not in rewritten
    assert b'<s v="&lt;ORG_001&gt;"/>' in read(out["data"], "xl/pivotCache/pivotCacheDefinition1.xml")
    assert b'<x v="0"/>' in read(out["data"], "xl/pivotCache/pivotCacheRecords1.xml")  # index records untouched
    restored = refill_office(out["data"], {"<ORG_001>": "甲公司"}, "xlsx")
    assert restored["status"] == "ok" and all_text(restored["data"], "xlsx") == all_text(data, "xlsx")



def test_geometry_and_scheme_attributes_are_not_text_units():
    data = package("pptx", **{"ppt/slideLayouts/slideLayout1.xml": f'<p:sldLayout xmlns:p="{P}" xmlns:a="{A}"><p:cSld><p:spTree><p:sp><p:nvSpPr><p:cNvPr id="2" name="标题 甲公司"/></p:nvSpPr><p:spPr><a:custGeom><a:gdLst><a:gd name="connsiteX0" fmla="*/ 0 w 1234"/></a:gdLst><a:pathLst><a:path><a:moveTo><a:pt x="0" y="0"/></a:moveTo></a:path></a:pathLst></a:custGeom><a:solidFill><a:schemeClr val="accent1"/></a:solidFill></p:spPr><p:txBody><a:bodyPr/><a:p><a:r><a:rPr lang="zh-CN"><a:latin typeface="Calibri"/></a:rPr><a:t>甲公司</a:t></a:r></a:p></p:txBody></p:sp></p:spTree></p:cSld></p:sldLayout>'})
    units = extract_office_texts(data, "pptx")
    texts = [unit.text for unit in units]
    assert texts == ["标题 甲公司", "甲公司"]  # no gd@fmla / gd@name / pt@x / schemeClr@val / typeface units
    assert not any(unit.readonly for unit in units)  # shape names are labels: writable
    out = redact_office(data, replacements(data, {"甲公司": "<ORG_001>"}, "pptx"), "pptx")
    assert all_text(out["data"], "pptx") == ["标题 <ORG_001>", "<ORG_001>"]
    restored = refill_office(out["data"], {"<ORG_001>": "甲公司"}, "pptx")
    assert all_text(restored["data"], "pptx") == ["标题 甲公司", "甲公司"]


def test_utf16_parts_are_parsed_and_written_back_as_utf8():
    """WPS writes customXml items as UTF-16 with a BOM; the NUL guard must not reject them."""
    item = '<?xml version="1.0" encoding="UTF-16" standalone="yes"?><props><company>甲公司</company></props>'.encode("utf-16")
    assert item[:2] == b"\xff\xfe" and b"\x00" in item
    data = package(**{"xl/worksheets/sheet1.xml": sheet('<c t="inlineStr"><is><t>甲公司</t></is></c>'), "customXml/item1.xml": item})
    assert all_text(data, "xlsx").count("甲公司") == 2
    out = redact_office(data, replacements(data, {"甲公司": "<ORG_001>"}), "xlsx")
    rewritten = read(out["data"], "customXml/item1.xml")
    assert rewritten[:2] != b"\xff\xfe" and b"\x00" not in rewritten and b"&lt;ORG_001&gt;" in rewritten
    assert "甲公司" not in "".join(all_text(out["data"], "xlsx"))
    restored = refill_office(out["data"], {"<ORG_001>": "甲公司"}, "xlsx")
    assert restored["status"] == "ok" and all_text(restored["data"], "xlsx") == all_text(data, "xlsx")
    with pytest.raises(OfficeUnsupportedError, match="unsafe_xml"):
        probe_office(package(**{"customXml/item1.xml": '<!DOCTYPE r [<!ENTITY x "s">]><r>&x;</r>'.encode("utf-16")}), "xlsx")


def test_external_workbook_link_cache_is_redacted_and_refilled():
    link = (
        f'<externalLink xmlns="{S}" xmlns:r="{office.R}"><externalBook r:id="rId1"><sheetNames><sheetName val="往来台账"/></sheetNames>'
        '<sheetDataSet><sheetData sheetId="0"><row r="1"><cell r="A1" t="str"><v>甲公司</v></cell><cell r="B1"><v>13800138000</v></cell>'
        '<cell r="C1"><v>850.5</v></cell></row></sheetData></sheetDataSet></externalBook></externalLink>'
    )
    rels = f'<Relationships xmlns="{office.R}"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/externalLinkPath" Target="往来台账.xlsx" TargetMode="External"/></Relationships>'
    data = package(**{
        "xl/worksheets/sheet1.xml": sheet('<c t="inlineStr"><is><t>甲公司</t></is></c>'),
        "xl/externalLinks/externalLink1.xml": link,
        "xl/externalLinks/_rels/externalLink1.xml.rels": rels,
    })
    assert "external_link_cache_rewritten" in codes(probe_office(data, "xlsx"))
    units = extract_office_texts(data, "xlsx")
    writable = {unit.text for unit in units if not unit.readonly}
    assert {"甲公司", "13800138000", "往来台账.xlsx"} <= writable
    sheet_name = next(unit for unit in units if unit.text == "往来台账")
    assert sheet_name.readonly and sheet_name.kind == "sheet_name"
    out = redact_office(data, replacements(data, {"甲公司": "<ORG_001>", "13800138000": "<PHONE_001>", "往来台账.xlsx": "<ORG_002>"}), "xlsx")
    rewritten = read(out["data"], "xl/externalLinks/externalLink1.xml")
    assert b"<v>&lt;ORG_001&gt;</v>" in rewritten
    assert b'<cell r="B1" t="str"><v>&lt;PHONE_001&gt;</v></cell>' in rewritten  # no inlineStr in external cells
    assert b"<v>850.5</v>" in rewritten
    assert "甲公司" not in "".join(all_text(out["data"], "xlsx"))
    assert b'Target="&lt;ORG_002&gt;"' in read(out["data"], "xl/externalLinks/_rels/externalLink1.xml.rels")
    restored = refill_office(out["data"], {"<ORG_001>": "甲公司", "<PHONE_001>": "13800138000", "<ORG_002>": "往来台账.xlsx"}, "xlsx")
    assert restored["status"] == "ok" and all_text(restored["data"], "xlsx") == all_text(data, "xlsx")
    with pytest.raises(OfficeUnsupportedError, match="external_link"):
        probe_office(package(**{"xl/worksheets/_rels/sheet1.xml.rels": f'<Relationships xmlns="{office.R}"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/image" Target="file:///C:/x.png" TargetMode="External"/></Relationships>'}), "xlsx")


def test_autofilter_criteria_and_vendor_person_attributes_are_writable():
    body = f'<worksheet xmlns="{S}"><sheetData><row r="1"><c t="inlineStr"><is><t>甲公司</t></is></c></row></sheetData><autoFilter ref="A1:B9"><filterColumn colId="0"><filters><filter val="甲公司"/></filters></filterColumn><filterColumn colId="1"><customFilters><customFilter operator="equal" val="甲公司*"/></customFilters></filterColumn></autoFilter></worksheet>'
    wps = '<comments xmlns="https://web.wps.cn/et/2018/main"><commentChains><item userName="张三" userID="u1" dateTime="2026-08-26"><text><r><t>甲公司</t></r></text></item></commentChains></comments>'
    data = package(**{"xl/worksheets/sheet1.xml": body, "customXml/item2.xml": wps})
    units = extract_office_texts(data, "xlsx")
    writable = {unit.text for unit in units if not unit.readonly}
    assert {"甲公司", "甲公司*", "张三"} <= writable
    assert "u1" not in {unit.text for unit in units}  # ids are not document text
    out = redact_office(data, replacements(data, {"甲公司": "<ORG_001>", "张三": "<PERSON_001>"}), "xlsx")
    rewritten = read(out["data"], "xl/worksheets/sheet1.xml")
    assert b'<filter val="&lt;ORG_001&gt;"/>' in rewritten and b'val="&lt;ORG_001&gt;*"' in rewritten
    assert b'userName="&lt;PERSON_001&gt;"' in read(out["data"], "customXml/item2.xml")
    restored = refill_office(out["data"], {"<ORG_001>": "甲公司", "<PERSON_001>": "张三"}, "xlsx")
    assert restored["status"] == "ok" and all_text(restored["data"], "xlsx") == all_text(data, "xlsx")


def test_readonly_units_report_their_structural_kind():
    data = package(**{
        "xl/workbook.xml": f'<workbook xmlns="{S}"><sheets><sheet name="2026年8月" sheetId="1"/></sheets><definedNames><definedName name="区域">Sheet1!$A$1</definedName></definedNames></workbook>',
        "xl/worksheets/sheet1.xml": sheet('<c t="str"><f>IF(A1="甲公司",1,0)</f><v>1</v></c>'),
    })
    kinds = {unit.text: unit.kind for unit in extract_office_texts(data, "xlsx") if unit.readonly}
    assert kinds["2026年8月"] == "sheet_name" and kinds["Sheet1!$A$1"] == "defined_name" and kinds['IF(A1="甲公司",1,0)'] == "formula"


def test_dangling_chart_external_data_reference_is_ignored():
    """A pasted chart may keep c:externalData whose r:id now names a style part."""
    chart = f'<c:chartSpace xmlns:c="{C}" xmlns:r="{RNS}"><c:chart><c:plotArea><c:barChart><c:ser><c:tx><c:strRef><c:f>Sheet1!$A$1</c:f><c:strCache><c:pt idx="0"><c:v>甲公司</c:v></c:pt></c:strCache></c:strRef></c:tx></c:ser></c:barChart></c:plotArea></c:chart><c:externalData r:id="rId1"><c:autoUpdate val="0"/></c:externalData></c:chartSpace>'
    rels = f'<Relationships xmlns="{office.R}"><Relationship Id="rId1" Type="http://schemas.microsoft.com/office/2011/relationships/chartStyle" Target="style1.xml"/></Relationships>'
    data = package(**{"xl/worksheets/sheet1.xml": sheet('<c t="inlineStr"><is><t>甲公司</t></is></c>'), "xl/charts/chart1.xml": chart, "xl/charts/_rels/chart1.xml.rels": rels, "xl/charts/style1.xml": "<cs:chartStyle xmlns:cs=\"http://schemas.microsoft.com/office/drawing/2012/chartStyle\"/>"})
    assert "chart_external_data_ignored" in codes(probe_office(data, "xlsx"))
    out = redact_office(data, replacements(data, {"甲公司": "<ORG_001>"}), "xlsx")
    assert "甲公司" not in "".join(all_text(out["data"], "xlsx"))
    # A reference to a real non-workbook embedding still fails closed.
    bad_rels = f'<Relationships xmlns="{office.R}"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/package" Target="../embeddings/oleObject1.bin"/></Relationships>'
    with pytest.raises(OfficeUnsupportedError, match="embedded_object"):
        probe_office(package(**{"xl/charts/chart1.xml": chart, "xl/charts/_rels/chart1.xml.rels": bad_rels}), "xlsx")
