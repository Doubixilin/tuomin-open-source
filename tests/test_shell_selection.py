"""Selected-text actions and the extended shortcut settings, without real input."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from tuomin_gateway.shell.bridge import ShellBridge
from tuomin_gateway.shell.clipboard import ClipboardController
from tuomin_gateway.shell.hotkeys import Hotkeys, format_shortcut, menu_key_equivalent, normalize_shortcut, parse_shortcut
from tuomin_gateway.shell.selection import KEY_C, KEY_V, SelectionFlow
from tuomin_gateway.shell.storage import SessionFile


class Board:
    def __init__(self, text="之前的剪贴板"):
        self.text, self.count, self.writes = text, 0, []

    def read_text(self):
        return self.text

    def write_text(self, text, concealed=False):
        self.text, self.count = text, self.count + 1
        self.writes.append((text, concealed))

    def clear(self):
        self.text, self.count = None, self.count + 1

    def change_count(self):
        return self.count


class Keyboard:
    def __init__(self, board, *, trusted=True, selection="选中的原文", copies=True):
        self.board, self._trusted, self.selection, self.copies = board, trusted, selection, copies
        self.pressed, self.prompted = [], 0

    def trusted(self):
        return self._trusted

    def prompt(self):
        self.prompted += 1

    def modifiers_down(self):
        return False

    def press(self, key, flags):
        self.pressed.append(key)
        if key == KEY_C and self.copies:
            self.board.write_text(self.selection)


class Controller:
    def __init__(self, board, result=True):
        self.board, self.result, self.calls = board, result, []

    def redact(self):
        self.calls.append(("redact", self.board.read_text()))
        if self.result is True:
            self.board.write_text("<ORG_001> 的原文")
        return self.result

    def refill(self):
        self.calls.append(("refill", self.board.read_text()))
        if self.result is True:
            self.board.write_text("还原后的原文", concealed=True)
        return self.result


def flow(board, keyboard, controller, notes):
    clock = iter(range(0, 10_000))
    return SelectionFlow(board, controller, keyboard, lambda text, action=None: notes.append((text, action)),
                         sleep=lambda _: None, clock=lambda: next(clock) * 0.01)


def test_redact_selection_leaves_masked_text_on_clipboard():
    board, notes = Board(), []
    keyboard = Keyboard(board)
    controller = Controller(board)
    assert flow(board, keyboard, controller, notes).run("redact_selection") is True
    assert controller.calls == [("redact", "选中的原文")] and keyboard.pressed == [KEY_C]
    assert board.text == "<ORG_001> 的原文"


def test_replace_actions_paste_then_restore_previous_clipboard():
    for action, op in (("redact_paste", "redact"), ("refill_paste", "refill")):
        board, notes = Board(), []
        keyboard = Keyboard(board)
        controller = Controller(board)
        assert flow(board, keyboard, controller, notes).run(action) is True
        assert controller.calls[0][0] == op and keyboard.pressed == [KEY_C, KEY_V]
        assert board.text == "之前的剪贴板"  # restored originals never linger


def test_empty_previous_clipboard_is_cleared_after_refill_replace():
    board, notes = Board(text=None), []
    assert flow(board, Keyboard(board), Controller(board), notes).run("refill_paste") is True
    assert board.text is None


def test_untrusted_process_prompts_and_does_not_type():
    board, notes = Board(), []
    keyboard = Keyboard(board, trusted=False)
    controller = Controller(board)
    assert flow(board, keyboard, controller, notes).run("redact_paste") is False
    assert keyboard.prompted == 1 and keyboard.pressed == [] and controller.calls == []
    assert notes[0][1] == "accessibility" and "辅助功能" in notes[0][0]


def test_no_selection_and_failed_operation_restore_state():
    board, notes = Board(), []
    assert flow(board, Keyboard(board, copies=False), Controller(board), notes).run("redact_paste") is False
    assert "没有读取到选中文字" in notes[-1][0] and board.text == "之前的剪贴板"
    board = Board()
    keyboard = Keyboard(board)
    assert flow(board, keyboard, Controller(board, result=None), notes).run("redact_paste") is False
    assert keyboard.pressed == [KEY_C] and board.text == "之前的剪贴板"


def test_shortcut_parsing_formatting_and_menu_equivalents():
    assert parse_shortcut("f13") == (105, 0)
    assert normalize_shortcut("cmd+option+D") == "alt+cmd+d" and format_shortcut("alt+cmd+d") == "⌥⌘D"
    assert format_shortcut("ctrl+shift+/") == "⌃⇧/" and format_shortcut("shift+cmd+space") == "⇧⌘空格"
    assert menu_key_equivalent("cmd+f5") == ("", 1 << 20)
    for bad, message in (("f5", "F13–F20"), ("shift+d", "cmd、alt 或 ctrl"), ("cmd+é", "F1–F20")):
        with pytest.raises(ValueError, match=message):
            parse_shortcut(bad)


def test_hotkeys_register_new_actions_and_reject_duplicates():
    from test_shell_hotkeys import Carbon
    carbon, fired = Carbon(), []
    hotkeys = Hotkeys({name: (lambda n=name: fired.append(n)) for name in ("redact", "refill", "redact_paste")}, carbon=carbon)
    assert hotkeys.configure({"redact": "alt+cmd+d", "refill": "alt+cmd+r", "redact_paste": "f13"}) == {}
    assert ("register", 105, 0, 3) in carbon.calls
    errors = hotkeys.configure({"redact": "alt+cmd+d", "refill": "alt+cmd+d"})
    assert errors == {"refill": "两个动作不能使用同一快捷键"}


class Registration:
    client = SimpleNamespace(base_url="http://127.0.0.1:8765")
    def sources(self): return [{"app_id": "", "label": "通用"}]
    def configure(self, source): pass


def _bridge(tmp_path):
    controller = ClipboardController(None, None, {}, SessionFile(tmp_path), lambda *args: None)
    bridge = ShellBridge(tmp_path, Registration(), controller)
    bridge._window = SimpleNamespace(window=SimpleNamespace(get_current_url=lambda: "http://127.0.0.1:8765/ui/"))
    return bridge


def test_bridge_merges_old_settings_normalizes_and_exposes_display(tmp_path, monkeypatch):
    monkeypatch.setattr('tuomin_gateway.shell.bridge.sys.platform', 'darwin')
    (tmp_path / "shell-settings.json").write_text('{"hotkeys": {"enabled": true, "redact": "alt+cmd+d", "refill": "alt+cmd+r"}}', encoding="utf-8")
    bridge = _bridge(tmp_path)
    assert bridge._settings["hotkeys"]["redact_paste"] == ""
    snapshot = bridge.save_shell_settings({"hotkeys": {"enabled": True, "redact_paste": "option+command+V"}})
    assert snapshot["hotkeys"]["redact_paste"] == "alt+cmd+v" and snapshot["hotkeys"]["redact"] == "alt+cmd+d"
    assert snapshot["hotkey_display"]["redact_paste"] == "⌥⌘V" and snapshot["hotkey_actions"]["refill_paste"] == "选中回填并替换"
    assert snapshot["accessibility"] == {"available": False, "trusted": False}
    with pytest.raises(ValueError):
        bridge.save_shell_settings({"hotkeys": {"enabled": True, "redact": "d"}})
    with pytest.raises(ValueError):
        bridge.save_shell_settings({"hotkeys": {"enabled": True, "bogus": "alt+cmd+x"}})
    assert ShellBridge.hotkey_bindings({"enabled": False, "redact": "alt+cmd+d"})["redact"] is None


def test_bridge_routes_selection_actions_and_accessibility(tmp_path):
    bridge = _bridge(tmp_path)
    ran = []
    bridge._selection = SimpleNamespace(run=lambda action: ran.append(action) or True)
    bridge._keyboard = SimpleNamespace(trusted=lambda: True, prompt=lambda: ran.append("prompt"))
    assert bridge.clipboard_action("redact_paste") is True and ran == ["redact_paste"]
    assert bridge.accessibility_action("prompt") == {"available": True, "trusted": True} and ran[-1] == "prompt"
    with pytest.raises(ValueError):
        bridge.accessibility_action("format_disk")
