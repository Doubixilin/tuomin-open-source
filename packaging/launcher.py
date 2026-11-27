"""Desktop launcher with a native shell and an explicit browser fallback."""

from __future__ import annotations

import atexit
import argparse
import importlib
import os
import socket
import secrets
import signal
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
import webbrowser
from pathlib import Path


_CROWN_NATIVE_MODULES = (
    "tuomin_gateway.policy",
    "tuomin_gateway.profiles",
    "tuomin_gateway.roles",
    "tuomin_gateway.fusion",
    "tuomin_gateway.redactor",
    "tuomin_gateway.refill",
    "tuomin_gateway.placeholders",
    "tuomin_gateway.mapping",
    "tuomin_gateway.detectors.rules",
    "tuomin_gateway.guard.injection",
    "tuomin_gateway.guard.secrets",
    "tuomin_gateway.document.refill_flow",
    "tuomin_gateway.document.docx_patch",
    "tuomin_gateway.document.office_patch",
    "tuomin_gateway.licensing.core",
    "tuomin_gateway.licensing.state",
)


def _open_inherited_windows_stream(handle_id: int, mode: str):
    """Recover a redirected stdio handle in a windowed Windows executable.

    PyInstaller's windowed bootloader leaves Python's streams as None even
    when an MCP client supplies pipes. Duplicate the handle so closing the
    Python stream never takes ownership of the original Windows std handle.
    This never attaches to or allocates a console.
    """
    import ctypes
    from ctypes import wintypes
    import msvcrt

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.GetStdHandle.argtypes = [wintypes.DWORD]
    kernel.GetStdHandle.restype = wintypes.HANDLE
    kernel.GetFileType.argtypes = [wintypes.HANDLE]
    kernel.GetFileType.restype = wintypes.DWORD
    kernel.GetCurrentProcess.argtypes = []
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    kernel.DuplicateHandle.argtypes = [wintypes.HANDLE, wintypes.HANDLE, wintypes.HANDLE,
                                      ctypes.POINTER(wintypes.HANDLE), wintypes.DWORD,
                                      wintypes.BOOL, wintypes.DWORD]
    kernel.DuplicateHandle.restype = wintypes.BOOL
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL
    handle = kernel.GetStdHandle(wintypes.DWORD(handle_id))
    if not handle or handle == ctypes.c_void_p(-1).value or kernel.GetFileType(handle) == 0:
        return None
    duplicate = wintypes.HANDLE()
    process = kernel.GetCurrentProcess()
    if not kernel.DuplicateHandle(process, handle, process, ctypes.byref(duplicate), 0, False, 2):
        return None
    try:
        flags = os.O_BINARY | (os.O_RDONLY if mode == "r" else os.O_WRONLY)
        fd = msvcrt.open_osfhandle(duplicate.value, flags)
    except OSError:
        kernel.CloseHandle(duplicate)
        return None
    try:
        return os.fdopen(fd, mode, buffering=1 if mode == "w" else -1,
                         encoding="utf-8", errors="replace")
    except Exception:
        os.close(fd)
        raise


def _prepare_standard_streams() -> None:
    """Keep GUI startup quiet and preserve redirected MCP/headless protocols."""
    if os.name != "nt":
        return
    for name, handle_id, mode in (("stdin", -10, "r"), ("stdout", -11, "w"), ("stderr", -12, "w")):
        if getattr(sys, name) is not None:
            continue
        stream = _open_inherited_windows_stream(handle_id, mode)
        if stream is None:
            stream = open(os.devnull, mode, encoding="utf-8")
        setattr(sys, name, stream)
        if getattr(sys, "__" + name + "__") is None:
            setattr(sys, "__" + name + "__", stream)



def _native_selftest() -> int:
    _prepare_unfrozen_imports()
    expected_suffix = ".pyd" if os.name == "nt" else ".so"
    failures: list[str] = []
    for module_name in _CROWN_NATIVE_MODULES:
        try:
            module = importlib.import_module(module_name)
        except Exception as exc:
            failures.append(f"{module_name}:{type(exc).__name__}")
            continue
        compiled = getattr(module, "__compiled__", None)
        extension = str(getattr(compiled, "extension_filename", ""))
        if not extension.lower().endswith(expected_suffix):
            failures.append(f"{module_name}:{extension or 'not-nuitka-compiled'}")
    if failures:
        print(f"[native-selftest] failed: {failures}", file=sys.stderr, flush=True)
        return 1
    print(
        f"[native-selftest] OK, modules={len(_CROWN_NATIVE_MODULES)}",
        flush=True,
    )
    return 0



