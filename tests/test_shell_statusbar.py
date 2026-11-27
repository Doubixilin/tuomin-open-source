"""Status menu behavior with a fake AppKit and main-thread dispatcher."""
from __future__ import annotations

import threading
from types import SimpleNamespace

import pytest

from tuomin_gateway.shell.autostart import LoginItem
from tuomin_gateway.shell.statusbar import StatusBar


class Item:
    @classmethod
    def alloc(cls):
        return cls()

    @classmethod
    def separatorItem(cls):
        return SimpleNamespace(separator=True)

    def initWithTitle_action_keyEquivalent_(self, title, action, key):
        self.title, self.action, self.key = title, action, key
        return self

    def setEnabled_(self, value):
        self.enabled = value

    def setHidden_(self, value):
        self.hidden = value

    def setTarget_(self, target):
        self.target = target

    def setTitle_(self, title):
        self.title = title

    def setState_(self, state):
        self.state = state

    def setSubmenu_(self, submenu):
        self.submenu = submenu


class Menu:
    @classmethod
    def alloc(cls):
        return cls()

    def init(self):
        self.items = []
        return self

    def addItem_(self, item):
        self.items.append(item)

    def setAutoenablesItems_(self, enabled):
        self.autoenables = enabled

    def indexOfItem_(self, item):
        return self.items.index(item)

    def removeItem_(self, item):
        self.items.remove(item)

    def insertItem_atIndex_(self, item, index):
        self.items.insert(index, item)


def make_bar(autostart=None):
    calls, queue = [], []
    button = Item()
    item = SimpleNamespace(button=lambda: button, setMenu_=lambda m: calls.append(("menu", m)))
    system = SimpleNamespace(
        statusItemWithLength_=lambda length: item,
        removeStatusItem_=lambda value: calls.append(("remove", value)),
    )
    kit = SimpleNamespace(NSStatusBar=SimpleNamespace(systemStatusBar=lambda: system),
                          NSVariableStatusItemLength=-1, NSMenu=Menu, NSMenuItem=Item)
    bar = StatusBar(on_open=lambda: calls.append("open"),
                    on_copy=lambda: calls.append("copy"),
                    on_quit=lambda: calls.append("quit"), autostart=autostart,
                    appkit=kit, call_after=lambda fn, *args: queue.append((fn, args)),
                    target_factory=lambda callback: SimpleNamespace(callback=callback))
    return bar, calls, queue


def find_item(bar, title):
    return next(item for item in bar._menu.items if getattr(item, "title", None) == title)


def test_menu_contains_status_and_actions_and_source_login_is_disabled():
    bar, calls, _ = make_bar()
    bar.start()
    assert bar.ready.is_set()
    assert not bar._menu.autoenables
    assert not find_item(bar, "Tuomin 正在启动").enabled
    assert not find_item(bar, "登录时启动").enabled
    for title in ("打开工作台", "复制网关地址", "退出 Tuomin"):
        find_item(bar, title).target.callback()
    assert calls[-3:] == ["open", "copy", "quit"]
    assert find_item(bar, "打开工作台").key == "o"
    assert find_item(bar, "退出 Tuomin").key == "q"


def test_updates_from_worker_are_dispatched_before_appkit_access():
    bar, _, queue = make_bar()
    bar.start()
    worker = threading.Thread(target=lambda: bar.update("运行中 · 8765", "NER 就绪"))
    worker.start()
    worker.join()
    assert bar._status_item.title == "Tuomin 正在启动"
    assert len(queue) == 1
    callback, args = queue.pop()
    callback(*args)
    assert bar._status_item.title == "运行中 · 8765"
    assert bar._detail_item.title == "NER 就绪"


def test_start_from_worker_signals_ready_only_after_item_created():
    bar, _, queue = make_bar()
    worker = threading.Thread(target=bar.start)
    worker.start()
    worker.join()
    assert not bar.ready.is_set()
    assert bar._item is None
    callback, args = queue.pop()
    callback(*args)
    assert bar.ready.is_set()


def test_result_future_actions_and_cleanup():
    bar, calls, _ = make_bar()
    bar.start()
    bar.set_result("已完成")
    assert bar._result_item.title == "已完成"
    assert not bar._result_item.hidden
    bar.add_separator()
    bar.add_action("后续动作", lambda: calls.append("future"))
    find_item(bar, "后续动作").target.callback()
    assert calls[-1] == "future"
    bar.stop()
    assert calls[-1][0] == "remove"
    assert not bar.ready.is_set()
    assert bar._targets == []


