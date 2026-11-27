# PyInstaller onedir desktop bundles. Full adds the local NER model and runtime.
# Both variants include the document workbench, UI assets and Skill template.
# See THIRD_PARTY_NOTICES.md before distributing a completed bundle.

import importlib.machinery
import os
import sys
import runpy
from pathlib import Path

from PyInstaller.utils.hooks import collect_all, collect_data_files, collect_submodules

project_root = Path(SPECPATH).resolve().parent
native_definition = runpy.run_path(
    str(project_root / "scripts" / "build_native_modules.py")
)
crown_modules = native_definition["CROWN_MODULES"]
expected_native_modules = {
    ".".join(("tuomin_gateway", *Path(relative).parent.parts, Path(relative).stem))
    for relative in crown_modules
}
public_build = os.environ.get("TUOMIN_PUBLIC_BUILD") == "1"
lite = os.environ.get("TUOMIN_LITE") == "1"
package_name = ("Tuomin-Lite" if lite else "Tuomin") if sys.platform == "win32" else ("tuomin-workbench-lite" if lite else "tuomin-workbench")
shell_datas, shell_binaries, shell_imports = [], [], []
if sys.platform == "darwin":
    shell_imports.extend(["webview.platforms.cocoa", "PyObjCTools.AppHelper"])
    shell_packages = ("objc", "Foundation", "AppKit", "WebKit", "UserNotifications")
elif sys.platform == "win32":
    shell_imports.extend(["webview.platforms.winforms", "webview.platforms.edgechromium", "clr", "pythonnet", "clr_loader", "pystray._win32", "PIL.Image"])
    shell_packages = ("pythonnet", "clr_loader")
else:
    shell_packages = ()
for package in shell_packages:
    data, binary, imports = collect_all(package)
    shell_datas.extend(data)
    shell_binaries.extend(binary)
    shell_imports.extend(imports)
# The macOS hook does not collect pywebview's injected JS bridge resources.
webview_resources = ["js/**/*.js"]
if sys.platform == "win32":
    if public_build and sys.maxsize > 2**32:
        webview_resources.extend(["lib/Microsoft.Web.WebView2.Core.dll",
                                  "lib/Microsoft.Web.WebView2.WinForms.dll",
                                  "lib/WebBrowserInterop.x64.dll",
                                  "lib/runtimes/win-x64/native/WebView2Loader.dll"])
    else:
        webview_resources.append("lib/**/*")
shell_datas.extend(collect_data_files("webview", includes=webview_resources))
model_datas = []
model_files = ("config.json", "pytorch_model.bin", "tokenizer_config.json", "special_tokens_map.json", "vocab.txt")
if not lite and not public_build:
    model_dir = Path(os.environ.get("TUOMIN_BUNDLE_MODEL_DIR", str(
        Path.home() / ".cache/huggingface/hub/models--uer--roberta-base-finetuned-cluener2020-chinese/snapshots/cddd8fc233e373855a8c0a7f4b7eb83acb686a2b"
    )))
    for name in model_files:
        source = model_dir / name
        if not source.is_file():
            raise SystemExit("Full requires the pinned model in the local cache or TUOMIN_BUNDLE_MODEL_DIR; no download is attempted")
        # Cache snapshots use symlinks to hash-named blobs. Preserve the
        # snapshot filename so AutoTokenizer/AutoModel can find each asset.
        model_datas.append((str(source), "ner-model"))
icon_datas = [(str(project_root / "packaging/icon" / name), "icon") for name in ("menubar.png", "menubar@2x.png", "tuomin-1024.png") if (project_root / "packaging/icon" / name).is_file()]
demo_datas = [(str(project_root / "demo" / name), "demo") for name in ("synthetic-apps.json", "synthetic-dictionary.json")]
demo_datas += [(str(project_root / "demo/samples" / name), "demo/samples") for name in ("synthetic-contract.docx", "synthetic-register.xlsx")]
ner_imports = [] if lite else [
    *collect_submodules("transformers.models.bert"), *collect_submodules("transformers.pipelines"),
    "transformers.modeling_utils", "transformers.configuration_utils",
    "transformers.tokenization_utils_base", "transformers.activations", "transformers.modeling_gelu",
]

