"""Local notifications carry only summary text and opaque action identifiers."""
from __future__ import annotations

import re
import secrets
import threading

from .autostart import LoginItem
from .main_thread import call_on_main

_delegate_class = None
_ACTION_TITLES = ("查看", "回填", "脱敏", "查看残留")
_OPAQUE_ID = re.compile(r"[A-Za-z0-9_-]{1,64}\Z")


def _make_delegate(owner):
    global _delegate_class
    if _delegate_class is None:
        from Foundation import NSObject

        class TuominNotificationDelegate(NSObject):
            def userNotificationCenter_didReceiveNotificationResponse_withCompletionHandler_(
                self, center, response, completion
            ):
                try:
                    if self.owner:
                        identifier = str(response.notification().request().identifier())
                        self.owner._respond(identifier)
                except Exception:
                    if self.owner:
                        self.owner.statusbar.set_result("请从菜单打开工作台查看")
                finally:
                    completion()

            def userNotificationCenter_willPresentNotification_withCompletionHandler_(
                self, center, notification, completion
            ):
                # Banner and list are available on every supported macOS.
                completion(16 | 8)
        _delegate_class = TuominNotificationDelegate
    delegate = _delegate_class.alloc().init()
    delegate.owner = owner
    return delegate


class Notifier:
    def __init__(self, statusbar, on_action=None, *, bundled=None, notifications=None,
                 center=None, call_after=None, delegate_factory=None):
        self.statusbar = statusbar
        self.on_action = on_action
        self.bundled = LoginItem().available if bundled is None else bundled
        self._notifications = notifications
        self._center = center
        self._call_after = call_after
        self._delegate_factory = delegate_factory or _make_delegate
        self._delegate = None
        self._authorized = False
        self._actions = {}
        self._started = False
        self._closed = False

    def start(self):
        return call_on_main(self._start, call_after=self._call_after)

    def _post(self, callback):
        if threading.current_thread() is threading.main_thread():
            callback()
        else:
            if self._call_after is None:
                from PyObjCTools import AppHelper
                self._call_after = AppHelper.callAfter
            self._call_after(callback)

    def _start(self):
        if self._started or self._closed:
            return
        self._started = True
        if not self.bundled:
            return
        try:
            if self._notifications is None:
                import UserNotifications
                self._notifications = UserNotifications
            api = self._notifications
            if self._center is None:
                self._center = api.UNUserNotificationCenter.currentNotificationCenter()
            self._delegate = self._delegate_factory(self)
            self._center.setDelegate_(self._delegate)
            categories = []
            for index, title in enumerate(_ACTION_TITLES):
                action = api.UNNotificationAction.actionWithIdentifier_title_options_(
                    "tuomin.open", title, api.UNNotificationActionOptionForeground
                )
                categories.append(api.UNNotificationCategory.categoryWithIdentifier_actions_intentIdentifiers_options_(
                    f"tuomin.action.{index}", [action], [], 0,
                ))
            self._center.setNotificationCategories_(set(categories))
            def authorized(granted, error):
                def finish():
                    self._authorized = bool(granted) and error is None and not self._closed
                self._post(finish)
            self._center.requestAuthorizationWithOptions_completionHandler_(
                api.UNAuthorizationOptionAlert, authorized,
            )
        except Exception:
            # Source/unavailable identity and denied permission use the status
            # title fallback. Never surface native exception details.
            self._authorized = False

    def notify(self, message, *, action=None, action_title=None, on_select=None):
        if not isinstance(message, str):
            raise TypeError("通知内容必须是摘要文本")
        if action is not None and (not isinstance(action, str) or not _OPAQUE_ID.fullmatch(action)):
            raise ValueError("通知动作必须是不透明标识")
        if action_title is None:
            action_title = {"refill": "回填", "redact": "脱敏"}.get(action, "查看")
        if action_title not in _ACTION_TITLES:
            raise ValueError("不支持的通知动作标题")
        if on_select is not None and not callable(on_select):
            raise TypeError("通知回调必须可调用")
        return call_on_main(lambda: self._notify(message, action, action_title, on_select),
                            call_after=self._call_after)

    def _notify(self, message, action, action_title, on_select):
        if self._closed:
            return
        self._start()
        self.statusbar.set_result(message)
        if not self._authorized:
            self.statusbar.flash(message)
            return
        identifier = secrets.token_hex(16)
        try:
            api = self._notifications
            content = api.UNMutableNotificationContent.alloc().init()
            content.setTitle_("Tuomin 剪贴板")
            content.setBody_(message)
            if action or on_select:
                content.setCategoryIdentifier_(f"tuomin.action.{_ACTION_TITLES.index(action_title)}")
                self._actions[identifier] = (action, on_select)
                # Callbacks and masked result snapshots stay in bounded memory;
                # the native payload contains only the opaque identifier.
                while len(self._actions) > 128:
                    del self._actions[next(iter(self._actions))]
            request = api.UNNotificationRequest.requestWithIdentifier_content_trigger_(identifier, content, None)
            def delivered(error):
                def finish():
                    if error is not None and not self._closed:
                        self._actions.pop(identifier, None)
                        self.statusbar.flash(message)
                self._post(finish)
            self._center.addNotificationRequest_withCompletionHandler_(request, delivered)
        except Exception:
            self._actions.pop(identifier, None)
            self.statusbar.flash(message)

    def _respond(self, identifier):
        if self._closed:
            return
        def invoke():
            route = self._actions.get(identifier)
            if route and not self._closed:
                try:
                    action, on_select = route
                    if on_select is not None:
                        on_select()
                    elif self.on_action is not None:
                        self.on_action(action)
                except Exception:
                    self.statusbar.set_result("请从菜单打开工作台查看")
        if self._call_after is None:
            from PyObjCTools import AppHelper
            self._call_after = AppHelper.callAfter
        self._call_after(invoke)

    def close(self):
        return call_on_main(self._close, call_after=self._call_after)

    def _close(self):
        self._closed = True
        self._authorized = False
        self._actions.clear()
        if self._center is not None:
            self._center.setDelegate_(None)
        if self._delegate is not None:
            self._delegate.owner = None
            self._delegate = None