def _restrict_runtime_path(path: Path, *, is_dir: bool) -> None:
    _prepare_unfrozen_imports()
    from tuomin_gateway.platform_security import restrict_permissions

    restrict_permissions(path, is_dir=is_dir)

def _dpapi_selftest(data_dir: Path) -> int:
    if os.name != "nt":
        print("[dpapi-selftest] Windows only", file=sys.stderr, flush=True)
        return 2
    _prepare_unfrozen_imports()
    from tuomin_gateway.store import MappingStore

    store_dir = data_dir / (
        f"dpapi-selftest-{os.getpid()}-{secrets.token_hex(4)}"
    )
    key = "synthetic-frozen-dpapi-selftest"
    payload = b'{"synthetic":true}'
    store = MappingStore(store_dir, ttl_seconds=60)
    try:
        path = store.save_payload(key, payload)
        raw = path.read_bytes()
        status = store.safe_status()
        ok = (
            raw.startswith(b"DPAPI1\n")
            and payload not in raw
            and store.load_payload(key) == payload
            and status.get("platform_mode") == "windows-dpapi"
        )
        if not ok:
            print(
                "[dpapi-selftest] failed: encrypted round-trip mismatch",
                file=sys.stderr,
                flush=True,
            )
            return 1
        print("[dpapi-selftest] OK, marker=DPAPI1", flush=True)
        return 0
    except Exception as exc:
        print(
            f"[dpapi-selftest] failed: {type(exc).__name__}",
            file=sys.stderr,
            flush=True,
        )
        return 1
    finally:
        store.delete(key)
        try:
            store_dir.rmdir()
        except OSError:
            pass

def _browser_enabled() -> bool:
    return os.environ.get("TUOMIN_NO_BROWSER", "").strip().lower() not in {
        "1",
        "true",
        "yes",
    }

def _windows_pid_is_alive(pid: int) -> bool:
    """Query a Windows process without sending it a signal.

    A process object can remain open briefly after the process has exited, so
    merely opening a PID is not sufficient. GetExitCodeProcess distinguishes
    that state from a genuinely running process.
    """
    import ctypes
    from ctypes import wintypes

    process_query_limited_information = 0x1000
    still_active = 259
    error_access_denied = 5
    error_invalid_parameter = 87

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    open_process = kernel32.OpenProcess
    open_process.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    open_process.restype = wintypes.HANDLE
    get_exit_code = kernel32.GetExitCodeProcess
    get_exit_code.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    get_exit_code.restype = wintypes.BOOL
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = [wintypes.HANDLE]
    close_handle.restype = wintypes.BOOL

    handle = open_process(process_query_limited_information, False, pid)
    if not handle:
        error = ctypes.get_last_error()
        if error == error_access_denied:
            # A protected process still exists; avoid deleting a lock that may
            # belong to a running elevated instance.
            return True
        if error == error_invalid_parameter:
            return False
        return False

    try:
        exit_code = wintypes.DWORD()
        if not get_exit_code(handle, ctypes.byref(exit_code)):
            # We opened the process but cannot safely prove it exited.
            return True
        return exit_code.value == still_active
    finally:
        close_handle(handle)


def _data_dir() -> Path:
    configured = os.environ.get("TUOMIN_DATA_DIR")
    if configured:
        path = Path(configured).expanduser()
    elif sys.platform == "darwin":
        path = Path.home() / "Library" / "Application Support" / "Tuomin"
    elif os.name == "nt":
        appdata = os.environ.get("APPDATA")
        base = Path(appdata) if appdata else Path.home() / "AppData" / "Roaming"
        path = base / "Tuomin"
    else:
        path = Path.home() / ".local" / "share" / "tuomin"
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    _prepare_unfrozen_imports()
    from tuomin_gateway.platform_security import restrict_permissions

    restrict_permissions(path, is_dir=True)
    return path