def test_login_toggle_reflects_approval_status():
    changes = []
    login = SimpleNamespace(available=True, status=lambda: 2, enabled=lambda: False,
                            set_enabled=changes.append)
    bar, _, _ = make_bar(login)
    bar.start()
    item = find_item(bar, "登录时启动")
    assert item.enabled
    assert item.state == 0
    assert "系统设置" in bar._result_item.title
    item.target.callback()
    assert changes == [True]


def test_callback_error_does_not_surface_exception_text():
    bar, _, _ = make_bar()
    bar.start()
    def fails():
        raise RuntimeError("not-for-display")
    bar.add_action("故障动作", fails)
    find_item(bar, "故障动作").target.callback()
    assert bar._result_item.title == "操作未完成，请打开工作台查看"


def test_source_login_unavailable_without_framework_import():
    login = LoginItem("/tmp/venv/bin/python")
    assert not login.available
    assert not login.enabled()
    with pytest.raises(RuntimeError, match="应用"):
        login.set_enabled(True)


def test_app_login_shape_and_success_and_failure(monkeypatch):
    monkeypatch.setattr("tuomin_gateway.shell.autostart.sys.platform", "darwin")
    login = LoginItem("/Applications/Tuomin.app/Contents/MacOS/tuomin-workbench")
    assert login.available
    service = SimpleNamespace(status=lambda: 1, registerAndReturnError_=lambda error: (True, None),
                              unregisterAndReturnError_=lambda error: (False, object()))
    monkeypatch.setattr(login, "_service", lambda: service)
    assert login.set_enabled(True) == 1
    assert login.enabled()
    with pytest.raises(RuntimeError, match="登录项"):
        login.set_enabled(False)


def test_confirm_exit_defaults_to_continue():
    bar, _, _ = make_bar()
    alert = SimpleNamespace(
        init=lambda: alert, setMessageText_=lambda text: None,
        setInformativeText_=lambda text: None, addButtonWithTitle_=lambda text: None,
        runModal=lambda: 1000,
    )
    bar._appkit.NSAlert = SimpleNamespace(alloc=lambda: alert)
    bar._appkit.NSAlertSecondButtonReturn = 1001
    assert not bar.confirm_exit()
    alert.runModal = lambda: 1001
    assert bar.confirm_exit()


def test_native_framework_bridge_is_available_when_installed():
    objc = pytest.importorskip("objc")
    foundation = pytest.importorskip("Foundation")
    bundle = foundation.NSBundle.bundleWithPath_(
        "/System/Library/Frameworks/ServiceManagement.framework"
    )
    if bundle is None:
        pytest.skip("ServiceManagement framework unavailable")
    assert bundle.load()
    assert objc.lookUpClass("SMAppService").mainAppService() is not None


def test_dynamic_menus_stay_before_open_and_do_not_accumulate_targets():
    bar, calls, _ = make_bar()
    bar.start()
    initial_targets = len(bar._targets)
    bar.set_action("redact", "脱敏剪贴板", lambda: calls.append("redact"))
    for index in range(4):
        bar.set_submenu("session", f"剪贴板会话 {index}", [
            ("查看", lambda: calls.append("view"), False),
            ("当前来源", lambda: calls.append("source"), True),
        ])
    assert len(bar._targets) == initial_targets
    assert len(bar._dynamic) == 2
    assert sum(len(targets) for item, targets in bar._dynamic.values()) == 3
    dynamic = find_item(bar, "剪贴板会话 3")
    assert bar._menu.indexOfItem_(dynamic) < bar._menu.indexOfItem_(bar._open_item)
    assert dynamic.submenu.items[1].state == 1
    dynamic.submenu.items[0].target.callback()
    assert calls[-1] == "view"
    bar.set_action("redact", "脱敏剪贴板（更新）", lambda: calls.append("new"))
    assert sum(len(targets) for item, targets in bar._dynamic.values()) == 3


def test_flash_restores_only_the_latest_generation():
    bar, _, _ = make_bar()
    later = []
    bar._call_later = lambda seconds, callback: later.append(callback)
    bar.start()
    bar.flash("已脱敏 2 处")
    bar.flash("已还原 1 处")
    later[0]()
    assert bar._item.button().title == "已还原 1 处"
    later[1]()
    assert bar._item.button().title == "Tuomin"
    bar.flash("已完成")
    bar.stop()
    later[2]()
