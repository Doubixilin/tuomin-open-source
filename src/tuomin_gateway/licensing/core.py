"""离线授权码验签核心（纯逻辑，无 I/O）。

本模块会被 Nuitka 编译为原生扩展（见 scripts/build_native_modules.py），
因此只使用标准语言特性：不用 inspect、不动态导入、不读 __file__ 资源。

授权码格式：``TM1.<base64url(payload-json)>.<base64url(ed25519-signature)>``
签名对象是 payload 段 base64url 解码后的**原始字节**——不得以任何方式重新
序列化 JSON 再验证，否则格式细微差异会被误判或绕过。
"""

from __future__ import annotations

import base64
import binascii
import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

# 可选授权机制的公钥占位。社区源码运行无需激活；默认不启用此机制。
# 研究该机制时请自行生成测试密钥，不复用任何发行身份。
ISSUER_PUBLIC_KEY_HEX: str = ""

# 产品标识：授权码必须声明签发给本产品，防止跨产品混用。
PRODUCT_ID = "tuomin-workbench"

# 到期后的宽限天数：宽限期内功能不受限，仅持续提示；宽限过后停止新建任务。
GRACE_DAYS: int = 30

_CODE_PREFIX = "TM1."


class LicenseError(Exception):
    """授权码相关错误基类。"""


class LicenseFormatError(LicenseError):
    """授权码结构、字段或取值不合法。"""


class LicenseSignatureError(LicenseError):
    """签名校验失败（含公钥未配置）。"""


class LicenseProductError(LicenseError):
    """授权码签发给其他产品。"""


class LicenseState(Enum):
    UNLICENSED = "unlicensed"
    VALID = "valid"
    GRACE = "grace"
    EXPIRED = "expired"


@dataclass(frozen=True)
class LicensePayload:
    license_id: str
    customer: str
    product: str
    issued_at: datetime
    expires_at: datetime
    raw_code: str


def _b64url_decode(segment: str) -> bytes:
    try:
        padding = "=" * (-len(segment) % 4)
        return base64.urlsafe_b64decode(segment + padding)
    except (binascii.Error, ValueError) as exc:
        raise LicenseFormatError("授权码包含非法 base64 段") from exc


def _parse_utc(value: object, field: str) -> datetime:
    if not isinstance(value, str):
        raise LicenseFormatError(f"授权码字段 {field} 缺失或不是字符串")
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise LicenseFormatError(f"授权码字段 {field} 不是合法 ISO 时间") from exc
    if parsed.tzinfo is None:
        raise LicenseFormatError(f"授权码字段 {field} 必须带时区")
    return parsed.astimezone(timezone.utc)


