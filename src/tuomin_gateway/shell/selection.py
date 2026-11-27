"""Act on the selected text in the frontmost app: copy → redact/refill → paste.

Only runs when the user presses a shortcut (or picks the menu item); nothing
watches the clipboard. Simulating ⌘C / ⌘V needs the macOS Accessibility
permission; without it the user is guided to System Settings and the ordinary
clipboard actions keep working. Replace-style actions put the user's previous
clipboard back afterwards, so restored originals never linger there.
"""
from __future__ import annotations

import ctypes
import ctypes.util
import time

KEY_C, KEY_V = 8, 9
FLAG_CMD = 0x100000
_MODIFIER_MASK = 0x100000 | 0x80000 | 0x40000 | 0x20000  # cmd, alt, ctrl, shift
ACCESSIBILITY_SETTINGS = "x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility"


class Keyboard:
    """CGEvent keystrokes and AX trust checks through ctypes (no extra deps)."""

    def __init__(self):
        services = ctypes.util.find_library("ApplicationServices")
        foundation = ctypes.util.find_library("CoreFoundation")
        if not services or not foundation:
            raise RuntimeError("系统不支持模拟按键")
        self.lib = ctypes.CDLL(services)
        self.cf = ctypes.CDLL(foundation)
        self.lib.CGEventCreateKeyboardEvent.restype = ctypes.c_void_p
        self.lib.CGEventCreateKeyboardEvent.argtypes = [ctypes.c_void_p, ctypes.c_uint16, ctypes.c_bool]
        self.lib.CGEventSetFlags.argtypes = [ctypes.c_void_p, ctypes.c_uint64]
        self.lib.CGEventPost.argtypes = [ctypes.c_uint32, ctypes.c_void_p]
        self.lib.CGEventSourceFlagsState.restype = ctypes.c_uint64
        self.lib.CGEventSourceFlagsState.argtypes = [ctypes.c_int32]
        self.lib.AXIsProcessTrusted.restype = ctypes.c_bool
        self.lib.AXIsProcessTrustedWithOptions.restype = ctypes.c_bool
        self.lib.AXIsProcessTrustedWithOptions.argtypes = [ctypes.c_void_p]
        self.cf.CFRelease.argtypes = [ctypes.c_void_p]

    def trusted(self) -> bool:
        return bool(self.lib.AXIsProcessTrusted())

    def prompt(self) -> bool:
        """Ask macOS to show its Accessibility prompt for this app."""
        try:
            import objc
            from Foundation import NSDictionary
            options = NSDictionary.dictionaryWithObject_forKey_(True, "AXTrustedCheckOptionPrompt")
            return bool(self.lib.AXIsProcessTrustedWithOptions(objc.pyobjc_id(options)))
        except Exception:
            return self.trusted()

    def modifiers_down(self) -> bool:
        return bool(self.lib.CGEventSourceFlagsState(1) & _MODIFIER_MASK)  # HID system state

    def press(self, keycode: int, flags: int) -> None:
        for down in (True, False):
            event = self.lib.CGEventCreateKeyboardEvent(None, keycode, down)
            if not event:
                raise RuntimeError("无法创建按键事件")
            try:
                self.lib.CGEventSetFlags(event, flags)
                self.lib.CGEventPost(0, event)  # kCGHIDEventTap
            finally:
                self.cf.CFRelease(event)


def open_accessibility_settings() -> None:
    import subprocess

    subprocess.run(["open", ACCESSIBILITY_SETTINGS], check=False, timeout=10)


class SelectionFlow:
    def __init__(self, board, controller, keyboard, notify, *, sleep=time.sleep, clock=time.monotonic,
                 copy_timeout: float = 0.8, paste_settle: float = 0.25):
        self.board, self.controller, self.keyboard, self.notify = board, controller, keyboard, notify
        self.sleep, self.clock = sleep, clock
        self.copy_timeout, self.paste_settle = copy_timeout, paste_settle

    def _wait(self, condition, timeout: float, step: float = 0.02) -> bool:
        deadline = self.clock() + timeout
        while self.clock() < deadline:
            if condition():
                return True
            self.sleep(step)
        return condition()

    def _restore(self, previous) -> None:
        if isinstance(previous, str):
            self.board.write_text(previous)
        else:
            self.board.clear()

    def run(self, action: str) -> bool:
        if action not in {"redact_selection", "redact_paste", "refill_paste"}:
            raise ValueError("unsupported selection action")
        if not self.keyboard.trusted():
            self.keyboard.prompt()
            self.notify("需要在「系统设置 → 隐私与安全性 → 辅助功能」里允许 Tuomin，才能读取和替换选中文字；授权后再按一次快捷键", "accessibility")
            return False
        # The shortcut's own modifiers would otherwise turn ⌘C into ⌥⌘C.
        if not self._wait(lambda: not self.keyboard.modifiers_down(), 1.0):
            self.notify('请松开快捷键后重试')
            return False
        foreground = getattr(self.keyboard, 'foreground', lambda: None)
        target = foreground()
        previous = self.board.read_text()
        before = self.board.change_count()
        self.keyboard.press(KEY_C, FLAG_CMD)
        if not self._wait(lambda: self.board.change_count() != before, self.copy_timeout):
            self.notify("没有读取到选中文字：请先选中文字，或当前应用不支持复制")
            return False
        operation = self.controller.refill if action == "refill_paste" else self.controller.redact
        keep_result = False
        try:
            if operation() is not True:
                return False
            if action == "redact_selection":
                keep_result = True
                return True
            if foreground() != target or self.keyboard.modifiers_down():
                self.notify('当前窗口或按键状态已变化，已取消替换；请重新选中文字后重试')
                return False
            self.keyboard.press(KEY_V, FLAG_CMD)
            self.sleep(self.paste_settle)
            return True
        finally:
            if not keep_result:
                self._restore(previous)
