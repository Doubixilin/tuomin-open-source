#!/usr/bin/env python3
"""Compile Tuomin crown-jewel modules to native extensions with Nuitka.

名单中的
纯逻辑模块被编译为 `.so`，PyInstaller 收 `.so` 而不再收对应 `.pyc`；
仓库源码与日常开发/测试流程完全不受影响（编译只是打包期步骤）。

产物写入 packaging/native/（相对 tuomin_gateway 包布局），spec 会读取该目录：
- PYZ 中剔除同名模块；
- `.so` 作为二进制收进包内对应包目录。

用法：
    .venv-packaging/bin/python scripts/build_native_modules.py          # 全量
    .venv-packaging/bin/python scripts/build_native_modules.py --check  # 只核对产物存在
"""

from __future__ import annotations

import argparse
import ast
import importlib.machinery
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = REPO_ROOT / "src"
OUT_DIR = REPO_ROOT / "packaging" / "native"

# 皇冠模块名单（相对 src/tuomin_gateway，纯逻辑模块；依赖 inspect/动态特性的
# FastAPI/pydantic 层、ctypes Keychain 层永不进入本名单）。
CROWN_MODULES = [
    "policy.py",
    "profiles.py",
    "roles.py",
    "fusion.py",
    "redactor.py",
    "refill.py",
    "placeholders.py",
    "mapping.py",
    "detectors/rules.py",
    "guard/injection.py",
    "guard/secrets.py",
    "document/refill_flow.py",
    "document/docx_patch.py",
    "document/office_patch.py",
    # 授权执行核心与持久化：分类表、enforce 后手、状态解析全部进原生代码。
    "licensing/core.py",
    "licensing/state.py",
]


def ext_suffix() -> str:
    return importlib.machinery.EXTENSION_SUFFIXES[0]


def _strip_docstrings(source: str) -> str:
    """移除模块/类/函数 docstring（AST 变换后 unparse）。

    逆向评估发现 Nuitka 产物 strings 可直接读出完整 docstring 与内部路径
    （2026-08-25）。docstring 只存在于分发产物之外的需要；源码树不受影响。
    """
    tree = ast.parse(source)

    def _drop_docstring(node: ast.AST) -> None:
        body = getattr(node, "body", None)
        if (
            isinstance(body, list)
            and body
            and isinstance(body[0], ast.Expr)
            and isinstance(body[0].value, ast.Constant)
            and isinstance(body[0].value.value, str)
        ):
            del body[0]
            if not body:  # docstring 是唯一语句时补 pass，避免空 body
                body.append(ast.Pass())

    for node in ast.walk(tree):
        _drop_docstring(node)
    return ast.unparse(ast.fix_missing_locations(tree))


# strings 基线：产物中不得出现的内部信息（docstring 片段、仓库内相对路径等）。
_STRINGS_DENYLIST = (
    "scripts/",
    "docs/",
    "DEVELOPMENT_STATUS",
    "build_native_modules",
    "sign_license",
)


def check_strings_baseline(artifacts: list[Path]) -> list[str]:
    """扫描原生产物的禁止字符串，返回违规描述列表（空 = 通过）。

    直接检查常见窄字符串与 UTF-16 编码，避免依赖 Windows 默认不存在的
    GNU ``strings``，同时让 ``--check`` 对复用的旧产物也能执行同一门禁。
    """
    violations: list[str] = []
    for artifact in artifacts:
        payload = artifact.read_bytes()
        for needle in _STRINGS_DENYLIST:
            encodings = (
                needle.encode("utf-8"),
                needle.encode("utf-16-le"),
                needle.encode("utf-16-be"),
            )
            if any(encoded in payload for encoded in encodings):
                violations.append(f"{artifact.name}: 含违禁字符串 {needle!r}")
    return violations


def expected_artifacts() -> list[Path]:
    suffix = ext_suffix()
    return [
        OUT_DIR / rel.replace(".py", suffix)
        for rel in CROWN_MODULES
    ]


def compile_one(module_rel: str, source_path: Path, work_dir: Path) -> Path:
    cmd = [
        sys.executable,
        "-m",
        "nuitka",
        "--module",
        f"--output-dir={work_dir}",
        "--remove-output",
        "--quiet",
        "--no-progressbar",
    ]
    if os.name == "nt":
        cmd.append("--msvc=latest")
    cmd.append(str(source_path))
    result = subprocess.run(
        cmd,
        cwd=source_path.parent,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=900,
        # Keep unattended builds from waiting for optional download prompts.
        stdin=subprocess.DEVNULL,
    )
    if result.returncode != 0:
        raise SystemExit(
            f"nuitka failed for {module_rel}:\n{result.stdout}\n{result.stderr}"
        )
    name = Path(module_rel).stem
    produced = work_dir / f"{name}{ext_suffix()}"
    if not produced.is_file():
        raise SystemExit(f"nuitka produced no artifact for {module_rel}")
    return produced


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="only verify that all expected artifacts exist",
    )
    args = parser.parse_args()

    if args.check:
        missing = [str(p) for p in expected_artifacts() if not p.is_file()]
        if missing:
            print("missing native artifacts:", *missing, sep="\n  ")
            return 1
        violations = check_strings_baseline(expected_artifacts())
        if violations:
            print("strings 基线违规:", *violations, sep="\n  ")
            return 1
        print(f"native artifacts OK ({len(expected_artifacts())} modules)")
        print(f"strings baseline OK ({len(expected_artifacts())} artifacts)")
        return 0

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    # 先清旧产物，避免名单收缩后残留过期 .so 被收进包
    for pattern in ("*.so", "*.pyd"):
        for stale in OUT_DIR.rglob(pattern):
            stale.unlink()

    with tempfile.TemporaryDirectory(prefix="tuomin-nuitka-") as tmp:
        work_dir = Path(tmp) / "out"
        stripped_dir = Path(tmp) / "stripped"
        for module_rel in CROWN_MODULES:
            src = SRC_DIR / "tuomin_gateway" / module_rel
            if not src.is_file():
                raise SystemExit(f"crown module missing: {src}")
            # docstring 剥离后的临时副本参与编译（源码树不受影响）
            stripped = stripped_dir / module_rel
            stripped.parent.mkdir(parents=True, exist_ok=True)
            stripped.write_text(
                _strip_docstrings(src.read_text(encoding="utf-8")), encoding="utf-8"
            )
            work_dir.mkdir(parents=True, exist_ok=True)
            produced = compile_one(module_rel, stripped, work_dir)
            dest = OUT_DIR / module_rel.replace(".py", ext_suffix())
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(produced, dest)
            for leftover in work_dir.glob(f"*{ext_suffix()}"):
                leftover.unlink()
            print(f"[native] {module_rel} -> {dest.relative_to(REPO_ROOT)}")

    violations = check_strings_baseline(expected_artifacts())
    if violations:
        print("strings 基线违规:", *violations, sep="\n  ")
        return 1
    print(f"strings baseline OK ({len(expected_artifacts())} artifacts)")
    print(f"native build complete: {len(CROWN_MODULES)} modules -> {OUT_DIR}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
