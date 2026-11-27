"""Summary-only Windows balloons and bounded, snapshot-bound tray actions."""
from __future__ import annotations

import re
import threading

_OPAQUE_ID = re.compile(r'[A-Za-z0-9_-]{1,64}\Z')
_ACTION_TITLES = {'查看', '回填', '脱敏', '查看残留'}


class Notifier:
    def __init__(self, statusbar, on_action=None):
        self.statusbar, self.on_action = statusbar, on_action
        self._recent = []
        self._closed = False
        self._lock = threading.RLock()

    def start(self):
        pass

    def notify(self, message, *, action=None, action_title=None, on_select=None):
        if not isinstance(message, str):
            raise TypeError('通知内容必须是摘要文本')
        if action is not None and (not isinstance(action, str) or not _OPAQUE_ID.fullmatch(action)):
            raise ValueError('通知动作必须是不透明标识')
        if on_select is not None and not callable(on_select):
            raise TypeError('通知回调必须可调用')
        action_title = action_title or {'refill': '回填', 'redact': '脱敏'}.get(action, '查看')
        if action_title not in _ACTION_TITLES:
            raise ValueError('不支持的通知动作标题')
        with self._lock:
            if self._closed:
                return
            if on_select is not None or action is not None:
                def select():
                    if self._closed:
                        return
                    try:
                        if on_select is not None:
                            on_select()
                        elif self.on_action is not None:
                            self.on_action(action)
                    except Exception:
                        self.statusbar.set_result('请从托盘打开工作台查看')
                self._recent.insert(0, (action_title + '：' + message, select, False))
                del self._recent[8:]
                # Win32 balloons have no per-message activation identity in
                # pystray. Only the explicit menu action can invoke a result;
                # an old balloon must never accidentally open a newer result.
                self.statusbar.set_submenu('clipboard-notices', '最近剪贴板结果', self._recent)
            self.statusbar.flash(message)

    def close(self):
        with self._lock:
            self._closed = True
            self._recent.clear()
