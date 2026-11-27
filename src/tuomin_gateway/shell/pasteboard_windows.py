"""Explicit Win32 Unicode clipboard access; no listener or background polling.

Privacy formats follow Microsoft's clipboard-formats contract. They opt out of
Windows history/sync, not arbitrary third-party clipboard readers.
"""
from __future__ import annotations

from contextlib import contextmanager
import ctypes
from ctypes import wintypes
import threading
import time

TEXT_TYPE = 13  # CF_UNICODETEXT
FILE_TYPE = 15  # CF_HDROP
PRIVACY_TYPES = (
    "ExcludeClipboardContentFromMonitorProcessing",
    "CanIncludeInClipboardHistory",
    "CanUploadToCloudClipboard",
)
_MAX_BYTES = 16 * 1024 * 1024


class _WindowsClipboard:
    def __init__(self):
        self.user = ctypes.WinDLL("user32", use_last_error=True)
        self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        specs = {
            "CreateWindowExW": (wintypes.HWND, [wintypes.DWORD, wintypes.LPCWSTR,
                wintypes.LPCWSTR, wintypes.DWORD, ctypes.c_int, ctypes.c_int,
                ctypes.c_int, ctypes.c_int, wintypes.HWND, wintypes.HMENU,
                wintypes.HINSTANCE, wintypes.LPVOID]),
            "DestroyWindow": (wintypes.BOOL, [wintypes.HWND]),
            "OpenClipboard": (wintypes.BOOL, [wintypes.HWND]),
            "CloseClipboard": (wintypes.BOOL, []),
            "EmptyClipboard": (wintypes.BOOL, []),
            "GetClipboardSequenceNumber": (wintypes.DWORD, []),
            "IsClipboardFormatAvailable": (wintypes.BOOL, [wintypes.UINT]),
            "GetClipboardData": (wintypes.HANDLE, [wintypes.UINT]),
            "SetClipboardData": (wintypes.HANDLE, [wintypes.UINT, wintypes.HANDLE]),
            "RegisterClipboardFormatW": (wintypes.UINT, [wintypes.LPCWSTR]),
        }
        for name, (result, args) in specs.items():
            function = getattr(self.user, name)
            function.restype, function.argtypes = result, args
        for name, result, args in (
            ("GlobalAlloc", wintypes.HGLOBAL, [wintypes.UINT, ctypes.c_size_t]),
            ("GlobalLock", wintypes.LPVOID, [wintypes.HGLOBAL]),
            ("GlobalUnlock", wintypes.BOOL, [wintypes.HGLOBAL]),
            ("GlobalSize", ctypes.c_size_t, [wintypes.HGLOBAL]),
            ("GlobalFree", wintypes.HGLOBAL, [wintypes.HGLOBAL]),
        ):
            function = getattr(self.kernel, name)
            function.restype, function.argtypes = result, args

    def create_owner(self):
        # OpenClipboard(NULL) + EmptyClipboard gives a NULL owner and makes
        # SetClipboardData fail. A hidden message-only STATIC window needs no
        # custom window procedure and belongs to this transaction's thread.
        owner = self.user.CreateWindowExW(0, "STATIC", "TuominClipboard", 0,
            0, 0, 0, 0, wintypes.HWND(-3), None, None, None)
        if not owner:
            raise RuntimeError("无法初始化系统剪贴板")
        return owner

    def destroy_owner(self, owner):
        self.user.DestroyWindow(owner)

    def open(self, owner):
        return bool(self.user.OpenClipboard(owner))

    def close(self):
        if not self.user.CloseClipboard():
            raise RuntimeError("无法关闭系统剪贴板")

    def available(self, kind):
        return bool(self.user.IsClipboardFormatAvailable(kind))

    def change_count(self):
        return int(self.user.GetClipboardSequenceNumber())

    def text(self):
        handle = self.user.GetClipboardData(TEXT_TYPE)
        if not handle:
            raise RuntimeError("无法读取系统剪贴板")
        size = self.kernel.GlobalSize(handle)
        if size < 2 or size > _MAX_BYTES or size % 2:
            raise RuntimeError("剪贴板文本格式无效或内容过长")
        pointer = self.kernel.GlobalLock(handle)
        if not pointer:
            raise RuntimeError("无法读取系统剪贴板")
        try:
            raw = ctypes.string_at(pointer, size)
        finally:
            self.kernel.GlobalUnlock(handle)
        # GlobalSize may include allocator padding after the terminator; that
        # padding is not text and must never be decoded or exposed.
        end = next((offset for offset in range(0, len(raw), 2)
                    if raw[offset:offset + 2] == b"\0\0"), None)
        if end is None:
            raise RuntimeError("剪贴板文本格式无效")
        try:
            return raw[:end].decode("utf-16-le") or None
        except UnicodeError:
            raise RuntimeError("剪贴板文本格式无效") from None

    def register(self, name):
        value = self.user.RegisterClipboardFormatW(name)
        if not value:
            raise RuntimeError("无法设置剪贴板隐私标记")
        return value

    def empty(self):
        if not self.user.EmptyClipboard():
            raise RuntimeError("无法清空系统剪贴板")

    def set_data(self, kind, data):
        handle = self.kernel.GlobalAlloc(0x0002, len(data))  # GMEM_MOVEABLE
        if not handle:
            raise RuntimeError("无法准备剪贴板内容")
        transferred = False
        try:
            pointer = self.kernel.GlobalLock(handle)
            if not pointer:
                raise RuntimeError("无法准备剪贴板内容")
            try:
                ctypes.memmove(pointer, data, len(data))
            finally:
                self.kernel.GlobalUnlock(handle)
            if not self.user.SetClipboardData(kind, handle):
                raise RuntimeError("无法写入系统剪贴板")
            transferred = True  # Windows owns the allocation after success.
        finally:
            if not transferred:
                self.kernel.GlobalFree(handle)


