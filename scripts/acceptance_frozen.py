#!/usr/bin/env python3
"""阶段 10 断网验收：对冻结产物跑全矩阵检查（本机/干净机器通用）。

用法：
    .venv/bin/python scripts/acceptance_frozen.py [dist/full/tuomin-workbench/tuomin-workbench]
    .venv/bin/python scripts/acceptance_frozen.py --lite dist/lite/tuomin-workbench-lite/tuomin-workbench

开源版冻结包不强制离线授权，打开即用；Full 直接运行完整业务矩阵，Lite 因
缺 NER 只验证壳、headless、隔离演示与规则词典路径。

矩阵（对应开发计划阶段 10 验收项）：
 1. 启动与 /readiness（捆绑 NER 模型真实加载、active）
 2. TXT/MD parse + 出站闸门（合成词典实体被脱敏，egress 通过）
 3. AI 回答本地回填（子集 + 重复占位符）
 4. DOCX 原格式脱敏/回填（文本完全往返）
 5. 可信预览 unlock-mapping（口令解包返回原值；审计无原值由测试套件覆盖）
 6. proxy 三档端点（masked/demo/auto，假上游）
 7. WebUI /ui/ 可加载
 8. Keychain 加密映射存储（MACAES1 文件头，非明文）
 9. --mcp-stdio：initialize / tools.list / readiness / redact 协议闭环

环境强制断网语义：HF_HUB_OFFLINE=1 + TRANSFORMERS_OFFLINE=1，且验收过程
不访问任何外网地址（假上游也绑回环）。真正的飞行模式复测在干净机器执行。
"""
from __future__ import annotations

import base64
import argparse
import hashlib
import json
import os
from pathlib import Path
import signal
import secrets
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

for _console_stream in (sys.stdout, sys.stderr):
    _reconfigure = getattr(_console_stream, "reconfigure", None)
    if callable(_reconfigure):
        _reconfigure(encoding="utf-8", errors="replace")


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_BINARY = (
    ROOT / "dist" / "Tuomin" / "Tuomin.exe"
    if os.name == "nt"
    else ROOT / "dist" / "full" / "tuomin-workbench" / "tuomin-workbench"
)
BINARY = DEFAULT_BINARY
MAPPING_MAGIC = b"DPAPI1" if os.name == "nt" else b"MACAES1"
LITE = False
FORMAL_MATRIX_RAN = False
PHONE = "13900001111"
ORG = "示例建设单位A"
PASS = secrets.token_urlsafe(24)
ADMIN = secrets.token_urlsafe(32)
CAPABILITY = secrets.token_urlsafe(32)
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))

failures: list[str] = []
results: list[dict] = []


class _SkipFormalMatrix(Exception):
    pass


def check(name: str, ok: bool, detail: str = "") -> None:
    name = name.replace("MACAES1", MAPPING_MAGIC.decode("ascii"))
    results.append({"name": name, "passed": bool(ok), "detail": detail})
    print(f"  [{'✓' if ok else '✗'}] {name}" + (f" — {detail}" if detail else ""))
    if not ok:
        failures.append(name + (f" ({detail})" if detail else ""))


def skip(name: str, detail: str) -> None:
    results.append({"name": name, "passed": None, "skipped": True, "detail": detail})
    print(f"  [跳过] {name} — {detail}")


