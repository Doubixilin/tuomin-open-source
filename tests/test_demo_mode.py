"""Synthetic demo isolation and standard Office fixture validation."""
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
from pathlib import Path
import secrets
import socket
import subprocess
import sys
import tempfile
import time
from urllib.request import ProxyHandler, build_opener

import pytest

from tuomin_gateway.document.extract import extract_document
from tuomin_gateway.document.docx_patch import probe_docx
from tuomin_gateway.document.office_patch import probe_office
from tuomin_gateway.runtime import configure_admin_token
from tuomin_gateway.shell.demo import SAMPLE_NAMES, demo_environment
from tests.permission_assertions import assert_owner_private


ROOT = Path(__file__).resolve().parents[1]


def _runtime_environment():
    return {key: value for key, value in os.environ.items() if key.startswith("TUOMIN_")}


def _launcher():
    spec = importlib.util.spec_from_file_location("demo_launcher_under_test", ROOT / "packaging/launcher.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _snapshot(directory):
    return {str(path.relative_to(directory)): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in directory.rglob("*") if path.is_file()}


def test_demo_environment_restores_all_runtime_settings_and_cleans(tmp_path, monkeypatch):
    monkeypatch.setenv("TUOMIN_DATA_DIR", str(tmp_path / "synthetic-formal"))
    monkeypatch.setenv("TUOMIN_CONFIG", str(tmp_path / "synthetic-formal" / "apps.json"))
    monkeypatch.setenv("TUOMIN_MAPPING_DIR", str(tmp_path / "synthetic-formal" / "maps"))
    monkeypatch.setenv("TUOMIN_ADMIN_TOKEN", secrets.token_urlsafe(32))
    monkeypatch.setenv("TUOMIN_GATEWAY_URL", "http://127.0.0.1:9876")
    monkeypatch.setenv("TUOMIN_LICENSE_REQUIRED", "1")
    monkeypatch.setenv("TUOMIN_KEYCHAIN_SERVICE", "synthetic-formal-service")
    monkeypatch.setenv("TUOMIN_UPSTREAM_OPENAI", "https://synthetic.invalid")
    monkeypatch.setenv("TUOMIN_PORT", "9876")
    monkeypatch.setenv("TUOMIN_OPEN_BROWSER", "0")
    before = _runtime_environment()
    with demo_environment() as demo:
        root = demo.root
        assert root.name.startswith(f"tuomin-demo-{os.getpid()}-")
        assert_owner_private(root, is_dir=True)
        assert set(_runtime_environment()) == {
            "TUOMIN_PORT", "TUOMIN_OPEN_BROWSER", "TUOMIN_DATA_DIR", "TUOMIN_CONFIG",
            "TUOMIN_MAPPING_DIR", "TUOMIN_ALLOW_PLAINTEXT_STORE", "TUOMIN_DEMO_SAMPLE_DIR",
            "TUOMIN_WORKBUDDY_MODELS",
        } | ({"TUOMIN_NO_BROWSER", "TUOMIN_WARM_NER", "TUOMIN_NER_MODEL_DIR"} & before.keys())
        assert os.environ["TUOMIN_PORT"] == "9876"
        assert os.environ["TUOMIN_OPEN_BROWSER"] == "0"
        assert os.environ["TUOMIN_ALLOW_PLAINTEXT_STORE"] == "1"
        assert Path(os.environ["TUOMIN_DEMO_SAMPLE_DIR"]) == demo.samples_dir
        config = Path(os.environ["TUOMIN_CONFIG"])
        assert config.is_relative_to(root)
        assert_owner_private(config, is_dir=False)
        registry = json.loads(config.read_text(encoding="utf-8"))
        for entry in registry["apps"].values():
            assert Path(entry["dictionary"]).is_relative_to(root)
        configure_admin_token(root)
        assert bool(os.environ["TUOMIN_ADMIN_TOKEN"] != before["TUOMIN_ADMIN_TOKEN"])
        assert_owner_private(root / "admin-token", is_dir=False)
        os.environ["TUOMIN_CREATED_INSIDE_DEMO"] = "synthetic"
    assert not root.exists()
    assert bool(_runtime_environment() == before)


def test_demo_exception_cleans_directory_and_restores_environment():
    before = _runtime_environment()
    with pytest.raises(RuntimeError, match="synthetic failure"):
        with demo_environment() as demo:
            root = demo.root
            raise RuntimeError("synthetic failure")
    assert not root.exists()
    assert bool(_runtime_environment() == before)


def test_demo_service_never_touches_inaccessible_formal_directory(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from tuomin_gateway import store as store_module
    from tuomin_gateway.service.app import create_app
    from tuomin_gateway.service.registry import AppRegistry

    formal = tmp_path / "synthetic-formal"
    formal.mkdir()
    config = formal / "apps.json"
    config.write_text('{"apps": {}}', encoding="utf-8")
    (formal / "marker").write_text("synthetic untouched marker", encoding="utf-8")
    before = _snapshot(formal)
    monkeypatch.setenv("TUOMIN_DATA_DIR", str(formal))
    monkeypatch.setenv("TUOMIN_CONFIG", str(config))
    monkeypatch.setenv("TUOMIN_MAPPING_DIR", str(formal / "mappings"))
    monkeypatch.setenv("TUOMIN_LICENSE_REQUIRED", "1")

    def forbidden_keychain(*args, **kwargs):
        raise AssertionError("demo must not construct a Keychain cipher")

    monkeypatch.setattr(store_module, "MacOSAesGcmMappingCipher", forbidden_keychain)
    formal.chmod(0)
    try:
        with demo_environment() as demo:
            configure_admin_token(demo.root)
            app = create_app()
            try:
                client = TestClient(app, base_url="http://127.0.0.1", headers={"x-tuomin-admin-token": os.environ["TUOMIN_ADMIN_TOKEN"]})
                response = client.get("/api/v1/license/status")
                assert response.status_code == 200
                assert response.json()["enforcement"] == "disabled"
                response = client.put("/admin/apps/synthetic_extra", json={"profile": "kb"})
                assert response.status_code == 200
                registry = AppRegistry.load()
                assert "synthetic_extra" in registry.known_apps()
                profile = registry.resolve_profile("demo_contract")
                assert profile.use_ner is False
                for app_id in ("demo_contract", "demo_general"):
                    response = client.post(
                        "/api/v1/documents/parse",
                        params={"filename": "synthetic-contract.docx", "app_id": app_id},
                        content=(demo.samples_dir / "synthetic-contract.docx").read_bytes(),
                    )
                    assert response.status_code == 200
                    assert response.json()["egress"]["egress_allowed"] is True
                store = store_module.MappingStore(os.environ["TUOMIN_MAPPING_DIR"])
                output = store.save_payload("synthetic-demo", b"fictional fixture")
                assert output.is_relative_to(demo.root)
                assert store.safe_status()["encrypted_at_rest"] is (os.name == "nt")
            finally:
                app.state.job_store.close()
    finally:
        formal.chmod(0o700)
    assert _snapshot(formal) == before


@pytest.mark.parametrize("name", SAMPLE_NAMES)
def test_demo_samples_are_valid_extractable_office_files(name):
    source = ROOT / "demo" / "samples" / name
    data = source.read_bytes()
    if source.suffix == ".docx":
        probe_docx(data)
    else:
        probe_office(data, "xlsx")
    extracted = extract_document(source, "synthetic_demo_sample")
    text = "\n".join(block.text for block in extracted.blocks)
    assert "虚构星橙工程有限公司" in text
    assert "虚构云帆技术有限公司" in text
    assert "DEMO-2026-001" in text
    assert "虚构" in text


def test_demo_instance_lock_is_independent_of_formal_lock(tmp_path):
    launcher = _launcher()
    formal = tmp_path / "synthetic-formal"
    formal.mkdir()
    formal_lock = formal / "tuomin.lock"
    formal_url = formal / "instance.url"
    assert launcher._acquire_instance_lock(formal_lock, formal_url)
    before = _snapshot(formal)
    with demo_environment() as demo:
        demo_lock = demo.root / "tuomin-demo.lock"
        demo_url = demo.root / "instance.url"
        assert launcher._acquire_instance_lock(demo_lock, demo_url)
        assert demo_lock.exists() and formal_lock.exists()
        launcher._cleanup_instance_files(demo_lock, demo_url)
    assert _snapshot(formal) == before
    launcher._cleanup_instance_files(formal_lock, formal_url)


def test_headless_demo_coexists_with_formal_and_leaves_it_unchanged(tmp_path, monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", lambda host, port, *a, **kw: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", port))])
    opener = build_opener(ProxyHandler({}))
    formal = tmp_path / "synthetic-formal"
    formal.mkdir()
    config = formal / "apps.json"
    config.write_text('{"apps":{}}', encoding="utf-8")
    env = {key: value for key, value in os.environ.items() if not key.startswith("TUOMIN_")}
    env.update(TUOMIN_DATA_DIR=str(formal), TUOMIN_CONFIG=str(config),
               TUOMIN_ADMIN_TOKEN=secrets.token_urlsafe(32), TUOMIN_ALLOW_PLAINTEXT_STORE="1",
               TUOMIN_WARM_NER="0", TUOMIN_OPEN_BROWSER="0", HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1")
    child_temp = tmp_path / "child-temp"
    child_temp.mkdir()
    env.update(TEMP=str(child_temp), TMP=str(child_temp), TMPDIR=str(child_temp))
    formal_process = subprocess.Popen([sys.executable, str(ROOT / "packaging/launcher.py"), "--headless", "--shutdown-on-stdin-close"], env=env,
                                      stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    demo_process = None

    def healthy(directory):
        try:
            origin = (directory / "instance.url").read_text().strip().removesuffix("/ui/")
            with opener.open(origin + "/healthz", timeout=.3) as response:
                return origin if response.status == 200 else None
        except OSError:
            return None

    try:
        deadline = time.monotonic() + 15
        formal_origin = None
        while time.monotonic() < deadline and formal_process.poll() is None:
            formal_origin = healthy(formal)
            if formal_origin:
                break
            time.sleep(.05)
        assert formal_origin
        before = _snapshot(formal)
        demo_process = subprocess.Popen([sys.executable, str(ROOT / "packaging/launcher.py"), "--headless", "--demo", "--shutdown-on-stdin-close"], env=env,
                                        stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        deadline = time.monotonic() + 15
        demo_origin = None
        demo_root = None
        while time.monotonic() < deadline and demo_process.poll() is None:
            # Windows venv redirectors may give Popen a different PID from the
            # interpreter. The per-process temp root is the ownership boundary.
            matches = list(child_temp.glob("tuomin-demo-*"))
            if matches:
                demo_root = matches[0]
                demo_origin = healthy(demo_root)
                if demo_origin:
                    break
            time.sleep(.05)
        assert demo_origin and demo_origin != formal_origin, (demo_process.stderr.read().decode("utf-8", "replace") if demo_process.poll() is not None else "demo health timeout")
        assert (demo_root / "tuomin-demo.lock").exists()
        assert not (demo_root / "tuomin.lock").exists()
        with opener.open(demo_origin + "/api/v1/runtime-mode", timeout=1) as response:
            assert json.load(response)["demo"] is True
        with opener.open(formal_origin + "/api/v1/runtime-mode", timeout=1) as response:
            assert json.load(response)["demo"] is False
        for name in SAMPLE_NAMES:
            with opener.open(demo_origin + "/demo/samples/" + name, timeout=1) as response:
                assert response.status == 200
        assert _snapshot(formal) == before
    finally:
        for process in (demo_process, formal_process):
            if process is not None:
                process.stdin.close()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
                assert process.returncode == 0, process.stderr.read().decode("utf-8", "replace")
    assert demo_root is not None and not demo_root.exists()
    assert not (formal / "tuomin.lock").exists()


def test_demo_blocks_external_client_apply_and_verify_without_host_file_access(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from tuomin_gateway import workbuddy_onboarding as onboarding
    from tuomin_gateway.service.app import create_app

    host = tmp_path / "synthetic-host"
    models = host / ".workbuddy" / "models.json"
    models.parent.mkdir(parents=True)
    models.write_text('{"synthetic": "untouched"}', encoding="utf-8")
    before = _snapshot(host)
    monkeypatch.setenv("TUOMIN_WORKBUDDY_MODELS", str(models))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: host))
    original_open = Path.open

    def guarded_open(path, *args, **kwargs):
        if path.is_relative_to(host):
            raise AssertionError("demo accessed synthetic host client configuration")
        return original_open(path, *args, **kwargs)

    def forbidden_integration(*args, **kwargs):
        raise AssertionError("demo invoked an external-client integration operation")

    with monkeypatch.context() as guard:
        guard.setattr(Path, "open", guarded_open)
        guard.setattr(onboarding, "apply", forbidden_integration)
        guard.setattr(onboarding, "verify", forbidden_integration)
        with demo_environment() as demo:
            assert onboarding.default_models_path().is_relative_to(demo.root)
            configure_admin_token(demo.root)
            app = create_app()
            app.state.demo = True
            try:
                client = TestClient(app, base_url="http://127.0.0.1", headers={
                    "x-tuomin-admin-token": os.environ["TUOMIN_ADMIN_TOKEN"],
                })
                for action in ("apply", "verify"):
                    response = client.post("/admin/integrations/workbuddy/" + action,
                                           json={"app_id": "demo_contract"})
                    assert response.status_code == 403
                    assert "演示模式" in response.json()["detail"]
                assert not onboarding.default_models_path().exists()
            finally:
                app.state.job_store.close()
    assert os.environ["TUOMIN_WORKBUDDY_MODELS"] == str(models)
    assert _snapshot(host) == before


def test_demo_clipboard_uses_isolated_dictionary_without_ner():
    from fastapi.testclient import TestClient

    from tuomin_gateway.profiles import RULES_WORKBENCH
    from tuomin_gateway.service.app import create_app
    from tuomin_gateway.shell.bridge import ShellBridge
    from tuomin_gateway.shell.clipboard import ClipboardController
    from tuomin_gateway.shell.registration import ClipboardRegistration
    from tuomin_gateway.shell.storage import SessionFile, write_private

    class Clipboard:
        text = "虚构星橙工程有限公司由许星舟负责验收"

        def read_text(self):
            return self.text

        def write_text(self, text, *, concealed):
            self.text = text

    with demo_environment() as demo:
        configure_admin_token(demo.root)
        token = os.environ["TUOMIN_ADMIN_TOKEN"]
        app = create_app()
        try:
            with TestClient(app, base_url="http://127.0.0.1") as api:
                class Gateway:
                    def request(self, method, path, payload=None, *, token, admin=False):
                        header = "x-tuomin-admin-token" if admin else "x-tuomin-capability-token"
                        response = api.request(method, path, json=payload, headers={
                            header: token, "X-Requested-With": "tuomin-shell"
                        })
                        return response.status_code, response.json()

                gateway = Gateway()
                board = Clipboard()
                credentials = ClipboardRegistration(gateway, token, demo.root, demo=True)
                controller = ClipboardController(board, gateway, credentials.tokens, SessionFile(demo.root), lambda *_: None)
                bridge = ShellBridge(demo.root, credentials, controller, demo=True)
                source = bridge.get_shell_settings()["dictionary_source"]
                assert source == "demo_contract"
                credentials.configure(source)
                registry = json.loads((demo.root / "synthetic-apps.json").read_text(encoding="utf-8"))["apps"]
                assert registry["clipboard"]["profile"] == RULES_WORKBENCH
                assert registry["clipboard"]["dictionary"] == registry[source]["dictionary"]
                original = board.text
                assert controller.redact()
                assert board.text == "<ORG_001>由<PERSON_001>负责验收"
                assert controller.refill()
                assert board.text == original

                write_private(demo.root / "shell-settings.json", {"dictionary_source": "demo_general"})
                saved = ShellBridge(demo.root, credentials, controller, demo=True)
                assert saved.get_shell_settings()["dictionary_source"] == "demo_general"
        finally:
            app.state.job_store.close()
