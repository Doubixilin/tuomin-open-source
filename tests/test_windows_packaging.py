from __future__ import annotations

import importlib.machinery
import importlib.util
import json
from pathlib import Path
import runpy
import shutil
import subprocess
import sys
import types

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]


def _load_script(name: str):
    path = REPO_ROOT / "scripts" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(f"test_{name}_module", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


NATIVE = _load_script("build_native_modules")
MODEL = _load_script("prepare_ner_model")
PACKAGE = _load_script("package_release")
VERIFY = _load_script("verify_build_manifest")


@pytest.fixture
def release_repository(tmp_path, monkeypatch):
    # The publisher records Git provenance. Exercise that contract even when
    # the test suite itself was downloaded as a source archive.
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    inputs = (
        "pyproject.toml", "src/tuomin_gateway/runtime.py", "service_runner.pyw",
        "demo/samples/synthetic-contract.docx", "demo/synthetic-apps.json",
        "packaging/icon/tuomin-1024.png",
    )
    for name in inputs:
        target = checkout / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(REPO_ROOT / name, target)
    subprocess.run(["git", "init", "-q"], cwd=checkout, check=True)
    subprocess.run(["git", "add", "."], cwd=checkout, check=True)
    subprocess.run(
        ["git", "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
         "-c", "commit.gpgsign=false", "commit", "-qm", "Synthetic build inputs"],
        cwd=checkout, check=True,
    )
    for name in ("local_private/sample", "private_workspace/sample",
                 "packaging/ner-model/sample", "src/demo.egg-info/sample"):
        target = checkout / name
        target.parent.mkdir(parents=True)
        target.write_bytes(b"synthetic")
    monkeypatch.setattr(PACKAGE, "REPO_ROOT", checkout)


def _synthetic_bundle(tmp_path: Path) -> Path:
    bundle = tmp_path / "Tuomin"
    bundle.mkdir()
    (bundle / "Tuomin.exe").write_bytes(b"MZ synthetic executable")
    suffix = importlib.machinery.EXTENSION_SUFFIXES[0]
    package_root = bundle / "_internal" / "tuomin_gateway"
    for relative in NATIVE.CROWN_MODULES:
        source = Path(relative)
        artifact = package_root / source.parent / f"{source.stem}{suffix}"
        artifact.parent.mkdir(parents=True, exist_ok=True)
        artifact.write_bytes(f"synthetic native {relative}".encode("utf-8"))
    return bundle


def test_native_artifact_contract_matches_current_platform() -> None:
    artifacts = NATIVE.expected_artifacts()
    assert len(artifacts) == 16
    assert len(set(artifacts)) == 16
    assert any("office_patch" in path.name for path in artifacts)
    assert all(path.name.endswith(NATIVE.ext_suffix()) for path in artifacts)
    if sys.platform == "win32":
        assert all(path.suffix == ".pyd" for path in artifacts)


def test_native_strings_baseline_uses_portable_binary_scan(tmp_path: Path) -> None:
    clean = tmp_path / "clean.pyd"
    clean.write_bytes(b"synthetic native payload")
    leaked = tmp_path / "leaked.pyd"
    leaked.write_bytes(b"prefix scripts/private-build.py suffix")
    wide = tmp_path / "wide.pyd"
    wide.write_bytes("docs/internal.md".encode("utf-16-le"))

    assert NATIVE.check_strings_baseline([clean]) == []
    assert NATIVE.check_strings_baseline([leaked]) == [
        "leaked.pyd: 含违禁字符串 'scripts/'"
    ]
    assert NATIVE.check_strings_baseline([wide]) == [
        "wide.pyd: 含违禁字符串 'docs/'"
    ]


def test_windows_native_build_keeps_platform_guards() -> None:
    script = (REPO_ROOT / "scripts" / "build_native_modules.py").read_text(
        encoding="utf-8"
    )
    assert '["strings"' not in script
    assert 'cmd.append("--msvc=latest")' in script
    assert 'encoding="utf-8"' in script
    assert 'for pattern in ("*.so", "*.pyd")' in script


def test_existing_pinned_model_directory_is_accepted(tmp_path: Path) -> None:
    model = tmp_path / "model"
    model.mkdir()
    for name in MODEL.REQUIRED_FILES:
        (model / name).write_text("{}", encoding="utf-8")
    (model / MODEL.MODEL_METADATA_FILE).write_text(
        json.dumps(MODEL.expected_model_metadata()),
        encoding="utf-8",
    )
    (model / "pytorch_model.bin").write_bytes(b"synthetic weights")

    assert MODEL.validate_model_directory(model) == []
    assert MODEL.materialize_model(model) == model.resolve()

def test_existing_model_directory_rejects_wrong_revision(
    tmp_path: Path,
) -> None:
    model = tmp_path / "model"
    model.mkdir()
    for name in MODEL.REQUIRED_FILES:
        (model / name).write_text("{}", encoding="utf-8")
    (model / "pytorch_model.bin").write_bytes(b"synthetic weights")
    (model / MODEL.MODEL_METADATA_FILE).write_text(
        '{"model":"wrong","revision":"wrong","offline_required":true}',
        encoding="utf-8",
    )
    assert (
        MODEL.MODEL_METADATA_FILE + "(model/revision mismatch)"
        in MODEL.validate_model_directory(model)
    )


def test_release_manifest_and_archive_round_trip(tmp_path: Path, release_repository) -> None:
    bundle = _synthetic_bundle(tmp_path)
    outputs = PACKAGE.build_release(bundle, tmp_path / "release", "Synthetic Customer")

    VERIFY.verify_bundle_security(bundle)
    manifest = VERIFY.verify_manifest(bundle, outputs["manifest"])
    VERIFY.verify_archive(bundle, outputs["archive"], manifest)
    VERIFY.verify_checksum(outputs["checksum"], outputs["archive"], manifest)
    assert manifest["customer"] == "Synthetic Customer"
    assert manifest["archive"]["name"].endswith("Synthetic_Customer.zip")


def test_build_verifier_rejects_private_key_material(tmp_path: Path) -> None:
    bundle = _synthetic_bundle(tmp_path)
    leaked = bundle / "issuer-key.txt"
    leaked.write_bytes(b"-----BEGIN PRIVATE KEY-----\nsynthetic-only\n")

    with pytest.raises(RuntimeError, match="private key material"):
        VERIFY.verify_bundle_security(bundle)


def test_public_verifier_checks_runtime_contents_without_native_modules(tmp_path: Path) -> None:
    (tmp_path / "Tuomin.exe").write_bytes(b"synthetic executable")
    VERIFY.verify_bundle_security(tmp_path, public_build=True)
    forbidden = tmp_path / "pytorch_model.bin"
    forbidden.write_bytes(b"synthetic weights")
    with pytest.raises(RuntimeError, match="private runtime paths"):
        VERIFY.verify_bundle_security(tmp_path, public_build=True)


def test_windows_build_script_pins_toolchain_and_safe_outputs() -> None:
    script = (REPO_ROOT / "scripts" / "build_app.ps1").read_text(encoding="utf-8")
    assert "Python 3.12.10" in script
    assert "pyinstaller==6.22.2" in script
    assert "nuitka==4.1.3" in script
    assert '$BundleName = if ($Lite) { "Tuomin-Lite" } else { "Tuomin" }' in script
    assert "Tuomin.exe" in script
    assert "Assert-ProjectChild" in script
    assert "$BuildOutputPath = Assert-ProjectChild (Resolve-ProjectPath $BuildOutputDir)" in script
    assert "$BuildWorkPath = Assert-ProjectChild (Resolve-ProjectPath $BuildWorkDir)" in script
    assert "$ExtractRoot = Assert-ProjectChild (Resolve-ProjectPath $ExtractDir)" in script
    assert '"--distpath", $BuildOutputPath, "--workpath", $BuildWorkPath' in script


def test_frozen_acceptance_has_windows_runtime_guards() -> None:
    script = (REPO_ROOT / "scripts" / "acceptance_frozen.py").read_text(
        encoding="utf-8"
    )
    assert 'MAPPING_MAGIC = b"DPAPI1" if os.name == "nt"' in script
    assert '"TUOMIN_NO_BROWSER": "1"' in script
    assert '"--shutdown-on-stdin-close"' in script
    assert "stdin=subprocess.PIPE" in script
    assert 'read_text(encoding="utf-8")' in script


def test_frozen_acceptance_forces_utf8_console_output() -> None:
    script = (REPO_ROOT / "scripts" / "acceptance_frozen.py").read_text(
        encoding="utf-8"
    )
    assert 'getattr(_console_stream, "reconfigure", None)' in script
    assert '_reconfigure(encoding="utf-8", errors="replace")' in script


@pytest.mark.parametrize("relative", ["LOCAL_PRIVATE/state.json", ".admin_token_local", "admin-token", "tuomin_apps.json", "private_workspace/project/readme.md"])
def test_build_verifier_rejects_machine_state(tmp_path, relative):
    bundle = _synthetic_bundle(tmp_path)
    leaked = bundle / relative
    leaked.parent.mkdir(parents=True, exist_ok=True)
    leaked.write_text("synthetic only", encoding="utf-8")
    with pytest.raises(RuntimeError, match="private runtime paths"):
        VERIFY.verify_bundle_security(bundle)


def test_release_rejects_source_drift_since_frozen_build(tmp_path, release_repository):
    bundle = _synthetic_bundle(tmp_path)
    (bundle / "_internal" / "delivery.json").write_text(
        json.dumps({"source": {"code_sha256": "old-source"}}), encoding="utf-8")
    with pytest.raises(RuntimeError, match="source changed"):
        PACKAGE.build_release(bundle, tmp_path / "release", "Synthetic")
    assert not (tmp_path / "release").exists()


def test_source_provenance_covers_new_inputs_and_excludes_machine_data(release_repository):
    provenance = PACKAGE.source_provenance()
    assert len(provenance["code_sha256"]) == 64
    files = provenance["files"]
    assert "src/tuomin_gateway/runtime.py" in files
    assert "service_runner.pyw" in files
    assert "demo/samples/synthetic-contract.docx" in files
    assert "demo/synthetic-apps.json" in files
    assert "packaging/icon/tuomin-1024.png" in files
    assert not any("local_private" in name or "private_workspace" in name or "ner-model/" in name or "egg-info" in name for name in files)


def test_native_compiler_closes_stdin_and_passes_source(monkeypatch, tmp_path):
    source = tmp_path / "policy.py"
    source.write_text("VALUE = 1", encoding="utf-8")
    artifact = tmp_path / ("policy" + NATIVE.ext_suffix())
    artifact.write_bytes(b"synthetic")
    captured = {}

    def run(command, **kwargs):
        captured.update(command=command, **kwargs)
        return types.SimpleNamespace(returncode=0)

    monkeypatch.setattr(NATIVE.subprocess, "run", run)
    assert NATIVE.compile_one("policy.py", source, tmp_path) == artifact
    assert captured["command"][-1] == str(source)
    assert captured["stdin"] == subprocess.DEVNULL
    assert captured["encoding"] == "utf-8"
    if sys.platform == "win32":
        assert "--msvc=latest" in captured["command"]


def test_lite_release_has_distinct_name_and_manifest(tmp_path, release_repository):
    bundle = _synthetic_bundle(tmp_path)
    outputs = PACKAGE.build_release(bundle, tmp_path / "release", "Synthetic", lite=True)
    manifest = VERIFY.verify_manifest(bundle, outputs["manifest"])
    assert manifest["variant"] == "lite"
    assert "-windows-x64-lite-" in outputs["archive"].name
    VERIFY.verify_archive(bundle, outputs["archive"], manifest)


@pytest.mark.parametrize("target_platform", ["win32", "darwin"])
def test_spec_preserves_platform_shell_and_console_entries(monkeypatch, tmp_path, target_platform):
    packaging = tmp_path / "packaging"
    packaging.mkdir()
    (tmp_path / "scripts").mkdir()
    shutil.copyfile(REPO_ROOT / "scripts/build_native_modules.py", tmp_path / "scripts/build_native_modules.py")
    static = tmp_path / "src/tuomin_gateway/service/static"
    static.mkdir(parents=True)
    (static / "index.html").write_text("synthetic", encoding="utf-8")
    for relative in NATIVE.CROWN_MODULES:
        artifact = packaging / "native" / relative.replace(".py", NATIVE.ext_suffix())
        artifact.parent.mkdir(parents=True, exist_ok=True)
        artifact.write_bytes(b"synthetic")
    monkeypatch.setenv("TUOMIN_LITE", "1")
    monkeypatch.delenv("TUOMIN_MACOS_APP", raising=False)
    monkeypatch.setattr(sys, "platform", target_platform)
    collected = []
    hooks = types.ModuleType("PyInstaller.utils.hooks")
    hooks.collect_all = lambda package: (collected.append(package) or ([], [], []))
    hooks.collect_data_files = lambda *args, **kwargs: []
    hooks.collect_submodules = lambda *args: []
    monkeypatch.setitem(sys.modules, "PyInstaller", types.ModuleType("PyInstaller"))
    monkeypatch.setitem(sys.modules, "PyInstaller.utils", types.ModuleType("PyInstaller.utils"))
    monkeypatch.setitem(sys.modules, "PyInstaller.utils.hooks", hooks)
    captured = {}

    def analysis(*args, **kwargs):
        captured.update(kwargs)
        return types.SimpleNamespace(pure=[], scripts=[], binaries=kwargs["binaries"], datas=kwargs["datas"])

    pyz = object()
    result = runpy.run_path(str(REPO_ROOT / "packaging/tuomin_gateway.spec"), init_globals={
        "SPECPATH": str(packaging), "Analysis": analysis, "PYZ": lambda *args: pyz,
        "EXE": lambda *args, **kwargs: {"inputs": args, **kwargs},
        "COLLECT": lambda *args, **kwargs: {"inputs": args, **kwargs},
    })
    assert len(captured["binaries"]) == 16
    assert "torch" in captured["excludes"]
    gui = result["executable"]
    assert gui["inputs"][0] is pyz
    if target_platform == "win32":
        assert collected == ["pythonnet", "clr_loader"]
        assert "webview.platforms.edgechromium" in captured["hiddenimports"]
        assert "pystray._win32" in captured["hiddenimports"]
        assert "webview.platforms.cocoa" not in captured["hiddenimports"]
        assert result["bundle"]["name"] == "Tuomin-Lite"
        assert gui["name"] == "Tuomin" and gui["console"] is False
        cli = result["cli_executable"]
        assert cli["name"] == "Tuomin-cli" and cli["console"] is True
        assert cli["inputs"][0] is pyz
        assert cli["inputs"][1] is gui["inputs"][1]
        assert result["bundle"]["inputs"][:2] == (gui, cli)
        assert result["bundle"]["inputs"][2:] == (captured["binaries"], captured["datas"])
    else:
        assert collected == ["objc", "Foundation", "AppKit", "WebKit", "UserNotifications"]
        assert "webview.platforms.cocoa" in captured["hiddenimports"]
        assert "webview.platforms.edgechromium" not in captured["hiddenimports"]
        assert result["bundle"]["name"] == "tuomin-workbench-lite"
        assert gui["name"] == "tuomin-workbench" and gui["console"] is True
        assert "cli_executable" not in result
        assert result["bundle"]["inputs"] == (gui, captured["binaries"], captured["datas"])
