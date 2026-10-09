"""授权状态持久化：已激活授权码 + 单调时钟（防回拨）。

安全语义：
- 激活必须先验签，未通过不落盘；
- 读路径（load/touch_clock）绝不抛异常——文件缺失或损坏一律按"无授权/
  无历史时间"处理，授权缺失只影响新建任务，不得损坏或阻断已有数据；
- 写盘均原子化（临时文件 + os.replace），权限 0600。
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

from tuomin_gateway.licensing.core import (
    LicensePayload,
    decode_license_code,
)
from tuomin_gateway.platform_security import restrict_permissions

_LICENSE_FILE = "license.json"
_CLOCK_FILE = "license-clock.json"


class LicenseStore:
    def __init__(self, data_dir: Path):
        self._data_dir = Path(data_dir)

    @property
    def license_path(self) -> Path:
        return self._data_dir / _LICENSE_FILE

    @property
    def clock_path(self) -> Path:
        return self._data_dir / _CLOCK_FILE

    def activate(self, code: str, *, public_key_hex: str = "") -> LicensePayload:
        """验签并保存授权码；验签失败抛 LicenseError 子类且不落盘。"""
        payload = decode_license_code(code, public_key_hex=public_key_hex)
        record = {
            "code": payload.raw_code,
            "license_id": payload.license_id,
            "customer": payload.customer,
            "product": payload.product,
            "issued_at": payload.issued_at.isoformat(),
            "expires_at": payload.expires_at.isoformat(),
        }
        self._write_json_atomic(self.license_path, record)
        return payload

    def load(self, *, public_key_hex: str = "") -> LicensePayload | None:
        """读取已激活授权；缺失/损坏/验签失败一律返回 None。"""
        try:
            record = json.loads(self.license_path.read_text(encoding="utf-8"))
            code = record["code"]
            return decode_license_code(code, public_key_hex=public_key_hex)
        except Exception:
            return None

    def touch_clock(self, now: datetime) -> datetime:
        """返回并持久化 max(历史最大时间, now)（UTC aware）。读取损坏按无历史处理。"""
        current = now.astimezone(timezone.utc)
        best = current
        try:
            record = json.loads(self.clock_path.read_text(encoding="utf-8"))
            text = str(record.get("max_seen", ""))
            if text.endswith("Z"):
                text = text[:-1] + "+00:00"
            seen = datetime.fromisoformat(text)
            if seen.tzinfo is not None and seen.astimezone(timezone.utc) > best:
                best = seen.astimezone(timezone.utc)
        except Exception:
            pass
        if best > current:
            return best
        try:
            self._write_json_atomic(self.clock_path, {"max_seen": current.isoformat()})
        except OSError:
            pass
        return current

    def _write_json_atomic(self, path: Path, record: dict) -> None:
        self._data_dir.mkdir(parents=True, exist_ok=True)
        restrict_permissions(self._data_dir, is_dir=True)
        tmp_path = path.with_name(f".{path.name}.tmp")
        fd = os.open(tmp_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            restrict_permissions(tmp_path, is_dir=False)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                fd = -1
                json.dump(record, handle, ensure_ascii=False, indent=2)
                handle.write("\n")
            os.replace(tmp_path, path)
            restrict_permissions(path, is_dir=False)
        finally:
            if fd >= 0:
                os.close(fd)
            tmp_path.unlink(missing_ok=True)


class ClockTracker:
    """进程内单调时钟：合并 LicenseStore 持久化的 max_seen，限频写盘。

    每个请求都 touch 磁盘太贵；首次调用加载持久化值，之后只在内存里推进，
    每隔 min_interval_seconds 落盘一次。进程重启后从持久化值继续，回拨的
    时钟不会超过已见过的最大时间。
    """

    def __init__(self, store: LicenseStore, min_interval_seconds: int = 3600):
        self._store = store
        self._min_interval = min_interval_seconds
        self._best: datetime | None = None
        self._last_write: datetime | None = None

    def observe(self, now: datetime) -> datetime:
        current = now.astimezone(timezone.utc)
        if self._best is None:
            self._best = self._store.touch_clock(current)
            self._last_write = current
            return self._best
        if current > self._best:
            self._best = current
        assert self._last_write is not None
        if (current - self._last_write).total_seconds() >= self._min_interval:
            self._best = self._store.touch_clock(self._best)
            self._last_write = current
        return self._best


def resolve_state(data_dir: Path | None = None):
    """从磁盘授权记录直接解析当前状态（不依赖 service 层装配的 provider）。

    供编译进原生代码的业务入口兜底使用：即使解释层的中间件/provider 被
    patch，原生代码仍能通过本函数得到真实授权状态。
    """
    import os

    from tuomin_gateway.licensing.core import compute_state

    if data_dir is None:
        data_dir = Path(os.environ.get("TUOMIN_DATA_DIR", "tuomin_data"))
    store = LicenseStore(data_dir)
    now = datetime.now(timezone.utc)
    max_seen = store.touch_clock(now)
    payload = store.load()
    return compute_state(payload, now=now, max_seen=max_seen)
