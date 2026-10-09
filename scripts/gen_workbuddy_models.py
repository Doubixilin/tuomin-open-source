#!/usr/bin/env python3
"""Generate (and self-heal) WorkBuddy custom-model entries for a Tuomin project.

Thin CLI over ``tuomin_gateway.workbuddy_models`` + ``gateway_discovery``; the
same logic backs the local WebUI wizard. Ports are never hard-coded: the script
auto-detects the live gateway and only uses the port that actually serves the
target app (via the side-effect-free ``GET /profiles``), and ``--write`` heals
entries left on an old port.

用法：
    python scripts/gen_workbuddy_models.py --app wb_test --api-key sk-xxx
    python scripts/gen_workbuddy_models.py --app wb_test --api-key sk-xxx --write
    python scripts/gen_workbuddy_models.py --list-gateways

默认把条目 JSON 打印到 stdout；`--write` 合并进 `~/.workbuddy/models.json`
（按 id/模式幂等覆盖，写入前自动备份）。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

try:  # installed package (normal case, and pytest's `pythonpath = ["src"]`)
    from tuomin_gateway.gateway_discovery import (
        DEFAULT_SCAN_RANGE,
        describe_gateways,
        parse_scan_range,
        resolve_gateway,
    )
    from tuomin_gateway.workbuddy_models import (
        MODES,
        PLACEHOLDER_API_KEY,
        build_entries,
        entries_for_app,
        load_models,
        merge_entries,
        mode_from_url,
        retarget_entries,
    )
except ImportError:  # run from a checkout without the package on sys.path
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
    from tuomin_gateway.gateway_discovery import (  # noqa: E402
        DEFAULT_SCAN_RANGE,
        describe_gateways,
        parse_scan_range,
        resolve_gateway,
    )
    from tuomin_gateway.workbuddy_models import (  # noqa: E402
        MODES,
        PLACEHOLDER_API_KEY,
        build_entries,
        entries_for_app,
        load_models,
        merge_entries,
        mode_from_url,
        retarget_entries,
    )

__all__ = [
    "MODES", "PLACEHOLDER_API_KEY", "build_entries", "entries_for_app",
    "load_models", "merge_entries", "mode_from_url", "retarget_entries", "main",
]


def _load_existing(target: Path) -> list:
    return load_models(target)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--app", required=True, help="Tuomin registry 中的 app_id")
    parser.add_argument("--base-url", default=None, help="显式网关地址（省略则自动探测本机在线网关）")
    parser.add_argument("--port", type=int, default=None, help="显式网关端口（等价于 --base-url http://127.0.0.1:<port>）")
    parser.add_argument("--scan", default=DEFAULT_SCAN_RANGE, help=f"自动探测的端口范围，默认 {DEFAULT_SCAN_RANGE}；设空串可关闭扫描")
    parser.add_argument("--timeout", type=float, default=0.4, help="单端口探测超时秒数，默认 0.4")
    parser.add_argument("--api-key", default=PLACEHOLDER_API_KEY, help="上游 provider 的 API key（透传，不落 Tuomin 盘）")
    parser.add_argument("--modes", default="masked,demo,auto", help="逗号分隔：masked,demo,auto 的子集")
    parser.add_argument("--write", action="store_true", help="合并写入 ~/.workbuddy/models.json（自动备份 + 旧端口自愈）")
    parser.add_argument("--list-gateways", action="store_true", help="只列出探测到的网关及其 app，然后退出")
    args = parser.parse_args()

    modes = [m.strip() for m in args.modes.split(",") if m.strip()]
    unknown = [m for m in modes if m not in MODES]
    if unknown:
        print(f"未知模式: {unknown}（可选：{sorted(MODES)}）", file=sys.stderr)
        return 2

    scan_ports = parse_scan_range(args.scan) if args.scan else []
    resolution = resolve_gateway(
        args.app,
        base_url=args.base_url,
        port=args.port,
        scan_ports=scan_ports,
        allow_scan=bool(scan_ports),
        timeout=args.timeout,
    )

    if args.list_gateways:
        print(describe_gateways(resolution.gateways))
        return 0

    if resolution.base_url is None:
        print(
            f"未找到挂载 app {args.app!r} 的在线 Tuomin 网关（已探测端口："
            f"{args.scan or '关闭扫描'}）。\n"
            f"探测结果：\n{describe_gateways(resolution.gateways)}\n"
            "请先启动网关，例如：\n"
            "  TUOMIN_CONFIG=<registry.json> .venv/bin/python -m tuomin_gateway.cli serve --host 127.0.0.1 --port 8767\n"
            "或用 --base-url / --port 显式指定。",
            file=sys.stderr,
        )
        return 3

    base_url = resolution.base_url
    if resolution.verified:
        print(f"[tuomin] 使用网关 {base_url}（{resolution.reason}）", file=sys.stderr)
    else:
        print(
            f"[tuomin] 警告：{resolution.reason}；仍按 {base_url} 生成，"
            "请确认该网关的 registry 已声明此 app。",
            file=sys.stderr,
        )

    entries = build_entries(args.app, base_url, args.api_key, modes)
    if not args.write:
        print(json.dumps(entries, ensure_ascii=False, indent=2))
        print(
            f"\n# 已自动定位网关 {base_url}；把以上条目追加进 ~/.workbuddy/models.json 的顶层 list，"
            "或加 --write 自动合并（会同时把旧端口的同 app 条目改指过来）",
            file=sys.stderr,
        )
        return 0

    import shutil
    import time

    target = Path.home() / ".workbuddy" / "models.json"
    existing = _load_existing(target)
    if target.exists():
        backup = target.with_suffix(f".json.bak-{int(time.time())}")
        shutil.copy2(target, backup)
        print(f"已备份原配置 → {backup}", file=sys.stderr)
    changed = retarget_entries(existing, args.app, base_url)
    merged = merge_entries(existing, entries, app=args.app)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(merged, ensure_ascii=False, indent=2), encoding="utf-8")
    healed = f"，并将 {len(changed)} 条旧端口条目改指到 {base_url}" if changed else ""
    print(f"已写入 {target}（{len(entries)} 条条目，共 {len(merged)} 条{healed}）。重启 WorkBuddy 生效。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
