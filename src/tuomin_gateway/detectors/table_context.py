"""表头作为规则线索：表格单元里往往只有值，线索词在列头上。

规则层不做裸号匹配（无线索的长数字串无法与普通编号区分），所以「收款账号」一列里
单独的 19 位卡号，在正文里能认、在表格里认不出。这里把每个数据单元按「列头：值」
拼起来交给同一套规则，只保留落在值里的命中，再映射回原文位置。

单元来源有两种：原格式工作台按文件结构给出（XLSX 工作表、DOCX 表格），解析
预览从渲染出的 Markdown 表格里切分。命中仍是 ``rule`` 来源，照常参与融合与策略。
"""
from __future__ import annotations

import dataclasses
import re

from tuomin_gateway.detectors.base import BaseDetector
from tuomin_gateway.detectors.rules import RuleDetector
from tuomin_gateway.schemas import DetectionSpan

# 列头过长时更像一段正文而不是字段名，拼进去反而可能制造线索。
_MAX_HEADER_CHARS = 20
_SEPARATOR_CELL = re.compile(r"\s*:?-{3,}:?\s*")


@dataclasses.dataclass(frozen=True)
class TableCell:
    """``text[start:end]`` 是一个数据单元的值，``headers`` 是它所在列的列头。"""

    start: int
    end: int
    headers: tuple[str, ...]


def usable_header(header: str) -> str | None:
    header = " ".join(header.split())
    if not header or len(header) > _MAX_HEADER_CHARS or any(ch.isdigit() for ch in header):
        return None
    return header


class TableHeaderRuleDetector(BaseDetector):
    name = "table_header_rule"
    version = "table-header-2026.11.03"

    def __init__(self, cells: list[TableCell] | None = None) -> None:
        # None：从文本里的 Markdown 表格切分；列表：调用方按文件结构给出。
        self._cells = cells
        self._rules = RuleDetector()

    def detect(self, text: str) -> list[DetectionSpan]:
        cells = self._cells if self._cells is not None else markdown_table_cells(text)
        spans: list[DetectionSpan] = []
        for cell in cells:
            value = text[cell.start:cell.end]
            if not value.strip():
                continue
            for header in dict.fromkeys(cell.headers):
                header = usable_header(header)
                if header is None:
                    continue
                probe = f"{header}：{value}"
                offset = len(header) + 1
                for span in self._rules.detect(probe):
                    if span.start < offset:
                        continue
                    spans.append(dataclasses.replace(
                        span,
                        start=span.start - offset + cell.start,
                        end=span.end - offset + cell.start,
                        metadata={**span.metadata, "context": "table_header"},
                    ))
        # 同一个值出现在没有线索的列（备注、无表头的格）也是同一个账号：一并标出，
        # 不靠下游的已知值补检（单次脱敏路径没有）。只认前后不接字母数字的完整出现。
        found = {(text[s.start:s.end], s.label): s for s in spans}
        for (value, _), span in found.items():
            pattern = re.compile(r"(?<![0-9A-Za-z])" + re.escape(value) + r"(?![0-9A-Za-z])")
            for match in pattern.finditer(text):
                if match.start() != span.start:
                    spans.append(dataclasses.replace(span, start=match.start(), end=match.end()))
        return spans


def markdown_table_cells(text: str) -> list[TableCell]:
    """切分 ``| 列头 | … |`` + ``| --- | … |`` 开头的 Markdown 表格的数据单元。"""
    cells: list[TableCell] = []
    lines: list[tuple[int, str]] = []
    cursor = 0
    for line in text.split("\n"):
        lines.append((cursor, line))
        cursor += len(line) + 1

    index = 0
    while index + 1 < len(lines):
        header_start, header_line = lines[index]
        _, separator_line = lines[index + 1]
        header_cells = _row_cells(header_start, header_line)
        separator = _row_cells(0, separator_line)
        if not header_cells or not separator or not all(_SEPARATOR_CELL.fullmatch(separator_line[s:e]) for s, e in separator):
            index += 1
            continue
        headers = [_unescape(text[s:e].strip()) for s, e in header_cells]
        index += 2
        while index < len(lines):
            row_start, row_line = lines[index]
            row = _row_cells(row_start, row_line)
            if not row:
                break
            for column, (start, end) in enumerate(row):
                if column < len(headers) and headers[column]:
                    cells.append(TableCell(start, end, (headers[column],)))
            index += 1
    return cells


def _row_cells(line_start: int, line: str) -> list[tuple[int, int]]:
    """``| a | b |`` 一行里每个单元的原文区间（去掉两端空白，跳过转义的 ``\\|``）。"""
    stripped = line.rstrip()
    if not stripped.startswith("|") or not stripped.endswith("|") or len(stripped) < 2:
        return []
    bounds = [i for i, ch in enumerate(stripped) if ch == "|" and (i == 0 or stripped[i - 1] != "\\")]
    result: list[tuple[int, int]] = []
    for left, right in zip(bounds, bounds[1:]):
        start, end = left + 1, right
        while start < end and stripped[start].isspace():
            start += 1
        while end > start and stripped[end - 1].isspace():
            end -= 1
        result.append((line_start + start, line_start + end))
    return result


def _unescape(text: str) -> str:
    return text.replace("\\|", "|")
