"""Windows notification-area menu; pystray owns its Win32 message loop."""
from __future__ import annotations

import threading


class StatusBar:
    def __init__(self, *, on_open, on_copy, on_quit, on_login=None,
                 autostart=None, tray_module=None, image=None, confirm=None):
        self.on_open, self.on_copy, self.on_quit = on_open, on_copy, on_quit
        self.on_login, self.autostart = on_login, autostart
        self._tray, self._image, self._confirm = tray_module, image, confirm
        self._icon = self._thread = None
        self._dynamic = {}
        self._status = 'Tuomin 正在启动'
        self._detail = '正在连接本地网关'
        self._result = ''
        self._lock = threading.RLock()
        self._stopped = threading.Event()
        self.ready = threading.Event()

    def start(self):
        with self._lock:
            if self._icon is not None or self._stopped.is_set():
                return
            if self._tray is None:
                import pystray
                self._tray = pystray
            if self._image is None:
                from PIL import Image
                from .assets import asset_path
                path = asset_path('tuomin-1024.png')
                if path:
                    with Image.open(path) as source:
                        self._image = source.convert('RGBA').resize((64, 64))
                else:
                    self._image = Image.new('RGBA', (64, 64), '#294b68')
            self._icon = self._tray.Icon('Tuomin', self._image, self._status, self._menu())

            def setup(icon):
                if self._stopped.is_set():
                    icon.stop()
                    return
                icon.visible = True
                self.ready.set()

            def run():
                try:
                    # pystray explicitly permits a worker-thread run loop on
                    # Windows, leaving the main thread to pywebview/WinForms.
                    self._icon.run(setup)
                except Exception:
                    # The startup handshake fails safely without a native
                    # traceback. The window orchestrator will stop the server.
                    self._stopped.set()
                    self._release_failed_setup()
                finally:
                    self.ready.clear()

            self._thread = threading.Thread(target=run, name='tuomin-tray', daemon=True)
            self._thread.start()

    def _release_failed_setup(self):
        # pystray 0.19.5 starts a setup helper before creating its Win32 window.
        # If window creation fails, stop() is a no-op (_running is still false)
        # and that helper waits forever on this queue. Wake it only after
        # _stopped is set, so our setup callback takes its no-show exit branch.
        # This narrow compatibility workaround is covered against the pinned
        # library's actual base class, without creating native windows.
        pending = getattr(self._icon, '_setup_thread', None)
        ready_queue = getattr(self._icon, '_Icon__queue', None)
        if pending is not None and ready_queue is not None and pending.is_alive():
            ready_queue.put(None)
            if pending is not threading.current_thread():
                pending.join(timeout=5)

    def _item(self, title, callback=None, **options):
        def guarded(*_):
            try:
                callback()
            except Exception:
                self.set_result('操作未完成，请打开工作台查看')
        return self._tray.MenuItem(title, guarded if callback else None,
                                   enabled=callback is not None, **options)

    def _menu(self):
        tray = self._tray
        entries = [self._item(self._status), self._item(self._detail)]
        if self._result:
            entries.append(self._item(self._result))
        entries.append(tray.Menu.SEPARATOR)
        for kind, title, content in self._dynamic.values():
            if kind == 'action':
                entries.append(self._item(title, content))
            else:
                children = [self._item(label, callback,
                    checked=lambda item, selected=checked: bool(selected))
                    for label, callback, checked, *_shortcut in content]
                entries.append(tray.MenuItem(title, tray.Menu(*children)))
        entries.extend([
            self._item('打开工作台', self.on_open, default=True),
            self._item('复制网关地址', self.on_copy), tray.Menu.SEPARATOR,
        ])
        available = bool(self.autostart and self.autostart.available)
        entries.append(self._item('登录时启动', self._toggle_login if available else None,
                                  checked=lambda item: bool(available and self.autostart.enabled())))
        entries.extend([tray.Menu.SEPARATOR, self._item('退出 Tuomin', self.on_quit)])
        return tray.Menu(*entries)

    def _refresh(self):
        if self._icon is not None and not self._stopped.is_set():
            self._icon.title = self._status[:127]
            self._icon.menu = self._menu()
            self._icon.update_menu()

    def set_action(self, key, title, callback, key_equivalent='', shortcut=None):
        with self._lock:
            self._dynamic[key] = ('action', title, callback)
            self._refresh()

    def set_submenu(self, key, title, entries):
        with self._lock:
            self._dynamic[key] = ('submenu', title, list(entries))
            self._refresh()

    def update(self, status, detail=''):
        with self._lock:
            self._status, self._detail = status, detail
            self._refresh()

    def set_result(self, text):
        with self._lock:
            self._result = text
            self._refresh()

    def flash(self, text, seconds=3):
        self.set_result(text)
        with self._lock:
            if self._icon is not None and self.ready.is_set() and not self._stopped.is_set():
                try:
                    self._icon.notify(text[:255], 'Tuomin 剪贴板')
                except Exception:
                    # A disabled Windows notification setting never hides the
                    # result: it remains available in the tray menu.
                    pass

    def _toggle_login(self):
        self.autostart.set_enabled(not self.autostart.enabled())
        if self.on_login:
            self.on_login()
        with self._lock:
            self._refresh()

    def confirm_exit(self, unknown=False):
        return bool(self._confirm and self._confirm())

    def stop(self):
        self._stopped.set()
        icon, thread = self._icon, self._thread
        if icon is not None:
            icon.stop()
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=5)
        self.ready.clear()
