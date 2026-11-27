#!/usr/bin/env python3
"""Materialize the pinned CLUENER snapshot for an offline frozen build."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import shutil
import sys
import tempfile


REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = REPO_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from tuomin_gateway.detectors.ner import MODEL_NAME, MODEL_REVISION  # noqa: E402


REQUIRED_FILES = {
    "config.json",
    "special_tokens_map.json",
    "tokenizer_config.json",
    "vocab.txt",
}
WEIGHT_FILES = {"model.safetensors", "pytorch_model.bin"}
MODEL_METADATA_FILE = "tuomin-model.json"


def expected_model_metadata() -> dict[str, object]:
    return {
        "model": MODEL_NAME,
        "revision": MODEL_REVISION,
        "offline_required": True,
    }


def validate_model_directory(directory: Path) -> list[str]:
    missing = sorted(name for name in REQUIRED_FILES if not (directory / name).is_file())
    metadata_path = directory / MODEL_METADATA_FILE
    try:
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        missing.append(MODEL_METADATA_FILE)
    else:
        expected = expected_model_metadata()
        if any(metadata.get(key) != value for key, value in expected.items()):
            missing.append(
                f"{MODEL_METADATA_FILE}(model/revision mismatch)"
            )
    if not any((directory / name).is_file() for name in WEIGHT_FILES):
        missing.append("model.safetensors|pytorch_model.bin")
    return missing


def materialize_model(destination: Path, *, allow_download: bool = False) -> Path:
    destination = destination.resolve()
    if destination.is_dir():
        missing = validate_model_directory(destination)
        if missing:
            raise RuntimeError(
                f"existing NER model directory is incomplete: {destination}; "
                f"missing={missing}"
            )
        return destination
    if destination.exists():
        raise RuntimeError(f"NER model destination is not a directory: {destination}")

    from huggingface_hub import snapshot_download

    try:
        source = Path(
            snapshot_download(
                MODEL_NAME,
                revision=MODEL_REVISION,
                local_files_only=not allow_download,
                allow_patterns=sorted(REQUIRED_FILES | WEIGHT_FILES),
            )
        )
    except Exception as exc:
        mode = "download-enabled" if allow_download else "local-cache-only"
        raise RuntimeError(
            f"pinned NER model is unavailable ({mode}): "
            f"{MODEL_NAME}@{MODEL_REVISION}: {exc}"
        ) from exc

    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(
        prefix="tuomin-ner-model-",
        dir=destination.parent,
    ) as temp_root:
        staged = Path(temp_root) / "model"
        staged.mkdir()
        for name in sorted(REQUIRED_FILES | WEIGHT_FILES):
            if (source / name).is_file():
                shutil.copy2(source / name, staged / name)
        (staged / MODEL_METADATA_FILE).write_text(
            json.dumps(expected_model_metadata(), ensure_ascii=False, indent=2)
            + "\n",
            encoding="utf-8",
        )
        missing = validate_model_directory(staged)
        if missing:
            raise RuntimeError(
                f"pinned NER snapshot is incomplete: missing={missing}"
            )
        staged.replace(destination)
    return destination


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--destination",
        type=Path,
        default=REPO_ROOT / "packaging" / "ner-model",
    )
    parser.add_argument(
        "--allow-download",
        action="store_true",
        help="allow fetching the exact pinned revision when it is not cached",
    )
    args = parser.parse_args()
    destination = materialize_model(
        args.destination,
        allow_download=args.allow_download,
    )
    print(f"NER model ready: {destination}")
    print(f"model revision: {MODEL_NAME}@{MODEL_REVISION}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
