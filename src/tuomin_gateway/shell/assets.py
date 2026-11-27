"""Locate packaged icon assets both in the frozen bundle and in a source checkout."""
from __future__ import annotations

import sys
from pathlib import Path


def asset_path(name: str) -> Path | None:
    candidates = []
    if getattr(sys, "frozen", False):
        candidates.append(Path(getattr(sys, "_MEIPASS", "")) / "icon" / name)
    candidates.append(Path(__file__).resolve().parents[3] / "packaging" / "icon" / name)
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return None
