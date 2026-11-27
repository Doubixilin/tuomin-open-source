"""Carbon registration contract without consuming real keyboard input."""
from __future__ import annotations

import pytest

from tuomin_gateway.shell.hotkeys import Hotkeys, parse_shortcut


class Carbon:
    def __init__(self):
        self.calls = []
        self.conflict = False

    def install(self, handler):
        self.handler = handler
        self.calls.append("install")
        return 99

    def register(self, key, modifiers, identifier):
        if self.conflict:
            raise RuntimeError("快捷键已被占用或系统拒绝注册")
        self.calls.append(("register", key, modifiers, identifier))
        return identifier

    def event_id(self, event):
        return event

    def unregister(self, reference):
        self.calls.append(("unregister", reference))

    def remove(self, reference):
        self.calls.append(("remove", reference))


def test_default_shortcuts_and_dispatch():
    carbon, actions = Carbon(), []
    hotkeys = Hotkeys({"redact": lambda: actions.append("redact"),
                       "refill": lambda: actions.append("refill")}, carbon=carbon)
    assert hotkeys.configure() == {}
    assert carbon.calls == ["install", ("register", 2, 2304, 1), ("register", 15, 2304, 2)]
    carbon.handler(None, 1, None)
    carbon.handler(None, 2, None)
    assert actions == ["redact", "refill"]
    hotkeys.close()
    assert carbon.calls[-3:] == [("unregister", 1), ("unregister", 2), ("remove", 99)]


def test_reconfigure_unregisters_previous_and_supports_disable():
    carbon = Carbon()
    hotkeys = Hotkeys({"redact": lambda: None, "refill": lambda: None}, carbon=carbon)
    hotkeys.configure()
    assert hotkeys.configure({"redact": "ctrl+shift+x", "refill": None}) == {}
    assert carbon.calls[-1] == ("register", 7, (1 << 12) | (1 << 9), 1)
    assert hotkeys.configure({}) == {}
    assert hotkeys._handler_ref is None
    assert hotkeys._references == {}


def test_invalid_duplicate_and_conflicting_shortcuts_are_visible():
    carbon = Carbon()
    hotkeys = Hotkeys({"redact": lambda: None, "refill": lambda: None}, carbon=carbon)
    errors = hotkeys.configure({"redact": "cmd+d", "refill": "cmd+d"})
    assert errors == {"refill": "两个动作不能使用同一快捷键"}
    carbon.conflict = True
    errors = hotkeys.configure({"redact": "cmd+d", "refill": "plain"})
    assert set(errors) == {"redact", "refill"}
    assert not hotkeys._references
    hotkeys.close()


@pytest.mark.parametrize("shortcut", ["d", "shift+d", "option+enter", "hyper+d", 42])
def test_invalid_shortcuts(shortcut):
    with pytest.raises(ValueError):
        parse_shortcut(shortcut)


def test_physical_ansi_key_codes_and_aliases():
    assert parse_shortcut("command+option+D") == (2, 2304)
    assert parse_shortcut("ctrl+0") == (29, 4096)
    assert parse_shortcut("cmd+y") == (16, 256)
    assert parse_shortcut(False) is None


def test_callback_exceptions_do_not_escape_ctypes():
    carbon = Carbon()
    def fails():
        raise RuntimeError("private callback detail")
    hotkeys = Hotkeys({"redact": fails}, carbon=carbon)
    hotkeys.configure()
    assert carbon.handler(None, 1, None) == 0
    hotkeys.close()
