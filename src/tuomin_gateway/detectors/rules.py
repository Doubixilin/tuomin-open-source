from __future__ import annotations

import re

from tuomin_gateway.detectors.base import BaseDetector
from tuomin_gateway.schemas import DetectionSpan, hash_text
from tuomin_gateway.textnorm import to_halfwidth


class UnsafeCredentialInput(ValueError):
    """Private-key framing is incomplete; no partial result is safe to export."""


_PRIVATE_KEY_MARKER = re.compile(
    r"(?P<leading>-{2,})(?P<kind>BEGIN|END)[ \t]+"
    r"(?P<label>(?:[A-Z0-9]+[ \t]+)?PRIVATE(?:[ \t]+KEY)?)"
    r"(?P<trailing>-*)", re.IGNORECASE,
)


def private_key_ranges(text: str) -> list[tuple[int, int]]:
    """Return whole PEM blocks or reject malformed framing without echoing input."""
    scan = to_halfwidth(text)
    pending = None
    ranges = []
    for marker in _PRIVATE_KEY_MARKER.finditer(scan):
        label = " ".join(marker["label"].upper().split())
        if (marker["leading"] != "-----" or marker["trailing"] != "-----"
                or not label.endswith("PRIVATE KEY")):
            raise UnsafeCredentialInput("incomplete private key block")
        if marker["kind"].upper() == "BEGIN":
            if pending is not None:
                raise UnsafeCredentialInput("nested private key block")
            pending = (marker.start(), marker.end(), label)
        else:
            if pending is None or pending[2] != label:
                raise UnsafeCredentialInput("mismatched private key block")
            if not text[pending[1]:marker.start()].strip():
                raise UnsafeCredentialInput("empty private key block")
            ranges.append((pending[0], marker.end()))
            pending = None
    if pending is not None:
        raise UnsafeCredentialInput("incomplete private key block")
    return ranges


def private_key_detections(text: str) -> list[DetectionSpan]:
    return [DetectionSpan(
        start=start, end=end, label="CREDENTIAL", confidence=1.0,
        source="rule", detector_version=RuleDetector.version,
        text_hash=hash_text(text[start:end]), risk_level="critical",
        metadata={"rule_id": "private_key_block", "whole_private_key": True},
    ) for start, end in private_key_ranges(text)]


