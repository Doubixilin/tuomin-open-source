from __future__ import annotations

import datetime as dt
import json
import math
import re
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterable

from .document_ir import DocumentBlock


_SOURCE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
_FORMULA_WARNING = "FORMULA_CACHE_NOT_RECALCULATED"
_MISSING_CACHE_WARNING = "FORMULA_CACHED_VALUE_MISSING"
_XLS_FORMULA_WARNING = "XLS_FORMULA_UNAVAILABLE"


def extract_spreadsheet(
    path: Path,
    source_id: str,
    source_uid: str,
) -> tuple[list[DocumentBlock], list[dict[str, Any]], dict[str, Any]]:
    """Extract a spreadsheet into deterministic workbook/sheet/cell blocks.

    XLSX and XLSM workbooks are opened twice: once with formulas visible and
    once with ``data_only=True`` for the cached results stored in the file.
    No calculation engine is invoked.  Legacy XLS files expose only cached
    values through xlrd, so the result always carries a capability-blocking
    formula-audit warning.
    """

    source = Path(path).expanduser().resolve()
    if not source.is_file():
        raise FileNotFoundError(f"spreadsheet does not exist: {source}")
    if not _SOURCE_ID.fullmatch(source_id):
        raise ValueError(
            "source_id must start with an ASCII letter or digit and contain "
            "1 to 64 ASCII letters, digits, underscores, or hyphens"
        )
    if not source_uid.strip():
        raise ValueError("source_uid is required")

    extension = source.suffix.lower()
    if extension in {".xlsx", ".xlsm"}:
        return _extract_openpyxl(source, source_id, source_uid, extension)
    if extension == ".xls":
        return _extract_xlrd(source, source_id, source_uid)
    raise ValueError(f"unsupported spreadsheet extension: {extension or '<none>'}")


