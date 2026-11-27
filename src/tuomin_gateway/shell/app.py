"""Own the native run loop while the unchanged gateway runs in a thread."""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import secrets
import signal
import os
import sys
from pathlib import Path
import threading
import time

from .http import LocalClient


def platform_backend():
    """Load only the current desktop's adapters; service imports stay GUI-free."""
    from types import SimpleNamespace
    if sys.platform == 'win32':
        from .pasteboard_windows import Pasteboard
        from .hotkeys_windows import Hotkeys
        from .notify_windows import Notifier
        from .statusbar_windows import StatusBar
        return SimpleNamespace(Pasteboard=Pasteboard, Hotkeys=Hotkeys,
            Notifier=Notifier, StatusBar=StatusBar, gui='edgechromium',
            dispatch=lambda callback: callback())
    if sys.platform == 'darwin':
        from PyObjCTools import AppHelper
        from .pasteboard_macos import Pasteboard
        from .hotkeys import Hotkeys
        from .notify import Notifier
        from .statusbar import StatusBar
        return SimpleNamespace(Pasteboard=Pasteboard, Hotkeys=Hotkeys,
            Notifier=Notifier, StatusBar=StatusBar, gui='cocoa',
            dispatch=AppHelper.callAfter)
    raise ImportError('Native shell supports Windows and macOS; use --headless')


def notify_clipboard_result(notifier, bridge, controller, text, action=None):
    on_select = None
    if action == "view" and controller.last_view is not None:
        snapshot = deepcopy(controller.last_view)
        on_select = lambda: bridge._background(lambda: bridge._open(snapshot))
    notifier.notify(text, action=action, on_select=on_select)


def recent_external_calls(calls: list[dict], *, now: float | None = None) -> bool:
    now = time.time() if now is None else now
    for call in calls:
        if call.get("entry") in {"workbench", "clipboard"}:
            continue
        timestamp = call.get("ts", call.get("created_at", call.get("timestamp")))
        try:
            if isinstance(timestamp, str):
                timestamp = datetime.fromisoformat(timestamp.replace("Z", "+00:00")).replace(tzinfo=timezone.utc).timestamp()
            age = now - float(timestamp)
        except (TypeError, ValueError):
            continue
        if 0 <= age <= 10:
            return True
    return False


def run_shell(app, *, port: int, admin_token: str, fallback_port: bool = False, selftest: bool = False, demo: bool = False,
              cleanup=None) -> int:
    # Lazy imports keep `serve`, headless builds and service tests GUI-free.
    import uvicorn
    import webview
    backend = platform_backend()

    base_url = f"http://127.0.0.1:{port}"
    client = LocalClient(base_url, timeout=2)
    # Quitting must not wait for an in-flight redaction to finish.
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", access_log=False,
                                           timeout_graceful_shutdown=2))
    worker = threading.Thread(target=server.run, name="tuomin-gateway", daemon=True)
    worker.start()
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        status, _ = client.request("GET", "/healthz")
        if status == 200:
            break
        if not worker.is_alive():
            return 1
        time.sleep(0.05)
    else:
        server.should_exit = True
        worker.join(timeout=5)
        return 1

    try:
        return _run_desktop(backend, webview, client, server, base_url=base_url,
                            port=port, admin_token=admin_token, fallback_port=fallback_port,
                            selftest=selftest, demo=demo, cleanup=cleanup)
    finally:
        # Includes failures before webview.start (for example corrupt private
        # settings). Never leave the local server running after shell failure.
        server.should_exit = True
        worker.join(timeout=10)


