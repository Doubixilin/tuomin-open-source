"""Only explicit, origin-checked actions are exposed to web content."""
import secrets
import traceback
from types import SimpleNamespace
import pytest
from tuomin_gateway.shell.bridge import ShellBridge
from tuomin_gateway.shell.clipboard import ClipboardController
from tuomin_gateway.shell.storage import SessionFile


class Registration:
    client=SimpleNamespace(base_url='http://127.0.0.1:8765')
    def sources(self): return [{'app_id':'','label':'通用 PII'}]
    def configure(self,source): pass


class NativeWindow:
    def __init__(self,url):self.url=url
    def get_current_url(self):return self.url


def bridge(tmp_path,url='http://127.0.0.1:8765/ui/'):
    controller=ClipboardController(None,None,{},SessionFile(tmp_path),lambda *args:None)
    b=ShellBridge(tmp_path,Registration(),controller)
    b._window=SimpleNamespace(window=NativeWindow(url))
    return b


def test_settings_defaults_update_and_token_free(tmp_path):
    b=bridge(tmp_path)
    assert b.get_shell_settings()['idle_hours']==8
    result=b.save_shell_settings({'idle_hours':None,'hotkeys':{'enabled':False,'redact':'','refill':''},'onboarding_complete':True})
    assert result['idle_hours'] is None and result['onboarding_complete']
    assert not result['autostart']['available']
    assert not any('token' in key for key in result)
    assert b._controller.idle_hours is None


@pytest.mark.parametrize('url',['http://example.invalid/ui/','http://127.0.0.1:8766/ui/','https://127.0.0.1:8765/ui/'])
def test_remote_navigation_cannot_use_bridge(tmp_path,url):
    b=bridge(tmp_path,url)
    for action in (b.get_shell_settings,lambda:b.save_shell_settings({}),lambda:b.clipboard_action('redact')):
        with pytest.raises(ValueError):action()


def test_native_exception_does_not_enter_webview_traceback(tmp_path):
    b=bridge(tmp_path);secret=secrets.token_urlsafe(32)
    def fail():raise RuntimeError(secret)
    b._registration.sources=fail
    try:b.get_shell_settings()
    except ValueError:
        assert secret not in traceback.format_exc()
    else:pytest.fail('expected safe error')


def test_invalid_preferences_are_not_persisted(tmp_path):
    b=bridge(tmp_path)
    for settings in ({'idle_hours':0},{'idle_hours':True},{'idle_hours':999},{'dictionary_source':'missing'},{'hotkeys':{}}):
        with pytest.raises(ValueError):b.save_shell_settings(settings)
    assert not b._path.exists()


def test_save_file_uses_the_system_panel_and_writes_only_the_chosen_path(tmp_path, monkeypatch):
    import base64
    import sys
    monkeypatch.setitem(sys.modules, 'webview', SimpleNamespace(SAVE_DIALOG=42))
    b = bridge(tmp_path)
    target = tmp_path / 'out' / '报告.redacted.docx'
    seen = {}
    def dialog(kind, **kwargs):
        seen.update(kind=kind, **kwargs)
        return (str(target),)
    b._window.window.create_file_dialog = dialog
    result = b.save_file('../../报告.redacted.docx', base64.b64encode(b'redacted bytes').decode())
    assert seen['kind'] == 42
    assert seen['save_filename'] == '报告.redacted.docx'  # directory parts never reach the panel
    assert result['path'] == str(target) and result['bytes'] == 14
    assert target.read_bytes() == b'redacted bytes'
    b._window.window.create_file_dialog = lambda *args, **kwargs: None
    assert b.save_file('x.md', base64.b64encode(b'x').decode()) == {'cancelled': True}


def test_save_and_reveal_refuse_untrusted_origin_and_unknown_paths(tmp_path):
    b = bridge(tmp_path)
    with pytest.raises(ValueError):
        b.reveal_file(str(tmp_path / 'never-saved.txt'))
    foreign = bridge(tmp_path, url='http://example.invalid/ui/')
    with pytest.raises(ValueError):
        foreign.save_file('a.md', 'YQ==')