# 分发加固：packaging/native/ 下的 Nuitka 编译产物（scripts/build_native_modules.py
# 生成）。同名模块必须从 PYZ 剔除、以 .so 形式收进包内包目录，否则 PYZ 里的
# .pyc 会优先于 .so 被加载，加固失效。
native_dir = project_root / "packaging" / "native"
native_binaries = []
compiled_module_names = set()
if not public_build and not native_dir.is_dir():
    raise SystemExit(f"native module directory is unavailable: {native_dir}")
extension_suffixes = tuple(importlib.machinery.EXTENSION_SUFFIXES)
for extension_path in sorted(
    path
    for path in native_dir.rglob("*")
    if not public_build and path.is_file() and path.name.endswith(extension_suffixes)
):
    rel = extension_path.relative_to(native_dir)
    module_name = ".".join(
        ("tuomin_gateway", *rel.parent.parts, rel.name.split(".")[0])
    )
    if module_name in compiled_module_names:
        raise SystemExit(f"duplicate native module artifact: {module_name}")
    compiled_module_names.add(module_name)
    dest_dir = Path("tuomin_gateway") / rel.parent
    native_binaries.append((str(extension_path), str(dest_dir)))
missing_native = (set() if public_build else expected_native_modules) - compiled_module_names
unexpected_native = compiled_module_names - expected_native_modules
if missing_native or unexpected_native:
    raise SystemExit(
        "native module set mismatch: "
        f"missing={sorted(missing_native)} unexpected={sorted(unexpected_native)}"
    )

# 静态 WebUI（横评/架构/注入演示页已于 2026-10-24 移除；benchmark-data.json
# 过滤保留为防御，防止历史产物混入分发包）
_static_dir = project_root / "src" / "tuomin_gateway" / "service" / "static"
_static_datas = [
    (str(p), "tuomin_gateway/service/static")
    for p in sorted(_static_dir.iterdir())
    if p.is_file() and p.name != "benchmark-data.json"
]

