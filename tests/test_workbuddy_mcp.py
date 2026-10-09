"""Tests for the Tuomin WorkBuddy MCP connector installer."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from tuomin_gateway.workbuddy_mcp import (
    build_server_entry,
    install_mcp_server,
    uninstall_mcp_server,
)

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "install_workbuddy_mcp.py"


def test_build_server_entry_has_absolute_runtime_and_tokens():
    entry = build_server_entry(
        python="/usr/bin/python3", gateway_url="http://127.0.0.1:8775",
        redact_token="rt", namespace_token="nt", app_id="wb_test",
    )
    assert entry["command"] == "/usr/bin/python3"
    assert entry["args"] == ["-m", "tuomin_gateway.mcp_stdio"]
    assert entry["env"]["TUOMIN_GATEWAY_URL"] == "http://127.0.0.1:8775"
    assert entry["env"]["TUOMIN_AGENT_REDACT_TOKEN"] == "rt"
    assert entry["env"]["TUOMIN_AGENT_APP"] == "wb_test"
    assert entry["env"]["PYTHONPATH"].endswith("src")
    assert entry["disabled"] is False


def test_install_preserves_other_servers_and_backs_up(tmp_path):
    mcp = tmp_path / "mcp.json"
    mcp.write_text(json.dumps({"mcpServers": {"connector:x": {"url": "https://x/mcp"}}}), encoding="utf-8")

    result = install_mcp_server(gateway_url="http://127.0.0.1:8775", mcp_file=mcp,
                                python="/usr/bin/python3", redact_token="rt")
    assert result["servers_total"] == 2
    assert result["backup"] and Path(result["backup"]).exists()

    data = json.loads(mcp.read_text(encoding="utf-8"))
    assert "connector:x" in data["mcpServers"]  # untouched
    assert data["mcpServers"]["tuomin"]["env"]["TUOMIN_GATEWAY_URL"] == "http://127.0.0.1:8775"

    # idempotent: a second install does not create a duplicate key
    install_mcp_server(gateway_url="http://127.0.0.1:9999", mcp_file=mcp, python="/usr/bin/python3")
    data = json.loads(mcp.read_text(encoding="utf-8"))
    assert len(data["mcpServers"]) == 2
    assert data["mcpServers"]["tuomin"]["env"]["TUOMIN_GATEWAY_URL"] == "http://127.0.0.1:9999"


def test_uninstall_removes_only_our_server(tmp_path):
    mcp = tmp_path / "mcp.json"
    install_mcp_server(gateway_url="http://127.0.0.1:8775", mcp_file=mcp, python="/usr/bin/python3")
    assert uninstall_mcp_server(mcp_file=mcp)["removed"] is True
    data = json.loads(mcp.read_text(encoding="utf-8"))
    assert "tuomin" not in data["mcpServers"]
    assert uninstall_mcp_server(mcp_file=mcp)["removed"] is False


def test_cli_dry_run_and_install_roundtrip(tmp_path):
    mcp = tmp_path / "mcp.json"
    dry = subprocess.run(
        [sys.executable, str(SCRIPT), "--base-url", "http://127.0.0.1:8775",
         "--app", "wb_test", "--dry-run"],
        capture_output=True, text=True,
    )
    assert dry.returncode == 0, dry.stderr
    payload = json.loads(dry.stdout)
    assert payload["mcpServers"]["tuomin"]["env"]["TUOMIN_GATEWAY_URL"] == "http://127.0.0.1:8775"
    assert not mcp.exists()

    done = subprocess.run(
        [sys.executable, str(SCRIPT), "--base-url", "http://127.0.0.1:8775",
         "--mcp-file", str(mcp), "--redact-token", "rt", "--namespace-token", "nt"],
        capture_output=True, text=True,
    )
    assert done.returncode == 0, done.stderr
    assert json.loads(mcp.read_text(encoding="utf-8"))["mcpServers"]["tuomin"]["env"]["TUOMIN_AGENT_REDACT_TOKEN"] == "rt"

    removed = subprocess.run(
        [sys.executable, str(SCRIPT), "--mcp-file", str(mcp), "--uninstall"],
        capture_output=True, text=True,
    )
    assert removed.returncode == 0, removed.stderr
    assert "tuomin" not in json.loads(mcp.read_text(encoding="utf-8"))["mcpServers"]
