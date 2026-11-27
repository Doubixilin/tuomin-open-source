"""Isolated synthetic Windows regression and source smoke; never uses root app config."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import secrets
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from tuomin_gateway.platform_security import restrict_permissions


def isolated_environment(runtime: Path, python: Path, node: Path | None = None) -> dict[str, str]:
    sensitive_prefixes = ("TUOMIN_", "OPENAI_", "ANTHROPIC_", "DEEPSEEK_", "AZURE_OPENAI_", "CLAUDE_", "KIMI_", "PYTEST_", "HF_")
    env = {k: v for k, v in os.environ.items() if not k.upper().startswith(sensitive_prefixes)
           and k.upper() not in {"PYTHONPATH", "PYTHONHOME", "PYTHONSTARTUP", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY"}}
    temp = runtime / "temp"
    temp.mkdir()
    home = runtime / 'home'
    home.mkdir()
    roaming, local = home / 'AppData' / 'Roaming', home / 'AppData' / 'Local'
    roaming.mkdir(parents=True)
    local.mkdir(parents=True)
    dictionary = runtime / "dictionary.json"
    dictionary.write_text(json.dumps([
        {"canonical_value": "星河测试公司", "label": "ORG", "version": "synthetic"},
        {"canonical_value": "沈测试", "label": "PERSON", "version": "synthetic"},
    ], ensure_ascii=False), encoding="utf-8")
    config = runtime / "apps.json"
    config.write_text(json.dumps({"apps": {"production": {
        "profile": "file_workbench", "dictionary": str(dictionary),
    }}}, ensure_ascii=False), encoding="utf-8")
    env.update({
        "TUOMIN_CONFIG": str(config), "TUOMIN_DATA_DIR": str(runtime / "data"),
        "TUOMIN_MAPPING_DIR": str(runtime / "mappings"),
        "TUOMIN_ADMIN_TOKEN": secrets.token_urlsafe(32),
        "TUOMIN_NER_MODEL_DIR": str(ROOT / "packaging" / "ner-model"),
        "HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1",
        "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8", "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONNOUSERSITE": "1", "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
        "PYTHONPATH": str(ROOT / "src"), "PYTHON": str(python),
        "TEMP": str(temp), "TMP": str(temp), "NO_PROXY": "127.0.0.1,localhost,::1",
        "TUOMIN_NO_BROWSER": "1",
        "HOME": str(home), "USERPROFILE": str(home),
        "APPDATA": str(roaming), "LOCALAPPDATA": str(local),
        # Git trust is scoped to this child; never inherit or edit global config.
        "GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "safe.directory", "GIT_CONFIG_VALUE_0": ROOT.as_posix(),
        # Issuer tests intentionally reject keys inside the repository.
        "TUOMIN_TEST_EXTERNAL_TEMP": tempfile.gettempdir(),
    })
    if node:
        env["TUOMIN_NODE_BIN"] = str(node)
        env["PATH"] = str(node.parent) + os.pathsep + env.get("PATH", "")
    return env


def run_logged(command: list[str], env: dict[str, str], log: Path) -> int:
    with log.open("w", encoding="utf-8") as handle:
        restrict_permissions(log, is_dir=False)
        proc = subprocess.Popen(command, cwd=ROOT, env=env, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace",
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        try:
            assert proc.stdout is not None
            for line in proc.stdout:
                safe = line.replace(env["TUOMIN_ADMIN_TOKEN"], "[redacted-admin-credential]")
                handle.write(safe)
                print(safe, end="", flush=True)
            return proc.wait()
        finally:
            if proc.poll() is None:
                proc.terminate()
                proc.wait(timeout=10)


def suite(args, runtime: Path, env: dict[str, str]) -> dict:
    command = [str(args.python), "-X", "utf8", "-m", "pytest", "-q", "-p", "no:cacheprovider",
               "--basetemp", str(runtime / "pytest"), *(args.tests or [])]
    codes = {"python": run_logged(command, env, runtime / "python-tests.log")}
    if not args.tests and not codes["python"]:
        assert args.node is not None
        test_files = sorted(str(p) for p in (ROOT / "tests" / "ccr").glob("*.test.cjs"))
        codes["node"] = run_logged([str(args.node), "--test", *test_files], env, runtime / "node-tests.log")
        codes["dpapi"] = run_logged([str(args.python), "-X", "utf8", "scripts/check_dpapi_store.py"], env, runtime / "dpapi.log")
    return {"status": "ok" if not any(codes.values()) else "failed", "exit_codes": codes}


def get_json(base: str, route: str, timeout: float = 90) -> dict:
    with urllib.request.urlopen(base + route, timeout=timeout) as response:
        return json.load(response)


def smoke(args, runtime: Path, env: dict[str, str]) -> dict:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", args.port))
        port = sock.getsockname()[1]
    base = f"http://127.0.0.1:{port}"
    env["TUOMIN_WARM_NER"] = "0" if args.no_warmup else "1"
    started = time.monotonic()
    log_path = runtime / "gateway.log"
    with log_path.open("w", encoding="utf-8") as log:
        restrict_permissions(log_path, is_dir=False)
        proc = subprocess.Popen([str(args.python), "-X", "utf8", "-m", "tuomin_gateway.cli", "serve",
                                 "--host", "127.0.0.1", "--port", str(port)], cwd=ROOT, env=env,
                                stdout=log, stderr=subprocess.STDOUT,
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        try:
            deadline = time.monotonic() + 60
            while True:
                if proc.poll() is not None:
                    raise RuntimeError("isolated gateway exited; inspect protected gateway.log")
                try:
                    health = get_json(base, "/healthz", 2)
                    break
                except (OSError, ValueError):
                    if time.monotonic() >= deadline:
                        raise RuntimeError("isolated gateway health timeout")
                    time.sleep(0.25)
            health_seconds = time.monotonic() - started
            readiness = get_json(base, "/readiness", 120)
            ready_seconds = time.monotonic() - started
            if readiness.get("status") != "ready":
                raise RuntimeError("isolated gateway is not ready")
            probe_start = time.monotonic()
            get_json(base, "/readiness", 30)
            probe_seconds = time.monotonic() - probe_start
            code = run_logged([str(args.python), "-X", "utf8", "scripts/security_battery_runtime.py",
                               "--base-url", base], env, runtime / "security-battery.log")
            return {"status": "ok" if code == 0 else "failed", "health": health["status"],
                    "readiness": readiness["status"], "port": port, "warmup_enabled": not args.no_warmup,
                    "health_seconds": round(health_seconds, 3), "ready_seconds": round(ready_seconds, 3),
                    "warm_probe_seconds": round(probe_seconds, 3), "battery_exit_code": code}
        finally:
            if proc.poll() is None:
                proc.terminate()
            proc.wait(timeout=15)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("suite", "smoke"))
    parser.add_argument("--python", type=Path, default=Path(sys.executable))
    parser.add_argument("--node", type=Path)
    parser.add_argument("--runtime-root", type=Path, default=ROOT / "local_private" / "windows-validation")
    parser.add_argument("--tests", nargs="*")
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--no-warmup", action="store_true")
    args = parser.parse_args()
    args.python = args.python.resolve()
    if args.node:
        args.node = args.node.resolve()
    if args.mode == "suite" and not args.tests and not args.node:
        parser.error("full suite requires an explicit Node executable")
    parent = args.runtime_root.resolve()
    if not parent.is_relative_to(ROOT / "local_private"):
        parser.error("runtime-root must be inside the repository local_private directory")
    parent.mkdir(parents=True, exist_ok=True)
    runtime = Path(tempfile.mkdtemp(prefix=args.mode + "-", dir=parent))
    restrict_permissions(runtime, is_dir=True)
    env = isolated_environment(runtime, args.python, args.node)
    print(f"[validation] isolated runtime: {runtime}", flush=True)
    try:
        result = suite(args, runtime, env) if args.mode == "suite" else smoke(args, runtime, env)
    except Exception as exc:
        result = {"status": "failed", "error_type": type(exc).__name__, "message": "inspect protected validation logs"}
    result.update({"runtime_dir": str(runtime), "synthetic_only": True, "live_upstream_used": False,
                   "source_root": str(ROOT / "src")})
    for log in runtime.glob("*.log"):
        if env["TUOMIN_ADMIN_TOKEN"] in log.read_text(encoding="utf-8", errors="replace"):
            result["status"] = "failed"
            result["credential_log_leak"] = True
    (runtime / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["status"] == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())
