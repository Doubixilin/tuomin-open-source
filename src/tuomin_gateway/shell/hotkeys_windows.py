"""Win32 RegisterHotKey on an owned message thread; no keyboard hooks."""
from __future__ import annotations

import ctypes
from ctypes import wintypes
import threading

DEFAULT_SHORTCUTS = {"redact": "ctrl+alt+d", "refill": "ctrl+alt+r"}
_MODIFIERS = {"alt": 1, "ctrl": 2, "control": 2, "shift": 4, "win": 8, "windows": 8}
_WM_HOTKEY, _WM_QUIT = 0x0312, 0x0012
_KEYS = {**{c: ord(c.upper()) for c in 'abcdefghijklmnopqrstuvwxyz0123456789'},
         **{f'f{n}': 0x6F + n for n in range(1, 25)}, 'space': 0x20,
         ';': 0xBA, '=': 0xBB, ',': 0xBC, '-': 0xBD, '.': 0xBE, '/': 0xBF,
         '`': 0xC0, '[': 0xDB, '\\': 0xDC, ']': 0xDD, "'": 0xDE}


def parse_shortcut(value):
    if value is None or value is False or value == "":
        return None
    if not isinstance(value, str):
        raise ValueError("快捷键格式应为 ctrl+alt+d，可留空关闭")
    parts = [part.strip().lower() for part in value.split("+")]
    key = parts[-1]
    if key not in _KEYS:
        raise ValueError("快捷键仅支持字母、数字、F1–F24、空格和常用标点")
    modifiers = 0
    for part in parts[:-1]:
        if part not in _MODIFIERS:
            raise ValueError("快捷键修饰键仅支持 ctrl、alt、shift、win")
        modifiers |= _MODIFIERS[part]
    if key not in {f'f{n}' for n in range(13, 25)} and not modifiers & (1 | 2 | 8):
        raise ValueError("快捷键至少需要 ctrl、alt 或 win")
    return _KEYS[key], modifiers


def normalize_shortcut(value):
    parsed = parse_shortcut(value)
    if parsed is None:
        return None
    _, modifiers = parsed
    key = value.split('+')[-1].strip().lower()
    return '+'.join([*[name for name in ('ctrl', 'alt', 'shift', 'win') if modifiers & _MODIFIERS[name]], key])


def format_shortcut(value):
    try:
        value = normalize_shortcut(value)
    except ValueError:
        return ''
    return '+'.join(part.upper() if len(part) == 1 or part.startswith('f') else part.title()
                    for part in value.split('+')) if value else ''


class _WindowsHotkeys:
    def __init__(self):
        self.user = ctypes.WinDLL("user32", use_last_error=True)
        self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        for name, result, args in (
            ("RegisterHotKey", wintypes.BOOL, [wintypes.HWND, ctypes.c_int, wintypes.UINT, wintypes.UINT]),
            ("UnregisterHotKey", wintypes.BOOL, [wintypes.HWND, ctypes.c_int]),
            ("PeekMessageW", wintypes.BOOL, [ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT, wintypes.UINT]),
            ("GetMessageW", ctypes.c_int, [ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT]),
            ("PostThreadMessageW", wintypes.BOOL, [wintypes.DWORD, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]),
        ):
            function = getattr(self.user, name)
            function.restype, function.argtypes = result, args
        self.kernel.GetCurrentThreadId.restype = wintypes.DWORD
        self.kernel.GetCurrentThreadId.argtypes = []

    def create_queue(self):
        message = wintypes.MSG()
        self.user.PeekMessageW(ctypes.byref(message), None, 0, 0, 0)
        return self.kernel.GetCurrentThreadId()

    def register(self, identifier, key, modifiers):
        return bool(self.user.RegisterHotKey(None, identifier, modifiers | 0x4000, key))

    def unregister(self, identifier):
        return bool(self.user.UnregisterHotKey(None, identifier))

    def message(self):
        message = wintypes.MSG()
        result = self.user.GetMessageW(ctypes.byref(message), None, 0, 0)
        if result == -1:
            raise RuntimeError("全局快捷键事件读取失败")
        return (_WM_QUIT, 0) if result == 0 else (message.message, int(message.wParam))

    def stop(self, thread_id):
        if not self.user.PostThreadMessageW(thread_id, _WM_QUIT, 0, 0):
            raise RuntimeError("无法停止全局快捷键")


class Hotkeys:
    def __init__(self, callbacks, *, api=None):
        self.callbacks = dict(callbacks)
        self._api = api
        self._thread = None
        self._thread_id = None
        self._lock = threading.RLock()
        self._stop = threading.Event()

    def configure(self, settings=None):
        settings = DEFAULT_SHORTCUTS if settings is None else dict(settings)
        with self._lock:
            self.close()
            errors, parsed, occupied = {}, {}, set()
            for name in self.callbacks:
                try:
                    shortcut = parse_shortcut(settings.get(name))
                    if shortcut and shortcut in occupied:
                        raise ValueError("两个动作不能使用同一快捷键")
                    if shortcut:
                        occupied.add(shortcut)
                        parsed[name] = shortcut
                except ValueError as error:
                    errors[name] = str(error)
            if not parsed:
                return errors
            if self._api is None:
                self._api = _WindowsHotkeys()
            ready = threading.Event()
            self._stop.clear()
            self._thread = threading.Thread(target=self._run, args=(parsed, errors, ready),
                                            name="tuomin-hotkeys", daemon=True)
            self._thread.start()
            if not ready.wait(5):
                self.close()
                raise RuntimeError("全局快捷键初始化超时")
            return dict(errors)

    def _run(self, parsed, errors, ready):
        registered = {}
        try:
            self._thread_id = self._api.create_queue()
            for identifier, (name, (key, modifiers)) in enumerate(parsed.items(), 1):
                if self._stop.is_set():
                    return
                if self._api.register(identifier, key, modifiers):
                    registered[identifier] = name
                else:
                    errors[name] = "快捷键已被占用或系统拒绝注册"
            ready.set()
            while registered and not self._stop.is_set():
                message, identifier = self._api.message()
                if message == _WM_QUIT:
                    break
                name = registered.get(identifier) if message == _WM_HOTKEY else None
                if name:
                    try:
                        self.callbacks[name]()
                    except Exception:
                        pass  # Never expose raw callback details from a native event.
        except Exception:
            for name in parsed:
                errors.setdefault(name, "系统无法启用全局快捷键")
        finally:
            for identifier in registered:
                self._api.unregister(identifier)
            ready.set()

    def close(self):
        with self._lock:
            worker = self._thread
            if worker is None:
                return
            self._stop.set()
            if worker.is_alive() and self._thread_id is not None:
                try:
                    self._api.stop(self._thread_id)
                except RuntimeError:
                    # The worker may have exited between is_alive() and the
                    # post. Joining below distinguishes that benign race from
                    # a message thread which really failed to stop.
                    pass
            if worker is threading.current_thread():
                return
            worker.join(timeout=5)
            if worker.is_alive():
                raise RuntimeError("全局快捷键尚未退出，请重新打开工作台")
            self._thread = self._thread_id = None
