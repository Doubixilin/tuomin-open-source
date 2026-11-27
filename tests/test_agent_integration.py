"""One-click AI client integration: agent app, client configs, sessions, self-test."""
from __future__ import annotations

import base64
import importlib.util
import json
import os
import stat
import sys
import zipfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from tuomin_gateway import agent_clients
from tuomin_gateway.agent_selftest import run_selftest
from tuomin_gateway.agent_setup import (
    AGENT_APP_ID,
    ensure_agent_app,
    load_agent_tokens,
    token_hashes,
    tokens_path,
)
from tuomin_gateway.mcp_stdio import AgentGateway
from tuomin_gateway.service.app import create_app
from tuomin_gateway.service.registry import AppRegistry
from tuomin_gateway.store import MappingStore

ROOT = Path(__file__).resolve().parents[1]
ORG = "示例建设单位A"
ADMIN = "admin-token-for-tests-0123456789"


@pytest.fixture
def home(tmp_path, monkeypatch):
    target = tmp_path / "home"
    target.mkdir()
    monkeypatch.setenv("HOME", str(target))
    monkeypatch.setenv("USERPROFILE", str(target))
    monkeypatch.setenv("APPDATA", str(target / 'AppData' / 'Roaming'))
    monkeypatch.setenv("LOCALAPPDATA", str(target / 'AppData' / 'Local'))
    monkeypatch.setenv("TUOMIN_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.delenv("TUOMIN_WORKBUDDY_MCP", raising=False)
    monkeypatch.setattr(agent_clients, "find_claude_cli", lambda: None)
    return target


# --- agent app provisioning ----------------------------------------------------

def test_ensure_agent_app_creates_private_tokens_and_reuses_workbench_dictionary(tmp_path):
    config = tmp_path / "apps.json"
    config.write_text(json.dumps({"apps": {"workbench": {"profile": "file_workbench", "dictionary": str(tmp_path / "wb.json")}}}), encoding="utf-8")
    assert ensure_agent_app(config, tmp_path) is True
    tokens = load_agent_tokens(tmp_path)
    assert set(tokens) == {"redact", "namespace"}
    from tests.permission_assertions import assert_owner_private
    assert_owner_private(tokens_path(tmp_path), is_dir=False)
    entry = json.loads(config.read_text())["apps"][AGENT_APP_ID]
    assert entry["capability_tokens"] == token_hashes(tokens)
    assert "trusted_refill" not in entry["capabilities"] and entry["refill_contracts"] == ["none"]
    assert entry["dictionary"] == str(tmp_path / "wb.json") and "namespace" in entry["mapping_scopes"]
    assert tokens["redact"] not in config.read_text()  # only verifiers in the registry
    assert ensure_agent_app(config, tmp_path) is False  # idempotent


def test_ensure_agent_app_repairs_verifiers_and_never_keeps_trusted_refill(tmp_path):
    config = tmp_path / "apps.json"
    config.write_text(json.dumps({"apps": {AGENT_APP_ID: {"profile": "strict", "capabilities": ["redact", "trusted_refill"], "capability_tokens": {"redact": "sha256:" + "0" * 64}}}}), encoding="utf-8")
    assert ensure_agent_app(config, tmp_path) is True
    entry = json.loads(config.read_text())["apps"][AGENT_APP_ID]
    assert entry["profile"] == "strict"  # user choice kept
    assert "trusted_refill" not in entry["capabilities"] and "trusted_refill" not in entry["capability_tokens"]
    assert entry["capability_tokens"] == token_hashes(load_agent_tokens(tmp_path))


# --- client configuration files --------------------------------------------------

def test_json_clients_merge_backup_and_uninstall_only_our_entry(home):
    cursor = home / ".cursor" / "mcp.json"
    cursor.parent.mkdir()
    cursor.write_text(json.dumps({"mcpServers": {"other": {"command": "x"}}, "keep": 1}), encoding="utf-8")
    result = agent_clients.install("cursor")
    data = json.loads(cursor.read_text())
    assert data["keep"] == 1 and data["mcpServers"]["other"] == {"command": "x"}
    command, args, _ = agent_clients.mcp_command()
    assert data["mcpServers"]["tuomin"]["command"] == command and data["mcpServers"]["tuomin"]["args"] == args
    assert "env" not in data["mcpServers"]["tuomin"] or "TOKEN" not in json.dumps(data["mcpServers"]["tuomin"]["env"])
    assert result["backup"] and Path(result["backup"]).exists()
    assert result["status"]["installed"] and result["status"]["current"]
    agent_clients.install("cursor")  # idempotent
    assert list(json.loads(cursor.read_text())["mcpServers"]) == ["other", "tuomin"]
    removed = agent_clients.uninstall("cursor")
    assert removed["removed"] and json.loads(cursor.read_text())["mcpServers"] == {"other": {"command": "x"}}


def test_workbuddy_entry_declares_stdio_and_installs_marked_skill(home):
    result = agent_clients.install("workbuddy")
    entry = json.loads((home / ".workbuddy" / "mcp.json").read_text())["mcpServers"]["tuomin"]
    assert entry["type"] == "stdio"
    skill = home / ".workbuddy" / "skills" / "tuomin-guard"
    assert result["skill_written"] and (skill / "SKILL.md").read_text(encoding="utf-8").startswith("---\nname: tuomin-guard")
    assert (skill / agent_clients.SKILL_MARKER).exists()
    agent_clients.uninstall("workbuddy")
    assert not skill.exists()


def test_user_skill_with_same_name_is_never_overwritten_or_deleted(home):
    skill = home / ".claude" / "skills" / "tuomin-guard"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("mine", encoding="utf-8")
    result = agent_clients.install("claude_code")
    assert result["skill_written"] is False and (skill / "SKILL.md").read_text() == "mine"
    agent_clients.uninstall("claude_code")
    assert (skill / "SKILL.md").read_text() == "mine"


def test_claude_code_falls_back_to_user_json_without_cli(home):
    claude = home / ".claude.json"
    claude.write_text(json.dumps({"projects": {"/x": {}}, "mcpServers": {}}), encoding="utf-8")
    result = agent_clients.install("claude_code")
    assert result["method"] == "file" and "未找到 claude 命令" in result["note"]
    data = json.loads(claude.read_text())
    assert data["projects"] == {"/x": {}} and "tuomin" in data["mcpServers"]


def test_claude_code_uses_cli_when_available(home, tmp_path, monkeypatch):
    monkeypatch.delenv("TUOMIN_DATA_DIR")  # platform default: no env flags
    fake = tmp_path / "claude.exe"
    calls = []
    monkeypatch.setattr(agent_clients, "find_claude_cli", lambda: str(fake))
    def run(cli, *args):
        assert cli == str(fake)
        calls.append(args)
        return __import__('subprocess').CompletedProcess([cli, *args], 0, '', '')
    monkeypatch.setattr(agent_clients, '_run_claude', run)
    result = agent_clients.install("claude_code", with_skill=False)
    assert result["method"] == "cli"
    assert calls[0] == ('mcp', 'remove', '--scope', 'user', 'tuomin')
    assert calls[1][:6] == ('mcp', 'add', '--scope', 'user', 'tuomin', '--') and calls[1][-1] == '--mcp-stdio'


def test_codex_toml_block_replaced_in_place_and_stripped_on_uninstall(home):
    config = home / ".codex" / "config.toml"
    config.parent.mkdir()
    config.write_text('model = "gpt-5"\n\n[mcp_servers.tuomin]\ncommand = "/old"\nargs = []\n[mcp_servers.tuomin.env]\nX = "1"\n\n[mcp_servers.other]\ncommand = "o"\n', encoding="utf-8")
    agent_clients.install("codex", with_skill=False)
    text = config.read_text()
    assert text.count("[mcp_servers.tuomin]") == 1 and '"/old"' not in text and "X = " not in text
    assert 'model = "gpt-5"' in text and "[mcp_servers.other]" in text
    try:
        import tomllib
    except ModuleNotFoundError:
        import tomli as tomllib
    parsed = tomllib.loads(text)
    assert parsed["mcp_servers"]["tuomin"]["args"][-1] == "--mcp-stdio"
    assert agent_clients.status("codex")["current"] is True
    agent_clients.uninstall("codex")
    text = config.read_text()
    assert "tuomin" not in text and "[mcp_servers.other]" in text and 'model = "gpt-5"' in text


def test_unreadable_config_is_not_overwritten(home):
    cursor = home / ".cursor" / "mcp.json"
    cursor.parent.mkdir()
    cursor.write_text("{ not json", encoding="utf-8")
    with pytest.raises(agent_clients.ClientError) as error:
        agent_clients.install("cursor")
    assert error.value.code == "config_unreadable" and cursor.read_text() == "{ not json"


def test_snippets_and_path_warnings(home, monkeypatch):
    monkeypatch.delenv("TUOMIN_DATA_DIR")
    assert json.loads(agent_clients.snippet("claude_desktop"))["mcpServers"]["tuomin"]["args"][-1] == "--mcp-stdio"
    snippet = agent_clients.snippet("claude_code")
    assert snippet.startswith("& 'claude' 'mcp' 'add'") if sys.platform == 'win32' else snippet.startswith("claude mcp add --scope user tuomin -- ")
    assert agent_clients.snippet("codex").startswith("[mcp_servers.tuomin]")
    assert "应用程序" in agent_clients.command_warning("/Volumes/Tuomin/Tuomin.app/Contents/MacOS/tuomin-workbench")
    assert agent_clients.command_warning("/Applications/Tuomin.app/Contents/MacOS/tuomin-workbench") is None
    with zipfile.ZipFile(__import__("io").BytesIO(agent_clients.skill_zip())) as archive:
        assert archive.namelist() == ["tuomin-guard/SKILL.md"]


def test_custom_data_dir_is_passed_to_the_mcp_command(home, tmp_path, monkeypatch):
    monkeypatch.setenv("TUOMIN_DATA_DIR", str(tmp_path / "custom"))
    _, _, env = agent_clients.mcp_command()
    assert env == {"TUOMIN_DATA_DIR": str((tmp_path / "custom").resolve())}


# --- admin API ----------------------------------------------------------------------

def _admin_app(tmp_path, *, demo=False):
    dictionary = tmp_path / "wb.json"
    dictionary.write_text(json.dumps([{"canonical_value": ORG, "aliases": [], "label": "ORG", "status": "active"}], ensure_ascii=False), encoding="utf-8")
    config = tmp_path / "apps.json"
    config.write_text(json.dumps({"apps": {"clipboard": {"name": "剪贴板", "profile": "file_workbench", "dictionary": str(dictionary)},
                                          "workbench": {"name": "本机工作台", "profile": "file_workbench", "dictionary": str(dictionary)}}}, ensure_ascii=False), encoding="utf-8")
    app = create_app(registry=AppRegistry.load(config), store=MappingStore(tmp_path / "maps"), admin_token=ADMIN)
    app.state.demo = demo
    client = TestClient(app, base_url="http://127.0.0.1")
    client.headers.update({"x-tuomin-admin-token": ADMIN})
    return client, config


def test_admin_overview_install_and_demo_guard(home, tmp_path):
    client, config = _admin_app(tmp_path)
    overview = client.get("/admin/agents").json()
    assert {c["id"] for c in overview["clients"]} == {"claude_code", "codex", "cursor", "claude_desktop", "workbuddy"}
    assert overview["app"]["registered"] is False and overview["sources"] == [{"app_id": "workbench", "name": "本机工作台"}]
    installed = client.post("/admin/agents/cursor/install", json={})
    assert installed.status_code == 200, installed.text
    registry_entry = json.loads(config.read_text())["apps"][AGENT_APP_ID]
    assert registry_entry["dictionary"].endswith("wb.json")
    state = client.get("/admin/agents").json()["app"]
    assert state["configured"] is True and state["dictionary_source"] == "workbench"  # not the mirroring clipboard app
    assert client.get("/admin/agents/codex/snippet").json()["snippet"].startswith("[mcp_servers.tuomin]")
    assert zipfile.ZipFile(__import__("io").BytesIO(base64.b64decode(client.get("/admin/agents/skill.zip").json()["b64"]))).namelist()
    assert client.post("/admin/agents/nope/install").status_code == 404
    rotated = client.post("/admin/agents/rotate").json()
    assert rotated["app"]["configured"] is True
    assert TestClient(client.app, base_url="http://127.0.0.1").get("/admin/agents").status_code == 401

    demo_client, _ = _admin_app(tmp_path / "demo" if (tmp_path / "demo").mkdir() is None else tmp_path, demo=True)
    assert demo_client.post("/admin/agents/codex/install").status_code == 403


# --- agent sessions: consistent placeholders and trusted-UI refill -------------------

def test_agent_session_shares_placeholders_and_refills_only_in_admin_ui(home, tmp_path):
    client, config = _admin_app(tmp_path)
    assert client.post("/admin/agents/setup", json={}).status_code == 200
    tokens = load_agent_tokens(tmp_path / "data")
    anonymous = TestClient(client.app, base_url="http://127.0.0.1")

    def poster(url, headers, payload, method):
        path = url.split("http://gw", 1)[1]
        response = anonymous.request(method or ("POST" if payload is not None else "GET"), path, json=payload, headers=headers)
        return response.status_code, response.json()

    gateway = AgentGateway(lambda: "http://gw", tokens["redact"], tokens["namespace"], http_post=poster, session=True)
    first = gateway.redact_text(AGENT_APP_ID, f"甲方：{ORG}，联系人张三")
    second = gateway.redact_text(AGENT_APP_ID, f"请 {ORG} 在 30 日内付款")
    assert first["status"] == "ok" and first["egress_allowed"] and ORG not in first["masked_text"]
    placeholder = next(p for p in __import__("re").findall(r"<[A-Z_]+_\d{3}>", first["masked_text"]) if p.startswith("<ORG") or p.startswith("<PARTYA"))
    assert placeholder in second["masked_text"]  # same value, same placeholder across calls
    assert first["session_id"] == second["session_id"] and first["session_id"].startswith("ns_")

    sessions = client.get("/api/v1/documents/agent-sessions").json()["sessions"]
    assert sessions[0]["namespace_id"] == first["session_id"] and sessions[0]["calls"] == 2
    answer = f"结论：{placeholder} 应按期付款。"
    restored = client.post(f"/api/v1/documents/agent-sessions/{first['session_id']}/refill-answer", json={"answer_markdown": answer}).json()
    assert restored["status"] == "ok" and ORG in restored["restored_markdown"]
    mapping = client.post(f"/api/v1/documents/agent-sessions/{first['session_id']}/mapping", json={}).json()
    assert any(e["original_value"] == ORG for e in mapping["entries"])
    # Agents (capability tokens only) cannot reach any restore surface.
    denied = anonymous.post(f"/api/v1/documents/agent-sessions/{first['session_id']}/refill-answer",
                            json={"answer_markdown": answer}, headers={"x-tuomin-capability-token": tokens["namespace"]})
    assert denied.status_code == 401
    assert client.post("/api/v1/documents/agent-sessions/ns_missing/mapping", json={}).status_code == 404


# --- self-test driver ------------------------------------------------------------------

FAKE_MCP = r'''
import json, sys
for line in sys.stdin:
    m = json.loads(line)
    if "id" not in m: continue
    if m["method"] == "initialize": r = {"serverInfo": {"name": "fake"}}
    elif m["method"] == "tools/list": r = {"tools": [{"name": n} for n in ("tuomin_readiness", "tuomin_redact_text", "tuomin_redact_values")]}
    elif m["params"]["name"] == "tuomin_readiness": r = {"content": [{"type": "text", "text": json.dumps({"ready": True})}]}
    else: r = {"content": [{"type": "text", "text": json.dumps({"egress_allowed": True, "masked_text": "联系人<PERSON_001>，电话<PHONE_001>"})}]}
    print(json.dumps({"jsonrpc": "2.0", "id": m["id"], "result": r}), flush=True)
'''


def test_selftest_reports_each_step(tmp_path):
    ok = run_selftest(sys.executable, ["-c", FAKE_MCP], timeout=10)
    assert ok["ok"] is True and [s["step"] for s in ok["steps"]] == ["MCP 握手", "工具列表", "智能体应用就绪", "合成样例脱敏"]
    dead = run_selftest(sys.executable, ["-c", "import sys; sys.exit(0)"],
                        timeout=15 if sys.platform == "win32" else 5)
    assert dead["ok"] is False and dead["steps"][-1]["detail"] == "MCP 进程提前退出"
    missing = run_selftest(str(tmp_path / "nope"), [], timeout=5)
    assert missing["ok"] is False and missing["steps"][0]["step"] == "启动 MCP 命令"


def test_selftest_timeout_returns_safe_failure():
    result = run_selftest(sys.executable, ['-c', 'import time; time.sleep(10)'], timeout=0.1)
    assert result['ok'] is False and result['steps'][-1]['step'] == '等待响应'


def test_selftest_invalid_utf8_returns_safe_failure():
    # This exercises decoding after startup. A cold Windows interpreter can
    # take more than five seconds to launch.
    result = run_selftest(sys.executable, ['-c', "import sys; sys.stdout.buffer.write(b'\\xff\\n'); sys.stdout.flush()"], timeout=15)
    assert result['ok'] is False and result['steps'][-1]['detail'] == 'MCP 进程提前退出'


def test_mcp_uses_utf8_even_when_console_encoding_starts_as_gbk():
    import subprocess
    code = "import sys; [s.reconfigure(encoding='gbk') for s in (sys.stdin,sys.stdout,sys.stderr)]; from tuomin_gateway.mcp_stdio import main; main()"
    message = {'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call', 'params': {'name': '合成未知工具', 'arguments': {}}}
    result = subprocess.run([sys.executable, '-X', 'utf8', '-c', code], cwd=ROOT / 'src',
                            input=(json.dumps(message, ensure_ascii=False) + '\n').encode('utf-8'),
                            capture_output=True, timeout=10, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    assert result.returncode == 0
    payload = json.loads(result.stdout.decode('utf-8'))
    assert '合成未知工具' in payload['error']['message']


# --- launcher: token-free --mcp-stdio with lazy app launch --------------------------------

def _launcher():
    spec = importlib.util.spec_from_file_location("launcher_for_agent_tests", ROOT / "packaging/launcher.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_mcp_stdio_reads_private_tokens_and_enables_sessions(tmp_path, monkeypatch):
    launcher = _launcher()
    config = tmp_path / "apps.json"
    config.write_text('{"apps": {}}', encoding="utf-8")
    ensure_agent_app(config, tmp_path)
    for key in ("TUOMIN_AGENT_APP", "TUOMIN_AGENT_REDACT_TOKEN", "TUOMIN_AGENT_NAMESPACE_TOKEN", "TUOMIN_AGENT_SESSION", "TUOMIN_GATEWAY_URL"):
        monkeypatch.delenv(key, raising=False)
    seen = {}
    import tuomin_gateway.mcp_stdio as mcp
    monkeypatch.setattr(mcp, "main", lambda gateway_resolver=None: seen.update(env=dict(os.environ), resolver=gateway_resolver) or 0)
    assert launcher._run_mcp_stdio(tmp_path) == 0
    tokens = load_agent_tokens(tmp_path)
    assert seen["env"]["TUOMIN_AGENT_APP"] == AGENT_APP_ID and seen["env"]["TUOMIN_AGENT_SESSION"] == "1"
    assert seen["env"]["TUOMIN_AGENT_REDACT_TOKEN"] == tokens["redact"] and callable(seen["resolver"])


def test_gateway_resolver_launches_app_once_and_waits_for_instance(tmp_path, monkeypatch):
    launcher = _launcher()
    monkeypatch.delenv("TUOMIN_GATEWAY_URL", raising=False)
    launches = []

    def launch():
        launches.append(1)
        (tmp_path / "instance.url").write_text("http://127.0.0.1:9123/ui/\n", encoding="utf-8")
        return True

    resolve = launcher._gateway_resolver(tmp_path, launch=launch, sleep=lambda _: None, alive=lambda url: True)
    assert resolve() == "http://127.0.0.1:9123" and launches == [1]
    assert launcher._gateway_resolver(tmp_path, launch=lambda: False, alive=lambda url: True)() == "http://127.0.0.1:9123"


def test_stale_instance_url_is_not_trusted(tmp_path, monkeypatch):
    launcher = _launcher()
    monkeypatch.delenv("TUOMIN_GATEWAY_URL", raising=False)
    (tmp_path / "instance.url").write_text("http://127.0.0.1:9124/ui/\n", encoding="utf-8")  # left by a crashed run
    launches = []
    resolve = launcher._gateway_resolver(tmp_path, launch=lambda: launches.append(1) or False, alive=lambda url: False)
    assert resolve() == "http://127.0.0.1:8765" and launches == [1]  # tried to start the app instead
