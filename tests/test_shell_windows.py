"""Windows shell contracts with fake UI: never touches the user's desktop."""
from __future__ import annotations

import builtins
import sys
import threading
from types import SimpleNamespace

import pytest

from tuomin_gateway.shell import app, main_thread
from tuomin_gateway.shell.notify_windows import Notifier
from tuomin_gateway.shell.statusbar_windows import StatusBar


class Item:
    def __init__(self, title, callback, **options):
        self.title, self.callback, self.options = title, callback, options


class Menu:
    SEPARATOR = object()
    def __init__(self, *items):
        self.items = items


class Icon:
    def __init__(self, name, image, title, menu):
        self.name, self.image, self.title, self.menu = name, image, title, menu
        self.notifications, self.updates = [], 0
        self.stopped = threading.Event()

    def run(self, setup):
        setup(self)
        self.stopped.wait(5)

    def stop(self):
        self.stopped.set()

    def update_menu(self):
        self.updates += 1

    def notify(self, text, title):
        self.notifications.append((text, title))


TRAY = SimpleNamespace(MenuItem=Item, Menu=Menu, Icon=Icon)


def tray(**options):
    return StatusBar(on_open=lambda: None, on_copy=lambda: None,
                     on_quit=lambda: None, tray_module=TRAY, image=object(), **options)


def test_windows_backend_does_not_import_any_cocoa_module(monkeypatch):
    monkeypatch.setattr(app.sys, 'platform', 'win32')
    original = builtins.__import__
    def guarded(name, *args, **kwargs):
        assert name.split('.')[0] not in {'objc', 'AppKit', 'Foundation', 'PyObjCTools', 'UserNotifications'}
        return original(name, *args, **kwargs)
    monkeypatch.setattr(builtins, '__import__', guarded)
    backend = app.platform_backend()
    assert backend.gui == 'edgechromium'
    assert backend.Pasteboard.__module__.endswith('pasteboard_windows')
    assert backend.Hotkeys.__module__.endswith('hotkeys_windows')
    assert backend.dispatch(lambda: 42) == 42


def test_windows_worker_calls_do_not_wait_for_cocoa_main_loop(monkeypatch):
    monkeypatch.setattr(main_thread.sys, 'platform', 'win32')
    values = []
    thread = threading.Thread(target=lambda: values.append(main_thread.call_on_main(lambda: 42)))
    thread.start(); thread.join(timeout=2)
    assert not thread.is_alive() and values == [42]


def test_shell_selftest_uses_edgechromium_and_closes_owned_components(monkeypatch):
    from tuomin_gateway.shell import window
    monkeypatch.setattr(app.sys, 'platform', 'win32')
    events = []
    class Window:
        def __init__(self, *args, on_ready, **kwargs):
            self.window = SimpleNamespace(create_confirmation_dialog=lambda *args: True)
            on_ready()
        def show(self):
            events.append('show')
        def request_exit(self):
            events.append('window-exit')
    class Bar:
        def __init__(self, **kwargs):
            self.ready = threading.Event()
        def start(self):
            self.ready.set(); events.append('tray-start')
        def stop(self):
            self.ready.clear(); events.append('tray-stop')
    def start(callback, **options):
        events.append(options)
        callback()
    monkeypatch.setattr(window, 'WorkbenchWindow', Window)
    backend = SimpleNamespace(Pasteboard=lambda: object(), StatusBar=Bar,
                              gui='edgechromium', dispatch=lambda callback: callback())
    server = SimpleNamespace(should_exit=False)
    result = app._run_desktop(backend, SimpleNamespace(start=start), None, server,
        base_url='http://127.0.0.1:8765', port=8765, admin_token='synthetic',
        fallback_port=False, selftest=True, demo=True)
    assert result == 0 and server.should_exit
    assert {'gui': 'edgechromium', 'debug': False} in events
    assert 'window-exit' in events and 'show' not in events


def test_setup_failure_stops_gateway_thread(monkeypatch):
    import sys
    servers = []
    class Server:
        should_exit = False
        def __init__(self, config):
            servers.append(self)
        def run(self):
            while not self.should_exit:
                threading.Event().wait(.01)
    monkeypatch.setitem(sys.modules, 'uvicorn', SimpleNamespace(Config=lambda *args, **kwargs: None, Server=Server))
    monkeypatch.setitem(sys.modules, 'webview', SimpleNamespace())
    monkeypatch.setattr(app, 'platform_backend', lambda: object())
    monkeypatch.setattr(app, 'LocalClient', lambda *args, **kwargs: SimpleNamespace(request=lambda *args: (200, {})))
    def fail(*args, **kwargs):
        raise ValueError('synthetic corrupt preferences')
    monkeypatch.setattr(app, '_run_desktop', fail)
    with pytest.raises(ValueError):
        app.run_shell(object(), port=8765, admin_token='synthetic')
    assert servers[0].should_exit
    assert not any(thread.name == 'tuomin-gateway' for thread in threading.enumerate())


