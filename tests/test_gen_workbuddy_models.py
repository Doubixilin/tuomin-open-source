"""Tests for the WorkBuddy models.json generator (scripts/gen_workbuddy_models.py)."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "gen_workbuddy_models.py"
sys.path.insert(0, str(SCRIPT.parent))

from gen_workbuddy_models import (  # noqa: E402
    build_entries,
    merge_entries,
    mode_from_url,
    retarget_entries,
)


def test_build_entries_matches_workbuddy_schema():
    entries = build_entries("wb_test", "http://127.0.0.1:8767", "sk-x", ["masked", "demo", "auto"])
    assert len(entries) == 3
    urls = {e["id"]: e["url"] for e in entries}
    assert urls["wb_test·脱敏·全mask"].endswith("/apps/wb_test/v1/chat/completions")
    assert urls["wb_test·脱敏·思维链mask"].endswith("/apps/wb_test/demo/v1/chat/completions")
    assert urls["wb_test·脱敏·无感回填"].endswith("/apps/wb_test/auto/v1/chat/completions")
    for entry in entries:
        assert entry["vendor"] == "Custom"
        assert entry["supportsToolCall"] == "True"  # 字符串型布尔（WorkBuddy 真实格式）
        assert set(entry) == {
            "id", "name", "vendor", "url", "apiKey", "supportsToolCall",
            "supportsImages", "supportsReasoning", "useCustomProtocol", "reasoning",
        }


def test_merge_entries_is_idempotent_by_id():
    existing = [{"id": "a", "url": "old"}, {"id": "b", "url": "keep"}]
    merged = merge_entries(existing, [{"id": "a", "url": "new"}])
    assert {e["id"]: e["url"] for e in merged} == {"a": "new", "b": "keep"}


def test_mode_from_url_recognizes_all_three_project_endpoints():
    assert mode_from_url("http://127.0.0.1:8775/apps/wb_test/v1/chat/completions", "wb_test") == "masked"
    assert mode_from_url("http://127.0.0.1:8775/apps/wb_test/demo/v1/chat/completions", "wb_test") == "demo"
    assert mode_from_url("http://127.0.0.1:8775/apps/wb_test/auto/v1/chat/completions", "wb_test") == "auto"
    assert mode_from_url("http://127.0.0.1:8775/apps/other/v1/chat/completions", "wb_test") is None
    assert mode_from_url("https://api.deepseek.com/v1/chat/completions", "wb_test") is None
    assert mode_from_url(None, "wb_test") is None


def test_retarget_entries_heals_only_the_target_app():
    existing = [
        {"id": "脱敏-无感", "url": "http://127.0.0.1:8767/apps/wb_test/auto/v1/chat/completions"},
        {"id": "thinking", "url": "http://127.0.0.1:8767/apps/wb_test/demo/v1/chat/completions?x=1"},
        {"id": "other", "url": "http://127.0.0.1:8767/apps/other_app/auto/v1/chat/completions"},
        {"id": "glm", "url": "https://open.bigmodel.cn/api/coding/paas/v4"},
    ]
    changed = retarget_entries(existing, "wb_test", "http://127.0.0.1:8775")
    assert len(changed) == 2
    assert existing[0]["url"] == "http://127.0.0.1:8775/apps/wb_test/auto/v1/chat/completions"
    # query string is preserved
    assert existing[1]["url"] == "http://127.0.0.1:8775/apps/wb_test/demo/v1/chat/completions?x=1"
    assert existing[2]["url"].startswith("http://127.0.0.1:8767")
    assert existing[3]["url"].startswith("https://")


def test_merge_entries_reuses_existing_mode_entry_without_clobbering():
    existing = [
        {
            "id": "脱敏-无感",
            "name": "脱敏-无感",
            "url": "http://127.0.0.1:8767/apps/wb_test/auto/v1/chat/completions",
            "apiKey": "sk-real-key",
            "reasoning": {"defaultEffort": "max"},
        }
    ]
    new = build_entries("wb_test", "http://127.0.0.1:8775", "EXAMPLE_ONLY_NO_SECRET", ["auto"])
    merged = merge_entries(existing, new, app="wb_test")
    assert len(merged) == 1  # no duplicate model entry
    assert merged[0]["id"] == "脱敏-无感"  # GUI identity preserved
    assert merged[0]["apiKey"] == "sk-real-key"  # placeholder must not clobber
    assert merged[0]["reasoning"] == {"defaultEffort": "max"}
    assert merged[0]["url"] == "http://127.0.0.1:8775/apps/wb_test/auto/v1/chat/completions"


def test_merge_entries_appends_when_mode_is_new():
    existing = [{"id": "glm", "url": "https://open.bigmodel.cn/api/coding/paas/v4"}]
    new = build_entries("wb_test", "http://127.0.0.1:8775", "sk-x", ["auto"])
    merged = merge_entries(existing, new, app="wb_test")
    assert {e["id"] for e in merged} == {"glm", "wb_test·脱敏·无感回填"}


def test_cli_prints_json_entries():
    proc = subprocess.run(
        [
            sys.executable, str(SCRIPT), "--app", "demo_app", "--api-key", "sk-x",
            "--modes", "auto", "--base-url", "http://127.0.0.1:9999",
        ],
        capture_output=True, text=True,
    )
    assert proc.returncode == 0, proc.stderr
    entries = json.loads(proc.stdout)
    assert len(entries) == 1
    assert entries[0]["url"] == "http://127.0.0.1:9999/apps/demo_app/auto/v1/chat/completions"


def test_cli_rejects_unknown_mode():
    proc = subprocess.run(
        [sys.executable, str(SCRIPT), "--app", "x", "--modes", "bogus"],
        capture_output=True, text=True,
    )
    assert proc.returncode == 2


def test_cli_fails_loudly_when_no_gateway_serves_app():
    """No scan + no hints => must not invent a URL; exit non-zero."""
    proc = subprocess.run(
        [sys.executable, str(SCRIPT), "--app", "demo_app", "--scan", ""],
        capture_output=True, text=True,
        env={"PATH": "/usr/bin:/bin", "HOME": "/nonexistent-home"},
    )
    assert proc.returncode == 3
    assert "未找到挂载 app" in proc.stderr
