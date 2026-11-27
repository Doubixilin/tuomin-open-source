"""Bulk dictionary import / export with a dry-run preview.

Accepted inputs: pasted lines, CSV, XLSX (first sheet), JSON (our export or a
list of entries) and another app's entries. Rows are merged into the existing
dictionary by (label, canonical value); new aliases are unioned. Every row gets
a status in the preview — added / updated / unchanged / error / blocked — and
only clean rows are written. Quality rules are the same curated-identity lint
that guards single-entry edits, applied only to what this import changes.

The preview contains dictionary values: it is for the dictionary owner in the
local admin UI and must never be written to audit streams.
"""
from __future__ import annotations

import csv
import io
import json
import re
import uuid
from dataclasses import dataclass, field
from typing import Any, Iterable

from tuomin_gateway.dictionary_quality import lint_dictionary_entries

MAX_ROWS = 20_000
MAX_BYTES = 5 * 1024 * 1024
FORMATS = {"text", "csv", "xlsx", "json"}

LABEL_RE = re.compile(r"^[A-Z][A-Z0-9_]{1,31}$")
LABEL_ALIASES = {
    "单位": "ORG", "公司": "ORG", "机构": "ORG", "组织": "ORG", "企业": "ORG", "法人": "ORG",
    "人名": "PERSON", "姓名": "PERSON", "人员": "PERSON", "联系人": "PERSON", "个人": "PERSON",
    "项目": "PROJECT", "工程": "PROJECT", "项目名称": "PROJECT",
    "供应商": "SUPPLIER", "客户": "CUSTOMER", "地址": "ADDRESS", "地点": "ADDRESS",
}
RISKS = {"low", "medium", "high", "critical"}
RISK_ALIASES = {"低": "low", "中": "medium", "高": "high", "极高": "critical", "关键": "critical", "严重": "critical"}
HEADERS = {
    "canonical_value": {"canonical_value", "canonical", "value", "name", "名称", "值", "词条", "实体", "原值", "全称"},
    "label": {"label", "type", "类型", "标签", "类别", "分类"},
    "aliases": {"aliases", "alias", "别名", "简称", "别称"},
    "risk_level": {"risk_level", "risk", "风险", "风险等级", "敏感度"},
}
_ALIAS_SPLIT = re.compile(r"[|、;；]")
ISSUE_MESSAGES = {
    "single_character_value": "单个字太短，会误伤正文",
    "value_too_long": "内容过长，不像名称",
    "sentence_punctuation": "含句号等句子标点，不像名称",
    "excessive_whitespace": "含多余空白",
    "generic_value": "过于宽泛的通用词（如「市」「公司」），会误伤正文",
    "standalone_suffix": "只有后缀（如「有限公司」），会误伤正文",
    "generic_fragment": "通用片段，会误伤正文",
    "context_prefix": "带了「甲方：」这类上下文前缀，请只写名称",
    "identity_conflict": "同一个值出现在两个词条里",
    "label_conflict": "同一个值被标成了不同类型",
    "alias_empty": "别名为空",
}


class ImportError_(ValueError):
    """Raised for whole-request problems (format, size); rows report their own."""


@dataclass
class Row:
    line: int
    canonical_value: str = ""
    label: str = ""
    aliases: list[str] = field(default_factory=list)
    risk_level: str = ""
    status: str = "pending"
    message: str = ""
    entry_id: str | None = None

    def view(self) -> dict:
        return {"line": self.line, "canonical_value": self.canonical_value, "label": self.label,
                "aliases": self.aliases, "risk_level": self.risk_level, "status": self.status, "message": self.message}


def _clean(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value if value is not None else "")).strip()


def normalize_label(raw: str, default_label: str = "") -> tuple[str, str | None]:
    value = _clean(raw) or _clean(default_label)
    if not value:
        return "", "缺少类型（如 ORG / PERSON / PROJECT，或在导入时选默认类型）"
    if value in LABEL_ALIASES:
        return LABEL_ALIASES[value], None
    upper = value.upper()
    if LABEL_RE.match(upper):
        return upper, None
    return "", f"类型「{value}」无法识别，请用大写英文（如 ORG）或 单位 / 人名 / 项目 / 供应商 / 客户 / 地址"


