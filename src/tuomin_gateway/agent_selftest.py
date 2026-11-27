"""Drive the real MCP command the way a client would, step by step.

Used by the 工具接入 page's 自检 button: it proves that the exact command
written into client configs starts, speaks MCP, sees a ready ``agent`` app and
redacts a synthetic sample. No user text is involved and nothing is persisted
as an agent session (session mode is off for the check).
"""
from __future__ import annotations

import json
import os
import queue
import subprocess
import threading
import time

SAMPLE = "联系人张三，电话13800138000，请核对付款安排。"
EXPECTED_TOOLS = {"tuomin_readiness", "tuomin_redact_text", "tuomin_redact_values"}


def _reader(stream, sink: queue.Queue) -> None:
    try:
        for line in iter(stream.readline, ""):
            sink.put(line)
    except (UnicodeError, OSError):
        pass  # Invalid transport becomes a failed self-test, never a thread traceback.
    finally:
        sink.put(None)


def _tool_payload(response: dict) -> tuple[dict, bool]:
    result = response.get("result") or {}
    content = (result.get("content") or [{}])[0]
    try:
        payload = json.loads(content.get("text") or "{}")
    except ValueError:
        payload = {}
    return payload, bool(result.get("isError"))


def run_selftest(command: str, args: list[str], env: dict[str, str] | None = None, *,
                 gateway_url: str | None = None, timeout: float = 30.0) -> dict:
    steps: list[dict] = []
    child_env = {**os.environ, **(env or {}), "TUOMIN_AGENT_SESSION": "0", "PYTHONUNBUFFERED": "1"}
    if gateway_url:
        child_env["TUOMIN_GATEWAY_URL"] = gateway_url
    started = time.monotonic()

    def step(name: str, ok: bool, detail: str = "") -> bool:
        steps.append({"step": name, "ok": ok, "detail": detail, "ms": int((time.monotonic() - started) * 1000)})
        return ok

    try:
        process = subprocess.Popen([command, *args], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                   stderr=subprocess.DEVNULL, text=True, encoding="utf-8", env=child_env,
                                   creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    except OSError as exc:
        step("启动 MCP 命令", False, f"无法启动：{exc.strerror or exc}")
        return {"ok": False, "steps": steps}
    lines: queue.Queue = queue.Queue()
    threading.Thread(target=_reader, args=(process.stdout, lines), daemon=True).start()
    next_id = [0]

    def request(method: str, params: dict | None = None, *, notify: bool = False) -> dict | None:
        message = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            message["params"] = params
        if not notify:
            next_id[0] += 1
            message["id"] = next_id[0]
        process.stdin.write(json.dumps(message, ensure_ascii=False) + "\n")
        process.stdin.flush()
        if notify:
            return None
        deadline = started + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError
            line = lines.get(timeout=remaining)
            if line is None:
                raise EOFError
            try:
                response = json.loads(line)
            except ValueError:
                continue
            if response.get("id") == message["id"]:
                return response

    try:
        initialized = request("initialize", {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "tuomin-selftest", "version": "1"}})
        server = ((initialized or {}).get("result") or {}).get("serverInfo", {}).get("name", "")
        if not step("MCP 握手", bool(server), server or "无响应"):
            return {"ok": False, "steps": steps}
        request("notifications/initialized", notify=True)
        tools = {tool.get("name") for tool in (((request("tools/list") or {}).get("result") or {}).get("tools") or [])}
        if not step("工具列表", EXPECTED_TOOLS <= tools, "、".join(sorted(tools)) or "空"):
            return {"ok": False, "steps": steps}
        readiness, error = _tool_payload(request("tools/call", {"name": "tuomin_readiness", "arguments": {}}) or {})
        ready = readiness.get("ready") is True and not error
        detail = "就绪" if ready else ((readiness.get("error") or {}).get("code") or "未就绪（Tuomin 是否在运行？）")
        if not step("智能体应用就绪", ready, detail):
            return {"ok": False, "steps": steps}
        redacted, error = _tool_payload(request("tools/call", {"name": "tuomin_redact_text", "arguments": {"text": SAMPLE}}) or {})
        masked = redacted.get("masked_text") or ""
        ok = redacted.get("egress_allowed") is True and "13800138000" not in masked and "<" in masked and not error
        step("合成样例脱敏", ok, masked if ok else ((redacted.get("error") or {}).get("code") or "未通过"))
        return {"ok": all(item["ok"] for item in steps), "steps": steps}
    except (TimeoutError, queue.Empty):
        step("等待响应", False, f"{int(timeout)} 秒内无响应")
        return {"ok": False, "steps": steps}
    except (EOFError, OSError):
        step("等待响应", False, "MCP 进程提前退出")
        return {"ok": False, "steps": steps}
    finally:
        try:
            process.stdin.close()
        except OSError:
            pass
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
        process.stdout.close()
