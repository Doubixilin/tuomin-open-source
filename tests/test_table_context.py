"""Column headers as rule clues: a bare account number under「收款账号」."""
from __future__ import annotations

import base64
import io

from test_documents_api import _bound_client, stub_ner  # noqa: F401  (fixture)
from tuomin_gateway.detectors.rules import RuleDetector
from tuomin_gateway.detectors.table_context import TableCell, TableHeaderRuleDetector, markdown_table_cells
from tuomin_gateway.document.docx_patch import extract_paragraph_texts
from tuomin_gateway.document.office_patch import extract_office_texts

ACCOUNT = "6222021001987654321"
ORG = "示例建设单位A"


def test_bare_account_needs_a_clue_in_plain_rules():
    assert RuleDetector().detect(ACCOUNT) == []
    assert [s.label for s in RuleDetector().detect("收款账号：" + ACCOUNT)] == ["BANK_ACCOUNT"]


def test_header_clue_maps_back_to_the_value_only():
    text = f"其他内容\n{ACCOUNT}"
    start = text.index(ACCOUNT)
    spans = TableHeaderRuleDetector([TableCell(start, start + len(ACCOUNT), ("收款账号",))]).detect(text)
    assert [(s.label, text[s.start:s.end], s.source) for s in spans] == [("BANK_ACCOUNT", ACCOUNT, "rule")]
    assert spans[0].metadata["context"] == "table_header"


def test_header_without_clue_or_with_digits_adds_nothing():
    cells = [TableCell(0, len(ACCOUNT), ("备注",)), TableCell(0, len(ACCOUNT), ("2026 收款账号",))]
    assert TableHeaderRuleDetector(cells).detect(ACCOUNT) == []


def test_markdown_table_cells_and_escaped_pipes():
    text = (
        "前言\n\n"
        "| 单位 | 收款账号 | 备注 |\n"
        "| --- | --- | --- |\n"
        f"| {ORG} | {ACCOUNT} | a \\| b |\n"
        "\n"
        "| 不是表格 |\n"
    )
    cells = markdown_table_cells(text)
    assert [(text[c.start:c.end], c.headers) for c in cells] == [
        (ORG, ("单位",)), (ACCOUNT, ("收款账号",)), ("a \\| b", ("备注",)),
    ]
    spans = TableHeaderRuleDetector().detect(text)
    assert [(s.label, text[s.start:s.end]) for s in spans] == [("BANK_ACCOUNT", ACCOUNT)]


def _ledger() -> bytes:
    from openpyxl import Workbook

    workbook = Workbook()
    sheet = workbook.active
    sheet.append(["单位", "收款账号", "备注"])
    sheet.append([ORG, ACCOUNT, ACCOUNT])
    sheet["D5"] = ACCOUNT  # same shared string, column without a header
    output = io.BytesIO()
    workbook.save(output)
    return output.getvalue()


def test_xlsx_inline_cells_carry_their_column_header():
    units = [u for u in extract_office_texts(_ledger(), "xlsx") if u.part.startswith("xl/worksheets/")]
    assert [(u.text, u.context) for u in units] == [
        ("单位", ()), ("收款账号", ()), ("备注", ()),  # headers themselves get no context
        (ORG, ("单位",)), (ACCOUNT, ("收款账号",)), (ACCOUNT, ("备注",)),
        (ACCOUNT, ()),  # D5: column without a header
    ]


def _shared_strings_ledger() -> bytes:
    """Excel's own layout: cells point into sharedStrings.xml by index."""
    import zipfile

    ns = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
    rel = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
    strings = ["单位", "收款账号", ORG, ACCOUNT, "备注"]
    cells = [("A1", 0), ("B1", 1), ("C1", 4), ("A2", 2), ("B2", 3), ("C2", 3)]
    rows: dict[str, list[str]] = {}
    for ref, index in cells:
        rows.setdefault(ref[1:], []).append(f'<c r="{ref}" t="s"><v>{index}</v></c>')
    parts = {
        "[Content_Types].xml": '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
        '<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
        '<Override PartName="/xl/sharedStrings.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sharedStrings+xml"/></Types>',
        "_rels/.rels": '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        f'<Relationship Id="rId1" Type="{rel}/officeDocument" Target="xl/workbook.xml"/></Relationships>',
        "xl/workbook.xml": f'<workbook xmlns="{ns}" xmlns:r="{rel}"><sheets><sheet name="S" sheetId="1" r:id="rId1"/></sheets></workbook>',
        "xl/_rels/workbook.xml.rels": '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        f'<Relationship Id="rId1" Type="{rel}/worksheet" Target="worksheets/sheet1.xml"/>'
        f'<Relationship Id="rId2" Type="{rel}/sharedStrings" Target="sharedStrings.xml"/></Relationships>',
        "xl/worksheets/sheet1.xml": f'<worksheet xmlns="{ns}"><sheetData>'
        + "".join(f'<row r="{r}">{"".join(c)}</row>' for r, c in rows.items()) + "</sheetData></worksheet>",
        "xl/sharedStrings.xml": f'<sst xmlns="{ns}">' + "".join(f"<si><t>{s}</t></si>" for s in strings) + "</sst>",
    }
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as package:
        for name, text in parts.items():
            package.writestr(name, text)
    return output.getvalue()