def _extract_openpyxl(
    path: Path,
    source_id: str,
    source_uid: str,
    extension: str,
) -> tuple[list[DocumentBlock], list[dict[str, Any]], dict[str, Any]]:
    try:
        import openpyxl
        from openpyxl.utils import get_column_letter
    except ImportError as exc:  # pragma: no cover - depends on runtime profile
        raise RuntimeError("openpyxl is required for XLSX/XLSM extraction") from exc

    keep_vba = extension == ".xlsm"
    load_args = {
        "read_only": False,
        "keep_links": True,
        "keep_vba": keep_vba,
    }
    formula_book = openpyxl.load_workbook(path, data_only=False, **load_args)
    try:
        cached_book = openpyxl.load_workbook(path, data_only=True, **load_args)
    except Exception:
        formula_book.close()
        raise

    warnings: list[dict[str, Any]] = []
    sheet_records: list[dict[str, Any]] = []
    formula_count = 0
    cached_formula_count = 0
    missing_cache_count = 0
    nonempty_cell_count = 0
    hidden_sheet_count = 0
    hidden_row_count = 0
    hidden_column_count = 0
    merged_range_count = 0

    try:
        defined_names = _openpyxl_defined_names(formula_book)
        external_links = _openpyxl_external_links(formula_book)
        calculation = _openpyxl_calculation(formula_book)

        cached_by_title = {sheet.title: sheet for sheet in cached_book.worksheets}
        for sheet_index, formula_sheet in enumerate(formula_book.worksheets, 1):
            cached_sheet = cached_by_title.get(formula_sheet.title)
            if cached_sheet is None:
                raise ValueError(
                    f"cached workbook is missing sheet {formula_sheet.title!r}"
                )

            sheet_state = str(getattr(formula_sheet, "sheet_state", "visible"))
            if sheet_state != "visible":
                hidden_sheet_count += 1
            hidden_rows = sorted(
                int(index)
                for index, dimension in formula_sheet.row_dimensions.items()
                if bool(getattr(dimension, "hidden", False))
            )
            hidden_columns = _openpyxl_hidden_columns(formula_sheet)
            hidden_row_count += len(hidden_rows)
            hidden_column_count += sum(
                item[1] - item[0] + 1 for item in hidden_columns
            )

            merged = sorted(
                (
                    int(item.min_row),
                    int(item.min_col),
                    int(item.max_row),
                    int(item.max_col),
                    str(item),
                )
                for item in formula_sheet.merged_cells.ranges
            )
            merged_range_count += len(merged)

            coordinates = _openpyxl_cell_coordinates(formula_sheet, cached_sheet)
            cells: list[dict[str, Any]] = []
            for row_index, column_index in coordinates:
                formula_cell = formula_sheet.cell(row=row_index, column=column_index)
                cached_cell = cached_sheet.cell(row=row_index, column=column_index)
                raw_value = formula_cell.value
                cached_value = cached_cell.value
                is_formula = bool(
                    formula_cell.data_type == "f"
                    or (isinstance(raw_value, str) and raw_value.startswith("="))
                )
                if raw_value is None and cached_value is None:
                    continue

                coordinate = f"{get_column_letter(column_index)}{row_index}"
                locator = f"{source_id}#sheet[{formula_sheet.title}]!{coordinate}"
                formula = str(raw_value) if is_formula else None
                display_value = cached_value if is_formula else raw_value
                if is_formula:
                    formula_count += 1
                    if cached_value is None:
                        missing_cache_count += 1
                        warnings.append(
                            _warning(
                                _MISSING_CACHE_WARNING,
                                "formula cell has no cached result in the workbook",
                                source_id=source_id,
                                locator=locator,
                                capability_blocking=True,
                                capability="formula_result",
                            )
                        )
                    else:
                        cached_formula_count += 1

                merged_range = _merged_range_for_cell(
                    row_index, column_index, merged
                )
                hidden_column = _column_is_hidden(column_index, hidden_columns)
                warning_codes: list[str] = []
                if is_formula:
                    warning_codes.append(_FORMULA_WARNING)
                    if cached_value is None:
                        warning_codes.append(_MISSING_CACHE_WARNING)
                cells.append(
                    {
                        "coordinate": coordinate,
                        "row": row_index,
                        "column": column_index,
                        "raw_value": _json_value(raw_value),
                        "value": _json_value(display_value),
                        "formula": formula,
                        "cached_value": _json_value(cached_value)
                        if is_formula
                        else None,
                        "is_formula": is_formula,
                        "formula_cached": bool(is_formula and cached_value is not None),
                        "data_type": str(formula_cell.data_type or ""),
                        "cached_data_type": str(cached_cell.data_type or ""),
                        "number_format": str(formula_cell.number_format or ""),
                        "hidden_row": row_index in hidden_rows,
                        "hidden_column": hidden_column,
                        "merged_range": merged_range,
                        "is_merged_anchor": bool(
                            merged_range
                            and merged_range.split(":", 1)[0] == coordinate
                        ),
                        "legacy_locator": locator,
                        "warning_codes": warning_codes,
                    }
                )
            nonempty_cell_count += len(cells)
            sheet_records.append(
                {
                    "sheet_index": sheet_index,
                    "sheet_name": formula_sheet.title,
                    "sheet_state": sheet_state,
                    "max_row": int(formula_sheet.max_row or 0),
                    "max_column": int(formula_sheet.max_column or 0),
                    "hidden_rows": hidden_rows,
                    "hidden_columns": [
                        {
                            "min": start,
                            "max": end,
                            "range": _column_range(start, end, get_column_letter),
                        }
                        for start, end in hidden_columns
                    ],
                    "merged_ranges": [item[4] for item in merged],
                    "cells": cells,
                }
            )

        if formula_count:
            warnings.insert(
                0,
                _warning(
                    _FORMULA_WARNING,
                    "formula text and stored cached values were preserved; no recalculation was performed",
                    source_id=source_id,
                    capability_blocking=True,
                    capability="formula_result_freshness",
                    details={
                        "formula_cells": formula_count,
                        "cached_results": cached_formula_count,
                        "missing_cached_results": missing_cache_count,
                    },
                ),
            )

        stats: dict[str, Any] = {
            "format": extension.removeprefix("."),
            "extraction_method": "openpyxl-formula+data-only",
            "recalculation_performed": False,
            "keep_vba": keep_vba,
            "macros_executed": False,
            "sheet_count": len(sheet_records),
            "hidden_sheet_count": hidden_sheet_count,
            "nonempty_cell_count": nonempty_cell_count,
            "formula_cell_count": formula_count,
            "formula_cached_value_count": cached_formula_count,
            "formula_missing_cached_value_count": missing_cache_count,
            "hidden_row_count": hidden_row_count,
            "hidden_column_count": hidden_column_count,
            "merged_range_count": merged_range_count,
            "defined_name_count": len(defined_names),
            "defined_names": defined_names,
            "external_link_count": len(external_links),
            "external_links": external_links,
            "calculation": calculation,
            "warning_count": len(warnings),
            "capability_blocking_warning_count": sum(
                1 for item in warnings if item["capability_blocking"]
            ),
        }
        blocks = _build_blocks(
            source_id=source_id,
            source_uid=source_uid,
            extraction_method="openpyxl-formula+data-only",
            sheet_records=sheet_records,
            workbook_metadata={
                "format": stats["format"],
                "recalculation_performed": False,
                "defined_names": defined_names,
                "external_links": external_links,
                "calculation": calculation,
                "keep_vba": keep_vba,
                "macros_executed": False,
            },
            workbook_warning_codes=[item["code"] for item in warnings],
            workbook_manual_review=any(
                item["capability_blocking"] for item in warnings
            ),
        )
        stats["block_count"] = len(blocks)
        stats["characters"] = sum(len(block.text) for block in blocks)
        stats["status"] = "complete_with_warnings" if warnings else "complete"
        return blocks, warnings, stats
    finally:
        formula_book.close()
        cached_book.close()


