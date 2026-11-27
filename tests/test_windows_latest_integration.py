"""New native-shell features on Windows without real keyboard or desktop input."""
from __future__ import annotations

import ctypes
import os
import runpy
from pathlib import Path
from types import SimpleNamespace

import pytest

from tuomin_gateway import agent_clients
from tuomin_gateway.shell.hotkeys_windows import parse_shortcut, normalize_shortcut, format_shortcut
from tuomin_gateway.shell.selection_windows import Input, Keyboard
from tuomin_gateway.shell.selection import KEY_C, KEY_V
from test_shell_bridge import bridge
from test_shell_selection import Board, Keyboard as FakeKeyboard, Controller, flow

ROOT = Path(__file__).resolve().parents[1]


def test_windows_shortcut_recording_storage_and_menu(tmp_path, monkeypatch):
    monkeypatch.setattr('tuomin_gateway.shell.bridge.sys.platform', 'win32')
    native = bridge(tmp_path)
    state = native.save_shell_settings({'hotkeys': {'enabled': True, 'redact_paste': 'control+windows+V'}})
    assert state['hotkeys']['redact'] == 'ctrl+alt+d'
    assert state['hotkeys']['redact_paste'] == 'ctrl+win+v'
    assert state['hotkey_display']['redact_paste'] == 'Ctrl+Win+V'
    assert parse_shortcut('f24') == (0x87, 0)
    assert normalize_shortcut('ALT+CONTROL+;') == 'ctrl+alt+;'
    assert format_shortcut('shift+f13') == 'Shift+F13'
    for value in ('cmd+d', 'shift+d', 'f12', 'd'):
        with pytest.raises(ValueError):
            parse_shortcut(value)
    from test_shell_windows import tray
    bar = tray()
    native._bar = bar
    native._refresh_menu()
    assert 'clipboard-selection' in bar._dynamic


@pytest.mark.skipif(os.name != 'nt', reason='Win32 ABI sizes')
def test_windows_input_abi_and_ctrl_sequence_without_sending_input():
    calls = []
    def send(count, events, size):
        calls.append((count, [(e.type, e.ki.wVk, e.ki.dwFlags) for e in events], size))
        return count
    keyboard = Keyboard(user=SimpleNamespace(SendInput=send))
    keyboard.press(KEY_C, 0)
    keyboard.press(KEY_V, 0)
    assert ctypes.sizeof(Input) == (40 if ctypes.sizeof(ctypes.c_void_p) == 8 else 28)
    assert calls[0][1] == [(1, 0x11, 0), (1, 0x43, 0), (1, 0x43, 2), (1, 0x11, 2)]
    assert calls[1][1][1][1] == 0x56


def test_partial_windows_input_releases_keys_and_reports_failure():
    calls = []
    def send(count, events, size):
        calls.append(count)
        return 1
    with pytest.raises(RuntimeError):
        Keyboard(user=SimpleNamespace(SendInput=send)).press(KEY_C, 0)
    assert calls == [4, 2]


def test_focus_change_cancels_paste_and_restores_clipboard():
    board, notes = Board(), []
    keyboard = FakeKeyboard(board)
    targets = iter((100, 200))
    keyboard.foreground = lambda: next(targets)
    assert not flow(board, keyboard, Controller(board), notes).run('refill_paste')
    assert keyboard.pressed == [KEY_C] and board.text == '之前的剪贴板'


def test_selection_exception_restores_original_clipboard():
    board, notes = Board(), []
    controller = Controller(board)
    def fail():
        raise RuntimeError('synthetic failure')
    controller.redact = fail
    with pytest.raises(RuntimeError):
        flow(board, FakeKeyboard(board), controller, notes).run('redact_paste')
    assert board.text == '之前的剪贴板'


def test_mcp_uses_cli_and_keeps_data_directory_environment(tmp_path, monkeypatch):
    executable = tmp_path / '中文 文件夹' / 'Tuomin.exe'
    executable.parent.mkdir()
    executable.write_bytes(b'synthetic')
    executable.with_name('Tuomin-cli.exe').write_bytes(b'synthetic')
    monkeypatch.setattr(agent_clients.sys, 'platform', 'win32')
    monkeypatch.setattr(agent_clients.sys, 'frozen', True, raising=False)
    monkeypatch.setattr(agent_clients.sys, 'executable', str(executable))
    data = str(tmp_path / '独立 数据')
    monkeypatch.setenv('TUOMIN_DATA_DIR', data)
    command, args, env = agent_clients.mcp_command()
    assert command.endswith('Tuomin-cli.exe') and args == ['--mcp-stdio']
    assert env['TUOMIN_DATA_DIR'] == data and os.environ['TUOMIN_DATA_DIR'] == data


