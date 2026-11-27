"""Windows shell contracts without reading clipboard or reserving real keys."""
from __future__ import annotations

import queue
import ctypes
import threading
from types import SimpleNamespace

import pytest

from tuomin_gateway.shell.pasteboard_windows import Pasteboard, TEXT_TYPE, FILE_TYPE, PRIVACY_TYPES, _WindowsClipboard
from tuomin_gateway.shell.hotkeys_windows import Hotkeys, parse_shortcut, _WM_HOTKEY, _WM_QUIT
from tuomin_gateway.shell.autostart import LoginItem


class Board:
    def __init__(self, *, kinds=(TEXT_TYPE,), text="合成文本"):
        self.kinds, self.value = kinds, text
        self.calls, self.failures, self.fail_kind = [], 0, None

    def create_owner(self):
        self.calls.append("owner")
        return 99

    def destroy_owner(self, owner):
        assert owner == 99
        self.calls.append("destroy")

    def open(self, owner):
        assert owner == 99
        self.calls.append("open")
        if self.failures:
            self.failures -= 1
            return False
        return True

    def close(self):
        self.calls.append("close")

    def available(self, kind):
        return kind in self.kinds

    def text(self):
        return self.value

    def register(self, name):
        self.calls.append(("register", name))
        return name

    def empty(self):
        self.calls.append("empty")

    def set_data(self, kind, data):
        if kind == self.fail_kind:
            raise RuntimeError("synthetic failure")
        self.calls.append(("write", kind, data))


@pytest.mark.parametrize("kinds", [(), (FILE_TYPE,), (FILE_TYPE, TEXT_TYPE), (49152,)])
def test_nontext_and_file_copies_are_left_alone(kinds):
    board = Board(kinds=kinds)
    assert Pasteboard(api=board).read_text() is None
    assert board.calls == ["owner", "open", "close", "destroy"]


def test_clipboard_read_and_bounded_lock_retry():
    board, delays = Board(), []
    board.failures = 2
    assert Pasteboard(api=board, sleep=delays.append).read_text() == "合成文本"
    assert delays == [0.01, 0.025]
    board = Board()
    board.failures = 100
    with pytest.raises(RuntimeError, match="占用"):
        Pasteboard(api=board, sleep=lambda _: None).read_text()
    assert board.calls.count("open") == 5
    assert board.calls[-1] == "destroy" and "close" not in board.calls


def test_private_markers_precede_text_and_close_is_guaranteed():
    board = Board()
    Pasteboard(api=board).write_text("合成😀文本", concealed=True)
    writes = [call for call in board.calls if isinstance(call, tuple) and call[0] == "write"]
    assert writes == [("write", name, b"\0" * 4) for name in PRIVACY_TYPES] + [
        ("write", TEXT_TYPE, "合成😀文本\0".encode("utf-16-le"))]
    assert board.calls[-2:] == ["close", "destroy"]
    board = Board()
    board.fail_kind = PRIVACY_TYPES[1]
    with pytest.raises(RuntimeError):
        Pasteboard(api=board).write_text("合成原文", concealed=True)
    assert not any(isinstance(c, tuple) and c[:2] == ("write", TEXT_TYPE) for c in board.calls)
    assert board.calls[-2:] == ["close", "destroy"]


def test_regular_masked_copy_has_only_unicode_text_and_null_is_rejected():
    board = Board()
    pasteboard = Pasteboard(api=board)
    pasteboard.write_text("<PERSON_001>")
    assert not any(isinstance(c, tuple) and c[0] == "register" for c in board.calls)
    before = list(board.calls)
    with pytest.raises(ValueError, match="空字符"):
        pasteboard.write_text("bad\0value")
    assert board.calls == before


def test_native_read_bounds_utf16_and_ignores_nontext_allocator_padding():
    raw = "合成😀\0".encode("utf-16-le") + b"\x00\xd8"
    buffer = ctypes.create_string_buffer(raw)
    unlocked = []
    native = _WindowsClipboard.__new__(_WindowsClipboard)
    native.user = SimpleNamespace(GetClipboardData=lambda kind: 123)
    native.kernel = SimpleNamespace(GlobalSize=lambda handle: len(raw),
        GlobalLock=lambda handle: ctypes.addressof(buffer),
        GlobalUnlock=unlocked.append)
    assert native.text() == "合成😀"
    assert unlocked == [123]
    raw = "未终止".encode("utf-16-le")
    buffer = ctypes.create_string_buffer(raw)
    with pytest.raises(RuntimeError, match="格式无效"):
        native.text()


def test_failed_native_write_frees_memory_and_success_transfers_ownership():
    buffer = ctypes.create_string_buffer(16)
    freed, unlocked = [], []
    native = _WindowsClipboard.__new__(_WindowsClipboard)
    native.kernel = SimpleNamespace(GlobalAlloc=lambda flags, length: 123,
        GlobalLock=lambda handle: ctypes.addressof(buffer), GlobalUnlock=unlocked.append,
        GlobalFree=freed.append)
    native.user = SimpleNamespace(SetClipboardData=lambda kind, handle: 0)
    with pytest.raises(RuntimeError, match="无法写入"):
        native.set_data(TEXT_TYPE, b"A\0\0\0")
    assert freed == [123] and unlocked == [123]
    freed.clear()
    native.user.SetClipboardData = lambda kind, handle: handle
    native.set_data(TEXT_TYPE, b"B\0\0\0")
    assert not freed and buffer.raw[:4] == b"B\0\0\0"