def _extract_xlrd(
    path: Path,
    source_id: str,
    source_uid: str,
) -> tuple[list[DocumentBlock], list[dict[str, Any]], dict[str, Any]]:
    try:
        import xlrd
    except ImportError as exc:  # pragma: no cover - depends on runtime profile
        raise RuntimeError("xlrd is required for legacy XLS extraction") from exc

    warning = _warning(
        _XLS_FORMULA_WARNING,
        "legacy XLS extraction preserves cached cell values but xlrd does not expose formula expressions",
        source_id=source_id,
        capability_blocking=True,
        capability="formula_audit",
    )
    warnings = [warning]
    try:
        book = xlrd.open_workbook(
            str(path),
            on_demand=True,
            formatting_info=True,
            ragged_rows=True,
        )
    except TypeError:
        book = xlrd.open_workbook(
            str(path), on_demand=True, formatting_info=True
        )

    sheet_records: list[dict[str, Any]] = []
    nonempty_cell_count = 0
    hidden_sheet_count = 0
    hidden_row_count = 0
    hidden_column_count = 0
    merged_range_count = 0
    try:
        for sheet_index, sheet in enumerate(book.sheets(), 1):
            visibility = int(getattr(sheet, "visibility", 0) or 0)
            sheet_state = {0: "visible", 1: "hidden", 2: "veryHidden"}.get(
                visibility, f"unknown:{visibility}"
            )
            if visibility:
                hidden_sheet_count += 1
            hidden_rows = sorted(
                int(index) + 1
                for index, info in getattr(sheet, "rowinfo_map", {}).items()
                if bool(getattr(info, "hidden", False))
            )
            hidden_columns = sorted(
                (
                    int(index) + 1,
                    int(index) + 1,
                )
                for index, info in getattr(sheet, "colinfo_map", {}).items()
                if bool(getattr(info, "hidden", False))
            )
            hidden_row_count += len(hidden_rows)
            hidden_column_count += len(hidden_columns)

            merged = sorted(
                (
                    int(row_low) + 1,
                    int(col_low) + 1,
                    int(row_high),
                    int(col_high),
                    _xls_range(row_low, row_high, col_low, col_high),
                )
                for row_low, row_high, col_low, col_high in getattr(
                    sheet, "merged_cells", []
                )
            )
            merged_range_count += len(merged)

            cells: list[dict[str, Any]] = []
            for row_index in range(sheet.nrows):
                row_length = sheet.row_len(row_index)
                for column_index in range(row_length):
                    cell = sheet.cell(row_index, column_index)
                    if cell.ctype in (xlrd.XL_CELL_EMPTY, xlrd.XL_CELL_BLANK):
                        continue
                    coordinate = f"{_column_name(column_index)}{row_index + 1}"
                    locator = f"{source_id}#sheet[{sheet.name}]!{coordinate}"
                    value = _xlrd_value(book, cell, xlrd)
                    merged_range = _merged_range_for_cell(
                        row_index + 1, column_index + 1, merged
                    )
                    cells.append(
                        {
                            "coordinate": coordinate,
                            "row": row_index + 1,
                            "column": column_index + 1,
                            "raw_value": value,
                            "value": value,
                            "formula": None,
                            "cached_value": value,
                            "is_formula": None,
                            "formula_cached": None,
                            "data_type": _xlrd_type_name(cell.ctype, xlrd),
                            "cached_data_type": _xlrd_type_name(cell.ctype, xlrd),
                            "number_format": None,
                            "hidden_row": row_index + 1 in hidden_rows,
                            "hidden_column": _column_is_hidden(
                                column_index + 1, hidden_columns
                            ),
                            "merged_range": merged_range,
                            "is_merged_anchor": bool(
                                merged_range
                                and merged_range.split(":", 1)[0] == coordinate
                            ),
                            "legacy_locator": locator,
                            "warning_codes": [_XLS_FORMULA_WARNING],
                        }
                    )
            nonempty_cell_count += len(cells)
            sheet_records.append(
                {
                    "sheet_index": sheet_index,
                    "sheet_name": sheet.name,
                    "sheet_state": sheet_state,
                    "max_row": int(sheet.nrows),
                    "max_column": int(sheet.ncols),
                    "hidden_rows": hidden_rows,
                    "hidden_columns": [
                        {
                            "min": start,
                            "max": end,
                            "range": _column_range(
                                start, end, _column_name, zero_based=True
                            ),
                        }
                        for start, end in hidden_columns
                    ],
                    "merged_ranges": [item[4] for item in merged],
                    "cells": cells,
                }
            )

        defined_names = _xlrd_defined_names(book)
        stats: dict[str, Any] = {
            "format": "xls",
            "extraction_method": "xlrd-cached-values",
            "recalculation_performed": False,
            "formula_text_available": False,
            "formula_results_are_cached_only": True,
            "sheet_count": len(sheet_records),
            "hidden_sheet_count": hidden_sheet_count,
            "nonempty_cell_count": nonempty_cell_count,
            "formula_cell_count": None,
            "formula_cached_value_count": None,
            "formula_missing_cached_value_count": None,
            "hidden_row_count": hidden_row_count,
            "hidden_column_count": hidden_column_count,
            "merged_range_count": merged_range_count,
            "defined_name_count": len(defined_names),
            "defined_names": defined_names,
            "external_link_count": None,
            "external_links": [],
            "external_link_metadata_available": False,
            "calculation": {"available": False},
            "warning_count": 1,
            "capability_blocking_warning_count": 1,
        }
        blocks = _build_blocks(
            source_id=source_id,
            source_uid=source_uid,
            extraction_method="xlrd-cached-values",
            sheet_records=sheet_records,
            workbook_metadata={
                "format": "xls",
                "recalculation_performed": False,
                "formula_text_available": False,
                "formula_results_are_cached_only": True,
                "defined_names": defined_names,
                "external_links": [],
                "external_link_metadata_available": False,
                "calculation": {"available": False},
            },
            workbook_warning_codes=[_XLS_FORMULA_WARNING],
            workbook_manual_review=True,
        )
        stats["block_count"] = len(blocks)
        stats["characters"] = sum(len(block.text) for block in blocks)
        stats["status"] = "complete_with_warnings"
        return blocks, warnings, stats
    finally:
        release = getattr(book, "release_resources", None)
        if callable(release):
            release()


