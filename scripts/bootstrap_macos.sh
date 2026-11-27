#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LOCK_FILE="$ROOT_DIR/requirements-macos-arm64.lock"
EXPECTED_PYTHON="3.12.10"
MODEL_ID="uer/roberta-base-finetuned-cluener2020-chinese"
MODEL_REVISION="cddd8fc233e373855a8c0a7f4b7eb83acb686a2b"
DOWNLOAD_MODEL=0
INIT_CONFIG=0
SKIP_INSTALL=0
EXTRAS="dev,ner,document,shell"

usage() {
  echo "Usage: scripts/bootstrap_macos.sh [--with-pdf] [--download-model] [--init-config] [--skip-install]"
}

for arg in "$@"; do
  case "$arg" in
    --with-pdf) EXTRAS="dev,ner,document,pdf,shell" ;;
    --download-model) DOWNLOAD_MODEL=1 ;;
    --init-config) INIT_CONFIG=1 ;;
    --skip-install) SKIP_INSTALL=1 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown argument: $arg" >&2; usage >&2; exit 2 ;;
  esac
done

if [[ "$(uname -s)" != "Darwin" || "$(uname -m)" != "arm64" ]]; then
  echo "Tuomin macOS bootstrap requires native macOS arm64." >&2
  exit 1
fi

cd "$ROOT_DIR"

if [[ -n "${PYTHON_BIN:-}" ]]; then
  PYTHON="$PYTHON_BIN"
elif command -v pyenv >/dev/null 2>&1; then
  PYTHON="$(pyenv which python 2>/dev/null || true)"
else
  PYTHON="$(command -v python3 || true)"
fi

if [[ -z "$PYTHON" ]]; then
  echo "Python $EXPECTED_PYTHON was not found. Install it with pyenv first." >&2
  exit 1
fi

ACTUAL_PYTHON="$($PYTHON -c 'import platform; print(platform.python_version())')"
if [[ "$ACTUAL_PYTHON" != "$EXPECTED_PYTHON" ]]; then
  echo "Expected Python $EXPECTED_PYTHON, found $ACTUAL_PYTHON at $PYTHON." >&2
  echo "Set PYTHON_BIN to the verified Python executable and retry." >&2
  exit 1
fi

if [[ ! -x .venv/bin/python ]]; then
  "$PYTHON" -m venv .venv
fi

if [[ "$SKIP_INSTALL" -eq 0 ]]; then
  export PIP_CONSTRAINT="$LOCK_FILE"
  .venv/bin/python -m pip install -e ".[$EXTRAS]"
  .venv/bin/python -m pip check
fi

if [[ "$DOWNLOAD_MODEL" -eq 1 ]]; then
  .venv/bin/hf download "$MODEL_ID" \
    config.json pytorch_model.bin special_tokens_map.json tokenizer_config.json vocab.txt \
    --revision "$MODEL_REVISION" --max-workers 4
fi

if [[ "$INIT_CONFIG" -eq 1 && ! -e tuomin_apps.json ]]; then
  cp tuomin_apps.macos.example.json tuomin_apps.json
  chmod 600 tuomin_apps.json
fi

if [[ "$DOWNLOAD_MODEL" -eq 1 ]]; then
  .venv/bin/python scripts/doctor_macos.py --load-ner
else
  .venv/bin/python scripts/doctor_macos.py
fi
