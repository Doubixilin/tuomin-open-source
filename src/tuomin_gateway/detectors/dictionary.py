from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from tuomin_gateway.detectors.base import BaseDetector
from tuomin_gateway.schemas import DetectionSpan
from tuomin_gateway.spans import overlaps as _overlaps
from tuomin_gateway.textnorm import to_halfwidth


class DictionaryDetector(BaseDetector):
    name = "dictionary"

    def __init__(self, entries: list[dict[str, Any]], version: str = "dictionary-mvp") -> None:
        self.entries = entries
        self.version = version

    @classmethod
    def from_json(cls, path: str | Path) -> "DictionaryDetector":
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        if not isinstance(data, list):
            raise ValueError("dictionary payload must be a list")
        entries = _active_entries(data)
        versions = sorted({entry.get("version", "unknown") for entry in entries})
        version = ",".join(versions) if versions else "dictionary-mvp"
        return cls(entries, version=version)

    @classmethod
    def from_entries(cls, entries: list[dict[str, Any]]) -> "DictionaryDetector":
        return cls(_active_entries(entries), version="dictionary-test")

    def detect(self, text: str) -> list[DetectionSpan]:
        # Match on the half-width view with half-width needles (entries are
        # normalized the same way, so full/half-width forms match either side);
        # spans always slice the ORIGINAL text (exact-surface invariant).
        scan = to_halfwidth(text)
        spans: list[DetectionSpan] = []
        for entry in self.entries:
            label = entry["label"]
            risk_level = entry.get("risk_level", "unknown")
            canonical = entry["canonical_value"]
            for value in _search_values(entry):
                needle = to_halfwidth(value)
                start = 0
                while True:
                    index = scan.find(needle, start)
                    if index == -1:
                        break
                    end = index + len(needle)
                    spans.append(
                        self.make_span(
                            text=text,
                            start=index,
                            end=end,
                            label=label,
                            confidence=0.99,
                            risk_level=risk_level,
                            metadata={
                                "entry_id": entry.get("entry_id"),
                                "canonical_value": canonical,
                                "risk_level": risk_level,
                                "dictionary_version": entry.get("version", self.version),
                            },
                        )
                    )
                    start = end
        spans.extend(self._leading_truncation_match(text, scan))
        return _deduplicate(spans)

    def _leading_truncation_match(
        self, text: str, scan: str
    ) -> list[DetectionSpan]:
        """Repair a one-character OCR loss at the start of an extracted block.

        PDF page boundaries can turn ``示例甲公司`` into ``例甲公司``.  A
        curated alias can identify that surface safely, but only when exactly
        one active identity owns the missing-one-character form.  This is not
        general fuzzy matching and never runs away from offset zero.
        """
        candidates: dict[tuple[str, str], list[dict[str, Any]]] = {}
        for entry in self.entries:
            if entry.get("allow_leading_ocr_truncation") is not True:
                continue
            for value in _search_values(entry):
                normalized = to_halfwidth(value)
                if len(normalized) < 5:
                    continue
                fragment = normalized[1:]
                if len(fragment) >= 4 and scan.startswith(fragment):
                    candidates.setdefault((entry["label"], fragment), []).append(entry)

        repaired: list[DetectionSpan] = []
        for (label, fragment), owners in candidates.items():
            canonical_values = {entry["canonical_value"] for entry in owners}
            if len(canonical_values) != 1:
                continue
            entry = owners[0]
            repaired.append(
                self.make_span(
                    text=text,
                    start=0,
                    end=len(fragment),
                    label=label,
                    confidence=0.96,
                    risk_level=entry.get("risk_level", "unknown"),
                    metadata={
                        "entry_id": entry.get("entry_id"),
                        "canonical_value": entry["canonical_value"],
                        "risk_level": entry.get("risk_level", "unknown"),
                        "dictionary_version": entry.get("version", self.version),
                        "boundary_repair": "leading_character_truncation",
                    },
                )
            )
        return repaired


def _active_entries(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    active: list[dict[str, Any]] = []
    for entry in entries:
        if entry.get("status", "active") != "active":
            continue
        canonical = entry.get("canonical_value")
        label = entry.get("label")
        if not isinstance(canonical, str) or not canonical.strip():
            raise ValueError("active dictionary entry requires non-empty canonical_value")
        if not isinstance(label, str) or not label.strip():
            raise ValueError("active dictionary entry requires non-empty label")
        active.append(entry)
    return active


def _search_values(entry: dict[str, Any]) -> list[str]:
    values = [entry["canonical_value"], *entry.get("aliases", [])]
    cleaned = {value.strip() for value in values if isinstance(value, str) and value.strip()}
    return sorted(cleaned, key=len, reverse=True)


def _deduplicate(spans: list[DetectionSpan]) -> list[DetectionSpan]:
    selected: list[DetectionSpan] = []
    for candidate in sorted(spans, key=lambda item: (item.start, -item.length())):
        if any(_overlaps(candidate, existing) and candidate.label == existing.label for existing in selected):
            continue
        selected.append(candidate)
    return sorted(selected, key=lambda item: (item.start, item.end, item.label))