def _build_blocks(
    *,
    source_id: str,
    source_uid: str,
    extraction_method: str,
    sheet_records: list[dict[str, Any]],
    workbook_metadata: dict[str, Any],
    workbook_warning_codes: list[str],
    workbook_manual_review: bool,
) -> list[DocumentBlock]:
    blocks: list[DocumentBlock] = []
    order = 1
    workbook_payload = {
        "kind": "workbook",
        **workbook_metadata,
        "sheets": [
            {
                "sheet_index": item["sheet_index"],
                "sheet_name": item["sheet_name"],
                "sheet_state": item["sheet_state"],
            }
            for item in sheet_records
        ],
    }
    blocks.append(
        DocumentBlock(
            source_id=source_id,
            source_uid=source_uid,
            block_id="workbook",
            order=order,
            block_type="spreadsheet_workbook",
            text=_stable_text(workbook_payload),
            extraction_method=extraction_method,
            legacy_locators=[f"{source_id}#workbook"],
            metadata=workbook_payload,
            quality=_quality(
                warning_codes=workbook_warning_codes,
                manual_review_required=workbook_manual_review,
                formula_audited=False,
            ),
        )
    )
    for sheet in sheet_records:
        order += 1
        sheet_index = int(sheet["sheet_index"])
        sheet_name = str(sheet["sheet_name"])
        sheet_payload = {
            key: value for key, value in sheet.items() if key != "cells"
        }
        blocks.append(
            DocumentBlock(
                source_id=source_id,
                source_uid=source_uid,
                block_id=f"sheet{sheet_index:03d}",
                order=order,
                block_type="spreadsheet_sheet",
                text=_stable_text({"kind": "sheet", **sheet_payload}),
                extraction_method=extraction_method,
                legacy_locators=[f"{source_id}#sheet[{sheet_name}]"],
                metadata=sheet_payload,
                quality=_quality(
                    warning_codes=workbook_warning_codes,
                    manual_review_required=workbook_manual_review,
                    formula_audited=False,
                ),
            )
        )
        for cell in sheet["cells"]:
            order += 1
            coordinate = str(cell["coordinate"])
            warning_codes = list(cell.get("warning_codes", []))
            blocks.append(
                DocumentBlock(
                    source_id=source_id,
                    source_uid=source_uid,
                    block_id=f"sheet{sheet_index:03d}-cell-{coordinate}",
                    order=order,
                    block_type="spreadsheet_cell",
                    text=_stable_text(
                        {
                            "sheet": sheet_name,
                            "cell": coordinate,
                            "formula": cell.get("formula"),
                            "cached_value": cell.get("cached_value"),
                            "value": cell.get("value"),
                        }
                    ),
                    extraction_method=extraction_method,
                    legacy_locators=[str(cell["legacy_locator"])],
                    metadata={
                        key: value
                        for key, value in cell.items()
                        if key not in {"legacy_locator", "warning_codes"}
                    }
                    | {"sheet_index": sheet_index, "sheet_name": sheet_name},
                    quality=_quality(
                        warning_codes=warning_codes,
                        manual_review_required=bool(warning_codes)
                        or workbook_manual_review,
                        formula_audited=False,
                    ),
                )
            )
    return blocks