def test_windows_defaults_command_and_reveal_use_windows_interfaces(tmp_path, monkeypatch):
    from tuomin_gateway.shell import bridge as module
    monkeypatch.setattr(module.sys, 'platform', 'win32')
    monkeypatch.setattr(module.sys, 'executable', "C:\\合成 工具\\O'Brien\\python.exe")
    b = bridge(tmp_path)
    settings = b.get_shell_settings()
    assert settings['hotkeys']['redact'] == 'ctrl+alt+d'
    assert settings['hotkeys']['refill'] == 'ctrl+alt+r'
    assert settings['mcp_command'].startswith("& 'C:\\合成 工具\\O''Brien\\python.exe'")
    assert settings['platform'] == 'win32'
    target = tmp_path / '合成 报告.txt'
    target.write_text('synthetic', encoding='utf-8')
    b._saved_paths = {str(target)}
    calls = []
    monkeypatch.setattr(module.subprocess, 'Popen', lambda command, **kwargs: calls.append(command))
    assert b.reveal_file(str(target))
    assert calls == [['explorer.exe', '/select,', str(target.resolve())]]


def test_windows_dictionary_switch_waits_for_confirmation(tmp_path, monkeypatch):
    from tuomin_gateway.shell import bridge as module
    monkeypatch.setattr(module.sys, 'platform', 'win32')
    b, saves = bridge(tmp_path), []
    b.save_shell_settings = saves.append
    b._window.window.create_confirmation_dialog = lambda *args: False
    b._select_source('')
    assert saves == []
    b._window.window.create_confirmation_dialog = lambda *args: True
    b._select_source('')
    assert saves == [{'dictionary_source': ''}]


@pytest.mark.parametrize('platform,frozen,cli_exists,use_cli', [
    ('win32', True, True, True),
    ('win32', True, False, False),
    ('win32', False, True, False),
    ('darwin', True, True, False),
])
def test_mcp_command_uses_packaged_windows_cli_only_when_available(
        tmp_path, monkeypatch, platform, frozen, cli_exists, use_cli):
    import shlex
    from tuomin_gateway.shell import bridge as module

    directory = tmp_path / "合成 工具 O'Brien"
    directory.mkdir()
    executable = directory / 'Tuomin.exe'
    cli = directory / 'Tuomin-cli.exe'
    if cli_exists:
        cli.write_bytes(b'synthetic')
    monkeypatch.setattr(module.sys, 'platform', platform)
    monkeypatch.setattr(module.sys, 'frozen', frozen, raising=False)
    monkeypatch.setattr(module.sys, 'executable', str(executable))
    command = bridge(tmp_path).get_shell_settings()['mcp_command']
    selected = str(cli if use_cli else executable)
    if platform == 'win32':
        assert command.startswith("& '" + selected.replace("'", "''") + "' ")
        assert command.endswith("'--mcp-stdio'")
    else:
        assert shlex.split(command) == [selected, '--mcp-stdio']
    assert ('launcher.py' in command) is (not frozen)


def test_save_file_restricts_temporary_before_writing_and_preserves_old_export_on_failure(tmp_path, monkeypatch):
    import base64
    from tuomin_gateway import platform_security
    b = bridge(tmp_path)
    target = tmp_path / '合成报告.txt'
    target.write_bytes(b'old export')
    b._window.window.create_file_dialog = lambda *args, **kwargs: (str(target),)
    def deny(path, *, is_dir):
        from pathlib import Path
        assert not is_dir and Path(path).read_bytes() == b''
        raise PermissionError('synthetic ACL failure')
    monkeypatch.setattr(platform_security, 'restrict_permissions', deny)
    with pytest.raises(ValueError, match='文件未保存'):
        b.save_file(target.name, base64.b64encode(b'synthetic new export').decode())
    assert target.read_bytes() == b'old export'
    assert not list(tmp_path.glob('.合成报告.txt*'))
