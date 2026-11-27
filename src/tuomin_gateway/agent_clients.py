"""One-click MCP + Skill setup for local AI clients.

Each client gets the same STDIO command (``tuomin-workbench --mcp-stdio`` for
the installed app, or the source launcher in development). No credential is
ever written: ``--mcp-stdio`` reads the ``agent`` app's tokens from Tuomin's
private data directory. Every write keeps a timestamped backup, is idempotent,
and can be undone; uninstall removes only the ``tuomin`` server entry and skill
directories carrying our marker file.
"""
from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

SERVER_NAME = "tuomin"
SKILL_NAME = "tuomin-guard"
SKILL_MARKER = ".tuomin-managed"


class ClientError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


# --- the command every client launches ---------------------------------------

def mcp_command() -> tuple[str, list[str], dict[str, str]]:
    """(command, args, env) for the STDIO server, absolute paths only."""
    from tuomin_gateway.agent_setup import default_data_dir

    env: dict[str, str] = {}
    configured = os.environ.get("TUOMIN_DATA_DIR")
    if configured:
        platform_default = default_data_dir(use_override=False)
        if Path(configured).expanduser().resolve() != platform_default.resolve():
            env["TUOMIN_DATA_DIR"] = str(Path(configured).expanduser().resolve())
    if getattr(sys, "frozen", False):
        executable = Path(sys.executable)
        if sys.platform == 'win32' and executable.with_name('Tuomin-cli.exe').is_file():
            executable = executable.with_name('Tuomin-cli.exe')
        return str(executable), ["--mcp-stdio"], env
    launcher = Path(__file__).resolve().parents[2] / "packaging" / "launcher.py"
    return sys.executable, [str(launcher), "--mcp-stdio"], env


def command_warning(command: str) -> str | None:
    """Paths that stop working once the app is moved or the DMG is ejected."""
    if "/Volumes/" in command or "AppTranslocation" in command:
        return "Tuomin 当前从磁盘映像或系统临时位置运行，写入的路径之后会失效。请先把 Tuomin 拖进「应用程序」文件夹，从那里打开后再接入。"
    if "/Downloads/" in command.replace('\\', '/') and sys.platform == 'win32':
        return "请先将完整解压的 Tuomin 文件夹放到固定位置，再配置接入；移动后需重新写入客户端配置。"
    if "/Downloads/" in command:
        return "Tuomin 位于「下载」文件夹，移动后客户端会找不到它。建议先拖进「应用程序」文件夹再接入。"
    return None


def server_entry(*, stdio_type: bool = False) -> dict:
    command, args, env = mcp_command()
    entry: dict = {"command": command, "args": list(args)}
    if env:
        entry["env"] = dict(env)
    if stdio_type:
        entry = {"type": "stdio", **entry}
    return entry


# --- skill files ------------------------------------------------------------

def skill_source() -> Path | None:
    candidates = []
    if getattr(sys, "frozen", False):
        candidates.append(Path(getattr(sys, "_MEIPASS", "")) / "skills" / SKILL_NAME)
    candidates.append(Path(__file__).resolve().parents[2] / "integrations" / "skills" / SKILL_NAME)
    for path in candidates:
        if (path / "SKILL.md").is_file():
            return path
    return None


def skill_text() -> str:
    source = skill_source()
    if source is None:
        raise ClientError("skill_missing", "找不到随包的 tuomin-guard skill 文件")
    return (source / "SKILL.md").read_text(encoding="utf-8")


def install_skill(target_root: Path) -> dict:
    target = target_root / SKILL_NAME
    if target.exists() and not (target / SKILL_MARKER).exists():
        return {"skill_path": str(target), "skill_written": False, "skill_note": "已存在同名的自建 skill，未覆盖"}
    target.mkdir(parents=True, exist_ok=True)
    (target / "SKILL.md").write_text(skill_text(), encoding="utf-8")
    (target / SKILL_MARKER).write_text("written by Tuomin; removed on uninstall\n", encoding="utf-8")
    return {"skill_path": str(target), "skill_written": True}