def _quality(
    *,
    warning_codes: Iterable[str],
    manual_review_required: bool,
    formula_audited: bool,
) -> dict[str, Any]:
    codes = sorted(set(str(item) for item in warning_codes if str(item)))
    return {
        "status": "complete_with_warnings" if codes else "complete",
        "level": "manual_review" if manual_review_required else "high",
        "requires_manual_review": bool(manual_review_required),
        "warning_codes": codes,
        "formula_audited": bool(formula_audited),
        "recalculation_performed": False,
    }


def _warning(
    code: str,
    message: str,
    *,
    source_id: str,
    capability_blocking: bool,
    capability: str,
    locator: str | None = None,
    details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "code": code,
        "severity": "warning",
        "message": message,
        "source_id": source_id,
        "requires_manual_review": True,
        "capability": capability,
        "capability_blocking": bool(capability_blocking),
    }
    if locator is not None:
        result["locator"] = locator
    if details is not None:
        result["details"] = details
    return result


def _openpyxl_cell_coordinates(*sheets: Any) -> list[tuple[int, int]]:
    coordinates: set[tuple[int, int]] = set()
    for sheet in sheets:
        instantiated = getattr(sheet, "_cells", None)
        if isinstance(instantiated, dict):
            for key in instantiated:
                if (
                    isinstance(key, tuple)
                    and len(key) == 2
                    and all(isinstance(item, int) for item in key)
                ):
                    coordinates.add((key[0], key[1]))
    if coordinates:
        return sorted(coordinates)
    # Defensive fallback for a future openpyxl implementation without _cells.
    first = sheets[0]
    for row in first.iter_rows():
        for cell in row:
            if cell.value is not None:
                coordinates.add((int(cell.row), int(cell.column)))
    return sorted(coordinates)