def normalize_risk(raw: str, default_risk: str = "high") -> tuple[str, str | None]:
    value = _clean(raw).lower()
    if not value:
        return default_risk, None
    value = RISK_ALIASES.get(value, value)
    if value in RISKS:
        return value, None
    return "", f"风险等级「{raw}」无法识别，可用 low / medium / high / critical 或 低 / 中 / 高 / 极高"


def _split_aliases(raw: Any) -> list[str]:
    if isinstance(raw, list):
        parts = raw
    else:
        parts = _ALIAS_SPLIT.split(str(raw or ""))
    seen, out = set(), []
    for part in parts:
        value = _clean(part)
        if value and value not in seen:
            seen.add(value)
            out.append(value)
    return out


def _header_map(cells: list[str]) -> dict[str, int] | None:
    mapping = {}
    for index, cell in enumerate(cells):
        key = _clean(cell).lower()
        for target, names in HEADERS.items():
            if key in {name.lower() for name in names} and target not in mapping:
                mapping[target] = index
    return mapping if "canonical_value" in mapping else None


def _tabular_rows(table: list[list[str]], *, default_label: str, default_risk: str, first_line: int = 1) -> list[Row]:
    rows: list[Row] = []
    if not table:
        return rows
    header = _header_map(table[0])
    body = table[1:] if header else table
    start = first_line + (1 if header else 0)
    columns = header or {"canonical_value": 0, "label": 1, "aliases": 2, "risk_level": 3}
    for offset, cells in enumerate(body):
        if not any(_clean(c) for c in cells):
            continue
        get = lambda name: cells[columns[name]] if name in columns and columns[name] < len(cells) else ""
        rows.append(_build_row(start + offset, get("canonical_value"), get("label"), get("aliases"), get("risk_level"), default_label, default_risk))
    return rows


def _build_row(line, canonical, label, aliases, risk, default_label, default_risk) -> Row:
    row = Row(line=line, canonical_value=_clean(canonical), aliases=_split_aliases(aliases))
    if not row.canonical_value:
        row.status, row.message = "error", "名称为空"
        return row
    row.label, label_error = normalize_label(label, default_label)
    row.risk_level, risk_error = normalize_risk(risk, default_risk)
    if label_error or risk_error:
        row.status, row.message = "error", label_error or risk_error
    row.aliases = [a for a in row.aliases if a != row.canonical_value]
    return row