def test_xlsx_shared_string_collects_every_header_it_sits_under():
    contexts = {u.text: u.context for u in extract_office_texts(_shared_strings_ledger(), "xlsx") if u.part == "xl/sharedStrings.xml"}
    assert contexts[ACCOUNT] == ("收款账号", "备注")  # one shared string, two headed columns
    assert contexts[ORG] == ("单位",)
    assert contexts["收款账号"] == ()


def test_original_format_xlsx_shared_strings(tmp_path, stub_ner):  # noqa: F811
    client = _client(tmp_path)
    response = client.post("/api/v1/documents/redact-office", params={"filename": "ledger.xlsx", "app_id": "wb_docs"}, content=_shared_strings_ledger())
    assert response.status_code == 200, response.text
    texts = [u.text for u in extract_office_texts(base64.b64decode(response.json()["redacted_office_b64"]), "xlsx")]
    assert all(ACCOUNT not in t for t in texts)


def _ledger_docx() -> bytes:
    from docx import Document

    document = Document()
    document.add_paragraph(f"付款账户见下表：{ORG}")
    table = document.add_table(rows=2, cols=2)
    table.cell(0, 0).text, table.cell(0, 1).text = "单位", "收款账号"
    table.cell(1, 0).text, table.cell(1, 1).text = ORG, ACCOUNT
    output = io.BytesIO()
    document.save(output)
    return output.getvalue()


def test_docx_table_paragraphs_carry_their_column_header():
    paragraphs = {p.text: p.context for p in extract_paragraph_texts(_ledger_docx())}
    assert paragraphs[ACCOUNT] == ("收款账号",)
    assert paragraphs[ORG] == ("单位",)
    assert paragraphs[f"付款账户见下表：{ORG}"] == ()


def _client(tmp_path):
    return _bound_client(tmp_path, [{"canonical_value": ORG, "aliases": [], "label": "ORG", "status": "active"}])


def test_original_format_xlsx_redacts_bare_account_under_header(tmp_path, stub_ner):  # noqa: F811
    client = _client(tmp_path)
    response = client.post("/api/v1/documents/redact-office", params={"filename": "ledger.xlsx", "app_id": "wb_docs"}, content=_ledger())
    assert response.status_code == 200, response.text
    texts = [u.text for u in extract_office_texts(base64.b64decode(response.json()["redacted_office_b64"]), "xlsx")]
    assert all(ACCOUNT not in t for t in texts)
    assert "<BANK_ACCOUNT_001>" in texts


def test_original_format_docx_redacts_bare_account_in_table(tmp_path, stub_ner):  # noqa: F811
    client = _client(tmp_path)
    response = client.post("/api/v1/documents/redact-docx", params={"filename": "ledger.docx", "app_id": "wb_docs"}, content=_ledger_docx())
    assert response.status_code == 200, response.text
    texts = [p.text for p in extract_paragraph_texts(base64.b64decode(response.json()["redacted_docx_b64"]))]
    assert all(ACCOUNT not in t for t in texts)
    assert "<BANK_ACCOUNT_001>" in texts


def test_parsed_markdown_redacts_bare_account_under_header(tmp_path, stub_ner):  # noqa: F811
    client = _client(tmp_path)
    response = client.post("/api/v1/documents/parse", params={"filename": "ledger.xlsx", "app_id": "wb_docs"}, content=_ledger())
    assert response.status_code == 200, response.text
    markdown = response.json()["redacted_markdown"]
    assert ACCOUNT not in markdown
    assert "<BANK_ACCOUNT_001>" in markdown
