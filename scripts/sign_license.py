#!/usr/bin/env python3
"""Tuomin 离线授权码签发工具（发行方专用）。

私钥纪律：
- 私钥只存在于发行方签发机，绝不进入仓库、分发包、CI；
- 本脚本拒绝把私钥写入仓库目录内；
- 私钥内容永远不出现在 stdout / 异常消息中。

用法：
    # 首次：生成密钥对（路径务必在仓库外），打印公钥 hex 供填入
    # src/tuomin_gateway/licensing/core.py 的 ISSUER_PUBLIC_KEY_HEX
    python scripts/sign_license.py --init-key --key-path ~/tuomin-issuer/key.pem

    # 签发：给客户「XX 单位」签 90 天
    python scripts/sign_license.py --key-path ~/tuomin-issuer/key.pem \
        --customer "XX 单位" --days 90
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import secrets
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
)
from cryptography.hazmat.primitives.serialization import (
    Encoding,
    NoEncryption,
    PrivateFormat,
    PublicFormat,
    load_pem_private_key,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
PRODUCT_ID = "tuomin-workbench"


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _refuse_repo_path(path: Path) -> None:
    resolved = path.resolve()
    if resolved == REPO_ROOT or REPO_ROOT in resolved.parents:
        raise SystemExit("拒绝把签发私钥写入仓库目录内；请放到仓库外的安全位置")


def _restrict_private_file(path: Path) -> None:
    """Apply real owner-private permissions on POSIX and Windows."""
    source_root = REPO_ROOT / "src"
    if str(source_root) not in sys.path:
        sys.path.insert(0, str(source_root))
    from tuomin_gateway.platform_security import restrict_permissions

    restrict_permissions(path, is_dir=False)


def _ledger_path(key_path: Path) -> Path:
    """签发台账：与私钥同目录（仓库外），只记元数据，不含任何秘密。"""
    return key_path.resolve().parent / "ledger.jsonl"


def _ledger_append(key_path: Path, payload: dict, note: str) -> None:
    entry = {
        "license_id": payload["license_id"],
        "customer": payload["customer"],
        "note": note,
        "issued_at": payload["issued_at"],
        "expires_at": payload["expires_at"],
    }
    path = _ledger_path(key_path)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        _restrict_private_file(path)
        with os.fdopen(fd, "a", encoding="utf-8") as handle:
            fd = -1
            handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
    finally:
        if fd >= 0:
            os.close(fd)


def list_ledger(key_path: Path) -> int:
    grace_days = 30  # 与 licensing.core.GRACE_DAYS 保持一致（本脚本须可独立运行）

    path = _ledger_path(key_path)
    if not path.is_file():
        print(f"[issuer] 台账为空（{path} 不存在）")
        return 0
    now = datetime.now(timezone.utc)
    print(f"{'license_id':<18}{'客户':<12}{'备注':<12}{'到期':<21}状态")
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        entry = json.loads(line)
        expires = datetime.fromisoformat(entry["expires_at"].replace("Z", "+00:00"))
        if now <= expires:
            state = f"有效（剩 {(expires - now).days} 天）"
        elif now <= expires + timedelta(days=grace_days):
            state = "宽限期"
        else:
            state = "已过期"
        print(
            f"{entry['license_id']:<18}{entry['customer']:<12}"
            f"{entry.get('note', ''):<12}{entry['expires_at']:<21}{state}"
        )
    return 0


def init_key(key_path: Path) -> int:
    _refuse_repo_path(key_path)
    if key_path.exists():
        raise SystemExit(f"私钥文件已存在，拒绝覆盖：{key_path}")
    key_path.parent.mkdir(parents=True, exist_ok=True)
    private_key = Ed25519PrivateKey.generate()
    pem = private_key.private_bytes(Encoding.PEM, PrivateFormat.PKCS8, NoEncryption())
    fd = os.open(key_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        _restrict_private_file(key_path)
        with os.fdopen(fd, "wb") as handle:
            fd = -1
            handle.write(pem)
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        if fd >= 0:
            os.close(fd)
        key_path.unlink(missing_ok=True)
        raise
    public_hex = private_key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw).hex()
    print(public_hex)
    if os.name == "nt":
        print(
            f"[issuer] private key written to {key_path} (owner-only Windows ACL); public key hex is shown above.",
            file=sys.stderr,
        )
    else:
        print(f"[issuer] 私钥已写入 {key_path}（0600）；上行是公钥 hex，", file=sys.stderr)
    print("[issuer] 请填入 src/tuomin_gateway/licensing/core.py 的 ISSUER_PUBLIC_KEY_HEX", file=sys.stderr)
    return 0


def sign_license(key_path: Path, customer: str, days: int, license_id: str | None, note: str) -> int:
    pem = key_path.read_bytes()
    private_key = load_pem_private_key(pem, password=None)
    if not isinstance(private_key, Ed25519PrivateKey):
        raise SystemExit("私钥文件不是 Ed25519 密钥")
    now = datetime.now(timezone.utc)
    payload = {
        "v": 1,
        "license_id": license_id or f"lic_{secrets.token_hex(6)}",
        "customer": customer,
        "product": PRODUCT_ID,
        "issued_at": now.isoformat().replace("+00:00", "Z"),
        "expires_at": (now + timedelta(days=days)).isoformat().replace("+00:00", "Z"),
    }
    payload_bytes = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    signature = private_key.sign(payload_bytes)
    code = f"TM1.{_b64url(payload_bytes)}.{_b64url(signature)}"
    print(code)
    _ledger_append(key_path, payload, note)
    print(
        f"[issuer] 已签发：customer={customer} days={days} "
        f"license_id={payload['license_id']} expires_at={payload['expires_at']}"
        + (f" note={note}" if note else ""),
        file=sys.stderr,
    )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--init-key", action="store_true", help="生成新的签发密钥对")
    parser.add_argument("--list", action="store_true", help="列出台账中的全部签发记录与状态")
    parser.add_argument("--key-path", required=True, type=Path, help="私钥 PEM 路径（必须在仓库外）")
    parser.add_argument("--customer", help="客户/单位名称")
    parser.add_argument("--days", type=int, help="有效天数")
    parser.add_argument("--license-id", help="自定义 license_id（缺省随机生成）")
    parser.add_argument("--note", default="", help="台账备注（如实际使用人），不进入授权码")
    args = parser.parse_args()

    if args.init_key:
        return init_key(args.key_path)
    if args.list:
        return list_ledger(args.key_path)
    if not args.customer or not args.days or args.days <= 0:
        parser.error("签发时需要 --customer 和正整数 --days")
    return sign_license(args.key_path, args.customer, args.days, args.license_id, args.note)


if __name__ == "__main__":
    raise SystemExit(main())
