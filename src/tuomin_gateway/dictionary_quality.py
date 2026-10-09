"""Quality gate for curated application dictionaries.

Benchmark dictionaries intentionally maximize recall and may contain extracted
phrases that are useful as evaluation gold but unsafe as active exact-match
policy.  This module keeps that distinction explicit: it does not change the
detector and it does not silently clean user data.  It reports entries that
must be reviewed before a dictionary is registered for a consuming app.
"""
from __future__ import annotations

from collections import defaultdict
import re
from typing import Any, Iterable


DICTIONARY_QUALITY_CONTRACT = "curated-identity-v1"

_GENERIC_VALUES_BY_LABEL = {
    "ADDRESS": {
        "地铁",
        "市",
        "县",
        "区",
    },
    "ORG": {
        "公司",
        "有限公司",
        "股份公司",
        "人民政府",
        "行政机关",
        "自然资源部",
        "国土资源管理部门",
        "人民政府土地管理部门",
        "城市规划、建设、房产管理部门",
    },
    "PROJECT": {
        "项目",
        "本项目",
        "投资变更",
        "房地产开发项目",
        "产开发项目",
        "幅经营性地块",
        "地块房地产开发项目",
        "投资变更报告",
        "投资变更的法律意见书",
        "实施本项目",
        "共同实施本项目",
    },
}

_STANDALONE_SUFFIXES = {
    "ORG": {"部", "局", "委", "公司", "有限公司"},
    "PROJECT": {"项目", "地块"},
}

_CONTEXT_PREFIXES = (
    "项目公司为",
    "提请",
    "编制单位：",
    "编制单位:",
    "报来",
    "送审",
)

_GENERIC_REFERENCE_FORMS = (
    "行政机关",
    "人民政府",
    "县人民政府土地管理部门",
    "市县人民政府国土资源行政主管部门",
    "人民政府国土资源行政主管部门",
    "直辖市人民政府",
    "国务院",
    "自然资源部",
    "国土资源部",
    "国土资源管理部门",
    "城市规划建设房产管理部门",
)

_MAX_VALUE_LENGTH_BY_LABEL = {
    "ORG": 48,
    "DEPARTMENT": 48,
    "PROJECT": 48,
    "ADDRESS": 80,
}
_CURATED_IDENTITY_LABELS = frozenset(_MAX_VALUE_LENGTH_BY_LABEL)
_SENTENCE_PUNCTUATION = frozenset("，。；！？,;:：")
_WHITESPACE_RUN_RE = re.compile(r"\s+")


def lint_dictionary_entries(entries: object) -> dict[str, Any]:
    """Return a deterministic local review report for one JSON dictionary.

    The report may contain dictionary values because it is intended for the
    dictionary owner at a local maintenance boundary.  Callers must not write
    it to ordinary redaction receipts or audit streams.
    """

    if not isinstance(entries, list):
        return _report(
            entry_count=0,
            active_entry_count=0,
            issues=[_issue("dictionary_not_list")],
        )

    issues: list[dict[str, str]] = []
    active: list[dict[str, Any]] = []
    entry_ids: dict[str, int] = defaultdict(int)
    identities: dict[tuple[str, str], list[tuple[str, str]]] = defaultdict(list)
    value_labels: dict[str, list[tuple[str, str]]] = defaultdict(list)

    for index, raw in enumerate(entries):
        if not isinstance(raw, dict):
            issues.append(_issue("entry_not_object", index=index))
            continue
        if raw.get("status", "active") != "active":
            continue
        active.append(raw)
        entry_id = str(raw.get("entry_id") or f"index-{index}")
        entry_ids[entry_id] += 1
        label = raw.get("label")
        canonical = raw.get("canonical_value")
        aliases = raw.get("aliases", [])
        allow_leading_ocr_truncation = raw.get("allow_leading_ocr_truncation")
        if not isinstance(label, str) or not label.strip():
            issues.append(_issue("label_missing", entry_id=entry_id, index=index))
            continue
        label = label.strip()
        if not isinstance(canonical, str) or not canonical.strip():
            issues.append(
                _issue("canonical_missing", entry_id=entry_id, label=label, index=index)
            )
            continue
        canonical = canonical.strip()
        if allow_leading_ocr_truncation is not None and not isinstance(
            allow_leading_ocr_truncation, bool
        ):
            issues.append(
                _issue(
                    "ocr_repair_flag_invalid",
                    entry_id=entry_id,
                    label=label,
                    index=index,
                )
            )
        issues.extend(
            _value_issues(
                canonical,
                label=label,
                entry_id=entry_id,
                field="canonical_value",
                index=index,
            )
        )
        identities[(label, canonical)].append((entry_id, "canonical_value"))
        value_labels[canonical].append((label, entry_id))

        if not isinstance(aliases, list):
            issues.append(
                _issue(
                    "aliases_not_list",
                    entry_id=entry_id,
                    label=label,
                    index=index,
                )
            )
            continue
        for alias_index, alias in enumerate(aliases):
            if not isinstance(alias, str) or not alias.strip():
                issues.append(
                    _issue(
                        "alias_empty",
                        entry_id=entry_id,
                        label=label,
                        field=f"aliases[{alias_index}]",
                        index=index,
                    )
                )
                continue
            alias = alias.strip()
            issues.extend(
                _value_issues(
                    alias,
                    label=label,
                    entry_id=entry_id,
                    field=f"aliases[{alias_index}]",
                    index=index,
                )
            )
            identities[(label, alias)].append((entry_id, f"aliases[{alias_index}]"))
            value_labels[alias].append((label, entry_id))

    for entry_id, count in sorted(entry_ids.items()):
        if count > 1:
            issues.append(_issue("entry_id_duplicate", entry_id=entry_id))

    for (label, value), owners in sorted(identities.items()):
        distinct_entries = sorted({entry_id for entry_id, _ in owners})
        if len(distinct_entries) > 1:
            issues.append(
                _issue(
                    "identity_conflict",
                    entry_id=",".join(distinct_entries),
                    label=label,
                    value=value,
                )
            )

    for value, owners in sorted(value_labels.items()):
        labels = sorted({label for label, _ in owners})
        if len(labels) > 1:
            issues.append(
                _issue(
                    "label_conflict",
                    entry_id=",".join(sorted({entry_id for _, entry_id in owners})),
                    label=",".join(labels),
                    value=value,
                )
            )

    issues.sort(
        key=lambda item: (
            item.get("index", ""),
            item.get("entry_id", ""),
            item.get("field", ""),
            item["code"],
            item.get("value", ""),
        )
    )
    return _report(
        entry_count=len(entries),
        active_entry_count=len(active),
        issues=issues,
    )


