"""Irreversible quasi-identifier generalizers (脱敏层级 L3).

A *generalizer* coarsens a sensitive VALUE to a less re-identifying form —
amount → range, address → province, exact date → quarter — instead of replacing
it with a reversible placeholder. The coarsened value is substituted in place and
is **NOT** part of the reversible mapping: ``refill`` never restores it. This is
the deliberate, one-way cost that lowers re-identification while keeping enough
signal for downstream analysis (privacy-utility 曲线的 x 轴).

Design notes:
- Pure, rule-based, no third-party deps (matches the project's zero-core-deps
  stance and stays CI-cheap).
- **Fail-closed**: a generalizer returns ``None`` when it cannot confidently
  coarsen its input. The caller (``redactor`` / ``SessionRedactor``) then falls
  back to a reversible placeholder rather than leaving the original in clear —
  so a parse miss can never become a leak.
- Coarse outputs are plain Chinese/number strings; they never look like a
  placeholder (``<LABEL_001>``), so they cannot trip the refill integrity check.

These are first-draft buckets/maps. The exact thresholds and the city→province
table are meant to be tuned per scenario (see
``docs/design/desensitization-layer-strategy.md`` §3/§5).
"""
from __future__ import annotations

import re
from typing import Callable

from tuomin_gateway.textnorm import to_halfwidth

# --- amount → range ---------------------------------------------------------

_AMOUNT_RE = re.compile(r"(\d[\d,，]*(?:\.\d+)?)\s*(亿|万)?")


def _parse_cny_yuan(value: str) -> float | None:
    """Extract a CNY amount in yuan from a detected amount string, handling
    ￥/人民币 prefixes, thousands commas (half- and full-width), and 万/亿
    units. None if no number."""
    cleaned = (
        value.replace("人民币", "").replace("￥", "").replace("¥", "").replace(" ", "")
    )
    match = _AMOUNT_RE.search(cleaned)
    if not match or not match.group(1):
        return None
    try:
        num = float(match.group(1).replace(",", "").replace("，", ""))
    except ValueError:
        return None
    unit = match.group(2)
    if unit == "亿":
        return num * 1e8
    if unit == "万":
        return num * 1e4
    return num


def amount_bucket(value: str) -> str | None:
    """Coarsen a CNY amount to a LOG-scale range (wide bands). Returns None if no
    number is found (caller falls back to a placeholder).

    Examples: ``30亿元`` → ``10亿–100亿元``; ``1500万元`` → ``1000万–1亿元``;
    ``￥99,800元`` → ``100万元以下``.
    """
    yuan = _parse_cny_yuan(value)
    if yuan is None:
        return None
    if yuan < 1e6:
        return "100万元以下"
    if yuan < 1e7:
        return "100万–1000万元"
    if yuan < 1e8:
        return "1000万–1亿元"
    if yuan < 1e9:
        return "1亿–10亿元"
    if yuan < 1e10:
        return "10亿–100亿元"
    return "100亿元以上"


def amount_band_10yi(value: str) -> str | None:
    """Coarsen a CNY amount to FIXED 10亿-wide bands in the 亿 range. Finer than
    ``amount_bucket`` because some investment amounts cross policy/approval
    thresholds, so a 10亿–100亿 lump would erase a decision-relevant distinction
    (用户要求：以 10 亿为一个区间).

    Examples: ``30亿元`` → ``30亿–40亿元``; ``15亿元`` → ``10亿–20亿元``;
    ``5亿元`` → ``10亿元以下``; ``120亿元`` → ``100亿元以上``.
    """
    yuan = _parse_cny_yuan(value)
    if yuan is None:
        return None
    yi = yuan / 1e8
    if yi < 10:
        return "10亿元以下"
    if yi >= 100:
        return "100亿元以上"
    lo = int(yi // 10) * 10
    return f"{lo}亿–{lo + 10}亿元"


# --- address → province (机构/项目地点粗化) ---------------------------------
# First-draft: municipalities → "<市>", explicit 省/自治区 kept as-is, a small
# major-city → province table for the common case, else "某地区". The table is
# deliberately small and meant to be extended per deployment (待补).

_MUNICIPALITIES = ("北京", "上海", "天津", "重庆")

_AUTONOMOUS_RE = re.compile(r"(内蒙古|广西壮族|西藏|宁夏回族|新疆维吾尔)自治区")
_PROVINCE_RE = re.compile(r"([一-龥]{2,3})省")

_CITY_TO_PROVINCE = {
    "济南": "山东省", "青岛": "山东省", "烟台": "山东省",
    "广州": "广东省", "深圳": "广东省", "东莞": "广东省",
    "杭州": "浙江省", "宁波": "浙江省",
    "南京": "江苏省", "苏州": "江苏省", "无锡": "江苏省",
    "成都": "四川省", "武汉": "湖北省", "长沙": "湖南省",
    "西安": "陕西省", "郑州": "河南省", "合肥": "安徽省",
    "福州": "福建省", "厦门": "福建省", "南昌": "江西省",
    "石家庄": "河北省", "太原": "山西省", "沈阳": "辽宁省",
    "大连": "辽宁省", "长春": "吉林省", "哈尔滨": "黑龙江省",
    "昆明": "云南省", "贵阳": "贵州省", "南宁": "广西壮族自治区",
    "兰州": "甘肃省", "乌鲁木齐": "新疆维吾尔自治区",
}


def region(value: str) -> str | None:
    """Coarsen an address to province/municipality level, dropping the specific
    street/district. Returns ``某地区`` for unrecognized inputs (never the
    original, so the specific location does not leak)."""
    for muni in _MUNICIPALITIES:
        if muni in value:
            return f"{muni}市"
    auto = _AUTONOMOUS_RE.search(value)
    if auto:
        return auto.group(0)
    prov = _PROVINCE_RE.search(value)
    if prov:
        return prov.group(0)
    for city, province in _CITY_TO_PROVINCE.items():
        if city in value:
            return province
    return "某地区"


# --- exact date → quarter ---------------------------------------------------

_DATE_RE = re.compile(r"(\d{4})\D{0,2}(\d{1,2})")


def quarter(value: str) -> str | None:
    """Coarsen a date to year + quarter. ``2026年06月12日`` / ``2026-06-12`` →
    ``2026年Q2``. Returns None if no year+month is found."""
    match = _DATE_RE.search(value)
    if not match:
        return None
    year = int(match.group(1))
    month = int(match.group(2))
    if not 1 <= month <= 12:
        return None
    q = (month - 1) // 3 + 1
    return f"{year}年Q{q}"


GENERALIZERS: dict[str, Callable[[str], str | None]] = {
    "amount_bucket": amount_bucket,
    "amount_band_10yi": amount_band_10yi,
    "region": region,
    "quarter": quarter,
}


def generalize_value(name: str | None, value: str) -> str | None:
    """Apply the named generalizer to ``value``.

    Returns the coarsened string, or ``None`` when the generalizer is unknown or
    cannot coarsen the value — the caller treats ``None`` as "fall back to a
    reversible placeholder" so the original is never left in clear (fail-closed).

    The value is normalized to the half-width view first: detection matches
    full-width forms (``３２亿元``) but the parsers below read half-width
    digits/punctuation. Coarse outputs are generated strings, so this never
    affects original-text offsets.
    """
    if not name:
        return None
    fn = GENERALIZERS.get(name)
    if fn is None:
        return None
    try:
        return fn(to_halfwidth(value))
    except Exception:
        return None