def _run_desktop(backend, webview, client, server, *, base_url, port, admin_token,
                 fallback_port, selftest, demo, cleanup=None):
    from .autostart import LoginItem
    from .window import WorkbenchWindow

    stopped = threading.Event()
    ready = threading.Event()
    failed = threading.Event()

    bridge = notifier = hotkeys = None
    pasteboard = backend.Pasteboard()
    shutdown_lock = threading.RLock()

    def shutdown():
        with shutdown_lock:
            if stopped.is_set():
                return
            stopped.set()
            server.should_exit = True
            for component in (hotkeys, notifier):
                if component:
                    try:
                        component.close()
                    except Exception:
                        failed.set()
            try:
                bar.stop()
            finally:
                window.request_exit()

    def check_recent():
        status, result = client.request("GET", "/api/v1/documents/calls?limit=100", token=admin_token, admin=True)
        if status != 200:
            return None
        return recent_external_calls(result.get("calls", []))

    def prepare_exit():
        # The process ends right after this (Cocoa terminate); clean up first.
        stopped.set()
        for step in (lambda: hotkeys and hotkeys.close(), lambda: notifier and notifier.close(), bar.stop):
            try:
                step()
            except Exception:
                pass
        server.should_exit = True
        if cleanup is not None:
            try:
                cleanup()
            except Exception:
                pass

    def hard_exit():
        try:
            if cleanup is not None:
                cleanup()
        finally:
            exit_process()

    def exit_process():
        import sys
        for stream in (sys.stdout, sys.stderr):
            try:
                stream.flush()
            except Exception:
                pass
        os._exit(0)

    from .quitting import QuitCoordinator
    quitter = QuitCoordinator(
        check_recent=check_recent,
        confirm=lambda unknown: bar.confirm_exit(unknown),
        prepare_exit=prepare_exit,
        reply=lambda proceed: (__import__("AppKit").NSApplication.sharedApplication().replyToApplicationShouldTerminate_(proceed)
                               if sys.platform == 'darwin' else None),
        exit_process=exit_process,
        call_after=backend.dispatch,
        status=lambda text: bar.set_result(text),
        hard_exit=hard_exit,
    )

    def quit_requested():
        if sys.platform == 'win32':
            quitter.should_terminate()
            return
        # Same path as the Dock menu, ⌘Q and logout: ask Cocoa to terminate.
        from AppKit import NSApplication
        backend.dispatch(lambda: NSApplication.sharedApplication().terminate_(None))

    def copy_gateway():
        pasteboard.write_text(base_url)

    def startup_error(*_):
        failed.set()
        ready.set()

    if not selftest:
        from .registration import ClipboardRegistration
        from .clipboard import ClipboardController
        from .storage import SessionFile
        from .bridge import ShellBridge
        from tuomin_gateway.service.limits import max_text_chars
        directory = Path(os.environ['TUOMIN_DATA_DIR'])
        registration = ClipboardRegistration(LocalClient(base_url, timeout=120), admin_token, directory)
        controller = ClipboardController(pasteboard, LocalClient(base_url, timeout=120), registration.tokens, SessionFile(directory), lambda text,action=None: notify_clipboard_result(notifier, bridge, controller, text, action), max_chars=max_text_chars())
        bridge = ShellBridge(directory, registration, controller, demo=demo)
        try:
            registration.configure(bridge._settings['dictionary_source'])
        except (RuntimeError, ValueError):
            controller.configuration_error = '词典来源不可用，请在设置中重新选择；脱敏已暂停'

        controller.idle_hours = bridge._settings['idle_hours']
    files = None
    pending_files: list[str] = []

    def receive_files(paths):
        # Finder / Dock events can arrive before the notifier is up; queue them.
        if files is None:
            pending_files.extend(paths)
            return
        files.submit(paths)

    if not selftest and sys.platform == 'darwin':
        from .files import install_open_handler
        install_open_handler(receive_files)
    if not selftest and sys.platform == 'darwin':
        from .window import install_terminate_handler
        install_terminate_handler(quitter.should_terminate)
    window = WorkbenchWindow(base_url, admin_token, demo=demo, hidden=selftest, on_ready=ready.set, on_error=startup_error, js_api=bridge)
    bar_options = {}
    if sys.platform == 'win32':
        bar_options['confirm'] = lambda: window.window.create_confirmation_dialog(
            '其他工具最近仍在使用 Tuomin', '退出后这些工具将无法连接本地网关。是否退出？')
    else:
        bar_options['on_files'] = None if selftest else receive_files
    bar = backend.StatusBar(on_open=window.show, on_copy=copy_gateway, on_quit=quit_requested,
                            autostart=None if demo else LoginItem(), **bar_options)

    if bridge:
        bridge._window, bridge._bar, bridge._login = window, bar, bar.autostart
        notifier = backend.Notifier(bar, on_action=lambda action: bridge._background(lambda: bridge._action(action)))
        from .hotkeys import ACTIONS
        hotkeys = backend.Hotkeys({name: (lambda selected=name: bridge._background(lambda: bridge._action(selected))) for name in ACTIONS})
        bridge._hotkeys = hotkeys
        try:
            from .selection import Keyboard, SelectionFlow
            if sys.platform == 'win32':
                from .selection_windows import Keyboard
            keyboard = Keyboard()
            bridge._keyboard = keyboard
            bridge._selection = SelectionFlow(controller.board, controller, keyboard,
                                              lambda text, action=None: notify_clipboard_result(notifier, bridge, controller, text, action))
        except Exception:
            pass  # selection actions stay unavailable; clipboard actions still work
        controller.on_change = bridge._refresh_menu

    def apply_dock_icon():
        # Frozen bundles get the icon from Info.plist; source runs would show
        # the Python rocket otherwise.
        try:
            from .assets import asset_path
            path = asset_path("tuomin-1024.png")
            if path is None:
                return
            from AppKit import NSApplication, NSImage
            image = NSImage.alloc().initWithContentsOfFile_(str(path))
            if image is not None:
                NSApplication.sharedApplication().setApplicationIconImage_(image)
        except Exception:
            pass

    def set_files(redactor):
        nonlocal files
        files = redactor
        if pending_files:
            queued = list(pending_files)
            pending_files.clear()
            redactor.submit(queued)

    def startup():
        if sys.platform == 'darwin':
            backend.dispatch(apply_dock_icon)
        try:
            bar.start()
        except Exception:
            failed.set()
            backend.dispatch(shutdown)
            return
        if selftest:
            if not bar.ready.wait(15) or not ready.wait(15):
                failed.set()
            backend.dispatch(shutdown)
            return
        if not bar.ready.wait(15) or not ready.wait(20) or failed.is_set():
            failed.set()
            backend.dispatch(shutdown)
            return
        if bridge:
            notifier.start()
            from .files import FileRedactor, install_service_provider, reveal

            def file_result(text, outputs):
                notifier.notify(text, action="reveal" if outputs else None, action_title="查看",
                                on_select=(lambda: reveal(outputs[0])) if outputs else None)

            def file_app():
                status, result = client.request("GET", "/admin/apps", token=admin_token, admin=True)
                apps = {a.get("app_id"): a for a in (result.get("apps", []) if status == 200 else [])}
                chosen = bridge._settings.get("dictionary_source") or "workbench"
                return chosen if chosen in apps else ("workbench" if "workbench" in apps else None)

            nonlocal_files = FileRedactor(base_url, admin_token, app_id=file_app, notify=file_result)
            if sys.platform == 'darwin':
                backend.dispatch(lambda: install_service_provider(receive_files))
            else:
                def choose_files():
                    chosen = window.window.create_file_dialog(webview.OPEN_DIALOG, allow_multiple=True)
                    if chosen:
                        receive_files(chosen)
                bar.set_action('redact-files', '选择文件脱敏…', lambda: bridge._background(choose_files))
            set_files(nonlocal_files)
            try:
                bridge._errors = hotkeys.configure(bridge.hotkey_bindings(bridge._settings['hotkeys']))
            except RuntimeError:
                bridge._errors = {name: '全局快捷键不可用，请使用托盘或菜单栏操作' for name in ACTIONS}
            bar.set_action('clipboard-result', '查看最近结果', lambda:bridge._background(lambda:bridge._action('view')))
            bridge._refresh_menu()
            if controller.configuration_error:
                bar.set_result(controller.configuration_error)
        while not stopped.is_set():
            status, health = client.request("GET", "/api/v1/readiness?app_id=clipboard", token=registration.tokens["namespace"])
            title = ("演示模式 · " if demo else "") + f"Tuomin 运行中 · 端口 {port}"
            if fallback_port:
                title += "（默认端口占用，已回退）"
            detail = "检测器就绪" if status == 200 and health.get("detectors", {}).get("ner", {}).get("loadable", False) else "检测器未全部就绪"
            bar.update(title, detail)
            stopped.wait(5)

    def force_quit():
        quitter.force()

    previous_signals = {name: signal.getsignal(name) for name in (signal.SIGTERM, signal.SIGINT)}
    for name in previous_signals:
        # Never raise SystemExit across an ObjC callback: Cocoa would abort
        # before the demo context and instance-lock cleanup can run.
        signal.signal(name, lambda signum, frame: backend.dispatch(shutdown if selftest else force_quit))
    try:
        webview.start(startup, gui=backend.gui, debug=False)
    finally:
        for name, handler in previous_signals.items():
            signal.signal(name, handler)
        stopped.set()
        for component in (hotkeys, notifier):
            if component:
                try:
                    component.close()
                except Exception:
                    failed.set()
        bar.stop()
        server.should_exit = True
    if selftest:
        print("[shell-selftest] " + ("failed" if failed.is_set() else "OK"), flush=True)
    return 1 if failed.is_set() else 0


def shell_selftest() -> int:
    from fastapi import FastAPI
    import socket
    app = FastAPI()
    @app.get("/healthz")
    def health():
        return {"status": "ok"}
    from fastapi.responses import HTMLResponse
    @app.get("/ui/", response_class=HTMLResponse)
    def page():
        return "<!doctype html><title>Tuomin selftest</title><p>Native shell check</p>"
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    return run_shell(app, port=port, admin_token=secrets.token_urlsafe(32), selftest=True)
