"""Tests for clean-Markdown rendering of Document IR blocks."""

from __future__ import annotations

from tuomin_gateway.document.markdown import render_clean_markdown
from tuomin_gateway.document.vendor.document_ir import DocumentBlock


def _block(
    block_id: str,
    order: int,
    block_type: str,
    text: str,
    *,
    metadata: dict | None = None,
) -> DocumentBlock:
    return DocumentBlock(
        source_id="M001",
        source_uid="sha256:" + "a" * 64,
        block_id=block_id,
        order=order,
        block_type=block_type,
        text=text,
        extraction_method="test",
        legacy_locators=[],
        metadata=dict(metadata or {}),
    )


def _spanned_lines(render, block_id: str) -> list[str]:
    span = next(item for item in render.spans if item.block_id == block_id)
    return render.text.splitlines()[span.start_line - 1 : span.end_line]


def test_orders_blocks_renders_heading_levels_and_skips_empty_blocks() -> None:
    blocks = [
        _block("plain", 5, "line", "Plain line"),
        _block("empty", 4, "paragraph", "  \n  "),
        _block("outline", 3, "paragraph", "Deep", metadata={"outline_level": "8"}),
        _block("style", 1, "paragraph", "Styled", metadata={"style_id": "Heading2"}),
    ]

    first = render_clean_markdown(blocks)
    second = render_clean_markdown(blocks)

    assert first == second
    assert first.text.encode("utf-8") == second.text.encode("utf-8")
    assert first.text == "## Styled\n\n###### Deep\n\nPlain line\n"
    assert first.text.endswith("\n") and not first.text.endswith("\n\n")
    assert [span.block_id for span in first.spans] == ["style", "outline", "plain"]
    assert _spanned_lines(first, "style") == ["## Styled"]
    assert _spanned_lines(first, "outline") == ["###### Deep"]
    assert _spanned_lines(first, "plain") == ["Plain line"]


def test_docx_cells_form_one_pipe_table_with_escaped_pipes_and_spans() -> None:
    blocks = [
        _block("tail", 10, "paragraph", "Tail"),
        _block("t1-r2-cC2", 9, "table_cell", "2", metadata={"table": 1, "row": 2, "column": 3}),
        _block("t1-r1-cA1", 3, "table_cell", "Name", metadata={"table": 1, "row": 1, "column": 1}),
        _block("t1", 2, "table", "summary", metadata={"table": 1}),
        _block("t1-r2-cB2", 8, "table_cell", "A|B", metadata={"table": 1, "row": 2, "column": 2}),
        _block("t1-r1-cC1", 5, "table_cell", "Qty", metadata={"table": 1, "row": 1, "column": 3}),
        _block("title", 1, "paragraph", "Contract", metadata={"outline_level": 0}),
        _block("t1-r2-cA2", 7, "table_cell", "Alice", metadata={"table": 1, "row": 2, "column": 1}),
        _block("t1-r1-cB1", 4, "table_cell", "Note", metadata={"table": 1, "row": 1, "column": 2}),
        _block("t1-r1", 6, "table_row", "duplicate summary", metadata={"table": 1, "row": 1}),
    ]

    render = render_clean_markdown(blocks)

    assert render.text == (
        "# Contract\n\n"
        "| Name | Note | Qty |\n"
        "| --- | --- | --- |\n"
        "| Alice | A\\|B | 2 |\n\n"
        "Tail\n"
    )
    assert _spanned_lines(render, "t1-r1-cA1") == ["| Name | Note | Qty |"]
    assert _spanned_lines(render, "t1-r1-cB1") == ["| Name | Note | Qty |"]
    assert _spanned_lines(render, "t1-r2-cB2") == ["| Alice | A\\|B | 2 |"]
    assert _spanned_lines(render, "tail") == ["Tail"]
    assert "t1" not in {span.block_id for span in render.spans}
    assert "t1-r1" not in {span.block_id for span in render.spans}


def test_ancillary_blocks_are_optional_and_marked_when_included() -> None:
    blocks = [
        _block("header", 1, "header", "Header text"),
        _block("body", 2, "paragraph", "Body"),
        _block("footnote", 3, "footnote", "Footnote text"),
        _block("image", 4, "image", "[Image not OCRed: chart.png]"),
    ]

    default = render_clean_markdown(blocks)
    included = render_clean_markdown(blocks, include_ancillary=True)

    assert default.text == "Body\n"
    assert [span.block_id for span in default.spans] == ["body"]
    assert included.text == (
        "<!-- tuomin:header --> Header text\n\n"
        "Body\n\n"
        "<!-- tuomin:footnote --> Footnote text\n\n"
        "<!-- tuomin:image --> [Image not OCRed: chart.png]\n"
    )
    assert _spanned_lines(included, "header") == ["<!-- tuomin:header --> Header text"]
    assert _spanned_lines(included, "image") == [
        "<!-- tuomin:image --> [Image not OCRed: chart.png]"
    ]


def test_spreadsheet_cells_form_one_table_per_sheet_using_display_values() -> None:
    blocks = [
        _block("workbook", 1, "spreadsheet_workbook", "metadata"),
        _block("sheet001", 2, "spreadsheet_sheet", "sheet metadata", metadata={"sheet_index": 1, "sheet_name": "People"}),
        _block("a1", 3, "spreadsheet_cell", "json-a1", metadata={"sheet_index": 1, "sheet_name": "People", "row": 1, "column": 1, "coordinate": "A1", "value": "Name"}),
        _block("b1", 4, "spreadsheet_cell", "json-b1", metadata={"sheet_index": 1, "sheet_name": "People", "row": 1, "column": 2, "coordinate": "B1", "value": "Role"}),
        _block("a2", 5, "spreadsheet_cell", "json-a2", metadata={"sheet_index": 1, "sheet_name": "People", "row": 2, "column": 1, "coordinate": "A2", "value": "Alice"}),
        _block("b2", 6, "spreadsheet_cell", "json-b2", metadata={"sheet_index": 1, "sheet_name": "People", "row": 2, "column": 2, "coordinate": "B2", "value": "Legal\nReviewer"}),
    ]

    render = render_clean_markdown(blocks)

    assert render.text == (
        "| Name | Role |\n"
        "| --- | --- |\n"
        "| Alice | Legal<br>Reviewer |\n"
    )
    assert _spanned_lines(render, "a1") == ["| Name | Role |"]
    assert _spanned_lines(render, "b2") == ["| Alice | Legal<br>Reviewer |"]
    assert "workbook" not in {span.block_id for span in render.spans}
    assert "sheet001" not in {span.block_id for span in render.spans}


def test_no_emitted_blocks_still_has_exactly_one_trailing_newline() -> None:
    render = render_clean_markdown([_block("empty", 1, "paragraph", " \t ")])
    assert render.text == "\n"
    assert render.spans == []
