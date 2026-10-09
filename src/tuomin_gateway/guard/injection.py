"""Signature-based prompt-injection detection (pillar 5, Tier 1).

Zero-dependency heuristics: regex signatures for the common direct-injection and
role-override patterns, English + Chinese. This is best-effort and intentionally
honest — it catches well-known patterns, NOT novel/obfuscated or adaptive
attacks, and is weak on Chinese relative to English. An optional small classifier
(Prompt Guard / deberta) can be layered later behind a ``[guard]`` extra.

Returns ``(pattern_id, matched_text)`` tuples; the caller turns them into
AlertEvents (which never store the raw matched text — only a hash).
"""
from __future__ import annotations

import re

_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("ignore_instructions",
     re.compile(r"ignore\s+(?:all\s+)?(?:the\s+)?(?:previous|prior|above|earlier)\s+"
                r"(?:instructions?|prompts?|messages?|context|rules?)", re.I)),
    ("disregard_above",
     re.compile(r"disregard\s+(?:all\s+)?(?:the\s+)?(?:previous|prior|above|earlier)", re.I)),
    ("forget",
     re.compile(r"forget\s+(?:everything|all\s+(?:previous|prior)|what\s+(?:i|you)\s+(?:said|told))", re.I)),
    ("role_override",
     re.compile(r"you\s+are\s+now\b|pretend\s+(?:to\s+be|you\s+are|that)|act\s+as\s+(?:if|a|an)\b", re.I)),
    ("reveal_system",
     re.compile(r"(?:reveal|show|print|repeat|output|display)\s+(?:me\s+)?(?:your\s+)?"
                r"(?:system\s+)?(?:prompt|instructions?|message|rules?)", re.I)),
    ("dev_mode",
     re.compile(r"\b(?:developer\s+mode|jailbreak|do\s+anything\s+now|\bDAN\b)\b", re.I)),
    # --- Chinese ---
    # Wider gap (was .{0,8}?) so phrasings like "忽略掉系统给你的所有先前的指令"
    # — where many connective chars sit between the verb and the noun — still hit.
    ("zh_ignore",
     re.compile(r"(?:忽略|忽视|无视|不要理会|不用理会|不必理会)(?:掉|了)?"
                r"(?:之前|上述|前面|以上|先前|所有|那些|系统|给你的)?"
                r".{0,20}?(?:指示|指令|提示词?|规则|要求|设定|限制)")),
    ("zh_reveal",
     re.compile(r"(?:显示|输出|打印|重复|告诉我|说出)(?:你的|出你的)?.{0,12}?(?:系统)?(?:提示词?|指令|设定|规则)")),
    ("zh_roleplay",
     re.compile(r"假装你是|扮演(?:一个|成)?|你现在是|从现在起你?是|进入开发者模式|越狱模式")),
)


def scan(text: str) -> list[tuple[str, str]]:
    if not text:
        return []
    findings: list[tuple[str, str]] = []
    for pattern_id, pattern in _PATTERNS:
        for match in pattern.finditer(text):
            matched = match.group(0)
            if matched.strip():
                findings.append((pattern_id, matched))
    return findings
