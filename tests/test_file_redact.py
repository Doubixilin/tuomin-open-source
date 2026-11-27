"""File redaction for CLI / Finder / drag & drop."""
from __future__ import annotations

import base64
import json
import sys
from pathlib import Path

import pytest

from tuomin_gateway import cli
from tuomin_gateway.file_redact import output_path, redact_file, summarize

sys.path.insert(0, str(Path(__file__).parent))
from test_office_documents_api import ORG, PHONE, _source  # noqa: E402


def _ok_office(fmt="xlsx", payload=b"PK-redacted"):
    return 200, {"task_id": "t_1", "file_format": fmt, "redacted_office_b64": base64.b64encode(payload).decode(),
                 "egress": {"egress_allowed": True}, "label_counts": {"ORG": 1}, "warnings": [{"code": "w1", "message": "m"}]}


def test_original_formats_are_saved_next_to_source_without_overwrite(tmp_path):
    source = tmp_path / "台账.xlsx"
    source.write_bytes(b"PK-source")
    routes = []
    post = lambda route, body: (routes.append(route), _ok_office())[1]
    first = redact_file(source, post=post)
    assert first["status"] == "ok" and Path(first["output"]).name == "台账（脱敏）.xlsx"
    assert Path(first["output"]).read_bytes() == b"PK-redacted" and first["task_id"] == "t_1" and first["warnings"] == ["w1"]
    assert routes[0].startswith("/api/v1/documents/redact-office?") and "app_id=workbench" in routes[0]
    second = redact_file(source, post=post)
    assert Path(second["output"]).name == "台账（脱敏 2）.xlsx"
    assert source.read_bytes() == b"PK-source"


def test_legacy_doc_becomes_docx_and_text_types_become_markdown(tmp_path):
    doc = tmp_path / "合同.doc"
    doc.write_bytes(b"\xd0\xcf")
    out = redact_file(doc, post=lambda r, b: (200, {"task_id": "t", "file_format": "docx", "redacted_docx_b64": base64.b64encode(b"D").decode(), "egress": {"egress_allowed": True}}))
    assert out["output"].endswith("合同（脱敏）.docx")
    pdf = tmp_path / "报告.pdf"
    pdf.write_bytes(b"%PDF")
    routes = []
    out = redact_file(pdf, post=lambda r, b: (routes.append(r), (200, {"task_id": "t", "redacted_markdown": "<ORG_001>", "egress": {"egress_allowed": True}}))[1], out_dir=tmp_path / "out")
    assert routes[0].startswith("/api/v1/documents/parse?") and out["output"] == str(tmp_path / "out" / "报告（脱敏）.md")
    assert Path(out["output"]).read_text(encoding="utf-8") == "<ORG_001>"


def test_blocked_errors_and_unsupported_write_nothing(tmp_path):
    source = tmp_path / "a.docx"
    source.write_bytes(b"x")
    blocked = redact_file(source, post=lambda r, b: (200, {"task_id": "t", "redacted_docx_b64": "eA==", "file_format": "docx", "egress": {"egress_allowed": False, "reasons": [{"message": "含不可外发内容"}]}}))
    assert blocked["status"] == "blocked" and blocked["message"] == "含不可外发内容" and blocked["output"] is None
    failed = redact_file(source, post=lambda r, b: (422, {"error": {"code": "embedded_object", "message": "文件包含无法处理的嵌入对象"}}))
    assert failed["status"] == "error" and "嵌入对象" in failed["message"]
    assert redact_file(tmp_path / "x.png", post=None)["message"].startswith("不支持")
    assert redact_file(tmp_path / "missing.docx", post=None)["message"] == "文件不存在"
    assert list(tmp_path.glob("*（脱敏*")) == []
    assert summarize([blocked, failed]).startswith("未能脱敏（a.docx：")
    assert summarize([]) == "没有可脱敏的文件"


def test_output_path_numbering(tmp_path):
    source = tmp_path / "x.docx"
    (tmp_path / "x（脱敏）.docx").write_text("")
    (tmp_path / "x（脱敏 2）.docx").write_text("")
    assert output_path(source, ".docx").name == "x（脱敏 3）.docx"


def test_cli_redact_file_in_process_end_to_end(tmp_path, monkeypatch, capsys):
    dictionary = tmp_path / "dict.json"
    dictionary.write_text(json.dumps([{"canonical_value": ORG, "aliases": [], "label": "ORG", "status": "active"}], ensure_ascii=False), encoding="utf-8")
    config = tmp_path / "apps.json"
    config.write_text(json.dumps({"apps": {"workbench": {"profile": "file_workbench", "dictionary": str(dictionary)}}}), encoding="utf-8")
    monkeypatch.setenv("TUOMIN_CONFIG", str(config))
    monkeypatch.setenv("TUOMIN_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("TUOMIN_MAPPING_DIR", str(tmp_path / "maps"))
    monkeypatch.delenv("TUOMIN_GATEWAY_URL", raising=False)
    monkeypatch.setattr("tuomin_gateway.detectors.ner.get_ner_detector", lambda: type("N", (), {"name": "ner", "version": "stub", "detect": lambda self, t: []})())
    source = tmp_path / "名单.xlsx"
    source.write_bytes(_source("xlsx"))
    assert cli.main(["redact-file", str(source), "--json"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["mode"] == "in-process" and report["results"][0]["status"] == "ok"
    output = Path(report["results"][0]["output"])
    assert output.name == "名单（脱敏）.xlsx"
    from tuomin_gateway.document.office_patch import extract_office_texts
    texts = [u.text for u in extract_office_texts(output.read_bytes(), "xlsx")]
    assert all(ORG not in t and PHONE not in t for t in texts)
    assert cli.main(["redact-file", str(tmp_path / "nope.png")]) == 1


def test_shell_file_redactor_batches_and_notifies_with_outputs(tmp_path):
    from tuomin_gateway.shell.files import FileRedactor
    good = tmp_path / "a.xlsx"
    good.write_bytes(b"PK")
    notes = []
    redactor = FileRedactor("http://127.0.0.1:1", "t", app_id=lambda: "workbench", notify=lambda text, outputs: notes.append((text, outputs)),
                            post=lambda route, body: _ok_office())
    results = redactor.run([str(good)])
    assert results[0]["status"] == "ok" and notes[-1][0] == "已脱敏 1 个文件，结果保存在原文件旁"
    assert notes[-1][1] == [results[0]["output"]]
    assert redactor.submit([str(tmp_path / "photo.png")]) == 0 and "不支持" in notes[-1][0]
    import time
    assert redactor.submit([str(good)]) == 1
    deadline = time.time() + 5
    while len(notes) < 3 and time.time() < deadline:
        time.sleep(0.02)
    assert notes[-1][0].startswith("已脱敏 1 个文件")


def test_cli_module_entry_runs_redact_file(tmp_path):
    """`python -m tuomin_gateway.cli redact-file` must reach the command."""
    import os
    import subprocess
    env = {**os.environ, "TUOMIN_DATA_DIR": str(tmp_path / "data"), "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src")}
    env.pop("TUOMIN_GATEWAY_URL", None)
    completed = subprocess.run([sys.executable, "-m", "tuomin_gateway.cli", "redact-file", str(tmp_path / "x.png"), "--json"],
                               capture_output=True, text=True, env=env, timeout=120)
    assert completed.returncode == 1, completed.stderr
    assert json.loads(completed.stdout)["results"][0]["message"].startswith("不支持")