class RuleDetector(BaseDetector):
    name = "rule"
    version = "rules-2026.09.14-secrets-pem"

    _PATTERNS: tuple[tuple[str, str, re.Pattern[str], str, float, int | None], ...] = (
        (
            # Boundary excludes ASCII letters+digits so an 11-digit phone-shaped
            # run embedded in an alphanumeric token (e.g. A13800138000B, an order
            # id) does not false-positive; CJK context around it still matches.
            "phone_cn",
            "CONTACT",
            re.compile(r"(?<![0-9A-Za-z])1[3-9]\d{9}(?![0-9A-Za-z])"),
            "high",
            0.97,
            None,
        ),
        (
            "email",
            "CONTACT",
            re.compile(r"(?<![\w.+-])[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}(?![\w.-])"),
            "high",
            0.96,
            None,
        ),
        (
            # 固定电话：0 开头 3-4 位区号 + 7-8 位号码，可选“转”分机。 plain 写法
            # 必须带 - 或空格分隔（无分隔的 0 开头数字串无法与编号区分，不匹配）；
            # 括号区号（(020)22905676 / （020）…）分隔由括号承担。左侧边界排除
            # 小数点，防止从 0.1234567 这类小数中截取。
            "landline_cn",
            "CONTACT",
            re.compile(
                r"(?<![0-9A-Za-z.])"
                r"(?:0\d{2,3}[- ]\d{7,8}|\(0\d{2,3}\)\s?\d{7,8}|（0\d{2,3}）\s?\d{7,8})"
                r"(?:转\d{1,5})?"
                r"(?![0-9A-Za-z])"
            ),
            "high",
            0.95,
            None,
        ),
        (
            # 18-char USCC. Charset kept as full [0-9A-Z]: real-world data is not
            # always GB32100-clean and the cue already gives high precision.
            "org_credit_code",
            "ORG_CODE",
            re.compile(r"(?:统一社会信用代码|信用代码)[:：\s]*([0-9A-Z]{18})"),
            "high",
            0.95,
            1,
        ),
        (
            # Bare 18-char USCC (no cue) for table cells / lists. Requires both a
            # digit and a letter so an all-digit 18-char run (an ID card) is not
            # mislabelled here.
            "uscc_bare",
            "ORG_CODE",
            re.compile(r"(?<![0-9A-Za-z])(?=[0-9A-Z]*[A-Z])(?=[0-9A-Z]*[0-9])[0-9A-Z]{18}(?![0-9A-Za-z])"),
            "high",
            0.9,
            None,
        ),
        (
            # 居民身份证号：18 位 = 6 位地区码 + YYYYMMDD 出生日期 + 3 位顺序码 + 1 位
            # 校验码(数字或 X)。出生日期的结构化校验(年 19/20、月 01-12、日 01-31)把
            # 误报压到极低，故无需线索即可命中；数字/字母边界防止从更长的卡号/订单号中
            # 截取子串。纯数字 18 位不会被 uscc_bare 命中(那条要求含字母)，二者不冲突。
            "id_card_cn",
            "ID_CARD",
            re.compile(
                r"(?<![0-9A-Za-z])\d{6}(?:19|20)\d{2}(?:0[1-9]|1[0-2])(?:0[1-9]|[12]\d|3[01])\d{3}[0-9Xx](?![0-9A-Za-z])"
            ),
            "critical",
            0.96,
            None,
        ),
        (
            # Allow digit-leading contract numbers (2024-001) too, not only
            # uppercase-prefixed ones; still cue-gated by 编号 to control precision.
            "contract_id",
            "CONTRACT_ID",
            re.compile(r"(?:合同|协议|采购|招标)?编号[:：\s]*([A-Z0-9][A-Z0-9-]{6,})"),
            "high",
            0.96,
            1,
        ),
        (
            "contract_id_meeting_or_case",
            "CONTRACT_ID",
            re.compile(r"(?:会议号|案号)[:：\s]*([A-Z]{2,5}-\d{4}-[A-Z]{2,8}-\d{3,4})"),
            "high",
            0.95,
            1,
        ),
        (
            "contract_id_legal_item",
            "CONTRACT_ID",
            re.compile(r"(?<![A-Z0-9-])([A-Z]{2,5}-\d{4}-[A-Z]{2,8}-\d{3,4})(?=项下)"),
            "high",
            0.94,
            1,
        ),
        (
            # 线索词与号码之间允许 ≤20 个非数字字符：真实文本常是非紧邻写法
            # （"户名：某公司 账号：…"、"开户行：XX银行，账号 …"）。间隙排除句读
            # 和换行，防止跨句牵扯无关数字。仍不做裸号匹配——无线索的长数字串
            # 无法与普通编号区分。
            "bank_account",
            "BANK_ACCOUNT",
            re.compile(
                r"(?:收款账号|银行账号|开户账号|账户|账号|卡号|开户行)"
                r"[^\d。；;\n]{0,20}?"
                r"([0-9](?:[ -]?[0-9]){11,27})"
            ),
            "high",
            0.96,
            1,
        ),
        (
            "bank_card_number",
            "BANK_ACCOUNT",
            re.compile(r"银行卡[:：\s]*([0-9](?:[ -]?[0-9]){11,27})"),
            "high",
            0.95,
            1,
        ),
        (
            # Include an optional 万/亿 unit so 人民币32亿元 is captured WHOLE. Without
            # it the span stopped at 人民币32 (=32 元), which both under-masked (left
            # 亿元 in clear) and mis-generalized 32亿 down to the「10亿以下」band.
            # The digit run accepts BOTH thousands-grouped and plain forms
            # (\d+ then optional ,ddd groups): \d{1,3}(?:,\d{3})* truncated
            # un-grouped values like 人民币5000元 to 人民币500, leaking the tail.
            "amount_cny",
            "AMOUNT",
            re.compile(r"(?:人民币|￥|¥)\s*\d+(?:[,，]\d{3})*(?:\.\d{1,2})?\s*[万亿]?\s*元?|(?:金额|报价|价款)为\s*\d+(?:\.\d{1,2})?万元"),
            "medium",
            0.88,
            None,
        ),
        (
            # Bare 万/亿 amounts with no ￥/人民币 cue (e.g. 合同额201亿元, 潜亏25亿,
            # 800万元). A digit run immediately followed by 万 or 亿 (and an optional
            # 元) is a strong money signal; the digit-boundary lookbehind keeps it
            # from starting mid-number, and requiring 万/亿 right after the digits
            # means quantity decoys like 200吨/24个月/占比15% never fire.
            "amount_cny_unit",
            "AMOUNT",
            re.compile(
                r"(?<![\d.])\d+(?:[,，]\d{3})*(?:\.\d+)?\s*[万亿]"
                r"(?!\s*(?:平\s*方\s*米|平\s*米|㎡|m²))(?:\s*元)?"
            ),
            "medium",
            0.85,
            None,
        ),
        (
            # Bid discount rate (下浮率) — a competitively sensitive figure. Strictly
            # cue-gated to 下浮(率/比例) so generic, non-sensitive percentages
            # (担保比例30%/增值税率9%/占比15%/违约金…20%) are NOT redacted; only the
            # numeric rate after the cue is captured (group 1).
            "discount_rate",
            "DISCOUNT_RATE",
            re.compile(r"下浮(?:率|比例)?\s*[为:：]?\s*(\d+(?:\.\d+)?\s*[%％])"),
            "high",
            0.9,
            1,
        ),
        (
            # 日期三种形态：中文全写（"日"字可选，如 2025年12月31）、连字符/斜杠、
            # 点分隔（2019.03.15）。分隔符用反向引用保持前后一致（2026-08.13 这类
            # 混排不匹配）。
            "date",
            "DATE",
            re.compile(r"\d{4}年\d{1,2}月\d{1,2}日?|\d{4}([-/.])\d{1,2}\1\d{1,2}"),
            "medium",
            0.9,
            None,
        ),
        (
            "url",
            "SYSTEM_URL",
            re.compile(r"https?://[^\s，。；;、！？（）()【】《》“”‘’\"'「」]+"),
            "high",
            0.94,
            None,
        ),
        (
            "token_like",
            "CREDENTIAL",
            re.compile(r"(?:token|api[_-]?key|secret|hr[_-]?key)\s*[:=]\s*([A-Za-z0-9_.=-]{16,})", re.IGNORECASE),
            "critical",
            0.93,
            1,
        ),
        # --- bare secret formats (2026.09.11) -----------------------------------
        # Credential-shaped tokens that appear WITHOUT an "api_key=" style
        # cue were previously only caught by the guard layer on proxy /
        # workbench egress paths; the redact paths let them through. These
        # rules close that gap. Lookarounds mirror phone_cn: ASCII-boundary
        # aware (no false hit inside longer alphanumeric runs) while still
        # matching when glued to CJK text (CJK chars are \w, so \b would
        # fail at the CJK/ASCII seam).
        (
            "aws_access_key",
            "CREDENTIAL",
            re.compile(r"(?<![0-9A-Za-z])AKIA[0-9A-Z]{16}(?![0-9A-Za-z])"),
            "critical",
            0.97,
            None,
        ),
        (
            "github_token",
            "CREDENTIAL",
            re.compile(r"(?<![0-9A-Za-z])gh[pousr]_[A-Za-z0-9]{36,255}(?![0-9A-Za-z])"),
            "critical",
            0.96,
            None,
        ),
        (
            "slack_token",
            "CREDENTIAL",
            re.compile(r"(?<![0-9A-Za-z])xox[baprs]-[0-9A-Za-z-]{10,255}(?![0-9A-Za-z])"),
            "critical",
            0.95,
            None,
        ),
        (
            "live_api_key",
            "CREDENTIAL",
            re.compile(r"(?<![0-9A-Za-z])[sr]k_live_[0-9A-Za-z]{16,255}(?![0-9A-Za-z])"),
            "critical",
            0.96,
            None,
        ),
        (
            "openai_like_key",
            "CREDENTIAL",
            re.compile(r"(?<![0-9A-Za-z])sk-(?:proj-)?[A-Za-z0-9_-]{20,255}(?![0-9A-Za-z])"),
            "critical",
            0.9,
            None,
        ),
        (
            "google_api_key",
            "CREDENTIAL",
            re.compile(r"(?<![0-9A-Za-z])AIza[0-9A-Za-z_\-]{35}(?![0-9A-Za-z])"),
            "critical",
            0.96,
            None,
        ),
        (
            "jwt",
            "CREDENTIAL",
            re.compile(
                r"(?<![0-9A-Za-z])eyJ[A-Za-z0-9_-]{8,1024}\.[A-Za-z0-9_-]{8,1024}\.[A-Za-z0-9_-]{8,1024}(?![0-9A-Za-z])"
            ),
            "critical",
            0.95,
            None,
        ),
        # Supplier/org named by an explicit business-relationship cue AND ending
        # in a business-type suffix. Cue-gated so generic terms like 该公司/本公司
        # never fire (precision-first: protects decoy/over-redaction rate). Helps
        # the "unknown entity" recall for orgs the dictionary/NER miss.
        (
            "org_by_cue",
            "SUPPLIER",
            re.compile(
                r"(?:供应商|分包方|分包队|劳务队|承包商|中标单位|监理单位|监理|对方|乙方|甲方|代表|委托)"
                r"[为是:：]*\s*"
                r"([㐀-鿿]{2,20}(?:有限公司|公司|集团|研究院|设计院|工程局|事务所|"
                r"监理|物流|劳务|租赁|装饰|建材|物资|设备|咨询|证券|地产|机电|钢构|供应链|"
                r"安保服务|服务|分包队|科技|实业|工程))"
            ),
            "high",
            0.85,
            1,
        ),
        (
            "enterprise_by_strong_cue",
            "SUPPLIER",
            re.compile(
                r"(?:未登记(?:机构|主体|顾问|供应商|相对方)|未登记项目名|供应商|相对方|候选人来自未登记机构|来自未登记机构)"
                r"[为是:：]?\s*"
                r"([一-龥]{2,20}(?:供应商[甲乙丙丁]?|规划设计院|设计院|研究院|事务所|工程局|"
                r"咨询|评估|运营|服务|建设|贸易|开发|运维|机电|地产|钢构|租赁|资本|"
                r"公司|集团|基金|证券|银行|保险|科技|实业|工程|劳务|材料|设备|装饰|建材|物资))"
            ),
            "high",
            0.84,
            1,
        ),
        (
            "enterprise_after_partner_cue",
            "SUPPLIER",
            re.compile(
                r"与"
                r"((?!(?:(?:商办|住宅|办公|商业|本|该)?(?:项目|地块|股权)|"
                r"股同权|同股同权|进行|实施|后续))"
                r"[一-龥]{2,20}(?:供应商[甲乙丙丁]?|规划设计院|设计院|研究院|事务所|工程局|"
                r"咨询|评估|运营|服务|建设|贸易|开发|运维|机电|地产|钢构|租赁|资本|"
                r"公司|集团|基金|证券|银行|保险|科技|实业|工程|劳务|材料|设备|装饰|建材|物资))"
                r"(?=就|协商|洽谈|沟通|联络|核对|确认|结算|谈判|提交|对接|发生|报价|，|。)"
            ),
            "high",
            0.83,
            1,
        ),
    )

    def detect(self, text: str) -> list[DetectionSpan]:
        # Scan the half-width view (1:1 translation, offsets identical) so
        # full-width digits/letters match the same patterns; spans are always
        # sliced from the ORIGINAL text (exact-surface invariant). Boundary
        # punctuation is NOT translated (see textnorm), so stop-char sets and
        # lookaheads behave exactly as before.
        scan = to_halfwidth(text)
        spans: list[DetectionSpan] = private_key_detections(text)
        for rule_id, label, pattern, risk_level, confidence, group in self._PATTERNS:
            for match in pattern.finditer(scan):
                start, end = match.span(group) if group is not None else match.span()
                if start == end:
                    continue
                spans.append(
                    self.make_span(
                        text=text,
                        start=start,
                        end=end,
                        label=label,
                        confidence=confidence,
                        risk_level=risk_level,
                        metadata={"rule_id": rule_id},
                    )
                )
        return _deduplicate(spans)


