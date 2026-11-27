"""First-run defaults for the desktop app: one editable local dictionary.

A fresh desktop install has an empty app registry, so the dictionary page had
nothing to edit and the clipboard had no dictionary to mirror. The launcher
calls :func:`ensure_default_workbench_app` on every desktop start (native or
``--headless``) when it owns the registry file; demo mode and explicit
``TUOMIN_CONFIG`` deployments are left alone. Pure Python, no Cocoa.
"""
from __future__ import annotations

import json
import sys
from tuomin_gateway.profiles import RULES_WORKBENCH
from pathlib import Path
from .storage import write_private

DEFAULT_APP_ID = "workbench"
DEFAULT_APP_NAME = "本机工作台"
DEFAULT_PROFILE = "file_workbench"


def is_public_lite_build() -> bool:
    bundle = getattr(sys, "_MEIPASS", None)
    return bool(getattr(sys, "frozen", False) and bundle and (Path(bundle) / "public-lite.txt").is_file())


def default_dictionary_path(data_dir: Path) -> Path:
    return Path(data_dir) / "dictionaries" / f"{DEFAULT_APP_ID}.json"


def ensure_default_workbench_app(config_path: Path, data_dir: Path) -> bool:
    """Register ``workbench`` with its own dictionary file if it is missing.

    Returns True when the registry was changed. Existing entries (including a
    user-modified ``workbench``) are never rewritten; a missing dictionary file
    for the default app is recreated empty so detection does not fail closed.
    """
    config_path = Path(config_path)
    try:
        raw = json.loads(config_path.read_text(encoding="utf-8")) if config_path.exists() else {}
    except (OSError, ValueError):
        return False
    if not isinstance(raw, dict):
        return False
    apps = raw.get("apps")
    if not isinstance(apps, dict):
        apps = {}
        raw["apps"] = apps

    dictionary = default_dictionary_path(data_dir)
    changed = False
    entry = apps.get(DEFAULT_APP_ID)
    if not isinstance(entry, dict):
        apps[DEFAULT_APP_ID] = {
            "name": DEFAULT_APP_NAME,
            "profile": RULES_WORKBENCH if is_public_lite_build() else DEFAULT_PROFILE,
            "dictionary": str(dictionary),
        }
        changed = True
        dictionary_path = dictionary
    else:
        declared = entry.get("dictionary")
        dictionary_path = Path(declared) if isinstance(declared, str) and declared else None

    if dictionary_path is not None and not dictionary_path.exists():
        dictionary_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        from tuomin_gateway.platform_security import restrict_permissions
        restrict_permissions(dictionary_path.parent, is_dir=True)
        dictionary_path.write_text("[]\n", encoding="utf-8")
        restrict_permissions(dictionary_path, is_dir=False)

    if changed:
        config_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        write_private(config_path, raw)
    return changed
