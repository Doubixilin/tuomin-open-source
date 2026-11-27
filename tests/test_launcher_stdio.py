"""Windowed startup retains redirected protocols without allocating a console."""
from __future__ import annotations

import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest


LAUNCHER = Path(__file__).resolve().parents[1] / "packaging/launcher.py"
spec = importlib.util.spec_from_file_location("windowed_launcher_under_test", LAUNCHER)
launcher = importlib.util.module_from_spec(spec)
spec.loader.exec_module(launcher)


def test_existing_console_streams_are_not_replaced(monkeypatch):
    streams = SimpleNamespace(stdin=io.StringIO(), stdout=io.StringIO(), stderr=io.StringIO())
    monkeypatch.setattr(launcher, "sys", streams)
    monkeypatch.setattr(launcher, "os", SimpleNamespace(name="nt", devnull=os.devnull))
    monkeypatch.setattr(launcher, "_open_inherited_windows_stream", lambda *args: pytest.fail("live streams replaced"))
    launcher._prepare_standard_streams()


def test_unredirected_gui_uses_null_streams_and_can_initialize_logging(monkeypatch):
    streams = SimpleNamespace(stdin=None, stdout=None, stderr=None,
                              __stdin__=None, __stdout__=None, __stderr__=None)
    monkeypatch.setattr(launcher, "sys", streams)
    monkeypatch.setattr(launcher, "os", SimpleNamespace(name="nt", devnull=os.devnull))
    monkeypatch.setattr(launcher, "_open_inherited_windows_stream", lambda *args: None)
    launcher._prepare_standard_streams()
    try:
        assert streams.stdin.read() == ""
        for stream in (streams.stdout, streams.stderr):
            # Windows NUL is a character device and can report isatty=True;
            # logging only requires a working method rather than a None stream.
            assert isinstance(stream.isatty(), bool)
            stream.write("synthetic startup status\n")
            stream.flush()
        original = streams.stdout
        launcher._prepare_standard_streams()
        assert streams.stdout is original is streams.__stdout__
    finally:
        for stream in (streams.stdin, streams.stdout, streams.stderr):
            stream.close()


@pytest.mark.skipif(os.name != "nt", reason="Windows inherited handle contract")
@pytest.mark.parametrize("output_to_file", [False, True])
def test_windowed_interpreter_recovers_real_inherited_pipes_without_console(tmp_path, output_to_file):
    pythonw = Path(sys.executable).with_name("pythonw.exe")
    assert pythonw.is_file(), "Windows test environment requires its matching GUI interpreter"
    code = """
import ctypes, json, runpy, sys
# Reproduce PyInstaller's windowed stream state, keeping original handles alive.
originals = (sys.stdin, sys.stdout, sys.stderr)
sys.stdin = sys.stdout = sys.stderr = None
sys.__stdin__ = sys.__stdout__ = sys.__stderr__ = None
namespace = runpy.run_path(sys.argv[1])
namespace['_prepare_standard_streams']()
payload = sys.stdin.readline().strip()
kernel = ctypes.WinDLL('kernel32')
kernel.GetConsoleWindow.restype = ctypes.c_void_p
print(json.dumps({'payload': payload, 'console': bool(kernel.GetConsoleWindow())}, ensure_ascii=False), flush=True)
print('synthetic diagnostic', file=sys.stderr, flush=True)
"""
    command = [str(pythonw), "-X", "utf8", "-c", code, str(LAUNCHER)]
    if output_to_file:
        path = tmp_path / "redirected.log"
        with path.open("wb") as output:
            completed = subprocess.run(command, input="合成管道文本\n".encode("utf-8"),
                                       stdout=output, stderr=subprocess.PIPE, timeout=15)
        stdout = path.read_bytes()
    else:
        completed = subprocess.run(command, input="合成管道文本\n".encode("utf-8"),
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=15)
        stdout = completed.stdout
    assert completed.returncode == 0
    assert json.loads(stdout.decode("utf-8")) == {"payload": "合成管道文本", "console": False}
    assert completed.stderr.decode("utf-8").strip() == "synthetic diagnostic"