def uninstall_skill(target_root: Path) -> bool:
    target = target_root / SKILL_NAME
    if (target / SKILL_MARKER).is_file():
        shutil.rmtree(target, ignore_errors=True)
        return True
    return False


def skill_zip() -> bytes:
    """A zip of the skill folder, for clients that take uploads (Claude Desktop)."""
    import io
    import zipfile

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(f"{SKILL_NAME}/SKILL.md", skill_text())
    return buffer.getvalue()


# --- config file helpers ------------------------------------------------------

def _backup(path: Path) -> str | None:
    if not path.exists():
        return None
    backup = path.with_name(path.name + f".bak-{time.time_ns()}-{uuid.uuid4().hex[:12]}")
    shutil.copy2(path, backup)
    return str(backup)


def _load_json(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8") or "{}")
    except (OSError, ValueError) as exc:
        raise ClientError("config_unreadable", f"无法解析 {path}，为避免破坏原配置未做修改，请先手动修复该文件") from exc
    if not isinstance(data, dict):
        raise ClientError("config_unreadable", f"{path} 不是 JSON 对象，未做修改")
    return data


def _write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tuomin-tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)


_TOML_HEADER = re.compile(r"^\s*\[\[?\s*([^\]]+?)\s*\]\]?\s*(#.*)?$")


def _toml_strip_server(text: str) -> tuple[str, bool]:
    kept, skipping, removed = [], False, False
    for line in text.splitlines(keepends=True):
        match = _TOML_HEADER.match(line)
        if match:
            name = match.group(1).replace('"', "").replace("'", "").strip()
            skipping = name == f"mcp_servers.{SERVER_NAME}" or name.startswith(f"mcp_servers.{SERVER_NAME}.")
            removed = removed or skipping
        if not skipping:
            kept.append(line)
    return "".join(kept), removed


def _toml_block(entry: dict) -> str:
    lines = [f"[mcp_servers.{SERVER_NAME}]", f"command = {json.dumps(entry['command'], ensure_ascii=False)}",
             "args = [" + ", ".join(json.dumps(a, ensure_ascii=False) for a in entry["args"]) + "]"]
    if entry.get("env"):
        lines.append(f"[mcp_servers.{SERVER_NAME}.env]")
        lines += [f"{key} = {json.dumps(value, ensure_ascii=False)}" for key, value in entry["env"].items()]
    return "\n".join(lines) + "\n"


def _toml_entry(path: Path) -> dict | None:
    if not path.exists():
        return None
    try:
        import tomllib
    except ModuleNotFoundError:  # Python 3.10
        import tomli as tomllib
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    server = (data.get("mcp_servers") or {}).get(SERVER_NAME)
    return server if isinstance(server, dict) else None


# --- clients ------------------------------------------------------------------

def _home() -> Path:
    return Path.home()


def _claude_desktop_config() -> Path:
    if sys.platform == "darwin":
        return _home() / "Library" / "Application Support" / "Claude" / "claude_desktop_config.json"
    if os.name == "nt":
        appdata = os.environ.get("APPDATA")
        return (Path(appdata) if appdata else _home() / "AppData" / "Roaming") / "Claude" / "claude_desktop_config.json"
    return _home() / ".config" / "Claude" / "claude_desktop_config.json"


def _app_installed(*names: str) -> bool:
    if sys.platform != "darwin":
        return False
    return any(Path(root, name).exists() for name in names for root in ("/Applications", str(_home() / "Applications")))


def find_claude_cli() -> str | None:
    """`claude` is often missing from a GUI app's PATH; also try common installs."""
    extra = [str(_home() / ".local" / "bin"), str(_home() / ".claude" / "local"), "/opt/homebrew/bin",
             "/usr/local/bin", str(_home() / ".npm-global" / "bin")]
    return shutil.which("claude", path=os.pathsep.join([os.environ.get("PATH", ""), *extra]))


@dataclass
class Client:
    id: str
    name: str
    kind: str  # "json" | "toml" | "claude_code"
    config: Callable[[], Path]
    detect: Callable[[], bool]
    skill_root: Callable[[], Path] | None = None
    stdio_type: bool = False
    skill_note: str = ""
    notes: list[str] = field(default_factory=list)


