"""离线授权码：签发（scripts/sign_license.py）与验签（licensing.core）。

设计要点（2026-08-24 与用户定稿）：
- 授权码是可粘贴字符串 `TM1.<b64url(payload)>.<b64url(sig)>`，客户在 WebUI
  粘贴一次即激活；payload 只含客户名与期限（site 模式，不绑机器）。
- Ed25519 验签，公钥内嵌于本模块（随 Nuitka 编译进原生代码），私钥只存在于
  发行方签发机，不进仓库、不进分发包。
- 到期语义从宽：到期后 30 天宽限期全功能 + 提示；宽限过后停止新建任务，
  已有 mapping 的回填/导出永远可用（恢复能力优先，不锁死客户数据）。
"""

from tuomin_gateway.licensing.core import (
    GRACE_DAYS,
    ISSUER_PUBLIC_KEY_HEX,
    LicenseBlockedError,
    LicenseError,
    LicenseFormatError,
    LicensePayload,
    LicenseProductError,
    LicenseSignatureError,
    LicenseState,
    compute_state,
    configure_enforcement,
    current_license_state,
    decode_license_code,
    enforce_new_task,
    is_new_task_request,
    new_tasks_allowed,
)
from tuomin_gateway.licensing.state import ClockTracker, LicenseStore

__all__ = [
    "ClockTracker",
    "GRACE_DAYS",
    "ISSUER_PUBLIC_KEY_HEX",
    "LicenseBlockedError",
    "LicenseError",
    "LicenseFormatError",
    "LicensePayload",
    "LicenseProductError",
    "LicenseSignatureError",
    "LicenseState",
    "LicenseStore",
    "compute_state",
    "configure_enforcement",
    "current_license_state",
    "decode_license_code",
    "enforce_new_task",
    "is_new_task_request",
    "new_tasks_allowed",
]
