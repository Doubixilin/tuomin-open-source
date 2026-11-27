"""Bulk dictionary import / export / learn-from-results."""
from __future__ import annotations

import base64
import io
import json

import pytest
from fastapi.testclient import TestClient

from tuomin_gateway.dictionary_import import candidates, export_entries, parse_rows, plan_import
from tuomin_gateway.service.app import create_app
from tuomin_gateway.service.registry import AppRegistry
from tuomin_gateway.store import MappingStore

ADMIN = "admin-token-for-dictionary-tests-01"


def test_text_lines_accept_chinese_labels_aliases_and_report_bad_rows():
    rows = parse_rows("text", "# 注释\n示例建设集团有限公司，单位，示例建设|示例集团，高\n张三\t人名\n\n,ORG\n李四\n王五,职位\n", default_label="")
    view = [(r.line, r.canonical_value, r.label, r.aliases, r.risk_level, r.status) for r in rows]
    assert view[0] == (2, "示例建设集团有限公司", "ORG", ["示例建设", "示例集团"], "high", "pending")
    assert view[1][2] == "PERSON"
    assert [r.status for r in rows[2:]] == ["error", "error", "error"]
    assert "缺少类型" in rows[3].message and "无法识别" in rows[4].message
    assert parse_rows("text", "李四", default_label="PERSON")[0].label == "PERSON"


def test_csv_and_xlsx_headers_are_detected_in_any_order():
    csv_rows = parse_rows("csv", "别名,名称,类型\n乙方简称,乙方建设有限公司,ORG\n")
    assert (csv_rows[0].canonical_value, csv_rows[0].aliases, csv_rows[0].label) == ("乙方建设有限公司", ["乙方简称"], "ORG")
    from openpyxl import Workbook
    book = Workbook()
    sheet = book.active
    sheet.append(["名称", "类型", "风险"])
    sheet.append(["丙方科技有限公司", "供应商", "极高"])
    buffer = io.BytesIO()
    book.save(buffer)
    xlsx = parse_rows("xlsx", buffer.getvalue())
    assert (xlsx[0].label, xlsx[0].risk_level, xlsx[0].line) == ("SUPPLIER", "critical", 2)


def test_merge_unions_aliases_flags_conflicts_and_blocks_only_bad_changes():
    existing = [{"entry_id": "e1", "canonical_value": "示例建设集团有限公司", "label": "ORG", "aliases": ["示例建设"], "status": "active"}]
    rows = parse_rows("text", "示例建设集团有限公司,ORG,示例集团\n示例建设集团有限公司,ORG,示例建设\n示例建设,PERSON\n有限公司,ORG\n新单位有限公司,ORG,示例集团\n")
    plan = plan_import(existing, rows)
    statuses = [r["status"] for r in plan["rows"]]
    assert statuses == ["updated", "unchanged", "error", "blocked", "error"]
    assert "已是类型 ORG" in plan["rows"][2]["message"] and "已属于其他词条" in plan["rows"][4]["message"]
    assert plan["entries"][0]["aliases"] == ["示例建设", "示例集团"] and len(plan["entries"]) == 1
    assert plan["changed"] == 1


def test_export_round_trips_through_import():
    entries = [{"entry_id": "e1", "canonical_value": "示例建设集团有限公司", "label": "ORG", "aliases": ["示例建设"], "risk_level": "high", "status": "active"},
               {"entry_id": "e2", "canonical_value": "旧单位", "label": "ORG", "status": "retired"}]
    content, mime = export_entries(entries, "csv")
    assert mime == "text/csv" and "旧单位" not in content
    again = plan_import([], parse_rows("csv", content))
    assert again["counts"] == {"added": 1} and again["entries"][0]["aliases"] == ["示例建设"]
    payload = json.loads(export_entries(entries, "json")[0])
    assert payload["format"] == "tuomin-dictionary-v1" and len(payload["entries"]) == 1