class Keys:
    def __init__(self):
        self.queue = queue.Queue()
        self.calls, self.conflict = [], False

    def create_queue(self):
        self.calls.append(("queue", threading.get_ident()))
        return 42

    def register(self, identifier, key, modifiers):
        self.calls.append(("register", identifier, key, modifiers, threading.get_ident()))
        return not self.conflict

    def unregister(self, identifier):
        self.calls.append(("unregister", identifier, threading.get_ident()))
        return True

    def message(self):
        return self.queue.get(timeout=2)

    def stop(self, thread_id):
        assert thread_id == 42
        self.queue.put((_WM_QUIT, 0))


def test_hotkey_thread_registers_dispatches_and_unregisters_on_same_thread():
    api, fired = Keys(), threading.Event()
    hotkeys = Hotkeys({"redact": fired.set, "refill": lambda: None}, api=api)
    assert hotkeys.configure() == {}
    try:
        api.queue.put((_WM_HOTKEY, 1))
        assert fired.wait(2)
        assert [c[1:4] for c in api.calls if c[0] == "register"] == [(1, 68, 3), (2, 82, 3)]
    finally:
        hotkeys.close()
    register_threads = [c[-1] for c in api.calls if c[0] in {"register", "unregister"}]
    assert len(set(register_threads)) == 1
    assert register_threads[0] != threading.get_ident()
    assert hotkeys._thread is None


def test_reconfigure_disable_duplicate_and_os_conflict():
    api = Keys()
    hotkeys = Hotkeys({"redact": lambda: None, "refill": lambda: None}, api=api)
    assert hotkeys.configure({"redact": "ctrl+d", "refill": "ctrl+d"}) == {"refill": "两个动作不能使用同一快捷键"}
    assert hotkeys.configure({}) == {}
    api.conflict = True
    assert set(hotkeys.configure()) == {"redact", "refill"}
    hotkeys.close()


@pytest.mark.parametrize("value", ["d", "shift+d", "cmd+d", "ctrl+enter", "ctrl+好", 12, True])
def test_windows_shortcuts_reject_ambiguous_input(value):
    with pytest.raises(ValueError):
        parse_shortcut(value)


def test_windows_key_aliases_and_disable():
    assert parse_shortcut("Control+Alt+D") == (68, 3)
    assert parse_shortcut("win+shift+0") == (48, 12)
    assert parse_shortcut("") is None


def test_autostart_roundtrip_is_explicit_private_and_unicode(tmp_path, monkeypatch):
    monkeypatch.setattr("sys.platform", "win32")
    executable = tmp_path / "中文 含空格" / "Tuomin.exe"
    executable.parent.mkdir()
    executable.write_bytes(b"synthetic executable")
    startup = tmp_path / "isolated-startup"
    item = LoginItem(executable, startup_dir=startup)
    assert item.available and item.status() == 0
    assert not startup.exists()
    assert item.set_enabled(True) == 1
    path = startup / "Tuomin.vbs"
    assert path.read_bytes().startswith(b"\xff\xfe")
    content = path.read_text(encoding="utf-16")
    assert str(executable) in content and '"--shell"' in content
    assert item.enabled()
    from tests.permission_assertions import assert_owner_private
    assert_owner_private(path, is_dir=False)
    assert item.set_enabled(True) == 1
    assert item.set_enabled(False) == 0
    assert not path.exists()


def test_autostart_never_overwrites_or_deletes_a_foreign_entry(tmp_path, monkeypatch):
    monkeypatch.setattr("sys.platform", "win32")
    exe = tmp_path / "Tuomin.exe"
    exe.write_bytes(b"synthetic")
    path = tmp_path / "Tuomin.vbs"
    path.write_text("unrelated user's script", encoding="utf-8")
    item = LoginItem(exe, startup_dir=tmp_path)
    assert item.status() == 2
    assert not item.enabled()  # A tray checked-state query must remain usable.
    for enabled in (True, False):
        with pytest.raises(RuntimeError, match="已有其他"):
            item.set_enabled(enabled)
    assert path.read_text(encoding="utf-8") == "unrelated user's script"


def test_moved_installation_login_item_is_readable_but_never_replaced(tmp_path, monkeypatch):
    monkeypatch.setattr("sys.platform", "win32")
    original = tmp_path / "old" / "Tuomin.exe"
    moved = tmp_path / "new" / "Tuomin.exe"
    for exe in (original, moved):
        exe.parent.mkdir()
        exe.write_bytes(b"synthetic executable")
    startup = tmp_path / "startup"
    previous = LoginItem(original, startup_dir=startup)
    previous.set_enabled(True)
    path = startup / "Tuomin.vbs"
    saved = path.read_bytes()
    current = LoginItem(moved, startup_dir=startup)
    assert current.status() == 2 and not current.enabled()
    for enabled in (True, False):
        with pytest.raises(RuntimeError, match="已有其他"):
            current.set_enabled(enabled)
    assert path.read_bytes() == saved


def test_development_python_is_not_available_for_login_registration(tmp_path, monkeypatch):
    monkeypatch.setattr("sys.platform", "win32")
    exe = tmp_path / "python.exe"
    exe.write_bytes(b"synthetic")
    item = LoginItem(exe, startup_dir=tmp_path)
    assert not item.available
    with pytest.raises(RuntimeError, match="打包"):
        item.set_enabled(True)