analysis = Analysis(
    [str(project_root / "packaging" / "launcher.py")],
    # 必须显式指向仓库源码：打包 venv 里若存在已安装的 tuomin_gateway 副本，
    # modulegraph 需要优先收集当前源码，确保产物提供本次构建的接口。
    pathex=[str(project_root / "src")],
    binaries=[*native_binaries, *shell_binaries],
    datas=[
        *_static_datas,
        (str(project_root / "LICENSE"), "licenses"),
        (str(project_root / "THIRD_PARTY_NOTICES.md"), "licenses"),
        (str(project_root / "src/tuomin_gateway/document/vendor/NOTICE.md"), "licenses/document"),
        *shell_datas,
        *model_datas,
        *([(str(project_root / "packaging/public-lite.txt"), ".")] if public_build and lite else []),
        *(collect_data_files("pdfminer", includes=["cmap/*"]) if public_build else []),
        *demo_datas,
        *icon_datas,
        # 随包的 Skill 模板（参考文件，供用户复制到各 agent 的 skills 目录）
        (str(project_root / "integrations" / "skills"), "skills"),
        # 交付标识（A 机制：构建期由 build_app.sh 按 TUOMIN_CUSTOMER 生成；
        # 不存在时产物无交付标识，service 层按 None 处理）
        *(
            [(str(project_root / "packaging" / "delivery.json"), ".")]
            if (project_root / "packaging" / "delivery.json").is_file()
            else []
        ),
    ],
    hiddenimports=[
        "uvicorn.logging",
        "uvicorn.loops.auto",
        "uvicorn.protocols.http.auto",
        "uvicorn.protocols.websockets.auto",
        "uvicorn.lifespan.on",
        "openpyxl",
        "xlrd",
        *(["pdfminer.high_level"] if public_build else ["pymupdf"]),
        *shell_imports,
        *ner_imports,
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[*(["pymupdf", "fitz"] if public_build else []), "tensorflow", "flax", "jax", "pytest", "_pytest", *(["torch", "transformers", "tokenizers", "safetensors", "huggingface_hub"] if lite else [])],
    module_collection_mode={"pystray": "py", "certifi": "py"} if public_build else None,
    noarchive=False,
)

if public_build:
    collect_notices = runpy.run_path(str(project_root / "scripts/bundle_notices.py"))["bundle_notices"]
    from PyInstaller.config import CONF
    analysis.datas.extend(collect_notices(analysis, project_root, CONF["workpath"]))

# 剔除已编译为原生扩展的模块，避免 PYZ 中的 .pyc 遮蔽 .so。
if compiled_module_names:
    present = {name for name, _, _ in analysis.pure}
    excluded = compiled_module_names & present
    absent = compiled_module_names - present
    if absent:
        print(f"[spec] WARNING: native modules not found in PYZ graph: {sorted(absent)}")
    analysis.pure = [
        (name, graph, typecode)
        for (name, graph, typecode) in analysis.pure
        if name not in compiled_module_names
    ]
    print(f"[spec] native modules excluded from PYZ: {len(excluded)}")

pyz = PYZ(analysis.pure)

executable = EXE(
    pyz,
    analysis.scripts,
    [],
    exclude_binaries=True,
    name="Tuomin" if sys.platform == "win32" else "tuomin-workbench",
    icon=str(project_root / "packaging/icon/tuomin-1024.png") if sys.platform == "win32" else None,
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=sys.platform != "win32",
)

executables = [executable]
if sys.platform == "win32":
    # A real GUI-subsystem entry avoids creating a Windows Terminal window.
    # Keep a console entry for MCP pipes, diagnostics and headless automation;
    # both entries use the same Python code and shared onedir dependencies.
    cli_executable = EXE(
        pyz,
        analysis.scripts,
        [],
        exclude_binaries=True,
        name="Tuomin-cli",
        icon=str(project_root / "packaging/icon/tuomin-1024.png"),
        debug=False,
        bootloader_ignore_signals=False,
        strip=False,
        upx=False,
        console=True,
    )
    executables.append(cli_executable)

bundle = COLLECT(
    *executables,
    analysis.binaries,
    analysis.datas,
    strip=False,
    upx=False,
    name=package_name,
)
if not lite and not public_build:
    packaged_model = Path(DISTPATH) / package_name / "_internal" / "ner-model"
    if any(not (packaged_model / name).is_file() for name in model_files):
        raise SystemExit("Full model assets are missing their standard filenames")

_REDACTABLE_TYPES = [
    "org.openxmlformats.wordprocessingml.document", "com.microsoft.word.doc",
    "org.openxmlformats.spreadsheetml.sheet", "com.microsoft.excel.xls",
    "org.openxmlformats.presentationml.presentation", "com.adobe.pdf",
    "public.plain-text", "net.daringfireball.markdown", "public.comma-separated-values-text",
    "public.json", "public.yaml",
]

# 独立发布流程启用标准 .app 布局；原有 onedir 构建入口保持可用。
if os.environ.get("TUOMIN_MACOS_APP") == "1":
    required_macos = runpy.run_path(str(project_root / "scripts/release_macos.py"))["required_macos"]
    minimum_system = required_macos(Path(DISTPATH) / package_name)
    print(f"[spec] measured minimum macOS: {minimum_system}")
    app = BUNDLE(
        bundle,
        name="Tuomin Lite.app" if lite else "Tuomin.app",
        bundle_identifier="com.tuomin.workbench.lite" if lite else "com.tuomin.workbench",
        icon=str(project_root / "packaging/icon/Tuomin.icns"),
        version="0.2.0",
        info_plist={
            "CFBundleVersion": os.environ.get("TUOMIN_BUILD_NUMBER", "20261024.1"),
            "LSMinimumSystemVersion": minimum_system,
            "NSUserNotificationAlertStyle": "alert",
            "LSApplicationCategoryType": "public.app-category.productivity",
            # Drop on the Dock icon / 打开方式: Alternate rank never takes over
            # the default app for these documents.
            "CFBundleDocumentTypes": [{
                "CFBundleTypeName": "可脱敏文档",
                "CFBundleTypeRole": "Viewer",
                "LSHandlerRank": "Alternate",
                "LSItemContentTypes": _REDACTABLE_TYPES,
            }],
            # Finder 右键「服务 / 快速操作 → 用 Tuomin 脱敏」.
            "NSServices": [{
                "NSMenuItem": {"default": "用 Tuomin 脱敏"},
                "NSMessage": "redactFiles",
                "NSPortName": "Tuomin",
                "NSRequiredContext": {},
                "NSSendFileTypes": _REDACTABLE_TYPES,
            }],
        },
    )