def _openpyxl_hidden_columns(sheet: Any) -> list[tuple[int, int]]:
    try:
        from openpyxl.utils.cell import column_index_from_string
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError("openpyxl is required for column metadata") from exc

    ranges: list[tuple[int, int]] = []
    for key, dimension in sheet.column_dimensions.items():
        if not bool(getattr(dimension, "hidden", False)):
            continue
        start = int(getattr(dimension, "min", 0) or 0)
        end = int(getattr(dimension, "max", 0) or 0)
        if start <= 0:
            start = column_index_from_string(str(key).split(":", 1)[0])
        if end <= 0:
            end = start
        ranges.append((start, max(start, end)))
    return _merge_integer_ranges(ranges)


def _merge_integer_ranges(ranges: Iterable[tuple[int, int]]) -> list[tuple[int, int]]:
    merged: list[list[int]] = []
    for start, end in sorted(ranges):
        if not merged or start > merged[-1][1] + 1:
            merged.append([start, end])
        else:
            merged[-1][1] = max(merged[-1][1], end)
    return [(item[0], item[1]) for item in merged]


def _column_is_hidden(column: int, ranges: Iterable[tuple[int, int]]) -> bool:
    return any(start <= column <= end for start, end in ranges)


def _merged_range_for_cell(
    row: int,
    column: int,
    ranges: Iterable[tuple[int, int, int, int, str]],
) -> str | None:
    for min_row, min_col, max_row, max_col, label in ranges:
        if min_row <= row <= max_row and min_col <= column <= max_col:
            return label
    return None


def _openpyxl_defined_names(book: Any) -> list[dict[str, Any]]:
    container = getattr(book, "defined_names", None)
    if container is None:
        return []
    if hasattr(container, "definedName"):
        items = list(container.definedName)
    elif hasattr(container, "values"):
        items = list(container.values())
    else:
        items = []
    records: list[dict[str, Any]] = []
    for item in items:
        destinations: list[dict[str, str]] = []
        try:
            destinations = sorted(
                (
                    {"sheet": str(sheet), "reference": str(reference)}
                    for sheet, reference in item.destinations
                ),
                key=lambda value: (value["sheet"], value["reference"]),
            )
        except (AttributeError, TypeError, ValueError):
            destinations = []
        records.append(
            {
                "name": str(getattr(item, "name", "")),
                "attr_text": str(getattr(item, "attr_text", "") or ""),
                "local_sheet_id": _optional_int(
                    getattr(item, "localSheetId", None)
                ),
                "hidden": bool(getattr(item, "hidden", False)),
                "destinations": destinations,
            }
        )
    return sorted(
        records,
        key=lambda item: (
            item["name"].casefold(),
            -1 if item["local_sheet_id"] is None else item["local_sheet_id"],
            item["attr_text"],
        ),
    )


def _openpyxl_external_links(book: Any) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for index, link in enumerate(getattr(book, "_external_links", []) or [], 1):
        file_link = getattr(link, "file_link", None)
        external_book = getattr(link, "externalBook", None)
        sheet_names_obj = getattr(external_book, "sheetNames", None)
        sheet_names = list(getattr(sheet_names_obj, "sheetName", []) or [])
        defined_names_obj = getattr(external_book, "definedNames", None)
        external_defined_names: list[dict[str, Any]] = []
        for name in getattr(defined_names_obj, "definedName", []) or []:
            external_defined_names.append(
                {
                    "name": str(getattr(name, "name", "")),
                    "refers_to": str(getattr(name, "refersTo", "") or ""),
                    "sheet_id": _optional_int(getattr(name, "sheetId", None)),
                }
            )
        records.append(
            {
                "index": index,
                "relationship_id": str(getattr(file_link, "Id", "") or ""),
                "target": str(getattr(file_link, "Target", "") or ""),
                "target_mode": str(
                    getattr(file_link, "TargetMode", "") or ""
                ),
                "sheet_names": [str(item) for item in sheet_names],
                "defined_names": sorted(
                    external_defined_names,
                    key=lambda item: (
                        item["name"].casefold(),
                        -1 if item["sheet_id"] is None else item["sheet_id"],
                    ),
                ),
            }
        )
    return records