def test_windows_test_environment_isolates_all_home_paths(tmp_path):
    helper = runpy.run_path(str(ROOT / 'scripts/windows_validation.py'))
    env = helper['isolated_environment'](tmp_path, Path(__import__('sys').executable))
    for name in ('HOME', 'USERPROFILE', 'APPDATA', 'LOCALAPPDATA'):
        assert Path(env[name]).is_relative_to(tmp_path)


def test_repeated_client_backups_preserve_each_original(tmp_path):
    path = tmp_path / 'client.json'
    path.write_bytes(b'first')
    first = agent_clients._backup(path)
    path.write_bytes(b'second')
    second = agent_clients._backup(path)
    assert first != second and Path(first).read_bytes() == b'first' and Path(second).read_bytes() == b'second'


def test_windows_manual_is_offline_and_uses_windows_paths(tmp_path):
    build = runpy.run_path(str(ROOT / 'scripts/build_manual.py'))['build']
    html = build(tmp_path / 'manual.html', windows=True).read_text(encoding='utf-8')
    assert '{{' not in html and 'data:image/webp;base64,' in html
    assert '%APPDATA%\\Tuomin' in html and 'Tuomin-cli.exe' in html and 'Ctrl+Alt+D' in html
    assert 'Mac 界面' in html and 'DPAPI' in html


def test_windows_desktop_startup_registers_new_actions_and_quits_without_cocoa(tmp_path, monkeypatch):
    import builtins
    import threading
    from tuomin_gateway.shell import app, window, quitting, registration, selection_windows
    from test_shell_windows import TRAY
    from tuomin_gateway.shell.statusbar_windows import StatusBar
    from tuomin_gateway.shell.notify_windows import Notifier
    monkeypatch.setattr(app.sys, 'platform', 'win32')
    monkeypatch.setenv('TUOMIN_DATA_DIR', str(tmp_path))
    events, bars = [], []
    class Registration:
        tokens = {'namespace': 'synthetic'}
        def __init__(self, client, *args): self.client = client
        def sources(self): return [{'app_id': '', 'label': '通用 PII'}]
        def configure(self, source): pass
    class Window:
        def __init__(self, base, token, on_ready, **kwargs):
            self.window = SimpleNamespace(create_confirmation_dialog=lambda *a: True)
            on_ready()
        def show(self): pass
        def request_exit(self): events.append('window-exit')
    class Hotkeys:
        def __init__(self, callbacks): self.callbacks = callbacks
        def configure(self, bindings):
            assert set(bindings) == set(self.callbacks)
            events.append(('bindings', bindings))
            return {}
        def close(self): events.append('hotkeys-close')
    class Quitter:
        def __init__(self, **kwargs): self.options = kwargs
        def should_terminate(self, *args): self.options['prepare_exit']()
        def force(self): self.should_terminate()
    def bar_factory(**kwargs):
        bar = StatusBar(**kwargs, tray_module=TRAY, image=object())
        bars.append(bar)
        return bar
    class Client:
        def request(self, *args, **kwargs):
            bars[0].on_quit()
            return 200, {}
    monkeypatch.setattr(registration, 'ClipboardRegistration', Registration)
    monkeypatch.setattr(window, 'WorkbenchWindow', Window)
    monkeypatch.setattr(quitting, 'QuitCoordinator', Quitter)
    monkeypatch.setattr(selection_windows, 'Keyboard', lambda: SimpleNamespace(trusted=lambda: True))
    original_import = builtins.__import__
    def guarded(name, *args, **kwargs):
        assert name.split('.')[0] not in {'objc', 'AppKit', 'Foundation', 'PyObjCTools'}
        return original_import(name, *args, **kwargs)
    monkeypatch.setattr(builtins, '__import__', guarded)
    monkeypatch.setattr(app, "LocalClient", lambda *args, **kwargs: Client())
    backend = SimpleNamespace(Pasteboard=Board, StatusBar=bar_factory, Notifier=Notifier,
                              Hotkeys=Hotkeys, gui='edgechromium', dispatch=lambda callback: callback())
    webview = SimpleNamespace(start=lambda callback, **kwargs: callback(), OPEN_DIALOG=1)
    server = SimpleNamespace(should_exit=False)
    assert app._run_desktop(backend, webview, Client(), server, base_url='http://127.0.0.1:8765',
                           port=8765, admin_token='synthetic', fallback_port=False, selftest=False,
                           demo=True, cleanup=lambda: events.append('cleanup')) == 0
    assert 'cleanup' in events and server.should_exit
    assert 'redact-files' in bars[0]._dynamic and 'clipboard-selection' in bars[0]._dynamic
    assert next(e[1] for e in events if isinstance(e, tuple))['redact_paste'] is None
    assert not any(t.name == 'tuomin-tray' for t in threading.enumerate())
