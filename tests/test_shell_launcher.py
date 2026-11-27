"""Native orchestration boundaries without touching a user's runtime files."""
from __future__ import annotations

import importlib.util
import os
from pathlib import Path
import secrets
import socket
import subprocess
import sys
import time
import urllib.request

import pytest

from tuomin_gateway.shell.app import recent_external_calls
from tuomin_gateway.shell.http import LocalClient

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("shell_launcher_under_test", ROOT / "packaging/launcher.py")
launcher = importlib.util.module_from_spec(spec)
spec.loader.exec_module(launcher)


def test_fixed_port_preference_and_occupied_fallback(monkeypatch):
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        occupied = listener.getsockname()[1]
        monkeypatch.setenv("TUOMIN_PORT", str(occupied))
        port, fallback = launcher._pick_shell_port()
        assert fallback and port != occupied
    with socket.socket() as free:
        free.bind(("127.0.0.1", 0))
        preferred = free.getsockname()[1]
    monkeypatch.setenv("TUOMIN_PORT", str(preferred))
    assert launcher._pick_shell_port() == (preferred, False)


def test_instance_url_is_complete_before_publication(tmp_path, monkeypatch):
    path = tmp_path / "instance.url"
    old_url = "http://127.0.0.1:8760/ui/"
    new_url = "http://127.0.0.1:8761/ui/"
    path.write_text(old_url + "\n", encoding="utf-8")
    def fail_before_replace(source, target):
        assert target == path
        assert path.read_text(encoding="utf-8") == old_url + "\n"
        assert source.read_text(encoding="utf-8") == new_url + "\n"
        raise PermissionError("publication failed")

    monkeypatch.setattr(launcher.os, "replace", fail_before_replace)
    with pytest.raises(PermissionError):
        launcher._publish_instance_url(path, new_url)
    assert path.read_text(encoding="utf-8") == old_url + "\n"
    assert list(tmp_path.iterdir()) == [path]


def test_invalid_port_is_not_silently_accepted(monkeypatch):
    for value in ("0", "65536", "not-a-port"):
        monkeypatch.setenv("TUOMIN_PORT", value)
        with pytest.raises(ValueError):
            launcher._pick_shell_port()


def test_recent_external_calls_use_real_ledger_timestamp():
    assert recent_external_calls([{"entry": "api", "ts": 995}], now=1000)
    assert not recent_external_calls([{"entry": "api", "ts": 989}], now=1000)
    assert not recent_external_calls([{"entry": "workbench", "ts": 999}, {"entry": "clipboard", "ts": 999}], now=1000)
    assert not recent_external_calls([{"entry": "api", "ts": "bad"}], now=1000)


def test_local_client_rejects_remote_and_credential_urls():
    for url in ("https://127.0.0.1", "http://example.invalid", "http://user@127.0.0.1"):
        with pytest.raises(ValueError):
            LocalClient(url)


def test_headless_starts_without_shell_and_cleans_lock(tmp_path, monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", lambda host, port, *a, **kw: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", port))])
    config = tmp_path / "synthetic-apps.json"
    config.write_text('{"apps":{}}')
    env = {key: value for key, value in os.environ.items() if not key.startswith("TUOMIN_")}
    env.update(TUOMIN_DATA_DIR=str(tmp_path), TUOMIN_CONFIG=str(config), TUOMIN_ADMIN_TOKEN=secrets.token_urlsafe(32), TUOMIN_ALLOW_PLAINTEXT_STORE="1", TUOMIN_WARM_NER="0", TUOMIN_OPEN_BROWSER="0", HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1")
    process = subprocess.Popen([sys.executable, str(ROOT / "packaging/launcher.py"), "--headless", "--shutdown-on-stdin-close"], env=env, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        deadline = time.monotonic() + 15
        healthy = False
        while time.monotonic() < deadline and process.poll() is None:
            try:
                origin = (tmp_path / "instance.url").read_text().strip().removesuffix("/ui/")
                with urllib.request.urlopen(origin + "/healthz", timeout=.3) as response:
                    healthy = response.status == 200
                if healthy:
                    break
            except OSError:
                pass
            time.sleep(.1)
        assert healthy
    finally:
        process.stdin.close()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
    assert not (tmp_path / "tuomin.lock").exists()