def test_candidates_skip_known_values_rule_labels_and_map_roles():
    class Entry:
        def __init__(self, label, value):
            self.label, self.original_value = label, value
    found = candidates([Entry("PARTYA", "甲方建设有限公司"), Entry("PHONE", "13800138000"), Entry("PERSON", "张三"),
                        Entry("ORG", "示例建设"), Entry("PERSON", "张三"), Entry("ORG", "某")],
                       [{"canonical_value": "示例建设集团有限公司", "label": "ORG", "aliases": ["示例建设"]}])
    assert found == [{"canonical_value": "甲方建设有限公司", "label": "ORG"}, {"canonical_value": "张三", "label": "PERSON"}]


@pytest.fixture
def client(tmp_path):
    for name in ("a", "b"):
        (tmp_path / f"{name}.json").write_text("[]", encoding="utf-8")
    (tmp_path / "b.json").write_text(json.dumps([{"entry_id": "x1", "canonical_value": "乙方建设有限公司", "label": "ORG", "aliases": [], "status": "active"}], ensure_ascii=False), encoding="utf-8")
    config = tmp_path / "apps.json"
    config.write_text(json.dumps({"apps": {"a": {"profile": "file_workbench", "dictionary": str(tmp_path / "a.json")},
                                          "b": {"profile": "file_workbench", "dictionary": str(tmp_path / "b.json")},
                                          "nodict": {"profile": "strict"}}}), encoding="utf-8")
    app = create_app(registry=AppRegistry.load(config), store=MappingStore(tmp_path / "maps"), admin_token=ADMIN)
    test_client = TestClient(app, base_url="http://127.0.0.1")
    test_client.headers.update({"x-tuomin-admin-token": ADMIN})
    test_client.tmp = tmp_path
    return test_client


def test_import_api_preview_then_commit_and_audit_has_no_values(client):
    body = {"format": "text", "content": "示例建设集团有限公司,单位,示例建设\n市,ORG\n"}
    preview = client.post("/admin/apps/a/dictionary/import", json=body).json()
    assert preview["dry_run"] is True and preview["counts"] == {"added": 1, "blocked": 1}
    assert json.loads((client.tmp / "a.json").read_text()) == []
    committed = client.post("/admin/apps/a/dictionary/import", json={**body, "dry_run": False}).json()
    assert committed["changed"] == 1
    stored = json.loads((client.tmp / "a.json").read_text())
    assert [e["canonical_value"] for e in stored] == ["示例建设集团有限公司"]
    audit = (client.tmp / "maps" / "audit" / "admin.jsonl").read_text(encoding="utf-8")
    assert "admin_dictionary_import" in audit and "示例建设" not in audit


def test_import_from_another_app_xlsx_upload_and_export(client):
    copied = client.post("/admin/apps/a/dictionary/import", json={"format": "app", "source_app": "b", "dry_run": False}).json()
    assert copied["counts"] == {"added": 1}
    from openpyxl import Workbook
    book = Workbook()
    book.active.append(["名称", "类型"])
    book.active.append(["丙方科技有限公司", "ORG"])
    buffer = io.BytesIO()
    book.save(buffer)
    uploaded = client.post("/admin/apps/a/dictionary/import", json={"format": "xlsx", "content_b64": base64.b64encode(buffer.getvalue()).decode(), "dry_run": False}).json()
    assert uploaded["counts"] == {"added": 1}
    exported = client.get("/admin/apps/a/dictionary/export", params={"format": "csv"}).json()
    assert exported["name"] == "a-dictionary.csv" and "乙方建设有限公司" in exported["content"] and "丙方科技有限公司" in exported["content"]
    assert client.post("/admin/apps/a/dictionary/import", json={"format": "app", "source_app": "a"}).status_code == 400
    assert client.post("/admin/apps/nodict/dictionary/import", json={"content": "x,ORG"}).status_code == 400
    assert client.post("/admin/apps/a/dictionary/import", json={"format": "docx", "content": "x"}).status_code == 400


def test_import_commit_refused_in_demo_but_preview_allowed(client):
    client.app.state.demo = True
    assert client.post("/admin/apps/a/dictionary/import", json={"content": "示例建设集团有限公司,ORG"}).status_code == 200
    assert client.post("/admin/apps/a/dictionary/import", json={"content": "示例建设集团有限公司,ORG", "dry_run": False}).status_code == 403