def decode_license_code(code: str, *, public_key_hex: str = "") -> LicensePayload:
    """解析并验签授权码；任何一步失败都抛 LicenseError 子类。"""
    key_hex = public_key_hex or ISSUER_PUBLIC_KEY_HEX
    if not key_hex:
        raise LicenseSignatureError("发行方公钥未配置，无法验证授权码")

    text = code.strip()
    if not text.startswith(_CODE_PREFIX):
        raise LicenseFormatError("授权码必须以 TM1. 开头")
    body = text[len(_CODE_PREFIX) :]
    parts = body.split(".")
    if len(parts) != 2 or not all(parts):
        raise LicenseFormatError("授权码结构应为 TM1.<payload>.<signature>")
    payload_segment, signature_segment = parts

    payload_bytes = _b64url_decode(payload_segment)
    signature = _b64url_decode(signature_segment)
    if len(signature) != 64:
        raise LicenseFormatError("授权码签名长度非法")

    try:
        public_key = Ed25519PublicKey.from_public_bytes(bytes.fromhex(key_hex))
    except ValueError as exc:
        raise LicenseSignatureError("发行方公钥配置非法") from exc
    try:
        public_key.verify(signature, payload_bytes)
    except Exception as exc:  # cryptography InvalidSignature 及其基类
        raise LicenseSignatureError("授权码签名校验失败") from exc

    try:
        payload = json.loads(payload_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise LicenseFormatError("授权码 payload 不是合法 JSON") from exc
    if not isinstance(payload, dict):
        raise LicenseFormatError("授权码 payload 必须是 JSON 对象")

    if payload.get("v") != 1:
        raise LicenseFormatError("授权码格式版本不受支持")
    license_id = payload.get("license_id")
    customer = payload.get("customer")
    if not isinstance(license_id, str) or not license_id.strip():
        raise LicenseFormatError("授权码缺少 license_id")
    if not isinstance(customer, str) or not customer.strip():
        raise LicenseFormatError("授权码缺少 customer")
    product = payload.get("product")
    if product != PRODUCT_ID:
        raise LicenseProductError(f"授权码签发给其他产品：{product!r}")

    issued_at = _parse_utc(payload.get("issued_at"), "issued_at")
    expires_at = _parse_utc(payload.get("expires_at"), "expires_at")
    if expires_at <= issued_at:
        raise LicenseFormatError("授权码到期时间必须晚于签发时间")

    return LicensePayload(
        license_id=license_id.strip(),
        customer=customer.strip(),
        product=product,
        issued_at=issued_at,
        expires_at=expires_at,
        raw_code=text,
    )


def compute_state(
    payload: LicensePayload | None,
    *,
    now: datetime,
    max_seen: datetime | None = None,
) -> LicenseState:
    """由授权内容与时间计算授权状态。

    effective_now 取 max(now, max_seen)：客户回拨系统时钟不能使已过期的
    授权重新生效（max_seen 由 LicenseStore 单调持久化）。
    """
    if payload is None:
        return LicenseState.UNLICENSED
    effective_now = now.astimezone(timezone.utc)
    if max_seen is not None and max_seen.astimezone(timezone.utc) > effective_now:
        effective_now = max_seen.astimezone(timezone.utc)
    if effective_now <= payload.expires_at:
        return LicenseState.VALID
    if effective_now <= payload.expires_at + timedelta(days=GRACE_DAYS):
        return LicenseState.GRACE
    return LicenseState.EXPIRED


def new_tasks_allowed(state: LicenseState) -> bool:
    """该状态下是否允许新建任务（脱敏/代理/MCP/文件工作台）。

    注意：已有 mapping 的回填、导出与恢复**不受本函数限制**——恢复能力
    优先是产品红线，上层不得用本函数拦截恢复路径。
    """
    return state in (LicenseState.VALID, LicenseState.GRACE)


# --- 执行层（enforcement） ---------------------------------------------------
#
# 两层结构：
# 1. service 层 HTTP 中间件按 is_new_task_request() 分类拦截，返回干净的 403；
# 2. 编译进原生代码的业务入口（如 redactor.redact_text）各自调用
#    enforce_new_task() 作为后手——只删中间件（解释层 .pyc）绕不过原生代码。
#
# 本模块会被 Nuitka 编译，路径分类表和执行逻辑随之进入原生代码。

_BLOCKED_PATH_PREFIXES = (
    # 文本脱敏（新建任务）
    "/redact",
    "/redact_snippet",
    "/api/v1/detect",
    "/api/v1/redact",
    # 文件工作台（新建任务）
    "/api/v1/documents/parse",
    "/api/v1/documents/redact-docx",
    # 会话与命名空间（新建/追加脱敏）
    "/session/open",
    "/api/v1/namespaces",
    # 代理（一切通向上游的流量）
    "/v1/messages",
    "/v1/chat/completions",
    "/apps/",
    "/api/v1/proxy-sessions",
    "/proxy-sessions/",
)

# 恢复与管理面（refill/导出/unlock/package/归档/inspection）一律不在名单内，
# 见 is_new_task_request 函数内的放行分支。


def is_new_task_request(method: str, path: str) -> bool:
    """该 HTTP 请求是否属于"新建任务"入口（授权拦截名单）。

    设计为**显式名单 + 默认放行**：将来新增路由忘了登记，最坏结果是商业
    层面漏管，绝不会误锁客户的恢复/导出路径。
    """
    if method not in ("POST", "PUT", "PATCH", "DELETE"):
        return False
    # 恢复与管理面明确放行
    if path.startswith("/api/v1/refill"):
        return False
    if path.startswith("/api/v1/documents/refill") or path.startswith(
        "/api/v1/documents/unlock-mapping"
    ):
        return False
    if path.startswith("/session/") and (
        path.endswith("/refill") or path.endswith("/unmask")
    ):
        return False
    # namespaces：创建(POST /api/v1/namespaces)与追加脱敏(…/redact)拦截；
    # 查询、归档、inspection 快照等既有数据管理放行。
    if path.startswith("/api/v1/namespaces/") and not path.endswith("/redact"):
        return False
    # proxy-sessions：创建(POST /api/v1/proxy-sessions)拦截；查询/关闭放行。
    if path.startswith("/api/v1/proxy-sessions/"):
        return False
    for prefix in _BLOCKED_PATH_PREFIXES:
        if path.startswith(prefix):
            return True
    return False


class LicenseBlockedError(LicenseError):
    """当前授权状态禁止新建任务。携带 state 供上层生成提示。"""

    def __init__(self, state: LicenseState):
        super().__init__(f"license state {state.value} forbids new tasks")
        self.state = state


_state_provider = None  # Callable[[], LicenseState] | None


def configure_enforcement(provider) -> None:
    """装配授权状态来源。provider 为 None 表示关闭执行（开发/测试默认）。"""
    global _state_provider
    _state_provider = provider


def _fallback_state():
    """provider 未装配时的原生兜底解析（冻结分发场景）。

    开发/测试（TUOMIN_LICENSE_REQUIRED 非 "1"）返回 None 表示不执行。
    延迟导入 state 模块避免循环依赖。
    """
    import os

    if os.environ.get("TUOMIN_LICENSE_REQUIRED", "") != "1":
        return None
    from tuomin_gateway.licensing.state import resolve_state

    return resolve_state()


def current_license_state() -> LicenseState:
    """当前授权状态；执行关闭（开发/测试）时恒为 VALID。"""
    if _state_provider is not None:
        return _state_provider()
    fallback = _fallback_state()
    return fallback if fallback is not None else LicenseState.VALID


def enforce_new_task() -> None:
    """业务入口后手：授权不允许新建任务时抛 LicenseBlockedError。

    执行开启（TUOMIN_LICENSE_REQUIRED=1）时**只信本模块原生解析的磁盘授权
    状态**——provider 只是 HTTP 层的状态显示来源，把 provider 换成返回 VALID
    的函数不能影响本函数（该路径曾被逆向评估指出为信任边界，2026-08-25 修复）。
    provider 仅在执行关闭（开发/测试）时被尊重。
    """
    state = _fallback_state()
    if state is None:
        if _state_provider is None:
            return
        state = _state_provider()
    if not new_tasks_allowed(state):
        raise LicenseBlockedError(state)
