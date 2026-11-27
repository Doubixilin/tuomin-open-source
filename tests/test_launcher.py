from __future__ import annotations

import importlib.util
import os
from pathlib import Path
import runpy
import subprocess
import sys
import time

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
LAUNCHER_PATH = REPO_ROOT / "packaging" / "launcher.py"


def _load_launcher():
    spec = importlib.util.spec_from_file_location("tuomin_packaging_launcher", LAUNCHER_PATH)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _sleeping_child() -> subprocess.Popen[str]:
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    return subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        text=True,
        creationflags=creationflags,
    )


def test_native_selftest_contract_matches_build_manifest() -> None:
    launcher = _load_launcher()
    definition = runpy.run_path(
        str(REPO_ROOT / "scripts" / "build_native_modules.py")
    )
    expected = tuple(
        ".".join(
            ("tuomin_gateway", *Path(relative).parent.parts, Path(relative).stem)
        )
        for relative in definition["CROWN_MODULES"]
    )
    assert launcher._CROWN_NATIVE_MODULES == expected

def test_browser_can_be_disabled_for_unattended_acceptance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    launcher = _load_launcher()
    monkeypatch.setenv("TUOMIN_NO_BROWSER", "1")
    assert launcher._browser_enabled() is False


def test_pid_probe_does_not_terminate_live_process() -> None:
    launcher = _load_launcher()
    child = _sleeping_child()
    try:
        assert launcher._pid_is_alive(child.pid) is True
        time.sleep(0.1)
        assert child.poll() is None
    finally:
        if child.poll() is None:
            child.terminate()
        child.wait(timeout=5)


@pytest.mark.skipif(os.name != "nt", reason="Windows process API behavior")
def test_windows_pid_probe_rejects_completed_process() -> None:
    launcher = _load_launcher()
    child = _sleeping_child()
    child.terminate()
    child.wait(timeout=5)

    assert launcher._pid_is_alive(child.pid) is False


def test_pid_probe_rejects_non_positive_pid() -> None:
    launcher = _load_launcher()

    assert launcher._pid_is_alive(0) is False
    assert launcher._pid_is_alive(-1) is False
