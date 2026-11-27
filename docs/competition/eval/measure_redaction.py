#!/usr/bin/env python
"""诚实脱敏率测量（合成数据，无任何真实信息）—— 报表渲染入口。

测量逻辑已上移到包内 ``tuomin_gateway.benchmark``，数据集已冻结为版本化 fixture
``tests/fixtures/redaction_benchmark.jsonl`` + ``redaction_benchmark_dictionary.json``
（由 ``build_benchmark_fixture.py`` 从 ``benchmark_dataset.py`` 生成）。本脚本只负责
把结构化结果渲染成原来的中文表格，数字与口径不变。

脱敏率定义（防泄漏口径）：一个敏感值被检测 span **完整覆盖** = 不泄漏。
  fully=完整覆盖(没漏) / partial=部分覆盖(仍泄漏一部分) / missed=未覆盖(整段泄漏)。
  脱敏率 = fully / total（与标签对错无关——值被遮住就不泄漏）。
分层：formatted（格式化PII，规则层）/ known（已入词典）/ unknown（未登记，靠NER）/ quasi_identifier / semantic_sensitive。
精确率侧：诱饵保护率 = 未被任何检测 span 触及的诱饵 / 诱饵总数。

用法：
  PYTHONPATH=src python docs/competition/eval/measure_redaction.py          # 规则+词典
  PYTHONPATH=src python docs/competition/eval/measure_redaction.py --ner    # 再加本地NER
"""
from __future__ import annotations

import argparse
import os

from tuomin_gateway.benchmark import run_benchmark

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
SAMPLES = os.path.join(ROOT, "tests", "fixtures", "redaction_benchmark.jsonl")
DICTIONARY = os.path.join(ROOT, "tests", "fixtures", "redaction_benchmark_dictionary.json")

_CATEGORY_NAMES = {"formatted": "格式化PII", "known": "已知(词典)", "unknown": "未知实体", "quasi_identifier": "准标识符", "semantic_sensitive": "语义敏感"}


def render(result: dict) -> None:
    title = "规则 + 词典" + (" + 本地NER" if result["use_ner"] else "")
    decoy_total = result["decoy_protection"]["total"]
    print(
        f"\n{'='*70}\n检测层：{title}　|　{result['sample_count']} 样本 · "
        f"{result['gold_total']} 敏感值 · {decoy_total} 诱饵\n{'='*70}"
    )
    head = f"{'类别':<11}{'总数':>6}{'完整脱敏':>10}{'部分':>7}{'漏检':>7}{'脱敏率':>9}{'   95%CI(Wilson)':<18}"
    print(head)
    print("-" * 78)
    for category in result["categories"]:
        block = result["categories"][category]
        lo, hi = block["ci"]
        name = _CATEGORY_NAMES[category]
        print(
            f"{name:<12}{block['total']:>6}{block['fully']:>10}{block['partial']:>7}"
            f"{block['missed']:>7}{block['rate']:>8.0%}   [{lo:.0%}, {hi:.0%}]"
        )
    overall = result["overall"]
    lo, hi = overall["ci"]
    print("-" * 78)
    print(
        f"{'合计':<12}{overall['total']:>6}{overall['fully']:>10}{overall['partial']:>7}"
        f"{overall['missed']:>7}{overall['rate']:>8.0%}   [{lo:.0%}, {hi:.0%}]"
    )
    prot = result["decoy_protection"]["rate"]
    safe = decoy_total - result["decoy_protection"]["hit"]
    print(f"\n脱敏率(完整覆盖) = {overall['rate']:.1%}  95%CI [{lo:.0%},{hi:.0%}]")
    print(f"诱饵保护率(不过脱敏) = {prot:.1%}（{safe}/{decoy_total} 个诱饵未被误触）")
    print(f"预测 span 总数 = {result['n_pred']}\n")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--ner", action="store_true")
    render(run_benchmark(SAMPLES, DICTIONARY, use_ner=ap.parse_args().ner))
