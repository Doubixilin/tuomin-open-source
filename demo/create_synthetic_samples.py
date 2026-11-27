"""Rebuild the two fictional Office fixtures using only the Python stdlib."""
from pathlib import Path
from xml.sax.saxutils import escape
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo


REL = "http://schemas.openxmlformats.org/package/2006/relationships"
OFFICE = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
CONTENT = "http://schemas.openxmlformats.org/package/2006/content-types"
WORD = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
SHEET = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
DECL = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'


def _write(destination, parts):
    with ZipFile(destination, "w") as archive:
        for name, value in parts.items():
            member = ZipInfo(name, date_time=(2026, 10, 24, 0, 0, 0))
            member.compress_type = ZIP_DEFLATED
            archive.writestr(member, (DECL + value).encode("utf-8"))


def _types(overrides):
    return (f'<Types xmlns="{CONTENT}"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
            '<Default Extension="xml" ContentType="application/xml"/>' + ''.join(
                f'<Override PartName="/{name}" ContentType="{kind}"/>' for name, kind in overrides.items()) + '</Types>')


def create_samples(directory):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    paragraphs = [
        "Tuomin 合成合同演示", "本文件所有人物、单位、编号与金额均为虚构，仅用于功能演示。",
        "合同编号：DEMO-2026-001", "甲方：虚构星橙工程有限公司；联系人：许星舟。",
        "乙方：虚构云帆技术有限公司；联系人：林月禾。", "演示事项：为虚构展厅制作两套纸质模型。",
        "合成合同金额：人民币 128000 元。请确认双方信息后提交演示记录。",
    ]
    document = f'<w:document xmlns:w="{WORD}"><w:body>' + ''.join(
        '<w:p><w:r><w:t>' + escape(text) + '</w:t></w:r></w:p>' for text in paragraphs
    ) + '<w:sectPr><w:pgSz w:w="11906" w:h="16838"/><w:pgMar w:top="1440" w:right="1440" w:bottom="1440" w:left="1440"/></w:sectPr></w:body></w:document>'
    _write(directory / "synthetic-contract.docx", {
        "[Content_Types].xml": _types({"word/document.xml": "application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"}),
        "_rels/.rels": f'<Relationships xmlns="{REL}"><Relationship Id="rId1" Type="{OFFICE}/officeDocument" Target="word/document.xml"/></Relationships>',
        "word/document.xml": document,
    })
    rows = [
        ["合成合同台账（所有内容均为虚构）"],
        ["合同编号", "甲方单位", "甲方联系人", "乙方单位", "乙方联系人", "金额（元）"],
        ["DEMO-2026-001", "虚构星橙工程有限公司", "许星舟", "虚构云帆技术有限公司", "林月禾", 128000],
        ["DEMO-2026-002", "虚构云帆技术有限公司", "林月禾", "虚构星橙工程有限公司", "许星舟", 68000],
    ]
    sheet_rows = []
    for row_number, row in enumerate(rows, 1):
        cells = []
        for column, value in enumerate(row):
            address = f"{chr(65 + column)}{row_number}"
            if isinstance(value, int):
                cells.append(f'<c r="{address}"><v>{value}</v></c>')
            else:
                cells.append(f'<c r="{address}" t="inlineStr"><is><t>{escape(value)}</t></is></c>')
        sheet_rows.append(f'<row r="{row_number}">' + ''.join(cells) + '</row>')
    _write(directory / "synthetic-register.xlsx", {
        "[Content_Types].xml": _types({
            "xl/workbook.xml": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml",
            "xl/worksheets/sheet1.xml": "application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml",
            "xl/styles.xml": "application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml",
        }),
        "_rels/.rels": f'<Relationships xmlns="{REL}"><Relationship Id="rId1" Type="{OFFICE}/officeDocument" Target="xl/workbook.xml"/></Relationships>',
        "xl/workbook.xml": f'<workbook xmlns="{SHEET}" xmlns:r="{OFFICE}"><sheets><sheet name="合成合同台账" sheetId="1" r:id="rId1"/></sheets></workbook>',
        "xl/_rels/workbook.xml.rels": f'<Relationships xmlns="{REL}"><Relationship Id="rId1" Type="{OFFICE}/worksheet" Target="worksheets/sheet1.xml"/><Relationship Id="rId2" Type="{OFFICE}/styles" Target="styles.xml"/></Relationships>',
        "xl/worksheets/sheet1.xml": f'<worksheet xmlns="{SHEET}"><dimension ref="A1:F4"/><sheetData>' + ''.join(sheet_rows) + '</sheetData></worksheet>',
        "xl/styles.xml": f'<styleSheet xmlns="{SHEET}"><fonts count="1"><font><sz val="11"/><name val="Calibri"/></font></fonts><fills count="2"><fill><patternFill patternType="none"/></fill><fill><patternFill patternType="gray125"/></fill></fills><borders count="1"><border/></borders><cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs><cellXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/></cellXfs><cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles></styleSheet>',
    })


if __name__ == "__main__":
    create_samples(Path(__file__).resolve().parent / "samples")