def assert_curated_dictionary(entries: object) -> dict[str, Any]:
    """Return the report or raise ``ValueError`` when the quality gate fails."""

    report = lint_dictionary_entries(entries)
    if report["status"] != "ok":
        codes = ",".join(report["issue_codes"])
        raise ValueError(f"dictionary quality gate failed: {codes}")
    return report


def _value_issues(
    value: str,
    *,
    label: str,
    entry_id: str,
    field: str,
    index: int,
) -> Iterable[dict[str, str]]:
    base = {
        "entry_id": entry_id,
        "label": label,
        "field": field,
        "value": value,
        "index": index,
    }
    if len(value) == 1:
        yield _issue("single_character_value", **base)
    limit = _MAX_VALUE_LENGTH_BY_LABEL.get(label)
    if limit is not None and len(value) > limit:
        yield _issue("value_too_long", **base)
    if label in _CURATED_IDENTITY_LABELS and any(
        char in _SENTENCE_PUNCTUATION for char in value
    ):
        yield _issue("sentence_punctuation", **base)
    if (
        label in _CURATED_IDENTITY_LABELS
        and len(_WHITESPACE_RUN_RE.findall(value)) >= 3
    ):
        yield _issue("excessive_whitespace", **base)
    if value in _GENERIC_VALUES_BY_LABEL.get(label, set()):
        yield _issue("generic_value", **base)
    if value in _STANDALONE_SUFFIXES.get(label, set()):
        yield _issue("standalone_suffix", **base)
    compact = value.replace("、", "").replace("，", "")
    if (
        label == "ORG"
        and len(compact) >= 2
        and any(compact in generic for generic in _GENERIC_REFERENCE_FORMS)
        and value not in _GENERIC_VALUES_BY_LABEL.get(label, set())
    ):
        yield _issue("generic_fragment", **base)
    if any(prefix in value for prefix in _CONTEXT_PREFIXES):
        yield _issue("context_prefix", **base)
    if label == "ORG" and value.startswith("经") and value.endswith(
        ("公司", "有限公司", "集团", "委员会", "局", "部")
    ):
        yield _issue("context_prefix", **base)


def _report(
    *,
    entry_count: int,
    active_entry_count: int,
    issues: list[dict[str, str]],
) -> dict[str, Any]:
    return {
        "status": "ok" if not issues else "blocked",
        "contract": DICTIONARY_QUALITY_CONTRACT,
        "entry_count": entry_count,
        "active_entry_count": active_entry_count,
        "issue_count": len(issues),
        "issue_codes": sorted({item["code"] for item in issues}),
        "issues": issues,
    }


def _issue(code: str, **fields: object) -> dict[str, str]:
    item = {"code": code}
    for key, value in fields.items():
        if value is not None:
            item[key] = str(value)
    return item


__all__ = [
    "DICTIONARY_QUALITY_CONTRACT",
    "assert_curated_dictionary",
    "lint_dictionary_entries",
]
