"""Quasi-identifier generalizers (pure functions; no model/deps)."""
from tuomin_gateway.generalizers import (
    amount_band_10yi,
    amount_bucket,
    generalize_value,
    quarter,
    region,
)


def test_amount_bucket_log_scale():
    assert amount_bucket("30亿元") == "10亿–100亿元"
    assert amount_bucket("1500万元") == "1000万–1亿元"
    assert amount_bucket("￥99,800元") == "100万元以下"
    assert amount_bucket("2亿元") == "1亿–10亿元"
    assert amount_bucket("100亿元") == "100亿元以上"
    assert amount_bucket("人民币123,456.78元") == "100万元以下"


def test_amount_band_10yi_fixed_10yi_bands():
    # Finer than the log buckets: 10亿-wide bands so policy/approval thresholds
    # in the 亿 range are not erased (用户要求).
    assert amount_band_10yi("30亿元") == "30亿–40亿元"
    assert amount_band_10yi("15亿元") == "10亿–20亿元"
    assert amount_band_10yi("5亿元") == "10亿元以下"
    assert amount_band_10yi("99亿元") == "90亿–100亿元"
    assert amount_band_10yi("120亿元") == "100亿元以上"
    assert amount_band_10yi("若干") is None


def test_amount_bucket_none_without_number():
    # No parseable number -> caller falls back to a reversible placeholder.
    assert amount_bucket("若干元") is None


def test_region_coarsens_to_province_or_municipality():
    assert region("济南市历下区某路100号") == "山东省"   # city table
    assert region("山东省青岛市黄岛区") == "山东省"        # explicit 省
    assert region("北京市朝阳区") == "北京市"             # municipality
    assert region("内蒙古自治区呼和浩特市") == "内蒙古自治区"
    assert region("某偏远小镇") == "某地区"               # unknown -> NOT the original


def test_quarter_coarsens_date():
    assert quarter("2026年06月12日") == "2026年Q2"
    assert quarter("2026-06-12") == "2026年Q2"
    assert quarter("2027年3月15日") == "2027年Q1"
    assert quarter("2026/10/01") == "2026年Q4"


def test_quarter_none_without_date():
    assert quarter("近期交付") is None


def test_generalize_value_dispatch_and_fail_closed():
    assert generalize_value("amount_bucket", "30亿元") == "10亿–100亿元"
    assert generalize_value("region", "济南市某路") == "山东省"
    assert generalize_value("quarter", "2026-06-12") == "2026年Q2"
    # Unknown / missing generalizer -> None (caller falls back to placeholder).
    assert generalize_value("nonexistent", "30亿元") is None
    assert generalize_value(None, "30亿元") is None