def _pid_is_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        return _windows_pid_is_alive(pid)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _acquire_instance_lock(lock_path: Path, url_path: Path) -> bool:
    while True:
        try:
            fd = os.open(lock_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            try:
                existing_pid = int(lock_path.read_text(encoding="ascii").strip())
            except (OSError, UnicodeError, ValueError):
                existing_pid = -1
            if _pid_is_alive(existing_pid):
                print(f"[tuomin] already running (PID {existing_pid})", flush=True)
                try:
                    existing_url = url_path.read_text(encoding="utf-8").strip()
                except OSError:
                    existing_url = ""
                if existing_url and _browser_enabled():
                    webbrowser.open(existing_url)
                return False
            try:
                lock_path.unlink()
            except FileNotFoundError:
                continue
            try:
                url_path.unlink()
            except FileNotFoundError:
                pass
            continue
        _restrict_runtime_path(lock_path, is_dir=False)
        with os.fdopen(fd, "w", encoding="ascii") as handle:
            handle.write(f"{os.getpid()}\n")
        return True


def _cleanup_instance_files(lock_path: Path, url_path: Path) -> None:
    for path in (url_path, lock_path):
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            pass


def _publish_instance_url(path: Path, url: str) -> None:
    # Discovery readers may poll while the launcher is starting.
    fd, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            _restrict_runtime_path(temporary, is_dir=False)
            stream.write(f"{url}\n")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _configure_admin_token(data_dir: Path) -> None:
    _prepare_unfrozen_imports()
    from tuomin_gateway.runtime import configure_admin_token, report_admin_token_location

    report_admin_token_location(configure_admin_token(data_dir))


def _pick_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _pick_shell_port() -> tuple[int, bool]:
    try:
        preferred = int(os.environ.get("TUOMIN_PORT", "8765"))
    except ValueError as exc:
        raise ValueError("TUOMIN_PORT must be an integer from 1 to 65535") from exc
    if not 1 <= preferred <= 65535:
        raise ValueError("TUOMIN_PORT must be an integer from 1 to 65535")
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        try:
            sock.bind(("127.0.0.1", preferred))
        except OSError:
            return _pick_free_port(), True
    return preferred, False


def _open_browser_when_ready(port: int, ui_url: str) -> None:
    if not _browser_enabled() or os.environ.get("TUOMIN_OPEN_BROWSER", "1") == "0":
        return
    health_url = f"http://127.0.0.1:{port}/healthz"
    for _ in range(50):
        try:
            with urllib.request.urlopen(health_url, timeout=0.1) as response:
                if response.status == 200:
                    webbrowser.open(ui_url)
                    return
        except (OSError, urllib.error.URLError):
            pass
        time.sleep(0.1)


def _prepare_unfrozen_imports() -> None:
    if not getattr(sys, "frozen", False):
        src_dir = Path(__file__).resolve().parents[1] / "src"
        if str(src_dir) not in sys.path:
            sys.path.insert(0, str(src_dir))


def _gateway_alive(url: str, timeout: float = 1.0) -> bool:
    try:
        with urllib.request.urlopen(url.rstrip("/") + "/healthz", timeout=timeout) as response:
            return response.status == 200
    except (OSError, urllib.error.URLError, ValueError):
        return False


def _instance_gateway(data_dir: Path, *, alive=_gateway_alive) -> str | None:
    """The running instance's URL; a stale instance.url (crash, killed
    process) is ignored so the caller can start the app instead."""
    try:
        url = (data_dir / "instance.url").read_text(encoding="utf-8").strip()
    except OSError:
        return None
    url = url.removesuffix("/ui/").removesuffix("/")
    return url if url and alive(url) else None


def _gateway_resolver(data_dir: Path, *, launch=None, wait_seconds: float = 30.0, sleep=time.sleep, alive=None):
    """Resolve the running instance on the first MCP tool call.

    When nothing is running and this is the installed macOS app, start it in
    the background (``open -g``) and wait for its ``instance.url``. The MCP
    handshake is answered before this runs, so clients never time out on
    startup."""
    def resolve() -> str:
        explicit = os.environ.get("TUOMIN_GATEWAY_URL")
        if explicit:
            return explicit
        check = alive or _gateway_alive
        found = _instance_gateway(data_dir, alive=check)
        if found:
            return found
        starter = launch if launch is not None else _launch_installed_app
        if starter():
            deadline = time.monotonic() + wait_seconds
            while time.monotonic() < deadline:
                found = _instance_gateway(data_dir, alive=check)
                if found:
                    return found
                sleep(0.5)
        print(
            "[tuomin] Tuomin is not running; open Tuomin first (or set TUOMIN_GATEWAY_URL)",
            file=sys.stderr,
            flush=True,
        )
        return "http://127.0.0.1:8765"
    return resolve


def _launch_installed_app() -> bool:
    if sys.platform != "darwin" or not getattr(sys, "frozen", False):
        return False
    bundle = Path(sys.executable).resolve().parents[2]
    if bundle.suffix != ".app":
        return False
    import subprocess

    try:
        return subprocess.run(["open", "-g", str(bundle)], check=False, timeout=10).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def _run_mcp_stdio(data_dir: Path) -> int:
    """``--mcp-stdio`` mode: a second, uvicorn-free process speaking MCP over
    stdio, forwarding to the already-running instance (or TUOMIN_GATEWAY_URL).
    Lets the frozen bundle serve as an MCP server for local agents without
    opening any port. The ``agent`` app's capability tokens are read from the
    private data directory, so client configs never contain a credential."""
    os.environ.setdefault(
        "TUOMIN_KEYCHAIN_SERVICE", "com.tuomin.workbench.mapping-store.v1"
    )
    from tuomin_gateway.agent_setup import AGENT_APP_ID, load_agent_tokens

    tokens = load_agent_tokens(data_dir) or {}
    os.environ.setdefault("TUOMIN_AGENT_APP", AGENT_APP_ID)
    if tokens.get("redact"):
        os.environ.setdefault("TUOMIN_AGENT_REDACT_TOKEN", tokens["redact"])
    if tokens.get("namespace"):
        os.environ.setdefault("TUOMIN_AGENT_NAMESPACE_TOKEN", tokens["namespace"])
    os.environ.setdefault("TUOMIN_AGENT_SESSION", "1")
    from tuomin_gateway.mcp_stdio import main as mcp_main

    return mcp_main(gateway_resolver=_gateway_resolver(data_dir))


def _multiprocessing_child_entry() -> int | None:
    """冻结二进制被 multiprocessing 当作子进程重启时的正确入口。

    torch/transformers 运行时会在后台创建 multiprocessing 同步原语，其
    resource_tracker 重启子进程的方式是以 `sys.executable -c <code>` 或
    `--multiprocessing-fork` 重新拉起本二进制。缺省会整 app 重启一遍
    （终端出现多余的 "already running" / PYI 报错）。这里把它们路由回
    multiprocessing 机制：freeze_support 处理 --multiprocessing-fork
    （匹配时自身 sys.exit，不会返回）；`-c` 只允许 multiprocessing 自身
    的引导命令（白名单前缀，不接受任意代码）。
    """
    import multiprocessing

    multiprocessing.freeze_support()
    argv = sys.argv[1:]
    if len(argv) == 2 and argv[0] == "-c" and argv[1].startswith("from multiprocessing."):
        exec(compile(argv[1], "<multiprocessing-child>", "exec"), {"__name__": "__main__"})
        return 0
    return None


def _main() -> int:
    child_rc = _multiprocessing_child_entry()
    if child_rc is not None:
        return child_rc
    if "--native-selftest" in sys.argv[1:]:
        return _native_selftest()
    if "--shell-selftest" in sys.argv[1:]:
        _prepare_unfrozen_imports()
        from tuomin_gateway.shell.app import shell_selftest
        return shell_selftest()
    parser = argparse.ArgumentParser(description="Tuomin 本地脱敏工作台")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--shell", action="store_true", help="打开原生窗口（默认）")
    mode.add_argument("--headless", action="store_true", help="使用原有浏览器工作台")
    parser.add_argument("--demo", action="store_true", help="隔离的合成数据演示")
    parser.add_argument("--mcp-stdio", action="store_true")
    parser.add_argument("--ner-selftest", action="store_true")
    parser.add_argument("--dpapi-selftest", action="store_true")
    parser.add_argument("--shutdown-on-stdin-close", action="store_true",
                        help="受父进程管理的无窗口服务：标准输入关闭时正常退出")
    args = parser.parse_args()
    if args.shutdown_on_stdin_close and not args.headless:
        parser.error("--shutdown-on-stdin-close requires --headless")
    data_dir = _data_dir()
    if args.dpapi_selftest:
        return _dpapi_selftest(data_dir)
    if "--ner-selftest" in sys.argv[1:]:
        # 打包自检：打印 NER 真实加载错误（冻结环境排障用）
        _prepare_unfrozen_imports()
        if getattr(sys, "frozen", False):
            os.environ.setdefault(
                "TUOMIN_NER_MODEL_DIR", str(Path(sys._MEIPASS) / "ner-model")
            )
        import traceback

        try:
            from tuomin_gateway.detectors.ner import get_ner_detector

            detector = get_ner_detector()
            spans = detector.detect("发包方：北京华创建设集团有限公司")
            print(f"[ner-selftest] OK, spans={len(spans)}")
            return 0
        except Exception:
            traceback.print_exc()
            return 1
    if "--mcp-stdio" in sys.argv[1:]:
        _prepare_unfrozen_imports()
        return _run_mcp_stdio(data_dir)
    lock_path = data_dir / ("tuomin-demo.lock" if args.demo else "tuomin.lock")
    url_path = data_dir / "instance.url"
    if not _acquire_instance_lock(lock_path, url_path):
        return 0
    atexit.register(_cleanup_instance_files, lock_path, url_path)

    def terminate(signum, frame):
        _cleanup_instance_files(lock_path, url_path)
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, terminate)

    os.environ.setdefault("TUOMIN_MAPPING_DIR", str(data_dir / "mappings"))
    os.environ.setdefault("TUOMIN_DATA_DIR", str(data_dir))
    if getattr(sys, "frozen", False):
        # 开源版不再强制离线授权：打开即用。授权执行只在显式设置
        # TUOMIN_LICENSE_REQUIRED=1 时启用（保留给自建发行方）。
        # A frozen binary has a different code-signing identity than any dev
        # interpreter; sharing the Keychain service name would wedge first
        # access in a securityd consent prompt. Use the app's own item.
        os.environ.setdefault(
            "TUOMIN_KEYCHAIN_SERVICE", "com.tuomin.workbench.mapping-store.v1"
        )
        # 全量大包：NER 模型随包（断网加载；env 可显式覆盖）。
        os.environ.setdefault(
            "TUOMIN_NER_MODEL_DIR", str(Path(sys._MEIPASS) / "ner-model")
        )
    _configure_admin_token(data_dir)
    port, fallback_port = (_pick_free_port(), False) if args.headless else _pick_shell_port()
    ui_url = f"http://127.0.0.1:{port}/ui/"
    _publish_instance_url(url_path, ui_url)

    _prepare_unfrozen_imports()
    import uvicorn

    from tuomin_gateway.service.app import create_app

    if not os.environ.get("TUOMIN_CONFIG"):
        # Desktop launches (native or --headless) have no meaningful working
        # directory. Both modes share one registry next to the protected runtime
        # state, so apps registered in one mode are visible in the other.
        # `tuomin-gateway serve` keeps the repo-relative developer default.
        config_path = data_dir / "apps.json"
        if not config_path.exists():
            config_path.write_text('{"apps": {}}\n', encoding="utf-8")
            _restrict_runtime_path(config_path, is_dir=False)
        os.environ["TUOMIN_CONFIG"] = str(config_path)
        # First run: one editable local dictionary so the dictionary page and
        # the clipboard have something to work with out of the box.
        from tuomin_gateway.shell.defaults import ensure_default_workbench_app
        from tuomin_gateway.agent_setup import ensure_agent_app

        ensure_default_workbench_app(config_path, data_dir)
        # The app AI tools connect through (MCP); tokens stay in data_dir.
        ensure_agent_app(config_path, data_dir)
    app = create_app()
    app.state.demo = args.demo
    if args.demo:
        from fastapi.staticfiles import StaticFiles
        app.mount("/demo/samples", StaticFiles(directory=os.environ["TUOMIN_DEMO_SAMPLE_DIR"]), name="demo-samples")
    from tuomin_gateway.runtime import warm_ner_async

    warm_ner_async()
    print(f"[tuomin] serving {ui_url}", flush=True)
    if not args.headless:
        try:
            from tuomin_gateway.shell.app import run_shell
            return run_shell(app, port=port, admin_token=os.environ["TUOMIN_ADMIN_TOKEN"], fallback_port=fallback_port, demo=args.demo,
                             cleanup=lambda: _cleanup_instance_files(lock_path, url_path))
        except ImportError:
            print("[tuomin] native shell unavailable; install the shell extra or use --headless", file=sys.stderr)
            return 2
        finally:
            _cleanup_instance_files(lock_path, url_path)
    browser_thread = threading.Thread(
        target=_open_browser_when_ready,
        args=(port, ui_url),
        name="tuomin-browser-opener",
        daemon=True,
    )
    browser_thread.start()
    config = uvicorn.Config(
        app,
        host="127.0.0.1",
        port=port,
        log_level="warning",
    )
    server = uvicorn.Server(config)
    if args.shutdown_on_stdin_close:
        def wait_for_parent():
            from tuomin_gateway.runtime import wait_for_parent_input_close
            wait_for_parent_input_close()
            server.should_exit = True
        threading.Thread(target=wait_for_parent, name="tuomin-parent-watch", daemon=True).start()
    try:
        server.run()
    except KeyboardInterrupt:
        # Ctrl+C 是正常退出路径：不打异常堆栈，干净返回（冻结产物里未处理
        # 异常会被 PyInstaller 打成 "Failed to execute script" 报错）。
        print("[tuomin] stopped", flush=True)
    finally:
        _cleanup_instance_files(lock_path, url_path)
    return 0


def main() -> int:
    _prepare_standard_streams()
    if "--demo" in sys.argv[1:]:
        _prepare_unfrozen_imports()
        from tuomin_gateway.shell.demo import demo_environment
        with demo_environment():
            return _main()
    return _main()


if __name__ == "__main__":
    raise SystemExit(main())
