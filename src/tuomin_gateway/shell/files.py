"""Finder / Dock / menu-bar file entry points for the native shell.

Files arriving by "Open With", a drop on the Dock icon, a drop on the menu-bar
icon, or the Finder service "用 Tuomin 脱敏" are redacted through the local
document API and saved next to the originals. Work runs on one background
thread; the user is told the outcome in a notification whose action reveals
the result in Finder.
"""
from __future__ import annotations

import queue
import subprocess
import sys
import threading
from pathlib import Path
from typing import Callable, Iterable

from tuomin_gateway.file_redact import http_post, redact_file, summarize, supported


class FileRedactor:
    def __init__(self, base_url: str, admin_token: str, *, app_id: Callable[[], str | None],
                 notify: Callable[[str, list[str]], None], post=None):
        self._post = post or http_post(base_url, admin_token)
        self._app_id = app_id
        self._notify = notify
        self._queue: queue.Queue = queue.Queue()
        self._worker: threading.Thread | None = None
        self._lock = threading.Lock()

    def submit(self, paths: Iterable[str]) -> int:
        """Queue files; returns how many are supported. Never blocks the caller."""
        chosen = [str(p) for p in paths if p and supported(p)]
        rejected = [str(p) for p in paths if p and not supported(p)]
        if not chosen:
            if rejected:
                self._notify("不支持这种文件：可拖入 Word、Excel、PPT、PDF 或文本文件", [])
            return 0
        self._queue.put(chosen)
        with self._lock:
            if self._worker is None or not self._worker.is_alive():
                self._worker = threading.Thread(target=self._drain, name="tuomin-file-redact", daemon=True)
                self._worker.start()
        return len(chosen)

    def _drain(self) -> None:
        while True:
            try:
                batch = self._queue.get(timeout=0.5)
            except queue.Empty:
                return
            self.run(batch)

    def run(self, paths: list[str]) -> list[dict]:
        app_id = self._app_id()
        results = [redact_file(path, post=self._post, app_id=app_id) for path in paths]
        outputs = [r["output"] for r in results if r["status"] == "ok" and r["output"]]
        self._notify(summarize(results), outputs)
        return results


def reveal(path: str) -> None:
    if sys.platform == 'win32':
        subprocess.Popen(['explorer.exe', '/select,', str(Path(path).resolve())],
                         creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    else:
        subprocess.run(["open", "-R", str(path)], check=False, timeout=10)


# --- Cocoa hooks (macOS only; every function is a no-op elsewhere) -----------

_provider = None
_drop_target = None


def install_open_handler(handler: Callable[[list[str]], None]) -> bool:
    """Route Dock drops / "Open With" to ``handler`` via pywebview's app delegate."""
    try:
        import objc
        from webview.platforms import cocoa
    except Exception:
        return False
    delegate = cocoa.BrowserView.AppDelegate

    def application_openFiles_(self, app, filenames):  # noqa: N802 - ObjC selector
        handler([str(name) for name in filenames])
        try:
            app.replyToOpenOrPrint_(0)  # NSApplicationDelegateReplySuccess
        except Exception:
            pass

    def application_openURLs_(self, app, urls):  # noqa: N802 - ObjC selector
        handler([str(url.path()) for url in urls if url.isFileURL()])

    try:
        objc.classAddMethods(delegate, [application_openFiles_, application_openURLs_])
    except Exception:
        return False
    return True


def _file_paths(pasteboard) -> list[str]:
    from AppKit import NSURL
    from Foundation import NSDictionary

    options = NSDictionary.dictionaryWithObject_forKey_(True, "NSPasteboardURLReadingFileURLsOnlyKey")
    urls = pasteboard.readObjectsForClasses_options_([NSURL], options) or []
    return [str(url.path()) for url in urls]


def install_service_provider(handler: Callable[[list[str]], None]) -> bool:
    """Finder「服务 → 用 Tuomin 脱敏」(declared as NSServices in Info.plist)."""
    global _provider
    try:
        import objc
        from AppKit import NSApplication, NSObject, NSUpdateDynamicServices
    except Exception:
        return False

    class TuominServiceProvider(NSObject):
        @objc.typedSelector(b"v@:@@o^@")
        def redactFiles_userData_error_(self, pasteboard, user_data, error):  # noqa: N802
            try:
                handler(_file_paths(pasteboard))
            except Exception:
                pass
            return None

    _provider = TuominServiceProvider.alloc().init()
    NSApplication.sharedApplication().setServicesProvider_(_provider)
    try:
        NSUpdateDynamicServices()
    except Exception:
        pass
    return True


def install_status_drop(status_item, handler: Callable[[list[str]], None]) -> bool:
    """Accept files dropped on the menu-bar icon (its window forwards drag
    messages to its delegate)."""
    global _drop_target
    try:
        import objc  # noqa: F401
        from AppKit import NSObject, NSPasteboardTypeFileURL
    except Exception:
        return False

    class TuominStatusDropTarget(NSObject):
        def draggingEntered_(self, info):  # noqa: N802
            return 1 if _file_paths(info.draggingPasteboard()) else 0  # NSDragOperationCopy

        def draggingUpdated_(self, info):  # noqa: N802
            return 1

        def prepareForDragOperation_(self, info):  # noqa: N802
            return True

        def performDragOperation_(self, info):  # noqa: N802
            paths = _file_paths(info.draggingPasteboard())
            if paths:
                handler(paths)
            return bool(paths)

    try:
        window = status_item.button().window()
        _drop_target = TuominStatusDropTarget.alloc().init()
        window.registerForDraggedTypes_([NSPasteboardTypeFileURL])
        window.setDelegate_(_drop_target)
    except Exception:
        return False
    return True