def parse_rows(fmt: str, content: str | bytes, *, default_label: str = "", default_risk: str = "high") -> list[Row]:
    if fmt not in FORMATS:
        raise ImportError_(f"不支持的导入格式：{fmt}")
    size = len(content.encode("utf-8") if isinstance(content, str) else content)
    if size > MAX_BYTES:
        raise ImportError_("导入内容超过 5 MB，请拆分后导入")
    default_risk = normalize_risk(default_risk, "high")[0] or "high"
    if fmt == "xlsx":
        if not isinstance(content, (bytes, bytearray)):
            raise ImportError_("XLSX 需要以文件内容导入")
        try:
            from openpyxl import load_workbook
            workbook = load_workbook(io.BytesIO(content), read_only=True, data_only=True)
        except Exception as exc:
            raise ImportError_("无法读取这个 Excel 文件，请确认是 .xlsx 格式") from exc
        try:
            sheet = workbook.worksheets[0]
            table = []
            for values in sheet.iter_rows(values_only=True):
                table.append(["" if v is None else str(v) for v in values])
                if len(table) > MAX_ROWS + 1:
                    break
        finally:
            workbook.close()
        rows = _tabular_rows(table, default_label=default_label, default_risk=default_risk)
    else:
        text = content.decode("utf-8-sig") if isinstance(content, (bytes, bytearray)) else content.lstrip("﻿")
        if fmt == "json":
            try:
                data = json.loads(text)
            except ValueError as exc:
                raise ImportError_("JSON 格式不正确") from exc
            entries = data.get("entries") if isinstance(data, dict) else data
            if not isinstance(entries, list):
                raise ImportError_("JSON 需要是词条数组，或含 entries 数组的对象")
            rows = []
            for index, item in enumerate(entries, 1):
                if not isinstance(item, dict):
                    rows.append(Row(line=index, status="error", message="不是词条对象"))
                    continue
                if item.get("status", "active") != "active":
                    continue
                rows.append(_build_row(index, item.get("canonical_value"), item.get("label"), item.get("aliases"), item.get("risk_level"), default_label, default_risk))
        elif fmt == "csv":
            sample = text[:4096]
            try:
                dialect = csv.Sniffer().sniff(sample, delimiters=",\t;，") if sample.strip() else csv.excel
            except csv.Error:
                dialect = csv.excel
            table = [list(r) for r in csv.reader(io.StringIO(text), dialect)]
            rows = _tabular_rows(table, default_label=default_label, default_risk=default_risk)
        else:  # text
            rows = []
            for number, raw in enumerate(text.splitlines(), 1):
                line = raw.strip()
                if not line or line.startswith("#"):
                    continue
                parts = line.split("\t") if "\t" in line else re.split(r"[,，]", line)
                parts += [""] * (4 - len(parts))
                rows.append(_build_row(number, parts[0], parts[1], parts[2], parts[3], default_label, default_risk))
    if len(rows) > MAX_ROWS:
        raise ImportError_(f"一次最多导入 {MAX_ROWS} 行，请拆分后导入")
    return rows


def plan_import(existing: list[dict], rows: list[Row], *, version: str = "import") -> dict:
    """Merge rows into a copy of ``existing``; return preview and result entries."""
    merged = [dict(entry, aliases=list(entry.get("aliases") or [])) for entry in existing]
    by_identity: dict[tuple[str, str], dict] = {}
    value_owner: dict[str, dict] = {}
    for entry in merged:
        if entry.get("status", "active") != "active":
            continue
        by_identity[(entry.get("label"), entry.get("canonical_value"))] = entry
        for value in [entry.get("canonical_value"), *entry.get("aliases", [])]:
            if isinstance(value, str):
                value_owner.setdefault(value, entry)
    touched: dict[str, list[Row]] = {}
    snapshots: dict[str, dict | None] = {}

    for row in rows:
        if row.status == "error":
            continue
        key = (row.label, row.canonical_value)
        target = by_identity.get(key)
        conflict = value_owner.get(row.canonical_value)
        if target is None and conflict is not None and conflict.get("label") != row.label:
            row.status, row.message = "error", f"「{row.canonical_value}」已是类型 {conflict.get('label')} 的词条或别名"
            continue
        if target is None and conflict is not None:
            target = conflict  # value already present as an alias of the same label
        known_before = {target["canonical_value"], *target["aliases"]} if target is not None else {row.canonical_value}
        alias_conflict = next((a for a in row.aliases if a not in known_before and a in value_owner and value_owner[a] is not target), None)
        if alias_conflict:
            row.status, row.message = "error", f"别名「{alias_conflict}」已属于其他词条"
            continue
        if target is None:
            entry_id = f"e_{uuid.uuid4().hex[:10]}"
            target = {"entry_id": entry_id, "canonical_value": row.canonical_value, "label": row.label,
                      "aliases": [], "risk_level": row.risk_level, "status": "active", "version": version}
            merged.append(target)
            snapshots[entry_id] = None
            by_identity[key] = target
            value_owner[row.canonical_value] = target
            row.status = "added"
        else:
            snapshots.setdefault(target["entry_id"], json.loads(json.dumps(target)))
        known = {target["canonical_value"], *target["aliases"]}
        new_aliases = [a for a in row.aliases if a not in known]
        target["aliases"].extend(new_aliases)
        for alias in new_aliases:
            value_owner[alias] = target
        if row.status != "added":
            row.status = "updated" if new_aliases else "unchanged"
            if new_aliases:
                row.message = "新增别名：" + "、".join(new_aliases)
        row.entry_id = target["entry_id"]
        touched.setdefault(target["entry_id"], []).append(row)

    # Quality: block only rows whose own changes introduce an issue.
    for _round in range(3):
        report = lint_dictionary_entries([e for e in merged])
        implicated: dict[str, str] = {}
        for issue in report["issues"]:
            for entry_id in str(issue.get("entry_id", "")).split(","):
                if entry_id in touched and entry_id not in implicated:
                    value = issue.get("value")
                    implicated[entry_id] = ISSUE_MESSAGES.get(issue["code"], issue["code"]) + (f"（{value}）" if value else "")
        if not implicated:
            break
        for entry_id, message in implicated.items():
            for row in touched.pop(entry_id):
                row.status, row.message = "blocked", message
            original = snapshots.get(entry_id)
            index = next(i for i, e in enumerate(merged) if e.get("entry_id") == entry_id)
            if original is None:
                merged.pop(index)
            else:
                merged[index] = original
    counts: dict[str, int] = {}
    for row in rows:
        counts[row.status] = counts.get(row.status, 0) + 1
    changed = sum(counts.get(k, 0) for k in ("added", "updated"))
    return {"rows": [row.view() for row in rows], "counts": counts, "changed": changed, "entries": merged}