def post(url: str, payload: dict | None = None, raw: bytes | None = None, headers: dict | None = None) -> tuple[int, dict]:
    h = {"x-tuomin-admin-token": ADMIN, "X-Requested-With": "tuomin-ui", **(headers or {})}
    data = raw if raw is not None else json.dumps(payload).encode()
    if raw is None:
        h["content-type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=h)
    try:
        with OPENER.open(req, timeout=120) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


def get(url: str) -> tuple[int, str]:
    req = urllib.request.Request(url, headers={"x-tuomin-admin-token": ADMIN})
    try:
        with OPENER.open(req, timeout=30) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")


class _FakeUpstream(BaseHTTPRequestHandler):
    def do_POST(self):
        length = int(self.headers.get("content-length", 0))
        body = json.loads(self.rfile.read(length) or b"{}")
        text = ""
        for msg in body.get("messages", []):
            content = msg.get("content")
            if isinstance(content, str):
                text += content
        canned = {"choices": [{"message": {"role": "assistant", "content": f"已记录 {text[:20]} <CONTACT_001>"}}]}
        if body.get("stream"):
            frames = (
                'data: {"choices":[{"index":0,"delta":{"reasoning_content":"看到了 <CONTACT_001>"}}]}\n\n'
                'data: {"choices":[{"index":0,"delta":{"content":"已记录 <CONTACT_001>"}}]}\n\n'
                "data: [DONE]\n\n"
            ).encode()
            self.send_response(200)
            self.send_header("content-type", "text/event-stream")
            self.end_headers()
            self.wfile.write(frames)
        else:
            self.send_response(200)
            self.send_header("content-type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(canned).encode())

    def log_message(self, *args):
        pass


def main() -> int:
    global ADMIN, BINARY, LITE, FORMAL_MATRIX_RAN
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("binary", nargs="?", type=Path)
    parser.add_argument("--lite", action="store_true", help="验证不含 NER 的 Lite 产物")
    parser.add_argument("--public-lite", action="store_true", help="验证公开 Lite 规则与词典正式业务矩阵")
    parser.add_argument("--skip-desktop", action="store_true", help="仅验证无界面服务与业务，明确跳过原生窗口/托盘")
    args = parser.parse_args()
    LITE = args.public_lite or args.lite or os.environ.get("TUOMIN_ACCEPTANCE_LITE") == "1"
    default_binary = (ROOT / "dist" / "Tuomin-Lite" / "Tuomin.exe" if os.name == "nt" else ROOT / "dist" / "lite" / "tuomin-workbench-lite" / "tuomin-workbench") if LITE else BINARY
    BINARY = (args.binary or default_binary).resolve()
    if not BINARY.exists():
        print(f"找不到产物: {BINARY}")
        return 1
    acceptance_root = os.environ.get("TUOMIN_ACCEPTANCE_ROOT")
    if acceptance_root:
        Path(acceptance_root).mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix="tuomin-accept-", dir=acceptance_root))
    data_dir = work / "data"
    data_dir.mkdir(mode=0o700)
    dictionary = data_dir / "dict.json"
    dictionary.write_text(
        json.dumps([{"canonical_value": ORG, "aliases": [], "label": "ORG", "status": "active"}], ensure_ascii=False),
        encoding="utf-8",
    )
    registry = data_dir / "apps.json"
    registry.write_text(json.dumps({"apps": {
        "accept": {
            "profile": "file_workbench_rules" if args.public_lite else "kb",
            "dictionary": str(dictionary),
            "capabilities": ["detect", "redact", "namespace"],
            "capability_tokens": {k: "sha256:" + hashlib.sha256(CAPABILITY.encode()).hexdigest() for k in ("detect", "redact", "namespace")},
            "mapping_scopes": ["document", "namespace"],
            "allow_auto_refill": True,
            "upstream_model": "accept-model",
        }
    }}), encoding="utf-8")

    upstream = ThreadingHTTPServer(("127.0.0.1", 0), _FakeUpstream)
    threading.Thread(target=upstream.serve_forever, daemon=True).start()
    upstream_port = upstream.server_address[1]

    keychain_service = "com.tuomin.acceptance." + work.name
    home = work / 'home'
    home.mkdir()
    for folder in (home / 'AppData' / 'Roaming', home / 'AppData' / 'Local'):
        folder.mkdir(parents=True)
    env = {
        **{k: v for k, v in os.environ.items() if not k.startswith("TUOMIN_")},
        "TUOMIN_CONFIG": str(registry),
        "TUOMIN_DATA_DIR": str(data_dir),
        "TUOMIN_KEYCHAIN_SERVICE": keychain_service,
        "TUOMIN_OPEN_BROWSER": "0",
        "TUOMIN_UPSTREAM_ALLOWLIST": "127.0.0.1",
        "TUOMIN_UPSTREAM_OPENAI": f"http://127.0.0.1:{upstream_port}/v1/chat/completions",
        "HF_HUB_OFFLINE": "1",
        "TRANSFORMERS_OFFLINE": "1",
        "PYTHONNOUSERSITE": "1",
        "PYTHONUTF8": "1",
        "PYTHONIOENCODING": "utf-8",
        "TUOMIN_NO_BROWSER": "1",
        "HOME": str(home), "USERPROFILE": str(home),
        "APPDATA": str(home / 'AppData' / 'Roaming'), "LOCALAPPDATA": str(home / 'AppData' / 'Local'),
    }
    # Security.framework resolves the default login keychain from HOME.
    # Only the formal service needs it; desktop/client probes keep the synthetic
    # home. The service writes a unique temporary keychain item deleted below.
    formal_env = dict(env)
    if sys.platform == "darwin":
        formal_env["HOME"] = str(Path.home())
    print(f"[1/9] 启动冻结产物: {BINARY}")
    # 每轮使用独立测试条目；不得删除或读取日常使用的映射加密密钥。
    print(f"  隔离验收目录: {work}")
    log_handle = (work / "service.log").open("w", encoding="utf-8")
    proc = subprocess.Popen(
        [str(BINARY), "--headless", "--shutdown-on-stdin-close"], env=formal_env,
        stdin=subprocess.PIPE,
        stdout=log_handle, stderr=subprocess.STDOUT, text=True,
        encoding="utf-8", errors="replace",
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    url = None
    try:
        deadline = time.time() + 120
        while time.time() < deadline:
            url_file = data_dir / "instance.url"
            if url_file.exists():
                url = url_file.read_text(encoding="utf-8").strip()
                url = url.removesuffix("/ui/").removesuffix("/")
                break
            if proc.poll() is not None:
                print("产物提前退出；未输出服务日志以保护凭证")
                return 1
            time.sleep(0.5)
        if not url:
            print("启动超时")
            return 1
        token_path = data_dir / "admin-token"
        original_token_file = token_path.read_bytes()
        ADMIN = next(line.strip() for line in token_path.read_text(encoding="utf-8").splitlines() if line.strip() and not line.startswith("#"))
        from acceptance_shell import owner_private
        check("Token 文件仅当前用户可访问", owner_private(token_path))
        print(f"  实例 URL: {url}")

        # instance.url 先于 uvicorn 监听写入；冻结应用带 torch 冷启动可达
        # 数十秒，必须先等端口真正接受连接再跑检查。
        health_deadline = time.time() + 240
        healthy = False
        while time.time() < health_deadline:
            try:
                with OPENER.open(url + "/healthz", timeout=2) as resp:
                    if resp.status == 200:
                        healthy = True
                        break
            except OSError:
                pass
            if proc.poll() is not None:
                print("产物启动后退出；未输出服务日志以保护凭证")
                return 1
            time.sleep(1)
        if not healthy:
            print("健康检查超时")
            return 1
        print("  健康检查通过（端口已监听）")
        check("--headless 浏览器回退可启动网关", True)
        if not LITE:
            code, agent_check = post(url + '/admin/agents/selftest', {})
            check('随包 MCP 命令通过智能体四步自检', code == 200 and agent_check.get('ok') is True)
            check('智能体凭证文件仅当前用户可访问', owner_private(data_dir / 'agent-tokens.json'))
            code, agent_status = get_json(url + '/admin/agents')
            command = (agent_status.get('mcp') or {}).get('command', '')
            check('Windows MCP 使用随包 CLI', os.name != 'nt' or Path(command).name == 'Tuomin-cli.exe')
            check('随包 Windows 离线手册', os.name != 'nt' or (BINARY.parent / 'Tuomin-Windows使用手册.html').is_file())

        # These checks must run even when no formal acceptance license exists.
        code, readiness = get_json(url + "/readiness")
        ner = readiness.get("detectors", {}).get("ner", {})
        if args.public_lite:
            mode_code, mode = get_json(url + "/api/v1/runtime-mode")
            check("公开 Lite 构建标记生效", mode_code == 200 and mode.get("public_lite") is True and mode.get("demo") is False)
        if LITE:
            check("Lite 明确不包含 NER 运行库", ner.get("installed") is False and ner.get("active") is False)
            check("Lite 严格档缺 NER 时未就绪", code == 503 and readiness.get("status") == "not_ready")
        else:
            check("readiness ready", code == 200 and readiness.get("status") == "ready")
            check("捆绑 NER active（断网真实加载）", ner.get("active") is True and ner.get("loadable") is True)
        code, ui = get(url + "/ui/")
        check("/ui/ 200 且含脱敏入口", code == 200 and "文件脱敏" in ui, str(code))
        from acceptance_shell import run_shell_checks
        run_shell_checks(BINARY, env=env, formal_data_dir=data_dir, formal_origin=url,
                         work=work, check=check, lite=LITE and not args.public_lite, desktop=not args.skip_desktop, skip=skip)
        if LITE and not args.public_lite:
            skip("Lite DOCX/XLSX 成功往返", "缺 NER 时验证导出阻断；未运行成功导出矩阵")

        # 开源版不要求授权：冻结包打开即用，业务矩阵直接运行（Lite 缺 NER 除外）。
        code, status = get_json(url + "/api/v1/license/status")
        check("冻结包不强制离线授权", code == 200 and status.get("enforcement") == "disabled", f"http={code}")
        if LITE and not args.public_lite:
            skip("正式业务矩阵", "Lite 产物不运行 Full 业务矩阵")
            raise _SkipFormalMatrix()

        FORMAL_MATRIX_RAN = True

        print("[3/9] TXT parse + 出站闸门 + 回答回填")
        code, parsed = post(url + "/api/v1/documents/parse?filename=a.txt&app_id=accept", raw=f"发包方：{ORG}，联系电话{PHONE}。".encode())
        check("TXT parse ok", code == 200, str(code))
        egress = parsed.get("egress", {})
        check("egress 通过", egress.get("egress_allowed") is True)
        check("合成词典实体脱敏", ORG not in parsed.get("redacted_markdown", "") and "<ORG_" in parsed.get("redacted_markdown", ""))
        code, pkg = post(url + f"/api/v1/documents/{parsed['task_id']}/package", {"passphrase": PASS})
        package = pkg.get("package", {})
        placeholders = sorted(package.get("binding", {}).get("expected_counts", {}))
        answer = f"结论：拨 {placeholders[-1]}，再拨 {placeholders[-1]}。"
        code, refilled = post(url + "/api/v1/documents/refill-answer", {"answer_markdown": answer, "package": package, "passphrase": PASS})
        check("AI 回答回填（子集+重复）", code == 200 and refilled.get("status") == "ok", str(code))

        print("[4/9] DOCX 原格式脱敏/回填")
        docx_bytes = _make_docx(work / "sample.docx", f"发包方：{ORG}，联系电话{PHONE}。")
        raw = docx_bytes.read_bytes()
        code, red = post(url + "/api/v1/documents/redact-docx?filename=sample.docx&app_id=accept", raw=raw)
        check("redact-docx ok", code == 200, str(code))
        if code == 200:
            check("DOCX egress 通过", red["egress"]["egress_allowed"] is True)
            code, pkg2 = post(url + f"/api/v1/documents/{red['task_id']}/package", {"passphrase": PASS})
            code, rfd = post(url + "/api/v1/documents/refill-docx", {
                "package": pkg2["package"], "passphrase": PASS,
                "redacted_docx_b64": red["redacted_docx_b64"],
            })
            restored_ok = code == 200 and _docx_text(base64.b64decode(rfd.get("restored_docx_b64", ""))) == _docx_text(raw)
            check("DOCX 文本完全往返", restored_ok, str(code))

        ole_docx = _make_docx(work / "embedded.docx", f"发包方：{ORG}", embedded_ole=True)
        code, ole_result = post(url + "/api/v1/documents/redact-docx?filename=embedded.docx&app_id=accept", raw=ole_docx.read_bytes())
        check("DOCX 内嵌 OLE 默认阻断", code == 422 and ole_result.get("error", {}).get("code") == "ole_object_present", str(code))

        print("[5/9] 可信预览 unlock-mapping")
        code, unlocked = post(url + "/api/v1/documents/unlock-mapping", {"package": package, "passphrase": PASS})
        check("unlock-mapping 返回原值", code == 200 and any(e["original_value"] == PHONE for e in unlocked.get("entries", [])), str(code))

        print("[6/9] proxy 三档端点")
        body = {"model": "x", "messages": [{"role": "user", "content": f"电话{PHONE}"}]}
        code, masked = post(url + "/apps/accept/v1/chat/completions", body)
        check("masked 档返回占位符", code == 200 and "<CONTACT_001>" in json.dumps(masked, ensure_ascii=False) and PHONE not in json.dumps(masked, ensure_ascii=False))
        # demo/auto 为流式（SSE）与 JSON 两种形态，分开取值
        req = urllib.request.Request(
            url + "/apps/accept/demo/v1/chat/completions",
            data=json.dumps({**body, "stream": True}).encode(),
            headers={"content-type": "application/json", "x-tuomin-admin-token": ADMIN, "X-Requested-With": "tuomin-ui"},
        )
        try:
            with OPENER.open(req, timeout=120) as resp:
                demo_code, demo_text = resp.status, resp.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            demo_code, demo_text = exc.code, exc.read().decode("utf-8", "replace")
        check("demo 档流式：思维链占位符+正文回填",
              demo_code == 200 and "<CONTACT_001>" in demo_text and PHONE in demo_text,
              str(demo_code))
        code, auto = post(url + "/apps/accept/auto/v1/chat/completions", body)
        auto_text = json.dumps(auto, ensure_ascii=False)
        check("auto 档透明回填", code == 200 and PHONE in auto_text and "<CONTACT_001>" not in auto_text, str(code))

        print("[7/9] WebUI")
        code, ui = get(url + "/ui/")
        check("/ui/ 200 且含脱敏入口", code == 200 and "文件脱敏" in ui, str(code))

        print("[8/9] 加密映射存储")
        grant_files = sorted((data_dir / "mappings").rglob("*.mapping.enc")) if (data_dir / "mappings").exists() else []  # task mappings live under mappings/documents/
        encrypted = grant_files and grant_files[0].read_bytes().startswith(MAPPING_MAGIC)
        check("mapping 文件 MACAES1 加密", bool(encrypted), grant_files[0].name if grant_files else "无文件")

        print("[9/9] --mcp-stdio 协议闭环")
        mcp_env = {**env, "TUOMIN_GATEWAY_URL": url,
                   "TUOMIN_AGENT_REDACT_TOKEN": CAPABILITY, "TUOMIN_AGENT_NAMESPACE_TOKEN": CAPABILITY}
        messages = "\n".join([
            '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}',
            '{"jsonrpc":"2.0","id":2,"method":"tools/list"}',
            json.dumps({"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "tuomin_readiness", "arguments": {"app_id": "accept"}}}),
            json.dumps({"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "tuomin_redact_text", "arguments": {"app_id": "accept", "text": f"发包方：{ORG}，电话{PHONE}"}}}),
        ]) + "\n"
        mcp = subprocess.run(
            [str(BINARY), "--mcp-stdio"], env=mcp_env,
            input=messages,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=60,
        )
        replies = [json.loads(line) for line in mcp.stdout.splitlines() if line.strip()]
        tools_ok = any(r.get("id") == 2 and {t["name"] for t in r.get("result", {}).get("tools", [])} >= {"tuomin_readiness", "tuomin_redact_text"} for r in replies)
        check("MCP initialize + tools/list", len(replies) >= 2 and tools_ok, f"exit={mcp.returncode}")
        for rid, name in ((3, "readiness"), (4, "redact")):
            reply = next((r for r in replies if r.get("id") == rid), {})
            result = reply.get("result", {})
            payload = json.loads(result.get("content", [{}])[0].get("text", "{}"))
            ok = not result.get("isError") and payload.get("status") == "ok"
            ok = ok and (payload.get("ready") is True if rid == 3 else payload.get("egress_allowed") is True and PHONE not in json.dumps(payload) and ORG not in json.dumps(payload, ensure_ascii=False))
            check("MCP " + name + " 实际调用", ok)

        # 对同一最终二进制验证第一批凭据规则，以及重启后的历史恢复。
        pem = "-----BEGIN PRIVATE KEY-----\nQUJDREVGR0hJSktMTU5PUFFSU1RVVldYWVo=\n-----END PRIVATE KEY-----"
        code, secret = post(url + "/api/v1/documents/parse?filename=key.txt&app_id=accept", raw=pem.encode())
        masked = secret.get("redacted_markdown", "")
        check("完整 PEM 整块脱敏", code == 200 and "<CREDENTIAL_" in masked and "PRIVATE KEY" not in masked and "QUJD" not in masked, str(code))
        code, rejected = post(url + "/api/v1/documents/parse?filename=bad.txt&app_id=accept", raw=pem.split("-----END")[0].encode())
        check("残缺 PEM 阻断", code == 400 and not rejected.get("egress", {}).get("egress_allowed"), str(code))
        # 重启服务，验证历史数据恢复与 Token 复用不依赖进程内状态。
        from acceptance_shell import _stop
        check("重启前服务正常退出", _stop(proc) and proc.returncode == 0)
        proc = subprocess.Popen([str(BINARY), "--headless", "--shutdown-on-stdin-close"], env=formal_env,
                                stdin=subprocess.PIPE, stdout=log_handle, stderr=subprocess.STDOUT,
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        deadline = time.time() + 120
        while time.time() < deadline:
            try:
                url = (data_dir / "instance.url").read_text(encoding="utf-8").strip().removesuffix("/ui/").removesuffix("/")
                if get(url + "/healthz")[0] == 200:
                    break
            except (OSError, ValueError):
                pass
            time.sleep(0.5)
        code, reparsed = post(url + "/api/v1/documents/parse?filename=b.txt&app_id=accept", raw=f"发包方：{ORG}。".encode())
        check("重启后无需授权即可新建任务", code == 200 and "<ORG_" in reparsed.get("redacted_markdown", ""), str(code))
        code, unlocked = post(url + "/api/v1/documents/unlock-mapping", {"package": package, "passphrase": PASS})
        check("重启后恢复包仍可解包", code == 200 and any(e["original_value"] == PHONE for e in unlocked.get("entries", [])), str(code))
        code, restored = post(url + "/api/v1/documents/refill-answer", {"answer_markdown": answer, "package": package, "passphrase": PASS})
        check("重启后回答仍可回填", code == 200 and restored.get("status") == "ok", str(code))
        check("重启复用 Token", token_path.read_bytes() == original_token_file)
        from acceptance_shell import log_contains_values
        check("日志未泄露 Token", not log_contains_values(work / "service.log", [ADMIN]))
    except _SkipFormalMatrix:
        pass
    finally:
        from acceptance_shell import _stop
        check("--headless 服务正常退出", _stop(proc) and proc.returncode == 0)
        log_handle.close()
        check("--headless 退出清理正式实例锁", not (data_dir / "tuomin.lock").exists())
        from acceptance_shell import log_contains_values
        check("正式验收日志不含凭证和合成原值", not log_contains_values(
            work / "service.log", (ADMIN, CAPABILITY, PASS, ORG, PHONE)))
        upstream.shutdown()
        upstream.server_close()
        if sys.platform == "darwin":
            subprocess.run(["security", "delete-generic-password", "-s", keychain_service], capture_output=True)

    print()
    if failures:
        print(f"✗ {len(failures)} 项未过:")
        for item in failures:
            print("  -", item)
        return 1
    print("✓ 已执行检查通过" + ("（正式业务矩阵已运行）" if FORMAL_MATRIX_RAN else "（正式业务矩阵未运行）"))
    return 0


def get_json(url: str) -> tuple[int, dict]:
    req = urllib.request.Request(url, headers={"x-tuomin-admin-token": ADMIN})
    try:
        with OPENER.open(req, timeout=120) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as error:
        return error.code, json.loads(error.read())


def _make_docx(path: Path, text: str, *, embedded_ole: bool = False) -> Path:
    import io
    import zipfile
    from xml.sax.saxutils import escape

    w = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
    ole = ('<w:p><w:r><w:object><o:OLEObject xmlns:o="urn:schemas-microsoft-com:office:office" '
           'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships" '
           'r:id="rId9"/></w:object></w:r></w:p>') if embedded_ole else ""
    document = (f'<?xml version="1.0" encoding="UTF-8"?>'
                f'<w:document xmlns:w="{w}"><w:body><w:p><w:r><w:t>{escape(text)}</w:t></w:r></w:p>{ole}</w:body></w:document>')
    override = ('<Override PartName="/word/embeddings/oleObject1.bin" '
                'ContentType="application/vnd.openxmlformats-officedocument.oleObject"/>') if embedded_ole else ""
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", f'<?xml version="1.0"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">{override}</Types>')
        archive.writestr("_rels/.rels", b'<?xml version="1.0"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"/>')
        archive.writestr("word/document.xml", document.encode())
        if embedded_ole:
            archive.writestr("word/embeddings/oleObject1.bin", b"SYNTHETIC-OLE")
            archive.writestr("word/_rels/document.xml.rels", '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId9" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/oleObject" Target="embeddings/oleObject1.bin"/></Relationships>')
    return path


def _docx_text(data: bytes) -> str:
    import io
    import zipfile
    from xml.etree import ElementTree as ET

    w = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}t"
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        root = ET.fromstring(archive.read("word/document.xml"))
    return "".join(node.text or "" for node in root.iter(w))


if __name__ == "__main__":
    try:
        rc = main()
    except Exception:
        # Local responses may contain originals or credentials; never print
        # exception details or a traceback into the acceptance report.
        check("验收流程完整执行", False, "进程、响应或合成工件检查失败")
        rc = 1
    if report_path := os.environ.get("TUOMIN_ACCEPTANCE_REPORT"):
        Path(report_path).write_text(json.dumps({
            "exit_code": rc,
            "full_matrix": FORMAL_MATRIX_RAN and rc == 0,
            "variant": "lite" if LITE else "full",
            "binary_sha256": hashlib.sha256(BINARY.read_bytes()).hexdigest() if BINARY.is_file() else None,
            "checks": results,
        }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    sys.exit(rc)
