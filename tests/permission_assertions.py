"""Cross-platform assertions for owner-private test artifacts."""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import stat
import subprocess

from tuomin_gateway.platform_security import windows_current_user_sid


def assert_owner_private(path: str | Path, *, is_dir: bool) -> None:
    target = Path(path)
    if os.name != "nt":
        expected = 0o700 if is_dir else 0o600
        assert stat.S_IMODE(target.stat().st_mode) == expected
        return

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
        env={**os.environ, "TUOMIN_ACL_TEST_TARGET": str(target)},
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    assert result.returncode == 0, result.stderr
    acl = json.loads(result.stdout)
    assert acl["protected"] is True
    assert set(acl["sids"]) == {windows_current_user_sid(), "S-1-5-18"}


__all__ = ["assert_owner_private"]
