"""Notification payload, opaque routing, and source fallback contracts."""
from __future__ import annotations

from types import SimpleNamespace
import weakref

import pytest

from tuomin_gateway.shell.notify import Notifier


class Content:
    @classmethod
    def alloc(cls):
        return cls()

    def init(self):
        return self

    def setTitle_(self, value):
        self.title = value

    def setBody_(self, value):
        self.body = value

    def setCategoryIdentifier_(self, value):
        self.category = value


class Center:
    def __init__(self, granted=True, delivery_error=None):
        self.granted = granted
        self.delivery_error = delivery_error
        self.requests = []

    def setDelegate_(self, delegate):
        self.delegate = delegate

    def setNotificationCategories_(self, categories):
        self.categories = categories

    def requestAuthorizationWithOptions_completionHandler_(self, options, callback):
        callback(self.granted, None)

    def addNotificationRequest_withCompletionHandler_(self, request, callback):
        self.requests.append(request)
        callback(self.delivery_error)


def make_notifier(*, bundled=True, granted=True, delivery_error=None):
    seen, queued = [], []
    statusbar = SimpleNamespace(set_result=lambda text: seen.append(("result", text)),
                                flash=lambda text: seen.append(("flash", text)))
    center = Center(granted, delivery_error)
    api = SimpleNamespace(
        UNNotificationAction=SimpleNamespace(actionWithIdentifier_title_options_=lambda *args: args),
        UNNotificationCategory=SimpleNamespace(
            categoryWithIdentifier_actions_intentIdentifiers_options_=
            lambda identifier, actions, intents, options: (identifier, tuple(actions))),
        UNMutableNotificationContent=Content,
        UNNotificationRequest=SimpleNamespace(requestWithIdentifier_content_trigger_=
            lambda identifier, content, trigger: SimpleNamespace(identifier=identifier, content=content)),
        UNNotificationActionOptionForeground=4,
        UNAuthorizationOptionAlert=4,
    )
    notifier = Notifier(statusbar, on_action=lambda action: seen.append(("action", action)),
                        bundled=bundled, notifications=api, center=center,
                        call_after=queued.append,
                        delegate_factory=lambda owner: SimpleNamespace(owner=owner))
    return notifier, center, seen, queued


def test_source_run_uses_status_title_without_native_center():
    notifier, center, seen, _ = make_notifier(bundled=False)
    notifier.notify("已脱敏 2 处", action="preview")
    assert seen == [("result", "已脱敏 2 处"), ("flash", "已脱敏 2 处")]
    assert center.requests == []


@pytest.mark.parametrize("granted,delivery_error", [(False, None), (True, object())])
def test_permission_denial_or_delivery_failure_falls_back(granted, delivery_error):
    notifier, _, seen, _ = make_notifier(granted=granted, delivery_error=delivery_error)
    notifier.notify("已完成")
    assert seen[-1] == ("flash", "已完成")


def test_notification_carries_summary_and_response_resolves_only_opaque_id():
    notifier, center, seen, queued = make_notifier()
    notifier.notify("已还原 2 处", action="result_42", action_title="查看残留")
    request = center.requests[0]
    assert request.content.body == "已还原 2 处"
    assert request.content.category == "tuomin.action.3"
    assert vars(request.content) == {
        "title": "Tuomin 剪贴板", "body": "已还原 2 处", "category": "tuomin.action.3",
    }
    assert len(request.identifier) == 32
    notifier._respond(request.identifier)
    assert not any(kind == "action" for kind, value in seen)
    queued.pop()()
    assert seen[-1] == ("action", "result_42")
    notifier.close()
    assert center.delegate is None
    assert not notifier._actions
    notifier._respond(request.identifier)
    assert not queued