def export_entries(entries: Iterable[dict], fmt: str) -> tuple[str, str]:
    """(content, mime) for CSV or JSON export of active entries."""
    active = [e for e in entries if isinstance(e, dict) and e.get("status", "active") == "active"]
    if fmt == "json":
        payload = {"format": "tuomin-dictionary-v1", "entries": [
            {k: e.get(k) for k in ("canonical_value", "label", "aliases", "risk_level") if e.get(k) is not None} for e in active]}
        return json.dumps(payload, ensure_ascii=False, indent=2) + "\n", "application/json"
    if fmt == "csv":
        buffer = io.StringIO()
        writer = csv.writer(buffer)
        writer.writerow(["名称", "类型", "别名", "风险"])
        for e in active:
            writer.writerow([e.get("canonical_value", ""), e.get("label", ""), "|".join(e.get("aliases") or []), e.get("risk_level", "")])
        return "﻿" + buffer.getvalue(), "text/csv"
    raise ImportError_(f"不支持的导出格式：{fmt}")


CANDIDATE_LABELS = {"ORG", "PERSON", "ADDRESS", "PROJECT", "SUPPLIER", "CUSTOMER"}
ROLE_TO_LABEL = {"PARTYA": "ORG", "PARTYB": "ORG", "GENCON": "ORG", "SUBCON": "ORG", "GUARANTOR": "ORG",
                 "SUPERVISOR": "ORG", "PRINCIPAL": "ORG", "LEAD": "ORG", "TARGET": "ORG", "PARTNER": "ORG",
                 "INVESTOR": "ORG", "TRANSFEROR": "ORG", "TRANSFEREE": "ORG"}


def candidates(mapping_entries: Iterable[Any], existing: list[dict]) -> list[dict]:
    """Detected entity values not yet covered by the dictionary (for 从结果学习)."""
    known = {value for entry in existing if entry.get("status", "active") == "active"
             for value in [entry.get("canonical_value"), *(entry.get("aliases") or [])] if isinstance(value, str)}
    seen, out = set(), []
    for entry in mapping_entries:
        label = getattr(entry, "label", None) or (entry.get("label") if isinstance(entry, dict) else None)
        value = getattr(entry, "original_value", None) or (entry.get("original_value") if isinstance(entry, dict) else None)
        label = ROLE_TO_LABEL.get(label, label)
        if label not in CANDIDATE_LABELS or not isinstance(value, str):
            continue
        value = _clean(value)
        if len(value) < 2 or value in known or value in seen:
            continue
        seen.add(value)
        out.append({"canonical_value": value, "label": label})
    return out
