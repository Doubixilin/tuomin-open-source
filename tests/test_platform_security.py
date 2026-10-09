from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess

import pytest

from tuomin_gateway.platform_security import (
    restrict_permissions,
    windows_current_user_sid,
)


def _windows_acl(path: Path) -> dict[str, object]:
    powershell = shutil.which("pwsh") or shutil.which("powershell")
    assert powershell is not None
    script = r"""
$acl = Get-Acl -LiteralPath $env:TUOMIN_ACL_TEST_TARGET
$sids = @(
    $acl.Access | ForEach-Object {
        $_.IdentityReference.Translate(
            [System.Security.Principal.SecurityIdentifier]
        ).Value
    }
)
[ordered]@{
    protected = $acl.AreAccessRulesProtected
    sids = $sids
} | ConvertTo-Json -Compress
"""
    result = subprocess.run(
        [powershell, "-NoProfile", "-NonInteractive", "-Command", script],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
        env={**os.environ, "TUOMIN_ACL_TEST_TARGET": str(path)},
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


@pytest.mark.skipif(os.name != "nt", reason="Windows DACL behavior")
def test_restrict_permissions_sets_private_windows_dacl(tmp_path: Path) -> None:
    directory = tmp_path / "private"
    directory.mkdir()
    file_path = directory / "state.json"
    file_path.write_text("{}", encoding="utf-8")

    restrict_permissions(directory, is_dir=True)
    restrict_permissions(file_path, is_dir=False)

    allowed = {windows_current_user_sid(), "S-1-5-18"}
    for target in (directory, file_path):
        acl = _windows_acl(target)
        assert acl["protected"] is True
        assert set(acl["sids"]) == allowed


@pytest.mark.skipif(os.name == "nt", reason="POSIX mode behavior")
def test_restrict_permissions_sets_owner_only_posix_modes(tmp_path: Path) -> None:
    directory = tmp_path / "private"
    directory.mkdir()
    file_path = directory / "state.json"
    file_path.write_text("{}", encoding="utf-8")

    restrict_permissions(directory, is_dir=True)
    restrict_permissions(file_path, is_dir=False)

    assert directory.stat().st_mode & 0o777 == 0o700
    assert file_path.stat().st_mode & 0o777 == 0o600