CLIENTS: dict[str, Client] = {
    "claude_code": Client(
        "claude_code", "Claude Code", "claude_code",
        config=lambda: _home() / ".claude.json",
        detect=lambda: find_claude_cli() is not None or (_home() / ".claude").exists(),
        skill_root=lambda: _home() / ".claude" / "skills",
        notes=["写入后新开一个 Claude Code 会话生效；用 /mcp 可查看连接状态。"],
    ),
    "codex": Client(
        "codex", "Codex", "toml",
        config=lambda: _home() / ".codex" / "config.toml",
        detect=lambda: (_home() / ".codex").exists() or shutil.which("codex") is not None or _app_installed("Codex.app"),
        skill_root=lambda: _home() / ".codex" / "skills",
        notes=["写入后重启 Codex（CLI 或 App）生效。"],
    ),
    "cursor": Client(
        "cursor", "Cursor", "json",
        config=lambda: _home() / ".cursor" / "mcp.json",
        detect=lambda: (_home() / ".cursor").exists() or _app_installed("Cursor.app"),
        skill_note="Cursor 没有用户级 skill；可在项目规则里引用下载的 SKILL.md。",
        notes=["写入后在 Cursor 设置的 MCP 页确认 tuomin 已启用。"],
    ),
    "claude_desktop": Client(
        "claude_desktop", "Claude Desktop", "json",
        config=_claude_desktop_config,
        detect=lambda: _claude_desktop_config().parent.exists() or _app_installed("Claude.app"),
        skill_note="Claude Desktop 的 skill 需要在「设置 → 功能」里上传；点「下载 skill」得到压缩包。",
        notes=["写入后完全退出并重新打开 Claude Desktop 生效。"],
    ),
    "workbuddy": Client(
        "workbuddy", "WorkBuddy", "json",
        config=lambda: Path(os.environ["TUOMIN_WORKBUDDY_MCP"]).expanduser() if os.environ.get("TUOMIN_WORKBUDDY_MCP") else _home() / ".workbuddy" / "mcp.json",
        detect=lambda: (_home() / ".workbuddy").exists() or _app_installed("WorkBuddy.app"),
        skill_root=lambda: _home() / ".workbuddy" / "skills",
        stdio_type=True,
        notes=["写入后在 WorkBuddy「连接器」里确认 tuomin 已启用。"],
    ),
}


def _client(client_id: str) -> Client:
    client = CLIENTS.get(client_id)
    if client is None:
        raise ClientError("unknown_client", f"不支持的客户端：{client_id}")
    return client


def _current_entry(client: Client) -> dict | None:
    path = client.config()
    if client.kind == "toml":
        return _toml_entry(path)
    try:
        data = _load_json(path)
    except ClientError:
        return None
    servers = data.get("mcpServers")
    entry = servers.get(SERVER_NAME) if isinstance(servers, dict) else None
    return entry if isinstance(entry, dict) else None


def status(client_id: str) -> dict:
    client = _client(client_id)
    command, args, env = mcp_command()
    entry = _current_entry(client)
    installed = entry is not None
    current = bool(installed and entry.get("command") == command and list(entry.get("args") or []) == args)
    skill_path = client.skill_root() / SKILL_NAME if client.skill_root else None
    return {
        "id": client.id, "name": client.name,
        "detected": bool(client.detect()),
        "installed": installed,
        "current": current,
        "config_path": str(client.config()),
        "skill_supported": client.skill_root is not None,
        "skill_installed": bool(skill_path and (skill_path / "SKILL.md").is_file()),
        "skill_path": str(skill_path) if skill_path else None,
        "skill_note": client.skill_note,
        "notes": client.notes,
    }


def all_status() -> list[dict]:
    return [status(client_id) for client_id in CLIENTS]


