#!/bin/bash
# Build the PyInstaller onedir distribution for the local workbench.

set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd -- "$SCRIPT_DIR/.." && pwd)"
VENV_DIR="$REPO_ROOT/.venv-packaging"
BUILD_VARIANT="full"
PACKAGE_NAME="tuomin-workbench"
if [[ "${TUOMIN_LITE:-0}" == "1" ]]; then
    BUILD_VARIANT="lite"
    PACKAGE_NAME="tuomin-workbench-lite"
fi
DIST_ROOT="$REPO_ROOT/dist/$BUILD_VARIANT"
DIST_DIR="$DIST_ROOT/$PACKAGE_NAME"
DIST_BINARY="$DIST_DIR/tuomin-workbench"

cd "$REPO_ROOT"

if [[ ! -x "$VENV_DIR/bin/python" ]]; then
    python3 -m venv "$VENV_DIR"
fi

export MACOSX_DEPLOYMENT_TARGET=13.0
export TUOMIN_MACOS_APP="${TUOMIN_MACOS_APP:-1}"
export TUOMIN_BUILD_NUMBER="${TUOMIN_BUILD_NUMBER:-$(date +%Y%m%d.%H%M%S)}"
export HF_HUB_OFFLINE=1
export TRANSFORMERS_OFFLINE=1
# Full bundles the NER runtime (torch/transformers); Lite excludes it in the spec.
EXTRAS="serve,document,pdf,shell"
if [[ "$BUILD_VARIANT" == "full" ]]; then
    EXTRAS="$EXTRAS,ner"
fi
if [[ "${TUOMIN_BUILD_OFFLINE:-0}" == "1" ]]; then
    # All pinned dependencies must already exist. No index or model access.
    "$VENV_DIR/bin/python" -m pip install --no-index --no-deps --no-build-isolation ".[$EXTRAS]"
else
    # The arm64 lock pins the verified versions (torch 2.13 / transformers 5.13 /
    # fastapi 0.139 …) so a rebuilt venv reproduces earlier releases.
    if [[ -f "$REPO_ROOT/requirements-macos-arm64.lock" ]]; then
        "$VENV_DIR/bin/python" -m pip install -r "$REPO_ROOT/requirements-macos-arm64.lock"
    fi
    "$VENV_DIR/bin/python" -m pip install ".[$EXTRAS]" pyinstaller nuitka
fi

# 交付标识：按 TUOMIN_CUSTOMER 生成烤入包内的 delivery.json
# （WebUI 与 /api/v1/license/status 展示"交付单位"；不设置则本构建无标识）。
DELIVERY_JSON="$REPO_ROOT/packaging/delivery.json"
if [[ -n "${TUOMIN_CUSTOMER:-}" ]]; then
    "$VENV_DIR/bin/python" - "$DELIVERY_JSON" <<'EOF'
import json, sys
from datetime import datetime, timezone
from pathlib import Path
import os
out = Path(sys.argv[1])
out.write_text(json.dumps({
    "customer": os.environ["TUOMIN_CUSTOMER"],
    "built_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    "product": "tuomin-workbench",
}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
print(f"[delivery] customer tag: {os.environ['TUOMIN_CUSTOMER']}")
EOF
else
    rm -f "$DELIVERY_JSON"
    echo "[delivery] TUOMIN_CUSTOMER 未设置，本构建不含交付标识"
fi

# 分发加固：先把皇冠模块编译为原生扩展（packaging/native/），spec 据此
# 剔除 PYZ 中对应 .pyc 并收编 .so。NER 依赖不随编译名单，编译仅覆盖纯逻辑模块。
"$VENV_DIR/bin/python" scripts/build_native_modules.py

"$VENV_DIR/bin/pyinstaller" --clean --noconfirm --distpath "$DIST_ROOT" --workpath "$REPO_ROOT/build/$BUILD_VARIANT" packaging/tuomin_gateway.spec

if [[ ! -x "$DIST_BINARY" ]]; then
    echo "Build failed: expected executable was not created at $DIST_BINARY" >&2
    exit 1
fi

# 交付清单：无秘密的交付追踪（版本/commit/客户/文件哈希），随 zip 发出。
"$VENV_DIR/bin/python" - "$DIST_BINARY" "$DIST_ROOT/DELIVERY_MANIFEST.json" <<'EOF'
import hashlib, json, os, subprocess, sys
from datetime import datetime, timezone
from pathlib import Path

binary = Path(sys.argv[1])
manifest_path = Path(sys.argv[2])
def sha256(p: Path) -> str:
    h = hashlib.sha256()
    with p.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()
commit = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
manifest = {
    "product": "tuomin-workbench",
    "built_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    "git_commit": commit,
    "customer": os.environ.get("TUOMIN_CUSTOMER") or None,
    "files": {binary.name: {"sha256": sha256(binary), "bytes": binary.stat().st_size}},
}
manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
print(f"[delivery] manifest: {manifest_path}")
EOF

echo "Build complete. Start the workbench with:"
echo "  $DIST_BINARY"
echo "The native workbench opens automatically; use --headless for a browser or --demo for isolated synthetic samples."
printf '%s\n' "$DIST_DIR"