@pytest.mark.parametrize("action,explicit_title,expected", [
    ("refill", None, "回填"),
    ("redact", None, "脱敏"),
    ("view", None, "查看"),
    ("refill", "查看残留", "查看残留"),
])
def test_action_button_title_matches_routed_action(action, explicit_title, expected):
    notifier, center, seen, queued = make_notifier()
    notifier.notify("合成状态摘要", action=action, action_title=explicit_title)
    request = center.requests[0]
    category = next(item for item in center.categories if item[0] == request.content.category)
    assert category[1][0][1] == expected
    notifier._respond(request.identifier)
    queued.pop()()
    assert seen[-1] == ("action", action)


def test_older_notification_keeps_its_masked_result_out_of_native_payload():
    from tuomin_gateway.shell.app import notify_clipboard_result

    notifier, center, seen, queued = make_notifier()
    notifier.on_action = None
    opened = []
    bridge = SimpleNamespace(_background=lambda callback: callback(), _open=opened.append)
    first = {"tab": "preview", "redacted_text": "<ORG_001>", "blocked_labels": ["ORG"]}
    controller = SimpleNamespace(last_view=first)
    notify_clipboard_result(notifier, bridge, controller, "合成摘要 A", "view")
    first["blocked_labels"].append("PERSON")
    controller.last_view = {"tab": "answer", "error_count": 1, "unknown_placeholders": ["<ORG_999>"]}
    notify_clipboard_result(notifier, bridge, controller, "合成摘要 B", "view")
    for request in center.requests:
        assert set(vars(request.content)) == {"title", "body", "category"}
        assert set(vars(request)) == {"identifier", "content"}
    notifier._respond(center.requests[0].identifier)
    queued.pop()()
    assert opened == [{"tab": "preview", "redacted_text": "<ORG_001>", "blocked_labels": ["ORG"]}]
    notifier._respond(center.requests[1].identifier)
    queued.pop()()
    assert opened[1] == controller.last_view
    assert not any(kind == "action" for kind, _ in seen)


def test_notification_callback_references_are_bounded_and_cleared():
    class Callback:
        def __call__(self):
            pass

    notifier, _, _, _ = make_notifier()
    callback = Callback()
    first = weakref.ref(callback)
    notifier.notify("合成摘要", action="view", on_select=callback)
    del callback
    assert first() is not None
    for _ in range(128):
        notifier.notify("合成摘要", action="view", on_select=Callback())
    assert first() is None
    callback = Callback()
    final = weakref.ref(callback)
    notifier.notify("合成摘要", action="view", on_select=callback)
    del callback
    notifier.close()
    assert final() is None
    assert not notifier._actions


def test_notification_rejects_noncallable_internal_callback():
    notifier, center, _, _ = make_notifier()
    with pytest.raises(TypeError, match="可调用"):
        notifier.notify("合成摘要", on_select="invalid")
    assert not center.requests


@pytest.mark.parametrize("action", ["https://example.test/path", "has whitespace", "a.b.c", "实体甲", "x" * 65])
def test_rejects_nonopaque_action_identifiers(action):
    notifier, center, _, _ = make_notifier()
    with pytest.raises(ValueError, match="不透明"):
        notifier.notify("摘要", action=action)
    assert not center.requests


def test_routing_history_is_bounded_and_cleared_on_close():
    notifier, center, _, _ = make_notifier()
    for index in range(130):
        notifier.notify("已完成", action=f"action_{index}")
    assert len(notifier._actions) == 128
    assert center.requests[0].identifier not in notifier._actions
    notifier.close()
    notifier.notify("已关闭")
    assert len(center.requests) == 130


def test_native_notification_content_can_be_constructed_without_permission_request():
    api = pytest.importorskip("UserNotifications")
    from tuomin_gateway.shell.notify import _make_delegate
    delegate = _make_delegate(None)
    content = api.UNMutableNotificationContent.alloc().init()
    content.setTitle_("Tuomin 平台自检")
    content.setBody_("合成通知摘要")
    request = api.UNNotificationRequest.requestWithIdentifier_content_trigger_("selftest", content, None)
    assert request.identifier() == "selftest"
    assert delegate.respondsToSelector_(
        "userNotificationCenter:didReceiveNotificationResponse:withCompletionHandler:"
    )
