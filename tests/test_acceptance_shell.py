"""Acceptance checks fail closed and report skipped work separately."""
from __future__ import annotations

import base64
import importlib.util
import io
from pathlib import Path
import subprocess
from types import SimpleNamespace
import zipfile

import pytest


ROOT = Path(__file__).resolve().parents[1]


def _load(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


helper = _load("acceptance_shell")


@pytest.mark.parametrize("platform,encrypted,expected", [
    ("nt", True, True), ("nt", False, False),
    ("posix", False, True), ("posix", True, False),
    ("nt", None, False), ("posix", None, False),
])
def test_demo_storage_requires_platform_encryption_and_isolation(monkeypatch, tmp_path,
                                                               platform, encrypted, expected):
    monkeypatch.setattr(helper, "os", SimpleNamespace(name=platform))
    settings = {"mapping_store": {"encrypted_at_rest": encrypted},
                "mapping_dir": str(tmp_path / "mappings")}
    assert helper.demo_storage_isolated(settings, tmp_path) is expected
    settings["mapping_dir"] = str(tmp_path / ".." / "outside")
    assert helper.demo_storage_isolated(settings, tmp_path) is False
    del settings["mapping_dir"]
    assert helper.demo_storage_isolated(settings, tmp_path) is False


@pytest.mark.parametrize("encoding", ["utf-8", "utf-16-le", "utf-16-be", "gbk", "gb18030"])
def test_log_scan_detects_chinese_originals_in_supported_encodings(tmp_path, encoding):
    path = tmp_path / "synthetic.log"
    path.write_bytes(b"\xff\x80 native progress\n" + "许星舟".encode(encoding) + b"\n")
    assert helper.log_contains_values(path, ["unrelated-credential", "许星舟"]) is True


def test_log_scan_keeps_ascii_tokens_and_ignores_unrelated_non_utf8_bytes(tmp_path):
    path = tmp_path / "synthetic.log"
    path.write_bytes(b"\xff\x80 native progress\n")
    assert helper.log_contains_values(path, ["synthetic-secret", "许星舟"]) is False
    path.write_bytes(path.read_bytes() + b"synthetic-secret")
    assert helper.log_contains_values(path, ["synthetic-secret"]) is True


def _office(text, file_format):
    data = io.BytesIO()
    path = "word/document.xml" if file_format == "docx" else "xl/sharedStrings.xml"
    with zipfile.ZipFile(data, "w") as archive:
        archive.writestr(path, f"<root><t>{text}</t></root>")
    return data.getvalue()


def _checks():
    results = []
    return results, lambda name, passed, detail="": results.append((name, bool(passed)))


def test_snapshot_detects_writes_and_permissions(tmp_path):
    path = tmp_path / "synthetic"
    path.write_text("synthetic fixture")
    before = helper.snapshot(tmp_path)
    path.chmod(0o400)
    assert helper.snapshot(tmp_path) != before
    path.chmod(0o600)
    before = helper.snapshot(tmp_path)
    path.write_text("synthetic changed fixture")
    assert helper.snapshot(tmp_path) != before


@pytest.mark.parametrize("file_format", ["docx", "xlsx"])
def test_office_text_detects_changed_restored_text(file_format):
    assert helper.office_text(_office("first", file_format), file_format) != helper.office_text(_office("second", file_format), file_format)


@pytest.mark.parametrize("file_format", ["docx", "xlsx"])
@pytest.mark.parametrize("tamper", [False, True])
def test_full_office_roundtrip_rejects_inexact_restoration(file_format, tamper):
    original = _office(helper.SYNTHETIC_ORG, file_format)
    masked = _office("&lt;ORG_001&gt;", file_format)
    restored = _office("modified", file_format) if tamper else original
    suffix = "docx" if file_format == "docx" else "office"

    class Client:
        def json(self, method, path, payload=None, **kwargs):
            if "/redact-" in path:
                return 200, {"egress": {"egress_allowed": True}, "task_id": "synthetic",
                             f"redacted_{suffix}_b64": base64.b64encode(masked).decode()}
            if path.endswith("/package"):
                return 200, {"package": {"synthetic": True}}
            return 200, {"status": "ok", f"restored_{suffix}_b64": base64.b64encode(restored).decode()}

    results, check = _checks()
    helper._demo_office_roundtrip(Client(), {f"sample.{file_format}": original}, check, lite=False)
    assert len(results) == 4
    assert all(passed for _, passed in results[:-1])
    assert results[-1][1] is not tamper


@pytest.mark.parametrize("response,expected", [
    ((422, {"egress_allowed": False}), True),
    ((200, {"egress_allowed": True, "redacted_office_b64": "synthetic"}), False),
    ((422, {"egress_allowed": False, "redacted_office_b64": "synthetic"}), False),
])
def test_lite_only_accepts_blocked_export_without_artifact(response, expected):
    class Client:
        def json(self, *args, **kwargs):
            return response

    results, check = _checks()
    helper._demo_office_roundtrip(Client(), {"sample.xlsx": b"synthetic"}, check, lite=True)
    assert len(results) == 1
    assert results[0][1] is expected


def test_forced_process_kill_is_not_counted_as_graceful_cleanup():
    class Process:
        def poll(self):
            return None

        def terminate(self):
            pass

        def wait(self, timeout):
            if timeout == 15:
                raise subprocess.TimeoutExpired("synthetic", timeout)

        def kill(self):
            pass

    assert helper._stop(Process()) is False


def test_acceptance_stop_closes_control_pipe_before_wait():
    events = []

    class Pipe:
        def close(self):
            events.append("closed")

    class Process:
        stdin = Pipe()

        def poll(self):
            return None

        def wait(self, timeout):
            events.append("wait")

        def terminate(self):
            raise AssertionError("Control pipe supports graceful shutdown")

    assert helper._stop(Process()) is True
    assert events == ["closed", "wait"]


def test_acceptance_owner_private_validates_platform_permissions(tmp_path):
    from tuomin_gateway.platform_security import restrict_permissions
    path = tmp_path / "synthetic-token"
    path.write_text("synthetic", encoding="utf-8")
    restrict_permissions(path, is_dir=False)
    assert helper.owner_private(path) is True


def test_demo_discovery_uses_private_temp_root_not_launcher_pid(monkeypatch, tmp_path):
    directory = tmp_path / "tuomin-demo-54321-synthetic"
    directory.mkdir()
    (directory / "instance.url").write_text("http://127.0.0.1:8765/ui/", encoding="utf-8")

    class Process:
        pid = 12345

        def poll(self):
            return None

    class Client:
        def __init__(self, *args, **kwargs):
            pass

        def json(self, *args):
            return 200, {"status": "ok"}

    monkeypatch.setattr(helper, "Client", Client)
    assert helper._wait_demo(Process(), tmp_path) == (directory, "http://127.0.0.1:8765")


def test_demo_registration_does_not_replay_public_token_metadata():
    class Client:
        def json(self, method, path, payload=None, **kwargs):
            if method == "GET":
                return 200, {"entry": {"profile": {"base": "file_workbench", "use_ner": False},
                                       "dictionary": "synthetic.json", "configured_capability_tokens": []}}
            if method == "PUT":
                assert "configured_capability_tokens" not in payload
                assert payload["profile"]["use_ner"] is False
                return 200, {}
            return 422, {"egress_allowed": False}

    results, check = _checks()
    credentials = helper._demo_text_roundtrip(Client(), check)
    assert len(credentials) == 2
    assert results[1][1] is True
    assert results[2][1] is False


def test_missing_license_matrix_skip_is_not_reported_as_pass(capsys):
    acceptance = _load("acceptance_frozen")
    acceptance.skip("正式授权业务矩阵", "未提供验收授权码")
    assert acceptance.results[0]["passed"] is None
    assert acceptance.results[0]["skipped"] is True
    assert acceptance.FORMAL_MATRIX_RAN is False
    assert "跳过" in capsys.readouterr().out
