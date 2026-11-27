"""Carbon global hot keys without Accessibility permission or extra packages."""
from __future__ import annotations

import ctypes
import ctypes.util

from .main_thread import call_on_main

DEFAULT_SHORTCUTS = {"redact": "alt+cmd+d", "refill": "alt+cmd+r"}
# Every clipboard action that can carry a global shortcut, in menu order.
ACTIONS = {
    "redact": "脱敏剪贴板",
    "refill": "回填剪贴板",
    "redact_selection": "脱敏选中文字",
    "redact_paste": "选中脱敏并替换",
    "refill_paste": "选中回填并替换",
}
SELECTION_ACTIONS = ("redact_selection", "redact_paste", "refill_paste")
# Virtual key codes are physical (ANSI) positions, so a shortcut keeps working
# when the user switches input methods.
_KEYS = dict(zip(
    "asdfhgzxcvbqwerty1234659780ouipljknm",
    [0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 11, 12, 13, 14, 15, 17, 16,
     18, 19, 20, 21, 22, 23, 25, 26, 28, 29, 31, 32, 34, 35, 37, 38, 40, 45, 46],
))
_KEYS.update({"-": 27, "=": 24, "[": 33, "]": 30, "\\": 42, ";": 41, "'": 39, ",": 43, ".": 47, "/": 44, "`": 50, "space": 49})
_FUNCTION_KEYS = {f"f{n}": code for n, code in enumerate(
    [122, 120, 99, 118, 96, 97, 98, 100, 101, 109, 103, 111, 105, 107, 113, 106, 64, 79, 80, 90], 1)}
_KEYS.update(_FUNCTION_KEYS)
_MODIFIERS = {"cmd": 1 << 8, "command": 1 << 8, "shift": 1 << 9,
              "alt": 1 << 11, "option": 1 << 11, "ctrl": 1 << 12, "control": 1 << 12}
_SIGNATURE = int.from_bytes(b"TMCL", "big")
_GLYPHS = (("ctrl", "⌃"), ("alt", "⌥"), ("shift", "⇧"), ("cmd", "⌘"))
_CANONICAL = {"command": "cmd", "option": "alt", "control": "ctrl"}
# F13–F20 have no system or app meaning, so they may be used on their own.
_STANDALONE = {f"f{n}" for n in range(13, 21)}


class EventHotKeyID(ctypes.Structure):
    _fields_ = [("signature", ctypes.c_uint32), ("id", ctypes.c_uint32)]


class EventTypeSpec(ctypes.Structure):
    _fields_ = [("eventClass", ctypes.c_uint32), ("eventKind", ctypes.c_uint32)]


_Callback = ctypes.CFUNCTYPE(ctypes.c_int32, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p)


def _split(value: str) -> tuple[list[str], str]:
    parts = [part.strip().lower() for part in value.replace(" ", "").split("+")]
    if value.strip().lower().endswith("++"):
        parts = parts[:-2] + ["+"]
    return [_CANONICAL.get(p, p) for p in parts[:-1]], parts[-1]


def parse_shortcut(value):
    if value is None or value is False or value == "":
        return None
    if not isinstance(value, str):
        raise ValueError("快捷键格式应为 alt+cmd+d，可留空关闭")
    modifiers_names, key = _split(value)
    if key not in _KEYS:
        raise ValueError("快捷键的按键仅支持字母、数字、F1–F20、空格和常用标点")
    modifiers = 0
    for part in modifiers_names:
        if part not in _MODIFIERS:
            raise ValueError("快捷键修饰键仅支持 cmd、alt、ctrl、shift")
        modifiers |= _MODIFIERS[part]
    if key not in _STANDALONE and not modifiers & (_MODIFIERS["cmd"] | _MODIFIERS["alt"] | _MODIFIERS["ctrl"]):
        if key in _FUNCTION_KEYS and modifiers_names == []:
            raise ValueError("F1–F12 需要搭配 cmd、alt 或 ctrl（F13–F20 可以单独使用）")
        raise ValueError("快捷键至少需要 cmd、alt 或 ctrl")
    return _KEYS[key], modifiers


def normalize_shortcut(value):
    """Canonical text form (``alt+cmd+d``) or None; raises like parse_shortcut."""
    if parse_shortcut(value) is None:
        return None
    modifiers, key = _split(value)
    ordered = [name for name, _ in _GLYPHS if name in modifiers]
    return "+".join([*ordered, key])


def format_shortcut(value) -> str:
    """Display form for menus and pages, e.g. ``⌥⌘D`` or ``F13``."""
    try:
        canonical = normalize_shortcut(value)
    except ValueError:
        return ""
    if not canonical:
        return ""
    modifiers, key = _split(canonical)
    label = {"space": "空格"}.get(key, key.upper())
    return "".join(glyph for name, glyph in _GLYPHS if name in modifiers) + label


