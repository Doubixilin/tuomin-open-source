from pathlib import Path
import os
import runpy
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]


def test_native_diagnostic_requires_every_module_to_be_compiled(monkeypatch):
    launcher = runpy.run_path(str(ROOT / "packaging/launcher.py"))
    monkeypatch.setattr(launcher["importlib"], "import_module", lambda name: SimpleNamespace())
    assert launcher["_native_selftest"]() == 1


def test_native_diagnostic_accepts_complete_compiled_set(monkeypatch):
    launcher = runpy.run_path(str(ROOT / "packaging/launcher.py"))
    imported = []

    def load(name):
        imported.append(name)
        return SimpleNamespace(__compiled__=SimpleNamespace(extension_filename=name + (".pyd" if os.name == "nt" else ".so")))

    monkeypatch.setattr(launcher["importlib"], "import_module", load)
    assert launcher["_native_selftest"]() == 0
    assert tuple(imported) == launcher["_CROWN_NATIVE_MODULES"]


def test_release_inventory_rejects_external_symlink(tmp_path):
    release = runpy.run_path(str(ROOT / "scripts/release_macos.py"))
    _symlink(tmp_path / "external", ROOT / "pyproject.toml")
    with pytest.raises(SystemExit, match="External symlink"):
        release["inventory"](tmp_path)


def test_release_inventory_hashes_files_and_records_internal_links(tmp_path):
    release = runpy.run_path(str(ROOT / "scripts/release_macos.py"))
    (tmp_path / "payload").write_bytes(b"release")
    _symlink(tmp_path / "link", "payload")
    manifest = release["inventory"](tmp_path)
    assert manifest["payload"]["sha256"] == release["sha256"](tmp_path / "payload")
    assert manifest["link"] == {"symlink": "payload"}


def _symlink(path, target):
    try:
        path.symlink_to(target)
    except OSError as error:
        if getattr(error, "winerror", None) == 1314:
            pytest.skip("Windows user lacks symbolic-link privilege")
        raise