def test_tray_updates_dynamic_actions_and_cleans_up():
    bar, called = tray(), []
    bar.start()
    try:
        assert bar.ready.wait(2)
        bar.set_action('redact', '脱敏', lambda: called.append('redact'))
        bar.set_action('redact', '新标题', lambda: called.append('new'))
        bar.set_submenu('source', '词典', [('合成词典', lambda: called.append('source'), True)])
        bar.update('Tuomin 运行中', '检测器就绪')
        bar.flash('已脱敏 1 处')
        action = next(item for item in bar._icon.menu.items if isinstance(item, Item) and item.title == '新标题')
        action.callback()
        assert called == ['new']
        assert len([entry for entry in bar._dynamic if entry == 'redact']) == 1
        assert bar._icon.notifications == [('已脱敏 1 处', 'Tuomin 剪贴板')]
        assert bar._icon.title == 'Tuomin 运行中'
        assert bar._result == '已脱敏 1 处'
    finally:
        bar.stop()
    assert not bar.ready.is_set() and not bar._thread.is_alive()


def test_notification_failure_preserves_visible_summary():
    bar = tray()
    bar.start()
    try:
        assert bar.ready.wait(2)
        def denied(*args):
            raise RuntimeError('notifications disabled')
        bar._icon.notify = denied
        bar.flash('已还原 1 处')
        assert bar._result == '已还原 1 处'
    finally:
        bar.stop()


def test_tray_confirmation_requires_explicit_yes():
    assert not tray().confirm_exit()
    assert not tray(confirm=lambda: False).confirm_exit()
    assert tray(confirm=lambda: True).confirm_exit()


@pytest.mark.skipif(sys.platform != 'win32', reason='Windows pystray setup lifecycle')
def test_early_pystray_failure_releases_its_pending_setup_thread():
    # Use pystray's real setup queue/thread implementation but no platform
    # Icon: importing its base class does not create windows or tray entries.
    import pystray
    from pystray._base import Icon as BaseIcon
    class FailingIcon(BaseIcon):
        def _run(self):
            raise RuntimeError('synthetic native window creation failure')
        def _show(self):
            pytest.fail('failed startup must never show a tray icon')
    module = SimpleNamespace(Icon=FailingIcon, Menu=pystray.Menu, MenuItem=pystray.MenuItem)
    bar = StatusBar(on_open=lambda: None, on_copy=lambda: None,
                    on_quit=lambda: None, tray_module=module, image=object())
    bar.start()
    bar._thread.join(timeout=2)
    try:
        assert not bar._thread.is_alive()
        assert not bar._icon._setup_thread.is_alive()
        assert not bar.ready.is_set() and not bar._icon.visible
    finally:
        bar.stop()


def test_recent_notification_action_keeps_its_original_masked_snapshot():
    bar, opened = tray(), []
    notifier = Notifier(bar)
    bridge = SimpleNamespace(_background=lambda callback: callback(), _open=opened.append)
    controller = SimpleNamespace(last_view={'redacted_text': '<ORG_001>'})
    app.notify_clipboard_result(notifier, bridge, controller, '已脱敏 1 处', 'view')
    old_action = notifier._recent[0][1]
    controller.last_view['redacted_text'] = '<ORG_002>'
    app.notify_clipboard_result(notifier, bridge, controller, '已脱敏 2 处', 'view')
    old_action()
    assert opened == [{'redacted_text': '<ORG_001>'}]
    for _ in range(20):
        notifier.notify('已脱敏 1 处', action='view')
    assert len(notifier._recent) == 8
    notifier.close()
    old_action()
    assert len(opened) == 1


@pytest.mark.parametrize('options', [dict(action='secret path/value'), dict(action_title='unsupported'), dict(on_select='not callable')])
def test_notification_rejects_noncontract_actions(options):
    with pytest.raises((ValueError, TypeError)):
        Notifier(tray()).notify('合成摘要', **options)