def _openpyxl_calculation(book: Any) -> dict[str, Any]:
    calculation = getattr(book, "calculation", None)
    if calculation is None:
        calculation = getattr(book, "calculation_properties", None)
    if calculation is None:
        return {"available": False, "recalculation_performed": False}
    fields = {
        "calc_mode": "calcMode",
        "calc_id": "calcId",
        "full_calc_on_load": "fullCalcOnLoad",
        "force_full_calc": "forceFullCalc",
        "iterate": "iterate",
        "iterate_count": "iterateCount",
        "iterate_delta": "iterateDelta",
        "reference_mode": "refMode",
    }
    result: dict[str, Any] = {
        "available": True,
        "recalculation_performed": False,
    }
    for output_name, attribute in fields.items():
        result[output_name] = _json_value(getattr(calculation, attribute, None))
    return result


def _xlrd_defined_names(book: Any) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for item in getattr(book, "name_obj_list", []) or []:
        records.append(
            {
                "name": str(getattr(item, "name", "")),
                "scope": _optional_int(getattr(item, "scope", None)),
                "hidden": bool(getattr(item, "hidden", False)),
                "macro": bool(getattr(item, "macro", False)),
                "binary": bool(getattr(item, "binary", False)),
                "result": _json_value(getattr(item, "result", None)),
            }
        )
    return sorted(
        records,
        key=lambda item: (
            item["name"].casefold(),
            -1 if item["scope"] is None else item["scope"],
        ),
    )


def _xlrd_value(book: Any, cell: Any, xlrd: Any) -> Any:
    value = cell.value
    if cell.ctype == xlrd.XL_CELL_DATE:
        try:
            return xlrd.xldate_as_datetime(value, book.datemode).isoformat()
        except (TypeError, ValueError, OverflowError):
            return _json_value(value)
    if cell.ctype == xlrd.XL_CELL_BOOLEAN:
        return bool(value)
    if cell.ctype == xlrd.XL_CELL_ERROR:
        return str(getattr(xlrd, "error_text_from_code", {}).get(value, value))
    return _json_value(value)


def _xlrd_type_name(cell_type: int, xlrd: Any) -> str:
    names = {
        xlrd.XL_CELL_EMPTY: "empty",
        xlrd.XL_CELL_TEXT: "text",
        xlrd.XL_CELL_NUMBER: "number",
        xlrd.XL_CELL_DATE: "date",
        xlrd.XL_CELL_BOOLEAN: "boolean",
        xlrd.XL_CELL_ERROR: "error",
        xlrd.XL_CELL_BLANK: "blank",
    }
    return names.get(cell_type, f"unknown:{cell_type}")


def _xls_range(
    row_low: int,
    row_high: int,
    col_low: int,
    col_high: int,
) -> str:
    start = f"{_column_name(col_low)}{row_low + 1}"
    end = f"{_column_name(col_high - 1)}{row_high}"
    return start if start == end else f"{start}:{end}"


def _column_range(
    start: int,
    end: int,
    name_function: Any,
    *,
    zero_based: bool = False,
) -> str:
    start_name = name_function(start - 1 if zero_based else start)
    end_name = name_function(end - 1 if zero_based else end)
    return start_name if start_name == end_name else f"{start_name}:{end_name}"


def _column_name(zero_based_index: int) -> str:
    if zero_based_index < 0:
        raise ValueError("column index must be non-negative")
    result = ""
    value = zero_based_index + 1
    while value:
        value, remainder = divmod(value - 1, 26)
        result = chr(65 + remainder) + result
    return result


def _json_value(value: Any) -> Any:
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if math.isfinite(value):
            return value
        return str(value)
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, (dt.datetime, dt.date, dt.time)):
        return value.isoformat()
    if isinstance(value, dt.timedelta):
        return value.total_seconds()
    if isinstance(value, bytes):
        return {"encoding": "hex", "value": value.hex()}
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    if isinstance(value, dict):
        return {
            str(key): _json_value(item)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
        }
    return str(value)


def _stable_text(value: dict[str, Any]) -> str:
    return json.dumps(
        _json_value(value),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def _optional_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
