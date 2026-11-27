"""Windows integration paths without touching desktop state or user settings."""
from __future__ import annotations

import os
from pathlib import Path
import sys
import subprocess
import queue
import threading

from tuomin_gateway import gateway_discovery as discovery
from tuomin_gateway.document import legacy_convert
from tuomin_gateway.shell.defaults import ensure_default_workbench_app
from tests.permission_assertions import assert_owner_private


def test_windows_gateway_uses_appdata_and_explicit_runtime_first(tmp_path, monkeypatch):
    monkeypatch.setattr(discovery.sys, 'platform', 'win32')
    roaming = tmp_path / '用户 空格' / 'Tuomin'
    roaming.mkdir(parents=True)
    (roaming / 'instance.url').write_text('http://127.0.0.1:54321/ui/', encoding='utf-8')
    env = {'APPDATA': str(roaming.parent)}
    assert discovery.desktop_instance_url(env) == 'http://127.0.0.1:54321'
    custom = tmp_path / '自定义'
    custom.mkdir()
    (custom / 'instance.url').write_text('http://127.0.0.1:54322/ui/', encoding='utf-8')
    env['TUOMIN_DATA_DIR'] = str(custom)
    assert discovery.candidate_sources(env=env, scan_ports=[]) == [('http://127.0.0.1:54322', 'desktop-instance')]


def test_windows_office_converter_discovered_without_path_change(tmp_path, monkeypatch):
    if os.name != 'nt':
        import pytest
        pytest.skip('Windows installation paths')
    monkeypatch.delenv('TUOMIN_SOFFICE', raising=False)
    monkeypatch.setattr(legacy_convert.shutil, 'which', lambda _: None)
    for key in ('ProgramW6432', 'ProgramFiles', 'ProgramFiles(x86)', 'LOCALAPPDATA'):
        monkeypatch.delenv(key, raising=False)
    exe = tmp_path / 'Program Files 中文' / 'LibreOffice' / 'program' / 'soffice.exe'
    exe.parent.mkdir(parents=True)
    exe.write_bytes(b'synthetic path fixture, never executed')
    monkeypatch.setenv('ProgramFiles', str(exe.parents[2]))
    assert legacy_convert.find_soffice() == str(exe)


def test_first_run_registry_and_dictionary_are_owner_private(tmp_path):
    config = tmp_path / '桌面数据' / 'apps.json'
    assert ensure_default_workbench_app(config, config.parent)
    assert_owner_private(config, is_dir=False)
    assert_owner_private(config.parent / 'dictionaries' / 'workbench.json', is_dir=False)
    assert_owner_private(config.parent / 'dictionaries', is_dir=True)


def test_managed_parent_pipe_waits_until_eof_and_allows_native_imports():
    import pytest
    pytest.importorskip('numpy')
    code = '''
import threading
from tuomin_gateway.runtime import wait_for_parent_input_close
waiter = threading.Thread(target=wait_for_parent_input_close)
waiter.start()
import numpy
print("native-ready", flush=True)
waiter.join()
print("parent-closed", flush=True)
'''
    env = {**os.environ, 'PYTHONPATH': str(Path(__file__).resolve().parents[1] / 'src')}
    process = subprocess.Popen([sys.executable, '-X', 'utf8', '-c', code], env=env,
                               stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    lines = queue.Queue()
    reader = threading.Thread(target=lambda: [lines.put(line) for line in process.stdout], daemon=True)
    reader.start()
    try:
        process.stdin.write(b'synthetic supervisor input')
        process.stdin.flush()
        # Keep the pipe OPEN during the native import, then close it and
        # require the watcher to return. Never wait indefinitely on readline.
        assert lines.get(timeout=20).strip() == b'native-ready'
        assert process.poll() is None
        process.stdin.close()
        assert process.wait(timeout=10) == 0
        assert lines.get(timeout=5).strip() == b'parent-closed'
    finally:
        if process.poll() is None:
            process.terminate()
            process.wait(timeout=5)
