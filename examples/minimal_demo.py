"""仅合成数据：无模型、无网络、无配置读取、映射不落盘。"""
from __future__ import annotations

from tuomin_gateway.detectors.dictionary import DictionaryDetector
from tuomin_gateway.detectors.rules import RuleDetector
from tuomin_gateway.fusion import fuse_detections
from tuomin_gateway.redactor import redact_text
from tuomin_gateway.refill import refill_text


def run_demo() -> dict[str, str]:
    text = "示例建设单位甲与合成供应商乙签署合同，合同编号：HT-2026-DEMO-0001。"
    names = ("示例建设单位甲", "合成供应商乙")
    dictionary = DictionaryDetector.from_entries([
        {"entry_id": f"synthetic_{i}", "canonical_value": name,
         "aliases": [], "label": "ORG", "risk_level": "high",
         "status": "active", "version": "synthetic-demo-v1"}
        for i, name in enumerate(names, 1)
    ])
    detections = fuse_detections(RuleDetector().detect(text) + dictionary.detect(text), text)
    result = redact_text(text, detections, task_id="synthetic_demo")
    expected_values = (*names, "HT-2026-DEMO-0001")
    if any(value in result.redacted_text for value in expected_values):
        raise RuntimeError("示例中已声明的敏感值未被完整替换")
    restored = refill_text(result.redacted_text, result.mapping)
    if restored.status != "ok" or restored.text != text:
        raise RuntimeError("示例未能逐字回填")
    blocked = refill_text(result.redacted_text + "<ORG_999>", result.mapping)
    if blocked.status != "blocked" or "unknown_placeholder" not in blocked.error_types:
        raise RuntimeError("未知占位符未被阻断")
    return {"original": text, "redacted": result.redacted_text,
            "restored": restored.text, "invalid_refill": blocked.status}


if __name__ == "__main__":
    output = run_demo()
    print("仅合成示例；规则与词典检测，不代表完整模型检测能力。")
    print("输入：", output["original"])
    print("脱敏：", output["redacted"])
    print("本地回填：", output["restored"])
    print("未知占位符：", output["invalid_refill"])
    print("示例验证通过；未加载模型，未发送网络请求，未保存映射。")