class Pasteboard:
    def __init__(self, *, api=None, sleep=time.sleep):
        self._api, self._sleep = api, sleep
        self._lock = threading.RLock()

    def change_count(self):
        with self._lock:
            if self._api is None:
                self._api = _WindowsClipboard()
            return self._api.change_count()

    def clear(self):
        with self._lock, self._opened() as api:
            api.empty()

    @contextmanager
    def _opened(self):
        if self._api is None:
            self._api = _WindowsClipboard()
        api = self._api
        owner, opened = api.create_owner(), False
        try:
            for delay in (0, 0.01, 0.025, 0.05, 0.1):
                if delay:
                    self._sleep(delay)
                if api.open(owner):
                    opened = True
                    break
            if not opened:
                raise RuntimeError("剪贴板正被其他应用占用，请重试")
            yield api
        finally:
            try:
                if opened:
                    api.close()
            finally:
                api.destroy_owner(owner)

    def read_text(self):
        with self._lock, self._opened() as api:
            if api.available(FILE_TYPE) or not api.available(TEXT_TYPE):
                return None
            return api.text()

    def write_text(self, text, concealed=False):
        if not isinstance(text, str):
            raise TypeError("剪贴板写入内容必须是文本")
        if "\0" in text:
            raise ValueError("剪贴板文本不能包含空字符")
        encoded = (text + "\0").encode("utf-16-le")
        if len(encoded) > _MAX_BYTES:
            raise ValueError("剪贴板文本过长，请使用工作台")
        with self._lock, self._opened() as api:
            # Register all formats before clearing the old clipboard. Any
            # failed privacy marker aborts before restored text is installed.
            privacy = [api.register(name) for name in PRIVACY_TYPES] if concealed else []
            api.empty()
            for kind in privacy:
                api.set_data(kind, b"\0\0\0\0")  # serialized DWORD zero
            api.set_data(TEXT_TYPE, encoded)
