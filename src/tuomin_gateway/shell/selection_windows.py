"""Explicit Ctrl+C / Ctrl+V actions on Windows; never installs input hooks."""
from __future__ import annotations

import ctypes
from ctypes import wintypes

from .selection import KEY_C, KEY_V


class KeyboardInput(ctypes.Structure):
    _fields_ = [('wVk', wintypes.WORD), ('wScan', wintypes.WORD),
                ('dwFlags', wintypes.DWORD), ('time', wintypes.DWORD),
                ('dwExtraInfo', ctypes.c_size_t)]


class MouseInput(ctypes.Structure):
    _fields_ = [('dx', wintypes.LONG), ('dy', wintypes.LONG),
                ('mouseData', wintypes.DWORD), ('dwFlags', wintypes.DWORD),
                ('time', wintypes.DWORD), ('dwExtraInfo', ctypes.c_size_t)]


class HardwareInput(ctypes.Structure):
    _fields_ = [('uMsg', wintypes.DWORD), ('wParamL', wintypes.WORD), ('wParamH', wintypes.WORD)]


class InputUnion(ctypes.Union):
    _fields_ = [('ki', KeyboardInput), ('mi', MouseInput), ('hi', HardwareInput)]


class Input(ctypes.Structure):
    _anonymous_ = ('value',)
    _fields_ = [('type', wintypes.DWORD), ('value', InputUnion)]


class Keyboard:
    def __init__(self, *, user=None):
        self.user = user or ctypes.WinDLL('user32', use_last_error=True)
        if user is None:
            for name, result, args in (
                ('SendInput', wintypes.UINT, [wintypes.UINT, ctypes.POINTER(Input), ctypes.c_int]),
                ('GetAsyncKeyState', ctypes.c_short, [ctypes.c_int]),
                ('GetForegroundWindow', wintypes.HWND, []),
            ):
                function = getattr(self.user, name)
                function.restype, function.argtypes = result, args

    def trusted(self):
        return True  # Windows applies UIPI per target; SendInput can still fail.

    def prompt(self):
        return self.trusted()

    def foreground(self):
        return self.user.GetForegroundWindow()

    def modifiers_down(self):
        return any(self.user.GetAsyncKeyState(key) & 0x8000 for key in (0x10, 0x11, 0x12, 0x5B, 0x5C))

    def press(self, keycode, flags):
        key = {KEY_C: ord('C'), KEY_V: ord('V')}.get(keycode)
        if key is None:
            raise ValueError('unsupported selection key')
        events = (Input * 4)()
        for event, vk, up in zip(events, (0x11, key, key, 0x11), (False, False, True, True)):
            event.type = 1
            event.ki = KeyboardInput(wVk=vk, dwFlags=2 if up else 0)
        if self.user.SendInput(4, events, ctypes.sizeof(Input)) != 4:
            # Release only our two keys after a partial insertion.
            self.user.SendInput(2, (Input * 2)(events[2], events[3]), ctypes.sizeof(Input))
            raise RuntimeError('无法向当前应用发送按键；请先复制文字，或在同等权限的应用中重试')