def menu_key_equivalent(value):
    """(keyEquivalent, NSEventModifierFlags) for an NSMenuItem, or None."""
    try:
        canonical = normalize_shortcut(value)
    except ValueError:
        return None
    if not canonical:
        return None
    modifiers, key = _split(canonical)
    flags = {"shift": 1 << 17, "ctrl": 1 << 18, "alt": 1 << 19, "cmd": 1 << 20}
    mask = sum(flags[m] for m in modifiers)
    if key in _FUNCTION_KEYS:
        char = chr(0xF704 + int(key[1:]) - 1)
    elif key == "space":
        char = " "
    else:
        char = key
    return char, mask


class _Carbon:
    def __init__(self):
        library = ctypes.util.find_library("Carbon")
        if not library:
            raise RuntimeError("系统不支持全局快捷键")
        self.lib = ctypes.CDLL(library)
        specs = {
            "GetApplicationEventTarget": (ctypes.c_void_p, []),
            "InstallEventHandler": (ctypes.c_int32, [ctypes.c_void_p, _Callback, ctypes.c_ulong,
                ctypes.POINTER(EventTypeSpec), ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)]),
            "RemoveEventHandler": (ctypes.c_int32, [ctypes.c_void_p]),
            "RegisterEventHotKey": (ctypes.c_int32, [ctypes.c_uint32, ctypes.c_uint32,
                EventHotKeyID, ctypes.c_void_p, ctypes.c_uint32, ctypes.POINTER(ctypes.c_void_p)]),
            "UnregisterEventHotKey": (ctypes.c_int32, [ctypes.c_void_p]),
            "GetEventParameter": (ctypes.c_int32, [ctypes.c_void_p, ctypes.c_uint32,
                ctypes.c_uint32, ctypes.POINTER(ctypes.c_uint32), ctypes.c_ulong,
                ctypes.POINTER(ctypes.c_ulong), ctypes.c_void_p]),
        }
        for name, (result, args) in specs.items():
            function = getattr(self.lib, name)
            function.restype, function.argtypes = result, args

    def install(self, handler):
        target = self.lib.GetApplicationEventTarget()
        event = EventTypeSpec(int.from_bytes(b"keyb", "big"), 5)
        reference = ctypes.c_void_p()
        result = self.lib.InstallEventHandler(target, handler, 1, ctypes.byref(event),
                                              None, ctypes.byref(reference))
        if result:
            raise RuntimeError("无法安装全局快捷键事件处理器")
        return reference

    def register(self, key, modifiers, identifier):
        reference = ctypes.c_void_p()
        result = self.lib.RegisterEventHotKey(
            key, modifiers, EventHotKeyID(_SIGNATURE, identifier),
            self.lib.GetApplicationEventTarget(), 1, ctypes.byref(reference),
        )
        if result:
            raise RuntimeError("快捷键已被占用或系统拒绝注册")
        return reference

    def event_id(self, event):
        identifier = EventHotKeyID()
        result = self.lib.GetEventParameter(event, int.from_bytes(b"----", "big"),
            int.from_bytes(b"hkid", "big"), None, ctypes.sizeof(identifier), None,
            ctypes.byref(identifier))
        if result or identifier.signature != _SIGNATURE:
            return None
        return identifier.id

    def unregister(self, reference):
        if self.lib.UnregisterEventHotKey(reference):
            raise RuntimeError("无法注销全局快捷键")

    def remove(self, reference):
        if self.lib.RemoveEventHandler(reference):
            raise RuntimeError("无法移除全局快捷键事件处理器")


class Hotkeys:
    def __init__(self, callbacks, *, carbon=None, call_after=None):
        self.callbacks = dict(callbacks)
        self._carbon = carbon
        self._call_after = call_after
        self._references = {}
        self._ids = {}
        self._handler_ref = None
        # ctypes callback must remain strongly referenced for the handler's full
        # native lifetime, including failed unregister attempts.
        self._handler = _Callback(self._handle)

    def configure(self, settings=None):
        settings = DEFAULT_SHORTCUTS if settings is None else dict(settings)
        return call_on_main(lambda: self._configure(settings), call_after=self._call_after)

    def _configure(self, settings):
        self._close()
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
        if self._carbon is None:
            self._carbon = _Carbon()
        self._handler_ref = self._carbon.install(self._handler)
        for identifier, (name, (key, modifiers)) in enumerate(parsed.items(), 1):
            try:
                self._references[name] = self._carbon.register(key, modifiers, identifier)
                self._ids[identifier] = name
            except RuntimeError as error:
                errors[name] = str(error)
        return errors

    def _handle(self, handler, event, userdata):
        try:
            name = self._ids.get(self._carbon.event_id(event))
            if name:
                self.callbacks[name]()
        except Exception:
            # Exceptions must not cross ctypes (which otherwise prints them).
            pass
        return 0

    def close(self):
        return call_on_main(self._close, call_after=self._call_after)

    def _close(self):
        for name, reference in list(self._references.items()):
            self._carbon.unregister(reference)
            del self._references[name]
        if self._handler_ref is not None:
            self._carbon.remove(self._handler_ref)
            self._handler_ref = None
        self._ids.clear()