class PublicContextRuleDetector(BaseDetector):
    """Deterministic public-reference classifier enabled by an app profile.

    It stays outside the generic RuleDetector so consumers that did not opt in
    retain byte-for-byte detection behavior and benchmark denominators.
    """

    name = "public_context_rule"
    version = "public-context-rules-2026.07.22"
    _PATTERNS = (
        (
            "public_region",
            "PUBLIC_REGION",
            re.compile(r"((?:[一-龥]{2,8}(?:省|自治区|自治州|市|区|县)){1,3})"),
            0.96,
        ),
        (
            "public_authority",
            "PUBLIC_AUTHORITY",
            re.compile(
                r"((?:[一-龥]{2,8}(?:省|自治区|自治州|市|区|县)){1,3}"
                r"(?:人民政府|规划和自然资源局|自然资源和规划局|自然资源局|发展和改革委员会|"
                r"住房和城乡建设委员会|住房和城乡建设局|财政局|国资委)|"
                r"国务院|自然资源部|国土资源部|规划和自然资源局|自然资源和规划局|"
                r"自然资源局|发展和改革委员会|住房和城乡建设委员会|"
                r"住房和城乡建设局|财政局|国资委)"
            ),
            0.97,
        ),
    )

    def detect(self, text: str) -> list[DetectionSpan]:
        spans: list[DetectionSpan] = []
        for rule_id, label, pattern, confidence in self._PATTERNS:
            for match in pattern.finditer(text):
                start, end = match.span(1)
                spans.append(
                    self.make_span(
                        text=text,
                        start=start,
                        end=end,
                        label=label,
                        confidence=confidence,
                        risk_level="low",
                        metadata={"rule_id": rule_id},
                    )
                )
        return _deduplicate(spans)


def _deduplicate(spans: list[DetectionSpan]) -> list[DetectionSpan]:
    unique: dict[tuple[int, int, str], DetectionSpan] = {}
    for span in spans:
        key = (span.start, span.end, span.label)
        if key not in unique or span.confidence > unique[key].confidence:
            unique[key] = span
    return sorted(unique.values(), key=lambda item: (item.start, item.end, item.label))
