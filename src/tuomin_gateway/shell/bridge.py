"""Narrow webview bridge for native preferences and explicit clipboard actions."""
from __future__ import annotations
import json
import threading
import shlex
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlsplit
from .storage import read_private, write_private

from .hotkeys import ACTIONS, SELECTION_ACTIONS, format_shortcut, menu_key_equivalent, normalize_shortcut

DEFAULT_HOTKEYS = {'enabled': True, 'redact': 'alt+cmd+d', 'refill': 'alt+cmd+r',
                   'redact_selection': '', 'redact_paste': '', 'refill_paste': ''}
DEFAULTS = {'idle_hours': 8, 'hotkeys': DEFAULT_HOTKEYS, 'dictionary_source': '', 'onboarding_complete': False}
PREFERRED_SOURCE = 'workbench'  # the launcher's default local dictionary app


class ShellBridge:
    def __init__(self, directory, registration, controller, *, demo=False):
        self._path = Path(directory) / 'shell-settings.json'
        saved = read_private(self._path)
        self._settings = {**DEFAULTS, **saved}
        # Older settings files only know redact/refill; new actions start unbound.
        self._settings['hotkeys'] = {**DEFAULT_HOTKEYS, **(saved.get('hotkeys') if isinstance(saved.get('hotkeys'), dict) else {})}
        if sys.platform == 'win32':
            from .hotkeys_windows import DEFAULT_SHORTCUTS
            self._settings['hotkeys'] = {**DEFAULT_HOTKEYS, **DEFAULT_SHORTCUTS,
                                        **(saved.get('hotkeys') if isinstance(saved.get('hotkeys'), dict) else {})}
        self._registration, self._controller = registration, controller
        self._demo = demo
        self._window = self._bar = self._hotkeys = self._login = None
        self._selection = self._keyboard = None
        self._sources = registration.sources()
        preferred = 'demo_contract' if demo else PREFERRED_SOURCE
        if 'dictionary_source' not in saved and any(item['app_id'] == preferred for item in self._sources):
            self._settings['dictionary_source'] = preferred
        self._errors = {}
        self._lock = threading.RLock()

    def _trusted_window(self):
        if self._window:
            current = self._window.window.get_current_url()
            if not current or urlsplit(current).netloc != urlsplit(self._registration.client.base_url).netloc or urlsplit(current).scheme != 'http':
                raise ValueError('native bridge is only available in the local workbench')

    def get_shell_settings(self):
        try:
            self._trusted_window()
            self._sources = self._registration.sources()
            return self._snapshot()
        except Exception:
            raise ValueError('无法读取原生设置，请检查本地服务') from None

    def _snapshot(self):
        session = {k: self._controller.session.get(k) for k in ('namespace_id', 'started_at', 'updated_at', 'segment_count', 'entity_count')}
        from .main_thread import call_on_main
        login_available = bool(self._login and self._login.available and not self._demo)
        enabled = call_on_main(self._login.enabled) if login_available else False
        frozen = getattr(sys, 'frozen', False)
        executable = sys.executable
        if frozen and sys.platform == 'win32':
            cli = Path(executable).with_name('Tuomin-cli.exe')
            if cli.is_file():
                executable = str(cli)
        command = [executable, *([] if frozen else [str(Path(__file__).resolve().parents[3] / 'packaging' / 'launcher.py')]), '--mcp-stdio']
        # The displayed command is intended for the platform's normal shell;
        # PowerShell needs its invocation operator before a quoted executable.
        mcp_command = ('& ' + ' '.join("'" + arg.replace("'", "''") + "'" for arg in command)
                       if sys.platform == 'win32' else shlex.join(command))
        keys = self._settings['hotkeys']
        formatter = format_shortcut
        if sys.platform == 'win32':
            from .hotkeys_windows import format_shortcut as formatter
        return {**self._settings, 'session': session, 'dictionary_sources': self._sources,
                'hotkey_actions': ACTIONS, 'hotkey_display': {name: formatter(keys.get(name)) for name in ACTIONS},
                'accessibility': self._accessibility_state(),
                'autostart': {'available': login_available, 'enabled': enabled}, 'hotkey_errors': self._errors, 'demo': self._demo,
                'platform': sys.platform,
                'dictionary_error': self._controller.configuration_error,
                'mcp_command': mcp_command}

    def save_shell_settings(self, settings):
        try:
            self._trusted_window()
            return self._save_settings(settings)
        except Exception:
            raise ValueError('设置未保存，请检查输入、词典与系统权限') from None

    def _save_settings(self, settings):
        with self._lock, self._controller._lock:
            candidate = dict(self._settings)
            if 'idle_hours' in settings:
                hours = settings['idle_hours']
                if hours is not None and (isinstance(hours, bool) or not isinstance(hours, (int, float)) or not 0 < hours <= 720):
                    raise ValueError('空闲时长须为 0 到 720 小时，或选择永不')
                candidate['idle_hours'] = hours
            if 'hotkeys' in settings:
                keys = settings['hotkeys']
                if not isinstance(keys, dict) or 'enabled' not in keys or not set(keys) <= {'enabled', *ACTIONS} or not isinstance(keys['enabled'], bool):
                    raise ValueError('快捷键设置无效')
                merged = dict(candidate['hotkeys'])
                merged['enabled'] = keys['enabled']
                for name in ACTIONS:
                    if name in keys:
                        value = keys[name]
                        if value is not None and not isinstance(value, str):
                            raise ValueError('快捷键设置无效')
                        normalizer = normalize_shortcut
                        if sys.platform == 'win32':
                            from .hotkeys_windows import normalize_shortcut as normalizer
                        merged[name] = normalizer(value) or ''
                candidate['hotkeys'] = merged
            if 'dictionary_source' in settings:
                source = settings['dictionary_source']
                if source not in {item['app_id'] for item in self._registration.sources()}:
                    raise ValueError('词典来源不可用')
                if source != candidate['dictionary_source'] or self._controller.configuration_error:
                    self._registration.configure(source)
                    self._controller.configuration_error = None
                candidate['dictionary_source'] = source
            if 'onboarding_complete' in settings:
                candidate['onboarding_complete'] = bool(settings['onboarding_complete'])
            if self._hotkeys:
                self._errors = self._hotkeys.configure(self.hotkey_bindings(candidate['hotkeys']))
            self._settings = candidate
            self._controller.idle_hours = candidate['idle_hours']
            write_private(self._path, candidate)
            if 'autostart' in settings and self._login and self._login.available and not self._demo:
                from .main_thread import call_on_main
                call_on_main(lambda: self._login.set_enabled(bool(settings['autostart'])))
            self._refresh_menu()
            return self._snapshot()

    def clipboard_action(self, action):
        try:
            self._trusted_window()
            return self._action(action)
        except Exception:
            raise ValueError('剪贴板操作未完成，请检查本地服务') from None

    # --- native save panel for page-produced files -------------------------
    _MAX_SAVE_BYTES = 256 * 1024 * 1024

    def save_file(self, name, content_b64):
        """Let the user pick where a page-produced file goes (system save panel)."""
        try:
            self._trusted_window()
            return self._save_file(name, content_b64)
        except Exception:
            raise ValueError('文件未保存，请重试或改用浏览器下载') from None

    def _save_file(self, name, content_b64):
        import base64
        import os
        import tempfile
        import webview
        safe_name = Path(str(name or '')).name.strip() or 'tuomin-output'
        data = base64.b64decode(str(content_b64), validate=True)
        if len(data) > self._MAX_SAVE_BYTES:
            raise ValueError('file too large')
        chosen = self._window.window.create_file_dialog(webview.SAVE_DIALOG, save_filename=safe_name)
        if not chosen:
            return {'cancelled': True}
        target = Path(chosen[0] if isinstance(chosen, (list, tuple)) else chosen).expanduser()
        if target.is_dir():
            target = target / safe_name
        target.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix='.' + target.name, dir=target.parent)
        try:
            with os.fdopen(fd, 'wb') as stream:
                from tuomin_gateway.platform_security import restrict_permissions
                restrict_permissions(temporary, is_dir=False)
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, target)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
        with self._lock:
            self._saved_paths = getattr(self, '_saved_paths', set())
            self._saved_paths.add(str(target))
        return {'path': str(target), 'name': target.name, 'bytes': len(data)}

    def reveal_file(self, path):
        """Reveal a file this session saved; other paths are refused."""
        try:
            self._trusted_window()
            with self._lock:
                allowed = str(path) in getattr(self, '_saved_paths', set())
            if not allowed:
                raise ValueError('unknown path')
            if sys.platform == 'win32':
                # Pass the selected path as an argument, never a shell string,
                # including filenames containing spaces or Chinese characters.
                subprocess.Popen(['explorer.exe', '/select,', str(Path(path).resolve())],
                                 creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            elif sys.platform == 'darwin':
                subprocess.run(['open', '-R', str(path)], check=False, timeout=10)
            else:
                subprocess.run(['xdg-open', str(Path(path).parent)], check=False, timeout=10)
            return True
        except Exception:
            raise ValueError('无法在文件管理器中显示该文件') from None

    @staticmethod
    def hotkey_bindings(keys):
        return {name: (keys.get(name) or None) if keys.get('enabled') else None for name in ACTIONS}

    def _accessibility_state(self):
        if self._keyboard is None:
            return {'available': False, 'trusted': False}
        try:
            return {'available': True, 'trusted': bool(self._keyboard.trusted())}
        except Exception:
            return {'available': False, 'trusted': False}

    def accessibility_action(self, action):
        """Page buttons: ask macOS for the permission or open its settings pane."""
        try:
            self._trusted_window()
            if self._keyboard is None:
                raise ValueError('unavailable')
            if action == 'prompt':
                self._keyboard.prompt()
            elif action == 'settings':
                from .selection import open_accessibility_settings
                open_accessibility_settings()
            elif action != 'status':
                raise ValueError('unsupported')
            return self._accessibility_state()
        except Exception:
            raise ValueError('无法读取辅助功能权限状态') from None

    def _action(self, action):
        if action in SELECTION_ACTIONS:
            if self._selection is None:
                self._controller._notice('当前环境不支持选中文字操作，请先复制再用剪贴板动作')
                return False
            with self._controller._lock:
                return self._selection.run(action)
        if action == 'accessibility':
            from .selection import open_accessibility_settings
            open_accessibility_settings()
            return True
        if action == 'redact': return self._controller.redact()
        if action == 'refill': return self._controller.refill()
        if action == 'new': return self._controller.new_session()
        if action == 'clear': return self._controller.new_session(clear=True)
        if action == 'view':
            self._open(self._controller.last_view or {'tab': 'records'})
            return True
        if action in ('records', 'mapping'):
            self._open({'tab': 'records' if action == 'records' else 'original', 'clipboard_mapping': action == 'mapping', 'namespace_id': self._controller.session.get('namespace_id')})
            return True
        raise ValueError('unsupported clipboard action')

    def _background(self, callback):
        def run():
            try:
                callback()
            except Exception:
                self._bar.set_result('操作未完成，请打开工作台检查设置')
        threading.Thread(target=run, name='tuomin-clipboard-action', daemon=True).start()

    def _open(self, detail):
        self._window.show()
        script = 'window.dispatchEvent(new CustomEvent("tuomin-shell-open", {detail: %s}))' % json.dumps(detail, ensure_ascii=False)
        self._window.window.evaluate_js(script)

    def _select_source(self, source):
        from .main_thread import call_on_main
        def confirm():
            if sys.platform == 'win32':
                return self._window.window.create_confirmation_dialog(
                    '切换剪贴板词典来源？',
                    '新词典从下一段脱敏开始生效，已建立的占位符保持不变。')
            from AppKit import NSAlert, NSAlertSecondButtonReturn
            alert = NSAlert.alloc().init()
            alert.setMessageText_('切换剪贴板词典来源？')
            alert.setInformativeText_('新词典从下一段脱敏开始生效，已建立的占位符保持不变。')
            alert.addButtonWithTitle_('取消')
            alert.addButtonWithTitle_('切换')
            import AppKit
            from .statusbar import bring_to_front
            bring_to_front(AppKit, alert)
            return alert.runModal() == NSAlertSecondButtonReturn
        if call_on_main(confirm):
            self.save_shell_settings({'dictionary_source': source})

    def _refresh_menu(self):
        if not self._bar: return
        session = self._controller.session
        import time
        title = '剪贴板会话：未开始'
        if session:
            started = time.strftime('%H:%M', time.localtime(session['started_at']))
            title = f'剪贴板会话：{started} 开始 · {session.get("segment_count",0)} 段 · {session.get("entity_count",0)} 个实体'
        self._bar.set_submenu('clipboard-session', title, [
            ('在工作台查看对照表', lambda:self._background(lambda:self._action('mapping')), False),
            ('新建会话', lambda:self._background(lambda:self._action('new')), False),
            ('结束并清除会话', lambda:self._background(lambda:self._action('clear')), False)])
        keys = self._settings['hotkeys']
        bound = self.hotkey_bindings(keys)
        for name in ('redact', 'refill'):
            self._bar.set_action('clipboard-' + name, ACTIONS[name],
                                 lambda selected=name: self._background(lambda:self._action(selected)),
                                 shortcut=menu_key_equivalent(bound[name]))
        self._bar.set_submenu('clipboard-selection', '选中文字', [
            (ACTIONS[name], lambda selected=name: self._background(lambda:self._action(selected)), False, menu_key_equivalent(bound[name]))
            for name in SELECTION_ACTIONS])
        source = self._settings['dictionary_source']
        sources = self._sources
        self._bar.set_submenu('clipboard-source', '词典来源：' + next((item['label'] for item in sources if item['app_id']==source), '不可用'), [
            (item['label'], lambda selected=item['app_id']: self._background(lambda:self._select_source(selected)), item['app_id']==source) for item in sources])
