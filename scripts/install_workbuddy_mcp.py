#!/usr/bin/env python3
"""Install Tuomin as a WorkBuddy MCP connector (``~/.workbuddy/mcp.json``).

WorkBuddy 5.5.6 has no plugin entry in the sidebar, but it does support local
MCP connectors. This writes Tuomin's STDIO MCP adapter into the user MCP file
with absolute paths, so the agent gains the three low-privilege Tuomin tools
(``tuomin_readiness`` / ``tuomin_redact_text`` / ``tuomin_redact_values``).

Usage:
    python scripts/install_workbuddy_mcp.py --app wb_test \
        --redact-token <tok> --namespace-token <tok>
    python scripts/install_workbuddy_mcp.py --app wb_test --dry-run
    python scripts/install_workbuddy_mcp.py --uninstall
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

try:
    from tuomin_gateway.gateway_discovery import resolve_gateway_url
    from tuomin_gateway.workbuddy_mcp import (
        DEFAULT_SERVER_NAME,
        build_server_entry,
        default_mcp_path,
        install_mcp_server,
        uninstall_mcp_server,
    )
except ImportError:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
    from tuomin_gateway.gateway_discovery import resolve_gateway_url  # noqa: E402
    from tuomin_gateway.workbuddy_mcp import (  # noqa: E402
        DEFAULT_SERVER_NAME,
        build_server_entry,
        default_mcp_path,
        install_mcp_server,
        uninstall_mcp_server,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--app", default=None, help="用于挑选网关的 app（建议填，确保选到挂载该 app 的端口）")
    parser.add_argument("--base-url", default=None, help="显式网关地址（省略则自动探测）")
    parser.add_argument("--python", default=None, help="运行 MCP server 的解释器（默认当前解释器）")
    parser.add_argument("--redact-token", default="", help="tuomin_readiness/redact_text 能力令牌")
    parser.add_argument("--namespace-token", default="", help="tuomin_redact_values 能力令牌")
    parser.add_argument("--server-name", default=DEFAULT_SERVER_NAME, help=f"mcpServers 键名，默认 {DEFAULT_SERVER_NAME}")
    parser.add_argument("--mcp-file", default=None, help="覆盖 mcp.json 路径（默认 ~/.workbuddy/mcp.json）")
    parser.add_argument("--dry-run", action="store_true", help="只打印将写入的条目")
    parser.add_argument("--uninstall", action="store_true", help="移除该 MCP server 条目")
    args = parser.parse_args()

    mcp_file = Path(args.mcp_file).expanduser() if args.mcp_file else default_mcp_path()

    if args.uninstall:
        result = uninstall_mcp_server(mcp_file=mcp_file, server_name=args.server_name)
        print(("已移除" if result["removed"] else "未找到") + f" {result['server_name']} → {result['mcp_file']}")
        return 0

    base_url = args.base_url
    if not base_url:
        resolution = resolve_gateway_url(args.app or None)
        base_url = resolution.base_url
        if not base_url:
            print(
                f"未找到在线 Tuomin 网关（app={args.app!r}）。请先启动网关，或用 --base-url 指定。",
                file=sys.stderr,
            )
            return 3
        print(f"[tuomin] 使用网关 {base_url}（{resolution.reason}）", file=sys.stderr)

    if args.dry_run:
        print(json.dumps({
            "mcp_file": str(mcp_file),
            "mcpServers": {args.server_name: build_server_entry(
                python=args.python, gateway_url=base_url,
                redact_token=args.redact_token, namespace_token=args.namespace_token,
                app_id=args.app or "",
            )},
        }, ensure_ascii=False, indent=2))
        return 0

    result = install_mcp_server(
        gateway_url=base_url,
        mcp_file=mcp_file,
        server_name=args.server_name,
        python=args.python,
        redact_token=args.redact_token,
        namespace_token=args.namespace_token,
        app_id=args.app or "",
    )
    if result["backup"]:
        print(f"已备份原配置 → {result['backup']}", file=sys.stderr)
    print(f"已写入 {result['mcp_file']}（server={result['server_name']}，共 {result['servers_total']} 个 MCP server）。")
    if not (result["has_redact_token"] and result["has_namespace_token"]):
        print(
            "提示：未提供完整能力令牌时，Tuomin 工具会因鉴权失败而不可用。"
            "用 scripts/generate_capability_token.py 生成并写进对应 app 的 capability_tokens 后再带上。",
            file=sys.stderr,
        )
    print("重启 WorkBuddy 或重新打开会话生效。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
