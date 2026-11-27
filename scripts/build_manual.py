#!/usr/bin/env python3
"""Build the single-file user manual (docs/manual/Tuomin使用手册.html).

The source page references screenshots as ``{{img:name}}`` (docs/manual/img/
name.webp) and the logo as ``{{logo}}``; everything is inlined so the manual can
be sent alongside the DMG and opened offline. Regenerate the screenshots from a
running instance with synthetic data only.
"""
from __future__ import annotations

import base64
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MANUAL = ROOT / "docs" / "manual"
OUTPUT = MANUAL / "Tuomin使用手册.html"


def build(output: Path = OUTPUT, *, windows: bool = False) -> Path:
    source = (MANUAL / ("windows.src.html" if windows else "manual.src.html")).read_text(encoding="utf-8")
    logo = (ROOT / "packaging" / "icon" / "tuomin.svg").read_text(encoding="utf-8")
    logo = re.sub(r"<\?xml[^>]*>\s*", "", logo)
    logo = re.sub(r'\s(width|height)="\d+"', "", logo, count=2).replace("<svg ", '<svg aria-hidden="true" ', 1)

    def image(match: re.Match) -> str:
        path = MANUAL / "img" / f"{match.group(1)}.webp"
        if not path.is_file():
            raise SystemExit(f"missing screenshot: {path.relative_to(ROOT)}")
        return "data:image/webp;base64," + base64.b64encode(path.read_bytes()).decode()

    html = re.sub(r"\{\{img:([\w-]+)\}\}", image, source).replace("{{logo}}", logo)
    leftover = re.findall(r"\{\{[^}]+\}\}", html)
    if leftover:
        raise SystemExit(f"unresolved placeholders: {leftover}")
    output.write_text(html, encoding="utf-8")
    return output


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('output', nargs='?', type=Path, default=OUTPUT)
    parser.add_argument('--windows', action='store_true')
    args = parser.parse_args()
    target = build(args.output, windows=args.windows)
    print(f"{target}  {target.stat().st_size / 1024 / 1024:.1f} MB")
