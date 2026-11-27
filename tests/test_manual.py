"""The distributable HTML manual builds offline with every screenshot inlined."""
from __future__ import annotations

import re
import runpy
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_manual_builds_self_contained(tmp_path):
    source = (ROOT / "docs/manual/manual.src.html").read_text(encoding="utf-8")
    referenced = set(re.findall(r"\{\{img:([\w-]+)\}\}", source))
    assert referenced and all((ROOT / "docs/manual/img" / f"{name}.webp").is_file() for name in referenced)
    out = runpy.run_path(str(ROOT / "scripts/build_manual.py"))["build"](tmp_path / "manual.html")
    html = out.read_text(encoding="utf-8")
    assert "{{" not in html and html.count("data:image/webp;base64,") == len(re.findall(r"\{\{img:", source))
    assert out.stat().st_size < 5 * 1024 * 1024
    for anchor in ("about", "install", "clipboard", "selection", "files", "drop", "refill", "dict", "agents", "faq", "uninstall"):
        assert f'id="{anchor}"' in html and f'href="#{anchor}"' in html
    # Only inline assets: the manual must open offline from a DMG or chat attachment.
    assert not re.search(r'<(script|link|img)[^>]+(src|href)="https?://', html)
