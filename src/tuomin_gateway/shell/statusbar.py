"""Cocoa menu-bar UI. Every AppKit operation is dispatched to the main thread."""
from __future__ import annotations

import threading

_action_target_class = None


def _target_class(appkit):
    global _action_target_class
    if _action_target_class is None:
        class TuominStatusActionTarget(appkit.NSObject):
            def performAction_(self, sender):
                self.callback()
        _action_target_class = TuominStatusActionTarget
    return _action_target_class


def bring_to_front(kit, alert):
    """A menu-bar click does not activate the app: without this, the alert
    can open behind other windows while the menu stays blocked."""
    try:
        kit.NSApplication.sharedApplication().activateIgnoringOtherApps_(True)
        alert.window().setLevel_(getattr(kit, "NSFloatingWindowLevel", 3))
    except Exception:
        pass


class StatusBar:
    def __init__(self, *, on_open, on_copy, on_quit, on_login=None,
                 autostart=None, appkit=None, call_after=None, target_factory=None,
                 call_later=None, on_files=None):
        self.on_open = on_open
        self.on_copy = on_copy
        self.on_quit = on_quit
        self.on_login = on_login
        self.on_files = on_files  # files dropped on the menu-bar icon
        self.autostart = autostart
        self._appkit = appkit
        self._call_after = call_after
        self._target_factory = target_factory
        self._call_later = call_later
        self._targets = []
        self._dynamic = {}
        self._flash_generation = 0
        self._item = None
        self._menu = None
        self._status = "Tuomin 正在启动"
        self._detail = "正在连接本地网关"
        self.ready = threading.Event()

    @staticmethod
    def _load_brand_image(kit):
        """Monochrome template glyph for the status item; None keeps the text title."""
        try:
            from .assets import asset_path
            path = asset_path("menubar@2x.png")
            if path is None:
                return None
            image = kit.NSImage.alloc().initWithContentsOfFile_(str(path))
            if image is None:
                return None
            image.setTemplate_(True)
            image.setSize_((18, 18))
            return image
        except Exception:
            return None

    def _dispatch(self, callback, *args):
        if self._call_after is None:
            from PyObjCTools import AppHelper
            self._call_after = AppHelper.callAfter
        if threading.current_thread() is threading.main_thread():
            callback(*args)
        else:
            self._call_after(callback, *args)

    def start(self):
        self._dispatch(self._start)

    def _start(self):
        if self._item is not None:
            return
        if self._appkit is None:
            import AppKit
            self._appkit = AppKit
        kit = self._appkit
        self._item = kit.NSStatusBar.systemStatusBar().statusItemWithLength_(
            kit.NSVariableStatusItemLength
        )
        self._brand_image = self._load_brand_image(kit)
        if self._brand_image is not None:
            self._item.button().setImage_(self._brand_image)
            self._item.button().setTitle_("")
        else:
            self._item.button().setTitle_("Tuomin")
        if self.on_files is not None:
            try:
                from .files import install_status_drop
                install_status_drop(self._item, self.on_files)
            except Exception:
                pass
        self._menu = kit.NSMenu.alloc().init()
        self._menu.setAutoenablesItems_(False)
        self._status_item = self._add_item(self._status)
        self._detail_item = self._add_item(self._detail)
        self._result_item = self._add_item("")
        self._result_item.setHidden_(True)
        self._separator()
        self._open_item = self._add_item("打开工作台", self.on_open, "o")
        self._add_item("复制网关地址", self.on_copy)
        self._separator()
        self._login_item = self._add_item("登录时启动", self._toggle_login)
        available = bool(self.autostart and self.autostart.available)
        self._login_item.setEnabled_(available)
        if available:
            self._refresh_login()
        self._separator()
        self._add_item("退出 Tuomin", self.on_quit, "q")
        self._item.setMenu_(self._menu)
        self.ready.set()

    def _new_item(self, title, callback=None, key="", *, targets=None, shortcut=None):
        if shortcut:
            key = shortcut[0]
        item = self._appkit.NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
            title, "performAction:" if callback else None, key
        )
        if shortcut:
            # Show the global shortcut the native way (right-aligned glyphs).
            setter = getattr(item, "setKeyEquivalentModifierMask_", None)
            if setter is not None:
                setter(shortcut[1])
        item.setEnabled_(callback is not None)
        if callback:
            def guarded():
                try:
                    callback()
                except Exception:
                    self.set_result("操作未完成，请打开工作台查看")
            target = (self._target_factory(guarded) if self._target_factory else
                      _target_class(self._appkit).alloc().init())
            target.callback = guarded
            (self._targets if targets is None else targets).append(target)
            item.setTarget_(target)
        return item

    def _add_item(self, title, callback=None, key=""):
        item = self._new_item(title, callback, key)
        self._menu.addItem_(item)
        return item

    def add_action(self, title, callback, key=""):
        self._dispatch(self._add_item, title, callback, key)

    def _separator(self):
        self._menu.addItem_(self._appkit.NSMenuItem.separatorItem())

    def add_separator(self):
        self._dispatch(self._separator)

    def set_action(self, key, title, callback, key_equivalent="", shortcut=None):
        self._dispatch(self._set_action, key, title, callback, key_equivalent, shortcut)

    def _set_action(self, key, title, callback, key_equivalent, shortcut=None):
        targets = []
        item = self._new_item(title, callback, key_equivalent, targets=targets, shortcut=shortcut)
        self._replace_dynamic(key, item, targets)

    def set_submenu(self, key, title, entries):
        """Replace a named menu; entries are (title, callback, checked) tuples."""
        self._dispatch(self._set_submenu, key, title, list(entries))

    def _set_submenu(self, key, title, entries):
        targets = []
        submenu = self._appkit.NSMenu.alloc().init()
        submenu.setAutoenablesItems_(False)
        for entry in entries:
            label, callback, checked = entry[:3]
            shortcut = entry[3] if len(entry) > 3 else None
            child = self._new_item(label, callback, targets=targets, shortcut=shortcut)
            child.setState_(1 if checked else 0)
            submenu.addItem_(child)
        item = self._new_item(title)
        item.setEnabled_(True)
        item.setSubmenu_(submenu)
        self._replace_dynamic(key, item, targets)

    def _replace_dynamic(self, key, item, targets):
        previous = self._dynamic.get(key)
        before = previous[0] if previous else self._open_item
        index = self._menu.indexOfItem_(before)
        if previous:
            self._menu.removeItem_(previous[0])
        self._menu.insertItem_atIndex_(item, index)
        self._dynamic[key] = (item, targets)

    def flash(self, text, seconds=3):
        self._dispatch(self._flash, text, seconds)

    def _flash(self, text, seconds):
        if self._item is None:
            return
        self._set_result(text)
        self._flash_generation += 1
        generation = self._flash_generation
        self._item.button().setTitle_(text[:32])
        if self._call_later is None:
            from PyObjCTools import AppHelper
            self._call_later = AppHelper.callLater

        def restore():
            if self._item is not None and generation == self._flash_generation:
                self._item.button().setTitle_("" if getattr(self, "_brand_image", None) is not None else "Tuomin")
        self._call_later(seconds, restore)

    def update(self, status, detail=""):
        self._dispatch(self._update, status, detail)

    def _update(self, status, detail):
        self._status, self._detail = status, detail
        if self._item is not None:
            self._status_item.setTitle_(status)
            self._detail_item.setTitle_(detail)

    def set_result(self, text):
        self._dispatch(self._set_result, text)

    def _set_result(self, text):
        if self._item is not None:
            self._result_item.setTitle_(text)
            self._result_item.setHidden_(not bool(text))

    def _refresh_login(self):
        try:
            status = self.autostart.status()
            self._login_item.setState_(1 if status == 1 else 0)
            if status == 2:
                self.set_result("请在系统设置中允许 Tuomin 登录时启动")
        except Exception:
            self._login_item.setEnabled_(False)
            self.set_result("登录项不可用，请在应用包中检查系统权限")

    def _toggle_login(self):
        self.autostart.set_enabled(not self.autostart.enabled())
        self._refresh_login()
        if self.on_login:
            self.on_login()

    def confirm_exit(self, unknown=False):
        """Call only on the main thread, after checking recent external calls."""
        if threading.current_thread() is not threading.main_thread():
            raise RuntimeError("退出确认必须在主线程执行")
        if self._appkit is None:
            import AppKit
            self._appkit = AppKit
        alert = self._appkit.NSAlert.alloc().init()
        if unknown:
            alert.setMessageText_("Tuomin 正在处理任务")
            alert.setInformativeText_("暂时无法确认是否有其他工具在使用本地网关。现在退出会中断正在进行的处理。是否退出？")
        else:
            alert.setMessageText_("其他工具最近仍在使用 Tuomin")
            alert.setInformativeText_("退出后这些工具将无法连接本地网关。是否退出？")
        alert.addButtonWithTitle_("继续运行")
        alert.addButtonWithTitle_("退出 Tuomin")
        bring_to_front(self._appkit, alert)
        return alert.runModal() == self._appkit.NSAlertSecondButtonReturn

    def stop(self):
        self._dispatch(self._stop)

    def _stop(self):
        self._flash_generation += 1
        if self._item is not None:
            self._appkit.NSStatusBar.systemStatusBar().removeStatusItem_(self._item)
            self._item = None
            self._targets.clear()
            self._dynamic.clear()
        self.ready.clear()
