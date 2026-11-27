"""Disposable synthetic runtime, configured before any production path lookup."""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
from tuomin_gateway.platform_security import restrict_permissions


SAMPLE_NAMES = ("synthetic-contract.docx", "synthetic-register.xlsx")


@dataclass(frozen=True)
class DemoEnvironment:
    root: Path
    samples_dir: Path


def _assets_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys._MEIPASS) / "demo"
    return Path(__file__).resolve().parents[3] / "demo"


def _clear_runtime_environment() -> None:
    for name in tuple(os.environ):
        if name.startswith("TUOMIN_"):
            del os.environ[name]


@contextmanager
def demo_environment():
    """Isolate Tuomin state; keep only port/browser preferences, then restore it.

    Assets are copied, never edited in place. The private random suffix avoids
    reusing a stale or pre-created directory after a PID is recycled. Runtime
    credentials created inside this context are removed from the environment
    before the caller's original settings are restored.
    """
    original = {name: value for name, value in os.environ.items() if name.startswith("TUOMIN_")}
    _clear_runtime_environment()
    try:
        for name in ("TUOMIN_PORT", "TUOMIN_OPEN_BROWSER", "TUOMIN_NO_BROWSER", "TUOMIN_WARM_NER", "TUOMIN_NER_MODEL_DIR"):
            if name in original:
                os.environ[name] = original[name]
        with tempfile.TemporaryDirectory(prefix=f"tuomin-demo-{os.getpid()}-") as temporary:
            root = Path(temporary)
            restrict_permissions(root, is_dir=True)
            assets = _assets_dir()
            dictionary = root / "synthetic-dictionary.json"
            shutil.copyfile(assets / "synthetic-dictionary.json", dictionary)
            restrict_permissions(dictionary, is_dir=False)
            registry = json.loads((assets / "synthetic-apps.json").read_text(encoding="utf-8"))
            for entry in registry["apps"].values():
                entry["dictionary"] = str(dictionary)
            config = root / "synthetic-apps.json"
            config.write_text(json.dumps(registry, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            restrict_permissions(config, is_dir=False)
            samples_dir = root / "samples"
            samples_dir.mkdir(mode=0o700)
            restrict_permissions(samples_dir, is_dir=True)
            for name in SAMPLE_NAMES:
                target = samples_dir / name
                shutil.copyfile(assets / "samples" / name, target)
                restrict_permissions(target, is_dir=False)
            os.environ.update({
                "TUOMIN_DATA_DIR": str(root),
                "TUOMIN_MAPPING_DIR": str(root / "mappings"),
                "TUOMIN_CONFIG": str(config),
                "TUOMIN_ALLOW_PLAINTEXT_STORE": "1",
                "TUOMIN_DEMO_SAMPLE_DIR": str(samples_dir),
                "TUOMIN_WORKBUDDY_MODELS": str(root / "workbuddy" / "models.json"),
            })
            yield DemoEnvironment(root=root, samples_dir=samples_dir)
    finally:
        _clear_runtime_environment()
        os.environ.update(original)