def snippet(client_id: str) -> str:
    client = _client(client_id)
    entry = server_entry(stdio_type=client.stdio_type)
    if client.kind == "toml":
        return _toml_block(entry)
    if client.kind == "claude_code":
        env_flags = [part for key, value in (entry.get("env") or {}).items() for part in ("-e", f"{key}={value}")]
        command = ["claude", "mcp", "add", "--scope", "user", *env_flags, SERVER_NAME, "--", entry["command"], *entry["args"]]
        return ('& ' + ' '.join("'" + arg.replace("'", "''") + "'" for arg in command)
                if sys.platform == 'win32' else shlex.join(command))
    return json.dumps({"mcpServers": {SERVER_NAME: entry}}, ensure_ascii=False, indent=2)


def _install_json(path: Path, entry: dict) -> dict:
    data = _load_json(path)
    backup = _backup(path)
    servers = data.get("mcpServers")
    if not isinstance(servers, dict):
        servers = {}
    servers[SERVER_NAME] = entry
    data["mcpServers"] = servers
    _write_json(path, data)
    return {"config_path": str(path), "backup": backup}


def _uninstall_json(path: Path) -> bool:
    if not path.exists():
        return False
    data = _load_json(path)
    servers = data.get("mcpServers")
    if not isinstance(servers, dict) or SERVER_NAME not in servers:
        return False
    _backup(path)
    del servers[SERVER_NAME]
    _write_json(path, data)
    return True


def _run_claude(cli: str, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run([cli, *args], capture_output=True, text=True, encoding='utf-8', errors='replace',
                          creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0), timeout=30, check=False)


def install(client_id: str, *, with_skill: bool = True) -> dict:
    client = _client(client_id)
    entry = server_entry(stdio_type=client.stdio_type)
    path = client.config()
    result: dict = {"id": client.id, "name": client.name, "method": "file"}
    if client.kind == "toml":
        original = path.read_text(encoding="utf-8") if path.exists() else ""
        backup = _backup(path)
        stripped, _ = _toml_strip_server(original)
        text = stripped.rstrip("\n")
        text = (text + "\n\n" if text else "") + _toml_block(entry)
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(path.name + ".tuomin-tmp")
        temporary.write_text(text, encoding="utf-8")
        os.replace(temporary, path)
        result.update(config_path=str(path), backup=backup)
    elif client.kind == "claude_code":
        cli = find_claude_cli()
        if cli:
            _run_claude(cli, "mcp", "remove", "--scope", "user", SERVER_NAME)
            env_flags = [part for key, value in (entry.get("env") or {}).items() for part in ("-e", f"{key}={value}")]
            completed = _run_claude(cli, "mcp", "add", "--scope", "user", *env_flags, SERVER_NAME, "--", entry["command"], *entry["args"])
            if completed.returncode != 0:
                raise ClientError("client_cli_failed", "claude mcp add 执行失败：" + (completed.stderr or completed.stdout).strip()[:300])
            result.update(method="cli", config_path=str(path), backup=None)
        else:
            result.update(_install_json(path, entry))
            result["note"] = "未找到 claude 命令，已直接写入 ~/.claude.json；如 Claude Code 正在运行，请退出后重开。"
    else:
        result.update(_install_json(path, entry))
    if with_skill and client.skill_root is not None:
        result.update(install_skill(client.skill_root()))
    warning = command_warning(entry["command"])
    if warning:
        result["warning"] = warning
    result["status"] = status(client_id)
    return result


def uninstall(client_id: str) -> dict:
    client = _client(client_id)
    path = client.config()
    removed = False
    if client.kind == "toml":
        if path.exists():
            original = path.read_text(encoding="utf-8")
            stripped, removed = _toml_strip_server(original)
            if removed:
                _backup(path)
                path.write_text(stripped.rstrip("\n") + ("\n" if stripped.strip() else ""), encoding="utf-8")
    elif client.kind == "claude_code" and find_claude_cli():
        completed = _run_claude(find_claude_cli(), "mcp", "remove", "--scope", "user", SERVER_NAME)
        removed = completed.returncode == 0
    else:
        removed = _uninstall_json(path)
    skill_removed = uninstall_skill(client.skill_root()) if client.skill_root else False
    return {"id": client.id, "removed": removed, "skill_removed": skill_removed, "status": status(client_id)}
